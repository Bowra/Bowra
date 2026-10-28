"""Phase A — payload_auto_fix.py UT.

Sprint agent-redesign-1 (2026-05-15).

Each soft rule R4-R20+ has:
  - 1 fixer-fires test asserting the exact transform.
  - 1 fixer-skips test asserting valid input passes through unchanged.

Plus:
  - R7 hard reject when all insights dropped.
  - R24 anti-shell (which stays hard) NOT mutated by auto-fix.
  - Combined multi-rule payload single-pass test.
  - Replay test for prod trace t_06b59114-shaped payload.
"""

from __future__ import annotations

import copy

import pytest
from bowhead_core.payload_auto_fix import (
    AutoFixResult,
    FixRecord,
    auto_fix_payload,
    dedup_fixes_per_rule,
)

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _base_valid_payload() -> dict:
    """A minimum valid payload (passes existing validate_payload)."""
    return {
        "title": "测试报告",
        "exec_summary": "执行摘要",
        "kpis": [{"label": "营收", "value": "100万元", "sentiment": "good"}],
        "insights": [
            {
                "title": "洞察1",
                "thesis": "Q3 营收增长 8.5%",
                "narrative": "Q3 营收较上一季度增长 8.5%, 主要由华东区销售拉动, 其中上海与江苏贡献了大部分增量, 这与新签客户数同步增加吻合。",
                "evidence": [
                    {
                        "kind": "callout",
                        "caption": "数据来源",
                        "callout_text": "来自 bi_query 2025Q3 销售汇总.",
                    }
                ],
                "confidence": "high",
            },
        ],
        "recommendations": [
            {"priority": "P1", "action": "维持华东渠道激励"},
        ],
    }


def _rule_fired(res: AutoFixResult, rule: str) -> bool:
    return any(f.rule == rule for f in res.fixes)


def _rule_actions(res: AutoFixResult, rule: str) -> list[str]:
    return [f.action for f in res.fixes if f.rule == rule]


# ─────────────────────────────────────────────────────────────
# R4 — bail kind coerce
# ─────────────────────────────────────────────────────────────


def test_r4_bail_kind_unknown_coerced_to_tool_failure() -> None:
    p = {"error": {"kind": "weird", "message": "test"}}
    res = auto_fix_payload(p)
    assert not res.hard_reject
    assert p["error"]["kind"] == "tool_failure"
    assert _rule_fired(res, "R4")


def test_r4_bail_kind_valid_unchanged() -> None:
    p = {"error": {"kind": "domain_mismatch", "message": "ws 无对应"}}
    res = auto_fix_payload(p)
    assert p["error"]["kind"] == "domain_mismatch"
    assert not _rule_fired(res, "R4")


# ─────────────────────────────────────────────────────────────
# R5 — bail message synth
# ─────────────────────────────────────────────────────────────


def test_r5_bail_message_empty_synthesized() -> None:
    p = {"error": {"kind": "tool_failure", "message": ""}}
    res = auto_fix_payload(p)
    assert p["error"]["message"]
    assert "auto-synthesized R5" in p["error"]["message"]
    assert _rule_fired(res, "R5")


# ─────────────────────────────────────────────────────────────
# R6 — top-level defaults synth (title/exec_summary)
# ─────────────────────────────────────────────────────────────


def test_r6_missing_title_synthesized() -> None:
    p = _base_valid_payload()
    del p["title"]
    res = auto_fix_payload(p)
    assert p["title"] == ""
    assert _rule_fired(res, "R6")


def test_r6_present_title_unchanged() -> None:
    p = _base_valid_payload()
    res = auto_fix_payload(p)
    assert p["title"] == "测试报告"
    assert not _rule_fired(res, "R6")


# ─────────────────────────────────────────────────────────────
# R8 — insights count > 7 truncate
# ─────────────────────────────────────────────────────────────


def test_r8_8_insights_truncated_to_7() -> None:
    p = _base_valid_payload()
    p["insights"] = [copy.deepcopy(p["insights"][0]) for _ in range(8)]
    res = auto_fix_payload(p)
    assert len(p["insights"]) == 7
    assert _rule_fired(res, "R8")
    assert "8→7" in next(f.original_excerpt for f in res.fixes if f.rule == "R8")


def test_r8_3_insights_unchanged_no_min() -> None:
    """PRD r1 said [3,7] range; real code is [1,7]; r2 confirms <3 no longer fires."""
    p = _base_valid_payload()
    p["insights"] = [copy.deepcopy(p["insights"][0]) for _ in range(3)]
    res = auto_fix_payload(p)
    assert len(p["insights"]) == 3
    assert not _rule_fired(res, "R8")


