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
        return "Invalid nickname."
    if len(nick) > 20:
        return "Nickname must be at most 20 characters."
    if _is_emoji_only_nickname(nick):
        return "Nickname cannot be only emoji."
    return None


def _get_member_role(guild: discord.Guild | None) -> discord.Role | None:
    if guild is None:
        return None

    role = guild.get_role(int(MEMBER_ROLE_ID or 0)) if int(MEMBER_ROLE_ID or 0) > 0 else None
    if role:
        return role

    if MEMBER_ROLE_NAME:
        return discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)

    return discord.utils.get(guild.roles, name="Member")


def build_registration_panel_embed(guild: discord.Guild) -> discord.Embed:
    proximo_channel = guild.get_channel(SALA_PROXIMO_ID) if SALA_PROXIMO_ID else None
    ajuda_channel = guild.get_channel(CANAL_AJUDA_ID) if CANAL_AJUDA_ID else None

    proximo_text = f"<#{proximo_channel.id}>" if proximo_channel else "a sala de fila"
    ajuda_text = f"<#{ajuda_channel.id}>" if ajuda_channel else "o canal de ajuda"

    embed = discord.Embed(
        title="🛡️ Athlete Registration",
        description=(
            "Welcome to the MixBot registration system! "
            "Follow the steps below to link your account and start your competitive journey.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x00FA9A,  # Vibrant Medium Spring Green
    )
    
    embed.add_field(
        name="✨ How it works?",
        value=(
            "1️⃣ Click the **`Register`** button below.\n"
            "2️⃣ Enter your **SteamID64** (17 digits).\n"
            "3️⃣ Set your unique **Mix Nickname**.\n"
            "4️⃣ Confirm your Steam data."
        ),
        inline=False,
    )
    
    embed.add_field(
        name="📍 Where to go next?",
        value=f"After registration, join {proximo_text} to participate in automatic mixes.",
        inline=False,
    )

    embed.add_field(
        name="🚨 Important Notices",
        value=(
            "• Use your **main Steam account**.\n"
            f"• Questions or support? Ask in {ajuda_text}."
        ),
        inline=False,
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
        
    embed.set_footer(
        text="Nickname locked after registration (except for VIPs)", 
        icon_url=guild.icon.url if guild.icon else None
    )
    
    return embed


def _build_confirmation_embed(steamid64: str, steam_nickname: str, nickname: str, avatar: str) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Confirm Your Data",
        description="Almost there! Check that the information below is correct before saving your profile.",
        color=0x00BFFF,  # Deep Sky Blue
    )
    
    embed.add_field(
        name="👤 Steam Profile", 
        value=f"**{steam_nickname}**", 
        inline=True
    )
    embed.add_field(
        name="🆔 SteamID64", 
        value=f"`{steamid64}`", 
        inline=True
    )
    
    embed.add_field(
        name="🎮 Mix Nickname", 
        value=f"**{nickname}**", 
        inline=False
    )

    if avatar:
        embed.set_thumbnail(url=avatar)
        
    embed.set_footer(text="Upon confirmation, your nickname will be automatically changed on the server.")
    
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
        await _send_ephemeral_fallback(interaction, "You have already completed registration.")
        return
    if await is_nickname_in_use(nick, exclude_discord_id=discord_id):
        await _send_ephemeral_fallback(
            interaction,
            "This nickname is already in use by another player. Choose another and try again.",
        )
        return

    row = await register_player(discord_id, str(steamid64 or "").strip(), nick)
    if not row:
        if await is_nickname_in_use(nick, exclude_discord_id=discord_id):
            await _send_ephemeral_fallback(
                interaction,
                "This nickname is already in use by another player. Choose another and try again.",
            )
            return
        await _send_ephemeral_fallback(
            interaction,
            "Could not complete registration. SteamID or Discord already registered.",
        )
        return

    role = _get_member_role(interaction.guild)
    if role:
        try:
            await interaction.user.add_roles(role)
        except Exception as exc:
            logger.error(f"Error adding member role: {exc}")

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
                    "\n\nWarning: registration completed, but I cannot change the server owner's nickname."
                )
            elif not can_manage_nicks:
                nick_warning = (
                    "\n\nWarning: registration completed, but the bot lacks 'Manage Nicknames' permission."
                )
            elif not has_hierarchy:
                nick_warning = (
                    "\n\nWarning: registration completed, but the role hierarchy prevents changing your nickname."
                )

        try:
            if not nick_warning:
                await interaction.user.edit(nick=nick, reason="MixBot initial registration")
            else:
                logger.warning(
                    "Registration without nickname change: "
                    f"guild={getattr(guild, 'id', None)} user={interaction.user.id} "
                    f"owner={getattr(guild, 'owner_id', None) == interaction.user.id if guild else None} "
                    f"bot_manage_nicks={getattr(bot_member.guild_permissions, 'manage_nicknames', None) if bot_member else None} "
                    f"bot_top={getattr(getattr(bot_member, 'top_role', None), 'position', None)} "
                    f"user_top={getattr(getattr(interaction.user, 'top_role', None), 'position', None)}"
                )
        except discord.Forbidden as exc:
            nick_warning = (
                "\n\nWarning: registration completed, but I could not change your nickname "
                "(check hierarchy/permissions)."
            )
            logger.warning(
                "Could not apply nickname during registration: "
                f"guild={getattr(guild, 'id', None)} user={interaction.user.id} "
                f"bot_manage_nicks={getattr(bot_member.guild_permissions, 'manage_nicknames', None) if bot_member else None} "
                f"bot_top={getattr(getattr(bot_member, 'top_role', None), 'position', None)} "
                f"user_top={getattr(getattr(interaction.user, 'top_role', None), 'position', None)} "
                f"owner={getattr(guild, 'owner_id', None) == interaction.user.id if guild else None} "
                f"error={exc}"
            )
        except Exception as exc:
            nick_warning = (
                "\n\nWarning: registration completed, but I could not change your nickname "
                "(check hierarchy/permissions)."
            )
            logger.warning(f"Could not apply nickname during registration for {interaction.user.id}: {exc}")

    await _send_ephemeral_fallback(interaction, f"Registration completed successfully.{nick_warning}")


