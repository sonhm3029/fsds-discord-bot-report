import asyncio
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest
import requests

from discord_gdocs_sync.bot import SyncBot
from discord_gdocs_sync.config import Config, ConfigError, ReminderConfig, StudentConfig, _parse_reminder
from discord_gdocs_sync.notify import NotifyError, TelegramNotifier, split_message
from discord_gdocs_sync.reminder import ReportItem, format_message, looks_like_report, plan_reminder, should_send
from discord_gdocs_sync.reminder_source import collect_items
from discord_gdocs_sync.state import StateStore

UTC = dt.timezone.utc
REVIEWER = 1
MON_21H = dt.datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # 21:00 in Asia/Ho_Chi_Minh, a Monday


def make_cfg(**reminder):
    students = (
        StudentConfig("Ann", "DOC_ANN_" + "a" * 14, (500,), topic="T-One", user_ids=(11,)),
        StudentConfig("Bob", "DOC_BOB_" + "b" * 14, (500,), topic="T-One", user_ids=(12,)),
        StudentConfig("Cid", "DOC_CID_" + "c" * 14, (600,), topic="T-Two"),
    )
    rc = ReminderConfig(enabled=True, reviewer_ids=(REVIEWER,), dm_user_id=REVIEWER, **reminder)
    return Config(students=students, credentials_file="x", state_path=":memory:", timezone_name="Asia/Ho_Chi_Minh", reminder=rc)


def item(student, doc, posted, *, replied=False, title="report"):
    return ReportItem(doc_id=doc, student=student, posted_at=posted, title=title, url=f"https://d/{student}", replied=replied)


# ---- the three report rules ---------------------------------------------------


@pytest.mark.parametrize(
    "text, thread, doc, expected",
    [
        ("[T5 - 15/10/2026]", False, False, True),
        ("dạ em nộp report T2 ạ", False, False, True),
        ("", True, False, True),           # a thread was opened from it
        ("", False, True, True),           # a .md/.pdf attached
        ("Mình đọc survey trước rồi mới đọc các baseline đúng không a", False, False, False),
        ("", False, False, False),         # e.g. only a screenshot
        ("dùng batch size 2**5 và lr 3e-4", False, False, False),
    ],
)
def test_looks_like_report(text, thread, doc, expected):
    pattern = ReminderConfig.report_pattern
    assert looks_like_report(text, has_thread=thread, has_document=doc, pattern=pattern) is expected


# ---- who has / has not reported -------------------------------------------------


def test_plan_lists_students_without_a_report_today():
    cfg = make_cfg()
    ann, bob, cid = (s.doc_id for s in cfg.students)
    items = [item("Ann", ann, MON_21H - dt.timedelta(hours=3)), item("Bob", bob, MON_21H - dt.timedelta(days=3))]
    plan = plan_reminder(cfg, items, MON_21H)
    assert plan.reported == ("Ann",)
    assert [s.name for s in plan.missing] == ["Bob", "Cid"] and plan.total == 3


def test_day_boundary_uses_the_local_timezone():
    cfg = make_cfg()
    ann = cfg.students[0].doc_id
    just_before_midnight = dt.datetime(2026, 10, 19, 16, 30, tzinfo=UTC)  # 23:30 local Monday
    just_after_midnight = dt.datetime(2026, 10, 19, 17, 30, tzinfo=UTC)   # 00:30 local Tuesday
    assert plan_reminder(cfg, [item("Ann", ann, just_before_midnight)], MON_21H).reported == ("Ann",)
    assert plan_reminder(cfg, [item("Ann", ann, just_after_midnight)], MON_21H).reported == ()


def test_a_report_posted_the_day_before_counts_as_early_submission():
    ann = make_cfg().students[0].doc_id
    sunday = dt.datetime(2026, 10, 18, 3, 0, tzinfo=UTC)       # Sunday 10:00 local, a day before the Monday slot
    last_thursday = dt.datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    assert plan_reminder(make_cfg(), [item("Ann", ann, sunday)], MON_21H).reported == ("Ann",)
    assert plan_reminder(make_cfg(), [item("Ann", ann, last_thursday)], MON_21H).reported == ()
    assert plan_reminder(make_cfg(early_days=0), [item("Ann", ann, sunday)], MON_21H).reported == ()


def test_unreplied_are_sorted_oldest_first_and_exclude_replied_and_stale():
    cfg = make_cfg(lookback_days=14)
    ann = cfg.students[0].doc_id
    day = dt.timedelta(days=1)
    items = [
        item("Ann", ann, MON_21H - 2 * day, title="newer"),
        item("Ann", ann, MON_21H - 9 * day, title="older"),
        item("Ann", ann, MON_21H - 5 * day, replied=True, title="answered"),
        item("Ann", ann, MON_21H - 30 * day, title="ancient"),
    ]
    assert [i.title for i in plan_reminder(cfg, items, MON_21H).unreplied] == ["older", "newer"]


