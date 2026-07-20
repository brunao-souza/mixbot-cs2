import asyncio
from typing import Optional, Dict, Any

from loguru import logger
from rcon.source import Client


def _one_line_error(err: Exception, max_len: int = 240) -> str:
    text = str(err or "").strip()
    if not text:
        return "no details"
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = first_line or text.replace("\n", " ").strip()
    if len(first_line) > max_len:
        return first_line[:max_len] + "..."
    return first_line


async def send_rcon(server_config: Dict[str, Any], command: str, log_errors: bool = True) -> Optional[str]:
    """
    Sends an RCON command to a specific server.

    Args:
        server_config: Server dictionary (SERVERS['server1'], etc)
        command: Command to execute (e.g., 'mp_restartgame 1')

    Returns:
        Server response or None on error.
    """
    try:
        host = server_config["cs2"]["host"]
        port = int(server_config["cs2"]["port"])
        password = server_config["cs2"]["rcon_password"]
    except KeyError as e:
        if log_errors:
            logger.error(f"Invalid RCON config: missing key {e}")
        return None

    if not isinstance(host, str) or not host.strip():
        if log_errors:
            logger.error("Invalid RCON config: cs2.host missing or invalid")
        return None

    if not isinstance(password, str) or not password:
        if log_errors:
            logger.error(f"Invalid RCON config ({host}:{port}): cs2.rcon_password missing")
        return None

    if port <= 0:
        if log_errors:
            logger.error(f"Invalid RCON config ({host}): cs2.port invalid ({port})")
        return None

    try:
        response = await asyncio.to_thread(_execute_rcon_internal, host, port, password, command)
        return response
    except Exception as e:
        if log_errors:
            logger.error(f"RCON error ({host}:{port}): {_one_line_error(e)}")
        return None


def _execute_rcon_internal(host, port, password, command):
    """Runs the blocking RCON client in a separate thread."""
    try:
        with Client(host, port, passwd=password, timeout=5) as client:
            try:
                return client.run(command)
            except UnicodeDecodeError as e:
                raw = getattr(e, "object", None)
                if isinstance(raw, (bytes, bytearray)):
                    return raw.decode("latin-1", errors="replace")
                return None
    except Exception as e:
        raise e


class LegacyRcon:
    async def command(self, cmd: str) -> Optional[str]:
        logger.critical("Attempted to use legacy RCON (Global).")
        logger.critical("Update to: await send_rcon(server_config, command)")
        return None


rcon = LegacyRcon()
