"""chat-v4 CHANGE-W4.3 — fabrication back-reference helpers."""

from __future__ import annotations

import re

import pytest
from bowhead_core.analysis_payload import (
    _extract_numbers_with_raw,
    _extract_numbers_with_units,
    _render_caveat_number,
    build_observed_numbers,
    check_fabrication_back_reference,
    coerce_verdict_confidence,
)

# ─── _extract_numbers_with_units / unit normalization ────────────────────


@pytest.mark.unit
def test_iso_date_fragments_are_not_reported_as_unverified_numbers() -> None:
    values = _extract_numbers_with_raw("On 2026-01-02 and 2026-02-03, the result was -9.5万元.")
    assert [raw for _, raw in values] == ["-9.5万"]
    payload = {"insights": [{"thesis": "On 2026-01-02, revenue was -9.5万元."}]}
    warnings = check_fabrication_back_reference(payload, {1.0}, lang="en-US")
    assert warnings and "-9.5" in warnings[0]
    assert "-01" not in warnings[0] and "-02" not in warnings[0]


@pytest.mark.unit
def test_invalid_date_like_text_does_not_hide_numeric_claims() -> None:
    values = _extract_numbers_with_raw("2026-19-99 and -12万元")
    assert any(raw == "-19" for _, raw in values)
    assert any(raw == "-12万" for _, raw in values)


@pytest.mark.unit
def test_unit_normalization_percent() -> None:
    """12% → 0.12, 0.5 百分点 → 0.005."""
    vals = _extract_numbers_with_units("营收增 12% 同比 0.5 百分点")
    # Both should be normalized as fractions
    assert 0.12 in {round(v, 6) for v in vals}
    assert 0.005 in {round(v, 6) for v in vals}


@pytest.mark.unit
def test_unit_normalization_yi_wan() -> None:
    """5 万 → 50000; 3 亿 → 300000000."""
    vals = _extract_numbers_with_units("收入 5 万 / 资产 3 亿")
    rounded = {round(v, 6) for v in vals}
    assert 50_000.0 in rounded
    assert 300_000_000.0 in rounded


@pytest.mark.unit
def test_unit_normalization_thousands_separator() -> None:
    """1,234.5 → 1234.5."""
    vals = _extract_numbers_with_units("总额 1,234.5 元")
    assert 1234.5 in {round(v, 6) for v in vals}


# ─── KPI back-reference match cases ──────────────────────────────────────


@pytest.mark.unit
def test_kpi_match_tool_result() -> None:
    """KPI numbers that round-trip to observed values → no caveat."""
    payload = {
        "kpis": [{"label": "合规率", "value": "11.39%"}],
        "insights": [],
    }
    tool_results = [{"rows": [{"label": "合规率", "value": 0.1139}]}]
    observed = build_observed_numbers(tool_results)
    caveats = check_fabrication_back_reference(payload, observed)
    assert caveats == []


@pytest.mark.unit
def test_kpi_fabricated_added_to_caveats() -> None:
    """KPI value with no back-reference → caveat string emitted."""
    payload = {
        "kpis": [{"label": "营收", "value": "9999 万"}],
        "insights": [],
    }
    tool_results = [{"rows": [{"label": "营收", "value": 100.0}]}]
    observed = build_observed_numbers(tool_results)
    caveats = check_fabrication_back_reference(payload, observed)
    assert len(caveats) == 1
    # Batch 0 0-C: internal field paths must not enter user-visible text; describe locations plainly.
    assert "KPI 1" in caveats[0]
    assert "kpis#" not in caveats[0]


@pytest.mark.unit
def test_empty_observed_returns_no_caveats() -> None:
    """No tool_results at all → cannot diagnose, return []."""
    payload = {"kpis": [{"label": "x", "value": "1%"}], "insights": []}
    caveats = check_fabrication_back_reference(payload, set())
    assert caveats == []


@pytest.mark.unit
def test_insight_narrative_fabricated_number_caveats() -> None:
    payload = {
        "kpis": [],
        "insights": [
            {"title": "i", "thesis": "t", "narrative": "本期 GMV 6789 万元", "evidence": []},
        ],
    }
    observed = build_observed_numbers([{"value": 100.0}])
    caveats = check_fabrication_back_reference(payload, observed, lang="zh-CN")
    assert len(caveats) == 1
    # Batch 0 0-C: describe the location plainly (insight N body); never expose internal paths.
    assert "洞察 1 正文" in caveats[0]
    assert "insights#" not in caveats[0]
    # No language → English fallback, with the same plain location wording.
    english = check_fabrication_back_reference(payload, observed)
    assert len(english) == 1 and "insight 1 narrative" in english[0]
    assert "insights#" not in english[0]


