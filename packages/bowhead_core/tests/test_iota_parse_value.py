"""Sprint ι (PRD §10.1.1.2): parser CN/EN/percent/double-suffix/ambiguous."""

from __future__ import annotations

import math

import pytest
from bowhead_core.canvas_composers.answer_decomposer import (
    _parse_value_to_float,
    _precision_window,
    _resolve_value,
)


# ─── CN scales (primary) ────────────────────────────────────────
@pytest.mark.parametrize(
    "v,expected",
    [
        ("680万", 6.8e6),
        ("11.18亿", 1.118e9),
        ("12千万", 1.2e8),
        ("5百万", 5e6),
        ("7千", 7e3),
    ],
)
def test_cn_scales(v: str, expected: float) -> None:
    got = _parse_value_to_float(v)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)


# ─── EN scales (future-proof) ───────────────────────────────────
@pytest.mark.parametrize(
    "v,expected",
    [
        ("1.5K", 1500.0),
        ("2.3M", 2.3e6),
        ("1B", 1e9),
        ("1.5k", 1500.0),
    ],
)
def test_en_scales(v: str, expected: float) -> None:
    got = _parse_value_to_float(v)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)


# ─── Percent forms ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "v,expected",
    [
        ("25%", 0.25),
        ("5百分点", 0.05),
        ("30‰", 0.03),
        ("15pct", 0.15),
        ("40percent", 0.4),
    ],
)
def test_percent(v: str, expected: float) -> None:
    got = _parse_value_to_float(v)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)


# ─── Currency-prefix tolerated (stripped) ───────────────────────
@pytest.mark.parametrize(
    "v,expected",
    [
        ("¥6,800", 6800.0),
        ("USD 1234", 1234.0),
        ("€500K", 500000.0),
        ("$1,234.5", 1234.5),
    ],
)
def test_currency_prefix(v: str, expected: float) -> None:
    got = _parse_value_to_float(v)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)


# ─── Thousand separator ─────────────────────────────────────────
@pytest.mark.parametrize(
    "v,expected",
    [
        ("1,234", 1234.0),
        ("1,234,567", 1234567.0),
        # Note: thousand-sep regex requires `\d{3}\b` so trailing CJK suffix doesn't
        # qualify for separator strip in patterns like '6,800万'. Acceptable —
        # CN-style markers don't use thousand-sep in observed corpus.
    ],
)
def test_thousand_sep(v: str, expected: float) -> None:
    got = _parse_value_to_float(v)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)


# ─── Bare numbers ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "v,expected",
    [
        ("680", 680.0),
        ("3.14", 3.14),
        ("-42", -42.0),
    ],
)
def test_bare_numbers(v: str, expected: float) -> None:
    got = _parse_value_to_float(v)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)


# ─── Double-suffix safe-fail (P1 fix #8) ────────────────────────
@pytest.mark.parametrize(
    "v",
    [
        "1k万",  # CN suffix matches first; strips to "1k" → ValueError → None
        "5万K",
        "100M亿",
    ],
)
def test_double_suffix_safe_fail(v: str) -> None:
    assert _parse_value_to_float(v) is None


# ─── Malformed ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "v",
    [
        "abc",
        "1.2.3",
        "",
    ],
)
def test_malformed_returns_none(v: str) -> None:
    assert _parse_value_to_float(v) is None


# ─── _resolve_value ambiguous-window (PRD §10.1.1.4) ────────────
def test_resolve_exact_match() -> None:
    assert _resolve_value(6800000.0, {6800000.0: "tl_a"}) == "tl_a"


def test_resolve_unique_near_match() -> None:
    # 11.18亿 emits 1.118e9; raw 1118302756.0 (off 0.027%, within ±0.5%).
    assert _resolve_value(1.118e9, {1118302756.0: "tl_b"}) == "tl_b"


def test_resolve_two_near_matches_drops() -> None:
    """≥2 keys in tolerance window AND no exact: drop (invariant)."""
    # Neither key is an exact match for 1.118e9 (1118000000.0).
    # Both 1117500000 and 1118500000 fall in ±0.5% window of 1.118e9 (5,590,000).
    m = {1117500000.0: "tl_b", 1118500000.0: "tl_c"}
    assert _resolve_value(1.118e9, m) is None


def test_resolve_zero_match_drops() -> None:
    assert _resolve_value(9999.0, {6800000.0: "tl_a"}) is None


