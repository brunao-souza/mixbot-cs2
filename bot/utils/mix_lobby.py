from typing import Dict, Optional, Set


def pick_free_lobby_server(
    servers: Dict[str, dict],
    sessions: Dict[str, dict],
    voice_member_counts: Dict[int, int],
    available_runtime_ids: Optional[Set[str]] = None,
) -> Optional[str]:
    normalized_runtime_ids = (
        {str(runtime_id).strip().lower() for runtime_id in available_runtime_ids if str(runtime_id).strip()}
        if available_runtime_ids is not None
        else None
    )

    for s_id in sorted(servers.keys()):
        server = servers.get(s_id) or {}
        if not server.get("active"):
            continue

        runtime_id = str(server.get("runtime_id") or "").strip().lower()
        if normalized_runtime_ids is not None and runtime_id and runtime_id not in normalized_runtime_ids:
            continue

        session = sessions.get(s_id, {})
        if session.get("active"):
            continue

        picks_voice_id = int(((server.get("channels") or {}).get("picks_voice")) or 0)
        if voice_member_counts.get(picks_voice_id, 0) > 0:
            continue

        return s_id

    return None