# ─── coerce_verdict_confidence cases ─────────────────────────────────────


@pytest.mark.unit
def test_verdict_coerced_from_source_agreement_high() -> None:
    """Δ ≤ 5% → high."""
    payload = {
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 102.0}},
            ],
            "verdict": {"rows": [{"label": "GMV", "verdict": "agree", "confidence": "low"}]},
        },
    }
    coerce_verdict_confidence(payload, set())
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "high"


@pytest.mark.unit
def test_verdict_coerced_med_spread() -> None:
    """5%-20% spread → med."""
    payload = {
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 115.0}},
            ],
            "verdict": {"rows": [{"label": "GMV", "verdict": "partial", "confidence": "high"}]},
        },
    }
    coerce_verdict_confidence(payload, set())
    # 15% spread → med
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "med"


@pytest.mark.unit
def test_verdict_coerced_low_spread() -> None:
    """> 20% spread → low."""
    payload = {
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 200.0}},
            ],
            "verdict": {"rows": [{"label": "GMV", "verdict": "disagree", "confidence": "high"}]},
        },
    }
    coerce_verdict_confidence(payload, set())
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "low"


@pytest.mark.unit
def test_verdict_capped_by_caveats() -> None:
    """high (from agreement) → med when payload has any caveats."""
    payload = {
        "caveats": ["数据来源 X 拉取超时"],
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 101.0}},
            ],
            "verdict": {"rows": [{"label": "GMV", "verdict": "agree", "confidence": "high"}]},
        },
    }
    coerce_verdict_confidence(payload, set())
    # Δ=1% → would be high, capped to med by caveats present.
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "med"


# ─── chat-v4 W4-fix CTO P0-2 — observed-membership + evidence-count ────


@pytest.mark.unit
def test_verdict_capped_low_when_observed_missing() -> None:
    """P0-2: row.values do NOT match any observed (±2%) → cap to "low",
    overriding the spread-based "high" verdict.

    Agreement spread is 1% → would normally → "high". Observed set is
    {200.0, 300.0} — neither 100 nor 101 within ±2% → cap to "low".
    """
    payload = {
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 101.0}},
            ],
            "verdict": {"rows": [{"label": "GMV", "verdict": "agree", "confidence": "low"}]},
        },
    }
    observed = {200.0, 300.0}  # neither 100 nor 101
    coerce_verdict_confidence(payload, observed)
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "low"


@pytest.mark.unit
def test_verdict_observed_membership_allows_high_when_match() -> None:
    """P0-2 inverse: observed has a value within ±2% of row.values →
    membership check passes, spread "high" stays "high".
    """
    payload = {
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 101.0}},
            ],
            "verdict": {"rows": [{"label": "GMV", "verdict": "agree", "confidence": "low"}]},
        },
    }
    observed = {100.5}  # within ±2% of both 100 and 101
    coerce_verdict_confidence(payload, observed)
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "high"


@pytest.mark.unit
def test_verdict_capped_low_when_partial_mismatch_tightened() -> None:
    """W4-fix r3 tightened: ANY mismatch → cap low (was previously
    NONE match → cap low). If one cited value matches observed but
    another doesn't, the unmatched one is potentially fabricated → cap.
    """
    payload = {
        "multi_source_compare": {
            "rows": [
                # BI=100 has match (observed has 100.5); ext=999 has NO match
                {"label": "GMV", "values": {"BI": 100.0, "ext": 999.0}},
            ],
            "verdict": {"rows": [{"label": "GMV", "verdict": "agree", "confidence": "high"}]},
        },
    }
    observed = {100.5}  # matches 100 only; 999 is fabricated
    coerce_verdict_confidence(payload, observed)
    # Tightened: 999 not in observed → cap low (previously would have stayed high)
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "low"


@pytest.mark.unit
def test_evidence_count_boost() -> None:
    """P0-2 evidence-count: ≥3 evidence_refs → boost med → high."""
    payload = {
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 110.0}},  # 10% spread → med
            ],
            "verdict": {
                "rows": [
                    {
                        "label": "GMV",
                        "verdict": "partial",
                        "confidence": "low",
                        "evidence_refs": ["e1", "e2", "e3", "e4"],  # ≥3
                    }
                ]
            },
        },
    }
    coerce_verdict_confidence(payload, set())
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "high"


