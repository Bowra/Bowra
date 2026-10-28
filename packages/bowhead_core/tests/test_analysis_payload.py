"""AnalysisPayload schema + validation tests (#142 M137-M1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_core.analysis_payload import (
    AnalysisPayload,
    EvidenceBlock,
    Insight,
    Kpi,
    Recommendation,
    payload_from_dict,
    validate_payload,
    validate_thin_canvas_sections,
)

_SPIKE_RUN = Path(__file__).resolve().parents[3] / "reports" / "spike-M137" / "run_01.json"


@pytest.mark.unit
@pytest.mark.parametrize("surface", ["chat", "report"])
@pytest.mark.parametrize(
    ("value", "ambiguous"),
    [
        ("Q1 / Operations reporting", False),
        ("Corporate", False),
        ("Operating ratio reviewed", False),
        ("1.618 ratio", True),
        ("0.15 rate", True),
        ("1.5RATIO", True),
        ("(0.2 Rate)", True),
        ("15%", False),
    ],
)
def test_kpi_unit_validation_distinguishes_numeric_units_from_text(surface, value, ambiguous):
    payload = _good_payload_dict()
    payload["delivery_surface"] = surface
    payload["kpis"][0]["value"] = value
    _, errors = validate_payload(payload)
    assert any("banned unit" in error for error in errors) is ambiguous


def _good_payload_dict() -> dict:
    return {
        "title": "EA 看板",
        "exec_summary": "整体合规率 11.39%。",
        "kpis": [{"label": "合规率", "value": "11.39%", "sentiment": "bad"}],
        "insights": [
            {
                "title": "部门支出拖累",
                "thesis": "占 90% 单量但合规率 1.85%",
                "evidence": [
                    {"kind": "chart", "chart_section_id": "sec_aaa", "caption": "对比"},
                ],
                "narrative": "部门支出在两个维度都是结构性问题，合规率和趋势都低。" * 2,
                "confidence": "high",
            }
        ],
        "recommendations": [{"priority": "P0", "action": "OA 必填字段", "owner": "IT 系统组"}],
    }


# ─────────────────────────────────────────────────────────────
# happy paths
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_validate_minimal_good_payload() -> None:
    ok, errs = validate_payload(_good_payload_dict())
    assert ok, errs


@pytest.mark.unit
def test_validate_dataclass_instance() -> None:
    p = AnalysisPayload(
        title="X",
        exec_summary="exec_summary 文字至少 30 字看够不够长足够长足够长足够长",
        kpis=[Kpi(label="a", value="1")],
        insights=[
            Insight(
                title="t",
                thesis="th",
                evidence=[EvidenceBlock(kind="chart", caption="c", chart_section_id="sec_x")],
                narrative="x" * 50,
            )
        ],
        recommendations=[Recommendation(priority="P1", action="do x")],
    )
    ok, errs = validate_payload(p)
    assert ok, errs


@pytest.mark.unit
def test_validate_short_answer_payload_without_report_scaffolding() -> None:
    """简单计算是 Chat 交付，不应为了通过 schema 硬凑 insight/recommendation。"""
    payload = {
        "delivery_surface": "chat",
        "exec_summary": "19 × 29 = 551",
        "kpis": [
            {
                "label": "19 × 29",
                "value": "551",
                "sentiment": "neutral",
            },
        ],
        "ground_facts": [
            {
                "key": "product",
                "label": "19 × 29",
                "value": 551,
                "unit": "",
                "formula": "19*29",
                "source": "python_sandbox",
            },
        ],
        "recommendations": [],
    }

    ok, errs = validate_payload(payload)

    assert ok, errs
    typed = payload_from_dict(payload)
    assert typed.delivery_surface == "chat"
    typed_ok, typed_errs = validate_payload(typed)
    assert typed_ok, typed_errs


@pytest.mark.unit
def test_long_report_shell_without_insights_still_fails() -> None:
    """短答豁免必须保守；长篇报告漏掉正文结构时仍要拒绝。"""
    payload = {
        "title": "经营复盘",
        "exec_summary": "这是一个缺少洞察结构的长篇报告草稿。" * 30,
        "kpis": [{"label": "GMV", "value": "5.2亿元", "sentiment": "neutral"}],
        "recommendations": [],
    }

    ok, errs = validate_payload(payload)

    assert not ok
    assert any("insight" in err for err in errs)


@pytest.mark.unit
def test_chat_declaration_is_honored_not_second_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """批 5ⓐ（用户拍板 2026-08-20）：peer 说 chat 就是 chat——后端不再用「形状够不够短」
    替它判断算不算报告。payload 里带 title/kpis/insights 只是它想在聊天里说的内容。
    历史：这个耦合让 chat 越界的 payload 掉进报告必填项校验，116 30 天里约 21 次拒收。"""
    payload = {
        "delivery_surface": "chat",
        "title": "经营复盘",
        "exec_summary": "只有摘要，没有报告正文。",
        "kpis": [{"label": "GMV", "value": "5.2亿元", "sentiment": "neutral"}],
        "recommendations": [],
    }

    ok, errs = validate_payload(payload)

    assert ok, errs


@pytest.mark.unit
def test_chat_declaration_skips_report_only_recommendation_and_shell_rules() -> None:
    """★ review-R5/R9：chat 声明下 recommendations / insights 只是「想在聊天里说的内容」，
    不该再吃报告的 priority/owner 强制与空壳扫描——那正是本批要消掉的拒收环
    （auto-fix 在短答分支提前 return，这些错它也修不了）。"""
    payload = {
        "delivery_surface": "chat",
        "exec_summary": "华东 GMV 5.2 亿，同比 +12%。",
        "recommendations": [{"action": "加投华东"}],  # No priority/owner
        "insights": [{"title": "华东领跑", "thesis": "待确认口径", "narrative": "占比 41%"}],  # thesis contains a placeholder term.
    }
    ok, errs = validate_payload(payload)
    assert ok, errs


@pytest.mark.unit
def test_chat_declaration_still_reports_shape_errors() -> None:
    """★ review2-R5：chat 豁免的是 report-only 的 priority/owner 与空壳纪律，**不是形状检查**。
    `recommendations: ["加投华东"]`（字符串列表）过了校验，渲染时会被静默丢掉，
    peer 永远不知道自己写错——那是另一种「说了没做」。"""
    ok, errs = validate_payload(
        {
            "delivery_surface": "chat",
            "exec_summary": "结论。",
            "recommendations": ["加投华东"],
            "insights": ["不是 dict"],
        }
    )
    assert not ok
    assert any("recommendations[0] not dict" in e for e in errs)
    assert any("insights[0] not dict" in e for e in errs)


@pytest.mark.unit
def test_chat_declaration_still_requires_exec_summary() -> None:
    ok, errs = validate_payload({"delivery_surface": "chat", "kpis": []})
    assert not ok
    assert any("exec_summary" in e for e in errs)


@pytest.mark.unit
def test_short_answer_rejects_non_list_kpis() -> None:
    """kpis 形状错（dict 不是 list）必须拒绝——这是**形状**不是「算不算报告」。"""
    ok, errs = validate_payload(
        {
            "delivery_surface": "chat",
            "exec_summary": "19 × 29 = 551",
            "kpis": {},
        }
    )

    assert not ok
    assert any("kpis" in err for err in errs)


@pytest.mark.unit
def test_payload_from_dict_round_trip() -> None:
    d = _good_payload_dict()
    p = payload_from_dict(d)
    assert isinstance(p, AnalysisPayload)
    assert p.insights[0].evidence[0].chart_section_id == "sec_aaa"
    ok, errs = validate_payload(p)
    assert ok, errs


# ─────────────────────────────────────────────────────────────
# invariants
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_missing_top_level_field_fails() -> None:
    d = _good_payload_dict()
    del d["title"]
    ok, errs = validate_payload(d)
    assert not ok
    assert any("title" in e for e in errs)


@pytest.mark.unit
def test_too_many_insights_fails() -> None:
    d = _good_payload_dict()
    d["insights"] = [d["insights"][0]] * 8  # > 7
    ok, errs = validate_payload(d)
    assert not ok
    assert any("insights count" in e for e in errs)


@pytest.mark.unit
def test_zero_insights_fails() -> None:
    d = _good_payload_dict()
    d["insights"] = []
    ok, _errs = validate_payload(d)
    assert not ok


# ─────────────────────────────────────────────────────────────
# M157 dual-mode priority + thin canvas section validation
# ─────────────────────────────────────────────────────────────


def _thin_payload_dict() -> dict:
    """thin path: insight_section_ids 非空, insights[] 空."""
    return {
        "title": "EA 看板",
        "exec_summary": "整体合规率 11.39%。",
        "kpis": [{"label": "合规率", "value": "11.39%", "sentiment": "bad"}],
        "insights": [],
        "insight_section_ids": ["sec_aaa", "sec_bbb"],
        "recommendations": [{"priority": "P0", "action": "OA 必填字段", "owner": "IT 系统组"}],
    }


@pytest.mark.unit
def test_thin_path_validates_with_empty_insights() -> None:
    """insight_section_ids 非空 → insights[] 允许空, schema 通过."""
    d = _thin_payload_dict()
    ok, errs = validate_payload(d)
    assert ok, errs


@pytest.mark.unit
def test_both_empty_rejected() -> None:
    """insight_section_ids 空 + insights[] 空 → 拒."""
    d = _thin_payload_dict()
    d["insight_section_ids"] = []
    d["insights"] = []
    ok, errs = validate_payload(d)
    assert not ok
    assert any("neither insight_section_ids nor insights" in e for e in errs)


@pytest.mark.unit
def test_thin_path_payload_from_dict_preserves_ids() -> None:
    d = _thin_payload_dict()
    p = payload_from_dict(d)
    assert p.insight_section_ids == ["sec_aaa", "sec_bbb"]
    assert p.insights == []


@pytest.mark.unit
def test_validate_thin_canvas_sections_all_valid() -> None:
    sections = {
        "sec_a": {"kind": "insight_card", "title": "体系维度", "thesis": "T", "narrative": "N" * 50},
        "sec_b": {"kind": "insight_card", "title": "组件专项", "thesis": "T2", "narrative": "N2" * 50},
    }
    errs = validate_thin_canvas_sections(
        insight_section_ids=["sec_a", "sec_b"],
        sections_by_id=sections,
        outline_tier_titles=["体系维度", "组件专项"],
    )
    assert errs == []


@pytest.mark.unit
def test_validate_thin_canvas_sections_missing_id() -> None:
    errs = validate_thin_canvas_sections(
        insight_section_ids=["sec_missing"],
        sections_by_id={},
    )
    assert any("unknown id" in e for e in errs)


@pytest.mark.unit
def test_validate_thin_canvas_sections_wrong_kind() -> None:
    sections = {"sec_a": {"kind": "chart", "title": "图"}}
    errs = validate_thin_canvas_sections(
        insight_section_ids=["sec_a"],
        sections_by_id=sections,
    )
    assert any("expected 'insight_card'" in e for e in errs)


@pytest.mark.unit
def test_validate_thin_canvas_sections_anti_shell() -> None:
    """canvas section narrative 含空壳词 → 拒 (P1 #6)."""
    sections = {
        "sec_a": {
            "kind": "insight_card",
            "title": "体系维度",
            "thesis": "T",
            "narrative": "数据加载后再补充具体数字",
        },
    }
    errs = validate_thin_canvas_sections(
        insight_section_ids=["sec_a"],
        sections_by_id=sections,
    )
    assert any("placeholder" in e for e in errs)


@pytest.mark.unit
def test_validate_thin_canvas_sections_tier_unique_match() -> None:
    """两个 insight 都含 '合规' 但只对应一个 tier → 唯一对应贪心匹配 (P0 #3)."""
    sections = {
        "sec_a": {"kind": "insight_card", "title": "体系合规率分化", "thesis": "T", "narrative": "N" * 50},
        "sec_b": {"kind": "insight_card", "title": "部门合规率拖累", "thesis": "T", "narrative": "N" * 50},
    }
    # outline has tiers "体系" + "组件"; the second tier is unmatched and should fail.
    errs = validate_thin_canvas_sections(
        insight_section_ids=["sec_a", "sec_b"],
        sections_by_id=sections,
        outline_tier_titles=["体系", "组件"],
    )
    assert any("未覆盖的 tier" in e and "组件" in e for e in errs)


@pytest.mark.unit
def test_validate_thin_canvas_sections_tier_greedy_removes_pool() -> None:
    """唯一对应: 一个 insight 不能算 cover 多个 tier."""
    sections = {
        "sec_a": {"kind": "insight_card", "title": "体系组件总论", "thesis": "T", "narrative": "N" * 50},
    }
    # One insight title contains both "体系" + "组件", but greedy matching removes only one, leaving the second uncovered.
    errs = validate_thin_canvas_sections(
        insight_section_ids=["sec_a"],
        sections_by_id=sections,
        outline_tier_titles=["体系", "组件"],
    )
    assert any("未覆盖的 tier" in e for e in errs)


@pytest.mark.unit
def test_validate_thin_canvas_sections_no_outline_skips_tier() -> None:
    """outline 为 None → 不跑 tier 校验."""
    sections = {
        "sec_a": {"kind": "insight_card", "title": "随便", "thesis": "T", "narrative": "N" * 50},
    }
    errs = validate_thin_canvas_sections(
        insight_section_ids=["sec_a"],
        sections_by_id=sections,
        outline_tier_titles=None,
    )
    assert errs == []


@pytest.mark.unit
def test_evidence_empty_fails() -> None:
    d = _good_payload_dict()
    d["insights"][0]["evidence"] = []
    ok, errs = validate_payload(d)
    assert not ok
    assert any("evidence count" in e for e in errs)


@pytest.mark.unit
def test_evidence_too_many_fails() -> None:
    d = _good_payload_dict()
    d["insights"][0]["evidence"] = [{"kind": "chart", "chart_section_id": f"sec_{i}", "caption": "c"} for i in range(6)]
    ok, _errs = validate_payload(d)
    assert not ok


@pytest.mark.unit
def test_narrative_too_short_fails() -> None:
    d = _good_payload_dict()
    d["insights"][0]["narrative"] = "短"
    ok, errs = validate_payload(d)
    assert not ok
    assert any("narrative" in e for e in errs)


@pytest.mark.unit
def test_p0_without_owner_fails() -> None:
    d = _good_payload_dict()
    d["recommendations"][0].pop("owner")
    ok, errs = validate_payload(d)
    assert not ok
    assert any("P0" in e and "owner" in e for e in errs)


@pytest.mark.unit
def test_invalid_evidence_kind_fails() -> None:
    d = _good_payload_dict()
    d["insights"][0]["evidence"][0]["kind"] = "bogus"
    ok, _errs = validate_payload(d)
    assert not ok


@pytest.mark.unit
def test_evidence_kind_chart_requires_chart_section_id() -> None:
    d = _good_payload_dict()
    d["insights"][0]["evidence"][0] = {"kind": "chart", "caption": "c"}  # Missing chart_section_id
    ok, errs = validate_payload(d)
    assert not ok
    assert any("chart_section_id" in e for e in errs)


@pytest.mark.unit
def test_evidence_multiple_ref_fields_fails() -> None:
    d = _good_payload_dict()
    d["insights"][0]["evidence"][0] = {
        "kind": "chart",
        "caption": "c",
        "chart_section_id": "sec_a",
        "table_artifact_id": "art_b",  # Both fields filled
    }
    ok, errs = validate_payload(d)
    assert not ok
    assert any("multiple ref fields" in e for e in errs)


@pytest.mark.unit
def test_invalid_confidence_fails() -> None:
    d = _good_payload_dict()
    d["insights"][0]["confidence"] = "very_high"
    ok, _errs = validate_payload(d)
    assert not ok


@pytest.mark.unit
def test_recommendation_priority_invalid_fails() -> None:
    d = _good_payload_dict()
    d["recommendations"][0]["priority"] = "URGENT"
    ok, _errs = validate_payload(d)
    assert not ok


# ─────────────────────────────────────────────────────────────
# Defensive parsing: malformed Qwen input
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_from_dict_unknown_sentiment_falls_back_to_neutral() -> None:
    d = _good_payload_dict()
    d["kpis"][0]["sentiment"] = "fantastic"  # Outside the enum
    p = payload_from_dict(d)
    assert p.kpis[0].sentiment == "neutral"


@pytest.mark.unit
def test_from_dict_missing_confidence_defaults_to_med() -> None:
    d = _good_payload_dict()
    d["insights"][0].pop("confidence", None)
    p = payload_from_dict(d)
    assert p.insights[0].confidence == "med"


@pytest.mark.unit
def test_from_dict_extra_keys_ignored() -> None:
    d = _good_payload_dict()
    d["lol_extra"] = {"useless": True}
    d["insights"][0]["nonsense_field"] = 123
    p = payload_from_dict(d)
    ok, errs = validate_payload(p)
    assert ok, errs


# ─────────────────────────────────────────────────────────────
# Integration: actual Qwen output from spike run_01
# ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _SPIKE_RUN.exists(), reason="spike output not present")
@pytest.mark.unit
def test_spike_run_01_validates() -> None:
    """实测真 Qwen 输出能过 validation —— spike 时本来就过了，回归用."""
    raw = json.loads(_SPIKE_RUN.read_text(encoding="utf-8"))
    parsed = raw["parsed"]
    assert parsed is not None
    ok, errs = validate_payload(parsed)
    assert ok, errs
    p = payload_from_dict(parsed)
    assert len(p.insights) >= 1
    # confidence was always None in the spike; from_dict should default to "med".
    for ins in p.insights:
        assert ins.confidence in ("high", "med", "low")


# ─────────────────────────────────────────────────────────────
# canvas-elements: the system guarantees only identity and traceability (tech-design-document-minimalism §2.1).
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_recommendations_and_kpis_are_no_longer_required() -> None:
    """★★用户拍板 2026-09-07：建议与 KPI 是**内容**不是身份，作者按场景决定写不写，
    由 skill 与提示词引导，不由校验器硬拒。

    116 实测 180 天 419 次 subagent_return 拒收里，100 次纯粹因为没写建议、32 次因为没写 KPI，
    每次都是整份 payload 打回重交。硬拒还制造双份：强制填 ⇒ 一定有 ⇒ 一定渲，
    而提示词又教作者在正文末尾写 `## 建议`（312 轮里 50% 的作者真写了，12.8% 两组同时在）。
    把这两个键加回 `required_keys` 这条必红。"""
    payload = _good_payload_dict()
    payload.pop("recommendations")
    payload.pop("kpis")
    ok, errs = validate_payload(payload)
    assert ok, errs
    assert not any("recommendations" in e for e in errs), errs
    assert not any("kpis" in e for e in errs), errs


@pytest.mark.unit
def test_empty_recommendations_list_no_longer_rejected() -> None:
    """★同上，显式空列表也不再拒（`recommendations empty` 那条已删）。"""
    payload = _good_payload_dict()
    payload["recommendations"] = []
    payload["kpis"] = []
    ok, errs = validate_payload(payload)
    assert ok, errs


@pytest.mark.unit
def test_identity_fields_are_still_required() -> None:
    """★正对照之一：身份与「这轮到底分析了什么」的证据照旧必填。
    没有它，上面两条绿了也可能只是因为把整个必填检查删了。"""
    for missing in ("title", "exec_summary", "insights"):
        payload = _good_payload_dict()
        payload.pop(missing)
        ok, errs = validate_payload(payload)
        assert not ok, f"缺 {missing} 竟然通过了"
        assert any(missing in e for e in errs), (missing, errs)


@pytest.mark.unit
def test_recommendation_shape_checks_all_survive() -> None:
    """★正对照之二：**写了就得写对**。形状不检查的话 `["加投华东"]` 这种能过校验、
    却在渲染时被静默丢掉，peer 永远不知道自己写错了（既有注释的理由）。"""
    payload = _good_payload_dict()
    payload["recommendations"] = ["加投华东"]
    ok, errs = validate_payload(payload)
    assert not ok and any("not dict" in e for e in errs), errs

    payload["recommendations"] = [{"priority": "P0", "action": "做事"}]  # P0 without owner
    ok, errs = validate_payload(payload)
    assert not ok and any("owner missing" in e for e in errs), errs

    payload["recommendations"] = [{"priority": "P9", "action": "做事", "owner": "x"}]
    ok, errs = validate_payload(payload)
    assert not ok and any("priority" in e for e in errs), errs

    payload["recommendations"] = [{"priority": "P1", "owner": "x"}]  # Missing action
    ok, errs = validate_payload(payload)
    assert not ok and any("action missing" in e for e in errs), errs
