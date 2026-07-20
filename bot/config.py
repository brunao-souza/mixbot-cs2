import os
from dotenv import load_dotenv
from pathlib import Path
from zoneinfo import ZoneInfo

# Carrega o arquivo .env
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = str(raw).strip().strip('"').strip("'")
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default

 
def _env_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    cleaned = str(raw).strip().strip('"').strip("'")
    if not cleaned:
        return []
    values: list[int] = []
    for part in cleaned.split(","):
        item = part.strip().strip('"').strip("'")
        if item.isdigit():
            values.append(int(item))
    return values


def _env_str_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    cleaned = str(raw).strip().strip('"').strip("'")
    if not cleaned:
        return []
    values: list[str] = []
    for part in cleaned.split(","):
        item = str(part).strip()
        if item:
            values.append(item)
    return values


def _env_float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = str(raw).strip().strip('"').strip("'")
    if not cleaned:
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def _env_timezone(name: str, default: str) -> ZoneInfo:
    raw = os.getenv(name, default)
    cleaned = str(raw).strip().strip('"').strip("'") or default
    try:
        return ZoneInfo(cleaned)
    except Exception:
        return ZoneInfo(default)

# ================= BOT E APIs =================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN') or DISCORD_TOKEN
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
FACEIT_API_KEY = os.getenv('FACEIT_API_KEY')

# ================= CS2 BRIDGE (DISCORD <-> CS2) =================
RCON_HOST = os.getenv('RCON_HOST') or os.getenv('POOL_S1_HOST', '')
RCON_PORT = _env_int('RCON_PORT', _env_int('POOL_S1_PORT', 0))
RCON_PASSWORD = os.getenv('RCON_PASSWORD') or os.getenv('POOL_S1_RCON_PASSWORD', '')
DISCORD_ADMIN_ALERT_CHANNEL_ID = _env_int('DISCORD_ADMIN_ALERT_CHANNEL_ID', 0)
DISCORD_COMPLETER_ALERT_CHANNEL_ID = _env_int('DISCORD_COMPLETER_ALERT_CHANNEL_ID', 0)
CS2_BRIDGE_INBOX_CHANNEL_ID = _env_int('CS2_BRIDGE_INBOX_CHANNEL_ID', 0)
DISCORD_CHAT_RELAY_CHANNEL_ID = _env_int('DISCORD_CHAT_RELAY_CHANNEL_ID', _env_int('CANAL_MONITOR_ID', 0))
DISCORD_CALLADMIN_ROLE_ID = _env_int('DISCORD_CALLADMIN_ROLE_ID', 0)
DISCORD_COMPLETER_ROLE_ID = _env_int('DISCORD_COMPLETER_ROLE_ID', 0)
CS2_SHARED_KEY = os.getenv('CS2_SHARED_KEY') or os.getenv('DISCORD_BRIDGE_API_KEY', '')
MATCHZY_WEBHOOK_KEY = os.getenv('MATCHZY_WEBHOOK_KEY', '')
MATCHZY_ADMIN_STEAMID64 = (os.getenv('MATCHZY_ADMIN_STEAMID64', '') or '').strip()

# ================= BANCO DE DADOS =================
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'autocommit': True
}

# ================= SYNC BRIDGE — WEBAPP DB (opcional) =================
# Quando configurado, o bot espelha resultados de ranking para o banco do ProjectMix.
# Se não configurado, o sync é desativado silenciosamente.
WEBAPP_DB_CONFIG = {
    'host': os.getenv('WEBAPP_DB_HOST', os.getenv('DB_HOST', '127.0.0.1')),
    'port': int(os.getenv('WEBAPP_DB_PORT', os.getenv('DB_PORT', 3306))),
    'user': os.getenv('WEBAPP_DB_USER', ''),
    'password': os.getenv('WEBAPP_DB_PASSWORD', ''),
    'database': os.getenv('WEBAPP_DB_NAME', ''),
}

