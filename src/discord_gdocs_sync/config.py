"""Configuration loading (TOML) and validation."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
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