# ---- when to send -------------------------------------------------------------------


@pytest.mark.parametrize(
    "when, last_sent, expected",
    [
        (dt.datetime(2026, 10, 19, 14, 0, tzinfo=UTC), None, True),         # Mon 21:00 local
        (dt.datetime(2026, 10, 19, 13, 59, tzinfo=UTC), None, False),       # Mon 20:59
        (dt.datetime(2026, 10, 19, 16, 40, tzinfo=UTC), None, True),        # Mon 23:40: bot was down at 21:00
        (dt.datetime(2026, 10, 19, 14, 0, tzinfo=UTC), "2026-10-19", False),  # already sent today
        (dt.datetime(2026, 10, 19, 14, 0, tzinfo=UTC), "2026-10-15", True),
        (dt.datetime(2026, 10, 20, 14, 0, tzinfo=UTC), None, False),        # Tuesday
        (dt.datetime(2026, 10, 22, 14, 0, tzinfo=UTC), None, True),         # Thursday
    ],
)
def test_should_send(when, last_sent, expected):
    assert should_send(make_cfg(days=(0, 3)), when, last_sent) is expected


# ---- the message ----------------------------------------------------------------------


def test_message_groups_missing_students_by_topic_and_lists_unanswered_with_links():
    cfg = make_cfg()
    ann = cfg.students[0].doc_id
    items = [
        item("Ann", ann, MON_21H - dt.timedelta(hours=3), replied=True),  # today's report, already answered
        item("Ann", ann, MON_21H - dt.timedelta(days=2), title="[T5 - 15/10/2026]"),  # an older one still waiting
    ]
    text = format_message(cfg, plan_reminder(cfg, items, MON_21H))
    assert "Nhắc report Thứ 2 19/10" in text
    assert "Còn 2/3 bạn chưa nộp" in text
    assert "• T-One: Bob" in text and "• T-Two: Cid" in text and "Ann," not in text
    assert "Ann — [T5 - 15/10/2026] · 2 ngày trước" in text and "https://d/Ann" in text


def test_message_when_everyone_reported_and_nothing_waits():
    cfg = make_cfg()
    items = [item(s.name, s.doc_id, MON_21H - dt.timedelta(hours=2), replied=True) for s in cfg.students]
    text = format_message(cfg, plan_reminder(cfg, items, MON_21H))
    assert "✅ Cả 3 bạn đều đã nộp" in text and "Không có report nào đang chờ" in text
    assert "chưa nộp" not in text


# ---- config -----------------------------------------------------------------------------


def test_reminder_config_defaults_and_parsing():
    rc = _parse_reminder({"enabled": True, "reviewer_ids": [5], "days": ["Mon", "THU"], "remind_at": "20:30"})
    assert rc.days == (0, 3) and rc.remind_at == dt.time(20, 30) and rc.dm_user_id == 5
    assert _parse_reminder({}).enabled is False


@pytest.mark.parametrize(
    "bad, msg",
    [
        ({"days": ["funday"]}, "unknown day"),
        ({"remind_at": "25:00"}, "remind_at"),
        ({"channels": ["sms"]}, "channels"),
        ({"enabled": True}, "reviewer_ids"),
        ({"report_pattern": "("}, "regex"),
    ],
)
def test_reminder_config_errors(bad, msg):
    with pytest.raises(ConfigError, match=msg):
        _parse_reminder(bad)


# ---- delivery ------------------------------------------------------------------------------


def test_split_message_keeps_every_line_and_respects_the_limit():
    text = "\n".join(f"line {i} " + "x" * 30 for i in range(100))
    chunks = split_message(text, 200)
    assert all(len(c) <= 200 for c in chunks) and "\n".join(chunks) == text
    assert split_message("a" * 450, 200) == ["a" * 200, "a" * 200, "a" * 50]


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.error:
            raise self.error
        return self.response


def resp(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body)


def test_telegram_posts_text_without_link_previews():
    s = FakeSession(resp(200, {"ok": True}))
    TelegramNotifier("TOKEN123", "42", session=s).send("hello")
    url, kw = s.calls[0]
    assert url == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert kw["json"] == {"chat_id": "42", "text": "hello", "disable_web_page_preview": True}


def test_telegram_errors_never_leak_the_bot_token():
    with pytest.raises(NotifyError, match="chat not found") as e1:
        TelegramNotifier("SECRET-TOKEN", "42", session=FakeSession(resp(400, {"ok": False, "description": "chat not found"}))).send("x")
    boom = requests.ConnectionError("HTTPSConnectionPool: /botSECRET-TOKEN/sendMessage")
    with pytest.raises(NotifyError) as e2:
        TelegramNotifier("SECRET-TOKEN", "42", session=FakeSession(error=boom)).send("x")
    assert "SECRET-TOKEN" not in str(e1.value) and "SECRET-TOKEN" not in str(e2.value)