# ================= CANAIS GERAIS (GLOBAIS) =================
CANAL_FILA_ID = int(os.getenv('CANAL_FILA_ID', 0))
CANAL_LOGS_ID = int(os.getenv('CANAL_LOGS_ID', 0))
CANAL_RANKING_ID = int(os.getenv('CANAL_RANKING_ID', 0))
CANAL_GERAL_ID = int(os.getenv('CANAL_GERAL_ID', 0))
CANAL_RESUMO_ID = int(os.getenv('CANAL_RESUMO_ID', 0))
CANAL_SKINS_ID = int(os.getenv('CANAL_SKINS_ID', 0))
SKINS_ACTIVITY_APPLICATION_ID = int(os.getenv('SKINS_ACTIVITY_APPLICATION_ID', 0))
CANAL_STEAMID_ID = int(os.getenv('CANAL_STEAMID_ID', 0))
CANAL_MONITOR_ID = int(os.getenv('CANAL_MONITOR_ID', 0)) 
CANAL_AJUDA_ID = int(os.getenv('CANAL_AJUDA_ID', 0))
CANAL_APOIO_ID = int(os.getenv('CANAL_APOIO_ID', 0))
CANAL_DENUNCIAS_ID = int(os.getenv('CANAL_DENUNCIAS_ID', 0))
CANAL_PUNICOES_ID = int(os.getenv('CANAL_PUNICOES_ID', 0))
CANAL_BOAS_VINDAS_ID = int(os.getenv('CANAL_BOAS_VINDAS_ID', 0))
CANAL_PAINEL_TICKETS_ID = int(os.getenv('CANAL_PAINEL_TICKETS_ID', 0))
CANAL_PAINEL_PUNICOES_ID = int(os.getenv('CANAL_PAINEL_PUNICOES_ID', 0))
SALA_PROXIMO_ID = int(os.getenv('SALA_PROXIMO_ID', 0))
SALA_SAIDA_ID = int(os.getenv('SALA_SAIDA_ID', 0))
TORNEIO_CATEGORY_ID = int(os.getenv('TORNEIO_CATEGORY_ID', 0))
TORNEIO_PICKS_BANS_CHANNEL_ID = int(os.getenv('TORNEIO_PICKS_BANS_CHANNEL_ID', 0))
TOURN_GROUPS_CHANNEL_ID = int(os.getenv('TOURN_GROUPS_CHANNEL_ID', 0))
RETAKE_FLAG = '🇫🇷'
MEMBER_ROLE_ID = int(os.getenv('MEMBER_ROLE_ID', 0))
MEMBER_ROLE_NAME = os.getenv('MEMBER_ROLE_NAME', 'Membro')
SFTP_HOST = os.getenv('SFTP_HOST', '')
SFTP_PORT = int(os.getenv('SFTP_PORT', 22))
SFTP_USER = os.getenv('SFTP_USER', '')
SFTP_PRIVATE_KEY = os.getenv('SFTP_PRIVATE_KEY', '')
SFTP_PASSWORD = os.getenv('SFTP_PASSWORD', '')
SFTP_TIMEOUT = int(os.getenv('SFTP_TIMEOUT', 15))
SFTP_DEBUG = _env_bool('SFTP_DEBUG', False)
DEMO_UPLOAD_URL = os.getenv('DEMO_UPLOAD_URL', '')
DEMO_DOWNLOAD_URL = os.getenv('DEMO_DOWNLOAD_URL', '')
WHATSAPP_GROUP_URL = os.environ.get('WHATSAPP_GROUP_URL', '')
STEAM_GROUP_URL = os.environ.get('STEAM_GROUP_URL', '')
INSTAGRAM_URL = os.environ.get('INSTAGRAM_URL', '')
TOURN_FTP_HOST = os.getenv('TOURN_FTP_HOST', '')
TOURN_FTP_USER = os.getenv('TOURN_FTP_USER', '')
TOURN_FTP_PASS = os.getenv('TOURN_FTP_PASS', '')
TOURN_FTP_PORT = int(os.getenv('TOURN_FTP_PORT', 21))
TOURN_FTP_TLS = _env_bool('TOURN_FTP_TLS', False)
TOURN_FTP_TIMEOUT = int(os.getenv('TOURN_FTP_TIMEOUT', 20))
TOURN_REMOTE_CFG_DIR = os.getenv('TOURN_REMOTE_CFG_DIR', 'cfg/MatchZy')
TOURN_SCHEDULE_CHANNEL_ID = int(os.getenv('TOURN_SCHEDULE_CHANNEL_ID', 0))
TICKET_CATEGORY_ID = int(os.getenv('TICKET_CATEGORY_ID', 0))
TICKET_ARCHIVE_CATEGORY_ID = int(os.getenv('TICKET_ARCHIVE_CATEGORY_ID', 0))
STAFF_ROLE_IDS = _env_int_list('STAFF_ROLE_IDS')
VIP_ROLE_IDS = _env_int_list('VIP_ROLE_IDS')
QUEUE_PRIORITY_TIMEZONE = _env_timezone('QUEUE_PRIORITY_TIMEZONE', 'Europe/Lisbon')
QUEUE_PRIORITY_RESET_HOUR = max(0, min(23, _env_int('QUEUE_PRIORITY_RESET_HOUR', 8)))

