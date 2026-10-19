"""Plain data structures describing a Discord thread.

The Discord layer converts discord.py objects into these, so rendering and syncing
can be tested without a Discord connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Attachment:
    filename: str
    url: str
    content_type: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    text: str | None = None  # content of small text-like files, so the Doc keeps it after the link expires


@dataclass(frozen=True)
class EmbedInfo:
    title: str | None = None
    description: str | None = None
    url: str | None = None
    fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReplyRef:
    jump_url: str
    author: str | None = None
    snippet: str | None = None


@dataclass(frozen=True)
class Forward:
    content: str = ""
    attachments: tuple[Attachment, ...] = ()
    embeds: tuple[EmbedInfo, ...] = ()


@dataclass(frozen=True)
class MessageSnapshot:
    id: int
    author_name: str
    author_username: str
    created_at: datetime
    content: str
    jump_url: str
    author_id: int = 0
    author_is_bot: bool = False
    edited_at: datetime | None = None
    reply_to: ReplyRef | None = None
    attachments: tuple[Attachment, ...] = ()
    embeds: tuple[EmbedInfo, ...] = ()
    forwards: tuple[Forward, ...] = ()


@dataclass(frozen=True)
class ThreadSnapshot:
    thread_id: int
    channel_id: int
    name: str
    created_at: datetime
    jump_url: str
    last_message_id: int
    messages: tuple[MessageSnapshot, ...]
    # Author of the report (the thread's starter message, else its first message).
    author_id: int | None = None
