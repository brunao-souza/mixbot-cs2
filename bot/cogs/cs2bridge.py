import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Literal

import discord
from aiohttp import web
from discord.ext import commands
from loguru import logger

from bot.config import (
    CANAL_FILA_ID,
    CS2_SHARED_KEY,
    CS2_BRIDGE_INBOX_CHANNEL_ID,
    DISCORD_ADMIN_ALERT_CHANNEL_ID,
    DISCORD_CALLADMIN_ROLE_ID,
    DISCORD_CHAT_RELAY_CHANNEL_ID,
    DISCORD_COMPLETER_ALERT_CHANNEL_ID,
    DISCORD_COMPLETER_ROLE_ID,
    RCON_HOST,
    RCON_PASSWORD,
    RCON_PORT,
    SALA_SAIDA_ID,
    SERVERS,
    STAFF_ROLE_IDS,
    TOURNAMENT_SERVERS,
)
from bot.database import (
    attach_matchguardian_log_message,
    claim_matchguardian_completer_request,
    close_matchguardian_log,
    create_matchguardian_log,
    get_matchguardian_completer_request,
    get_player_rank,
    get_player_team_in_match,
    save_matchguardian_completer_request,
)
from bot.utils.cs2 import send_rcon

AlertKind = Literal["admin", "completer"]

ADMIN_ALERT_KEYWORD = "admin request"
COMPLETER_ALERT_KEYWORD = "substitute request"
REPLY_BUTTON_LABEL = "Reply in game"
OPEN_CHAT_LABEL = "Open chat"
PAUSE_LABEL = "Pause"
UNPAUSE_LABEL = "Unpause"
END_CALL_LABEL = "Close"
ACCEPT_BUTTON_LABEL = "Accept substitution"
REPLY_BUTTON_STYLE = discord.ButtonStyle.primary
OPEN_CHAT_STYLE = discord.ButtonStyle.secondary
PAUSE_BUTTON_STYLE = discord.ButtonStyle.secondary
END_CALL_STYLE = discord.ButtonStyle.danger
ACCEPT_BUTTON_STYLE = discord.ButtonStyle.success
REPLY_MAX_CHARS = 220
RATE_LIMIT_SECONDS = 3.0
CHAT_SESSION_TTL_SECONDS = 45 * 60
OUTBOX_MAX_PER_ALIAS = 80
OUTBOX_MAX_PULL = 10
INBOX_ALERT_DEDUP_SECONDS = 12.0
CUSTOM_ID_PREFIX = "cs2bridge:reply:"
OPEN_CHAT_CUSTOM_ID = "cs2bridge:openchat:admin"
ADMIN_PAUSE_CUSTOM_ID = "cs2bridge:pause:admin"
ADMIN_CLOSE_CUSTOM_ID = "cs2bridge:close:admin"
ACCEPT_CUSTOM_ID = "cs2bridge:accept:completer"
# Patterns used to parse alert messages from MatchZy plugin
SERVER_ID_PATTERN = re.compile(r"^\s*Server:\s*`([^`]+)`", re.IGNORECASE | re.MULTILINE)
MATCH_ID_PATTERN = re.compile(r"^\s*Match:\s*`([^`]+)`", re.IGNORECASE | re.MULTILINE)
TEAM_PATTERN = re.compile(r"^\s*Team:\s*\*?\*?(.+?)\*?\*?\s*$", re.IGNORECASE | re.MULTILINE)
ABANDONED_STEAMID_PATTERN = re.compile(r"abandon\w*.*?\(`(\d{16,20})`\)", re.IGNORECASE)
ABANDONED_NAME_PATTERN = re.compile(
    r"player who abandoned:\s*\*\*(.+?)\*\*\s*\(`\d{16,20}`\)",
    re.IGNORECASE,
)
ROLE_MENTION_PATTERN = re.compile(r"<@&\d+>")
META_PREFIX = "cs2bridge-meta:"
CALL_ID_PATTERN = re.compile(r"#(\d+)")
CONNECT_CMD_PATTERN = re.compile(
    r"connect\s+[^\s`]+(?:\s*;\s*password\s+[^\s`]+)?",
    re.IGNORECASE,
)


def _one_line(value: str, max_len: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""
    text = " ".join(text.split())
    return text[:max_len]


def _sanitize_rcon_message(raw: str) -> str:
    one_line = _one_line(raw, REPLY_MAX_CHARS)
    return one_line.replace('"', r"\"")


class CS2ReplyModal(discord.ui.Modal, title=REPLY_BUTTON_LABEL):
    reply_text = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.short,
        max_length=REPLY_MAX_CHARS,
        required=True,
        placeholder="Write a short reply for the in-game chat.",
    )

    def __init__(self, cog: "CS2BridgeCog", alert_kind: AlertKind, server_id: str):
        super().__init__()
        self.cog = cog
        self.alert_kind = alert_kind
        self.server_id = server_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_modal_submit(
            interaction,
            self.alert_kind,
            str(self.reply_text.value),
            self.server_id,
        )


class CS2ReplyButton(discord.ui.Button):
    def __init__(self, cog: "CS2BridgeCog", alert_kind: AlertKind):
        super().__init__(
            label=REPLY_BUTTON_LABEL,
            style=REPLY_BUTTON_STYLE,
            custom_id=f"{CUSTOM_ID_PREFIX}{alert_kind}",
        )
        self.cog = cog
        self.alert_kind = alert_kind

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_button_click(interaction, self.alert_kind)


