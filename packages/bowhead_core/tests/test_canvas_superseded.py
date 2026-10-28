"""report-agent-authorship α v1 (2026-06-24) — collapse-in-place "latest report wins".

Tests the store-layer `superseded` marker (collapse-in-place semantics) + the pure
prior-block computation helper (`canvas_revise.prior_block_ids`).

canvas-update-turn 批 A1（2026-09-03）：旧块改按 `Section.turn_id != 本轮` 判——原 seq
阈值被 `promote_document_chrome` 的整份重编 seq 打瞎（本轮 chrome 排到 seq 1 ⇒ 旧块空集）；
只在本轮 append 了 h1 时折；plan 执行轮不折；本轮 patch 过的段与正文仍引用的图不折。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents.lead_orchestrator import _alpha_fold_prior_blocks
from bowhead_core.canvas_revise import (
    prior_block_ids,
    referenced_figure_ids,
    turn_opened_new_document,
)
from bowhead_core.canvas_store import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


class _CaptureBus:
    """Minimal bus: async emit() captures events; holds the `_alpha_fold_decided` guard."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "w.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


async def _append_turn(store: CanvasStore, cid: str, turn: str, n: int, *, with_h1: bool = True) -> list:
    """一轮 = （可选）一个 h1 + n 段 narrative。批 A1 后折叠只在本轮 append 了 h1 时触发。"""
    out: list = []
    if with_h1:
        out.append(
            await store.append_section(
                canvas_id=cid,
                kind="h1",
                payload={"text": f"{turn} report"},
                by_turn_id=turn,
            )
        )
    for i in range(n):
        out.append(
            await store.append_section(
                canvas_id=cid,
                kind="narrative",
                payload={"markdown": f"{turn}-{i}"},
                by_turn_id=turn,
            )
        )
    return out


# ─── store layer: superseded marker ──────────────────────────────────────────


async def test_mark_sections_superseded_returns_newly_then_idempotent(store: CanvasStore) -> None:
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 3)
    ids = [s.id for s in t1]
    folded = await store.mark_sections_superseded(section_ids=ids, superseded_by_turn="t2")
    assert sorted(folded) == sorted(ids)  # all 3 transitioned 0→1
    # idempotent: re-marking the same ids returns [] (already superseded)
    again = await store.mark_sections_superseded(section_ids=ids, superseded_by_turn="t2")
    assert again == []
    # empty input → []
    assert await store.mark_sections_superseded(section_ids=[]) == []


async def test_superseded_sections_stay_visible_collapse_in_place(store: CanvasStore) -> None:
    # COLLAPSE-IN-PLACE: superseded != hidden_for_chat_mode. list_sections must STILL
    # return superseded sections (FE renders them collapsed, not removed) — this is what
    # keeps the block recoverable + hydration consistent (the review P0 fix).
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 2)
    await _append_turn(store, cv.id, "t2", 2)
    await store.mark_sections_superseded(
        section_ids=[s.id for s in t1],
        superseded_by_turn="t2",
    )
    visible = await store.list_sections(cv.id)  # default include_hidden=False
    assert len(visible) == 6  # ALL 6 (2 × h1 + 4 narrative) still returned, superseded ones included
    assert sum(1 for s in visible if s.superseded) == 3


async def test_to_report_section_surfaces_superseded_only_when_set(store: CanvasStore) -> None:
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 1)
    t2 = await _append_turn(store, cv.id, "t2", 1)
    await store.mark_sections_superseded(section_ids=[t1[0].id], superseded_by_turn="t2")
    rows = {s.id: s for s in await store.list_sections(cv.id)}
    assert rows[t1[0].id].to_report_section().get("superseded") is True
    assert "superseded" not in rows[t2[0].id].to_report_section()  # clean when not set


async def test_superseded_survives_reload_hydration_consistent(store: CanvasStore, tmp_path: Path) -> None:
    # Review P0: a reopened session must hydrate the folded block (with its flag), not
    # drop it. Superseded sections are returned by list_sections, so a fresh store on the
    # same DB sees them + the flag → collapsed-on-reload (no vanish, no data loss).
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 2)
    await _append_turn(store, cv.id, "t2", 1)
    await store.mark_sections_superseded(section_ids=[s.id for s in t1], superseded_by_turn="t2")
    reopened = CanvasStore(tmp_path / "w.db")  # same DB path as the fixture
    rows = await reopened.list_sections(cv.id)
    assert len(rows) == 5  # nothing dropped on reload (2 × h1 + 3 narrative)
    assert sum(1 for s in rows if s.superseded) == 3


