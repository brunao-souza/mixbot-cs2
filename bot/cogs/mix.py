import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button
from loguru import logger
import asyncio
import random
import re
from typing import List, Optional, Dict
from types import SimpleNamespace
from datetime import datetime
import copy

# Imports do Banco de Dados
from bot.database import (
    get_player_rank, get_match_overview,
    set_active_match, clear_active_match, get_active_matches,
    ping_db,
    get_live_match_by_teams, set_active_session, clear_active_session,
    is_active_session, reserve_match_id, clear_reserved_match_id,
    get_rank_positions, get_matchguardian_notconnect_rows,
    get_match_runtime_server,
)

# Imports de Configuração
from bot.config import (
    SERVERS, CANAL_MONITOR_ID, CANAL_LOGS_ID, CANAL_APOIO_ID,
    MEMBER_ROLE_ID, MEMBER_ROLE_NAME, SALA_PROXIMO_ID, SALA_SAIDA_ID, MIX_ACCEPT_TIMEOUT, CAPTAIN_VOTE_TIMEOUT, MAP_VETO_TIMEOUT, MAP_VETO_FINAL_TIMEOUT, DEMO_UPLOAD_URL,
    MAPS_BASE, MAP_NAME_CONVERT, MAP_IMAGES, MATCHZY_ADMIN_STEAMID64
)

from bot.utils.cs2 import send_rcon
from bot.cogs.monitor import update_monitor_combined
from bot.cogs.bemvindo import send_welcome_if_needed, sync_member_role_if_registered
from bot.cogs.fila import get_fila_cog
from bot.utils.faceit_api import get_faceit_profile
from bot.utils.steam_api import validate_steamid64
from bot.utils.mix_lobby import pick_free_lobby_server
from bot.utils.server_pool import get_server_pool, NoServerAvailableError

# SteamID64 opcional para manter liberado como espectador/admin.
ALWAYS_ALLOW_STEAMID64 = MATCHZY_ADMIN_STEAMID64
APOIO_PUBLI_SECONDS = 10
TORNEIO_PUBLI_SECONDS = 20
TORNEIO_CANAL_ID = 1474026496166203504
TORNEIO_BANNER_URL = "https://cdn.discordapp.com/attachments/1452985230565834804/1509289684558155866/brasilmixEbrz.png?ex=6a18a30e&is=6a17518e&hm=b99902e9a50f1f840f5948e225bfde55d421e60dba9c24837a5e5c9f03be8ed9&"
MIX_CHANNEL_CLEANUP_DELAY_SECONDS = 120
MIX_CHANNEL_CLEANUP_HISTORY_LIMIT = 300

# ================= ESTRUTURA DE SESSÃO =================
DEFAULT_SESSION_STATE = {
    "active": False, "status": "IDLE", "players": [], "player_ratings": {},
    "player_rank_positions": {},
    "captains": [], "team1": [], "team2": [], "available": [], "turn": None,
    "maps": [], "message": None, "captain_votes": {}, "voted_users": {},
    "captain_vote_active": False, "accepts": set(), "accept_message": None,
    "accept_task": None, "accept_expires_at": None, "match_password": None, "match_map": None,
    "match_id": None, "pick_reason": "", "faceit_info": {},
    "map_veto_task": None,
    "veto_round": 0,
    "veto_votes": {},
    "veto_player_votes": {},
    "promo_sent": False,
    "torneio_promo_sent": False,
    "suspend_auto_restore": False,
    "player_steamids": {},
    "live_started_at": None,
    "notconnect_recovered": False,
    "recovering_notconnect": False,
    "runtime_server_id": None,
    "runtime_tmux_session": None,
    "runtime_host": None,
    "runtime_port": None,
    "runtime_gotv_port": None,
    "runtime_already_online": False,
    # Fase atual da partida (atualizado via webhooks do MatchZy)
    "match_phase": None,       # "knife" | "halftime" | "overtime" | "paused" | None
    "match_overtime_num": 0,   # número da overtime (1, 2, ...)
    "match_pause_team": None,  # time que pediu pausa
    "match_round_num": 0,      # round atual
    "match_round_mvp": None,   # {"name": str, "kills": int, "damage": int}
}

global_state = {
    "monitor_msgs": {},
    "channel_cleanup_tasks": {},
}

sessions: Dict[str, dict] = {}
map_veto_locks: Dict[str, asyncio.Lock] = {}
draft_locks: Dict[str, asyncio.Lock] = {}
category_visibility_locks: Dict[str, asyncio.Lock] = {}


def get_map_veto_lock(s_id: str) -> asyncio.Lock:
    lock = map_veto_locks.get(s_id)
    if lock is None:
        lock = asyncio.Lock()
        map_veto_locks[s_id] = lock
    return lock


def get_draft_lock(s_id: str) -> asyncio.Lock:
    lock = draft_locks.get(s_id)
    if lock is None:
        lock = asyncio.Lock()
        draft_locks[s_id] = lock
    return lock


def get_category_visibility_lock(s_id: str) -> asyncio.Lock:
    lock = category_visibility_locks.get(s_id)
    if lock is None:
        lock = asyncio.Lock()
        category_visibility_locks[s_id] = lock
    return lock


