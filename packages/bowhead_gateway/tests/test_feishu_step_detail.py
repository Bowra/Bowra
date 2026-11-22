"""The Feishu status card labels steps the way the browser does.

An empty `input_preview` means the step has no readable label (model-only arguments or a
bare internal ID); the browser then shows only the Tool name. 116 dogfood of 1.7.21 showed
"Read Chart · sec_4a5c8d7d2b6e"; the card must not substitute the raw arguments instead.
"""

from __future__ import annotations

import pytest
from bowhead_gateway.feishu import renderer

pytestmark = pytest.mark.unit

ARGUMENTS = '{"section_id": "sec_4a5c8d7d2b6e"}'


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"input_preview": "", "arguments_preview": ARGUMENTS}, ""),
        ({"input_preview": "2024 营业额", "arguments_preview": ARGUMENTS}, "2024 营业额"),
        # Events from emitters without a label keep the earlier fallback.
        ({"arguments_preview": "sales.xlsx"}, "sales.xlsx"),
    ],
)
def test_step_detail_never_replaces_an_empty_label_with_raw_arguments(payload, detail) -> None:
    state = renderer.FeishuTurnCardState(status="running", locale="en-US")
    renderer.apply_event_to_state(state, "step_started", {"step_id": "s1", "tool_name": "read_chart", **payload})
    assert state.steps["s1"].detail == detail


def test_code_being_written_stays_out_of_the_card() -> None:
    """Turn visibility FR5: the card has no code box, so streamed code arguments never become a row; the run row stays."""
    state = renderer.FeishuTurnCardState(status="running", locale="en-US")
    writing = {
        "step_id": "writing_c1",
        "step_type": "lead_thinking_writing",
        "tool_name": "python_sandbox",
        "code_language": "python",
        "header_zh": "写代码",
    }
    assert renderer.apply_event_to_state(state, "step_started", writing) is False
    assert renderer.apply_event_to_state(state, "step_progress", {"step_id": "writing_c1", "content_delta": '{"code": "print(1)'}) is False
    assert renderer.apply_event_to_state(state, "step_done", {"step_id": "writing_c1", "state": "success"}) is False
    renderer.apply_event_to_state(
        state, "step_started", {"step_id": "step_c1", "step_type": "tool_run", "tool_name": "python_sandbox", "input_preview": "Draw the chart"}
    )
    assert list(state.steps) == ["step_c1"]
    # Lead's query writing keeps its row, as before.
    renderer.apply_event_to_state(
        state, "step_started", {"step_id": "writing_q1", "step_type": "lead_thinking_writing", "tool_name": "bi_query", "header_zh": "写查询 (SQL)"}
    )
    assert "writing_q1" in state.steps
