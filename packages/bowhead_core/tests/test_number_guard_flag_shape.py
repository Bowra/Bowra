"""被标数字的派生形状分类（feedback-triage-2026-08-24）。

客户的报告顶上挂着「以下数字未在工具返回结果中查到，可能是模型推算或臆觉: 210,079,277」，
而这个数就是同段正文里 Top 3 的求和。现有的 `_is_derivable` 只覆盖**两项**派生，
三项求和不在内。

⚠ 但不能就这么放宽：授权集常有几十个数，允许任意子集求和等于把闸拆了。
⚠⚠ 而误杀率**用现有数据量不出来** —— `authorized` 从不落库（number_verifier 里那段
2026-08-10 的教训写着：事后重建会把集合撑大到什么都可派生）。

所以本轮**零判定改动**，只在标记发生的那一刻就地分类并落日志；这些用例钉住分类本身
靠得住，攒够样本才谈放宽。
"""

from __future__ import annotations

from bowhead_core.number_verifier import classify_flagged_number, verify_numbers_in_text

# Actual numbers from the customer's report.
TOP1 = 146_459_000.0
TOP2 = 32_805_940.0
TOP3 = 30_814_337.0
TOP3_SUM = 210_079_277.0


def test_customer_case_is_recognised_as_a_three_term_sum() -> None:
    """★ 210,079,277 = Top1+Top2+Top3，必须认出来是 sum3 而不是 none。"""
    assert classify_flagged_number(TOP3_SUM, {TOP1, TOP2, TOP3}) == "sum3"


def test_two_term_sum_is_recognised() -> None:
    assert classify_flagged_number(TOP1 + TOP2, {TOP1, TOP2, TOP3}) == "sum2"


def test_unit_scale_mismatch_is_recognised() -> None:
    """差一个 10 的整数次幂 = 单位换算没对上，与「凭空数字」是两回事。"""
    assert classify_flagged_number(TOP1 / 1e4, {TOP1}) == "scaled"


def test_a_genuinely_invented_number_stays_none() -> None:
    """★ 反向对照：真的凭空数字不能被分类器美化成派生量。

    没有这条，分类器可以退化成「永远返 sum3」而测试仍绿，攒出来的数据就是废的。
    """
    assert classify_flagged_number(987_654_321.0, {TOP1, TOP2, TOP3}) == "none"


def test_empty_authorized_set_is_none() -> None:
    assert classify_flagged_number(TOP3_SUM, set()) == "none"


def test_zero_target_is_none() -> None:
    assert classify_flagged_number(0.0, {TOP1}) == "none"


def test_large_authorized_set_does_not_blow_up() -> None:
    """候选集截断后仍应在毫秒级返回（标记路径不能变成性能坑）。"""
    big = {float(i) * 1_000 for i in range(1, 500)}
    assert classify_flagged_number(123_456_789.0, big) in {"none", "scaled", "sum2", "sum3"}


def test_classification_does_not_change_the_verdict() -> None:
    """★★ 本轮**零判定改动** —— 分类只进日志。

    这条钉住：客户那个 sum3 现在**仍然会被标记**（我们只是记下了它的形状）。
    哪天有人顺手把分类接进判定，这条会红，提醒他先看误杀数据。
    """
    authorized = {TOP1, TOP2, TOP3}
    flagged = verify_numbers_in_text(f"Top 3 合计 {TOP3_SUM:,.0f} 支盒。", authorized)
    assert [raw for raw, _ in flagged] == [f"{TOP3_SUM:,.0f}"]
