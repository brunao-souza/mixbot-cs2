import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import sync_bridge


class _DummyDb:
    def __init__(self):
        self.fetchone = AsyncMock()


class SyncBridgeBotTests(unittest.IsolatedAsyncioTestCase):
    def test_player_stats_insert_values_backfills_modern_columns(self):
        payload = sync_bridge._player_stats_insert_values(
            2_000_000_077,
            1,
            {
                "steamid64": "76561198000000001",
                "team": "Team A",
                "name": "Player",
                "kills": 22,
                "deaths": 15,
                "damage": 1900,
                "assists": 4,
                "head_shot_kills": 9,
                "cash_earned": 3200,
            },
        )
        row = dict(zip(sync_bridge._PLAYER_STATS_COLUMNS, payload))

        self.assertEqual(len(payload), len(sync_bridge._PLAYER_STATS_COLUMNS))
        self.assertEqual(row["matchid"], 2_000_000_077)
        self.assertEqual(row["steamid64"], "76561198000000001")
        self.assertEqual(row["kills"], 22)
        self.assertEqual(row["cash_earned"], 3200)
        self.assertEqual(row["health_points_removed_total"], 0)
        self.assertEqual(row["shots_fired_total"], 0)
        self.assertEqual(row["entry_count"], 0)
        self.assertEqual(row["equipment_value"], 0)
        self.assertEqual(row["enemies_flashed"], 0)

    async def test_sync_match_result_marks_applied_only_after_complete_sync(self):
        db = _DummyDb()
        db.fetchone.return_value = {
            "discord_id": 123,
            "nickname": "Player",
            "rating": 1100,
            "wins": 10,
            "losses": 3,
            "total_matches": 13,
            "win_streak": 4,
        }
        match_row = {
            "matchid": 77,
            "team1_name": "Team A",
            "team2_name": "Team B",
            "team1_score": 13,
            "team2_score": 9,
            "winner": "Team A",
            "start_time": None,
        }
        players_rows = [
            {"steamid64": "76561198000000001", "mapnumber": 1},
            {"steamid64": "76561198000000001", "mapnumber": 1},
        ]

        with (
            patch.object(sync_bridge, "_has_webapp_config", return_value=True),
            patch.object(sync_bridge, "_is_match_already_applied", AsyncMock(return_value=False)),
            patch.object(
                sync_bridge,
                "_load_match_bundle_with_retry",
                AsyncMock(return_value=(match_row, [], players_rows)),
            ),
            patch.object(sync_bridge, "_copy_match_stats", AsyncMock(return_value=True)) as copy_mock,
            patch.object(sync_bridge, "_upsert_ranking_in_webapp", AsyncMock(return_value=True)) as upsert_mock,
            patch.object(sync_bridge, "_mark_match_applied", AsyncMock()) as mark_mock,
        ):
            await sync_bridge.sync_match_result(77, db)

        copy_mock.assert_awaited_once()
        copy_kwargs = copy_mock.await_args.kwargs
        self.assertEqual(copy_kwargs, {})
        self.assertEqual(copy_mock.await_args.args[0], 77)
        self.assertEqual(len(copy_mock.await_args.args[2]), 1)
        self.assertEqual(copy_mock.await_args.args[2][0]["team1_score"], 13)
        self.assertEqual(upsert_mock.await_count, 1)
        mark_mock.assert_awaited_once_with(77)
        db.fetchone.assert_awaited_once()

    async def test_sync_match_result_does_not_mark_applied_when_copy_fails(self):
        db = _DummyDb()
        match_row = {"matchid": 88, "team1_score": 13, "team2_score": 4}
        players_rows = [{"steamid64": "76561198000000002", "mapnumber": 1}]

        with (
            patch.object(sync_bridge, "_has_webapp_config", return_value=True),
            patch.object(sync_bridge, "_is_match_already_applied", AsyncMock(return_value=False)),
            patch.object(
                sync_bridge,
                "_load_match_bundle_with_retry",
                AsyncMock(return_value=(match_row, [], players_rows)),
            ),
            patch.object(sync_bridge, "_copy_match_stats", AsyncMock(return_value=False)) as copy_mock,
            patch.object(sync_bridge, "_upsert_ranking_in_webapp", AsyncMock()) as upsert_mock,
            patch.object(sync_bridge, "_mark_match_applied", AsyncMock()) as mark_mock,
        ):
            await sync_bridge.sync_match_result(88, db)

        copy_mock.assert_awaited_once()
        upsert_mock.assert_not_awaited()
        mark_mock.assert_not_awaited()
        db.fetchone.assert_not_awaited()

    async def test_sync_match_result_does_not_mark_applied_when_player_snapshot_missing(self):
        db = _DummyDb()
        db.fetchone.return_value = None
        match_row = {
            "matchid": 91,
            "team1_name": "A",
            "team2_name": "B",
            "team1_score": 13,
            "team2_score": 11,
        }
        players_rows = [{"steamid64": "76561198000000003", "mapnumber": 1}]

        with (
            patch.object(sync_bridge, "_has_webapp_config", return_value=True),
            patch.object(sync_bridge, "_is_match_already_applied", AsyncMock(return_value=False)),
            patch.object(
                sync_bridge,
                "_load_match_bundle_with_retry",
                AsyncMock(return_value=(match_row, [], players_rows)),
            ),
            patch.object(sync_bridge, "_copy_match_stats", AsyncMock(return_value=True)),
            patch.object(sync_bridge, "_upsert_ranking_in_webapp", AsyncMock()) as upsert_mock,
            patch.object(sync_bridge, "_mark_match_applied", AsyncMock()) as mark_mock,
        ):
            await sync_bridge.sync_match_result(91, db)

        db.fetchone.assert_awaited_once()
        upsert_mock.assert_not_awaited()
        mark_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