def test_r8_1_insight_unchanged() -> None:
    p = _base_valid_payload()
    assert len(p["insights"]) == 1
    res = auto_fix_payload(p)
    assert len(p["insights"]) == 1
    assert not _rule_fired(res, "R8")


# ─────────────────────────────────────────────────────────────
# R9 — insights[i] missing required keys drop
# ─────────────────────────────────────────────────────────────


def test_r9_insight_missing_thesis_dropped() -> None:
    p = _base_valid_payload()
    p["insights"].append({"title": "incomplete", "narrative": "x" * 50, "evidence": []})
    # ↑ missing thesis
    res = auto_fix_payload(p)
    assert len(p["insights"]) == 1
    assert _rule_fired(res, "R9")


def test_r9_all_invalid_insights_drop_triggers_r7_hard_reject() -> None:
    p = _base_valid_payload()
    p["insights"] = [
        {"title": "x", "evidence": []},  # missing thesis + narrative
        {"foo": "bar"},  # missing all
    ]
    res = auto_fix_payload(p)
    assert res.hard_reject
    assert "R7" in res.hard_reject_reason


# ─────────────────────────────────────────────────────────────
# R10 — evidence count
# ─────────────────────────────────────────────────────────────


def test_r10_evidence_empty_synthesizes_callout_from_thesis() -> None:
    p = _base_valid_payload()
    p["insights"][0]["evidence"] = []
    res = auto_fix_payload(p)
    ev = p["insights"][0]["evidence"]
    assert len(ev) == 1
    assert ev[0]["kind"] == "callout"
    assert ev[0]["_synthetic"] is True
    assert _rule_fired(res, "R10")


def test_r10_evidence_count_6_truncated_to_5() -> None:
    p = _base_valid_payload()
    p["insights"][0]["evidence"] = [{"kind": "callout", "caption": f"e{i}", "callout_text": f"text {i}"} for i in range(6)]
    res = auto_fix_payload(p)
    assert len(p["insights"][0]["evidence"]) == 5
    assert _rule_fired(res, "R10")


# ─────────────────────────────────────────────────────────────
# R11 — evidence kind unknown coerce to callout
# ─────────────────────────────────────────────────────────────


def test_r11_unknown_kind_coerced_to_callout() -> None:
    p = _base_valid_payload()
    p["insights"][0]["evidence"][0]["kind"] = "weird_kind"
    res = auto_fix_payload(p)
    ev = p["insights"][0]["evidence"][0]
    assert ev["kind"] == "callout"
    assert ev["_synthetic"] is True
    assert _rule_fired(res, "R11")


# ─────────────────────────────────────────────────────────────
# R12 — evidence caption synth
# ─────────────────────────────────────────────────────────────


def test_r12_missing_caption_synthesized_from_kind() -> None:
    p = _base_valid_payload()
    p["insights"][0]["evidence"][0]["caption"] = ""
    res = auto_fix_payload(p)
    assert p["insights"][0]["evidence"][0]["caption"] == "备注"
    assert _rule_fired(res, "R12")


# ─────────────────────────────────────────────────────────────
# R13 — kind-specific field missing drop
# ─────────────────────────────────────────────────────────────


def test_r13_chart_missing_section_id_dropped() -> None:
    p = _base_valid_payload()
    p["insights"][0]["evidence"] = [
        {"kind": "chart", "caption": "图表"},  # missing chart_section_id, no salvageable text
        {"kind": "callout", "caption": "ok", "callout_text": "ok"},
    ]
    res = auto_fix_payload(p)
    ev = p["insights"][0]["evidence"]
    assert len(ev) == 1
    assert ev[0]["kind"] == "callout"
    assert _rule_fired(res, "R13")


def test_r13c_computation_missing_id_with_callout_text_converted_to_callout() -> None:
    """0.39.7 #7 — peer wrote real prose but forgot computation_artifact_id.

    Old R13 would drop the entry → R10 synth thinner callout from thesis →
    ξ density gate then reject for missing markers. New R13-c converts
    in-place, preserving the analytical prose.
    """
    p = _base_valid_payload()
    p["insights"][0]["evidence"] = [
        {
            "kind": "computation",
            "caption": "中位数衍生",
            "callout_text": "Pearson 相关系数 0.72，表明 GMV 与 margin 强正相关",
        },
    ]
    res = auto_fix_payload(p)
    ev = p["insights"][0]["evidence"]
    assert len(ev) == 1
    assert ev[0]["kind"] == "callout"
    assert "Pearson" in ev[0]["callout_text"]
    assert _rule_fired(res, "R13-c")


