import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.utils import local_runtime


class LocalRuntimeTests(unittest.TestCase):
    def test_write_match_json_atomic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(local_runtime, "MATCHZY_BASE_DIR", tmpdir):
                with patch.object(local_runtime, "MATCHZY_MATCHES_SUBDIR", "matches"):
                    path = local_runtime.write_match_json_atomic(547, {"ok": True, "value": 1})
                    self.assertTrue(path.endswith(str(Path("matches") / "match547.json")))
                    self.assertTrue(Path(path).exists())
                    data = json.loads(Path(path).read_text(encoding="utf-8"))
                    self.assertEqual(data["ok"], True)
                    self.assertEqual(data["value"], 1)

    def test_load_match_in_tmux_builds_expected_command(self):
        captured = {}

        def _fake_run(args, check=True, timeout=None):
            captured["args"] = list(args)
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch.object(local_runtime, "run_as_runtime_user", side_effect=_fake_run):
            cmd = local_runtime.load_match_in_tmux("mix2", 547)

        self.assertEqual(cmd, "matchzy_loadmatch cfg/MatchZy/matches/match547.json")
        self.assertEqual(
            captured["args"],
            ["tmux", "send-keys", "-t", "mix2", "matchzy_loadmatch cfg/MatchZy/matches/match547.json", "C-m"],
        )

    def test_start_runtime_server_calls_sudo_with_script(self):
        fake_cp = SimpleNamespace(returncode=0, stderr="", stdout="")
        with patch.object(local_runtime, "RUNTIME_SUDO_USER", "servidores"):
            with patch("bot.utils.local_runtime.subprocess.run", return_value=fake_cp) as run_mock:
                local_runtime.start_runtime_server("/home/servidores/start_mix2.sh")
        args = run_mock.call_args[0][0]
        self.assertEqual(args, ["sudo", "-n", "-u", "servidores", "/home/servidores/start_mix2.sh"])

    def test_is_runtime_service_active_uses_systemctl(self):
        active = SimpleNamespace(returncode=0, stderr="", stdout="active\n")
        with patch.object(local_runtime, "run_as_runtime_user", return_value=active) as run_mock:
            result = local_runtime.is_runtime_service_active("cs2-mix2.service")

        self.assertTrue(result)
        run_mock.assert_called_once_with(["systemctl", "is-active", "cs2-mix2.service"], check=False)

    def test_runtime_is_online_prefers_service_over_tmux(self):
        with patch.object(local_runtime, "is_runtime_service_active", return_value=True) as svc_mock:
            with patch.object(local_runtime, "tmux_has_session", return_value=False) as tmux_mock:
                result = local_runtime.runtime_is_online("cs2-mix2.service", "mix2")

        self.assertTrue(result)
        svc_mock.assert_called_once_with("cs2-mix2.service")
        tmux_mock.assert_not_called()

    def test_stop_runtime_server_uses_service_when_configured(self):
        stopped = SimpleNamespace(returncode=0, stderr="", stdout="")
        with patch.object(local_runtime, "run_as_runtime_user", return_value=stopped) as run_mock:
            result = local_runtime.stop_runtime_server(service_name="cs2-mix2.service")

        self.assertTrue(result)
        run_mock.assert_called_once()
        self.assertEqual(
            run_mock.call_args.kwargs,
            {"check": False, "timeout": local_runtime.RUNTIME_START_TIMEOUT_SECONDS},
        )

    def test_stop_tmux_session_is_idempotent_when_missing(self):
        missing = SimpleNamespace(returncode=1, stderr="can't find session: mix3", stdout="")
        with patch.object(local_runtime, "run_as_runtime_user", return_value=missing):
            stopped = local_runtime.stop_tmux_session("mix3")
        self.assertFalse(stopped)


if __name__ == "__main__":
    unittest.main()
