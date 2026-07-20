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
        1: ("warning", None),
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
    discord.SelectOption(label="Level 1 - 1st occurrence", value="1:1", description="Verbal/private warning."),
    discord.SelectOption(label="Level 1 - 2nd occurrence", value="1:2", description="Default 2h timeout."),
    discord.SelectOption(label="Level 1 - 3rd occurrence", value="1:3", description="Default 24h timeout."),
    discord.SelectOption(label="Level 2 - 1st occurrence", value="2:1", description="Default 12h timeout."),
    discord.SelectOption(label="Level 2 - 2nd occurrence", value="2:2", description="Default 3 day timeout."),
    discord.SelectOption(label="Level 2 - 3rd occurrence", value="2:3", description="Default 7 day timeout."),
    discord.SelectOption(label="Level 3 - 1st occurrence", value="3:1", description="Default 14 day timeout."),
    discord.SelectOption(label="Level 3 - repeat", value="3:2", description="Permanent ban."),
    discord.SelectOption(label="Level 4 - single", value="4:1", description="Immediate permanent ban."),
]

DURATION_OPTIONS = [
    discord.SelectOption(label="Use default duration", value="default", description="Uses the matrix duration."),
    discord.SelectOption(label="30 minutes", value="30m"),
    discord.SelectOption(label="2 hours", value="2h"),
    discord.SelectOption(label="12 hours", value="12h"),
    discord.SelectOption(label="24 hours", value="24h"),
    discord.SelectOption(label="3 days", value="3d"),
    discord.SelectOption(label="7 days", value="7d"),
    discord.SelectOption(label="14 days", value="14d"),
    discord.SelectOption(label="Permanent", value="perma"),
]

