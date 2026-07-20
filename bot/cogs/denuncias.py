import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Select, Modal, TextInput
from typing import Awaitable, Callable, Dict, List
from loguru import logger

from bot.config import (
    CANAL_DENUNCIAS_ID, TICKET_CATEGORY_ID, TICKET_ARCHIVE_CATEGORY_ID,
    STAFF_ROLE_IDS
)
from bot.database import is_player_in_match, save_match_feedback, save_match_report

LIKE_OPTIONS = [
    "Boa comunicacao",
    "Teamplay",
    "Jogou bem"
]

DISLIKE_OPTIONS = [
    "Sem comunicacao",
    "Nao jogou em equipe",
    "AFK/Leaver"
]

REPORT_OPTIONS = [
    "Toxicidade",
    "Cheat",
    "AFK/Leaver",
    "Griefing",
    "Outro"
]


def _normalize_reason(text: str, options: List[str]) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    if value.isdigit():
        idx = int(value) - 1
        if 0 <= idx < len(options):
            return options[idx]
    lowered = value.lower()
    for opt in options:
        if lowered == opt.lower():
            return opt
    return ""


class FeedbackReasonSelect(Select):
    def __init__(
        self,
        match_id: int,
        target_id: int,
        target_name: str,
        vote_type: str,
        options: List[str]
    ):
        select_options = [
            discord.SelectOption(label=opt, value=opt)
            for opt in options
        ]
        super().__init__(
            placeholder="Selecione o motivo",
            min_values=1,
            max_values=1,
            options=select_options
        )
        self.match_id = match_id
        self.target_id = target_id
        self.target_name = target_name
        self.vote_type = vote_type

    async def callback(self, interaction: discord.Interaction):
        if not await is_player_in_match(self.match_id, interaction.user.id):
            await interaction.response.send_message(
                "Somente quem jogou a partida pode enviar feedback.",
                ephemeral=True
            )
            return
        if interaction.user.id == self.target_id:
            await interaction.response.send_message(
                "Voce nao pode avaliar a si mesmo.",
                ephemeral=True
            )
            return
        reason = self.values[0]
        saved = await save_match_feedback(
            self.match_id,
            interaction.user.id,
            self.target_id,
            self.vote_type,
            reason,
            None
        )
        if not saved:
            await interaction.response.send_message(
                f"Voce ja enviou feedback para {self.target_name} nesta partida.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Feedback registrado para {self.target_name}.",
            ephemeral=True
        )


class FeedbackReasonView(View):
    def __init__(
        self,
        match_id: int,
        target_id: int,
        target_name: str,
        vote_type: str,
        options: List[str]
    ):
        super().__init__(timeout=120)
        self.add_item(FeedbackReasonSelect(match_id, target_id, target_name, vote_type, options))


class ReportModal(Modal):
    def __init__(self, match_id: int, reporter_id: int, target_id: int, target_name: str):
        super().__init__(title="Denuncia de Jogador")
        self.match_id = match_id
        self.reporter_id = reporter_id
        self.target_id = target_id
        self.target_name = target_name

        options_text = "\n".join([f"{i + 1}) {opt}" for i, opt in enumerate(REPORT_OPTIONS)])
        self.reason = TextInput(
            label="Motivo (use numero ou texto)",
            placeholder=options_text,
            max_length=50
        )
        self.details = TextInput(
            label="Detalhes (texto livre)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000
        )
        self.add_item(self.reason)
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        if not await is_player_in_match(self.match_id, interaction.user.id):
            await interaction.response.send_message(
                "Somente quem jogou a partida pode denunciar.",
                ephemeral=True
            )
            return
        if interaction.user.id == self.target_id:
            await interaction.response.send_message(
                "Voce nao pode denunciar a si mesmo.",
                ephemeral=True
            )
            return
        reason = _normalize_reason(self.reason.value, REPORT_OPTIONS)
        if not reason:
            await interaction.response.send_message(
                "Motivo invalido. Use o numero ou o texto exato da lista.",
                ephemeral=True
            )
            return
        details = (self.details.value or "").strip()
        if reason == "Outro" and not details:
            await interaction.response.send_message(
                "Para 'Outro', descreva o motivo no campo de detalhes.",
                ephemeral=True
            )
            return
        saved = await save_match_report(
            self.match_id,
            interaction.user.id,
            self.target_id,
            reason,
            details
        )
        if not saved:
            await interaction.response.send_message(
                f"Voce ja denunciou {self.target_name} nesta partida.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Denuncia enviada contra {self.target_name}.",
            ephemeral=True
        )
        channel = interaction.client.get_channel(CANAL_DENUNCIAS_ID) if CANAL_DENUNCIAS_ID else None
        if channel:
            embed = discord.Embed(
                title="Nova denuncia",
                color=0xe74c3c
            )
            embed.add_field(name="Match", value=f"#{self.match_id}", inline=True)
            embed.add_field(name="Denunciante", value=interaction.user.mention, inline=True)
            embed.add_field(name="Denunciado", value=f"<@{self.target_id}>", inline=True)
            embed.add_field(name="Motivo", value=reason, inline=False)
            if details:
                embed.add_field(name="Detalhes", value=details, inline=False)
            await channel.send(embed=embed)


