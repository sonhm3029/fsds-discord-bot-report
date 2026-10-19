"""Configuration loading (TOML) and validation."""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DOC_URL = re.compile(r"/document/d/([A-Za-z0-9_-]+)")
_DOC_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StudentConfig:
    name: str
    doc_id: str
    channel_ids: tuple[int, ...]
    topic: str | None = None
    # Discord user ids whose reports (thread starter messages) belong to this student.
    # Empty means every thread in the student's channels.
    user_ids: tuple[int, ...] = ()


DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
REMINDER_CHANNELS = ("telegram", "discord_dm")


@dataclass(frozen=True)
class ReminderConfig:
    """End-of-day nudge: who has not reported yet, and which reports still wait for a reply."""

    enabled: bool = False
    days: tuple[int, ...] = (0, 3)  # report days, Monday=0 (Mon and Thu)
    remind_at: dt.time = dt.time(21, 0)  # local time (settings.timezone)
    channels: tuple[str, ...] = ("discord_dm",)
    reviewer_ids: tuple[int, ...] = ()  # Discord ids whose replies count as "answered"
    early_days: int = 1  # a report posted up to this many days before the report day still counts
    lookback_days: int = 14  # only reports newer than this are listed as unanswered
    # A top-level message with neither a thread nor a document attachment is a report only if its
    # beginning matches this regex (default: a day/month like 15/10, or the T2..T7 weekday code).
    report_pattern: str = r"(?i)\bT[2-7]\b|\d{1,2}/\d{1,2}"
    dm_user_id: int | None = None  # who gets the Discord DM; defaults to the first reviewer
    # Also post in each topic channel, tagging that channel's students who have not reported.
    public: bool = False
    public_at: dt.time | None = None  # defaults to remind_at


@dataclass(frozen=True)
class Config:
    students: tuple[StudentConfig, ...]
    credentials_file: Path
    state_path: Path
    timezone_name: str = "UTC"
    poll_interval_seconds: int = 120
    debounce_seconds: int = 15
    full_resync_hours: float = 6.0
    embed_images: bool = True
    tab_title_format: str = "Report {date} - {title}"
    reminder: ReminderConfig = field(default_factory=ReminderConfig)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def watched_channels(self) -> frozenset[int]:
        return frozenset(cid for s in self.students for cid in s.channel_ids)

    def route(self, channel_id: int, author_id: int | None) -> StudentConfig | None:
        """Pick the student a thread belongs to from its channel and report author."""
        candidates = [s for s in self.students if channel_id in s.channel_ids]
        for s in candidates:
            if author_id is not None and author_id in s.user_ids:
                return s
        for s in candidates:
            if not s.user_ids:
                return s
        return None


def parse_doc_id(value: str) -> str:
    value = value.strip()
    if m := _DOC_URL.search(value):
        doc_id = m.group(1)
    elif _DOC_ID.match(value):
        doc_id = value
    else:
        raise ConfigError(f"Cannot extract a Google Doc id from {value!r}")
    if doc_id.upper().startswith("REPLACE"):
        raise ConfigError(f"Replace the placeholder Google Doc URL {value!r} with a real one")
    return doc_id


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path} (copy config.example.toml to config.toml)") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    base = path.resolve().parent
    settings = raw.get("settings", {})
    google = raw.get("google", {})

    tz_name = settings.get("timezone", "UTC")
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ConfigError(f"Unknown timezone {tz_name!r} (use e.g. 'Asia/Ho_Chi_Minh')") from None

    title_fmt = settings.get("tab_title_format", "Report {date} - {title}")
    try:
        title_fmt.format(date="", title="", student="")
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"tab_title_format is invalid ({exc}); allowed fields: {{date}} {{title}} {{student}}") from exc

    students: list[StudentConfig] = []
    for i, s in enumerate(raw.get("students", []), 1):
        name = str(s.get("name", "")).strip()
        if not name:
            raise ConfigError(f"students[{i}]: 'name' is required")
        if "doc" not in s:
            raise ConfigError(f"student {name!r}: 'doc' (Google Doc URL or id) is required")
        channels = _id_list(s, name, "channels", ("channels", "channel_ids", "channel_id"), required=True)
        users = _id_list(s, name, "users", ("users", "user_ids", "user_id"), required=False)
        students.append(
            StudentConfig(
                name=name,
                doc_id=parse_doc_id(str(s["doc"])),
                channel_ids=channels,
                topic=s.get("topic"),
                user_ids=users,
            )
        )
    if not students:
        raise ConfigError("No [[students]] configured")
    _check_unambiguous(students)

    return Config(
        students=tuple(students),
        credentials_file=_resolve(base, google.get("credentials_file", "secrets/google-credentials.json")),
        state_path=_resolve(base, settings.get("state_path", "data/state.db")),
        timezone_name=tz_name,
        poll_interval_seconds=int(settings.get("poll_interval_seconds", 120)),
        debounce_seconds=int(settings.get("debounce_seconds", 15)),
        full_resync_hours=float(settings.get("full_resync_hours", 6)),
        embed_images=bool(settings.get("embed_images", True)),
        tab_title_format=title_fmt,
        reminder=_parse_reminder(raw.get("reminder", {})),
    )


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


