"""Discord client: watches threads, debounces changes, and feeds the sync engine.

Three mechanisms keep the docs current:
  * gateway events (new/edited/deleted messages, thread create/rename) mark a thread dirty;
  * a debounced worker re-fetches each dirty thread and applies it, one at a time;
  * a periodic scan compares Discord against the state DB, so anything missed while the bot
    was offline or disconnected is picked up automatically.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass

import discord

from .config import Config
from .discord_source import (
    THREAD_PARENT_TYPES,
    active_threads_by_parent,
    archived_threads,
    snapshot_thread,
)
from .notify import DISCORD_LIMIT, split_message
from .reminder import format_message, plan_reminder, should_send
from .reminder_source import collect_items
from .state import StateStore
from .sync import SyncEngine

log = logging.getLogger(__name__)

ARCHIVED_POLL_LIMIT = 25  # archived threads inspected per periodic poll (startup scans all)
MAX_RETRY_DELAY = 600
ONCE_MAX_ATTEMPTS = 3


@dataclass
class _Pending:
    due: float
    attempts: int = 0


class SyncBot(discord.Client):
    """mode: 'run' (forever), 'once' (sync everything, then exit), 'check' (diagnose, then exit)
    or 'reminder' (build the reminder once, print it, send it only if send=True, then exit)."""

    def __init__(
        self,
        config: Config,
        engine: SyncEngine,
        state: StateStore,
        *,
        mode: str = "run",
        telegram=None,
        send: bool = False,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable it in the Discord developer portal
        super().__init__(intents=intents)
        self._cfg = config
        self._engine = engine
        self._state = state
        self._mode = mode
        self._telegram = telegram  # TelegramNotifier or None
        self._send = send
        self._watched = config.watched_channels()
        # Threads whose report author is not mapped to any student (e.g. the teacher's own threads).
        self._ignored: set[int] = set()
        self._pending: dict[int, _Pending] = {}
        self._busy = False
        self._started = False
        self._initial_scan_done = False
        self._scan_now = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.failures: list[int] = []

    # -- scheduling --------------------------------------------------------

    def _mark(self, thread_id: int, delay: float) -> None:
        if thread_id in self._ignored:
            return
        old = self._pending.get(thread_id)
        self._pending[thread_id] = _Pending(time.monotonic() + delay, old.attempts if old else 0)

    def _is_ours(self, channel_id: int) -> bool:
        """True for a thread under a mapped channel, or one we already track."""
        if self._state.get(channel_id) is not None:
            return True
        ch = self.get_channel(channel_id)
        return isinstance(ch, discord.Thread) and ch.parent_id in self._watched

    # -- lifecycle ---------------------------------------------------------

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        if self._started:
            self._scan_now.set()  # reconnected: catch up right away
            return
        self._started = True
        if self._mode == "check":
            await self._diagnose()
            await self.close()
            return
        if self._mode == "reminder":
            await self._reminder_once()
            await self.close()
            return
        self._tasks = [
            asyncio.create_task(self._worker(), name="worker"),
            asyncio.create_task(self._poll_loop(), name="poll"),
        ]
        if self._cfg.reminder.enabled and self._mode == "run":
            self._tasks.append(asyncio.create_task(self._reminder_loop(), name="reminder"))

    async def close(self) -> None:
        current = asyncio.current_task()
        for t in self._tasks:
            if t is not current:  # close() may be called from the worker/poll task itself
                t.cancel()
        await super().close()

    # -- events ------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        ch = message.channel
        if isinstance(ch, discord.Thread) and ch.parent_id in self._watched:
            self._mark(ch.id, self._cfg.debounce_seconds)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if self._is_ours(payload.channel_id):
            self._mark(payload.channel_id, self._cfg.debounce_seconds)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if self._is_ours(payload.channel_id):
            self._mark(payload.channel_id, self._cfg.debounce_seconds)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if self._is_ours(payload.channel_id):
            self._mark(payload.channel_id, self._cfg.debounce_seconds)

    async def on_thread_create(self, thread: discord.Thread) -> None:
        if thread.parent_id in self._watched:
            self._mark(thread.id, self._cfg.debounce_seconds)

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        if after.parent_id in self._watched:
            self._mark(after.id, self._cfg.debounce_seconds)

    async def on_thread_delete(self, thread: discord.Thread) -> None:
        if self._state.get(thread.id) is not None:
            log.info("Thread %s was deleted on Discord; its Google Docs tab is kept", thread.id)

    # -- worker ------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            now = time.monotonic()
            ready = [tid for tid, p in self._pending.items() if p.due <= now]
            if not ready:
                if self._mode == "once" and self._initial_scan_done and not self._pending:
                    log.info("Initial sync finished")
                    await self.close()
                    return
                await asyncio.sleep(1)
                continue
            # Oldest thread first: snowflake ids are chronological, so tabs get created in order.
            tid = min(ready)
            pending = self._pending.pop(tid)
            try:
                await self._sync_thread(tid)
            except asyncio.CancelledError:
                raise
            except Exception:
                attempts = pending.attempts + 1
                if self._mode == "once" and attempts >= ONCE_MAX_ATTEMPTS:
                    log.exception("Giving up on thread %s", tid)
                    self.failures.append(tid)
                    continue
                delay = min(MAX_RETRY_DELAY, 30 * 2 ** (attempts - 1))
                log.exception("Sync of thread %s failed (attempt %d); retrying in %ds", tid, attempts, delay)
                self._pending.setdefault(tid, _Pending(time.monotonic() + delay, attempts))

    async def _sync_thread(self, thread_id: int) -> None:
        channel = self.get_channel(thread_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(thread_id)
            except discord.NotFound:
                log.info("Thread %s no longer exists; skipping", thread_id)
                return
        if not isinstance(channel, discord.Thread):
            return
        if channel.parent_id not in self._watched:
            return
        snap = await snapshot_thread(channel)
        student = self._cfg.route(channel.parent_id, snap.author_id)
        if student is None:
            self._ignored.add(thread_id)
            log.info(
                "Skipping thread %r (%s): its report author (%s) is not mapped to a student",
                channel.name, thread_id, snap.author_id,
            )
            return
        await asyncio.to_thread(self._engine.apply, student, snap)

    # -- periodic scan -----------------------------------------------------

    async def _poll_loop(self) -> None:
        full = True  # first pass looks at every thread, including all archived ones
        while True:
            try:
                await self._scan(full)
                full = False
                self._initial_scan_done = True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scan failed")
                if self._mode == "once":
                    self.failures.append(0)
                    await self.close()
                    return
            if self._mode == "once":
                return
            try:
                await asyncio.wait_for(self._scan_now.wait(), self._cfg.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
            self._scan_now.clear()

    async def _channel(self, channel_id: int):
        ch = self.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.error("Channel %s not found or not visible to the bot", channel_id)
                return None
        if not isinstance(ch, THREAD_PARENT_TYPES):
            log.error("Channel %s (%s) is not a text or forum channel; skipping", channel_id, type(ch).__name__)
            return None
        return ch

    def _needs_sync(self, thread: discord.Thread) -> bool:
        if thread.id in self._ignored:
            return False
        row = self._state.get(thread.id)
        if row is None:
            return True
        if (thread.last_message_id or 0) > row.last_message_id:
            return True
        if thread.name != row.thread_name:
            return True
        return time.time() - row.synced_at > self._cfg.full_resync_hours * 3600

    async def _scan(self, full: bool) -> None:
        active_by_guild: dict[int, dict[int, list[discord.Thread]]] = {}
        queued = 0
        for cid in sorted(self._watched):
            channel = await self._channel(cid)
            if channel is None:
                continue
            gid = channel.guild.id
            if gid not in active_by_guild:
                active_by_guild[gid] = await active_threads_by_parent(channel.guild)
            threads = {t.id: t for t in active_by_guild[gid].get(cid, [])}
            async for t in archived_threads(channel, None if full else ARCHIVED_POLL_LIMIT):
                threads.setdefault(t.id, t)
            for t in threads.values():
                if t.id not in self._ignored and (full or self._needs_sync(t)):
                    self._mark(t.id, 0)
                    queued += 1
        if full or queued:
            log.info("Scan queued %d thread(s)%s", queued, " (full)" if full else "")

    # -- reminder ----------------------------------------------------------

    async def build_reminder(self, now: dt.datetime) -> str:
        items = []
        for cid in sorted(self._watched):
            channel = await self._channel(cid)
            if channel is None:
                # Sending a list that wrongly accuses those students of not reporting would be worse.
                raise RuntimeError(f"channel {cid} is not reachable; not building a reminder from partial data")
            items += await collect_items(channel, self._cfg, now)
        return format_message(self._cfg, plan_reminder(self._cfg, items, now))

    async def _deliver(self, text: str) -> bool:
        """Send to every configured channel; True if at least one succeeded."""
        rc = self._cfg.reminder
        ok = False
        for name in rc.channels:
            try:
                if name == "discord_dm":
                    user = await self.fetch_user(rc.dm_user_id)
                    for chunk in split_message(text, DISCORD_LIMIT):
                        await user.send(chunk)
                elif name == "telegram":
                    if self._telegram is None:
                        raise RuntimeError("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
                    await asyncio.to_thread(self._telegram.send, text)
                ok = True
                log.info("Reminder sent via %s", name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Could not send the reminder via %s: %s", name, exc)
        return ok

    async def _maybe_remind(self, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.timezone.utc)
        if not should_send(self._cfg, now, self._state.get_kv("reminder_last_sent")):
            return
        text = await self.build_reminder(now)
        if await self._deliver(text):
            self._state.set_kv("reminder_last_sent", now.astimezone(self._cfg.tz).date().isoformat())

    async def _reminder_loop(self) -> None:
        rc = self._cfg.reminder
        log.info("Reminder enabled: %s at %s via %s", [d for d in rc.days], rc.remind_at, list(rc.channels))
        while True:
            try:
                await self._maybe_remind()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder failed; will retry in a minute")
            await asyncio.sleep(60)

    async def _reminder_once(self) -> None:
        text = await self.build_reminder(dt.datetime.now(dt.timezone.utc))
        print(text)
        if not self._send:
            print("\n(preview only: nothing was sent. Run `reminder --send` to deliver it.)")
            return
        if await self._deliver(text):
            print("\nSent.")
        else:
            self.failures.append(0)

    # -- diagnostics -------------------------------------------------------

    async def _diagnose(self) -> None:
        ok = True
        for cid in sorted(self._watched):
            students = ", ".join(s.name for s in self._cfg.students if cid in s.channel_ids)
            channel = await self._channel(cid)
            if channel is None:
                ok = False
                print(f"  ✗ channel {cid} not reachable (students: {students})")
                continue
            perms = channel.permissions_for(channel.guild.me)
            missing = [n for n in ("view_channel", "read_message_history") if not getattr(perms, n)]
            active = (await active_threads_by_parent(channel.guild)).get(cid, [])
            kind = type(channel).__name__
            if missing:
                ok = False
                print(f"  ✗ #{channel.name} ({kind}) missing permissions: {', '.join(missing)}")
            else:
                print(f"  ✓ #{channel.name} ({kind}), {len(active)} active thread(s), students: {students}")
        print("Discord check:", "OK" if ok else "problems found")
        if not ok:
            self.failures.append(0)
