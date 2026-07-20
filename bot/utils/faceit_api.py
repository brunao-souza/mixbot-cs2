import aiohttp
import time
from typing import Optional, Dict
from bot.config import FACEIT_API_KEY

_cache: Dict[str, Dict] = {}
_cache_ttl = 3600


async def get_faceit_profile(steamid64: str) -> Optional[Dict]:
    if not FACEIT_API_KEY or not steamid64:
        return None
    now = time.time()
    cached = _cache.get(steamid64)
    if cached and (now - cached.get("ts", 0)) < _cache_ttl:
        return cached.get("data")
 
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
    timeout = aiohttp.ClientTimeout(total=8)
    data = None
    game_used = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for game in ["cs2"]:
            url = f"https://open.faceit.com/data/v4/players?game={game}&game_player_id={steamid64}"
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        game_used = game
                        break
            except:
                continue

    if not data or not game_used:
        _cache[steamid64] = {"ts": now, "data": None}
        return None

    game_data = data.get("games", {}).get(game_used, {}) or {}
    profile = {
        "nickname": data.get("nickname"),
        "elo": game_data.get("faceit_elo"),
        "level": game_data.get("skill_level"),
    }
    _cache[steamid64] = {"ts": now, "data": profile}
    return profile
