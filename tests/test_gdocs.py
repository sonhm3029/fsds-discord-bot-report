import json

import pytest
import requests

from conftest import make_message, make_thread, TZ
from discord_gdocs_sync import gdocs
from discord_gdocs_sync.gdocs import DocsError, GoogleDocsClient, build_write_plan, _flatten_tabs
from discord_gdocs_sync.models import Attachment
from discord_gdocs_sync.render import render_thread, u16len


def rendered(messages):
    return render_thread(
        make_thread(messages), student="Sophie", topic=None, tz=TZ, tz_name="Asia/Ho_Chi_Minh"
    )


def rich_tab():
    img = lambda n: Attachment(f"p{n}.png", f"https://cdn.discordapp.com/a/p{n}.png?ex=1", "image/png", 10, 100, 100)
    msgs = [
        make_message(i, f"😀 **m{i}** `c` https://x.io/{i}\n```\ncode {i}\n```", attachments=(img(i),))
        for i in range(1, 8)
    ]
    return rendered(msgs)


class DocSimulator:
    """Validates Docs index arithmetic: every range must lie inside the tab body."""

    def __init__(self, end_index):
        self.end = end_index  # exclusive end of body, includes the final newline

    def check(self, req):
        (name, body), = req.items()
        if name == "deleteContentRange":
            r = body["range"]
            assert 1 <= r["startIndex"] < r["endIndex"] <= self.end - 1, "cannot delete the final newline"
            self.end -= r["endIndex"] - r["startIndex"]
        elif name == "insertText":
            assert 1 <= body["location"]["index"] < self.end
            self.end += u16len(body["text"])
        elif name in ("updateTextStyle", "updateParagraphStyle"):
            r = body["range"]
            assert 1 <= r["startIndex"] < r["endIndex"] <= self.end, (name, r, self.end)
            assert body["fields"]
        elif name == "insertInlineImage":
            assert 1 <= body["location"]["index"] < self.end
            self.end += 1
        else:
            raise AssertionError(name)


@pytest.mark.parametrize("old_end", [2, 3, 500])
def test_write_plan_ranges_are_always_valid(old_end):
    r = rich_tab()
    batches, images = build_write_plan("t.1", old_end, r)
    sim = DocSimulator(old_end)
    for req in [x for b in batches for x in b] + images:
        sim.check(req)
    # inline images were inserted
    assert sim.end == 2 + u16len(r.text) + len(r.images)


def test_delete_is_skipped_for_an_empty_tab_and_targets_the_tab():
    r = rendered([make_message(1, "hi")])
    empty, _ = build_write_plan("t.1", 2, r)
    assert "deleteContentRange" not in json.dumps(empty[0])
    full, _ = build_write_plan("t.1", 50, r)
    assert full[0][0]["deleteContentRange"]["range"] == {"tabId": "t.1", "startIndex": 1, "endIndex": 49}
    assert all("t.1" in json.dumps(req) for b in full for req in b)


def test_content_is_reset_before_styling_and_images_go_last_in_descending_order():
    r = rich_tab()
    batches, images = build_write_plan("t.1", 2, r)
    first = batches[0]
    assert "insertText" in first[0]
    assert [next(iter(x)) for x in first[1:]] == ["updateTextStyle", "updateParagraphStyle"]
    idx = [i["insertInlineImage"]["location"]["index"] for i in images]
    assert idx == sorted(idx, reverse=True) and len(idx) == 7
    assert images[0]["insertInlineImage"]["objectSize"]["width"]["unit"] == "PT"


def test_style_requests_are_chunked(monkeypatch):
    r = rich_tab()
    monkeypatch.setattr(gdocs, "STYLE_BATCH_SIZE", 10)
    batches, _ = build_write_plan("t.1", 2, r)
    assert len(batches) > 3
    assert all(len(b) <= 10 for b in batches[1:])
    # every style request is present exactly once across the chunks
    assert sum(len(b) for b in batches[1:]) == len(r.styles)


def test_flatten_tabs_reads_end_index_and_child_tabs():
    doc = {
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "A", "index": 0},
                "documentTab": {"body": {"content": [{"endIndex": 1}, {"endIndex": 2}]}},
                "childTabs": [{"tabProperties": {"tabId": "t.1", "title": "B", "index": 0}}],
            }
        ]
    }
    out = []
    _flatten_tabs(doc["tabs"], out, None, False)
    assert [(t.tab_id, t.end_index, t.parent_id, t.is_empty) for t in out] == [
        ("t.0", 2, None, True),
        ("t.1", 1, "t.0", True),
    ]


# ---- REST client -----------------------------------------------------------


class Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data)

    def json(self):
        return self._data


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(*responses):
    sleeps = []
    s = Session(*responses)
    return GoogleDocsClient(None, session=s, sleep=sleeps.append), s, sleeps


