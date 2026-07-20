import aiomysql
import time
import asyncio
from datetime import datetime, timedelta
from loguru import logger
from typing import Optional, Dict, List
from bot.config import DB_CONFIG

MIX_MATCH_ID_OUTLIER_FLOOR = 1_000_000_000

class Database:
    """Gerenciador de conex?es MySQL com pool"""

    def __init__(self):
        self.pool: Optional[aiomysql.Pool] = None
        self._conn_lock = asyncio.Lock()
        self._named_lock_guard = asyncio.Lock()
        self._named_lock_conns: Dict[str, aiomysql.Connection] = {}

    async def _release_named_lock_conns(self):
        async with self._named_lock_guard:
            held_items = list(self._named_lock_conns.items())
            self._named_lock_conns.clear()
        for lock_name, conn in held_items:
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute("DO RELEASE_LOCK(%s)", (str(lock_name),))
            except Exception:
                pass
            try:
                if self.pool and not self._pool_unusable():
                    self.pool.release(conn)
                else:
                    conn.close()
            except Exception:
                pass

    def _pool_unusable(self) -> bool:
        if not self.pool:
            return True
        if getattr(self.pool, "closed", False):
            return True
        if getattr(self.pool, "_closing", False):
            return True
        return False

    async def connect(self):
        """Cria o connection pool"""
        async with self._conn_lock:
            if not self._pool_unusable():
                return
            try:
                self.pool = await aiomysql.create_pool(
                    host=DB_CONFIG['host'],
                    port=DB_CONFIG['port'],
                    user=DB_CONFIG['user'],
                    password=DB_CONFIG['password'],
                    db=DB_CONFIG['database'],
                    charset="utf8mb4",
                    use_unicode=True,
                    autocommit=True,
                    pool_recycle=3600,
                    minsize=2,
                    maxsize=10,
                )
                logger.info("\u2705 Connection pool MySQL criado com sucesso")
                await ensure_tables()
            except Exception as e:
                logger.error(f"\u274C Erro ao conectar ao MySQL: {e}")
                raise

    async def close(self):
        async with self._conn_lock:
            if self.pool:
                await self._release_named_lock_conns()
                self.pool.close()
                await self.pool.wait_closed()
                self.pool = None
                logger.info("\U0001F6D1 Connection pool MySQL fechado")

    async def _reconnect(self):
        async with self._conn_lock:
            try:
                if self.pool:
                    await self._release_named_lock_conns()
                    self.pool.close()
                    await self.pool.wait_closed()
            except Exception:
                pass
            self.pool = None
        await self.connect()

    async def _run_with_retry(self, coro_fn):
        if self._pool_unusable():
            await self.connect()

        last_err = None
        for _ in range(2):
            try:
                return await coro_fn()
            except (aiomysql.OperationalError, aiomysql.InterfaceError, ConnectionResetError, RuntimeError) as e:
                if isinstance(e, RuntimeError):
                    msg = str(e).lower()
                    if "closing pool" not in msg and "cannot acquire connection" not in msg:
                        raise
                last_err = e
                await self._reconnect()

        if last_err:
            raise last_err

    async def execute(self, query: str, params: tuple = None) -> int:
        async def _do():
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(query, params or ())
                    return cursor.rowcount

        return await self._run_with_retry(_do)

    async def insert_and_get_id(self, query: str, params: tuple = None) -> int:
        async def _do():
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(query, params or ())
                    return cursor.lastrowid

        return await self._run_with_retry(_do)

    async def fetchone(self, query: str, params: tuple = None) -> Optional[Dict]:
        async def _do():
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(query, params or ())
                    return await cursor.fetchone()

        return await self._run_with_retry(_do)

    async def fetchall(self, query: str, params: tuple = None) -> List[Dict]:
        async def _do():
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(query, params or ())
                    return await cursor.fetchall()

        return await self._run_with_retry(_do)

# Instância global
db = Database()

# ================= FUNÇÕES DE FILA =================

async def save_queue_member(
    discord_id: int,
    joined_at: Optional[datetime] = None,
    damage: int = 0,
    priority_awarded_at: Optional[datetime] = None,
):
    """Salva/atualiza um jogador na fila com timestamp, dano e carimbo da prioridade de vencedor."""
    joined_value = joined_at or datetime.utcnow()
    dmg_value = int(damage or 0)
    priority_value = priority_awarded_at.replace(tzinfo=None) if priority_awarded_at else None
    query = """
        INSERT INTO waiting_queue (discord_id, joined_at, last_damage, priority_awarded_at)
        VALUES (%s, %s, %s, %s) AS new_values
        ON DUPLICATE KEY UPDATE
            joined_at = new_values.joined_at,
            last_damage = new_values.last_damage,
            priority_awarded_at = new_values.priority_awarded_at
    """
    await db.execute(query, (int(discord_id), joined_value.replace(tzinfo=None), dmg_value, priority_value))

async def remove_queue_member(discord_id: int):
    """Remove um jogador da fila"""
    query = "DELETE FROM waiting_queue WHERE discord_id = %s"
    await db.execute(query, (int(discord_id),))


async def set_queue_member_damage(discord_id: int, damage: int):
    query = "UPDATE waiting_queue SET last_damage = %s WHERE discord_id = %s"
    await db.execute(query, (int(damage or 0), int(discord_id)))

async def get_saved_queue():
    """Busca a fila salva ordenada por tempo"""
    query = """
        SELECT
            discord_id,
            joined_at,
            COALESCE(last_damage, 0) AS last_damage,
            priority_awarded_at
        FROM waiting_queue
        ORDER BY joined_at ASC
    """
    return await db.fetchall(query)

# ================= FUNCOES DE TIMES (TORNEIO) =================

async def upsert_tournament_team_player(
    team_name: str,
    player_name: str,
    steamid64: str,
    discord_id: int,
    is_captain: bool = False,
):
    query = """
        INSERT INTO tournament_teams (name, players, steamid, discord_id, is_captain)
        VALUES (%s, %s, %s, %s, %s) AS new_values
        ON DUPLICATE KEY UPDATE
            players = new_values.players,
            steamid = new_values.steamid,
            is_captain = new_values.is_captain
    """
    await db.execute(query, (team_name, player_name, steamid64, discord_id, 1 if is_captain else 0))


async def set_tournament_team_group(team_name: str, group_name: str):
    grp = (group_name or "").strip().upper()
    if grp not in ("A", "B"):
        raise ValueError("group_name deve ser A ou B")
    query = """
        UPDATE tournament_teams
        SET group_name = %s
        WHERE name = %s
    """
    await db.execute(query, (grp, team_name))


async def get_tournament_team_group(team_name: str) -> Optional[str]:
    query = """
        SELECT group_name
        FROM tournament_teams
        WHERE name = %s
          AND group_name IS NOT NULL
          AND group_name <> ''
        LIMIT 1
    """
    row = await db.fetchone(query, (team_name,))
    if not row:
        return None
    grp = str(row.get("group_name") or "").strip().upper()
    return grp if grp in ("A", "B") else None


async def list_tournament_teams_by_group(group_name: str) -> List[str]:
    grp = (group_name or "").strip().upper()
    if grp not in ("A", "B"):
        return []
    query = """
        SELECT DISTINCT name
        FROM tournament_teams
        WHERE group_name = %s
        ORDER BY name ASC
    """
    rows = await db.fetchall(query, (grp,))
    return [str(r.get("name")) for r in rows if r.get("name")]


