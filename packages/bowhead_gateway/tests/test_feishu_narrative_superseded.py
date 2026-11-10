"""The Feishu status card follows the browser's narrative rules.

116 t_7edbc155f8d8: the Turn's `done` carried `narrative_superseded`, so the browser hid
the bounced question, but the IM card kept it and joined the narrative beats without a
break ("…按方案1执行；…绿色柱状图已经画好…").
"""

from __future__ import annotations

import json

import pytest
from bowhead_gateway.feishu import renderer

pytestmark = pytest.mark.unit

QUESTION = "改色没有成功。您回复「重画」，我就按方案1执行。"
REPLY = "绿色柱状图已经画好，并已放到画布上。"


def _stream(state: renderer.FeishuTurnCardState) -> None:
    for event_type, payload in [
        ("narrative.delta", {"delta": QUESTION}),
        ("step_started", {"step_id": "s1", "tool_name": "subagent_spawn", "header_zh": "派出智能体"}),
        ("step_done", {"step_id": "s1", "state": "success", "output_preview": "Dispatched compute"}),
        ("narrative.delta", {"delta": REPLY}),
    ]:
        renderer.apply_event_to_state(state, event_type, payload)


def test_narrative_beats_are_separated_where_a_step_started() -> None:
    state = renderer.FeishuTurnCardState(status="running", locale="zh-CN")
    _stream(state)
    assert state.narrative == QUESTION + "\n\n" + REPLY
    assert state.narrative_stream == QUESTION + REPLY, "Restart withdrawals keep counting raw code points"
    renderer.apply_event_to_state(state, "llm.stream_restarted", {"discarded_narrative_chars": len(REPLY)})
    assert state.narrative == QUESTION


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("done", {"turn_id": "t_1", "status": "completed", "narrative_superseded": True}),
        ("turn_started", {"turn_id": "t_1", "clarification_id": "clr_1", "narrative_superseded": True}),
    ],
)
def test_superseded_narrative_leaves_the_status_card(event_type: str, payload: dict) -> None:
    state = renderer.FeishuTurnCardState(status="running", locale="zh-CN")
    _stream(state)
    renderer.apply_event_to_state(state, "final_answer", {"text": "旧图仍保留，是否改为重画一张绿色图？"})
    assert renderer.apply_event_to_state(state, event_type, payload) is True
    dumped = json.dumps(renderer.turn_status_card(state), ensure_ascii=False)
    assert QUESTION not in dumped and REPLY not in dumped
    assert "派出智能体" in dumped, "Steps stay visible"


def test_done_without_the_marker_keeps_the_narrative() -> None:
    state = renderer.FeishuTurnCardState(status="running", locale="zh-CN")
    _stream(state)
    assert renderer.apply_event_to_state(state, "done", {"turn_id": "t_1", "status": "completed"}) is False
    assert QUESTION in json.dumps(renderer.turn_status_card(state), ensure_ascii=False)
