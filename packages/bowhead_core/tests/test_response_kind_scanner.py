"""UT for Sprint ω Phase 2.1 `response_kind_scanner` state machine.

Covers Q3 (130 LLM call) ground truth + r3.1 P0 fixes:
- Clean full-form match (3-11 chunk split + case-mixed + pre-prose 6-61 char)
- DeepSeek missing-open (47% of streams) with `(?<![A-Za-z0-9])_kind>` lookbehind
- Absent (20% chat DeepSeek) — fallback after `_ABSENT_THRESHOLD`
- Unicode adversarial — `<response_kind>对话</response_kind>` → absent, not crash
- Per-turn reset — frozen-corpus replay invariant
- Buffer never leaks any `<response_kind` / `</response_kind` / `_kind>` substring
"""

from __future__ import annotations

from bowhead_core.agents.response_kind_scanner import (
    feed,
    init_per_turn_scanner_state,
)

# ─── Clean full-form match ──────────────────────────────────────────────


def test_clean_whole_in_one_chunk() -> None:
    """Marker arrives whole in single chunk (theoretical — Q3 0/65 in practice)."""
    s = init_per_turn_scanner_state()
    safe, s = feed(s, "<response_kind>chat</response_kind>hello")
    assert s.decision == "chat"
    assert s.match_mode == "clean"
    assert safe == "hello"
    assert s.locked is True


def test_clean_canvas() -> None:
    s = init_per_turn_scanner_state()
    safe, s = feed(s, "<response_kind>canvas</response_kind>report content")
    assert s.decision == "canvas"
    assert safe == "report content"


def test_clean_split_across_two_chunks() -> None:
    """`<response_kind>chat</response_kind>` split mid-tag — typical DeepSeek shape."""
    s = init_per_turn_scanner_state()
    safe1, s = feed(s, "<resp")
    safe2, s = feed(s, "onse_kind>chat</response_kind>hello")
    assert s.decision == "chat"
    # No marker fragment leaks
    assert "<resp" not in safe1
    assert "response_kind" not in (safe1 + safe2).lower()
    assert safe1 + safe2 == "hello"


def test_clean_split_across_many_chunks() -> None:
    """Q3 DeepSeek mean delta=2.18 char — split into 10+ chunks is the norm."""
    s = init_per_turn_scanner_state()
    full = "<response_kind>canvas</response_kind>final"
    accum = ""
    # Feed 2 chars at a time
    for i in range(0, len(full), 2):
        chunk = full[i : i + 2]
        safe, s = feed(s, chunk)
        accum += safe
    assert s.decision == "canvas"
    assert accum == "final"


def test_clean_case_mixed() -> None:
    s = init_per_turn_scanner_state()
    safe, s = feed(s, "<Response_Kind>CHAT</response_kind>x")
    assert s.decision == "chat"
    assert safe == "x"


def test_clean_with_pre_prose_chinese() -> None:
    """Q3: pre-marker prose avg 6 / max 61 chars (Chinese-dominant corpus)."""
    s = init_per_turn_scanner_state()
    safe, s = feed(s, "好的，分析\n<response_kind>chat</response_kind>real prose")
    assert s.decision == "chat"
    assert safe == "好的，分析\nreal prose"


def test_clean_with_whitespace_inside_tag() -> None:
    s = init_per_turn_scanner_state()
    _safe, s = feed(s, "<response_kind> chat </response_kind>x")
    assert s.decision == "chat"


# ─── Missing-open (DeepSeek 47%) ────────────────────────────────────────


def test_missing_open_at_start() -> None:
    """Stream starts with `_kind>chat</response_kind>` — Q3 DS 47%."""
    s = init_per_turn_scanner_state()
    safe, s = feed(s, "_kind>chat</response_kind>real")
    assert s.decision == "chat"
    assert s.match_mode == "missing_open"
    assert safe == "real"