def test_r13c_chart_missing_id_with_callout_text_converted() -> None:
    """Same convert rule applies to chart kind too."""
    p = _base_valid_payload()
    p["insights"][0]["evidence"] = [
        {
            "kind": "chart",
            "caption": "GMV 排名",
            "callout_text": "女装 ¥6.25B 占据 portfolio GMV 头部，远超其他品类",
        },
    ]
    res = auto_fix_payload(p)
    ev = p["insights"][0]["evidence"]
    assert len(ev) == 1
    assert ev[0]["kind"] == "callout"
    assert _rule_fired(res, "R13-c")


def test_r13c_thin_callout_text_falls_back_to_drop() -> None:
    """≤20 char callout_text isn't substantive enough — drop, don't salvage."""
    p = _base_valid_payload()
    p["insights"][0]["evidence"] = [
        {"kind": "computation", "caption": "x", "callout_text": "短"},  # 1 char
        {"kind": "callout", "caption": "ok", "callout_text": "ok"},
    ]
    res = auto_fix_payload(p)
    ev = p["insights"][0]["evidence"]
    assert len(ev) == 1  # short one dropped
    assert ev[0]["kind"] == "callout"
    assert _rule_fired(res, "R13")
    # R13-c should NOT fire on the short one
    fired_rules = {f.rule for f in res.fixes}
    assert "R13-c" not in fired_rules


# ─────────────────────────────────────────────────────────────
# R14 — narrative length
# ─────────────────────────────────────────────────────────────


def test_r14_narrative_too_long_truncated_at_sentence() -> None:
    # After deciding on default ghostwriting, the hard limit increased to 1200 via _NARR_MAX (R14 shares the validator's source).
    p = _base_valid_payload()
    long = "短句子。" * 400 + "结尾。"
    p["insights"][0]["narrative"] = long[:1300]
    res = auto_fix_payload(p)
    narr = p["insights"][0]["narrative"]
    assert 30 <= len(narr) <= 1200
    assert _rule_fired(res, "R14")


def test_r14_narrative_too_short_padded() -> None:
    p = _base_valid_payload()
    p["insights"][0]["narrative"] = "短文。"  # 3 chars
    res = auto_fix_payload(p)
    narr = p["insights"][0]["narrative"]
    assert len(narr) >= 30
    assert _rule_fired(res, "R14")


def test_r14_narrative_446_chars_t_260ecd81_replay() -> None:
    """Replay-class: t_06b59114 narrative was 446 chars——旧 400 上限时代被截 12%。
    缺省代笔拍板（2026-08-13）后上限 1200：这类真实长度的解读**原样保留**，
    不再为省字数砍模型的分析（当年砍掉的正是结论段）。"""
    p = _base_valid_payload()
    seed = (
        "华东区 2025H2 销售达成金额同比下滑 12.4%，其中 LV1 类别 Destination 下滑 46.3%，RFC-VBP 下滑 7.7%，受供应链与渠道库存周期共同影响整体表现。"
    )
    # repeat seed until ≥446 chars then truncate
    long_narr = (seed * 10)[:446]
    assert len(long_narr) == 446
    p["insights"][0]["narrative"] = long_narr
    res = auto_fix_payload(p)
    assert not res.hard_reject
    assert not _rule_fired(res, "R14")  # 446 ≤ 1200 → preserve unchanged
    assert p["insights"][0]["narrative"] == long_narr


# ─────────────────────────────────────────────────────────────
# R15 — confidence coerce
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inp,out",
    [
        ("h", "high"),
        ("HIGH", "high"),
        ("medium", "med"),
        ("MED", "med"),
        ("m", "med"),
        ("l", "low"),
        ("LOW", "low"),
        ("garbage", "med"),
    ],
)
def test_r15_confidence_coerce(inp: str, out: str) -> None:
    p = _base_valid_payload()
    p["insights"][0]["confidence"] = inp
    res = auto_fix_payload(p)
    assert p["insights"][0]["confidence"] == out
    assert _rule_fired(res, "R15")


