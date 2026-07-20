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
    Gera configuração JSON para o MatchZy.
    
    Args:
        match_id: ID único da partida
        captain1_name: Nome do capitão do Time 1
        captain2_name: Nome do capitão do Time 2
        team1_players: Lista de jogadores do Time 1 com name e steamid64
        team2_players: Lista de jogadores do Time 2 com name e steamid64
        map_name: Nome do mapa (ex: de_mirage)
        clinch_series: Se True, melhor de 1 (BO1)
        always_allow_steamid64: SteamID64 para manter sempre liberado (whitelist)
        always_allow_name: Nome exibido para o SteamID liberado (não entra em time)
    
    Returns:
        String JSON formatada para o MatchZy
    """
    
    # Configuração base
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
            "tag": captain1_name[:8],  # Tag limitada a 8 chars
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
            # Compatibilidade MatchZy Enhanced (fork): garante modo de partida
            # ativo e fluxo de ready->knife sem depender de .start manual.
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
    
    # Adiciona jogadores do Time 1
    for player in team1_players:
        config["team1"]["players"][player["steamid64"]] = player["name"]
    
    # Adiciona jogadores do Time 2
    for player in team2_players:
        config["team2"]["players"][player["steamid64"]] = player["name"]

    # Mantém um SteamID sempre liberado na whitelist (como espectador)
    allow_id = always_allow_steamid64 or ALWAYS_ALLOW_STEAMID64
    allow_name = always_allow_name or ALWAYS_ALLOW_NAME
    if allow_id:
        if allow_id not in config["team1"]["players"] and allow_id not in config["team2"]["players"]:
            config["spectators"]["players"][allow_id] = allow_name
            logger.debug(f"Whitelist extra: {allow_name} ({allow_id})")
    
    # Converte para JSON
    json_str = json.dumps(config, indent=2, ensure_ascii=False)
    
    logger.info(f"✅ Match config gerado para Match #{match_id}")
    logger.debug(f"Time 1: {captain1_name} ({len(team1_players)} jogadores)")
    logger.debug(f"Time 2: {captain2_name} ({len(team2_players)} jogadores)")
    logger.debug(f"Mapa: {map_name}")
    
    return json_str


def escape_json_for_rcon(json_str: str) -> str:
    """
    Escapa o JSON para envio via RCON.
    
    RCON precisa que aspas duplas sejam escapadas com backslash.
    
    Args:
        json_str: String JSON original
    
    Returns:
        String JSON escapada para RCON
    """
    # Escapa aspas duplas
    escaped = json_str.replace('"', '\\"')
    
    # Remove quebras de linha (opcional, mas recomendado para RCON)
    escaped = escaped.replace('\n', ' ').replace('\r', '')
    
    # Remove espaços extras
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
        # Verifica se já é string ou se é dicionário
        if isinstance(config_data, dict):
            f.write(json.dumps(config_data, indent=4, ensure_ascii=False))
        else:
            f.write(str(config_data))
    
    logger.info(f"📁 Match config salvo em: {filename}")
    return filename


# ==================== EXEMPLO DE USO ====================

def example_usage():
    """Exemplo de como usar as funções"""
    
    # Dados do mix
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
    
    # Gera config
    config_json = generate_matchzy_config(
        match_id=match_id,
        captain1_name=captain1,
        captain2_name=captain2,
        team1_players=team1,
        team2_players=team2,
        map_name="de_mirage"
    )
    
    # Escapa para RCON
    escaped_json = escape_json_for_rcon(config_json)
    
    # Comando RCON final
    rcon_command = f'matchzy_loadmatch_url "{escaped_json}"'
    
    print("=== COMANDO RCON ===")
    print(rcon_command)
    
    # Salva arquivo (opcional)
    save_match_config_file(config_json, match_id)
    
    return rcon_command


if __name__ == "__main__":
    example_usage()