# ─── helper: prior-block computation (P1-1 robustness) ────────────────────────


def _sec(sid: str, seq: int, turn: str, superseded: bool = False, kind: str = "narrative", markdown: str = ""):
    return SimpleNamespace(id=sid, seq=seq, turn_id=turn, superseded=superseded, kind=kind, payload={"markdown": markdown})


def test_prior_block_ids_by_turn_survives_chrome_reorder() -> None:
    # promote_document_chrome moves current chrome to seq 1-2 and renumbers whole document;
    # old seq criterion finds no old blocks here, while turn-based criterion still folds all prior-turn sections.
    visible = [
        _sec("t2_h1", 1, "t2", kind="h1"),
        _sec("t2_kpi", 2, "t2", kind="kpi_row"),
        _sec("t1_h1", 3, "t1", kind="h1"),
        _sec("t1_body", 4, "t1"),
        _sec("t2_body", 5, "t2"),
    ]
    assert prior_block_ids(visible, this_turn_id="t2") == ["t1_h1", "t1_body"]


def test_prior_block_ids_skips_already_superseded_and_excluded() -> None:
    visible = [_sec("a", 1, "t1", superseded=True), _sec("b", 2, "t1"), _sec("c", 3, "t1"), _sec("d", 4, "t2")]
    assert prior_block_ids(visible, this_turn_id="t2", exclude_ids={"c"}) == ["b"]


def test_prior_block_ids_empty_when_first_report() -> None:
    visible = [_sec("a", 1, "t1"), _sec("b", 2, "t1")]
    assert prior_block_ids(visible, this_turn_id="t1") == []


def test_turn_opened_new_document_and_referenced_figures() -> None:
    charts_only = [_sec("c1", 1, "t2", kind="chart"), _sec("code", 2, "t2", kind="code_block")]
    assert turn_opened_new_document(charts_only) is False
    with_h1 = [*charts_only, _sec("h", 3, "t2", kind="h1")]
    assert turn_opened_new_document(with_h1) is True
    body = _sec("n", 4, "t2", markdown="见 [figure:sec_old_chart] 与 [figure:sec_new]")
    assert referenced_figure_ids([body, *charts_only]) == {"sec_old_chart", "sec_new"}


# ─── seam helper: _alpha_fold_prior_blocks (flag gating, fold, emit, idempotency) ─


