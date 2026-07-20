import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from loguru import logger

from bot.config import (
    CANAL_FILA_ID,
    MEMBER_ROLE_ID,
    MEMBER_ROLE_NAME,
    QUEUE_PRIORITY_RESET_HOUR,
    QUEUE_PRIORITY_TIMEZONE,
    SALA_PROXIMO_ID,
    SALA_SAIDA_ID,
)
from bot.database import (
    get_saved_queue,
    get_active_ban,
    remove_queue_member,
    save_queue_member,
    set_queue_member_damage,
)


def get_fila_cog(bot: commands.Bot):
    return bot.get_cog("FilaCog")


class FilaCog(commands.Cog):
    READY_RECHECK_DELAY_SECONDS = 10.0
    READY_SEND_DELAY_SECONDS = 0.0

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._queue: Dict[int, Dict[str, Any]] = {}
        self._pending_damage: Dict[int, int] = {}
        self._pending_winner_ids: set[int] = set()
        self._pending_priority_awarded_at: Dict[int, datetime] = {}
        self._queue_message: discord.Message | None = None
        self._queue_ping_message: discord.Message | None = None
        self._needs_display_update = True
        self._ready_dispatch_task: asyncio.Task | None = None

    async def cog_load(self):
        self.display_loop.start()
        self.bot.loop.create_task(self.restore_queue_on_startup())
        logger.debug("FilaCog carregado.")

    async def cog_unload(self):
        self.display_loop.cancel()
        if self._ready_dispatch_task and not self._ready_dispatch_task.done():
            self._ready_dispatch_task.cancel()

    def _normalize_dt(self, value):
        if value is None:
            return discord.utils.utcnow()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _legacy_sort_key(self, uid: int, entry: Dict[str, Any]):
        return (
            self._normalize_dt(entry.get("join_time")),
            -int(entry.get("damage") or 0),
            int(uid),
        )

    def _priority_window_key(self, value):
        local_dt = self._normalize_dt(value).astimezone(QUEUE_PRIORITY_TIMEZONE)
        if local_dt.hour < int(QUEUE_PRIORITY_RESET_HOUR):
            local_dt = local_dt.replace(hour=0, minute=0, second=0, microsecond=0) - datetime.resolution
        return local_dt.date()

    def _entry_has_winner_priority(self, entry: Optional[Dict[str, Any]]) -> bool:
        if not entry:
            return False
        if entry.get("priority_awarded_at") is not None:
            return True
        return str(entry.get("source") or "").startswith("winner_")

    def _winner_priority_is_stale(self, entry: Optional[Dict[str, Any]], *, reference_time=None) -> bool:
        if not self._entry_has_winner_priority(entry):
            return False
        reference_dt = self._normalize_dt(reference_time)
        awarded_at = (entry or {}).get("priority_awarded_at") or (entry or {}).get("join_time")
        if awarded_at is None:
            return False
        return self._priority_window_key(awarded_at) != self._priority_window_key(reference_dt)

    async def _expire_stale_winner_priority_locked(self, *, now=None) -> bool:
        reference_dt = self._normalize_dt(now)
        stale_entry_ids = [
            int(uid)
            for uid, entry in self._queue.items()
            if self._winner_priority_is_stale(entry, reference_time=reference_dt)
        ]
        stale_pending_ids = [
            int(uid)
            for uid in self._pending_winner_ids
            if self._priority_window_key(self._pending_priority_awarded_at.get(int(uid)) or reference_dt)
            != self._priority_window_key(reference_dt)
        ]

        changed = False
        next_tail_order = self._next_tail_order_locked() if stale_entry_ids else None
        for uid in stale_entry_ids:
            entry = self._queue.get(uid)
            if entry is None:
                continue
            entry["join_time"] = reference_dt
            entry["damage"] = 0
            entry["source"] = "queue"
            entry["priority_awarded_at"] = None
            entry["order"] = int(next_tail_order or self._next_tail_order_locked())
            if next_tail_order is not None:
                next_tail_order += 1
            self._pending_damage.pop(uid, None)
            self._pending_winner_ids.discard(uid)
            self._pending_priority_awarded_at.pop(uid, None)
            await save_queue_member(uid, joined_at=reference_dt, damage=0, priority_awarded_at=None)
            changed = True

        for uid in stale_pending_ids:
            self._pending_winner_ids.discard(uid)
            self._pending_damage.pop(uid, None)
            self._pending_priority_awarded_at.pop(uid, None)
            changed = True

        if changed:
            self._needs_display_update = True
        return changed

    def _ensure_order_metadata_locked(self):
        next_order = 1
        for uid, entry in sorted(self._queue.items(), key=lambda item: self._legacy_sort_key(item[0], item[1])):
            if not isinstance(entry.get("order"), int):
                entry["order"] = int(next_order)
            if not entry.get("source"):
                entry["source"] = "queue"
            next_order += 1

    def _next_tail_order_locked(self) -> int:
        self._ensure_order_metadata_locked()
        if not self._queue:
            return 1
        return max(int(entry.get("order") or 0) for entry in self._queue.values()) + 1

    def _queue_sorted(self):
        self._ensure_order_metadata_locked()
        return sorted(
            self._queue.items(),
            key=lambda item: (
                int(item[1].get("order") or 0),
                self._normalize_dt(item[1].get("join_time")),
                -int(item[1].get("damage") or 0),
                int(item[0]),
            ),
        )

    async def _dispatch_batches(self, batches: List[List[Dict[str, Any]]]):
        for batch in batches:
            self.bot.dispatch("fila_pronta", batch)

    def _has_grace_delay(self) -> bool:
        return self.READY_RECHECK_DELAY_SECONDS > 0 or self.READY_SEND_DELAY_SECONDS > 0

    def _get_primary_guild(self) -> Optional[discord.Guild]:
        if not self.bot.guilds:
            return None
        return self.bot.guilds[0]

    def _get_member_role(self, guild: Optional[discord.Guild]) -> Optional[discord.Role]:
        if guild is None:
            return None
        role = guild.get_role(MEMBER_ROLE_ID) if MEMBER_ROLE_ID else None
        if role is None and MEMBER_ROLE_NAME:
            role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
        return role

    def _build_queue_ping_content(self, role: discord.Role) -> Optional[str]:
        missing = max(0, 10 - len(self._queue))
        if missing <= 0:
            return None
        return f"+{missing} MIX {role.mention}"

    async def _find_queue_ping_message(self, channel: discord.abc.Messageable) -> Optional[discord.Message]:
        try:
            async for msg in channel.history(limit=15):
                if msg.author != self.bot.user or not msg.content:
                    continue
                if msg.content.startswith("+") and " MIX " in msg.content:
                    return msg
        except Exception:
            return None
        return None

    async def _sync_queue_ping_message(self, channel: discord.abc.Messageable) -> None:
        if not self._queue:
            if self._queue_ping_message is None:
                self._queue_ping_message = await self._find_queue_ping_message(channel)
            if self._queue_ping_message is not None:
                try:
                    await self._queue_ping_message.delete()
                except discord.NotFound:
                    pass
                except Exception as exc:
                    logger.warning(f"Falha ao remover ping da fila: {exc}")
                self._queue_ping_message = None
            return

        role = self._get_member_role(getattr(channel, "guild", None))
        if role is None:
            return
        content = self._build_queue_ping_content(role)
        if content is None:
            if self._queue_ping_message is None:
                self._queue_ping_message = await self._find_queue_ping_message(channel)
            if self._queue_ping_message is not None:
                try:
                    await self._queue_ping_message.delete()
                except discord.NotFound:
                    pass
                except Exception as exc:
                    logger.warning(f"Falha ao remover ping da fila: {exc}")
                self._queue_ping_message = None
            return

        if self._queue_ping_message is None:
            self._queue_ping_message = await self._find_queue_ping_message(channel)
        try:
            if self._queue_ping_message is not None:
                try:
                    await self._queue_ping_message.delete()
                except discord.NotFound:
                    pass
                self._queue_ping_message = None

            self._queue_ping_message = await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except Exception as exc:
            logger.warning(f"Falha ao sincronizar ping da fila: {exc}")

    def _schedule_ready_dispatch(self, guild: Optional[discord.Guild]):
        if guild is None or not self._has_grace_delay():
            return
        if self._ready_dispatch_task and not self._ready_dispatch_task.done():
            return
        # Não agenda dispatch enquanto vencedores de partida ainda não foram priorizados.
        if self._pending_winner_ids:
            return
        self._ready_dispatch_task = self.bot.loop.create_task(self._run_ready_dispatch_after_grace(int(guild.id)))

    async def _count_valid_candidates_locked(self, guild: discord.Guild, *, purge_stale: bool) -> int:
        await self._expire_stale_winner_priority_locked()
        valid_count = 0
        stale_ids: List[int] = []
        for uid, _entry in self._queue_sorted():
            member = guild.get_member(uid)
            if not member or member.bot:
                stale_ids.append(uid)
                continue
            if not member.voice or not member.voice.channel or member.voice.channel.id != SALA_PROXIMO_ID:
                stale_ids.append(uid)
                continue
            valid_count += 1
            if valid_count >= 10:
                break

        changed = False
        if purge_stale:
            for uid in stale_ids:
                if self._queue.pop(uid, None) is not None:
                    changed = True
                    await remove_queue_member(uid)
            if changed:
                self._needs_display_update = True
        return valid_count

    async def _run_ready_dispatch_after_grace(self, guild_id: int):
        try:
            if self.READY_RECHECK_DELAY_SECONDS > 0:
                await asyncio.sleep(self.READY_RECHECK_DELAY_SECONDS)

            async with self._lock:
                guild = self.bot.get_guild(int(guild_id)) or self._get_primary_guild()
                if guild is None:
                    return
                if len(self._queue) < 10:
                    return
                valid_count = await self._count_valid_candidates_locked(guild, purge_stale=True)
                if valid_count < 10:
                    return

            if self.READY_SEND_DELAY_SECONDS > 0:
                await asyncio.sleep(self.READY_SEND_DELAY_SECONDS)

            batches: List[List[Dict[str, Any]]] = []
            async with self._lock:
                guild = self.bot.get_guild(int(guild_id)) or self._get_primary_guild()
                if guild is None:
                    return
                batches = await self._collect_ready_batches_locked(guild)
            await self._dispatch_batches(batches)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"Falha no agendamento de fila pronta: {exc}")
        finally:
            self._ready_dispatch_task = None

    async def register_match_winners(self, values: Dict[int, int]):
        if not values:
            return
        async with self._lock:
            await self._expire_stale_winner_priority_locked()
            # Cancela qualquer dispatch ativo para evitar que um timer antigo
            # dispare enquanto os vencedores ainda não foram priorizados.
            if self._ready_dispatch_task and not self._ready_dispatch_task.done():
                self._ready_dispatch_task.cancel()
                self._ready_dispatch_task = None
            awarded_at = discord.utils.utcnow()
            for uid, dmg in values.items():
                user_id = int(uid)
                damage = int(dmg or 0)
                if damage < 0:
                    damage = 0
                self._pending_damage[user_id] = damage
                self._pending_winner_ids.add(user_id)
                self._pending_priority_awarded_at[user_id] = awarded_at
                entry = self._queue.get(user_id)
                if entry is not None:
                    entry["damage"] = damage
                    await set_queue_member_damage(user_id, damage)
            self._needs_display_update = True

    async def prioritize_match_winners(self, winners: List[Tuple[discord.Member, int]]):
        if not winners:
            return
        batches: List[List[Dict[str, Any]]] = []
        normalized: List[Tuple[discord.Member, int]] = []
        for member, damage in winners:
            if member is None or member.bot:
                continue
            normalized.append((member, max(0, int(damage or 0))))
        if not normalized:
            return
        ordered_winners = sorted(normalized, key=lambda item: (-int(item[1] or 0), int(item[0].id)))
        async with self._lock:
            await self._expire_stale_winner_priority_locked()
            self._ensure_order_metadata_locked()
            has_winner_block = any(str(entry.get("source") or "").startswith("winner_") for entry in self._queue.values())
            if has_winner_block:
                insert_mode = "tail"
                next_order = self._next_tail_order_locked()
            else:
                insert_mode = "front"
                min_order = min((int(entry.get("order") or 0) for entry in self._queue.values()), default=1)
                next_order = min_order - len(ordered_winners)

            for member, damage in ordered_winners:
                uid = int(member.id)
                is_in_queue_room = bool(
                    member.voice and member.voice.channel and int(member.voice.channel.id) == int(SALA_PROXIMO_ID)
                )
                if not is_in_queue_room:
                    self._pending_winner_ids.discard(uid)
                    continue

                existing = self._queue.get(uid) or {}
                join_time = self._normalize_dt(existing.get("join_time"))
                source = "winner_front" if insert_mode == "front" else "winner_tail"
                priority_awarded_at = self._normalize_dt(
                    self._pending_priority_awarded_at.get(uid) or existing.get("priority_awarded_at") or discord.utils.utcnow()
                )
                self._queue[uid] = {
                    "join_time": join_time,
                    "damage": int(damage),
                    "order": int(next_order),
                    "source": source,
                    "priority_awarded_at": priority_awarded_at,
                }
                next_order += 1
                await save_queue_member(
                    uid,
                    joined_at=join_time,
                    damage=int(damage),
                    priority_awarded_at=priority_awarded_at,
                )
                self._pending_damage[uid] = int(damage)
                self._pending_winner_ids.discard(uid)
                self._pending_priority_awarded_at.pop(uid, None)

            self._needs_display_update = True
            if self.bot.guilds:
                guild = self._get_primary_guild()
                if guild and len(self._queue) >= 10:
                    if self._has_grace_delay():
                        self._schedule_ready_dispatch(guild)
                    else:
                        batches = await self._collect_ready_batches_locked(guild)
        await self._dispatch_batches(batches)

    async def prioritize_match_losers(self, losers: List[Tuple[discord.Member, int]]):
        if not losers:
            return
        batches: List[List[Dict[str, Any]]] = []
        normalized: List[Tuple[discord.Member, int]] = []
        for member, damage in losers:
            if member is None or member.bot:
                continue
            normalized.append((member, max(0, int(damage or 0))))
        if not normalized:
            return

        ordered_losers = sorted(normalized, key=lambda item: (-int(item[1] or 0), int(item[0].id)))
        async with self._lock:
            await self._expire_stale_winner_priority_locked()
            self._ensure_order_metadata_locked()
            next_order = self._next_tail_order_locked()

            for member, damage in ordered_losers:
                uid = int(member.id)
                is_in_queue_room = bool(
                    member.voice and member.voice.channel and int(member.voice.channel.id) == int(SALA_PROXIMO_ID)
                )
                if not is_in_queue_room:
                    continue

                existing = self._queue.get(uid) or {}
                join_time = self._normalize_dt(existing.get("join_time"))
                self._queue[uid] = {
                    "join_time": join_time,
                    "damage": int(damage),
                    "order": int(next_order),
                    "source": "loser_tail",
                    "priority_awarded_at": None,
                }
                next_order += 1
                await save_queue_member(
                    uid,
                    joined_at=join_time,
                    damage=int(damage),
                    priority_awarded_at=None,
                )
                self._pending_damage[uid] = int(damage)

            self._needs_display_update = True
            if self.bot.guilds:
                guild = self._get_primary_guild()
                if guild and len(self._queue) >= 10:
                    if self._has_grace_delay():
                        self._schedule_ready_dispatch(guild)
                    else:
                        batches = await self._collect_ready_batches_locked(guild)
        await self._dispatch_batches(batches)

    async def _collect_ready_batches_locked(self, guild: discord.Guild):
        await self._expire_stale_winner_priority_locked()
        batches: List[List[Dict[str, Any]]] = []
        while len(self._queue) >= 10:
            chosen: List[Dict[str, Any]] = []
            stale_ids: List[int] = []
            for uid, entry in self._queue_sorted():
                member = guild.get_member(uid)
                if not member or member.bot:
                    stale_ids.append(uid)
                    continue
                if not member.voice or not member.voice.channel or member.voice.channel.id != SALA_PROXIMO_ID:
                    stale_ids.append(uid)
                    continue
                chosen.append(
                    {
                        "member": member,
                        "join_time": self._normalize_dt(entry.get("join_time")),
                        "damage": int(entry.get("damage") or 0),
                        "order": int(entry.get("order") or 0),
                        "source": str(entry.get("source") or "queue"),
                        "priority_awarded_at": entry.get("priority_awarded_at"),
                    }
                )
                if len(chosen) == 10:
                    break

            changed = False
            for uid in stale_ids:
                if self._queue.pop(uid, None) is not None:
                    changed = True
                    await remove_queue_member(uid)

            if len(chosen) < 10:
                if changed:
                    self._needs_display_update = True
                    continue
                break

            for item in chosen:
                uid = int(item["member"].id)
                self._queue.pop(uid, None)
                await remove_queue_member(uid)

            self._needs_display_update = True
            batches.append(chosen)
        return batches

    async def request_display_update(self):
        self._needs_display_update = True

    async def remove_players_from_queue(self, user_ids: List[int]):
        if not user_ids:
            return
        async with self._lock:
            changed = False
            for uid in user_ids:
                if self._queue.pop(int(uid), None) is not None:
                    changed = True
                await remove_queue_member(int(uid))
            if changed:
                self._needs_display_update = True

    async def take_next_players(self, guild: discord.Guild, count: int) -> List[discord.Member]:
        if count <= 0:
            return []
        selected: List[discord.Member] = []
        batches: List[List[Dict[str, Any]]] = []
        async with self._lock:
            await self._expire_stale_winner_priority_locked()
            for uid, entry in self._queue_sorted():
                if len(selected) >= count:
                    break
                member = guild.get_member(uid)
                if not member or member.bot:
                    self._queue.pop(uid, None)
                    await remove_queue_member(uid)
                    self._needs_display_update = True
                    continue
                if not member.voice or not member.voice.channel or member.voice.channel.id != SALA_PROXIMO_ID:
                    self._queue.pop(uid, None)
                    await remove_queue_member(uid)
                    self._needs_display_update = True
                    continue
                selected.append(member)

            for member in selected:
                uid = int(member.id)
                self._queue.pop(uid, None)
                await remove_queue_member(uid)

            if selected:
                self._needs_display_update = True
            batches = await self._collect_ready_batches_locked(guild)

        await self._dispatch_batches(batches)
        return selected

    async def set_recent_damage(self, discord_id: int, damage: int):
        uid = int(discord_id)
        dmg = int(damage or 0)
        if dmg < 0:
            dmg = 0
        async with self._lock:
            self._pending_damage[uid] = dmg
            entry = self._queue.get(uid)
            if entry is not None:
                entry["damage"] = dmg
                await set_queue_member_damage(uid, dmg)
                self._needs_display_update = True

    async def set_recent_damage_bulk(self, values: Dict[int, int]):
        if not values:
            return
        for uid, dmg in values.items():
            await self.set_recent_damage(int(uid), int(dmg or 0))

    async def dispatch_ready_if_possible(self):
        if not self.bot.guilds:
            return
        batches: List[List[Dict[str, Any]]] = []
        async with self._lock:
            await self._expire_stale_winner_priority_locked()
            guild = self._get_primary_guild()
            if guild is None:
                return
            if self._has_grace_delay():
                if len(self._queue) >= 10:
                    self._schedule_ready_dispatch(guild)
            else:
                batches = await self._collect_ready_batches_locked(guild)
        for batch in batches:
            self.bot.dispatch("fila_pronta", batch)

    async def requeue_payload(self, payload: List[Dict[str, Any]], dispatch_ready: bool = False):
        if not payload:
            return
        batches: List[List[Dict[str, Any]]] = []
        async with self._lock:
            await self._expire_stale_winner_priority_locked()
            for item in payload:
                member = item.get("member")
                if not member or member.bot:
                    continue
                if not member.voice or not member.voice.channel or member.voice.channel.id != SALA_PROXIMO_ID:
                    continue
                uid = int(member.id)
                join_time = self._normalize_dt(item.get("join_time"))
                damage = int(item.get("damage") or self._pending_damage.get(uid, 0) or 0)
                existing = self._queue.get(uid) or {}
                order_value: Optional[int] = item.get("order")
                if not isinstance(order_value, int):
                    order_value = existing.get("order") if isinstance(existing.get("order"), int) else None
                if not isinstance(order_value, int):
                    order_value = self._next_tail_order_locked()
                source_value = str(item.get("source") or existing.get("source") or "queue")
                priority_awarded_at = item.get("priority_awarded_at")
                self._queue[uid] = {
                    "join_time": join_time,
                    "damage": damage,
                    "order": int(order_value),
                    "source": source_value,
                    "priority_awarded_at": self._normalize_dt(priority_awarded_at) if priority_awarded_at else None,
                }
                await save_queue_member(
                    uid,
                    joined_at=join_time,
                    damage=damage,
                    priority_awarded_at=self._queue[uid].get("priority_awarded_at"),
                )
            self._needs_display_update = True
            if dispatch_ready and self.bot.guilds:
                guild = self._get_primary_guild()
                if guild and len(self._queue) >= 10:
                    if self._has_grace_delay():
                        self._schedule_ready_dispatch(guild)
                    else:
                        batches = await self._collect_ready_batches_locked(guild)
        await self._dispatch_batches(batches)

    async def restore_queue_on_startup(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(2)

        room = self.bot.get_channel(SALA_PROXIMO_ID)
        if not room:
            return

        saved_users = await get_saved_queue()
        voice_members = {m.id: m for m in room.members if not m.bot}
        batches: List[List[Dict[str, Any]]] = []
        async with self._lock:
            self._queue.clear()
            for row in saved_users:
                uid = int(row.get("discord_id") or 0)
                if uid <= 0:
                    continue
                try:
                    if await get_active_ban(uid):
                        await remove_queue_member(uid)
                        continue
                except Exception:
                    pass

                if uid in voice_members:
                    join_time = self._normalize_dt(row.get("joined_at"))
                    damage = int(row.get("last_damage") or 0)
                    priority_awarded_at = row.get("priority_awarded_at")
                    self._queue[uid] = {
                        "join_time": join_time,
                        "damage": damage,
                        "source": "queue",
                        "priority_awarded_at": self._normalize_dt(priority_awarded_at) if priority_awarded_at else None,
                    }
                    del voice_members[uid]
                else:
                    await remove_queue_member(uid)

            now = discord.utils.utcnow()
            for uid, member in voice_members.items():
                try:
                    if await get_active_ban(uid):
                        sala_saida = self.bot.get_channel(SALA_SAIDA_ID) if SALA_SAIDA_ID else None
                        if sala_saida and member.voice:
                            try:
                                await member.move_to(sala_saida)
                            except Exception:
                                pass
                        continue
                except Exception:
                    pass
                damage = int(self._pending_damage.pop(uid, 0))
                self._queue[uid] = {"join_time": now, "damage": damage, "source": "queue", "priority_awarded_at": None}
                await save_queue_member(uid, joined_at=now, damage=damage, priority_awarded_at=None)

            await self._expire_stale_winner_priority_locked(now=now)
            self._ensure_order_metadata_locked()
            self._needs_display_update = True
            if len(self._queue) >= 10:
                if self._has_grace_delay():
                    self._schedule_ready_dispatch(room.guild)
                else:
                    batches = await self._collect_ready_batches_locked(room.guild)

        await self._dispatch_batches(batches)

        logger.info(f"\u2705 Fila reconstruida: {len(self._queue)} jogadores.")

    async def _render_queue_embed(self):
        channel = self.bot.get_channel(CANAL_FILA_ID)
        if not channel:
            return

        async with self._lock:
            await self._expire_stale_winner_priority_locked()

        embed = discord.Embed(
            title="📋 FILA DE ESPERA",
            color=0x3498DB,
            timestamp=discord.utils.utcnow(),
        )

        if not self._queue:
            embed.add_field(name="💤 Status", value="Fila vazia", inline=False)
        else:
            now = discord.utils.utcnow()
            lines = []
            for pos, (uid, entry) in enumerate(self._queue_sorted(), 1):
                member = channel.guild.get_member(uid)
                join_time = self._normalize_dt(entry.get("join_time"))
                mins = int(max(0, (now - join_time).total_seconds() // 60))
                damage = int(entry.get("damage") or 0)
                name = member.display_name if member else "Saiu"
                dmg_suffix = f" | 💥 {damage}" if damage > 0 else ""
                lines.append(f"`#{pos:02}` - **{name}** 🔹 🕒 {mins}m{dmg_suffix}")
            embed.add_field(name=f"👥 Jogadores ({len(self._queue)})", value="\n".join(lines), inline=False)

        if self._queue_message is None:
            try:
                async for msg in channel.history(limit=15):
                    title = (msg.embeds[0].title or "").upper() if msg.embeds else ""
                    if msg.author == self.bot.user and msg.embeds and "FILA DE ESPERA" in title:
                        self._queue_message = msg
                        break
            except Exception:
                self._queue_message = None

        try:
            if self._queue_message:
                await self._queue_message.edit(embed=embed)
            else:
                self._queue_message = await channel.send(embed=embed)
        except discord.NotFound:
            self._queue_message = await channel.send(embed=embed)
        except Exception as exc:
            logger.warning(f"Falha ao atualizar embed da fila: {exc}")

        await self._sync_queue_ping_message(channel)

    @tasks.loop(seconds=3)
    async def display_loop(self):
        if not self._needs_display_update:
            return
        self._needs_display_update = False
        await self._render_queue_embed()

    @display_loop.before_loop
    async def _before_display_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if member.bot or before.channel == after.channel:
            return

        if after.channel and after.channel.id == SALA_PROXIMO_ID:
            try:
                ban = await get_active_ban(member.id)
            except Exception:
                ban = None
            if ban:
                sala_saida = self.bot.get_channel(SALA_SAIDA_ID) if SALA_SAIDA_ID else None
                if sala_saida and member.voice:
                    try:
                        await member.move_to(sala_saida)
                    except Exception:
                        pass
                try:
                    await member.send("Voce possui punicao ativa e nao pode entrar na fila.")
                except Exception:
                    pass
                return

            batches: List[List[Dict[str, Any]]] = []
            async with self._lock:
                now = discord.utils.utcnow()
                await self._expire_stale_winner_priority_locked(now=now)
                uid = int(member.id)
                existing = self._queue.get(uid)
                existing_damage = int((existing or {}).get("damage") or 0)
                damage = int(self._pending_damage.pop(uid, existing_damage))
                if existing is None:
                    entry_order = self._next_tail_order_locked()
                    join_time = now
                    source_value = "queue"
                else:
                    join_time = self._normalize_dt(existing.get("join_time"))
                    entry_order = int(existing.get("order") or self._next_tail_order_locked())
                    source_value = str(existing.get("source") or "queue")
                self._queue[uid] = {
                    "join_time": join_time,
                    "damage": damage,
                    "order": int(entry_order),
                    "source": source_value,
                    "priority_awarded_at": existing.get("priority_awarded_at") if existing else None,
                }
                await save_queue_member(
                    uid,
                    joined_at=join_time,
                    damage=damage,
                    priority_awarded_at=self._queue[uid].get("priority_awarded_at"),
                )
                self._needs_display_update = True
                if uid not in self._pending_winner_ids and len(self._queue) >= 10:
                    if self._has_grace_delay():
                        self._schedule_ready_dispatch(member.guild)
                    else:
                        batches = await self._collect_ready_batches_locked(member.guild)

            await self._dispatch_batches(batches)
            return

        if before.channel and before.channel.id == SALA_PROXIMO_ID:
            async with self._lock:
                if self._queue.pop(int(member.id), None) is not None:
                    self._needs_display_update = True
                await remove_queue_member(int(member.id))
                self._pending_winner_ids.discard(int(member.id))
                self._pending_priority_awarded_at.pop(int(member.id), None)
                self._pending_damage.pop(int(member.id), None)


async def setup(bot: commands.Bot):
    await bot.add_cog(FilaCog(bot))
