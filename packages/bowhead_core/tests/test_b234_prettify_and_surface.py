"""Sprint chat-polish-r3 Phase 3 (B2/B3/B4) — prettify + surface helpers.

B2 — bi_query single-query shape `{columns, rows, row_count}` (no `results`
     wrapper) now prettifies to "N 行 · M 列" instead of falling through to
     raw 80-char JSON dump.

B3 — subagent_return `{ok: False, error: "..."}` (legit payload rejection)
     now reads "结果被退回: …" instead of misleading "结果已交付"
     fallback.

B4 — `skill_invoke({name: "python_sandbox", ...})` now surfaces tool_name
     as "python_sandbox" so FE catalog renders "🔧 运行代码" instead of
     wrapper-only "📒 调用 skill".
"""

import pytest
from bowhead_core.subagent import (
    _prettify_output_preview,
    _surface_inner_skill_name,
)


@pytest.fixture(autouse=True)
def _zh_ui_turn():
    """These goldens model a zh-CN interface turn. Without language context the step previews
    use English (docs/design/language-policy.md, "AI answers": deterministic product copy without
    browser context or saved preferences uses English; covered by test_output_preview_i18n)."""
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language

    token = set_current_ui_language("zh-CN")
    yield
    reset_current_ui_language(token)


class TestB2BiQuerySingleFormPrettify:
    def test_single_query_shape_renders_rows_and_cols(self) -> None:
        result = {
            "columns": ["每月", "营业额 / 元"],
            "rows": [
                {"每月": "2025年1月", "营业额 / 元": 51800000000.0},
                {"每月": "2025年2月", "营业额 / 元": 50000000000.0},
                {"每月": "2025年3月", "营业额 / 元": 51800000000.0},
            ],
            "row_count": 3,
        }
        out = _prettify_output_preview("bi_query", result, None)
        assert out == "3 行 · 2 列"

    def test_empty_rows(self) -> None:
        result = {"columns": ["a"], "rows": [], "row_count": 0}
        out = _prettify_output_preview("bi_query", result, None)
        assert out == "0 行 · 1 列"

    def test_columns_missing_yields_zero_cols(self) -> None:
        result = {"rows": [{"a": 1}], "row_count": 1}
        out = _prettify_output_preview("bi_query", result, None)
        assert out == "1 行 · 0 列"

    def test_batch_shape_still_takes_priority(self) -> None:
        # Batch shape (with `results`) keeps its existing prettify, NOT
        # the new single-form branch.
        result = {
            "results": [
                {"ok": True, "sample_rows": [{}, {}]},
                {"ok": True, "sample_rows": [{}, {}, {}]},
            ],
        }
        out = _prettify_output_preview("bi_query", result, None)
        assert "查询" in out and "成功" in out
        assert "行 · " not in out or "列" not in out  # not the new branch's shape


class TestB3SubagentReturnFailurePrettify:
    def test_ok_false_with_error_string_renders_rejection(self) -> None:
        result = {
            "ok": False,
            "error": "payload schema invalid: missing 'insights' field",
        }
        out = _prettify_output_preview("subagent_return", result, None)
        assert out.startswith("结果被退回:")
        assert "payload schema invalid" in out

    def test_ok_false_long_error_truncated(self) -> None:
        result = {"ok": False, "error": "x" * 500}
        out = _prettify_output_preview("subagent_return", result, None)
        assert out.startswith("结果被退回:")
        # 60-char cap on the error tail (after the rejection prefix)
        assert "x" * 60 in out
        assert "x" * 61 not in out

    def test_ok_false_prefers_the_user_facing_message(self) -> None:
        """★ 摘要也要走「用户面 / 模型面」那条分离。

        `subagent_return_tool` 的 args-parse 分支两个字段都带：`error` 是写给**模型**的
        自愈指令（「请修正后原样重发；先检查转义…（错误位置 offset=1083，附近原文：…）.
        重发标准 JSON: subagent_return(payload={...AnalysisPayload})」），
        `error_message` 才是给人看的。

        ⚠ 我第一次只在 `_extract_result_error`（失败卡那条路）做了分离，**摘要这条路
        仍直接读 `error`** —— 于是同一段协议文本换条路又摆到客户面前。116 dogfood
        2026-09-08 复查代码时逮到。

        突变：把实现改回 `result.get("error") or …` → 本条红。
        """
        result = {
            "ok": False,
            "error_message": "这一步返回的内容格式不合法，正在重新整理。",
            "error": "args 解析失败：… 重发标准 JSON: subagent_return(payload={...AnalysisPayload}).",
        }
        out = _prettify_output_preview("subagent_return", result, None)
        assert "这一步返回的内容格式不合法" in out
        for internal in ("subagent_return(", "AnalysisPayload", "解析失败"):
            assert internal not in out, f"摘要漏了内部协议文本 {internal!r}：{out!r}"

    def test_ok_false_no_error_field_has_fallback_text(self) -> None:
        result = {"ok": False}
        out = _prettify_output_preview("subagent_return", result, None)
        assert "结果被退回" in out
        assert "返回格式无效" in out

    def test_ok_true_with_summary_unchanged(self) -> None:
        """Success path: summary read still wins over fallback."""
        result = {
            "ok": True,
            "summary": {
                "title": "2025 GMV 同比报告",
                "insights_count": 4,
                "recommendations_count": 3,
            },
        }
        out = _prettify_output_preview("subagent_return", result, None)
        assert "4 洞察" in out
        assert "3 建议" in out

    def test_zero_counts_not_rendered_as_empty_delivery(self) -> None:
        """★ 零计数不写出来——它不代表「这个 agent 什么也没产出」。

        `insights_count` 数的是结构化 payload 的 `insights[]`；简单 / 无数据的 query
        按 prompt 要求走 chat 短答、不许为凑 report 强加 insight，所以把洞察与建议
        讲在正文里的 peer 天然是 0/0。渲成「0 洞察 · 0 建议」会被读成「交了白卷」。

        116 dogfood 实证：一个 peer 穷尽遍历 BI schema、如实报告该工作区结构性缺失
        这两个指标并给出下一步，步骤行仍渲「0 洞察 · 0 建议」，**连写这条带的人
        都据此误判它交了白卷**。

        突变：把 `> 0` 去掉 → 本条必红。
        """
        result = {
            "ok": True,
            "summary": {"insights_count": 0, "recommendations_count": 0},
        }
        out = _prettify_output_preview("subagent_return", result, None)
        assert "0 洞察" not in out
        assert "0 建议" not in out
        assert "结果已交付" in out  # Use the existing fallback, never an empty string.

    def test_zero_counts_with_title_keeps_title(self) -> None:
        """有标题就照渲标题——丢的只是零计数，不是整条摘要。"""
        result = {
            "ok": True,
            "summary": {
                "title": "2026 各门店营业额 TOP5",
                "insights_count": 0,
                "recommendations_count": 0,
            },
        }
        out = _prettify_output_preview("subagent_return", result, None)
        assert "2026 各门店营业额 TOP5" in out
        assert "0 洞察" not in out

    def test_nonzero_counts_still_rendered(self) -> None:
        """正对照：真有产出时计数必须照写（否则「一律不写计数」也能全绿）。"""
        result = {
            "ok": True,
            "summary": {"insights_count": 3, "recommendations_count": 0},
        }
        out = _prettify_output_preview("subagent_return", result, None)
        assert "3 洞察" in out
        assert "0 建议" not in out

    def test_ok_true_no_summary_renders_fallback(self) -> None:
        """没有 summary 的裸成功路：仍要有一句话，且**不含内部协议词**。

        原文案是「payload 已交」——同一个步骤的标签早在 chat-ux-2026-08 批 A 就把
        `payload` 换掉了（「回传 payload」→「整理结果」），摘要这半漏了，
        于是 116 dogfood 2026-09-08 实拍仍是「✅ 整理结果 · payload 已交」。
        """
        result = {"ok": True}
        out = _prettify_output_preview("subagent_return", result, None)
        assert out == "结果已交付"
        assert "payload" not in out.lower()