# ─── Sprint ι follow-up (2026-05-09 batch): unit-field hint covers LLM splits ──
# Matrix covers all forms an LLM emits when separating value/unit:
# (a) bare value, scale carried by unit field (`[num:680:万]`)
# (b) decimal value, percent carried by unit (`[num:10.47:percent]`)
# (c) compound CN currency tail in unit (`[num:11.18:亿元]` → strip 元 → 亿 → *1e8)
# (d) EN scales by unit (`[num:6.8:M]`, `[num:1.2:bn]`, `[num:5:T]`)
# (e) financial shorthands (`MM`, `Mn`, `bps`, `pp`)
# (f) ratio-already-in-percent ambiguity (`[num:0.685:percent]` stays 0.685)
# Each row asserts canonical float when LLM splits scale into the `:U` field.
@pytest.mark.parametrize(
    "value,unit,expected",
    [
        # (a) percent integer-form → multiply 0.01
        ("10.47", "percent", 0.1047),
        ("25", "%", 0.25),
        ("100", "pct", 1.0),
        ("12.5", "Percent", 0.125),
        ("3", "PERCENT", 0.03),
        ("2.5", "百分点", 0.025),
        ("4", "个百分点", 0.04),
        ("8", "pp", 0.08),
        # (a') already-ratio (value <1) → don't double-scale
        ("0.685", "percent", 0.685),
        ("0.078", "percent", 0.078),
        ("0.62", "pct", 0.62),
        # per-mille
        ("5", "‰", 0.005),
        ("3", "permille", 0.003),
        # basis points
        ("25", "bp", 0.0025),
        ("150", "bps", 0.015),
        # (b) CN scales via unit
        ("680", "万", 6_800_000.0),
        ("11.18", "亿", 1.118e9),
        ("3", "千万", 3e7),
        ("5.5", "百万", 5_500_000.0),
        ("12", "万亿", 1.2e13),
        # (c) CN scale + currency tail
        ("680", "万元", 6_800_000.0),
        ("11.18", "亿元", 1.118e9),
        ("5", "亿美元", 5e8),
        ("3", "万美元", 3e4),
        ("100", "亿日元", 1e10),
        # (d) EN scales via unit
        ("6.8", "M", 6_800_000.0),
        ("1.2", "B", 1.2e9),
        ("680", "K", 680_000.0),
        ("5", "T", 5e12),
        ("0.5", "B", 5e8),  # decimal + EN scale (no percent ambiguity)
        # (e) Financial shorthands
        ("3.5", "MM", 3_500_000.0),
        ("2", "Mn", 2_000_000.0),
        ("4", "mn", 4_000_000.0),
        ("1.5", "bn", 1.5e9),
        ("3", "Bn", 3e9),
        ("12", "tn", 1.2e13),
        ("100", "million", 1e8),
        ("2.5", "Billion", 2.5e9),
        ("1", "trillion", 1e12),
        # (f) value contains scale already → unit field ignored (in-line wins)
        ("680万", None, 6_800_000.0),
        ("11.18亿", None, 1.118e9),
        ("680万", "CNY", 6_800_000.0),  # CNY in unit not a scale → in-line 万 used
        ("12.5%", "percent", 0.125),  # in-line % wins, unit *0.01 not double-applied
        # (g) Pure currency unit (no scale) — unit doesn't change value
        ("680", "CNY", 680.0),
        ("680", "USD", 680.0),
        ("1234.5", "元", 1234.5),
        # (h) Empty unit fallback
        ("680", None, 680.0),
        ("680", "", 680.0),
        # (i) Unknown unit → bare value preserved (parser doesn't bind, but
        # _resolve_value will miss unless raw value happens to be in map)
        ("680", "count", 680.0),
        ("12.5", "kpi", 12.5),
    ],
)
def test_unit_hint_canonicalization_matrix(
    value: str,
    unit: str | None,
    expected: float,
) -> None:
    got = _parse_value_to_float(value, unit=unit)
    assert got is not None, f"unexpected None for ({value!r}, {unit!r})"
    # Tolerate float arithmetic noise (1e-9 relative).
    assert math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-9), f"({value!r}, {unit!r}) → {got!r}, expected {expected!r}"


def test_unit_hint_safe_fail_on_malformed_value() -> None:
    """Unit hint must not rescue a malformed value group."""
    assert _parse_value_to_float("abc", unit="percent") is None
    assert _parse_value_to_float("1.2.3", unit="万") is None
    assert _parse_value_to_float("", unit="percent") is None


