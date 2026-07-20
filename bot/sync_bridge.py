"""
Sync Bridge — Bot Side
======================

Responsible for synchronizing data from the bot's legacy database (bot DB)
to the ProjectMix Webapp database (projectmix DB) after ranking events.

Covered flows:
  - After update_ranks(match_id): copies matchzy_stats_* and triggers reconcile in Webapp
  - After register_player(): mirrors the new user in Webapp

Security:
  - All queries are parameterized; no external data interpolation.
  - discord_id and steamid64 are validated before any use.
  - Errors never propagate to the bot's main operation.
  - If WEBAPP_DB_* is not configured, all functions silently return.

Idempotency:
  - Uses the `sync_bridge_applied_matches` table in the webapp database to ensure
    each match is only processed once.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiomysql

from .config import WEBAPP_DB_CONFIG

logger = logging.getLogger(__name__)

# Namespace para match IDs do bot no banco projectmix — mesmo offset do webapp
BOT_MATCH_ID_OFFSET = 2_000_000_000
SYNC_MATCH_RETRY_DELAYS_SEC = (0, 2, 5, 10)

_PLAYER_STATS_COLUMNS = (
    "matchid",
    "mapnumber",
    "steamid64",
    "team",
    "name",
    "kills",
    "deaths",
    "damage",
    "assists",
    "enemy5ks",
    "enemy4ks",
    "enemy3ks",
    "enemy2ks",
    "utility_count",
    "utility_damage",
    "utility_successes",
    "utility_enemies",
    "flash_count",
    "flash_successes",
    "health_points_removed_total",
    "health_points_dealt_total",
    "shots_fired_total",
    "shots_on_target_total",
    "v1_count",
    "v1_wins",
    "v2_count",
    "v2_wins",
    "entry_count",
    "entry_wins",
    "equipment_value",
    "money_saved",
    "kill_reward",
    "live_time",
    "head_shot_kills",
    "cash_earned",
    "enemies_flashed",
)

_PLAYER_STATS_INSERT_SQL = """
    INSERT INTO matchzy_stats_players
        (matchid, mapnumber, steamid64, team, name,
         kills, deaths, damage, assists,
         enemy5ks, enemy4ks, enemy3ks, enemy2ks,
         utility_count, utility_damage, utility_successes, utility_enemies,
         flash_count, flash_successes, health_points_removed_total,
         health_points_dealt_total, shots_fired_total, shots_on_target_total,
         v1_count, v1_wins, v2_count, v2_wins, entry_count,
         entry_wins, equipment_value, money_saved, kill_reward, live_time,
         head_shot_kills, cash_earned, enemies_flashed)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        team = VALUES(team),
        name = VALUES(name),
        kills = VALUES(kills),
        deaths = VALUES(deaths),
        damage = VALUES(damage),
        assists = VALUES(assists),
        enemy5ks = VALUES(enemy5ks),
        enemy4ks = VALUES(enemy4ks),
        enemy3ks = VALUES(enemy3ks),
        enemy2ks = VALUES(enemy2ks),
        utility_count = VALUES(utility_count),
        utility_damage = VALUES(utility_damage),
        utility_successes = VALUES(utility_successes),
        utility_enemies = VALUES(utility_enemies),
        flash_count = VALUES(flash_count),
        flash_successes = VALUES(flash_successes),
        health_points_removed_total = VALUES(health_points_removed_total),
        health_points_dealt_total = VALUES(health_points_dealt_total),
        shots_fired_total = VALUES(shots_fired_total),
        shots_on_target_total = VALUES(shots_on_target_total),
        v1_count = VALUES(v1_count),
        v1_wins = VALUES(v1_wins),
        v2_count = VALUES(v2_count),
        v2_wins = VALUES(v2_wins),
        entry_count = VALUES(entry_count),
        entry_wins = VALUES(entry_wins),
        equipment_value = VALUES(equipment_value),
        money_saved = VALUES(money_saved),
        kill_reward = VALUES(kill_reward),
        live_time = VALUES(live_time),
        head_shot_kills = VALUES(head_shot_kills),
        cash_earned = VALUES(cash_earned),
        enemies_flashed = VALUES(enemies_flashed)