def _spawn_background_task(coro, label: str):
    task = asyncio.create_task(coro)

    def _done_callback(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(f"\u274C Task em background falhou ({label}): {exc}")

    task.add_done_callback(_done_callback)
    return task


def init_sessions():
    for s_id in SERVERS:
        sessions[s_id] = copy.deepcopy(DEFAULT_SESSION_STATE)
        map_veto_locks[s_id] = asyncio.Lock()
        draft_locks[s_id] = asyncio.Lock()
        category_visibility_locks[s_id] = asyncio.Lock()
        global_state["monitor_msgs"][s_id] = None


def _get_server_category(bot, s_id: str) -> Optional[discord.CategoryChannel]:
    server = SERVERS.get(s_id) or {}
    channels = server.get("channels") or {}
    category_id = int(channels.get("category") or 0)
    category = bot.get_channel(category_id) if category_id > 0 else None
    if isinstance(category, discord.CategoryChannel):
        return category

    picks_text_id = int(channels.get("picks_text") or 0)
    picks_text = bot.get_channel(picks_text_id) if picks_text_id > 0 else None
    inferred = getattr(picks_text, "category", None)
    if isinstance(inferred, discord.CategoryChannel):
        return inferred
    return None


def _iter_server_voice_channels(bot, s_id: str) -> List[discord.VoiceChannel]:
    server = SERVERS.get(s_id) or {}
    channels = server.get("channels") or {}
    ids = [
        int(channels.get("picks_voice") or 0),
        int(channels.get("team1_voice") or 0),
        int(channels.get("team2_voice") or 0),
    ]
    output: List[discord.VoiceChannel] = []
    for ch_id in ids:
        if ch_id <= 0:
            continue
        channel = bot.get_channel(ch_id)
        if isinstance(channel, discord.VoiceChannel):
            output.append(channel)
    return output


def _iter_server_managed_channels(
    bot,
    s_id: str,
) -> List[discord.abc.GuildChannel]:
    server = SERVERS.get(s_id) or {}
    channels = server.get("channels") or {}
    ids = [
        int(channels.get("picks_text") or 0),
        int(channels.get("picks_voice") or 0),
        int(channels.get("team1_voice") or 0),
        int(channels.get("team2_voice") or 0),
    ]
    output: List[discord.abc.GuildChannel] = []
    seen_ids: set[int] = set()
    for ch_id in ids:
        if ch_id <= 0 or ch_id in seen_ids:
            continue
        channel = bot.get_channel(ch_id)
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            output.append(channel)
            seen_ids.add(ch_id)
    return output


def _get_member_role(guild: Optional[discord.Guild]) -> Optional[discord.Role]:
    if guild is None:
        return None
    role = guild.get_role(MEMBER_ROLE_ID) if MEMBER_ROLE_ID else None
    if role is None and MEMBER_ROLE_NAME:
        role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
    return role


def _overwrite_needs_update(
    channel: discord.abc.GuildChannel,
    target: discord.Role,
    **expected,
) -> bool:
    overwrite = channel.overwrites_for(target)
    return any(getattr(overwrite, key) != value for key, value in expected.items())


async def _sync_server_channel_access(bot, s_id: str, visible: bool, reason: str = "") -> bool:
    category = _get_server_category(bot, s_id)
    if not category or not category.guild:
        return False

    guild = category.guild
    member_role = _get_member_role(guild)
    if member_role is None:
        logger.warning(f"Cargo MEMBRO nao encontrado ao sincronizar permissoes do servidor {s_id}")
        return False

    target_view = bool(visible)
    category_reason = f"mixbot:visibility:{reason or 'auto'}:{s_id}"
    changed = False

    everyone_category_overwrite = {
        "view_channel": target_view,
        "connect": False,
    }
    member_category_overwrite = {
        "view_channel": target_view,
        "connect": target_view,
    }

    if _overwrite_needs_update(category, guild.default_role, **everyone_category_overwrite):
        await category.set_permissions(
            guild.default_role,
            reason=category_reason,
            **everyone_category_overwrite,
        )
        changed = True

    if _overwrite_needs_update(category, member_role, **member_category_overwrite):
        await category.set_permissions(
            member_role,
            reason=category_reason,
            **member_category_overwrite,
        )
        changed = True

    for channel in _iter_server_managed_channels(bot, s_id):
        channel_everyone_overwrite = {
            "view_channel": target_view,
        }
        channel_member_overwrite = {
            "view_channel": target_view,
        }

        if isinstance(channel, discord.TextChannel):
            channel_everyone_overwrite["send_messages"] = False
            channel_everyone_overwrite["read_message_history"] = target_view
            channel_member_overwrite["send_messages"] = target_view
            channel_member_overwrite["read_message_history"] = target_view
        elif isinstance(channel, discord.VoiceChannel):
            channel_everyone_overwrite["connect"] = False
            channel_everyone_overwrite["speak"] = None
            channel_member_overwrite["connect"] = target_view
            channel_member_overwrite["speak"] = None

        if _overwrite_needs_update(channel, guild.default_role, **channel_everyone_overwrite):
            await channel.set_permissions(
                guild.default_role,
                reason=category_reason,
                **channel_everyone_overwrite,
            )
            changed = True

        if _overwrite_needs_update(channel, member_role, **channel_member_overwrite):
            await channel.set_permissions(
                member_role,
                reason=category_reason,
                **channel_member_overwrite,
            )
            changed = True

    return changed


def _server_has_humans(bot, s_id: str) -> bool:
    for voice in _iter_server_voice_channels(bot, s_id):
        if any(not m.bot for m in voice.members):
            return True
    return False


def resolve_server_by_channel_id(channel_id: int) -> Optional[str]:
    if not channel_id:
        return None
    for s_id, server in SERVERS.items():
        channels = server.get("channels") or {}
        if channel_id in {
            int(channels.get("picks_voice") or 0),
            int(channels.get("team1_voice") or 0),
            int(channels.get("team2_voice") or 0),
            int(channels.get("picks_text") or 0),
        }:
            return s_id
    return None


def _is_managed_mix_text_channel(channel_id: int) -> bool:
    if not channel_id:
        return False
    return any(
        int((server.get("channels") or {}).get("picks_text") or 0) == int(channel_id)
        for server in SERVERS.values()
    )


def _message_should_survive_mix_cleanup(
    message: discord.Message,
    bot_user_id: int,
) -> bool:
    return bool(int(getattr(message.author, "id", 0) or 0) == int(bot_user_id or 0) and message.embeds)


async def _cleanup_mix_text_channel(
    channel: discord.TextChannel,
    bot_user_id: int,
) -> None:
    deleted = 0
    async for message in channel.history(limit=MIX_CHANNEL_CLEANUP_HISTORY_LIMIT):
        if _message_should_survive_mix_cleanup(message, bot_user_id):
            continue
        try:
            await message.delete()
            deleted += 1
        except (discord.Forbidden, discord.NotFound):
            continue
        except discord.HTTPException:
            continue
    if deleted > 0:
        logger.info(f"MIX_CLEANUP: canal={channel.id} removidas={deleted}")


def _schedule_mix_text_channel_cleanup(channel: discord.abc.GuildChannel | discord.Thread) -> None:
    if not isinstance(channel, discord.TextChannel):
        return
    if not channel.guild or not _is_managed_mix_text_channel(channel.id):
        return

    bot_member = channel.guild.me
    bot_user_id = int(getattr(bot_member, "id", 0) or 0)
    if bot_user_id <= 0:
        return

    tasks_by_channel = global_state.setdefault("channel_cleanup_tasks", {})
    previous_task = tasks_by_channel.get(channel.id)
    if previous_task:
        previous_task.cancel()

    async def _runner():
        try:
            await asyncio.sleep(MIX_CHANNEL_CLEANUP_DELAY_SECONDS)
            await _cleanup_mix_text_channel(channel, bot_user_id)
        except asyncio.CancelledError:
            return
        finally:
            current = tasks_by_channel.get(channel.id)
            if current is task:
                tasks_by_channel.pop(channel.id, None)

    task = asyncio.create_task(_runner())
    tasks_by_channel[channel.id] = task


async def set_server_category_visibility(bot, s_id: str, visible: bool, reason: str = "") -> bool:
    category = _get_server_category(bot, s_id)
    if not category or not category.guild:
        return False

    lock = get_category_visibility_lock(s_id)
    async with lock:
        try:
            changed = await _sync_server_channel_access(bot, s_id, bool(visible), reason=reason)
            logger.debug(
                f"Categoria {s_id} visibilidade -> {bool(visible)} ({reason or 'auto'}) changed={changed}"
            )
            return changed
        except Exception as exc:
            logger.warning(f"Falha ao alterar visibilidade da categoria {s_id}: {exc}")
            return False


async def refresh_server_category_visibility(
    bot,
    s_id: str,
    *,
    reason: str = "",
    force_visible: Optional[bool] = None,
) -> bool:
    if s_id not in SERVERS:
        return False

    if force_visible is True:
        should_show = True
    elif force_visible is False:
        should_show = False
    else:
        session_active = bool((sessions.get(s_id) or {}).get("active"))
        has_humans = _server_has_humans(bot, s_id)
        should_show = bool(session_active or has_humans)

    return await set_server_category_visibility(bot, s_id, should_show, reason=reason)


async def refresh_all_server_categories(bot, reason: str = ""):
    for s_id, server in SERVERS.items():
        if not server.get("active"):
            continue
        await refresh_server_category_visibility(bot, s_id, reason=reason)

def reset_session(s_id):
    if s_id in sessions:
        match_id = sessions[s_id].get("match_id")
        runtime_preexisting = bool(sessions[s_id].get("runtime_already_online"))
        try:
            accept_task = sessions[s_id].get("accept_task")
            if accept_task:
                accept_task.cancel()
        except:
            pass
        try:
            task = sessions[s_id].get("map_veto_task")
            if task:
                task.cancel()
        except:
            pass
        sessions[s_id] = copy.deepcopy(DEFAULT_SESSION_STATE)
        logger.debug(f"\U0001F504 Sessao {s_id} resetada.")
        try:
            _spawn_background_task(clear_active_match(s_id), f"clear_active_match:{s_id}")
            _spawn_background_task(clear_active_session(s_id), f"clear_active_session:{s_id}")
            if match_id:
                _spawn_background_task(
                    clear_reserved_match_id(int(match_id)),
                    f"clear_reserved_match_id:{s_id}:{match_id}",
                )
                _spawn_background_task(
                    get_server_pool().release_server_for_match(
                        int(match_id),
                        reason="session_reset",
                        force_clear_mapping_on_stop_error=True,
                        stop_session=not runtime_preexisting,
                    ),
                    f"release_runtime:{s_id}:{match_id}",
                )
        except:
            pass

# ================= HELPERS TÉCNICOS =================

def parse_rcon_value(rcon_output):
    if not rcon_output: return "0"
    match = re.search(r'=\s*"(\d+)"', str(rcon_output))
    if match: return match.group(1)
    match = re.search(r'=\s*(\d+)', str(rcon_output))
    return match.group(1) if match else "0"

def parse_rcon_string(rcon_output):
    if not rcon_output: return "Team"
    matches = re.findall(r'"([^"]*)"', str(rcon_output))
    if matches: return matches[-1]
    clean = str(rcon_output).replace('"', '').strip()
    if "=" in clean: return clean.split('=')[-1].strip()
    return clean if len(clean) > 1 else "Team"

def parse_match_teams_from_hostname(hostname: str):
    if not hostname:
        return None, None
    # Esperado: "... | TEAM1 vs TEAM2"
    match = re.search(r'\|\s*(.+?)\s+vs\s+(.+)$', hostname)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()

def get_progress_bar(count, total=10):
    return "🟩" * count + "⬜" * (total - count)

def build_accept_missing_players(session: dict) -> List[discord.Member]:
    players = session.get("players") or []
    accepts = set(session.get("accepts") or set())
    return [player for player in players if player.id not in accepts]


def build_accept_missing_mentions(missing_players: List[discord.Member]) -> str:
    if not missing_players:
        return "-"
    return "\n".join([f"- {player.mention}" for player in missing_players])


def build_accept_deadline_text(session: dict) -> str:
    expires_at = session.get("accept_expires_at")
    if not expires_at:
        return "-"
    expires_at = int(expires_at)
    return f"<t:{expires_at}:R> (<t:{expires_at}:T>)"

def faceit_ball(level: Optional[int], smurf: bool = False) -> str:
    if smurf:
        return "🔵"
    if level is None:
        return "⚪"
    if level == 1:
        return "⚪"
    if level in (2, 3):
        return "🟢"
    if 4 <= level <= 7:
        return "🟡"
    if level in (8, 9):
        return "🟠"
    return "🔴"

_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)

def strip_emojis(text: str) -> str:
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = cleaned.replace("\uFE0F", "").replace("\u200D", "")
    return " ".join(cleaned.split())


def clean_name(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '', name)


def normalize_match_player_name(name: str, fallback: str = "Player") -> str:
    """
    Nome do player dentro do JSON do MatchZy.
    Mantem caracteres de nickname (ex.: -, ^, ç, ã) e remove apenas controles.
    """
    raw = str(name or "").strip()
    if not raw:
        raw = str(fallback or "").strip()
    if not raw:
        raw = "Player"
    raw = re.sub(r"[\r\n\t]+", " ", raw)
    # Evita caracteres que costumam quebrar parser/console do MatchZy.
    raw = raw.replace("\\", "").replace('"', "").replace("'", "").replace("`", "")
    raw = " ".join(raw.split())
    return raw[:32]


def _compact_rcon_error(resp, max_len: int = 220) -> str:
    text = str(resp or "").strip()
    if not text:
        return "sem resposta"
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not first_line:
        first_line = text.replace("\n", " ").strip()
    if len(first_line) > max_len:
        return first_line[:max_len] + "..."
    return first_line


async def safe_move_member(member: discord.Member, channel: Optional[discord.abc.GuildChannel], *, context: str = ""):
    """Move member to voice channel without leaving unhandled task exceptions."""
    if member is None or channel is None:
        return
    try:
        await member.move_to(channel)
    except Exception as exc:
        # Common race: user disconnects from voice before move.
        logger.warning(f"VOICE move falhou ({context}): {member.id} -> {getattr(channel, 'id', 'n/a')} | {type(exc).__name__}: {exc}")

async def process_teams_parallel(team_list_1, team_list_2):
    t1_dict, t2_dict = {}, {}
    skipped_players: List[str] = []
    used_steamids = set()
    all_players = team_list_1 + team_list_2
    tasks = [get_player_rank(p.id) for p in all_players]
    results = await asyncio.gather(*tasks)
    for i, player in enumerate(all_players):
        rank_data = results[i]
        if rank_data and rank_data.get('steamid64'):
            sid = str(rank_data['steamid64']).strip()
            if not validate_steamid64(sid):
                skipped_players.append(player.display_name)
                logger.warning(
                    f"MATCH_JSON: steamid invalido ignorado user={player.id} sid={sid!r}"
                )
                continue
            if sid in used_steamids:
                skipped_players.append(player.display_name)
                logger.warning(
                    f"MATCH_JSON: steamid duplicado ignorado user={player.id} sid={sid!r}"
                )
                continue
            used_steamids.add(sid)
            nick = normalize_match_player_name(
                str(rank_data.get("nickname") or ""),
                fallback=player.display_name,
            )
            if player in team_list_1:
                t1_dict[sid] = nick
            else:
                t2_dict[sid] = nick
    return t1_dict, t2_dict, skipped_players

async def _ensure_faceit_info(session, players):
    for player in players:
        if player.id in session.get("faceit_info", {}):
            continue
        try:
            rank_data = await get_player_rank(player.id)
        except Exception:
            rank_data = None
        steamid = rank_data.get("steamid64") if rank_data else None
        if not steamid:
            continue
        try:
            faceit = await get_faceit_profile(str(steamid))
        except Exception:
            faceit = None
        if isinstance(faceit, dict):
            session.setdefault("faceit_info", {})[player.id] = faceit

    # Aplica overrides de smurf — substitui level e elo pelo valor definido pela moderação
    try:
        from bot.database import db as _db
        discord_ids = [p.id for p in players]
        if discord_ids:
            placeholders = ",".join(["%s"] * len(discord_ids))
            overrides = await _db.fetchall(
                f"SELECT discord_id, override_elo, override_level FROM smurf_overrides WHERE discord_id IN ({placeholders})",
                tuple(discord_ids)
            )
            for row in (overrides or []):
                did = row["discord_id"]
                session.setdefault("faceit_info", {}).setdefault(did, {})
                session["faceit_info"][did]["elo"] = row["override_elo"]
                session["faceit_info"][did]["level"] = row["override_level"]
                session["faceit_info"][did]["is_smurf"] = True
    except Exception as e:
        logger.warning(f"_ensure_faceit_info: erro ao checar smurf_overrides: {e}")

async def get_online_count(server_config):
    try:
        res = await send_rcon(server_config, "status")
        match = re.search(r'players\s*:\s*(\d+)', str(res))
        return match.group(1) if match else "0"
    except: return "0"

