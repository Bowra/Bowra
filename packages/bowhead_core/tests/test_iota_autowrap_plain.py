"""binder 判据（Sprint ι dogfood-1 起的「哪些散文数字该绑」契约）—— gen3 形态。

M3-4 换形态：绑定动作从 gen2 的 `_autowrap_plain_numbers`（产 `[num:V:src:S]`
标记）换成 `citation_marks.bind_to_numbered`（产 `值 [N]`），**判据一字未改**——
匹配骨架仍是同一个 `_AUTOWRAP_PLAIN_RAW`（本文件前半段直接锁它），单源命中 /
ambiguous-drop / 已引用跳过 等条件由 binder 逐字复用。

原契约四条全保：(a) 高置信形态绑、(b) 低置信跳过（Top 5 假阳性）、
(c) 已引用的数字不重复标、(d) map 命中/落空的行为。
"""

from __future__ import annotations

import pytest
from bowhead_core.canvas_composers.answer_decomposer import _AUTOWRAP_PLAIN_RAW
from bowhead_core.citation_marks import bind_to_numbered
from bowhead_core.sources_resolver import TurnSourcesRegistry

# ─── Pattern matching (regex coverage): regex unchanged, assertions retained ───


@pytest.mark.parametrize(
    "text,expected_match",
    [
        # CN scale + currency tail
        ("总营业额13.9亿元", "13.9亿"),
        ("总利润2.4亿元", "2.4亿"),
        ("营收 5.5万元", "5.5万"),
        ("总额 100亿", "100亿"),
        # Percent
        ("退款率17.8%", "17.8%"),
        ("增长 25%", "25%"),
        ("0.5‰", "0.5‰"),
        # Currency prefix
        ("¥1,800", "¥1,800"),
        ("$1.5", "$1.5"),
        # Multi-comma thousand sep
        ("1,921,861 人", "1,921,861"),
        ("交易 5,432,100", "5,432,100"),
        # EN scale single letter
        ("总额 6.8M", "6.8M"),
        ("规模 1.5B", "1.5B"),
        # Plus prefix
        ("增长 +2.38%", "+2.38%"),
    ],
)
def test_autowrap_regex_high_confidence_matches(text: str, expected_match: str) -> None:
    m = _AUTOWRAP_PLAIN_RAW.search(text)
    assert m is not None, f"expected match for {text!r}"
    assert m.group(1) == expected_match


@pytest.mark.parametrize(
    "text",
    [
        # Bare integers with no unit — DON'T match (avoid Top 5 false-positive)
        "Top 5 品牌中第一",
        "排名 1-3 位",
        "总共 10 个",
        # Single-comma thousand-sep < 1,000,000 — also skipped (only 2+ comma groups match)
        # Samples must not include 「元」: since 5ed70855c, bare-yuan forms (`60,484元` / `1,234 元`) deliberately signal a strong unit for binding.
        "总计 1,234 笔",
        # Decimal without unit
        "比率 0.42 高",
    ],
)
def test_autowrap_regex_low_confidence_skip(text: str) -> None:
    m = _AUTOWRAP_PLAIN_RAW.search(text)
    assert m is None, f"unexpected match {m.group(0) if m else None!r} in {text!r}"


# ─── End-to-end binding (gen2 `[num:V:src:S]` → gen3 `value [N]`) ───


def _bind(text: str, v2s: dict[float, str]) -> tuple[str, TurnSourcesRegistry]:
    reg = TurnSourcesRegistry()
    out = bind_to_numbered(
        text,
        value_to_source=v2s,
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    return out, reg


def test_bind_with_map_hit() -> None:
    out, reg = _bind("营业额13.9亿元，利润2.4亿元", {1.39e9: "tl_revenue", 2.4e8: "tl_profit"})
    assert out == "营业额13.9亿元 [1]，利润2.4亿元 [2]"  # Unit absorption: [N] follows the unit.
    assert [s["artifact_id"] for s in reg.sources] == ["tl_revenue", "tl_profit"]


def test_bind_with_map_miss_leaves_plain() -> None:
    out, _ = _bind("营业额13.9亿元，无关数 6.8M", {1.39e9: "tl_revenue"})
    assert "13.9亿元 [1]" in out
    assert "6.8M" in out and "6.8M [" not in out  # No source → plain text.


def test_bind_skips_number_already_cited() -> None:
    """已经带 [N] 的数字不重复绑（#11 双标记防护——原 gen2 版锁的是「已 wrap 的
    marker 不被二次 wrap」，gen3 下的等价面就是这条）。"""
    reg = TurnSourcesRegistry()
    reg.register_source("tl_x", tool="bi_query", query="q")  # Occupy [1] first, simulating the real pipeline.
    out = bind_to_numbered(
        "已引 13.9亿元 [1]，新数2.4亿元应绑",
        value_to_source={1.39e9: "tl_x", 2.4e8: "tl_profit"},
        sources=reg.sources,
        id_to_index=reg.id_to_index,
    )
    assert out.count("[1]") == 1  # Existing citation is not duplicated (adjacent-[N] skipped).
    assert "2.4亿元 [2]" in out  # New number gets the next index.


def test_bind_thousand_separator_spaced_unit_leaves_n_before_unit() -> None:
    """⚠ 如实记录现状（**不是**期望形态）：单位与数字之间有空格时，`_UNIT_SWALLOW_RE`
    （锚 `^`，不吃前导空格）不吞单位 → `[N]` 落在数字与单位之间。

    与 r3 P1-4 修过的「9627万 [1]元」同源，只是那次修的是**紧凑**形。
    **prod 实测该割裂形态 0 命中**（binder 只跑 narrative 散文，那里数字多为紧凑形
    `9.627亿元`）→ 判为理论缺口，归 M1 binder 打磨 follow-up，不在 M3（读端下线）
    范围内顺手改。本断言锁住现状，将来真修时会红——那正是它该做的。
    """
    out, _ = _bind("总客流量 1,921,861 人", {1921861.0: "tl_count"})
    assert out == "总客流量 1,921,861 [1] 人"


def test_bind_percent() -> None:
    out, _ = _bind("平均退款率17.8%", {0.178: "tl_refund"})
    assert out == "平均退款率17.8% [1]"


def test_bind_plus_prefix() -> None:
    out, _ = _bind("同比增长 +2.38%", {0.0238: "tl_growth"})
    assert out == "同比增长 +2.38% [1]"


def test_bind_no_top_n_false_positive() -> None:
    """裸整数（Top 5 / 排名 3）即使值在 map 里也不绑——低置信判据不变。"""
    out, _ = _bind("Top 5 品牌占主导，排名 3 是华东", {5.0: "tl_a", 3.0: "tl_b"})
    assert out == "Top 5 品牌占主导，排名 3 是华东"


def test_bind_no_op_on_empty_inputs() -> None:
    assert _bind("", {1: "x"})[0] == ""
    assert _bind("hello world", {})[0] == "hello world"
    assert _bind("无数字 text", {1: "x"})[0] == "无数字 text"


def test_bind_lazy_registers_sources_in_order() -> None:
    """binder 铸 [N] 与 registry 登记 lockstep：编号即 sources 下标+1（[N] 可解析的地基）。"""
    out, reg = _bind("营收13.9亿元，利润2.4亿元", {1.39e9: "tl_rev", 2.4e8: "tl_profit"})
    assert out == "营收13.9亿元 [1]，利润2.4亿元 [2]"
    assert reg.sources[0]["artifact_id"] == "tl_rev"
    assert reg.sources[1]["artifact_id"] == "tl_profit"
