"""Sync engine: make one Google Docs tab mirror one Discord thread.

The engine is idempotent. Every call re-renders the whole thread and rewrites the tab
only when the rendered content (or title) differs from what was last written, so
duplicates, edits, deletions, restarts and missed events all converge to the same state.
Calls must be serialized by the caller (the bot runs a single worker).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace

from .config import Config, StudentConfig
from .gdocs import DocsBackend, TabInfo
from .models import ThreadSnapshot
from .render import render_thread, tab_title
from .state import Report, StateStore

log = logging.getLogger(__name__)

# Matches the header line written by render_thread: "Discord thread · thread_id: 123 · ..."
_MARKER = re.compile(r"^Discord thread.*?thread_id: (\d+)", re.M)


@dataclass(frozen=True)
class ApplyResult:
    action: str  # created | updated | unchanged
    tab_id: str
    title: str


class SyncEngine:
    def __init__(self, config: Config, docs: DocsBackend, state: StateStore, clock=time.time) -> None:
        self._cfg = config
        self._docs = docs
        self._state = state
        self._clock = clock
        self._reconciled: set[str] = set()

    # -- public ------------------------------------------------------------

    def apply(self, student: StudentConfig, snap: ThreadSnapshot) -> ApplyResult:
        cfg = self._cfg
        self._reconcile_doc(student.doc_id)

        rendered = render_thread(
            snap,
            student=student.name,
            topic=student.topic,
            tz=cfg.tz,
            tz_name=cfg.timezone_name,
            embed_images=cfg.embed_images,
        )
        title = tab_title(cfg.tab_title_format, snap, student.name, cfg.tz)

        row = self._state.get(snap.thread_id)
        if row is not None and row.doc_id != student.doc_id:
            log.info("Thread %s is now mapped to a different doc; creating a new tab", snap.thread_id)
            row = None

        if (
            row is not None
            and row.content_hash == rendered.content_hash
            and row.tab_title == title
        ):
            # Nothing changed: avoid touching Google at all.
            self._save(row, snap, title, rendered.content_hash)
            return ApplyResult("unchanged", row.tab_id, title)

        tabs = self._docs.list_tabs(student.doc_id)
        tab = next((t for t in tabs if row and t.tab_id == row.tab_id), None)
        created = tab is None
        if row is not None and tab is None:
            log.warning("Tab %s for thread %s no longer exists in the doc; recreating it", row.tab_id, snap.thread_id)

        if tab is None:
            tab = self._new_tab(student, snap, title, tabs)
            row = Report(
                thread_id=snap.thread_id,
                channel_id=snap.channel_id,
                doc_id=student.doc_id,
                tab_id=tab.tab_id,
                tab_title=title,
            )
            # Persist before writing content so a crash cannot orphan the tab.
            self._state.upsert(row)
        elif tab.title != title:
            self._docs.rename_tab(student.doc_id, tab.tab_id, title)

        assert row is not None
        if created or row.content_hash != rendered.content_hash:
            self._docs.write_tab(student.doc_id, tab.tab_id, tab.end_index, rendered)
        self._save(replace(row, tab_id=tab.tab_id), snap, title, rendered.content_hash)
        action = "created" if created else "updated"
        log.info("%s tab %r for thread %s (%d messages)", action, title, snap.thread_id, len(snap.messages))
        return ApplyResult(action, tab.tab_id, title)

    # -- helpers -----------------------------------------------------------

    def _save(self, row: Report, snap: ThreadSnapshot, title: str, content_hash: str) -> None:
        self._state.upsert(
            replace(
                row,
                channel_id=snap.channel_id,
                tab_title=title,
                thread_name=snap.name,
                content_hash=content_hash,
                last_message_id=max(snap.last_message_id, row.last_message_id),
                synced_at=self._clock(),
            )
        )

    def _new_tab(self, student: StudentConfig, snap: ThreadSnapshot, title: str, tabs: list[TabInfo]) -> TabInfo:
        # A brand-new Google Doc has one empty "Tab 1"; reuse it for the first report.
        if len(tabs) == 1 and tabs[0].is_empty and not self._state.for_doc(student.doc_id):
            tab = tabs[0]
            if tab.title != title:
                self._docs.rename_tab(student.doc_id, tab.tab_id, title)
            return replace(tab, title=title)
        index = self._chronological_index(student.doc_id, snap.thread_id, tabs)
        tab_id = self._docs.add_tab(student.doc_id, title, index)
        return TabInfo(tab_id=tab_id, title=title, index=index if index is not None else len(tabs), end_index=2)

    def _chronological_index(self, doc_id: str, thread_id: int, tabs: list[TabInfo]) -> int | None:
        """Where to insert a new report tab so tabs stay in posting order (None = append).

        Thread ids are Discord snowflakes, i.e. ordered by time, so neighbours can be found
        from the tabs this bot already tracks in the doc; the user's own tabs are left alone.
        """
        position = {t.tab_id: t.index for t in tabs if t.parent_id is None}
        tracked = [
            r for r in self._state.for_doc(doc_id) if r.thread_id != thread_id and r.tab_id in position
        ]
        earlier = [r for r in tracked if r.thread_id < thread_id]
        later = [r for r in tracked if r.thread_id > thread_id]
        if earlier:
            return position[max(earlier, key=lambda r: r.thread_id).tab_id] + 1
        if later:
            return position[min(later, key=lambda r: r.thread_id).tab_id]
        return None

    def _reconcile_doc(self, doc_id: str) -> None:
        """If the state DB was lost, re-adopt tabs this bot wrote earlier (avoids duplicates)."""
        if doc_id in self._reconciled:
            return
        self._reconciled.add(doc_id)
        if self._state.for_doc(doc_id):
            return
        if not any(not t.is_empty for t in self._docs.list_tabs(doc_id)):
            return
        for tab in self._docs.list_tabs(doc_id, with_text=True):
            m = _MARKER.search(tab.head_text)
            if not m:
                continue
            thread_id = int(m.group(1))
            if self._state.get(thread_id) is None:
                log.info("Re-adopting existing tab %r for thread %s", tab.title, thread_id)
                # Empty hash forces a rewrite, which brings the tab fully up to date.
                self._state.upsert(Report(thread_id=thread_id, channel_id=0, doc_id=doc_id, tab_id=tab.tab_id))