# ─────────────────────────────────────────────────────────────
# R17 — priority coerce
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inp,out",
    [
        ("p0", "P0"),
        ("critical", "P0"),
        ("high", "P0"),
        ("p1", "P1"),
        ("med", "P1"),
        ("medium", "P1"),
        ("p2", "P2"),
        ("low", "P2"),
        ("urgent", "P1"),  # unknown → P1
    ],
)
def test_r17_priority_coerce(inp: str, out: str) -> None:
    p = _base_valid_payload()
    p["recommendations"][0]["priority"] = inp
    p["recommendations"][0]["owner"] = "市场部"  # prevent R19 demotion on P0 cases
    res = auto_fix_payload(p)
    assert p["recommendations"][0]["priority"] == out
    assert _rule_fired(res, "R17")


# ─────────────────────────────────────────────────────────────
# R18 — rec missing action drop
# ─────────────────────────────────────────────────────────────


def test_r18_recommendation_missing_action_dropped() -> None:
    p = _base_valid_payload()
    p["recommendations"].append({"priority": "P1"})  # missing action
    res = auto_fix_payload(p)
    assert len(p["recommendations"]) == 1
    assert _rule_fired(res, "R18")


# ─────────────────────────────────────────────────────────────
# R19 — P0 missing owner demote
# ─────────────────────────────────────────────────────────────


def test_r19_p0_no_owner_demoted_to_p1() -> None:
    p = _base_valid_payload()
    p["recommendations"][0]["priority"] = "P0"
    # no owner field
    res = auto_fix_payload(p)
    assert p["recommendations"][0]["priority"] == "P1"
    assert _rule_fired(res, "R19")


def test_r19_p0_with_owner_unchanged() -> None:
    p = _base_valid_payload()
    p["recommendations"][0]["priority"] = "P0"
    p["recommendations"][0]["owner"] = "市场部"
    res = auto_fix_payload(p)
    assert p["recommendations"][0]["priority"] == "P0"
    assert not _rule_fired(res, "R19")


# ─────────────────────────────────────────────────────────────
# R20 — sentiment coerce
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inp,out",
    [
        ("pos", "good"),
        ("positive", "good"),
        ("GOOD", "good"),
        ("neg", "bad"),
        ("negative", "bad"),
        ("BAD", "bad"),
        ("neu", "neutral"),
        ("garbage", "neutral"),
    ],
)
def test_r20_sentiment_coerce(inp: str, out: str) -> None:
    p = _base_valid_payload()
    p["kpis"][0]["sentiment"] = inp
    res = auto_fix_payload(p)
    assert p["kpis"][0]["sentiment"] == out
    assert _rule_fired(res, "R20")


# ─────────────────────────────────────────────────────────────
# R24 (HARD) — narrative anti-shell NOT mutated by auto-fix
# ─────────────────────────────────────────────────────────────


def test_r24_NA_keyword_removed_per_r2() -> None:
    """Sprint r2: 'NA' / 'N/A' / 'n/a' removed from anti-shell keywords.

    Real prod trace t_06b59114 wrote '-NA%' meaning '无可比期'. Should pass now.
    """
    from bowhead_core.analysis_payload import _SHELL_PLACEHOLDER_PATTERNS

    assert "NA" not in _SHELL_PLACEHOLDER_PATTERNS
    assert "N/A" not in _SHELL_PLACEHOLDER_PATTERNS
    assert "n/a" not in _SHELL_PLACEHOLDER_PATTERNS


def test_r24_legit_dash_NA_prose_passes_validate() -> None:
    """t_06b59114 replay — '-NA%' phrasing should pass anti-shell now."""
    from bowhead_core.analysis_payload import validate_payload

    p = _base_valid_payload()
    p["insights"][0]["narrative"] = "Q3 营收 12,345,357 元（-NA% 同比，因 2024 年同期未运营无可比期），环比 +8.2%，主要受华东渠道驱动。整体趋势健康。"
    ok, errs = validate_payload(p)
    assert ok, f"expected pass, got errs: {errs}"


def test_r24_real_shell_keyword_still_rejected() -> None:
    """暂无 / 待计算 still hard-reject narrative."""
    from bowhead_core.analysis_payload import validate_payload

    p = _base_valid_payload()
    p["insights"][0]["narrative"] = "营收 待计算 中，待数据 加载后更新。" * 5
    ok, errs = validate_payload(p)
    assert not ok
    assert any("待计算" in e or "待数据" in e or "narrative" in e.lower() for e in errs)


# ─────────────────────────────────────────────────────────────
# R25 — shell impact note drop
# ─────────────────────────────────────────────────────────────


