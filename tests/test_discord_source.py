import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_gdocs_sync.discord_source import message_snapshot, snapshot_thread

T0 = datetime(2026, 10, 5, 7, 30, tzinfo=timezone.utc)


def msg(i, content="", *, type=discord.MessageType.default, **kw):
    base = dict(
        id=i,
        type=type,
        author=SimpleNamespace(id=777, display_name="Sophie", name="sophie", bot=False),
        created_at=T0 + timedelta(minutes=i),
        edited_at=None,
        clean_content=content,
        jump_url=f"https://discord.com/channels/9/5/{i}",
        reference=None,
        attachments=[],
        embeds=[],
        guild=SimpleNamespace(id=9),
        message_snapshots=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def thread_of(parent, history, *, tid=100, last=0):
    async def hist(limit=None, oldest_first=False):
        for m in history:
            yield m

    return SimpleNamespace(
        id=tid, parent=parent, parent_id=5, name="KV", created_at=T0, jump_url="https://discord.com/channels/9/100",
        last_message_id=last, history=hist,
    )


def run(coro):
    return asyncio.run(coro)


def test_text_channel_starter_message_is_fetched_and_placed_first():
    starter = msg(100, "the report")
    parent = MagicMock(spec=discord.TextChannel)
    parent.fetch_message = AsyncMock(return_value=starter)
    history = [
        msg(100 + 1, "", type=discord.MessageType.thread_starter_message),  # system echo, must be dropped
        msg(105, "reply one"),
        msg(106, "reply two"),
    ]
    snap = run(snapshot_thread(thread_of(parent, history, last=106)))
    assert [m.content for m in snap.messages] == ["the report", "reply one", "reply two"]
    assert snap.author_id == 777  # the report author is the starter message's author
    parent.fetch_message.assert_awaited_once_with(100)
    assert snap.last_message_id == 106 and snap.channel_id == 5 and snap.name == "KV"


def test_starter_already_in_history_is_not_duplicated():
    parent = MagicMock(spec=discord.TextChannel)
    parent.fetch_message = AsyncMock()
    snap = run(snapshot_thread(thread_of(parent, [msg(100, "a"), msg(101, "b")])))
    assert [m.id for m in snap.messages] == [100, 101]
    parent.fetch_message.assert_not_awaited()


def test_forum_thread_uses_history_only():
    parent = MagicMock(spec=discord.ForumChannel)
    snap = run(snapshot_thread(thread_of(parent, [msg(100, "post body"), msg(102, "comment")])))
    assert [m.content for m in snap.messages] == ["post body", "comment"]


def test_missing_starter_message_is_tolerated():
    parent = MagicMock(spec=discord.TextChannel)
    parent.fetch_message = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason=""), "gone"))
    snap = run(snapshot_thread(thread_of(parent, [msg(105, "only reply")])))
    assert [m.content for m in snap.messages] == ["only reply"]


def test_last_message_id_includes_the_pre_fetch_value():
    parent = MagicMock(spec=discord.ForumChannel)
    snap = run(snapshot_thread(thread_of(parent, [msg(100, "a")], last=500)))
    assert snap.last_message_id == 500


def test_custom_emoji_and_embeds_and_attachments():
    rich = SimpleNamespace(type="rich", title="Result", description="loss 0.3", url=None, fields=[SimpleNamespace(name="acc", value="91")])
    link_preview = SimpleNamespace(type="link", title="x", description="y", url="https://x", fields=[])
    att = SimpleNamespace(filename="p.png", url="https://cdn.discordapp.com/p.png?ex=1", content_type="image/png", size=5, width=10, height=20)
    snap = message_snapshot(msg(1, "nice <:pog:123456> and <a:dance:99>", embeds=[rich, link_preview], attachments=[att]))
    assert snap.content == "nice :pog: and :dance:"
    assert len(snap.embeds) == 1 and snap.embeds[0].fields == (("acc", "91"),)
    assert snap.attachments[0].filename == "p.png" and snap.attachments[0].width == 10
    assert snap.author_name == "Sophie" and snap.author_username == "sophie"


def test_unresolved_reply_still_gets_a_jump_link():
    ref = SimpleNamespace(message_id=77, channel_id=5, guild_id=9, resolved=None, cached_message=None)
    snap = message_snapshot(msg(1, "re", reference=ref))
    assert snap.reply_to.jump_url == "https://discord.com/channels/9/5/77"
    assert snap.reply_to.author is None


def test_resolved_reply_carries_author_and_snippet():
    target = MagicMock(spec=discord.Message)
    target.author = SimpleNamespace(display_name="Khoa")
    target.clean_content = "what about batch size?"
    target.attachments = []
    ref = SimpleNamespace(message_id=77, channel_id=5, guild_id=9, resolved=target, cached_message=None)
    snap = message_snapshot(msg(1, "re", reference=ref))
    assert (snap.reply_to.author, snap.reply_to.snippet) == ("Khoa", "what about batch size?")


def test_forwarded_message_content_is_kept():
    fwd = SimpleNamespace(content="forwarded <:e:1>", attachments=[], embeds=[])
    snap = message_snapshot(msg(1, "", message_snapshots=[fwd]))
    assert snap.forwards[0].content == "forwarded :e:"


def test_text_attachments_are_downloaded_but_binary_ones_are_not():
    def att(i, name, ctype, size, data=b""):
        a = SimpleNamespace(id=i, filename=name, url=f"https://cdn.discordapp.com/{name}", content_type=ctype,
                            size=size, width=None, height=None)
        a.read = AsyncMock(return_value=data)
        return a

    md = att(1, "report.md", "text/markdown; charset=utf-8", 12, "# Kết quả".encode())
    big = att(2, "huge.txt", "text/plain", 10 * 1024 * 1024)
    pdf = att(3, "paper.pdf", "application/pdf", 100)
    parent = MagicMock(spec=discord.ForumChannel)
    history = [msg(100, "see file", attachments=[md, big, pdf])]
    snap = run(snapshot_thread(thread_of(parent, history)))
    a_md, a_big, a_pdf = snap.messages[0].attachments
    assert a_md.text == "# Kết quả"
    assert a_big.text is None and a_pdf.text is None
    big.read.assert_not_awaited() and pdf.read.assert_not_awaited()


def test_report_date_is_when_the_student_posted_not_when_the_thread_was_opened():
    starter = msg(100, "the report")           # posted at T0 + 100 min
    parent = MagicMock(spec=discord.TextChannel)
    parent.fetch_message = AsyncMock(return_value=starter)
    th = thread_of(parent, [msg(105, "feedback")])
    th.created_at = T0 + timedelta(days=3)       # the teacher opened the thread days later
    snap = run(snapshot_thread(th))
    assert snap.created_at == starter.created_at


def test_a_failing_attachment_download_does_not_fail_the_thread():
    a = SimpleNamespace(id=1, filename="report.md", url="https://cdn.discordapp.com/r.md", content_type="text/markdown",
                        size=10, width=None, height=None)
    a.read = AsyncMock(side_effect=OSError("ssl: wrong version number"))
    parent = MagicMock(spec=discord.ForumChannel)
    snap = run(snapshot_thread(thread_of(parent, [msg(100, "see file", attachments=[a])])))
    assert snap.messages[0].attachments[0].text is None  # link only; content retried on the next sync
