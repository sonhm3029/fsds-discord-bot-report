"""Read recent reports straight from Discord for the reminder.

This looks at the channel's top-level messages, not only at threads: the teacher opens a thread
*after* a student posts, so a report that was not answered yet may have no thread at all.
"""

from __future__ import annotations

import datetime as dt

import discord

from .config import Config, StudentConfig
from .discord_source import _CONTENT_TYPES, _FORUM_TYPES, archived_threads
from .reminder import ReportItem, looks_like_report


def student_for(cfg: Config, channel_id: int, author_id: int) -> StudentConfig | None:
    """The student a message author is, within a channel (None for anyone else)."""
    for s in cfg.students:
        if channel_id in s.channel_ids and (author_id in s.user_ids or not s.user_ids):
            return s
    return None


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")


def _has_document(m: discord.Message) -> bool:
    """An attachment that is not just a picture (e.g. a .md/.pdf report file)."""
    for a in m.attachments:
        ctype = (a.content_type or "").lower()
        if not (ctype.startswith("image/") or (not ctype and a.filename.lower().endswith(_IMAGE_EXTS))):
            return True
    return False


async def _reviewer_posted(thread: discord.Thread, reviewers: set[int], skip_id: int | None = None) -> bool:
    async for m in thread.history(limit=None):
        if m.author.id in reviewers and m.id != skip_id and m.type in _CONTENT_TYPES:
            return True
    return False


def _title(m: discord.Message, thread: discord.Thread | None) -> str:
    if thread is not None:
        return thread.name
    first_line = next((ln.strip() for ln in (m.clean_content or "").splitlines() if ln.strip()), "")
    if first_line:
        return first_line
    return m.attachments[0].filename if m.attachments else "(report)"


async def collect_items(channel, cfg: Config, now: dt.datetime) -> list[ReportItem]:
    rc = cfg.reminder
    since = now - dt.timedelta(days=rc.lookback_days)
    reviewers = set(rc.reviewer_ids)

    threads: dict[int, discord.Thread] = {
        t.id: t for t in await channel.guild.active_threads() if t.parent_id == channel.id
    }
    async for t in archived_threads(channel, 50):
        threads.setdefault(t.id, t)

    if isinstance(channel, _FORUM_TYPES):
        return await _forum_items(channel, threads, cfg, since, reviewers)

    messages = [m async for m in channel.history(limit=None, after=since, oldest_first=True)]
    replied_to = {
        m.reference.message_id
        for m in messages
        if m.author.id in reviewers and m.type in _CONTENT_TYPES and m.reference and m.reference.message_id
    }
    items: list[ReportItem] = []
    for m in messages:
        if m.type not in _CONTENT_TYPES or m.author.bot or m.author.id in reviewers:
            continue
        student = student_for(cfg, channel.id, m.author.id)
        if student is None:
            continue
        thread = threads.get(m.id)  # a thread opened from this message shares its id
        if not looks_like_report(
            m.clean_content, has_thread=thread is not None, has_document=_has_document(m), pattern=rc.report_pattern
        ):
            continue
        replied = m.id in replied_to or (thread is not None and await _reviewer_posted(thread, reviewers))
        items.append(
            ReportItem(
                doc_id=student.doc_id,
                student=student.name,
                posted_at=m.created_at,
                title=_title(m, thread),
                url=thread.jump_url if thread else m.jump_url,
                replied=replied,
            )
        )
    return items


async def _forum_items(channel, threads, cfg: Config, since: dt.datetime, reviewers: set[int]) -> list[ReportItem]:
    items: list[ReportItem] = []
    for t in sorted(threads.values(), key=lambda t: t.id):
        if discord.utils.snowflake_time(t.id) < since:
            continue
        try:
            starter = await t.fetch_message(t.id)
        except (discord.NotFound, discord.Forbidden):
            continue
        if starter.author.bot or starter.author.id in reviewers:
            continue
        student = student_for(cfg, channel.id, starter.author.id)
        if student is None:
            continue
        items.append(
            ReportItem(
                doc_id=student.doc_id,
                student=student.name,
                posted_at=starter.created_at,
                title=t.name,
                url=t.jump_url,
                replied=await _reviewer_posted(t, reviewers, skip_id=starter.id),
            )
        )
    return items
