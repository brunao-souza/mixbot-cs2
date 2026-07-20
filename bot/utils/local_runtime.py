from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any, Dict

from loguru import logger

from bot.config import (
    MATCHZY_BASE_DIR,
    MATCHZY_MATCHES_SUBDIR,
    MATCHZY_LOAD_BASE,
    RUNTIME_SUDO_USER,
    RUNTIME_SUBPROCESS_TIMEOUT,
    RUNTIME_START_TIMEOUT_SECONDS,
)

_FORCE_SUDO_MATCH_WRITE = False
_REPORTED_LOCAL_WRITE_PERMISSION = False
_SUDO_WRITE_LEGACY_ONLY = False


def _join_posix(*parts: str) -> str:
    cleaned = [str(p).strip("/\\") for p in parts if str(p).strip("/\\")]
    return "/".join(cleaned)


def build_match_filename(match_id: int) -> str:
    return f"match{int(match_id)}.json"


def build_match_absolute_path(match_id: int) -> str:
    return os.path.join(MATCHZY_BASE_DIR, MATCHZY_MATCHES_SUBDIR, build_match_filename(match_id))


def build_match_load_path(match_id: int) -> str:
    return _join_posix(MATCHZY_LOAD_BASE, MATCHZY_MATCHES_SUBDIR, build_match_filename(match_id))


def _write_match_json_via_sudo(final_path: str, payload_json: str) -> None:
    global _SUDO_WRITE_LEGACY_ONLY
    tmp_path = final_path + ".tmp"
    if _SUDO_WRITE_LEGACY_ONLY:
        _write_match_json_via_sudo_legacy(final_path, tmp_path, payload_json)
        return

    quoted_dir = shlex.quote(os.path.dirname(final_path))
    quoted_tmp = shlex.quote(tmp_path)
    quoted_final = shlex.quote(final_path)
    script = f"mkdir -p {quoted_dir} && cat > {quoted_tmp} && mv -f {quoted_tmp} {quoted_final}"
    try:
        run_as_runtime_user(["bash", "-lc", script], check=True, input_text=payload_json)
    except RuntimeError:
        # On hosts with restricted sudoers, /usr/bin/bash may not be authorized.
        # Falls back to individual commands (mkdir/tee/mv), which are normally whitelisted.
        _SUDO_WRITE_LEGACY_ONLY = True
        logger.info(
            "LOCAL_MATCH: writer=sudo(shell) unavailable; using writer=sudo(legacy: mkdir/tee/mv)."
        )
        _write_match_json_via_sudo_legacy(final_path, tmp_path, payload_json)


def _write_match_json_via_sudo_legacy(final_path: str, tmp_path: str, payload_json: str) -> None:
    run_as_runtime_user(["mkdir", "-p", os.path.dirname(final_path)], check=True)
    run_as_runtime_user(["tee", tmp_path], check=True, input_text=payload_json)
    run_as_runtime_user(["mv", "-f", tmp_path, final_path], check=True)


def write_match_json_atomic(match_id: int, payload: Dict[str, Any]) -> str:
    global _FORCE_SUDO_MATCH_WRITE, _REPORTED_LOCAL_WRITE_PERMISSION
    final_path = build_match_absolute_path(match_id)
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp_path = final_path + ".tmp"

    if _FORCE_SUDO_MATCH_WRITE:
        _write_match_json_via_sudo(final_path, payload_json)
        logger.info(f"LOCAL_MATCH: json saved writer=sudo match={match_id} path={final_path}")
        return final_path

    try:
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload_json)
        os.replace(tmp_path, final_path)
        logger.info(f"LOCAL_MATCH: json saved writer=local match={match_id} path={final_path}")
        return final_path
    except PermissionError:
        _FORCE_SUDO_MATCH_WRITE = True
        # On some hosts, the bot lacks permission on this path by design; avoids repetitive warning.
        if not _REPORTED_LOCAL_WRITE_PERMISSION:
            logger.info(
                f"LOCAL_MATCH: writer=local no permission on {final_path}; "
                f"switching to writer=sudo ({RUNTIME_SUDO_USER}) for future matches."
            )
            _REPORTED_LOCAL_WRITE_PERMISSION = True
        _write_match_json_via_sudo(final_path, payload_json)
        logger.info(f"LOCAL_MATCH: json saved writer=sudo match={match_id} path={final_path}")
    return final_path