def test_missing_open_with_chinese_prose_prefix() -> None:
    """`\\n_kind>...` after Chinese prose — boundary OK (`\\n` is non-word)."""
    s = init_per_turn_scanner_state()
    safe, s = feed(s, "好的，分析\n_kind>chat</response_kind>real prose")
    assert s.decision == "chat"
    assert s.match_mode == "missing_open"
    assert safe == "好的，分析\nreal prose"


def test_missing_open_with_english_letter_prefix() -> None:
    """r3.1 CTO P0-r3-1 catch: `the_kind>chat` MUST NOT fire — `the` is word
    chars, `_kind>` could be part of `the_kind` identifier (e.g. snake_case
    variable echoed in trace). Should fall to absent."""
    s = init_per_turn_scanner_state()
    # 100+ chars so we hit absent threshold
    payload = "the_kind>chat</response_kind>" + "x" * 100
    _safe, s = feed(s, payload)
    # Not "chat" — letter-prefix means it's not a marker, treat as absent
    assert s.decision == "absent", f"`the_kind>` should NOT match (English-letter prefix). Got decision={s.decision}"


def test_missing_open_case_mixed() -> None:
    s = init_per_turn_scanner_state()
    _safe, s = feed(s, "_KIND>CANVAS</response_kind>X")
    assert s.decision == "canvas"


# ─── Absent (DeepSeek 20% chat) ─────────────────────────────────────────


def test_absent_when_no_marker_within_window() -> None:
    """20% DS chat queries emit NO marker. After 80+ char prose, give up."""
    s = init_per_turn_scanner_state()
    prose = "这是没有任何标记的纯散文回答" * 10  # 110+ chars
    safe, s = feed(s, prose)
    assert s.decision == "absent"
    assert s.match_mode == "absent"
    assert safe == prose


def test_absent_in_multiple_chunks() -> None:
    s = init_per_turn_scanner_state()
    for _ in range(10):
        _safe, s = feed(s, "no marker here ")
    # After enough chunks past threshold, decision="absent"
    assert s.decision == "absent"


# ─── Unicode adversarial ────────────────────────────────────────────────


def test_unicode_kind_value_falls_to_absent() -> None:
    """`<response_kind>对话</response_kind>` — Q3 r3.1 invariant. Strict
    (chat|canvas) enum rejects Chinese kind. Must NOT raise KeyError."""
    s = init_per_turn_scanner_state()
    # Add enough trailing text to exceed absent threshold (no buffer leak)
    payload = "<response_kind>对话</response_kind>" + "y" * 100
    _safe, s = feed(s, payload)
    # Strict regex doesn't match — falls to absent
    assert s.decision == "absent"
    # Critical: no KeyError, no crash


# ─── Post-decision pass-through ─────────────────────────────────────────


def test_locked_state_passes_through() -> None:
    s = init_per_turn_scanner_state()
    _, s = feed(s, "<response_kind>chat</response_kind>")
    assert s.locked is True
    # Subsequent chunks pass through unchanged
    safe, s = feed(s, "more text after lock")
    assert safe == "more text after lock"


# ─── Per-turn reset invariant (CTO P0-r3.1-2) ───────────────────────────


def test_scanner_resets_per_turn() -> None:
    """Frozen-corpus replay 同 turn_id 重放 if stale state, locked=True →
    pass-through → safe_text 不同 → false negative on rouge-L diff."""
    # Turn 1
    s1 = init_per_turn_scanner_state()
    _safe1, s1 = feed(s1, "<response_kind>chat</response_kind>turn1")
    assert s1.decision == "chat" and s1.locked is True

    # Turn 2 — fresh state, NOT reusing s1
    s2 = init_per_turn_scanner_state()
    assert s2.locked is False
    assert s2.decision is None
    _safe2, s2 = feed(s2, "<response_kind>canvas</response_kind>turn2")
    assert s2.decision == "canvas"


# ─── Buffer-leak invariants ─────────────────────────────────────────────


