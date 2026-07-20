import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import discord

from bot.cogs import fila as fila_module
from bot.cogs.fila import FilaCog


class _DummyChannel:
    def __init__(self, channel_id: int):
        self.id = int(channel_id)


class _DummyVoiceState:
    def __init__(self, channel: _DummyChannel):
        self.channel = channel


class _DummyMember:
    def __init__(self, member_id: int, channel: _DummyChannel):
        self.id = int(member_id)
        self.bot = False
        self.voice = _DummyVoiceState(channel)
        self.display_name = f"user{member_id}"


class _DummyGuild:
    def __init__(self, members):
        self._members = {int(m.id): m for m in members}

    def get_member(self, user_id: int):
        return self._members.get(int(user_id))


class _DummyBot:
    def __init__(self, guild: _DummyGuild):
        self.guilds = [guild]
        self.dispatched = []

    def dispatch(self, event_name: str, payload):
        self.dispatched.append((event_name, payload))

    def get_channel(self, _channel_id: int):
        return None

    async def wait_until_ready(self):
        return None


class FilaPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_winner_priority_front_then_fill_tail_by_damage(self):
        queue_channel_id = 12345
        queue_channel = _DummyChannel(queue_channel_id)

        waiters = [
            _DummyMember(1, queue_channel),
            _DummyMember(2, queue_channel),
        ]
        winners_a = [
            _DummyMember(101, queue_channel),
            _DummyMember(102, queue_channel),
            _DummyMember(103, queue_channel),
            _DummyMember(104, queue_channel),
            _DummyMember(105, queue_channel),
        ]
        winners_b = [
            _DummyMember(201, queue_channel),
            _DummyMember(202, queue_channel),
            _DummyMember(203, queue_channel),
            _DummyMember(204, queue_channel),
            _DummyMember(205, queue_channel),
        ]

        guild = _DummyGuild(waiters + winners_a + winners_b)
        bot = _DummyBot(guild)
        cog = FilaCog(bot)
        cog.READY_RECHECK_DELAY_SECONDS = 0.0
        cog.READY_SEND_DELAY_SECONDS = 0.0

        now = discord.utils.utcnow()
        cog._queue = {
            1: {"join_time": now - timedelta(minutes=8), "damage": 10, "order": 1, "source": "queue"},
            2: {"join_time": now - timedelta(minutes=7), "damage": 8, "order": 2, "source": "queue"},
        }

        winners_a_stats = [
            (winners_a[0], 500),
            (winners_a[1], 200),
            (winners_a[2], 700),
            (winners_a[3], 300),
            (winners_a[4], 600),
        ]
        winners_b_stats = [
            (winners_b[0], 150),
            (winners_b[1], 420),
            (winners_b[2], 350),
            (winners_b[3], 90),
            (winners_b[4], 480),
        ]

        sorted_a_ids = [member.id for member, _ in sorted(winners_a_stats, key=lambda item: (-item[1], item[0].id))]
        sorted_b_ids = [member.id for member, _ in sorted(winners_b_stats, key=lambda item: (-item[1], item[0].id))]

        with patch.object(fila_module, "SALA_PROXIMO_ID", queue_channel_id):
            with patch.object(fila_module, "save_queue_member", AsyncMock()):
                with patch.object(fila_module, "remove_queue_member", AsyncMock()):
                    await cog.prioritize_match_winners(winners_a_stats)
                    after_a_ids = [uid for uid, _ in cog._queue_sorted()]
                    self.assertEqual(after_a_ids[:5], sorted_a_ids)
                    self.assertEqual(after_a_ids[5:7], [1, 2])

                    await cog.prioritize_match_winners(winners_b_stats)

        self.assertEqual(len(bot.dispatched), 1)
        event_name, batch = bot.dispatched[0]
        self.assertEqual(event_name, "fila_pronta")
        dispatched_ids = [int(item["member"].id) for item in batch]
        self.assertEqual(dispatched_ids, sorted_a_ids + [1, 2] + sorted_b_ids[:3])

        remaining_ids = [uid for uid, _ in cog._queue_sorted()]
        self.assertEqual(remaining_ids, sorted_b_ids[3:])

    async def test_losers_fill_after_existing_queue_by_damage(self):
        queue_channel_id = 12345
        queue_channel = _DummyChannel(queue_channel_id)

        waiters = [
            _DummyMember(1, queue_channel),
            _DummyMember(2, queue_channel),
            _DummyMember(3, queue_channel),
        ]
        winners = [
            _DummyMember(101, queue_channel),
            _DummyMember(102, queue_channel),
            _DummyMember(103, queue_channel),
            _DummyMember(104, queue_channel),
            _DummyMember(105, queue_channel),
        ]
        losers = [
            _DummyMember(201, queue_channel),
            _DummyMember(202, queue_channel),
            _DummyMember(203, queue_channel),
            _DummyMember(204, queue_channel),
            _DummyMember(205, queue_channel),
        ]

        guild = _DummyGuild(waiters + winners + losers)
        bot = _DummyBot(guild)
        cog = FilaCog(bot)
        cog.READY_RECHECK_DELAY_SECONDS = 0.0
        cog.READY_SEND_DELAY_SECONDS = 0.0

        now = discord.utils.utcnow()
        cog._queue = {
            1: {"join_time": now - timedelta(minutes=10), "damage": 0, "order": 1, "source": "queue"},
            2: {"join_time": now - timedelta(minutes=9), "damage": 0, "order": 2, "source": "queue"},
            3: {"join_time": now - timedelta(minutes=8), "damage": 0, "order": 3, "source": "queue"},
        }

        winners_stats = [
            (winners[0], 500),
            (winners[1], 200),
            (winners[2], 700),
            (winners[3], 300),
            (winners[4], 600),
        ]
        losers_stats = [
            (losers[0], 150),
            (losers[1], 420),
            (losers[2], 350),
            (losers[3], 90),
            (losers[4], 480),
        ]

        sorted_winner_ids = [member.id for member, _ in sorted(winners_stats, key=lambda item: (-item[1], item[0].id))]
        sorted_loser_ids = [member.id for member, _ in sorted(losers_stats, key=lambda item: (-item[1], item[0].id))]

        with patch.object(fila_module, "SALA_PROXIMO_ID", queue_channel_id):
            with patch.object(fila_module, "save_queue_member", AsyncMock()):
                with patch.object(fila_module, "remove_queue_member", AsyncMock()):
                    await cog.prioritize_match_winners(winners_stats)
                    await cog.prioritize_match_losers(losers_stats)

        self.assertEqual(len(bot.dispatched), 1)
        event_name, batch = bot.dispatched[0]
        self.assertEqual(event_name, "fila_pronta")
        dispatched_ids = [int(item["member"].id) for item in batch]
        self.assertEqual(dispatched_ids, sorted_winner_ids + [1, 2, 3] + sorted_loser_ids[:2])

        remaining_ids = [uid for uid, _ in cog._queue_sorted()]
        self.assertEqual(remaining_ids, sorted_loser_ids[2:])

    async def test_winner_priority_expires_on_next_day_rejoin(self):
        queue_channel_id = 12345
        queue_channel = _DummyChannel(queue_channel_id)
        member = _DummyMember(101, queue_channel)

        guild = _DummyGuild([member])
        bot = _DummyBot(guild)
        cog = FilaCog(bot)

        awarded_at = datetime(2026, 3, 12, 22, 30, tzinfo=timezone.utc)
        rejoin_time = datetime(2026, 3, 13, 15, 2, tzinfo=timezone.utc)
        cog._queue = {
            101: {
                "join_time": awarded_at,
                "damage": 2304,
                "order": 1,
                "source": "winner_front",
                "priority_awarded_at": awarded_at,
            }
        }

        before = type("Before", (), {"channel": None})()
        after = type("After", (), {"channel": queue_channel})()

        with patch.object(fila_module, "SALA_PROXIMO_ID", queue_channel_id):
            with patch.object(fila_module, "QUEUE_PRIORITY_TIMEZONE", ZoneInfo("Europe/Lisbon")):
                with patch.object(fila_module, "QUEUE_PRIORITY_RESET_HOUR", 8):
                    with patch.object(fila_module, "get_active_ban", AsyncMock(return_value=None)):
                        with patch.object(fila_module, "save_queue_member", AsyncMock()):
                            with patch.object(fila_module.discord.utils, "utcnow", return_value=rejoin_time):
                                await cog.on_voice_state_update(member, before, after)

        entry = cog._queue[101]
        self.assertEqual(entry["source"], "queue")
        self.assertEqual(entry["damage"], 0)
        self.assertIsNone(entry["priority_awarded_at"])
        self.assertEqual(entry["join_time"], rejoin_time)

    async def test_winner_priority_survives_overnight_until_reset_hour(self):
        queue_channel_id = 12345
        queue_channel = _DummyChannel(queue_channel_id)
        member = _DummyMember(101, queue_channel)

        guild = _DummyGuild([member])
        bot = _DummyBot(guild)
        cog = FilaCog(bot)

        awarded_at = datetime(2026, 3, 12, 22, 30, tzinfo=timezone.utc)
        rejoin_time = datetime(2026, 3, 13, 6, 30, tzinfo=timezone.utc)
        cog._queue = {
            101: {
                "join_time": awarded_at,
                "damage": 2304,
                "order": 1,
                "source": "winner_front",
                "priority_awarded_at": awarded_at,
            }
        }

        before = type("Before", (), {"channel": None})()
        after = type("After", (), {"channel": queue_channel})()

        with patch.object(fila_module, "SALA_PROXIMO_ID", queue_channel_id):
            with patch.object(fila_module, "QUEUE_PRIORITY_TIMEZONE", ZoneInfo("Europe/Lisbon")):
                with patch.object(fila_module, "QUEUE_PRIORITY_RESET_HOUR", 8):
                    with patch.object(fila_module, "get_active_ban", AsyncMock(return_value=None)):
                        with patch.object(fila_module, "save_queue_member", AsyncMock()):
                            with patch.object(fila_module.discord.utils, "utcnow", return_value=rejoin_time):
                                await cog.on_voice_state_update(member, before, after)

        entry = cog._queue[101]
        self.assertEqual(entry["source"], "winner_front")
        self.assertEqual(entry["damage"], 2304)
        self.assertEqual(entry["priority_awarded_at"], awarded_at)
        self.assertEqual(entry["join_time"], awarded_at)


if __name__ == "__main__":
    unittest.main()
