import json
import unittest
from unittest.mock import AsyncMock, patch

from bot.cogs.matches import MatchesCog
from bot.cogs import matches as matches_module
from bot.cogs.mix import sessions


class _DummyBot:
    pass


class _DummyRequest:
    def __init__(self, headers: dict, payload=None, *, raise_json: bool = False):
        self.headers = headers
        self._payload = payload
        self._raise_json = raise_json

    async def json(self):
        if self._raise_json:
            raise ValueError("bad-json")
        return self._payload


class MatchesWebhookTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        sessions.clear()

    async def test_unauthorized_when_key_invalid(self):
        cog = MatchesCog(_DummyBot())
        req = _DummyRequest(headers={"X-MatchZy-Key": "wrong"}, payload={"event": "match_ended", "match_id": 1})
        with patch.object(matches_module, "MATCHZY_WEBHOOK_KEY", "secret"):
            resp = await cog.handle_match_webhook(req)
        self.assertEqual(resp.status, 401)

    async def test_reject_invalid_payload(self):
        cog = MatchesCog(_DummyBot())
        req = _DummyRequest(headers={"X-MatchZy-Key": "secret"}, payload={"event": "other", "match_id": 1})
        with patch.object(matches_module, "MATCHZY_WEBHOOK_KEY", "secret"):
            resp = await cog.handle_match_webhook(req)
        self.assertEqual(resp.status, 400)

    async def test_reject_missing_server_id(self):
        cog = MatchesCog(_DummyBot())
        req = _DummyRequest(headers={"X-MatchZy-Key": "secret"}, payload={"event": "match_ended", "match_id": 1})
        with patch.object(matches_module, "MATCHZY_WEBHOOK_KEY", "secret"):
            resp = await cog.handle_match_webhook(req)
        self.assertEqual(resp.status, 400)

    async def test_finalize_valid_match_ended_webhook(self):
        cog = MatchesCog(_DummyBot())
        req = _DummyRequest(
            headers={"X-MatchZy-Key": "secret"},
            payload={"event": "match_ended", "match_id": 547, "server_id": "mix2"},
        )
        with (
            patch.object(matches_module, "MATCHZY_WEBHOOK_KEY", "secret"),
            patch.object(matches_module, "get_match_runtime_server", AsyncMock(return_value={"runtime_server_id": "mix2"})),
            patch.object(cog, "_runtime_to_session_id", return_value="server2"),
            patch.object(
                cog,
                "_finalize_match_end",
                AsyncMock(return_value={"ok": True, "released": True, "posted": True}),
            ) as finalize_mock,
        ):
            resp = await cog.handle_match_webhook(req)

        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.text)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["released"])
        finalize_mock.assert_awaited_once_with(
            547,
            s_id_hint="server2",
            trigger="webhook",
            authoritative_end=True,
        )

    async def test_finalize_series_result_webhook(self):
        cog = MatchesCog(_DummyBot())
        req = _DummyRequest(
            headers={"X-MatchZy-Key": "secret"},
            payload={"event_type": "series_end", "match_id": 777, "server_id": "mix3"},
        )
        with (
            patch.object(matches_module, "MATCHZY_WEBHOOK_KEY", "secret"),
            patch.object(cog, "_runtime_to_session_id", return_value="server3"),
            patch.object(
                cog,
                "_finalize_match_end",
                AsyncMock(return_value={"ok": True, "released": True, "posted": True}),
            ) as finalize_mock,
        ):
            resp = await cog.handle_match_webhook(req)

        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.text)
        self.assertTrue(payload["ok"])
        finalize_mock.assert_awaited_once_with(
            777,
            s_id_hint="server3",
            trigger="webhook_series",
            authoritative_end=True,
        )

    async def test_finalize_series_result_webhook_accepts_matchid_payload(self):
        cog = MatchesCog(_DummyBot())
        req = _DummyRequest(
            headers={"X-MatchZy-Key": "secret"},
            payload={"event": "series_end", "matchid": 2057},
        )
        with (
            patch.object(matches_module, "MATCHZY_WEBHOOK_KEY", "secret"),
            patch.object(
                cog,
                "_finalize_match_end",
                AsyncMock(return_value={"ok": True, "released": True, "posted": True}),
            ) as finalize_mock,
        ):
            resp = await cog.handle_match_webhook(req)

        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.text)
        self.assertTrue(payload["ok"])
        finalize_mock.assert_awaited_once_with(
            2057,
            s_id_hint=None,
            trigger="webhook_series",
            authoritative_end=True,
        )

    async def test_load_finalize_payload_waits_for_complete_session_snapshot(self):
        cog = MatchesCog(_DummyBot())
        sessions["server1"] = {
            "player_steamids": {idx: f"765611980000000{idx:02d}" for idx in range(1, 11)}
        }
        match = {
            "matchid": 2044,
            "team1_name": "T1_Alezin",
            "team2_name": "T2_dig4o",
            "team1_score": 13,
            "team2_score": 10,
            "win1": 1,
            "win2": 0,
        }
        incomplete_players = [
            {"steamid64": f"765611980000000{idx:02d}", "team": "T1_Alezin" if idx <= 4 else "T2_dig4o"}
            for idx in range(1, 10)
        ]
        complete_players = incomplete_players + [{"steamid64": "76561198000000010", "team": "T2_dig4o"}]

        with (
            patch.object(matches_module, "get_match_overview", AsyncMock(side_effect=[match, match])),
            patch.object(matches_module, "get_match_details", AsyncMock(return_value=None)),
            patch.object(
                matches_module,
                "get_match_players",
                AsyncMock(side_effect=[incomplete_players, complete_players]),
            ) as players_mock,
            patch.object(cog, "_repair_missing_winner_from_map", AsyncMock(return_value=match)),
            patch.object(matches_module.asyncio, "sleep", AsyncMock()) as sleep_mock,
        ):
            loaded_match, loaded_players = await cog._load_match_finalize_payload(
                2044,
                s_id_hint="server1",
                authoritative_end=True,
                attempts=2,
                delay_seconds=0,
            )

        self.assertEqual(loaded_match, match)
        self.assertEqual(loaded_players, complete_players)
        self.assertEqual(players_mock.await_count, 2)
        sleep_mock.assert_awaited_once()

    async def test_player_snapshot_accepts_special_admin_steamid_when_player_is_in_match(self):
        cog = MatchesCog(_DummyBot())
        sessions["server1"] = {
            "player_steamids": {
                1: "76561198342053209",
                **{idx: f"765611980000000{idx:02d}" for idx in range(2, 11)},
            }
        }
        match = {
            "matchid": 2044,
            "team1_name": "T1_Alezin",
            "team2_name": "T2_dig4o",
        }
        players = [
            {"steamid64": "76561198342053209", "team": "T1_Alezin"},
            *[
                {"steamid64": f"765611980000000{idx:02d}", "team": "T1_Alezin" if idx <= 5 else "T2_dig4o"}
                for idx in range(2, 11)
            ],
        ]

        self.assertTrue(cog._player_snapshot_is_complete(match, players, s_id_hint="server1"))


if __name__ == "__main__":
    unittest.main()
