import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot.utils import server_pool


class ServerPoolTests(unittest.IsolatedAsyncioTestCase):
    def _build_slots(self, count: int = 2):
        slots = {}
        for i in range(1, count + 1):
            slots[f"mix{i}"] = {
                "slot_id": i,
                "enabled": True,
                "runtime_id": f"mix{i}",
                "start_script": f"/home/servidores/scripts/start_mix{i}.sh",
                "stop_script": "",
                "service_name": f"cs2-mix{i}.service",
                "tmux_session": f"mix{i}",
                "host": "127.0.0.1",
                "port": 27000 + i,
                "gotv_port": 27100 + i,
                "rcon_password": "secret",
                "modes": ["mix", "tourney"],
            }
        return slots

    async def test_allocate_server_picks_first_free(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            with patch.object(server_pool, "acquire_named_lock", AsyncMock(return_value=True)):
                with patch.object(server_pool, "release_named_lock", AsyncMock()):
                    with patch.object(server_pool, "get_match_runtime_server", AsyncMock(return_value=None)):
                        with patch.object(server_pool, "get_busy_runtime_servers", AsyncMock(return_value=[])):
                            with patch.object(server_pool, "bind_match_runtime_server", AsyncMock()) as bind_mock:
                                result = await pool.allocate_server(match_id=547, source="mix")

        self.assertEqual(result["runtime_id"], "mix1")
        self.assertEqual(result["tmux_session"], "mix1")
        bind_mock.assert_awaited_once()

    async def test_allocate_server_raises_when_pool_full(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            busy_rows = [
                {"runtime_server_id": "mix1", "match_id": 1},
                {"runtime_server_id": "mix2", "match_id": 2},
            ]
            with patch.object(server_pool, "acquire_named_lock", AsyncMock(return_value=True)):
                with patch.object(server_pool, "release_named_lock", AsyncMock()):
                    with patch.object(server_pool, "get_match_runtime_server", AsyncMock(return_value=None)):
                        with patch.object(server_pool, "get_busy_runtime_servers", AsyncMock(return_value=busy_rows)):
                            with self.assertRaises(server_pool.NoServerAvailableError):
                                await pool.allocate_server(match_id=999, source="mix")

    async def test_allocate_server_falls_back_when_preferred_busy(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            busy_rows = [{"runtime_server_id": "mix1", "match_id": 1}]
            with patch.object(server_pool, "acquire_named_lock", AsyncMock(return_value=True)):
                with patch.object(server_pool, "release_named_lock", AsyncMock()):
                    with patch.object(server_pool, "get_match_runtime_server", AsyncMock(return_value=None)):
                        with patch.object(server_pool, "get_busy_runtime_servers", AsyncMock(return_value=busy_rows)):
                            with patch.object(server_pool, "bind_match_runtime_server", AsyncMock()) as bind_mock:
                                result = await pool.allocate_server(
                                    match_id=777,
                                    source="mix",
                                    preferred_runtime_id="mix1",
                                )

        self.assertEqual(result["runtime_id"], "mix2")
        bind_mock.assert_awaited_once()

    async def test_allocate_server_raises_when_strict_preferred_busy(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            busy_rows = [{"runtime_server_id": "mix1", "match_id": 1}]
            with patch.object(server_pool, "acquire_named_lock", AsyncMock(return_value=True)):
                with patch.object(server_pool, "release_named_lock", AsyncMock()):
                    with patch.object(server_pool, "get_match_runtime_server", AsyncMock(return_value=None)):
                        with patch.object(server_pool, "get_busy_runtime_servers", AsyncMock(return_value=busy_rows)):
                            with self.assertRaises(server_pool.PreferredRuntimeUnavailableError):
                                await pool.allocate_server(
                                    match_id=778,
                                    source="mix",
                                    preferred_runtime_id="mix1",
                                    strict_preferred_runtime=True,
                                )

    async def test_release_server_for_match_clears_mapping(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            row = {
                "match_id": 547,
                "runtime_server_id": "mix2",
                "tmux_session": "mix2",
                "source": "mix",
                "lobby_server_id": "server1",
            }
            with patch.object(server_pool, "acquire_named_lock", AsyncMock(return_value=True)):
                with patch.object(server_pool, "release_named_lock", AsyncMock()):
                    with patch.object(server_pool, "get_match_runtime_server", AsyncMock(return_value=row)):
                        with patch.object(server_pool, "clear_match_runtime_server", AsyncMock()) as clear_mock:
                            with patch.object(server_pool, "send_rcon", AsyncMock(return_value="")):
                                with patch.object(server_pool, "stop_runtime_server", return_value=True):
                                    with patch.object(server_pool, "RUNTIME_USE_RCON_LOAD_ONLY", False):
                                        result = await pool.release_server_for_match(547, reason="test")

        self.assertTrue(result["released"])
        self.assertEqual(result["runtime_id"], "mix2")
        clear_mock.assert_awaited_once_with(547)

    async def test_release_server_for_match_restarts_runtime_on_cancel(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            row = {
                "match_id": 547,
                "runtime_server_id": "mix2",
                "tmux_session": "mix2",
                "source": "mix",
                "lobby_server_id": "server1",
            }
            with patch.object(server_pool, "acquire_named_lock", AsyncMock(return_value=True)):
                with patch.object(server_pool, "release_named_lock", AsyncMock()):
                    with patch.object(server_pool, "get_match_runtime_server", AsyncMock(return_value=row)):
                        with patch.object(server_pool, "clear_match_runtime_server", AsyncMock()) as clear_mock:
                            with patch.object(server_pool, "send_rcon", AsyncMock(return_value="")):
                                with patch.object(server_pool, "stop_runtime_server", return_value=True) as stop_mock:
                                    with patch.object(pool, "_start_runtime_with_online_check", AsyncMock()) as start_mock:
                                        with patch.object(server_pool, "RUNTIME_USE_RCON_LOAD_ONLY", True):
                                            with patch.object(server_pool, "RUNTIME_BOOT_DELAY_SECONDS", 0):
                                                result = await pool.release_server_for_match(
                                                    547,
                                                    reason="test_cancel_restart",
                                                    restart_runtime=True,
                                                )

        self.assertTrue(result["released"])
        self.assertTrue(result["stopped"])
        self.assertTrue(result["restarted"])
        stop_mock.assert_called_once()
        start_mock.assert_awaited_once()
        clear_mock.assert_awaited_once_with(547)

    async def test_prepare_and_start_match_runs_local_runtime_steps(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            alloc = {
                "match_id": 547,
                "source": "mix",
                "lobby_server_id": "server1",
                "slot_id": 1,
                "runtime_id": "mix1",
                "start_script": "/home/servidores/scripts/start_mix1.sh",
                "stop_script": "",
                "service_name": "cs2-mix1.service",
                "tmux_session": "mix1",
                "host": "127.0.0.1",
                "port": 27015,
                "gotv_port": 27020,
            }
            with patch.object(pool, "allocate_server", AsyncMock(return_value=alloc)):
                with patch.object(server_pool, "write_match_json_atomic", return_value="/tmp/match547.json"):
                    with patch.object(pool, "_start_runtime_with_online_check", AsyncMock()):
                        with patch.object(server_pool, "load_match_in_tmux", return_value="matchzy_loadmatch cfg/MatchZy/matches/match547.json"):
                            with patch.object(server_pool, "RUNTIME_BOOT_DELAY_SECONDS", 0):
                                with patch.object(server_pool, "RUNTIME_USE_RCON_LOAD_ONLY", False):
                                    result = await pool.prepare_and_start_match(
                                        match_id=547,
                                        payload={"matchid": "547"},
                                        source="mix",
                                    )

        self.assertEqual(result["runtime_id"], "mix1")
        self.assertEqual(result["json_path"], "/tmp/match547.json")

    async def test_load_match_via_rcon_boots_and_falls_back_tmux_when_needed(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            slot = pool._slots["mix1"]

            with patch.object(server_pool, "send_rcon", AsyncMock(side_effect=[None, None, None, None])) as rcon_mock:
                with patch.object(pool, "_runtime_online", AsyncMock(side_effect=[False, True])):
                    with patch.object(pool, "_start_runtime_with_online_check", AsyncMock()) as start_mock:
                        with patch.object(
                            server_pool,
                            "load_match_in_tmux",
                            return_value="matchzy_loadmatch cfg/MatchZy/matches/match606.json",
                        ) as load_mock:
                            with patch.object(server_pool, "RUNTIME_BOOT_DELAY_SECONDS", 0):
                                result = await pool._load_match_via_rcon(slot, 606)

        self.assertEqual(result, "matchzy_loadmatch cfg/MatchZy/matches/match606.json")
        self.assertEqual(rcon_mock.await_count, 4)
        start_mock.assert_awaited_once_with(slot, tmux_session=slot.tmux_session)
        load_mock.assert_called_once_with(slot.tmux_session, 606)

    async def test_load_match_via_rcon_treats_text_error_as_failure_and_falls_back_tmux(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            slot = pool._slots["mix1"]

            with patch.object(server_pool, "send_rcon", AsyncMock(return_value="Unknown command: matchzy_loadmatch")):
                with patch.object(pool, "_runtime_online", AsyncMock(return_value=True)):
                    with patch.object(
                        server_pool,
                        "load_match_in_tmux",
                        return_value="matchzy_loadmatch cfg/MatchZy/matches/match607.json",
                    ) as load_mock:
                        result = await pool._load_match_via_rcon(slot, 607)

        self.assertEqual(result, "matchzy_loadmatch cfg/MatchZy/matches/match607.json")
        load_mock.assert_called_once_with(slot.tmux_session, 607)

    async def test_boot_runtime_for_match_checks_systemd_online_state(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            alloc = {
                "match_id": 547,
                "source": "mix",
                "lobby_server_id": "server1",
                "slot_id": 1,
                "runtime_id": "mix1",
                "start_script": "/home/servidores/scripts/start_mix1.sh",
                "stop_script": "",
                "service_name": "cs2-mix1.service",
                "tmux_session": "mix1",
                "host": "127.0.0.1",
                "port": 27015,
                "gotv_port": 27020,
            }
            with patch.object(pool, "allocate_server", AsyncMock(return_value=alloc)):
                with patch.object(pool, "_runtime_online", AsyncMock(return_value=False)):
                    with patch.object(pool, "_start_runtime_with_online_check", AsyncMock()) as start_mock:
                        with patch.object(server_pool, "RUNTIME_BOOT_DELAY_SECONDS", 0):
                            result = await pool.boot_runtime_for_match(
                                match_id=547,
                                source="mix",
                            )

        self.assertFalse(result["already_online"])
        start_mock.assert_awaited_once()

    async def test_boot_runtime_for_match_skips_start_when_rcon_already_responds(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots()):
            pool = server_pool.ServerPool()
            alloc = {
                "match_id": 547,
                "source": "mix",
                "lobby_server_id": "server1",
                "slot_id": 1,
                "runtime_id": "mix1",
                "start_script": "/home/servidores/scripts/start_mix1.sh",
                "stop_script": "",
                "service_name": "",
                "tmux_session": "",
                "host": "127.0.0.1",
                "port": 27015,
                "gotv_port": 27020,
            }
            with patch.object(pool, "allocate_server", AsyncMock(return_value=alloc)):
                with patch.object(server_pool, "runtime_is_online", return_value=False):
                    with patch.object(server_pool, "send_rcon", AsyncMock(return_value="hostname: mix1")):
                        with patch.object(pool, "_start_runtime_with_online_check", AsyncMock()) as start_mock:
                            with patch.object(server_pool, "RUNTIME_BOOT_DELAY_SECONDS", 0):
                                result = await pool.boot_runtime_for_match(
                                    match_id=547,
                                    source="mix",
                                )

        self.assertTrue(result["already_online"])
        start_mock.assert_not_awaited()

    async def test_parallel_allocations_6_for_5_slots(self):
        with patch.object(server_pool, "POOL_SERVERS", self._build_slots(count=5)):
            pool = server_pool.ServerPool()
            rows_by_match = {}
            db_lock = asyncio.Lock()

            async def _get_match_runtime_server(match_id: int):
                return rows_by_match.get(int(match_id))

            async def _get_busy_runtime_servers():
                return list(rows_by_match.values())

            async def _bind_match_runtime_server(match_id, runtime_server_id, tmux_session, source, lobby_server_id=None):
                async with db_lock:
                    for row in rows_by_match.values():
                        if row["runtime_server_id"] == runtime_server_id and int(row["match_id"]) != int(match_id):
                            raise RuntimeError("runtime_server_id already in use")
                    rows_by_match[int(match_id)] = {
                        "match_id": int(match_id),
                        "runtime_server_id": str(runtime_server_id),
                        "tmux_session": str(tmux_session),
                        "source": str(source),
                        "lobby_server_id": lobby_server_id,
                    }

            async def _allocate_one(match_id: int):
                try:
                    result = await pool.allocate_server(match_id=match_id, source="mix")
                    return ("ok", result["runtime_id"])
                except server_pool.NoServerAvailableError:
                    return ("no_slot", None)

            with patch.object(server_pool, "acquire_named_lock", AsyncMock(return_value=True)):
                with patch.object(server_pool, "release_named_lock", AsyncMock()):
                    with patch.object(server_pool, "get_match_runtime_server", side_effect=_get_match_runtime_server):
                        with patch.object(server_pool, "get_busy_runtime_servers", side_effect=_get_busy_runtime_servers):
                            with patch.object(server_pool, "bind_match_runtime_server", side_effect=_bind_match_runtime_server):
                                results = await asyncio.gather(*[_allocate_one(mid) for mid in range(100, 106)])

        ok_count = sum(1 for status, _ in results if status == "ok")
        fail_count = sum(1 for status, _ in results if status == "no_slot")
        allocated_runtime_ids = {runtime_id for status, runtime_id in results if status == "ok"}
        self.assertEqual(ok_count, 5)
        self.assertEqual(fail_count, 1)
        self.assertEqual(len(allocated_runtime_ids), 5)


if __name__ == "__main__":
    unittest.main()
