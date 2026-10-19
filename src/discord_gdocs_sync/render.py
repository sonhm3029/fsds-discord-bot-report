"""Render a ThreadSnapshot into plain text plus style ranges for a Google Docs tab.

All offsets are UTF-16 code units, which is how the Google Docs API indexes text
(an emoji outside the BMP counts as 2). Offsets are relative to the start of the
rendered text; the Docs layer adds 1 because a tab body starts at index 1.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from urllib.parse import urlsplit

from .models import Attachment, EmbedInfo, MessageSnapshot, ThreadSnapshot

_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}

# Docs can embed these formats only, up to 50 MB and 25 megapixels.
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif"}
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif")
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_IMAGE_PIXELS = 25_000_000
_PAGE_WIDTH_PT = 468.0

TITLE_MAX = 50  # Google Docs rejects tab titles longer than 50 characters


def u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class Style:
    start: int
    end: int
    # text kinds: bold italic underline strike code muted link
    # paragraph kinds: p_h1 p_msghead p_code p_quote
    kind: str
    url: str | None = None

    @property
    def is_paragraph(self) -> bool:
        return self.kind.startswith("p_")


@dataclass(frozen=True)
class ImageSlot:
    offset: int
    url: str
    filename: str
    width_pt: float


@dataclass
class RenderedTab:
    text: str
    styles: list[Style]
    images: list[ImageSlot]
    content_hash: str


class _Builder:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self.pos = 0
        self.styles: list[Style] = []
        self.images: list[ImageSlot] = []

    def write(self, text: str, *kinds: str, url: str | None = None) -> None:
        text = _UNSAFE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
        if not text:
            return
        start = self.pos
        self._parts.append(text)
        self.pos += u16len(text)
        for kind in kinds:
            self.styles.append(Style(start, self.pos, kind, url if kind == "link" else None))

    def para(self, kind: str, start: int) -> None:
        if self.pos > start:
            self.styles.append(Style(start, self.pos, kind))

    def text(self) -> str:
        return "".join(self._parts)


# ---------------------------------------------------------------------------
# Inline Discord markdown
# ---------------------------------------------------------------------------

_INLINE = [
    ("code", re.compile(r"(?<!`)(`{1,2})(?!`)(.+?)(?<!`)\1(?!`)")),
    ("mdlink", re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")),
    ("url", re.compile(r"<?(https?://[^\s<>]+)>?")),
    ("bold", re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")),
    ("underline", re.compile(r"__(?=\S)(.+?)(?<=\S)__")),
    ("strike", re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")),
    ("italic", re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])")),
    ("italic", re.compile(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])")),
]
_TRAILING_PUNCT = ".,;:!?'\""


def _split_url(url: str) -> tuple[str, str]:
    """Split trailing punctuation that is almost never part of a pasted URL."""
    trail = ""
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT or (last == ")" and url.count(")") > url.count("(")):
            trail = last + trail
            url = url[:-1]
        else:
            break
    return url, trail


def _inline(b: _Builder, text: str, kinds: tuple[str, ...] = (), url: str | None = None) -> None:
    pos = 0
    while pos < len(text):
        best: tuple[str, re.Match[str]] | None = None
        for name, rx in _INLINE:
            m = rx.search(text, pos)
            if m and (best is None or m.start() < best[1].start()):
                best = (name, m)
        if best is None:
            break
        name, m = best
        if m.start() > pos:
            b.write(text[pos : m.start()], *kinds, url=url)
        if name == "code":
            b.write(m.group(2), *kinds, "code", url=url)
        elif name == "mdlink":
            _inline(b, m.group(1), (*kinds, "link"), m.group(2))
        elif name == "url":
            core, trail = _split_url(m.group(1))
            if "link" in kinds:
                b.write(core, *kinds, url=url)
            else:
                b.write(core, *kinds, "link", url=core)
            b.write(trail, *kinds, url=url)
        else:
            _inline(b, m.group(1), (*kinds, name), url)
        pos = m.end()
    if pos < len(text):
        b.write(text[pos:], *kinds, url=url)


# ---------------------------------------------------------------------------
# Block-level Discord markdown
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:([\w+#.-]+)[ \t]*\n)?(.*?)```", re.S)
_HEADING = re.compile(r"^#{1,3}\s+(.*)$")
_SUBTEXT = re.compile(r"^-#\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")


def _write_body(b: _Builder, content: str) -> None:
    pos = 0
    for m in _FENCE.finditer(content):
        _write_lines(b, content[pos : m.start()])
        _write_code_block(b, m.group(2))
        pos = m.end()
    _write_lines(b, content[pos:])


def _write_code_block(b: _Builder, code: str) -> None:
    code = code.strip("\n")
    if not code:
        return
    start = b.pos
    b.write(code + "\n", "code")
    b.para("p_code", start)


def _write_lines(b: _Builder, text: str) -> None:
    text = text.replace("\r\n", "\n").strip("\n")
    if not text.strip():
        return
    quote_rest = False
    for line in text.split("\n"):
        start = b.pos
        quoted = quote_rest
        if line.startswith(">>> "):
            quote_rest = quoted = True
            line = line[4:]
        elif line.startswith("> "):
            quoted = True
            line = line[2:]
        kinds: tuple[str, ...] = ()
        if (m := _HEADING.match(line)) is not None:
            line, kinds = m.group(1), ("bold",)
        elif (m := _SUBTEXT.match(line)) is not None:
            line, kinds = m.group(1), ("muted",)
        elif (m := _BULLET.match(line)) is not None:
            b.write(f"{m.group(1)}• ")
            line = m.group(2)
        _inline(b, line, kinds)
        b.write("\n")
        if quoted:
            b.para("p_quote", start)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _fmt(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _one_line(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _human_size(n: int | None) -> str | None:
    if n is None:
        return None
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return None


def _embeddable_image(a: Attachment) -> bool:
    ctype = (a.content_type or "").split(";")[0].strip().lower()
    if ctype:
        ok = ctype in _IMAGE_TYPES
    else:
        ok = a.filename.lower().endswith(_IMAGE_EXTS)
    if not ok:
        return False
    if a.size is not None and a.size > _MAX_IMAGE_BYTES:
        return False
    if a.width and a.height and a.width * a.height > _MAX_IMAGE_PIXELS:
        return False
    return True


def _write_attachments(b: _Builder, attachments: tuple[Attachment, ...], embed_images: bool) -> None:
    for a in attachments:
        b.write("📎 ")
        b.write(a.filename, "link", url=a.url)
        meta = [x for x in ((a.content_type or "").split(";")[0] or None, _human_size(a.size)) if x]
        if meta:
            b.write(f"  ({', '.join(meta)})", "muted")
        b.write("\n")
        if a.text and a.text.strip():
            start = b.pos
            if a.filename.lower().endswith((".md", ".markdown")):
                _write_body(b, a.text)
            else:
                _write_code_block(b, a.text)
            b.para("p_quote", start)
        if embed_images and _embeddable_image(a):
            width = min(_PAGE_WIDTH_PT, a.width * 0.75) if a.width else _PAGE_WIDTH_PT
            b.images.append(ImageSlot(b.pos, a.url, a.filename, width))
            b.write("\n")  # empty paragraph that the image is inserted into


def _write_embeds(b: _Builder, embeds: tuple[EmbedInfo, ...]) -> None:
    for e in embeds:
        start = b.pos
        if e.title:
            if e.url:
                b.write(e.title, "bold", "link", url=e.url)
            else:
                b.write(e.title, "bold")
            b.write("\n")
        if e.description:
            _inline(b, e.description)
            b.write("\n")
        for name, value in e.fields:
            b.write(f"{name}: ", "bold")
            _inline(b, value)
            b.write("\n")
        if e.url and not e.title:
            b.write(e.url, "link", url=e.url)
            b.write("\n")
        b.para("p_quote", start)


def _write_message(b: _Builder, m: MessageSnapshot, tz: tzinfo, embed_images: bool) -> None:
    start = b.pos
    b.write(m.author_name, "bold")
    if m.author_username and m.author_username.lower() != m.author_name.lower():
        b.write(f" (@{m.author_username})", "muted")
    if m.author_is_bot:
        b.write(" [bot]", "muted")
    b.write(f"  ·  {_fmt(m.created_at, tz)}", "muted")
    if m.edited_at:
        b.write(f"  ·  edited {_fmt(m.edited_at, tz)}", "muted")
    b.write("  ·  ", "muted")
    b.write("open in Discord", "muted", "link", url=m.jump_url)
    b.write(f"  ·  msg {m.id}", "muted")
    b.write("\n")
    b.para("p_msghead", start)

    if m.reply_to:
        r = m.reply_to
        b.write("↪ replying to ", "muted", "italic")
        b.write(r.author or "a message", "muted", "italic", "link", url=r.jump_url)
        if r.snippet:
            b.write(f": “{_one_line(r.snippet)}”", "muted", "italic")
        b.write("\n")

    if m.content.strip():
        _write_body(b, m.content)
    _write_attachments(b, m.attachments, embed_images)
    _write_embeds(b, m.embeds)
    for f in m.forwards:
        b.write("⤷ forwarded message\n", "muted", "italic")
        if f.content.strip():
            _write_body(b, f.content)
        _write_attachments(b, f.attachments, embed_images)
        _write_embeds(b, f.embeds)


# ---------------------------------------------------------------------------
# Whole tab
# ---------------------------------------------------------------------------


def _norm_url(url: str | None) -> str | None:
    """Discord CDN URLs carry expiring signatures; ignore them when hashing."""
    if not url:
        return url
    parts = urlsplit(url)
    if parts.netloc in _CDN_HOSTS:
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    return url


def render_thread(
    thread: ThreadSnapshot,
    *,
    student: str,
    topic: str | None,
    tz: tzinfo,
    tz_name: str,
    embed_images: bool = True,
) -> RenderedTab:
    b = _Builder()

    start = b.pos
    b.write(thread.name or "(untitled thread)")
    b.write("\n")
    b.para("p_h1", start)

    b.write("Student: ", "bold")
    b.write(student)
    if topic:
        b.write("  ·  Topic: ", "bold")
        b.write(topic)
    b.write("\n")

    # The thread_id marker lets the bot re-adopt this tab if its state database is lost.
    b.write("Discord thread", "muted", "link", url=thread.jump_url)
    b.write(
        f"  ·  thread_id: {thread.thread_id}  ·  posted {_fmt(thread.created_at, tz)}"
        f"  ·  {len(thread.messages)} messages  ·  times in {tz_name}",
        "muted",
    )
    b.write("\n")

    for m in thread.messages:
        _write_message(b, m, tz, embed_images)
    if not thread.messages:
        b.write("(no messages)\n", "muted", "italic")

    text = b.text()
    assert text.endswith("\n")
    text = text[:-1]  # the tab body already ends with a newline of its own
    total = u16len(text)

    styles: list[Style] = []
    for s in b.styles:
        limit = total + 1 if s.is_paragraph else total
        end = min(s.end, limit)
        if s.start < end and s.start <= total:
            styles.append(Style(s.start, end, s.kind, s.url))
    images = [i for i in b.images if i.offset <= total]

    payload = [
        text,
        [(s.start, s.end, s.kind, _norm_url(s.url)) for s in styles],
        [(i.offset, _norm_url(i.url), i.filename, i.width_pt) for i in images],
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RenderedTab(text=text, styles=styles, images=images, content_hash=digest)


def tab_title(fmt: str, thread: ThreadSnapshot, student: str, tz: tzinfo) -> str:
    title = fmt.format(
        date=thread.created_at.astimezone(tz).strftime("%Y-%m-%d"),
        title=thread.name or "",
        student=student,
    )
    title = " ".join(title.split()).strip(" -")
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1] + "…"
    return title or f"Report {thread.thread_id}"
