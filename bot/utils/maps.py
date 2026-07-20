_WORKSHOP_MAP_DISPLAY_ALIASES = {
    "cache": "de_cache",
    "de_cache": "de_cache",
    "de_cache_d": "de_cache",
    "cble": "de_cobblestone",
    "cbble": "de_cobblestone",
    "cobblestone": "de_cobblestone",
    "de_cbble": "de_cobblestone",
    "de_cbble_d": "de_cobblestone",
    "de_cobblestone": "de_cobblestone",
}

_WORKSHOP_MAP_IMAGE_KEYS = {
    "cache": "cache",
    "de_cache": "cache",
    "de_cache_d": "cache",
    "cble": "cobblestone",
    "cbble": "cobblestone",
    "cobblestone": "cobblestone",
    "de_cbble": "cobblestone",
    "de_cbble_d": "cobblestone",
    "de_cobblestone": "cobblestone",
}


def normalize_map_key(map_value) -> str:
    map_raw = str(map_value or "unknown").strip()
    if not map_raw:
        return "unknown"

    map_key = map_raw.split("/")[-1].strip().lower()
    if map_key in _WORKSHOP_MAP_IMAGE_KEYS:
        return _WORKSHOP_MAP_IMAGE_KEYS[map_key]

    if map_key.startswith("de_"):
        map_key = map_key[3:]
    if map_key.endswith("_d"):
        map_key = map_key[:-2]
    return map_key or "unknown"


def format_monitor_map_name(map_value) -> str:
    map_raw = str(map_value or "unknown").strip()
    if not map_raw:
        return "Unknown"

    map_key = map_raw.split("/")[-1].strip().lower()
    if map_key in _WORKSHOP_MAP_DISPLAY_ALIASES:
        return _WORKSHOP_MAP_DISPLAY_ALIASES[map_key]

    normalized = normalize_map_key(map_key)
    return normalized.capitalize()