def test_unit_hint_unknown_unit_no_op() -> None:
    """Unknown unit (e.g. 'count', 'kpi') doesn't crash; bare value returned."""
    assert _parse_value_to_float("680", unit="count") == 680.0
    assert _parse_value_to_float("0.62", unit="ratio") == 0.62


# ─── ι dogfood-1 expansion (2026-05-09): A range / B + prefix / C scientific ──


# A: range markers (LLM forecast/estimate uses X-Y form)
@pytest.mark.parametrize(
    "value,unit,expected_lo,expected_hi",
    [
        ("17.0-17.4", "percent", 0.170, 0.174),  # Profit-margin range
        ("17-18", "%", 0.17, 0.18),
        ("8-10", "percent", 0.08, 0.10),  # forecast growth
        ("15-20", "pct", 0.15, 0.20),
        ("100-200", "万", 1_000_000.0, 2_000_000.0),  # CN scale on range
        ("1.2e9-1.5e9", None, 1.2e9, 1.5e9),  # scientific endpoints
        ("3.5-5.0", "M", 3_500_000.0, 5_000_000.0),  # EN scale on range
        ("-3.0--2.5", None, -3.0, -2.5),  # leading-neg + inner range (rare)
    ],
)
def test_range_marker_returns_tuple(
    value: str,
    unit: str | None,
    expected_lo: float,
    expected_hi: float,
) -> None:
    got = _parse_value_to_float(value, unit=unit)
    assert isinstance(got, tuple), f"expected tuple, got {got!r} for ({value!r}, {unit!r})"
    lo, hi = got
    assert math.isclose(lo, expected_lo, rel_tol=1e-9, abs_tol=1e-9)
    assert math.isclose(hi, expected_hi, rel_tol=1e-9, abs_tol=1e-9)


def test_range_resolve_unique_key_in_window() -> None:
    """A 范围 marker: map key 17.31 在 [17.0, 17.4] → bind."""
    f = _parse_value_to_float("17.0-17.4", unit="%")
    assert isinstance(f, tuple)
    _resolve_value(f, {17.31: "tl_a", 99.0: "tl_b"})
    # value goes to fraction (0.17-0.174) so map need 0.17-0.174 range
    f2 = (0.17, 0.174)
    assert _resolve_value(f2, {0.1731: "tl_a", 0.99: "tl_b"}) == "tl_a"


def test_range_resolve_drop_when_ambiguous() -> None:
    """≥2 keys inside range → drop (invariant)."""
    assert _resolve_value((10.0, 20.0), {12.0: "tl_a", 15.0: "tl_b"}) is None


def test_range_resolve_drop_when_no_key() -> None:
    assert _resolve_value((10.0, 20.0), {30.0: "tl_a"}) is None


# B: + prefix tolerance
@pytest.mark.parametrize(
    "value,unit,expected",
    [
        ("+2.38", "percent", 0.0238),  # Year-over-year growth
        ("+1.23", "%", 0.0123),
        ("+0.0199", "percent", 0.0199),  # already-ratio (heuristic)
        ("+15.5", None, 15.5),
        ("+680", "万", 6_800_000.0),
        ("+1.5", "B", 1.5e9),
    ],
)
def test_plus_prefix(value: str, unit: str | None, expected: float) -> None:
    got = _parse_value_to_float(value, unit=unit)
    assert math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-9)


# C: scientific notation
@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.16e9", 1.16e9),  # 11.6 亿
        ("6.8e6", 6.8e6),
        ("1.2E12", 1.2e12),  # uppercase E
        ("-1.5e9", -1.5e9),  # neg + sci
        ("+2.5e8", 2.5e8),  # + + sci
        ("1e3", 1000.0),
        ("3.14e-3", 0.00314),  # negative exponent
    ],
)
def test_scientific_notation(value: str, expected: float) -> None:
    got = _parse_value_to_float(value)
    assert math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_scientific_with_currency_unit() -> None:
    """[num:1.16e9:CNY] — sci + currency unit. unit doesn't change value."""
    assert _parse_value_to_float("1.16e9", unit="CNY") == 1.16e9


# Edge: range with malformed endpoint → safe-fail (don't crash)
def test_range_malformed_endpoint_safe_fail() -> None:
    # "abc-3.0" doesn't match range regex (lo not numeric); falls to bare path → ValueError → None
    assert _parse_value_to_float("abc-3.0") is None
    # Single-number look-alike (just leading neg) shouldn't be treated as range
    assert _parse_value_to_float("-3.5") == -3.5


