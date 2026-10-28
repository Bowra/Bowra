"""RC-8 终稿一致性闸 — report_final_gate + section_ids_touched_by_turn 单测.

覆盖 design acceptance(docs/sprints/kb-optimization/rc8-final-gate-design.md §4):
底座查询(append/patch 计入,reorder/delete 不计) / stale 警示插入(caption 后+
空行隔离最坏排布 / 幂等) / 跨面比对(∀ corroborated 谓词 / canonical 单位归一 /
10× 同单位负向 / delta 面 / [num:] 三代剥离) / 终稿 pass emit-contract
(mock bus 断言 canvas.section_patched + rev bump) / flag off 零行为变化.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents.report_tools import _read_report
from bowhead_core.report_final_gate import (
    STALE_NOTE,
    _unwrap_num_markers,
    build_crossface_footer,
    find_crossface_mismatches,
    insert_stale_figure_notes,
    run_final_consistency_pass,
)
from bowhead_core.report_writer_loop import _evidence_block  # T5: helper moved to writer-loop.

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "t.db")
    await s.initialize()
    return s


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list = []
        self._pi_turn_state: dict = {}

    async def emit(self, ev) -> None:
        self.events.append(ev)


# ── WP-A section_ids_touched_by_turn ─────────────────────────────────────────


async def test_touched_by_turn_counts_append_and_patch(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    s1 = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    s2 = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {}}, by_turn_id="t2")
    # t2 patches t1 chart (like D6 upgrade) → fresh from t2 perspective.
    await store.patch_section(section_id=s1.id, patch={"spec": {"title": "x"}}, by_turn_id="t2")
    assert await store.section_ids_touched_by_turn("t2") == {s1.id, s2.id}
    assert await store.section_ids_touched_by_turn("t1") == {s1.id}
    assert await store.section_ids_touched_by_turn("") == set()


async def test_touched_by_turn_ignores_reorder_and_delete(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    s1 = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    s2 = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    await store.reorder_sections(canvas_id=c.id, ordered_ids=[s2.id, s1.id], by_turn_id="t2")
    await store.delete_section(section_id=s2.id, by_turn_id="t2")
    assert await store.section_ids_touched_by_turn("t2") == set()


# ── WP-B2 insert_stale_figure_notes ──────────────────────────────────────────


def test_stale_note_worst_layout_caption_not_sucked_into_blockquote() -> None:
    """最坏排布(review P1-1):锚行+caption+正文连续无空行。警示必须插在 caption
    之后且前后有空行 —— 否则 CommonMark lazy continuation 把 caption/正文吸进
    blockquote。"""
    md = "[figure:sec_a]\n**图1**:存款结构\n后续正文继续。"
    out, n = insert_stale_figure_notes(md, {"sec_a"})
    assert n == 1
    lines = out.split("\n")
    i = lines.index(STALE_NOTE)
    assert lines[i - 1] == ""  # Blank line before.
    assert lines[i + 1] == ""  # Blank line after.
    assert lines.index("**图1**:存款结构") < i  # Caption before warning, belongs to figure.
    assert "后续正文继续。" in lines[i + 2 :]


def test_stale_note_without_caption() -> None:
    md = "[figure:sec_a]\n后续正文。"
    out, n = insert_stale_figure_notes(md, {"sec_a"})
    assert n == 1
    lines = out.split("\n")
    i = lines.index(STALE_NOTE)
    assert lines[i - 1] == "" and lines[i + 1] == ""
    assert lines[0] == "[figure:sec_a]"


def test_stale_note_idempotent() -> None:
    md = "[figure:sec_a]\n**图1**:x\n正文"
    once, n1 = insert_stale_figure_notes(md, {"sec_a"})
    twice, n2 = insert_stale_figure_notes(once, {"sec_a"})
    assert n1 == 1 and n2 == 0
    assert twice == once


def test_stale_note_fresh_and_inline_refs_untouched() -> None:
    md = "[figure:sec_fresh]\n**图1**:x\n行内提到[figure:sec_a]不是锚行。"
    out, n = insert_stale_figure_notes(md, {"sec_a"})
    assert n == 0
    assert out == md


# ── WP-C [num:] stripping (three formats, review P2-1) ───────────


@pytest.mark.parametrize(
    ("marked", "plain"),
    [
        ("增速[num:17.2:%:src:tl_1]创新高", "增速17.2%创新高"),
        ("[num:4.18%:src:tl_2]", "4.18%"),  # smushed unit + src
        ("[num:12.3:万元]", "12.3万元"),
        ("[num:8:%:src:tl_3#highlight=h1]", "8%"),  # Highlight tail.
        ("[num:42]", "42"),
    ],
)
def test_unwrap_num_markers(marked: str, plain: str) -> None:
    assert _unwrap_num_markers(marked) == plain


# ── WP-C find_crossface_mismatches ───────────────────────────────────────────

_KPI_GROWTH = [{"lbl": "金总增速", "val": "4.0%"}]


def test_crossface_prod_case_three_faces() -> None:
    """prod s_ba3888d6e73a C2:金总增速 正文 17.2% / KPI 4.0% / 图 caption 2.3%。"""
    mismatches, _ = find_crossface_mismatches(
        _KPI_GROWTH,
        ["本期金总增速达17.2%,创同期新高。"],
        ["金总增速2.3%(2024)"],
    )
    faces = {m["face"] for m in mismatches}
    assert faces == {"narrative", "chart"}
    assert all(m["label"] == "金总增速" for m in mismatches)


def test_crossface_corroborated_comparison_sentence_not_reported() -> None:
    """review P1-2:「由2.3%升至4.0%」行内含 KPI 值 → corroborated 不报。"""
    mismatches, _ = find_crossface_mismatches(
        _KPI_GROWTH,
        ["金总增速由2.3%升至4.0%,持续改善。"],
        [],
    )
    assert mismatches == []


def test_crossface_canonical_scale_variants_ok_but_10x_caught() -> None:
    """review P1-4:声明单位 canonical 归一 —— 34.71亿元 == 347100万元 不报;
    同类 10× 错值(4万元 vs 40万元)必须报(负向门)。"""
    ok, _ = find_crossface_mismatches(
        [{"lbl": "存款总额", "val": "34.71亿元"}],
        ["存款总额达347100万元。"],
        [],
    )
    assert ok == []
    bad, _ = find_crossface_mismatches(
        [{"lbl": "户均存款", "val": "4万元"}],
        ["户均存款约40万元,明显偏低。"],
        [],
    )
    assert len(bad) == 1 and bad[0]["face"] == "narrative"


def test_crossface_delta_field_participates() -> None:
    """review P2-2:增速类指标常在 delta 不在 val —— delta 值集参与 corroborate。"""
    mismatches, _ = find_crossface_mismatches(
        [{"lbl": "存款增速", "val": "258亿元", "delta": "+4.0%"}],
        ["存款增速4.0%,符合预期。"],
        [],
    )
    assert mismatches == []


def test_crossface_unresolved_unit_counted_not_compared() -> None:
    mismatches, unresolved = find_crossface_mismatches(
        [{"lbl": "存款占比", "val": "0.172"}],
        ["存款占比17.2%。"],
        [],
    )
    assert mismatches == []
    assert unresolved == 1


def test_crossface_skips_table_blockquote_lines() -> None:
    mismatches, _ = find_crossface_mismatches(
        _KPI_GROWTH,
        ["| 金总增速 | 9.9% |\n> 金总增速 9.8%\n# 金总增速 9.7%"],
        [],
    )
    assert mismatches == []


def test_crossface_chat_face_labeled() -> None:
    mismatches, _ = find_crossface_mismatches(
        [{"lbl": "存款占比", "val": "48.24%"}],
        [],
        [],
        chat_text="存款占比从43.9%升至51.5%。",
    )
    assert len(mismatches) == 1 and mismatches[0]["face"] == "chat"


def test_crossface_num_marker_wrapped_numbers_compared() -> None:
    """writer-loop 正文数字被 [num:] 包裹后仍参与比对。"""
    mismatches, _ = find_crossface_mismatches(
        _KPI_GROWTH,
        ["金总增速[num:17.2:%:src:tl_1]。"],
        [],
    )
    assert len(mismatches) == 1


def test_crossface_footer_shape_and_cap() -> None:
    ms = [{"label": f"指标{i}", "face": "narrative", "kpi_values": [4.0], "found": [17.2], "line": "x"} for i in range(8)] + [
        {"label": "chat指标", "face": "chat", "kpi_values": [1], "found": [2], "line": "y"}
    ]
    footer = build_crossface_footer(ms)
    assert sum(1 for ln in footer.split("\n") if ln.startswith("> - ")) == 5  # cap 5
    assert "chat指标" not in footer  # Chat always shadow-only.
    assert all(ln.startswith(">") for ln in footer.strip().split("\n"))


# ── Final pass integration (real CanvasStore + mock bus, emit-contract) ──


async def _mk_turn2_fixture(store: CanvasStore, *, ref_old: bool = True):
    """t1 出一张图;t2 写 narrative(引 t1 旧图)——prod stale 场景复刻。"""
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    old_chart = await store.append_section(
        canvas_id=c.id,
        kind="chart",
        payload={"spec": {"title": "金总增速2.3%"}},
        by_turn_id="t1",
    )
    md = f"[figure:{old_chart.id}]\n**图1**:增速走势\n正文继续。" if ref_old else "无图正文。"
    narrative = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": md}, by_turn_id="t2")
    return c, old_chart, narrative


async def test_pass_inserts_stale_note_and_emits_patched(store: CanvasStore) -> None:
    c, old_chart, narrative = await _mk_turn2_fixture(store)
    bus = _CapturingBus()
    turn_sections = await store.list_sections_appended_by_turn("t2")
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)

    fresh = {s.id: s for s in await store.list_sections(c.id, include_hidden=True)}
    assert STALE_NOTE in fresh[narrative.id].payload["markdown"]
    assert fresh[narrative.id].rev == 2
    patched = [e for e in bus.events if getattr(e, "event_type", None) == "canvas.section_patched"]
    assert len(patched) == 1
    assert patched[0].payload["section_id"] == narrative.id
    assert patched[0].payload["rev"] == 2
    assert STALE_NOTE in patched[0].payload["patch"]["markdown"]  # live == persisted.


async def test_pass_fresh_figure_no_note(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    chart = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {"title": "x"}}, by_turn_id="t2")
    await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": f"[figure:{chart.id}]\n正文"}, by_turn_id="t2")
    bus = _CapturingBus()
    turn_sections = await store.list_sections_appended_by_turn("t2")
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)
    assert not bus.events  # New this-turn chart: no note/patch/emit.


async def test_pass_hidden_historical_figure_still_noted(store: CanvasStore) -> None:
    """review P1-3:历史 chat-mode 轮的图可能 hidden —— include_hidden 索引兜住。"""
    c, old_chart, narrative = await _mk_turn2_fixture(store)
    await store.mark_turn_hidden_for_chat_mode("t1", "chat-mode")
    bus = _CapturingBus()
    turn_sections = await store.list_sections_appended_by_turn("t2")
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)
    fresh = {s.id: s for s in await store.list_sections(c.id, include_hidden=True)}
    assert STALE_NOTE in fresh[narrative.id].payload["markdown"]


async def test_pass_flags_off_zero_change(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_STALE_FIGURE_NOTE", "0")
    monkeypatch.setenv("BOWHEAD_CROSSFACE_NUMBERS", "off")
    c, old_chart, narrative = await _mk_turn2_fixture(store)
    bus = _CapturingBus()
    turn_sections = await store.list_sections_appended_by_turn("t2")
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)
    fresh = {s.id: s for s in await store.list_sections(c.id)}
    assert fresh[narrative.id].payload["markdown"] == narrative.payload["markdown"]
    assert fresh[narrative.id].rev == 1
    assert not bus.events


async def test_pass_shadow_default_no_content_change_on_mismatch(store: CanvasStore) -> None:
    """默认 shadow:跨面 mismatch 只观测,正文零变化(无 stale 图时零 patch)。"""
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    await store.append_section(canvas_id=c.id, kind="kpi_row", payload={"cells": [{"lbl": "金总增速", "val": "4.0%"}]}, by_turn_id="t2")
    nar = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "金总增速达17.2%。"}, by_turn_id="t2")
    bus = _CapturingBus()
    turn_sections = await store.list_sections_appended_by_turn("t2")
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)
    fresh = {s.id: s for s in await store.list_sections(c.id)}
    assert fresh[nar.id].payload["markdown"] == "金总增速达17.2%。"
    assert not bus.events


async def test_pass_annotate_appends_footer(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_CROSSFACE_NUMBERS", "annotate")
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    await store.append_section(canvas_id=c.id, kind="kpi_row", payload={"cells": [{"lbl": "金总增速", "val": "4.0%"}]}, by_turn_id="t2")
    nar = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": "金总增速达17.2%。"}, by_turn_id="t2")
    bus = _CapturingBus()
    turn_sections = await store.list_sections_appended_by_turn("t2")
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)
    fresh = {s.id: s for s in await store.list_sections(c.id)}
    assert "一致性提示" in fresh[nar.id].payload["markdown"]
    assert any(getattr(e, "event_type", None) == "canvas.section_patched" for e in bus.events)


async def test_pass_rev_conflict_skips_quietly(store: CanvasStore) -> None:
    """expected_rev CAS(review P2-3):快照后 narrative 被并发 patch → 跳过不炸。"""
    c, old_chart, narrative = await _mk_turn2_fixture(store)
    turn_sections = await store.list_sections_appended_by_turn("t2")
    # Concurrent write after snapshot advances rev; pass expected_rev stale.
    await store.patch_section(section_id=narrative.id, patch={"markdown": "并发改写。"}, by_turn_id="t9")
    bus = _CapturingBus()
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)
    fresh = {s.id: s for s in await store.list_sections(c.id)}
    assert fresh[narrative.id].payload["markdown"] == "并发改写。"  # Never overwrite concurrent write.
    assert not bus.events


async def test_pass_multi_narrative_all_covered(store: CanvasStore) -> None:
    """multi-writer:每 writer 一段 narrative,stale 引用都要标。"""
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    old_chart = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    n1 = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": f"[figure:{old_chart.id}]\nA 段。"}, by_turn_id="t2")
    n2 = await store.append_section(canvas_id=c.id, kind="narrative", payload={"markdown": f"[figure:{old_chart.id}]\nB 段。"}, by_turn_id="t2")
    bus = _CapturingBus()
    turn_sections = await store.list_sections_appended_by_turn("t2")
    await run_final_consistency_pass(store=store, turn_id="t2", turn_sections=turn_sections, bus=bus)
    fresh = {s.id: s for s in await store.list_sections(c.id)}
    assert STALE_NOTE in fresh[n1.id].payload["markdown"]
    assert STALE_NOTE in fresh[n2.id].payload["markdown"]
    patched = [e for e in bus.events if getattr(e, "event_type", None) == "canvas.section_patched"]
    assert len(patched) == 2


# ── Done boundary integration (impl review F5: hook actually calls gate) ──


async def test_done_chokepoint_invokes_final_gate(store: CanvasStore) -> None:
    """驱动真 lead_orchestrator._emit_event(name='done'):断言 done-branch 挂点
    真调用了 run_final_consistency_pass(stale 警示落库 + section_patched 上 bus),
    防挂点重构漂移后功能静默死。"""
    from bowhead_core.agents.lead_orchestrator import _emit_event

    c, old_chart, narrative = await _mk_turn2_fixture(store)
    bus = _CapturingBus()
    bus._done_emitted = False  # type: ignore[attr-defined]
    bus._d6_canvas_store = store  # type: ignore[attr-defined]
    bus._d6_turn_id = "t2"  # type: ignore[attr-defined]
    await _emit_event(bus, "t2", "done", {"status": "completed"})

    fresh = {s.id: s for s in await store.list_sections(c.id, include_hidden=True)}
    assert STALE_NOTE in fresh[narrative.id].payload["markdown"]
    assert any(getattr(e, "event_type", None) == "canvas.section_patched" for e in bus.events)


# ── WP-B1 freshness notes (read_report / _evidence_block) ─────────


async def test_read_report_figures_carry_fresh_flag(store: CanvasStore) -> None:
    c = await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    old_chart = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {"title": "旧"}}, by_turn_id="t1")
    new_chart = await store.append_section(canvas_id=c.id, kind="chart", payload={"spec": {"title": "新"}}, by_turn_id="t2")
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2")
    out = await _read_report({}, ctx)
    figs = {f["id"]: f for f in out["figures"]}
    assert figs[new_chart.id]["fresh"] is True
    assert figs[old_chart.id]["fresh"] is False
    assert "历史图" in figs[old_chart.id]["note"]


def test_evidence_block_tags_stale_figures() -> None:
    # W-2②: historical figure keeps short machine-readable [历史图] + standalone writing-note/do-not-copy line;
    # old full inline 〔历史图:…〕 sentence retired (writer copied as caption into body, blind-judge evidence).
    block = _evidence_block(
        {"title": "T"},
        [
            {"id": "sec_new", "title": "新图", "fresh": True},
            {"id": "sec_old", "title": "旧图", "fresh": False},
        ],
    )
    # Old tagged, new untagged.
    assert "id=sec_old | 旧图 [历史图]" in block
    assert "id=sec_new | 新图" in block
    assert "新图 [历史图]" not in block
    # Explanation on own line, explicitly writing note (do not copy into body).
    assert "写作须知(勿抄进正文)" in block
    # Regression pin: old copyable inline sentence must disappear.
    assert "〔历史图" not in block