def run_as_runtime_user(
    args: list[str],
    *,
    check: bool = True,
    timeout: int | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n", "-u", RUNTIME_SUDO_USER] + list(args)
    logger.debug(f"RUNTIME_CMD: {' '.join(cmd)}")
    cp = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout or RUNTIME_SUBPROCESS_TIMEOUT,
    )
    if check and cp.returncode != 0:
        stderr = (cp.stderr or "").strip()
        stdout = (cp.stdout or "").strip()
        details = stderr or stdout or "no details"
        low_details = details.lower()
        sudoers_hint = ""
        if any(
            token in low_details
            for token in (
                "password is required",
                "a terminal is required",
                "not allowed to execute",
                "permission denied",
                "nopasswd",
                "sudoers",
            )
        ):
            sudoers_hint = " Check sudoers (NOPASSWD) for the bot user."
        raise RuntimeError(
            f"Failed to execute runtime command ({' '.join(cmd)}): {details}.{sudoers_hint}"
        )
    return cp


def start_runtime_server(start_script: str) -> None:
    start_script = str(start_script or "").strip()
    if not start_script:
        raise RuntimeError("Runtime start script not configured.")
    run_as_runtime_user(
        [start_script],
        check=True,
        timeout=RUNTIME_START_TIMEOUT_SECONDS,
    )


def is_runtime_service_active(service_name: str) -> bool:
    service_name = str(service_name or "").strip()
    if not service_name:
        return False
    cp = run_as_runtime_user(
        ["systemctl", "is-active", service_name],
        check=False,
    )
    state = (cp.stdout or cp.stderr or "").strip().lower()
    return cp.returncode == 0 or state == "active"


def runtime_is_online(service_name: str = "", tmux_session: str = "") -> bool:
    if is_runtime_service_active(service_name):
        return True
    tmux_session = str(tmux_session or "").strip()
    if not tmux_session:
        return False
    return tmux_has_session(tmux_session)


def tmux_has_session(tmux_session: str) -> bool:
    cp = run_as_runtime_user(
        ["tmux", "has-session", "-t", str(tmux_session)],
        check=False,
    )
    return cp.returncode == 0


def load_match_in_tmux(tmux_session: str, match_id: int) -> str:
    load_cmd = f"matchzy_loadmatch {build_match_load_path(match_id)}"
    run_as_runtime_user(
        ["tmux", "send-keys", "-t", str(tmux_session), load_cmd, "C-m"],
        check=True,
    )
    logger.info(f"LOCAL_MATCH: load sent session={tmux_session} cmd={load_cmd}")
    return load_cmd


def stop_runtime_server(
    *,
    stop_script: str = "",
    service_name: str = "",
    tmux_session: str = "",
) -> bool:
    stop_script = str(stop_script or "").strip()
    service_name = str(service_name or "").strip()
    tmux_session = str(tmux_session or "").strip()

    if stop_script:
        run_as_runtime_user(
            [stop_script],
            check=True,
            timeout=RUNTIME_START_TIMEOUT_SECONDS,
        )
        return True

    if service_name:
        cp = run_as_runtime_user(
            ["systemctl", "stop", service_name],
            check=False,
            timeout=RUNTIME_START_TIMEOUT_SECONDS,
        )
        if cp.returncode == 0:
            return True
        details = (cp.stderr or cp.stdout or "").strip().lower()
        if "not loaded" in details or "not found" in details or "inactive" in details:
            return False
        raise RuntimeError(
            f"Failed to stop service '{service_name}': {cp.stderr or cp.stdout}"
        )

    if tmux_session:
        return stop_tmux_session(tmux_session)

    raise RuntimeError("No stop mechanism configured for the runtime.")


def stop_tmux_session(tmux_session: str) -> bool:
    cp = run_as_runtime_user(
        ["tmux", "kill-session", "-t", str(tmux_session)],
        check=False,
    )
    if cp.returncode == 0:
        return True

    stderr = (cp.stderr or "").lower()
    if "can't find session" in stderr or "failed to connect to server" in stderr:
        return False
    raise RuntimeError(f"Failed to stop tmux '{tmux_session}': {cp.stderr or cp.stdout}")
