import json
from typing import List, Dict, Optional
from loguru import logger
from datetime import datetime
from bot.config import MATCHZY_ADMIN_STEAMID64

ALWAYS_ALLOW_STEAMID64 = MATCHZY_ADMIN_STEAMID64
ALWAYS_ALLOW_NAME = "Admin"


def generate_matchzy_config(
    match_id: int,
    captain1_name: str,
    captain2_name: str,
    team1_players: List[Dict[str, str]],  # [{"name": "Player", "steamid64": "76561..."}]
    team2_players: List[Dict[str, str]],
    map_name: str,
    clinch_series: bool = True,
    always_allow_steamid64: Optional[str] = None,
    always_allow_name: Optional[str] = None
) -> str:
    """
    Generates JSON configuration for MatchZy.
    
    Args:
        match_id: Unique match ID
        captain1_name: Name of Team 1 captain
        captain2_name: Name of Team 2 captain
        team1_players: List of Team 1 players with name and steamid64
        team2_players: List of Team 2 players with name and steamid64
        map_name: Map name (e.g., de_mirage)
        clinch_series: If True, best of 1 (BO1)
        always_allow_steamid64: SteamID64 to keep always allowed (whitelist)
        always_allow_name: Display name for the allowed SteamID (not on a team)
    
    Returns:
        Formatted JSON string for MatchZy
    """
    
    # Base configuration
    config = {
        "matchid": str(match_id),
        "num_maps": 1,
        "matchzy_version": "0.8.15",
        "clinch_series": clinch_series,
        "side_type": "standard",
        "spectators": {
            "players": {},
            "name": "Spectators"
        },
        "maplist": [map_name],
        "skip_veto": True,
        "team1": {
            "name": f"Team_{captain1_name}",
            "tag": captain1_name[:8],  # Tag limited to 8 chars
            "flag": "BR",
            "players": {}
        },
        "team2": {
            "name": f"Team_{captain2_name}",
            "tag": captain2_name[:8],
            "flag": "BR",
            "players": {}
        },
        "cvars": {
            "mp_teamprediction_txt": "0",
            "sv_pausable": "1",
            "mp_pause_match_end": "1",
            "mp_unpause_match_end": "1",
            "mp_win_panel_display_time": "15",
            "mp_endmatch_votenextmap": "0",
            "mp_match_end_restart": "1",
            "mp_match_end_changelevel": "1",
            # MatchZy Enhanced (fork) compatibility: ensures match mode
            # active and ready->knife flow without relying on manual .start.
            "matchzy_autostart_mode": "1",
            "matchzy_kick_unassigned_players": "1",
            "matchzy_kick_when_no_match_loaded": "0",
            "sv_kick_players_with_cooldown": "0",
            "matchzy_allow_force_ready": "1",
            "matchzy_minimum_ready_required": "10",
            "matchzy_autoready_enabled": "0",
            "matchzy_auto_ready_active_players": "0",
            "matchzy_whitelist_enabled_default": "1",
            "matchzy_whitelist_enabled": "1",
            "matchzy_knife_enabled_default": "1",
            "matchzy_knife_enabled": "1",
            "matchzy_warmup_knife": "0",
            "mp_warmup_pausetimer": "1",
            "mp_warmuptime": "120",
            "mp_maxrounds": "24",
            "mp_overtime_enable": "1",
            "mp_overtime_maxrounds": "6",
            "mp_overtime_startmoney": "16000"
        }
    }
    
    # Adds Team 1 players
    for player in team1_players:
        config["team1"]["players"][player["steamid64"]] = player["name"]
    
    # Adds Team 2 players
    for player in team2_players:
        config["team2"]["players"][player["steamid64"]] = player["name"]

    # Keeps a SteamID always allowed in the whitelist (as spectator)
    allow_id = always_allow_steamid64 or ALWAYS_ALLOW_STEAMID64
    allow_name = always_allow_name or ALWAYS_ALLOW_NAME
    if allow_id:
        if allow_id not in config["team1"]["players"] and allow_id not in config["team2"]["players"]:
            config["spectators"]["players"][allow_id] = allow_name
            logger.debug(f"Whitelist extra: {allow_name} ({allow_id})")
    
    # Converts to JSON
    json_str = json.dumps(config, indent=2, ensure_ascii=False)
    
    logger.info(f"✅ Match config generated for Match #{match_id}")
    logger.debug(f"Team 1: {captain1_name} ({len(team1_players)} players)")
    logger.debug(f"Team 2: {captain2_name} ({len(team2_players)} players)")
    logger.debug(f"Map: {map_name}")
    
    return json_str


def escape_json_for_rcon(json_str: str) -> str:
    """
    Escapes JSON for sending via RCON.
    
    RCON requires double quotes to be escaped with backslash.
    
    Args:
        json_str: Original JSON string
    
    Returns:
        JSON string escaped for RCON
    """
    # Escapes double quotes
    escaped = json_str.replace('"', '\\"')
    
    # Removes line breaks (optional, but recommended for RCON)
    escaped = escaped.replace('\n', ' ').replace('\r', '')
    
    # Removes extra spaces
    import re
    escaped = re.sub(r'\s+', ' ', escaped)
    
    return escaped


def save_match_config_file(config_data: any, match_id: int, output_dir: str = "match_configs") -> str:
    import os
    from pathlib import Path
    
    Path(output_dir).mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{output_dir}/match_{match_id}_{timestamp}.json"
    
    with open(filename, 'w', encoding='utf-8') as f:
        # Checks if it's already a string or a dictionary
        if isinstance(config_data, dict):
            f.write(json.dumps(config_data, indent=4, ensure_ascii=False))
        else:
            f.write(str(config_data))
    
    logger.info(f"📁 Match config saved to: {filename}")
    return filename


# ==================== USAGE EXAMPLE ====================

def example_usage():
    """Example of how to use the functions"""
    
    # Mix data
    match_id = 123
    captain1 = "PlayerAlpha"
    captain2 = "PlayerBravo"
    
    team1 = [
        {"name": "PlayerAlpha", "steamid64": "76561198000000001"},
        {"name": "Player2", "steamid64": "76561198000000002"},
        {"name": "Player3", "steamid64": "76561198000000003"},
        {"name": "Player4", "steamid64": "76561198000000004"},
        {"name": "Player5", "steamid64": "76561198000000005"},
    ]
    
    team2 = [
        {"name": "PlayerBravo", "steamid64": "76561198000000006"},
        {"name": "Player7", "steamid64": "76561198000000007"},
        {"name": "Player8", "steamid64": "76561198000000008"},
        {"name": "Player9", "steamid64": "76561198000000009"},
        {"name": "Player10", "steamid64": "76561198000000010"},
    ]
    
    # Generate config
    config_json = generate_matchzy_config(
        match_id=match_id,
        captain1_name=captain1,
        captain2_name=captain2,
        team1_players=team1,
        team2_players=team2,
        map_name="de_mirage"
    )
    
    # Escapes for RCON
    escaped_json = escape_json_for_rcon(config_json)
    
    # Final RCON command
    rcon_command = f'matchzy_loadmatch_url "{escaped_json}"'
    
    print("=== RCON COMMAND ===")
    print(rcon_command)
    
    # Saves file (optional)
    save_match_config_file(config_json, match_id)
    
    return rcon_command


if __name__ == "__main__":
    example_usage()
