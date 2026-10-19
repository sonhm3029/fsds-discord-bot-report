"""Thin Google Docs REST client (tabs aware) plus the request builder for tab content."""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests
from google.auth.transport.requests import AuthorizedSession

from .render import ImageSlot, RenderedTab

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/documents"]
API = "https://docs.googleapis.com/v1/documents"

# The Docs API allows 60 write requests/minute/user; stay safely below that.
MIN_WRITE_INTERVAL = 1.2
MAX_ATTEMPTS = 7
STYLE_BATCH_SIZE = 400
HEAD_TEXT_CHARS = 1500


class DocsError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Google Docs API error {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class TabInfo:
    tab_id: str
    title: str
    index: int
    end_index: int  # end of the tab body; an empty tab has end_index 2
    parent_id: str | None = None
    head_text: str = ""

    @property
    def is_empty(self) -> bool:
        return self.end_index <= 2


class DocsBackend(Protocol):
    def doc_title(self, doc_id: str) -> str: ...
    def list_tabs(self, doc_id: str, *, with_text: bool = False) -> list[TabInfo]: ...
    def add_tab(self, doc_id: str, title: str, index: int | None = None) -> str: ...
    def rename_tab(self, doc_id: str, tab_id: str, title: str) -> None: ...
    def write_tab(self, doc_id: str, tab_id: str, end_index: int, rendered: RenderedTab) -> None: ...


# ---------------------------------------------------------------------------
# Request building (pure, unit-tested)
# ---------------------------------------------------------------------------


def _rgb(v: float) -> dict[str, Any]:
    return {"color": {"rgbColor": {"red": v, "green": v, "blue": v}}}


def _pt(v: float) -> dict[str, Any]:
    return {"magnitude": v, "unit": "PT"}


_TEXT_RESET_FIELDS = (
    "bold,italic,underline,strikethrough,link,weightedFontFamily,"
    "backgroundColor,foregroundColor,fontSize"
)
_PARA_RESET_FIELDS = (
    "namedStyleType,spaceAbove,spaceBelow,indentStart,indentFirstLine,"
    "shading,borderLeft,keepWithNext"
)

_TEXT_STYLES: dict[str, tuple[dict[str, Any], str]] = {
    "bold": ({"bold": True}, "bold"),
    "italic": ({"italic": True}, "italic"),
    "underline": ({"underline": True}, "underline"),
    "strike": ({"strikethrough": True}, "strikethrough"),
    "code": (
        {"weightedFontFamily": {"fontFamily": "Roboto Mono"}, "backgroundColor": _rgb(0.94)},
        "weightedFontFamily,backgroundColor",
    ),
    "muted": ({"foregroundColor": _rgb(0.4), "fontSize": _pt(9)}, "foregroundColor,fontSize"),
}

_PARA_STYLES: dict[str, tuple[dict[str, Any], str]] = {
    "p_h1": ({"namedStyleType": "HEADING_1"}, "namedStyleType"),
    "p_msghead": (
        {"spaceAbove": _pt(16), "keepWithNext": True},
        "spaceAbove,keepWithNext",
    ),
    "p_code": (
        {"shading": {"backgroundColor": _rgb(0.94)}, "indentStart": _pt(8), "indentFirstLine": _pt(8)},
        "shading,indentStart,indentFirstLine",
    ),
    "p_quote": (
        {
            "indentStart": _pt(18),
            "indentFirstLine": _pt(18),
            "borderLeft": {
                "color": _rgb(0.7),
                "width": _pt(2),
                "padding": _pt(8),
                "dashStyle": "SOLID",
            },
        },
        "indentStart,indentFirstLine,borderLeft",
    ),
}


def build_write_plan(
    tab_id: str, end_index: int, rendered: RenderedTab
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Return (content batches, image requests) that replace a tab's whole body.

    Content batches must run in order. Image requests are separate so that one image
    Google cannot fetch does not roll back the text.
    """
    total = len(rendered.text.encode("utf-16-le")) // 2
    body_end = 1 + total  # exclusive end of the inserted text

    def rng(start: int, end: int) -> dict[str, Any]:
        return {"tabId": tab_id, "startIndex": start, "endIndex": end}

    head: list[dict[str, Any]] = []
    if end_index > 2:
        head.append({"deleteContentRange": {"range": rng(1, end_index - 1)}})
    if total:
        head.append({"insertText": {"location": {"tabId": tab_id, "index": 1}, "text": rendered.text}})
    # Clear whatever formatting the leftover paragraph carried so nothing leaks in.
    head.append(
        {
            "updateTextStyle": {
                "range": rng(1, body_end + 1),
                "textStyle": {},
                "fields": _TEXT_RESET_FIELDS,
            }
        }
    )
    head.append(
        {
            "updateParagraphStyle": {
                "range": rng(1, body_end + 1),
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": _PARA_RESET_FIELDS,
            }
        }
    )

    styles: list[dict[str, Any]] = []
    for s in rendered.styles:
        r = rng(1 + s.start, 1 + s.end)
        if s.is_paragraph:
            style, fields = _PARA_STYLES[s.kind]
            styles.append({"updateParagraphStyle": {"range": r, "paragraphStyle": style, "fields": fields}})
        elif s.kind == "link":
            styles.append(
                {"updateTextStyle": {"range": r, "textStyle": {"link": {"url": s.url}}, "fields": "link"}}
            )
        else:
            style, fields = _TEXT_STYLES[s.kind]
            styles.append({"updateTextStyle": {"range": r, "textStyle": style, "fields": fields}})

    batches = [head]
    for i in range(0, len(styles), STYLE_BATCH_SIZE):
        batches.append(styles[i : i + STYLE_BATCH_SIZE])

    # Insert from the end so earlier offsets stay valid as images add characters.
    images = [_image_request(tab_id, img) for img in sorted(rendered.images, key=lambda i: -i.offset)]
    return batches, images


def _image_request(tab_id: str, img: ImageSlot) -> dict[str, Any]:
    return {
        "insertInlineImage": {
            "location": {"tabId": tab_id, "index": 1 + img.offset},
            "uri": img.url,
            "objectSize": {"width": _pt(round(img.width_pt, 1))},
        }
    }


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


def load_credentials(path: str | Path):
    """Load a service-account key or an authorized-user token file."""
    data = json.loads(Path(path).read_text())
    kind = data.get("type")
    if kind is None and "refresh_token" in data and "client_id" in data:
        kind = "authorized_user"  # google-auth's Credentials.to_json() omits the type
    if kind == "service_account":
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(data, scopes=SCOPES)
    if kind == "authorized_user":
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_info(data, scopes=SCOPES)
    raise ValueError(
        f"{path}: unsupported credentials type {kind!r} (expected service_account or authorized_user)"
    )


class GoogleDocsClient:
    def __init__(self, credentials, *, session: Any | None = None, sleep=time.sleep) -> None:
        self._session = session or AuthorizedSession(credentials)
        self._sleep = sleep
        self._last_write = 0.0

    # -- low level ---------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._last_write + MIN_WRITE_INTERVAL - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_write = time.monotonic()

    def _call(self, method: str, url: str, *, write: bool = False, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(MAX_ATTEMPTS):
            if write:
                self._throttle()
            last = attempt == MAX_ATTEMPTS - 1
            try:
                resp = self._session.request(method, url, timeout=120, **kwargs)
            except requests.RequestException as exc:
                if last:
                    raise DocsError(0, f"network error: {exc}") from exc
                self._backoff(attempt, None, f"network error: {exc}")
                continue
            if resp.status_code < 300:
                return resp.json() if resp.content else {}
            text = resp.text[:500]
            retryable = resp.status_code in (429, 500, 502, 503, 504) or (
                resp.status_code == 403 and "ratelimit" in text.lower().replace(" ", "")
            )
            if retryable and not last:
                self._backoff(attempt, resp.headers.get("Retry-After"), f"HTTP {resp.status_code}")
                continue
            raise DocsError(resp.status_code, _error_message(resp))
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int, retry_after: str | None, why: str) -> None:
        try:
            delay = float(retry_after) if retry_after else 0.0
        except ValueError:
            delay = 0.0
        delay = max(delay, min(60.0, 3.0 * 2**attempt) + random.uniform(0, 1.5))
        log.warning("Google Docs %s; retrying in %.1fs", why, delay)
        self._sleep(delay)

    def _batch_update(self, doc_id: str, requests_: list[dict[str, Any]]) -> dict[str, Any]:
        return self._call(
            "POST", f"{API}/{doc_id}:batchUpdate", write=True, json={"requests": requests_}
        )

    # -- DocsBackend -------------------------------------------------------

    def doc_title(self, doc_id: str) -> str:
        return self._call("GET", f"{API}/{doc_id}", params={"fields": "title"}).get("title", "")

    def list_tabs(self, doc_id: str, *, with_text: bool = False) -> list[TabInfo]:
        params: dict[str, Any] = {"includeTabsContent": "true"}
        if not with_text:
            params["fields"] = "tabs(tabProperties,documentTab(body(content(endIndex))),childTabs)"
        doc = self._call("GET", f"{API}/{doc_id}", params=params)
        out: list[TabInfo] = []
        _flatten_tabs(doc.get("tabs", []), out, None, with_text)
        return out

    def add_tab(self, doc_id: str, title: str, index: int | None = None) -> str:
        props: dict[str, Any] = {"title": title}
        if index is not None:
            props["index"] = index
        try:
            res = self._batch_update(doc_id, [{"addDocumentTab": {"tabProperties": props}}])
        except DocsError as exc:
            if index is None or exc.status != 400:
                raise
            log.warning("Could not insert tab at index %s (%s); appending instead", index, exc.message)
            res = self._batch_update(doc_id, [{"addDocumentTab": {"tabProperties": {"title": title}}}])
        return res["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

    def rename_tab(self, doc_id: str, tab_id: str, title: str) -> None:
        self._batch_update(
            doc_id,
            [
                {
                    "updateDocumentTabProperties": {
                        "tabProperties": {"tabId": tab_id, "title": title},
                        "fields": "title",
                    }
                }
            ],
        )

    def write_tab(self, doc_id: str, tab_id: str, end_index: int, rendered: RenderedTab) -> None:
        batches, images = build_write_plan(tab_id, end_index, rendered)
        for batch in batches:
            self._batch_update(doc_id, batch)
        if not images:
            return
        try:
            self._batch_update(doc_id, images)
            return
        except DocsError as exc:
            if exc.status not in (400, 404):
                raise
            log.warning("Bulk image insert failed (%s); retrying one by one", exc.message)
        # Positions are only valid if applied from the end, and a failed batch applied nothing.
        for req in images:
            try:
                self._batch_update(doc_id, [req])
            except DocsError as exc:
                log.warning("Skipping image %s: %s", req["insertInlineImage"]["uri"].split("?")[0], exc.message)


def _error_message(resp: Any) -> str:
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return resp.text[:300]


def _flatten_tabs(tabs: list[dict], out: list[TabInfo], parent: str | None, with_text: bool) -> None:
    for tab in tabs:
        props = tab.get("tabProperties", {})
        content = tab.get("documentTab", {}).get("body", {}).get("content", [])
        end_index = content[-1].get("endIndex", 1) if content else 1
        out.append(
            TabInfo(
                tab_id=props.get("tabId", ""),
                title=props.get("title", ""),
                index=props.get("index", 0),
                end_index=end_index,
                parent_id=parent,
                head_text=_head_text(content) if with_text else "",
            )
        )
        _flatten_tabs(tab.get("childTabs", []), out, props.get("tabId"), with_text)


def _head_text(content: list[dict]) -> str:
    lines: list[str] = []
    size = 0
    for el in content:
        para = el.get("paragraph")
        if not para:
            continue
        line = "".join(e.get("textRun", {}).get("content", "") for e in para.get("elements", []))
        lines.append(line.rstrip("\n"))
        size += len(line)
        if size >= HEAD_TEXT_CHARS:
            break
    return "\n".join(lines)
