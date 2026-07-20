import unicodedata

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, Modal, TextInput, View
from loguru import logger

from bot.config import (
    CANAL_AJUDA_ID,
    CANAL_STEAMID_ID,
    MEMBER_ROLE_ID,
    MEMBER_ROLE_NAME,
    SALA_PROXIMO_ID,
    VIP_ROLE_IDS,
)
from bot.database import (
    get_registered_player,
    has_complete_registration,
    is_nickname_in_use,
    register_player,
    update_player_nickname,
)
from bot.utils.steam_api import get_steam_profile, validate_steamid64


_EMOJI_COMPONENTS = {
    0x200D,
    0xFE0E,
    0xFE0F,
    0x20E3,
}

_EMOJI_RANGES = (
    (0x1F1E6, 0x1F1FF),
    (0x1F300, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
)


def _is_emoji_codepoint(cp: int) -> bool:
    if cp in _EMOJI_COMPONENTS:
        return True
    for start, end in _EMOJI_RANGES:
        if start <= cp <= end:
            return True
    return False


def _is_emoji_only_nickname(value: str) -> bool:
    compact = "".join(ch for ch in str(value or "") if not ch.isspace())
    if not compact:
        return False

    saw_emoji = False
    for ch in compact:
        cp = ord(ch)
        if _is_emoji_codepoint(cp):
            saw_emoji = True
            continue

        cat = unicodedata.category(ch)
        if cat:
            return False

    return saw_emoji


def _validate_nickname_input(nickname: str) -> str | None:
    nick = str(nickname or "").strip()
    if not nick:
        return "Nickname invalido."
    if len(nick) > 20:
        return "Nickname deve ter no maximo 20 caracteres."
    if _is_emoji_only_nickname(nick):
        return "Nickname nao pode ser apenas emoji."
    return None


def _get_member_role(guild: discord.Guild | None) -> discord.Role | None:
    if guild is None:
        return None

    role = guild.get_role(int(MEMBER_ROLE_ID or 0)) if int(MEMBER_ROLE_ID or 0) > 0 else None
    if role:
        return role

    if MEMBER_ROLE_NAME:
        return discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)

    return discord.utils.get(guild.roles, name="Membro")


