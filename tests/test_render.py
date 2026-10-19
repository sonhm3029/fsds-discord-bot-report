from dataclasses import replace

from conftest import TZ, T0, make_message, make_thread, slice16
from discord_gdocs_sync.models import Attachment, ReplyRef
from discord_gdocs_sync.render import TITLE_MAX, render_thread, tab_title, u16len


def render(messages, **kw):
    return render_thread(
        make_thread(messages), student="Sophie", topic="KV-Cache", tz=TZ, tz_name="Asia/Ho_Chi_Minh", **kw
    )


def styled(r, kind):
    return [(slice16(r.text, s.start, s.end), s) for s in r.styles if s.kind == kind]


def test_u16len_counts_astral_characters_twice():
    assert u16len("a") == 1
    assert u16len("😀") == 2
    assert u16len("é") == 1


def test_offsets_are_utf16_after_emoji():
    r = render([make_message(1, "😀😀 **bold** after")])
    assert [t for t, _ in styled(r, "bold") if t == "bold"] == ["bold"]
    # Python indices would be off by two here; UTF-16 offsets must slice exactly.
    assert "😀😀 bold after" in r.text


def test_header_contains_marker_and_metadata():
    r = render([make_message(1, "hi")])
    lines = r.text.split("\n")
    assert lines[0] == "KV cache ablation"
    assert "Student: Sophie" in lines[1] and "Topic: KV-Cache" in lines[1]
    assert "thread_id: 1000" in lines[2]
    assert "times in Asia/Ho_Chi_Minh" in lines[2]
    assert not r.text.endswith("\n")


def test_message_header_has_author_time_and_link():
    r = render([make_message(1, "hello", edited_at=T0)])
    assert "(@sophie)" not in r.text  # username equals display name apart from case
    distinct = replace(make_message(1, "hi"), author_username="sophie_h")
    assert "Sophie (@sophie_h)" in render([distinct]).text
    assert "2026-10-05 14:31" in r.text  # 07:31 UTC → 14:31 in Ho Chi Minh
    assert "edited 2026-10-05 14:30" in r.text
    links = [s.url for s in r.styles if s.kind == "link"]
    assert "https://discord.com/channels/900/500/1001" in links
    assert "msg 1001" in r.text


def test_inline_markdown_and_links():
    r = render([make_message(1, "**b** *i* `c` see https://ex.com/a.")])
    assert [t for t, _ in styled(r, "bold") if t == "b"]
    assert "i" in [t for t, _ in styled(r, "italic")]
    assert "c" in [t for t, _ in styled(r, "code")]
    urls = [(t, s.url) for t, s in styled(r, "link") if s.url == "https://ex.com/a"]
    assert urls == [("https://ex.com/a", "https://ex.com/a")]  # trailing '.' excluded


def test_snake_case_and_math_are_not_italic():
    r = render([make_message(1, "use kv_cache_size and 2*3*4 here")])
    assert styled(r, "italic") == []
    assert "kv_cache_size" in r.text and "2*3*4" in r.text


def test_masked_link():
    r = render([make_message(1, "[paper](https://arxiv.org/abs/1)")])
    assert ("paper", "https://arxiv.org/abs/1") in [(t, s.url) for t, s in styled(r, "link")]
    assert "](" not in r.text


def test_fenced_code_block_is_preserved_verbatim():
    r = render([make_message(1, "before\n```python\nx = 1\n\nprint(x)\n```\nafter")])
    assert "x = 1\n\nprint(x)" in r.text
    assert "```" not in r.text and "python\n" not in r.text
    assert styled(r, "code")[0][0] == "x = 1\n\nprint(x)\n"
    assert any(s.kind == "p_code" for s in r.styles)


def test_inline_triple_backticks_without_newline_are_code_not_language():
    r = render([make_message(1, "run ```ls -la``` now")])
    assert "ls -la" in r.text


def test_quotes_bullets_and_headings():
    r = render([make_message(1, "> quoted\n- item one\n# Title")])
    assert "quoted" in r.text and "> " not in r.text
    assert "• item one" in r.text
    assert "Title" in [t for t, _ in styled(r, "bold")]
    assert any(s.kind == "p_quote" for s in r.styles)


def test_reply_reference_is_rendered_with_link():
    ref = ReplyRef(jump_url="https://discord.com/channels/900/500/999", author="Khoa", snippet="earlier\nquestion")
    r = render([make_message(1, "answer", reply_to=ref)])
    assert "↪ replying to Khoa: “earlier question”" in r.text
    assert "https://discord.com/channels/900/500/999" in [s.url for s in r.styles]


