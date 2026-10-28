"""没有内容可报的一轮不许 compose（116 prod 1.0.79 dogfood P1）。

实测三步复现（`s_ca475963b4d1`，最自然的用法）：

1. 「帮我做一份本月各省营业额与利润的经营分析报告」→ 画布 11 段真报告，导出的
   `2026年7月各省经营分析报告.docx` **41024 字节**、内容齐全。
2. 「把画布上这份报告导出成 Word」→ 那一轮什么分析都没做（工具只有
   `skill_read` / `skill_invoke(canvas-docx)`），payload 的
   kpis/insights/recommendations/sources **全空**、`title` 是空串。可它照样进了
   composer：writer-loop 的 `_emit_report_header` 见 title 为空就**拿用户原话兜底**
   ⇒ 画布多出一份「标题 = 把画布上这份报告导出成 Word 文档（.docx）给我」的伪报告。
3. 「再把这份报告导出成 Excel」→ 伪报告已把真报告 11 段全 **supersede**，而
   `list_export_projection` 只取 `superseded=0` ⇒ 拿到的 xlsx **5117 字节、一个数据
   都没有**，只剩那句指令当标题。

判据只问「这一轮有没有产出内容」，不猜「是不是导出轮」——猜意图的判据挡不住下一种
无内容轮（纯问候 / 纯确认 / 重发失败）。
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.agents.lead_orchestrator import (
    _payload_has_report_content,
    _turn_produced_content,
)


def _inv(tool: str) -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool)


# ── Actual reproduction shape ───────────────────────────────────

# `t_5b91c70281c9` lead.final.payload, copied field for field.
EXPORT_TURN_PAYLOAD = {
    "title": "",
    "exec_summary": "Word 文档已生成：**2026年7月各省经营分析报告.docx**，内容包含分省营业额与利润明细。可以直接下载使用。",
    "kpis": [],
    "insights": [],
    "recommendations": [],
    "actions": [],
    "caveats": [],
    "sources": [],
    "inline_artifacts": [{"kind": "file", "id": "art_3a1dcb838c1a"}],
}
EXPORT_TURN_TOOLS = [_inv("skill_read"), _inv("skill_invoke"), _inv("subagent_return"), _inv("subagent_spawn")]


def test_export_turn_is_recognized_as_contentless() -> None:
    """★ 核心：这一轮两条判据都不命中 ⇒ 不 compose。"""
    assert _payload_has_report_content(EXPORT_TURN_PAYLOAD) is False
    assert _turn_produced_content(EXPORT_TURN_TOOLS) is False


def test_exec_summary_alone_does_not_count_as_content() -> None:
    """★ 这条最容易写错：导出轮的 payload **恰恰**只有 exec_summary。

    它是操作回执（「已成功导出为 Word 文档」），聊天答案里已经说过一遍；
    把它算作「有内容」等于放行整个 bug。
    """
    assert _payload_has_report_content({"exec_summary": "已成功导出为 Word 文档。", "title": "导出完成"}) is False


# ── Real report must still pass ────────────────────────────────


def test_real_report_payload_passes() -> None:
    assert _payload_has_report_content({"kpis": [{"label": "营收", "value": "9.80亿元"}]}) is True
    assert _payload_has_report_content({"insights": [{"title": "上海领先"}]}) is True
    assert _payload_has_report_content({"recommendations": [{"action": "优化河南门店结构"}]}) is True


def test_sources_alone_do_not_count_as_content() -> None:
    """delivery-surface C1（r2/F1）：sources/caveats 是元数据不是结论。

    Lead 聚合几乎必带 peer sources（merge 循环 `merged["sources"].append`）——
    把它算内容，这道闸对 BI 轮**恒不生效**，写手在只有 exec_summary+sources 的
    payload 上产壳报告（116 t_b816e1879d6d「需要补充以下证据…」）。
    """
    assert _payload_has_report_content({"sources": [{"id": 1}]}) is False
    assert _payload_has_report_content({"caveats": ["口径差异"]}) is False


def test_bi_read_tools_do_not_count_as_content() -> None:
    """delivery-surface C1：读取 ≠ 产出。

    bi_query/kb_search/web_search 只把数据取回来；「取回来了」不等于「有结论可写」。
    旧断言（bi_query → True）保护的「Lead 偷懒 bi-only path」由 C2 一并退役——
    简单查数的答案属于 chat，不再靠 composer 的 bi-fallback 强行成报告
    （tech-design §4.5 R1：用户真要报告而 Lead 偷懒的形态，由负对照 E 盯，
    修法在 Lead 路由侧不在 composer 兜底）。
    """
    for tool in ("bi_query", "bi_fetch_rows", "kb_search", "web_search"):
        assert _turn_produced_content([_inv(tool)]) is False, tool


def test_canvas_writing_tools_count_as_content() -> None:
    for tool in (
        "add_chart",
        "canvas_add_kpi_row",
        "canvas_add_table",
        "canvas_add_data_table",
        "canvas_add_insight",
        "canvas_add_recommendations",
        "write_report",
        "image_generate",
    ):
        assert _turn_produced_content([_inv(tool)]) is True, tool


def test_content_producing_tools_covers_canvas_writers() -> None:
    """守卫（r2/F1 顺带修）：canvas 写入工具全集必须都算「产内容」。

    原表漏了 kpi_row/data_table/recommendations 三个，靠 bi_query 兜着没暴露；
    C1 收窄后若再漂移就是真回归——任何一边加了 canvas_add_* 另一边必须跟上。
    """
    from bowhead_core.agents.lead_orchestrator import (
        _CANVAS_WRITE_PREFIXES,
        _CONTENT_PRODUCING_TOOLS,
    )

    missing = set(_CANVAS_WRITE_PREFIXES) - set(_CONTENT_PRODUCING_TOOLS)
    assert not missing, f"canvas 写入工具不算产内容: {sorted(missing)}"


def test_export_skill_alone_never_counts() -> None:
    """★ 导出 skill 消费画布、不生产画布——把它算作产内容就等于没修。"""
    assert _turn_produced_content([_inv("skill_invoke"), _inv("skill_read")]) is False


def test_non_dict_payload_falls_back_to_truthiness() -> None:
    """老形态 payload（非 dict）不能因为读不出字段就被判成空。"""
    assert _payload_has_report_content(None) is False
    assert _payload_has_report_content({}) is False
    assert _payload_has_report_content(["something"]) is True
