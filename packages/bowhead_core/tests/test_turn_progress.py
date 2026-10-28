"""Transient per-turn agent progress (agent run limits, phase 3)."""

import pytest

from bowhead_core import turn_progress

pytestmark = pytest.mark.unit


def test_steps_are_recorded_per_agent_and_cleared_when_agents_or_the_turn_finish() -> None:
    turn_progress.note_step("t1", "sub_a", profile="compute", display_name="数据分析专家", step=3)
    turn_progress.note_step("t1", "sub_a", profile="compute", display_name="数据分析专家", step=4)
    turn_progress.note_step("t1", "sub_b", profile="kb-analyst", display_name=None, step=1)
    assert sorted((a["subagent_id"], a["step"]) for a in turn_progress.snapshot("t1")) == [("sub_a", 4), ("sub_b", 1)]

    turn_progress.finish("t1", "sub_a")
    assert [a["subagent_id"] for a in turn_progress.snapshot("t1")] == ["sub_b"]
    turn_progress.clear_turn("t1")
    assert turn_progress.snapshot("t1") == []


def test_missing_identity_is_ignored() -> None:
    turn_progress.note_step(None, "sub_a", profile="compute", display_name="", step=1)
    turn_progress.note_step("t2", None, profile="compute", display_name="", step=1)
    assert turn_progress.snapshot("t2") == []