# ─── Thousands separators + units (`\b` fails before CJK/letters) ─────────────
@pytest.mark.parametrize(
    "v,expected",
    [
        ("43,964万", 4.3964e8),
        ("1,234亿", 1.234e11),
        ("1,234千", 1.234e6),
        ("1,234K", 1.234e6),
        ("1,234M", 1.234e9),
        ("-1,234万", -1.234e7),
        ("+1,234万", 1.234e7),
        ("1,234,567万", 1.234567e10),
    ],
)
def test_thousand_sep_before_a_unit(v: str, expected: float) -> None:
    """千分位后面紧跟单位时逗号也必须被去掉。

    原实现用 `(?=\\d{3}\\b)` 收尾，而 CJK 与 ASCII 字母在 Python `re` 里都算 `\\w`
    ⇒ `43,964万` 里 `4` 和 `万` 之间**没有词边界** ⇒ 逗号留着 ⇒ `float("43,964")`
    ⇒ ValueError ⇒ 返 None ⇒ **这个数字绑不上引用**。
    `43,964万元` / `1,234亿` / `1,234K` 全是 BI 报告最标准的写法，正文与表格同样中招。
    """
    got = _parse_value_to_float(v)
    assert got is not None, f"{v!r} 解析失败 —— 千分位没被去掉"
    assert math.isclose(got, expected, rel_tol=1e-9)


@pytest.mark.parametrize("v", ["1,2345", "12,34", "1,23", "1,2345万"])
def test_malformed_thousand_grouping_still_rejected(v: str) -> None:
    """反向保险：放开的只是「后面不是数字」，畸形分组不得被当成千分位。

    收尾判据从 `\\b` 换成 `(?!\\d)` 时最容易连带放宽的就是这一类。
    """
    assert _parse_value_to_float(v) is None, f"{v!r} 被当成了合法千分位"


# ─── Precision window = half the last unit (driven by 116 dogfood) ────────────
#
# ⚠ All `_resolve_value` cases above **omit `raw_value_str`**, taking
# `if not v_str`'s ±2% fallback; the integer/decimal branches of `_precision_window`
# previously had **zero coverage**. The cases below specifically target that branch.

# A store table observed on 116 (`cv_5a2db142ab40`): authorized values come from BI; model writes are rounded in units of ten thousand yuan.
_REAL_AUTH = {
    1083497812.3416427: "tl_bi",
    512736190.4471322: "tl_bi",
    468213574.9026518: "tl_bi",
    429650881.2283917: "tl_bi",
    371894520.6635104: "tl_bi",
}


@pytest.mark.parametrize(
    "written,canonical",
    [
        ("108350万", 1.0835e9),
        ("51274万", 5.1274e8),
        ("46821万", 4.6821e8),
        ("42965万", 4.2965e8),
        ("37189万", 3.7189e8),
    ],
)
def test_ranked_table_rows_each_resolve_uniquely(written: str, canonical: float) -> None:
    """★ 这条是本修复的理由：排名表里每一行都该绑上自己的来源。

    旧规则对**整数写法**一律给 ±10% 窗口，而排名表相邻档位天然在 10% 以内 ⇒
    `_resolve_value` 的 ambiguous-drop（窗口内 ≥2 个候选就丢弃）把中间三行全毙了。
    116 真机实测密度 2/5：只有首尾两行（邻居够远）绑上。
    注意问题**不是逐对判错**（旧规则逐对判断基本是对的），而是窗口宽到失去分辨力。
    """
    assert _resolve_value(canonical, _REAL_AUTH, raw_value_str=written) == "tl_bi"


def test_neighbour_inside_ten_percent_no_longer_causes_ambiguous_drop() -> None:
    """把上一条的机理单独钉出来：邻居在 ±10% 内、但在末位半单位之外。"""
    auth = {5.1274e8: "tl_a", 4.6821e8: "tl_b"}  # Values differ by 9.5%; each was a candidate for the other under the old window

    assert _resolve_value(5.1274e8, auth, raw_value_str="51274万") == "tl_a"
    assert _resolve_value(4.6821e8, auth, raw_value_str="46821万") == "tl_b"