async def get_finished_tournament_match_rows() -> List[Dict]:
    query = """
        SELECT
            tm.matchid,
            tm.mode,
            tm.series,
            tm.team1,
            tm.team2,
            COALESCE(m.team1_score, tm.team1_score) AS score1,
            COALESCE(m.team2_score, tm.team2_score) AS score2,
            COALESCE(NULLIF(m.winner, ''), tm.winner) AS winner,
            COALESCE(ma.round_score1, 0) AS round_score1,
            COALESCE(ma.round_score2, 0) AS round_score2,
            COALESCE(ma.max_loser_rounds, 0) AS max_loser_rounds
        FROM tournament_matches tm
        LEFT JOIN matchzy_stats_matches m
          ON CAST(tm.matchid AS UNSIGNED) = m.matchid
        LEFT JOIN (
            SELECT
                mp.matchid,
                SUM(COALESCE(mp.team1_score, 0)) AS round_score1,
                SUM(COALESCE(mp.team2_score, 0)) AS round_score2,
                MAX(LEAST(COALESCE(mp.team1_score, 0), COALESCE(mp.team2_score, 0))) AS max_loser_rounds
            FROM matchzy_stats_maps mp
            GROUP BY mp.matchid
        ) ma
          ON CAST(tm.matchid AS UNSIGNED) = ma.matchid
        WHERE COALESCE(m.team1_score, tm.team1_score) IS NOT NULL
          AND COALESCE(m.team2_score, tm.team2_score) IS NOT NULL
        ORDER BY tm.created_at ASC, tm.id ASC
    """
    return await db.fetchall(query)


async def get_tournament_match_by_id(matchid: str) -> Optional[Dict]:
    query = """
        SELECT
            id, matchid, mode, series, team1, team2,
            winner, team1_score, team2_score, result_type
        FROM tournament_matches
        WHERE matchid = %s
        LIMIT 1
    """
    return await db.fetchone(query, (str(matchid),))


async def set_tournament_match_result(
    matchid: str,
    winner: str,
    team1_score: int,
    team2_score: int,
    result_type: str = "WO",
) -> int:
    query = """
        UPDATE tournament_matches
        SET winner = %s,
            team1_score = %s,
            team2_score = %s,
            result_type = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE matchid = %s
    """
    return await db.execute(
        query,
        (winner, int(team1_score), int(team2_score), str(result_type), str(matchid)),
    )


async def get_tournament_team_players(team_name: str) -> List[Dict]:
    query = """
        SELECT id, name, players, steamid, discord_id, is_captain, group_name
        FROM tournament_teams
        WHERE name = %s
        ORDER BY id ASC
    """
    return await db.fetchall(query, (team_name,))


async def get_tournament_team_captain(team_name: str) -> Optional[Dict]:
    query = """
        SELECT id, name, players, steamid, discord_id, is_captain, group_name
        FROM tournament_teams
        WHERE name = %s AND is_captain = 1
        LIMIT 1
    """
    return await db.fetchone(query, (team_name,))


async def list_tournament_team_names() -> List[str]:
    query = "SELECT DISTINCT name FROM tournament_teams ORDER BY name ASC"
    rows = await db.fetchall(query)
    return [r["name"] for r in rows if r.get("name")]

async def get_tournament_team_name_by_discord(discord_id: int) -> Optional[str]:
    query = """
        SELECT name
        FROM tournament_teams
        WHERE discord_id = %s
        ORDER BY id DESC
        LIMIT 1
    """
    row = await db.fetchone(query, (discord_id,))
    return row.get("name") if row else None

async def get_tournament_wl_by_steamid(steamid64: str, start_date: Optional[str] = None) -> Dict:
    query = """
        SELECT
            COALESCE(SUM(CASE WHEN CAST(m.winner AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_unicode_ci =
                                   CAST(pt.team AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_unicode_ci
                              THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN CAST(m.winner AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_unicode_ci <>
                                   CAST(pt.team AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_unicode_ci
                              THEN 1 ELSE 0 END), 0) AS losses
        FROM (
            SELECT p.matchid, MAX(p.team) AS team
            FROM matchzy_stats_players p
            JOIN tournament_matches tm ON CAST(tm.matchid AS UNSIGNED) = p.matchid
            WHERE p.steamid64 = %s
            GROUP BY p.matchid
        ) pt
        JOIN matchzy_stats_matches m ON m.matchid = pt.matchid
        WHERE m.winner IS NOT NULL AND m.winner <> ''
    """
    params: List = [steamid64]
    if start_date:
        query += """
          AND DATE(COALESCE(m.end_time, m.start_time)) >= %s
        """
        params.append(str(start_date))
    row = await db.fetchone(query, tuple(params))
    return {
        "wins": int((row or {}).get("wins") or 0),
        "losses": int((row or {}).get("losses") or 0),
    }


async def remove_tournament_team_player(team_name: str, discord_id: int) -> int:
    query = """
        DELETE FROM tournament_teams
        WHERE name = %s AND discord_id = %s
    """
    return await db.execute(query, (team_name, discord_id))


async def upsert_tournament_match(matchid: str, mode: str, series: str, team1: str, team2: str):
    query = """
        INSERT INTO tournament_matches (matchid, mode, series, team1, team2)
        VALUES (%s, %s, %s, %s, %s) AS new_values
        ON DUPLICATE KEY UPDATE
            mode = new_values.mode,
            series = new_values.series,
            team1 = new_values.team1,
            team2 = new_values.team2
    """
    await db.execute(query, (matchid, mode, series, team1, team2))

# ================= FUNÇÕES DE RANKING =================

async def register_player(discord_id: int, steamid64: str, nickname: str) -> Optional[Dict]:
    """Cria cadastro do jogador em players e ranking."""
    nick = str(nickname or "").strip()
    sid = str(steamid64 or "").strip()
    did = int(discord_id)
    if not nick or not sid:
        return None
    if await is_nickname_in_use(nick, exclude_discord_id=did):
        return None

    query = """
        INSERT INTO players (nickname, steamid64, discord_id, total_matches, wins, losses, win_streak)
        VALUES (%s, %s, %s, 0, 0, 0, 0)
    """
    try:
        player_id = await db.insert_and_get_id(query, (nick, sid, did))
    except Exception as e:
        if "duplicate" in str(e).lower():
            return None
        raise

    if not player_id:
        row = await db.fetchone("SELECT id FROM players WHERE discord_id = %s LIMIT 1", (did,))
        if not row:
            return None
        player_id = int(row["id"])

    rank_query = """
        INSERT INTO ranking (id, nickname, rating)
        VALUES (%s, %s, 1000) AS new_values
        ON DUPLICATE KEY UPDATE
            nickname = new_values.nickname
    """
    await db.execute(rank_query, (int(player_id), nick))

    # Sync Bridge: DESATIVADO — webapp usa projectmix direto
    # import asyncio as _asyncio
    # from bot.sync_bridge import sync_new_player as _sync_new_player
    # _asyncio.ensure_future(_sync_new_player(discord_id=did, steamid64=sid, nickname=nick))

    return {"id": int(player_id), "discord_id": did, "steamid64": sid, "nickname": nick}

async def get_registered_player(discord_id: int) -> Optional[Dict]:
    query = """
        SELECT id, nickname, steamid64, discord_id, created_at, updated_at
        FROM players
        WHERE discord_id = %s
        LIMIT 1
    """
    return await db.fetchone(query, (int(discord_id),))


async def is_nickname_in_use(nickname: str, exclude_discord_id: int | None = None) -> bool:
    nick = str(nickname or "").strip()
    if not nick:
        return False

    query = """
        SELECT 1
        FROM players
        WHERE LOWER(TRIM(nickname)) = LOWER(TRIM(%s))
    """
    params: list = [nick]
    if exclude_discord_id is not None:
        query += " AND discord_id <> %s"
        params.append(int(exclude_discord_id))
    query += " LIMIT 1"
    return await db.fetchone(query, tuple(params)) is not None


async def update_player_nickname(discord_id: int, nickname: str) -> bool:
    """Atualiza nickname do player em players e ranking."""
    nick = str(nickname or "").strip()
    did = int(discord_id)
    if not nick:
        return False
    if await is_nickname_in_use(nick, exclude_discord_id=did):
        return False

    player = await db.fetchone(
        "SELECT id FROM players WHERE discord_id = %s LIMIT 1",
        (did,),
    )
    if not player:
        return False

    player_id = int(player["id"])
    await db.execute("UPDATE players SET nickname = %s WHERE id = %s", (nick, player_id))

    rank_query = """
        INSERT INTO ranking (id, nickname, rating)
        VALUES (%s, %s, 1000) AS new_values
        ON DUPLICATE KEY UPDATE
            nickname = new_values.nickname
    """
    await db.execute(rank_query, (player_id, nick))
    return True

