from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

from bot.config import (
    POOL_SERVERS,
    RUNTIME_BOOT_DELAY_SECONDS,
    RUNTIME_ONLINE_GRACE_SECONDS,
    RUNTIME_USE_RCON_LOAD_ONLY,
)
from bot.database import (
    acquire_named_lock,
    bind_match_runtime_server,
    clear_match_runtime_server,
    get_active_matches,
    get_busy_runtime_servers,
    get_match_runtime_server,
    is_match_finished,
    release_named_lock,
)
from bot.utils.cs2 import send_rcon
from bot.utils.local_runtime import (
    build_match_load_path,
    load_match_in_tmux,
    runtime_is_online,
    start_runtime_server,
    stop_runtime_server,
    write_match_json_atomic,
)

_WORKSHOP_MAP_CHANGE_DELAY = 8.0


class NoServerAvailableError(RuntimeError):
    pass


class PreferredRuntimeUnavailableError(NoServerAvailableError):
    pass


@dataclass(frozen=True)
class RuntimeSlot:
    slot_id: int
    runtime_id: str
    start_script: str
    stop_script: str
    service_name: str
    tmux_session: str
    host: str
    port: int
    gotv_port: int
    rcon_password: str
    modes: tuple[str, ...]


class ServerPool:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._db_lock_key = "mixbot_runtime_server_pool"
        self._slots: Dict[str, RuntimeSlot] = {}
        self._preexisting_runtime_matches: set[int] = set()
        for runtime_id, raw in POOL_SERVERS.items():
            if not raw.get("enabled", True):
                continue
            slot = RuntimeSlot(
                slot_id=int(raw.get("slot_id") or 0),
                runtime_id=str(runtime_id),
                start_script=str(raw.get("start_script") or ""),
                stop_script=str(raw.get("stop_script") or ""),
                service_name=str(raw.get("service_name") or ""),
                tmux_session=str(raw.get("tmux_session") or runtime_id),
                host=str(raw.get("host") or ""),
                port=int(raw.get("port") or 0),
                gotv_port=int(raw.get("gotv_port") or 0),
                rcon_password=str(raw.get("rcon_password") or ""),
                modes=tuple(str(m).strip().lower() for m in (raw.get("modes") or []) if str(m).strip()),
            )
            self._slots[slot.runtime_id] = slot

    def _normalize_source(self, source: str) -> str:
        norm = str(source or "mix").strip().lower()
        return norm if norm in ("mix", "tourney") else "mix"

    def _supports_mode(self, slot: RuntimeSlot, source: str) -> bool:
        if not slot.modes:
            return True
        return source in slot.modes

    def _normalize_runtime_token(self, token: Optional[str]) -> str:
        raw = str(token or "").strip().lower()
        if not raw:
            return ""
        if raw in self._slots:
            return raw
        if raw.startswith("tserver"):
            digits = raw.replace("tserver", "", 1)
            if digits.isdigit():
                return f"mix{int(digits)}"
        if raw.startswith("ts"):
            digits = raw.replace("ts", "", 1)
            if digits.isdigit():
                return f"mix{int(digits)}"
        if raw.isdigit():
            return f"mix{int(raw)}"
        return raw

    def get_runtime_connection(self, runtime_id: Optional[str]) -> Dict[str, Any]:
        rid = self._normalize_runtime_token(runtime_id)
        slot = self._slots.get(rid)
        if not slot:
            return {}
        return {
            "runtime_id": slot.runtime_id,
            "host": str(slot.host or ""),
            "port": int(slot.port or 0),
            "gotv_port": int(slot.gotv_port or 0),
        }

    async def _get_effective_busy_rows(self) -> list[Dict[str, Any]]:
        busy_rows = await get_busy_runtime_servers()
        if not busy_rows:
            return []

        try:
            active_rows = await get_active_matches()
        except Exception as exc:
            logger.warning(f"POOL: falha ao consultar active_matches para reconciliar busy runtimes: {exc}")
            active_rows = []

        active_match_ids = {
            int(row.get("match_id"))
            for row in active_rows
            if row.get("match_id") is not None
        }

        effective_rows: list[Dict[str, Any]] = []
        for row in busy_rows:
            match_id = int(row.get("match_id") or 0)
            runtime_id = str(row.get("runtime_server_id") or "").strip()
            if match_id > 0 and match_id in active_match_ids:
                effective_rows.append(row)
                continue

            finished = False
            if match_id > 0:
                try:
                    finished = await is_match_finished(match_id)
                except Exception as exc:
                    logger.warning(
                        f"POOL: falha ao verificar fim da partida match={match_id} runtime={runtime_id}: {exc}"
                    )

            if finished:
                logger.warning(
                    f"POOL: limpando alocacao stale runtime={runtime_id} match={match_id} "
                    f"lobby={row.get('lobby_server_id')} source={row.get('source')} reason=match_finished"
                )
                try:
                    await clear_match_runtime_server(match_id)
                except Exception as exc:
                    logger.error(
                        f"POOL: falha ao limpar alocacao stale runtime={runtime_id} match={match_id}: {exc}"
                    )
                    effective_rows.append(row)
                continue

            effective_rows.append(row)

        return effective_rows

    async def available_runtime_ids(self, source: str) -> list[str]:
        source = self._normalize_source(source)
        busy_rows = await self._get_effective_busy_rows()
        busy_ids = {str(row.get("runtime_server_id") or "").strip() for row in busy_rows}
        ordered = sorted(self._slots.values(), key=lambda s: s.slot_id)
        return [
            slot.runtime_id
            for slot in ordered
            if self._supports_mode(slot, source) and slot.runtime_id not in busy_ids
        ]

    async def status_snapshot(self, source: Optional[str] = None) -> list[Dict[str, Any]]:
        source_norm = self._normalize_source(source or "mix") if source else ""
        busy_rows = await self._get_effective_busy_rows()
        busy_by_runtime = {
            str(row.get("runtime_server_id") or "").strip(): row
            for row in busy_rows
            if row.get("runtime_server_id")
        }
        output: list[Dict[str, Any]] = []
        for slot in sorted(self._slots.values(), key=lambda s: s.slot_id):
            if source_norm and not self._supports_mode(slot, source_norm):
                continue
            busy_row = busy_by_runtime.get(slot.runtime_id)
            output.append(
                {
                    "slot_id": slot.slot_id,
                    "runtime_id": slot.runtime_id,
                    "service_name": slot.service_name,
                    "tmux_session": slot.tmux_session,
                    "host": slot.host,
                    "port": slot.port,
                    "gotv_port": slot.gotv_port,
                    "modes": list(slot.modes),
                    "busy": busy_row is not None,
                    "match_id": int(busy_row.get("match_id")) if busy_row and busy_row.get("match_id") else None,
                    "source": str(busy_row.get("source") or "") if busy_row else "",
                    "lobby_server_id": str(busy_row.get("lobby_server_id") or "") if busy_row else "",
                }
            )
        return output

    async def allocate_server(
        self,
        match_id: int,
        source: str,
        lobby_server_id: Optional[str] = None,
        preferred_runtime_id: Optional[str] = None,
        strict_preferred_runtime: bool = False,
    ) -> Dict[str, Any]:
        source = self._normalize_source(source)
        preferred_runtime_id = self._normalize_runtime_token(preferred_runtime_id)

        async with self._lock:
            locked = await acquire_named_lock(self._db_lock_key, timeout_seconds=5)
            if not locked:
                raise RuntimeError("Nao foi possivel obter lock global para alocar servidor.")
            try:
                existing = await get_match_runtime_server(int(match_id))
                if existing:
                    runtime_id = str(existing.get("runtime_server_id") or "").strip()
                    slot = self._slots.get(runtime_id)
                    if slot:
                        return self._allocation_payload(
                            slot,
                            match_id=int(match_id),
                            source=source,
                            lobby_server_id=existing.get("lobby_server_id"),
                            tmux_session=str(existing.get("tmux_session") or slot.tmux_session),
                        )

                busy_rows = await self._get_effective_busy_rows()
                busy_ids = {str(row.get("runtime_server_id") or "").strip() for row in busy_rows}
                chosen_slot: Optional[RuntimeSlot] = None

                if preferred_runtime_id:
                    slot = self._slots.get(preferred_runtime_id)
                    if slot and self._supports_mode(slot, source) and preferred_runtime_id not in busy_ids:
                        chosen_slot = slot
                    else:
                        reason = "inexistente"
                        if slot and not self._supports_mode(slot, source):
                            reason = f"nao_suporta_modo:{source}"
                        elif slot and preferred_runtime_id in busy_ids:
                            reason = "ocupado"
                        blocker = next(
                            (
                                row for row in busy_rows
                                if str(row.get("runtime_server_id") or "").strip() == preferred_runtime_id
                            ),
                            None,
                        )
                        blocker_text = ""
                        if blocker:
                            blocker_text = (
                                f" match={blocker.get('match_id')} lobby={blocker.get('lobby_server_id')} "
                                f"source={blocker.get('source')} started_at={blocker.get('started_at')}"
                            )
                        if strict_preferred_runtime:
                            logger.warning(
                                f"POOL: runtime preferido indisponivel "
                                f"runtime={preferred_runtime_id} motivo={reason}; fallback bloqueado.{blocker_text}"
                            )
                            raise PreferredRuntimeUnavailableError(
                                f"Runtime preferido indisponivel: {preferred_runtime_id} ({reason})"
                            )
                        logger.warning(
                            f"POOL: runtime preferido indisponivel "
                            f"runtime={preferred_runtime_id} motivo={reason}; buscando fallback livre.{blocker_text}"
                        )

                if not chosen_slot:
                    for slot in sorted(self._slots.values(), key=lambda s: s.slot_id):
                        if not self._supports_mode(slot, source):
                            continue
                        if slot.runtime_id in busy_ids:
                            continue
                        chosen_slot = slot
                        break

                if not chosen_slot:
                    raise NoServerAvailableError("Nenhum servidor livre no pool.")

                await bind_match_runtime_server(
                    match_id=int(match_id),
                    runtime_server_id=chosen_slot.runtime_id,
                    tmux_session=chosen_slot.tmux_session,
                    source=source,
                    lobby_server_id=lobby_server_id,
                )
                logger.info(
                    f"POOL: match={match_id} source={source} alocado={chosen_slot.runtime_id} lobby={lobby_server_id}"
                )
                return self._allocation_payload(
                    chosen_slot,
                    match_id=int(match_id),
                    source=source,
                    lobby_server_id=lobby_server_id,
                    tmux_session=chosen_slot.tmux_session,
                )
            finally:
                await release_named_lock(self._db_lock_key)

    async def release_server_for_match(
        self,
        match_id: int,
        reason: str = "",
        force_clear_mapping_on_stop_error: bool = False,
        stop_session: Optional[bool] = None,
        restart_runtime: bool = False,
    ) -> Dict[str, Any]:
        async with self._lock:
            locked = await acquire_named_lock(self._db_lock_key, timeout_seconds=5)
            if not locked:
                raise RuntimeError("Nao foi possivel obter lock global para liberar servidor.")
            try:
                row = await get_match_runtime_server(int(match_id))
                if not row:
                    return {"released": False, "reason": "not_found", "match_id": int(match_id)}

                runtime_id = str(row.get("runtime_server_id") or "").strip()
                slot = self._slots.get(runtime_id)
                tmux_session = str(row.get("tmux_session") or (slot.tmux_session if slot else runtime_id)).strip()
                match_id_int = int(match_id)

                if slot:
                    try:
                        _rcon_cfg = {
                            "cs2": {
                                "host": slot.host,
                                "port": slot.port,
                                "rcon_password": slot.rcon_password,
                            }
                        }
                        await send_rcon(_rcon_cfg, "changelevel de_mirage", log_errors=False)
                        logger.info(
                            f"POOL_RELEASE: changelevel de_mirage enviado para match={match_id} runtime={runtime_id}"
                        )
                    except Exception as _cl_err:
                        logger.warning(f"POOL_RELEASE: falha ao enviar changelevel match={match_id}: {_cl_err}")

                if RUNTIME_USE_RCON_LOAD_ONLY:
                    should_stop = bool(stop_session) if stop_session is not None else bool(restart_runtime)
                else:
                    default_stop = match_id_int not in self._preexisting_runtime_matches
                    should_stop = bool(stop_session) if stop_session is not None else default_stop
                    if restart_runtime:
                        should_stop = True

                stop_error: Optional[Exception] = None
                stopped = False
                if should_stop:
                    try:
                        stopped = await asyncio.to_thread(
                            stop_runtime_server,
                            stop_script=(slot.stop_script if slot else ""),
                            service_name=(slot.service_name if slot else ""),
                            tmux_session=tmux_session,
                        )
                    except Exception as exc:
                        stop_error = exc
                        if not force_clear_mapping_on_stop_error:
                            raise
                        logger.error(
                            f"POOL: stop runtime falhou para match={match_id} runtime={runtime_id} "
                            f"service={slot.service_name if slot else ''} legacy_session={tmux_session}; "
                            f"limpando mapeamento mesmo assim ({reason}): {exc}"
                        )
                else:
                    logger.info(
                        f"POOL_RELEASE: match={match_id} runtime={runtime_id} session_preexistente; "
                        "skip kill-session."
                    )

                await clear_match_runtime_server(int(match_id))
                self._preexisting_runtime_matches.discard(match_id_int)

                restart_error: Optional[Exception] = None
                restarted = False
                if restart_runtime and slot:
                    try:
                        await self._start_runtime_with_online_check(slot, tmux_session=tmux_session)
                        if RUNTIME_BOOT_DELAY_SECONDS > 0:
                            await asyncio.sleep(float(RUNTIME_BOOT_DELAY_SECONDS))
                        restarted = True
                        logger.info(
                            f"POOL: runtime reiniciado match={match_id} runtime={runtime_id} "
                            f"tmux={tmux_session} reason={reason}"
                        )
                    except Exception as exc:
                        restart_error = exc
                        logger.error(
                            f"POOL: falha ao reiniciar runtime match={match_id} runtime={runtime_id} "
                            f"tmux={tmux_session} reason={reason}: {exc}"
                        )

                logger.info(
                    f"POOL: match={match_id} liberado runtime={runtime_id} tmux={tmux_session} "
                    f"stopped={stopped} restarted={restarted} reason={reason}"
                )
                result = {
                    "released": True,
                    "match_id": int(match_id),
                    "runtime_id": runtime_id,
                    "tmux_session": tmux_session,
                    "stopped": bool(stopped),
                    "restarted": bool(restarted),
                }
                if stop_error is not None:
                    result["stop_error"] = str(stop_error)
                if restart_error is not None:
                    result["restart_error"] = str(restart_error)
                return result
            finally:
                await release_named_lock(self._db_lock_key)

    async def prepare_and_start_match(
        self,
        match_id: int,
        payload: Dict[str, Any],
        source: str,
        lobby_server_id: Optional[str] = None,
        preferred_runtime_id: Optional[str] = None,
        strict_preferred_runtime: bool = False,
    ) -> Dict[str, Any]:
        allocation = await self.allocate_server(
            match_id=match_id,
            source=source,
            lobby_server_id=lobby_server_id,
            preferred_runtime_id=preferred_runtime_id,
            strict_preferred_runtime=strict_preferred_runtime,
        )
        try:
            json_path = await asyncio.to_thread(write_match_json_atomic, int(match_id), payload)
            slot = self._slots.get(str(allocation.get("runtime_id") or ""))
            if not slot:
                raise RuntimeError(f"Runtime alocado inexistente no pool: {allocation.get('runtime_id')}")

            map_path = (payload.get("maplist") or [None])[0]
            if RUNTIME_USE_RCON_LOAD_ONLY:
                load_cmd = await self._load_match_via_rcon(slot, int(match_id), map_path=map_path)
            else:
                await self._start_runtime_with_online_check(slot, tmux_session=allocation["tmux_session"])
                if RUNTIME_BOOT_DELAY_SECONDS > 0:
                    await asyncio.sleep(float(RUNTIME_BOOT_DELAY_SECONDS))
                if not str(allocation["tmux_session"] or "").strip():
                    raise RuntimeError(
                        f"Runtime {slot.runtime_id} sem TMUX configurado para fallback de console. "
                        "Use RUNTIME_USE_RCON_LOAD_ONLY=true ou configure POOL_Sx_TMUX_SESSION."
                    )
                load_cmd = await asyncio.to_thread(load_match_in_tmux, allocation["tmux_session"], int(match_id))

            allocation["json_path"] = json_path
            allocation["load_cmd"] = load_cmd
            return allocation
        except Exception:
            logger.exception(f"POOL: falha no start/load match={match_id}; liberando runtime.")
            try:
                await self.release_server_for_match(
                    int(match_id),
                    reason="start_failure",
                    force_clear_mapping_on_stop_error=True,
                )
            except Exception:
                logger.exception(f"POOL: falha ao liberar runtime apos start_failure match={match_id}")
            raise

    async def boot_runtime_for_match(
        self,
        match_id: int,
        source: str,
        lobby_server_id: Optional[str] = None,
        preferred_runtime_id: Optional[str] = None,
        strict_preferred_runtime: bool = False,
    ) -> Dict[str, Any]:
        allocation = await self.allocate_server(
            match_id=match_id,
            source=source,
            lobby_server_id=lobby_server_id,
            preferred_runtime_id=preferred_runtime_id,
            strict_preferred_runtime=strict_preferred_runtime,
        )
        slot = self._slots.get(str(allocation.get("runtime_id") or "").strip())
        if not slot:
            raise RuntimeError(f"Runtime alocado inexistente no pool: {allocation.get('runtime_id')}")

        try:
            already_online = await self._runtime_online(
                slot,
                tmux_session=str(allocation.get("tmux_session") or "").strip(),
            )
            if not already_online:
                await self._start_runtime_with_online_check(
                    slot,
                    tmux_session=str(allocation.get("tmux_session") or "").strip(),
                )
                if RUNTIME_BOOT_DELAY_SECONDS > 0:
                    await asyncio.sleep(float(RUNTIME_BOOT_DELAY_SECONDS))
                self._preexisting_runtime_matches.discard(int(match_id))
            else:
                self._preexisting_runtime_matches.add(int(match_id))
            allocation["already_online"] = bool(already_online)
            return allocation
        except Exception:
            logger.exception(f"POOL: falha ao iniciar runtime match={match_id}; liberando runtime.")
            try:
                await self.release_server_for_match(
                    int(match_id),
                    reason="boot_failure",
                    force_clear_mapping_on_stop_error=True,
                )
            except Exception:
                logger.exception(f"POOL: falha ao liberar runtime apos boot_failure match={match_id}")
            raise

    async def load_match_on_allocated_runtime(
        self,
        match_id: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        row = await get_match_runtime_server(int(match_id))
        if not row:
            raise NoServerAvailableError(f"Nenhuma alocacao encontrada para match={match_id}")

        runtime_id = str(row.get("runtime_server_id") or "").strip()
        slot = self._slots.get(runtime_id)
        if not slot:
            raise RuntimeError(f"Runtime alocado inexistente no pool: {runtime_id}")

        tmux_session = str(row.get("tmux_session") or slot.tmux_session).strip()
        try:
            json_path = await asyncio.to_thread(write_match_json_atomic, int(match_id), payload)
            map_path = (payload.get("maplist") or [None])[0]
            if RUNTIME_USE_RCON_LOAD_ONLY:
                load_cmd = await self._load_match_via_rcon(slot, int(match_id), map_path=map_path)
            else:
                is_online = await self._runtime_online(slot, tmux_session=tmux_session)
                if not is_online:
                    await self._start_runtime_with_online_check(slot, tmux_session=tmux_session)
                    if RUNTIME_BOOT_DELAY_SECONDS > 0:
                        await asyncio.sleep(float(RUNTIME_BOOT_DELAY_SECONDS))
                if not tmux_session:
                    raise RuntimeError(
                        f"Runtime {slot.runtime_id} sem TMUX configurado para fallback de console. "
                        "Use RUNTIME_USE_RCON_LOAD_ONLY=true ou configure POOL_Sx_TMUX_SESSION."
                    )
                load_cmd = await asyncio.to_thread(load_match_in_tmux, tmux_session, int(match_id))
            return {
                "match_id": int(match_id),
                "source": str(row.get("source") or "mix"),
                "lobby_server_id": row.get("lobby_server_id"),
                "slot_id": slot.slot_id,
                "runtime_id": slot.runtime_id,
                "start_script": slot.start_script,
                "stop_script": slot.stop_script,
                "service_name": slot.service_name,
                "tmux_session": tmux_session,
                "host": slot.host,
                "port": slot.port,
                "gotv_port": slot.gotv_port,
                "json_path": json_path,
                "load_cmd": load_cmd,
            }
        except Exception:
            logger.exception(f"POOL: falha ao carregar match em runtime alocado match={match_id}")
            raise

    async def _load_match_via_rcon(self, slot: RuntimeSlot, match_id: int, map_path: Optional[str] = None) -> str:
        def _response_preview(value: Optional[str], max_len: int = 220) -> str:
            text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
            if not text:
                return "<vazio>"
            if len(text) > max_len:
                return text[:max_len] + "..."
            return text

        def _is_rcon_load_success(value: Optional[str]) -> bool:
            if value is None:
                return False
            text = str(value).strip()
            if not text:
                return True

            low = text.lower()
            fail_tokens = (
                "unknown command",
                "not found",
                "no such file",
                "could not",
                "cannot",
                "failed",
                "error",
                "invalid",
            )
            if "loaded" in low or "loadmatch" in low:
                return True
            return not any(tok in low for tok in fail_tokens)

        load_cmd = f"matchzy_loadmatch {build_match_load_path(match_id)}"
        server_config = {
            "cs2": {
                "host": slot.host,
                "port": int(slot.port or 0),
                "rcon_password": slot.rcon_password,
            }
        }

        if map_path and str(map_path).startswith("workshop/"):
            parts = str(map_path).split("/")
            workshop_id = parts[1] if len(parts) >= 2 else ""
            if workshop_id:
                workshop_cmd = f"host_workshop_map {workshop_id}"
                logger.info(
                    f"POOL_LOAD: match={match_id} runtime={slot.runtime_id} "
                    f"workshop=True cmd={workshop_cmd}; aguardando {_WORKSHOP_MAP_CHANGE_DELAY}s para reload do mapa."
                )
                await send_rcon(server_config, workshop_cmd, log_errors=False)
                await asyncio.sleep(_WORKSHOP_MAP_CHANGE_DELAY)

        retries = 3
        retry_delay = 2.0
        response = None
        for attempt in range(1, retries + 1):
            response = await send_rcon(server_config, load_cmd, log_errors=False)
            if _is_rcon_load_success(response):
                logger.info(
                    f"POOL_LOAD: match={match_id} runtime={slot.runtime_id} method=rcon status=ok "
                    f"attempt={attempt}/{retries} target={slot.host}:{slot.port} "
                    f"cmd={load_cmd} resp={_response_preview(response)}"
                )
                return load_cmd
            logger.warning(
                f"POOL_LOAD: match={match_id} runtime={slot.runtime_id} method=rcon status=falha "
                f"attempt={attempt}/{retries} target={slot.host}:{slot.port} "
                f"resp={_response_preview(response)}"
            )
            if attempt < retries:
                await asyncio.sleep(retry_delay)

        tmux_session = str(slot.tmux_session or "").strip()
        online = await self._runtime_online(slot, tmux_session=tmux_session)
        if not online and str(slot.start_script or "").strip():
            logger.warning(
                f"POOL_LOAD: match={match_id} runtime={slot.runtime_id} rcon=sem_resposta runtime=offline; "
                f"tentando start_script={slot.start_script} service={slot.service_name}."
            )
            try:
                await self._start_runtime_with_online_check(slot, tmux_session=tmux_session)
                if RUNTIME_BOOT_DELAY_SECONDS > 0:
                    await asyncio.sleep(float(RUNTIME_BOOT_DELAY_SECONDS))
            except Exception:
                logger.exception(
                    f"POOL: falha ao iniciar runtime apos erro de RCON runtime={slot.runtime_id} "
                    f"service={slot.service_name} legacy_session={tmux_session}"
                )
            else:
                retry = await send_rcon(server_config, load_cmd, log_errors=False)
                if _is_rcon_load_success(retry):
                    logger.info(
                        f"POOL_LOAD: match={match_id} runtime={slot.runtime_id} "
                        f"method=rcon_retry status=ok target={slot.host}:{slot.port} "
                        f"cmd={load_cmd} resp={_response_preview(retry)}"
                    )
                    return load_cmd
                if retry is not None:
                    logger.warning(
                        f"POOL_LOAD: match={match_id} runtime={slot.runtime_id} method=rcon_retry "
                        f"status=erro_textual target={slot.host}:{slot.port} "
                        f"cmd={load_cmd} resp={_response_preview(retry)}"
                    )
            online = await self._runtime_online(slot, tmux_session=tmux_session)

        if online and tmux_session:
            logger.info(
                f"POOL_LOAD: match={match_id} runtime={slot.runtime_id} "
                f"method=tmux_fallback reason=rcon_sem_resposta session={tmux_session}"
            )
            return await asyncio.to_thread(load_match_in_tmux, tmux_session, int(match_id))

        if online and not tmux_session:
            raise RuntimeError(
                f"Falha ao executar load via RCON no runtime={slot.runtime_id} ({slot.host}:{slot.port}); "
                "runtime online, mas sem TMUX configurado para fallback de console."
            )

        raise RuntimeError(
            f"Falha ao executar load via RCON no runtime={slot.runtime_id} ({slot.host}:{slot.port}); "
            "RCON sem resposta e runtime offline."
        )

    async def _runtime_online(self, slot: RuntimeSlot, tmux_session: Optional[str] = None) -> bool:
        try:
            online = await asyncio.to_thread(
                runtime_is_online,
                str(slot.service_name or "").strip(),
                str(tmux_session or slot.tmux_session or "").strip(),
            )
            if online:
                return True
        except Exception:
            pass

        if not str(slot.host or "").strip() or int(slot.port or 0) <= 0 or not str(slot.rcon_password or "").strip():
            return False

        server_cfg = {
            "cs2": {
                "host": slot.host,
                "port": int(slot.port or 0),
                "rcon_password": slot.rcon_password,
            }
        }
        try:
            response = await send_rcon(server_cfg, "status", log_errors=False)
            return response is not None
        except Exception:
            return False

    async def _start_runtime_with_online_check(self, slot: RuntimeSlot, tmux_session: Optional[str] = None) -> None:
        legacy_session = str(tmux_session or slot.tmux_session or "").strip()
        logger.info(
            f"POOL: iniciando runtime script={slot.start_script} service={slot.service_name} "
            f"legacy_session={legacy_session}"
        )
        try:
            await asyncio.to_thread(start_runtime_server, slot.start_script)
            logger.info(f"POOL: start finalizado script={slot.start_script} service={slot.service_name}")
            return
        except Exception as exc:
            msg = str(exc or "").lower()
            is_timeout = ("timed out" in msg) or ("timeoutexpired" in msg)
            if is_timeout:
                wait_seconds = max(1, int(RUNTIME_ONLINE_GRACE_SECONDS))
                logger.warning(
                    f"POOL: start timeout ({slot.start_script}); aguardando ate {wait_seconds}s "
                    f"por runtime online (service={slot.service_name} legacy_session={legacy_session}) "
                    "antes de falhar."
                )
                for attempt in range(wait_seconds):
                    online = await self._runtime_online(slot, tmux_session=legacy_session)
                    if online:
                        logger.warning(
                            f"POOL: start timeout ({slot.start_script}), mas runtime ficou online "
                            f"(service={slot.service_name} legacy_session={legacy_session}); seguindo."
                        )
                        return
                    if attempt < wait_seconds - 1:
                        await asyncio.sleep(1.0)
            raise

    def _allocation_payload(
        self,
        slot: RuntimeSlot,
        *,
        match_id: int,
        source: str,
        lobby_server_id: Optional[str],
        tmux_session: str,
    ) -> Dict[str, Any]:
        return {
            "match_id": int(match_id),
            "source": source,
            "lobby_server_id": lobby_server_id,
            "slot_id": slot.slot_id,
            "runtime_id": slot.runtime_id,
            "start_script": slot.start_script,
            "stop_script": slot.stop_script,
            "service_name": slot.service_name,
            "tmux_session": tmux_session,
            "host": slot.host,
            "port": slot.port,
            "gotv_port": slot.gotv_port,
        }


_SERVER_POOL: Optional[ServerPool] = None


def get_server_pool() -> ServerPool:
    global _SERVER_POOL
    if _SERVER_POOL is None:
        _SERVER_POOL = ServerPool()
    return _SERVER_POOL
