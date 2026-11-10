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