async def get_player_rank(discord_id: int) -> Optional[Dict]:
    """Busca dados individuais (identidade + ranking + W/L)."""
    query = """
        SELECT
            p.id,
            p.discord_id,
            p.nickname,
            p.steamid64,
            COALESCE(r.rating, 1000) AS rating,
            p.total_matches,
            p.wins,
            p.losses,
            COALESCE(p.win_streak, 0) AS win_streak
        FROM players p
        LEFT JOIN ranking r ON r.id = p.id
        WHERE p.discord_id = %s
        LIMIT 1
    """
    return await db.fetchone(query, (int(discord_id),))

async def has_complete_registration(discord_id: int) -> bool:
    query = """
        SELECT 1
        FROM players
        WHERE discord_id = %s
          AND steamid64 IS NOT NULL
          AND steamid64 <> ''
        LIMIT 1
    """
    return await db.fetchone(query, (int(discord_id),)) is not None

async def add_rank_points(discord_id: int, points: int) -> int:
    player = await db.fetchone(
        "SELECT id, nickname FROM players WHERE discord_id = %s LIMIT 1",
        (int(discord_id),),
    )
    if not player:
        return 0
    query = """
        INSERT INTO ranking (id, nickname, rating)
        VALUES (%s, %s, %s) AS new_values
        ON DUPLICATE KEY UPDATE
            rating = ranking.rating + new_values.rating,
            nickname = new_values.nickname
    """
    return await db.execute(query, (int(player["id"]), str(player.get("nickname") or ""), int(points)))

async def get_top_ranking(limit: int = 20) -> List[Dict]:
    """Busca o ranking global."""
    query = """
        SELECT
            p.discord_id,
            p.steamid64,
            p.nickname AS name,
            COALESCE(r.rating, 1000) AS rating,
            p.wins,
            p.losses,
            p.total_matches,
            COALESCE(p.win_streak, 0) AS win_streak
        FROM players p
        LEFT JOIN ranking r ON r.id = p.id
        WHERE p.discord_id IS NOT NULL
          AND p.total_matches > 0
        ORDER BY COALESCE(r.rating, 1000) DESC
        LIMIT %s
    """
    return await db.fetchall(query, (int(limit),))

async def get_player_stats(steamid64: str, start_date: Optional[str] = None) -> Dict:
    """Busca estatísticas acumuladas do MatchZy"""
    query = """
        SELECT
            COUNT(*) AS total_matches,
            AVG(p.kills) AS avg_kills,
            AVG(p.deaths) AS avg_deaths,
            AVG(p.assists) AS avg_assists,
            AVG(p.damage) AS avg_adr,
            SUM(CAST(p.enemy5ks AS SIGNED)) AS total_aces,
            SUM(CAST(p.enemy2ks AS SIGNED)) AS total_2ks,
            SUM(CAST(p.enemy3ks AS SIGNED)) AS total_3ks,
            SUM(CAST(p.enemy4ks AS SIGNED)) AS total_4ks,
            SUM(CAST(p.enemy5ks AS SIGNED)) AS total_5ks,
            SUM(CAST(p.shots_fired_total AS SIGNED)) AS shots_fired_total,
            SUM(CAST(p.shots_on_target_total AS SIGNED)) AS shots_on_target_total,
            SUM(CAST(p.head_shot_kills AS SIGNED)) AS head_shot_kills_total,
            SUM(CAST(p.entry_wins AS SIGNED)) AS entry_wins,
            SUM(CAST(p.entry_count AS SIGNED)) AS entry_count,
            SUM(CAST(p.utility_damage AS SIGNED)) AS utility_damage_total
        FROM matchzy_stats_players p
        JOIN matchzy_stats_matches m ON p.matchid = m.matchid
        WHERE p.steamid64 = %s
    """
    params: List = [steamid64]
    if start_date:
        query += """
          AND DATE(COALESCE(m.end_time, m.start_time)) >= %s
        """
        params.append(str(start_date))
    result = await db.fetchone(query, tuple(params))
    return result or {}

async def get_player_history(steamid64: str, limit: int = 10, start_date: Optional[str] = None) -> List[Dict]:
    """Busca histórico de partidas"""
    query = """
        SELECT
            p.matchid, p.team, p.kills, p.deaths, p.assists, p.damage,
            m.team1_name, m.team2_name, m.team1_score, m.team2_score,
            m.start_time,
            mp.mapname, mp.team1_score as map_score1, mp.team2_score as map_score2
        FROM matchzy_stats_players p
        JOIN matchzy_stats_matches m ON p.matchid = m.matchid
        JOIN matchzy_stats_maps mp
          ON m.matchid = mp.matchid
         AND mp.mapnumber = (
            SELECT MAX(mp2.mapnumber)
            FROM matchzy_stats_maps mp2
            WHERE mp2.matchid = m.matchid
         )
        WHERE p.steamid64 = %s
    """
    params: List = [steamid64]
    if start_date:
        query += """
          AND DATE(COALESCE(m.end_time, m.start_time)) >= %s
        """
        params.append(str(start_date))
    query += """
        ORDER BY (m.start_time IS NULL) ASC, m.start_time DESC, p.matchid DESC
        LIMIT %s
    """
    params.append(int(limit))
    return await db.fetchall(query, tuple(params))

async def update_ranks(match_id: int):
    """Atualiza o ranking apos uma partida."""
    # Sync Bridge: DESATIVADO — imports removidos
    # import asyncio as _asyncio
    # from bot.sync_bridge import sync_match_result as _sync_match_result
    try:
        match_query = """
            SELECT m.team1_name, m.team2_name, mp.team1_score as s1, mp.team2_score as s2
            FROM matchzy_stats_matches m
            JOIN matchzy_stats_maps mp ON m.matchid = mp.matchid
            WHERE m.matchid = %s ORDER BY mp.mapnumber DESC LIMIT 1
        """
        match = await db.fetchone(match_query, (match_id,))
        if not match:
            return

        winner_team = match["team1_name"] if match["s1"] > match["s2"] else match["team2_name"]
        total_rounds = int(match.get("s1") or 0) + int(match.get("s2") or 0)
        players = await db.fetchall(
            "SELECT steamid64, team, damage FROM matchzy_stats_players WHERE matchid = %s",
            (match_id,),
        )

        for p in players:
            if p.get("team") not in (match["team1_name"], match["team2_name"]):
                continue

            steamid64 = str(p.get("steamid64") or "").strip()
            if not steamid64:
                continue

            player_row = await db.fetchone(
                "SELECT id, nickname FROM players WHERE steamid64 = %s LIMIT 1",
                (steamid64,),
            )
            if not player_row:
                logger.warning(f"update_ranks: steamid sem cadastro em players, ignorando: {steamid64}")
                continue

            is_winner = p["team"] == winner_team
            damage = int(p.get("damage") or 0)
            adr = damage / total_rounds if total_rounds > 0 else 0
            bonus = max(0, min(20, int((adr / 100) * 20)))
            pts = (30 + bonus) if is_winner else (-50 + bonus)
            w, l = (1, 0) if is_winner else (0, 1)

            upsert_rank_query = """
                INSERT INTO ranking (id, nickname, rating)
                VALUES (%s, %s, %s) AS new_values
                ON DUPLICATE KEY UPDATE
                    rating = ranking.rating + new_values.rating,
                    nickname = new_values.nickname
            """
            await db.execute(
                upsert_rank_query,
                (int(player_row["id"]), str(player_row.get("nickname") or ""), int(pts)),
            )

            update_player_query = """
                UPDATE players
                SET total_matches = total_matches + 1,
                    wins = wins + %s,
                    losses = losses + %s,
                    win_streak = IF(%s = 1, COALESCE(win_streak, 0) + 1, 0),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """
            await db.execute(update_player_query, (int(w), int(l), int(w), int(player_row["id"])))
    except Exception as e:
        logger.error(f"Erro ao atualizar ranking: {e}")
        return

    # Sync Bridge: DESATIVADO — webapp usa projectmix direto
    # _asyncio.ensure_future(_sync_match_result(match_id, db))

# ================= AUXILIARES DE PARTIDA =================