# ================= MATCHZY LOCAL RUNTIME =================
MATCHZY_BASE_DIR = os.getenv('MATCHZY_BASE_DIR', '/home/cs2/game/csgo/cfg/MatchZy').strip()
MATCHZY_MATCHES_SUBDIR = os.getenv('MATCHZY_MATCHES_SUBDIR', 'matches').strip().strip('/\\') or 'matches'
MATCHZY_LOAD_BASE = os.getenv('MATCHZY_LOAD_BASE', 'cfg/MatchZy').strip().strip('/\\') or 'cfg/MatchZy'
RUNTIME_SUDO_USER = os.getenv('RUNTIME_SUDO_USER', 'cs2').strip() or 'cs2'
RUNTIME_BOOT_DELAY_SECONDS = _env_float('RUNTIME_BOOT_DELAY_SECONDS', 2.0)
RUNTIME_SUBPROCESS_TIMEOUT = _env_int('RUNTIME_SUBPROCESS_TIMEOUT', 30)
RUNTIME_START_TIMEOUT_SECONDS = _env_int('RUNTIME_START_TIMEOUT_SECONDS', 30)
RUNTIME_ONLINE_GRACE_SECONDS = _env_int(
    'RUNTIME_ONLINE_GRACE_SECONDS',
    _env_int('RUNTIME_TMUX_ONLINE_GRACE_SECONDS', 30),
)
RUNTIME_TMUX_ONLINE_GRACE_SECONDS = RUNTIME_ONLINE_GRACE_SECONDS
RUNTIME_USE_RCON_LOAD_ONLY = _env_bool('RUNTIME_USE_RCON_LOAD_ONLY', True)

# ================= POOL GLOBAL DE SERVIDORES =================
POOL_SERVER_IDS = _env_int_list('POOL_SERVER_IDS') or [1, 2, 3, 4, 5]
POOL_SERVERS = {}
_DEFAULT_SERVER_FLAGS = {
    1: "🇫🇷",
    2: "🇩🇪",
    3: "🇪🇸",
    4: "🇮🇹",
    5: "🇵🇹",
}

for _sid in POOL_SERVER_IDS:
    _slot_modes = [m.strip().lower() for m in _env_str_list(f'POOL_S{_sid}_MODES', 'mix,tourney')]
    _slot_enabled = _env_bool(f'POOL_S{_sid}_ENABLED', True)
    _runtime_id = os.getenv(f'POOL_S{_sid}_RUNTIME_ID', f'mix{_sid}').strip() or f'mix{_sid}'
    _picks_text = _env_int(f'POOL_S{_sid}_PICKS_TEXT_ID', 0)
    _picks_voice = _env_int(f'POOL_S{_sid}_PICKS_VOICE_ID', 0)
    _team1_voice = _env_int(f'POOL_S{_sid}_TEAM1_VOICE_ID', 0)
    _team2_voice = _env_int(f'POOL_S{_sid}_TEAM2_VOICE_ID', 0)
    _category_id = _env_int(f'POOL_S{_sid}_CATEGORY_ID', 0)
    _slot = {
        "slot_id": _sid,
        "enabled": _slot_enabled,
        "runtime_id": _runtime_id,
        "start_script": os.getenv(
            f'POOL_S{_sid}_START_SCRIPT',
            f'/home/cs2/scripts/start_mix{_sid}.sh',
        ).strip(),
        "stop_script": os.getenv(f'POOL_S{_sid}_STOP_SCRIPT', '').strip(),
        "service_name": os.getenv(f'POOL_S{_sid}_SERVICE_NAME', '').strip(),
        "tmux_session": os.getenv(f'POOL_S{_sid}_TMUX_SESSION', f'mix{_sid}').strip() or f'mix{_sid}',
        "host": os.getenv(f'POOL_S{_sid}_HOST', '').strip(),
        "port": _env_int(f'POOL_S{_sid}_PORT', 0),
        "gotv_port": _env_int(f'POOL_S{_sid}_GOTV_PORT', 0),
        "modes": [m for m in _slot_modes if m in ("mix", "tourney")],
        "lobby_name": os.getenv(f'POOL_S{_sid}_NAME', f'SERVER #{_sid:02d}').strip() or f'SERVER #{_sid:02d}',
        "lobby_flag": os.getenv(f'POOL_S{_sid}_FLAG', _DEFAULT_SERVER_FLAGS.get(_sid, "🏳️")).strip()
        or _DEFAULT_SERVER_FLAGS.get(_sid, "🏳️"),
        "lobby_channels": {
            "picks_text": _picks_text,
            "picks_voice": _picks_voice,
            "team1_voice": _team1_voice,
            "team2_voice": _team2_voice,
            "category": _category_id,
        },
        "rcon_password": os.getenv(f'POOL_S{_sid}_RCON_PASSWORD', '').strip(),
    }
    if not _slot["modes"]:
        _slot["modes"] = ["mix", "tourney"]
    POOL_SERVERS[_runtime_id] = _slot

