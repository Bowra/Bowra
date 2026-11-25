"""Step summaries of dict-shaped Tool errors agree with the failed step state.

an observed execution: failed `add_chart` results (`{"error": "chart_edit_incomplete", …}`)
read "Added · chart" and failed `read_chart` results showed truncated raw JSON, although
the shared outcome extraction marked both steps failed.
"""

from __future__ import annotations

import pytest
from bowhead_core.subagent import _prettify_output_preview as preview
from bowhead_core.tool_outcome import classify_tool_outcome

pytestmark = pytest.mark.unit

_CHART_EDIT = {"error": "chart_edit_incomplete", "hint": "The chart edit did not complete. Retry the in-place edit or report the error."}
_READ_CHART = {
    "section_id": "sec_09014d821ca8",
    "error": "chart_source_projection_unavailable",
    "hint": "Check access to the chart's sources and try again.",
    "chart_guarded": True,
}


@pytest.mark.parametrize("tool", ["add_chart", "canvas_add_chart", "add_image"])
def test_failed_canvas_write_reads_not_added(tool: str) -> None:
    assert classify_tool_outcome(_CHART_EDIT).state == "failure"
    assert preview(tool, _CHART_EDIT, None, ui_language="zh-CN") == "未添加 · 执行出错"
    assert preview(tool, _CHART_EDIT, None, ui_language="en-US") == "Not added · execution error"


@pytest.mark.parametrize(
    ("tool", "result", "zh", "en"),
    [
        ("read_chart", _READ_CHART, "失败: 执行出错", "Failed: execution error"),
        ("edit_chart", {"ok": False, "error_kind": "invalid_chart_binding", "error": "x"}, "失败: 参数无效", "Failed: invalid input"),
        ("bi_query", {"ok": False, "error_kind": "artifact_not_found"}, "失败: 所需内容不可用", "Failed: resource unavailable"),
        ("subagent_spawn", {"subagent_id": "sub_82b29e8dd0", "profile": "compute", "ok": False}, "失败: 执行出错", "Failed: execution error"),
        ("python_sandbox", {"error": "x"}, "运行出错", "Run failed"),
    ],
)
def test_other_failed_results_name_the_failure_without_raw_json(tool: str, result: dict, zh: str, en: str) -> None:
    assert classify_tool_outcome(result).state == "failure"
    assert preview(tool, result, None, ui_language="zh-CN") == zh
    assert preview(tool, result, None, ui_language="en-US") == en
    assert "{" not in preview(tool, result, None, ui_language="en-US")


@pytest.mark.parametrize(
    ("code", "zh", "en"),
    [
        ("not_found", "未接续：本会话没有这个可接着做的运行", "Not continued: no resumable run with this ID in this conversation"),
        ("superseded", "未接续：这次运行已被接着做过，进度在更新的运行里", "Not continued: this run was already continued in a newer run"),
        ("in_use", "未接续：这次运行正在另一条消息里继续", "Not continued: this run is being continued by another message"),
        ("same_turn", "未接续：这次运行刚在本轮停下", "Not continued: this run stopped in this turn"),
        ("unknown", "未接续：之前保存的进度不可用", "Not continued: the saved progress is not available"),
    ],
)
def test_a_refused_resume_names_why(code: str, zh: str, en: str) -> None:
    # an observed execution: Lead guessed resume_checkpoint_id after a clarification; the row read "失败: 执行出错".
    result = {"ok": False, "error_kind": "resume_refused", "resume_refused": code, "error": "No resumable run with this resume_checkpoint_id"}
    assert classify_tool_outcome(result).state == "failure"
    assert preview("subagent_spawn", result, None, ui_language="zh-CN") == zh
    assert preview("subagent_spawn", result, None, ui_language="en-US") == en


def test_expected_outcomes_and_successes_stay_neutral() -> None:
    clarify = {"error_kind": "needs_clarification", "clarification": {"options": [1], "prompt": "Pick a store"}}
    assert preview("bi_query", clarify, None, ui_language="en-US") == "Waiting for you: Pick a store"
    assert preview("add_chart", {"section_id": "sec_1", "kind": "bar"}, None, ui_language="en-US") == "Added · bar"
    assert preview("add_chart", {"skipped": "invalid_artifact_id_format"}, None, ui_language="en-US") == "Not added · bad data id"