# ---- reading reports from Discord ---------------------------------------------------------------


def agen(items):
    async def gen(**kw):
        for i in items:
            yield i
    return gen


def dmsg(i, author, text="", *, attachments=(), reference=None, mtype=discord.MessageType.default, minutes=0):
    return SimpleNamespace(
        id=i, type=mtype, author=SimpleNamespace(id=author, bot=False), clean_content=text, content=text,
        attachments=list(attachments), reference=reference, jump_url=f"https://discord.com/channels/9/500/{i}",
        created_at=MON_21H - dt.timedelta(hours=10) + dt.timedelta(minutes=minutes),
    )


def run_collect(messages, threads=()):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 500
    channel.guild = SimpleNamespace(active_threads=lambda: asyncio.sleep(0, result=list(threads)))
    channel.history = agen(messages)
    channel.archived_threads = lambda **kw: agen([])()
    cfg = make_cfg()
    return asyncio.run(collect_items(channel, cfg, MON_21H))


def dthread(tid, reviewer_posts):
    th = SimpleNamespace(id=tid, parent_id=500, name="[T2 - 19/10/2026]", jump_url=f"https://discord.com/channels/9/{tid}")
    th.history = agen([dmsg(tid + 1, REVIEWER if r else 11, "x") for r in reviewer_posts])
    return th


def test_collect_reads_reports_threads_and_replies():
    pdf = SimpleNamespace(content_type="application/pdf", filename="r.pdf")
    png = SimpleNamespace(content_type="image/png", filename="shot.png")
    reply_ref = SimpleNamespace(message_id=103)
    msgs = [
        dmsg(101, 11, "[T2 - 19/10/2026] báo cáo", ),                    # Ann, has a thread the reviewer answered in
        dmsg(102, 12, "", attachments=[pdf]),                            # Bob, document, no thread, unanswered
        dmsg(103, 11, "T2 report nè"),                                   # Ann, answered by a Discord reply
        dmsg(104, REVIEWER, "ok", reference=reply_ref),                  # the reviewer's reply to 103
        dmsg(105, 11, "Thầy ơi cho em hỏi về batch size ạ"),             # chatter: ignored
        dmsg(106, 12, "", attachments=[png]),                            # just a screenshot: ignored
        dmsg(107, REVIEWER, "thông báo T5 15/10 nhé"),                   # reviewer's own post: ignored
        dmsg(108, 99, "T2 15/10 xin chào"),                              # not a configured student: ignored
        dmsg(109, 11, "", mtype=discord.MessageType.thread_created),     # system message: ignored
    ]
    items = run_collect(msgs, threads=[dthread(101, [False, True])])
    by_url = {i.url: i for i in items}
    assert len(items) == 3
    assert by_url["https://discord.com/channels/9/101"].replied is True      # thread url, reviewer wrote in it
    assert by_url["https://discord.com/channels/9/500/102"].replied is False
    assert by_url["https://discord.com/channels/9/500/103"].replied is True


def test_thread_without_any_reviewer_message_counts_as_unanswered():
    items = run_collect([dmsg(101, 11, "[T2]")], threads=[dthread(101, [False, False])])
    assert [i.replied for i in items] == [False]


# ---- the scheduler in the bot ------------------------------------------------------------------------


def make_bot(deliver_ok=True):
    bot = SyncBot(make_cfg(days=(0, 3)), engine=SimpleNamespace(), state=StateStore(":memory:"))
    sent = []

    async def build(now):
        return "REMINDER TEXT"

    async def deliver(text):
        sent.append(text)
        return deliver_ok

    bot.build_reminder, bot._deliver = build, deliver
    return bot, sent


def test_reminder_is_sent_once_per_day_and_remembered():
    bot, sent = make_bot()
    asyncio.run(bot._maybe_remind(MON_21H))
    asyncio.run(bot._maybe_remind(MON_21H + dt.timedelta(minutes=1)))
    assert sent == ["REMINDER TEXT"]
    assert bot._state.get_kv("reminder_last_sent") == "2026-10-19"
    asyncio.run(bot._maybe_remind(MON_21H + dt.timedelta(days=3)))  # Thursday
    assert len(sent) == 2


def test_not_sent_on_other_days_or_before_the_time():
    bot, sent = make_bot()
    asyncio.run(bot._maybe_remind(MON_21H - dt.timedelta(minutes=1)))
    asyncio.run(bot._maybe_remind(MON_21H + dt.timedelta(days=1)))
    assert sent == []


def test_failed_delivery_is_retried_and_not_marked_as_sent():
    bot, sent = make_bot(deliver_ok=False)
    asyncio.run(bot._maybe_remind(MON_21H))
    asyncio.run(bot._maybe_remind(MON_21H + dt.timedelta(minutes=1)))
    assert len(sent) == 2 and bot._state.get_kv("reminder_last_sent") is None
