"""compose 期实质性标记 —— 判据本身的测试。

背景（`docs/sprints/empty-report-library-2026-08-25/assessment.md`）：
「我的报告」里在积累点进去零结论的条目。事后从画布形状反推**做不到**——我实测出双向
错判：散文写手把表/图/建议写进 narrative 里（漏杀真报告），而 agent 又会围绕「没有数据」
这件事本身产出 KPI 卡和建议（放行真空报告）。

所以判断要在 **compose 期**做：那时手里还有 payload、工具结果、渲了哪些 section。

★本判据**精度优先**：只在有把握时下结论，拿不准返 None。
  绝不能把海报/绘图/真报告判成空——那比留着空报告严重得多。

★2026-08-26（review §5.10 R-1）换过一次信号：从「造数守卫的 observed 数字集合为空」
  换成「取数/计算工具**返没返行**」。旧信号在 116 全库 549 轮里**恒为真**
  （`row_count: 0`/`elapsed_ms`/`status: 504` 都算数字）⇒ `empty` 一次都打不出来。
  本文件下半部分 `tool_content_signal` 的形状用例，逐条对应 116 上量到的真实结果形状。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.canvas_content_status import (
    CONTENT_EMPTY,
    CONTENT_SUBSTANTIVE,
    TOOL_CONTENT,
    TOOL_EMPTY,
    TOOL_UNKNOWN,
    assess_canvas_content,
    tool_content_signal,
)

pytestmark = pytest.mark.unit


def _assess(payload=None, kinds=(), signal=TOOL_UNKNOWN):
    return assess_canvas_content(payload=payload or {}, emitted_kinds=list(kinds), tool_signal=signal)


def _inv(tool_name: str, result, nested=None):
    return SimpleNamespace(tool_name=tool_name, result=result, nested=nested)


# ── Substantive: visual/material output ───────────────────


def test_visual_output_is_always_substantive() -> None:
    """海报 / 设计稿 / 绘图演示 —— 没有一个数字也是正当交付物。

    实测样本：`cv_fc5894e69a09` 促销海报、`cv_0f169f9adc3a` y=x² 曲线图、
    `cv_f56e7e6f8f93`（用户明说「不用写分析结论」的出图轮）。
    按「有没有数字」判会把这一整类误杀，所以视觉产出**第一优先**判 substantive。
    """
    for kind in ("chart", "image", "design"):
        status, reason = _assess(kinds=[kind, "narrative"])
        assert status == CONTENT_SUBSTANTIVE, kind
        assert reason == "visual_output"


def test_visual_kinds_use_the_real_section_vocabulary() -> None:
    """★R-2 回归锁：段类型词表必须是**本系统真有的那套**。

    初版自己抄了一份 `{"chart","image","design","table"}` —— `table` 在本系统
    **不存在**（canvas 段的表格 kind 是 `data_table`；`table` 只是 export IR 的
    block kind，另一套词表），同时漏掉了 `data_table`/`datarc_iframe`
    等真实存在的视觉段。现在复用 `report_tools._FIGURE_KINDS`，不再维护第二份。
    """
    for kind in ("data_table", "datarc_iframe", "html_artifact", "external_iframe"):
        status, _ = _assess(kinds=[kind, "narrative"])
        assert status == CONTENT_SUBSTANTIVE, f"{kind} 是真实存在的视觉段，却没被认出来"


def test_structured_payload_with_real_tool_data_is_substantive() -> None:
    """有 KPI/洞察/建议，**且**工具真返了行 ⇒ 有内容。"""
    status, reason = _assess(
        payload={"kpis": [{"label": "营业额", "value": "8.63亿"}]},
        kinds=["h1", "kpi_row", "narrative"],
        signal=TOOL_CONTENT,
    )
    assert status == CONTENT_SUBSTANTIVE
    assert reason == "structured_with_data"


# ── Empty: no output and data-fetch tools ran but returned nothing ───


def test_no_output_and_tools_returned_nothing_is_empty() -> None:
    """无视觉产出、payload 无结构化内容、取数工具跑了却一条没返 ⇒ 空。

    实测形态 `t_57481411d476`「今年电商销售额是多少？直接查数给个数字」——
    两次 `bi_query` 全是 `BIQueryFailedError: download_data failed`，
    画布上只剩 `h1/meta/narrative`。
    """
    status, reason = _assess(
        payload={"exec_summary": "BI 连续返回 failed，无法获取数据"},
        kinds=["h1", "narrative", "sources"],
        signal=TOOL_EMPTY,
    )
    assert status == CONTENT_EMPTY
    assert reason == "no_data_no_output"


def test_bare_narrative_only_is_empty() -> None:
    """只剩一段说明 + meta（`cv_103d347ac77d` 那种 17 字画布）。"""
    status, _ = _assess(kinds=["narrative", "meta"], signal=TOOL_EMPTY)
    assert status == CONTENT_EMPTY


# ── ★ No verdict: return None whenever uncertain ──────────


def test_prose_report_without_structured_sections_is_not_marked_empty() -> None:
    """★关键误杀防线：散文写手的真报告。

    实测 `cv_c4c9b12c7586`「查一下最近的销售表现」——9 处引注、真数字
    （营业额 -5.2% / 利润 -18.7% / 客流 +3.8%）、正文内嵌 markdown 表格、
    `[figure:]` 图引用、`## 建议` 小节，但**一个 chart/kpi_row/recommendations 段都没有**
    （散文路把这些都写进 narrative 的 markdown 里了）。
    工具真返了行 ⇒ **绝不能判空**。
    """
    status, reason = _assess(kinds=["h1", "meta", "narrative", "sources"], signal=TOOL_CONTENT)
    assert status != CONTENT_EMPTY, f"散文真报告被判空了: {reason}"


def test_structured_output_without_tool_data_is_undecided() -> None:
    """★关键漏杀的诚实处置：agent 围绕「没有数据」也会产出 KPI 卡和建议。

    实测 `cv_53ea95cfb793`：标题逐字「2026年Q1综合经营分析报告 — 数据不足，无法编制」，
    却带着 kpi_row + 3 张 insight_card + recommendations。
    这种既不能判 substantive（它确实没结论）、也**不该**判 empty（判据没把握），
    ⇒ 返 None，交给后续迭代，别把不确定伪装成确定。
    """
    status, reason = _assess(
        payload={"kpis": [{"label": "可用数据源", "value": "0"}], "insights": [{"title": "数据缺口"}]},
        kinds=["h1", "kpi_row", "insight_card", "recommendations"],
        signal=TOOL_EMPTY,
    )
    assert status is None, f"不该下结论却下了: {status}/{reason}"
    assert reason == "undecided"


def test_unknown_tool_signal_never_yields_empty() -> None:
    """★`unknown` ≠ `empty`：本轮一个认得出的取数工具都没跑过，就没资格说「没有」。

    这条与 `emitted_kinds` 非空那道闸同族——都是不让「我不知道」被写成一个
    **自信的错误结论**（默认值/缺失把漏接线藏成阴性）。
    """
    status, reason = _assess(kinds=["h1", "narrative"], signal=TOOL_UNKNOWN)
    assert status is None, f"unknown 被当成 empty 了: {status}/{reason}"


def test_no_output_but_tool_returned_data_is_undecided() -> None:
    """工具返了行、却没渲出任何结构化产出 —— 可能是真报告(散文)，也可能是
    `cv_5263603bcd65` 那种「BI 丢弃条件返回全量兜底值」。判不了就别判。"""
    status, reason = _assess(kinds=["narrative", "sources"], signal=TOOL_CONTENT)
    assert status is None
    assert reason == "undecided"


def test_empty_inputs_are_undecided_not_empty() -> None:
    """负对照：什么都没有（还没 compose 完 / 调用方没传）不等于「空报告」。"""
    status, _ = _assess()
    assert status is None


def test_empty_requires_emitted_kinds() -> None:
    """正对照：`empty` 的语义是「渲过了而没实质内容」，不是「我没拿到信息」。
    调用方漏传 `emitted_kinds` 时不许下结论。"""
    status, _ = _assess(kinds=(), signal=TOOL_EMPTY)
    assert status is None


# ── tool_content_signal: each shape follows real 116 data ──


def test_signal_bi_batch_with_rows_is_content() -> None:
    """BI 批量形状 `{"results":[{ok, sample_rows|rows, ...}]}`（116 上 bi_query 的主形态）。"""
    assert tool_content_signal([_inv("bi_query", {"results": [{"ok": True, "sample_rows": [{"月": "2025-01", "营业额": 1.98}]}]})]) == TOOL_CONTENT


def test_signal_bi_batch_all_subresults_failed_is_empty() -> None:
    """★真机形态 `t_28622727ba06`：批量返回了，但每个 sub 都 `ok=False`、零行。

    ⚠ 这正是旧信号判不出来的那一族——结果 JSON 里照样有一堆数字。
    """
    result = {"results": [{"ok": False, "rows": [], "error": "BIAsyncTaskTimeout"}, {"ok": False, "rows": []}]}
    assert tool_content_signal([_inv("bi_query", result)]) == TOOL_EMPTY


def test_signal_bi_hard_failure_is_empty() -> None:
    """`t_57481411d476`：两次 `download_data failed`。"""
    fail = {"ok": False, "error": "BIQueryFailedError: download_data failed"}
    assert tool_content_signal([_inv("bi_query", fail), _inv("bi_query", fail)]) == TOOL_EMPTY


def test_signal_bi_metric_shape_is_content() -> None:
    """BI 指标形状 `{"type":"metric","metrics":[...]}`。"""
    assert tool_content_signal([_inv("bi_query", {"type": "metric", "metrics": [{"name": "营业额", "content": "8.63亿"}]})]) == TOOL_CONTENT


def test_signal_sandbox_needs_ok_and_output() -> None:
    """沙箱：跑成功且有 stdout/产物才算内容；`ok=False` 一律不算（`exit 125` 那族）。"""
    assert tool_content_signal([_inv("python_sandbox", {"ok": True, "stdout": "斜率 b = 82.937\n"})]) == TOOL_CONTENT
    assert tool_content_signal([_inv("python_sandbox", {"ok": True, "stdout": "", "output_artifacts": ["art_1"]})]) == TOOL_CONTENT
    assert tool_content_signal([_inv("python_sandbox", {"ok": False, "stdout": "", "error": "sandbox 未就绪"})]) == TOOL_EMPTY


def test_signal_web_search_compat_summary_counts() -> None:
    """★compat 接口按官方文档设计上就**不返来源**、只返 summary —— 那也是内容
    （116 实测 web_search 的 `results: []` + 非空 summary 是常态）。"""
    assert tool_content_signal([_inv("web_search", {"query": "q", "results": [], "summary": "截至 2026 年 6 月……"})]) == TOOL_CONTENT
    assert tool_content_signal([_inv("web_search", {"query": "q", "results": [], "summary": ""})]) == TOOL_EMPTY


def test_signal_kb_search_uses_chunks() -> None:
    """116 实测 kb_search 的真键是 `chunks`（`hits` 是 preview 侧的旧写法，一并认）。"""
    assert tool_content_signal([_inv("kb_search", {"chunks": [{"text": "…"}], "total_candidates": 3})]) == TOOL_CONTENT
    assert tool_content_signal([_inv("kb_search", {"chunks": [], "total_candidates": 0})]) == TOOL_EMPTY


def test_signal_ignores_the_models_own_self_report() -> None:
    """★★`subagent_spawn` / `subagent_return` 是模型对自己干了什么的**自述**，不是工具数据。

    把自述当证据正是 `project_chat_fabricates_when_payload_empty` 那个洞
    （造数守卫的 observed 混进 `subagent_spawn.result` ⇒ 复述一遍就算有来源）。
    这里断言：只有自述、没有取数工具 ⇒ `unknown`，**不是** content。
    """
    spawn = _inv("subagent_spawn", {"ok": True, "payload_returned": True, "tokens_input": 198180, "tool_count": 22})
    ret = _inv("subagent_return", {"ok": True, "summary": "{'insights_count': 5}", "__terminal": True})
    assert tool_content_signal([spawn, ret]) == TOOL_UNKNOWN


def test_signal_ignores_metadata_only_tools() -> None:
    """`bi_schema` / `skill_read` 返的是元数据，不是这一轮拿到的内容。"""
    assert tool_content_signal([_inv("bi_schema", {"measures": [{"id": 1}], "dimensions": [{"id": 48}]})]) == TOOL_UNKNOWN


def test_signal_walks_nested_invocations() -> None:
    """Nested peer results remain content after the temporary context field is removed."""
    nested = _inv("bi_query", {"results": [{"ok": True, "rows": [{"x": 1}]}]})
    spawn = _inv("subagent_spawn", {"ok": True, "payload_returned": True}, nested=[nested])
    assert tool_content_signal([spawn]) == TOOL_CONTENT


def test_signal_production_tool_success_counts_as_content() -> None:
    """产出类工具写成功了 ⇒ 这一轮往画布上放了**实物**。

    ⚠ 2026-08-26 轮 6-e 后收窄：`write_report`（散文正文）已移出产出族，见下方
    `test_signal_write_report_is_not_evidence_that_tools_returned_anything`。
    """
    assert tool_content_signal([_inv("add_chart", {"ok": True, "section_id": "sec_1", "row_count": 12})]) == TOOL_CONTENT
    assert tool_content_signal([_inv("add_chart", {"skipped": True, "hint": "no data"})]) == TOOL_UNKNOWN


def test_signal_no_invocations_at_all_is_unknown() -> None:
    """负对照：一次工具都没调 ≠ 「工具什么都没返」。"""
    assert tool_content_signal([]) == TOOL_UNKNOWN
    assert tool_content_signal(None) == TOOL_UNKNOWN


def test_signal_write_report_is_not_evidence_that_tools_returned_anything() -> None:
    """★★116 轮 6-e 现场（`cv_beb417ab26e0` / `t_28f4658546fc`）：`write_report` 写的是
    **散文正文**，而「一份如实说明『查不到数据』的散文报告」**正是 `empty` 要标的那种东西**。

    把它算成「产出」⇒ 任何亲笔轮恒为 `content` ⇒ `empty` 在批 3 之后的主路上**再次
    结构性打不出来** —— 与 R-1 同一类错误（唯一会下结论的那条腿够不着真实输入）。
    """
    write = _inv("write_report", {"ok": True, "section_id": "sec_1", "seq": 2, "owner": "sub_x"})
    assert tool_content_signal([write]) == TOOL_UNKNOWN, "亲笔写正文不等于工具返了数据"
    # Add BI that actually returned nothing: must be empty, not promoted to content by write_report.
    dead_bi = _inv("bi_query", {"ok": False, "error": "BIQueryFailedError: download_data failed"})
    assert tool_content_signal([dead_bi, write]) == TOOL_EMPTY


def test_signal_canvas_artifact_writers_still_count() -> None:
    """正对照：真往画布上放了**实物**的工具照旧算 content（别把闸开得过大）。"""
    for name in ("add_chart", "add_image", "canvas_create_design"):
        assert tool_content_signal([_inv(name, {"ok": True, "section_id": "sec_1"})]) == TOOL_CONTENT, name
