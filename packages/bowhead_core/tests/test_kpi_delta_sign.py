"""Percentage deltas make their direction explicit.

Deterministic normalization applies to KPI.delta; prose guidance is tested through
the compute prompt separately.
"""

from __future__ import annotations

import pytest
from bowhead_core.analysis_payload import _kpi_from_dict, _normalize_delta_sign


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("0.40%", "+0.40%"),  # ★ Main case
        ("1.26%", "+1.26%"),
        ("0.4 %", "+0.4 %"),
        ("32.50%", "+32.50%"),
    ],
)
def test_bare_positive_percent_gets_plus(raw: str, want: str) -> None:
    assert _normalize_delta_sign(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "-1.43%",  # Negative number already has a sign.
        "+2%",  # Already signed.
        "同比下降 2%",  # Contains a directional word.
        "环比 3%",  # Contains the direction term 环比.
        "▲ 5%",  # Contains a direction marker.
        "3 家门店",  # Not a percentage at all.
        "",
    ],
)
def test_leaves_everything_else_alone(raw: str) -> None:
    """★ 正对照：宁可少补，不可改错。

    没有这组，「无脑给所有 delta 加 +」的坏实现也会让上一组绿——
    那会把 `-1.43%` 写成 `+-1.43%`。
    """
    assert _normalize_delta_sign(raw) == raw


def test_non_string_passes_through() -> None:
    assert _normalize_delta_sign(None) is None
    assert _normalize_delta_sign(12) == 12


def test_applied_at_the_kpi_chokepoint() -> None:
    """归一要挂在落地点上，不能只是个没人调的纯函数。"""
    kpi = _kpi_from_dict({"label": "销售金额", "value": "5.62亿", "delta": "0.40%"})
    assert kpi.delta == "+0.40%"


def test_prompt_teaches_direction_for_prose() -> None:
    """散文那半够不着确定性归一，提示词必须写着。"""
    from pathlib import Path

    md = (Path(__file__).parent.parent / "src" / "bowhead_core" / "agents" / "prompts" / "system" / "compute.md").read_text()
    assert "变化率必须能看出方向" in md
    assert "同比0.40%" in md, "要贴反例，不然模型不知道在说哪种写法"


def test_prompt_teaches_conclusion_first_with_a_real_counterexample() -> None:
    """批5：只约束长度不约束成分，简单事实题也会被写成复核报告体。

    用户报「回答太 technical，为啥给一句特别长的很多数字的话」。实例 130 字，
    **在 30-200 字规定之内** —— 所以不是长度问题，是成分问题。

    反例必须来自**生产实录**：编的反例模型会学偏。
    """
    from pathlib import Path

    md = (Path(__file__).parent.parent / "src" / "bowhead_core" / "agents" / "prompts" / "system" / "compute.md").read_text()
    assert "结论优先，过程其次" in md
    assert "第一句必须是答案" in md
    # Include both negative and positive examples; negatives alone do not teach the model what to write.
    assert "已按原口径复核" in md, "缺生产实录反例"
    assert "69 位代表 2025 年四个季度销售金额逐季下滑" in md, "缺正例改写"
    assert "caveats" in md, "没说记账该挪去哪"