class TestB4SkillInvokeSurface:
    def test_python_sandbox_via_skill_invoke_surfaces(self) -> None:
        out = _surface_inner_skill_name(
            "skill_invoke",
            {"name": "python_sandbox", "script": "print(1)"},
        )
        assert out == "python_sandbox"

    def test_skill_read_dispatch_also_surfaces(self) -> None:
        out = _surface_inner_skill_name(
            "skill_read",
            {"name": "python_sandbox"},
        )
        assert out == "python_sandbox"

    def test_unknown_inner_skill_keeps_wrapper(self) -> None:
        """Inner skills NOT in the curated surface list pass through as wrapper."""
        out = _surface_inner_skill_name(
            "skill_invoke",
            {"name": "some_random_unlisted_skill"},
        )
        assert out == "skill_invoke"

    def test_non_wrapper_tool_passes_through(self) -> None:
        out = _surface_inner_skill_name("bi_query", {"queries": ["a"]})
        assert out == "bi_query"

    def test_missing_arguments_passes_through(self) -> None:
        out = _surface_inner_skill_name("skill_invoke", None)
        assert out == "skill_invoke"

    def test_non_dict_arguments_passes_through(self) -> None:
        out = _surface_inner_skill_name("skill_invoke", "{name:python_sandbox}")
        assert out == "skill_invoke"

    def test_empty_inner_name_passes_through(self) -> None:
        out = _surface_inner_skill_name("skill_invoke", {"name": ""})
        assert out == "skill_invoke"


def test_rejected_canvas_write_does_not_say_it_was_added() -> None:
    """★★★ 被拒的画布写入不许在步骤卡上写「已添加」。

    116 实测：`add_chart` 有 **11.5%**（166/1442）、`canvas_add_chart` **7.2%**（180/2509）
    的调用因为 `data_artifact_id` 传成了序号（`"15"` / `"40"` 这种）被 `skipped`，
    而这 **366 次全部**在用户面上显示「已添加 · chart」。其中 `t_17e216c2316b`
    被拒 52 次、画布里一张图都没有，用户看到的是 52 条「已添加」。
    """
    rejected = {
        "skipped": "invalid_artifact_id_format",
        "data_artifact_id": "15",
        "hint": "data_artifact_id '15' 不是合法格式 …",
    }
    for tool in ("add_chart", "canvas_add_chart", "add_image", "canvas_add_image"):
        zh = _prettify_output_preview(tool, rejected, None, ui_language="zh-CN")
        en = _prettify_output_preview(tool, rejected, None, ui_language="en-US")
        assert "已添加" not in zh and "未添加" in zh, f"{tool}: {zh}"
        assert "Added" not in en and "Not added" in en, f"{tool}: {en}"
        assert "数据 ID 不对" in zh and "bad data id" in en

    # Expose unregistered reasons verbatim: an internal string is preferable to claiming 「已添加」.
    odd = _prettify_output_preview("add_chart", {"skipped": "some_new_reason"}, None, ui_language="zh-CN")
    assert "未添加" in odd and "some_new_reason" in odd

    # Negative control: an actual addition still says 「已添加」.
    ok = _prettify_output_preview("add_chart", {"section_id": "sec_1", "kind": "line"}, None, ui_language="zh-CN")
    assert ok == "已添加 · line"