class RegistrationModal(Modal, title="Mix Registration"):
    def __init__(self, cog: "SteamCog", discord_id: int, steamid64: str = "", nickname: str = ""):
        super().__init__()
        self.cog = cog
        self.discord_id = int(discord_id)
        self.steamid64 = TextInput(
            label="Enter your SteamID64",
            placeholder="E.g.: 7656119...",
            max_length=20,
            default=str(steamid64 or "").strip(),
        )
        self.nickname = TextInput(
            label="Choose your permanent nickname",
            placeholder="Choose carefully: it gets locked after registration",
            max_length=20,
            default=str(nickname or "").strip(),
        )
        self.add_item(self.steamid64)
        self.add_item(self.nickname)

    async def on_submit(self, interaction: discord.Interaction):
        if int(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("This form is not for you.", ephemeral=True)
            return

        if await has_complete_registration(self.discord_id):
            await interaction.response.send_message("You have already completed registration.", ephemeral=True)
            return

        steamid64 = str(self.steamid64.value or "").strip()
        nickname = str(self.nickname.value or "").strip()

        if not validate_steamid64(steamid64):
            await interaction.response.send_message(
                "Invalid SteamID (must start with 7656... and have 17 digits).",
                ephemeral=True,
            )
            return

        nick_error = _validate_nickname_input(nickname)
        if nick_error:
            await interaction.response.send_message(nick_error, ephemeral=True)
            return
        if await is_nickname_in_use(nickname, exclude_discord_id=self.discord_id):
            await interaction.response.send_message(
                "This nickname is already in use by another player. Choose another.",
                ephemeral=True,
            )
            return

        profile = await get_steam_profile(steamid64)
        if not profile:
            await interaction.response.send_message(
                "Account not found on Steam or profile is private.",
                ephemeral=True,
            )
            return

        steam_nickname = str(profile.get("nickname") or "Steam User")
        avatar = str(profile.get("avatar") or "")
        embed = _build_confirmation_embed(steamid64, steam_nickname, nickname, avatar)
        view = RegistrationConfirmView(
            cog=self.cog,
            discord_id=self.discord_id,
            steamid64=steamid64,
            steam_nickname=steam_nickname,
            nickname=nickname,
            avatar=avatar,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.opt(exception=error).error(f"Error in registration modal for {self.discord_id}")
        await _send_ephemeral_fallback(
            interaction,
            "Failed to validate your registration. Try again using the registration button.",
        )


class RegistrationConfirmView(View):
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
        await interaction.response.send_message("This button is not for you.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await _finalize_registration(interaction, self.steamid64, self.nickname)

    @discord.ui.button(label="Fix data", style=discord.ButtonStyle.secondary)
    async def retry(self, interaction: discord.Interaction, button: Button):
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.send_modal(
            RegistrationModal(self.cog, self.discord_id, self.steamid64, self.nickname)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logger.opt(exception=error).error(
            f"Error in RegistrationConfirmView discord_id={self.discord_id} item={getattr(item, 'custom_id', None)}"
        )
        await _send_ephemeral_fallback(
            interaction,
            "Failed this registration action. Click the registration button again.",
        )


class RegistrationPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Register",
        style=discord.ButtonStyle.success,
        custom_id="steam_registration_open",
    )
    async def open_registration(self, interaction: discord.Interaction, button: Button):
        cog = interaction.client.get_cog("SteamCog")
        if cog is None:
            await interaction.response.send_message(
                "Registration is currently unavailable. Try again in a moment.",
                ephemeral=True,
            )
            return

        await cog.open_registration_modal(interaction)


class SteamCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def open_registration_modal(self, interaction: discord.Interaction):
        if int(interaction.channel_id or 0) != int(CANAL_STEAMID_ID or 0):
            await interaction.response.send_message(f"Use registration in <#{CANAL_STEAMID_ID}>.", ephemeral=True)
            return

        if await has_complete_registration(interaction.user.id):
            await interaction.response.send_message("You have already completed registration.", ephemeral=True)
            return

        await interaction.response.send_modal(RegistrationModal(self, interaction.user.id))
        logger.info(f"Registration: {interaction.user.name} started registration flow")

    @app_commands.command(name="register", description="Manually open the registration modal.")
    async def register(self, interaction: discord.Interaction):
        await self.open_registration_modal(interaction)

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
                logger.warning(f"VIP without valid nickname to sync: user={after.id}")
                return
            if synced_nick == expected_nick:
                return
            try:
                if await is_nickname_in_use(synced_nick, exclude_discord_id=after.id):
                    logger.warning(
                        f"VIP tried to use existing nickname: guild={after.guild.id} user={after.id} nick={synced_nick!r}"
                    )
                    await after.edit(nick=expected_nick, reason="Duplicate nickname in registration")
                    return
                updated = await update_player_nickname(after.id, synced_nick)
                if updated:
                    logger.info(
                        f"VIP nickname synced in database: guild={after.guild.id} user={after.id} nick={synced_nick!r}"
                    )
                else:
                    logger.warning(
                        f"VIP not found for nickname sync: guild={after.guild.id} user={after.id}"
                    )
            except Exception as exc:
                logger.warning(f"Failed to sync VIP nickname for {after.id}: {exc}")
            return

        guild = after.guild
        bot_member = guild.me if guild else None
        if not bot_member and guild and self.bot.user:
            bot_member = guild.get_member(self.bot.user.id)

        if guild and bot_member:
            if after.id == guild.owner_id:
                logger.warning(f"Nickname lock ignored for owner: {after.id}")
                return
            if not bot_member.guild_permissions.manage_nicknames:
                logger.warning(
                    f"Nickname lock without manage_nicknames permission: guild={guild.id} user={after.id}"
                )
                return
            if not (bot_member.top_role > after.top_role):
                logger.warning(
                    "Nickname lock blocked by hierarchy: "
                    f"guild={guild.id} user={after.id} bot_top={bot_member.top_role.position} user_top={after.top_role.position}"
                )
                return

        try:
            await after.edit(nick=expected_nick, reason="Registration nickname lock")
        except Exception as exc:
            logger.warning(f"Could not revert nickname for {after.id}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamCog(bot))
    logger.debug("SteamCog loaded")
