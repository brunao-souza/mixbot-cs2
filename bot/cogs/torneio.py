import asyncio
import re
import time
import random
import secrets
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View
from loguru import logger

from bot.config import (
    MAPS_BASE,
    MAP_NAME_CONVERT,
    SALA_SAIDA_ID,
    STAFF_ROLE_IDS,
    TORNEIO_CATEGORY_ID,
    TORNEIO_PICKS_BANS_CHANNEL_ID,
    TOURN_GROUPS_CHANNEL_ID,
    TOURN_SCHEDULE_CHANNEL_ID,
)
from bot.database import (
    get_match_overview,
    get_player_rank,
    remove_tournament_team_player,
    get_tournament_team_captain,
    get_tournament_team_players,
    get_tournament_match_by_id,
    set_tournament_match_result,
    list_tournament_team_names,
    list_tournament_teams_by_group,
    get_tournament_team_group,
    set_tournament_team_group,
    get_finished_tournament_match_rows,
    upsert_tournament_match,
    upsert_tournament_team_player,
)
from bot.utils.server_pool import get_server_pool, NoServerAvailableError


STEAMID_RE = re.compile(r"^\d{17}$")
READY_TIMEOUT_SECONDS = 600

# Pools per mode (can be adjusted easily).
TOURNEY_POOL_5V5 = [
    "Ancient",
    "Anubis",
    "Dust2",
    "Inferno",
    "Mirage",
    "Nuke",
    "Overpass",
]

TOURNEY_POOL_2V2 = [
    "Inferno",
    "Nuke",
    "Overpass",
    "Vertigo",
]

TOURNEY_POOL_1V1_WORKSHOP = [
    "workshop/3070244460/am_redline",
    "workshop/3070192312/am_must2",
    "workshop/3070221308/am_basement",
    "workshop/3070234901/am_multimap",
    "workshop/3070250060/am_crashz_dust_v2",
]

GROUP_A_SCHEDULE = [
    {"block": "01", "games": [("OLingles", "Synapha"), ("Nerdullets", "Luumer")]},
    {"block": "02", "games": [("Caiell", "Tronizera"), ("Pepecão", "OLingles")]},
    {"block": "03", "games": [("Synapha", "Luumer"), ("Nerdullets", "Tronizera")]},
    {"block": "04", "games": [("Caiell", "Pepecão"), ("OLingles", "Luumer")]},
    {"block": "05", "games": [("Synapha", "Tronizera"), ("Nerdullets", "Caiell")]},
    {"block": "06", "games": [("Pepecão", "Luumer"), ("OLingles", "Tronizera")]},
    {"block": "07", "games": [("Synapha", "Nerdullets"), ("Caiell", "Luumer")]},
    {"block": "08", "games": [("Pepecão", "Tronizera"), ("OLingles", "Caiell")]},
    {"block": "09", "games": [("Synapha", "Pepecão"), ("Nerdullets", "Luumer")]},
    {"block": "10", "games": [("Tronizera", "Luumer"), ("OLingles", "Nerdullets")]},
    {"block": "11", "games": [("Synapha", "Caiell")]},
]

GROUP_B_SCHEDULE = [
    {"block": "01", "games": [("Embassavão", "Raiko"), ("Slatah", "Rioraes")]},
    {"block": "02", "games": [("GRuNao", "VSex"), ("Raiko", "VSex")]},
    {"block": "03", "games": [("Rioraes", "GRuNao"), ("Embassavão", "Slatah")]},
    {"block": "04", "games": [("Slatah", "Raiko"), ("GRuNao", "Embassavão")]},
    {"block": "05", "games": [("VSex", "Rioraes"), ("Raiko", "Rioraes")]},
    {"block": "06", "games": [("Embassavão", "VSex"), ("Slatah", "GRuNao")]},
    {"block": "07", "games": [("GRuNao", "Raiko"), ("VSex", "Slatah")]},
    {"block": "08", "games": [("Rioraes", "Embassavão")]},
]

MIX_LOGO_URL = (
    "https://cdn.discordapp.com/attachments/1452985230565834804/1474128296668303464/ChatGPT_Image_17_de_fev._de_2026_02_47_26.png"
)


class TournamentReadyOpenButton(Button):
    def __init__(self, cog: "TournamentCog", session_id: str):
        super().__init__(label="READY", style=discord.ButtonStyle.success, emoji="✅")
        self.cog = cog
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_ready_click_open(interaction, self.session_id)


class TournamentReadyOpenView(View):
    def __init__(self, cog: "TournamentCog", session_id: str):
        super().__init__(timeout=READY_TIMEOUT_SECONDS)
        self.add_item(TournamentReadyOpenButton(cog, session_id))