"""

_webapp_pool: Optional[aiomysql.Pool] = None
_pool_lock = asyncio.Lock()


def _has_webapp_config() -> bool:
    return bool(
        WEBAPP_DB_CONFIG.get("user")
        and WEBAPP_DB_CONFIG.get("password")
        and WEBAPP_DB_CONFIG.get("database")
    )


async def _get_webapp_pool() -> Optional[aiomysql.Pool]:
    """Gets (or creates) the webapp database connection pool. Thread-safe."""
    global _webapp_pool
    if _webapp_pool is not None and not getattr(_webapp_pool, "closed", False):
        return _webapp_pool

    if not _has_webapp_config():
        return None

    async with _pool_lock:
        # Double check after lock
        if _webapp_pool is not None and not getattr(_webapp_pool, "closed", False):
            return _webapp_pool
        try:
            _webapp_pool = await aiomysql.create_pool(
                host=str(WEBAPP_DB_CONFIG.get("host") or "127.0.0.1"),
                port=int(WEBAPP_DB_CONFIG.get("port") or 3306),
                user=str(WEBAPP_DB_CONFIG["user"]),
                password=str(WEBAPP_DB_CONFIG["password"]),
                db=str(WEBAPP_DB_CONFIG["database"]),
                charset="utf8mb4",
                use_unicode=True,
                autocommit=True,
                minsize=1,
                maxsize=5,
                pool_recycle=3600,
            )
            logger.info(
                "sync_bridge(bot): webapp pool connected at %s/%s",
                WEBAPP_DB_CONFIG.get("host"),
                WEBAPP_DB_CONFIG.get("database"),
            )
        except Exception as exc:
            logger.error("sync_bridge(bot): failed to connect to webapp database: %s", exc)
            _webapp_pool = None

    return _webapp_pool


async def close_webapp_pool() -> None:
    """Closes the webapp database connection pool (call on bot shutdown)."""
    global _webapp_pool
    if _webapp_pool is not None:
        _webapp_pool.close()
        await _webapp_pool.wait_closed()
        _webapp_pool = None
        logger.info("sync_bridge(bot): webapp pool closed")


# ------------------------------------------------------------------
# Idempotency
# ------------------------------------------------------------------

def _player_stats_insert_values(matchid: int, mapnumber: int, player: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        matchid,
        mapnumber,
        str(player.get("steamid64") or "").strip(),
        str(player.get("team") or "")[:64],
        str(player.get("name") or "")[:128],
        int(player.get("kills") or 0),
        int(player.get("deaths") or 0),
        int(player.get("damage") or 0),
        int(player.get("assists") or 0),
        int(player.get("enemy5ks") or 0),
        int(player.get("enemy4ks") or 0),
        int(player.get("enemy3ks") or 0),
        int(player.get("enemy2ks") or 0),
        int(player.get("utility_count") or 0),
        int(player.get("utility_damage") or 0),
        int(player.get("utility_successes") or 0),
        int(player.get("utility_enemies") or 0),
        int(player.get("flash_count") or 0),
        int(player.get("flash_successes") or 0),
        int(player.get("health_points_removed_total") or 0),
        int(player.get("health_points_dealt_total") or 0),
        int(player.get("shots_fired_total") or 0),
        int(player.get("shots_on_target_total") or 0),
        int(player.get("v1_count") or 0),
        int(player.get("v1_wins") or 0),
        int(player.get("v2_count") or 0),
        int(player.get("v2_wins") or 0),
        int(player.get("entry_count") or 0),
        int(player.get("entry_wins") or 0),
        int(player.get("equipment_value") or 0),
        int(player.get("money_saved") or 0),
        int(player.get("kill_reward") or 0),
        int(player.get("live_time") or 0),
        int(player.get("head_shot_kills") or 0),
        int(player.get("cash_earned") or 0),
        int(player.get("enemies_flashed") or 0),
    )


async def _is_match_already_applied(bot_matchid: int) -> bool:
    """Checks if this match has already been processed by the bot in the webapp database."""
    namespaced = BOT_MATCH_ID_OFFSET + int(bot_matchid)
    pool = await _get_webapp_pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT 1 FROM sync_bridge_applied_matches
                    WHERE matchzy_match_id = %s AND applied_by = 'bot'
                    LIMIT 1
                    """,
                    (namespaced,),
                )
                return await cur.fetchone() is not None
    except Exception as exc:
        logger.error("sync_bridge(bot): idempotency error match=%s: %s", bot_matchid, exc)
        return False