def _id_list(s: dict, name: str, label: str, keys: tuple[str, ...], *, required: bool) -> tuple[int, ...]:
    value = next((s[k] for k in keys if k in s), None)
    if value is None:
        if required:
            raise ConfigError(f"student {name!r}: '{label}' is required")
        return ()
    if isinstance(value, int):
        value = [value]
    try:
        ids = tuple(int(v) for v in value)
    except (TypeError, ValueError):
        raise ConfigError(f"student {name!r}: '{label}' must be a list of numeric Discord ids") from None
    if required and not ids:
        raise ConfigError(f"student {name!r}: '{label}' is empty")
    return ids


def _check_unambiguous(students: list[StudentConfig]) -> None:
    """Every thread must resolve to at most one student, and each Doc to one student."""
    docs: dict[str, str] = {}
    for s in students:
        if s.doc_id in docs:
            raise ConfigError(f"students {docs[s.doc_id]!r} and {s.name!r} use the same Google Doc")
        docs[s.doc_id] = s.name

    by_channel: dict[int, list[StudentConfig]] = {}
    for s in students:
        for cid in s.channel_ids:
            by_channel.setdefault(cid, []).append(s)
    for cid, group in by_channel.items():
        if len(group) == 1:
            continue
        names = " and ".join(repr(s.name) for s in group)
        if any(not s.user_ids for s in group):
            raise ConfigError(
                f"channel {cid} is shared by {names}: give each of them a 'users' list "
                "(Discord user ids of the report authors) so threads can be told apart"
            )
        owner: dict[int, str] = {}
        for s in group:
            for uid in s.user_ids:
                if uid in owner:
                    raise ConfigError(f"user {uid} is mapped to both {owner[uid]!r} and {s.name!r} in channel {cid}")
                owner[uid] = s.name


def _parse_reminder(r: dict) -> ReminderConfig:
    days_raw = r.get("days", ["mon", "thu"])
    try:
        days = tuple(sorted({DAY_NAMES[str(d).strip().lower()[:3]] for d in days_raw}))
    except KeyError as exc:
        raise ConfigError(f"[reminder] days: unknown day {exc} (use mon, tue, wed, thu, fri, sat, sun)") from None
    if not days:
        raise ConfigError("[reminder] days is empty")

    def parse_time(key: str, default: str | None) -> dt.time | None:
        value = r.get(key, default)
        if value is None:
            return None
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
        if not m:
            raise ConfigError(f"[reminder] {key} {str(value)!r} must look like 21:00")
        return dt.time(int(m.group(1)), int(m.group(2)))

    remind_at = parse_time("remind_at", "21:00")
    public_at = parse_time("public_at", None)

    channels = tuple(r.get("channels", ["discord_dm"]))
    bad = [c for c in channels if c not in REMINDER_CHANNELS]
    if bad or not channels:
        raise ConfigError(f"[reminder] channels must be a non-empty subset of {list(REMINDER_CHANNELS)}, got {list(channels)}")

    try:
        reviewers = tuple(int(x) for x in r.get("reviewer_ids", []))
        dm_user = int(r["dm_user_id"]) if "dm_user_id" in r else None
    except (TypeError, ValueError):
        raise ConfigError("[reminder] reviewer_ids / dm_user_id must be numeric Discord user ids") from None

    pattern = str(r.get("report_pattern", ReminderConfig.report_pattern))
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"[reminder] report_pattern is not a valid regex: {exc}") from None

    enabled = bool(r.get("enabled", False))
    if enabled and not reviewers:
        raise ConfigError("[reminder] reviewer_ids is required (your Discord user id: replies from it count as answered)")
    return ReminderConfig(
        enabled=enabled,
        days=days,
        remind_at=remind_at,
        channels=channels,
        reviewer_ids=reviewers,
        early_days=int(r.get("early_days", 1)),
        lookback_days=int(r.get("lookback_days", 14)),
        report_pattern=pattern,
        dm_user_id=dm_user if dm_user is not None else (reviewers[0] if reviewers else None),
        public=bool(r.get("public", False)),
        public_at=public_at,
    )