def test_buffer_never_leaks_open_tag_fragment() -> None:
    s = init_per_turn_scanner_state()
    accum = ""
    for chunk in ["<res", "pon", "se_k", "ind>", "chat", "</res", "ponse_kind>", "trail"]:
        safe, s = feed(s, chunk)
        accum += safe
        # At every step, no fragment of marker visible
        assert "<response_kind" not in accum
        assert "</response_kind" not in accum
    assert s.decision == "chat"
    assert accum == "trail"


def test_adversarial_truncated_stream_times_out() -> None:
    """LLM truncates after `<respo` and never closes — scanner times out at
    ABSENT_THRESHOLD, decision="absent" (avoid hanging forever)."""
    s = init_per_turn_scanner_state()
    # Send `<respo` then 200 chars of nothing related
    safe, s = feed(s, "<respo")
    assert s.decision is None  # still scanning
    for _ in range(20):
        _safe, s = feed(s, " filler " * 5)
    assert s.decision == "absent"


def test_mid_stream_orphan_not_at_start_of_buffer() -> None:
    """`prefix <response_kind>chat</response_kind>` — clean match handles this
    via re.search (not anchored), so it should work as `clean`."""
    s = init_per_turn_scanner_state()
    safe, s = feed(s, "prefix <response_kind>chat</response_kind> mid")
    assert s.decision == "chat"
    assert s.match_mode == "clean"
    assert safe == "prefix  mid"


# ─── Phase 2 P0-2 split-chunk lookbehind bypass regression ─────────────────


def test_split_chunk_word_char_then_missing_open_does_not_false_match() -> None:
    """Phase 2 P0-2 fix (Sprint ω 2026-05-12): if a word-char like `e` was
    emitted in a previous chunk, the next chunk starting with `_kind>chat...`
    must NOT false-match — the real semantic predecessor is a letter, so
    missing-open hint should be rejected.

    Pre-fix: 1st feed emits `"the"`, buffer empty. 2nd feed buffer=`_kind>...`,
    `(?<![A-Za-z0-9])` lookbehind sees no predecessor (start-of-buffer) and
    passes → decision=`chat` erroneously, AND the leaked `_kind>chat</response_kind>`
    bytes are emitted as safe_text (visible to user).
    Post-fix: scanner tracks `last_word_char=e` and prepends it during regex
    search, so the lookbehind fails and the buffer stays in scanning state.
    """
    s = init_per_turn_scanner_state()
    safe1, s = feed(s, "the")
    assert safe1 == "the"
    assert s.decision is None
    assert s.last_word_char == "e"
    safe2, s = feed(s, "_kind>chat</response_kind>tail")
    # Critical: scanner must NOT decide based on the spurious missing-open
    # (the real predecessor is `e`, a word-char). Falls to absent after
    # threshold OR stays scanning. In either case, NO false `chat` decision.
    assert s.decision != "chat" or s.match_mode != "missing_open", (
        f"False missing-open match: decision={s.decision} mode={s.match_mode} safe2={safe2!r}"
    )


def test_split_chunk_non_word_then_missing_open_still_matches() -> None:
    """Inverse: word-char tracker must NOT suppress legitimate missing-open
    after non-word predecessors (Chinese, punctuation, whitespace, newline).
    """
    for prefix_emit in ["你好，", "hello.\n", "  "]:
        s = init_per_turn_scanner_state()
        _safe1, s = feed(s, prefix_emit)
        # last_word_char should NOT carry a word-char (last char is non-word)
        assert s.last_word_char == ""
        _safe2, s = feed(s, "_kind>chat</response_kind>tail")
        assert s.decision == "chat", f"Should match missing-open after non-word prefix {prefix_emit!r}: got {s.decision}"
        assert s.match_mode == "missing_open"


def test_last_word_char_resets_after_marker_decision() -> None:
    """After decision lock, state is reset (last_word_char cleared)."""
    s = init_per_turn_scanner_state()
    _safe, s = feed(s, "abc<response_kind>canvas</response_kind>xyz")
    assert s.decision == "canvas"
    assert s.last_word_char == ""
    assert s.locked is True