async def get_match_details(match_id: int) -> Optional[Dict]:
    query = """
        SELECT m.matchid, m.team1_name, m.team2_name, mp.team1_score, mp.team2_score, mp.mapname
        FROM matchzy_stats_matches m
        JOIN matchzy_stats_maps mp ON m.matchid = mp.matchid
        WHERE m.matchid = %s ORDER BY mp.mapnumber DESC LIMIT 1
    """
    return await db.fetchone(query, (match_id,))

async def get_match_players(match_id: int) -> List[Dict]:
    query = """
        SELECT p.name, p.team, p.kills, p.deaths, p.assists, p.damage, p.steamid64,
               CAST(p.head_shot_kills AS SIGNED) AS f_hs, 
               CAST(p.enemy5ks AS SIGNED) AS f_5k, 
               CAST(p.enemy4ks AS SIGNED) AS f_4k, 
               CAST(p.enemy3ks AS SIGNED) AS f_3k, 
               CAST(p.enemy2ks AS SIGNED) AS f_2k,
               COALESCE(r.rating, 1000) AS rating, pl.discord_id
        FROM matchzy_stats_players p
        LEFT JOIN players pl ON pl.steamid64 = p.steamid64
        LEFT JOIN ranking r ON r.id = pl.id
        WHERE p.matchid = %s ORDER BY p.damage DESC
    """
    return await db.fetchall(query, (match_id,))

async def get_player_team_in_match(match_id: int, steamid64: str) -> Optional[str]:
    """
    Retorna "team1" ou "team2" para o player em um match, ou None se não achar.
    """
    query = """
        SELECT p.team, m.team1_name, m.team2_name
        FROM matchzy_stats_players p
        JOIN matchzy_stats_matches m ON p.matchid = m.matchid
        WHERE p.matchid = %s AND p.steamid64 = %s
        LIMIT 1
    """
    row = await db.fetchone(query, (match_id, steamid64))
    if not row:
        return None
    team_name = row.get("team")
    if team_name and team_name == row.get("team2_name"):
        return "team2"
    if team_name and team_name == row.get("team1_name"):
        return "team1"
    return None

async def get_last_match() -> Optional[Dict]:
    """Busca a última partida (Sem IP para evitar erro)"""
    query = """
        SELECT m.matchid, m.team1_name, m.team2_name, m.team1_score as win1, m.team2_score as win2,
               mp.mapname, mp.team1_score, mp.team2_score
        FROM matchzy_stats_matches m
        JOIN matchzy_stats_maps mp ON m.matchid = mp.matchid
        ORDER BY COALESCE(m.end_time, m.start_time) DESC, m.matchid DESC LIMIT 1
    """
    return await db.fetchone(query)

async def get_match_overview(match_id: int) -> Optional[Dict]:
    query = """
        SELECT m.matchid, m.team1_name, m.team2_name, m.team1_score as win1, m.team2_score as win2,
               mp.mapname, mp.team1_score, mp.team2_score
        FROM matchzy_stats_matches m
        JOIN matchzy_stats_maps mp ON m.matchid = mp.matchid
        WHERE m.matchid = %s
        ORDER BY mp.mapnumber DESC LIMIT 1
    """
    return await db.fetchone(query, (match_id,))

async def get_latest_map_result(match_id: int) -> Optional[Dict]:
    query = """
        SELECT m.matchid, m.team1_name, m.team2_name,
               mp.team1_score as map_score1, mp.team2_score as map_score2
        FROM matchzy_stats_matches m
        JOIN matchzy_stats_maps mp ON m.matchid = mp.matchid
        WHERE m.matchid = %s
        ORDER BY mp.mapnumber DESC LIMIT 1
    """
    return await db.fetchone(query, (match_id,))

async def fix_match_winner_from_maps(match_id: int) -> Optional[Dict]:
    match = await get_latest_map_result(match_id)
    if not match:
        return None
    s1 = int(match.get("map_score1") or 0)
    s2 = int(match.get("map_score2") or 0)
    if s1 == s2:
        return None
    t1_won = s1 > s2
    win1, win2 = (1, 0) if t1_won else (0, 1)
    winner = match["team1_name"] if t1_won else match["team2_name"]
    query = """
        UPDATE matchzy_stats_matches
        SET team1_score = %s,
            team2_score = %s,
            winner = %s,
            end_time = COALESCE(end_time, NOW())
        WHERE matchid = %s
    """
    await db.execute(query, (win1, win2, winner, match_id))
    match.update({"win1": win1, "win2": win2, "winner": winner})
    return match

async def get_recent_match_ids(limit: int = 20) -> List[Dict]:
    query = """
        SELECT matchid
        FROM matchzy_stats_matches
        ORDER BY COALESCE(end_time, start_time) DESC, matchid DESC
        LIMIT %s
    """
    return await db.fetchall(query, (limit,))

async def get_recent_mix_match_ids(limit: int = 20, start_date: Optional[str] = None) -> List[Dict]:
    query = """
        SELECT matchid
        FROM matchzy_stats_matches
        WHERE team1_name LIKE 'T1\\_%%'
          AND team2_name LIKE 'T2\\_%%'
    """
    params: List = []
    if start_date:
        query += """
          AND DATE(COALESCE(end_time, start_time)) >= %s
        """
        params.append(str(start_date))
    query += """
        ORDER BY COALESCE(end_time, start_time) DESC, matchid DESC
        LIMIT %s
    """
    params.append(int(limit))
    return await db.fetchall(query, tuple(params))

async def get_rank_positions(discord_ids: List[int]) -> Dict[int, int]:
    if not discord_ids:
        return {}
    placeholders = ",".join(["%s"] * len(discord_ids))
    query = f"""
        SELECT
            p1.discord_id,
            (
                SELECT COUNT(*) + 1
                FROM players p2
                LEFT JOIN ranking r2 ON r2.id = p2.id
                WHERE COALESCE(r2.rating, 1000) > COALESCE(r1.rating, 1000)
            ) AS pos
        FROM players p1
        LEFT JOIN ranking r1 ON r1.id = p1.id
        WHERE p1.discord_id IN ({placeholders})
    """
    rows = await db.fetchall(query, tuple(discord_ids))
    positions = {}
    for row in rows:
        if row.get("discord_id") is not None and row.get("pos") is not None:
            positions[int(row["discord_id"])] = int(row["pos"])
    return positions

async def get_live_match_by_teams(team1: str, team2: str) -> Optional[Dict]:
    query = """
        SELECT matchid
        FROM matchzy_stats_matches
        WHERE team1_name = %s AND team2_name = %s
          AND (team1_score < 1 AND team2_score < 1)
        ORDER BY COALESCE(start_time, end_time) DESC, matchid DESC LIMIT 1
    """
    return await db.fetchone(query, (team1, team2))

async def is_match_posted(match_id: int) -> bool:
    result = await db.fetchone("SELECT 1 FROM discord_match_posts WHERE match_id = %s", (match_id,))
    return result is not None

async def mark_match_posted(match_id: int):
    await db.execute("INSERT IGNORE INTO discord_match_posts (match_id) VALUES (%s)", (match_id,))


async def get_match_server_id(match_id: int) -> Optional[str]:
    row = await db.fetchone(
        "SELECT server_id FROM match_id_sequence WHERE id = %s LIMIT 1",
        (match_id,),
    )
    if not row:
        return None
    server_id = row.get("server_id")
    return str(server_id) if server_id else None

async def _get_first_free_match_id_hint() -> int:
    query = """
        SELECT COALESCE(MAX(used_id), 0) + 1 AS next_id
        FROM (
            SELECT MAX(matchid) AS used_id
            FROM matchzy_stats_matches
            WHERE matchid < %s
              AND team1_name LIKE 'T1\\_%%'
              AND team2_name LIKE 'T2\\_%%'
            UNION ALL
            SELECT MAX(match_id) AS used_id FROM active_matches WHERE match_id < %s
            UNION ALL
            SELECT MAX(id) AS used_id FROM match_id_sequence WHERE id < %s
            UNION ALL
            SELECT MAX(match_id) AS used_id FROM discord_match_posts WHERE match_id < %s
        ) ids
    """
    row = await db.fetchone(
        query,
        (
            MIX_MATCH_ID_OUTLIER_FLOOR,
            MIX_MATCH_ID_OUTLIER_FLOOR,
            MIX_MATCH_ID_OUTLIER_FLOOR,
            MIX_MATCH_ID_OUTLIER_FLOOR,
        ),
    )
    if row and row.get("next_id") is not None:
        return int(row["next_id"])
    return 1

