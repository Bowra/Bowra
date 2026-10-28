"""delivery-surface C5 —— 用户显式「不要写报告」的确定性识别。

成对写 blocked/allowed（[[feedback_assert_absence_needs_positive_control]]）：
只测「拦住了」不测「放行了」= 守卫可能是死的；反之亦然。
判据刻意保守：宁可漏判（退回证据驱动默认）不可误判（真报告被压 chat）。
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.lead_orchestrator import parse_user_delivery_directive

# ─── blocked: explicit imperative refusal of reports → "chat" ──────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # Actual wording from the post-fix 116 window (discovery §3.3)
        "分两步：①用 BI 查今年利润前三的品牌 ②一句话总结。不要写报告。",
        "Dogfood 1.0.70：请只回答 19×29 的结果，不要生成报告。",
        "查一下最近的销售表现，不用写报告",
        "别写报告，直接告诉我结果",
        "无需生成报告",
        "不需要出报告",
        "不要做分析报告",
        "帮我查 GMV，不要报告",
    ],
)
def test_explicit_no_report_directive(text: str) -> None:
    assert parse_user_delivery_directive(text) == "chat"


# ─── allowed: no directive / ambiguous wording → None (evidence-driven default) ─────────


@pytest.mark.parametrize(
    "text",
    [
        # Actual report requests (negative controls: must never match)
        "按省统计近30天的营业额，出一份分析报告，重点说明排名前五的省份",
        "帮我做一份本月各省营业额与利润的经营分析报告",
        # ★ r1 regex false-positive anchor (t_fddef587b612): "一句话总结" is only one of four questions;
        #   suppressing the whole turn is incorrect; ambiguous wording must never match.
        "华东地区的销售额是多少？哪个区域同比增长最快？用一句话总结当前整体销售情况。各区域的利润率从高到低是多少",
        "简单说说上个月的经营情况",
        "直接给数字就行",
        "去年每月营业额",
        # Opposite meaning: the user explicitly requests a report.
        "写一份报告，不要太长",
        # review #1: compound-word false positives for "别"; pin each lookbehind [分特级类个组差] case.
        # All these phrases **request** reports; a false match suppresses a real report into chat.
        "分别写报告给华东和华北团队",
        "特别出报告说明利润率变化",
        "按品类类别出报告",
        "按组别生成报告",
        # review #1: "报告以外" means only the report, not a refusal of reports (negative lookahead).
        "不要报告以外的内容，只要报告本身",
        "",
    ],
)
def test_ambiguous_or_report_text_returns_none(text: str) -> None:
    assert parse_user_delivery_directive(text) is None


def test_none_input() -> None:
    assert parse_user_delivery_directive(None) is None
