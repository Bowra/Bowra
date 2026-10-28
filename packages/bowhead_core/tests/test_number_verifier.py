"""number_verifier —— 数字幻觉检测 + 派生规则.

重点覆盖 #79 G7 扩展：单位换算 / 2-step 链式 / 占比乘法.
"""

from __future__ import annotations

import pytest
from bowhead_core.number_verifier import (
    _expand_for_unit_scale,
    _is_derivable,
    build_authorized_numbers,
    verify_numbers_in_text,
)
from bowhead_core.types import ToolInvocation


def _inv(result):
    return ToolInvocation(
        tool_name="bi_ask_question",
        arguments={},
        result=result,
    )


def test_unit_scale_expansion_covers_亿_element() -> None:
    """authorized 集合应涵盖 v 和 v×1e8 两种形式."""
    auth = {165.0}
    out = _expand_for_unit_scale(auth)
    assert 165 in out
    assert 16_500_000_000 in out  # 165 亿
    assert 1_650_000 in out  # 165 万


def test_authorized_numbers_scale_variant_matches() -> None:
    """tool 返 value=16500000000，文本写 '165亿' → 识别为合法."""
    invs = [
        _inv(
            {
                "type": "metric",
                "metrics": [{"name": "GMV", "content": "165亿", "value": 16500000000}],
            }
        )
    ]
    auth = build_authorized_numbers(invs)
    # The authorization set should accept 165 in text (raw matching must also pass without scanning the unit suffix).
    unauth = verify_numbers_in_text("今年 GMV 为 165 亿", auth)
    assert unauth == []


def test_derivable_ratio_percentage() -> None:
    """17.54% = 29亿 / 165亿 × 100 → 识别为合法派生."""
    auth = {29.0, 165.0}
    assert _is_derivable(17.5757, auth)  # 17.58% is within tolerance.


def test_derivable_two_step_chain() -> None:
    """2-step 链式派生 (a-b)/c → 识别.

    e.g. Q2 vs Q1 变化率 = (q2 - q1) / q1  → 带入具体数字.
    """
    a, b = 60.50, 60.45
    # (a-b)/b = 0.000826...
    target = (a - b) / b
    auth = {a, b}
    assert _is_derivable(target, auth)


def test_derivable_fraction_multiplication_exact() -> None:
    """a × {10/20/25/50/75%} 常见占比精确命中（严容差 0.5%）."""
    assert _is_derivable(10.0, {100.0})  # 100 × 10%
    assert _is_derivable(30.0, {120.0})  # 120 × 25%
    assert _is_derivable(75.0, {100.0})  # 100 × 75%


def test_derivable_fraction_strict_not_any_near_value() -> None:
    """1% 偏差不算 derivable（防止 100*0.9=90 → 把 92 当合法）."""
    # 100 × 0.9 = 90; 92 vs 90 differs by 2.2%, exceeding the strict 0.5% tolerance.
    assert not _is_derivable(92.0, {100.0})


def test_unauthorized_hallucination_still_caught() -> None:
    """行业均值 150 亿 没来源，不属任何派生 → 仍判未授权."""
    auth = {165.0, 28.96}
    # 150 matches neither authorized value and cannot derive from 165/28.96.
    unauth = verify_numbers_in_text("行业均值 150 亿", auth)
    assert any(raw == "150" for raw, _ in unauth)


def test_small_integers_ignored() -> None:
    """`第 3 条` / `top 5` 等小整数白名单跳过."""
    auth = {100.0}
    unauth = verify_numbers_in_text("top 5 的前 3 条合规", auth)
    # 5 and 3 are small integers; do not flag them.
    assert unauth == []


def test_year_ignored() -> None:
    auth = {100.0}
    unauth = verify_numbers_in_text("2025 年 和 2024 年对比", auth)
    assert unauth == []


# ── ★ Spacing + faithful-rounding criterion (observed on 2026-08-09) ────────────────────────────
#
# A user saw 「以下数字未在数据返回结果查到：1.38亿」 in a report on 116, while the same report's
# table contained 1.38 and BI's raw value was 138371946.52 (=1.384 亿); writing 1.38亿元 is faithful
# rounding. Root cause: adjacent unit slicing makes the tail of "1.38 亿元" equal " 亿元",
# so startswith("亿") is false ⇒ no multiplier ⇒ remains 1.38 ⇒ absent from the authorization set ⇒ false flag.
# ⇒ Whether a correct number is falsely flagged depends on whether the model inserted that space.

# ⚠ Use exactly one authorized value: multiple values let `_is_derivable` accidentally produce 1.38 through pairwise derivation
# ((a-b)/c, etc., with 2% tolerance), accepting it even without unit conversion and making these tests falsely green.
# Reverting to the spacing bug would not fail them. A single-value set genuinely tests unit conversion alone.
PROD_AUTH = {138371946.5193577}


@pytest.mark.parametrize(
    "text",
    [
        "天津营业额1.38亿元",  # No space
        "天津营业额 1.38 亿元",  # ASCII space (observed form)
        "天津营业额　1.38　亿元",  # Fullwidth space
        "天津营业额 1.38亿元",  # Space only before the number
    ],
)
def test_space_before_unit_does_not_cause_false_flag(text: str) -> None:
    assert verify_numbers_in_text(text, PROD_AUTH) == [], f"正确数字被误标: {text}"


