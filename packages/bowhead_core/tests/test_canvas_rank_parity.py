"""A3 (report-edit-audit) — canonical rank 的双端对拍 + reorder 补全重编.

seq 权威化的前提是"服务端写的序 == 前端认的序"。这里把前端
`report-view.tsx preprocessSections` 的 rank 表**从源码里抠出来**逐条比对 ——
两边分叉就是 P1-1 的病根（页面一套序、库/导出另一套序）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bowhead_core.canvas_rank import canonical_order, canonical_rank, is_canonically_ordered

from bowhead_core import CanvasStore

_FE_REPORT_VIEW = Path(__file__).resolve().parents[2] / "bowhead_frontend" / "components" / "canvas" / "report-view.tsx"


def _fe_rank_table() -> dict[str, int]:
    """从 FE 源码抽 `if (k === "x") return N;` 与 `k === "a" || k === "b"` 形态。"""
    src = _FE_REPORT_VIEW.read_text(encoding="utf-8")
    body = src.split("const rank = (s: ReportSection): number => {", 1)[1].split("};", 1)[0]
    table: dict[str, int] = {}
    for line in body.splitlines():
        rank_match = re.search(r"return\s+(\d+)", line)
        if not rank_match:
            continue
        kinds = re.findall(r'k === "([a-z0-9_]+)"', line)
        for kind in kinds:
            table[kind] = int(rank_match.group(1))
    return table


def _fe_default_rank() -> int:
    src = _FE_REPORT_VIEW.read_text(encoding="utf-8")
    body = src.split("const rank = (s: ReportSection): number => {", 1)[1].split("};", 1)[0]
    tail = [line for line in body.splitlines() if re.fullmatch(r"\s*return\s+\d+;\s*", line)]
    assert tail, "FE rank 的默认分支没找到"
    return int(re.search(r"\d+", tail[-1]).group(0))


def test_fe_rank_table_is_parseable() -> None:
    table = _fe_rank_table()
    # Must extract at least four headers and two appendices; otherwise parsing failed (frontend shape changed) and this test is meaningless.
    assert {"eyebrow", "h1", "meta", "kpi_row", "code_block", "sql_block"} <= set(table)


def test_backend_rank_matches_frontend_for_every_declared_kind() -> None:
    for kind, fe_rank in _fe_rank_table().items():
        assert canonical_rank(kind) == fe_rank, f"kind={kind} 双端 rank 分叉"


def test_backend_default_rank_matches_frontend_default() -> None:
    assert canonical_rank("narrative") == _fe_default_rank()
    assert canonical_rank("chart") == _fe_default_rank()
    assert canonical_rank("some_future_kind") == _fe_default_rank()


def test_canonical_order_is_stable_within_same_rank() -> None:
    class _S:
        def __init__(self, sid: str, kind: str) -> None:
            self.id, self.kind = sid, kind

    sections = [
        _S("s_code", "code_block"),
        _S("s_chart", "chart"),
        _S("s_eyebrow", "eyebrow"),
        _S("s_h1", "h1"),
        _S("s_meta", "meta"),
        _S("s_kpi", "kpi_row"),
        _S("s_nar1", "narrative"),
        _S("s_nar2", "narrative"),
        _S("s_sources", "sources"),
    ]
    assert canonical_order(sections) == [
        "s_eyebrow",
        "s_h1",
        "s_meta",
        "s_kpi",
        # Body rank preserves relative input order: chart precedes both narratives, retaining weave order.
        "s_chart",
        "s_nar1",
        "s_nar2",
        "s_sources",
        "s_code",
    ]
    assert is_canonically_ordered([sections[2], sections[3], sections[6]]) is True
    assert is_canonically_ordered([sections[0], sections[2]]) is False


def test_canonical_order_multi_report_segments_do_not_interleave() -> None:
    """★116 dogfood 1.0.93 P2（F 轮实测两 h1 连排）：plan 多腿轮每腿各出一份
    完整报告——全局 rank 会把所有 h1/kpi_row 各自归堆拆散两腿。第二个 h1 起
    新段（紧邻其前的 eyebrow 随行），段内各自排、段间保持传入序。"""

    class _S:
        def __init__(self, sid: str, kind: str) -> None:
            self.id, self.kind = sid, kind

    legs = [
        _S("a_h1", "h1"),
        _S("a_meta", "meta"),
        _S("a_nar", "narrative"),
        _S("a_kpi", "kpi_row"),  # Sorting still applies within segment: kpi_row precedes a_nar
        _S("b_eyebrow", "eyebrow"),  # Leg 2 eyebrow adjoins its h1 → joins the new segment
        _S("b_h1", "h1"),
        _S("b_kpi", "kpi_row"),
        _S("b_nar", "narrative"),
    ]
    assert canonical_order(legs) == [
        "a_h1",
        "a_meta",
        "a_kpi",
        "a_nar",
        "b_eyebrow",
        "b_h1",
        "b_kpi",
        "b_nar",
    ]
    # Legs already follow leg order → recognized as canonical, avoiding migration no-op work.
    ordered = [legs[0], legs[1], legs[3], legs[2], legs[4], legs[5], legs[6], legs[7]]
    assert is_canonically_ordered(ordered) is True


def test_canonical_order_single_report_unchanged_by_segmentation() -> None:
    """单 h1 画布=单段——晚 append 的 header 仍被拉到最前(原有语义逐字节保留)。"""

    class _S:
        def __init__(self, sid: str, kind: str) -> None:
            self.id, self.kind = sid, kind

    secs = [
        _S("s_chart", "chart"),
        _S("s_code", "code_block"),
        _S("s_h1", "h1"),
        _S("s_kpi", "kpi_row"),
        _S("s_nar", "narrative"),
    ]
    assert canonical_order(secs) == ["s_h1", "s_kpi", "s_chart", "s_nar", "s_code"]


@pytest.fixture
async def store(tmp_path):
    s = CanvasStore(tmp_path / "rank.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_reorder_backfills_unlisted_ids_and_keeps_seq_unique(store) -> None:
    """A3: 部分序（agent 工具会这么发）不得再制造 seq 撞车 —— 未列出的按原相对序补全。"""
    cv = await store.ensure_canvas(session_id="s_r", workspace_id="w", user_id="u")
    ids = [(await store.append_section(canvas_id=cv.id, kind=kind, payload={})).id for kind in ("code_block", "chart", "eyebrow", "h1", "narrative")]
    code, chart, eyebrow, h1, narrative = ids

    # Send only three IDs, simulating canvas_reorder_sections partial order.
    final = await store.reorder_sections(canvas_id=cv.id, ordered_ids=[eyebrow, h1, narrative])

    assert final[:3] == [eyebrow, h1, narrative]
    # Unlisted sections retain relative order (code before chart), appended at the end.
    assert final[3:] == [code, chart]
    stored = [s.id for s in await store.list_sections(cv.id)]
    assert stored == final
    seqs = [s.seq for s in await store.list_sections(cv.id)]
    assert seqs == [1, 2, 3, 4, 5]
    assert len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_reorder_ignores_unknown_ids(store) -> None:
    cv = await store.ensure_canvas(session_id="s_u", workspace_id="w", user_id="u")
    a = await store.append_section(canvas_id=cv.id, kind="narrative", payload={})
    b = await store.append_section(canvas_id=cv.id, kind="chart", payload={})

    final = await store.reorder_sections(canvas_id=cv.id, ordered_ids=[b.id, "sec_not_here", a.id])

    assert final == [b.id, a.id]
    assert [s.seq for s in await store.list_sections(cv.id)] == [1, 2]


@pytest.mark.asyncio
async def test_reorder_full_order_is_exact(store) -> None:
    cv = await store.ensure_canvas(session_id="s_f", workspace_id="w", user_id="u")
    ids = [(await store.append_section(canvas_id=cv.id, kind="narrative", payload={})).id for _ in range(4)]
    target = list(reversed(ids))

    final = await store.reorder_sections(canvas_id=cv.id, ordered_ids=target)

    assert final == target
    assert [s.id for s in await store.list_sections(cv.id)] == target
