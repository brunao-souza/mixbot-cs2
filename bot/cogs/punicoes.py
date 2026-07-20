import re
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger
from discord.ui import View, Button, Select, UserSelect, Modal, TextInput, Label

from bot.config import CANAL_PUNICOES_ID, STAFF_ROLE_IDS
from bot.database import add_bot_ban, revoke_bot_ban, add_bot_infraction

PUNISHMENT_MATRIX = {
    1: {
        1: ("aviso", None),
        2: ("timeout", 2 * 3600),
        3: ("timeout", 24 * 3600)
    },
    2: {
        1: ("timeout", 12 * 3600),
        2: ("timeout", 3 * 86400),
        3: ("timeout", 7 * 86400)
    },
    3: {
        1: ("timeout", 14 * 86400),
        2: ("ban", None)
    },
    4: {
        1: ("ban", None)
    }
}

LEVEL_OCCURRENCE_OPTIONS = [
    discord.SelectOption(label="Nivel 1 - 1a ocorrencia", value="1:1", description="Aviso verbal/privado."),
    discord.SelectOption(label="Nivel 1 - 2a ocorrencia", value="1:2", description="Timeout padrao de 2h."),
    discord.SelectOption(label="Nivel 1 - 3a ocorrencia", value="1:3", description="Timeout padrao de 24h."),
    discord.SelectOption(label="Nivel 2 - 1a ocorrencia", value="2:1", description="Timeout padrao de 12h."),
    discord.SelectOption(label="Nivel 2 - 2a ocorrencia", value="2:2", description="Timeout padrao de 3 dias."),
    discord.SelectOption(label="Nivel 2 - 3a ocorrencia", value="2:3", description="Timeout padrao de 7 dias."),
    discord.SelectOption(label="Nivel 3 - 1a ocorrencia", value="3:1", description="Timeout padrao de 14 dias."),
    discord.SelectOption(label="Nivel 3 - reincidencia", value="3:2", description="Banimento permanente."),
    discord.SelectOption(label="Nivel 4 - unica", value="4:1", description="Banimento permanente imediato."),
]

DURATION_OPTIONS = [
    discord.SelectOption(label="Usar duracao padrao", value="default", description="Usa a duracao do quadro."),
    discord.SelectOption(label="30 minutos", value="30m"),
    discord.SelectOption(label="2 horas", value="2h"),
    discord.SelectOption(label="12 horas", value="12h"),
    discord.SelectOption(label="24 horas", value="24h"),
    discord.SelectOption(label="3 dias", value="3d"),
    discord.SelectOption(label="7 dias", value="7d"),
    discord.SelectOption(label="14 dias", value="14d"),
    discord.SelectOption(label="Permanente", value="perma"),
]

PUNISHMENT_REASON_OPTIONS = [
    discord.SelectOption(label="Sem motivo", value="__none__", description="Nao informar motivo."),
    discord.SelectOption(label="Zoacao passou do limite", value="Zoacao passou do limite"),
    discord.SelectOption(label="Spam leve", value="Spam leve"),
    discord.SelectOption(label="Nao avisou que nao podia jogar", value="Nao avisou que nao podia jogar"),
    discord.SelectOption(label="Interrupcoes constantes no call", value="Interrupcoes constantes no call"),
    discord.SelectOption(label="Desrespeito repetido", value="Desrespeito repetido"),
    discord.SelectOption(label="Abandono", value="Abandono"),
    discord.SelectOption(label="Nao seguir capitao", value="Nao seguir capitao"),
    discord.SelectOption(label="Gritar no microfone", value="Gritar no microfone"),
    discord.SelectOption(label="Flood", value="Flood"),
    discord.SelectOption(label="Xenofobia", value="Xenofobia"),
    discord.SelectOption(label="Homofobia", value="Homofobia"),
    discord.SelectOption(label="Racismo", value="Racismo"),
    discord.SelectOption(label="Machismo", value="Machismo"),
    discord.SelectOption(label="Assedio", value="Assedio"),
    discord.SelectOption(label="Ghosting", value="Ghosting"),
    discord.SelectOption(label="Bugs/glitches", value="Bugs/glitches"),
    discord.SelectOption(label="Desrespeito a staff", value="Desrespeito a staff"),
    discord.SelectOption(label="Cheats", value="Cheats"),
    discord.SelectOption(label="Ameacas", value="Ameacas"),
    discord.SelectOption(label="Discriminacao contra mulheres", value="Discriminacao contra mulheres"),
    discord.SelectOption(label="Conteudo NSFW", value="Conteudo NSFW"),
    discord.SelectOption(label="Assedio sexual", value="Assedio sexual"),
    discord.SelectOption(label="Doxxing", value="Doxxing"),
    discord.SelectOption(label="Evasao de ban", value="Evasao de ban"),
]