def test_prod_case_two_rounded_values_both_pass() -> None:
    """真机原句：江苏 1.39 亿、天津 1.38 亿，都是合法舍入。"""
    auth = {138371946.5193577, 139400000.0}  # noqa: E501 — Each of the two values corresponds to one figure, not a derivation source.
    assert verify_numbers_in_text("营业额分别为 1.39 亿元和 1.38 亿元", auth) == []


@pytest.mark.parametrize(
    ("stated", "auth_value"),
    [
        ("2 亿元", 184000000.0),  # Zero decimal places: legitimate rounding error can reach 16.7%.
        ("18%", 0.1771),  # Zero-decimal ratio: can reach 25%.
        ("17.7%", 0.1771),
        ("1.4 亿元", 138371946.52),  # One decimal place
    ],
)
def test_coarse_but_faithful_rounding_passes(stated: str, auth_value: float) -> None:
    """★ 合法舍入误差随**书写精度**变化，不随量级 —— 固定容差档必然两头不讨好。

    116 全库 95 条答案实测：同为「亿」量级，2 位小数只需 0.41%、0 位小数需 16.67%。
    """
    assert verify_numbers_in_text(f"本月 {stated}", {auth_value}) == []


def test_fabrication_within_old_5pct_window_is_now_caught() -> None:
    """★ 旧的 rel_tol=0.05 放过这条（差 1.94%）；忠实四舍五入判据必须抓到。

    9.99 不是 9.80 的任何一档有效数字/小数位形式 —— 它是**改了数**，不是圆了数。
    """
    unauth = verify_numbers_in_text("营业额 9.99 亿元", {980000000.0})
    assert [raw for raw, _ in unauth] == ["9.99"]


def test_unrelated_number_still_flagged() -> None:
    assert verify_numbers_in_text("营业额 45.6 亿元", {980000000.0}) != []


# ── Diagnostic logs (directly motivated by 2026-08-10 dogfood on 116) ────────────────────────────
#
# Trigger: a user reported a false flag I could not diagnose afterward; authorized came from runtime
# tool_invocations and was never persisted. Both reconstructions inflated the set and prevented reproducing the old bug.
# This log must answer the three previously missing questions at once: set size / flagged figures / surrounding number text.


def test_flag_emits_diagnostic_log_with_spacing_context(caplog) -> None:
    """★ 上下文窗口必须能看出「数字与单位之间有没有空格」——那是两条不同的代码路径，
    也正是用户那条误标的成因（`1.38 亿元` 被标、同句 `1.39 亿元` 没被标）。
    """
    import logging

    with caplog.at_level(logging.INFO, logger="bowhead_core.number_verifier"):
        out = verify_numbers_in_text("营业额 45.6 亿元，比上月好", {980000000.0}, trace="t_abc")
    assert out, "这个数确实该被标（前置条件）"
    rec = [r.getMessage() for r in caplog.records if "number_guard.flagged" in r.getMessage()]
    assert len(rec) == 1, rec
    msg = rec[0]
    assert "trace=t_abc" in msg
    assert "authorized=" in msg
    assert "45.6" in msg
    assert "亿" in msg, "上下文必须带上单位，否则看不出空格/单位问题"


def test_no_log_when_nothing_flagged(caplog) -> None:
    """★ 防刷屏：本函数是**逐行**调用的（writer loop 对 markdown 每行调一次），
    无条件记会把 prod 日志淹掉。只在真的标记时才记。

    ⚠ 这条**必须成对断言**（`feedback_assert_absence_needs_positive_control`）：
    第一版我只断言"没有日志"，用裸 `logging.Handler` 且没设 level ——
    `logger.info` 压根到不了 handler，于是把日志改成无条件（`if True`）它照样绿。
    **测「没发生」的测试，先得证明「发生了能被抓到」。**
    """
    import logging

    clean = ["本月营业额 9.8 亿元", "环比下降 2%", "详见下表", ""]
    with caplog.at_level(logging.INFO, logger="bowhead_core.number_verifier"):
        for line in clean:
            verify_numbers_in_text(line, {980000000.0, 0.02})
        quiet = [r.getMessage() for r in caplog.records if "number_guard" in r.getMessage()]
        # Positive control: same caplog and level; actual flagging must be captured.
        verify_numbers_in_text("营业额 45.6 亿元", {980000000.0})
        loud = [r.getMessage() for r in caplog.records if "number_guard" in r.getMessage()]
    assert quiet == [], "干净的行不该产生任何日志"
    assert len(loud) == 1, "阳性对照失败：这个测试抓不到日志，那上一条断言就是空的"


def test_trace_is_optional_and_does_not_change_verdict() -> None:
    """trace 只进日志，不许影响判定。"""
    a = verify_numbers_in_text("营业额 45.6 亿元", {980000000.0})
    b = verify_numbers_in_text("营业额 45.6 亿元", {980000000.0}, trace="t_x")
    assert a == b
