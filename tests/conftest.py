from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from discord_gdocs_sync.config import Config, StudentConfig
from discord_gdocs_sync.models import MessageSnapshot, ThreadSnapshot

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)
GUILD = 900


def make_message(i: int, content: str, *, author="Sophie", minutes=0, **kw) -> MessageSnapshot:
    return MessageSnapshot(
        id=1000 + i,
        author_name=author,
        author_username=author.lower(),
        created_at=T0 + timedelta(minutes=minutes or i),
        content=content,
        jump_url=f"https://discord.com/channels/{GUILD}/500/{1000 + i}",
        **kw,
    )


def make_thread(messages, *, thread_id=1000, name="KV cache ablation", channel_id=500) -> ThreadSnapshot:
    last = max((m.id for m in messages), default=thread_id)
    return ThreadSnapshot(
        thread_id=thread_id,
        channel_id=channel_id,
        name=name,
        created_at=T0,
        jump_url=f"https://discord.com/channels/{GUILD}/{thread_id}",
        last_message_id=last,
        messages=tuple(messages),
    )


@pytest.fixture
def student() -> StudentConfig:
    return StudentConfig(name="Sophie", doc_id="DOC_SOPHIE_" + "x" * 12, channel_ids=(500,), topic="KV-Cache")


@pytest.fixture
def config(student, tmp_path) -> Config:
    return Config(
        students=(student,),
        credentials_file=tmp_path / "creds.json",
        state_path=tmp_path / "state.db",
        timezone_name="Asia/Ho_Chi_Minh",
    )


def slice16(text: str, start: int, end: int) -> str:
    """Slice by UTF-16 code units, like the Docs API indexes text."""
    return text.encode("utf-16-le")[2 * start : 2 * end].decode("utf-16-le")


TZ = ZoneInfo("Asia/Ho_Chi_Minh")