@pytest.mark.unit
def test_evidence_count_cap_single_ref() -> None:
    """P0-2 evidence-count: ==1 evidence_ref → cap "high" → "med"."""
    payload = {
        "multi_source_compare": {
            "rows": [
                {"label": "GMV", "values": {"BI": 100.0, "ext": 101.0}},  # 1% → high
            ],
            "verdict": {
                "rows": [
                    {
                        "label": "GMV",
                        "verdict": "agree",
                        "confidence": "low",
                        "evidence_refs": ["only_one"],
                    }
                ]
            },
        },
    }
    # Observed must allow membership for the high to even be considered.
    coerce_verdict_confidence(payload, {100.5})
    # spread → high, but evidence_refs == 1 caps to med.
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "med"


def test_caveat_text_contains_no_bracket_index_that_renders_as_citation() -> None:
    """caveat 正文里不得出现 ``[N]`` —— 前端会把它渲成可点的引用 chip。

    116 真机实测（s_8c97c4c375a9）：位置标签写作 ``insights[1].thesis`` 时，那个 ``[1]``
    被 `lib/md.ts` 的 rehypeCardCitations 渲成指向「来源1」的 chip ——**一条「这些数字在
    工具结果中未找到对应来源」的警告，自己挂着一个假引用**，点进去是不相干的来源。
    （该渲染器只对 code/pre 豁免，普通散文照渲。）

    本条钉的是**用户可见文本**这个契约，不是某个具体分隔符：换回方括号即红。
    """
    payload = {
        "kpis": [{"label": "斜率", "value": "83.885 箱/月"}],
        "insights": [{"thesis": "增长 41.7%", "narrative": "环比 -0.029 个百分点"}],
    }
    caveats = check_fabrication_back_reference(payload, {1.0, 2.0})
    assert caveats, "构造的 payload 应触发 caveat，否则本条没测到东西"
    assert not re.search(r"\[\d+\]", caveats[0]), caveats[0]


# ─── Caveat numbers: never use scientific notation (feedback-triage-2026-08-26) ───


@pytest.mark.unit
def test_caveat_number_never_scientific() -> None:
    """116 真机原症：报告底部印出 `4.28e+09`。

    判据写成「不含 e+」这个**不变式**而不是钉死某个字面 —— 换千分位/换小数位
    都该继续成立，只有科学计数法回归才红。
    """
    for v in (4.28e9, 2.10079e8, 1e15 - 1, 123456789.5, -4.28e9):
        out = _render_caveat_number(v)
        assert "e+" not in out.lower(), f"{v} 渲成了科学计数法: {out}"


@pytest.mark.unit
def test_caveat_number_renders_readable() -> None:
    assert _render_caveat_number(4.28e9) == "4,280,000,000"
    assert _render_caveat_number(210079277.0) == "210,079,277"
    assert _render_caveat_number(0.14) == "0.14"
    assert _render_caveat_number(-0.029) == "-0.029"
    assert _render_caveat_number(1234.5) == "1,234.5"
    assert _render_caveat_number(0.0) == "0"


@pytest.mark.unit
def test_caveat_number_tiny_value_not_rendered_as_zero() -> None:
    """比 1e-6 还小也不能渲成 "0" —— 那是在谎报一个**不同的**数。"""
    out = _render_caveat_number(1e-8)
    assert out not in {"0", "-0"}
    assert "e-" not in out.lower()


@pytest.mark.unit
def test_fabrication_caveat_end_to_end_has_no_exponent() -> None:
    """穿过真正的 caveat 组装路径，不是只测 helper（判据必须在闸本身上）。"""
    payload = {
        "kpis": [],
        "insights": [{"thesis": "预计营业额约 4.28 亿元", "narrative": ""}],
    }
    caveats = check_fabrication_back_reference(payload, observed={1.0}, lang="zh-CN")
    assert caveats, "该轮应当判出未核实数字"
    joined = " ".join(caveats)
    assert "e+" not in joined.lower(), joined
    # 「4.28 亿」 normalizes to 4.28e8; old `%g` renders that magnitude as `4.28e+08`; this test guards that original symptom.
    # Since 2026-09-09, caveats show the **original body literal**, not normalized values: 122 numbers were flagged across the 116 database,
    # but 90 (73%) could not be found in the body: users saw 「1,380,000,000」 while the body said 「13.8 亿」, preventing comparison.
    assert "4.28 亿" in joined, joined
    assert "428,000,000" not in joined, "别再印归一化值——读者要能在正文里搜到它"