def test_attachments_listed_and_image_slot_created():
    img = Attachment("plot.png", "https://cdn.discordapp.com/attachments/1/2/plot.png?ex=a&hm=b", "image/png", 2048, 400, 300)
    pdf = Attachment("report.pdf", "https://cdn.discordapp.com/attachments/1/3/report.pdf?ex=a", "application/pdf", 5_000_000)
    r = render([make_message(1, "see attached", attachments=(img, pdf)), make_message(2, "next")])
    assert "📎 plot.png  (image/png, 2.0 KB)" in r.text
    assert "📎 report.pdf  (application/pdf, 4.8 MB)" in r.text
    assert len(r.images) == 1
    slot = r.images[0]
    assert slot.width_pt == 300.0  # 400px * 0.75, below the page width
    wide = Attachment("wide.png", "https://cdn.discordapp.com/wide.png", "image/png", 10, 2000, 1000)
    assert render([make_message(1, "x", attachments=(wide,))]).images[0].width_pt == 468.0  # capped
    # The image goes into an empty paragraph directly after the attachment line.
    assert slice16(r.text, slot.offset - 1, slot.offset + 1) == "\n\n"


def test_images_disabled_adds_no_slots_or_blank_lines():
    img = Attachment("plot.png", "https://cdn.discordapp.com/a/plot.png", "image/png", 10)
    with_img = render([make_message(1, "x", attachments=(img,))], embed_images=True)
    without = render([make_message(1, "x", attachments=(img,))], embed_images=False)
    assert without.images == [] and len(without.text) < len(with_img.text)


def test_unsupported_or_huge_images_are_links_only():
    webp = Attachment("a.webp", "https://cdn.discordapp.com/a.webp", "image/webp", 10)
    huge = Attachment("b.png", "https://cdn.discordapp.com/b.png", "image/png", 10, 6000, 6000)
    r = render([make_message(1, "x", attachments=(webp, huge))])
    assert r.images == []


def test_hash_ignores_expiring_cdn_signature_but_not_content():
    def att(q):
        return Attachment("p.png", f"https://cdn.discordapp.com/attachments/1/2/p.png?ex={q}&hm={q}", "image/png", 10)

    a = render([make_message(1, "x", attachments=(att("aaa"),))])
    b = render([make_message(1, "x", attachments=(att("bbb"),))])
    c = render([make_message(1, "x edited", attachments=(att("aaa"),))])
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_deleted_message_changes_hash():
    both = render([make_message(1, "one"), make_message(2, "two")])
    one = render([make_message(1, "one")])
    assert both.content_hash != one.content_hash and "two" not in one.text


def test_style_ranges_stay_inside_the_text():
    r = render([make_message(i, f"**m{i}** `c` https://x.io/{i}\n```\ncode\n```") for i in range(1, 6)])
    total = u16len(r.text)
    for s in r.styles:
        assert 0 <= s.start < s.end <= (total + 1 if s.is_paragraph else total), s


def test_control_characters_are_stripped():
    r = render([make_message(1, "a\x00b\x0bc")])
    assert "abc" in r.text


def test_empty_thread_renders():
    r = render([])
    assert "(no messages)" in r.text


def test_tab_title_format_and_truncation():
    t = make_thread([], name="A" * 200)
    title = tab_title("Report {date} - {title}", t, "Sophie", TZ)
    assert title.startswith("Report 2026-10-05 - AAA") and len(title) == TITLE_MAX == 50
    t2 = replace(t, name="")
    assert tab_title("Report {date} - {title}", t2, "Sophie", TZ) == "Report 2026-10-05"


def test_markdown_attachment_content_is_embedded_and_rendered():
    md = Attachment("report.md", "https://cdn.discordapp.com/r.md?ex=1", "text/markdown", 90,
                    text="# Results\n- **ppl** 6.2\n```\nrun.sh\n```")
    r = render([make_message(1, "", attachments=(md,))])
    assert "📎 report.md" in r.text and "Results" in r.text and "• ppl 6.2" in r.text and "run.sh" in r.text
    assert "# Results" not in r.text and "```" not in r.text
    assert "ppl" in [t for t, _ in styled(r, "bold")]


def test_non_markdown_text_attachment_is_a_verbatim_code_block():
    csv = Attachment("metrics.csv", "https://cdn.discordapp.com/m.csv", "text/csv", 20, text="a,b\n*1*,_2_\n")
    r = render([make_message(1, "", attachments=(csv,))])
    assert "a,b\n*1*,_2_" in r.text  # no markdown interpretation
    assert any(s.kind == "p_code" for s in r.styles)


def test_attachment_text_changes_the_hash():
    def h(text):
        a = Attachment("r.md", "https://cdn.discordapp.com/r.md", "text/markdown", 5, text=text)
        return render([make_message(1, "", attachments=(a,))]).content_hash

    assert h("v1") != h("v2") and h("v1") == h("v1")
