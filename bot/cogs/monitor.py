import os
import re
from datetime import datetime

import discord

from bot.config import SERVERS, CANAL_MONITOR_ID, RETAKE_FLAG
from bot.database import clear_active_match
from bot.utils.cs2 import send_rcon
from bot.utils.maps import format_monitor_map_name

RETAKE_HOST = os.environ.get("RETAKE_HOST", "")
RETAKE_PORT = int(os.environ.get("RETAKE_PORT", "27015"))
RETAKE_RCON_PASSWORD = os.environ.get("RETAKE_RCON_PASSWORD", "")

_RETAKE_RCON_CFG = {
    "cs2": {
        "host": RETAKE_HOST,
        "port": RETAKE_PORT,
        "rcon_password": RETAKE_RCON_PASSWORD,
    }
}

_PLAYERS_RE = re.compile(r"players\s*:\s*(\d+)\s+humans?.*?\((\d+)\s+max\)", re.IGNORECASE)


async def get_retake_online():
    res = await send_rcon(_RETAKE_RCON_CFG, "status", log_errors=False)
    if not res:
        return None, None
    match = _PLAYERS_RE.search(str(res))
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


async def find_monitor_message(channel, title_keyword, bot_user):
    if not channel or not bot_user:
        return None

    found_msg = None
    to_delete = []

    try:
        async for msg in channel.history(limit=10):
            if msg.author == bot_user and msg.embeds:
                if title_keyword in (msg.embeds[0].title or ""):
                    if not found_msg:
                        found_msg = msg
                    else:
                        to_delete.append(msg)
    except Exception:
        pass

    for msg in to_delete:
        try:
            await msg.delete()
        except Exception:
            pass

    return found_msg


def _build_server_block_clean(server, session, db_match, get_progress_bar, bot):
    name = server["name"].upper()

    is_live_db = db_match and (db_match.get("win1") != 1 and db_match.get("win2") != 1)

    if is_live_db:
        s1 = db_match.get("team1_score") or 0
        s2 = db_match.get("team2_score") or 0
        n1 = db_match.get("team1_name") or "Time 1"
        n2 = db_match.get("team2_name") or "Time 2"
        map_raw = str(db_match.get("mapname", "unknown"))
        map_clean = format_monitor_map_name(map_raw)
        gotv_port = server["cs2"].get("gotv_port", server["cs2"]["port"])

        # Fase / estado da partida (atualizado via webhooks)
        phase = session.get("match_phase")
        overtime_num = int(session.get("match_overtime_num") or 0)
        pause_team = session.get("match_pause_team")
        round_num = int(session.get("match_round_num") or 0)
        mvp = session.get("match_round_mvp")

        if phase == "knife":
            status_icon = "🔪 **FACA**"
        elif phase == "halftime":
            status_icon = "☕ **INTERVALO**"
        elif phase == "overtime":
            ot_label = f" #{overtime_num}" if overtime_num > 1 else ""
            status_icon = f"⚡ **OVERTIME{ot_label}**"
        elif phase == "paused":
            pause_label = f" — {pause_team}" if pause_team else ""
            status_icon = f"⏸️ **PAUSADO{pause_label}**"
        else:
            status_icon = "🟡 **AO VIVO**"

        map_line = f"> 🗺️ **{map_clean}**"
        if round_num > 0 and phase not in ("knife", "halftime"):
            map_line += f"  ·  Round {round_num}"

        mvp_line = ""
        if mvp and mvp.get("name"):
            mvp_line = f"\n> ⭐ MVP: **{mvp['name']}** ({mvp.get('kills', 0)}K · {mvp.get('damage', 0)}dmg)"

        details = (
            f"{map_line}\n"
            f"> 🏆 {n1} `{s1}` vs `{s2}` {n2}"
            f"{mvp_line}\n"
            f"📺 **ASSISTIR:**\n"
            f"```connect {server['cs2']['host']}:{gotv_port}```"
        )
        return name, status_icon, details, map_raw

    if session.get("active") and session.get("status") in ["ACCEPT", "BOOTING", "DRAFT", "VETO", "LIVE"] and not is_live_db:
        if session.get("status") == "LIVE":
            host = str(session.get("runtime_host") or server["cs2"].get("host") or "").strip()
            gotv_port = int(session.get("runtime_gotv_port") or server["cs2"].get("gotv_port") or 0)
            map_name = format_monitor_map_name(session.get("match_map") or "desconhecido")
            if host and gotv_port > 0:
                gotv_cmd = f"connect {host}:{gotv_port}" if gotv_port > 0 else "GOTV nao configurado."
                status_icon = "🟡 **AGUARDANDO PLAYERS**"
                details = (
                    f"> 🗺️ **{map_name}**\n"
                    f"> 📺 **ASSISTIR:**\n"
                    f"```{gotv_cmd}```"
                )
                return name, status_icon, details, None
            status_msg = "Jogadores se conectando..."
        elif session.get("status") == "BOOTING":
            status_msg = "Ligando servidor..."
        else:
            status_msg = "Votacao de Capitaes" if session.get("status") == "DRAFT" else "Vetando Mapas"
            if session.get("status") == "ACCEPT":
                status_msg = "Aguardando Jogadores..."

        status_icon = "🟣 **PREPARANDO**"
        details = (
            f"> 🔨 **Fase:** {status_msg}\n"
            f"> ⚠️ *Aguarde o inicio...*"
        )
        return name, status_icon, details, None

    try:
        voice_id = server["channels"]["picks_voice"]
        vc = bot.get_channel(voice_id)
        count = len(vc.members) if vc else 0
    except Exception:
        count = 0

    bar = get_progress_bar(count)
    if count >= 10:
        status_icon = "🔴 **SALA CHEIA**"
        action_text = "Aguardando inicio..."
    else:
        status_icon = "🟢 **DISPONIVEL**"
        action_text = "Entre na sala de voz!"

    details = (
        f"> 👥 **Jogadores:** `{count}/10`\n"
        f"> {bar}\n"
        f"🔹 *{action_text}*"
    )
    return name, status_icon, details, None


