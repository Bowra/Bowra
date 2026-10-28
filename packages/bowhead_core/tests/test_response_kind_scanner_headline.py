"""UT for Sprint chat-style-fix Phase A2 — headline scanner extension.

Covers tech-design §I-1 disorder rules:
  R-1: `<headline>` before response_kind decision → still captured.
  R-2: body > _MAX_HEADLINE_BUFFER without close → truncated.
  R-3: stream ends without `</headline>` → missing-close (finalize_headline).
  R-4: second `<headline>` open observed → FIRST-WINS, second stripped.

8 UT matrix per task spec:
  1. Clean full-form
  2. Split-across-chunks
  3. Missing-open (no headline emitted)
  4. Never emitted (same as #3, asserts finalize → absent)
  5. >200 char truncate
  6. Out-of-order before response_kind (R-1, captured)
  7. Duplicate (R-4)
  8. Post-hoc sanitizer strip
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.response_kind_scanner import (
    feed,
    finalize_headline,
    flush,
    init_per_turn_scanner_state,
    strip_response_kind_markers,
)


def test_headline_clean_full_form() -> None:
    """`<response_kind>chat</response_kind><headline>梳理三年合同</headline>剩余`.

    In a single feed call, response_kind locks AND headline is captured
    (scanner pipes residual through `_feed_headline` same tick).
    """
    s = init_per_turn_scanner_state()
    safe, s = feed(
        s,
        "<response_kind>chat</response_kind><headline>梳理三年合同</headline>剩余",
    )
    assert s.decision == "chat"
    assert s.headline_text == "梳理三年合同"
    assert s.headline_decision == "captured"
    assert s.headline_locked is True
    assert safe == "剩余"


def test_headline_split_across_chunks() -> None:
    """Split mid-tag — typical stream shape."""
    s = init_per_turn_scanner_state()
    accum = ""
    chunks = [
        "<resp",
        "onse_kind>chat",
        "</response_kind><head",
        "line>X</head",
        "line>Y",
    ]
    for c in chunks:
        safe, s = feed(s, c)
        accum += safe
    assert s.decision == "chat"
    assert s.headline_text == "X"
    assert s.headline_decision == "captured"
    # safe_text accumulates only the prose "Y" — markers stripped.
    assert accum == "Y"


def test_headline_missing_open_finalize() -> None:
    """response_kind locked, no `<headline>` ever → finalize → absent."""
    s = init_per_turn_scanner_state()
    _safe, s = feed(s, "<response_kind>chat</response_kind>just prose no marker")
    assert s.decision == "chat"
    assert s.headline_locked is False
    # Stream end — finalize:
    s = finalize_headline(s)
    assert s.headline_locked is True
    assert s.headline_decision == "absent"
    assert s.headline_text is None


def test_headline_never_emitted_after_locking() -> None:
    """response_kind absent decision + no headline → still finalize cleanly."""
    s = init_per_turn_scanner_state()
    # Feed enough to force absent decision (no markers).
    _safe, s = feed(s, "x" * 200)
    assert s.decision == "absent"
    s = finalize_headline(s)
    # When response_kind is absent the headline phase still finalizes:
    # headline_seen_open=False → decision="absent"
    assert s.headline_decision == "absent"
    assert s.headline_text is None


def test_headline_truncate_over_200() -> None:
    """Body 250 chars + `</headline>` → headline_text len ≤ 200, truncate."""
    s = init_per_turn_scanner_state()
    body = "x" * 250
    _safe, s = feed(s, f"<response_kind>chat</response_kind><headline>{body}</headline>tail")
    # The non-greedy regex won't match if body > 200 (regex caps at 200) — so
    # this falls into the truncate branch.
    assert s.headline_locked is True
    assert s.headline_decision == "truncated"
    assert s.headline_text is not None
    assert len(s.headline_text) <= 200


def _feed_all(s, chunks):
    accum = ""
    for c in chunks:
        safe, s = feed(s, c)
        accum += safe
    return accum, s


def test_headline_before_response_kind_is_captured() -> None:
    """R-1 — `<headline>` before the response_kind marker is captured, not leaked."""
    s = init_per_turn_scanner_state()
    accum, s = _feed_all(
        s,
        ["<headline>foo</headline>", "<response_kind>chat</response_kind>", "body"],
    )
    assert s.decision == "chat"
    assert s.headline_text == "foo"
    assert s.headline_decision == "captured"
    assert s.headline_locked is True
    assert accum == "body"


def test_long_headline_before_response_kind_split_into_small_chunks() -> None:
    """A headline longer than the 64-char lookahead must not leak before its close tag."""
    title = "Quarterly revenue grew across every region while margins held steady"
    stream = f"<headline>{title}</headline>\n<response_kind>canvas</response_kind>\n正文"
    s = init_per_turn_scanner_state()
    accum, s = _feed_all(s, [stream[i : i + 3] for i in range(0, len(stream), 3)])
    assert s.decision == "canvas"
    assert s.headline_text == title
    assert "headline" not in accum
    assert "response_kind" not in accum
    assert accum.strip() == "正文"


def test_headline_only_stream_without_response_kind() -> None:
    """A headline is captured even when the model never emits response_kind."""
    s = init_per_turn_scanner_state()
    body = "这是正文。" * 40
    accum, s = _feed_all(s, ["<head", "line>增长放缓</headline>\n\n", body])
    assert s.decision == "absent"
    assert s.headline_text == "增长放缓"
    assert s.headline_decision == "captured"
    assert accum == "\n\n" + body


def test_markers_repeated_on_a_later_llm_call_are_dropped() -> None:
    """One scanner state spans every LLM call of a Lead turn; repeats never leak."""
    s = init_per_turn_scanner_state()
    first = "<response_kind>chat</response_kind><headline>First</headline>先查数据。"
    second = "<response_kind>chat</response_kind>\n<headline>Second</headline>\n结论如下 a<b。"
    chunks = [first[i : i + 4] for i in range(0, len(first), 4)]
    chunks += [second[i : i + 2] for i in range(0, len(second), 2)]
    accum, s = _feed_all(s, chunks)
    assert s.headline_text == "First"
    assert "response_kind" not in accum
    assert "headline" not in accum
    assert "Second" not in accum
    assert accum == "先查数据。\n\n结论如下 a<b。"


def test_missing_open_marker_on_a_later_llm_call_is_dropped() -> None:
    s = init_per_turn_scanner_state()
    accum, s = _feed_all(
        s,
        ["<response_kind>chat</response_kind>第一段。", "_kind>", "chat</response_kind>第二段 snake_case_name。"],
    )
    assert accum == "第一段。第二段 snake_case_name。"


def test_post_lock_prose_with_angle_brackets_passes_through() -> None:
    s = init_per_turn_scanner_state()
    accum, s = _feed_all(
        s,
        ["<response_kind>chat</response_kind>", "x <", "= 3 and <h2>Title</h2> <he", "ight>"],
    )
    assert accum == "x <= 3 and <h2>Title</h2> <height>"


def test_headline_duplicate_first_wins() -> None:
    """R-4 — second `<headline>` after first captured → first wins, second stripped."""
    s = init_per_turn_scanner_state()
    safe, s = feed(
        s,
        "<response_kind>chat</response_kind><headline>A</headline><headline>B</headline>剩余",
    )
    assert s.decision == "chat"
    assert s.headline_text == "A"
    assert s.headline_decision == "duplicate"
    # Second `<headline>B</headline>` stripped.
    assert "<headline>" not in safe
    assert "B" not in safe
    assert safe == "剩余"


def test_headline_post_hoc_strip() -> None:
    """`strip_response_kind_markers` covers headline tags too."""
    text = "<response_kind>chat</response_kind><headline>hi</headline>body"
    cleaned = strip_response_kind_markers(text)
    assert cleaned == "body"

    # Idempotent.
    assert strip_response_kind_markers(cleaned) == "body"

    # Mid-text headline scrub (archived turn replay).
    text2 = "prose<headline>summary</headline>more prose"
    assert strip_response_kind_markers(text2) == "prosemore prose"


def test_post_hoc_strip_leaves_no_leading_blank_lines() -> None:
    """Marker lines at the top used to leave "\\n\\n\\n\\n" before the answer."""
    text = "<response_kind>chat</response_kind>\n\n<headline>结论</headline>\n\n第一段。\n\n第二段。"
    assert strip_response_kind_markers(text) == "第一段。\n\n第二段。"


def test_post_hoc_strip_takes_blank_lines_after_a_marker_line_only() -> None:
    text = "第一段。\n\n<headline>重复</headline>\n\n第二段。\n<response_kind>canvas</response_kind> 第三段。"
    assert strip_response_kind_markers(text) == "第一段。\n\n第二段。\n 第三段。"


def test_post_hoc_strip_keeps_code_blank_lines_and_indentation() -> None:
    text = "<response_kind>chat</response_kind>\n    indented first line\n\n```py\nimport os\n\n\ndef f():\n    pass\n```"
    assert strip_response_kind_markers(text) == "    indented first line\n\n```py\nimport os\n\n\ndef f():\n    pass\n```"


def test_post_hoc_strip_agrees_with_stream_filter_on_non_markers() -> None:
    """Orphan close tags, non-enum bodies and multi-line headlines are prose in both."""
    for prose in (
        "write </headline> literally",
        "text <response_kind>对话</response_kind> tail",
        "```xml\n<headline>\n  <title>x</title>\n</headline>\n```",
    ):
        assert strip_response_kind_markers(prose) == prose
        streamed = "".join(_feed_charwise("<response_kind>chat</response_kind>" + prose))
        assert streamed == prose


def test_post_hoc_strip_keeps_text_without_markers_byte_identical() -> None:
    text = "\n\n  indented start\n\n\n\nkept as is"
    assert strip_response_kind_markers(text) == text


def _feed_charwise(text: str, *, use_flush: bool = True) -> list[str]:
    state = init_per_turn_scanner_state()
    out: list[str] = []
    for char in text:
        safe, state = feed(state, char)
        out.append(safe)
    if use_flush:
        tail, state = flush(state)
        out.append(tail)
    return out


_MARKERS = "<response_kind>chat</response_kind>\n<headline>Rev up</headline>\n"
_PROSE_CASES = [
    "If a < b and c<d then x <= y; also 3<5.",
    "```html\n<div><h1>Title</h1><p>a</p></div>\n```\nDone.",
    "```xml\n<headline>\n  <title>x</title>\n```\nEnd of answer.",
    "参见《报告》第3章，数据<100万的城市占比_kind较低。",
    "the_kind> foo, sort_kind>bar and _kind> baz ok",
    "Column _kind>chat is weird but end",
    "x is a <",
    "name it _",
    "see <h",
    "value _kind",
    "write </headline> literally",
    "text <response_kind>对话</response_kind> tail",
    "Generic List<String> and Map<K,V> and <T> type and <-- arrow",
    "<head><title>x</title></head> <header>h</header> <h1>a</h1>",
]


@pytest.mark.parametrize("prose", _PROSE_CASES)
def test_no_prose_is_lost_after_the_markers(prose: str) -> None:
    """Every non-marker character reaches narrative once the held tail is flushed."""
    assert "".join(_feed_charwise(_MARKERS + prose)) == "\n\n" + prose


@pytest.mark.parametrize(
    "text",
    [
        "<headline>Revenue grew\n\n" + "The answer is long. " * 30,
        "<headline>Revenue grew\n" + "Body text here. " * 5 + "<response_kind>chat</response_kind> more",
        "<headline>" + "x" * 230 + " and then the body continues for a while.",
    ],
)
def test_unclosed_headline_before_the_lock_does_not_hold_the_stream(text: str) -> None:
    out = _feed_charwise(text, use_flush=False)
    assert "".join(out) == text.replace("<response_kind>chat</response_kind>", "")


def test_single_line_headline_over_the_cap_before_the_lock_is_truncated() -> None:
    state = init_per_turn_scanner_state()
    for char in "<headline>" + "x" * 230 + " body":
        _safe, state = feed(state, char)
    assert state.headline_decision == "truncated"
    assert state.headline_text == "x" * 200


def test_flush_releases_held_text_and_strips_complete_markers() -> None:
    state = init_per_turn_scanner_state()
    _safe, state = feed(state, "<response_kind>chat</response_kind>see <h")
    tail, state = flush(state)
    assert tail == "<h"
    assert state.headline_buffer == "" and state.buffer == ""
    # A later call continues normally after the flush.
    safe, state = feed(state, "<headline>T</headline>next")
    assert safe == "next" and state.headline_text == "T"
    empty, state = flush(state)
    assert empty == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("para1\n<headline>x</headline>\n\npara2", "para1\n\npara2"),
        ("para1\n<headline>x</headline>\npara2", "para1\npara2"),
        ("para1\n\n<headline>x</headline>\n\npara2", "para1\n\npara2"),
        ("para1\n<response_kind>chat</response_kind>\n<headline>x</headline>\n\npara2", "para1\n\npara2"),
        ("para1\n\n<response_kind>chat</response_kind>\n<headline>x</headline>\n\npara2", "para1\n\npara2"),
        ("para1\n<response_kind>chat</response_kind>\n\n<headline>x</headline>\n\npara2", "para1\n\npara2"),
        ("<response_kind>chat</response_kind>\n<headline>x</headline>\n\npara2", "para2"),
    ],
)
def test_post_hoc_strip_keeps_paragraph_breaks_around_marker_lines(text: str, expected: str) -> None:
    assert strip_response_kind_markers(text) == expected


@pytest.mark.parametrize("size", [1, 2, 3, 5])
@pytest.mark.parametrize("lead_in", ["", "好的，", "Sure.\n"])
def test_split_missing_open_marker_before_the_lock_never_leaks(size: int, lead_in: str) -> None:
    """DeepSeek drops `<response`; the remaining marker used to leak when chunked small."""
    text = lead_in + "_kind>chat</response_kind>正文 with snake_kind> and _kind> prose"
    state = init_per_turn_scanner_state()
    out = []
    for i in range(0, len(text), size):
        safe, state = feed(state, text[i : i + size])
        out.append(safe)
        assert "response_kind" not in "".join(out)
    tail, state = flush(state)
    assert state.decision == "chat" and state.match_mode == "missing_open"
    assert "".join(out) + tail == lead_in + "正文 with snake_kind> and _kind> prose"


@pytest.mark.parametrize("length", [189, 190, 195, 199, 200])
@pytest.mark.parametrize("size", [1, 2, 3, 5, 7])
@pytest.mark.parametrize("prefix", ["", "<response_kind>chat</response_kind>"])
def test_headline_near_the_cap_is_captured_when_chunked(length: int, size: int, prefix: str) -> None:
    body = "h" * length
    text = f"{prefix}<headline>{body}</headline>tail text"
    state = init_per_turn_scanner_state()
    out = []
    for i in range(0, len(text), size):
        safe, state = feed(state, text[i : i + size])
        out.append(safe)
    tail, state = flush(state)
    assert "".join(out) + tail == "tail text"
    assert state.headline_text == body