def parse_user_id(raw: str) -> Optional[int]:
    if not raw:
        return None
    raw = raw.strip()
    mention = re.match(r"<@!?(\d+)>", raw)
    if mention:
        return int(mention.group(1))
    if raw.isdigit():
        return int(raw)
    return None


def parse_duration(raw: str) -> Optional[int]:
    if not raw:
        return None
    text = raw.strip().lower()
    if text in ("perma", "permanente", "perm"):
        return 0
    match = re.match(r"^(\d+)\s*([smhd])$", text)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "permanente"
    if seconds == 0:
        return "permanente"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else f"{seconds}s"


def parse_level_occurrence(raw: str) -> Optional[tuple]:
    level_raw = (raw or "").strip().lower()
    parts = re.split(r"[\s\-_/]+", level_raw)
    if not parts or not parts[0].isdigit():
        return None
    level = int(parts[0])
    occurrence = None
    if len(parts) > 1:
        occ_raw = parts[1]
        if occ_raw.isdigit():
            occurrence = int(occ_raw)
        elif occ_raw in ("reincidencia", "recorrencia"):
            occurrence = 2
        elif occ_raw in ("unica", "unico"):
            occurrence = 1
    return level, occurrence


def parse_occurrence_input(raw: str, level: int) -> Optional[int]:
    text = (raw or "").strip().lower()
    if not text:
        return 1 if level in (1, 2, 3, 4) else None
    if text.isdigit():
        occurrence = int(text)
    elif text in ("reincidencia", "recorrencia"):
        occurrence = 2
    elif text in ("unica", "unico"):
        occurrence = 1
    else:
        return None
    if occurrence not in PUNISHMENT_MATRIX.get(level, {}):
        return None
    return occurrence


