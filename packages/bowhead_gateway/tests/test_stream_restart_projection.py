"""Viewer projection keeps narrative deltas that a Lead stream restart counts on."""

from __future__ import annotations

import pytest
from bowhead_gateway.source_access_projection import _project_legacy_chat_failures


def _project(events: list[tuple[str, dict]]) -> list[dict]:
    return _project_legacy_chat_failures(events, [dict(payload) for _, payload in events])


@pytest.mark.unit
def test_legacy_inline_reasoning_is_still_removed_without_restarts() -> None:
    events = [("narrative.delta", {"delta": "<think>private"}), ("narrative.delta", {"delta": "</think>Answer"})]
    assert [payload["delta"] for payload in _project(events)] == ["", "Answer"]


@pytest.mark.unit
def test_deltas_counted_by_a_lead_restart_reach_the_viewer_unchanged() -> None:
    events = [
        ("narrative.delta", {"delta": "<think>x</think>Partial"}),
        ("llm.retry_scheduled", {"retry": 1}),
        ("llm.stream_restarted", {"subagent_id": "lead_root", "discarded_narrative_chars": 23, "discarded_step_ids": []}),
        ("narrative.delta", {"delta": "Answer"}),
    ]
    projected = _project(events)
    assert projected == [payload for _, payload in events]