def test_r25_shell_impact_note_drops_impact() -> None:
    p = _base_valid_payload()
    p["insights"][0]["impact"] = {"note": "待计算 估计值", "scale": "大"}
    res = auto_fix_payload(p)
    assert "impact" not in p["insights"][0]
    assert _rule_fired(res, "R25")


# ─────────────────────────────────────────────────────────────
# Combined multi-rule single-pass
# ─────────────────────────────────────────────────────────────


def test_combined_6_simultaneous_soft_violations_single_pass() -> None:
    p = {
        "title": "",
        # missing exec_summary → R6
        "kpis": [{"label": "x", "value": "1", "sentiment": "POS"}],  # R20
        "insights": [
            {
                "title": "i1",
                "thesis": "claim",
                "narrative": "短文",  # R14 <30
                "evidence": [],  # R10 empty → synth callout
                "confidence": "h",  # R15
            },
        ],
        "recommendations": [
            {"priority": "p0", "action": "do X"},  # R17 + R19 (no owner→P1)
        ],
    }
    res = auto_fix_payload(p)
    assert not res.hard_reject
    # All 6 rules fired
    fired = {f.rule for f in res.fixes}
    assert {"R6", "R20", "R14", "R10", "R15", "R17"}.issubset(fired)
    # P0 with no owner first coerced to P0 (R17), then demoted to P1 (R19)
    # OR went directly to P1 via R17 's "p0"→"P0" then R19 demote.
    assert p["recommendations"][0]["priority"] == "P1"
    # Narrative padded
    assert len(p["insights"][0]["narrative"]) >= 30
    # Synthetic callout marked
    assert p["insights"][0]["evidence"][0]["_synthetic"] is True


# ─────────────────────────────────────────────────────────────
# Dedup / observability
# ─────────────────────────────────────────────────────────────


def test_dedup_fixes_per_rule_keeps_first() -> None:
    fixes = [
        FixRecord(rule="R20", field_path="kpis[0]", action="coerce", original_excerpt="a"),
        FixRecord(rule="R20", field_path="kpis[1]", action="coerce", original_excerpt="b"),
        FixRecord(rule="R14", field_path="insights[0]", action="truncate"),
    ]
    out = dedup_fixes_per_rule(fixes)
    assert len(out) == 2
    assert out[0].rule == "R20" and out[0].field_path == "kpis[0]"  # first kept
    assert out[1].rule == "R14"


# ─────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────


def test_fully_valid_payload_no_fixes() -> None:
    p = _base_valid_payload()
    res = auto_fix_payload(p)
    assert not res.hard_reject
    assert res.fixes == []


def test_auto_fix_is_idempotent_when_rerun() -> None:
    p = _base_valid_payload()
    p["kpis"][0]["sentiment"] = "POSITIVE"
    res1 = auto_fix_payload(p)
    assert _rule_fired(res1, "R20")
    res2 = auto_fix_payload(p)
    assert res2.fixes == []  # already fixed


# ─────────────────────────────────────────────────────────────
# delivery-surface C6: downgrade delivery_surface to a suggestion
# ─────────────────────────────────────────────────────────────


def test_chat_declaration_is_not_downgraded_by_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """批 5ⓐ（用户拍板 2026-08-20）：写了 chat 就是 chat——**不再**因为「超出短答边界」
    （长摘要 / 多 KPI）被降级成 report。长文在聊天里难读是 agent 的判断依据，不是后端的判据。
    116 t_0f77a0a0bd3b 那个形状（多 KPI 无 insights）现在原样通过。"""
    p = {
        "delivery_surface": "chat",
        "exec_summary": "近3月各品牌销售额与利润率对比…" * 50,
        "kpis": [{"label": f"k{i}", "value": str(i)} for i in range(8)],
    }
    res = auto_fix_payload(p)
    assert res.payload.get("delivery_surface") == "chat"
    assert not _rule_fired(res, "C6")


def test_c6_five_kpi_lookup_stays_chat_after_widening() -> None:
    """review #3 锚定诉求：「3-5 个查数」是 chat 短答，不该被窗口卡成报告
    （旧窗口 KPI≤2 时 5 个 KPI 会掉出豁免 → C6 降级 → 画布报告）。"""
    p = {
        "delivery_surface": "chat",
        "exec_summary": "五个品牌本月销售额：A 1.2亿 / B 0.9亿 / C 0.7亿 / D 0.5亿 / E 0.3亿。",
        "kpis": [{"label": f"品牌{i}", "value": f"{i}亿"} for i in range(5)],
    }
    res = auto_fix_payload(p)
    assert res.payload.get("delivery_surface") == "chat"
    assert not _rule_fired(res, "C6")


