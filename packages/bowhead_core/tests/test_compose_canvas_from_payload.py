"""compose_canvas_from_payload tests (#142 M137-M1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_core.canvas_composer import (
    _build_sections_from_payload,
    compose_canvas_from_payload,
)
from bowhead_core.types import IntentType

_SPIKE_RUN = Path(__file__).resolve().parents[3] / "reports" / "spike-M137" / "run_01.json"


def _good_payload() -> dict:
    return {
        "title": "EA 看板",
        "eyebrow": "合规监控",
        "exec_summary": "整体合规率 11.39%，部门支出 1.85% 是绝对拖累。",
        "kpis": [
            {"label": "合规率", "value": "11.39%", "delta": "vs -73.6pp", "sentiment": "bad"},
            {"label": "总单据数", "value": "84,037", "sentiment": "neutral"},
        ],
        "insights": [
            {
                "title": "部门支出拖累",
                "thesis": "占 90% 单量但合规率 1.85%",
                "evidence": [
                    {"kind": "chart", "chart_section_id": "sec_a", "caption": "对比"},
                    {"kind": "callout", "callout_text": "杠杆 16.5pp", "caption": "测算"},
                ],
                "narrative": "部门支出在两个维度都是结构性问题，合规率和趋势都低。" * 2,
                "impact": {"quantity": 16.5, "unit": "pp", "direction": "+"},
                "confidence": "high",
            },
            {
                "title": "原因高度集中：92.5% 是空值",
                "thesis": "66,224 单无原因",
                "evidence": [{"kind": "chart", "chart_section_id": "sec_b", "caption": "Pareto"}],
                "narrative": "无请购原因字段空值占据不合规单绝对主导。" * 2,
                "confidence": "high",
            },
        ],
        "recommendations": [
            {
                "priority": "P1",
                "action": "为关联方能源费用设计轻量替代流程",
                "owner": "流程治理办",
                "timeline": "1 周内",
            },
            {
                "priority": "P0",
                "action": "OA 必填字段",
                "owner": "IT 系统组",
                "timeline": "48 小时",
                "expected_impact": "解决 66,224 单空值",
            },
        ],
        "caveats": ["仅 4 月单月数据", "命名异称"],
        "sources": [
            {"label": "支出分类", "tool": "bi_fetch_rows", "artifact_id": "art_001"},
        ],
    }


# ─────────────────────────────────────────────────────────────
# section building
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_sections_full_order() -> None:
    sections = _build_sections_from_payload(_good_payload(), IntentType.REPORT)
    kinds = [s["kind"] for s in sections]
    # Expected: eyebrow → h1 → meta → narrative(executive) → kpi_row → insight_card×2 → sources.
    # Recommendations kind and author-written limitations retired (tech-design-document-minimalism §2.2 / §2.3):
    # author writes that content in body. System keeps only guard allegations and missing-plan-leg caveats.
    assert kinds == [
        "eyebrow",
        "h1",
        "meta",
        "narrative",
        "kpi_row",
        "insight_card",
        "insight_card",
        "sources",
    ]


@pytest.mark.unit
def test_build_sections_kpi_cells_have_sentiment() -> None:
    sections = _build_sections_from_payload(_good_payload(), IntentType.REPORT)
    kpi_row = next(s for s in sections if s["kind"] == "kpi_row")
    assert kpi_row["cells"][0]["sentiment"] == "bad"
    assert kpi_row["cells"][0]["delta"] == "vs -73.6pp"


@pytest.mark.unit
def test_insight_card_preserves_chart_section_id() -> None:
    sections = _build_sections_from_payload(_good_payload(), IntentType.REPORT)
    cards = [s for s in sections if s["kind"] == "insight_card"]
    assert cards[0]["evidence"][0]["chart_section_id"] == "sec_a"
    assert cards[0]["evidence"][1]["callout_text"] == "杠杆 16.5pp"
    # Both narrative + thesis in card.
    assert cards[0]["thesis"] == "占 90% 单量但合规率 1.85%"
    assert "结构性问题" in cards[0]["narrative"]


@pytest.mark.unit
def test_insight_card_preserves_data_artifact_id_for_auto_chart() -> None:
    payload = _good_payload()
    payload["insights"][0]["evidence"][0] = {
        "kind": "chart",
        "caption": "营业额月度走势",
        "data_artifact_id": "art_trend001",
    }
    sections = _build_sections_from_payload(payload, IntentType.REPORT)
    cards = [s for s in sections if s["kind"] == "insight_card"]
    assert cards[0]["evidence"][0]["data_artifact_id"] == "art_trend001"


@pytest.mark.unit
def test_insight_card_drops_empty_impact() -> None:
    payload = _good_payload()
    payload["insights"][1].pop("impact", None)
    payload["insights"][1]["impact"] = {"quantity": None, "unit": None}  # All empty.
    sections = _build_sections_from_payload(payload, IntentType.REPORT)
    cards = [s for s in sections if s["kind"] == "insight_card"]
    assert "impact" not in cards[1]


@pytest.mark.unit
def test_only_system_generated_caveats_are_rendered() -> None:
    """★tech-design-document-minimalism §2.3：局限分三类，只渲**系统生成**的两类。

    作者自己写的建模局限交回正文（它是内容）；守卫的溯源指控与 plan 缺腿提示保留——
    这两种是机器判定、作者写不出来、关乎报告诚不诚实。缺腿那条的机器标记渲染时要剥掉。
    """
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX as _FAB
    from bowhead_core.analysis_payload import PLAN_GAP_CAVEAT_PREFIX as _GAP

    payload = _good_payload()
    author_only = _build_sections_from_payload(payload, IntentType.REPORT)
    assert not [s for s in author_only if s.get("style") == "caveats"], "作者自己写的局限不该再渲"

    payload["caveats"] = list(payload.get("caveats") or []) + [
        _GAP + "「compute」这步执行失败，本报告未包含其分析结果。",
        _FAB + "KPI 1",
    ]
    secs = _build_sections_from_payload(payload, IntentType.REPORT)
    cav = [s for s in secs if s["kind"] == "narrative" and s.get("style") == "caveats"]
    md = "\n".join(s["markdown"] for s in cav)
    assert "这步执行失败" in md, "plan 缺腿提示必须还在（FIX-2 / 2026-06-25 生产事故）"
    assert "KPI 1" in md, "守卫的溯源指控必须还在"
    assert _GAP not in md, "机器标记不该渲给用户看"
    assert "命名异称" not in md, "作者自己写的那条不该再渲"


@pytest.mark.unit
def test_no_data_table_or_sql_block_in_payload_path() -> None:
    """关键：payload 路径绝不产 raw data_table / sql_block —— 那是 Data tab 的事."""
    sections = _build_sections_from_payload(_good_payload(), IntentType.REPORT)
    kinds = {s["kind"] for s in sections}
    assert "data_table" not in kinds
    assert "sql_block" not in kinds


@pytest.mark.unit
def test_sources_entry_preserves_inline_data_preview() -> None:
    """citation-hover 看原文: a source carrying a bi_query sample_rows preview must
    survive the sources-section whitelist (which str()-flattens the 4 text fields)
    verbatim, so the popover can render 原文 rows instead of a dead tl_ link."""
    payload = _good_payload()
    payload["sources"] = [
        {
            "label": "bi_query: 2026年1-6月 销售金额",
            "tool": "bi_query",
            "artifact_id": "tl_61adf9f8df39",
            "query": "2026年1-6月 销售金额",
            "preview_columns": ["销售金额"],
            "preview_rows": [{"销售金额": "9608715342.51"}],
            "total_rows": 1,
        }
    ]
    sections = _build_sections_from_payload(payload, IntentType.REPORT)
    src = next(s for s in sections if s["kind"] == "sources")
    entry = src["entries"][0]
    assert entry["preview_columns"] == ["销售金额"]
    assert entry["preview_rows"] == [{"销售金额": "9608715342.51"}]
    assert entry["total_rows"] == 1
    # the text fields are still str()-flattened as before (no regression)
    assert entry["artifact_id"] == "tl_61adf9f8df39"


@pytest.mark.unit
def test_f4_backstop_missing_exec_summary_synthesizes_from_insight_theses() -> None:
    """canvas-pipeline-fix F4: payload 无 exec_summary 但有 insights →
    composer 自动从 insight theses 合成 narrative section, 保证 canvas 永远有
    narrative (不再"全图无 prose").
    """
    payload = _good_payload()
    payload.pop("exec_summary", None)
    sections = _build_sections_from_payload(payload, IntentType.REPORT)
    exec_narratives = [s for s in sections if s["kind"] == "narrative" and s.get("style") == "executive"]
    assert len(exec_narratives) == 1, "F4 backstop 应合成 narrative"
    md = exec_narratives[0]["markdown"]
    assert "关键发现" in md
    # Both insight theses concatenated.
    assert "占 90% 单量但合规率 1.85%" in md
    assert "66,224 单无原因" in md


@pytest.mark.unit
def test_f4_backstop_skips_when_no_exec_summary_no_insights() -> None:
    """payload 既无 exec_summary 也无 insights → backstop 不合成 (没素材)."""
    payload = {"title": "Empty", "kpis": [], "insights": [], "recommendations": []}
    sections = _build_sections_from_payload(payload, IntentType.REPORT)
    exec_narratives = [s for s in sections if s["kind"] == "narrative" and s.get("style") == "executive"]
    assert len(exec_narratives) == 0


@pytest.mark.unit
def test_f4_backstop_skips_when_exec_summary_present() -> None:
    """payload 有 exec_summary → backstop 不干预, 用真 exec_summary."""
    payload = _good_payload()
    sections = _build_sections_from_payload(payload, IntentType.REPORT)
    exec_narratives = [s for s in sections if s["kind"] == "narrative" and s.get("style") == "executive"]
    assert len(exec_narratives) == 1
    # Original exec_summary retained, never replaced by backstop.
    assert "整体合规率 11.39%" in exec_narratives[0]["markdown"]
    assert "关键发现" not in exec_narratives[0]["markdown"]


@pytest.mark.unit
def test_N_T1_backstop_synthesizes_narrative_from_chart_titles() -> None:
    """chat-truth N-T1: peer payload empty (no exec_summary + no insight theses)
    but tool_invocations has add_chart × N → backstop synthesizes
    narrative from chart titles. Realistic prod walkthrough Case A shape.
    """
    from dataclasses import dataclass

    @dataclass
    class _Inv:
        name: str
        arguments: dict

    # Realistic shape: Session 4 walkthrough — chart-heavy peer return
    invs = [
        _Inv(name="add_chart", arguments={"title": "2026年4月 品牌GMV排名"}),
        _Inv(name="add_chart", arguments={"title": "品类GMV同比变化"}),
        _Inv(name="add_chart", arguments={"title": "区域GMV分布"}),
        _Inv(name="bi_query", arguments={"query": "..."}),  # non-chart, ignored
    ]
    # Empty payload — peer did subagent_return(payload={})
    payload = {"insights": [], "kpis": [], "recommendations": []}
    sections = _build_sections_from_payload(
        payload,
        IntentType.REPORT,
        tool_invocations=invs,
    )
    exec_narratives = [s for s in sections if s["kind"] == "narrative" and s.get("style") == "executive"]
    assert len(exec_narratives) == 1, "N backstop 应从 chart titles 合成 narrative"
    md = exec_narratives[0]["markdown"]
    assert "关键发现" in md
    assert "2026年4月 品牌GMV排名" in md
    assert "品类GMV同比变化" in md
    assert "区域GMV分布" in md
    # bi_query must not appear.
    assert "bi_query" not in md


@pytest.mark.unit
def test_N_T2_backstop_no_op_on_empty_payload_and_no_invocations() -> None:
    """chat-truth N-T2: payload + tool_invocations 都 empty → backstop 无 crash,
    返 sections 不含 executive narrative.
    """
    payload: dict = {}
    sections = _build_sections_from_payload(
        payload,
        None,
        tool_invocations=None,
    )
    exec_narratives = [s for s in sections if s.get("kind") == "narrative" and s.get("style") == "executive"]
    assert exec_narratives == []
    # Also test empty list.
    sections2 = _build_sections_from_payload(payload, None, tool_invocations=[])
    exec_narratives2 = [s for s in sections2 if s.get("kind") == "narrative" and s.get("style") == "executive"]
    assert exec_narratives2 == []


@pytest.mark.unit
def test_minimal_payload_optional_fields_skipped() -> None:
    minimal = {
        "title": "X",
        "exec_summary": "结论。",
        "kpis": [],
        "insights": [],
        "recommendations": [],
    }
    sections = _build_sections_from_payload(minimal, None)
    kinds = [s["kind"] for s in sections]
    # Omit eyebrow / meta / kpi_row / insight_card / recommendations / caveats / sources.
    assert "eyebrow" not in kinds
    assert "meta" not in kinds
    assert "kpi_row" not in kinds
    assert "insight_card" not in kinds
    assert "recommendations" not in kinds
    assert "sources" not in kinds
    # At least h1 + executive narrative.
    assert "h1" in kinds
    assert any(s["kind"] == "narrative" and s.get("style") == "executive" for s in sections)


# ─────────────────────────────────────────────────────────────
# integration with spike output
# ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _SPIKE_RUN.exists(), reason="spike output missing")
@pytest.mark.unit
def test_spike_run_01_payload_produces_clean_canvas() -> None:
    """跑真 Qwen 输出过 _build_sections_from_payload，看产出干净不干净."""
    raw = json.loads(_SPIKE_RUN.read_text(encoding="utf-8"))["parsed"]
    sections = _build_sections_from_payload(raw, IntentType.REPORT)
    kinds = [s["kind"] for s in sections]
    # No raw debug sections.
    assert "data_table" not in kinds
    assert "sql_block" not in kinds
    # insight_card count = insights count (actual spike: 3).
    insight_cards = [s for s in sections if s["kind"] == "insight_card"]
    assert len(insight_cards) == len(raw["insights"])
    # Total sections ≤25 (M137 KPI #137-2).
    assert len(sections) <= 25, f"section count={len(sections)} > 25"
    # Each insight_card has ≥1 chart evidence.
    for card in insight_cards:
        chart_evs = [e for e in card["evidence"] if e.get("chart_section_id")]
        assert len(chart_evs) >= 1, f"insight {card['title']!r} 没有 chart evidence"


# ─────────────────────────────────────────────────────────────
# end-to-end with stub canvas_store + bus
# ─────────────────────────────────────────────────────────────


class _StubBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, ev) -> None:
        self.events.append((ev.event_type, ev.payload))


class _StubSection:
    def __init__(self, sid: str, seq: int, kind: str, payload: dict, turn_id: str | None = None, owner: str | None = None) -> None:
        self.id = sid
        self.seq = seq
        self.kind = kind
        self.payload = payload
        self.turn_id = turn_id
        self.rev = 1
        self.superseded = False
        self.owner = owner

    def to_report_section(self) -> dict:
        return {"id": self.id, "kind": self.kind, **self.payload}


class _StubCanvas:
    def __init__(self) -> None:
        self.id = "cv_test"
        self.session_id = "s_test"
        self.title = None


class _StubCanvasStore:
    def __init__(self) -> None:
        self._appended: list[_StubSection] = []
        self._seq = 0

    async def ensure_canvas(self, session_id: str, workspace_id: str, user_id: str):
        return _StubCanvas()

    async def append_section(self, *, canvas_id: str, kind: str, payload: dict, by_turn_id: str, owner: str | None = None):
        self._seq += 1
        sid = f"sec_{self._seq:03d}"
        sec = _StubSection(sid, self._seq, kind, payload, turn_id=by_turn_id, owner=owner)
        self._appended.append(sec)
        return sec

    async def list_sections(self, canvas_id: str):
        return list(self._appended)

    async def reorder_sections(self, *, canvas_id: str, ordered_ids: list, by_turn_id: str | None = None):
        pos = {sid: i for i, sid in enumerate(ordered_ids)}
        self._appended.sort(key=lambda s: (pos.get(s.id, 10**6), s.seq))
        for i, sec in enumerate(self._appended, 1):
            sec.seq = i
        return [s.id for s in self._appended]

    async def mark_sections_superseded(self, *, section_ids: list, superseded_by_turn: str | None = None):
        hit = [s for s in self._appended if s.id in set(section_ids)]
        for s in hit:
            s.superseded = True
        return [s.id for s in hit]

    async def patch_section(self, *, section_id: str, patch: dict, expected_rev=None):
        for sec in self._appended:
            if sec.id == section_id:
                sec.payload.update(patch)
                sec.rev += 1
                return sec
        return None


@pytest.mark.unit
async def test_compose_canvas_from_payload_end_to_end() -> None:
    from bowhead_core.types import TurnRequest

    store = _StubCanvasStore()
    bus = _StubBus()
    request = TurnRequest(
        turn_id="t_x",
        session_id="s_x",
        workspace_id="ws_x",
        user_id="u_x",
        member_uid="m_x",
        text="x",
    )
    kinds = await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=_good_payload(),
        intent=IntentType.REPORT,
    )
    # Written canvas-section count = returned-list length.
    assert len(store._appended) == len(kinds)
    # Require canvas.opened / outline_ready / section_added×N / canvas.done.
    types = [t for t, _ in bus.events]
    assert "canvas.opened" in types
    assert "canvas.outline_ready" in types
    assert types.count("canvas.section_added") == len(kinds)
    assert types[-1] == "canvas.done"
    done_payload = bus.events[-1][1]
    assert done_payload["source"] == "payload"
    assert done_payload["section_count"] == len(kinds)


# ─────────────────────────────────────────────────────────────────
# chat-truth L-BE · canvas section dedup by data hash
# ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l_t5_dedup_drops_identical_data_tables() -> None:
    """L-T5: 4 identical data_table sections (different titles, same rows+cols)
    → only 1 kept. Walkthrough Session 7 evidence: 4 identical 36-row 品类
    tables with titles 「Q1 各品类营业额排名」/「Q1 品类营收占比分布」/「Q1 品类
    分层明细表」/「Q1 各品类营业额排名 (重排)」.
    """
    from bowhead_core.canvas_composer import dedup_sections_by_data_hash

    rows = [{"品类": "女装", "营业额": 2_000_000_000}, {"品类": "鞋包皮件", "营业额": 729_000_000}, {"品类": "金银珠宝", "营业额": 683_000_000}]
    columns = ["品类", "营业额"]
    sections = [
        {"kind": "data_table", "title": "Q1 各品类营业额排名", "rows": rows, "columns": columns},
        {"kind": "data_table", "title": "Q1 品类营收占比分布", "rows": rows, "columns": columns},
        {"kind": "data_table", "title": "Q1 品类分层明细表", "rows": rows, "columns": columns},
        {"kind": "data_table", "title": "Q1 各品类营业额排名 (重排)", "rows": rows, "columns": columns},
    ]
    out = dedup_sections_by_data_hash(sections)
    assert len(out) == 1, "4 identical data tables should dedup to 1"
    assert out[0]["title"] == "Q1 各品类营业额排名", "preserve first occurrence"


@pytest.mark.unit
def test_l_t6_dedup_keeps_distinct_data() -> None:
    """L-T6: 2 data_tables with DIFFERENT data → both kept."""
    from bowhead_core.canvas_composer import dedup_sections_by_data_hash

    sections = [
        {"kind": "data_table", "title": "T1", "rows": [{"a": 1}, {"a": 2}], "columns": ["a"]},
        {"kind": "data_table", "title": "T2", "rows": [{"a": 3}, {"a": 4}], "columns": ["a"]},
    ]
    out = dedup_sections_by_data_hash(sections)
    assert len(out) == 2


@pytest.mark.unit
def test_l_t6b_dedup_passes_through_non_data_sections() -> None:
    """L-T6b: narrative / sources / h1 sections never dropped by dedup."""
    from bowhead_core.canvas_composer import dedup_sections_by_data_hash

    sections = [
        {"kind": "h1", "text": "T"},
        {"kind": "narrative", "markdown": "x", "style": "executive"},
        {"kind": "narrative", "markdown": "x", "style": "caveats"},  # same text, different style
        {"kind": "sources", "label": "数据来源", "entries": []},
    ]
    out = dedup_sections_by_data_hash(sections)
    # None of these have rows/columns/cells/spec — all pass through
    assert len(out) == len(sections)


# ── citation-audit M1 slice 3: composer integration (gen3-only) ──


@pytest.mark.unit
async def test_compose_payload_gen3_only_binds_and_flags() -> None:
    """新模式（默认）：peer 文本的显式 :src: 归因 content-addressed 转 [N]；
    plain gen2 残余 bake 成纯值（单位保留）；裸值经 binder 补 [N]；编造数字
    → 结构化 unverified_numbers。落库全程零 gen2 标记。"""
    import json as _json
    from types import SimpleNamespace

    from bowhead_core.types import TurnRequest

    store = _StubCanvasStore()
    bus = _StubBus()
    request = TurnRequest(
        turn_id="t_g3",
        session_id="s_g3",
        workspace_id="ws",
        user_id="u",
        member_uid="m",
        text="x",
    )
    invs = [
        SimpleNamespace(
            call_id="tl_a",
            tool_name="bi_query",
            arguments={"query": "全省合计"},
            result={"artifact_id": "art_abcd", "rows": [{"合计": 96270000}]},
            error=None,
        )
    ]
    payload = _good_payload()
    payload["exec_summary"] = "显式引用 [num:4.18:%:src:tl_a] 成立；合计 9627万元；预计 150亿元；残余 [num:1.5:倍]。伪造死号结论 [1] 应被剥。"
    await compose_canvas_from_payload(
        canvas_store=store,
        request=request,
        bus=bus,
        payload=payload,
        intent=IntentType.REPORT,
        tool_invocations=invs,
    )
    dump = _json.dumps([s.payload for s in store._appended], ensure_ascii=False)
    assert "[num:" not in dump  # No gen2 persisted.
    assert "4.18 % [" in dump  # Explicit :src: → [N] (current resolve_in_text).
    assert "9627万元 [" in dump  # Bare-value binding + trailing unit.
    assert "1.5倍" in dump  # Bake plain residuals (keep unit).
    assert "150亿元" in dump  # Fabricated number stays plain.
    # SEC-P0: strip peer-forged bare [N] before minting legal [N] (remaining double spaces cosmetic, A-P3 recorded).
    assert "伪造死号结论 [1]" not in dump
    assert "伪造死号结论" in dump
    # Fabricated numbers enter structured warning (exec_summary narrative).
    narr = [s.payload for s in store._appended if s.kind == "narrative"]
    flat_unv = [u for p in narr for u in (p.get("unverified_numbers") or [])]
    assert any("150" in u for u in flat_unv)
    # Sources section contains cited sources.
    srcs = [s.payload for s in store._appended if s.kind == "sources"]
    assert srcs and any(e.get("artifact_id") in ("art_abcd", "tl_a") for e in srcs[0]["entries"])


# ─────────────────────────────────────────────────────────────
# Batch 1-a / 1-b (plan-report-assembly): owner stamping + no promote_to_top for N>=2.
# ─────────────────────────────────────────────────────────────


def _req(turn_id: str = "t_slot"):
    from bowhead_core.types import TurnRequest

    return TurnRequest(turn_id=turn_id, session_id="s_slot", workspace_id="ws_slot", user_id="u_slot", member_uid="m_slot", text="")


@pytest.mark.unit
async def test_compose_stamps_owner_from_peer_id() -> None:
    """批 1-a：peer_id 非空时，本次写入的每段都要带上 owner=peer_id。

    owner 是装订的槽位钥匙：plan 侧按 `owner == f"sub_{step.id}"` 反查本步产出的段
    （tech-design §2.1，R2 之后不再走 composer 出参）。
    """
    store, bus = _StubCanvasStore(), _StubBus()
    await compose_canvas_from_payload(
        canvas_store=store, request=_req(), bus=bus, payload=_good_payload(), intent=IntentType.REPORT, peer_id="sub_ps_aaa"
    )
    assert store._appended, "compose 应写出 section"
    by_kind = {s.kind: s.owner for s in store._appended}
    # sources is **document-level** (sources_resolver turn upsert, one section per turn),
    # inherently unowned; tech-design §2.3 explicitly says current behavior correct, never stamp to chapter.
    chaptered = {k: v for k, v in by_kind.items() if k != "sources"}
    assert chaptered, "compose 应写出章节段"
    assert set(chaptered.values()) == {"sub_ps_aaa"}, f"owner 未 stamp: {by_kind}"
    assert by_kind.get("sources") is None, f"sources 不该带章的 owner: {by_kind}"


@pytest.mark.unit
async def test_compose_leaves_owner_none_without_peer_id() -> None:
    """正对照：不传 peer_id（Lead 末轮/普通轮）时 owner 必须仍是 None，行为逐字节不变。"""
    store, bus = _StubCanvasStore(), _StubBus()
    await compose_canvas_from_payload(canvas_store=store, request=_req(), bus=bus, payload=_good_payload(), intent=IntentType.REPORT)
    assert store._appended
    assert {s.owner for s in store._appended} == {None}


@pytest.mark.unit
async def test_promote_to_top_still_runs_for_single_producing_peer() -> None:
    """正对照（批 1-b 的另一半）：本轮只有 1 条产出腿时，promote_to_top 照旧发。

    没有这条，下面那条「N>=2 不发」可能因为别的原因假绿。
    """
    store, bus = _StubCanvasStore(), _StubBus()
    # Seed streamed chart (bare figure persisted during peer-loop); must move behind.
    store._seq += 1
    store._appended.append(_StubSection("sec_chart", store._seq, "chart", {"spec": {}}, turn_id="t_slot"))
    await compose_canvas_from_payload(
        canvas_store=store, request=_req(), bus=bus, payload=_good_payload(), intent=IntentType.REPORT, peer_id="sub_ps_only"
    )
    reorders = [p for t, p in bus.events if t == "canvas.reordered"]
    assert reorders, "单产出腿轮必须仍然 promote_to_top"
    assert reorders[-1]["reason"] == "payload_path_promote_to_top"


@pytest.mark.unit
async def test_promote_to_top_suppressed_once_second_peer_produces() -> None:
    """批 1-b：本轮出现第 2 条产出腿后不再 promote_to_top —— 否则它会把新块顶到最前，
    和槽位序互相打架，用户看见块跳动（tech-design §2.1 / self-review R1）。"""
    store, bus = _StubCanvasStore(), _StubBus()
    await compose_canvas_from_payload(
        canvas_store=store, request=_req(), bus=bus, payload=_good_payload(), intent=IntentType.REPORT, peer_id="sub_ps_1st"
    )
    bus.events.clear()
    await compose_canvas_from_payload(
        canvas_store=store, request=_req(), bus=bus, payload=_good_payload(), intent=IntentType.REPORT, peer_id="sub_ps_2nd"
    )
    reorders = [p for t, p in bus.events if t == "canvas.reordered"]
    assert not reorders, f"第 2 条腿不该再 promote_to_top: {reorders}"


# ─────────────────────────────────────────────────────────────
# Batch 1-c primitive: reorder_canvas_by_slots shared by plans (plan_steps.seq) and
# nonplans (spawn order); only slot-number source differs.
# ─────────────────────────────────────────────────────────────


def _owned(store: _StubCanvasStore, sid: str, kind: str, owner: str | None) -> None:
    store._seq += 1
    store._appended.append(_StubSection(sid, store._seq, kind, {}, turn_id="t_slot", owner=owner))


@pytest.mark.unit
async def test_reorder_by_slots_orders_chapters_and_trails_unowned() -> None:
    """槽位序把各章按槽位号排好，无主段（流式 chart / 代码）按原相对序尾随。

    这正是 discovery C3 的解：今天是「谁后交谁在前」＝完成倒序。
    """
    from bowhead_core.canvas_composer import reorder_canvas_by_slots

    store, bus = _StubCanvasStore(), _StubBus()
    # Persistence order = completion order: chapter 2 before chapter 0 (actual plans do this).
    _owned(store, "sec_ch2_a", "h1", "sub_ps_two")
    _owned(store, "sec_ch2_b", "narrative", "sub_ps_two")
    _owned(store, "sec_chart", "chart", None)  # Unowned: streamed chart.
    _owned(store, "sec_ch0_a", "h1", "sub_ps_zero")
    _owned(store, "sec_ch1_a", "h1", "sub_ps_one")

    ordered = await reorder_canvas_by_slots(
        canvas_store=store,
        bus=bus,
        turn_id="t_slot",
        canvas_id="cv_test",
        slot_of_owner={"sub_ps_zero": 0, "sub_ps_one": 1, "sub_ps_two": 2},
    )
    assert ordered[:4] == ["sec_ch0_a", "sec_ch1_a", "sec_ch2_a", "sec_ch2_b"], ordered
    assert "sec_chart" not in ordered[:4], "无主段不该插进章节之间"
    ev = [p for t, p in bus.events if t == "canvas.reordered"]
    assert len(ev) == 1 and ev[0]["reason"] == "slot_order", ev


@pytest.mark.unit
async def test_reorder_by_slots_noop_when_no_owned_sections() -> None:
    """正对照：画布上没有任何带槽位的段时，不排序、不发事件（别惊动普通轮）。"""
    from bowhead_core.canvas_composer import reorder_canvas_by_slots

    store, bus = _StubCanvasStore(), _StubBus()
    _owned(store, "sec_plain", "narrative", None)
    ordered = await reorder_canvas_by_slots(canvas_store=store, bus=bus, turn_id="t_slot", canvas_id="cv_test", slot_of_owner={"sub_ps_zero": 0})
    assert ordered == []
    assert not [p for t, p in bus.events if t == "canvas.reordered"]


@pytest.mark.unit
async def test_reorder_by_slots_noop_when_already_ordered() -> None:
    """正对照：已经是槽位序时不发多余 reorder（同 A3 的 no-op 守卫口径）。"""
    from bowhead_core.canvas_composer import reorder_canvas_by_slots

    store, bus = _StubCanvasStore(), _StubBus()
    _owned(store, "sec_a", "h1", "sub_ps_zero")
    _owned(store, "sec_b", "h1", "sub_ps_one")
    await reorder_canvas_by_slots(
        canvas_store=store, bus=bus, turn_id="t_slot", canvas_id="cv_test", slot_of_owner={"sub_ps_zero": 0, "sub_ps_one": 1}
    )
    assert not [p for t, p in bus.events if t == "canvas.reordered"]


@pytest.mark.unit
async def test_reorder_by_slots_sinks_unowned_when_a_later_chapter_lands_after_them() -> None:
    """★G1（116 真机 t_c9853b490565 逮到）：**带主段彼此有序 ≠ 画布已是终态**。

    生产形状：前几章交稿后 reorder 过一次 ⇒ 画布 = [ch0..ch5][图/代码...]。
    随后 ch6 完成、composer 把它 append 到**最末**（在那堆图后面）。此时
    `owned` 按画布序取出来仍是 ch0..ch6 —— **彼此之间就是槽位序**，
    老 no-op 闸（`ordered == [s.id for s in owned]`）于是短路、一个字都不写，
    那堆无主的图/代码就永远卡在 ch5 和 ch6 中间。

    实测坐实：8 步只落了 4 次 reorder 写入（canvas_revisions op=reorder），
    步 3/6/7 完成后都没写；`plan_exec.slot_order` 日志却照打（它记的是返回值
    长度，分不出写没写）。用户读到的是 …第6章 → sources+图 → 第7章。

    根因：`reorder_sections` 的语义是「列出的在前 + 未列出的按原相对序尾随」，
    所以**绝对位置也是结果的一部分**，闸只比带主段的相对序是不够的。
    """
    from bowhead_core.canvas_composer import reorder_canvas_by_slots

    store, bus = _StubCanvasStore(), _StubBus()
    # First two chapters ordered.
    _owned(store, "sec_ch0", "h1", "sub_ps_zero")
    _owned(store, "sec_ch1", "h1", "sub_ps_one")
    # Unowned streamed output follows.
    _owned(store, "sec_chart_a", "chart", None)
    _owned(store, "sec_code_a", "code_block", None)
    # Third chapter finishes later → appended at end, **after** chart/code.
    _owned(store, "sec_ch2", "h1", "sub_ps_two")

    ordered = await reorder_canvas_by_slots(
        canvas_store=store,
        bus=bus,
        turn_id="t_slot",
        canvas_id="cv_test",
        slot_of_owner={"sub_ps_zero": 0, "sub_ps_one": 1, "sub_ps_two": 2},
    )
    # Three chapters contiguous, chart/code move behind.
    assert ordered == ["sec_ch0", "sec_ch1", "sec_ch2"], ordered
    ev = [p for t, p in bus.events if t == "canvas.reordered"]
    assert len(ev) == 1 and ev[0]["reason"] == "slot_order", f"必须真写一次库: {ev}"
    final = [s.id for s in await store.list_sections("cv_test")]
    assert final == ["sec_ch0", "sec_ch1", "sec_ch2", "sec_chart_a", "sec_code_a"], final


# ─────────────────────────────────────────────────────────────
# Batch 1-c2: retry **exactly** supersedes old chapter by owner (decision #3).
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_supersede_stale_owner_sections_folds_only_that_chapter() -> None:
    """重跑第 N 步 ⇒ 只折第 N 章的旧段，别的章一段都不许动。

    对照现状：α 折叠（`_alpha_fold_prior_blocks`）的粒度是整个前序 turn（批 A1 后按 turn_id
    判、plan 执行轮直接跳过）⇒ 重跑一步不能靠它，只能按 owner 折本步（tech-design 决议 #3）。
    按 owner 折才是「重跑这一步」的语义，而这个修法**只有 stamp 了 owner 之后才表达得出来**。
    """
    from bowhead_core.canvas_composer import supersede_stale_owner_sections

    store = _StubCanvasStore()
    store._seq += 1
    store._appended.append(_StubSection("sec_old_ch1", store._seq, "h1", {}, turn_id="t_old", owner="sub_ps_one"))
    store._seq += 1
    store._appended.append(_StubSection("sec_old_ch2", store._seq, "h1", {}, turn_id="t_old", owner="sub_ps_two"))
    store._seq += 1
    store._appended.append(_StubSection("sec_new_ch1", store._seq, "h1", {}, turn_id="t_new", owner="sub_ps_one"))

    folded = await supersede_stale_owner_sections(canvas_store=store, canvas_id="cv_test", owner="sub_ps_one", keep_turn_id="t_new")
    assert folded == ["sec_old_ch1"], folded
    by_id = {x.id: x for x in store._appended}
    assert by_id["sec_old_ch1"].superseded is True
    assert by_id["sec_old_ch2"].superseded is False, "别的章不许被连坐折掉"
    assert by_id["sec_new_ch1"].superseded is False, "本轮新章当然不能折"


@pytest.mark.unit
async def test_supersede_stale_owner_noop_on_first_run() -> None:
    """正对照：首跑（该 owner 只有本轮的段）不折任何东西、不写库。"""
    from bowhead_core.canvas_composer import supersede_stale_owner_sections

    store = _StubCanvasStore()
    store._seq += 1
    store._appended.append(_StubSection("sec_a", store._seq, "h1", {}, turn_id="t_new", owner="sub_ps_one"))
    assert await supersede_stale_owner_sections(canvas_store=store, canvas_id="cv_test", owner="sub_ps_one", keep_turn_id="t_new") == []


@pytest.mark.unit
async def test_reorder_by_slots_skips_superseded() -> None:
    """已折叠的旧章不参与槽位序（否则重试后同一槽位会排出两份）。"""
    from bowhead_core.canvas_composer import reorder_canvas_by_slots

    store, bus = _StubCanvasStore(), _StubBus()
    _owned(store, "sec_new_b", "h1", "sub_ps_one")
    _owned(store, "sec_old_a", "h1", "sub_ps_zero")
    store._appended[-1].superseded = True
    _owned(store, "sec_new_a", "h1", "sub_ps_zero")
    ordered = await reorder_canvas_by_slots(
        canvas_store=store,
        bus=bus,
        turn_id="t_slot",
        canvas_id="cv_test",
        slot_of_owner={"sub_ps_zero": 0, "sub_ps_one": 1},
    )
    assert ordered == ["sec_new_a", "sec_new_b"], ordered


# ─────────────────────────────────────────────────────────────
# Batch 1-e: nonplan multileg turn slot numbers from **spawn order** (tech-design §2.7).
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_spawn_order_slots_from_bus() -> None:
    """Lead 派发顺序就是非 plan 轮的章序 —— 它是 Lead 自己定的，有意义。"""
    from types import SimpleNamespace

    from bowhead_core.canvas_composer import spawn_order_slots

    bus = SimpleNamespace(_spawn_order=["sub_a", "sub_b", "sub_c"])
    assert spawn_order_slots(bus) == {"sub_a": 0, "sub_b": 1, "sub_c": 2}


@pytest.mark.unit
def test_spawn_order_slots_empty_when_absent_or_single() -> None:
    """正对照：没记录 / 只有 1 条腿 ⇒ 空 map ⇒ 调用方不排序（普通轮零影响）。

    533 个非 plan 多 peer 轮里 89% 只有 2 条腿、h1>=2 的只有 3 个 —— 这条闸保证
    绝大多数轮次连探测都不做。
    """
    from types import SimpleNamespace

    from bowhead_core.canvas_composer import spawn_order_slots

    assert spawn_order_slots(SimpleNamespace()) == {}
    assert spawn_order_slots(SimpleNamespace(_spawn_order=[])) == {}
    assert spawn_order_slots(SimpleNamespace(_spawn_order=["sub_only"])) == {}


@pytest.mark.unit
def test_spawn_order_slots_dedups_repeated_ids() -> None:
    """agent_spawned 是多态事件（同 id 会为「完成」再发一次）—— 记序必须按首次出现。"""
    from types import SimpleNamespace

    from bowhead_core.canvas_composer import spawn_order_slots

    bus = SimpleNamespace(_spawn_order=["sub_a", "sub_b", "sub_a", "sub_c"])
    assert spawn_order_slots(bus) == {"sub_a": 0, "sub_b": 1, "sub_c": 2}


# ─────────────────────────────────────────────────────────────
# Batch 2-a: leg delivers a chapter, not a whole document: chapter_mode.
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_chapter_mode_drops_leg_level_chrome() -> None:
    """章不自带封面与文档级尾巴：eyebrow / recommendations / caveats 交给文档级池。

    今天 8 条腿各渲一份完整报告 ⇒ 4 个 eyebrow、5 组建议、6 堵「数据局限性」
    （discovery C4）。章该留的是标题+正文+数字+洞察。
    """
    secs = _build_sections_from_payload(_good_payload(), IntentType.REPORT, chapter_mode=True)
    kinds = [s["kind"] for s in secs]
    assert "h1" in kinds, kinds
    assert "kpi_row" in kinds and "insight_card" in kinds, kinds
    assert "eyebrow" not in kinds, "章不该自带 eyebrow"
    # Review B2-4: meta pills (updated at/data systems/Bowhead generated) identify document;
    # one per chapter makes 8 rows; treat like eyebrow.
    assert "meta" not in kinds, "章不该自带 meta pills"
    assert "recommendations" not in kinds, "建议归文档级池"
    # This path renders caveats as style=caveats narrative.
    assert not [s for s in secs if s.get("style") == "caveats"], "数据局限性归文档级池"


@pytest.mark.unit
def test_default_mode_unchanged_positive_control() -> None:
    """正对照：不开 chapter_mode（普通单腿轮 / Lead 末轮）时腿级 chrome 一件不少。

    建议与作者局限已整体退役（§2.2/§2.3），所以这里改看仍然区分两档的那两件：eyebrow 与 meta。
    """
    secs = _build_sections_from_payload(_good_payload(), IntentType.REPORT)
    kinds = [s["kind"] for s in secs]
    assert "eyebrow" in kinds and "meta" in kinds, kinds


@pytest.mark.unit
async def test_compose_passes_chapter_mode_through() -> None:
    """compose 层要能把 chapter_mode 透下去（ψ-E 在 plan 轮传 True）。"""
    store, bus = _StubCanvasStore(), _StubBus()
    await compose_canvas_from_payload(
        canvas_store=store,
        request=_req(),
        bus=bus,
        payload=_good_payload(),
        intent=IntentType.REPORT,
        peer_id="sub_ps_ch",
        chapter_mode=True,
    )
    kinds = [s.kind for s in store._appended]
    assert "h1" in kinds
    assert "eyebrow" not in kinds and "recommendations" not in kinds, kinds


# ─────────────────────────────────────────────────────────────────
# source-accessibility surface e1 · composer half: turn-namespace [cite:N] never enters canvas.
# ─────────────────────────────────────────────────────────────────


def _all_text_fields(sections: list[_StubSection]) -> list[str]:
    out: list[str] = []

    def _walk(obj) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, str):
            out.append(obj)

    for sec in sections:
        _walk(sec.payload)
    return out


@pytest.mark.unit
async def test_e1_composer_path_strips_turn_scope_cite_markers() -> None:
    """`_auto_append_citations` 贴在 exec_summary / facts 段末的 `[cite:N]` 是 turn
    registry 空间（chat 面）；composer 给同一段正文铸的 `[N]` 是 sources 空间。
    08-28 拍板画布正文归 gen3 `[N]`，e1 只剥了 writer 路——本条钉住 payload 直落路。
    正对照：输入带 `[cite:1]` / `[cite:2]`，落库段一个 `[cite:` 都不能有；散文与
    `[figure:sec_x]` 锚原样保留（锚与写路 figure 体系同名同义，e1 同款不剥）。
    """
    from bowhead_core.types import TurnRequest

    payload = _good_payload()
    payload["exec_summary"] = "整体合规率 11.39%，部门支出 1.85% 是绝对拖累。[figure:sec_a] [cite:1]"
    payload["insights"][0]["facts"] = [{"text": "部门支出占 90% 单量但合规率仅 1.85%，是结构性短板。 [cite:2]"}]
    payload["insights"][1]["narrative"] = "无请购原因字段空值占据不合规单绝对主导。 [cite:1]"
    # Positive control not vacuous: input actually has 3 turn-namespace markers.
    assert (
        sum(t.count("[cite:") for t in (payload["exec_summary"], payload["insights"][0]["facts"][0]["text"], payload["insights"][1]["narrative"]))
        == 3
    )

    store = _StubCanvasStore()
    bus = _StubBus()
    request = TurnRequest(turn_id="t_x", session_id="s_x", workspace_id="ws_x", user_id="u_x", member_uid="m_x", text="x")
    await compose_canvas_from_payload(canvas_store=store, request=request, bus=bus, payload=payload, intent=IntentType.REPORT)

    texts = _all_text_fields(store._appended)
    leaked = [t for t in texts if "[cite:" in t]
    assert leaked == [], f"turn 空间 [cite:N] 漏进画布: {leaked!r}"
    narrative = next(s for s in store._appended if s.kind == "narrative" and s.payload.get("style") == "executive")
    md = narrative.payload["markdown"]
    assert "整体合规率 11.39%，部门支出 1.85% 是绝对拖累。" in md
    assert "[figure:sec_a]" in md, "figure 锚不是 cite 标记，不能被顺手剥掉"


# ─────────────────────────────────────────────────────────────────
# canvas-elements · document projection (FEATURE_CANVAS_DOC_PROJECTION).
# ─────────────────────────────────────────────────────────────────


def _skeleton() -> list[dict]:
    """今天的固定骨架（`_build_sections_from_payload` 的产出形）。"""
    return _build_sections_from_payload(_good_payload(), IntentType.REPORT)


@pytest.mark.unit
def test_doc_projection_cards_become_prose_and_chrome_folds() -> None:
    from bowhead_core.canvas_doc_projection import project_document

    src = _skeleton()
    assert {s["kind"] for s in src} >= {"eyebrow", "h1", "meta", "narrative", "kpi_row", "insight_card", "sources"}
    # The fixture is a Chinese report; its deterministic labels (impact, figure caption) use zh-CN.
    out = project_document(src, valid_chart_ids={"sec_a"}, lang="zh-CN")
    kinds = [s["kind"] for s in out]
    # Remove every card-shaped kind, add no kind beyond existing set.
    assert not {"insight_card", "recommendations", "eyebrow", "meta"} & set(kinds)
    assert set(kinds) <= {"h1", "narrative", "kpi_row", "sources"}
    # Head: h1 → introduction → KPI strip. Byline retired (§2.4): metadata, not content; belongs to UI frame.
    assert [(x["kind"], x.get("style") or "") for x in out[:3]] == [("h1", ""), ("narrative", "executive"), ("kpi_row", "")]
    assert out[1]["markdown"].startswith("整体合规率 11.39%")
    assert not any(x.get("style") == "byline" for x in out), "署名行已退役"
    # Even meta pills produce no byline; intent enum (REPORT) still dropped.
    out_meta = project_document(src, valid_chart_ids={"sec_a"}, meta_pills=["更新于 2026-09-06", "2 个数据系统", "Bowhead 生成"])
    assert not any(x.get("style") == "byline" for x in out_meta)
    assert not any("REPORT" in (x.get("markdown") or "") for x in out_meta)
    assert not any("Bowhead 生成" in (x.get("markdown") or "") for x in out_meta)
    # Plan-leg chapters have no document chrome/byline.
    chap = _build_sections_from_payload(_good_payload(), IntentType.REPORT, chapter_mode=True)
    out_chap = project_document(chap, valid_chart_ids=set(), meta_pills=["更新于 2026-09-06"], chapter_mode=True)
    assert not any(x.get("style") == "byline" for x in out_chap) and out_chap[1]["markdown"].startswith("整体合规率")
    assert out[2]["cells"][0]["lbl"] == "合规率"
    # Insight card → ## heading + thesis blockquote (with impact) + body + figure anchor + caption + callout.
    card1 = next(s for s in out if s["kind"] == "narrative" and s.get("style") == "insight")
    md = card1["markdown"]
    assert md.startswith("## 部门支出拖累\n\n> 占 90% 单量但合规率 1.85% — 影响 +16.5pp\n\n部门支出在两个维度")
    assert "\n\n[figure:sec_a]\n\n图1：对比\n\n" in md
    assert "**杠杆 16.5pp**\n测算" in md
    # Card 2 sec_b outside valid set → no anchor, italic caption only.
    card2 = [s for s in out if s.get("style") == "insight"][1]["markdown"]
    assert "[figure:sec_b]" not in card2 and "*Pareto*" in card2
    # Skeleton no longer emits recommendations (§2.2, author writes in body); projection still
    # supports recommendations kind (thin path may supply it); only confirm skeleton no longer emits.
    assert not [s for s in out if s.get("style") == "recommendations"]
    # caveats / sources unchanged.
    # Skeleton no longer emits author-written limitations (§2.3); projection unchanged, upstream no longer supplies.
    assert not any(s.get("style") == "caveats" for s in out)
    assert out[-1]["kind"] == "sources" and out[-1]["entries"][0]["artifact_id"] == "art_001"
    # Pure function: input unchanged.
    assert [s["kind"] for s in src] == [s["kind"] for s in _skeleton()]


@pytest.mark.unit
def test_doc_projection_confidence_and_multi_report_turn() -> None:
    from bowhead_core.canvas_doc_projection import project_document

    payload = _good_payload()
    payload["insights"][0]["confidence"] = "med"
    payload["insights"][1]["confidence"] = "low"
    src = _build_sections_from_payload(payload, IntentType.REPORT)
    # Multi-report turn: two chrome sets; second h1/kpi_row stay, second eyebrow becomes italic line.
    src = src + [{"kind": "eyebrow", "text": "第二章"}, {"kind": "h1", "text": "第二份"}, {"kind": "kpi_row", "cells": [{"lbl": "x", "val": "1"}]}]
    out = project_document(src, lang="zh-CN", valid_chart_ids=set())
    insights = [s["markdown"] for s in out if s.get("style") == "insight"]
    assert "> 占 90% 单量但合规率 1.85% — 影响 +16.5pp — 置信度：中" in insights[0]
    assert "> 66,224 单无原因 — 置信度：低" in insights[1]
    # Second chrome h1/kpi_row stay; drop second eyebrow along with retired byline (§2.4).
    tail = out[-2:]
    assert [s["kind"] for s in tail] == ["h1", "kpi_row"]
    assert tail[0]["text"] == "第二份"
    assert not any("第二章" in (s.get("markdown") or "") for s in out)
    # English report uses English chrome.
    out_en = project_document(_build_sections_from_payload(_good_payload(), IntentType.REPORT), lang="en-US", valid_chart_ids={"sec_a"})
    md_en = "\n".join(s["markdown"] for s in out_en if s["kind"] == "narrative")
    assert "Figure 1: 对比" in md_en and "Impact +16.5pp" in md_en
    # Recommendation brackets / clause punctuation also follow language (116 A6 English report had fullwidth "（P1 · …）", "；").
    from bowhead_core.canvas_doc_projection import recommendations_markdown

    recs = [{"priority": "p1", "action": "Act", "owner": "Ops", "timeline": "Q4", "rationale": "Why.", "expected_impact": "+2pp"}]
    assert recommendations_markdown(recs, "en-US") == "## Recommendations\n1. **Act** (P1 · Ops · Q4)\n   Why.; Expected: +2pp"
    assert recommendations_markdown(recs, "zh-CN") == "## 建议\n1. **Act**（P1 · Ops · Q4）\n   Why.；预期：+2pp"


@pytest.mark.unit
async def test_document_projection_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认（未设）：经 compose_canvas_from_payload 落库的段里没有卡片 kind；chart 校验后的
    真 id 成为图锚；Phase-F 之后 sources 段照旧。kill-switch=0：kinds 与卡片骨架逐字节相同。"""
    from bowhead_core.types import TurnRequest

    async def _run() -> list[_StubSection]:
        store = _StubCanvasStore()
        # Peer already wrote chart sec_a via add_chart (evidence references it).
        store._appended.append(_StubSection("sec_a", 0, "chart", {"spec": {"kind": "bar", "title": "对比", "rows": []}}, turn_id="t_prev"))
        request = TurnRequest(turn_id="t_x", session_id="s_x", workspace_id="ws_x", user_id="u_x", member_uid="m_x", text="x")
        await compose_canvas_from_payload(canvas_store=store, request=request, bus=_StubBus(), payload=_good_payload(), intent=IntentType.REPORT)
        return [s for s in store._appended if s.turn_id == "t_x"]

    on = await _run()
    assert [(s.kind, s.payload.get("style") or "") for s in on] == [
        ("h1", ""),
        ("kpi_row", ""),
        ("narrative", "executive"),
        ("narrative", "insight"),
        ("narrative", "insight"),
        ("sources", ""),
    ]
    md = "\n".join(s.payload.get("markdown", "") for s in on if s.kind == "narrative")
    assert "[figure:sec_a]" in md and "## 部门支出拖累" in md and "[figure:sec_b]" not in md
    # Byline retired (§2.4): no persisted style=byline; no meta pill section either.
    assert not [s for s in on if (s.payload or {}).get("style") == "byline"]
    assert not [s for s in on if s.kind == "meta"]
