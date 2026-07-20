import discord
from discord.ext import commands, tasks
from loguru import logger
import asyncio
from typing import Optional, Dict, List
import secrets
from datetime import date

from aiohttp import web

from bot.database import (
    get_match_overview, is_match_posted, mark_match_posted,
    update_ranks, get_match_details, get_match_players,
    get_top_ranking, get_active_matches,
    get_recent_mix_match_ids, get_match_server_id, get_match_runtime_server,
    fix_match_winner_from_maps,
)
from bot.config import (
    CANAL_RESUMO_ID, CANAL_RANKING_ID, MAP_IMAGES,
    SALA_PROXIMO_ID,
    MATCH_CHECK_INTERVAL, SERVERS, MATCHZY_WEBHOOK_KEY, SEASON_START_DATE,
    DEMO_DOWNLOAD_URL,
)
from bot.cogs.mix import (
    sessions,
    global_state,
    reset_session,
    get_online_count,
    get_progress_bar,
    ALWAYS_ALLOW_STEAMID64,
    refresh_server_category_visibility,
)
from bot.cogs.fila import get_queue_cog
from bot.cogs.monitor import update_monitor_combined
from bot.cogs.ranking import build_ranking_embed
from bot.cogs.denuncias import MatchFeedbackView
from bot.utils.maps import normalize_map_key
from bot.utils.server_pool import get_server_pool



class MatchesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._season_start_date: Optional[str] = None
        self._finalize_locks: Dict[int, asyncio.Lock] = {}
        raw_cutoff = str(SEASON_START_DATE or "").strip()
        if raw_cutoff:
            try:
                date.fromisoformat(raw_cutoff)
                self._season_start_date = raw_cutoff
            except ValueError:
                logger.warning(
                    f"Invalid SEASON_START_DATE ('{raw_cutoff}'). Backfill without date filter."
                )

    async def cog_load(self):
        self.check_matches.start()
        self.bot.loop.create_task(self.backfill_unposted_matches())
        setattr(self.bot, "match_webhook_handler", self.handle_match_webhook)
        logger.debug("Match monitoring started")

    async def cog_unload(self):
        self.check_matches.cancel()
        if hasattr(self.bot, "match_webhook_handler"):
            setattr(self.bot, "match_webhook_handler", None)

    @staticmethod
    def _is_decisive_map_score(score1: int, score2: int) -> bool:
        hi = max(int(score1 or 0), int(score2 or 0))
        lo = min(int(score1 or 0), int(score2 or 0))

        # MR12 (mp_maxrounds=24): fim da regulacao.
        if hi == 13 and lo <= 11:
            return True

        # OT MR3 por lado (mp_overtime_maxrounds=6):
        # placares finais validos: 16-x, 19-x, 22-x, ... (x no maximo hi-2 e no minimo hi-4).
        # Isso evita falso positivo em placares intermediarios como 13-12, 14-12, 15-12.
        if hi >= 16 and ((hi - 16) % 3 == 0):
            return (hi - 4) <= lo <= (hi - 2)

        return False

    async def _repair_missing_winner_from_map(self, match_id: int, match: Optional[Dict]) -> Optional[Dict]:
        if not match:
            return match

        win1 = int(match.get("win1") or 0)
        win2 = int(match.get("win2") or 0)
        if win1 == 1 or win2 == 1:
            return match

        map_s1 = int(match.get("team1_score") or 0)
        map_s2 = int(match.get("team2_score") or 0)
        if not self._is_decisive_map_score(map_s1, map_s2):
            return match

        repaired = await fix_match_winner_from_maps(int(match_id))
        if not repaired:
            return match

        logger.warning(
            f"MATCH_REPAIR: vencedor ausente no MatchZy; ajustado por placar do mapa match={match_id} "
            f"map={map_s1}x{map_s2}"
        )
        refreshed = await get_match_overview(int(match_id))
        return refreshed or match

    def _get_finalize_lock(self, match_id: int) -> asyncio.Lock:
        lock = self._finalize_locks.get(int(match_id))
        if lock is None:
            lock = asyncio.Lock()
            self._finalize_locks[int(match_id)] = lock
        return lock

    @staticmethod
    def _score_fields(match: Optional[Dict]) -> tuple[int, int]:
        if not match:
            return 0, 0
        return int(match.get("team1_score") or 0), int(match.get("team2_score") or 0)

    @staticmethod
    def _competitive_player_steamids(match: Optional[Dict], players: List[Dict]) -> set[str]:
        if not match:
            return set()
        team_names = {
            str(match.get("team1_name") or "").strip(),
            str(match.get("team2_name") or "").strip(),
        }
        steamids: set[str] = set()
        for player in players:
            if str(player.get("team") or "").strip() not in team_names:
                continue
            steamid64 = str(player.get("steamid64") or "").strip()
            if not steamid64:
                continue
            steamids.add(steamid64)
        return steamids

    def _player_snapshot_is_complete(
        self,
        match: Optional[Dict],
        players: List[Dict],
        *,
        s_id_hint: Optional[str] = None,
    ) -> bool:
        competitive_steamids = self._competitive_player_steamids(match, players)
        if not competitive_steamids:
            return False

        team_names = {
            str((match or {}).get("team1_name") or "").strip(),
            str((match or {}).get("team2_name") or "").strip(),
        }
        competitive_rows = [
            player for player in players
            if str(player.get("team") or "").strip() in team_names
        ]

        session = sessions.get(s_id_hint or "")
        if session:
            expected_steamids = {
                str(steamid64).strip()
                for steamid64 in (session.get("player_steamids") or {}).values()
                if str(steamid64).strip()
            }
            if expected_steamids:
                missing_steamids = expected_steamids - competitive_steamids
                if missing_steamids:
                    logger.warning(
                        f"FINALIZE_MATCH: aguardando snapshot completo match={match.get('matchid')} "
                        f"server={s_id_hint} presentes={len(competitive_steamids)}/{len(expected_steamids)} "
                        f"faltando={len(missing_steamids)}"
                    )
                    return False
                return True

        if len(competitive_steamids) < 10:
            roster_debug = ", ".join(
                f"{str(row.get('team') or '?').strip()}:{str(row.get('name') or '?').strip()}:{str(row.get('steamid64') or '').strip()}"
                for row in competitive_rows[:12]
            ) or "-"
            logger.warning(
                f"FINALIZE_MATCH: snapshot parcial match={match.get('matchid')} "
                f"players={len(competitive_steamids)}/10 s_id={s_id_hint or '-'} roster=[{roster_debug}]"
            )
            return False
        return True

    def _match_has_final_result(self, match: Optional[Dict], *, authoritative_end: bool = False) -> bool:
        if not match:
            return False
        if int(match.get("win1") or 0) == 1 or int(match.get("win2") or 0) == 1:
            return True
        score1, score2 = self._score_fields(match)
        if self._is_decisive_map_score(score1, score2):
            return True
        if authoritative_end and score1 != score2 and (score1 > 0 or score2 > 0):
            return True
        return False

    @staticmethod
    def _payload_match_id(payload: dict):
        return payload.get("match_id") or payload.get("matchid")

    def _resolve_session_id_for_match(self, match_id: int, s_id_hint: Optional[str] = None) -> Optional[str]:
        if s_id_hint and s_id_hint in sessions:
            return s_id_hint
        for s_id, sess in sessions.items():
            try:
                if int(sess.get("match_id") or 0) == int(match_id):
                    return s_id
            except Exception:
                continue
        return None

    async def _load_match_finalize_payload(
        self,
        match_id: int,
        *,
        s_id_hint: Optional[str] = None,
        authoritative_end: bool = False,
        attempts: int = 5,
        delay_seconds: float = 2.0,
    ) -> tuple[Optional[Dict], List[Dict]]:
        last_match: Optional[Dict] = None
        last_players: List[Dict] = []
        for attempt in range(1, attempts + 1):
            match = await get_match_overview(match_id)
            if match:
                match = await self._repair_missing_winner_from_map(match_id, match)
                full_details = await get_match_details(match_id)
                if full_details:
                    match = full_details
            players = await get_match_players(match_id) if match else []
            last_match = match
            last_players = players
            if (
                match
                and players
                and self._match_has_final_result(match, authoritative_end=authoritative_end)
                and self._player_snapshot_is_complete(match, players, s_id_hint=s_id_hint)
            ):
                return match, players
            if attempt < attempts:
                await asyncio.sleep(delay_seconds)
        return last_match, last_players

    async def _finalize_match_end(
        self,
        match_id: int,
        *,
        s_id_hint: Optional[str] = None,
        trigger: str,
        authoritative_end: bool = False,
    ) -> Dict:
        lock = self._get_finalize_lock(match_id)
        async with lock:
            summary_already_posted = await is_match_posted(match_id)
            s_id = self._resolve_session_id_for_match(match_id, s_id_hint=s_id_hint)

            summary_skip_reason: Optional[str] = None
            if not summary_already_posted:
                summary_channel = self.bot.get_channel(CANAL_RESUMO_ID)
                if not isinstance(summary_channel, discord.TextChannel):
                    logger.warning(
                        f"FINALIZE_MATCH: canal de resumo indisponivel para match={match_id} trigger={trigger}"
                    )
                    summary_skip_reason = "summary_channel_unavailable"
                else:
                    match, players = await self._load_match_finalize_payload(
                        match_id,
                        s_id_hint=s_id,
                        authoritative_end=authoritative_end,
                    )
                    has_result = bool(
                        match
                        and players
                        and self._match_has_final_result(match, authoritative_end=authoritative_end)
                    )
                    snapshot_ok = has_result and self._player_snapshot_is_complete(
                        match, players, s_id_hint=s_id
                    )

                    if not has_result:
                        logger.warning(
                            f"FINALIZE_MATCH: dados finais ainda indisponiveis "
                            f"match={match_id} trigger={trigger} authoritative={authoritative_end} — liberando servidor mesmo assim"
                        )
                        summary_skip_reason = "match_data_not_ready"
                    else:
                        if not snapshot_ok:
                            competitive_count = len(
                                self._competitive_player_steamids(match, players)
                            )
                            logger.warning(
                                f"FINALIZE_MATCH: snapshot parcial ({competitive_count}/10) mas resultado disponivel — "
                                f"finalizando com dados parciais match={match_id} trigger={trigger}"
                            )
                        server_cfg = SERVERS.get(s_id) if s_id in SERVERS else {}
                        await self.move_teams_after_match(match, server_cfg, s_id or "", players)
                        await self.post_match_summary(summary_channel, match_id, match, players, server_id=s_id)
                        await self.update_ranking_channel()
                        summary_already_posted = True
                        logger.info(f"FINALIZE_MATCH: resumo publicado match={match_id} trigger={trigger} server={s_id}")

            release_result = await self._release_runtime_for_match(
                match_id,
                reason=f"{trigger}_end",
                stop_session=True,
            )

            cleared_servers: List[str] = []
            if s_id and s_id in sessions and int(sessions[s_id].get("match_id") or 0) == int(match_id):
                reset_session(s_id)
                await refresh_server_category_visibility(self.bot, s_id, reason=f"{trigger}_match_end")
                cleared_servers.append(s_id)
            else:
                for current_s_id, sess in sessions.items():
                    try:
                        if int(sess.get("match_id") or 0) != int(match_id):
                            continue
                    except Exception:
                        continue
                    reset_session(current_s_id)
                    await refresh_server_category_visibility(self.bot, current_s_id, reason=f"{trigger}_match_end")
                    cleared_servers.append(current_s_id)

            fila_cog = get_queue_cog(self.bot)
            if fila_cog:
                await fila_cog.request_display_update()
                await fila_cog.dispatch_ready_if_possible()

            return {
                "ok": True,
                "match_id": int(match_id),
                "posted": bool(summary_already_posted),
                "summary_skip_reason": summary_skip_reason,
                "released": bool(release_result.get("released")),
                "server_id": s_id,
                "cleared_servers": cleared_servers,
            }

    @tasks.loop(seconds=MATCH_CHECK_INTERVAL)
    async def check_matches(self):
        match_id = None
        try:
            channel = self.bot.get_channel(CANAL_RESUMO_ID)
            if not channel: return

            try:
                active_rows = await get_active_matches()
                for row in active_rows:
                    s_id = row.get("server_id")
                    m_id = row.get("match_id")
                    if s_id in sessions and m_id and not sessions[s_id].get("match_id"):
                        sessions[s_id]["match_id"] = int(m_id)
                        sessions[s_id]["active"] = True
                        sessions[s_id]["status"] = "LIVE"
            except:
                pass
            
            # sem fallback cruzado entre servidores

            for s_id, sess in sessions.items():
                if not sess.get("active") or not sess.get("match_id"):
                    continue
                match_id = sess.get("match_id")
                match = await get_match_overview(match_id)
                if not match: continue
                match = await self._repair_missing_winner_from_map(match_id, match)
                match_ended = (match.get('win1') == 1 or match.get('win2') == 1)
                if not match_ended: continue
                if await is_match_posted(match_id): continue

                logger.info(f"⚡ Match #{match_id} finished. Processing...")

                await self._finalize_match_end(
                    int(match_id),
                    s_id_hint=s_id,
                    trigger="poll",
                    authoritative_end=False,
                )
       
        except Exception as e:
            msg = f"#{match_id}" if match_id else "DB Error"
            logger.error(f"❌ Error in match loop ({msg}): {e}")

    @check_matches.before_loop
    async def before_check_matches(self):
        await self.bot.wait_until_ready()

    async def backfill_unposted_matches(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(2)
        channel = self.bot.get_channel(CANAL_RESUMO_ID)
        if not channel:
            return
        try:
            recent = await get_recent_mix_match_ids(20, start_date=self._season_start_date)
            if self._season_start_date:
                logger.info(
                    f"Match backfill with season filter: matches since {self._season_start_date}"
                )
            for row in reversed(recent):
                match_id = row.get("matchid")
                if not match_id:
                    continue
                if await is_match_posted(match_id):
                    continue
                match = await get_match_overview(match_id)
                if not match:
                    continue
                match = await self._repair_missing_winner_from_map(match_id, match)
                if not self._match_has_final_result(match):
                    continue
                players = await get_match_players(match_id)
                if not players:
                    continue
                full_details = await get_match_details(match_id)
                if full_details:
                    match = full_details
                await self.post_match_summary(channel, match_id, match, players)
                await self.update_ranking_channel()
        except Exception as e:
            logger.error(f"❌ Error in match backfill: {e}")

    async def _release_runtime_for_match(
        self,
        match_id: int,
        reason: str,
        stop_session: bool | None = None,
    ) -> Dict:
        try:
            result = await get_server_pool().release_server_for_match(
                int(match_id),
                reason=reason,
                stop_session=stop_session,
            )
            if result.get("released"):
                logger.info(
                    f"RUNTIME: match={match_id} liberado runtime={result.get('runtime_id')} "
                    f"stopped={result.get('stopped')} reason={reason}"
                )
            else:
                logger.info(f"RUNTIME: sem alocacao ativa para match={match_id} reason={reason}")
            return result
        except Exception as e:
            logger.error(f"RUNTIME: falha ao liberar match={match_id} reason={reason}: {e}")
            return {"released": False, "error": str(e), "match_id": int(match_id)}

    @staticmethod
    def _runtime_to_session_id(runtime_id: str) -> Optional[str]:
        """Converts runtime_id ('mix1') → session key ('server1')."""
        rid = runtime_id.strip().lower()
        for s_id, server in SERVERS.items():
            if server.get("runtime_id", "").lower() == rid:
                return s_id
        return None

    @staticmethod
    def _resolve_session_id(payload: dict) -> Optional[str]:
        """
        Resolves the session from the payload.
        1) Tries via server_id (runtime_id → s_id)
        2) Tries via match_id (looks for session with active match)
        3) Fallback: first LIVE session (only one active server)
        """
        runtime_id = str(payload.get("server_id") or "").strip().lower()
        if runtime_id:
            for s_id, server in SERVERS.items():
                if server.get("runtime_id", "").lower() == runtime_id:
                    return s_id

        match_id_raw = MatchesCog._payload_match_id(payload)
        if match_id_raw:
            try:
                mid = int(match_id_raw)
                for s_id, sess in sessions.items():
                    if int(sess.get("match_id") or 0) == mid:
                        return s_id
            except Exception:
                pass

        # Fallback: session that is LIVE
        live = [s for s, sess in sessions.items() if sess.get("status") == "LIVE" and sess.get("active")]
        if len(live) == 1:
            return live[0]

        return None

    async def _trigger_monitor_update(self) -> None:
        try:
            await update_monitor_combined(
                self.bot, sessions, global_state, reset_session,
                get_match_overview, get_online_count, get_progress_bar,
                get_active_matches,
            )
        except Exception as e:
            logger.debug(f"WEBHOOK: erro ao atualizar monitor: {e}")

    async def handle_match_webhook(self, request: web.Request) -> web.Response:
        if not MATCHZY_WEBHOOK_KEY:
            return web.json_response({"ok": False, "error": "webhook_not_configured"}, status=503)

        provided_key = request.headers.get("X-MatchZy-Key", "")
        if not secrets.compare_digest(provided_key, MATCHZY_WEBHOOK_KEY):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "invalid_payload_type"}, status=400)

        # MatchZy Enhanced usa "event_type"; sistema legado usa "event"
        event_name = str(
            payload.get("event_type") or payload.get("event") or ""
        ).strip().lower()

        if event_name in ("round_ended", "round_end"):
            return await self._handle_round_ended(payload)
        elif event_name in ("round_started", "round_start"):
            return await self._handle_round_started(payload)
        elif event_name in ("series_start", "series_started", "going_live"):
            return await self._handle_series_started(payload)
        elif event_name in ("series_end", "series_result", "series_ended", "map_result"):
            return await self._handle_series_result(payload)
        elif event_name in ("match_ended", "match_end", "end_match"):
            return await self._handle_match_ended_custom(payload)
        elif event_name in ("knife_round_started", "knife_started"):
            return await self._handle_knife_round_started(payload)
        elif event_name in ("knife_round_ended", "knife_ended"):
            return await self._handle_knife_round_ended(payload)
        elif event_name in ("halftime_started", "halftime"):
            return await self._handle_halftime_started(payload)
        elif event_name in ("overtime_started", "overtime"):
            return await self._handle_overtime_started(payload)
        elif event_name in ("match_paused", "paused"):
            return await self._handle_match_paused(payload)
        elif event_name in ("match_unpaused", "unpaused"):
            return await self._handle_match_unpaused(payload)
        elif event_name in ("player_round_stats", "player_stats", "round_stats"):
            return await self._handle_player_round_stats(payload)
        else:
            logger.debug(f"WEBHOOK: evento recebido: '{event_name}' (ignorado)")
            return web.json_response({"ok": True}, status=200)

    async def _handle_round_ended(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        runtime_id = str(payload.get("server_id") or "").strip().lower()
        match_id = self._payload_match_id(payload) or (sessions.get(s_id or "", {}).get("match_id"))
        logger.debug(f"WEBHOOK: round_ended server={runtime_id} s_id={s_id} match={match_id}")
        if s_id and s_id in sessions:
            # Clears transient end-of-round phases
            if sessions[s_id].get("match_phase") in ("knife", "halftime"):
                sessions[s_id]["match_phase"] = None
            round_num = payload.get("round_num") or payload.get("round")
            if round_num is not None:
                try:
                    sessions[s_id]["match_round_num"] = int(round_num)
                except Exception:
                    pass
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_round_started(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        runtime_id = str(payload.get("server_id") or "").strip().lower()
        logger.debug(f"WEBHOOK: round_started server={runtime_id} s_id={s_id}")
        if s_id and s_id in sessions:
            sessions[s_id]["match_round_mvp"] = None
            # Limpa pausa/halftime ao iniciar o round
            if sessions[s_id].get("match_phase") in ("halftime", "paused"):
                sessions[s_id]["match_phase"] = None
                sessions[s_id]["match_pause_team"] = None
            round_num = payload.get("round_num") or payload.get("round")
            if round_num is not None:
                try:
                    sessions[s_id]["match_round_num"] = int(round_num)
                except Exception:
                    pass
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_knife_round_started(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        logger.info(f"WEBHOOK: knife_round_started → s_id={s_id}")
        if s_id and s_id in sessions:
            sessions[s_id]["match_phase"] = "knife"
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_knife_round_ended(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        logger.info(f"WEBHOOK: knife_round_ended → s_id={s_id}")
        if s_id and s_id in sessions:
            sessions[s_id]["match_phase"] = None
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_halftime_started(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        logger.info(f"WEBHOOK: halftime_started → s_id={s_id}")
        if s_id and s_id in sessions:
            sessions[s_id]["match_phase"] = "halftime"
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_overtime_started(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        logger.info(f"WEBHOOK: overtime_started → s_id={s_id}")
        if s_id and s_id in sessions:
            sessions[s_id]["match_phase"] = "overtime"
            try:
                sessions[s_id]["match_overtime_num"] = int(sessions[s_id].get("match_overtime_num") or 0) + 1
            except Exception:
                sessions[s_id]["match_overtime_num"] = 1
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_match_paused(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        pause_team = str(payload.get("team") or payload.get("pausing_team") or "").strip()
        logger.info(f"WEBHOOK: match_paused → s_id={s_id} team={pause_team}")
        if s_id and s_id in sessions:
            sessions[s_id]["match_phase"] = "paused"
            sessions[s_id]["match_pause_team"] = pause_team or None
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_match_unpaused(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        logger.info(f"WEBHOOK: match_unpaused → s_id={s_id}")
        if s_id and s_id in sessions:
            if sessions[s_id].get("match_phase") == "paused":
                sessions[s_id]["match_phase"] = None
            sessions[s_id]["match_pause_team"] = None
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_player_round_stats(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        if not s_id or s_id not in sessions:
            return web.json_response({"ok": True}, status=200)

        # Payload may carry a list of players or a single player
        players = payload.get("players") or payload.get("stats") or []
        if isinstance(payload.get("name"), str):
            # single player format
            players = [payload]

        current_mvp = sessions[s_id].get("match_round_mvp") or {}
        best_kills = int(current_mvp.get("kills") or 0)
        best_damage = int(current_mvp.get("damage") or 0)

        for p in players:
            if not isinstance(p, dict):
                continue
            kills = int(p.get("kills") or p.get("kill") or 0)
            damage = int(p.get("damage") or p.get("dmg") or 0)
            name = str(p.get("name") or p.get("player_name") or "?")
            if kills > best_kills or (kills == best_kills and damage > best_damage):
                best_kills = kills
                best_damage = damage
                sessions[s_id]["match_round_mvp"] = {"name": name, "kills": kills, "damage": damage}

        return web.json_response({"ok": True}, status=200)

    async def _handle_series_started(self, payload: dict) -> web.Response:
        runtime_id = str(payload.get("server_id") or "").strip().lower()
        s_id = self._resolve_session_id(payload)
        if not s_id and runtime_id:
            s_id = self._runtime_to_session_id(runtime_id)
        match_id = self._payload_match_id(payload) or (sessions.get(s_id or "", {}).get("match_id"))
        logger.info(f"WEBHOOK: series_start server={runtime_id} match={match_id} s_id={s_id}")
        if s_id and sessions.get(s_id) is not None:
            if match_id and not sessions[s_id].get("match_id"):
                try:
                    sessions[s_id]["match_id"] = int(match_id)
                    sessions[s_id]["active"] = True
                except Exception:
                    pass
            # Reset phase fields on new series
            sessions[s_id]["match_phase"] = None
            sessions[s_id]["match_overtime_num"] = 0
            sessions[s_id]["match_pause_team"] = None
            sessions[s_id]["match_round_num"] = 0
            sessions[s_id]["match_round_mvp"] = None
            sessions[s_id]["status"] = "LIVE"
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_series_result(self, payload: dict) -> web.Response:
        runtime_id = str(payload.get("server_id") or "").strip().lower()
        s_id = self._resolve_session_id(payload)
        if not s_id and runtime_id:
            s_id = self._runtime_to_session_id(runtime_id)
        match_id_raw = self._payload_match_id(payload) or (sessions.get(s_id or "", {}).get("match_id"))
        logger.info(f"WEBHOOK: series_end server={runtime_id} match={match_id_raw} s_id={s_id}")
        if match_id_raw:
            try:
                match_id = int(match_id_raw)
                result = await self._finalize_match_end(
                    match_id,
                    s_id_hint=s_id,
                    trigger="webhook_series",
                    authoritative_end=True,
                )
                asyncio.create_task(self._trigger_monitor_update())
                return web.json_response(result, status=200 if result.get("ok") else 202)
            except Exception as exc:
                logger.warning(f"WEBHOOK: falha ao processar series_end match={match_id_raw}: {exc}")
        else:
            logger.warning(f"WEBHOOK: series_end sem match_id resolvido server={runtime_id} s_id={s_id}")
        asyncio.create_task(self._trigger_monitor_update())
        return web.json_response({"ok": True}, status=200)

    async def _handle_match_ended_custom(self, payload: dict) -> web.Response:
        s_id = self._resolve_session_id(payload)
        raw_match_id = self._payload_match_id(payload) or (sessions.get(s_id or "", {}).get("match_id"))
        try:
            match_id = int(raw_match_id)
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_match_id"}, status=400)

        incoming_server_id = str(payload.get("server_id") or "").strip().lower()
        if not incoming_server_id and s_id in SERVERS:
            incoming_server_id = str((SERVERS.get(s_id) or {}).get("runtime_id") or "").strip().lower()
        if not incoming_server_id and not s_id:
            return web.json_response({"ok": False, "error": "invalid_server_id"}, status=400)

        runtime_row = await get_match_runtime_server(match_id)
        if runtime_row:
            persisted_server_id = str(runtime_row.get("runtime_server_id") or "").strip().lower()
            if persisted_server_id and incoming_server_id != persisted_server_id:
                logger.warning(
                    f"WEBHOOK: server_id divergente match={match_id} incoming={incoming_server_id} persisted={persisted_server_id}"
                )

        if not s_id and incoming_server_id:
            s_id = self._runtime_to_session_id(incoming_server_id)
        result = await self._finalize_match_end(
            match_id,
            s_id_hint=s_id,
            trigger="webhook",
            authoritative_end=True,
        )
        return web.json_response(result, status=200 if result.get("ok") else 202)



    async def move_teams_after_match(self, match: Dict, server_config, s_id, db_players_stats: List[Dict]):
        try:
            guild = self.bot.guilds[0]
            t1_score = int(match.get('team1_score') or 0)
            t2_score = int(match.get('team2_score') or 0)
            team1_won = t1_score > t2_score
            
            winning_stats = []
            losing_stats = []
            team1_name = match.get("team1_name")
            team2_name = match.get("team2_name")
            for p in db_players_stats:
                if ALWAYS_ALLOW_STEAMID64 and p.get("steamid64") == ALWAYS_ALLOW_STEAMID64:
                    p_team = p.get("team")
                    if p_team not in (team1_name, team2_name):
                        continue
                p_team = p.get("team")
                if p_team not in (team1_name, team2_name):
                    continue
                m = guild.get_member(int(p['discord_id'])) if p.get('discord_id') else None
                if m:
                    if (p['team'] == match['team1_name'] and team1_won) or \
                       (p['team'] == match['team2_name'] and not team1_won):
                        winning_stats.append((m, int(p.get('damage') or 0)))
                    else:
                        losing_stats.append((m, int(p.get('damage') or 0)))

            sala_proximo = guild.get_channel(SALA_PROXIMO_ID)
            fila_cog = get_queue_cog(self.bot)

            # 1) Registra vencedores para entrada prioritaria por dano.
            if fila_cog and winning_stats:
                damage_payload = {int(member.id): int(dmg or 0) for member, dmg in winning_stats}
                await fila_cog.register_match_winners(damage_payload)
            if fila_cog and losing_stats:
                damage_payload = {int(member.id): int(dmg or 0) for member, dmg in losing_stats}
                await fila_cog.set_recent_damage_bulk(damage_payload)

            # 2) Move todos os participantes para a sala de fila.
            if sala_proximo:
                for member, _ in winning_stats:
                    if member.voice:
                        try:
                            await member.move_to(sala_proximo)
                        except Exception:
                            pass

                for member, _ in losing_stats:
                    if not member.voice or not member.voice.channel:
                        continue
                    if member.voice.channel.id == sala_proximo.id:
                        continue
                    try:
                        await member.move_to(sala_proximo)
                    except Exception:
                        pass

            if fila_cog and winning_stats:
                await fila_cog.prioritize_match_winners(winning_stats)
            if fila_cog and losing_stats:
                await fila_cog.prioritize_match_losers(losing_stats)
            elif fila_cog:
                await fila_cog.request_display_update()

        except Exception as e:
            logger.error(f"❌ Error in final movement: {e}")
        finally:
            pass

    async def post_match_summary(
        self,
        channel: discord.TextChannel,
        match_id: int,
        match: Dict,
        players: List[Dict],
        server_id: Optional[str] = None,
    ):
        if not server_id:
            try:
                server_id = await get_match_server_id(match_id)
            except Exception:
                server_id = None
        embed = self.build_match_embed(match, players, server_id=server_id)
        view = self.build_feedback_view(match_id, players)
        if view:
            msg = await channel.send(embed=embed, view=view)
            view.message = msg
        else:
            await channel.send(embed=embed)
        await update_ranks(match_id)
        await mark_match_posted(match_id)

    def build_feedback_view(self, match_id: int, players: List[Dict]):
        options = []
        seen = set()
        for p in players:
            discord_id = p.get("discord_id")
            if not discord_id:
                continue
            if discord_id in seen:
                continue
            seen.add(discord_id)
            options.append({
                "discord_id": int(discord_id),
                "name": p.get("name", "Player")
            })
        if not options:
            return None
        return MatchFeedbackView(match_id, options)

    def build_match_embed(self, match: Dict, players: List[Dict], server_id: Optional[str] = None) -> discord.Embed:
        cap1, cap2 = match.get("team1_name", "T1"), match.get("team2_name", "T2")
        s1, s2 = int(match.get("team1_score") or 0), int(match.get("team2_score") or 0)
        map_name = str(match.get('mapname', 'unknown')).lower()
        map_raw = normalize_map_key(map_name)
        t1_won = s1 > s2
        em1, em2 = ("🟢", "🔴") if t1_won else ("🔴", "🟢")
        sy1, sy2 = ("+", "-") if t1_won else ("-", "+")

        filtered_players = [
            p for p in players
            if str(p.get("team")) in (str(cap1), str(cap2))
        ]

        try: mvp = max(filtered_players, key=lambda p: int(p.get('damage') or 0))
        except: mvp = {}
        
        embed = discord.Embed(title="📋 Match Summary", color=0x2ecc71)
        if MAP_IMAGES.get(map_raw): embed.set_thumbnail(url=MAP_IMAGES[map_raw])
        embed.add_field(name="Resultado", value=f"{em1} **{cap1}** `{s1} — {s2}` **{cap2}** {em2}", inline=False)
        embed.add_field(name="👑 MVP", value=f"**{mvp.get('name', 'Unknown')}** ({int(mvp.get('damage') or 0)} dmg)", inline=False)

        header = " PLAYER     RANK  K  D  A  DMG HS 5 4 3 2\n"
        h_t1, h_t2 = f"```diff\n{sy1}{header}", f"```diff\n{sy2}{header}"
        st1, st2 = "", ""

        for p in filtered_players:
            n = str(p.get('name', 'P'))[:11] 
            r, k, d, a = int(p.get('rating') or 0), int(p.get('kills') or 0), int(p.get('deaths') or 0), int(p.get('assists') or 0)
            dmg, hs = int(p.get('damage') or 0), int(p.get('f_hs') or 0)
            k5, k4, k3, k2 = int(p.get('f_5k') or 0), int(p.get('f_4k') or 0), int(p.get('f_3k') or 0), int(p.get('f_2k') or 0)
            line = f" {n:<11} {r:>4} {k:>2} {d:>2} {a:>2} {dmg:>4} {hs:>2} {k5} {k4} {k3} {k2}\n"
            if str(p.get('team')) == str(cap1): st1 += line
            else: st2 += line

        if st1: embed.add_field(name=f"{em1} {cap1}", value=f"{h_t1}{st1}```", inline=False)
        if st2: embed.add_field(name=f"{em2} {cap2}", value=f"{h_t2}{st2}```", inline=False)
        link_map = map_name
        if not link_map.startswith("de_") and "/" not in link_map:
            link_map = f"de_{link_map}"
        demo_base = DEMO_DOWNLOAD_URL or ""
        link = f"{demo_base}/match_{match.get('matchid')}_{link_map}.dem.gz"
        embed.add_field(name="📥 Demo Download", value=f"[Click here]({link})", inline=False)
        server_name = None
        if server_id and server_id in SERVERS:
            server_name = SERVERS[server_id].get("name") or server_id.upper()
        elif server_id:
            server_name = server_id.upper()
        footer = f"MatchID #{match.get('matchid')}"
        if server_name:
            footer = f"{footer} | {server_name}"
        embed.set_footer(text=footer)
        return embed

    async def update_ranking_channel(self):
        channel = self.bot.get_channel(CANAL_RANKING_ID)
        if not channel: return
        try:
            top_players = await get_top_ranking(100)
            if not top_players: return
            guild = self.bot.guilds[0]
            embed = build_ranking_embed(
                top_players,
                guild,
                "🏆 CURRENT SEASON RANKING",
                "⭐**LEADERS (TOP 3)**",
                "📊**STANDINGS**",
                "https://media.discordapp.net/attachments/1452985230565834804/1466928923702071339/LogoMixLeve.png"
            )
            await channel.purge(limit=5)
            await channel.send(embed=embed)
        except Exception as e:         logger.error(f"❌ Ranking error: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(MatchesCog(bot))