class TournamentBanButton(Button):
    def __init__(self, cog: "TournamentCog", session_id: str, map_name: str):
        super().__init__(label=map_name, style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.session_id = session_id
        self.map_name = map_name

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_map_ban(interaction, self.session_id, self.map_name)


class TournamentBanView(View):
    def __init__(self, cog: "TournamentCog", session_id: str, maps_left: List[str]):
        super().__init__(timeout=None)
        for map_name in maps_left:
            self.add_item(TournamentBanButton(cog, session_id, map_name))


class TournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: Dict[str, Dict] = {}
        self._reserved_servers: set[str] = set()
        self._groups_board_message_id: Optional[int] = None
        self._schedule_message_id: Optional[int] = None

    async def cog_load(self):
        self.tournament_server_watchdog.start()

    async def cog_unload(self):
        self.tournament_server_watchdog.cancel()

    def _is_overtime(self, mode: str, max_loser_rounds: int) -> bool:
        # 2x2: overtime at 8x8 (MR8). 5x5: overtime at 12x12 (MR12).
        threshold = 8 if (mode or "").strip().lower() == "2x2" else 12
        return int(max_loser_rounds or 0) >= threshold

    async def _build_group_rows(self, group_name: str) -> List[Dict]:
        group = (group_name or "").strip().upper()
        if group not in ("A", "B"):
            return []

        team_names = await list_tournament_teams_by_group(group)
        stats: Dict[str, Dict] = {
            team: {
                "team": team,
                "PJ": 0,
                "V": 0,
                "GP": 0,
                "PP": 0,
                "D": 0,
                "DIFF": 0,
                "PTS": 0,
            }
            for team in team_names
        }

        match_rows = await get_finished_tournament_match_rows()
        for row in match_rows:
            team1 = str(row.get("team1") or "").strip()
            team2 = str(row.get("team2") or "").strip()
            if not team1 or not team2:
                continue

            g1 = await get_tournament_team_group(team1)
            g2 = await get_tournament_team_group(team2)
            if g1 != group or g2 != group:
                continue

            if team1 not in stats:
                stats[team1] = {"team": team1, "PJ": 0, "V": 0, "GP": 0, "PP": 0, "D": 0, "DIFF": 0, "PTS": 0}
            if team2 not in stats:
                stats[team2] = {"team": team2, "PJ": 0, "V": 0, "GP": 0, "PP": 0, "D": 0, "DIFF": 0, "PTS": 0}

            score1 = int(row.get("score1") or 0)  # series score (maps)
            score2 = int(row.get("score2") or 0)  # series score (maps)
            round1 = int(row.get("round_score1") or 0)  # accumulated round score
            round2 = int(row.get("round_score2") or 0)  # accumulated round score
            mode = str(row.get("mode") or "")
            series = str(row.get("series") or "md1")
            needed_wins = (self._series_num_maps(series) // 2) + 1
            if max(score1, score2) < needed_wins:
                continue
            winner = str(row.get("winner") or "").strip()
            if winner not in (team1, team2):
                winner = team1 if score1 > score2 else team2
            if winner not in (team1, team2):
                continue

            stats[team1]["PJ"] += 1
            stats[team2]["PJ"] += 1
            stats[team1]["DIFF"] += round1 - round2
            stats[team2]["DIFF"] += round2 - round1

            ot = self._is_overtime(mode, int(row.get("max_loser_rounds") or 0))
            loser = team2 if winner == team1 else team1
            if ot:
                stats[winner]["GP"] += 1
                stats[winner]["PTS"] += 2
                stats[loser]["PP"] += 1
                stats[loser]["PTS"] += 1
            else:
                stats[winner]["V"] += 1
                stats[winner]["PTS"] += 3
                stats[loser]["D"] += 1

        rows = list(stats.values())
        rows.sort(key=lambda r: (-r["PTS"], -r["DIFF"], -r["GP"], -r["V"], r["team"].lower()))
        return rows

    def _render_group_table(self, title: str, rows: List[Dict]) -> str:
        if not rows:
            return f"{title}\n```text\nNo teams in the group.\n```"
        header = "POS TEAM            GP W OTW OTL L  +/- PTS\n"
        body_lines = []
        for i, r in enumerate(rows, start=1):
            team = str(r["team"])[:15]
            diff = int(r["DIFF"])
            diff_txt = f"{diff:+d}"
            body_lines.append(
                f"{i:>2}  {team:<15} {int(r['PJ']):>2} {int(r['V']):>1} {int(r['GP']):>2} {int(r['PP']):>2} {int(r['D']):>1} {diff_txt:>4} {int(r['PTS']):>3}"
            )
        body = "\n".join(body_lines)
        return f"{title}\n```text\n{header}{body}\n```"

    async def _build_groups_embed(self) -> discord.Embed:
        rows_a = await self._build_group_rows("A")
        rows_b = await self._build_group_rows("B")
        embed = discord.Embed(
            title="🏆 Tournament Group Standings",
            description="📊 Automatic group classification update.",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=MIX_LOGO_URL)
        embed.add_field(name="🅰️ Group A", value=self._render_group_table("Group A", rows_a), inline=False)
        embed.add_field(name="🅱️ Group B", value=self._render_group_table("Group B", rows_b), inline=False)
        embed.set_footer(text="✅ W=3 | 🟨 OTW=2 | 🟧 OTL=1 | ❌ L=0 | ⚖️ +/- round differential")
        return embed

    async def _upsert_groups_embed(self, preferred_channel: Optional[discord.TextChannel] = None):
        # Updates only in the channel defined by env.
        channel: Optional[discord.TextChannel] = None
        if TOURN_GROUPS_CHANNEL_ID:
            ch = self.bot.get_channel(TOURN_GROUPS_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                channel = ch
        if channel is None:
            return

        embed = await self._build_groups_embed()
        msg = None
        if self._groups_board_message_id:
            try:
                fetched = await channel.fetch_message(self._groups_board_message_id)
                if isinstance(fetched, discord.Message):
                    msg = fetched
            except Exception:
                msg = None

        if msg:
            await msg.edit(embed=embed)
        else:
            sent = await channel.send(embed=embed)
            self._groups_board_message_id = sent.id

    @staticmethod
    def _pair_key(team1: str, team2: str) -> tuple[str, str]:
        def _norm(value: str) -> str:
            raw = (value or "").strip().lower()
            no_accents = "".join(
                ch for ch in unicodedata.normalize("NFKD", raw)
                if not unicodedata.combining(ch)
            )
            return " ".join(no_accents.split())
        a = _norm(team1)
        b = _norm(team2)
        return tuple(sorted((a, b)))

    @staticmethod
    def _fmt_score(team1: str, score1: int, score2: int, team2: str) -> str:
        return f"{team1} {score1}x{score2} {team2}"

    def _collect_live_pairs(self) -> set[tuple[str, str]]:
        live_pairs: set[tuple[str, str]] = set()
        for sess in self.sessions.values():
            if sess.get("status") != "live":
                continue
            t1 = str(sess.get("team1_name") or "").strip()
            t2 = str(sess.get("team2_name") or "").strip()
            if t1 and t2:
                live_pairs.add(self._pair_key(t1, t2))
        return live_pairs

    async def _build_schedule_embed(self) -> discord.Embed:
        finished_rows = await get_finished_tournament_match_rows()
        live_pairs = self._collect_live_pairs()

        pair_results: Dict[tuple[str, str], List[Dict]] = {}

        def _row_finished(row: Dict) -> bool:
            s1 = int(row.get("score1") or 0)
            s2 = int(row.get("score2") or 0)
            series = str(row.get("series") or "md1")
            needed_wins = (self._series_num_maps(series) // 2) + 1
            return max(s1, s2) >= needed_wins

        for row in finished_rows:
            if not _row_finished(row):
                continue
            t1 = str(row.get("team1") or "").strip()
            t2 = str(row.get("team2") or "").strip()
            if not t1 or not t2:
                continue
            key = self._pair_key(t1, t2)
            pair_results.setdefault(key, []).append(row)

        def consume_result(team1: str, team2: str) -> Optional[Dict]:
            key = self._pair_key(team1, team2)
            lst = pair_results.get(key) or []
            if not lst:
                return None
            return lst.pop(0)

        def render_group_lines(group_rows: List[Dict]) -> str:
            lines: List[str] = []
            for row in group_rows:
                block = str(row.get("block") or "")
                games = row.get("games") or []
                game_texts = []
                for team1, team2 in games:
                    result = consume_result(team1, team2)
                    if result:
                        s1 = int(result.get("score1") or 0)
                        s2 = int(result.get("score2") or 0)
                        status = "✅ Finished"
                        score_txt = self._fmt_score(team1, s1, s2, team2)
                        game_texts.append(f"{status} • {score_txt}")
                    else:
                        key = self._pair_key(team1, team2)
                        if key in live_pairs:
                            game_texts.append(f"🟡 In progress • {team1} vs {team2}")
                        else:
                            game_texts.append(f"⏳ Scheduled • {team1} vs {team2}")

                if len(game_texts) == 1:
                    lines.append(f"**Block {block}**\n• {game_texts[0]}")
                else:
                    lines.append(f"**Block {block}**\n• {game_texts[0]}\n• {game_texts[1]}")
            return "\n\n".join(lines) if lines else "No matches."

        embed = discord.Embed(
            title="🗓️ Match Schedule - Group Stage",
            description="Start: **19:00 (London)** • Updates at the end of each match.",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=MIX_LOGO_URL)
        embed.add_field(name="🅰️ Group A", value=render_group_lines(GROUP_A_SCHEDULE), inline=False)
        embed.add_field(name="🅱️ Group B", value=render_group_lines(GROUP_B_SCHEDULE), inline=False)
        embed.set_footer(text="Status: ⏳ Scheduled | 🟡 In progress | ✅ Finished")
        return embed

    async def _upsert_schedule_embed(self, preferred_channel: Optional[discord.TextChannel] = None):
        # Updates only in the channel defined by env.
        channel: Optional[discord.TextChannel] = None
        if TOURN_SCHEDULE_CHANNEL_ID:
            ch = self.bot.get_channel(TOURN_SCHEDULE_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                channel = ch
        if channel is None:
            return

        embed = await self._build_schedule_embed()
        msg = None
        if self._schedule_message_id:
            try:
                fetched = await channel.fetch_message(self._schedule_message_id)
                if isinstance(fetched, discord.Message):
                    msg = fetched
            except Exception:
                msg = None

        if msg:
            await msg.edit(embed=embed)
        else:
            sent = await channel.send(embed=embed)
            self._schedule_message_id = sent.id

    def _sanitize_slug(self, value: str) -> str:
        base = (value or "").strip().lower()
        cleaned = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
        return cleaned or "team"

    def _is_server_reserved(self, server_id: str) -> bool:
        if server_id in self._reserved_servers:
            return True
        for sess in self.sessions.values():
            if sess.get("tserver_id") != server_id:
                continue
            if sess.get("status") in ("cancelled_timeout", "finished", "failed"):
                continue
            return True
        return False

    def _normalize_runtime_request(self, requested: str) -> str:
        raw = str(requested or "").strip().lower()
        if not raw:
            return ""
        if raw.startswith("tserver"):
            digits = raw.replace("tserver", "", 1)
            if digits.isdigit():
                return f"mix{int(digits)}"
        if raw.startswith("ts"):
            digits = raw.replace("ts", "", 1)
            if digits.isdigit():
                return f"mix{int(digits)}"
        if raw.isdigit():
            return f"mix{int(raw)}"
        return raw

    async def _pick_available_tournament_server(self) -> Optional[str]:
        free_ids = await get_server_pool().available_runtime_ids("tourney")
        for runtime_id in free_ids:
            if self._is_server_reserved(runtime_id):
                continue
            return runtime_id
        return None

    async def _pick_tournament_server(self, requested: str = "auto") -> tuple[Optional[str], Optional[str]]:
        req = str(requested or "auto").strip().lower()
        if req in ("auto", ""):
            picked = await self._pick_available_tournament_server()
            if not picked:
                return None, "No tournament server available right now."
            return picked, None

        runtime_id = self._normalize_runtime_request(req)
        if not runtime_id:
            return None, f"Invalid server: `{requested}`."
        if self._is_server_reserved(runtime_id):
            return None, f"Server `{runtime_id}` is already in use."

        free_ids = await get_server_pool().available_runtime_ids("tourney")
        if runtime_id not in free_ids:
            return None, f"Server `{runtime_id}` is busy or unavailable for tournament."
        return runtime_id, None

    def _compact_response(self, value: object, limit: int = 220) -> str:
        text = str(value or "").strip()
        if not text:
            return "no response"
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        line = line or text.replace("\n", " ").strip()
        if len(line) > limit:
            return line[:limit] + "..."
        return line

    async def _release_runtime_server(self, session: Dict, reason: str) -> None:
        matchid = session.get("matchid")
        if not matchid:
            return
        try:
            await get_server_pool().release_server_for_match(int(matchid), reason=reason)
        except Exception as e:
            logger.warning(
                f"TOURNAMENT[{session.get('session_id')}] failed to release runtime match={matchid}: {self._compact_response(e)}"
            )

    async def _execute_tournament_match(self, session: Dict, payload: Dict):
        preferred_runtime_id = str(session.get("tserver_id") or "").strip() or None
        runtime = await get_server_pool().prepare_and_start_match(
            match_id=int(session["matchid"]),
            payload=payload,
            source="tourney",
            preferred_runtime_id=preferred_runtime_id,
            lobby_server_id="tourney",
        )
        session["runtime_server_id"] = runtime.get("runtime_id")
        session["runtime_tmux_session"] = runtime.get("tmux_session")
        session["runtime_host"] = runtime.get("host")
        session["runtime_port"] = runtime.get("port")
        session["runtime_gotv_port"] = runtime.get("gotv_port")
        session["json_filename"] = f"match{session.get('matchid')}.json"
        session["json_local_path"] = runtime.get("json_path")
        session["tserver_name"] = runtime.get("runtime_id")
        logger.info(
            f"TOURNAMENT[{session.get('session_id')}] match={session.get('matchid')} runtime={runtime.get('runtime_id')} "
            f"json={runtime.get('json_path')}"
        )


    @tasks.loop(seconds=20)
    async def tournament_server_watchdog(self):
        for sid, sess in list(self.sessions.items()):
            if sess.get("status") != "live":
                continue
            try:
                overview = await get_match_overview(int(str(sess["matchid"])))
            except Exception:
                overview = None
            if not overview:
                continue
            score1 = int(overview.get("win1") or 0)
            score2 = int(overview.get("win2") or 0)
            needed_wins = (self._series_num_maps(str(sess.get("series") or "md1")) // 2) + 1
            ended = (score1 >= needed_wins or score2 >= needed_wins)
            logger.info(
                f"TOURNAMENT[{sid}] series={sess.get('series')} series_score={score1}-{score2} "
                f"needed={needed_wins} ended={ended}"
            )
            if not ended:
                continue
            sess["status"] = "finished"
            tserver_id = sess.get("tserver_id")
            if tserver_id and tserver_id in self._reserved_servers:
                self._reserved_servers.discard(tserver_id)
            await self._release_runtime_server(sess, reason="tourney_poll_end")
            try:
                await self._move_waiting_players_to_exit(sess)
                await self._cleanup_session_resources(sess)
            except Exception:
                pass
            text_ch = self.bot.get_channel(sess.get("text_channel_id", 0))
            if text_ch:
                try:
                    await text_ch.send(f"Tournament match #{sess['matchid']} finished. Server released.")
                except Exception:
                    pass
            self.sessions.pop(sid, None)

    @tournament_server_watchdog.before_loop
    async def before_tournament_watchdog(self):
        await self.bot.wait_until_ready()

    async def _team_name_autocomplete(self, interaction: discord.Interaction, current: str):
        names = await list_tournament_team_names()
        current_l = current.lower().strip()
        if not current_l:
            return [app_commands.Choice(name=n, value=n) for n in names[:25]]
        filtered = [n for n in names if current_l in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]

    async def _reply_ctx(self, ctx: commands.Context, *args, **kwargs):
        interaction = getattr(ctx, "interaction", None)
        if interaction:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer(thinking=False, ephemeral=True)
                except discord.HTTPException as e:
                    # Race condition: interaction may already be acknowledged by bridge/hybrid.
                    if getattr(e, "code", None) != 40060:
                        raise
            return await interaction.followup.send(*args, **kwargs)
        return await ctx.send(*args, **kwargs)

    async def _serie_autocomplete(self, interaction: discord.Interaction, current: str):
        modo_raw = getattr(interaction.namespace, "modo", None)
        if hasattr(modo_raw, "value"):
            modo_raw = modo_raw.value
        modo = str(modo_raw or "").lower().strip()
        allowed = self._allowed_series_for_mode(modo) or ["md1", "md3", "md5"]
        current_l = (current or "").lower().strip()
        if not current_l:
            return [app_commands.Choice(name=s, value=s) for s in allowed]
        filtered = [s for s in allowed if current_l in s]
        return [app_commands.Choice(name=s, value=s) for s in filtered[:25]]

    def _mode_size(self, mode: str) -> Optional[int]:
        mode = (mode or "").lower().strip()
        return {"1x1": 1, "2x2": 2, "5x5": 5}.get(mode)

    def _map_pool_for_mode(self, mode: str) -> List[str]:
        m = (mode or "").lower().strip()
        if m == "1x1":
            pool = list(TOURNEY_POOL_1V1_WORKSHOP)
            random.shuffle(pool)
            return pool[:3]
        if m == "2x2":
            return list(TOURNEY_POOL_2V2)
        if m == "5x5":
            return list(TOURNEY_POOL_5V5)
        return list(MAPS_BASE)

    def _series_num_maps(self, series: str) -> int:
        return {"md1": 1, "md3": 3, "md5": 5}.get((series or "").lower().strip(), 1)

    def _allowed_series_for_mode(self, mode: str) -> List[str]:
        m = (mode or "").lower().strip()
        if m == "1x1":
            return ["md1"]
        if m == "2x2":
            return ["md1", "md3"]
        if m == "5x5":
            return ["md1", "md3", "md5"]
        return []

    def _series_steps(self, series: str, mode: str, team1: str, team2: str, maps_len: int):
        s = (series or "").lower().strip()
        if s == "md1":
            steps = []
            turn = team1
            for _ in range(max(0, maps_len - 1)):
                steps.append(("ban", turn))
                turn = team2 if turn == team1 else team1
            return steps
        if s == "md3":
            if (mode or "").lower().strip() == "2x2":
                # Custom rule requested:
                # pick T1, pick T2, ban T2, decider
                return [
                    ("pick", team1),
                    ("pick", team2),
                    ("ban", team2),
                ]
            return [
                ("ban", team1),
                ("ban", team2),
                ("pick", team1),
                ("pick", team2),
                ("ban", team1),
                ("ban", team2),
            ]
        if s == "md5":
            return [
                ("ban", team1),
                ("ban", team2),
                ("pick", team1),
                ("pick", team2),
                ("pick", team1),
                ("pick", team2),
            ]
        return []

    async def _steamid_from_ranking(self, discord_id: int) -> Optional[str]:
        rank = await get_player_rank(discord_id)
        if not rank:
            return None
        steamid = str(rank.get("steamid64") or "").strip()
        if not steamid:
            return None
        if not STEAMID_RE.match(steamid):
            return None
        return steamid

    async def _cleanup_session_resources(self, session: Dict):
        guild = self.bot.get_guild(session["guild_id"])
        if not guild:
            return

        channel_ids = list(session.get("generated_channel_ids", []))
        for cid in channel_ids:
            ch = guild.get_channel(cid)
            if ch:
                try:
                    await ch.delete(reason="Tournament cancelled/finished")
                except Exception:
                    pass

        text_channel_id = session.get("text_channel_id")
        text_channel = guild.get_channel(text_channel_id) if text_channel_id else None
        if text_channel:
            for uid in session.get("text_overwrite_user_ids", []):
                member = guild.get_member(uid)
                if not member:
                    continue
                try:
                    await text_channel.set_permissions(member, overwrite=None, reason="Tournament permission cleanup")
                except Exception:
                    pass

    async def _move_waiting_players_to_exit(self, session: Dict):
        guild = self.bot.get_guild(session["guild_id"])
        if not guild:
            return
        exit_channel = guild.get_channel(SALA_SAIDA_ID) if SALA_SAIDA_ID else None
        voice_ids = [
            session.get("voice_team1_id"),
            session.get("voice_team2_id"),
        ]
        for vid in voice_ids:
            if not vid:
                continue
            vc = guild.get_channel(vid)
            if not vc or not isinstance(vc, discord.VoiceChannel):
                continue
            for member in list(vc.members):
                if member.bot:
                    continue
                try:
                    await member.move_to(exit_channel if exit_channel else None)
                except Exception:
                    pass

    def _build_veto_embed(self, session: Dict) -> discord.Embed:
        maps_left = session["maps_left"]
        steps = session.get("steps", [])
        step_index = int(session.get("step_index", 0))
        action = None
        turn = None
        if step_index < len(steps):
            action, turn = steps[step_index]
        banned = session.get("banned_maps", [])
        picked = session.get("picked_maps", [])
        embed = discord.Embed(
            title=f"🗺️ Map Veto - {session['team1_name']} vs {session['team2_name']}",
            color=0xE67E22,
        )
        embed.add_field(name="🎯 Maps remaining", value="\n".join([f"- {m}" for m in maps_left]), inline=False)
        if banned:
            embed.add_field(name="❌ Banned maps", value="\n".join([f"- {m}" for m in banned]), inline=False)
        if picked:
            embed.add_field(name="✅ Picked maps", value="\n".join([f"- {m}" for m in picked]), inline=False)
        if action and turn:
            action_label = "ban" if action == "ban" else "pick"
            cap_name = "Captain"
            cap_avatar = None
            cap_profile = (session.get("captain_profiles", {}) or {}).get(turn, {})
            if cap_profile:
                cap_name = cap_profile.get("name") or cap_name
                cap_avatar = cap_profile.get("avatar")
            else:
                cap_id = int(session.get("captain_ids", {}).get(turn, 0) or 0)
                guild = self.bot.get_guild(int(session.get("guild_id", 0) or 0))
                if guild and cap_id:
                    member = guild.get_member(cap_id)
                    if member:
                        cap_name = member.display_name
                        try:
                            cap_avatar = member.display_avatar.url
                        except Exception:
                            cap_avatar = None
            embed.set_footer(text=f"⏳ Turn to {action_label}: {turn} | {cap_name}")
            if cap_avatar:
                embed.set_author(name=f"{turn} • {cap_name}", icon_url=cap_avatar)
            else:
                embed.set_author(name=f"{turn} • {cap_name}")
        else:
            embed.set_footer(text="🏁 Veto finished")
        embed.set_thumbnail(url=MIX_LOGO_URL)
        return embed

    def _build_ready_panel_embed(self, session: Dict) -> discord.Embed:
        pending_ids = sorted(session.get("pending_ids", []))
        ready_ids = sorted(session.get("ready_ids", []))
        pending_text = "\n".join([f"- <@{uid}>" for uid in pending_ids]) if pending_ids else "No one pending."
        ready_text = "\n".join([f"- <@{uid}>" for uid in ready_ids]) if ready_ids else "-"

        embed = discord.Embed(
            title="✅ READY Confirmation",
            description=(
                f"Match: **{session['team1_name']} vs {session['team2_name']}**\n"
                "Confirm using the button below in this channel."
            ),
            color=0x2ECC71 if pending_ids else 0x3498DB,
        )
        embed.add_field(
            name="Status",
            value=f"Confirmed: **{len(ready_ids)}** | Pending: **{len(pending_ids)}**",
            inline=False,
        )
        embed.add_field(
            name="Match",
            value=(
                f"MatchID: **{session.get('matchid')}**\n"
                f"Mode: **{session.get('mode')}** | Series: **{str(session.get('series', '')).upper()}**\n"
                f"Server: **{session.get('tserver_name', '-')}**"
            ),
            inline=False,
        )
        embed.add_field(name="Pending", value=pending_text, inline=False)
        embed.add_field(name="Confirmed", value=ready_text, inline=False)
        embed.set_thumbnail(url=MIX_LOGO_URL)
        embed.set_footer(text="Max time: 10 minutes")
        return embed

    async def _edit_flow_message(self, session_id: str, embed: discord.Embed, view: Optional[View]):
        session = self.sessions.get(session_id)
        if not session:
            return
        channel = self.bot.get_channel(session.get("text_channel_id", 0))
        msg_id = session.get("ready_panel_message_id")
        if not channel or not msg_id:
            return
        try:
            panel_msg = await channel.fetch_message(msg_id)
        except Exception:
            return
        try:
            await panel_msg.edit(embed=embed, view=view)
        except Exception:
            pass

    async def _update_ready_panel(self, session_id: str, disable_view: bool = False):
        session = self.sessions.get(session_id)
        if not session:
            return
        embed = self._build_ready_panel_embed(session)
        view = None if disable_view else TournamentReadyOpenView(self, session_id)
        await self._edit_flow_message(session_id, embed, view)

    def _build_match_json(self, session: Dict) -> Dict:
        final_maps = list(session.get("picked_maps", []))
        if session.get("maps_left"):
            final_maps.append(session["maps_left"][0])  # decider
        maplist = [MAP_NAME_CONVERT.get(m, m) for m in final_maps]
        team1_players = {str(p["steamid"]): str(p["players"]) for p in session["team1_players"]}
        team2_players = {str(p["steamid"]): str(p["players"]) for p in session["team2_players"]}
        players_per_team = self._mode_size(session["mode"]) or 5
        matchid_int = int(session["matchid"])
        mode_raw = str(session.get("mode", "")).lower().strip()
        # CS2:
        # - 5x5 competitive: game_type 0 / game_mode 1
        # - 2x2 wingman:     game_type 0 / game_mode 2
        extra_mode_cvars = {}
        if mode_raw == "5x5":
            extra_mode_cvars = {"game_type": "0", "game_mode": "1"}
        elif mode_raw == "2x2":
            extra_mode_cvars = {"game_type": "0", "game_mode": "2"}
        return {
            "match_type": "tournament",
            "mode": session["mode"],
            "series": session["series"],
            "matchid": matchid_int,
            "num_maps": self._series_num_maps(session["series"]),
            "skip_veto": True,
            "players_per_team": players_per_team,
            "maplist": maplist,
            "team1": {"name": session["team1_name"], "players": team1_players},
            "team2": {"name": session["team2_name"], "players": team2_players},
            "spectators": {
                "players": {},
                "name": "Streamer",  # TODO: set streamer steamid when available
            },
            "clinch_series": True,
            "cvars": {
                "hostname": f"{session['team1_name']} vs {session['team2_name']} #{session['matchid']}",
                "sv_password": session["match_password"],
                "matchzy_demo_name_format": f"tournament_{session['matchid']}",
                "matchzy_autostart_mode": "1",
                "matchzy_minimum_ready_required": str(players_per_team),
                "matchzy_knife_enabled_default": "1",
                **extra_mode_cvars,
            },
        }

    async def _start_map_veto(self, session_id: str):
        session = self.sessions.get(session_id)
        if not session:
            return
        session["status"] = "veto"
        session["maps_left"] = self._map_pool_for_mode(session["mode"])
        session["banned_maps"] = []
        session["picked_maps"] = []
        session["steps"] = self._series_steps(
            session["series"], session["mode"], session["team1_name"], session["team2_name"], len(session["maps_left"])
        )
        session["step_index"] = 0

        embed = self._build_veto_embed(session)
        view = TournamentBanView(self, session_id, session["maps_left"])
        await self._edit_flow_message(session_id, embed, view)
        session["veto_message_id"] = session.get("ready_panel_message_id")

    async def _process_ready_click(
        self,
        interaction: discord.Interaction,
        session_id: str,
        expected_user_id: Optional[int] = None,
    ):
        session = self.sessions.get(session_id)
        if not session:
            await interaction.response.send_message("Tournament session not found.", ephemeral=True)
            return
        if expected_user_id is not None and interaction.user.id != expected_user_id:
            await interaction.response.send_message("This button is not for you.", ephemeral=True)
            return
        if interaction.user.id not in session.get("all_player_ids", []):
            await interaction.response.send_message("You are not part of this match.", ephemeral=True)
            return
        if session.get("status") != "waiting_ready":
            await interaction.response.send_message("READY window is closed.", ephemeral=True)
            return
        if interaction.user.id not in session["pending_ids"]:
            await interaction.response.send_message("You have already confirmed READY.", ephemeral=True)
            return

        team_key = session["user_team"].get(interaction.user.id)
        invite_url = session["team_invites"].get(team_key)
        target_voice_id = session["team_voice_ids"].get(team_key)

        session["pending_ids"].remove(interaction.user.id)
        session["ready_ids"].add(interaction.user.id)

        guild = self.bot.get_guild(session["guild_id"])
        moved = False
        if guild and target_voice_id:
            member = guild.get_member(interaction.user.id)
            voice_channel = guild.get_channel(target_voice_id)
            if member and member.voice and voice_channel and isinstance(voice_channel, discord.VoiceChannel):
                try:
                    await member.move_to(voice_channel)
                    moved = True
                except Exception:
                    moved = False

        msg = "READY confirmed."
        if moved:
            msg += " You have been moved to your room."
        elif invite_url:
            msg += f" Join your team's room: {invite_url}"
        await interaction.response.send_message(msg, ephemeral=True)
        await self._update_ready_panel(session_id)

        if not session["pending_ids"]:
            task = session.get("ready_timeout_task")
            if task:
                task.cancel()
            await self._update_ready_panel(session_id, disable_view=True)
            await self._start_map_veto(session_id)

    async def handle_ready_click_open(self, interaction: discord.Interaction, session_id: str):
        await self._process_ready_click(interaction, session_id, None)

    async def handle_map_ban(self, interaction: discord.Interaction, session_id: str, map_name: str):
        session = self.sessions.get(session_id)
        if not session:
            await interaction.response.send_message("Tournament session not found.", ephemeral=True)
            return
        if session.get("status") == "map_done":
            await interaction.response.send_message("Veto finished. Waiting for match load.", ephemeral=True)
            return
        if session.get("status") != "veto":
            logger.warning(
                f"TOURNAMENT[{session_id}] veto click ignored status={session.get('status')} "
                f"user={interaction.user.id} map={map_name}"
            )
            await interaction.response.send_message("Veto is not active.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in session["captain_ids"].values():
            logger.warning(
                f"TOURNAMENT[{session_id}] veto denied user={user_id} is not captain"
            )
            await interaction.response.send_message("Only captains can ban/pick maps.", ephemeral=True)
            return

        if map_name not in session["maps_left"]:
            logger.warning(
                f"TOURNAMENT[{session_id}] veto on invalid map user={user_id} map={map_name}"
            )
            await interaction.response.send_message("Map has already been removed.", ephemeral=True)
            return

        steps = session.get("steps", [])
        step_index = int(session.get("step_index", 0))
        if step_index >= len(steps):
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass
            await interaction.response.send_message("Veto flow has already been completed.", ephemeral=True)
            return

        current_action, current_team = steps[step_index]
        expected_captain = int(session["captain_ids"][current_team])
        if user_id != expected_captain:
            logger.warning(
                f"TOURNAMENT[{session_id}] veto out of turn user={user_id} expected={expected_captain} "
                f"action={current_action} team={current_team}"
            )
            await interaction.response.send_message(
                f"It is now the turn of the captain of **{current_team}**.",
                ephemeral=True,
            )
            return

        logger.info(
            f"TOURNAMENT[{session_id}] veto accepted user={user_id} team={current_team} "
            f"action={current_action} map={map_name} step={step_index+1}/{len(steps)}"
        )
        session["maps_left"].remove(map_name)
        if current_action == "pick":
            session["picked_maps"].append(map_name)
        else:
            session["banned_maps"].append(map_name)
        session["step_index"] = step_index + 1

        if session["step_index"] >= len(steps):
            session["status"] = "map_done"
            payload = self._build_match_json(session)
            logger.info(
                f"TOURNAMENT[{session_id}] veto finished match={session.get('matchid')} "
                f"maps={payload.get('maplist')}"
            )
            processing = discord.Embed(
                title="⏳ Finishing veto...",
                description="Loading match on the reserved server. Please wait a few seconds.",
                color=0xF1C40F,
            )
            processing.set_thumbnail(url=MIX_LOGO_URL)
            await interaction.response.edit_message(embed=processing, view=None)
            try:
                await upsert_tournament_match(
                    matchid=str(session["matchid"]),
                    mode=str(session["mode"]),
                    series=str(session["series"]),
                    team1=str(session["team1_name"]),
                    team2=str(session["team2_name"]),
                )
            except Exception as e:
                logger.error(f"Failed to register tournament_matches ({session['matchid']}): {e}")

            try:
                await self._execute_tournament_match(session, payload)
                session["status"] = "live"
            except Exception as e:
                session["status"] = "failed"
                await self._release_runtime_server(session, reason="tourney_start_failed")
                tserver_id = session.get("tserver_id")
                if tserver_id and tserver_id in self._reserved_servers:
                    self._reserved_servers.discard(tserver_id)
                logger.error(
                    f"TOURNAMENT[{session_id}] failed to start match={session.get('matchid')} "
                    f"server={session.get('tserver_name')} error={self._compact_response(e)}"
                )
                err_txt = self._compact_response(e)
                fail_embed = discord.Embed(
                    title="❌ Failed to start match",
                    description=f"`{err_txt}`",
                    color=0xE74C3C,
                )
                fail_embed.set_footer(text="Use /cancelartorneio to end and create again.")
                fail_embed.set_thumbnail(url=MIX_LOGO_URL)
                await interaction.edit_original_response(embed=fail_embed, view=None)
                channel = self.bot.get_channel(session.get("text_channel_id", 0))
                if channel:
                    try:
                        await channel.send(f"❌ Failed to start match on the reserved server: `{err_txt}`")
                    except Exception:
                        pass
                return

            host = session.get("runtime_host")
            port = int(session.get("runtime_port") or 0)
            gotv_port = int(session.get("runtime_gotv_port") or 0)
            password = session.get("match_password", "")
            conn = (
                f"connect {host}:{port}; password {password}"
                if host and port > 0
                else "Runtime host/port not configured."
            )
            gotv = f"connect {host}:{gotv_port}" if host and gotv_port else "Not configured"

            final_maps_txt = "\n".join([f"- {m}" for m in payload.get("maplist", [])]) or "-"
            embed = discord.Embed(
                title=f"🏆 Tournament Match Ready #{session['matchid']}",
                description=f"✅ Series **{session['series'].upper()}** completed and match loaded.",
                color=0x2ECC71,
            )
            embed.add_field(name="⚔️ Mode", value=session["mode"], inline=True)
            embed.add_field(name="🆚 Match", value=f"{session['team1_name']} vs {session['team2_name']}", inline=True)
            embed.add_field(name="🖥️ Server", value=session.get("tserver_name", "-"), inline=True)
            embed.add_field(name="🗺️ Maplist", value=final_maps_txt, inline=False)
            embed.add_field(name="🎮 Play", value=f"```{conn}```", inline=False)
            embed.add_field(name="📺 Watch (GOTV)", value=f"```{gotv}```", inline=False)
            embed.set_thumbnail(url=MIX_LOGO_URL)
            await interaction.edit_original_response(embed=embed, view=None)

            return

        embed = self._build_veto_embed(session)
        view = TournamentBanView(self, session_id, session["maps_left"])
        await interaction.response.edit_message(embed=embed, view=view)

    @app_commands.command(name="cancelartorneio", description="Cancels an active tournament.")
    async def cancelar_torneio(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        interaction = getattr(ctx, "interaction", None)
        if interaction and not interaction.response.is_done():
            await interaction.response.defer(thinking=False, ephemeral=True)

        async def _send(msg: str):
            if interaction:
                return await interaction.followup.send(msg, ephemeral=True)
            return await ctx.send(msg)

        guild = ctx.guild
        if not guild:
            await _send("Use this command inside the server.")
            return

        session_id = None
        for sid, sess in self.sessions.items():
            if sess.get("guild_id") != guild.id:
                continue
            if sess.get("status") in ("finished", "failed", "cancelled_timeout"):
                continue
            session_id = sid
            break

        if not session_id:
            await _send("No active tournament to cancel.")
            return

        session = self.sessions.get(session_id)
        if not session:
            await _send("Tournament session not found.")
            return

        try:
            timeout_task = session.get("ready_timeout_task")
            if timeout_task:
                timeout_task.cancel()
        except Exception:
            pass

        session["status"] = "failed"
        tserver_id = session.get("tserver_id")
        if tserver_id and tserver_id in self._reserved_servers:
            self._reserved_servers.discard(tserver_id)

        try:
            await self._update_ready_panel(session_id, disable_view=True)
        except Exception:
            pass

        veto_channel = self.bot.get_channel(session.get("text_channel_id", 0))
        veto_msg_id = session.get("veto_message_id")
        if veto_channel and veto_msg_id:
            try:
                veto_msg = await veto_channel.fetch_message(veto_msg_id)
                await veto_msg.edit(view=None)
            except Exception:
                pass

        await self._release_runtime_server(session, reason="tourney_manual_cancel")
        await self._move_waiting_players_to_exit(session)
        await self._cleanup_session_resources(session)
        self.sessions.pop(session_id, None)
        await _send("Tournament cancelled and resources cleaned.")

    @app_commands.command(name="statusservidorestorneio", description="Shows tournament server status (TS1..TS5).")
    async def status_servidores_torneio(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        embed = discord.Embed(
            title="Tournament Server Status",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=MIX_LOGO_URL)

        active_by_server: Dict[str, Dict] = {}
        for sid, sess in self.sessions.items():
            if sess.get("status") in ("finished", "failed", "cancelled_timeout"):
                continue
            t_id = str(sess.get("tserver_id") or "").strip()
            if not t_id:
                continue
            active_by_server[t_id] = sess

        lines = []
        snapshot = await get_server_pool().status_snapshot(source="tourney")
        for item in snapshot:
            sid = str(item.get("runtime_id") or "").strip()
            sess = active_by_server.get(sid)
            reserved = sid in self._reserved_servers
            busy = bool(item.get("busy")) or reserved or sess is not None
            status = "🔴 BUSY" if busy else "🟢 FREE"

            host = str(item.get("host") or "-")
            port = int(item.get("port") or 0)
            gotv_port = int(item.get("gotv_port") or 0)
            complete_cfg = bool(host and host != "-" and port > 0)

            match_txt = "-"
            teams_txt = "-"
            if sess:
                match_txt = str(sess.get("matchid") or "-")
                t1 = str(sess.get("team1_name") or "?")
                t2 = str(sess.get("team2_name") or "?")
                teams_txt = f"{t1} vs {t2}"
            elif item.get("match_id"):
                match_txt = str(item.get("match_id"))

            cfg_txt = "OK" if complete_cfg else "INCOMPLETE"
            gotv_txt = str(gotv_port) if gotv_port > 0 else "-"
            lines.append(
                f"**{sid}** (slot {item.get('slot_id')})\n"
                f"Status: {status} | Config: `{cfg_txt}`\n"
                f"Host: `{host}:{port}` | GOTV: `{gotv_txt}`\n"
                f"Match: `{match_txt}` | Teams: {teams_txt}"
            )

        embed.description = "\n\n".join(lines) if lines else "No tournament servers configured."
        await self._reply_ctx(ctx, embed=embed)

    async def _ready_timeout(self, session_id: str):
        await asyncio.sleep(READY_TIMEOUT_SECONDS)
        session = self.sessions.get(session_id)
        if not session:
            return
        if session.get("status") != "waiting_ready":
            return

        session["status"] = "cancelled_timeout"
        tserver_id = session.get("tserver_id")
        if tserver_id and tserver_id in self._reserved_servers:
            self._reserved_servers.discard(tserver_id)
        await self._release_runtime_server(session, reason="tourney_ready_timeout")
        channel = self.bot.get_channel(session["text_channel_id"])
        if channel:
            timeout_embed = discord.Embed(
                title="❌ Tournament Cancelled",
                description="READY time expired (10 min).",
                color=0xE74C3C,
            )
            timeout_embed.set_thumbnail(url=MIX_LOGO_URL)
            timeout_embed.set_footer(text="Use /torneio to start again.")
            await self._edit_flow_message(session_id, timeout_embed, None)

        await self._move_waiting_players_to_exit(session)
        await self._cleanup_session_resources(session)
        self.sessions.pop(session_id, None)

    @app_commands.command(name="cadastartime", description="Creates a tournament team and sets the captain.")
    @app_commands.describe(
        nome="Team name",
        capitao="Team captain",
    )
    async def cadastrar_time(self, interaction: discord.Interaction, nome: str, capitao: discord.Member):
        ctx = await commands.Context.from_interaction(interaction)
        team = (nome or "").strip()
        if not team:
            await self._reply_ctx(ctx, "Enter a valid team name.")
            return

        captain_steamid = await self._steamid_from_ranking(capitao.id)
        if not captain_steamid:
            await self._reply_ctx(
                ctx,
                f"Captain **{capitao.display_name}** does not have a valid SteamID on record. "
                "Ask them to link with `/cadastro`."
            )
            return

        existing_captain = await get_tournament_team_captain(team)
        if existing_captain and int(existing_captain.get("discord_id") or 0) != capitao.id:
            await self._reply_ctx(ctx, f"Team **{team}** already has a registered captain.")
            return

        await upsert_tournament_team_player(
            team_name=team,
            player_name=capitao.display_name,
            steamid64=captain_steamid,
            discord_id=capitao.id,
            is_captain=True,
        )
        await self._reply_ctx(ctx, f"Team **{team}** created/updated with captain **{capitao.display_name}**.")

    @app_commands.command(name="adicionarjogadortime", description="Adds a player to an existing team.")
    @app_commands.describe(
        time="Team name",
        jogador="Player to add",
    )
    @app_commands.autocomplete(time=_team_name_autocomplete)
    async def adicionar_jogador_time(self, interaction: discord.Interaction, time: str, jogador: discord.Member):
        ctx = await commands.Context.from_interaction(interaction)
        team = (time or "").strip()
        if not team:
            await self._reply_ctx(ctx, "Enter a valid team name.")
            return

        captain = await get_tournament_team_captain(team)
        if not captain:
            await self._reply_ctx(ctx, f"Team **{team}** does not exist or has no captain. Use `/cadastartime` first.")
            return

        steamid = await self._steamid_from_ranking(jogador.id)
        if not steamid:
            await self._reply_ctx(
                ctx,
                f"Player **{jogador.display_name}** does not have a valid SteamID on record. "
                "Ask them to link with `/cadastro`."
            )
            return

        await upsert_tournament_team_player(
            team_name=team,
            player_name=jogador.display_name,
            steamid64=steamid,
            discord_id=jogador.id,
            is_captain=False,
        )
        await self._reply_ctx(ctx, f"Player **{jogador.display_name}** added to team **{team}**.")

    @app_commands.command(name="removerplayertime", description="Removes a player from a registered team.")
    @app_commands.describe(
        time="Team name",
        jogador="Player to remove",
    )
    @app_commands.autocomplete(time=_team_name_autocomplete)
    async def remover_player_time(self, interaction: discord.Interaction, time: str, jogador: discord.Member):
        ctx = await commands.Context.from_interaction(interaction)
        team = (time or "").strip()
        if not team:
            await self._reply_ctx(ctx, "Enter a valid team name.")
            return

        players = await get_tournament_team_players(team)
        if not players:
            await self._reply_ctx(ctx, f"Team **{team}** does not exist.")
            return

        target = next((p for p in players if int(p.get("discord_id") or 0) == jogador.id), None)
        if not target:
            await self._reply_ctx(ctx, f"Player **{jogador.display_name}** is not in team **{team}**.")
            return

        rows = await remove_tournament_team_player(team, jogador.id)
        if rows <= 0:
            await self._reply_ctx(ctx, "Could not remove the player (no rows affected).")
            return

        remaining = await get_tournament_team_players(team)
        if not remaining:
            await self._reply_ctx(
                ctx,
                f"Player **{jogador.display_name}** removed from **{team}**. "
                "The team is now empty."
            )
            return

        captain = next((p for p in remaining if int(p.get("is_captain") or 0) == 1), None)
        if captain:
            await self._reply_ctx(ctx, f"Player **{jogador.display_name}** removed from team **{team}**.")
        else:
            await self._reply_ctx(
                ctx,
                f"Player **{jogador.display_name}** removed from team **{team}**.\n"
                "Warning: the team has no captain. Use `/cadastartime` to set one."
            )

    @app_commands.command(name="listatimes", description="Lists teams registered in the championship.")
    async def lista_times(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        names = await list_tournament_team_names()
        if not names:
            await self._reply_ctx(ctx, "No teams registered.")
            return

        guild = ctx.guild
        sections = []
        for team_name in names:
            players = await get_tournament_team_players(team_name)
            captain = next((p for p in players if int(p.get("is_captain") or 0) == 1), None)
            captain_label = "No captain"
            if captain:
                cap_id = int(captain.get("discord_id") or 0)
                if guild and cap_id:
                    member = guild.get_member(cap_id)
                    captain_label = member.mention if member else str(captain.get("players") or "Captain")
                else:
                    captain_label = str(captain.get("players") or "Captain")

            sorted_players = sorted(
                players,
                key=lambda p: (0 if int(p.get("is_captain") or 0) == 1 else 1, str(p.get("players") or "").lower()),
            )
            group_label = "-"
            if sorted_players:
                grp = str(sorted_players[0].get("group_name") or "").strip().upper()
                if grp in ("A", "B"):
                    group_label = grp
            player_lines = []
            for p in sorted_players:
                p_name = str(p.get("players") or "Unknown")
                p_id = int(p.get("discord_id") or 0)
                p_member = guild.get_member(p_id) if guild and p_id else None
                p_label = p_member.mention if p_member else p_name
                if int(p.get("is_captain") or 0) == 1:
                    p_label = f"{p_label} (Captain)"
                player_lines.append(f"- {p_label}")

            section = (
                f"**{team_name}**\n"
                f"Group: {group_label}\n"
                f"Captain: {captain_label}\n"
                f"Players ({len(players)}):\n"
                f"{chr(10).join(player_lines) if player_lines else '- No players'}"
            )
            sections.append(section)

        chunk = []
        current_len = 0
        for section in sections:
            block = section + "\n\n"
            block_len = len(block)
            if current_len + block_len > 1900 and chunk:
                await self._reply_ctx(ctx, "\n".join(chunk))
                chunk = []
                current_len = 0
            chunk.append(section)
            current_len += block_len
        if chunk:
            await self._reply_ctx(ctx, "\n".join(chunk))

    @app_commands.command(name="definirgrupotime", description="Sets group (A/B) for a team.")
    @app_commands.describe(time="Team name", grupo="Team group (A or B)")
    @app_commands.choices(
        grupo=[
            app_commands.Choice(name="Group A", value="A"),
            app_commands.Choice(name="Group B", value="B"),
        ]
    )
    @app_commands.autocomplete(time=_team_name_autocomplete)
    async def definir_grupo_time(self, interaction: discord.Interaction, time: str, grupo: str):
        ctx = await commands.Context.from_interaction(interaction)
        team = (time or "").strip()
        grp = (grupo or "").strip().upper()
        if not team:
            await self._reply_ctx(ctx, "Enter a valid team name.")
            return
        if grp not in ("A", "B"):
            await self._reply_ctx(ctx, "Invalid group. Use A or B.")
            return
        players = await get_tournament_team_players(team)
        if not players:
            await self._reply_ctx(ctx, f"Team **{team}** does not exist.")
            return
        await set_tournament_team_group(team, grp)
        await self._reply_ctx(ctx, f"Team **{team}** set to **Group {grp}**.")

    @app_commands.command(name="tabelagrupos", description="Publishes/updates the group standings embed.")
    async def tabela_grupos(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        await self._upsert_groups_embed()
        if TOURN_GROUPS_CHANNEL_ID:
            await self._reply_ctx(ctx, f"Group standings updated in <#{TOURN_GROUPS_CHANNEL_ID}>.")
        else:
            await self._reply_ctx(ctx, "TOURN_GROUPS_CHANNEL_ID not configured.")

    @app_commands.command(name="cronogramajogos", description="Publishes/updates the match schedule embed.")
    async def cronograma_jogos(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        await self._upsert_schedule_embed()
        if TOURN_SCHEDULE_CHANNEL_ID:
            await self._reply_ctx(ctx, f"Match schedule updated in <#{TOURN_SCHEDULE_CHANNEL_ID}>.")
        else:
            await self._reply_ctx(ctx, "TOURN_SCHEDULE_CHANNEL_ID not configured.")

    @app_commands.command(name="wo", description="Applies W.O. to a tournament match.")
    @app_commands.describe(
        matchid="Match ID",
        vencedor="Winning team (W.O.)",
        perdedor="Losing team (W.O.)",
        serie="Match series (if needed to create in DB)",
    )
    @app_commands.choices(
        serie=[
            app_commands.Choice(name="md1", value="md1"),
            app_commands.Choice(name="md3", value="md3"),
            app_commands.Choice(name="md5", value="md5"),
        ]
    )
    @app_commands.autocomplete(vencedor=_team_name_autocomplete, perdedor=_team_name_autocomplete)
    async def aplicar_wo(
        self,
        interaction: discord.Interaction,
        matchid: str,
        vencedor: str,
        perdedor: str,
        serie: str = "md1",
    ):
        ctx = await commands.Context.from_interaction(interaction)
        mid = str(matchid or "").strip()
        winner = str(vencedor or "").strip()
        loser = str(perdedor or "").strip()
        series = str(serie or "md1").strip().lower()
        if not mid:
            await self._reply_ctx(ctx, "Enter a valid matchid.")
            return
        if not winner:
            await self._reply_ctx(ctx, "Enter the winning team name.")
            return
        if not loser:
            await self._reply_ctx(ctx, "Enter the losing team name.")
            return
        if winner == loser:
            await self._reply_ctx(ctx, "Winner and loser cannot be the same team.")
            return
        if series not in ("md1", "md3", "md5"):
            await self._reply_ctx(ctx, "Invalid series. Use md1, md3 or md5.")
            return

        match = await get_tournament_match_by_id(mid)
        created = False
        if not match:
            winner_players = await get_tournament_team_players(winner)
            loser_players = await get_tournament_team_players(loser)
            if not winner_players:
                await self._reply_ctx(ctx, f"The winning team **{winner}** does not exist in tournament_teams.")
                return
            if not loser_players:
                await self._reply_ctx(ctx, f"The losing team **{loser}** does not exist in tournament_teams.")
                return

            winner_size = len(winner_players)
            loser_size = len(loser_players)
            if winner_size != loser_size or winner_size not in (1, 2, 5):
                await self._reply_ctx(
                    ctx,
                    "Could not infer mode automatically. "
                    "Make sure both teams have the same size (1, 2 or 5 players).",
                )
                return

            mode = {1: "1x1", 2: "2x2", 5: "5x5"}[winner_size]
            await upsert_tournament_match(
                matchid=mid,
                mode=mode,
                series=series,
                team1=winner,
                team2=loser,
            )
            match = await get_tournament_match_by_id(mid)
            created = True
            if not match:
                await self._reply_ctx(ctx, f"Failed to create match `{mid}` in tournament_matches.")
                return

        team1 = str(match.get("team1") or "").strip()
        team2 = str(match.get("team2") or "").strip()
        if winner not in (team1, team2) or loser not in (team1, team2):
            await self._reply_ctx(
                ctx,
                f"Invalid teams for this match. Match teams: **{team1}** and **{team2}**.",
            )
            return
        if not ((winner == team1 and loser == team2) or (winner == team2 and loser == team1)):
            await self._reply_ctx(
                ctx,
                f"For match `{mid}` the teams must be exactly: "
                f"winner/loser between **{team1}** and **{team2}**.",
            )
            return

        series = str(match.get("series") or "md1")
        needed_wins = (self._series_num_maps(series) // 2) + 1
        score1 = needed_wins if winner == team1 else 0
        score2 = needed_wins if winner == team2 else 0

        rows = await set_tournament_match_result(
            matchid=mid,
            winner=winner,
            team1_score=score1,
            team2_score=score2,
            result_type="WO",
        )
        if rows <= 0:
            await self._reply_ctx(ctx, "Could not apply W.O. (no rows affected).")
            return

        created_txt = "Match created and " if created else ""
        await self._reply_ctx(
            ctx,
            f"{created_txt}W.O. applied for match `{mid}`.\n"
            f"Winner: **{winner}** | Loser: **{loser}**\n"
            f"Score registered: **{team1} {score1}x{score2} {team2}**",
        )

    @app_commands.command(name="torneio", description="Creates a tournament match with READY and map veto/picks.")
    @app_commands.choices(
        modo=[
            app_commands.Choice(name="1x1", value="1x1"),
            app_commands.Choice(name="2x2", value="2x2"),
            app_commands.Choice(name="5x5", value="5x5"),
        ],
        servidor=[
            app_commands.Choice(name="Auto (first available)", value="auto"),
            app_commands.Choice(name="TS1", value="tserver1"),
            app_commands.Choice(name="TS2", value="tserver2"),
            app_commands.Choice(name="TS3", value="tserver3"),
            app_commands.Choice(name="TS4", value="tserver4"),
            app_commands.Choice(name="TS5", value="tserver5"),
        ],
    )
    @app_commands.describe(
        modo="Mode",
        serie="Series format",
        team1="Team 1 name",
        team2="Team 2 name",
        servidor="Tournament server (TS1..TS5) or Auto",
    )
    @app_commands.autocomplete(team1=_team_name_autocomplete, team2=_team_name_autocomplete, serie=_serie_autocomplete)
    async def torneio(self, interaction: discord.Interaction, modo: str, serie: str, team1: str, team2: str, servidor: str = "auto"):
        ctx = await commands.Context.from_interaction(interaction)
        interaction = getattr(ctx, "interaction", None)
        if interaction and not interaction.response.is_done():
            await interaction.response.defer(thinking=False)

        async def _send(*args, **kwargs):
            if interaction:
                return await interaction.followup.send(*args, **kwargs)
            return await ctx.send(*args, **kwargs)

        guild = ctx.guild
        if not guild:
            await _send("Use this command inside the server.")
            return
        if TORNEIO_PICKS_BANS_CHANNEL_ID <= 0:
            await _send("TORNEIO_PICKS_BANS_CHANNEL_ID not configured.")
            return
        if not ctx.channel or ctx.channel.id != TORNEIO_PICKS_BANS_CHANNEL_ID:
            picks_channel = guild.get_channel(TORNEIO_PICKS_BANS_CHANNEL_ID)
            target = picks_channel.mention if picks_channel else f"<#{TORNEIO_PICKS_BANS_CHANNEL_ID}>"
            await _send(f"This command can only be used in {target}.")
            return
        if TORNEIO_CATEGORY_ID <= 0:
            await _send("TORNEIO_CATEGORY_ID not configured.")
            return
        fixed_category = guild.get_channel(TORNEIO_CATEGORY_ID)
        if not fixed_category or not isinstance(fixed_category, discord.CategoryChannel):
            await _send("Fixed tournament category not found.")
            return
        picks_bans_channel = guild.get_channel(TORNEIO_PICKS_BANS_CHANNEL_ID)
        if not picks_bans_channel or not isinstance(picks_bans_channel, discord.TextChannel):
            await _send("Fixed picks/bans channel not found.")
            return
        if team1 == team2:
            await _send("Select two different teams.")
            return

        size = self._mode_size(modo)
        if not size:
            await _send("Invalid mode. Use 1x1, 2x2 or 5x5.")
            return

        serie = (serie or "").lower().strip()
        allowed_series = self._allowed_series_for_mode(modo)
        if serie not in allowed_series:
            allowed_txt = ", ".join(allowed_series) if allowed_series else "md1"
            await _send(f"Invalid series for {modo}. Options: {allowed_txt}.")
            return

        team1_players = await get_tournament_team_players(team1)
        team2_players = await get_tournament_team_players(team2)
        if len(team1_players) != size:
            await _send(f"Team **{team1}** must have exactly {size} player(s).")
            return
        if len(team2_players) != size:
            await _send(f"Team **{team2}** must have exactly {size} player(s).")
            return

        captain1 = next((p for p in team1_players if int(p.get("is_captain") or 0) == 1), None)
        captain2 = next((p for p in team2_players if int(p.get("is_captain") or 0) == 1), None)
        if not captain1 or not captain2:
            await _send("Both teams need a captain set via `/cadastartime`.")
            return

        pool_preview = self._map_pool_for_mode(modo)
        if serie == "md3":
            needed = 4 if modo == "2x2" else 7
            if len(pool_preview) < needed:
                await _send(f"MD3 in {modo} requires a pool of at least {needed} maps.")
                return
        if serie == "md5" and len(pool_preview) < 7:
            await _send("MD5 requires a pool of at least 7 maps.")
            return

        team1_ids = [int(p["discord_id"]) for p in team1_players]
        team2_ids = [int(p["discord_id"]) for p in team2_players]
        all_ids = team1_ids + team2_ids

        missing = [uid for uid in all_ids if guild.get_member(uid) is None]
        if missing:
            await _send("Some registered players are not in this server. Adjust registrations.")
            return

        picked, pick_error = await self._pick_tournament_server(servidor)
        if not picked:
            await _send(pick_error or "Could not select a tournament server.")
            return
        tserver_id = picked
        self._reserved_servers.add(tserver_id)

        session_id = str(int(time.time()))
        # MatchZy requires int32. Unix timestamp fits and avoids overflow.
        matchid = int(time.time())
        match_password = str(secrets.randbelow(900000) + 100000)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                connect=True,
                move_members=True,
            ),
        }

        for role_id in STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_channels=True, connect=True, move_members=True
                )

        for uid in all_ids:
            member = guild.get_member(uid)
            if member:
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        try:
            voice1_overwrites = dict(overwrites)
            voice2_overwrites = dict(overwrites)
            for uid in team1_ids:
                member = guild.get_member(uid)
                if member:
                    voice1_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
                    voice2_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=False, speak=False)
            for uid in team2_ids:
                member = guild.get_member(uid)
                if member:
                    voice2_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
                    voice1_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=False, speak=False)

            voice1_name = f"{team1}-{matchid}"
            voice2_name = f"{team2}-{matchid}"
            voice1 = await guild.create_voice_channel(voice1_name[:100], category=fixed_category, overwrites=voice1_overwrites)
            voice2 = await guild.create_voice_channel(voice2_name[:100], category=fixed_category, overwrites=voice2_overwrites)

            invite1 = await voice1.create_invite(max_age=READY_TIMEOUT_SECONDS, max_uses=0, unique=True)
            invite2 = await voice2.create_invite(max_age=READY_TIMEOUT_SECONDS, max_uses=0, unique=True)

            text_overwrite_user_ids = []
            for uid in all_ids:
                member = guild.get_member(uid)
                if not member:
                    continue
                try:
                    await picks_bans_channel.set_permissions(
                        member,
                        view_channel=True,
                        send_messages=False,
                        add_reactions=False,
                        read_message_history=True,
                        reason="Tournament: bot interaction only",
                    )
                    text_overwrite_user_ids.append(uid)
                except Exception:
                    pass
        except Exception as e:
            self._reserved_servers.discard(tserver_id)
            await _send(f"Could not create match structure: `{self._compact_response(e)}`")
            return

        session = {
            "session_id": session_id,
            "matchid": matchid,
            "match_password": match_password,
            "guild_id": guild.id,
            "mode": modo,
            "series": serie,
            "team1_name": team1,
            "team2_name": team2,
            "team1_players": team1_players,
            "team2_players": team2_players,
            "all_player_ids": all_ids,
            "pending_ids": set(all_ids),
            "ready_ids": set(),
            "status": "waiting_ready",
            "category_id": fixed_category.id,
            "text_channel_id": picks_bans_channel.id,
            "voice_team1_id": voice1.id,
            "voice_team2_id": voice2.id,
            "team_voice_ids": {team1: voice1.id, team2: voice2.id},
            "team_invites": {team1: invite1.url, team2: invite2.url},
            "generated_channel_ids": [voice1.id, voice2.id],
            "text_overwrite_user_ids": text_overwrite_user_ids,
            "user_team": {uid: team1 for uid in team1_ids} | {uid: team2 for uid in team2_ids},
            "captain_ids": {
                team1: int(captain1["discord_id"]),
                team2: int(captain2["discord_id"]),
            },
            "captain_profiles": {
                team1: {
                    "name": (guild.get_member(int(captain1["discord_id"])) or None).display_name
                    if guild.get_member(int(captain1["discord_id"]))
                    else str(captain1.get("players") or "Captain"),
                    "avatar": str((guild.get_member(int(captain1["discord_id"])) or None).display_avatar.url)
                    if guild.get_member(int(captain1["discord_id"]))
                    else None,
                },
                team2: {
                    "name": (guild.get_member(int(captain2["discord_id"])) or None).display_name
                    if guild.get_member(int(captain2["discord_id"]))
                    else str(captain2.get("players") or "Captain"),
                    "avatar": str((guild.get_member(int(captain2["discord_id"])) or None).display_avatar.url)
                    if guild.get_member(int(captain2["discord_id"]))
                    else None,
                },
            },
            "tserver_id": tserver_id,
            "tserver_name": tserver_id,
        }
        self.sessions[session_id] = session

        ready_panel_embed = self._build_ready_panel_embed(session)
        ready_panel = await picks_bans_channel.send(
            embed=ready_panel_embed,
            view=TournamentReadyOpenView(self, session_id),
        )
        session["ready_panel_message_id"] = ready_panel.id

        session["ready_timeout_task"] = asyncio.create_task(self._ready_timeout(session_id))

        if interaction:
            await interaction.followup.send(
                f"Tournament created: **{team1} vs {team2}** | MatchID `{matchid}`. "
                "Check the READY panel in the picks/bans channel.",
                ephemeral=True,
            )
        else:
            await ctx.send(f"Tournament created with MatchID `{matchid}`. Check the panel in the picks/bans channel.")


async def setup(bot: commands.Bot):
    await bot.add_cog(TournamentCog(bot))