async def test_fold_helper_noop_when_flag_off(store: CanvasStore, monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_CANVAS_LATEST_WINS", raising=False)  # default OFF
    cv = await store.get_canvas_by_session("s1")
    await _append_turn(store, cv.id, "t1", 2)
    t2 = await _append_turn(store, cv.id, "t2", 1)
    bus = _CaptureBus()
    folded = await _alpha_fold_prior_blocks(
        store=store,
        bus=bus,
        turn_id="t2",
        this_turn_sections=t2,
    )
    assert folded == []
    assert bus.events == []  # no SSE emit when flag off
    assert all(not s.superseded for s in await store.list_sections(cv.id))  # nothing folded


async def test_fold_helper_folds_prior_and_emits(store: CanvasStore, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 3)
    t2 = await _append_turn(store, cv.id, "t2", 2)
    bus = _CaptureBus()
    folded = await _alpha_fold_prior_blocks(
        store=store,
        bus=bus,
        turn_id="t2",
        this_turn_sections=t2,
    )
    assert sorted(folded) == sorted(s.id for s in t1)  # exactly turn-1's block folded
    # SSE event emitted with the folded ids
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev.event_type == "canvas.sections_superseded"
    assert sorted(ev.payload["superseded_section_ids"]) == sorted(s.id for s in t1)
    assert ev.payload["superseded_by_turn"] == "t2"
    # DB: turn-1 superseded, turn-2 not; all still visible (collapse-in-place)
    rows = {s.id: s for s in await store.list_sections(cv.id)}
    assert all(rows[s.id].superseded for s in t1)
    assert all(not rows[s.id].superseded for s in t2)
    assert len(rows) == 7


async def test_fold_helper_idempotent_via_bus_guard(store: CanvasStore, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv = await store.get_canvas_by_session("s1")
    await _append_turn(store, cv.id, "t1", 2)
    t2 = await _append_turn(store, cv.id, "t2", 1)
    bus = _CaptureBus()
    first = await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t2", this_turn_sections=t2)
    assert len(first) == 3 and len(bus.events) == 1  # t1 = h1 + 2 narrative
    # same bus (guard set) → second call no-ops, no second emit (P1-2)
    second = await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t2", this_turn_sections=t2)
    assert second == []
    assert len(bus.events) == 1


async def test_fold_helper_first_report_no_prior(store: CanvasStore, monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 2)  # the only (first) report
    bus = _CaptureBus()
    folded = await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t1", this_turn_sections=t1)
    assert folded == []
    assert bus.events == []  # nothing below the first report → no fold, no emit


# ─── canvas-update-turn batch A1: four new gates ────────────────


async def test_fold_helper_survives_chrome_reorder(store: CanvasStore, monkeypatch) -> None:
    """promote_document_chrome 把本轮 chrome 提到最前并整份重编 seq 后，旧块仍全折。"""
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 2)
    t2 = await _append_turn(store, cv.id, "t2", 1)
    # Simulate chrome promotion: current h1 first, whole document resequenced 1..N.
    await store.reorder_sections(canvas_id=cv.id, ordered_ids=[t2[0].id], by_turn_id="t2")
    t2_fresh = [s for s in await store.list_sections(cv.id) if s.turn_id == "t2"]
    assert min(s.seq for s in t2_fresh) == 1  # Old seq criterion gives empty set here.
    bus = _CaptureBus()
    folded = await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t2", this_turn_sections=t2_fresh)
    assert sorted(folded) == sorted(s.id for s in t1)


async def test_fold_helper_skips_turn_without_new_document(store: CanvasStore, monkeypatch) -> None:
    """只加了图/代码块（没 append h1）的续轮不折整份旧报告（spike S5b 形状）。"""
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv = await store.get_canvas_by_session("s1")
    await _append_turn(store, cv.id, "t1", 2)
    chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}}, by_turn_id="t2")
    bus = _CaptureBus()
    folded = await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t2", this_turn_sections=[chart])
    assert folded == [] and bus.events == []
    assert all(not s.superseded for s in await store.list_sections(cv.id))
    assert not getattr(bus, "_alpha_fold_decided", False)  # Undecided, may decide later this turn.


async def test_fold_helper_skips_plan_execution_turn(store: CanvasStore, monkeypatch) -> None:
    """plan 中断/重试是新 turn：按 turn 判会把前轮各章全收掉，plan 轮一律不走 α 折叠。"""
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv = await store.get_canvas_by_session("s1")
    await _append_turn(store, cv.id, "t1", 2)  # Prior-turn chapter.
    t2 = await _append_turn(store, cv.id, "t2", 1)  # Rerun chapter (has h1).
    bus = _CaptureBus()
    bus._d6_is_plan_execution = True
    folded = await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t2", this_turn_sections=t2)
    assert folded == [] and bus.events == []
    assert all(not s.superseded for s in await store.list_sections(cv.id))


async def test_fold_helper_keeps_referenced_figure_and_touched_sections(store: CanvasStore, monkeypatch) -> None:
    """本轮正文仍引用的上一轮图、本轮 patch 过的上一轮段，都不折；其余旧块照折。"""
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv = await store.get_canvas_by_session("s1")
    t1 = await _append_turn(store, cv.id, "t1", 1)
    old_chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    old_body = t1[1]
    # Current turn patches prior section (batch B0 revision shape; directly persists caller=None here).
    await store.patch_section(section_id=old_body.id, patch={"markdown": "revised"}, expected_rev=1, by_turn_id="t2", caller=None)
    h1 = await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "t2"}, by_turn_id="t2")
    body = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": f"仍引用 [figure:{old_chart.id}]"},
        by_turn_id="t2",
    )
    bus = _CaptureBus()
    folded = await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t2", this_turn_sections=[h1, body])
    assert folded == [t1[0].id]  # Only prior h1 folded.
    rows = {s.id: s for s in await store.list_sections(cv.id)}
    assert not rows[old_chart.id].superseded and not rows[old_body.id].superseded