# ================= SERVIDORES DE TORNEIO =================
TOURNAMENT_SERVERS = {
    "tserver1": {
        "active": _env_bool("TS1_ACTIVE", False),
        "name": os.getenv("TS1_NAME", "TOURN SERVER #01"),
        "cs2": {
            "host": os.getenv("TS1_HOST", ""),
            "port": int(os.getenv("TS1_PORT", 0)),
            "rcon_password": os.getenv("TS1_RCON_PASSWORD", ""),
            "gotv_port": int(os.getenv("TS1_GOTV_PORT", 0)),
        },
    },
    "tserver2": {
        "active": _env_bool("TS2_ACTIVE", False),
        "name": os.getenv("TS2_NAME", "TOURN SERVER #02"),
        "cs2": {
            "host": os.getenv("TS2_HOST", ""),
            "port": int(os.getenv("TS2_PORT", 0)),
            "rcon_password": os.getenv("TS2_RCON_PASSWORD", ""),
            "gotv_port": int(os.getenv("TS2_GOTV_PORT", 0)),
        },
    },
    "tserver3": {
        "active": _env_bool("TS3_ACTIVE", False),
        "name": os.getenv("TS3_NAME", "TOURN SERVER #03"),
        "cs2": {
            "host": os.getenv("TS3_HOST", ""),
            "port": int(os.getenv("TS3_PORT", 0)),
            "rcon_password": os.getenv("TS3_RCON_PASSWORD", ""),
            "gotv_port": int(os.getenv("TS3_GOTV_PORT", 0)),
        },
    },
    "tserver4": {
        "active": _env_bool("TS4_ACTIVE", False),
        "name": os.getenv("TS4_NAME", "TOURN SERVER #04"),
        "cs2": {
            "host": os.getenv("TS4_HOST", ""),
            "port": int(os.getenv("TS4_PORT", 0)),
            "rcon_password": os.getenv("TS4_RCON_PASSWORD", ""),
            "gotv_port": int(os.getenv("TS4_GOTV_PORT", 0)),
        },
    },
    "tserver5": {
        "active": _env_bool("TS5_ACTIVE", False),
        "name": os.getenv("TS5_NAME", "TOURN SERVER #05"),
        "cs2": {
            "host": os.getenv("TS5_HOST", ""),
            "port": int(os.getenv("TS5_PORT", 0)),
            "rcon_password": os.getenv("TS5_RCON_PASSWORD", ""),
            "gotv_port": int(os.getenv("TS5_GOTV_PORT", 0)),
        },
    },
}

# ================= SERVIDORES (ARENAS) =================
SERVERS = {}
for _runtime_id, _slot in sorted(POOL_SERVERS.items(), key=lambda item: int(item[1].get("slot_id") or 0)):
    _sid = int(_slot.get("slot_id") or 0)
    _mix_mode = "mix" in (_slot.get("modes") or [])
    _channels = dict(_slot.get("lobby_channels") or {})
    _has_lobby_channels = all(int(_channels.get(k) or 0) > 0 for k in ("picks_text", "picks_voice", "team1_voice", "team2_voice"))
    _server_key = f"server{_sid}"
    SERVERS[_server_key] = {
        "active": bool(_slot.get("enabled", True) and _mix_mode and _has_lobby_channels),
        "name": str(_slot.get("lobby_name") or f"SERVER #{_sid:02d}"),
        "flag": str(_slot.get("lobby_flag") or _DEFAULT_SERVER_FLAGS.get(_sid, "🏳️")),
        "runtime_id": _runtime_id,
        "cs2": {
            "host": str(_slot.get("host") or ""),
            "port": int(_slot.get("port") or 0),
            "rcon_password": str(_slot.get("rcon_password") or ""),
            "gotv_port": int(_slot.get("gotv_port") or 0),
        },
        "ftp": {
            "host": "",
            "user": "",
            "password": "",
            "port": 21,
        },
        "channels": {
            "picks_text": int(_channels.get("picks_text") or 0),
            "picks_voice": int(_channels.get("picks_voice") or 0),
            "team1_voice": int(_channels.get("team1_voice") or 0),
            "team2_voice": int(_channels.get("team2_voice") or 0),
            "category": int(_channels.get("category") or 0),
        },
    }

