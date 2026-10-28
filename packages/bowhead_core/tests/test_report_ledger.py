"""report-consistency-gate v3 — 授权台账确定性核心 (code-review v2 重构后)。

匹配/基数复用 number_verifier(native-int/float + combo + 单位归一);本模块新增 = 对
声明派生量的 formula 确定性重算校验。重点:既接受单位/百分数 scale 变体(`3.57/8.26` 与
`*100` 都出现),又拒绝 C4 那种 38× / 100× magnitude 错;且 `**` 指数炸弹被 AST 拒。
"""

import types

from bowhead_core.number_verifier import build_authorized_numbers
from bowhead_core.report_ledger import (
    _eval_formula,
    build_ledger,
    reconcile_payload,
    unit_class,
    verify_formula,
)


def _inv(text_or_obj, cid="c1"):
    # Real pipeline invocations are objects (.error/.result/.nested); number_verifier reads native numbers.
    return types.SimpleNamespace(call_id=cid, result=text_or_obj, error=None, nested=[])


# Banking: tool output uses **native numbers** (bi_query list[dict]); test F2 native-number extraction.
BANK_ROWS = [
    {"指标": "存款增量", "值": 8.26, "单位": "亿"},
    {"指标": "新客数", "值": 8438, "单位": "户"},
    {"指标": "新客存款增量", "值": 3.57, "单位": "亿"},
    {"指标": "存款余额", "值": 70.17, "单位": "亿"},
    {"指标": "金总余额", "值": 136.38, "单位": "亿"},
]
INVS = [_inv(BANK_ROWS)]
AUTH = build_authorized_numbers(INVS)


def test_unit_class_pp_not_pct():
    assert unit_class("%")[0] == "pct"
    assert unit_class("百分点")[0] == "pp"
    assert unit_class("个百分点")[0] == "pp"  # F6: 个百分点 = pp, not count.
    assert unit_class("%") != unit_class("百分点")  # pp ≠ %
    assert unit_class("亿") == ("cny", 1e8)
    assert unit_class("万元/户")[0] == "cny"  # Compound unit uses numerator.
    assert unit_class("万笔") == ("count", 1e4)
    assert unit_class("") == ("none", 1.0)


def test_eval_formula_arithmetic_and_safety():
    assert abs(_eval_formula("3.57*10000/8438") - 4.231) < 0.01
    assert abs(_eval_formula("70.17/136.38*100") - 51.45) < 0.05
    assert _eval_formula("(6500-5000)/5000*100") == 30.0  # Correct minus (not -5000).
    assert _eval_formula("given") is None
    assert _eval_formula("1/0") is None
    assert _eval_formula("__import__('os')") is None  # Injection rejected.
    # F1(P0): AST rejects ** exponent bomb (Pow not allowed) → None, never eval / huge integer.
    assert _eval_formula("9**9**9") is None
    assert _eval_formula("2**10") is None


def test_verify_formula_C4_correct_accepted():
    assert verify_formula("3.57*10000/8438", 4.23, "万元", AUTH)[0]  # Correct 4.23万 per customer.


def test_verify_formula_C4_wrong_rejected():
    ok, reason = verify_formula("3.57*10000/8438", 163.0, "万元", AUTH)  # Claims 163 (actual 4.23).
    assert not ok and "recompute_mismatch" in reason


def test_verify_formula_100x_magnitude_rejected():
    # F7: claims 423 per customer (=4.23×100, classic 元↔万元 magnitude error); cny allows only ×1 → reject.
    ok, reason = verify_formula("3.57*10000/8438", 423.0, "万元", AUTH)
    assert not ok and "recompute_mismatch" in reason


def test_verify_formula_percent_fraction_and_pct_both_accepted():
    assert verify_formula("3.57/8.26", 43.22, "%", AUTH)[0]  # Fraction notation.
    assert verify_formula("3.57/8.26*100", 43.22, "%", AUTH)[0]  # Percentage notation.
    assert verify_formula("70.17/136.38*100", 51.45, "%", AUTH)[0]


def test_verify_formula_operand_unverified():
    ok, reason = verify_formula("9999.9/2", 4999.95, "", AUTH)  # 9999.9 not authorized.
    assert not ok and reason == "operand_unverified"


def test_verify_formula_unparseable():
    ok, reason = verify_formula("given", 8.26, "亿", AUTH)
    assert not ok and reason == "unparseable"


def test_derived_from_derived_authorized():
    # Combined share = East China + South China shares (both derived); operand authorization includes declared facts.
    gfs = [
        {"key": "e", "label": "华东占比", "value": 50.0, "unit": "%", "formula": "3.2/6.4*100"},
        {"key": "s", "label": "华南占比", "value": 28.125, "unit": "%", "formula": "1.8/6.4*100"},
        {"key": "t", "label": "合计占比", "value": 78.125, "unit": "%", "formula": "50+28.125"},
    ]
    led = build_ledger(gfs, [_inv([{"区域": "华东", "GMV": 3.2}, {"区域": "华南", "GMV": 1.8}, {"区域": "全国", "GMV": 6.4}])])
    assert led.rejected == []


