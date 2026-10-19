"""SQLite store: which Discord thread lives in which Google Docs tab."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Report:
    thread_id: int
    channel_id: int
    doc_id: str
    tab_id: str
    tab_title: str | None = None
    thread_name: str | None = None
    content_hash: str | None = None
    last_message_id: int = 0
    synced_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    thread_id       INTEGER PRIMARY KEY,
    channel_id      INTEGER NOT NULL,
    doc_id          TEXT    NOT NULL,
    tab_id          TEXT    NOT NULL,
    tab_title       TEXT,
    thread_name     TEXT,
    content_hash    TEXT,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    synced_at       REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reports_doc ON reports(doc_id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_COLUMNS = (
    "thread_id, channel_id, doc_id, tab_id, tab_title, thread_name, "
    "content_hash, last_message_id, synced_at"
)


class StateStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def get(self, thread_id: int) -> Report | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM reports WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return Report(*row) if row else None

    def for_doc(self, doc_id: str) -> list[Report]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM reports WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        return [Report(*r) for r in rows]

    def upsert(self, r: Report) -> None:
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO reports ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    r.thread_id, r.channel_id, r.doc_id, r.tab_id, r.tab_title,
                    r.thread_name, r.content_hash, r.last_message_id, r.synced_at,
                ),
            )
            self._db.commit()

    def delete(self, thread_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM reports WHERE thread_id = ?", (thread_id,))
            self._db.commit()

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
