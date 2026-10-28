"""Sprint κ Phase A · StreamingDecomposer unit tests (r2 — closes 3 P0 + 8 P1).

Per tech-design-r3 + Phase A r2 patches:
- Heading schema matches legacy decompose_answer (kind="section" + h1/h2/h3
  attr); body becomes separate narrative section
- Codeblock fence ``` suppresses heading detection inside (P0-3)
- Body extraction uses _last_emit_pos cap (P0-2)
- Marker state reset on section close (P1-4)
- Buffer cap MAX_CONTENT_LEN (P1-10)
- 12-seed grammar coverage per PRD r3 §4.2.3.1 (P1-14)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bowhead_core.canvas_composers.streaming import StreamingDecomposer


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    def by_type(self, et: str) -> list[dict[str, Any]]:
        return [p for t, p in self.events if t == et]

    def kinds(self, et: str) -> list[str]:
        return [p.get("kind") for p in self.by_type(et)]


def _make(turn_id: str = "t_test", invocations: list[Any] | None = None) -> tuple[StreamingDecomposer, _FakeBus]:
    bus = _FakeBus()
    sd = StreamingDecomposer(
        canvas_id="cv_test",
        turn_id=turn_id,
        emit=bus.emit,
        tool_invocations=invocations or [],
        question="",
    )
    return sd, bus


# ─── Empty / single-shot ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_feed_no_events() -> None:
    sd, bus = _make()
    await sd.feed("")
    sections = await sd.finalize()
    assert sections == []
    assert bus.events == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalize_without_feed() -> None:
    sd, bus = _make()
    sections = await sd.finalize()
    assert sections == []
    assert bus.events == []


# ─── Heading schema (matches legacy decompose_answer) ───────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_emits_section_kind_with_h2_attr() -> None:
    """## Heading produces kind=section with h2 attr (legacy schema)."""
    sd, bus = _make()
    await sd.feed("## 数据洞察\n\n营收 +25%\n")
    await sd.finalize()
    dones = bus.by_type("canvas.section_done")
    # 1 section (heading) + 1 narrative for body
    section_dones = [d for d in dones if d["kind"] == "section"]
    narrative_dones = [d for d in dones if d["kind"] == "narrative"]
    assert len(section_dones) == 1
    assert section_dones[0]["payload"]["h2"] == "数据洞察"
    assert section_dones[0]["payload"]["label"] == ""
    assert len(narrative_dones) == 1
    assert "营收" in narrative_dones[0]["payload"]["markdown"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h1_h2_h3_each_emit_correct_attr() -> None:
    sd, bus = _make()
    await sd.feed("# A\n## B\n### C\n内容\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 3
    assert section_dones[0]["payload"]["h1"] == "A"
    assert section_dones[1]["payload"]["h2"] == "B"
    assert section_dones[2]["payload"]["h3"] == "C"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_then_h2_emits_two_sections_two_narratives() -> None:
    sd, bus = _make()
    await sd.feed("## A\n\n内容 A\n\n## B\n\n内容 B\n")
    await sd.finalize()
    dones = bus.by_type("canvas.section_done")
    section_dones = [d for d in dones if d["kind"] == "section"]
    narrative_dones = [d for d in dones if d["kind"] == "narrative"]
    assert len(section_dones) == 2
    assert section_dones[0]["payload"]["h2"] == "A"
    assert section_dones[1]["payload"]["h2"] == "B"
    assert len(narrative_dones) == 2


# ─── Body extraction (P0-2 fix) ─────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_body_section_carries_empty_markdown_not_next_heading() -> None:
    """## A\\n## B\\n  → A's narrative is empty, NOT containing "## B"."""
    sd, bus = _make()
    await sd.feed("## A\n## B\n")
    await sd.finalize()
    narratives = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "narrative"]
    for n in narratives:
        md = n["payload"].get("markdown", "")
        assert "## B" not in md, f"narrative leaked next heading: {md!r}"


# ─── Codeblock state (P0-3 fix) ─────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_h2_inside_codeblock_not_split() -> None:
    """## inside ``` fence is NOT a section boundary."""
    sd, bus = _make()
    await sd.feed("## Real\n\n```python\n## not heading\n```\n后续\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 1, f"expected 1 heading section, got {len(section_dones)}: {section_dones}"
    assert section_dones[0]["payload"]["h2"] == "Real"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_codeblock_fence_balance_matters() -> None:
    """Two ``` fences (open + close) → middle is codeblock, then heading after closes."""
    sd, bus = _make()
    await sd.feed("```\n## X\n```\n## Y\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    # `## X` is in codeblock, `## Y` is real
    assert len(section_dones) == 1
    assert section_dones[0]["payload"]["h2"] == "Y"


# ─── Mid-marker buffering ────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_marker_buffered_until_close() -> None:
    sd, bus = _make()
    await sd.feed("## H\n\n营收 [num:13")
    chunks_after_first = bus.by_type("canvas.section_chunk")
    for c in chunks_after_first:
        assert "[num:13" not in c["delta"], f"half marker leaked: {c['delta']!r}"
    await sd.feed(".9亿:CNY] 元\n")
    await sd.finalize()
    chunks = bus.by_type("canvas.section_chunk")
    full_chunks = "".join(c["delta"] for c in chunks)
    done_md = ""
    for d in bus.by_type("canvas.section_done"):
        done_md += (d["payload"] or {}).get("markdown", "")
    assert "[num:13.9亿:CNY]" in full_chunks + done_md


@pytest.mark.unit
@pytest.mark.asyncio
async def test_marker_overflow_flushes_as_plain() -> None:
    sd, bus = _make()
    await sd.feed("## A\n\n[num:" + "a" * 300)
    await sd.finalize()
    chunks = bus.by_type("canvas.section_chunk")
    full = "".join(c["delta"] for c in chunks)
    done_md = ""
    for d in bus.by_type("canvas.section_done"):
        done_md += (d["payload"] or {}).get("markdown", "")
    combined = full + done_md
    assert "[num:" in combined
    assert "a" * 100 in combined


# ─── Marker state reset across sections (P1-4 fix) ──────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_marker_state_reset_on_section_close() -> None:
    """Half marker in section A → close A → fresh marker scan in section B."""
    sd, bus = _make()
    # Section A has unclosed marker, then forced close by ## B
    await sd.feed("## A\n\n[num:13" + "x" * 50)  # unclosed marker
    await sd.feed("\n\n## B\n\n[num:99]\n")
    await sd.finalize()
    # Both sections should have done events; marker overflow should NOT prevent B
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 2


# ─── Buffer cap (P1-10) ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_truncation_at_cap() -> None:
    """LLM emits 1MB → truncated to 256KB cap, no crash."""
    sd, bus = _make()
    huge = "## H\n\n" + ("x" * (300 * 1024))  # 300KB > 256KB cap
    await sd.feed(huge)
    await sd.finalize()
    # No exception. Section done emitted.
    dones = bus.by_type("canvas.section_done")
    assert dones  # got at least one


# ─── Section_id format (P0-C) ───────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_section_ids_pinned_to_turn_id() -> None:
    sd, bus = _make(turn_id="t_abc12345")
    await sd.feed("## A\n\n.\n## B\n\n.\n")
    await sd.finalize()
    streamings = bus.by_type("canvas.section_streaming")
    for s in streamings:
        assert s["section_id"].startswith("sec_lead_t_abc12345_")


# ─── Char-by-char drip ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_char_by_char_drip_equivalent_to_one_shot() -> None:
    md = "## H1\n\n内容 A\n\n## H2\n\n内容 B\n"

    sd1, bus1 = _make()
    await sd1.feed(md)
    await sd1.finalize()

    sd2, bus2 = _make()
    for ch in md:
        await sd2.feed(ch)
    await sd2.finalize()

    assert bus1.kinds("canvas.section_done").count("section") == bus2.kinds("canvas.section_done").count("section") == 2
    assert bus1.kinds("canvas.section_done").count("narrative") == bus2.kinds("canvas.section_done").count("narrative") == 2


# ─── Line-start invariant ───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inline_hash_not_treated_as_heading() -> None:
    """`example: ## CSS class` mid-narrative is NOT a section boundary."""
    sd, bus = _make()
    await sd.feed("## H\n\n这里写了 example: ## CSS class 用法。\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 1


# ─── Callout detection ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_callout_detected_from_blockquote_lead() -> None:
    sd, bus = _make()
    await sd.feed("> 这是引用 callout 段。\n")
    await sd.finalize()
    dones = bus.by_type("canvas.section_done")
    assert any(d["kind"] == "callout" for d in dones), f"no callout detected in {[d['kind'] for d in dones]}"


# ─── PRD r3 §4.2.3.1 12-seed grammar coverage (subset, P1-14) ───────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_escape_hash_literal() -> None:
    """`\\#` literal hash: not a heading. (Markdown escape.) — body literal."""
    sd, bus = _make()
    # Backslash at line start escapes the # — but our minimal parser doesn't
    # handle escape; this becomes implicit narrative without splitting.
    # We assert: no section heading is emitted.
    await sd.feed("\\# not a heading\n后续\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert section_dones == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_emphasis_cross_chunk() -> None:
    """`*emph*` spanning chunks → renders fine; doesn't break section detection."""
    sd, bus = _make()
    await sd.feed("## H\n\n这是 *emp")
    await sd.feed("hasis* text\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_strong_cross_chunk() -> None:
    sd, bus = _make()
    await sd.feed("## H\n\n这是 **str")
    await sd.feed("ong** text\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_link_cross_chunk() -> None:
    """[label](url) span chunks — should land in narrative payload."""
    sd, bus = _make()
    await sd.feed("## H\n\n点击 [文档")
    await sd.feed("](https://example.com) 链接\n")
    await sd.finalize()
    narratives = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "narrative"]
    assert len(narratives) == 1
    assert "https://example.com" in narratives[0]["payload"]["markdown"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_table_in_narrative() -> None:
    """Markdown table inside narrative → preserved in markdown payload (no
    separate data_table emit; tables come from invocations path)."""
    sd, bus = _make()
    await sd.feed("## 数据\n\n| Col | Val |\n|-----|-----|\n| a | 1 |\n")
    await sd.finalize()
    narratives = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "narrative"]
    assert any("| Col |" in n["payload"]["markdown"] for n in narratives)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_trailing_partial_heading_then_complete() -> None:
    """Trailing `\\n##` mid-stream (no body) then chunk continues `## H`."""
    sd, bus = _make()
    await sd.feed("内容\n##")
    await sd.feed(" Heading\n体\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 1
    assert section_dones[0]["payload"]["h2"] == "Heading"


# ─── abort_open_section flow (Phase B r2 P0-CTO-1 fix) ──────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_abort_open_section_emits_partial_done() -> None:
    """abort_open_section closes current narrative as is_partial=True."""
    sd, bus = _make()
    # A complete line opens the implicit narrative section
    await sd.feed("Let me check the data...\n")
    # Mid-stream: LLM decides to call a tool. Caller signals abort.
    await sd.abort_open_section(reason="tool_call_after_stream")
    dones = bus.by_type("canvas.section_done")
    assert len(dones) >= 1
    last = dones[-1]
    assert last.get("is_partial") is True
    assert last.get("abort_reason") == "tool_call_after_stream"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_abort_open_section_noop_when_no_open_section() -> None:
    """abort_open_section with no current section is no-op."""
    sd, bus = _make()
    await sd.abort_open_section()
    assert bus.events == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_invocations_rebuilds_value_to_source() -> None:
    """update_invocations replaces the cumulative map."""
    sd, _bus = _make()
    # Empty initial
    assert sd._value_to_source == {}
    # Build a fake invocation via the existing answer_decomposer path is heavy;
    # test that the method runs without error using empty list (rebuilds).
    sd.update_invocations([])
    assert sd._value_to_source == {}


# ─── Sprint π Phase B §H — streaming abort dedup ─────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_abort_advances_offset_to_full_md_len() -> None:
    """π H §1: after abort, `_last_emit_pos` / `_last_scanned_pos` advance to
    `len(_full_md)` so subsequent feed() doesn't re-emit aborted prose."""
    sd, bus = _make()
    # Feed a chunk WITHOUT a trailing newline so scan stops at incomplete
    # line — this leaves `_last_scanned_pos` BEFORE the abort marker (so we
    # can verify the abort path explicitly advances it, not just feed()).
    await sd.feed("alpha\n" + ("x" * 93) + "z")  # 100 chars, no trailing \n
    pre_abort_full_md_len = len(sd._full_md)
    assert pre_abort_full_md_len == 100
    # Scan stopped at the last newline (pos 6); _last_scanned_pos == 6 since
    # remainder has no newline yet.
    assert sd._last_scanned_pos == 6, f"scan should pause at first newline, got {sd._last_scanned_pos}"

    await sd.abort_open_section()

    # After abort: both offsets == _full_md len (100). _full_md unchanged.
    assert sd._last_emit_pos == 100, f"_last_emit_pos should advance to 100, got {sd._last_emit_pos}"
    assert sd._last_scanned_pos == 100, f"_last_scanned_pos should advance to 100, got {sd._last_scanned_pos}"
    assert len(sd._full_md) == 100

    # 0.27.1 chunk-batch: capture section_id of the aborted section.
    aborted_section_id = None
    for d in bus.by_type("canvas.section_done"):
        if d.get("is_partial"):
            aborted_section_id = d["section_id"]
            break

    # Second feed appends 50 more chars. New section opens; its chunks must
    # NOT contain any 'x' bytes (the aborted prose).
    await sd.feed("beta\n" + ("y" * 45))  # 50 chars total, opens new section
    await sd.finalize()

    # Filter chunks by section_id: only the NEW (non-aborted) section's
    # chunks must be free of 'x'. (Pre-0.27.1 token-emit relied on chunk
    # count snapshot timing; with debounce, aborted-section chunks are
    # force-flushed during abort itself, so snapshot snapshot is unreliable.)
    new_section_chunks = [c for c in bus.by_type("canvas.section_chunk") if c.get("section_id") != aborted_section_id]
    new_delta = "".join(c["delta"] for c in new_section_chunks)
    assert "x" not in new_delta, f"aborted prose re-emitted in post-abort section: {new_delta!r}"
    # The new content (beta + y) IS present in the new section.
    assert "beta" in new_delta or any("beta" in (d.get("payload") or {}).get("markdown", "") for d in bus.by_type("canvas.section_done"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_abort_no_drop() -> None:
    """π H §2: multi-abort cycle — feed → abort → feed → abort → feed →
    finalize keeps every post-abort segment exactly once (no dup, no drop)."""
    sd, bus = _make()
    # Use sentinel chars that do NOT appear in any other segment's prose:
    # 'a' (only in segment 1), 'b' (only in segment 2), 'k' (only in segment 3).
    # Prose words avoid these letters to make per-segment counts unambiguous.
    await sd.feed("ONE\n" + ("a" * 96))  # 100 chars total
    assert len(sd._full_md) == 100
    await sd.abort_open_section()
    assert sd._last_emit_pos == 100

    await sd.feed("TWO\n" + ("b" * 46))  # 50 chars (total 150)
    assert len(sd._full_md) == 150
    await sd.abort_open_section()
    assert sd._last_emit_pos == 150

    await sd.feed("THREE\n" + ("k" * 44))  # 50 chars (total 200)
    await sd.finalize()

    # Collect all chunks + final done-payload markdown from sections that were
    # NOT aborted (is_partial=True dones carry empty payload by design).
    chunks = bus.by_type("canvas.section_chunk")
    full = "".join(c["delta"] for c in chunks)
    done_md = ""
    for d in bus.by_type("canvas.section_done"):
        if d.get("is_partial"):
            continue
        done_md += (d.get("payload") or {}).get("markdown", "")

    # Invariant: third segment present (no drop)
    assert "kkk" in full + done_md, "third (post-abort-2) segment lost"

    # Aborted segments' chunk bytes ARE emitted live (that's correct — the
    # frontend marks them cancelled via is_partial=True). The dedup invariant
    # is: post-abort feeds must NOT *re-emit* aborted-segment bytes. Test
    # this by counting each segment's unique sentinel.
    a_in_chunks, a_in_done = full.count("a"), done_md.count("a")
    b_in_chunks, b_in_done = full.count("b"), done_md.count("b")
    k_in_chunks, k_in_done = full.count("k"), done_md.count("k")

    # 'a' fed 96 times. Aborted segment never lands in any done payload (its
    # done was is_partial). So 'a' appears in chunks once (96) and done 0.
    assert a_in_chunks == 96, f"'a' chunks should be 96 (live stream), got {a_in_chunks}"
    assert a_in_done == 0, f"'a' in non-partial done should be 0, got {a_in_done}"

    # 'b' fed 46 times. Same as 'a' — aborted, only in chunks.
    assert b_in_chunks == 46, f"'b' chunks should be 46, got {b_in_chunks}"
    assert b_in_done == 0, f"'b' in non-partial done should be 0, got {b_in_done}"

    # 'k' fed 44 times. Third section is the survivor — its content goes
    # through chunks (live) AND into the section_done payload (final body).
    # Expected: 44 in chunks + 44 in done = 88 total.
    assert k_in_chunks == 44, f"'k' chunks should be 44, got {k_in_chunks}"
    assert k_in_done == 44, f"'k' in third done should be 44, got {k_in_done}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cumulative_cap_after_abort() -> None:
    """π H §3 (H-CapReal): real cap is 256 KB. Feed 250 KB → abort → feed
    10 KB. The 10 KB post-abort segment streams cleanly; the abort doesn't
    falsely 'reset' the cumulative cap (cap = bytes ever appended)."""
    from bowhead_core.canvas_composers.streaming import _MAX_CONTENT_LEN

    assert _MAX_CONTENT_LEN == 256 * 1024

    sd, bus = _make()
    # 250_000 chars (well under 256 KB cap). One implicit narrative section.
    big = "start\n" + ("x" * (250_000 - 6))
    await sd.feed(big)
    assert len(sd._full_md) == 250_000
    assert not sd._content_truncated

    await sd.abort_open_section()
    assert sd._last_emit_pos == 250_000

    # 0.27.1 chunk-batch: capture aborted section_id (chunks for it must NOT
    # appear in the post-abort section's chunks).
    aborted_section_id = None
    for d in bus.by_type("canvas.section_done"):
        if d.get("is_partial"):
            aborted_section_id = d["section_id"]
            break

    # 10 KB more — total is 260_000, still under 256 KB cap (262_144). The
    # post-abort segment streams without re-emitting the pre-abort 250_000
    # 'x' bytes. Use 'go\n' (no x/y) as the line prefix to avoid spurious
    # letter collisions.
    await sd.feed("go\n" + ("y" * (10_000 - 3)))
    await sd.finalize()

    # Capacity tracking: cap is reached only if 250_000 + 10_000 > 262_144 (it
    # is — 260_000 < 262_144 actually, so cap NOT triggered). Confirm.
    assert len(sd._full_md) == 260_000
    assert not sd._content_truncated

    # Filter by section_id: post-abort section MUST NOT contain pre-abort x's.
    new_section_chunks = [c for c in bus.by_type("canvas.section_chunk") if c.get("section_id") != aborted_section_id]
    new_delta = "".join(c["delta"] for c in new_section_chunks)
    assert "x" not in new_delta, f"aborted 'x' content leaked into post-abort section ({new_delta.count('x')} 'x' chars in {len(new_delta)} bytes)"
    # Real content present
    y_total_in_new = new_delta.count("y")
    done_md = ""
    for d in bus.by_type("canvas.section_done"):
        if d.get("is_partial"):
            continue
        done_md += (d.get("payload") or {}).get("markdown", "")
    y_total_in_done = done_md.count("y")
    # The 9997 'y' chars (`"y" * (10_000 - 3)`) must show up in chunks and
    # in the section_done payload (plain prose lands in both — chunks
    # emit live as bytes flow; section_done payload carries the same body
    # markdown as the final summary). So total = 2 * 9997.
    assert y_total_in_new == 9997, f"'y' chars in post-abort chunks: expected 9997, got {y_total_in_new}"
    assert y_total_in_done == 9997, f"'y' chars in non-partial done: expected 9997, got {y_total_in_done}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_codeblock_reset_on_abort() -> None:
    """π H §4 (CTO P1-5): if `_in_codeblock` is True at abort, the next
    section must NOT inherit the open fence state. Verified via state
    inspection + functional check that a `## H` after abort is detected."""
    sd, bus = _make()
    # Open a codeblock fence inside narrative — fence toggles _in_codeblock=True
    await sd.feed("intro line\n```python\nprint('partial')\n")
    assert sd._in_codeblock is True, "precondition: should be inside codeblock"
    pre_abort_full_md = sd._full_md

    await sd.abort_open_section()

    # Post-abort: fence state cleared.
    assert sd._in_codeblock is False, "_in_codeblock should be reset to False on abort"
    # Synthetic close fence appended (we WERE in codeblock at abort).
    assert sd._full_md != pre_abort_full_md
    assert sd._full_md.endswith("\n```\n")
    # Offsets at new full_md len — next scan starts clean.
    assert sd._last_emit_pos == len(sd._full_md)
    assert sd._last_scanned_pos == len(sd._full_md)

    # Functional: next feed has a `## H` that MUST be detected as heading
    # (would NOT be detected if _in_codeblock remained True — heading scan
    # inside fence is suppressed by line 198-201).
    await sd.feed("## After Abort\n\nbody\n")
    await sd.finalize()
    section_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "section"]
    assert len(section_dones) == 1
    assert section_dones[0]["payload"]["h2"] == "After Abort"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cap_notice_emitted() -> None:
    """π H §5 (Risk P2-1): when cumulative content exceeds `_MAX_CONTENT_LEN`,
    a `notice` event is emitted with payload reason=`stream_content_truncated`."""
    from bowhead_core.canvas_composers.streaming import _MAX_CONTENT_LEN

    sd, bus = _make()
    # Single large feed that exceeds the cap by a wide margin.
    over_cap = "## H\n\n" + ("x" * (_MAX_CONTENT_LEN + 10_000))
    await sd.feed(over_cap)
    await sd.finalize()

    notice_events = bus.by_type("notice")
    assert len(notice_events) >= 1, f"expected at least 1 'notice' event on cap hit, got {[t for t, _ in bus.events]}"
    n0 = notice_events[0]
    assert n0.get("reason") == "stream_content_truncated"
    assert n0.get("turn_id") == sd.turn_id
    assert "message" in n0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cap_notice_emitted_once_across_overflow_calls() -> None:
    """Cap notice should not spam — each new feed past cap re-emits notice
    only when content_truncated transitions False→True. Subsequent feeds past
    cap should still be safe (no crash); the precise count is allowed to be
    1 or 2 (first overflow + one no-op chunk emit) but never N=100."""
    from bowhead_core.canvas_composers.streaming import _MAX_CONTENT_LEN

    sd, bus = _make()
    big = "## H\n\n" + ("x" * (_MAX_CONTENT_LEN + 100))
    await sd.feed(big)
    # Many subsequent feeds, each past cap (early-return without notice).
    for _ in range(20):
        await sd.feed("more\n")
    await sd.finalize()
    notice_events = bus.by_type("notice")
    # The transition fires the notice; later feeds early-return without
    # re-emitting. Allow up to 2 (one mid-feed truncation + one early-return).
    assert 1 <= len(notice_events) <= 2, f"expected 1-2 notice events, got {len(notice_events)}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trace_replay_t_bdb53ce1_shape() -> None:
    """π H §6: synthetic fixture mimicking prod trace t_6e855d9b — section_001
    partial prose, abort, section_002 stream. The post-abort section's
    content must NOT contain section_001's prefix."""
    sd, bus = _make(turn_id="t_6e855d9b")
    # Section 001: Lead streams pre-tool prose mid-narrative.
    section_001_prose = "我来查一下营收数据。先看下季度趋势。\n"
    await sd.feed(section_001_prose)
    # Tool_call mid-stream → SubagentRunner calls abort_open_section.
    await sd.abort_open_section(reason="tool_call_after_stream")

    # Section 002: After tool returns, Lead resumes with the actual answer.
    section_002_prose = "## 营收洞察\n\n根据查询结果, 营收同比 +25%。\n"
    await sd.feed(section_002_prose)
    await sd.finalize()

    # Collect non-partial section_done payloads (i.e. section_002 sections).
    real_done_payloads = [d for d in bus.by_type("canvas.section_done") if not d.get("is_partial")]
    # Combine all markdown / h2 from real (non-partial) sections.
    section_002_text = ""
    for d in real_done_payloads:
        p = d.get("payload") or {}
        section_002_text += p.get("markdown", "") + p.get("h2", "") + p.get("text", "")
    # The aborted prefix MUST NOT appear in section_002 content.
    assert "我来查一下营收数据" not in section_002_text, f"section_001 prose leaked into section_002: {section_002_text!r}"
    # And section_002's real content IS present.
    assert "营收洞察" in section_002_text or "营收同比" in section_002_text

    # Also: chunks emitted AFTER abort should not re-stream section_001 text.
    # Find the index of the partial done (the abort marker) and check
    # subsequent chunks.
    partial_done_idx = next(i for i, (et, p) in enumerate(bus.events) if et == "canvas.section_done" and p.get("is_partial"))
    post_abort_chunks = [p["delta"] for et, p in bus.events[partial_done_idx + 1 :] if et == "canvas.section_chunk"]
    post_abort_full = "".join(post_abort_chunks)
    assert "我来查一下营收数据" not in post_abort_full, f"post-abort chunks re-streamed section_001 prose: {post_abort_full!r}"


# ─── 0.27.1 hotfix: chunk debounce (prod t_be5558edae3c 167 chunks) ─────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunk_debounce_batches_token_burst() -> None:
    """Many small feeds within 50ms window → 1 chunk emit (was: N emits)."""
    sd, bus = _make()
    await sd.feed("## H\n\n")  # open narrative via heading
    # Drain section_done from heading + the heading's own force-flush.
    pre_chunks = len(bus.by_type("canvas.section_chunk"))
    # Feed 10 single chars rapid-fire (mimics LLM Chinese 1-token-per-chunk).
    for ch in "营收同比增长百分之二十":
        await sd.feed(ch)
    # Right after feeding, timer is scheduled but hasn't fired — emit count
    # must not have jumped to 10.
    immediate_chunks = len(bus.by_type("canvas.section_chunk"))
    assert immediate_chunks == pre_chunks, f"chunk emitted before debounce window: pre={pre_chunks} now={immediate_chunks}"
    # Wait past the 50ms window for the timer to fire.
    await asyncio.sleep(0.08)
    after_chunks = bus.by_type("canvas.section_chunk")
    new_emits = len(after_chunks) - pre_chunks
    assert new_emits == 1, f"expected 1 batched chunk after debounce, got {new_emits}: {[c['delta'] for c in after_chunks[pre_chunks:]]}"
    # The single chunk's delta concatenates all 10 tokens (may have leading
    # whitespace from the narrative section's opening newline — that's fine).
    assert "营收同比增长百分之二十" in after_chunks[pre_chunks]["delta"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_force_flush_bypasses_debounce_on_finalize() -> None:
    """finalize() must drain pending buffer immediately (no 50ms wait)."""
    sd, bus = _make()
    await sd.feed("## H\n\n")
    pre_chunks = len(bus.by_type("canvas.section_chunk"))
    await sd.feed("结果")  # buffered, timer scheduled
    # Finalize WITHOUT waiting for the timer — force=True path must flush.
    await sd.finalize()
    after_chunks = bus.by_type("canvas.section_chunk")
    new_emits = len(after_chunks) - pre_chunks
    assert new_emits >= 1, f"finalize did not flush pending chunk: new_emits={new_emits}"
    # Combined deltas must include the buffered text.
    combined = "".join(c["delta"] for c in after_chunks[pre_chunks:])
    assert "结果" in combined, f"finalize lost buffered content: combined={combined!r}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_drained_across_section_transitions() -> None:
    """Section transition (## heading mid-stream) must flush prior buf to OLD
    section_id before opening the new section. Avoids cross-section bleed."""
    sd, bus = _make()
    await sd.feed("## A\n\n")  # open narrative A
    # Feed buffered tokens into narrative A — DON'T sleep, leave them pending.
    await sd.feed("内容A")
    # Now feed a new heading WITHOUT sleeping. The heading triggers section
    # close → force-flush path must drain "内容A" to narrative-A's section_id.
    await sd.feed("\n## B\n\n内容B\n")
    await sd.finalize()
    chunks = bus.by_type("canvas.section_chunk")
    # Find the section_done for the narrative under heading A — it must have
    # received "内容A" via a section_chunk under the SAME section_id.
    narrative_dones = [d for d in bus.by_type("canvas.section_done") if d["kind"] == "narrative"]
    assert len(narrative_dones) >= 2, f"expected ≥2 narrative sections (A + B), got {len(narrative_dones)}"
    narrative_a_id = narrative_dones[0]["section_id"]
    narrative_b_id = narrative_dones[1]["section_id"]
    a_chunks = [c for c in chunks if c["section_id"] == narrative_a_id]
    b_chunks = [c for c in chunks if c["section_id"] == narrative_b_id]
    a_text = "".join(c["delta"] for c in a_chunks)
    b_text = "".join(c["delta"] for c in b_chunks)
    assert "内容A" in a_text, f"内容A leaked out of narrative-A's section_id: a={a_text!r} b={b_text!r}"
    assert "内容A" not in b_text, f"内容A bled into narrative-B's section_id: b={b_text!r}"


# ── citation-audit M1 slice 3: streaming strip-only (§2a) ──────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_section_bakes_legacy_markers_gen3_mode() -> None:
    """新模式（默认）：streaming 段是 SSE-only 预览、从不落库——关段时退役标记
    bake 成纯值（单位保留），预览不再发 gen2；[N] 化依赖 streaming 持久化
    follow-up（tech-design §2a, out of scope）。"""
    sd, bus = _make()
    await sd.feed("## H\n\n营收 [num:13.9亿:CNY] 与 [num:5.2:亿元:src:tl_x] 双升。\n\n## B\n\n收尾。\n")
    await sd.finalize()
    done_md = "".join(p.get("payload", {}).get("markdown", "") for p in bus.by_type("canvas.section_done"))
    assert "[num:" not in done_md
    assert "13.9亿元" in done_md  # CNY enum unit mapping + preserved value
    assert "5.2亿元" in done_md  # :src: shape is also baked (preview has no [N] numbering)


# ── canvas-summon: emitted_section_ids must match those actually sent to the frontend ──
# None of these sections persist in a pure chat turn; D6's terminal decision must use this list to hide them individually
# (otherwise frontend reveal at done flashes them; see docs/sprints/canvas-summon-2026-08-03/).


@pytest.mark.asyncio
async def test_emitted_section_ids_matches_streamed_ids() -> None:
    sd, bus = _make(turn_id="t_ids")
    await sd.feed("# 标题\n\n正文一段。\n")
    await sd.finalize()

    streamed = [p["section_id"] for p in bus.by_type("canvas.section_streaming")]
    assert streamed, "用例本身没流出段就测不到东西"
    assert sd.emitted_section_ids == streamed
    assert all(sid.startswith("sec_lead_t_ids_") for sid in sd.emitted_section_ids)


@pytest.mark.asyncio
async def test_emitted_section_ids_includes_aborted_section() -> None:
    """abort 掉的段也流给过前端 → 也必须在名单里。

    _collected_sections 只收完整闭合的段，拿它当名单会漏掉这一条，
    前端那边就会剩一个永远藏不掉的 buffered 段。
    """
    sd, bus = _make(turn_id="t_abort")
    await sd.feed("先说半句话就去调工具了。\n")
    await sd.abort_open_section(reason="tool_call_after_stream")
    await sd.finalize()

    streamed = [p["section_id"] for p in bus.by_type("canvas.section_streaming")]
    assert streamed
    assert sd.emitted_section_ids == streamed


@pytest.mark.asyncio
async def test_emitted_section_ids_is_a_copy() -> None:
    sd, _ = _make(turn_id="t_copy")
    await sd.feed("正文。\n")
    ids = sd.emitted_section_ids
    ids.append("sec_lead_injected")
    assert "sec_lead_injected" not in sd.emitted_section_ids