# ================= MAPAS E OUTRAS CONFIGS =================
TIME_1_NAME = "Time 1"
TIME_2_NAME = "Time 2"

MAPS_BASE = [
    "Mirage", "Inferno", "Nuke", "Ancient",
    "Anubis", "Vertigo", "Overpass",
    "Dust2", "Train", "Cache", "Cobblestone"
]

MAP_NAME_CONVERT = {
    "Mirage": "de_mirage",
    "Inferno": "de_inferno",
    "Nuke": "de_nuke",
    "Ancient": "de_ancient",
    "Anubis": "de_anubis",
    "Vertigo": "de_vertigo",
    "Overpass": "de_overpass",
    "Dust2": "de_dust2",
    "Train": "de_train",
    "Cache": "de_cache",
    "Cobblestone": "workshop/3329387648/de_cbble_d"
}

MAP_IMAGES = {
    "inferno": "https://media.discordapp.net/attachments/1452985230565834804/1452985306147192842/inferno.png",
    "train": "https://media.discordapp.net/attachments/1452985230565834804/1452985307241906359/train.png",
    "mirage": "https://media.discordapp.net/attachments/1452985230565834804/1452985306457575484/mirage.png",
    "dust2": "https://media.discordapp.net/attachments/1452985230565834804/1452985305840746516/dust2.png",
    "overpass": "https://media.discordapp.net/attachments/1452985230565834804/1452985307942355156/Overpass.png",
    "nuke": "https://media.discordapp.net/attachments/1452985230565834804/1452985306780270653/nuke.png",
    "vertigo": "https://media.discordapp.net/attachments/1452985230565834804/1452985307610746921/Vertigo.png",
    "ancient": "https://cdn.discordapp.com/attachments/1452985230565834804/1452985305140559955/ancient.png",
    "anubis": "https://media.discordapp.net/attachments/1452985230565834804/1452985305505468429/anubis.png",
    "cache": "https://cdn.discordapp.com/attachments/1452985230565834804/1459527019313238076/cache.png", 
    "de_cache_d": "https://cdn.discordapp.com/attachments/1452985230565834804/1459527019313238076/cache.png", 
    "cbble": "https://cdn.discordapp.com/attachments/1452985230565834804/1459526683630506024/cobble.png",
    "de_cbble_d": "https://cdn.discordapp.com/attachments/1452985230565834804/1459526683630506024/cobble.png",
    "cobblestone": "https://cdn.discordapp.com/attachments/1452985230565834804/1459526683630506024/cobble.png"
}

# Timeouts
MIX_ACCEPT_TIMEOUT = 60
CAPTAIN_VOTE_TIMEOUT = 40
PICK_TIMEOUT = 30
MAP_VETO_TIMEOUT = 30
MAP_VETO_FINAL_TIMEOUT = 15
MATCH_CHECK_INTERVAL = 10
MATCH_POST_ACTION_DELAY = 60  # segundos para reset/changelevel apos fim da partida
# Data de corte para backfill de partidas no startup (YYYY-MM-DD).
# Usado para evitar retroprocessar partidas de seasons antigas.
SEASON_START_DATE = os.getenv('SEASON_START_DATE', '2026-03-01').strip()

# Ranking Points
POINTS_WIN_BASE = 30
POINTS_LOSS_BASE = -50
ADR_BONUS_MAX = 20

# ==============================================================================
# RETROCOMPATIBILIDADE (FIX CRÃTICO)
# Cria variÃ¡veis globais apontando para o Server 1 para que o main.py nÃ£o quebre
# ==============================================================================
try:
    # Apelidos para o primeiro lobby ativo/configurado.
    _primary_key = "server1" if "server1" in SERVERS else next(iter(SERVERS.keys()))
    _primary_server = SERVERS[_primary_key]
    SALA_MIX_ID = _primary_server["channels"]["picks_voice"]
    CANAL_PICKS_ID = _primary_server["channels"]["picks_text"]
    SALA_TIME1_ID = _primary_server["channels"]["team1_voice"]
    SALA_TIME2_ID = _primary_server["channels"]["team2_voice"]
    
    # Apelidos para Configs de ConexÃ£o Antigas
    CS2_CONFIG = _primary_server["cs2"]
    FTP_CONFIG = _primary_server.get("ftp", {})
except (KeyError, StopIteration):
    print("Aviso: nenhum servidor de lobby configurado no .env")
    SALA_MIX_ID = 0
    CANAL_PICKS_ID = 0
    SALA_TIME1_ID = 0
    SALA_TIME2_ID = 0
    CS2_CONFIG = {}
    FTP_CONFIG = {}