async def apply_punishment(interaction: discord.Interaction, data: dict) -> Optional[int]:
    level = data["level"]
    occurrence = data["occurrence"]
    if level not in PUNISHMENT_MATRIX:
        await interaction.followup.send("Nivel invalido. Use 1, 2, 3 ou 4.", ephemeral=True)
        return None
    if occurrence is None or occurrence not in PUNISHMENT_MATRIX[level]:
        await interaction.followup.send("Ocorrencia invalida para este nivel.", ephemeral=True)
        return None

    action_type, base_duration = PUNISHMENT_MATRIX[level][occurrence]
    duration_seconds = base_duration

    if data["duration"] is not None:
        duration_seconds = data["duration"] if data["duration"] > 0 else None

    user_id = data["user_id"]
    report_id = data["report_id"]
    reason = data["reason"]

    if action_type == "timeout" and not duration_seconds:
        await interaction.followup.send("Timeout precisa de duracao valida.", ephemeral=True)
        return None

    infraction_id = await add_bot_infraction(
        user_id,
        level,
        occurrence,
        action_type,
        duration_seconds if duration_seconds else None,
        reason,
        interaction.user.id,
        report_id
    )
    member = interaction.guild.get_member(user_id)
    if member:
        if action_type == "timeout" and duration_seconds:
            try:
                until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
                await member.timeout(until, reason=reason or "Punicao automatica")
            except Exception as exc:
                logger.error(f"Falha ao aplicar timeout: {exc}")
                await interaction.followup.send("Nao foi possivel aplicar o mute no Discord.", ephemeral=True)
        if action_type == "ban":
            try:
                await interaction.guild.ban(member, reason=reason or "Banimento permanente")
                await add_bot_ban(user_id, "discord", reason, interaction.user.id, None, report_id)
            except Exception as exc:
                logger.error(f"Falha ao banir: {exc}")
                await interaction.followup.send("Nao foi possivel banir o usuario.", ephemeral=True)
                return None
    elif action_type == "ban":
        try:
            await interaction.guild.ban(discord.Object(id=user_id), reason=reason or "Banimento permanente")
            await add_bot_ban(user_id, "discord", reason, interaction.user.id, None, report_id)
        except Exception as exc:
            logger.error(f"Falha ao banir por ID: {exc}")
            await interaction.followup.send("Nao foi possivel banir o usuario.", ephemeral=True)
            return None

    channel = interaction.client.get_channel(CANAL_PUNICOES_ID) if CANAL_PUNICOES_ID else None
    if channel:
        if action_type == "aviso":
            duration_text = "sem duracao"
        else:
            duration_text = format_duration(duration_seconds)
        embed = discord.Embed(
            title="Punicao aplicada",
            color=0xe67e22
        )
        embed.add_field(name="Jogador", value=f"<@{user_id}>", inline=True)
        embed.add_field(name="Nivel", value=str(level), inline=True)
        embed.add_field(name="Ocorrencia", value=str(occurrence), inline=True)
        embed.add_field(name="Punicao", value=action_type, inline=True)
        embed.add_field(name="Duracao", value=duration_text, inline=True)
        if report_id:
            embed.add_field(name="Denuncia ID", value=str(report_id), inline=True)
        if reason:
            embed.add_field(name="Motivo", value=reason, inline=False)
        embed.set_footer(text=f"Aplicado por {interaction.user.display_name} | Registro #{infraction_id}")
        await channel.send(embed=embed)

    await interaction.followup.send("Punicao registrada.", ephemeral=True)
    return infraction_id


async def remove_punishment(interaction: discord.Interaction, user_id: int, reason: str = "") -> None:
    revoked = await revoke_bot_ban(user_id, interaction.user.id, reason)
    if revoked <= 0:
        try:
            await interaction.followup.send("Nenhum registro ativo no banco. Removendo timeout se existir.", ephemeral=True)
        except Exception:
            pass
    member = interaction.guild.get_member(user_id)
    if member:
        try:
            await member.timeout(None, reason=reason or "Punicao removida")
        except Exception as exc:
            logger.error(f"Falha ao remover timeout: {exc}")
    try:
        ban = await interaction.guild.fetch_ban(discord.Object(id=user_id))
        if ban:
            await interaction.guild.unban(ban.user, reason=reason or "Punicao removida")
    except Exception:
        pass
    await interaction.followup.send("Punicao removida.", ephemeral=True)


def parse_reason_and_report(content: str) -> tuple:
    raw = (content or "").strip()
    if not raw:
        return "", None
    parts = raw.split("|", 1)
    motivo = parts[0].strip()
    report_id = None
    if len(parts) > 1:
        tail = parts[1].strip()
        if tail.isdigit():
            report_id = int(tail)
    return motivo, report_id