# ================= EMBED BUILDERS =================

def create_pick_embed(s_id):
    session = sessions[s_id]
    turn_player = session["turn"]
    cap1 = session["captains"][0]
    cap2 = session["captains"][1]

    is_cap1_turn = turn_player.id == cap1.id
    embed_color = 0x5D7CA6 if is_cap1_turn else 0xC0392B
    team_label = "TIME 1" if is_cap1_turn else "TIME 2"

    embed = discord.Embed(
        title=f"⚔️ PROTOCOLO DE DRAFT — {team_label}",
        color=embed_color
    )

    if session["pick_reason"]:
        embed.description = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ **Nota tática:** {session['pick_reason']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    def get_rank_pos(p):
        pos = session.get("player_rank_positions", {}).get(p.id)
        return f"#{pos}" if pos else "#-"

    def get_fmt_name(p, is_captain=False):
        faceit = session.get("faceit_info", {}).get(p.id) or {}
        lvl = faceit.get("level")
        elo = faceit.get("elo")
        is_smurf = faceit.get("is_smurf", False)
        pos = get_rank_pos(p)
        icon = "⭐" if is_captain else "👤"
        name_str = f"{icon} **{p.display_name}** `{pos}`"
        if is_smurf:
            stats = f"` Lv {lvl: <2} ` 🔵 ` CONTA SMURF `"
            return f"{name_str}\n└ {stats}"
        if lvl is not None:
            ball = faceit_ball(lvl)
            stats = f"` Lv {lvl: <2} ` {ball} ` {elo if elo else '----'} ELO `"
            return f"{name_str}\n└ {stats}"
        return name_str

    def build_team_list(players, captain_obj):
        if not players:
            return "_Aguardando escolhas..._"
        return "\n\n".join(get_fmt_name(p, p.id == captain_obj.id) for p in players)

    embed.add_field(
        name=f"🟦 EQUIPE {cap1.display_name.upper()}",
        value=build_team_list(session["team1"], cap1),
        inline=True
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name=f"🟥 EQUIPE {cap2.display_name.upper()}",
        value=build_team_list(session["team2"], cap2),
        inline=True
    )

    avail = session["available"]
    if avail:
        avail_list = []
        for p in avail:
            pos = get_rank_pos(p)
            faceit = session.get("faceit_info", {}).get(p.id) or {}
            lvl = faceit.get("level")
            elo = faceit.get("elo")
            is_smurf = faceit.get("is_smurf", False)
            if is_smurf:
                avail_list.append(f"` Lv {lvl: <2} ` 🔵 ` CONTA SMURF ` **{p.display_name}** `{pos}`")
            elif lvl is not None:
                ball = faceit_ball(lvl)
                elo_str = str(elo) if elo else "----"
                avail_list.append(f"` Lv {lvl: <2} ` {ball} ` {elo_str} ELO ` **{p.display_name}** `{pos}`")
            else:
                avail_list.append(f"` -- ` **{p.display_name}** `{pos}`")
        embed.add_field(
            name="👥 CANDIDATOS NA POOL",
            value="\n".join(avail_list),
            inline=False
        )
    else:
        embed.add_field(name="🏁 STATUS", value="` RECRUTAMENTO FINALIZADO `", inline=False)

    embed.set_footer(
        text=f"AGUARDANDO DECISÃO DE: {turn_player.display_name.upper()}",
        icon_url=turn_player.display_avatar.url
    )

    return embed

def create_map_voting_embed(s_id):
    session = sessions[s_id]
    round_num = session.get("veto_round", 1)
    bans_this_round = 3 if round_num <= 2 else 1
    team_num = 1 if round_num % 2 == 1 else 2
    banning_team = session["team1"] if team_num == 1 else session["team2"]

    is_team1 = team_num == 1
    team_color = 0x5D7CA6 if is_team1 else 0xC0392B
    team_label = "TIME 1" if is_team1 else "TIME 2"
    team_icon = "🟦" if is_team1 else "🟥"
    bar_fill = "🟦" if is_team1 else "🟥"

    max_votes = bans_this_round
    veto_votes = session.get("veto_votes", {})
    veto_player_votes = session.get("veto_player_votes", {})

    available_maps = []
    banned_maps = []
    for m in MAPS_BASE:
        if m not in session["maps"]:
            banned_maps.append(f"~~{m}~~ ❌")
        else:
            vote_count = len(veto_votes.get(m, set()))
            suffix = f" `[{vote_count} votos]`" if vote_count > 0 else ""
            available_maps.append(f"🔹 **{m}**{suffix}")

    expires_at = session.get("veto_expires_at")
    remaining_seconds = 0
    if expires_at:
        remaining_seconds = max(0, int(expires_at) - int(discord.utils.utcnow().timestamp()))
    time_str = f"⏳ {remaining_seconds} segundos\n" if expires_at else ""

    embed = discord.Embed(
        title=f"🗺️ FASE DE VETO — RODADA {round_num}",
        color=team_color,
        description=(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{team_icon} **{team_label}** está no controle!\n"
            f"Ação: Banir **{bans_this_round}** mapa(s) da pool.\n"
            f"{time_str}"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    )

    embed.add_field(
        name="📍 Map Pool Ativa",
        value="\n".join(available_maps) if available_maps else "—",
        inline=True
    )
    embed.add_field(
        name="🚫 Histórico de Bans",
        value="\n".join(banned_maps) if banned_maps else "Nenhum",
        inline=True
    )

    voted_status = []
    for p in banning_team:
        used = len(veto_player_votes.get(p.id, set()))
        progress_bar = bar_fill * used + "⬛" * (max_votes - used)
        if used >= max_votes:
            voted_status.append(f"✅ `{progress_bar}` **{p.display_name}**")
        elif used > 0:
            voted_status.append(f"🔄 `{progress_bar}` **{p.display_name}**")
        else:
            voted_status.append(f"⏳ `{progress_bar}` **{p.display_name}**")

    if voted_status:
        embed.add_field(
            name="👥 Status da Votação Coletiva",
            value="\n".join(voted_status),
            inline=False
        )

    embed.set_footer(text=f"Cada jogador tem {max_votes} voto(s) • Decisão Coletiva")

    return embed


# ================= FILA GLOBAL =================

async def schedule_queue_update(bot):
    fila_cog = get_fila_cog(bot)
    if fila_cog:
        await fila_cog.request_display_update()

async def update_queue_display(bot):
    fila_cog = get_fila_cog(bot)
    if fila_cog:
        await fila_cog.request_display_update()

class MixCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._fila_ready_lock = asyncio.Lock()
        init_sessions()

    async def cog_load(self):
        logger.debug("MixCog carregado")
        self.server_monitor_loop.start()
        self.notconnect_recovery_loop.start()
        self.db_keep_alive.start()
        self.bot.loop.create_task(self.restore_active_matches_on_startup())
        self.bot.loop.create_task(refresh_all_server_categories(self.bot, reason="cog_load"))

    async def cog_unload(self):
        self.server_monitor_loop.cancel()
        self.notconnect_recovery_loop.cancel()
        self.db_keep_alive.cancel()
        tasks_by_channel = global_state.get("channel_cleanup_tasks", {})
        for task in list(tasks_by_channel.values()):
            task.cancel()
        tasks_by_channel.clear()

    async def _pick_free_lobby_server(self) -> Optional[str]:
        available_runtime_ids: Optional[set[str]] = None
        try:
            available_runtime_ids = {
                str(runtime_id).strip().lower()
                for runtime_id in await get_server_pool().available_runtime_ids("mix")
                if str(runtime_id).strip()
            }
        except Exception as exc:
            logger.warning(f"Nao foi possivel consultar runtimes livres do pool: {exc}")

        voice_member_counts: Dict[int, int] = {}
        for server in SERVERS.values():
            picks_voice_id = int(((server.get("channels") or {}).get("picks_voice")) or 0)
            if picks_voice_id <= 0 or picks_voice_id in voice_member_counts:
                continue
            picks_voice = self.bot.get_channel(picks_voice_id)
            members = getattr(picks_voice, "members", []) if picks_voice else []
            voice_member_counts[picks_voice_id] = len([member for member in members if not getattr(member, "bot", False)])

        return pick_free_lobby_server(
            SERVERS,
            sessions,
            voice_member_counts,
            available_runtime_ids=available_runtime_ids,
        )

    @commands.Cog.listener()
    async def on_fila_pronta(self, jogadores: List[Dict]):
        async with self._fila_ready_lock:
            if not jogadores or len(jogadores) < 10:
                return

            first_member = jogadores[0].get("member")
            if not first_member:
                return
            guild = first_member.guild
            sala_proximo = guild.get_channel(SALA_PROXIMO_ID) if SALA_PROXIMO_ID else None
            fila_cog = get_fila_cog(self.bot)
            target_server_id = await self._pick_free_lobby_server()
            if not target_server_id:
                logger.warning("Fila pronta, mas nenhum lobby livre para iniciar novo mix.")
                if fila_cog:
                    await fila_cog.requeue_payload(jogadores, dispatch_ready=True)
                return

            server = SERVERS[target_server_id]
            picks_voice = guild.get_channel(server["channels"].get("picks_voice"))
            picks_text = guild.get_channel(server["channels"].get("picks_text"))
            if not picks_voice or not picks_text:
                logger.warning(f"Fila pronta ignorada: canais de lobby ausentes em {target_server_id}.")
                if fila_cog:
                    await fila_cog.requeue_payload(jogadores, dispatch_ready=True)
                return

            await refresh_server_category_visibility(
                self.bot,
                target_server_id,
                reason="fila_pronta",
                force_visible=True,
            )

            eligible: List[discord.Member] = []
            for item in jogadores:
                member = item.get("member")
                if not member or member.bot:
                    continue
                if not member.voice or not member.voice.channel:
                    continue
                if member.voice.channel.id != SALA_PROXIMO_ID:
                    continue
                eligible.append(member)

            if len(eligible) < 10:
                logger.warning(
                    f"Fila pronta invalida para {target_server_id}: elegiveis={len(eligible)}. Repondo fila."
                )
                if fila_cog:
                    await fila_cog.requeue_payload(jogadores, dispatch_ready=True)
                await refresh_server_category_visibility(self.bot, target_server_id, reason="fila_invalid_payload")
                return

            selected = eligible[:10]
            pulled_payload: List[Dict] = []
            logger.info(f"Fila pronta para {target_server_id}: aguardando 5s antes de mover jogadores.")
            await asyncio.sleep(5.0)

            still_ready: List[discord.Member] = []
            for member in selected:
                if not member.voice or not member.voice.channel:
                    continue
                if member.voice.channel.id != SALA_PROXIMO_ID:
                    continue
                still_ready.append(member)

            if len(still_ready) < 10:
                logger.warning(
                    f"Fila pronta cancelada para {target_server_id}: "
                    f"somente {len(still_ready)}/10 permaneceram na fila apos espera de 5s."
                )
                if fila_cog:
                    await fila_cog.requeue_payload(jogadores, dispatch_ready=True)
                await refresh_server_category_visibility(self.bot, target_server_id, reason="fila_cancelled_wait")
                return

            selected = still_ready
            for member in selected:
                await safe_move_member(member, picks_voice, context=f"{target_server_id}:fila_pronta")

            # Aguarda o cache de voz estabilizar para evitar falso negativo de "movidos".
            await asyncio.sleep(1.0)
            lobby_ids = {m.id for m in picks_voice.members if not m.bot}
            moved: List[discord.Member] = [member for member in selected if member.id in lobby_ids]

            if len(moved) < 10 and fila_cog:
                needed = 10 - len(moved)
                logger.warning(
                    f"Fila pronta parcial para {target_server_id}: movidos={len(moved)}. "
                    f"Tentando completar com +{needed} da fila."
                )
                extra_members = await fila_cog.take_next_players(guild, needed)
                now = discord.utils.utcnow()
                for member in extra_members:
                    pulled_payload.append(
                        {
                            "member": member,
                            "join_time": now,
                            "damage": 0,
                        }
                    )
                    await safe_move_member(member, picks_voice, context=f"{target_server_id}:fila_pronta_fill")

                if extra_members:
                    await asyncio.sleep(1.0)
                    lobby_ids = {m.id for m in picks_voice.members if not m.bot}
                    moved_map = {member.id: member for member in moved}
                    for member in extra_members:
                        if member.id in lobby_ids:
                            moved_map[member.id] = member
                    moved = list(moved_map.values())

            if len(moved) != 10:
                logger.warning(
                    f"Fila pronta incompleta para {target_server_id}: movidos={len(moved)}. Repondo fila."
                )
                if sala_proximo:
                    for member in moved:
                        await safe_move_member(member, sala_proximo, context=f"{target_server_id}:rollback_fila")
                if fila_cog:
                    await fila_cog.requeue_payload(jogadores + pulled_payload, dispatch_ready=True)
                await refresh_server_category_visibility(self.bot, target_server_id, reason="fila_rollback")
                return

            started = await self._bootstrap_accept_flow(
                s_id=target_server_id,
                channel=picks_text,
                players=moved,
            )
            if not started:
                logger.warning(f"Nao foi possivel iniciar fluxo ACCEPT em {target_server_id}. Repondo fila.")
                if sala_proximo:
                    for member in moved:
                        await safe_move_member(member, sala_proximo, context=f"{target_server_id}:rollback_accept")
                if fila_cog:
                    await fila_cog.requeue_payload(jogadores, dispatch_ready=True)
                await refresh_server_category_visibility(self.bot, target_server_id, reason="accept_rollback")

    @tasks.loop(hours=4)
    async def db_keep_alive(self):
        try:
            ok = await ping_db()
            if ok:
                logger.info("✅ DB Keep-Alive OK.")
            else:
                logger.warning("DB Keep-Alive falhou; aguardando reconexao.")
        except Exception as e:
            logger.warning(f"DB Keep-Alive falhou: {e}")


    @tasks.loop(seconds=10)
    async def server_monitor_loop(self):
        try:
            for s_id, server in SERVERS.items():
                if not server["active"]: continue
                sess = sessions.get(s_id)
                if sess and sess.get("suspend_auto_restore"):
                    continue
                if sessions.get(s_id, {}).get("active") and sessions[s_id].get("match_id"):
                    try:
                        rows = await get_active_matches()
                        if not any(r.get("server_id") == s_id for r in rows):
                            await set_active_match(s_id, sessions[s_id]["match_id"])
                    except:
                        pass
                try:
                    await refresh_server_category_visibility(self.bot, s_id, reason="monitor_loop")
                except Exception:
                    pass
            await update_monitor_combined(
                self.bot, sessions, global_state, reset_session,
                get_match_overview, get_online_count, get_progress_bar,
                get_active_matches
            )
        except Exception as e: logger.error(f"Erro Monitor: {e}")

    async def _ensure_session_steamids(self, session: dict) -> Dict[int, str]:
        steamids = session.get("player_steamids")
        if not isinstance(steamids, dict):
            steamids = {}
        players = session.get("players") or []
        for player in players:
            if not player:
                continue
            sid = str(steamids.get(player.id) or "").strip()
            if sid:
                steamids[player.id] = sid
                continue
            try:
                rank_data = await get_player_rank(player.id)
            except Exception:
                rank_data = None
            if rank_data and rank_data.get("steamid64"):
                steamids[player.id] = str(rank_data["steamid64"]).strip()
        session["player_steamids"] = steamids
        return steamids

    async def _bootstrap_accept_flow(
        self,
        *,
        s_id: str,
        channel: discord.abc.GuildChannel,
        players: List[discord.Member],
        restart_reason: Optional[str] = None,
    ) -> bool:
        if len(players) != 10:
            return False

        reset_session(s_id)
        session = sessions[s_id]
        session["suspend_auto_restore"] = False

        await set_active_session(s_id)
        session.update(
            {
                "active": True,
                "status": "ACCEPT",
                "players": players.copy(),
                "accept_expires_at": int(discord.utils.utcnow().timestamp()) + int(MIX_ACCEPT_TIMEOUT),
            }
        )
        await refresh_server_category_visibility(
            self.bot,
            s_id,
            reason="bootstrap_accept",
            force_visible=True,
        )

        faceit_tasks = {}
        fila_cog = get_fila_cog(self.bot)
        if fila_cog:
            await fila_cog.remove_players_from_queue([p.id for p in session["players"]])
        for p in session["players"]:
            r_data = await get_player_rank(p.id)
            session["player_ratings"][p.id] = r_data['rating'] if r_data else 1000
            if r_data and r_data.get("steamid64"):
                session["player_steamids"][p.id] = str(r_data["steamid64"]).strip()
                faceit_tasks[p.id] = asyncio.create_task(get_faceit_profile(str(r_data["steamid64"])))

        session["player_rank_positions"] = await get_rank_positions([p.id for p in session["players"]])

        if faceit_tasks:
            async def _load_faceit():
                ids = list(faceit_tasks.keys())
                results = await asyncio.gather(*faceit_tasks.values(), return_exceptions=True)
                for i, res in enumerate(results):
                    if isinstance(res, dict):
                        session["faceit_info"][ids[i]] = res
            asyncio.create_task(_load_faceit())

        await schedule_queue_update(self.bot)

        missing_players = build_accept_missing_players(session)
        missing_text = build_accept_missing_mentions(missing_players)
        title = f"MIX REINICIADO - {SERVERS[s_id]['name']}" if restart_reason else f"MIX INICIADO - {SERVERS[s_id]['name']}"
        embed = discord.Embed(title=title, color=0xf1c40f)
        if restart_reason:
            embed.add_field(name="Motivo", value=restart_reason, inline=False)
        embed.add_field(name="Progresso", value=f"{get_progress_bar(0)} (0/10)", inline=False)
        embed.add_field(name="Prazo para aceitar", value=build_accept_deadline_text(session), inline=False)
        embed.add_field(name="Aguardando confirmacao de:", value=missing_text, inline=False)

        session["accept_message"] = await channel.send(embed=embed)
        await session["accept_message"].edit(view=AcceptMixView(s_id))
        session["accept_task"] = asyncio.create_task(accept_timeout(self.bot, channel, s_id))
        return True

    async def _recover_notconnect_flow(
        self,
        s_id: str,
        missing_steamids: set[str],
        db_rows: List[Dict],
    ):
        session = sessions.get(s_id)
        if not session or not session.get("active"):
            return

        server = SERVERS.get(s_id)
        if not server:
            return

        picks_text = self.bot.get_channel(server["channels"].get("picks_text"))
        if not picks_text:
            logger.warning(f"Recovery notconnect sem canal de texto ({s_id})")
            return

        guild = picks_text.guild
        picks_voice = guild.get_channel(server["channels"].get("picks_voice"))
        sala_saida = guild.get_channel(SALA_SAIDA_ID) if SALA_SAIDA_ID else None

        if not picks_voice:
            logger.warning(f"Recovery notconnect sem canal de picks voice ({s_id})")
            return

        players = list(session.get("players") or [])
        steamids = await self._ensure_session_steamids(session)

        connected_players: List[discord.Member] = []
        missing_players: List[discord.Member] = []
        for player in players:
            sid = str(steamids.get(player.id) or "").strip()
            if sid and sid in missing_steamids:
                missing_players.append(player)
            else:
                connected_players.append(player)

        for player in connected_players:
            if not player.voice or not player.voice.channel:
                continue
            if player.voice.channel.id == picks_voice.id:
                continue
            await safe_move_member(player, picks_voice, context=f"{s_id}:notconnect-recover-connected")

        for player in missing_players:
            if not player.voice or not player.voice.channel or not sala_saida:
                continue
            if player.voice.channel.id == sala_saida.id:
                continue
            await safe_move_member(player, sala_saida, context=f"{s_id}:notconnect-recover-missing")

        previous_match_id = session.get("match_id")
        if previous_match_id:
            try:
                release_result = await get_server_pool().release_server_for_match(
                    int(previous_match_id),
                    reason="notconnect_recovery",
                    restart_runtime=True,
                )
                if release_result.get("restart_error"):
                    logger.warning(
                        f"Recovery notconnect: runtime reinicio com falha ({previous_match_id}): "
                        f"{release_result.get('restart_error')}"
                    )
            except Exception as e:
                logger.error(f"Recovery notconnect: erro ao liberar runtime server ({previous_match_id}): {e}")
        reset_session(s_id)
        sessions[s_id]["suspend_auto_restore"] = True
        try:
            await clear_active_match(s_id)
        except Exception as e:
            logger.error(f"Recovery notconnect: erro ao limpar active_match ({s_id}): {e}")
        try:
            await clear_active_session(s_id)
        except Exception as e:
            logger.error(f"Recovery notconnect: erro ao limpar active_session ({s_id}): {e}")
        if previous_match_id:
            try:
                await clear_reserved_match_id(int(previous_match_id))
            except Exception as e:
                logger.error(f"Recovery notconnect: erro ao limpar match_id reservado ({previous_match_id}): {e}")
        await refresh_server_category_visibility(self.bot, s_id, reason="notconnect_reset")

        needed = max(0, 10 - len(connected_players))
        pulled_members: List[discord.Member] = []
        if needed > 0:
            fila_cog = get_fila_cog(self.bot)
            candidates: List[discord.Member] = []
            if fila_cog:
                candidates = await fila_cog.take_next_players(guild, needed)
            for member in candidates:
                await safe_move_member(member, picks_voice, context=f"{s_id}:notconnect-recover-queue")
                if member.voice and member.voice.channel and member.voice.channel.id == picks_voice.id:
                    pulled_members.append(member)

        await update_queue_display(self.bot)

        recovered_names = [p.display_name for p in connected_players]
        missing_names = [p.display_name for p in missing_players]
        pulled_names = [p.display_name for p in pulled_members]
        embed = discord.Embed(
            title=f"⛔ Partida cancelada por não conexão ({SERVERS[s_id]['name']})",
            color=0xe67e22,
            description="Fluxo automático acionado para reaproveitar quem conectou e repor pela fila.",
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Conectaram e voltaram para picks", value="\n".join([f"✅ {n}" for n in recovered_names]) or "-", inline=False)
        embed.add_field(name="Não conectaram", value="\n".join([f"❌ {n}" for n in missing_names]) or "-", inline=False)
        if pulled_names:
            embed.add_field(name="Puxados da fila (ordem)", value="\n".join([f"➡️ {n}" for n in pulled_names]), inline=False)

        restart_players = [m for m in picks_voice.members if not m.bot]
        restarted = False
        if len(restart_players) >= 10:
            restart_players = restart_players[:10]
            restarted = await self._bootstrap_accept_flow(
                s_id=s_id,
                channel=picks_text,
                players=restart_players,
                restart_reason="Partida anterior cancelada por não conexão.",
            )

        if restarted:
            embed.add_field(name="Próximo passo", value="✅ Mix reiniciado automaticamente na fase de ACEITAR.", inline=False)
            await refresh_server_category_visibility(
                self.bot,
                s_id,
                reason="notconnect_restarted",
                force_visible=True,
            )
        else:
            embed.add_field(
                name="Próximo passo",
                value="⚠️ Não foi possível fechar 10 jogadores. Complete a sala de picks e inicie novamente.",
                inline=False,
            )
            await refresh_server_category_visibility(self.bot, s_id, reason="notconnect_no_restart")
        await picks_text.send(embed=embed)

    @tasks.loop(seconds=12)
    async def notconnect_recovery_loop(self):
        for s_id, server in SERVERS.items():
            try:
                if not server.get("active"):
                    continue
                session = sessions.get(s_id)
                if not session:
                    continue
                if not session.get("active") or session.get("status") != "LIVE":
                    continue
                if session.get("recovering_notconnect") or session.get("notconnect_recovered"):
                    continue

                match_id = session.get("match_id")
                if not match_id:
                    continue
                since_utc = session.get("live_started_at")
                rows = await get_matchguardian_notconnect_rows(str(match_id), since_utc=since_utc, limit=40)
                if not rows:
                    continue

                missing_steamids = {
                    str(row.get("steamid64") or "").strip()
                    for row in rows
                    if str(row.get("steamid64") or "").strip()
                }
                if not missing_steamids:
                    continue

                logger.warning(
                    f"Recovery notconnect detectado ({s_id}) match={match_id} missing={len(missing_steamids)}"
                )
                session["recovering_notconnect"] = True
                session["notconnect_recovered"] = True
                await self._recover_notconnect_flow(s_id, missing_steamids, rows)
            except Exception as e:
                logger.error(f"Recovery notconnect falhou ({s_id}): {type(e).__name__}: {e}")
            finally:
                current = sessions.get(s_id)
                if current is not None:
                    current["recovering_notconnect"] = False

    @notconnect_recovery_loop.before_loop
    async def before_notconnect_recovery_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(3)

    @app_commands.command(name="fixpainel", description="Reseta o painel de monitoramento.")
    async def fix_painel(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        ch = self.bot.get_channel(CANAL_MONITOR_ID)
        if ch: await ch.purge(limit=10)
        global_state["monitor_msgs"] = {}; await ctx.send("✅ Painel resetado.", delete_after=5)
    @app_commands.command(name="cancelarmix", description="Cancela o mix ativo no servidor atual.")
    async def cancelar_mix(self, interaction: discord.Interaction, server_num: Optional[str] = None):
        ctx = await commands.Context.from_interaction(interaction)
        await self._cancelar_mix_impl(
            guild=ctx.guild,
            channel=ctx.channel,
            send=ctx.send,
            server_num=server_num,
        )

    async def _cancelar_mix_impl(
        self,
        *,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel,
        send,
        server_num: Optional[str] = None,
    ):
        s_id = None
        if server_num:
            s_id = f"server{server_num}"
            if s_id not in SERVERS:
                await send("? Server invalido. Use um servidor configurado (ex.: 1, 2, 3, 4 ou 5).")
                return
        else:
            s_id = next((i for i, s in SERVERS.items() if channel.id == s["channels"]["picks_text"]), None)
            if not s_id:
                await send("? Use este comando no canal de picks ou passe o numero do servidor. Ex: /cancelarmix 1")
                return

        session = sessions.get(s_id, {})
        try:
            task = session.get("accept_task")
            if task:
                task.cancel()
                session["accept_task"] = None
        except Exception:
            pass
        session["active"] = False
        session["status"] = "IDLE"

        match_id = session.get("match_id")
        if match_id:
            try:
                release_result = await get_server_pool().release_server_for_match(
                    int(match_id),
                    reason="manual_cancel",
                    restart_runtime=True,
                )
                if release_result.get("restart_error"):
                    logger.warning(
                        f"Erro ao reiniciar runtime server no cancelamento ({s_id}/{match_id}): "
                        f"{release_result.get('restart_error')}"
                    )
            except Exception as e:
                logger.warning(f"Erro ao liberar runtime server no cancelamento ({s_id}/{match_id}): {e}")
        server = SERVERS.get(s_id)
        if server:
            picks_voice = guild.get_channel(server["channels"].get("picks_voice"))
            team1_voice = guild.get_channel(server["channels"].get("team1_voice"))
            team2_voice = guild.get_channel(server["channels"].get("team2_voice"))
            targets = [team1_voice, team2_voice]
            for vc in targets:
                if not vc:
                    continue
                for member in list(vc.members):
                    if member.bot:
                        continue
                    if picks_voice:
                        try:
                            await member.move_to(picks_voice)
                        except Exception:
                            pass

        reset_session(s_id)
        sessions[s_id]["suspend_auto_restore"] = True
        try:
            await clear_active_match(s_id)
        except Exception as e:
            logger.error(f"Erro ao limpar active_matches ({s_id}): {e}")
        try:
            await clear_active_session(s_id)
        except Exception as e:
            logger.error(f"Erro ao limpar active_session ({s_id}): {e}")
        if match_id:
            try:
                await clear_reserved_match_id(int(match_id))
            except Exception as e:
                logger.error(f"Erro ao limpar match_id reservado ({match_id}): {e}")
        await refresh_server_category_visibility(self.bot, s_id, reason="manual_cancel")

        await send(f"Mix cancelado para {s_id}.")
    async def restore_active_matches_on_startup(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(2)
        try:
            rows = await get_active_matches()
        except Exception as e:
            logger.error(f"❌ Erro ao recuperar partidas ativas: {e}")
            return
        for row in rows:
            s_id = row.get("server_id")
            m_id = row.get("match_id")
            if s_id in sessions and m_id:
                sessions[s_id].update({
                    "active": True,
                    "status": "LIVE",
                    "match_id": int(m_id),
                    "notconnect_recovered": True,  # evita que o recovery rode numa partida já em andamento após restart do bot
                })
                try:
                    runtime_row = await get_match_runtime_server(int(m_id))
                except Exception:
                    runtime_row = None
                if runtime_row:
                    sessions[s_id].update(
                        {
                            "runtime_server_id": runtime_row.get("runtime_server_id"),
                            "runtime_tmux_session": runtime_row.get("tmux_session"),
                        }
                    )
                await refresh_server_category_visibility(self.bot, s_id, reason="restore_active")
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return

        before_sid = resolve_server_by_channel_id(getattr(before.channel, "id", 0) if before.channel else 0)
        after_sid = resolve_server_by_channel_id(getattr(after.channel, "id", 0) if after.channel else 0)
        touched_servers = {sid for sid in (before_sid, after_sid) if sid}
        for sid in touched_servers:
            await refresh_server_category_visibility(self.bot, sid, reason="voice_state")

        logs_channel = self.bot.get_channel(CANAL_LOGS_ID) if CANAL_LOGS_ID else None
        if not logs_channel:
            return

        def resolve_log_label(channel_id):
            if channel_id == SALA_PROXIMO_ID:
                return "Próximo"
            for srv in SERVERS.values():
                if channel_id == srv["channels"]["picks_voice"]:
                    return srv["name"]
            return None

        ts = datetime.now().strftime("%H:%M")
        if after.channel:
            label = resolve_log_label(after.channel.id)
            if label:
                try: await logs_channel.send(f"[{ts}] 🟢 **ENTROU** | **{member.display_name}** → **{label}**")
                except: pass
        if before.channel:
            label = resolve_log_label(before.channel.id)
            if label:
                try: await logs_channel.send(f"[{ts}] 🔴 **SAIU** | **{member.display_name}** → **{label}**")
                except: pass

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await sync_member_role_if_registered(member)
        await send_welcome_if_needed(member)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        if not _is_managed_mix_text_channel(getattr(message.channel, "id", 0)):
            return
        _schedule_mix_text_channel_cleanup(message.channel)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild:
            return
        if not _is_managed_mix_text_channel(getattr(after.channel, "id", 0)):
            return
        _schedule_mix_text_channel_cleanup(after.channel)

    @app_commands.command(name="startmix", description="Inicia o mix com 10 jogadores no servidor do canal atual.")
    async def startmix(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)
        channel = interaction.channel
        author = interaction.user if isinstance(interaction.user, discord.Member) else None

        if channel is None:
            await interaction.followup.send("Nao consegui identificar o canal deste comando.", ephemeral=True)
            return
        if author is None:
            await interaction.followup.send("Este comando deve ser usado dentro do servidor.", ephemeral=True)
            return

        async def _send(msg: str):
            await interaction.followup.send(msg, ephemeral=True)

        started = await self._startmix_impl(
            channel=channel,
            author=author,
            send=_send,
        )
        if started:
            await interaction.followup.send("✅ Fluxo de aceitacao iniciado.", ephemeral=True)

    async def _startmix_impl(self, *, channel: discord.abc.GuildChannel, author: discord.Member, send):
        s_id = next((i for i, s in SERVERS.items() if channel.id == s["channels"]["picks_text"]), None)
        if not s_id or sessions[s_id]["active"]:
            await send("Canal invalido ou mix ativo.")
            return False
        if await is_active_session(s_id):
            await send("Ja existe uma sessao ativa neste servidor.")
            return False
        if not author.voice or author.voice.channel.id != SERVERS[s_id]["channels"]["picks_voice"]:
            await send("Entre na sala de voz.")
            return False

        sala = author.voice.channel
        players = [m for m in sala.members if not m.bot]
        if len(players) != 10:
            await send("E necessario ter 10 jogadores na sala de voz.")
            return False

        await refresh_server_category_visibility(
            self.bot,
            s_id,
            reason="startmix_manual",
            force_visible=True,
        )
        await self._bootstrap_accept_flow(s_id=s_id, channel=channel, players=players)
        return True

# ================= VIEWS (BOTOES) =================

class TorneioInscricaoView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Button(
            label="📋 Inscreva-se",
            style=discord.ButtonStyle.link,
            url="https://forms.gle/VpQLYXW43MkGwLjB8",
        ))

class AcceptMixView(View):
    def __init__(self, s_id): super().__init__(timeout=None); self.s_id = s_id
    @discord.ui.button(label="✅ ACEITAR", style=discord.ButtonStyle.success, custom_id="accept_mix_btn")
    async def accept(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        session = sessions[self.s_id]
        if session.get("status") not in ("ACCEPT", "BOOTING"):
            return
        if interaction.user not in session["players"]: return
        if interaction.user.id in session["accepts"]: return
        session["accepts"].add(interaction.user.id)
        total = len(session["accepts"])
        await interaction.followup.send("✅ **Você aceitou a partida!**", ephemeral=True)
        try:
            missing_players = build_accept_missing_players(session)
            missing_text = build_accept_missing_mentions(missing_players)
            embed = discord.Embed(title=f"🚨 PARTIDA ENCONTRADA - {SERVERS[self.s_id]['name']}", color=0xf1c40f)
            embed.add_field(name="Progresso", value=f"{get_progress_bar(total)} ({total}/10)", inline=False)
            embed.add_field(name="Prazo para aceitar", value=build_accept_deadline_text(session), inline=False)
            if missing_players:
                embed.add_field(name="⏳ Aguardando Confirmação de:", value=missing_text, inline=False)
            await session["accept_message"].edit(embed=embed, view=self)
        except: pass
        if total == 10:
            if session["accept_task"]: session["accept_task"].cancel()
            try: await session["accept_message"].delete()
            except: pass
            session["status"] = "BOOTING"
            if not session.get("match_id"):
                try:
                    session["match_id"] = await reserve_match_id(self.s_id)
                except Exception as e:
                    logger.error(f"Erro ao reservar match_id ({self.s_id}): {e}")
                    await interaction.channel.send("⛔ Falha ao reservar MatchID. Tente novamente.")
                    reset_session(self.s_id)
                    sessions[self.s_id]["suspend_auto_restore"] = True
                    await refresh_server_category_visibility(interaction.client, self.s_id, reason="accept_matchid_error")
                    return
            try:
                server = SERVERS.get(self.s_id)
                preferred_runtime_id = str(server.get("runtime_id") or "").strip() if server else ""
                runtime = await get_server_pool().boot_runtime_for_match(
                    match_id=int(session["match_id"]),
                    source="mix",
                    lobby_server_id=self.s_id,
                    preferred_runtime_id=(preferred_runtime_id or None),
                    strict_preferred_runtime=True,
                )
            except NoServerAvailableError:
                await interaction.channel.send("⛔ Nenhum servidor livre no pool (1-5). Aguarde um mix finalizar.")
                reset_session(self.s_id)
                sessions[self.s_id]["suspend_auto_restore"] = True
                await refresh_server_category_visibility(interaction.client, self.s_id, reason="accept_pool_unavailable")
                return
            except Exception as e:
                logger.error(f"BOOT runtime falhou ({self.s_id}/{session.get('match_id')}): {e}")
                await interaction.channel.send(f"⛔ Falha ao ligar servidor local: `{_compact_rcon_error(e)}`")
                reset_session(self.s_id)
                sessions[self.s_id]["suspend_auto_restore"] = True
                await refresh_server_category_visibility(interaction.client, self.s_id, reason="accept_boot_error")
                return
            session.update(
                {
                    "runtime_server_id": runtime.get("runtime_id"),
                    "runtime_tmux_session": runtime.get("tmux_session"),
                    "runtime_host": runtime.get("host"),
                    "runtime_port": runtime.get("port"),
                    "runtime_gotv_port": runtime.get("gotv_port"),
                    "runtime_already_online": bool(runtime.get("already_online")),
                }
            )
            allocated_runtime_id = str(runtime.get("runtime_id") or "").strip()
            if preferred_runtime_id and allocated_runtime_id and allocated_runtime_id != preferred_runtime_id:
                logger.error(
                    f"BOOT runtime divergente para {self.s_id}: lobby espera {preferred_runtime_id}, "
                    f"mas pool retornou {allocated_runtime_id}."
                )
                await interaction.channel.send(
                    "⛔ O servidor deste lobby ficou indisponivel durante o boot. "
                    "O mix foi cancelado para evitar mandar voces para o server errado."
                )
                reset_session(self.s_id)
                sessions[self.s_id]["suspend_auto_restore"] = True
                await refresh_server_category_visibility(
                    interaction.client,
                    self.s_id,
                    reason="accept_runtime_mismatch",
                )
                return
            await start_faceit_draft(interaction.channel, self.s_id)

    @discord.ui.button(label="❌ RECUSAR", style=discord.ButtonStyle.danger, custom_id="reject_mix_btn")
    async def reject(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        session = sessions[self.s_id]
        if interaction.user not in session["players"]: return
        if session["accept_task"]: session["accept_task"].cancel()
        try: await session["accept_message"].delete()
        except: pass
        await interaction.channel.send(f"🚫 **Mix recusado por:** {interaction.user.display_name}")
        reset_session(self.s_id)
        await refresh_server_category_visibility(interaction.client, self.s_id, reason="accept_rejected")

class CaptainVoteView(View):
    def __init__(self, players, s_id): super().__init__(timeout=None); [self.add_item(CaptainVoteButton(p, s_id)) for p in players]

class CaptainVoteButton(Button):
    def __init__(self, player, s_id):
        session = sessions.get(s_id, {})
        faceit = session.get("faceit_info", {}).get(player.id) or {}
        lvl = faceit.get("level")
        is_smurf = faceit.get("is_smurf", False)
        label = player.display_name
        if is_smurf:
            label = f"{label} • Lv {lvl} 🔵"
        elif lvl is not None:
            ball = faceit_ball(lvl)
            label = f"{label} • Lv {lvl} {ball}"
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.player, self.s_id = player, s_id
    async def callback(self, interaction):
        session = sessions[self.s_id]
        if interaction.user not in session["players"]: return
        uid = interaction.user.id
        votos = session["voted_users"].get(uid, [])
        if self.player.id in votos or len(votos) >= 2: return await interaction.response.send_message("❌ Voto inválido.", ephemeral=True)
        votos.append(self.player.id)
        session["voted_users"][uid] = votos
        session["captain_votes"][self.player.id] = session["captain_votes"].get(self.player.id, 0) + 1
        await interaction.response.send_message("🗳️ Voto computado.", ephemeral=True)
        await update_vote_message(self.s_id)

class PickView(View):
    def __init__(self, s_id): super().__init__(timeout=None); self.s_id = s_id; [self.add_item(PickButton(p, s_id)) for p in sessions[s_id]["available"]]

class PickButton(Button):
    def __init__(self, player, s_id): super().__init__(label=player.display_name, style=discord.ButtonStyle.secondary); self.player, self.s_id = player, s_id
    async def callback(self, interaction):
        await interaction.response.defer()
        lock = get_draft_lock(self.s_id)
        async with lock:
            session = sessions.get(self.s_id)
            if not session or session.get("status") != "DRAFT":
                await interaction.followup.send("Draft nao esta ativo.", ephemeral=True)
                return

            turn = session.get("turn")
            turn_id = getattr(turn, "id", None)
            if turn_id != interaction.user.id:
                await interaction.followup.send("Nao e sua vez de escolher.", ephemeral=True)
                return

            chosen = next((p for p in session.get("available", []) if p.id == self.player.id), None)
            if not chosen:
                await interaction.followup.send("Este jogador ja foi escolhido.", ephemeral=True)
                return

            remaining_before = len(session["available"])
            cap1 = session["captains"][0]
            cap2 = session["captains"][1]
            allow_team2_double_pick = remaining_before == 3 and interaction.user.id == cap2.id
            session["available"].remove(chosen)

            team = session["team1"] if interaction.user.id == cap1.id else session["team2"]
            team.append(chosen)

            v_id = (
                SERVERS[self.s_id]["channels"]["team1_voice"]
                if interaction.user.id == cap1.id
                else SERVERS[self.s_id]["channels"]["team2_voice"]
            )
            asyncio.create_task(
                safe_move_member(
                    chosen,
                    interaction.guild.get_channel(v_id),
                    context=f"{self.s_id}:pick-main",
                )
            )

            if allow_team2_double_pick:
                session["turn"] = cap2
            else:
                session["turn"] = cap2 if interaction.user.id == cap1.id else cap1

            if len(session["available"]) == 1:
                last = session["available"].pop()
                l_team = session["team1"] if getattr(session["turn"], "id", None) == cap1.id else session["team2"]
                l_team.append(last)
                l_v_id = (
                    SERVERS[self.s_id]["channels"]["team1_voice"]
                    if getattr(session["turn"], "id", None) == cap1.id
                    else SERVERS[self.s_id]["channels"]["team2_voice"]
                )
                asyncio.create_task(
                    safe_move_member(
                        last,
                        interaction.guild.get_channel(l_v_id),
                        context=f"{self.s_id}:pick-last",
                    )
                )
                session["maps"] = MAPS_BASE.copy()
                session["turn"] = None
                session["status"] = "VETO"
                session["veto_round"] = 0
                session["veto_votes"] = {}
                session["veto_player_votes"] = {}
                await start_map_veto_round(self.s_id, interaction.channel)
            else:
                await session["message"].edit(embed=create_pick_embed(self.s_id), view=PickView(self.s_id))

class MapVoteView(View):
    def __init__(self, s_id):
        super().__init__(timeout=None)
        self.s_id = s_id
        session = sessions[s_id]
        veto_votes = session.get("veto_votes", {})
        for mapa in session["maps"]:
            vote_count = len(veto_votes.get(mapa, set()))
            self.add_item(MapVoteButton(mapa, s_id, vote_count))

class MapVoteButton(Button):
    def __init__(self, mapa, s_id, vote_count):
        label = f"{mapa} ({vote_count})" if vote_count > 0 else mapa
        super().__init__(label=label, style=discord.ButtonStyle.danger)
        self.mapa, self.s_id = mapa, s_id

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        lock = get_map_veto_lock(self.s_id)
        votes_used = None
        max_votes_out = None
        async with lock:
            session = sessions.get(self.s_id)
            if not session or session.get("status") != "VETO":
                await interaction.followup.send("Veto não está ativo.", ephemeral=True)
                return

            round_num = session.get("veto_round", 1)
            team_num = 1 if round_num % 2 == 1 else 2
            banning_team = session["team1"] if team_num == 1 else session["team2"]

            if interaction.user not in banning_team:
                other_label = "Time 1" if team_num == 2 else "Time 2"
                await interaction.followup.send(f"Não é a vez do seu time. Aguarde o **{other_label}** terminar.", ephemeral=True)
                return

            if self.mapa not in session.get("maps", []):
                await interaction.followup.send("Este mapa já foi vetado.", ephemeral=True)
                return

            max_votes = 3 if round_num <= 2 else 1
            max_votes_out = max_votes
            veto_votes = session.setdefault("veto_votes", {})
            veto_player_votes = session.setdefault("veto_player_votes", {})

            if interaction.user.id in veto_votes.get(self.mapa, set()):
                await interaction.followup.send(f"Você já votou em **{self.mapa}**.", ephemeral=True)
                return

            player_voted_maps = veto_player_votes.get(interaction.user.id, set())
            if len(player_voted_maps) >= max_votes:
                await interaction.followup.send(f"Você já usou todos os seus **{max_votes}** voto(s).", ephemeral=True)
                return

            veto_votes.setdefault(self.mapa, set()).add(interaction.user.id)
            veto_player_votes.setdefault(interaction.user.id, set()).add(self.mapa)
            votes_used = len(veto_player_votes[interaction.user.id])

            # Verifica se todos do time banning já usaram todos os votos
            all_voted = all(
                len(veto_player_votes.get(p.id, set())) >= max_votes
                for p in banning_team
            )

            try:
                await session["message"].edit(
                    embed=create_map_voting_embed(self.s_id),
                    view=MapVoteView(self.s_id)
                )
            except Exception:
                pass

            if all_voted:
                task = session.get("map_veto_task")
                channel = session.get("veto_channel")
                if task:
                    task.cancel()
                    session["map_veto_task"] = None
                if channel:
                    asyncio.create_task(_execute_veto_round(self.s_id, channel))

        if votes_used is not None:
            await interaction.followup.send(
                f"✅ Votou em **{self.mapa}** ({votes_used}/{max_votes_out} votos usados).",
                ephemeral=True
            )

# ================= LÓGICA DE DRAFT =================

async def update_vote_message(s_id):
    session = sessions[s_id]
    if not session.get("message"): return
    embed = discord.Embed(title=f"🗳️ VOTAÇÃO - {SERVERS[s_id]['name']}", color=0x9b59b6)
    embed.description = "\n".join([f"**{p.display_name}**: {session['captain_votes'].get(p.id, 0)} votos" for p in session["players"]])
    try: await session["message"].edit(embed=embed)
    except: pass

def _select_top_n_voted(available_maps, veto_votes, n):
    """Selects up to n maps with most votes, leaving at least 1 map. Random tie-breaking."""
    n = min(n, len(available_maps) - 1)
    if n <= 0:
        return []
    counted = [(m, len(veto_votes.get(m, set()))) for m in available_maps]
    random.shuffle(counted)
    counted.sort(key=lambda x: x[1], reverse=True)
    return [m for m, _ in counted[:n]]


def _map_veto_timeout_for_round(round_num: int) -> int:
    return int(MAP_VETO_TIMEOUT if int(round_num or 1) <= 2 else MAP_VETO_FINAL_TIMEOUT)

async def start_map_veto_round(s_id, channel):
    session = sessions.get(s_id)
    if not session:
        return
    session["veto_round"] = session.get("veto_round", 0) + 1
    session["veto_votes"] = {}
    session["veto_player_votes"] = {}
    round_num = session["veto_round"]
    bans = 3 if round_num <= 2 else 1
    session["veto_expires_at"] = int(discord.utils.utcnow().timestamp()) + _map_veto_timeout_for_round(round_num)
    team_label = "Time 1" if round_num % 2 == 1 else "Time 2"
    logger.info(f"VETO rodada {round_num} — {team_label} banindo {bans} mapa(s) ({s_id})")
    try:
        await session["message"].edit(
            embed=create_map_voting_embed(s_id),
            view=MapVoteView(s_id)
        )
    except Exception:
        pass
    await schedule_map_veto_timeout(s_id, channel)

async def _execute_veto_round(s_id, channel):
    """Processa o fim de uma rodada de veto (compartilhado entre timeout e early-finish)."""
    lock = get_map_veto_lock(s_id)
    should_finish = False
    next_round = False
    banned_maps = []
    round_num_out = 1
    team_label_out = "Time 1"
    async with lock:
        session = sessions.get(s_id)
        if session:
            session["map_veto_task"] = None
        if not session or session.get("status") != "VETO":
            return
        if not session.get("maps") or not session.get("message"):
            return

        round_num = session.get("veto_round", 1)
        round_num_out = round_num
        team_label_out = "Time 1" if round_num % 2 == 1 else "Time 2"
        bans_this_round = 3 if round_num <= 2 else 1
        veto_votes = session.get("veto_votes", {})
        available = list(session["maps"])

        to_ban = _select_top_n_voted(available, veto_votes, bans_this_round)
        banned_maps = to_ban
        for m in to_ban:
            session["maps"].remove(m)

        if len(session["maps"]) <= 1:
            if len(session["maps"]) == 0 and to_ban:
                session["maps"] = [to_ban[-1]]
                banned_maps = to_ban[:-1]
                logger.warning(f"VETO inconsistente ({s_id}): restaurando mapa final.")
            session["status"] = "LOADING"
            should_finish = True
        else:
            next_round = True

    if should_finish:
        dummy = SimpleNamespace(channel=channel, guild=channel.guild)
        await finish_map_veto(dummy, s_id)
    elif next_round:
        await start_map_veto_round(s_id, channel)


async def schedule_map_veto_timeout(s_id, channel):
    session = sessions.get(s_id)
    if not session or not channel:
        return
    if session.get("map_veto_task"):
        session["map_veto_task"].cancel()

    session["veto_channel"] = channel
    round_num = int(session.get("veto_round", 1) or 1)
    timeout_seconds = _map_veto_timeout_for_round(round_num)

    async def _timeout():
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            return
        await _execute_veto_round(s_id, channel)

    session["map_veto_task"] = asyncio.create_task(_timeout())

async def accept_timeout(bot, channel, s_id):
    await asyncio.sleep(MIX_ACCEPT_TIMEOUT)
    session = sessions[s_id]
    if not session.get("active") or session.get("status") != "ACCEPT":
        return
    if len(session["accepts"]) < 10:
        guild = channel.guild
        picks_voice = guild.get_channel(SERVERS[s_id]["channels"]["picks_voice"])
        sala_saida = guild.get_channel(SALA_SAIDA_ID) if SALA_SAIDA_ID else None

        missing_players = [p for p in session["players"] if p.id not in session["accepts"]]
        removed_names = []
        for player in missing_players:
            if player.voice:
                try:
                    await player.move_to(sala_saida)
                    removed_names.append(player.display_name)
                except:
                    pass
        session["players"] = [p for p in session["players"] if p.id in session["accepts"]]
        session["accepts"] = set([p.id for p in session["players"]])

        remaining_players = [p for p in session["players"] if p.id in session["accepts"]]
        needed = max(0, 10 - len(remaining_players))
        pulled = 0
        pulled_names = []
        fila_cog = get_fila_cog(bot)
        candidates: List[discord.Member] = []
        if fila_cog and needed > 0:
            candidates = await fila_cog.take_next_players(guild, needed)
        for member in candidates:
            try:
                await member.move_to(picks_voice)
                if member.voice and member.voice.channel and member.voice.channel.id == picks_voice.id:
                    pulled_names.append(member.display_name)
                    pulled += 1
            except Exception:
                continue

        try:
            embed_log = discord.Embed(
                title="🧹 Não aceitou, rodou!",
                color=0xe67e22,
                description=f"{SERVERS[s_id]['name']}: buscando jogadores de próximo."
            )
            if removed_names:
                embed_log.add_field(
                    name="Removidos (não aceitaram)",
                    value="\n".join([f"❌ {n}" for n in removed_names]),
                    inline=False,
                )
            if pulled_names:
                embed_log.add_field(
                    name="Puxados da fila",
                    value="\n".join([f"✅ {n}" for n in pulled_names]),
                    inline=False,
                )
            await channel.send(embed=embed_log)
        except:
            pass

        try: await session["accept_message"].delete()
        except: pass
        await channel.send(f"⛔ **MIX CANCELADO - {SERVERS[s_id]['name']}**")
        reset_session(s_id)
        sessions[s_id]["suspend_auto_restore"] = True
        await refresh_server_category_visibility(bot, s_id, reason="accept_timeout_cancel")

async def start_faceit_draft(channel, s_id):
    session = sessions[s_id]
    await _ensure_faceit_info(session, session["players"])

    def score(p):
        faceit = session.get("faceit_info", {}).get(p.id) or {}
        lvl = faceit.get("level") or 0
        elo = faceit.get("elo") or 0
        rating = session.get("player_ratings", {}).get(p.id) or 0
        return (lvl, elo, rating)

    ordered = sorted(session["players"], key=score, reverse=True)
    if len(ordered) < 2:
        ordered = random.sample(session["players"], 2)
    cap2, cap1 = ordered[0], ordered[1]

    session["captain_vote_active"] = False
    session["captains"] = [cap1, cap2]
    session["pick_reason"] = f"**{cap2.display_name}** tem mais Faceit elo; {cap1.display_name} começa!"
    session.update({
        "status": "DRAFT",
        "team1": [cap1],
        "team2": [cap2],
        "available": [p for p in session["players"] if p not in (cap1, cap2)],
        "turn": cap1,
    })

    s_conf = SERVERS[s_id]
    asyncio.create_task(
        safe_move_member(
            cap1,
            channel.guild.get_channel(s_conf["channels"]["team1_voice"]),
            context=f"{s_id}:captain1",
        )
    )
    asyncio.create_task(
        safe_move_member(
            cap2,
            channel.guild.get_channel(s_conf["channels"]["team2_voice"]),
            context=f"{s_id}:captain2",
        )
    )

    session["message"] = await channel.send(embed=create_pick_embed(s_id), view=PickView(s_id))

async def start_captain_vote(channel, s_id):
    session = sessions[s_id]
    session.update({"status": "DRAFT", "captain_vote_active": True})
    msg = await channel.send(embed=discord.Embed(title=f"🗳️ VOTAÇÃO DE CAPITÃES", color=0x9b59b6), view=CaptainVoteView(session["players"], s_id))
    session["message"] = msg
    await update_vote_message(s_id)
    await asyncio.sleep(CAPTAIN_VOTE_TIMEOUT)
    if session["captain_vote_active"]: await finish_captain_vote(channel, s_id)

async def finish_captain_vote(channel, s_id):
    session = sessions[s_id]
    session["captain_vote_active"] = False
    votes = sorted(session["captain_votes"].items(), key=lambda x: x[1], reverse=True)
    ids = [pid for pid, _ in votes[:2]]
    if len(ids) < 2: ids = random.sample([p.id for p in session["players"]], 2)
    c1, c2 = channel.guild.get_member(ids[0]), channel.guild.get_member(ids[1])
    r1, r2 = await get_player_rank(c1.id), await get_player_rank(c2.id)
    if (r1['rating'] if r1 else 1000) <= (r2['rating'] if r2 else 1000): session["captains"] = [c1, c2]
    else: session["captains"] = [c2, c1]
    session["pick_reason"] = f"🔻 **{session['captains'][0].display_name}** tem menos pontos e começa!"
    s_conf = SERVERS[s_id]
    asyncio.create_task(
        safe_move_member(
            session["captains"][0],
            channel.guild.get_channel(s_conf["channels"]["team1_voice"]),
            context=f"{s_id}:vote-captain1",
        )
    )
    asyncio.create_task(
        safe_move_member(
            session["captains"][1],
            channel.guild.get_channel(s_conf["channels"]["team2_voice"]),
            context=f"{s_id}:vote-captain2",
        )
    )
    session.update({"team1": [session["captains"][0]], "team2": [session["captains"][1]], "available": [p for p in session["players"] if p not in session["captains"]], "turn": session["captains"][0]})
    await session["message"].edit(embed=create_pick_embed(s_id), view=PickView(s_id))

async def finish_map_veto(interaction, s_id):
    session, server = sessions[s_id], SERVERS[s_id]
    if not session.get("promo_sent"):
        apoio_ch = f"<#{CANAL_APOIO_ID}>" if CANAL_APOIO_ID else "#apoie-o-servidor"
        apoio = discord.Embed(
            title="\U0001F680 FORTALEÇA A COMUNIDADE!",
            description=(
                "Manter os servidores online tem custos.\n"
                "**Gosta de jogar aqui? Nos ajude a continuar!**\n\n"
                f"\U0001F4B0 **Apoio Financeiro:** {apoio_ch}\n"
                "\U0001F193 **Apoio Gratuito:** Use a tag :bee: **MIX** !"
            ),
            color=0x00FF00,
        )
        apoio.set_thumbnail(
            url="https://cdn.discordapp.com/attachments/1452985230565834804/1466928923702071339/LogoMixLeve.png?ex=697e8785&is=697d3605&hm=f87984bc7edff3658818b9c984ee3f2c4036c37c621445f5a07047ced0e34a75&"
        )
        apoio.set_footer(text="Agradecemos demais sua força! Bom jogo!")

        try:
            if session.get("message"):
                await session["message"].edit(content="", embed=apoio, view=None)
            else:
                session["message"] = await interaction.channel.send(embed=apoio, view=None)
            session["promo_sent"] = True
            await asyncio.sleep(APOIO_PUBLI_SECONDS)
        except Exception:
            pass

    if not session.get("torneio_promo_sent"):
        try:
            if TORNEIO_BANNER_URL:
                if session.get("message"):
                    await session["message"].edit(content=TORNEIO_BANNER_URL, embed=None, view=TorneioInscricaoView())
                else:
                    session["message"] = await interaction.channel.send(content=TORNEIO_BANNER_URL, view=TorneioInscricaoView())
            else:
                torneio = discord.Embed(
                    title="🏆 TORNEIO 5x5 — INSCRIÇÕES ABERTAS!",
                    color=0xFFD700,
                )
                torneio.set_footer(text="Não perca! Vagas limitadas a 40 jogadores.")
                if session.get("message"):
                    await session["message"].edit(content="", embed=torneio, view=TorneioInscricaoView())
                else:
                    session["message"] = await interaction.channel.send(embed=torneio, view=TorneioInscricaoView())
            session["torneio_promo_sent"] = True
            await asyncio.sleep(TORNEIO_PUBLI_SECONDS)
        except Exception:
            pass

    f_map = session["maps"][0]
    map_t = MAP_NAME_CONVERT.get(f_map, f_map)
    map_name_for_demo = map_t.split("/")[-1]
    if not map_name_for_demo.startswith("de_") and not map_t.startswith("workshop/"):
        map_name_for_demo = f"de_{map_name_for_demo}"

    m_id = session.get("match_id") or await reserve_match_id(s_id)
    pwd = str(random.randint(1000, 9999))
    logger.info(f"CONFIG: {s_id} match_id={m_id} mapa={map_t}")
    session.update(
        {
            "match_map": f_map,
            "match_id": m_id,
            "match_password": pwd,
            "status": "LIVE",
            "live_started_at": discord.utils.utcnow(),
            "notconnect_recovered": False,
            "recovering_notconnect": False,
            "runtime_server_id": session.get("runtime_server_id"),
            "runtime_tmux_session": session.get("runtime_tmux_session"),
            "runtime_host": session.get("runtime_host"),
            "runtime_port": session.get("runtime_port"),
            "runtime_gotv_port": session.get("runtime_gotv_port"),
        }
    )
    try:
        await set_active_match(s_id, m_id)
        logger.info(f"CONFIG: active_matches atualizado {s_id} -> {m_id}")
    except Exception as e:
        logger.error(f"CONFIG: falha ao gravar active_matches ({s_id}): {e}")

    t1, t2, skipped_players = await process_teams_parallel(session["team1"], session["team2"])
    expected_t1 = len(session["team1"])
    expected_t2 = len(session["team2"])
    if len(t1) != expected_t1 or len(t2) != expected_t2:
        skipped_txt = ", ".join(skipped_players[:6]) if skipped_players else "jogadores com cadastro invalido"
        if len(skipped_players) > 6:
            skipped_txt += ", ..."
        await interaction.channel.send(
            "⛔ Nao foi possivel iniciar a partida porque alguns cadastros estao invalidos para o MatchZy.\n"
            f"Jogadores afetados: {skipped_txt}\n"
            "Peça para os jogadores executarem `/cadastro` novamente."
        )
        reset_session(s_id)
        sessions[s_id]["suspend_auto_restore"] = True
        try:
            await clear_active_match(s_id)
            await clear_active_session(s_id)
            await clear_reserved_match_id(int(m_id))
        except Exception:
            pass
        await refresh_server_category_visibility(interaction.client, s_id, reason="map_veto_invalid_registration")
        return
    conf = {
        "matchid": int(m_id),
        "num_maps": 1,
        "maplist": [map_t],
        "skip_veto": True,
        "players_per_team": 5,
        "spectators": {"players": {}},
        "team1": {
            "name": f"T1_{clean_name(session['captains'][0].display_name)}",
            "tag": "T1",
            "players": t1,
        },
        "team2": {
            "name": f"T2_{clean_name(session['captains'][1].display_name)}",
            "tag": "T2",
            "players": t2,
        },
        "cvars": {
            "sv_password": pwd,
            "matchzy_demo_name_format": f"match_{m_id}_{map_name_for_demo}",
            "matchzy_autostart_mode": "1",
            "matchzy_minimum_ready_required": "5",
            "matchzy_knife_enabled_default": "1",
            "mp_death_drop_gun": "1",
            "mp_death_drop_grenade": "2",
            "mp_give_player_c4": "1",
        },
    }
    if ALWAYS_ALLOW_STEAMID64 and ALWAYS_ALLOW_STEAMID64 not in t1 and ALWAYS_ALLOW_STEAMID64 not in t2:
        conf["spectators"]["players"][ALWAYS_ALLOW_STEAMID64] = "Admin"

    if DEMO_UPLOAD_URL:
        demo_upload_url = DEMO_UPLOAD_URL
        if "{MATCH_ID}" in demo_upload_url or "{MAP}" in demo_upload_url:
            demo_upload_url = demo_upload_url.replace("{MATCH_ID}", str(m_id)).replace(
                "{MAP}", map_name_for_demo.replace("de_", "")
            )
        else:
            sep = "&" if "?" in demo_upload_url else "?"
            if "matchid=" not in demo_upload_url:
                demo_upload_url += f"{sep}matchid={m_id}"
                sep = "&"
            if "map=" not in demo_upload_url:
                demo_upload_url += f"{sep}map={map_name_for_demo.replace('de_', '')}"
        conf["cvars"]["matchzy_demo_upload_url"] = demo_upload_url

    try:
        if session.get("runtime_server_id"):
            runtime = await get_server_pool().load_match_on_allocated_runtime(
                match_id=int(m_id),
                payload=conf,
            )
        else:
            preferred_runtime_id = str(server.get("runtime_id") or "").strip()
            runtime = await get_server_pool().prepare_and_start_match(
                match_id=int(m_id),
                payload=conf,
                source="mix",
                lobby_server_id=s_id,
                preferred_runtime_id=(preferred_runtime_id or None),
                strict_preferred_runtime=True,
            )
    except NoServerAvailableError:
        await interaction.channel.send("? Nenhum servidor livre no pool (1-5). Aguarde um mix finalizar.")
        reset_session(s_id)
        sessions[s_id]["suspend_auto_restore"] = True
        try:
            await clear_active_match(s_id)
            await clear_active_session(s_id)
            await clear_reserved_match_id(int(m_id))
        except Exception:
            pass
        await refresh_server_category_visibility(interaction.client, s_id, reason="map_veto_no_pool")
        return
    except Exception as e:
        logger.error(f"START/LOAD local falhou ({s_id}/{m_id}): {e}")
        await interaction.channel.send(f"\u26D4 Falha ao iniciar servidor local: `{_compact_rcon_error(e)}`")
        reset_session(s_id)
        sessions[s_id]["suspend_auto_restore"] = True
        try:
            await clear_active_match(s_id)
            await clear_active_session(s_id)
            await clear_reserved_match_id(int(m_id))
        except Exception:
            pass
        await refresh_server_category_visibility(interaction.client, s_id, reason="map_veto_start_error")
        return

    session.update(
        {
            "runtime_server_id": runtime.get("runtime_id"),
            "runtime_tmux_session": runtime.get("tmux_session"),
            "runtime_host": runtime.get("host"),
            "runtime_port": runtime.get("port"),
            "runtime_gotv_port": runtime.get("gotv_port"),
        }
    )

    runtime_id = str(runtime.get("runtime_id") or session.get("runtime_server_id") or "").strip()
    runtime_cfg = get_server_pool().get_runtime_connection(runtime_id)
    host = str(runtime.get("host") or runtime_cfg.get("host") or "").strip()
    port = int(runtime.get("port") or runtime_cfg.get("port") or 0)
    gotv_port = int(runtime.get("gotv_port") or runtime_cfg.get("gotv_port") or 0)

    if not host or port <= 0:
        logger.warning(
            f"EMBED_CONN: runtime sem host/porta validos s_id={s_id} runtime={runtime_id} "
            f"runtime_host={runtime.get('host')} runtime_port={runtime.get('port')} "
            f"cfg_host={runtime_cfg.get('host')} cfg_port={runtime_cfg.get('port')}"
        )

    # Persistimos o valor efetivo usado no embed para manter consistencia em fluxos seguintes.
    session["runtime_host"] = host or None
    session["runtime_port"] = port or None
    session["runtime_gotv_port"] = gotv_port or None
    conn = f"connect {host}:{port}; password {pwd}" if host and port > 0 else "Host/porta do runtime nao configurados."
    gotv = f"connect {host}:{gotv_port}" if host and gotv_port > 0 else "GOTV nao configurado."
    embed_final = discord.Embed(title=f"\u2705 PARTIDA PRONTA! #{m_id}", color=0x2ECC71)
    embed_final.description = f"\U0001F5FA\uFE0F Mapa: **{f_map}**"
    map_key = map_t.split("/")[-1].replace("de_", "")
    if map_key.endswith("_d"):
        map_key = map_key[:-2]
    if MAP_IMAGES.get(map_key):
        embed_final.set_thumbnail(url=MAP_IMAGES[map_key])
    embed_final.add_field(
        name="\U0001F7E6 Time 1",
        value="\n".join([f"\u2022 {p.display_name}" for p in session["team1"]]),
        inline=True,
    )
    embed_final.add_field(
        name="\U0001F7E5 Time 2",
        value="\n".join([f"\u2022 {p.display_name}" for p in session["team2"]]),
        inline=True,
    )
    embed_final.add_field(name="\U0001F579\uFE0F JOGAR:", value=f"```{conn}```", inline=False)
    embed_final.add_field(name="\U0001F5A5\uFE0F ASSISTIR:", value=f"```{gotv}```", inline=False)
    await session["message"].edit(embed=embed_final)

async def setup(bot): await bot.add_cog(MixCog(bot))