async def _is_match_id_taken(match_id: int) -> bool:
    query = """
        SELECT 1
        FROM (
            SELECT matchid AS used_id FROM matchzy_stats_matches WHERE matchid = %s
            UNION ALL
            SELECT matchid AS used_id FROM matchzy_stats_maps WHERE matchid = %s
            UNION ALL
            SELECT match_id AS used_id FROM active_matches WHERE match_id = %s
            UNION ALL
            SELECT id AS used_id FROM match_id_sequence WHERE id = %s
        ) ids
        LIMIT 1
    """
    row = await db.fetchone(query, (match_id, match_id, match_id, match_id))
    return row is not None

async def get_next_match_id() -> int:
    return await _get_first_free_match_id_hint()

async def reserve_match_id(server_id: str) -> int:
    lock_key = "mixbot_matchid_reserve"
    got_lock = False
    for _ in range(3):
        lock = await db.fetchone("SELECT GET_LOCK(%s, 2) AS got", (lock_key,))
        if lock and lock.get("got") == 1:
            got_lock = True
            break
        await asyncio.sleep(0.2)
    if not got_lock:
        logger.warning("Nao foi possivel obter lock; tentando reserva otimista de matchid.")
    try:
        candidate = await _get_first_free_match_id_hint()
        if candidate < 1:
            candidate = 1
        while True:
            if await _is_match_id_taken(candidate):
                candidate += 1
                continue
            try:
                await db.execute(
                    "INSERT INTO match_id_sequence (id, server_id, started_at) VALUES (%s, %s, NOW())",
                    (candidate, server_id)
                )
                return candidate
            except Exception as e:
                if "Duplicate" in str(e) or "duplicate" in str(e):
                    candidate += 1
                    continue
                raise
    finally:
        if got_lock:
            await db.execute("DO RELEASE_LOCK(%s)", (lock_key,))

async def clear_reserved_match_id(match_id: int):
    await db.execute("DELETE FROM match_id_sequence WHERE id = %s", (match_id,))

