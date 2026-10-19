from dataclasses import replace

import pytest

from conftest import make_message, make_thread
from discord_gdocs_sync.config import StudentConfig
from discord_gdocs_sync.gdocs import TabInfo
from discord_gdocs_sync.render import u16len
from discord_gdocs_sync.state import StateStore
from discord_gdocs_sync.sync import SyncEngine


class FakeDocs:
    """In-memory stand-in for the Docs API: docs -> ordered tabs with their text."""

    def __init__(self):
        self.docs: dict[str, list[dict]] = {}
        self.writes = 0
        self.adds = 0
        self.renames = 0
        self._n = 0

    def new_doc(self, doc_id, *, default_tab=True):
        self.docs[doc_id] = []
        if default_tab:
            self._add(doc_id, "Tab 1")

    def _add(self, doc_id, title, index=None):
        self._n += 1
        tab = {"tab_id": f"t.{self._n}", "title": title, "text": ""}
        tabs = self.docs[doc_id]
        tabs.insert(len(tabs) if index is None else index, tab)
        return tab["tab_id"]

    def doc_title(self, doc_id):
        return "Fake doc"

    def list_tabs(self, doc_id, *, with_text=False):
        return [
            TabInfo(t["tab_id"], t["title"], i, 2 + u16len(t["text"]), None, t["text"][:1500] if with_text else "")
            for i, t in enumerate(self.docs[doc_id])
        ]

    def add_tab(self, doc_id, title, index=None):
        self.adds += 1
        return self._add(doc_id, title, index)

    def rename_tab(self, doc_id, tab_id, title):
        self.renames += 1
        next(t for t in self.docs[doc_id] if t["tab_id"] == tab_id)["title"] = title

    def write_tab(self, doc_id, tab_id, end_index, rendered):
        self.writes += 1
        tab = next(t for t in self.docs[doc_id] if t["tab_id"] == tab_id)
        assert end_index == 2 + u16len(tab["text"]), "engine must pass the tab's real end index"
        tab["text"] = rendered.text

    def tabs(self, doc_id):
        return self.docs[doc_id]


@pytest.fixture
def docs(student):
    d = FakeDocs()
    d.new_doc(student.doc_id)
    return d


@pytest.fixture
def state():
    s = StateStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def engine(config, docs, state):
    return SyncEngine(config, docs, state, clock=lambda: 1234.0)


def thread(*contents, **kw):
    return make_thread([make_message(i, c) for i, c in enumerate(contents)], **kw)


def test_first_report_reuses_the_empty_default_tab(engine, docs, student, state):
    res = engine.apply(student, thread("hello"))
    assert res.action == "created"
    tabs = docs.tabs(student.doc_id)
    assert len(tabs) == 1 and docs.adds == 0
    assert tabs[0]["title"] == "Report 2026-10-05 - KV cache ablation"
    assert "hello" in tabs[0]["text"]
    row = state.get(1000)
    assert row.tab_id == tabs[0]["tab_id"] and row.content_hash and row.synced_at == 1234.0


def test_each_thread_gets_its_own_tab_in_creation_order(engine, docs, student):
    engine.apply(student, thread("one", thread_id=1000, name="first"))
    engine.apply(student, thread("two", thread_id=2000, name="second"))
    titles = [t["title"] for t in docs.tabs(student.doc_id)]
    assert titles == ["Report 2026-10-05 - first", "Report 2026-10-05 - second"]
    assert docs.adds == 1


def test_resync_without_changes_does_not_touch_google(engine, docs, student):
    snap = thread("hello")
    engine.apply(student, snap)
    before = (docs.writes, docs.adds, docs.renames)
    assert engine.apply(student, snap).action == "unchanged"
    assert (docs.writes, docs.adds, docs.renames) == before
    assert len(docs.tabs(student.doc_id)) == 1


def test_new_reply_updates_the_same_tab_without_duplicates(engine, docs, student, state):
    engine.apply(student, thread("question"))
    res = engine.apply(student, thread("question", "my feedback"))
    assert res.action == "updated"
    tabs = docs.tabs(student.doc_id)
    assert len(tabs) == 1
    text = tabs[0]["text"]
    assert text.index("question") < text.index("my feedback")  # chronological


def test_edit_and_delete_are_reflected(engine, docs, student):
    engine.apply(student, thread("original", "reply"))
    engine.apply(student, thread("edited", "reply"))
    text = docs.tabs(student.doc_id)[0]["text"]
    assert "edited" in text and "original" not in text
    engine.apply(student, thread("edited"))
    assert "reply" not in docs.tabs(student.doc_id)[0]["text"]