class MatchTargetSelect(Select):
    def __init__(self, players: List[Dict]):
        options = [
            discord.SelectOption(
                label=str(p["name"])[:100],
                value=str(p["discord_id"])
            )
            for p in players
        ]
        super().__init__(
            placeholder="Selecione o jogador",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        view: MatchFeedbackView = self.view
        target_id = int(self.values[0])
        view.user_selection[interaction.user.id] = target_id
        name = view.player_names.get(target_id, "Jogador")
        await interaction.response.send_message(
            f"Selecionado: {name}.",
            ephemeral=True
        )


class FeedbackButton(Button):
    def __init__(self, label: str, style: discord.ButtonStyle, vote_type: str):
        super().__init__(label=label, style=style)
        self.vote_type = vote_type

    async def callback(self, interaction: discord.Interaction):
        view: MatchFeedbackView = self.view
        target_id = view.user_selection.get(interaction.user.id)
        if not target_id:
            await interaction.response.send_message(
                "Selecione um jogador antes de enviar feedback.",
                ephemeral=True
            )
            return
        if not await is_player_in_match(view.match_id, interaction.user.id):
            await interaction.response.send_message(
                "Somente quem jogou a partida pode enviar feedback.",
                ephemeral=True
            )
            return
        target_name = view.player_names.get(target_id, "Jogador")
        options = LIKE_OPTIONS if self.vote_type == "like" else DISLIKE_OPTIONS
        reason_view = FeedbackReasonView(
            view.match_id,
            target_id,
            target_name,
            self.vote_type,
            options
        )
        await interaction.response.send_message(
            f"Escolha o motivo para {target_name}.",
            view=reason_view,
            ephemeral=True
        )


class ReportButton(Button):
    def __init__(self, label: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction):
        view: MatchFeedbackView = self.view
        target_id = view.user_selection.get(interaction.user.id)
        if not target_id:
            await interaction.response.send_message(
                "Selecione um jogador antes de denunciar.",
                ephemeral=True
            )
            return
        if not await is_player_in_match(view.match_id, interaction.user.id):
            await interaction.response.send_message(
                "Somente quem jogou a partida pode denunciar.",
                ephemeral=True
            )
            return
        target_name = view.player_names.get(target_id, "Jogador")
        await interaction.response.send_modal(
            ReportModal(
                view.match_id,
                interaction.user.id,
                target_id,
                target_name
            )
        )


class MatchFeedbackView(View):
    def __init__(self, match_id: int, players: List[Dict]):
        super().__init__(timeout=3600)
        self.match_id = match_id
        self.message = None
        self.user_selection: Dict[int, int] = {}
        self.player_names = {int(p["discord_id"]): p["name"] for p in players}
        self.add_item(MatchTargetSelect(players))
        self.add_item(FeedbackButton("Like", discord.ButtonStyle.success, "like"))
        self.add_item(FeedbackButton("Dislike", discord.ButtonStyle.secondary, "dislike"))
        self.add_item(ReportButton("Denunciar", discord.ButtonStyle.danger))

    async def on_timeout(self):
        if not self.message:
            return
        try:
            await self.message.edit(view=None)
        except:
            pass


class TicketOpenConfirmView(View):
    def __init__(
        self,
        kind: str,
        requester_id: int,
        open_handler: Callable[[discord.Interaction, str], Awaitable[None]]
    ):
        super().__init__(timeout=45)
        self.kind = kind
        self.requester_id = requester_id
        self.open_handler = open_handler

    @discord.ui.button(label="Confirmar abertura", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Esta confirmacao nao e para voce.",
                ephemeral=True
            )
            return
        await self.open_handler(interaction, self.kind)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Esta confirmacao nao e para voce.",
                ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content="Abertura cancelada.",
            embed=None,
            view=None
        )


class TicketPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _create_ticket_channel(self, interaction: discord.Interaction, kind: str):
        try:
            if not TICKET_CATEGORY_ID:
                await interaction.response.send_message(
                    "TICKET_CATEGORY_ID nao configurado.",
                    ephemeral=True
                )
                return
            category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
            if not category:
                await interaction.response.send_message(
                    "Categoria de tickets nao encontrada.",
                    ephemeral=True
                )
                return

            topic = f"{kind}:{interaction.user.id}"
            existing = None
            for ch in category.channels:
                if not isinstance(ch, discord.TextChannel):
                    continue
                if ch.topic == topic:
                    existing = ch
                    break
            if existing:
                await interaction.response.send_message(
                    f"Voce ja tem um canal aberto: {existing.mention}",
                    ephemeral=True
                )
                return

            # Evita "Interaction Failed" caso a criacao demore
            await interaction.response.defer(ephemeral=True)

            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False)
            }
            overwrites[interaction.user] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )
            for role_id in STAFF_ROLE_IDS:
                role = interaction.guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_channels=True
                    )

            safe_name = interaction.user.name.replace(" ", "-").lower()
            channel_name = f"{kind}-{safe_name}-{interaction.user.id}"
            try:
                channel = await interaction.guild.create_text_channel(
                    channel_name,
                    category=category,
                    overwrites=overwrites,
                    topic=topic
                )
            except Exception as exc:
                logger.error(f"Erro ao criar ticket: {exc}")
                await interaction.followup.send(
                    "Nao foi possivel criar o ticket. Verifique permissoes do bot.",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="Ticket aberto" if kind == "ticket" else "Denuncia aberta",
                description="Descreva o ocorrido com detalhes. A staff vai responder aqui.",
                color=0x3498db if kind == "ticket" else 0xe74c3c
            )
            await channel.send(content=interaction.user.mention, embed=embed)
            await ensure_ticket_control_message(channel, kind)
            await interaction.followup.send(
                f"Canal criado: {channel.mention}",
                ephemeral=True
            )
        except Exception as exc:
            logger.error(f"Erro no painel de tickets ({kind}): {exc}")
            try:
                await interaction.response.send_message(
                    "Erro interno ao abrir ticket. Avise a staff.",
                    ephemeral=True
                )
            except Exception:
                pass

    @discord.ui.button(label="Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: Button):
        try:
            view = TicketOpenConfirmView("ticket", interaction.user.id, self._create_ticket_channel)
            await interaction.response.send_message(
                "Confirma a abertura de um ticket privado com a staff?",
                view=view,
                ephemeral=True
            )
        except Exception as exc:
            logger.error(f"Erro no botao Ticket: {exc}")
            try:
                await interaction.response.send_message("Erro interno.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Denúncia", style=discord.ButtonStyle.danger, custom_id="ticket_report")
    async def open_report(self, interaction: discord.Interaction, button: Button):
        try:
            view = TicketOpenConfirmView("denuncia", interaction.user.id, self._create_ticket_channel)
            await interaction.response.send_message(
                "Confirma a abertura de uma denuncia privada com a staff?",
                view=view,
                ephemeral=True
            )
        except Exception as exc:
            logger.error(f"Erro no botao Denuncia: {exc}")
            try:
                await interaction.response.send_message("Erro interno.", ephemeral=True)
            except Exception:
                pass


class DenunciasCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="painel_denuncias", description="Envia o painel de tickets e denuncias.")
    async def painel_denuncias(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        embed = discord.Embed(
            title="Central de Tickets e Denuncias",
            description="Use os botoes abaixo para abrir um canal privado com a staff.",
            color=0x2ecc71
        )
        await ctx.send(embed=embed, view=TicketPanelView())


async def setup(bot: commands.Bot):
    await bot.add_cog(DenunciasCog(bot))


class CloseTicketView(View):
    def __init__(self, channel_id: int, kind: str):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.kind = kind
        label = "Encerrar Ticket" if kind == "ticket" else "Encerrar Denuncia"
        self.add_item(CloseTicketButton(label, channel_id, kind))


class CloseTicketButton(Button):
    def __init__(self, label: str, channel_id: int, kind: str):
        custom_id = f"ticket_close:{kind}:{channel_id}"
        super().__init__(label=label, style=discord.ButtonStyle.danger, custom_id=custom_id)
        self.channel_id = channel_id
        self.kind = kind

    async def callback(self, interaction: discord.Interaction):
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message("Canal invalido.", ephemeral=True)
            return
        if not _has_admin_role(interaction.user):
            await interaction.response.send_message("Sem permissao.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await close_ticket_channel(interaction.channel, self.kind)
        if ok:
            await interaction.followup.send("Ticket encerrado.", ephemeral=True)
        else:
            await interaction.followup.send("Falha ao encerrar ticket.", ephemeral=True)


def _has_admin_role(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if not STAFF_ROLE_IDS:
        return False
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


async def ensure_ticket_control_message(channel: discord.TextChannel, kind: str):
    if not channel:
        return
    pinned = []
    try:
        pinned = await channel.pins()
    except Exception:
        pinned = []
    bot_user = channel.guild.me
    for msg in pinned:
        if bot_user and msg.author.id == bot_user.id and msg.components:
            return
    embed = discord.Embed(
        title="Controle do Ticket" if kind == "ticket" else "Controle da Denuncia",
        description="Use o botao abaixo para encerrar e arquivar este canal.",
        color=0xe74c3c
    )
    view = CloseTicketView(channel.id, kind)
    msg = await channel.send(embed=embed, view=view)
    try:
        await msg.pin()
    except Exception:
        pass


async def close_ticket_channel(channel: discord.TextChannel, kind: str) -> bool:
    if not channel:
        return False
    archive_category = channel.guild.get_channel(TICKET_ARCHIVE_CATEGORY_ID) if TICKET_ARCHIVE_CATEGORY_ID else None
    if not archive_category:
        return False
    topic = channel.topic or ""
    requester_id = None
    if ":" in topic:
        parts = topic.split(":", 1)
        if len(parts) > 1 and parts[1].isdigit():
            requester_id = int(parts[1])
    overwrites = {
        channel.guild.default_role: discord.PermissionOverwrite(view_channel=False)
    }
    if requester_id:
        member = channel.guild.get_member(requester_id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=False)
    for role_id in STAFF_ROLE_IDS:
        role = channel.guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True
            )
    try:
        await channel.edit(category=archive_category, overwrites=overwrites, name=f"{kind}-arquivado-{channel.id}")
        return True
    except Exception:
        return False


async def ensure_open_ticket_controls(bot: commands.Bot):
    if not TICKET_CATEGORY_ID:
        return
    category = bot.get_channel(TICKET_CATEGORY_ID)
    if not category:
        return
    for channel in category.channels:
        if not isinstance(channel, discord.TextChannel):
            continue
        topic = channel.topic or ""
        if not topic or ":" not in topic:
            continue
        kind = topic.split(":", 1)[0]
        if kind not in ("ticket", "denuncia"):
            continue
        bot.add_view(CloseTicketView(channel.id, kind))
        try:
            await ensure_ticket_control_message(channel, kind)
        except Exception:
            pass