def test_c6_bounded_chat_surface_untouched() -> None:
    """正对照：合形状的短答不降级——delivery_surface 保留，走 DF-04 短路。"""
    p = {"delivery_surface": "chat", "exec_summary": "今年GMV 5.2 亿"}
    res = auto_fix_payload(p)
    assert res.payload.get("delivery_surface") == "chat"
    assert not _rule_fired(res, "C6")
    assert not res.hard_reject


def test_c6_report_payload_without_surface_unchanged() -> None:
    """无 delivery_surface 的报告 payload 与 C6 之前逐字节同行为。"""
    p = _base_valid_payload()
    res = auto_fix_payload(p)
    assert not _rule_fired(res, "C6")
    assert res.fixes == []


# Normalize escaped prose newlines before figure-anchor processing.


def test_n1_escaped_newline_restores_the_figure_anchor_to_its_own_line() -> None:
    """The exact production shape: anchors buried between literal `\\n` never render.

    `md.ts:167` matches an anchor at END OF LINE. With the two-character `\\n` the
    whole summary is one line, the anchor has text after it, and the frontend takes
    the "mid-sentence anchor" branch = strip, no chart. Four charts, none shown.
    """
    payload = {
        "delivery_surface": "chat",
        "exec_summary": "示例产品销售额2.50亿元，同比下降4.2%。\\n[figure:sec_aaaaaaaaaaaa]\\n渠道上，Hospital为主渠道。",
    }
    res = auto_fix_payload(payload)

    lines = res.payload["exec_summary"].splitlines()
    assert "[figure:sec_aaaaaaaaaaaa]" in lines, "anchor must sit alone on its own line"
    assert "\\n" not in res.payload["exec_summary"]
    assert [f.rule for f in res.fixes] == ["N1"]
    assert res.fixes[0].field_path == "exec_summary"


def test_n1_reaches_every_prose_field_including_the_short_answer_exit() -> None:
    payload = {
        "delivery_surface": "chat",
        "exec_summary": "结论。\\n细节。",
        "insights": [
            {"title": "A\\nB", "thesis": "t\\nu", "narrative": "n\\nm", "evidence": [{"kind": "chart", "caption": "c\\nd", "chart_ref": "sec_1"}]}
        ],
        "recommendations": [{"action": "先查渠道\\n再查品规"}],
        "caveats": ["9月未完整\\n按截点理解"],
    }
    res = auto_fix_payload(payload)
    p = res.payload
    assert "\\n" not in p["exec_summary"]
    assert "\\n" not in p["insights"][0]["title"] and "\\n" not in p["insights"][0]["narrative"]
    assert "\\n" not in p["insights"][0]["evidence"][0]["caption"]
    assert "\\n" not in p["recommendations"][0]["action"]
    assert "\\n" not in p["caveats"][0]
    assert {f.rule for f in res.fixes} == {"N1"}


def test_n1_leaves_code_and_data_alone() -> None:
    """Negative control: a `\\n` inside code is content, and KPI values are data."""
    payload = {
        "delivery_surface": "chat",
        "exec_summary": "用 `re.split('\\n', s)` 拆行；见下\n```python\nre.sub('\\n', '', s)\n```",
        "kpis": [{"label": "换行符", "value": "\\n"}],
    }
    before = copy.deepcopy(payload)
    res = auto_fix_payload(payload)
    assert res.payload["exec_summary"] == before["exec_summary"], "code spans/fences must survive verbatim"
    assert res.payload["kpis"][0]["value"] == "\\n", "KPI values are data, not prose"
    assert not [f for f in res.fixes if f.rule == "N1"]


def test_n1_is_a_noop_and_idempotent_on_clean_prose() -> None:
    payload = {"delivery_surface": "chat", "exec_summary": "第一行\n[figure:sec_a]\n第二行"}
    before = copy.deepcopy(payload)
    once = auto_fix_payload(payload)
    twice = auto_fix_payload(once.payload)
    assert twice.payload["exec_summary"] == before["exec_summary"]
    assert not [f for f in once.fixes + twice.fixes if f.rule == "N1"]


def test_n1_spares_a_deliberately_escaped_backslash() -> None:
    """`\\\\n` (escaped backslash + n) is what a peer writes when it means the characters."""
    payload = {"delivery_surface": "chat", "exec_summary": r"路径分隔写作 \\n 两个字符"}
    res = auto_fix_payload(payload)
    assert res.payload["exec_summary"] == r"路径分隔写作 \\n 两个字符"