def test_build_ledger_accepts_correct_derived_rejects_C4_and_ghost():
    gfs = [
        {"key": "dep_inc", "label": "存款增量", "value": 8.26, "unit": "亿", "source": "given"},
        {"key": "new_cust", "label": "新客数", "value": 8438, "unit": "户", "source": "given"},
        {"key": "avg", "label": "户均", "value": 4.23, "unit": "万元/户", "formula": "3.57*10000/8438"},  # Correct derivation.
        {"key": "bad_avg", "label": "户均(错)", "value": 163.0, "unit": "万元/户", "formula": "3.57*10000/8438"},  # C4: claims 163.
        {"key": "ghost", "label": "凭空给定", "value": 999.99, "unit": "亿", "source": "given"},
    ]
    led = build_ledger(gfs, INVS)
    rej_keys = {r["key"] for r in led.rejected}
    assert "bad_avg" in rej_keys  # C4 rejected.
    assert "ghost" in rej_keys  # Invented given value rejected.
    assert "avg" not in rej_keys  # Correct derivation accepted.
    assert round(4.23, 6) in led.derived_values  # Correct average enters derived-value set.


# ── reconcile_payload (intra-payload reconciliation) ────────────
_AVG_OK = {"key": "avg", "label": "户均", "value": 4.23, "unit": "万元", "formula": "3.57*10000/8438"}
_RATIO_OK = {"key": "r", "label": "存款/金总", "value": 51.45, "unit": "%", "formula": "70.17/136.38*100"}


def test_reconcile_clean_when_numbers_in_ledger():
    p = {"exec_summary": "新客户均存款 4.23万元，存款占金总比 51.45%。", "kpis": [], "insights": [], "ground_facts": [_AVG_OK, _RATIO_OK]}
    assert reconcile_payload(p, INVS).clean


def test_reconcile_flags_C4_written_number():
    # Narrative claims 163万; ledger per-customer 4.23万 → 163万 unauthorized.
    p = {"exec_summary": "新客户均存款高达 163万元。", "kpis": [], "insights": [], "ground_facts": [_AVG_OK]}
    r = reconcile_payload(p, INVS)
    assert not r.clean
    assert any(abs(u["value"] - 163) < 0.5 for u in r.unauthorized)


def test_reconcile_flags_C3_written_ratio():
    # Narrative claims 43.9%; ledger only 51.45% →43.9% unauthorized.
    p = {"exec_summary": "存款占金总比 43.9%。", "kpis": [], "insights": [], "ground_facts": [_RATIO_OK]}
    r = reconcile_payload(p, INVS)
    assert any(abs(u["value"] - 43.9) < 0.1 for u in r.unauthorized)


def test_reconcile_skips_bare_number_noise():
    # Bare year/month/rank ignored (number_verifier allowlist); unit-bearing data (8.26亿) authorized →clean.
    p = {
        "exec_summary": "2026年1-5月，排名第3，存款增量 8.26亿。",
        "kpis": [],
        "insights": [],
        "ground_facts": [{"key": "d", "label": "增量", "value": 8.26, "unit": "亿", "source": "given"}],
    }
    assert reconcile_payload(p, INVS).clean


def test_reconcile_surfaces_rejected_derived_fact():
    p = {
        "exec_summary": "户均存款见下。",
        "kpis": [],
        "insights": [],
        "ground_facts": [{"key": "avg", "label": "户均", "value": 163.0, "unit": "万元", "formula": "3.57*10000/8438"}],
    }
    r = reconcile_payload(p, INVS)
    assert any(rf["key"] == "avg" for rf in r.rejected_facts)


def test_reconcile_kpi_and_insight_fields_scanned():
    p = {
        "exec_summary": "",
        "ground_facts": [_AVG_OK],
        "kpis": [{"label": "户均", "value": "999万元"}],  # 999万 unauthorized.
        "insights": [{"thesis": "户均 4.23万元", "narrative": "稳健。"}],
    }
    r = reconcile_payload(p, INVS)
    assert any(u["field"].startswith("kpis[0]") and abs(u["value"] - 999) < 1 for u in r.unauthorized)


# ── re-review (2nd pass) regressions ─────────────────────────────────────────
def test_no_scale_expansion_magnitude_blindness():
    # FN-1: raw tool 8.26 does not authorize written 82600元 via 8.26×1e4 (base never scale-expands).
    p = {"exec_summary": "新客户均存款 82600元。", "kpis": [], "insights": [], "ground_facts": []}
    r = reconcile_payload(p, INVS)
    assert any(abs(u["value"] - 82600) < 1 for u in r.unauthorized)


def test_numeric_range_no_spurious_negative():
    # FP-4: range "3-5万元" must not extract fabricated -5万元.
    invs = [_inv([{"lo": 3}, {"hi": 5}])]
    p = {"exec_summary": "增幅区间 3-5万元。", "kpis": [], "insights": [], "ground_facts": []}
    r = reconcile_payload(p, invs)
    assert not any(u["value"] < 0 for u in r.unauthorized)


def test_operand_data_anchor_required():
    # FN-2b: fabricated small-integer product 500*326 has no data anchor → operand_unverified.
    ok, reason = verify_formula("500*326", 163000.0, "元", AUTH)
    assert not ok and reason == "operand_unverified"
    # Data anchor (8438 from tool) + constant 0.5 → accepted.
    assert verify_formula("8438*0.5", 4219.0, "户", AUTH)[0]


def test_zero_operand_not_anchor():
    # P2 (confirm-pass): 99999+0 cannot use 0 as data anchor (no-op constant, not anchor).
    ok, reason = verify_formula("99999+0", 99999.0, "元", AUTH)
    assert not ok and reason == "operand_unverified"
    # Sanity: valid 8438*0.5 still accepted (8438 data anchor).
    assert verify_formula("8438*0.5", 4219.0, "户", AUTH)[0]


def test_pp_rejects_100x_error():
    # pp (percentage points) absolute delta, no fraction↔% ambiguity; 2.1 claimed 210pp rejects (×1 only).
    ok, reason = verify_formula("70.17-68.07", 210.0, "百分点", AUTH)
    assert not ok and "recompute_mismatch" in reason