class ApplyPunishmentModal(Modal, title="Aplicar punicao v3"):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

        self.level_occurrence_select = Select(
            placeholder="Selecione nivel e ocorrencia",
            min_values=1,
            max_values=1,
            options=LEVEL_OCCURRENCE_OPTIONS,
        )
        self.duration_select = Select(
            placeholder="Selecione a duracao",
            min_values=1,
            max_values=1,
            options=DURATION_OPTIONS,
        )
        self.reason_select = Select(
            placeholder="Selecione o motivo",
            min_values=1,
            max_values=1,
            options=PUNISHMENT_REASON_OPTIONS,
        )

        self.target_select = UserSelect(
            placeholder="Selecione o jogador",
            min_values=1,
            max_values=1,
        )
        self.add_item(Label(text="Jogador", component=self.target_select))
        self.add_item(Label(text="Nivel / ocorrencia", component=self.level_occurrence_select))
        self.add_item(Label(text="Duracao", component=self.duration_select))
        self.add_item(Label(text="Motivo", component=self.reason_select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Apenas quem abriu pode usar.", ephemeral=True)
            return

        if not self.target_select.values:
            await interaction.response.send_message("Selecione um jogador.", ephemeral=True)
            return
        target = self.target_select.values[0]
        user_id = int(target.id)

        if not self.level_occurrence_select.values:
            await interaction.response.send_message("Selecione nivel e ocorrencia.", ephemeral=True)
            return
        level_occurrence_raw = self.level_occurrence_select.values[0]
        try:
            level_str, occurrence_str = level_occurrence_raw.split(":", 1)
            level = int(level_str)
            occurrence = int(occurrence_str)
        except Exception:
            await interaction.response.send_message("Opcao de nivel/ocorrencia invalida.", ephemeral=True)
            return

        if level not in PUNISHMENT_MATRIX or occurrence not in PUNISHMENT_MATRIX[level]:
            await interaction.response.send_message("Ocorrencia invalida para esse nivel.", ephemeral=True)
            return

        if not self.duration_select.values:
            await interaction.response.send_message("Selecione a duracao.", ephemeral=True)
            return
        duration_raw = self.duration_select.values[0]
        if duration_raw == "default":
            duration = None
        else:
            duration = parse_duration(duration_raw)
            if duration is None:
                await interaction.response.send_message("Duracao invalida. Ex.: 30m, 2h, 3d, 7d, 14d ou perma.", ephemeral=True)
                return

        if not self.reason_select.values:
            await interaction.response.send_message("Selecione o motivo.", ephemeral=True)
            return
        reason_raw = self.reason_select.values[0]
        reason = "" if reason_raw == "__none__" else reason_raw
        report_id = None

        await interaction.response.defer(ephemeral=True)
        await apply_punishment(
            interaction,
            {
                "user_id": user_id,
                "level": level,
                "occurrence": occurrence,
                "duration": duration,
                "report_id": report_id,
                "reason": reason,
            },
        )


class RemovePunishmentModal(Modal, title="Remover punicao"):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

        self.target_input = TextInput(
            label="Jogador",
            placeholder="@usuario ou ID do Discord",
            required=True,
            max_length=64,
        )
        self.reason_input = TextInput(
            label="Motivo",
            placeholder="motivo opcional",
            required=False,
            max_length=400,
            style=discord.TextStyle.paragraph,
        )

        self.add_item(self.target_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Apenas quem abriu pode usar.", ephemeral=True)
            return

        user_id = parse_user_id(str(self.target_input.value))
        if not user_id:
            await interaction.response.send_message("Jogador invalido. Use @usuario ou ID.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await remove_punishment(interaction, user_id, str(self.reason_input.value or "").strip())


class ApplyPunishmentView(View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.user_id: Optional[int] = None
        self.level: Optional[int] = None
        self.occurrence: Optional[int] = None
        self.duration: Optional[int] = None
        self._reason_prompt = None

        self.add_item(TargetSelect())
        self.add_item(LevelOccurrenceSelect())
        self.add_item(DurationSelect())
        self.add_item(ConfirmApplyButton())
        self.add_item(CancelApplyButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Apenas quem abriu pode usar.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self._reason_prompt:
            try:
                await self._reason_prompt.delete()
            except:
                pass

    async def confirm(self, interaction: discord.Interaction):
        if not self.user_id or not self.level or not self.occurrence:
            await interaction.followup.send("Selecione jogador e nivel/ocorrencia.", ephemeral=True)
            return

        prompt = discord.Embed(
            title="Motivo (opcional)",
            description="Digite o motivo. Para denuncia_id: `motivo | 123` ou envie `skip`.",
            color=0x3498db
        )
        self._reason_prompt = await interaction.channel.send(embed=prompt)

        def check(msg: discord.Message) -> bool:
            return msg.author.id == interaction.user.id and msg.channel.id == interaction.channel.id

        reason = ""
        report_id = None
        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=120)
            if msg.content.strip().lower() != "skip":
                reason, report_id = parse_reason_and_report(msg.content)
        except asyncio.TimeoutError:
            pass

        data = {
            "user_id": self.user_id,
            "level": self.level,
            "occurrence": self.occurrence,
            "duration": self.duration,
            "report_id": report_id,
            "reason": reason
        }
        await apply_punishment(interaction, data)

    async def cancel(self, interaction: discord.Interaction):
        if self._reason_prompt:
            try:
                await self._reason_prompt.delete()
            except Exception:
                pass
        try:
            await interaction.message.delete()
        except Exception:
            await interaction.followup.send("Operacao cancelada.", ephemeral=True)


class RemovePunishmentView(View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.user_id: Optional[int] = None
        self._reason_prompt = None
        self.add_item(TargetSelect())
        self.add_item(ConfirmRemoveButton())
        self.add_item(CancelRemoveButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Apenas quem abriu pode usar.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self._reason_prompt:
            try:
                await self._reason_prompt.delete()
            except:
                pass

    async def confirm(self, interaction: discord.Interaction):
        if not self.user_id:
            await interaction.followup.send("Selecione o jogador.", ephemeral=True)
            return

        prompt = discord.Embed(
            title="Motivo (opcional)",
            description="Digite o motivo ou envie `skip`.",
            color=0x3498db
        )
        self._reason_prompt = await interaction.channel.send(embed=prompt)

        def check(msg: discord.Message) -> bool:
            return msg.author.id == interaction.user.id and msg.channel.id == interaction.channel.id

        reason = ""
        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=120)
            if msg.content.strip().lower() != "skip":
                reason = msg.content.strip()
        except asyncio.TimeoutError:
            pass

        revoked = await revoke_bot_ban(self.user_id, interaction.user.id, reason)
        if revoked <= 0:
            try:
                await interaction.followup.send("Nenhum registro ativo no banco. Removendo timeout se existir.", ephemeral=True)
            except Exception:
                pass
        member = interaction.guild.get_member(self.user_id)
        if member:
            try:
                await member.timeout(None, reason=reason or "Punicao removida")
            except Exception as exc:
                logger.error(f"Falha ao remover timeout: {exc}")
        try:
            ban = await interaction.guild.fetch_ban(discord.Object(id=self.user_id))
            if ban:
                await interaction.guild.unban(ban.user, reason=reason or "Punicao removida")
        except Exception:
            pass
        await interaction.followup.send("Punicao removida.", ephemeral=True)

    async def cancel(self, interaction: discord.Interaction):
        if self._reason_prompt:
            try:
                await self._reason_prompt.delete()
            except Exception:
                pass
        try:
            await interaction.message.delete()
        except Exception:
            await interaction.followup.send("Operacao cancelada.", ephemeral=True)


class TargetSelect(UserSelect):
    def __init__(self):
        super().__init__(placeholder="Selecione o jogador", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        target = self.values[0]
        if isinstance(view, ApplyPunishmentView):
            view.user_id = target.id
        if isinstance(view, RemovePunishmentView):
            view.user_id = target.id
        await interaction.response.send_message(f"Selecionado: {target.display_name}", ephemeral=True)


class LevelOccurrenceSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Nivel 1 - 1ª ocorrencia", value="1-1"),
            discord.SelectOption(label="Nivel 1 - 2ª ocorrencia", value="1-2"),
            discord.SelectOption(label="Nivel 1 - 3ª ocorrencia", value="1-3"),
            discord.SelectOption(label="Nivel 2 - 1ª ocorrencia", value="2-1"),
            discord.SelectOption(label="Nivel 2 - 2ª ocorrencia", value="2-2"),
            discord.SelectOption(label="Nivel 2 - 3ª ocorrencia", value="2-3"),
            discord.SelectOption(label="Nivel 3 - 1ª ocorrencia", value="3-1"),
            discord.SelectOption(label="Nivel 3 - reincidencia", value="3-2"),
            discord.SelectOption(label="Nivel 4 - unica", value="4-1")
        ]
        super().__init__(placeholder="Selecione nivel/ocorrencia", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        level_occ = parse_level_occurrence(self.values[0])
        if not level_occ:
            await interaction.response.send_message("Nivel/ocorrencia invalido.", ephemeral=True)
            return
        level, occurrence = level_occ
        if isinstance(view, ApplyPunishmentView):
            view.level = level
            view.occurrence = occurrence
        await interaction.response.send_message("Nivel/ocorrencia definido.", ephemeral=True)


class DurationSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Usar duracao padrao", value="padrao"),
            discord.SelectOption(label="30m", value="30m"),
            discord.SelectOption(label="2h", value="2h"),
            discord.SelectOption(label="12h", value="12h"),
            discord.SelectOption(label="24h", value="24h"),
            discord.SelectOption(label="3d", value="3d"),
            discord.SelectOption(label="7d", value="7d"),
            discord.SelectOption(label="14d", value="14d"),
            discord.SelectOption(label="perma", value="perma")
        ]
        super().__init__(placeholder="Duracao (opcional)", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        raw = self.values[0]
        if raw == "padrao":
            duration = None
        else:
            duration = parse_duration(raw)
        if isinstance(view, ApplyPunishmentView):
            view.duration = duration
        await interaction.response.send_message("Duracao selecionada.", ephemeral=True)


class ConfirmApplyButton(Button):
    def __init__(self):
        super().__init__(label="Confirmar", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, ApplyPunishmentView):
            await interaction.response.defer(ephemeral=True)
            await view.confirm(interaction)


class CancelApplyButton(Button):
    def __init__(self):
        super().__init__(label="Cancelar", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, ApplyPunishmentView):
            await interaction.response.defer(ephemeral=True)
            await view.cancel(interaction)


class ConfirmRemoveButton(Button):
    def __init__(self):
        super().__init__(label="Confirmar", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, RemovePunishmentView):
            await interaction.response.defer(ephemeral=True)
            await view.confirm(interaction)


class CancelRemoveButton(Button):
    def __init__(self):
        super().__init__(label="Cancelar", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, RemovePunishmentView):
            await interaction.response.defer(ephemeral=True)
            await view.cancel(interaction)


class PunicoesView(View):
    def __init__(self):
        super().__init__(timeout=None)

    def _has_staff_access(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        perms = getattr(member, "guild_permissions", None)
        if perms and perms.administrator:
            return True
        if not member or not STAFF_ROLE_IDS:
            return False
        return any(role.id in STAFF_ROLE_IDS for role in member.roles)

    @discord.ui.button(label="Aplicar punicao", style=discord.ButtonStyle.danger, custom_id="punicoes_apply")
    async def aplicar(self, interaction: discord.Interaction, button: Button):
        if not self._has_staff_access(interaction):
            await interaction.response.send_message("Sem permissao.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyPunishmentModal(interaction.user.id))

    @discord.ui.button(label="Remover punicao", style=discord.ButtonStyle.secondary, custom_id="punicoes_remove")
    async def remover(self, interaction: discord.Interaction, button: Button):
        if not self._has_staff_access(interaction):
            await interaction.response.send_message("Sem permissao.", ephemeral=True)
            return
        await interaction.response.send_modal(RemovePunishmentModal(interaction.user.id))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        logger.error(f"PunicoesView on_error: {error}")
        try:
            await interaction.response.send_message("Erro interno no painel de punicoes.", ephemeral=True)
        except Exception:
            pass


class PunicoesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _has_staff_access(self, member: discord.abc.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        perms = getattr(member, "guild_permissions", None)
        if perms and perms.administrator:
            return True
        if not STAFF_ROLE_IDS:
            return False
        return any(role.id in STAFF_ROLE_IDS for role in member.roles)

    def _build_quadro_punicoes_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="**É DO BRASIL MIX | Quadro Oficial de Punições**",
            description="Regras e normas do servidor. Use este quadro para analisar ocorrencias.",
            color=0xe67e22
        )
        embed.add_field(
            name="**Nivel 1 - Infracoes leves**",
            value=(
                "Exemplos: zoacao passou do limite, spam leve, nao avisar que nao pode jogar, "
                "interrupcoes constantes no call.\n"
                "1º ocorrencia: Aviso verbal/privado (registro interno)\n"
                "2º ocorrencia: Timeout 1-3h (conforme gravidade)\n"
                "3º ocorrencia: Timeout 24h (aviso final)"
            ),
            inline=False
        )
        embed.add_field(
            name="**Nivel 2 - Infracoes medias**",
            value=(
                "Exemplos: desrespeito repetido, abandono, nao seguir capitao, gritar no microfone, flood.\n"
                "1º ocorrencia: Timeout 12h\n"
                "2º ocorrencia: Timeout 3 dias\n"
                "3º ocorrencia: Timeout 7 dias (aviso de ban na proxima)"
            ),
            inline=False
        )
        embed.add_field(
            name="**Nivel 3 - Infracoes graves**",
            value=(
                "Exemplos: xenofobia, homofobia, racismo, machismo, assedio, ghosting, "
                "bugs/glitches, desrespeito a staff.\n"
                "1º ocorrencia: Timeout 14 dias\n"
                "Reincidencia: Banimento permanente"
            ),
            inline=False
        )
        embed.add_field(
            name="**Nivel 4 - Infracoes gravissimas (tolerancia zero)**",
            value=(
                "Exemplos: cheats, ameacas, discriminacao contra mulheres, conteudo NSFW, "
                "assedio sexual, doxxing, evasao de ban.\n"
                "Ocorrencia unica: Banimento permanente imediato"
            ),
            inline=False
        )
        embed.add_field(
            name="**Sistema de pontos (opcional)**",
            value=(
                "Leve (Nivel 1): 1 ponto - expira em 60 dias\n"
                "Media (Nivel 2): 3 pontos - expira em 60 dias\n"
                "Acumulo: 5 pontos = Timeout 7 dias\n"
                "Apos cumprir punicao: zerar pontos"
            ),
            inline=False
        )
        embed.set_footer(text="**Staff: use este quadro como referencia oficial.**")
        return embed

    @app_commands.command(name="punir", description="Abre o formulario rapido para aplicar punicao.")
    async def punir(self, interaction: discord.Interaction):
        if not self._has_staff_access(interaction.user):
            await interaction.response.send_message("Sem permissao.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyPunishmentModal(interaction.user.id))

    @app_commands.command(name="quadro_punicoes", description="Mostra o quadro oficial de regras e punicoes.")
    async def quadro_punicoes(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.send(embed=self._build_quadro_punicoes_embed())


async def setup(bot: commands.Bot):
    await bot.add_cog(PunicoesCog(bot))
    logger.info(
        f"PunicoesCog carregado | discord.py={discord.__version__} | file={Path(__file__).resolve()}"
    )
