import unittest

from bot.utils.mix_lobby import pick_free_lobby_server


class MixLobbySelectionTests(unittest.TestCase):
    def test_pick_free_lobby_server_skips_lobby_with_busy_runtime(self):
        servers = {
            "server1": {
                "active": True,
                "runtime_id": "mix1",
                "channels": {"picks_voice": 101},
            },
            "server3": {
                "active": True,
                "runtime_id": "mix3",
                "channels": {"picks_voice": 303},
            },
        }
        sessions = {
            "server1": {"active": False},
            "server3": {"active": False},
        }
        voice_member_counts = {
            101: 0,
            303: 0,
        }

        picked = pick_free_lobby_server(
            servers,
            sessions,
            voice_member_counts,
            available_runtime_ids={"mix3"},
        )

        self.assertEqual(picked, "server3")


if __name__ == "__main__":
    unittest.main()