async def ensure_tables():
    async def table_exists(name: str) -> bool:
        check_query = """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            LIMIT 1
        """
        return await db.fetchone(check_query, (DB_CONFIG['database'], name)) is not None

    async def column_exists(table: str, column: str) -> bool:
        query = """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
            LIMIT 1
        """
        return await db.fetchone(query, (DB_CONFIG['database'], table, column)) is not None

    async def index_exists(table: str, index_name: str) -> bool:
        query = """
            SELECT 1
            FROM information_schema.statistics
            WHERE table_schema = %s AND table_name = %s AND index_name = %s
            LIMIT 1
        """
        return await db.fetchone(query, (DB_CONFIG["database"], table, index_name)) is not None

    async def fk_exists(table: str, constraint_name: str) -> bool:
        query = """
            SELECT 1
            FROM information_schema.referential_constraints
            WHERE constraint_schema = %s AND table_name = %s AND constraint_name = %s
            LIMIT 1
        """
        return await db.fetchone(query, (DB_CONFIG["database"], table, constraint_name)) is not None

    if not await table_exists("waiting_queue"):
        query = """
            CREATE TABLE waiting_queue (
                discord_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_damage INT NOT NULL DEFAULT 0,
                priority_awarded_at TIMESTAMP NULL DEFAULT NULL,
                INDEX idx_waiting_joined_at (joined_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("waiting_queue", "joined_at"):
            await db.execute("ALTER TABLE waiting_queue ADD COLUMN joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
        if not await column_exists("waiting_queue", "last_damage"):
            await db.execute("ALTER TABLE waiting_queue ADD COLUMN last_damage INT NOT NULL DEFAULT 0")
        if not await column_exists("waiting_queue", "priority_awarded_at"):
            await db.execute("ALTER TABLE waiting_queue ADD COLUMN priority_awarded_at TIMESTAMP NULL DEFAULT NULL")

    if not await table_exists("active_matches"):
        query = """
            CREATE TABLE active_matches (
                server_id VARCHAR(50) PRIMARY KEY,
                match_id INT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("active_sessions"):
        query = """
            CREATE TABLE active_sessions (
                server_id VARCHAR(50) PRIMARY KEY,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("match_runtime_servers"):
        query = """
            CREATE TABLE match_runtime_servers (
                match_id INT PRIMARY KEY,
                runtime_server_id VARCHAR(64) NOT NULL UNIQUE,
                tmux_session VARCHAR(128) NOT NULL,
                source VARCHAR(16) NOT NULL,
                lobby_server_id VARCHAR(64) NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("match_runtime_servers", "tmux_session"):
            await db.execute("ALTER TABLE match_runtime_servers ADD COLUMN tmux_session VARCHAR(128) NOT NULL DEFAULT ''")
        if not await column_exists("match_runtime_servers", "source"):
            await db.execute("ALTER TABLE match_runtime_servers ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'mix'")
        if not await column_exists("match_runtime_servers", "lobby_server_id"):
            await db.execute("ALTER TABLE match_runtime_servers ADD COLUMN lobby_server_id VARCHAR(64) NULL")
        if not await column_exists("match_runtime_servers", "started_at"):
            await db.execute("ALTER TABLE match_runtime_servers ADD COLUMN started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    if not await table_exists("match_id_sequence"):
        query = """
            CREATE TABLE match_id_sequence (
                id INT AUTO_INCREMENT PRIMARY KEY,
                server_id VARCHAR(50),
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("match_id_sequence", "server_id"):
            await db.execute("ALTER TABLE match_id_sequence ADD COLUMN server_id VARCHAR(50)")
        if not await column_exists("match_id_sequence", "started_at"):
            await db.execute("ALTER TABLE match_id_sequence ADD COLUMN started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    if await table_exists("ranking") and not await column_exists("ranking", "id"):
        legacy_name = "ranking_legacy_backup"
        if await table_exists(legacy_name):
            legacy_name = f"ranking_legacy_backup_{int(time.time())}"
        await db.execute(f"RENAME TABLE ranking TO {legacy_name}")
        logger.warning(f"Tabela ranking legada detectada e renomeada para {legacy_name}. Recadastro obrigatorio.")

    if not await table_exists("players"):
        query = """
            CREATE TABLE players (
                id INT AUTO_INCREMENT PRIMARY KEY,
                nickname VARCHAR(100) NOT NULL,
                steamid64 VARCHAR(20) NOT NULL,
                discord_id BIGINT NOT NULL,
                total_matches INT NOT NULL DEFAULT 0,
                wins INT NOT NULL DEFAULT 0,
                losses INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                win_streak INT NOT NULL DEFAULT 0,
                UNIQUE KEY uniq_players_discord_id (discord_id),
                UNIQUE KEY uniq_players_steamid64 (steamid64),
                INDEX idx_players_nickname (nickname)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("players", "nickname"):
            await db.execute("ALTER TABLE players ADD COLUMN nickname VARCHAR(100) NOT NULL DEFAULT 'Player'")
        if not await column_exists("players", "steamid64"):
            await db.execute("ALTER TABLE players ADD COLUMN steamid64 VARCHAR(20) NOT NULL")
        if not await column_exists("players", "discord_id"):
            await db.execute("ALTER TABLE players ADD COLUMN discord_id BIGINT NOT NULL")
        if not await column_exists("players", "total_matches"):
            await db.execute("ALTER TABLE players ADD COLUMN total_matches INT NOT NULL DEFAULT 0")
        if not await column_exists("players", "wins"):
            await db.execute("ALTER TABLE players ADD COLUMN wins INT NOT NULL DEFAULT 0")
        if not await column_exists("players", "losses"):
            await db.execute("ALTER TABLE players ADD COLUMN losses INT NOT NULL DEFAULT 0")
        if not await column_exists("players", "created_at"):
            await db.execute("ALTER TABLE players ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        if not await column_exists("players", "updated_at"):
            await db.execute(
                "ALTER TABLE players ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
            )
        if not await column_exists("players", "win_streak"):
            await db.execute("ALTER TABLE players ADD COLUMN win_streak INT NOT NULL DEFAULT 0")
        if not await index_exists("players", "uniq_players_discord_id"):
            await db.execute("ALTER TABLE players ADD UNIQUE KEY uniq_players_discord_id (discord_id)")
        if not await index_exists("players", "uniq_players_steamid64"):
            await db.execute("ALTER TABLE players ADD UNIQUE KEY uniq_players_steamid64 (steamid64)")
        if not await index_exists("players", "idx_players_nickname"):
            await db.execute("CREATE INDEX idx_players_nickname ON players (nickname)")

    if not await table_exists("ranking"):
        query = """
            CREATE TABLE ranking (
                id INT PRIMARY KEY,
                nickname VARCHAR(100) NOT NULL,
                rating INT NOT NULL DEFAULT 1000,
                INDEX idx_ranking_rating (rating),
                CONSTRAINT fk_ranking_player FOREIGN KEY (id) REFERENCES players(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("ranking", "nickname"):
            await db.execute("ALTER TABLE ranking ADD COLUMN nickname VARCHAR(100) NOT NULL DEFAULT 'Player'")
        if not await column_exists("ranking", "rating"):
            await db.execute("ALTER TABLE ranking ADD COLUMN rating INT NOT NULL DEFAULT 1000")
        if not await index_exists("ranking", "idx_ranking_rating"):
            await db.execute("CREATE INDEX idx_ranking_rating ON ranking (rating)")
        if not await fk_exists("ranking", "fk_ranking_player"):
            try:
                await db.execute(
                    "ALTER TABLE ranking ADD CONSTRAINT fk_ranking_player FOREIGN KEY (id) REFERENCES players(id) ON DELETE CASCADE"
                )
            except Exception as e:
                logger.warning(f"Nao foi possivel criar FK fk_ranking_player: {e}")

    if not await table_exists("welcome_messages"):
        query = """
            CREATE TABLE welcome_messages (
                discord_id BIGINT PRIMARY KEY,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("match_feedback"):
        query = """
            CREATE TABLE match_feedback (
                id INT AUTO_INCREMENT PRIMARY KEY,
                match_id INT NOT NULL,
                reporter_id BIGINT NOT NULL,
                target_id BIGINT NOT NULL,
                vote_type VARCHAR(10) NOT NULL,
                reason VARCHAR(50) NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY unique_feedback (match_id, reporter_id, target_id),
                INDEX idx_match (match_id),
                INDEX idx_target (target_id),
                INDEX idx_reporter (reporter_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("match_reports"):
        query = """
            CREATE TABLE match_reports (
                id INT AUTO_INCREMENT PRIMARY KEY,
                match_id INT NOT NULL,
                reporter_id BIGINT NOT NULL,
                reported_id BIGINT NOT NULL,
                reason VARCHAR(50) NOT NULL,
                details TEXT,
                status VARCHAR(20) DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                handled_by BIGINT,
                handled_at TIMESTAMP NULL,
                UNIQUE KEY unique_report (match_id, reporter_id, reported_id),
                INDEX idx_match (match_id),
                INDEX idx_reported (reported_id),
                INDEX idx_reporter (reporter_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("smurf_overrides"):
        query = """
            CREATE TABLE smurf_overrides (
                discord_id BIGINT PRIMARY KEY,
                override_elo INT NOT NULL,
                override_level INT NOT NULL,
                set_by BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("bot_bans"):
        query = """
            CREATE TABLE bot_bans (
                id INT AUTO_INCREMENT PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                ban_type VARCHAR(20) NOT NULL,
                reason TEXT,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NULL,
                revoked_at TIMESTAMP NULL,
                revoked_by BIGINT NULL,
                revoked_reason TEXT,
                report_id INT NULL,
                INDEX idx_discord_id (discord_id),
                INDEX idx_expires_at (expires_at),
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("bot_bans", "report_id"):
            await db.execute("ALTER TABLE bot_bans ADD COLUMN report_id INT NULL")

    if not await table_exists("bot_infractions"):
        query = """
            CREATE TABLE bot_infractions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                level INT NOT NULL,
                occurrence INT NOT NULL,
                action_type VARCHAR(20) NOT NULL,
                action_duration INT NULL,
                reason TEXT,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                report_id INT NULL,
                INDEX idx_discord_id (discord_id),
                INDEX idx_created_at (created_at),
                INDEX idx_report_id (report_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("matchguardian_logs"):
        query = """
            CREATE TABLE matchguardian_logs (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                alert_kind VARCHAR(20) NOT NULL,
                matchid VARCHAR(64) NOT NULL,
                server_id VARCHAR(64) NOT NULL,
                source_message_id BIGINT NULL,
                discord_guild_id BIGINT NULL,
                discord_channel_id BIGINT NULL,
                discord_message_id BIGINT NULL,
                discord_thread_id BIGINT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP NULL,
                closed_by_discord_id BIGINT NULL,
                closed_by_name VARCHAR(128) NULL,
                transcript LONGTEXT NULL,
                INDEX idx_mg_logs_matchid (matchid),
                INDEX idx_mg_logs_server (server_id),
                INDEX idx_mg_logs_status (status),
                INDEX idx_mg_logs_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("matchguardian_completer_requests"):
        query = """
            CREATE TABLE matchguardian_completer_requests (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                source_message_id BIGINT NULL,
                discord_channel_id BIGINT NULL,
                discord_message_id BIGINT NOT NULL,
                server_id VARCHAR(64) NOT NULL,
                matchid VARCHAR(64) NOT NULL,
                team_text VARCHAR(64) NULL,
                abandoned_steamid VARCHAR(32) NOT NULL,
                abandoned_name VARCHAR(128) NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'open',
                claimed_by_discord_id BIGINT NULL,
                claimed_by_name VARCHAR(128) NULL,
                claimed_steamid64 VARCHAR(32) NULL,
                created_at DATETIME NOT NULL DEFAULT NOW(),
                claimed_at DATETIME NULL,
                UNIQUE KEY uniq_mgcr_discord_message_id (discord_message_id),
                INDEX idx_mgcr_matchid (matchid),
                INDEX idx_mgcr_server (server_id),
                INDEX idx_mgcr_status (status),
                INDEX idx_mgcr_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)

    if not await table_exists("matchguardian_notconnect"):
        query = """
            CREATE TABLE matchguardian_notconnect (
                id INT AUTO_INCREMENT PRIMARY KEY,
                matchid VARCHAR(64) NOT NULL,
                steamid64 VARCHAR(32) NOT NULL,
                player_name VARCHAR(128) NULL,
                created_at DATETIME NOT NULL DEFAULT NOW(),
                INDEX idx_mgnc_matchid (matchid),
                INDEX idx_mgnc_steamid64 (steamid64),
                INDEX idx_mgnc_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("matchguardian_notconnect", "matchid"):
            await db.execute("ALTER TABLE matchguardian_notconnect ADD COLUMN matchid VARCHAR(64) NOT NULL")
        if not await column_exists("matchguardian_notconnect", "steamid64"):
            await db.execute("ALTER TABLE matchguardian_notconnect ADD COLUMN steamid64 VARCHAR(32) NOT NULL")
        if not await column_exists("matchguardian_notconnect", "player_name"):
            await db.execute("ALTER TABLE matchguardian_notconnect ADD COLUMN player_name VARCHAR(128) NULL")
        if not await column_exists("matchguardian_notconnect", "created_at"):
            await db.execute(
                "ALTER TABLE matchguardian_notconnect ADD COLUMN created_at DATETIME NOT NULL DEFAULT NOW()"
            )
        if not await index_exists("matchguardian_notconnect", "idx_mgnc_matchid"):
            await db.execute("CREATE INDEX idx_mgnc_matchid ON matchguardian_notconnect (matchid)")
        if not await index_exists("matchguardian_notconnect", "idx_mgnc_steamid64"):
            await db.execute("CREATE INDEX idx_mgnc_steamid64 ON matchguardian_notconnect (steamid64)")
        if not await index_exists("matchguardian_notconnect", "idx_mgnc_created_at"):
            await db.execute("CREATE INDEX idx_mgnc_created_at ON matchguardian_notconnect (created_at)")

    if not await table_exists("tournament_teams"):
        query = """
            CREATE TABLE tournament_teams (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                players VARCHAR(100) NOT NULL,
                steamid VARCHAR(32) NOT NULL,
                discord_id BIGINT NOT NULL,
                is_captain TINYINT(1) NOT NULL DEFAULT 0,
                group_name CHAR(1) NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_team_member (name, discord_id),
                INDEX idx_team_name (name),
                INDEX idx_group_name (group_name),
                INDEX idx_discord_id (discord_id),
                INDEX idx_steamid (steamid)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("tournament_teams", "is_captain"):
            await db.execute("ALTER TABLE tournament_teams ADD COLUMN is_captain TINYINT(1) NOT NULL DEFAULT 0")
        if not await column_exists("tournament_teams", "group_name"):
            await db.execute("ALTER TABLE tournament_teams ADD COLUMN group_name CHAR(1) NULL")
            await db.execute("CREATE INDEX idx_group_name ON tournament_teams (group_name)")

    if not await table_exists("tournament_matches"):
        query = """
            CREATE TABLE tournament_matches (
                id INT AUTO_INCREMENT PRIMARY KEY,
                matchid VARCHAR(20) NOT NULL,
                mode VARCHAR(10) NOT NULL,
                series VARCHAR(10) NOT NULL,
                team1 VARCHAR(100) NOT NULL,
                team2 VARCHAR(100) NOT NULL,
                winner VARCHAR(100) NULL,
                team1_score INT NULL,
                team2_score INT NULL,
                result_type VARCHAR(20) NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_tournament_matchid (matchid),
                INDEX idx_mode (mode),
                INDEX idx_series (series),
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        await db.execute(query)
    else:
        if not await column_exists("tournament_matches", "winner"):
            await db.execute("ALTER TABLE tournament_matches ADD COLUMN winner VARCHAR(100) NULL")
        if not await column_exists("tournament_matches", "team1_score"):
            await db.execute("ALTER TABLE tournament_matches ADD COLUMN team1_score INT NULL")
        if not await column_exists("tournament_matches", "team2_score"):
            await db.execute("ALTER TABLE tournament_matches ADD COLUMN team2_score INT NULL")
        if not await column_exists("tournament_matches", "result_type"):
            await db.execute("ALTER TABLE tournament_matches ADD COLUMN result_type VARCHAR(20) NULL")

async def set_active_match(server_id: str, match_id: int) -> bool:
    existing = await db.fetchone(
        "SELECT server_id FROM active_matches WHERE match_id = %s LIMIT 1",
        (match_id,),
    )
    if existing and existing.get("server_id") != server_id:
        logger.warning(
            f"active_matches: match_id {match_id} ja em uso por {existing.get('server_id')}; ignorando para {server_id}"
        )
        return False
    query = """
        INSERT INTO active_matches (server_id, match_id)
        VALUES (%s, %s) AS new_values
        ON DUPLICATE KEY UPDATE match_id = new_values.match_id
    """
    await db.execute(query, (server_id, match_id))
    return True

async def clear_active_match(server_id: str):
    await db.execute("DELETE FROM active_matches WHERE server_id = %s", (server_id,))

async def get_active_matches() -> List[Dict]:
    return await db.fetchall("SELECT server_id, match_id FROM active_matches")


async def bind_match_runtime_server(
    match_id: int,
    runtime_server_id: str,
    tmux_session: str,
    source: str,
    lobby_server_id: Optional[str] = None,
) -> None:
    query = """
        INSERT INTO match_runtime_servers
            (match_id, runtime_server_id, tmux_session, source, lobby_server_id, started_at)
        VALUES (%s, %s, %s, %s, %s, NOW()) AS new_values
        ON DUPLICATE KEY UPDATE
            runtime_server_id = new_values.runtime_server_id,
            tmux_session = new_values.tmux_session,
            source = new_values.source,
            lobby_server_id = new_values.lobby_server_id,
            started_at = NOW()
    """
    await db.execute(
        query,
        (
            int(match_id),
            str(runtime_server_id or ""),
            str(tmux_session or ""),
            str(source or "mix"),
            str(lobby_server_id) if lobby_server_id is not None else None,
        ),
    )


async def get_match_runtime_server(match_id: int) -> Optional[Dict]:
    return await db.fetchone(
        """
        SELECT
            match_id,
            runtime_server_id,
            tmux_session,
            source,
            lobby_server_id,
            started_at
        FROM match_runtime_servers
        WHERE match_id = %s
        LIMIT 1
        """,
        (int(match_id),),
    )


async def get_busy_runtime_servers() -> List[Dict]:
    return await db.fetchall(
        """
        SELECT
            match_id,
            runtime_server_id,
            tmux_session,
            source,
            lobby_server_id,
            started_at
        FROM match_runtime_servers
        """
    )


async def is_match_finished(match_id: int) -> bool:
    row = await db.fetchone(
        """
        SELECT 1
        FROM matchzy_stats_matches
        WHERE matchid = %s
          AND (
              (winner IS NOT NULL AND winner <> '')
              OR end_time IS NOT NULL
          )
        LIMIT 1
        """,
        (int(match_id),),
    )
    return row is not None


async def clear_match_runtime_server(match_id: int) -> None:
    await db.execute("DELETE FROM match_runtime_servers WHERE match_id = %s", (int(match_id),))


async def acquire_named_lock(lock_name: str, timeout_seconds: int = 5) -> bool:
    key = str(lock_name)
    if db._pool_unusable():
        await db.connect()

    async with db._named_lock_guard:
        if key in db._named_lock_conns:
            return True

        conn = await db.pool.acquire()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("SELECT GET_LOCK(%s, %s) AS got", (key, int(timeout_seconds)))
                row = await cursor.fetchone()
            got = bool(row and int(row.get("got") or 0) == 1)
            if got:
                db._named_lock_conns[key] = conn
                return True
            db.pool.release(conn)
            return False
        except Exception:
            try:
                db.pool.release(conn)
            except Exception:
                pass
            raise


async def release_named_lock(lock_name: str) -> None:
    key = str(lock_name)
    async with db._named_lock_guard:
        conn = db._named_lock_conns.pop(key, None)

    if conn is None:
        return

    try:
        async with conn.cursor() as cursor:
            await cursor.execute("DO RELEASE_LOCK(%s)", (key,))
    except Exception:
        pass
    finally:
        try:
            if db.pool and not db._pool_unusable():
                db.pool.release(conn)
            else:
                conn.close()
        except Exception:
            pass

async def set_active_session(server_id: str):
    query = """
        INSERT INTO active_sessions (server_id, started_at)
        VALUES (%s, NOW()) AS new_values
        ON DUPLICATE KEY UPDATE started_at = new_values.started_at
    """
    await db.execute(query, (server_id,))

async def clear_active_session(server_id: str):
    await db.execute("DELETE FROM active_sessions WHERE server_id = %s", (server_id,))

async def is_active_session(server_id: str) -> bool:
    result = await db.fetchone("SELECT 1 FROM active_sessions WHERE server_id = %s", (server_id,))
    return result is not None

async def ping_db() -> bool:
    try:
        await db.execute("SELECT 1")
        return True
    except Exception:
        return False

async def has_welcome_message(discord_id: int) -> bool:
    result = await db.fetchone("SELECT 1 FROM welcome_messages WHERE discord_id = %s", (discord_id,))
    return result is not None

async def mark_welcome_message(discord_id: int):
    await db.execute("INSERT IGNORE INTO welcome_messages (discord_id) VALUES (%s)", (discord_id,))


async def create_matchguardian_log(
    alert_kind: str,
    matchid: str,
    server_id: str,
    source_message_id: Optional[int] = None,
    discord_guild_id: Optional[int] = None,
    discord_channel_id: Optional[int] = None,
    discord_message_id: Optional[int] = None,
) -> int:
    query = """
        INSERT INTO matchguardian_logs
        (
            alert_kind,
            matchid,
            server_id,
            source_message_id,
            discord_guild_id,
            discord_channel_id,
            discord_message_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    return await db.insert_and_get_id(
        query,
        (
            str(alert_kind),
            str(matchid or ""),
            str(server_id or ""),
            source_message_id,
            discord_guild_id,
            discord_channel_id,
            discord_message_id,
        ),
    )


async def attach_matchguardian_log_message(
    log_id: int,
    discord_message_id: Optional[int] = None,
    discord_thread_id: Optional[int] = None,
) -> int:
    query = """
        UPDATE matchguardian_logs
        SET discord_message_id = COALESCE(%s, discord_message_id),
            discord_thread_id = COALESCE(%s, discord_thread_id)
        WHERE id = %s
    """
    return await db.execute(query, (discord_message_id, discord_thread_id, int(log_id)))


async def close_matchguardian_log(
    log_id: int,
    closed_by_discord_id: int,
    closed_by_name: str,
    transcript: str,
) -> int:
    query = """
        UPDATE matchguardian_logs
        SET status = 'closed',
            closed_at = NOW(),
            closed_by_discord_id = %s,
            closed_by_name = %s,
            transcript = %s
        WHERE id = %s
    """
    return await db.execute(
        query,
        (
            int(closed_by_discord_id),
            str(closed_by_name or ""),
            str(transcript or ""),
            int(log_id),
        ),
    )


async def save_matchguardian_completer_request(
    discord_message_id: int,
    source_message_id: Optional[int] = None,
    discord_channel_id: Optional[int] = None,
    server_id: str = "",
    matchid: str = "",
    team_text: str = "",
    abandoned_steamid: str = "",
    abandoned_name: str = "",
) -> int:
    query = """
        INSERT INTO matchguardian_completer_requests
        (
            source_message_id,
            discord_channel_id,
            discord_message_id,
            server_id,
            matchid,
            team_text,
            abandoned_steamid,
            abandoned_name
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            source_message_id = VALUES(source_message_id),
            discord_channel_id = VALUES(discord_channel_id),
            server_id = VALUES(server_id),
            matchid = VALUES(matchid),
            team_text = VALUES(team_text),
            abandoned_steamid = VALUES(abandoned_steamid),
            abandoned_name = VALUES(abandoned_name)
    """
    return await db.execute(
        query,
        (
            source_message_id,
            discord_channel_id,
            int(discord_message_id),
            str(server_id or ""),
            str(matchid or ""),
            str(team_text or ""),
            str(abandoned_steamid or ""),
            str(abandoned_name or ""),
        ),
    )


async def get_matchguardian_completer_request(discord_message_id: int) -> Optional[Dict]:
    query = """
        SELECT
            id,
            source_message_id,
            discord_channel_id,
            discord_message_id,
            server_id,
            matchid,
            team_text,
            abandoned_steamid,
            abandoned_name,
            status,
            claimed_by_discord_id,
            claimed_by_name,
            claimed_steamid64,
            created_at,
            claimed_at
        FROM matchguardian_completer_requests
        WHERE discord_message_id = %s
        LIMIT 1
    """
    return await db.fetchone(query, (int(discord_message_id),))


async def claim_matchguardian_completer_request(
    discord_message_id: int,
    claimed_by_discord_id: int,
    claimed_by_name: str,
    claimed_steamid64: str,
) -> int:
    query = """
        UPDATE matchguardian_completer_requests
        SET status = 'claimed',
            claimed_at = NOW(),
            claimed_by_discord_id = %s,
            claimed_by_name = %s,
            claimed_steamid64 = %s
        WHERE discord_message_id = %s
          AND status <> 'claimed'
    """
    return await db.execute(
        query,
        (
            int(claimed_by_discord_id),
            str(claimed_by_name or ""),
            str(claimed_steamid64 or ""),
            int(discord_message_id),
        ),
    )


async def get_matchguardian_notconnect_rows(
    matchid: str,
    since_utc: Optional[datetime] = None,
    limit: int = 30,
) -> List[Dict]:
    base_query = """
        SELECT steamid64, player_name, created_at
        FROM matchguardian_notconnect
        WHERE matchid = %s
    """
    params: List = [str(matchid or "")]
    if since_utc is not None:
        base_query += " AND created_at >= %s"
        # MySQL DATETIME normalmente e salvo sem timezone.
        params.append(since_utc.replace(tzinfo=None))
    base_query += " ORDER BY id DESC LIMIT %s"
    params.append(max(1, int(limit)))
    try:
        return await db.fetchall(base_query, tuple(params))
    except Exception as e:
        # Tolerancia para bancos novos sem a tabela ainda provisionada.
        text = str(e).lower()
        if "1146" in text and "matchguardian_notconnect" in text:
            return []
        raise

async def get_active_match_by_server(server_ip=None):
    sql = """
        SELECT m.matchid, m.team1_name, m.team2_name, mp.mapname, mp.team1_score, mp.team2_score
        FROM matchzy_stats_matches m
        JOIN matchzy_stats_maps mp ON m.matchid = mp.matchid
        WHERE (m.team1_score < 1 AND m.team2_score < 1)
        ORDER BY m.matchid DESC LIMIT 1
    """
    return await db.fetchone(sql)

# ================= FEEDBACK/REPORT DE PARTIDA =================

async def is_player_in_match(match_id: int, discord_id: int) -> bool:
    query = """
        SELECT 1
        FROM matchzy_stats_players p
        JOIN players pl ON p.steamid64 = pl.steamid64
        WHERE p.matchid = %s AND pl.discord_id = %s
        LIMIT 1
    """
    return await db.fetchone(query, (match_id, discord_id)) is not None

async def save_match_feedback(
    match_id: int,
    reporter_id: int,
    target_id: int,
    vote_type: str,
    reason: str,
    details: str = None
) -> bool:
    query = """
        INSERT IGNORE INTO match_feedback
        (match_id, reporter_id, target_id, vote_type, reason, details)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    rows = await db.execute(query, (match_id, reporter_id, target_id, vote_type, reason, details))
    return rows > 0

async def save_match_report(
    match_id: int,
    reporter_id: int,
    reported_id: int,
    reason: str,
    details: str = None
) -> bool:
    query = """
        INSERT IGNORE INTO match_reports
        (match_id, reporter_id, reported_id, reason, details)
        VALUES (%s, %s, %s, %s, %s)
    """
    rows = await db.execute(query, (match_id, reporter_id, reported_id, reason, details))
    return rows > 0

# ================= PUNICOES =================

async def get_active_ban(discord_id: int) -> Optional[Dict]:
    query = """
        SELECT *
        FROM bot_bans
        WHERE discord_id = %s
          AND revoked_at IS NULL
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY created_at DESC
        LIMIT 1
    """
    return await db.fetchone(query, (discord_id,))

async def add_bot_ban(
    discord_id: int,
    ban_type: str,
    reason: str,
    created_by: int,
    duration_seconds: Optional[int] = None,
    report_id: Optional[int] = None
) -> int:
    expires_at = None
    if duration_seconds:
        expires_at = datetime.utcnow() + timedelta(seconds=duration_seconds)
    query = """
        INSERT INTO bot_bans (discord_id, ban_type, reason, created_by, expires_at, report_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    return await db.insert_and_get_id(query, (discord_id, ban_type, reason, created_by, expires_at, report_id))

async def add_bot_infraction(
    discord_id: int,
    level: int,
    occurrence: int,
    action_type: str,
    action_duration: Optional[int],
    reason: str,
    created_by: int,
    report_id: Optional[int] = None
) -> int:
    query = """
        INSERT INTO bot_infractions
        (discord_id, level, occurrence, action_type, action_duration, reason, created_by, report_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    return await db.insert_and_get_id(
        query,
        (discord_id, level, occurrence, action_type, action_duration, reason, created_by, report_id)
    )

async def revoke_bot_ban(discord_id: int, revoked_by: int, revoked_reason: str = None) -> int:
    query = """
        UPDATE bot_bans
        SET revoked_at = NOW(),
            revoked_by = %s,
            revoked_reason = %s
        WHERE discord_id = %s
          AND revoked_at IS NULL
          AND (expires_at IS NULL OR expires_at > NOW())
    """
    return await db.execute(query, (revoked_by, revoked_reason, discord_id))