PUNISHMENT_REASON_OPTIONS = [
    discord.SelectOption(label="No reason", value="__none__", description="Do not inform reason."),
    discord.SelectOption(label="Banter went too far", value="Banter went too far"),
    discord.SelectOption(label="Light spam", value="Light spam"),
    discord.SelectOption(label="Did not say they could not play", value="Did not say they could not play"),
    discord.SelectOption(label="Constant interruptions in call", value="Constant interruptions in call"),
    discord.SelectOption(label="Repeated disrespect", value="Repeated disrespect"),
    discord.SelectOption(label="Abandonment", value="Abandonment"),
    discord.SelectOption(label="Not following captain", value="Not following captain"),
    discord.SelectOption(label="Screaming in microphone", value="Screaming in microphone"),
    discord.SelectOption(label="Flood", value="Flood"),
    discord.SelectOption(label="Xenophobia", value="Xenophobia"),
    discord.SelectOption(label="Homophobia", value="Homophobia"),
    discord.SelectOption(label="Racism", value="Racism"),
    discord.SelectOption(label="Sexism", value="Sexism"),
    discord.SelectOption(label="Harassment", value="Harassment"),
    discord.SelectOption(label="Ghosting", value="Ghosting"),
    discord.SelectOption(label="Bugs/glitches", value="Bugs/glitches"),
    discord.SelectOption(label="Disrespect towards staff", value="Disrespect towards staff"),
    discord.SelectOption(label="Cheats", value="Cheats"),
    discord.SelectOption(label="Threats", value="Threats"),
    discord.SelectOption(label="Discrimination against women", value="Discrimination against women"),
    discord.SelectOption(label="NSFW content", value="NSFW content"),
    discord.SelectOption(label="Sexual harassment", value="Sexual harassment"),
    discord.SelectOption(label="Doxxing", value="Doxxing"),
    discord.SelectOption(label="Ban evasion", value="Ban evasion"),
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
    if text in ("perma", "permanent", "perm"):
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
        return "permanent"
    if seconds == 0:
        return "permanent"
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
        elif occ_raw in ("repeat", "recurrence"):
            occurrence = 2
        elif occ_raw in ("single", "unique"):
            occurrence = 1
    return level, occurrence


def parse_occurrence_input(raw: str, level: int) -> Optional[int]:
    text = (raw or "").strip().lower()
    if not text:
        return 1 if level in (1, 2, 3, 4) else None
    if text.isdigit():
        occurrence = int(text)
    elif text in ("repeat", "recurrence"):
        occurrence = 2
    elif text in ("single", "unique"):
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
        await interaction.followup.send("Invalid level. Use 1, 2, 3 or 4.", ephemeral=True)
        return None
    if occurrence is None or occurrence not in PUNISHMENT_MATRIX[level]:
        await interaction.followup.send("Invalid occurrence for this level.", ephemeral=True)
        return None

    action_type, base_duration = PUNISHMENT_MATRIX[level][occurrence]
    duration_seconds = base_duration

    if data["duration"] is not None:
        duration_seconds = data["duration"] if data["duration"] > 0 else None

    user_id = data["user_id"]
    report_id = data["report_id"]
    reason = data["reason"]

    if action_type == "timeout" and not duration_seconds:
        await interaction.followup.send("Timeout needs a valid duration.", ephemeral=True)
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
                await member.timeout(until, reason=reason or "Automatic punishment")
            except Exception as exc:
                logger.error(f"Failed to apply timeout: {exc}")
                await interaction.followup.send("Could not apply the mute on Discord.", ephemeral=True)
        if action_type == "ban":
            try:
                await interaction.guild.ban(member, reason=reason or "Permanent ban")
                await add_bot_ban(user_id, "discord", reason, interaction.user.id, None, report_id)
            except Exception as exc:
                logger.error(f"Failed to ban: {exc}")
                await interaction.followup.send("Could not ban the user.", ephemeral=True)
                return None
    elif action_type == "ban":
        try:
            await interaction.guild.ban(discord.Object(id=user_id), reason=reason or "Permanent ban")
            await add_bot_ban(user_id, "discord", reason, interaction.user.id, None, report_id)
        except Exception as exc:
            logger.error(f"Failed to ban by ID: {exc}")
            await interaction.followup.send("Could not ban the user.", ephemeral=True)
            return None

    channel = interaction.client.get_channel(CANAL_PUNICOES_ID) if CANAL_PUNICOES_ID else None
    if channel:
        if action_type == "warning":
            duration_text = "no duration"
        else:
            duration_text = format_duration(duration_seconds)
        embed = discord.Embed(
            title="Punishment applied",
            color=0xe67e22
        )
        embed.add_field(name="Player", value=f"<@{user_id}>", inline=True)
        embed.add_field(name="Level", value=str(level), inline=True)
        embed.add_field(name="Occurrence", value=str(occurrence), inline=True)
        embed.add_field(name="Punishment", value=action_type, inline=True)
        embed.add_field(name="Duration", value=duration_text, inline=True)
        if report_id:
            embed.add_field(name="Report ID", value=str(report_id), inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Applied by {interaction.user.display_name} | Record #{infraction_id}")
        await channel.send(embed=embed)

    await interaction.followup.send("Punishment registered.", ephemeral=True)
    return infraction_id


async def remove_punishment(interaction: discord.Interaction, user_id: int, reason: str = "") -> None:
    revoked = await revoke_bot_ban(user_id, interaction.user.id, reason)
    if revoked <= 0:
        try:
            await interaction.followup.send("No active record in the database. Removing timeout if it exists.", ephemeral=True)
        except Exception:
            pass
    member = interaction.guild.get_member(user_id)
    if member:
        try:
            await member.timeout(None, reason=reason or "Punishment removed")
        except Exception as exc:
            logger.error(f"Failed to remove timeout: {exc}")
    try:
        ban = await interaction.guild.fetch_ban(discord.Object(id=user_id))
        if ban:
            await interaction.guild.unban(ban.user, reason=reason or "Punishment removed")
    except Exception:
        pass
    await interaction.followup.send("Punishment removed.", ephemeral=True)


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


class ApplyPunishmentModal(Modal, title="Apply Punishment v3"):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

        self.level_occurrence_select = Select(
            placeholder="Select level and occurrence",
            min_values=1,
            max_values=1,
            options=LEVEL_OCCURRENCE_OPTIONS,
        )
        self.duration_select = Select(
            placeholder="Select duration",
            min_values=1,
            max_values=1,
            options=DURATION_OPTIONS,
        )
        self.reason_select = Select(
            placeholder="Select reason",
            min_values=1,
            max_values=1,
            options=PUNISHMENT_REASON_OPTIONS,
        )

        self.target_select = UserSelect(
            placeholder="Select the player",
            min_values=1,
            max_values=1,
        )
        self.add_item(Label(text="Player", component=self.target_select))
        self.add_item(Label(text="Level / occurrence", component=self.level_occurrence_select))
        self.add_item(Label(text="Duration", component=self.duration_select))
        self.add_item(Label(text="Reason", component=self.reason_select))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the opener can use this.", ephemeral=True)
            return

        if not self.target_select.values:
            await interaction.response.send_message("Select a player.", ephemeral=True)
            return
        target = self.target_select.values[0]
        user_id = int(target.id)

        if not self.level_occurrence_select.values:
            await interaction.response.send_message("Select level and occurrence.", ephemeral=True)
            return
        level_occurrence_raw = self.level_occurrence_select.values[0]
        try:
            level_str, occurrence_str = level_occurrence_raw.split(":", 1)
            level = int(level_str)
            occurrence = int(occurrence_str)
        except Exception:
            await interaction.response.send_message("Invalid level/occurrence option.", ephemeral=True)
            return

        if level not in PUNISHMENT_MATRIX or occurrence not in PUNISHMENT_MATRIX[level]:
            await interaction.response.send_message("Invalid occurrence for this level.", ephemeral=True)
            return

        if not self.duration_select.values:
            await interaction.response.send_message("Select the duration.", ephemeral=True)
            return
        duration_raw = self.duration_select.values[0]
        if duration_raw == "default":
            duration = None
        else:
            duration = parse_duration(duration_raw)
            if duration is None:
                await interaction.response.send_message("Invalid duration. Ex.: 30m, 2h, 3d, 7d, 14d or perma.", ephemeral=True)
                return

        if not self.reason_select.values:
            await interaction.response.send_message("Select the reason.", ephemeral=True)
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


class RemovePunishmentModal(Modal, title="Remove Punishment"):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

        self.target_input = TextInput(
            label="Player",
            placeholder="@user or Discord ID",
            required=True,
            max_length=64,
        )
        self.reason_input = TextInput(
            label="Reason",
            placeholder="optional reason",
            required=False,
            max_length=400,
            style=discord.TextStyle.paragraph,
        )

        self.add_item(self.target_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the opener can use this.", ephemeral=True)
            return

        user_id = parse_user_id(str(self.target_input.value))
        if not user_id:
            await interaction.response.send_message("Invalid player. Use @user or ID.", ephemeral=True)
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
            await interaction.response.send_message("Only the opener can use this.", ephemeral=True)
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
            await interaction.followup.send("Select player and level/occurrence.", ephemeral=True)
            return

        prompt = discord.Embed(
            title="Reason (optional)",
            description="Type the reason. For report_id: `reason | 123` or send `skip`.",
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
            await interaction.followup.send("Operation cancelled.", ephemeral=True)


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
            await interaction.response.send_message("Only the opener can use this.", ephemeral=True)
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
            await interaction.followup.send("Select the player.", ephemeral=True)
            return

        prompt = discord.Embed(
            title="Reason (optional)",
            description="Type the reason or send `skip`.",
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
                await interaction.followup.send("No active record in the database. Removing timeout if it exists.", ephemeral=True)
            except Exception:
                pass
        member = interaction.guild.get_member(self.user_id)
        if member:
            try:
                await member.timeout(None, reason=reason or "Punishment removed")
            except Exception as exc:
                logger.error(f"Failed to remove timeout: {exc}")
        try:
            ban = await interaction.guild.fetch_ban(discord.Object(id=self.user_id))
            if ban:
                await interaction.guild.unban(ban.user, reason=reason or "Punishment removed")
        except Exception:
            pass
        await interaction.followup.send("Punishment removed.", ephemeral=True)

    async def cancel(self, interaction: discord.Interaction):
        if self._reason_prompt:
            try:
                await self._reason_prompt.delete()
            except Exception:
                pass
        try:
            await interaction.message.delete()
        except Exception:
            await interaction.followup.send("Operation cancelled.", ephemeral=True)


class TargetSelect(UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select the player", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        target = self.values[0]
        if isinstance(view, ApplyPunishmentView):
            view.user_id = target.id
        if isinstance(view, RemovePunishmentView):
            view.user_id = target.id
        await interaction.response.send_message(f"Selected: {target.display_name}", ephemeral=True)


class LevelOccurrenceSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Level 1 - 1st occurrence", value="1-1"),
            discord.SelectOption(label="Level 1 - 2nd occurrence", value="1-2"),
            discord.SelectOption(label="Level 1 - 3rd occurrence", value="1-3"),
            discord.SelectOption(label="Level 2 - 1st occurrence", value="2-1"),
            discord.SelectOption(label="Level 2 - 2nd occurrence", value="2-2"),
            discord.SelectOption(label="Level 2 - 3rd occurrence", value="2-3"),
            discord.SelectOption(label="Level 3 - 1st occurrence", value="3-1"),
            discord.SelectOption(label="Level 3 - repeat", value="3-2"),
            discord.SelectOption(label="Level 4 - single", value="4-1")
        ]
        super().__init__(placeholder="Select level/occurrence", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        level_occ = parse_level_occurrence(self.values[0])
        if not level_occ:
            await interaction.response.send_message("Invalid level/occurrence.", ephemeral=True)
            return
        level, occurrence = level_occ
        if isinstance(view, ApplyPunishmentView):
            view.level = level
            view.occurrence = occurrence
        await interaction.response.send_message("Level/occurrence set.", ephemeral=True)


class DurationSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Use default duration", value="default"),
            discord.SelectOption(label="30m", value="30m"),
            discord.SelectOption(label="2h", value="2h"),
            discord.SelectOption(label="12h", value="12h"),
            discord.SelectOption(label="24h", value="24h"),
            discord.SelectOption(label="3d", value="3d"),
            discord.SelectOption(label="7d", value="7d"),
            discord.SelectOption(label="14d", value="14d"),
            discord.SelectOption(label="perma", value="perma")
        ]
        super().__init__(placeholder="Duration (optional)", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        raw = self.values[0]
        if raw == "default":
            duration = None
        else:
            duration = parse_duration(raw)
        if isinstance(view, ApplyPunishmentView):
            view.duration = duration
        await interaction.response.send_message("Duration selected.", ephemeral=True)


class ConfirmApplyButton(Button):
    def __init__(self):
        super().__init__(label="Confirm", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, ApplyPunishmentView):
            await interaction.response.defer(ephemeral=True)
            await view.confirm(interaction)


class CancelApplyButton(Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, ApplyPunishmentView):
            await interaction.response.defer(ephemeral=True)
            await view.cancel(interaction)


class ConfirmRemoveButton(Button):
    def __init__(self):
        super().__init__(label="Confirm", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, RemovePunishmentView):
            await interaction.response.defer(ephemeral=True)
            await view.confirm(interaction)


class CancelRemoveButton(Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

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

    @discord.ui.button(label="Apply punishment", style=discord.ButtonStyle.danger, custom_id="punicoes_apply")
    async def aplicar(self, interaction: discord.Interaction, button: Button):
        if not self._has_staff_access(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyPunishmentModal(interaction.user.id))

    @discord.ui.button(label="Remove punishment", style=discord.ButtonStyle.secondary, custom_id="punicoes_remove")
    async def remover(self, interaction: discord.Interaction, button: Button):
        if not self._has_staff_access(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.send_modal(RemovePunishmentModal(interaction.user.id))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        logger.error(f"PunicoesView on_error: {error}")
        try:
            await interaction.response.send_message("Internal error in the punishment panel.", ephemeral=True)
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
            title="**BRAZIL MIX | Official Punishment Board**",
            description="Server rules and guidelines. Use this board to analyze occurrences.",
            color=0xe67e22
        )
        embed.add_field(
            name="**Level 1 - Light infractions**",
            value=(
                "Examples: banter went too far, light spam, not saying you can't play, "
                "constant interruptions in call.\n"
                "1st occurrence: Verbal/private warning (internal record)\n"
                "2nd occurrence: Timeout 1-3h (depending on severity)\n"
                "3rd occurrence: Timeout 24h (final warning)"
            ),
            inline=False
        )
        embed.add_field(
            name="**Level 2 - Medium infractions**",
            value=(
                "Examples: repeated disrespect, abandonment, not following captain, screaming in microphone, flood.\n"
                "1st occurrence: Timeout 12h\n"
                "2nd occurrence: Timeout 3 days\n"
                "3rd occurrence: Timeout 7 days (ban warning next)"
            ),
            inline=False
        )
        embed.add_field(
            name="**Level 3 - Serious infractions**",
            value=(
                "Examples: xenophobia, homophobia, racism, sexism, harassment, ghosting, "
                "bugs/glitches, disrespect towards staff.\n"
                "1st occurrence: Timeout 14 days\n"
                "Repeat: Permanent ban"
            ),
            inline=False
        )
        embed.add_field(
            name="**Level 4 - Very serious infractions (zero tolerance)**",
            value=(
                "Examples: cheats, threats, discrimination against women, NSFW content, "
                "sexual harassment, doxxing, ban evasion.\n"
                "Single occurrence: Immediate permanent ban"
            ),
            inline=False
        )
        embed.add_field(
            name="**Points system (optional)**",
            value=(
                "Light (Level 1): 1 point - expires in 60 days\n"
                "Medium (Level 2): 3 points - expires in 60 days\n"
                "Accumulation: 5 points = Timeout 7 days\n"
                "After serving punishment: reset points"
            ),
            inline=False
        )
        embed.set_footer(text="**Staff: use this board as the official reference.**")
        return embed

    @app_commands.command(name="punish", description="Opens the quick form to apply a punishment.")
    async def punir(self, interaction: discord.Interaction):
        if not self._has_staff_access(interaction.user):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplyPunishmentModal(interaction.user.id))

    @app_commands.command(name="punishments_board", description="Shows the official rules and punishments board.")
    async def quadro_punicoes(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        await ctx.send(embed=self._build_quadro_punicoes_embed())


async def setup(bot: commands.Bot):
    await bot.add_cog(PunicoesCog(bot))
    logger.info(
        f"PunicoesCog loaded | discord.py={discord.__version__} | file={Path(__file__).resolve()}"
    )
