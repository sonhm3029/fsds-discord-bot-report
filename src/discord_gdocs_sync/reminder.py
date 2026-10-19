"""End-of-day reminder: who has not reported yet, and which reports still wait for a reply.

Pure logic over plain data (no Discord, no network), so it is easy to test. Everything here is a
deterministic comparison of authors and timestamps; no language model is involved.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .config import Config, StudentConfig

WEEKDAY_VI = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]
TITLE_CHARS = 60


@dataclass(frozen=True)
class ReportItem:
    """One report a student posted: a top-level message, usually with a thread opened from it."""

    doc_id: str  # identifies the student
    student: str
    posted_at: dt.datetime  # timezone-aware
    title: str
    url: str
    replied: bool  # a reviewer wrote in its thread or replied to it


@dataclass(frozen=True)
class ReminderPlan:
    day: dt.date  # local date the reminder is about
    now: dt.datetime
    total: int
    reported: tuple[str, ...]  # names of students who reported on `day`
    missing: tuple[StudentConfig, ...]
    unreplied: tuple[ReportItem, ...]


def looks_like_report(text: str, *, has_thread: bool, has_document: bool, pattern: str) -> bool:
    """Tell a report from ordinary chatter in the channel (no language model, just three rules)."""
    return has_thread or has_document or re.search(pattern, (text or "")[:200]) is not None


def plan_reminder(cfg: Config, items: list[ReportItem], now: dt.datetime) -> ReminderPlan:
    tz = cfg.tz
    today = now.astimezone(tz).date()
    first_day = today - dt.timedelta(days=cfg.reminder.early_days)
    reported_docs = {i.doc_id for i in items if first_day <= i.posted_at.astimezone(tz).date() <= today}
    oldest = now - dt.timedelta(days=cfg.reminder.lookback_days)
    unreplied = sorted(
        (i for i in items if not i.replied and i.posted_at >= oldest), key=lambda i: i.posted_at
    )
    return ReminderPlan(
        day=today,
        now=now,
        total=len(cfg.students),
        reported=tuple(s.name for s in cfg.students if s.doc_id in reported_docs),
        missing=tuple(s for s in cfg.students if s.doc_id not in reported_docs),
        unreplied=tuple(unreplied),
    )


def should_send(cfg: Config, now: dt.datetime, last_sent: str | None) -> bool:
    """True once per report day, from `remind_at` until midnight (survives restarts via last_sent)."""
    local = now.astimezone(cfg.tz)
    rc = cfg.reminder
    return (
        local.weekday() in rc.days
        and local.time() >= rc.remind_at
        and last_sent != local.date().isoformat()
    )


def _age(posted: dt.datetime, now: dt.datetime, cfg: Config) -> str:
    days = (now.astimezone(cfg.tz).date() - posted.astimezone(cfg.tz).date()).days
    if days <= 0:
        return "hôm nay"
    if days == 1:
        return "hôm qua"
    return f"{days} ngày trước"


def _short(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= TITLE_CHARS else flat[: TITLE_CHARS - 1] + "…"


def format_message(cfg: Config, plan: ReminderPlan) -> str:
    lines = [f"📋 Nhắc report {WEEKDAY_VI[plan.day.weekday()]} {plan.day:%d/%m}"]
    if plan.missing:
        lines.append(f"Còn {len(plan.missing)}/{plan.total} bạn chưa nộp:")
        by_topic: dict[str, list[str]] = {}
        for s in plan.missing:
            by_topic.setdefault(s.topic or "Khác", []).append(s.name)
        lines += [f"• {topic}: {', '.join(names)}" for topic, names in by_topic.items()]
    else:
        lines.append(f"✅ Cả {plan.total} bạn đều đã nộp report hôm nay.")

    lines.append("")
    if plan.unreplied:
        lines.append(f"💬 Đã nộp nhưng bạn chưa phản hồi ({len(plan.unreplied)}):")
        for it in plan.unreplied:
            lines.append(f"• {it.student} — {_short(it.title)} · {_age(it.posted_at, plan.now, cfg)}")
            lines.append(f"  {it.url}")
    else:
        lines.append("💬 Không có report nào đang chờ bạn phản hồi.")
    return "\n".join(lines)