def test_thread_rename_updates_the_tab_title(engine, docs, student):
    engine.apply(student, thread("x", name="old name"))
    engine.apply(student, thread("x", name="new name"))
    tabs = docs.tabs(student.doc_id)
    assert len(tabs) == 1 and tabs[0]["title"].endswith("new name")


def test_title_only_change_renames_without_rewriting_content(engine, docs, student, config):
    engine.apply(student, thread("x"))
    writes = docs.writes
    engine2 = SyncEngine(replace(config, tab_title_format="R {date}"), docs, engine._state)
    engine2.apply(student, thread("x"))
    assert docs.tabs(student.doc_id)[0]["title"] == "R 2026-10-05"
    assert docs.writes == writes


def test_tab_deleted_by_hand_is_recreated(engine, docs, student, state):
    engine.apply(student, thread("a", thread_id=1000))
    engine.apply(student, thread("b", thread_id=2000))
    docs.docs[student.doc_id].pop(1)  # user deletes the second tab
    res = engine.apply(student, thread("b", "more", thread_id=2000))
    assert res.action == "created"
    tabs = docs.tabs(student.doc_id)
    assert len(tabs) == 2 and "more" in tabs[1]["text"]
    assert state.get(2000).tab_id == tabs[1]["tab_id"]


def test_lost_state_db_adopts_existing_tabs_instead_of_duplicating(config, docs, student):
    first = SyncEngine(config, docs, StateStore(":memory:"))
    first.apply(student, thread("a", thread_id=1000, name="one"))
    first.apply(student, thread("b", thread_id=2000, name="two"))
    assert len(docs.tabs(student.doc_id)) == 2

    fresh = SyncEngine(config, docs, StateStore(":memory:"))  # state DB lost
    fresh.apply(student, thread("a", "new reply", thread_id=1000, name="one"))
    fresh.apply(student, thread("b", thread_id=2000, name="two"))
    tabs = docs.tabs(student.doc_id)
    assert len(tabs) == 2 and docs.adds == 1
    assert "new reply" in tabs[0]["text"]


def test_user_tab_with_other_content_is_left_alone(engine, docs, student):
    docs.docs[student.doc_id][0]["text"] = "my own notes"
    engine.apply(student, thread("hello"))
    tabs = docs.tabs(student.doc_id)
    assert len(tabs) == 2 and tabs[0]["text"] == "my own notes"


def test_remapping_a_channel_to_another_doc_creates_a_tab_there(engine, docs, student):
    engine.apply(student, thread("x"))
    other = StudentConfig("Sophie", "OTHER_DOC_" + "y" * 12, student.channel_ids)
    docs.new_doc(other.doc_id)
    assert engine.apply(other, thread("x")).action == "created"
    assert len(docs.tabs(other.doc_id)) == 1


def test_crash_after_tab_creation_does_not_orphan_the_tab(config, docs, student, state):
    class Crash(Exception):
        pass

    orig = docs.write_tab
    docs.write_tab = lambda *a, **k: (_ for _ in ()).throw(Crash())
    engine = SyncEngine(config, docs, state)
    with pytest.raises(Crash):
        engine.apply(student, thread("hello"))
    assert state.get(1000) is not None and state.get(1000).content_hash is None

    docs.write_tab = orig
    engine.apply(student, thread("hello"))
    assert len(docs.tabs(student.doc_id)) == 1 and "hello" in docs.tabs(student.doc_id)[0]["text"]


def test_state_tracks_last_message_and_thread_name(engine, state, student):
    engine.apply(student, thread("a", "b", name="Topic"))
    row = state.get(1000)
    assert row.thread_name == "Topic" and row.last_message_id == 1001


def test_tabs_stay_in_posting_order_even_when_synced_out_of_order(engine, docs, student):
    engine.apply(student, thread("b", thread_id=2000, name="second"))
    engine.apply(student, thread("d", thread_id=4000, name="fourth"))
    engine.apply(student, thread("c", thread_id=3000, name="third"))   # arrives late: goes in the middle
    engine.apply(student, thread("a", thread_id=1000, name="first"))   # arrives last: goes first
    assert [t["title"].split(" - ")[1] for t in docs.tabs(student.doc_id)] == ["first", "second", "third", "fourth"]


def test_new_tab_goes_after_tracked_ones_and_leaves_foreign_tabs_alone(engine, docs, student):
    docs.docs[student.doc_id][0]["text"] = "my own notes"           # a tab the bot must not touch
    engine.apply(student, thread("a", thread_id=1000, name="first"))
    engine.apply(student, thread("b", thread_id=2000, name="second"))
    titles = [t["title"] for t in docs.tabs(student.doc_id)]
    assert titles[0] == "Tab 1" and titles[1].endswith("first") and titles[2].endswith("second")
