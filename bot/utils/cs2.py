import asyncio
from typing import Optional, Dict, Any

from loguru import logger
from rcon.source import Client


def _one_line_error(err: Exception, max_len: int = 240) -> str:
    text = str(err or "").strip()
    if not text:
        return "sem detalhes"
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = first_line or text.replace("\n", " ").strip()
    if len(first_line) > max_len:
        return first_line[:max_len] + "..."
    return first_line


async def send_rcon(server_config: Dict[str, Any], command: str, log_errors: bool = True) -> Optional[str]:
    """
    Envia um comando RCON para um servidor especifico.

    Args:
        server_config: Dicionario do servidor (SERVERS['server1'], etc)
        command: Comando para executar (ex: 'mp_restartgame 1')

    Returns:
        Resposta do servidor ou None em caso de erro.
    """
    try:
        host = server_config["cs2"]["host"]
        port = int(server_config["cs2"]["port"])
        password = server_config["cs2"]["rcon_password"]
    except KeyError as e:
        if log_errors:
            logger.error(f"RCON config invalida: faltando chave {e}")
        return None

    if not isinstance(host, str) or not host.strip():
        if log_errors:
            logger.error("RCON config invalida: cs2.host ausente ou invalido")
        return None

    if not isinstance(password, str) or not password:
        if log_errors:
            logger.error(f"RCON config invalida ({host}:{port}): cs2.rcon_password ausente")
        return None

    if port <= 0:
        if log_errors:
            logger.error(f"RCON config invalida ({host}): cs2.port invalido ({port})")
        return None

    try:
        response = await asyncio.to_thread(_execute_rcon_internal, host, port, password, command)
        return response
    except Exception as e:
        if log_errors:
            logger.error(f"Erro RCON ({host}:{port}): {_one_line_error(e)}")
        return None


def _execute_rcon_internal(host, port, password, command):
    """Executa o client RCON bloqueante em thread separada."""
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
        logger.critical("Tentativa de usar RCON legado (Global).")
        logger.critical("Atualize para: await send_rcon(server_config, command)")
        return None


rcon = LegacyRcon()