class CS2OpenChatButton(discord.ui.Button):
    def __init__(self, cog: "CS2BridgeCog"):
        super().__init__(
            label=OPEN_CHAT_LABEL,
            style=OPEN_CHAT_STYLE,
            custom_id=OPEN_CHAT_CUSTOM_ID,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_open_chat_click(interaction)


class CS2ReplyView(discord.ui.View):
    def __init__(self, cog: "CS2BridgeCog", alert_kind: AlertKind):
        super().__init__(timeout=None)
        if alert_kind != "admin":
            self.add_item(CS2ReplyButton(cog, alert_kind))


class CS2AdminPauseButton(discord.ui.Button):
    def __init__(self, cog: "CS2BridgeCog", paused: bool = False, disabled: bool = False):
        super().__init__(
            label=UNPAUSE_LABEL if paused else PAUSE_LABEL,
            style=PAUSE_BUTTON_STYLE,
            custom_id=ADMIN_PAUSE_CUSTOM_ID,
            disabled=disabled,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_admin_pause_click(interaction)


class CS2AdminCloseButton(discord.ui.Button):
    def __init__(self, cog: "CS2BridgeCog", disabled: bool = False):
        super().__init__(
            label=END_CALL_LABEL,
            style=END_CALL_STYLE,
            custom_id=ADMIN_CLOSE_CUSTOM_ID,
            disabled=disabled,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_admin_close_click(interaction)


class CS2AdminAlertView(discord.ui.View):
    def __init__(self, cog: "CS2BridgeCog", paused: bool = False, pause_disabled: bool = False):
        super().__init__(timeout=None)
        self.add_item(CS2OpenChatButton(cog))
        self.add_item(CS2AdminPauseButton(cog, paused=paused, disabled=pause_disabled))
        self.add_item(CS2AdminCloseButton(cog))


class CS2CompleterAcceptButton(discord.ui.Button):
    def __init__(self, cog: "CS2BridgeCog"):
        super().__init__(
            label=ACCEPT_BUTTON_LABEL,
            style=ACCEPT_BUTTON_STYLE,
            custom_id=ACCEPT_CUSTOM_ID,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_completer_accept_click(interaction)


class CS2CompleterAcceptView(discord.ui.View):
    def __init__(self, cog: "CS2BridgeCog"):
        super().__init__(timeout=None)
        self.add_item(CS2CompleterAcceptButton(cog))


class CS2BridgeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rate_limit_by_user: dict[int, float] = {}
        self._processed_alert_messages: set[int] = set()
        self._claimed_completer_messages: set[int] = set()
        self._processing_completer_messages: set[int] = set()
        self._chat_sessions_by_server: dict[str, dict[str, float | int]] = {}
        self._pending_outbox_by_alias: dict[str, list[dict[str, Any]]] = {}
        self._recent_inbox_alerts: dict[str, float] = {}
        self._admin_meta_by_message: dict[int, dict[str, Any]] = {}
        self._completer_meta_by_message: dict[int, dict[str, Any]] = {}
        self._rcon_cfg = {
            "cs2": {
                "host": RCON_HOST,
                "port": int(RCON_PORT or 0),
                "rcon_password": RCON_PASSWORD,
            }
        }
        self._log_config_warnings()

    async def cog_load(self) -> None:
        setattr(self.bot, "cs2bridge_http_handler", self.handle_cs2_chat_http)
        setattr(self.bot, "cs2bridge_poll_http_handler", self.handle_cs2_poll_http)
        if not getattr(self.bot, "_cs2bridge_views_registered", False):
            self.bot.add_view(CS2AdminAlertView(self))
            self.bot.add_view(CS2ReplyView(self, "completer"))
            self.bot.add_view(CS2CompleterAcceptView(self))
            setattr(self.bot, "_cs2bridge_views_registered", True)
        logger.info("\u2705 cs2bridge: HTTP handlers /cs2/chat and /cs2/bridge/poll registered")

    def cog_unload(self) -> None:
        if hasattr(self.bot, "cs2bridge_http_handler"):
            setattr(self.bot, "cs2bridge_http_handler", None)
        if hasattr(self.bot, "cs2bridge_poll_http_handler"):
            setattr(self.bot, "cs2bridge_poll_http_handler", None)

    def _log_config_warnings(self) -> None:
        if not CS2_BRIDGE_INBOX_CHANNEL_ID:
            logger.info("cs2bridge: CS2_BRIDGE_INBOX_CHANNEL_ID not configured (legacy direct alert mode)")
        if not DISCORD_ADMIN_ALERT_CHANNEL_ID:
            logger.warning("cs2bridge: DISCORD_ADMIN_ALERT_CHANNEL_ID not configured")
        if not DISCORD_COMPLETER_ALERT_CHANNEL_ID and not CANAL_FILA_ID:
            logger.warning("cs2bridge: DISCORD_COMPLETER_ALERT_CHANNEL_ID/CANAL_FILA_ID not configured")
        if DISCORD_CHAT_RELAY_CHANNEL_ID:
            logger.info("\U0001F4AC cs2bridge: global relay disabled by default; chat appears only with open conversation")
        if not DISCORD_CALLADMIN_ROLE_ID:
            logger.warning("cs2bridge: DISCORD_CALLADMIN_ROLE_ID not configured (falling back to STAFF_ROLE_IDS)")
        if not DISCORD_COMPLETER_ROLE_ID:
            logger.warning("cs2bridge: DISCORD_COMPLETER_ROLE_ID not configured (falling back to STAFF_ROLE_IDS)")
        if not DISCORD_CALLADMIN_ROLE_ID and not DISCORD_COMPLETER_ROLE_ID and not STAFF_ROLE_IDS:
            logger.warning("cs2bridge: no role configured for response (set *_ROLE_ID or STAFF_ROLE_IDS)")
        if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
            logger.warning("cs2bridge: RCON_HOST/RCON_PORT/RCON_PASSWORD incomplete")
        if not CS2_SHARED_KEY:
            logger.warning("cs2bridge: CS2_SHARED_KEY not configured")

    def _detect_alert_kind(self, channel_id: int, content: str) -> AlertKind | None:
        lowered = (content or "").lower()

        if (
            DISCORD_ADMIN_ALERT_CHANNEL_ID
            and channel_id == DISCORD_ADMIN_ALERT_CHANNEL_ID
            and ADMIN_ALERT_KEYWORD in lowered
        ):
            return "admin"

        if (
            DISCORD_COMPLETER_ALERT_CHANNEL_ID
            and channel_id == DISCORD_COMPLETER_ALERT_CHANNEL_ID
            and COMPLETER_ALERT_KEYWORD in lowered
        ):
            return "completer"

        return None

    def _expected_channel_id(self, alert_kind: AlertKind) -> int:
        if alert_kind == "admin":
            return int(DISCORD_ADMIN_ALERT_CHANNEL_ID or 0)
        return int(DISCORD_COMPLETER_ALERT_CHANNEL_ID or CANAL_FILA_ID or 0)

    def _is_valid_channel_for_alert(self, alert_kind: AlertKind, channel_id: int) -> bool:
        expected_channel_id = self._expected_channel_id(alert_kind)
        return expected_channel_id > 0 and channel_id == expected_channel_id

    def _is_valid_channel_or_thread_for_alert(
        self,
        alert_kind: AlertKind,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> bool:
        expected_channel_id = self._expected_channel_id(alert_kind)
        if expected_channel_id <= 0 or channel is None:
            return False
        if getattr(channel, "id", 0) == expected_channel_id:
            return True
        if isinstance(channel, discord.Thread):
            return int(channel.parent_id or 0) == expected_channel_id
        return False

    def _required_role_ids(self, alert_kind: AlertKind) -> list[int]:
        role_ids: list[int] = []
        if alert_kind == "admin":
            role_id = int(DISCORD_CALLADMIN_ROLE_ID or 0)
        else:
            role_id = int(DISCORD_COMPLETER_ROLE_ID or 0)
        if role_id > 0:
            role_ids.append(role_id)
        role_ids.extend([rid for rid in STAFF_ROLE_IDS if int(rid) > 0])
        deduped = list(dict.fromkeys(role_ids))
        return deduped

    def _has_required_role(self, member: discord.Member, alert_kind: AlertKind) -> bool:
        role_ids = self._required_role_ids(alert_kind)
        if not role_ids:
            return False
        allowed = set(role_ids)
        return any(role.id in allowed for role in member.roles)

    def _extract_server_id(self, content: str) -> str:
        match = SERVER_ID_PATTERN.search(content or "")
        if not match:
            return ""
        return _one_line(match.group(1), 64)

    def _extract_server_id_from_message(self, message: discord.Message) -> str:
        candidates: list[str] = [message.content or ""]
        for embed in message.embeds:
            if embed.description:
                candidates.append(embed.description)
            for field in embed.fields:
                candidates.append(field.value or "")
        for text in candidates:
            server_id = self._extract_server_id(text)
            if server_id:
                return server_id
        return ""

    def _detect_alert_kind_by_content(self, content: str) -> AlertKind | None:
        lowered = (content or "").lower()
        if ADMIN_ALERT_KEYWORD in lowered:
            return "admin"
        if COMPLETER_ALERT_KEYWORD in lowered:
            return "completer"
        return None

    def _extract_match_id(self, content: str) -> str:
        match = MATCH_ID_PATTERN.search(content or "")
        if not match:
            return ""
        return _one_line(match.group(1), 64)

    def _extract_team_text(self, content: str) -> str:
        match = TEAM_PATTERN.search(content or "")
        if not match:
            return ""
        return _one_line(match.group(1), 64)

    def _extract_abandoned_steamid(self, content: str) -> str:
        match = ABANDONED_STEAMID_PATTERN.search(content or "")
        if not match:
            return ""
        return _one_line(match.group(1), 20)

    def _extract_abandoned_name(self, content: str) -> str:
        match = ABANDONED_NAME_PATTERN.search(content or "")
        if not match:
            return ""
        return _one_line(match.group(1), 64)

    async def _resolve_channel(
        self,
        channel_id: int,
    ) -> discord.TextChannel | discord.Thread | None:
        if channel_id <= 0:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def _resolve_alert_destination_channel(
        self,
        alert_kind: AlertKind,
    ) -> discord.TextChannel | discord.Thread | None:
        if alert_kind == "admin":
            return await self._resolve_channel(int(DISCORD_ADMIN_ALERT_CHANNEL_ID or 0))
        channel_id = int(DISCORD_COMPLETER_ALERT_CHANNEL_ID or CANAL_FILA_ID or 0)
        return await self._resolve_channel(channel_id)

    def _extract_role_mentions(self, content: str) -> str:
        matches = ROLE_MENTION_PATTERN.findall(content or "")
        if not matches:
            return ""
        return " ".join(dict.fromkeys(matches))

    def _parse_completer_meta_from_message(self, message: discord.Message) -> dict | None:
        for embed in message.embeds:
            footer = (embed.footer.text or "").strip() if embed.footer else ""
            if not footer.startswith(META_PREFIX):
                continue
            raw = footer[len(META_PREFIX) :].strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return None

    def _store_completer_meta(self, message_id: int, meta: dict[str, Any]) -> None:
        safe = {
            "server_id": _one_line(str(meta.get("server_id", "")), 64),
            "matchid": _one_line(str(meta.get("matchid", "")), 64),
            "team_text": _one_line(str(meta.get("team_text", "")), 64),
            "abandoned_steamid": _one_line(str(meta.get("abandoned_steamid", "")), 20),
            "abandoned_name": _one_line(str(meta.get("abandoned_name", "")), 64),
            "status": _one_line(str(meta.get("status", "open")), 20) or "open",
        }
        self._completer_meta_by_message[int(message_id)] = safe

    async def _get_completer_meta(self, message: discord.Message) -> dict[str, Any]:
        cached = self._completer_meta_by_message.get(int(message.id), {})
        if isinstance(cached, dict) and cached:
            return dict(cached)

        try:
            row = await get_matchguardian_completer_request(int(message.id))
        except Exception as exc:
            logger.warning(f"cs2bridge: falha ao buscar completer meta {message.id}: {exc}")
            row = None
        if isinstance(row, dict) and row:
            self._store_completer_meta(int(message.id), row)
            return dict(self._completer_meta_by_message.get(int(message.id), {}))

        parsed = self._parse_completer_meta_from_message(message)
        if isinstance(parsed, dict) and parsed:
            self._store_completer_meta(int(message.id), parsed)
            return dict(self._completer_meta_by_message.get(int(message.id), {}))

        return {}

    def _call_id_from_embed_title(self, message: discord.Message) -> int:
        title = ""
        if message.embeds:
            title = _one_line(message.embeds[0].title or "", 128)
        match = CALL_ID_PATTERN.search(title)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except Exception:
            return 0

    def _get_admin_meta(self, message: discord.Message) -> dict[str, Any]:
        cached = self._admin_meta_by_message.get(int(message.id), {})
        if isinstance(cached, dict) and cached:
            return dict(cached)
        parsed = self._parse_admin_meta_from_message(message)
        if isinstance(parsed, dict) and parsed:
            return dict(parsed)

        server_id = self._extract_server_id_from_message(message)
        matchid = self._extract_matchid_from_message(message)
        paused = False
        for row in message.components:
            for component in row.children:
                if getattr(component, "custom_id", "") == ADMIN_PAUSE_CUSTOM_ID:
                    label = _one_line(str(getattr(component, "label", "")), 32).lower()
                    paused = label == UNPAUSE_LABEL.lower()
                    break
        return {
            "log_id": self._call_id_from_embed_title(message),
            "server_id": server_id,
            "matchid": matchid,
            "paused": paused,
            "live": bool(matchid) and not matchid.upper().startswith("SEM_MATCH"),
        }

    def _store_admin_meta(self, message_id: int, meta: dict[str, Any]) -> None:
        safe = {
            "log_id": int(meta.get("log_id") or 0),
            "server_id": _one_line(str(meta.get("server_id", "")), 64),
            "matchid": _one_line(str(meta.get("matchid", "")), 64),
            "paused": bool(meta.get("paused", False)),
            "live": bool(meta.get("live", False)),
        }
        self._admin_meta_by_message[int(message_id)] = safe

    def _coerce_team_label(self, raw_team: str) -> str:
        team = _one_line(raw_team, 64).lower()
        if team in ("team1", "time 1", "time1"):
            return "team1"
        if team in ("team2", "time 2", "time2"):
            return "team2"
        return ""

    def _display_team_label(self, team: str) -> str:
        normalized = self._coerce_team_label(team)
        if normalized == "team1":
            return "Team 1"
        if normalized == "team2":
            return "Team 2"
        return _one_line(team, 64) or "Not defined"

    async def _resolve_live_mix_session(self, server_key: str, matchid: str = "") -> tuple[str, dict] | tuple[None, None]:
        try:
            from bot.cogs.mix import sessions as mix_sessions
        except Exception:
            return None, None

        incoming_server_tokens = self._incoming_server_tokens(server_key)
        normalized_matchid = _one_line(matchid, 64)
        for session_server_id, session in mix_sessions.items():
            if not isinstance(session, dict) or session.get("status") != "LIVE":
                continue

            session_matchid = _one_line(str(session.get("match_id") or ""), 64)
            if normalized_matchid and session_matchid != normalized_matchid:
                continue

            session_tokens = {str(session_server_id).strip().lower()}
            runtime_server_id = _one_line(str(session.get("runtime_server_id") or ""), 64).lower()
            if runtime_server_id:
                session_tokens.add(runtime_server_id)

            runtime_host = _one_line(str(session.get("runtime_host") or ""), 128).lower()
            runtime_port = int(session.get("runtime_port") or 0)
            if runtime_host and runtime_port > 0:
                session_tokens.add(f"{runtime_host}:{runtime_port}")
                session_tokens.add(f"port:{runtime_port}")

            if incoming_server_tokens and not incoming_server_tokens.intersection(session_tokens):
                continue
            return str(session_server_id), session

        return None, None

    async def _ensure_mix_session_steamids(self, session: dict) -> dict[int, str]:
        steamids = session.get("player_steamids")
        if not isinstance(steamids, dict):
            steamids = {}

        players = session.get("players") or []
        for player in players:
            if not player:
                continue
            sid = _one_line(str(steamids.get(player.id) or ""), 20)
            if sid:
                steamids[player.id] = sid
                continue
            try:
                rank_data = await get_player_rank(player.id)
            except Exception:
                rank_data = None
            player_sid = _one_line(str((rank_data or {}).get("steamid64", "")), 20)
            if player_sid:
                steamids[player.id] = player_sid

        session["player_steamids"] = steamids
        return steamids

    async def _sync_completer_voice_and_session(
        self,
        member: discord.Member,
        abandoned_steamid: str,
        steamid64: str,
        team_label: str,
        server_key: str,
        matchid: str,
    ) -> None:
        try:
            from bot.cogs.mix import safe_move_member
        except Exception as exc:
            logger.warning(f"cs2bridge: nao foi possivel importar helper de voice do mix: {exc}")
            return

        session_server_id, session = await self._resolve_live_mix_session(server_key, matchid)
        if not session_server_id or not isinstance(session, dict):
            logger.warning(
                f"cs2bridge: sessao live nao encontrada para sync de substituicao "
                f"(server={server_key} match={matchid})"
            )
            return

        steamids = await self._ensure_mix_session_steamids(session)
        abandoned_member = next(
            (
                player
                for player in (session.get("players") or [])
                if player and _one_line(str(steamids.get(player.id) or ""), 20) == abandoned_steamid
            ),
            None,
        )

        team_bucket = "team1" if team_label == "team1" else "team2"
        other_bucket = "team2" if team_bucket == "team1" else "team1"

        players = [p for p in (session.get("players") or []) if p and p.id != member.id]
        team_players = [p for p in (session.get(team_bucket) or []) if p and p.id != member.id]
        other_players = [p for p in (session.get(other_bucket) or []) if p and p.id != member.id]

        if abandoned_member:
            players = [p for p in players if p.id != abandoned_member.id]
            team_players = [p for p in team_players if p.id != abandoned_member.id]
            other_players = [p for p in other_players if p.id != abandoned_member.id]
            steamids.pop(abandoned_member.id, None)
            faceit_info = session.get("faceit_info")
            if isinstance(faceit_info, dict):
                faceit_info.pop(abandoned_member.id, None)
            rank_positions = session.get("player_rank_positions")
            if isinstance(rank_positions, dict):
                rank_positions.pop(abandoned_member.id, None)

        if not any(p.id == member.id for p in players):
            players.append(member)
        if not any(p.id == member.id for p in team_players):
            team_players.append(member)

        session["players"] = players
        session[team_bucket] = team_players
        session[other_bucket] = other_players
        steamids[member.id] = steamid64
        session["player_steamids"] = steamids

        server_cfg = SERVERS.get(session_server_id) or {}
        channels = server_cfg.get("channels", {}) if isinstance(server_cfg, dict) else {}
        guild = member.guild
        target_voice_id = int(
            (channels.get("team1_voice") if team_bucket == "team1" else channels.get("team2_voice")) or 0
        )
        target_voice = guild.get_channel(target_voice_id) if guild and target_voice_id > 0 else None
        sala_saida = guild.get_channel(SALA_SAIDA_ID) if guild and SALA_SAIDA_ID else None

        if abandoned_member and guild:
            abandoned_member = guild.get_member(abandoned_member.id) or abandoned_member

        if abandoned_member and abandoned_member.voice and abandoned_member.voice.channel:
            if sala_saida is not None:
                await safe_move_member(
                    abandoned_member,
                    sala_saida,
                    context=f"{session_server_id}:completer-remove-old",
                )
            else:
                try:
                    await abandoned_member.move_to(None)
                except Exception as exc:
                    logger.warning(
                        f"VOICE move failed ({session_server_id}:completer-disconnect-old): "
                        f"{abandoned_member.id} | {type(exc).__name__}: {exc}"
                    )

        if isinstance(target_voice, discord.VoiceChannel):
            await safe_move_member(
                member,
                target_voice,
                context=f"{session_server_id}:completer-add-new",
            )
        else:
            logger.warning(
                f"cs2bridge: canal de voice do time nao encontrado para substituicao "
                f"(server={session_server_id} team={team_bucket})"
            )

    def _extract_connect_commands_from_text(self, text: str) -> list[str]:
        commands: list[str] = []
        for match in CONNECT_CMD_PATTERN.finditer(text or ""):
            cmd = _one_line(match.group(0), 240)
            if cmd:
                commands.append(cmd)
        return commands

    def _extract_connect_from_message(self, message: discord.Message) -> str:
        commands: list[str] = []
        commands.extend(self._extract_connect_commands_from_text(message.content or ""))
        for embed in message.embeds:
            if embed.description:
                commands.extend(self._extract_connect_commands_from_text(embed.description))
            for field in embed.fields:
                commands.extend(self._extract_connect_commands_from_text(field.value or ""))
        if not commands:
            return ""
        with_password = next((c for c in commands if "password" in c.lower()), "")
        return with_password or commands[0]

    def _build_connect_command(self, host: str, port: int, password: str = "") -> str:
        safe_host = _one_line(host, 128)
        safe_password = _one_line(password, 128)
        if not safe_host or port <= 0:
            return ""
        if safe_password:
            return f"connect {safe_host}:{port}; password {safe_password}"
        return f"connect {safe_host}:{port}"

    def _find_live_connect_command(self, server_key: str, server_cfg: dict, matchid: str = "") -> str:
        try:
            from bot.cogs.mix import sessions as mix_sessions
        except Exception:
            return ""

        incoming_server_tokens = self._incoming_server_tokens(server_key)
        normalized_matchid = _one_line(matchid, 64)
        for session_server_id, session in mix_sessions.items():
            if not isinstance(session, dict) or session.get("status") != "LIVE":
                continue
            session_matchid = _one_line(str(session.get("match_id") or ""), 64)
            if normalized_matchid and session_matchid != normalized_matchid:
                continue

            session_tokens = {str(session_server_id).strip().lower()}
            runtime_server_id = _one_line(str(session.get("runtime_server_id") or ""), 64).lower()
            if runtime_server_id:
                session_tokens.add(runtime_server_id)

            runtime_host = _one_line(str(session.get("runtime_host") or ""), 128)
            runtime_port = int(session.get("runtime_port") or 0)
            if runtime_host and runtime_port > 0:
                session_tokens.add(f"{runtime_host.lower()}:{runtime_port}")
                session_tokens.add(f"port:{runtime_port}")

            if incoming_server_tokens and not incoming_server_tokens.intersection(session_tokens):
                continue

            password = _one_line(str(session.get("match_password") or ""), 128)
            connect_cmd = self._build_connect_command(runtime_host, runtime_port, password)
            if connect_cmd:
                return connect_cmd

        cs2 = server_cfg.get("cs2", {}) if isinstance(server_cfg, dict) else {}
        host = _one_line(str(cs2.get("host", "")), 128)
        port = int(cs2.get("port") or 0)
        return self._build_connect_command(host, port)

    async def _find_connect_command_for_server(self, server_key: str, server_cfg: dict, matchid: str = "") -> str:
        live_connect_cmd = self._find_live_connect_command(server_key, server_cfg, matchid)
        if live_connect_cmd:
            return live_connect_cmd

        channels = server_cfg.get("channels", {}) if isinstance(server_cfg, dict) else {}
        picks_channel_id = int(channels.get("picks_text") or 0)
        if picks_channel_id > 0:
            picks_channel = await self._resolve_channel(picks_channel_id)
            if picks_channel is not None and hasattr(picks_channel, "history"):
                try:
                    async for msg in picks_channel.history(limit=80):
                        connect_cmd = self._extract_connect_from_message(msg)
                        if connect_cmd:
                            return connect_cmd
                except Exception:
                    pass

        return f"connect {server_key}"

    async def _find_recent_alert_server_id(
        self,
        channel: discord.abc.Messageable,
        alert_kind: AlertKind,
        before: discord.Message,
        limit: int = 15,
    ) -> str:
        try:
            async for msg in channel.history(limit=limit, before=before):
                if msg.webhook_id is None:
                    continue
                detected = self._detect_alert_kind_by_content(msg.content or "")
                if detected != alert_kind:
                    continue
                server_id = self._extract_server_id_from_message(msg)
                if server_id:
                    return server_id
        except Exception:
            return ""
        return ""

    def _extract_matchid_from_message(self, message: discord.Message) -> str:
        candidates: list[str] = [message.content or ""]
        for embed in message.embeds:
            if embed.description:
                candidates.append(embed.description)
            for field in embed.fields:
                candidates.append(field.value or "")
        for text in candidates:
            matchid = self._extract_match_id(text)
            if matchid:
                return matchid
        return ""

    async def _build_thread_transcript(self, thread: discord.Thread | None, max_lines: int = 500) -> str:
        if thread is None:
            return ""
        lines: list[str] = []
        try:
            async for msg in thread.history(limit=max_lines, oldest_first=True):
                if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
                    continue
                content = _one_line(msg.content or "", 1800)
                if not content:
                    continue
                when = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                author = "BOT" if msg.author.bot else _one_line(getattr(msg.author, "display_name", msg.author.name), 64)
                lines.append(f"[{when}] {author}: {content}")
        except Exception:
            return ""
        transcript = "\n".join(lines)
        if len(transcript) > 500000:
            return transcript[-500000:]
        return transcript

    def _iter_rcon_servers(self) -> dict[str, dict]:
        all_servers: dict[str, dict] = {}
        for source in (SERVERS, TOURNAMENT_SERVERS):
            for key, cfg in source.items():
                if isinstance(key, str) and isinstance(cfg, dict):
                    all_servers[key] = cfg
        return all_servers

    def _server_has_rcon(self, cfg: dict) -> bool:
        cs2 = cfg.get("cs2", {}) if isinstance(cfg, dict) else {}
        host = cs2.get("host")
        port = int(cs2.get("port") or 0)
        password = cs2.get("rcon_password")
        return bool(host and port > 0 and password)

    def _server_match_tokens(self, key: str, cfg: dict) -> set[str]:
        tokens = {str(key).strip().lower()}
        cs2 = cfg.get("cs2", {}) if isinstance(cfg, dict) else {}
        host = _one_line(str(cs2.get("host", "")), 128).lower()
        port = int(cs2.get("port") or 0)
        if host and port > 0:
            tokens.add(f"{host}:{port}")
        if port > 0:
            tokens.add(f"port:{port}")
        return tokens

    def _incoming_server_tokens(self, server_id: str) -> set[str]:
        raw = _one_line(server_id, 128).lower()
        if not raw:
            return set()
        tokens = {raw}
        if raw.startswith("port:"):
            return tokens
        if ":" in raw:
            maybe_port = raw.rsplit(":", 1)[-1]
            if maybe_port.isdigit():
                tokens.add(f"port:{maybe_port}")
        return tokens

    def _resolve_server(self, server_id: str) -> tuple[dict | None, str]:
        candidates = self._iter_rcon_servers()
        available_keys: list[str] = list(candidates.keys())

        if server_id:
            lowered = server_id.lower()
            incoming_tokens = self._incoming_server_tokens(lowered) or {lowered}
            matches: list[tuple[str, dict]] = []
            for key, cfg in candidates.items():
                if incoming_tokens.intersection(self._server_match_tokens(key, cfg)):
                    matches.append((key, cfg))

            if len(matches) == 1:
                return matches[0][1], matches[0][0]
            if len(matches) > 1:
                return None, server_id
            return None, server_id

        if len(available_keys) == 1:
            only_key = available_keys[0]
            return candidates[only_key], only_key

        if len(available_keys) > 1:
            return None, ""
        return None, ""

    def _resolve_rcon_server(self, server_id: str) -> tuple[dict | None, str]:
        cfg, key = self._resolve_server(server_id)
        if cfg is not None and self._server_has_rcon(cfg):
            return cfg, key
        if not _one_line(server_id, 128) and RCON_HOST and int(RCON_PORT or 0) > 0 and RCON_PASSWORD:
            return self._rcon_cfg, "legacy-default"
        return None, ""

    def _server_display_label(self, server_id: str, server_name_hint: str = "") -> str:
        cfg, key = self._resolve_server(server_id)
        if cfg is not None:
            configured_name = _one_line(str(cfg.get("name", "")), 64)
            if configured_name:
                return configured_name
            if key:
                return _one_line(key, 64)

        hinted = _one_line(server_name_hint, 64)
        if hinted and hinted.lower() != "unknown":
            return hinted
        return _one_line(server_id, 64)

    def _session_aliases(self, server_id: str) -> tuple[list[str], str]:
        cleaned = _one_line(server_id, 128).lower()
        aliases: list[str] = []

        cfg = None
        resolved_server_key = ""
        if cleaned:
            cfg, resolved_server_key = self._resolve_server(cleaned)
            incoming_tokens = self._incoming_server_tokens(cleaned)
            if incoming_tokens:
                aliases.extend(sorted(incoming_tokens))
            else:
                aliases.append(cleaned)

        if cfg is not None and resolved_server_key:
            aliases.append(resolved_server_key.lower())
            aliases.extend(sorted(self._server_match_tokens(resolved_server_key, cfg)))

        deduped: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            token = _one_line(alias, 128).lower()
            if not token or token in seen:
                continue
            seen.add(token)
            deduped.append(token)

        display_name = resolved_server_key if resolved_server_key else cleaned
        return deduped, display_name

    def _purge_expired_chat_sessions(self) -> None:
        now = time.monotonic()
        expired_keys = [
            key
            for key, data in self._chat_sessions_by_server.items()
            if float(data.get("expires_at", 0.0)) <= now
        ]
        for key in expired_keys:
            self._chat_sessions_by_server.pop(key, None)

    def _set_chat_session(self, server_key: str, channel_id: int, opened_by: int) -> None:
        self._chat_sessions_by_server[server_key] = {
            "channel_id": int(channel_id),
            "opened_by": int(opened_by),
            "expires_at": time.monotonic() + CHAT_SESSION_TTL_SECONDS,
        }

    async def _resolve_chat_relay_target(
        self,
        server_id: str,
    ) -> tuple[discord.TextChannel | discord.Thread | None, str]:
        self._purge_expired_chat_sessions()
        aliases, display_name = self._session_aliases(server_id)
        if not aliases:
            return None, display_name

        for alias in aliases:
            session = self._chat_sessions_by_server.get(alias)
            if not session:
                continue
            channel_id = int(session.get("channel_id", 0))
            channel = await self._resolve_channel(channel_id)
            if channel is None or not isinstance(channel, discord.Thread):
                self._chat_sessions_by_server.pop(alias, None)
                continue
            return channel, display_name or alias

        return None, display_name

    def _resolve_session_server_for_channel(self, channel_id: int) -> tuple[list[str], str]:
        self._purge_expired_chat_sessions()
        if channel_id <= 0:
            return [], ""

        aliases: list[str] = []
        for alias, data in self._chat_sessions_by_server.items():
            if int(data.get("channel_id", 0)) != channel_id:
                continue
            token = _one_line(alias, 128).lower()
            if token:
                aliases.append(token)

        deduped = list(dict.fromkeys(aliases))
        if not deduped:
            return [], ""

        display = ""
        for token in deduped:
            _, resolved = self._resolve_server(token)
            if resolved:
                display = _one_line(resolved, 128).lower()
                break
        if not display:
            display = deduped[0]

        return deduped, display

    def _enqueue_outbox_packet_for_aliases(self, aliases: list[str], packet: dict[str, Any]) -> None:
        if not aliases:
            return

        payload: dict[str, Any] = {"id": secrets.token_hex(8), "queued_at": float(time.monotonic())}
        payload.update(packet)
        payload["type"] = _one_line(str(payload.get("type", "chat")), 24).lower() or "chat"

        text = _one_line(str(payload.get("text", "")), REPLY_MAX_CHARS)
        if payload["type"] == "chat":
            if not text:
                return
            payload["text"] = text
        else:
            payload["command"] = _one_line(str(payload.get("command", "")), 32).lower()
            if not payload["command"]:
                return

        for alias in aliases:
            token = _one_line(alias, 128).lower()
            if not token:
                continue
            queue = self._pending_outbox_by_alias.setdefault(token, [])
            queue.append(dict(payload))
            if len(queue) > OUTBOX_MAX_PER_ALIAS:
                del queue[: len(queue) - OUTBOX_MAX_PER_ALIAS]

    def _enqueue_outbox_chat_for_aliases(self, aliases: list[str], text: str) -> None:
        self._enqueue_outbox_packet_for_aliases(aliases, {"type": "chat", "text": text})

    def _enqueue_outbox_control_for_aliases(self, aliases: list[str], command: str) -> None:
        self._enqueue_outbox_packet_for_aliases(aliases, {"type": "control", "command": command})

    def _dequeue_outbox_for_server(self, server_id: str, max_messages: int) -> tuple[list[dict[str, Any]], str]:
        aliases, display = self._session_aliases(server_id)
        if not aliases:
            raw = _one_line(server_id, 128).lower()
            if raw:
                aliases = sorted(self._incoming_server_tokens(raw) or {raw})

        if not aliases:
            return [], display

        max_count = max(1, min(int(max_messages or 1), OUTBOX_MAX_PULL))
        seen_ids: set[str] = set()
        merged: list[dict[str, str | float]] = []
        for alias in aliases:
            for item in self._pending_outbox_by_alias.get(alias, []):
                msg_id = _one_line(str(item.get("id", "")), 64)
                if not msg_id or msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                merged.append(item)

        if not merged:
            return [], display

        merged.sort(key=lambda item: float(item.get("queued_at", 0.0)))
        selected = merged[:max_count]
        selected_ids = {_one_line(str(item.get("id", "")), 64) for item in selected}

        for alias in aliases:
            queue = self._pending_outbox_by_alias.get(alias, [])
            if not queue:
                continue
            self._pending_outbox_by_alias[alias] = [
                item
                for item in queue
                if _one_line(str(item.get("id", "")), 64) not in selected_ids
            ]
            if not self._pending_outbox_by_alias[alias]:
                self._pending_outbox_by_alias.pop(alias, None)

        packets: list[dict[str, Any]] = []
        for item in selected:
            packet_type = _one_line(str(item.get("type", "chat")), 24).lower() or "chat"
            if packet_type == "chat":
                msg_text = _one_line(str(item.get("text", "")), REPLY_MAX_CHARS)
                if msg_text:
                    packets.append({"type": "chat", "text": msg_text})
                continue
            cmd = _one_line(str(item.get("command", "")), 32).lower()
            if cmd:
                packets.append({"type": "control", "command": cmd})
        return packets, display

    async def _relay_staff_message_to_cs2(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        channel_id = getattr(message.channel, "id", 0)
        session_aliases, display_server = self._resolve_session_server_for_channel(channel_id)
        if not session_aliases:
            return

        member = message.author if isinstance(message.author, discord.Member) else None
        if member is None:
            return
        if not self._has_required_role(member, "admin"):
            return

        raw = message.content or ""
        if not raw.strip():
            return

        staff_text = raw
        sanitized = _sanitize_rcon_message(staff_text)
        if not sanitized:
            return

        primary_alias = session_aliases[0]
        self._enqueue_outbox_chat_for_aliases(session_aliases, staff_text)
        logger.info(f"cs2bridge: mensagem enfileirada para bridge HTTP ({display_server or primary_alias})")

        try:
            await message.add_reaction("✅")
        except Exception:
            pass

    def _is_rate_limited(self, user_id: int) -> tuple[bool, float]:
        now = time.monotonic()
        last = self._rate_limit_by_user.get(user_id, 0.0)
        diff = now - last
        if diff < RATE_LIMIT_SECONDS:
            return True, RATE_LIMIT_SECONDS - diff
        self._rate_limit_by_user[user_id] = now
        if len(self._rate_limit_by_user) > 5000:
            cutoff = now - 60.0
            self._rate_limit_by_user = {
                uid: ts for uid, ts in self._rate_limit_by_user.items() if ts >= cutoff
            }
        return False, 0.0

    def _purge_recent_inbox_alerts(self) -> None:
        now = time.monotonic()
        expired = [
            fp
            for fp, ts in self._recent_inbox_alerts.items()
            if (now - ts) > INBOX_ALERT_DEDUP_SECONDS
        ]
        for fp in expired:
            self._recent_inbox_alerts.pop(fp, None)

    def _is_duplicate_inbox_alert(
        self,
        alert_kind: AlertKind,
        server_id: str,
        matchid: str,
        team_text: str,
        abandoned_steamid: str = "",
    ) -> bool:
        self._purge_recent_inbox_alerts()
        fingerprint = "|".join(
            [
                _one_line(alert_kind, 16).lower(),
                _one_line(server_id, 64).lower(),
                _one_line(matchid, 64).lower(),
                _one_line(team_text, 64).lower(),
                _one_line(abandoned_steamid, 20).lower(),
            ]
        )
        now = time.monotonic()
        last = float(self._recent_inbox_alerts.get(fingerprint, 0.0))
        if last > 0.0 and (now - last) <= INBOX_ALERT_DEDUP_SECONDS:
            return True
        self._recent_inbox_alerts[fingerprint] = now
        if len(self._recent_inbox_alerts) > 5000:
            self._purge_recent_inbox_alerts()
        return False

    def _message_has_custom_id(self, message: discord.Message, custom_id: str) -> bool:
        for row in message.components:
            for component in row.children:
                if getattr(component, "custom_id", None) == custom_id:
                    return True
        return False

    def _message_has_reply_button(self, message: discord.Message, alert_kind: AlertKind) -> bool:
        expected_custom_id = f"{CUSTOM_ID_PREFIX}{alert_kind}"
        return self._message_has_custom_id(message, expected_custom_id)

    async def _append_reply_button(self, message: discord.Message, alert_kind: AlertKind) -> None:
        if message.id in self._processed_alert_messages:
            return
        has_reply = self._message_has_reply_button(message, alert_kind)
        has_open_chat = self._message_has_custom_id(message, OPEN_CHAT_CUSTOM_ID)
        has_pause = self._message_has_custom_id(message, ADMIN_PAUSE_CUSTOM_ID)
        has_close = self._message_has_custom_id(message, ADMIN_CLOSE_CUSTOM_ID)
        if (alert_kind == "admin" and has_open_chat and has_pause and has_close) or (
            alert_kind != "admin" and has_reply
        ):
            self._processed_alert_messages.add(message.id)
            return

        try:
            view = discord.ui.View.from_message(message, timeout=None)
        except Exception:
            view = discord.ui.View(timeout=None)

        needed_buttons = 1
        if alert_kind == "admin":
            needed_buttons = 3
        if len(view.children) > 25 - needed_buttons:
            logger.warning(f"cs2bridge: sem espaco para botao no alerta {message.id}")
            return

        if alert_kind == "admin" and not has_open_chat:
            view.add_item(CS2OpenChatButton(self))
        if alert_kind == "admin" and not has_pause:
            view.add_item(CS2AdminPauseButton(self, paused=False, disabled=False))
        if alert_kind == "admin" and not has_close:
            view.add_item(CS2AdminCloseButton(self))
        elif alert_kind != "admin" and not has_reply:
            view.add_item(CS2ReplyButton(self, alert_kind))

        try:
            await message.edit(view=view)
            self._processed_alert_messages.add(message.id)
            if len(self._processed_alert_messages) > 20000:
                self._processed_alert_messages.clear()
            return
        except discord.Forbidden:
            logger.warning(f"cs2bridge: sem permissao para editar alerta {message.id}, usando fallback")
        except discord.HTTPException as exc:
            logger.warning(f"cs2bridge: falha ao anexar botao no alerta {message.id}: {exc}")

        try:
            fallback_view: discord.ui.View
            if alert_kind == "admin":
                fallback_view = CS2AdminAlertView(self, paused=False, pause_disabled=False)
            else:
                fallback_view = CS2ReplyView(self, alert_kind)
            server_id = self._extract_server_id(message.content or "")
            fallback_text = "Open conversation:" if alert_kind == "admin" else "Reply in game:"
            if server_id:
                fallback_text += f"\nServer: `{server_id}`"
            await message.channel.send(fallback_text, view=fallback_view)
            self._processed_alert_messages.add(message.id)
        except Exception as exc:
            logger.error(f"cs2bridge: falha no fallback de botao para alerta {message.id}: {exc}")

    async def _relay_inbox_admin_alert(self, source: discord.Message) -> None:
        target_channel = await self._resolve_alert_destination_channel("admin")
        if target_channel is None:
            logger.error("cs2bridge: canal destino de admin indisponivel")
            return

        server_id = self._extract_server_id(source.content or "")
        matchid = self._extract_match_id(source.content or "")
        team_text = self._extract_team_text(source.content or "")
        mentions = self._extract_role_mentions(source.content or "")
        if self._is_duplicate_inbox_alert("admin", server_id, matchid, team_text):
            logger.info(
                f"cs2bridge: alerta admin duplicado ignorado "
                f"(server={server_id} match={matchid} team={team_text})"
            )
            return

        log_id = 0
        try:
            log_id = await create_matchguardian_log(
                alert_kind="admin",
                matchid=matchid or "",
                server_id=server_id or "",
                source_message_id=int(source.id),
                discord_guild_id=int(source.guild.id) if source.guild else None,
                discord_channel_id=int(target_channel.id),
            )
        except Exception as exc:
            logger.warning(f"cs2bridge: falha ao criar matchguardian_logs para admin: {exc}")

        title = "Admin Request"
        if log_id > 0:
            title = f"Admin Request #{log_id}"
        is_live = bool(matchid) and not matchid.upper().startswith("SEM_MATCH")

        embed = discord.Embed(
            title=title,
            description="Alert received from the CS2 server.",
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        if server_id:
            embed.add_field(name="Server", value=f"`{server_id}`", inline=True)
        if matchid:
            embed.add_field(name="Match", value=f"`{matchid}`", inline=True)
        if team_text:
            embed.add_field(name="Team", value=team_text, inline=True)

        content_lines = []
        if mentions:
            content_lines.append(mentions)
        if server_id:
            content_lines.append(f"Servidor: `{server_id}`")
        content = "\n".join(content_lines) if content_lines else None

        sent = await target_channel.send(
            content=content,
            embed=embed,
            view=CS2AdminAlertView(self, paused=False, pause_disabled=False),
        )
        self._store_admin_meta(
            sent.id,
            {
                "log_id": log_id,
                "server_id": server_id,
                "matchid": matchid,
                "paused": False,
                "live": is_live,
            },
        )
        if log_id > 0:
            try:
                await attach_matchguardian_log_message(log_id, discord_message_id=sent.id)
            except Exception as exc:
                logger.warning(f"cs2bridge: falha ao vincular mensagem ao log {log_id}: {exc}")

    async def handle_open_chat_click(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Action available only in server.", ephemeral=True)
            return

        if not self._is_valid_channel_or_thread_for_alert("admin", interaction.channel):
            await interaction.response.send_message(
                "Abra a conversa no canal de alertas admin.",
                ephemeral=True,
            )
            return

        if not self._has_required_role(member, "admin"):
            await interaction.response.send_message(
                "Voce nao tem permissao para abrir esta conversa.",
                ephemeral=True,
            )
            return

        message = interaction.message
        if message is None:
            await interaction.response.send_message("Context message not found.", ephemeral=True)
            return

        server_id = self._extract_server_id_from_message(message)
        if not server_id and interaction.channel and hasattr(interaction.channel, "history"):
            server_id = await self._find_recent_alert_server_id(
                interaction.channel,
                "admin",
                message,
            )

        session_aliases, display_server = self._session_aliases(server_id)
        if not session_aliases:
            await interaction.response.send_message(
                "Nao foi possivel extrair um identificador de servidor deste alerta.",
                ephemeral=True,
            )
            return

        target_channel: discord.TextChannel | discord.Thread | None = None
        thread = getattr(message, "thread", None)
        if isinstance(thread, discord.Thread):
            target_channel = thread
        elif isinstance(interaction.channel, discord.TextChannel):
            thread_name = f"chat-{display_server or server_id}"[:95]
            try:
                target_channel = await message.create_thread(name=thread_name, auto_archive_duration=60)
            except Exception:
                target_channel = None
        else:
            target_channel = interaction.channel if isinstance(interaction.channel, discord.Thread) else None

        if target_channel is None or not isinstance(target_channel, discord.Thread):
            await interaction.response.send_message(
                "Nao foi possivel abrir um topico para conversa. "
                "O bridge CS2 funciona apenas em topico.",
                ephemeral=True,
            )
            return

        for alias in session_aliases:
            self._set_chat_session(alias, target_channel.id, member.id)
        try:
            await target_channel.send(
                f"CS2 conversation opened by {member.mention} for `{display_server or server_id}` "
                f"(expires in {int(CHAT_SESSION_TTL_SECONDS/60)} min)."
            )
        except Exception:
            pass

        await interaction.response.send_message(
            f"Conversation opened for `{display_server or server_id}` in {target_channel.mention}.",
            ephemeral=True,
        )

        meta = self._get_admin_meta(message)
        log_id = int(meta.get("log_id") or 0)
        if log_id > 0:
            try:
                await attach_matchguardian_log_message(log_id, discord_thread_id=target_channel.id)
            except Exception as exc:
                logger.warning(f"cs2bridge: falha ao vincular thread ao log {log_id}: {exc}")

    async def handle_admin_pause_click(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Action available only in server.", ephemeral=True)
            return
        if not self._is_valid_channel_or_thread_for_alert("admin", interaction.channel):
            await interaction.response.send_message("Acao valida apenas em alerta admin.", ephemeral=True)
            return
        if not self._has_required_role(member, "admin"):
            await interaction.response.send_message("Voce nao tem permissao para esta acao.", ephemeral=True)
            return

        message = interaction.message
        if message is None:
            await interaction.response.send_message("Context message not found.", ephemeral=True)
            return

        meta = self._get_admin_meta(message)
        server_id = _one_line(str(meta.get("server_id", "")), 64) or self._extract_server_id_from_message(message)
        matchid = _one_line(str(meta.get("matchid", "")), 64) or self._extract_matchid_from_message(message)
        is_live = bool(matchid) and not matchid.upper().startswith("SEM_MATCH")
        if not is_live:
            await interaction.response.send_message(
                "Partida nao esta live. Pausa indisponivel para este chamado.",
                ephemeral=True,
            )
            return

        aliases, display_server = self._session_aliases(server_id)
        if not aliases:
            raw = _one_line(server_id, 128).lower()
            aliases = sorted(self._incoming_server_tokens(raw) or {raw}) if raw else []
        if not aliases:
            await interaction.response.send_message("Servidor nao identificado para comando de pausa.", ephemeral=True)
            return

        paused = bool(meta.get("paused", False))
        command = "unpause" if paused else "pause"
        self._enqueue_outbox_control_for_aliases(aliases, command)

        meta["server_id"] = server_id
        meta["matchid"] = matchid
        meta["paused"] = not paused
        meta["live"] = is_live

        embed = message.embeds[0] if message.embeds else discord.Embed(title="Pedido de admin")
        view = CS2AdminAlertView(self, paused=bool(meta.get("paused", False)), pause_disabled=False)
        await message.edit(embed=embed, view=view)
        self._store_admin_meta(message.id, meta)
        await interaction.response.send_message(
            f"Comando `{command}` enviado para `{display_server or server_id}`.",
            ephemeral=True,
        )

    async def handle_admin_close_click(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Action available only in server.", ephemeral=True)
            return
        if not self._is_valid_channel_or_thread_for_alert("admin", interaction.channel):
            await interaction.response.send_message("Acao valida apenas em alerta admin.", ephemeral=True)
            return
        if not self._has_required_role(member, "admin"):
            await interaction.response.send_message("Voce nao tem permissao para encerrar.", ephemeral=True)
            return

        message = interaction.message
        if message is None:
            await interaction.response.send_message("Context message not found.", ephemeral=True)
            return

        meta = self._get_admin_meta(message)
        log_id = int(meta.get("log_id") or 0)
        server_id = _one_line(str(meta.get("server_id", "")), 64) or self._extract_server_id_from_message(message)

        thread = getattr(message, "thread", None)
        if not isinstance(thread, discord.Thread) and isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel

        transcript = await self._build_thread_transcript(thread)
        if log_id > 0:
            try:
                await close_matchguardian_log(
                    log_id=log_id,
                    closed_by_discord_id=member.id,
                    closed_by_name=member.display_name or member.name,
                    transcript=transcript,
                )
            except Exception as exc:
                logger.error(f"cs2bridge: falha ao fechar log {log_id}: {exc}")

        aliases, _ = self._session_aliases(server_id)
        for alias in aliases:
            self._chat_sessions_by_server.pop(alias, None)
        self._admin_meta_by_message.pop(int(message.id), None)

        await interaction.response.send_message("Call closed and log saved.", ephemeral=True)

        if isinstance(thread, discord.Thread):
            try:
                await thread.edit(archived=True, locked=True)
            except Exception:
                pass

        try:
            await message.delete()
        except Exception:
            try:
                closed_embed = discord.Embed(
                    title="Call closed",
                    description=f"Closed by {member.mention}.",
                    color=0x95A5A6,
                    timestamp=datetime.now(timezone.utc),
                )
                await message.edit(embed=closed_embed, view=None)
            except Exception:
                pass

    async def _relay_inbox_completer_alert(self, source: discord.Message) -> None:
        target_channel = await self._resolve_alert_destination_channel("completer")
        if target_channel is None:
            logger.error("cs2bridge: canal destino de completer indisponivel")
            return

        server_id = self._extract_server_id(source.content or "")
        matchid = self._extract_match_id(source.content or "")
        team_text = self._extract_team_text(source.content or "")
        abandoned_steamid = self._extract_abandoned_steamid(source.content or "")
        abandoned_name = self._extract_abandoned_name(source.content or "")
        mentions = self._extract_role_mentions(source.content or "")

        if not abandoned_steamid:
            logger.warning(f"cs2bridge: alerta completer sem steamid de abandono (msg {source.id})")
            return
        if self._is_duplicate_inbox_alert("completer", server_id, matchid, team_text, abandoned_steamid):
            logger.info(
                f"cs2bridge: alerta completer duplicado ignorado "
                f"(server={server_id} match={matchid} sid={abandoned_steamid})"
            )
            return

        public_abandoned_label = abandoned_name or "Disconnected player"
        pretty_team_text = self._display_team_label(team_text)
        embed = discord.Embed(
            title="🚨 Substitute Request",
            description=(
                "A player has left the match.\n"
                "Click **Accept substitution** to automatically take their place."
            ),
            color=0xF39C12,
            timestamp=datetime.now(timezone.utc),
        )
        if team_text:
            embed.add_field(name="🎯 Team", value=f"`{pretty_team_text}`", inline=True)
        embed.add_field(name="👤 Player", value=f"`{public_abandoned_label}`", inline=True)
        embed.add_field(name="⚡ Status", value="`Waiting for substitute`", inline=False)

        content_lines = []
        if mentions:
            content_lines.append(mentions)
        content = "\n".join(content_lines) if content_lines else None

        sent = await target_channel.send(content=content, embed=embed, view=CS2CompleterAcceptView(self))
        self._store_completer_meta(
            int(sent.id),
            {
                "server_id": server_id,
                "matchid": matchid,
                "team_text": team_text,
                "abandoned_steamid": abandoned_steamid,
                "abandoned_name": abandoned_name,
            },
        )
        try:
            await save_matchguardian_completer_request(
                discord_message_id=int(sent.id),
                source_message_id=int(source.id),
                discord_channel_id=int(getattr(target_channel, "id", 0) or 0),
                server_id=server_id,
                matchid=matchid,
                team_text=team_text,
                abandoned_steamid=abandoned_steamid,
                abandoned_name=abandoned_name,
            )
        except Exception as exc:
            logger.warning(f"cs2bridge: falha ao persistir completer meta {sent.id}: {exc}")

        # Relay para o webapp ProjectMIX (Activity)
        await self._relay_completer_to_webapp(
            matchid=matchid,
            team_name=team_text,
            abandoned_steamid=abandoned_steamid,
            abandoned_name=abandoned_name,
        )

    async def _relay_completer_to_webapp(
        self,
        matchid: str,
        team_name: str,
        abandoned_steamid: str,
        abandoned_name: str | None,
    ) -> None:
        """Repassa pedido de substituto ao backend do ProjectMIX webapp (dual-path)."""
        webapp_url = os.getenv("PROJECTMIX_API_URL", "").rstrip("/")
        bot_api_key = os.getenv("PROJECTMIX_BOT_API_KEY", "")
        if not webapp_url or not bot_api_key:
            logger.debug("cs2bridge: PROJECTMIX_API_URL/BOT_API_KEY not configured, relay disabled")
            return

        endpoint = f"{webapp_url}/api/v1/completer/request"
        payload = {
            "matchid": _one_line(matchid, 64),
            "team_name": _one_line(team_name, 100),
            "abandoned_steamid": _one_line(abandoned_steamid, 32),
            "abandoned_name": _one_line(abandoned_name or "", 128) or None,
        }
        try:
            from aiohttp import ClientSession, ClientTimeout
            async with ClientSession(timeout=ClientTimeout(total=8)) as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers={"X-Bot-Api-Key": bot_api_key},
                ) as resp:
                    if resp.status in (200, 201):
                        logger.info(f"cs2bridge: completer repassado ao webapp (matchid={matchid})")
                    else:
                        body = await resp.text()
                        logger.warning(f"cs2bridge: webapp retornou {resp.status} no relay completer: {body[:200]}")
        except Exception as exc:
            logger.warning(f"cs2bridge: falha no relay completer ao webapp: {exc}")

    async def handle_completer_accept_click(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Action available only in server.", ephemeral=True)
            return

        message = interaction.message
        if message is None:
            await interaction.response.send_message("Context message not found.", ephemeral=True)
            return

        if message.id in self._claimed_completer_messages:
            await interaction.response.send_message("Este pedido ja foi aceito.", ephemeral=True)
            return
        if message.id in self._processing_completer_messages:
            await interaction.response.send_message("Este pedido esta sendo processado.", ephemeral=True)
            return

        meta = await self._get_completer_meta(message)
        if not meta:
            await interaction.response.send_message("Dados do pedido invalidos.", ephemeral=True)
            return

        if _one_line(str(meta.get("status", "")), 20).lower() == "claimed":
            self._claimed_completer_messages.add(message.id)
            await interaction.response.send_message("Este pedido ja foi aceito.", ephemeral=True)
            return

        abandoned_steamid = _one_line(str(meta.get("abandoned_steamid", "")), 20)
        abandoned_name = _one_line(str(meta.get("abandoned_name", "")), 64)
        server_id = _one_line(str(meta.get("server_id", "")), 64)
        matchid_text = _one_line(str(meta.get("matchid", "")), 64)
        team_text = _one_line(str(meta.get("team_text", "")), 64)
        if not abandoned_steamid:
            await interaction.response.send_message("SteamID do abandono nao encontrado.", ephemeral=True)
            return

        rank_data = await get_player_rank(member.id)
        steamid64 = _one_line(str((rank_data or {}).get("steamid64", "")), 20)
        if not steamid64:
            await interaction.response.send_message(
                "Voce precisa vincular sua Steam antes de aceitar (`/cadastro`).",
                ephemeral=True,
            )
            return

        team_label = ""
        if matchid_text.isdigit():
            try:
                team_label = _one_line(
                    str(await get_player_team_in_match(int(matchid_text), abandoned_steamid) or ""),
                    16,
                )
            except Exception:
                team_label = ""
        if not team_label:
            team_label = self._coerce_team_label(team_text)
        if team_label not in ("team1", "team2"):
            await interaction.response.send_message(
                "Nao foi possivel identificar o time para substituir.",
                ephemeral=True,
            )
            return

        server_cfg, resolved_server_key = self._resolve_rcon_server(server_id)
        if server_cfg is None:
            cfg_without_rcon, candidate_server_key = self._resolve_server(server_id)
            if cfg_without_rcon is not None and candidate_server_key:
                await interaction.response.send_message(
                    f"Servidor `{candidate_server_key}` identificado, mas sem RCON configurado.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Could not securely identify the server for this request.",
                ephemeral=True,
            )
            return

        self._processing_completer_messages.add(message.id)
        try:
            safe_name = (member.display_name or "Sub").replace('"', "").strip() or "Sub"
            remove_cmd = f'matchzy_removeplayer "{abandoned_steamid}"'
            add_cmd = f'matchzy_addplayer {steamid64} {team_label} "{safe_name}"'

            remove_resp = await send_rcon(server_cfg, remove_cmd, log_errors=True)
            if remove_resp is None:
                await interaction.response.send_message(
                    "Falha ao remover jogador antigo no servidor.",
                    ephemeral=True,
                )
                return

            add_resp = await send_rcon(server_cfg, add_cmd, log_errors=True)
            if add_resp is None:
                await interaction.response.send_message(
                    "Falha ao adicionar seu jogador no servidor.",
                    ephemeral=True,
                )
                return

            try:
                await self._sync_completer_voice_and_session(
                    member=member,
                    abandoned_steamid=abandoned_steamid,
                    steamid64=steamid64,
                    team_label=team_label,
                    server_key=resolved_server_key,
                    matchid=matchid_text,
                )
            except Exception as exc:
                logger.warning(f"cs2bridge: falha ao sincronizar voice da substituicao {message.id}: {exc}")

            self._claimed_completer_messages.add(message.id)
            view = discord.ui.View.from_message(message, timeout=None)
            for item in view.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

            connect_cmd = await self._find_connect_command_for_server(
                resolved_server_key,
                server_cfg,
                matchid_text,
            )
            from_label = abandoned_name or "Disconnected player"
            to_label = member.display_name or member.name or "Substitute"
            pretty_team_label = self._display_team_label(team_label)
            embed = discord.Embed(
                title="✅ Substitution Confirmed",
                description="The swap has been completed and the new player has been released to enter the match.",
                color=0x27AE60,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="🔁 Swap", value=f"`{from_label} -> {to_label}`", inline=False)
            embed.add_field(name="🎯 Team", value=f"`{pretty_team_label}`", inline=True)
            embed.add_field(name="🎮 Connect", value=f"```{connect_cmd}```", inline=False)

            if interaction.response.is_done():
                await interaction.followup.send(
                    "Substituicao confirmada. Comandos enviados ao servidor.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Substituicao confirmada. Comandos enviados ao servidor.",
                    ephemeral=True,
                )

            try:
                await claim_matchguardian_completer_request(
                    discord_message_id=int(message.id),
                    claimed_by_discord_id=int(member.id),
                    claimed_by_name=member.display_name or member.name,
                    claimed_steamid64=steamid64,
                )
            except Exception as exc:
                logger.warning(f"cs2bridge: falha ao marcar completer {message.id} como claimed: {exc}")
            self._store_completer_meta(
                int(message.id),
                {
                    **meta,
                    "status": "claimed",
                },
            )
            await message.edit(embed=embed, view=view)
        finally:
            self._processing_completer_messages.discard(message.id)

    async def handle_button_click(self, interaction: discord.Interaction, alert_kind: AlertKind) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Action available only in server.",
                ephemeral=True,
            )
            return

        channel_id = getattr(interaction.channel, "id", 0)
        if not self._is_valid_channel_for_alert(alert_kind, channel_id):
            await interaction.response.send_message(
                "Este botao nao corresponde ao canal/tipo deste alerta.",
                ephemeral=True,
            )
            return

        if not self._has_required_role(member, alert_kind):
            await interaction.response.send_message(
                "Voce nao tem o cargo necessario para responder este alerta.",
                ephemeral=True,
            )
            return

        server_id = self._extract_server_id_from_message(interaction.message)
        if not server_id and interaction.channel and hasattr(interaction.channel, "history"):
            server_id = await self._find_recent_alert_server_id(
                interaction.channel,
                alert_kind,
                interaction.message,
            )
        server_cfg, resolved_server_key = self._resolve_rcon_server(server_id)
        if server_cfg is None:
            cfg_without_rcon, candidate_server_key = self._resolve_server(server_id)
            if cfg_without_rcon is not None and candidate_server_key:
                await interaction.response.send_message(
                    f"Servidor `{candidate_server_key}` identificado, mas sem RCON configurado.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Could not securely identify the server for this alert. "
                "Verifique o identificador enviado pelo plugin.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(CS2ReplyModal(self, alert_kind, resolved_server_key))

    async def handle_modal_submit(
        self,
        interaction: discord.Interaction,
        alert_kind: AlertKind,
        raw_message: str,
        server_id: str,
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Action available only in server.",
                ephemeral=True,
            )
            return

        channel_id = getattr(interaction.channel, "id", 0)
        if not self._is_valid_channel_for_alert(alert_kind, channel_id):
            await interaction.response.send_message(
                "Este alerta nao permite resposta para este tipo de acao.",
                ephemeral=True,
            )
            return

        if not self._has_required_role(member, alert_kind):
            await interaction.response.send_message(
                "Voce nao tem o cargo necessario para enviar no jogo.",
                ephemeral=True,
            )
            return

        limited, retry_after = self._is_rate_limited(member.id)
        if limited:
            await interaction.response.send_message(
                f"Espere {retry_after:.1f}s para enviar novamente.",
                ephemeral=True,
            )
            return

        sanitized = _sanitize_rcon_message(raw_message)
        if not sanitized:
            await interaction.response.send_message(
                "Mensagem vazia ou invalida.",
                ephemeral=True,
            )
            return

        server_cfg, resolved_server_key = self._resolve_rcon_server(server_id)
        if server_cfg is None:
            cfg_without_rcon, candidate_server_key = self._resolve_server(server_id)
            if cfg_without_rcon is not None and candidate_server_key:
                await interaction.response.send_message(
                    f"Servidor `{candidate_server_key}` identificado, mas sem RCON configurado.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Failed to resolve the server for this alert. Send cancelled for security.",
                ephemeral=True,
            )
            return

        command = f'mg_adminsay "{sanitized}"'
        response = await send_rcon(server_cfg, command, log_errors=True)

        if response is None:
            logger.error(f"cs2bridge: falha ao enviar mg_adminsay via RCON no servidor {resolved_server_key}")
            await interaction.response.send_message(
                "Nao foi possivel enviar ao CS2 agora. Tente novamente em instantes.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Mensagem enviada no chat do jogo ({resolved_server_key}).",
            ephemeral=True,
        )

    async def _resolve_relay_channel(self) -> discord.TextChannel | discord.Thread | None:
        return await self._resolve_channel(int(DISCORD_CHAT_RELAY_CHANNEL_ID or 0))

    async def handle_cs2_chat_http(self, request: web.Request) -> web.Response:
        if not CS2_SHARED_KEY:
            return web.json_response(
                {"ok": False, "error": "server_not_configured"},
                status=503,
            )

        provided_key = request.headers.get("X-CS2-Key", "")
        if not secrets.compare_digest(provided_key, CS2_SHARED_KEY):
            return web.json_response(
                {"ok": False, "error": "unauthorized"},
                status=401,
            )

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"ok": False, "error": "invalid_json"},
                status=400,
            )
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid_json"},
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "invalid_payload_type"},
                status=400,
            )

        expected_fields = [
            "type",
            "matchid",
            "team_chat",
            "steamid64",
            "player_name",
            "message",
            "sent_at_utc",
        ]
        missing = [name for name in expected_fields if name not in payload]
        if missing:
            return web.json_response(
                {"ok": False, "error": "missing_fields", "fields": missing},
                status=400,
            )

        if payload.get("type") != "player_chat":
            return web.json_response(
                {"ok": False, "error": "unsupported_type"},
                status=400,
            )

        if not isinstance(payload.get("team_chat"), bool):
            return web.json_response(
                {"ok": False, "error": "invalid_team_chat"},
                status=400,
            )

        team_chat = payload["team_chat"]
        server_id = _one_line(str(payload.get("server_id", "")), 64)
        server_name = _one_line(str(payload.get("server_name", "")), 64)
        steamid64 = _one_line(str(payload.get("steamid64", "")), 32)
        player_name = _one_line(str(payload.get("player_name", "")), 64)
        message = _one_line(str(payload.get("message", "")), 1600)

        if not steamid64 or not player_name or not message:
            return web.json_response(
                {"ok": False, "error": "invalid_payload_values"},
                status=400,
            )

        relay_channel, resolved_server_key = await self._resolve_chat_relay_target(server_id)
        if relay_channel is None:
            return web.json_response(
                {
                    "ok": True,
                    "relayed": False,
                    "reason": "chat_not_opened",
                    "server": resolved_server_key or server_id or "",
                },
                status=200,
            )

        scope = "TEAM" if team_chat else "ALL"
        display_server = self._server_display_label(resolved_server_key or server_id, server_name)
        server_prefix = f"[{display_server}] " if display_server else ""
        relay_text = f"[{scope}] {server_prefix}{player_name} ({steamid64}): {message}"
        relay_text = relay_text[:2000]

        try:
            await relay_channel.send(relay_text)
        except Exception as exc:
            logger.error(f"cs2bridge: falha ao publicar relay no Discord: {exc}")
            return web.json_response(
                {"ok": False, "error": "relay_send_failed"},
                status=500,
            )

        return web.json_response({"ok": True}, status=200)

    async def handle_cs2_poll_http(self, request: web.Request) -> web.Response:
        if not CS2_SHARED_KEY:
            return web.json_response({"ok": False, "error": "server_not_configured"}, status=503)

        provided_key = request.headers.get("X-CS2-Key", "")
        if not secrets.compare_digest(provided_key, CS2_SHARED_KEY):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "invalid_payload_type"}, status=400)

        server_id = _one_line(str(payload.get("server_id", "")), 64)
        if not server_id:
            return web.json_response({"ok": False, "error": "missing_server_id"}, status=400)

        max_messages = 1
        try:
            max_messages = int(payload.get("max_messages", 1) or 1)
        except Exception:
            max_messages = 1

        messages, display_server = self._dequeue_outbox_for_server(server_id, max_messages)
        return web.json_response(
            {
                "ok": True,
                "server": display_server or server_id,
                "messages": messages,
            },
            status=200,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return

        if message.webhook_id is None:
            await self._relay_staff_message_to_cs2(message)
            return

        inbox_channel_id = int(CS2_BRIDGE_INBOX_CHANNEL_ID or 0)
        if inbox_channel_id > 0 and message.channel.id == inbox_channel_id:
            alert_kind = self._detect_alert_kind_by_content(message.content or "")
            if alert_kind == "admin":
                await self._relay_inbox_admin_alert(message)
            elif alert_kind == "completer":
                await self._relay_inbox_completer_alert(message)
            return

        alert_kind = self._detect_alert_kind(message.channel.id, message.content or "")
        if alert_kind:
            await self._append_reply_button(message, alert_kind)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CS2BridgeCog(bot))