def test_add_tab_parses_tab_id():
    c, s, _ = client(Resp(200, {"replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.9"}}}]}))
    assert c.add_tab("DOC", "Report") == "t.9"
    method, url, kw = s.calls[0]
    assert url.endswith("/DOC:batchUpdate")
    assert kw["json"] == {"requests": [{"addDocumentTab": {"tabProperties": {"title": "Report"}}}]}


def test_add_tab_at_index_and_fallback_to_append_when_rejected():
    ok = {"replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.5"}}}]}
    c, s, _ = client(Resp(200, ok))
    assert c.add_tab("DOC", "R", index=2) == "t.5"
    assert s.calls[0][2]["json"]["requests"][0]["addDocumentTab"]["tabProperties"] == {"title": "R", "index": 2}

    c, s, _ = client(Resp(400, {"error": {"message": "bad index"}}), Resp(200, ok))
    assert c.add_tab("DOC", "R", index=9) == "t.5"
    assert s.calls[1][2]["json"]["requests"][0]["addDocumentTab"]["tabProperties"] == {"title": "R"}


def test_rename_tab_request():
    c, s, _ = client(Resp(200, {}))
    c.rename_tab("DOC", "t.1", "New")
    req = s.calls[0][2]["json"]["requests"][0]["updateDocumentTabProperties"]
    assert req == {"tabProperties": {"tabId": "t.1", "title": "New"}, "fields": "title"}


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(gdocs.random, "uniform", lambda a, b: 0)
    c, s, sleeps = client(Resp(429, {"error": {"message": "quota"}}), Resp(503, {}), Resp(200, {"title": "T"}))
    assert c.doc_title("DOC") == "T"
    assert len(s.calls) == 3 and sleeps == [3.0, 6.0]


def test_retry_after_header_is_honoured(monkeypatch):
    monkeypatch.setattr(gdocs.random, "uniform", lambda a, b: 0)
    c, _, sleeps = client(Resp(429, {}, {"Retry-After": "30"}), Resp(200, {"title": "T"}))
    c.doc_title("DOC")
    assert sleeps == [30.0]


def test_client_errors_are_not_retried():
    c, s, _ = client(Resp(404, {"error": {"message": "Requested entity was not found."}}))
    with pytest.raises(DocsError) as exc:
        c.doc_title("DOC")
    assert exc.value.status == 404 and "not found" in exc.value.message
    assert len(s.calls) == 1


def test_network_errors_are_retried(monkeypatch):
    monkeypatch.setattr(gdocs.random, "uniform", lambda a, b: 0)
    c, s, _ = client(requests.ConnectionError("boom"), Resp(200, {"title": "T"}))
    assert c.doc_title("DOC") == "T"


def test_writes_are_throttled():
    c, _, sleeps = client(Resp(200, {}), Resp(200, {}))
    c.rename_tab("D", "t", "a")
    c.rename_tab("D", "t", "b")
    assert sleeps and 0 < sleeps[0] <= gdocs.MIN_WRITE_INTERVAL


def test_image_failure_falls_back_to_individual_inserts_and_keeps_text(monkeypatch):
    r = rendered([make_message(1, "x", attachments=(
        Attachment("a.png", "https://cdn.discordapp.com/a.png", "image/png", 1),
        Attachment("b.png", "https://cdn.discordapp.com/b.png", "image/png", 1),
    ))])
    batches, images = build_write_plan("t", 2, r)
    ok = Resp(200, {})
    bad = Resp(400, {"error": {"message": "image not accessible"}})
    # content batches ok, bulk image call fails, then first single fails and second succeeds
    c, s, _ = client(*[ok] * len(batches), bad, bad, ok)
    c.write_tab("D", "t", 2, r)
    assert len(s.calls) == len(batches) + 3
    singles = [c_[2]["json"]["requests"] for c_ in s.calls[-2:]]
    assert all(len(x) == 1 for x in singles)


def test_load_credentials_rejects_unknown_type(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"type": "nope"}))
    with pytest.raises(ValueError):
        gdocs.load_credentials(p)


def test_load_credentials_accepts_oauth_token_without_type_field(tmp_path):
    """Credentials.to_json() omits "type"; the bot must still recognise the file."""
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "refresh_token": "r", "client_id": "cid", "client_secret": "cs",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": gdocs.SCOPES,
    }))
    creds = gdocs.load_credentials(p)
    assert creds.refresh_token == "r" and creds.client_id == "cid"
    p.write_text(json.dumps({"type": "authorized_user", "refresh_token": "r", "client_id": "cid", "client_secret": "cs"}))
    assert gdocs.load_credentials(p).refresh_token == "r"
