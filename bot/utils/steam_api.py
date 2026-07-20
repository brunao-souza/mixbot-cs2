import aiohttp
from loguru import logger
from typing import Optional, Dict
from bot.config import STEAM_API_KEY


async def get_steam_profile(steamid64: str) -> Optional[Dict]:
    """
    Busca perfil do Steam via API
    
    Args:
        steamid64: SteamID64 do jogador
    
    Returns:
        Dict com 'nickname' e 'avatar' ou None se não encontrado
    """
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    params = {
        "key": STEAM_API_KEY,
        "steamids": steamid64
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logger.error(f"❌ Steam API retornou status {response.status}")
                    return None
                
                data = await response.json()
                players = data.get("response", {}).get("players", [])
                
                if not players:
                    logger.warning(f"⚠️ SteamID {steamid64} não encontrado")
                    return None
                
                player = players[0]
                return {
                    "nickname": player.get("personaname", "Unknown"),
                    "avatar": player.get("avatarfull", "")
                }
    
    except aiohttp.ClientError as e:
        logger.error(f"❌ Erro ao conectar à Steam API: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Erro inesperado na Steam API: {e}")
        return None


def validate_steamid64(steamid: str) -> bool:
    """
    Valida formato de SteamID64
    
    Args:
        steamid: String para validar
    
    Returns:
        True se válido, False caso contrário
    """
    return (
        steamid.isdigit() 
        and len(steamid) == 17 
        and steamid.startswith("7656")
    )