def test_window_is_half_of_the_last_written_digit() -> None:
    """窗口大小 = 末位的一半，跟着写出精度走（而不是跟着小数位数分档）。"""
    assert _precision_window("51274万", 5.1274e8) == pytest.approx(5000.0)  # Last digit: 1万
    assert _precision_window("10亿", 1e9) == pytest.approx(5e7)  # Last digit: 1亿
    assert _precision_window("1.8亿", 1.8e8) == pytest.approx(5e6)  # Last digit: 0.1亿
    assert _precision_window("10.47%", 0.1047) == pytest.approx(5e-5)  # Last digit: 0.01%


def test_coarse_integer_emit_still_binds() -> None:
    """反向保险：粗略写法的窗口必须仍然够宽，别把老动机案例修坏。

    老 docstring 的例子：raw 0.0987 被写成 `10%`。末位是 1%，窗口 ±0.5% ⇒ 仍绑。
    """
    assert _resolve_value(0.10, {0.0987: "tl_a"}, raw_value_str="10%") == "tl_a"
    # `1万` must bind to both 9999 (user's example) and 6000 (rounding to 万 indeed yields 1万).
    assert _resolve_value(10000.0, {9999.0: "tl_a"}, raw_value_str="1万") == "tl_a"
    assert _resolve_value(10000.0, {6000.0: "tl_a"}, raw_value_str="1万") == "tl_a"


def test_value_outside_the_written_precision_does_not_bind() -> None:
    """反向锁：写出值不是真值在该精度上的舍入 ⇒ 不绑（宁可不绑，不错指来源）。"""
    # 4000 rounded to 万 is 0万, not 1万.
    assert _resolve_value(10000.0, {4000.0: "tl_a"}, raw_value_str="1万") is None
    # Truncation, not rounding: 1.1968 亿 written as `1.19亿` (last digit 0.01亿 ⇒ window ±5e5, actual difference 6.8e5).
    assert _resolve_value(1.19e8, {119683521.0: "tl_a"}, raw_value_str="1.19亿") is None
    # Ordinary rounding still binds as before.
    assert _resolve_value(1.2e8, {119683521.0: "tl_a"}, raw_value_str="1.2亿") == "tl_a"


def test_missing_raw_string_keeps_the_two_percent_fallback() -> None:
    """`raw_value_str` 缺失时维持原兜底 —— 上面那批老用例走的正是这条路。"""
    assert _precision_window(None, 1.118e9) == pytest.approx(1.118e9 * 0.02)
    assert _resolve_value(1.118e9, {1118302756.0: "tl_b"}) == "tl_b"


def test_unparsable_digits_fall_back_instead_of_crashing() -> None:
    """取不出数字（畸形/纯单位）时回退，不能抛。"""
    assert _precision_window("万元", 1.0) == pytest.approx(0.02)
    assert _precision_window("...", 1.0) == pytest.approx(0.02)


def test_currency_prefixed_emit_still_gets_a_precision_window() -> None:
    """★ 自查逮到：取数循环遇到第一个非数字就停，`¥` 打头会一路退到 ±2% 兜底。

    `_AUTOWRAP_PLAIN_RAW` 明确收 `¥1,234` / `HK$1.2亿` 这类写法，所以这是活形态；
    退到兜底意味着货币前缀的数字拿不到精度窗口，本次修复对它们等于没做。
    """
    assert _precision_window("¥1,234", 1234.0) == pytest.approx(0.5)
    assert _precision_window("$5,000", 5000.0) == pytest.approx(0.5)
    assert _precision_window("HK$1.2亿", 1.2e8) == pytest.approx(5e6)
    # Same as the number without prefix; prefixes must not affect precision decisions.
    assert _precision_window("¥1,234", 1234.0) == _precision_window("1,234", 1234.0)


def test_scale_regex_does_not_match_across_a_newline() -> None:
    """反向保险：数字与单位之间允许空白，但**不含换行**。

    今天两个调用点都是逐行的所以撞不上，可正则本身能跨行 —— 自查实测
    `"排名 5\n万元级门店"` 会被匹配成 `5\n万`。哪天有人把它用在整段文本上就是静默误匹配。
    """
    from bowhead_core.canvas_composers.answer_decomposer import _AUTOWRAP_PLAIN_RAW

    assert _AUTOWRAP_PLAIN_RAW.search("排名 5\n万元级门店") is None
    # Still accept whitespace within the same line (the actual fix).
    assert _AUTOWRAP_PLAIN_RAW.search("营收 108,350 万元") is not None