def build_cadastro_panel_embed(guild: discord.Guild) -> discord.Embed:
    proximo_channel = guild.get_channel(SALA_PROXIMO_ID) if SALA_PROXIMO_ID else None
    ajuda_channel = guild.get_channel(CANAL_AJUDA_ID) if CANAL_AJUDA_ID else None

    proximo_text = f"<#{proximo_channel.id}>" if proximo_channel else "a sala de fila"
    ajuda_text = f"<#{ajuda_channel.id}>" if ajuda_channel else "o canal de ajuda"

    embed = discord.Embed(
        title="🛡️ Registro de Atleta",
        description=(
            "Bem-vindo ao sistema de registro do MixBot! "
            "Siga os passos abaixo para vincular sua conta e começar sua jornada competitiva.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x00FA9A,  # Vibrant Medium Spring Green
    )
    
    embed.add_field(
        name="✨ Como funciona?",
        value=(
            "1️⃣ Clique no botão **`Cadastrar`** abaixo.\n"
            "2️⃣ Informe seu **SteamID64** (17 dígitos).\n"
            "3️⃣ Defina seu **Nickname** único para o Mix.\n"
            "4️⃣ Confirme os dados da sua Steam."
        ),
        inline=False,
    )
    
    embed.add_field(
        name="📍 Onde ir depois?",
        value=f"Após o registro, entre em {proximo_text} para participar dos mixes automáticos.",
        inline=False,
    )

    embed.add_field(
        name="🚨 Avisos Importantes",
        value=(
            "• Use sua **conta principal** da Steam.\n"
            f"• Dúvidas ou suporte? Procure {ajuda_text}."
        ),
        inline=False,
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
        
    embed.set_footer(
        text="Nickname bloqueado após registro (exceto para VIPs)", 
        icon_url=guild.icon.url if guild.icon else None
    )
    
    return embed


def _build_confirmation_embed(steamid64: str, steam_nickname: str, nickname: str, avatar: str) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Confirme seus Dados",
        description="Quase lá! Verifique se as informações abaixo estão corretas antes de salvar seu perfil.",
        color=0x00BFFF,  # Deep Sky Blue
    )
    
    embed.add_field(
        name="👤 Perfil Steam", 
        value=f"**{steam_nickname}**", 
        inline=True
    )
    embed.add_field(
        name="🆔 SteamID64", 
        value=f"`{steamid64}`", 
        inline=True
    )
    
    embed.add_field(
        name="🎮 Nickname no Mix", 
        value=f"**{nickname}**", 
        inline=False
    )

    if avatar:
        embed.set_thumbnail(url=avatar)
        
    embed.set_footer(text="Ao confirmar, seu nickname será alterado automaticamente no servidor.")
    
    return embed


async def _send_ephemeral_fallback(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _finalize_registration(interaction: discord.Interaction, steamid64: str, nickname: str):
    discord_id = int(interaction.user.id)
    nick = str(nickname or "").strip()

    if await has_complete_registration(discord_id):
        await _send_ephemeral_fallback(interaction, "Voce ja concluiu o cadastro.")
        return
    if await is_nickname_in_use(nick, exclude_discord_id=discord_id):
        await _send_ephemeral_fallback(
            interaction,
            "Esse nickname ja esta em uso por outro player. Escolha outro e tente novamente.",
        )
        return

    row = await register_player(discord_id, str(steamid64 or "").strip(), nick)
    if not row:
        if await is_nickname_in_use(nick, exclude_discord_id=discord_id):
            await _send_ephemeral_fallback(
                interaction,
                "Esse nickname ja esta em uso por outro player. Escolha outro e tente novamente.",
            )
            return
        await _send_ephemeral_fallback(
            interaction,
            "Nao foi possivel concluir o cadastro. SteamID ou Discord ja cadastrados.",
        )
        return

    role = _get_member_role(interaction.guild)
    if role:
        try:
            await interaction.user.add_roles(role)
        except Exception as exc:
            logger.error(f"Erro ao adicionar cargo de membro: {exc}")

    nick_warning = ""
    if isinstance(interaction.user, discord.Member):
        guild = interaction.guild
        bot_member = guild.me if guild else None
        if not bot_member and guild and interaction.client.user:
            bot_member = guild.get_member(interaction.client.user.id)

        if guild and bot_member:
            is_owner = interaction.user.id == guild.owner_id
            can_manage_nicks = bool(bot_member.guild_permissions.manage_nicknames)
            has_hierarchy = bool(bot_member.top_role > interaction.user.top_role)
            if is_owner:
                nick_warning = (
                    "\n\nAviso: cadastro concluido, mas nao consigo alterar nickname do dono do servidor."
                )
            elif not can_manage_nicks:
                nick_warning = (
                    "\n\nAviso: cadastro concluido, mas o bot esta sem permissao 'Gerenciar apelidos'."
                )
            elif not has_hierarchy:
                nick_warning = (
                    "\n\nAviso: cadastro concluido, mas a hierarquia de cargos impede alterar seu nickname."
                )

        try:
            if not nick_warning:
                await interaction.user.edit(nick=nick, reason="Cadastro inicial MixBot")
            else:
                logger.warning(
                    "Cadastro sem alteracao de nickname: "
                    f"guild={getattr(guild, 'id', None)} user={interaction.user.id} "
                    f"owner={getattr(guild, 'owner_id', None) == interaction.user.id if guild else None} "
                    f"bot_manage_nicks={getattr(bot_member.guild_permissions, 'manage_nicknames', None) if bot_member else None} "
                    f"bot_top={getattr(getattr(bot_member, 'top_role', None), 'position', None)} "
                    f"user_top={getattr(getattr(interaction.user, 'top_role', None), 'position', None)}"
                )
        except discord.Forbidden as exc:
            nick_warning = (
                "\n\nAviso: cadastro concluido, mas nao consegui alterar seu nickname "
                "(verifique hierarquia/permissoes)."
            )
            logger.warning(
                "Nao foi possivel aplicar nickname no cadastro: "
                f"guild={getattr(guild, 'id', None)} user={interaction.user.id} "
                f"bot_manage_nicks={getattr(bot_member.guild_permissions, 'manage_nicknames', None) if bot_member else None} "
                f"bot_top={getattr(getattr(bot_member, 'top_role', None), 'position', None)} "
                f"user_top={getattr(getattr(interaction.user, 'top_role', None), 'position', None)} "
                f"owner={getattr(guild, 'owner_id', None) == interaction.user.id if guild else None} "
                f"erro={exc}"
            )
        except Exception as exc:
            nick_warning = (
                "\n\nAviso: cadastro concluido, mas nao consegui alterar seu nickname "
                "(verifique hierarquia/permissoes)."
            )
            logger.warning(f"Nao foi possivel aplicar nickname no cadastro para {interaction.user.id}: {exc}")

    await _send_ephemeral_fallback(interaction, f"Cadastro concluido com sucesso.{nick_warning}")


class CadastroModal(Modal, title="Cadastro Mix"):
    def __init__(self, cog: "SteamCog", discord_id: int, steamid64: str = "", nickname: str = ""):
        super().__init__()
        self.cog = cog
        self.discord_id = int(discord_id)
        self.steamid64 = TextInput(
            label="Informe seu SteamID64",
            placeholder="Ex.: 7656119...",
            max_length=20,
            default=str(steamid64 or "").strip(),
        )
        self.nickname = TextInput(
            label="Escolha seu nickname definitivo",
            placeholder="Defina com cuidado: depois do cadastro ele fica bloqueado",
            max_length=20,
            default=str(nickname or "").strip(),
        )
        self.add_item(self.steamid64)
        self.add_item(self.nickname)

    async def on_submit(self, interaction: discord.Interaction):
        if int(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("Este formulario nao e para voce.", ephemeral=True)
            return

        if await has_complete_registration(self.discord_id):
            await interaction.response.send_message("Voce ja concluiu o cadastro.", ephemeral=True)
            return

        steamid64 = str(self.steamid64.value or "").strip()
        nickname = str(self.nickname.value or "").strip()

        if not validate_steamid64(steamid64):
            await interaction.response.send_message(
                "SteamID invalido (deve comecar com 7656... e ter 17 numeros).",
                ephemeral=True,
            )
            return

        nick_error = _validate_nickname_input(nickname)
        if nick_error:
            await interaction.response.send_message(nick_error, ephemeral=True)
            return
        if await is_nickname_in_use(nickname, exclude_discord_id=self.discord_id):
            await interaction.response.send_message(
                "Esse nickname ja esta em uso por outro player. Escolha outro.",
                ephemeral=True,
            )
            return

        profile = await get_steam_profile(steamid64)
        if not profile:
            await interaction.response.send_message(
                "Conta nao encontrada na Steam ou perfil privado.",
                ephemeral=True,
            )
            return

        steam_nickname = str(profile.get("nickname") or "Steam User")
        avatar = str(profile.get("avatar") or "")
        embed = _build_confirmation_embed(steamid64, steam_nickname, nickname, avatar)
        view = CadastroConfirmView(
            cog=self.cog,
            discord_id=self.discord_id,
            steamid64=steamid64,
            steam_nickname=steam_nickname,
            nickname=nickname,
            avatar=avatar,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.opt(exception=error).error(f"Erro no modal de cadastro para {self.discord_id}")
        await _send_ephemeral_fallback(
            interaction,
            "Falha ao validar seu cadastro. Tente novamente pelo botao de cadastro.",
        )


class CadastroConfirmView(View):
    def __init__(
        self,
        cog: "SteamCog",
        discord_id: int,
        steamid64: str,
        steam_nickname: str,
        nickname: str,
        avatar: str,
    ):
        super().__init__(timeout=900)
        self.cog = cog
        self.discord_id = int(discord_id)
        self.steamid64 = str(steamid64 or "").strip()
        self.steam_nickname = str(steam_nickname or "Steam User")
        self.nickname = str(nickname or "").strip()
        self.avatar = str(avatar or "")

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.discord_id:
            return True
        await interaction.response.send_message("Este botao nao e para voce.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await _finalize_registration(interaction, self.steamid64, self.nickname)

    @discord.ui.button(label="Corrigir dados", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, button: Button):
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.send_modal(
            CadastroModal(self.cog, self.discord_id, self.steamid64, self.nickname)
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.edit_message(content="Cancelado.", embed=None, view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logger.opt(exception=error).error(
            f"Erro em CadastroConfirmView discord_id={self.discord_id} item={getattr(item, 'custom_id', None)}"
        )
        await _send_ephemeral_fallback(
            interaction,
            "Falha nesta acao do cadastro. Clique no botao de cadastro novamente.",
        )


class CadastroPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Cadastrar",
        style=discord.ButtonStyle.success,
        custom_id="steam_cadastro_open",
    )
    async def open_cadastro(self, interaction: discord.Interaction, button: Button):
        cog = interaction.client.get_cog("SteamCog")
        if cog is None:
            await interaction.response.send_message(
                "Cadastro indisponivel no momento. Tente novamente em instantes.",
                ephemeral=True,
            )
            return

        await cog.open_cadastro_modal(interaction)


class SteamCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def open_cadastro_modal(self, interaction: discord.Interaction):
        if int(interaction.channel_id or 0) != int(CANAL_STEAMID_ID or 0):
            await interaction.response.send_message(f"Use o cadastro em <#{CANAL_STEAMID_ID}>.", ephemeral=True)
            return

        if await has_complete_registration(interaction.user.id):
            await interaction.response.send_message("Voce ja concluiu o cadastro.", ephemeral=True)
            return

        await interaction.response.send_modal(CadastroModal(self, interaction.user.id))
        logger.info(f"Cadastro: {interaction.user.name} iniciou o fluxo de cadastro")

    @app_commands.command(name="cadastro", description="Abre manualmente o modal de cadastro.")
    async def cadastro(self, interaction: discord.Interaction):
        await self.open_cadastro_modal(interaction)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.bot:
            return
        if before.nick == after.nick:
            return

        player = await get_registered_player(after.id)
        if not player:
            return
        expected_nick = str(player.get("nickname") or "").strip()
        if not expected_nick:
            return
        new_nick = str(after.nick or "").strip()
        if new_nick == expected_nick:
            return

        vip_role_ids = {int(role_id) for role_id in VIP_ROLE_IDS if int(role_id) > 0}
        is_vip_nick = bool(
            vip_role_ids
            and any(int(role.id) in vip_role_ids for role in getattr(after, "roles", []))
        )

        if is_vip_nick:
            synced_nick = new_nick or str(after.display_name or "").strip()
            if not synced_nick:
                logger.warning(f"VIP sem nickname valido para sincronizar: user={after.id}")
                return
            if synced_nick == expected_nick:
                return
            try:
                if await is_nickname_in_use(synced_nick, exclude_discord_id=after.id):
                    logger.warning(
                        f"VIP tentou usar nickname ja existente: guild={after.guild.id} user={after.id} nick={synced_nick!r}"
                    )
                    await after.edit(nick=expected_nick, reason="Nickname duplicado no cadastro")
                    return
                updated = await update_player_nickname(after.id, synced_nick)
                if updated:
                    logger.info(
                        f"VIP nickname sincronizado no banco: guild={after.guild.id} user={after.id} nick={synced_nick!r}"
                    )
                else:
                    logger.warning(
                        f"VIP nao encontrado para sincronizar nickname: guild={after.guild.id} user={after.id}"
                    )
            except Exception as exc:
                logger.warning(f"Falha ao sincronizar nickname VIP de {after.id}: {exc}")
            return

        guild = after.guild
        bot_member = guild.me if guild else None
        if not bot_member and guild and self.bot.user:
            bot_member = guild.get_member(self.bot.user.id)

        if guild and bot_member:
            if after.id == guild.owner_id:
                logger.warning(f"Trava nickname ignorada para owner: {after.id}")
                return
            if not bot_member.guild_permissions.manage_nicknames:
                logger.warning(
                    f"Trava nickname sem permissao manage_nicknames: guild={guild.id} user={after.id}"
                )
                return
            if not (bot_member.top_role > after.top_role):
                logger.warning(
                    "Trava nickname bloqueada por hierarquia: "
                    f"guild={guild.id} user={after.id} bot_top={bot_member.top_role.position} user_top={after.top_role.position}"
                )
                return

        try:
            await after.edit(nick=expected_nick, reason="Trava de nickname do cadastro")
        except Exception as exc:
            logger.warning(f"Nao foi possivel reverter nickname de {after.id}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamCog(bot))
    logger.debug("SteamCog carregado")