async def _mark_match_applied(bot_matchid: int) -> None:
    namespaced = BOT_MATCH_ID_OFFSET + int(bot_matchid)
    pool = await _get_webapp_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT IGNORE INTO sync_bridge_applied_matches
                        (matchzy_match_id, applied_by, applied_at)
                    VALUES (%s, 'bot', %s)
                    """,
                    (namespaced, datetime.now(timezone.utc).replace(tzinfo=None)),
                )
    except Exception as exc:
        logger.error(
            "sync_bridge(bot): error registering applied match=%s: %s", bot_matchid, exc
        )


# ------------------------------------------------------------------
# Copy Bot match stats → projectmix
# ------------------------------------------------------------------

async def _copy_match_stats(
    bot_matchid: int,
    match_row: Dict[str, Any],
    maps_rows: List[Dict[str, Any]],
    players_rows: List[Dict[str, Any]],
) -> bool:
    """
    Copies the stats of a match from the bot database to the webapp database (projectmix),
    using matchid = BOT_MATCH_ID_OFFSET + bot_matchid.
    """
    namespaced = BOT_MATCH_ID_OFFSET + int(bot_matchid)
    pool = await _get_webapp_pool()
    if pool is None:
        return False

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 1. matchzy_stats_matches
                await cur.execute(
                    """
                    INSERT INTO matchzy_stats_matches
                        (matchid, start_time, end_time, winner, series_type,
                         team1_name, team1_score, team2_name, team2_score, server_ip)
                    VALUES (%s, %s, NOW(), %s, 'bo1', %s, %s, %s, %s, 'bot-legacy')
                    ON DUPLICATE KEY UPDATE
                        winner      = VALUES(winner),
                        team1_score = VALUES(team1_score),
                        team2_score = VALUES(team2_score),
                        end_time    = VALUES(end_time)
                    """,
                    (
                        namespaced,
                        match_row.get("start_time"),
                        str(match_row.get("winner") or match_row.get("team1_name", ""))[:64],
                        str(match_row.get("team1_name") or "")[:100],
                        int(match_row.get("team1_score") or 0),
                        str(match_row.get("team2_name") or "")[:100],
                        int(match_row.get("team2_score") or 0),
                    ),
                )

                # 2. matchzy_stats_maps
                for mp in maps_rows:
                    mapnum = int(mp.get("mapnumber") or 1)
                    t1 = int(mp.get("team1_score") or 0)
                    t2 = int(mp.get("team2_score") or 0)
                    winner = str(mp.get("winner") or (
                        match_row.get("team1_name") if t1 > t2
                        else match_row.get("team2_name") or ""
                    ))[:64]
                    await cur.execute(
                        """
                        INSERT INTO matchzy_stats_maps
                            (matchid, mapnumber, start_time, end_time, winner,
                             mapname, team1_score, team2_score)
                        VALUES (%s, %s, NOW(), NOW(), %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            winner      = VALUES(winner),
                            team1_score = VALUES(team1_score),
                            team2_score = VALUES(team2_score)
                        """,
                        (
                            namespaced,
                            mapnum,
                            winner,
                            str(mp.get("mapname") or "")[:64],
                            t1,
                            t2,
                        ),
                    )

                # 3. matchzy_stats_players
                for p in players_rows:
                    sid = str(p.get("steamid64") or "").strip()
                    if not sid:
                        continue
                    mapnum = int(p.get("mapnumber") or 1)
                    await cur.execute(
                        _PLAYER_STATS_INSERT_SQL,
                        _player_stats_insert_values(namespaced, mapnum, p),
                    )

        logger.info(
            "sync_bridge(bot): stats copied bot_matchid=%s → namespaced=%s (%d players)",
            bot_matchid, namespaced, len(players_rows),
        )
        return True

    except Exception as exc:
        logger.error(
            "sync_bridge(bot): failed to copy stats match=%s: %s", bot_matchid, exc
        )
        return False


async def _load_match_bundle(
    db: Any,
    match_id: int,
) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]], List[Dict[str, Any]]]:
    mid = int(match_id or 0)
    if mid <= 0:
        return None, [], []

    match_row = await db.fetchone(
        """
        SELECT m.matchid, m.team1_name, m.team2_name,
               m.start_time, m.winner,
               COALESCE(mp.team1_score, m.team1_score, 0) AS team1_score,
               COALESCE(mp.team2_score, m.team2_score, 0) AS team2_score
        FROM matchzy_stats_matches m
        LEFT JOIN matchzy_stats_maps mp
            ON mp.matchid = m.matchid
            AND mp.mapnumber = (
                SELECT MAX(mp2.mapnumber)
                FROM matchzy_stats_maps mp2
                WHERE mp2.matchid = m.matchid
            )
        WHERE m.matchid = %s
        LIMIT 1
        """,
        (mid,),
    )
    maps_rows = await db.fetchall(
        """
        SELECT mapnumber, mapname, team1_score, team2_score, winner
        FROM matchzy_stats_maps
        WHERE matchid = %s
        ORDER BY mapnumber ASC
        """,
        (mid,),
    )
    players_rows = await db.fetchall(
        """
        SELECT mp.mapnumber, mp.steamid64, mp.team, mp.name,
               mp.kills, mp.deaths, mp.damage, mp.assists,
               mp.enemy5ks, mp.enemy4ks, mp.enemy3ks, mp.enemy2ks,
               mp.utility_count, mp.utility_damage, mp.utility_successes, mp.utility_enemies,
               mp.flash_count, mp.flash_successes, mp.head_shot_kills, mp.cash_earned
        FROM matchzy_stats_players mp
        WHERE mp.matchid = %s
        """,
        (mid,),
    )
    return match_row, maps_rows or [], players_rows or []


async def _load_match_bundle_with_retry(
    db: Any,
    match_id: int,
) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]], List[Dict[str, Any]]]:
    mid = int(match_id or 0)
    last_match_row: Dict[str, Any] | None = None
    last_maps_rows: List[Dict[str, Any]] = []
    last_players_rows: List[Dict[str, Any]] = []

    for attempt, delay in enumerate(SYNC_MATCH_RETRY_DELAYS_SEC, start=1):
        if delay > 0:
            await asyncio.sleep(delay)

        match_row, maps_rows, players_rows = await _load_match_bundle(db, mid)
        last_match_row, last_maps_rows, last_players_rows = match_row, maps_rows, players_rows

        if match_row and players_rows:
            if attempt > 1:
                logger.info(
                    "sync_bridge(bot): stats became ready after retry match_id=%s attempt=%s players=%s",
                    mid,
                    attempt,
                    len(players_rows),
                )
            return match_row, maps_rows, players_rows

        logger.warning(
            "sync_bridge(bot): waiting for complete bundle match_id=%s attempt=%s has_match=%s players=%s",
            mid,
            attempt,
            bool(match_row),
            len(players_rows or []),
        )

    return last_match_row, last_maps_rows, last_players_rows


def _ensure_maps_snapshot(
    match_row: Dict[str, Any] | None,
    maps_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if maps_rows:
        return list(maps_rows)
    row = dict(match_row or {})
    if not row:
        return []
    return [
        {
            "mapnumber": 1,
            "mapname": "",
            "team1_score": int(row.get("team1_score") or 0),
            "team2_score": int(row.get("team2_score") or 0),
            "winner": str(row.get("winner") or "")[:64],
        }
    ]


# ------------------------------------------------------------------
# Upsert de ranking no banco webapp
# ------------------------------------------------------------------

async def _upsert_ranking_in_webapp(
    *,
    discord_id: int,
    steamid64: str,
    nickname: str,
    rating: int,
    wins: int,
    losses: int,
    total_matches: int,
    win_streak: int,
) -> bool:
    """
    Writes the final ranking state of a player to the webapp database (projectmix).
    Used when the Webapp cannot reconcile via matchzy_stats (e.g., player without User).
    """
    pool = await _get_webapp_pool()
    if pool is None:
        return False

    sid = str(steamid64 or "").strip()
    did = int(discord_id or 0)
    if not sid and did <= 0:
        return False

    nick = str(nickname or "").strip()[:100] or "?"
    rat = max(0, int(rating))
    w = max(0, int(wins))
    l = max(0, int(losses))
    tm = max(0, int(total_matches))
    ws = max(0, int(win_streak))

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Upsert in users (if not already exists)
                if did > 0:
                    await cur.execute(
                        """
                        INSERT INTO users
                            (discord_id, steamid64, nickname,
                             onboarding_completed, roles, created_at)
                        VALUES (%s, %s, %s, 0, '["player"]', NOW())
                        ON DUPLICATE KEY UPDATE
                            steamid64 = IF(
                                steamid64 IS NULL OR steamid64 = '',
                                VALUES(steamid64), steamid64
                            ),
                            nickname = COALESCE(NULLIF(nickname, ''), VALUES(nickname))
                        """,
                        (did, sid or None, nick),
                    )

                # Upsert em ranking (webapp)
                if sid:
                    await cur.execute(
                        """
                        INSERT INTO ranking
                            (steamid64, discord_id, nickname, rating,
                             total_matches, wins, losses, win_streak, last_rank_source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'bot_sync')
                        ON DUPLICATE KEY UPDATE
                            discord_id    = COALESCE(discord_id, VALUES(discord_id)),
                            nickname      = COALESCE(NULLIF(VALUES(nickname), ''), nickname),
                            rating        = VALUES(rating),
                            total_matches = VALUES(total_matches),
                            wins          = VALUES(wins),
                            losses        = VALUES(losses),
                            win_streak    = VALUES(win_streak),
                            last_rank_source = 'bot_sync',
                            updated_at    = CURRENT_TIMESTAMP
                        """,
                        (sid, did or None, nick, rat, tm, w, l, ws),
                    )

                # Upsert em player_stats
                if did > 0:
                    await cur.execute(
                        """
                        INSERT INTO player_stats
                            (discord_id, steamid64, rating, wins, losses,
                             total_matches, win_streak)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            steamid64     = COALESCE(NULLIF(VALUES(steamid64), ''),
                                                     player_stats.steamid64),
                            rating        = VALUES(rating),
                            wins          = VALUES(wins),
                            losses        = VALUES(losses),
                            total_matches = VALUES(total_matches),
                            win_streak    = VALUES(win_streak)
                        """,
                        (did, sid or None, rat, w, l, tm, ws),
                    )

        return True

    except Exception as exc:
        logger.error(
            "sync_bridge(bot): failed upsert_ranking discord_id=%s steamid64=%s: %s",
            discord_id, steamid64, exc,
        )
        return False


# ------------------------------------------------------------------
    # Public API — called after update_ranks()
# ------------------------------------------------------------------

async def sync_match_result(match_id: int, db: Any) -> None:
    """
    Synchronizes the result of a bot match to the webapp database.

    Must be called AFTER update_ranks(match_id) completes successfully.

    Flow:
      1. Checks idempotency (match already synced?)
      2. Waits for the stats bundle to materialize in the legacy database
      3. Copies matchzy_stats_* to the webapp database (namespace BOT_MATCH_ID_OFFSET)
      4. Does direct upsert of ranking/player_stats for each player
      5. Marks match as applied only if everything completes without failures

    `db`: instancia Database do bot (bot.database.db)
    """
    if not _has_webapp_config():
        return

    mid = int(match_id or 0)
    if mid <= 0:
        return

    try:
        if await _is_match_already_applied(mid):
            logger.debug("sync_bridge(bot): match=%s already applied, skip", mid)
            return

        match_row, maps_rows, players_rows = await _load_match_bundle_with_retry(db, mid)
        if not match_row:
            logger.warning("sync_bridge(bot): match_row not found match_id=%s", mid)
            return
        if not players_rows:
            logger.warning(
                "sync_bridge(bot): incomplete bundle, no players match_id=%s", mid
            )
            return

        maps_payload = _ensure_maps_snapshot(match_row, maps_rows or [])
        copied = await _copy_match_stats(mid, match_row, maps_payload, players_rows)
        if not copied:
            logger.error(
                "sync_bridge(bot): failed to copy stats match_id=%s, will not mark applied",
                mid,
            )
            return

        ranking_upserts = 0
        ranking_failures = 0
        seen_steamids: set[str] = set()

        for p in players_rows:
            sid = str(p.get("steamid64") or "").strip()
            if not sid or sid in seen_steamids:
                continue
            seen_steamids.add(sid)

            player_row = await db.fetchone(
                """
                SELECT pl.id, pl.discord_id, pl.nickname,
                       pl.total_matches, pl.wins, pl.losses, pl.win_streak,
                       COALESCE(r.rating, 1000) AS rating
                FROM players pl
                LEFT JOIN ranking r ON r.id = pl.id
                WHERE pl.steamid64 = %s
                LIMIT 1
                """,
                (sid,),
            )
            if not player_row:
                ranking_failures += 1
                logger.warning(
                    "sync_bridge(bot): missing player snapshot match_id=%s steamid64=%s",
                    mid,
                    sid,
                )
                continue

            ok = await _upsert_ranking_in_webapp(
                discord_id=int(player_row.get("discord_id") or 0),
                steamid64=sid,
                nickname=str(player_row.get("nickname") or "").strip(),
                rating=int(player_row.get("rating") or 1000),
                wins=int(player_row.get("wins") or 0),
                losses=int(player_row.get("losses") or 0),
                total_matches=int(player_row.get("total_matches") or 0),
                win_streak=int(player_row.get("win_streak") or 0),
            )
            if not ok:
                ranking_failures += 1
                logger.error(
                    "sync_bridge(bot): failed upsert ranking match_id=%s steamid64=%s",
                    mid,
                    sid,
                )
                continue

            ranking_upserts += 1

        if ranking_failures > 0:
            logger.error(
                "sync_bridge(bot): sync parcial detectada match_id=%s upserts_ok=%s failures=%s; match nao marcado como aplicado",
                mid,
                ranking_upserts,
                ranking_failures,
            )
            return

        await _mark_match_applied(mid)
        logger.info(
            "sync_bridge(bot): match_id=%s synced successfully players=%s",
            mid,
            ranking_upserts,
        )

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(
            "sync_bridge(bot): error syncing match_id=%s: %s\n%s",
            match_id, exc, tb,
        )


async def sync_new_player(
    *,
    discord_id: int,
    steamid64: str,
    nickname: str,
) -> None:
    """
    Mirrors a new player registered in the bot to the webapp database.
    Called after register_player() completes successfully.
    """
    if not _has_webapp_config():
        return

    did = int(discord_id or 0)
    sid = str(steamid64 or "").strip()
    if did <= 0 or not sid:
        return

    try:
        await _upsert_ranking_in_webapp(
            discord_id=did,
            steamid64=sid,
            nickname=nickname,
            rating=1000,
            wins=0,
            losses=0,
            total_matches=0,
            win_streak=0,
        )
        logger.info(
            "sync_bridge(bot): new player mirrored discord_id=%s steamid64=%s",
            did, sid,
        )
    except Exception as exc:
        logger.error(
            "sync_bridge(bot): failed to mirror player discord_id=%s: %s", did, exc
        )