async def update_monitor_combined(bot, sessions, global_state, reset_session, get_match_overview, get_online_count, get_progress_bar, get_active_matches=None):
    channel = bot.get_channel(CANAL_MONITOR_ID)
    if not channel:
        return

    msg = global_state["monitor_msgs"].get("combined")
    if not msg:
        msg = await find_monitor_message(channel, "STATUS DOS SERVIDORES", bot.user)
        if msg:
            global_state["monitor_msgs"]["combined"] = msg

    embed = discord.Embed(
        title="🖥️ STATUS DOS SERVIDORES",
        color=0x2B2D31,
        timestamp=datetime.now(),
    )

    active_rows_by_server = {}
    if get_active_matches:
        try:
            rows = await get_active_matches()
            active_rows_by_server = {str(r.get("server_id") or ""): r for r in rows if r.get("server_id")}
        except Exception:
            active_rows_by_server = {}

    visible_count = 0
    for s_id, server in SERVERS.items():
        if not server.get("active"):
            continue

        session = sessions.get(s_id, {})
        row = active_rows_by_server.get(s_id)
        if row and (not session.get("active") or not session.get("match_id")):
            try:
                session.update({"match_id": int(row["match_id"]), "active": True, "status": "LIVE"})
            except Exception:
                pass

        db_match = None
        if session.get("active") and session.get("match_id"):
            db_match = await get_match_overview(session["match_id"])

        if db_match:
            is_live = (db_match.get("win1") != 1 and db_match.get("win2") != 1)
            if not is_live and session.get("status") == "LIVE":
                reset_session(s_id)
                try:
                    await clear_active_match(s_id)
                except Exception:
                    pass
                db_match = None
        elif session.get("active") and session.get("status") == "LIVE" and not row:
            # Sessao LIVE sem registro ativo no DB: limpa estado zumbi para nao poluir a embed.
            reset_session(s_id)
            try:
                await clear_active_match(s_id)
            except Exception:
                pass
            continue

        has_runtime = bool(session.get("runtime_server_id"))
        if not db_match and not (session.get("active") and has_runtime):
            continue

        srv_name, icon, text, _ = _build_server_block_clean(server, session, db_match, get_progress_bar, bot)
        flag = server.get("flag", "🏳️")
        srv_name_with_flag = f"{srv_name} {flag}"

        if visible_count > 0:
            embed.add_field(name="\u200b", value="\u200b", inline=False)

        embed.add_field(name=f"{icon}  |  {srv_name_with_flag}", value=text, inline=False)
        visible_count += 1

    retake_online, retake_max = await get_retake_online()
    if retake_online is None or retake_max is None:
        retake_status = "🔴 OFFLINE"
        retake_players = "👥 **Jogadores**: `0`"
    else:
        retake_status = "🟢 ONLINE"
        retake_players = f"👥 **Jogadores**: `{retake_online}`"

    if visible_count > 0:
        embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(
        name=f"🔫 **RETAKE EXCLUSIVO** {RETAKE_FLAG}  |  {retake_status}",
        value=f"{retake_players}\n```connect {RETAKE_HOST}:{RETAKE_PORT}; password 1234```",
        inline=False,
    )

    embed.set_footer(text="MixBot System", icon_url=bot.user.display_avatar.url)

    try:
        if msg:
            await msg.edit(embed=embed)
        else:
            global_state["monitor_msgs"]["combined"] = await channel.send(embed=embed)
    except discord.NotFound:
        global_state["monitor_msgs"]["combined"] = await channel.send(embed=embed)
    except Exception as e:
        print(f"Erro Monitor: {e}")
