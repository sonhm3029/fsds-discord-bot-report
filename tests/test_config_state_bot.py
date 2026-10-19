import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from discord_gdocs_sync.bot import SyncBot
from discord_gdocs_sync.config import ConfigError, load_config, parse_doc_id
from discord_gdocs_sync.state import Report, StateStore

DOC = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def write(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


GOOD = f"""
[settings]
timezone = "Asia/Ho_Chi_Minh"
[google]
credentials_file = "secrets/key.json"
[[students]]
name = "Sophie"
doc = "https://docs.google.com/document/d/{DOC}/edit?tab=t.0"
channels = [1, 2]
topic = "KV"
[[students]]
name = "Khoa"
doc = "{DOC}b"
channel_id = 3
"""


def test_example_config_parses(tmp_path):
    from pathlib import Path

    text = (Path(__file__).parent.parent / "config.example.toml").read_text()
    with pytest.raises(ConfigError, match="placeholder"):  # shipped placeholders must not run by accident
        load_config(write(tmp_path, text))
    filled = text.replace("REPLACE_WITH_SOPHIE_DOC_ID", "s" * 30).replace("REPLACE_WITH_KHOA_DOC_ID", "k" * 30)
    cfg = load_config(write(tmp_path, filled))
    assert [s.name for s in cfg.students] == ["Sophie", "Khoa"]


def test_loads_and_resolves_paths(tmp_path):
    cfg = load_config(write(tmp_path, GOOD))
    assert cfg.students[0].doc_id == DOC and cfg.students[0].channel_ids == (1, 2)
    assert cfg.students[1].channel_ids == (3,)
    assert cfg.credentials_file == tmp_path / "secrets/key.json"
    assert cfg.state_path == tmp_path / "data/state.db"
    assert cfg.route(2, None).name == "Sophie"
    assert cfg.watched_channels() == {1, 2, 3}
    assert str(cfg.tz) == "Asia/Ho_Chi_Minh"


@pytest.mark.parametrize(
    "body, msg",
    [
        ("", "No [[students]]"),
        ('[[students]]\nname="a"\nchannels=[1]\n', "'doc'"),
        (f'[[students]]\nname="a"\ndoc="{DOC}"\n', "'channels'"),
        (f'[[students]]\nname="a"\ndoc="{DOC}"\nchannels=["x"]\n', "numeric"),
        (f'[[students]]\nname="a"\ndoc="nope"\nchannels=[1]\n', "Google Doc id"),
        (
            f'[[students]]\nname="a"\ndoc="{DOC}"\nchannels=[1]\n[[students]]\nname="b"\ndoc="{DOC}"\nchannels=[1]\n',
            "same Google Doc",
        ),
        (
            f'[[students]]\nname="a"\ndoc="{DOC}"\nchannels=[1]\n[[students]]\nname="b"\ndoc="{DOC}b"\nchannels=[1]\n',
            "shared by",
        ),
        (
            f'[[students]]\nname="a"\ndoc="{DOC}"\nchannels=[1]\nusers=[7]\n'
            f'[[students]]\nname="b"\ndoc="{DOC}b"\nchannels=[1]\nusers=[7]\n',
            "mapped to both",
        ),
        (f'[[students]]\nname="a"\ndoc="REPLACE_WITH_REAL_DOC_ID_HERE"\nchannels=[1]\n', "placeholder"),
        (f'[settings]\ntimezone="Mars/Base"\n[[students]]\nname="a"\ndoc="{DOC}"\nchannels=[1]\n', "timezone"),
        (f'[settings]\ntab_title_format="{{nope}}"\n[[students]]\nname="a"\ndoc="{DOC}"\nchannels=[1]\n', "tab_title_format"),
    ],
)
def test_invalid_configs(tmp_path, body, msg):
    with pytest.raises(ConfigError, match=msg.replace("[", r"\[")):
        load_config(write(tmp_path, body))


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_parse_doc_id():
    assert parse_doc_id(f"https://docs.google.com/document/d/{DOC}/edit") == DOC
    assert parse_doc_id(DOC) == DOC


def test_state_roundtrip_and_queries(tmp_path):
    s = StateStore(tmp_path / "sub" / "state.db")
    r = Report(1, 10, "docA", "t.1", "title", "name", "hash", 99, 5.0)
    s.upsert(r)
    s.upsert(Report(2, 10, "docB", "t.2"))
    assert s.get(1) == r and s.get(3) is None
    assert [x.thread_id for x in s.for_doc("docA")] == [1]
    s.upsert(Report(1, 10, "docA", "t.1", content_hash="new"))
    assert s.get(1).content_hash == "new" and s.count() == 2
    s.close()
    assert StateStore(tmp_path / "sub" / "state.db").get(1).content_hash == "new"  # persisted


# ---- bot scheduling logic (no Discord connection) --------------------------


@pytest.fixture
def bot(config, student):
    b = SyncBot(config, engine=SimpleNamespace(), state=StateStore(":memory:"))
    return b


def fake_thread(tid, *, last=0, name="n"):
    return SimpleNamespace(id=tid, last_message_id=last, name=name)


def test_needs_sync_rules(bot, student):
    t = fake_thread(10, last=50, name="n")
    assert bot._needs_sync(t)  # unknown thread
    bot._state.upsert(Report(10, 500, student.doc_id, "t", "x", "n", "h", 50, time.time()))
    assert not bot._needs_sync(t)  # up to date
    assert bot._needs_sync(fake_thread(10, last=51))  # new message
    assert bot._needs_sync(fake_thread(10, last=50, name="renamed"))
    bot._state.upsert(Report(10, 500, student.doc_id, "t", "x", "n", "h", 50, time.time() - 7 * 3600))
    assert bot._needs_sync(t)  # periodic full resync due
    bot._ignored.add(10)
    assert not bot._needs_sync(t)  # threads of unmapped authors are never re-fetched


def test_mark_debounces_by_pushing_the_deadline_forward(bot):
    bot._mark(7, 15)
    first = bot._pending[7].due
    time.sleep(0.01)
    bot._mark(7, 15)
    assert bot._pending[7].due > first and len(bot._pending) == 1


def test_is_ours_uses_state_for_unknown_threads(bot, student):
    assert not bot._is_ours(10)
    bot._state.upsert(Report(10, 500, student.doc_id, "t"))
    assert bot._is_ours(10)


async def run_worker(bot, seconds=0.3):
    task = asyncio.create_task(bot._worker())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_worker_processes_oldest_thread_first(bot):
    seen = []

    async def sync(tid):
        seen.append(tid)

    bot._sync_thread = sync
    for tid in (30, 10, 20):
        bot._mark(tid, 0)
    asyncio.run(run_worker(bot))
    assert seen == [10, 20, 30] and not bot._pending


def test_worker_retries_failures_with_backoff_and_keeps_going(bot):
    seen = []

    async def sync(tid):
        seen.append(tid)
        if tid == 10:
            raise RuntimeError("google is down")

    bot._sync_thread = sync
    bot._mark(10, 0)
    bot._mark(20, 0)
    asyncio.run(run_worker(bot))
    assert seen == [10, 20]  # the failure did not block the next thread
    assert bot._pending[10].attempts == 1 and bot._pending[10].due > time.monotonic() + 20


def test_event_during_processing_is_not_lost(bot):
    seen = []

    async def sync(tid):
        seen.append(tid)
        if len(seen) == 1:
            bot._mark(tid, 0)  # a new message arrives while we are syncing

    bot._sync_thread = sync
    bot._mark(10, 0)
    asyncio.run(run_worker(bot, 1.6))
    assert seen == [10, 10]


# ---- routing by report author ------------------------------------------------


def shared_config(tmp_path):
    body = f"""
[[students]]
name = "Sophie"
doc = "{DOC}"
channels = [10]
users = [111]
[[students]]
name = "Khoa"
doc = "{DOC}b"
channels = [10, 20]
users = [222]
[[students]]
name = "Solo"
doc = "{DOC}c"
channels = [30]
"""
    return load_config(write(tmp_path, body))


def test_route_by_report_author_in_a_shared_channel(tmp_path):
    cfg = shared_config(tmp_path)
    assert cfg.route(10, 111).name == "Sophie"
    assert cfg.route(10, 222).name == "Khoa"
    assert cfg.route(20, 222).name == "Khoa"
    assert cfg.route(10, 999) is None  # e.g. the teacher's own thread
    assert cfg.route(10, None) is None
    assert cfg.route(20, 111) is None  # right user, wrong channel
    assert cfg.route(30, 999).name == "Solo"  # single-student channel without users: everything
    assert cfg.route(40, 111) is None


def thread_stub(tid=1, parent_id=10):
    import discord

    t = MagicMock(spec=discord.Thread)
    t.id, t.parent_id, t.name = tid, parent_id, "[T5 - 15/10/2026]"
    return t


def routed_bot(tmp_path, monkeypatch, author_id):
    import discord_gdocs_sync.bot as botmod
    from conftest import make_thread

    cfg = shared_config(tmp_path)
    applied, fetched = [], []
    engine = SimpleNamespace(apply=lambda student, snap: applied.append((student.name, snap.thread_id)))
    b = SyncBot(cfg, engine=engine, state=StateStore(":memory:"))
    thread = thread_stub()
    b.get_channel = lambda cid: thread

    async def fake_snapshot(t):
        fetched.append(t.id)
        return replace(make_thread([], thread_id=t.id, channel_id=10), author_id=author_id)

    monkeypatch.setattr(botmod, "snapshot_thread", fake_snapshot)
    return b, applied, fetched


def test_thread_is_synced_into_its_authors_doc(tmp_path, monkeypatch):
    b, applied, _ = routed_bot(tmp_path, monkeypatch, author_id=222)
    asyncio.run(b._sync_thread(1))
    assert applied == [("Khoa", 1)]


def test_thread_of_an_unmapped_author_is_skipped_and_not_refetched(tmp_path, monkeypatch):
    b, applied, fetched = routed_bot(tmp_path, monkeypatch, author_id=999)
    asyncio.run(b._sync_thread(1))
    assert applied == [] and 1 in b._ignored
    b._mark(1, 0)  # a new message arrives in the ignored thread
    assert 1 not in b._pending and fetched == [1]
