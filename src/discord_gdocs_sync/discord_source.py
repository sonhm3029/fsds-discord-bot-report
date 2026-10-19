"""Read threads and messages from Discord and convert them to plain snapshots."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

import discord

from .models import (
    Attachment,
    EmbedInfo,
    Forward,
    MessageSnapshot,
    ReplyRef,
    ThreadSnapshot,
)

log = logging.getLogger(__name__)

_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_CONTENT_TYPES = (discord.MessageType.default, discord.MessageType.reply)

# Channels whose threads we can read. MediaChannel behaves like a forum.
THREAD_PARENT_TYPES: tuple[type, ...] = tuple(
    t for t in (discord.TextChannel, discord.ForumChannel, getattr(discord, "MediaChannel", None)) if t
)
_FORUM_TYPES: tuple[type, ...] = tuple(
    t for t in (discord.ForumChannel, getattr(discord, "MediaChannel", None)) if t
)


# Small text-like attachments are downloaded and embedded: Discord CDN links expire after ~24h,
# and for students a report is often just an attached .md file.
_TEXT_EXTS = (".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".py", ".log", ".tex", ".rst", ".sh")
MAX_TEXT_BYTES = 256 * 1024


def _is_text(a: discord.Attachment) -> bool:
    ctype = (a.content_type or "").split(";")[0].strip().lower()
    return (ctype.startswith("text/") or a.filename.lower().endswith(_TEXT_EXTS)) and (a.size or 0) <= MAX_TEXT_BYTES


async def _read_texts(m: discord.Message) -> dict[int, str]:
    out: dict[int, str] = {}
    for a in m.attachments:
        if _is_text(a):
            try:
                out[a.id] = (await a.read()).decode("utf-8", errors="replace")
            except Exception as exc:  # network/SSL/Discord errors: keep the link, never fail the whole thread
                log.warning("Could not download attachment %s: %s", a.filename, exc)
    return out


def _attachment(a: discord.Attachment, texts: dict[int, str] | None = None) -> Attachment:
    return Attachment(
        filename=a.filename,
        url=a.url,
        content_type=a.content_type,
        size=a.size,
        width=a.width,
        height=a.height,
        text=(texts or {}).get(getattr(a, "id", None)),
    )


def _embed(e: discord.Embed) -> EmbedInfo | None:
    # Link previews repeat a URL already present in the message text; keep authored embeds only.
    if e.type != "rich":
        return None
    return EmbedInfo(
        title=e.title,
        description=e.description,
        url=e.url,
        fields=tuple((f.name or "", f.value or "") for f in e.fields),
    )


def _embeds(items) -> tuple[EmbedInfo, ...]:
    return tuple(x for x in (_embed(e) for e in items) if x is not None)


def _clean(text: str) -> str:
    return _CUSTOM_EMOJI.sub(r":\1:", text)


def _reply(m: discord.Message) -> ReplyRef | None:
    ref = m.reference
    if ref is None or ref.message_id is None:
        return None
    guild_id = ref.guild_id or (m.guild.id if m.guild else "@me")
    jump = f"https://discord.com/channels/{guild_id}/{ref.channel_id}/{ref.message_id}"
    resolved = ref.resolved if isinstance(ref.resolved, discord.Message) else ref.cached_message
    if resolved is None:
        return ReplyRef(jump_url=jump)
    snippet = resolved.clean_content or ("[attachment]" if resolved.attachments else None)
    return ReplyRef(jump_url=jump, author=resolved.author.display_name, snippet=snippet)


def message_snapshot(m: discord.Message, texts: dict[int, str] | None = None) -> MessageSnapshot:
    forwards = tuple(
        Forward(
            content=_clean(getattr(s, "content", "") or ""),
            attachments=tuple(_attachment(a) for a in getattr(s, "attachments", ())),
            embeds=_embeds(getattr(s, "embeds", ())),
        )
        for s in getattr(m, "message_snapshots", ())
    )
    return MessageSnapshot(
        id=m.id,
        author_name=m.author.display_name,
        author_username=m.author.name,
        author_id=m.author.id,
        author_is_bot=m.author.bot,
        created_at=m.created_at,
        edited_at=m.edited_at,
        content=_clean(m.clean_content),
        jump_url=m.jump_url,
        reply_to=_reply(m),
        attachments=tuple(_attachment(a, texts) for a in m.attachments),
        embeds=_embeds(m.embeds),
        forwards=forwards,
    )


async def snapshot_thread(thread: discord.Thread) -> ThreadSnapshot:
    """Fetch the complete thread (starter message included) in chronological order."""
    # Taken before reading history so a message arriving meanwhile triggers another sync.
    last_seen = thread.last_message_id or 0

    messages: list[discord.Message] = []
    async for m in thread.history(limit=None, oldest_first=True):
        if m.type in _CONTENT_TYPES:
            messages.append(m)

    # A thread started from a message in a text channel has that message in the parent
    # channel (same id as the thread); forum threads already contain it in their history.
    parent = thread.parent
    if (
        isinstance(parent, discord.TextChannel)
        and not any(m.id == thread.id for m in messages)
    ):
        try:
            starter = await parent.fetch_message(thread.id)
        except (discord.NotFound, discord.Forbidden):
            starter = None
        if starter is not None and starter.type in _CONTENT_TYPES:
            messages.insert(0, starter)

    snapped = []
    for m in messages:
        snapped.append(message_snapshot(m, await _read_texts(m)))
    snaps = tuple(snapped)
    last = max([last_seen, *(m.id for m in snaps)])
    return ThreadSnapshot(
        thread_id=thread.id,
        channel_id=thread.parent_id or 0,
        name=thread.name,
        # Report date = when the student posted it (the starter message), not when the thread was opened.
        created_at=snaps[0].created_at if snaps else (thread.created_at or discord.utils.utcnow()),
        jump_url=thread.jump_url,
        last_message_id=last,
        messages=snaps,
        author_id=snaps[0].author_id if snaps else None,
    )


async def active_threads_by_parent(guild: discord.Guild) -> dict[int, list[discord.Thread]]:
    out: dict[int, list[discord.Thread]] = {}
    for t in await guild.active_threads():
        if t.parent_id is not None:
            out.setdefault(t.parent_id, []).append(t)
    return out


async def archived_threads(channel, limit: int | None) -> AsyncIterator[discord.Thread]:
    """Archived threads, newest first. Private ones need the Manage Threads permission."""
    variants = [False]
    if isinstance(channel, discord.TextChannel):
        variants.append(True)
    for private in variants:
        try:
            if isinstance(channel, _FORUM_TYPES):
                it = channel.archived_threads(limit=limit)
            else:
                it = channel.archived_threads(limit=limit, private=private)
            async for t in it:
                yield t
        except discord.Forbidden:
            if private:
                log.debug("No access to archived private threads in #%s", channel.name)
            else:
                log.warning("Cannot list archived threads in #%s (missing permissions)", channel.name)
