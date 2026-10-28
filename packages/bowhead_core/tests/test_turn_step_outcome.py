"""``summarize_step_outcome``: structural recovery of recorded step issues (chat turn feedback D6)."""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.turn_step_outcome import (
    STEP_OUTCOME_RECORD_CAP,
    StepFact,
    step_fact_from_event,
    step_outcome_field,
    summarize_step_outcome,
)

pytestmark = pytest.mark.unit


class _Facts:
    def __init__(self) -> None:
        self.items: list[StepFact] = []

    def _add(self, event_type: str, payload: dict[str, Any], owner: str | None = None) -> None:
        fact = step_fact_from_event(event_type, payload, parent_agent_id=owner, seq=len(self.items) + 1)
        assert fact is not None
        self.items.append(fact)

    def step(self, step_id: str, state: str, *, tool: str = "bi_query", owner: str | None = None, call_id: str | None = None) -> None:
        payload = {"step_id": step_id, "tool_name": tool, "call_id": call_id or f"call_{step_id}"}
        self._add("step_started", payload, owner)
        self._add("step_done", {**payload, "state": state}, owner)

    def started(self, step_id: str, *, owner: str | None = None) -> None:
        self._add("step_started", {"step_id": step_id, "tool_name": "bi_query"}, owner)

    def peer(self, subagent_id: str, profile: str, status: str, *, call_id: str | None = None) -> None:
        self._add(
            "lead.subagent_summary",
            {"subagent_id": subagent_id, "profile": profile, "status": status, "call_id": call_id},
        )

    def peer_failed(self, subagent_id: str, profile: str, *, call_id: str | None = None) -> None:
        self._add("peer_dispatch_failed", {"subagent_id": subagent_id, "peer_name": profile, "call_id": call_id})


def _local_issue_count(facts: _Facts) -> int:
    """The pre-D6 frontend count: recorded failed/partial rows."""
    return sum(1 for fact in facts.items if fact.event_type == "step_done" and fact.state in ("failure", "partial"))


def test_a_later_same_owner_same_tool_success_recovers_the_issue() -> None:
    facts = _Facts()
    facts.step("s1", "failure")
    facts.step("s2", "success")

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert outcome == {"schema_version": 1, "issue_steps": 1, "recovered_steps": 1, "unresolved_steps": 0, "answer_affected": False}


def test_a_success_by_another_owner_or_tool_or_before_the_failure_is_not_a_recovery() -> None:
    facts = _Facts()
    facts.step("s0", "success")  # earlier, same owner + tool
    facts.step("s1", "failure")
    facts.step("s2", "success", tool="python_sandbox")
    facts.step("s3", "success", owner="sub_peer")

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert outcome["recovered_steps"] == 0
    assert outcome["unresolved_steps"] == 1


def test_a_failed_peer_redispatched_successfully_recovers_its_steps() -> None:
    facts = _Facts()
    facts.step("p1", "failure", owner="sub_a")
    facts.step("spawn_a", "failure", tool="subagent_spawn", call_id="call_spawn_a")
    facts.peer_failed("sub_a", "compute", call_id="call_spawn_a")
    facts.peer("sub_a", "compute", "failed", call_id="call_spawn_a")
    facts.step("spawn_b", "success", tool="subagent_spawn", call_id="call_spawn_b")
    facts.peer("sub_b", "compute", "succeeded", call_id="call_spawn_b")

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert outcome["issue_steps"] == 2
    assert outcome["recovered_steps"] == 2
    assert outcome["answer_affected"] is False


def test_a_spawn_of_a_different_peer_does_not_recover_a_failed_peer() -> None:
    facts = _Facts()
    facts.step("spawn_a", "failure", tool="subagent_spawn", call_id="call_spawn_a")
    facts.peer_failed("sub_a", "compute", call_id="call_spawn_a")
    facts.step("spawn_b", "success", tool="subagent_spawn", call_id="call_spawn_b")
    facts.peer("sub_b", "research", "succeeded", call_id="call_spawn_b")

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert outcome["recovered_steps"] == 0
    assert outcome["unresolved_steps"] == 1
    assert outcome["answer_affected"] is True


def test_an_unresolved_partial_batch_affects_the_answer() -> None:
    facts = _Facts()
    facts.step("s1", "partial", tool="bi_query_batch")

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert outcome["unresolved_steps"] == 1
    assert outcome["answer_affected"] is True


def test_an_unresolved_lead_failure_does_not_affect_the_answer() -> None:
    facts = _Facts()
    facts.step("s1", "failure")
    facts.step("s2", "failure", tool="kb_read")
    facts.step("s3", "success")  # recovers s1 only

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert _local_issue_count(facts) == 2
    assert outcome["issue_steps"] == 2
    assert outcome["recovered_steps"] == 1
    assert outcome["unresolved_steps"] == 1
    assert outcome["answer_affected"] is False


def test_a_peer_that_ended_with_warnings_affects_the_answer() -> None:
    facts = _Facts()
    facts.step("p1", "failure", owner="sub_a")
    facts.peer("sub_a", "compute", "succeeded_with_warnings")

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert outcome["answer_affected"] is True


def test_a_started_step_without_a_done_is_unresolved() -> None:
    facts = _Facts()
    facts.started("s1")
    facts.step("s2", "success")

    outcome = summarize_step_outcome(facts.items, status="completed")

    assert outcome["issue_steps"] == 0
    assert outcome["unresolved_steps"] == 1
    assert outcome["answer_affected"] is False


def test_a_failed_or_cancelled_turn_never_claims_an_affected_answer() -> None:
    facts = _Facts()
    facts.step("s1", "partial", tool="bi_query_batch")

    for status in ("error", "failed", "cancelled"):
        assert summarize_step_outcome(facts.items, status=status)["answer_affected"] is False


def test_the_last_state_of_a_step_counts_once() -> None:
    facts = _Facts()
    facts.step("s1", "failure")
    facts._add("step_done", {"step_id": "s1", "tool_name": "bi_query", "state": "failure"})

    assert summarize_step_outcome(facts.items, status="completed")["issue_steps"] == 1


@pytest.mark.asyncio
async def test_the_bus_records_facts_and_omits_the_field_past_the_cap() -> None:
    from bowhead_core.events import OrchestratorEvent

    bus = TurnEventBus()
    await bus.emit(OrchestratorEvent(event_type="step_started", turn_id="t1", payload={"step_id": "s1", "tool_name": "bi_query"}))
    await bus.emit(OrchestratorEvent(event_type="step_done", turn_id="t1", payload={"step_id": "s1", "tool_name": "bi_query", "state": "failure"}))
    field = step_outcome_field(bus, "completed")
    assert field["step_outcome"]["issue_steps"] == 1

    for index in range(STEP_OUTCOME_RECORD_CAP):
        await bus.emit(OrchestratorEvent(event_type="step_started", turn_id="t1", payload={"step_id": f"x{index}"}))

    assert step_outcome_field(bus, "completed") == {}


def test_terminal_v1_and_foreign_buses_omit_the_field() -> None:
    class _V1:
        terminal_protocol_version = 1
        _step_outcome_facts: list[StepFact] = []

    assert step_outcome_field(_V1(), "completed") == {}
    assert step_outcome_field(object(), "completed") == {}


@pytest.mark.asyncio
async def test_a_continuation_generation_omits_the_field() -> None:
    """After a clarification the resumed bus starts past earlier generations' events; its facts
    would under-report, so the outcome is unknown and the frontend keeps the neutral local count."""
    from bowhead_core.events import OrchestratorEvent

    resumed = TurnEventBus(initial_seq=12)
    await resumed.emit(
        OrchestratorEvent(event_type="step_done", turn_id="t1", payload={"step_id": "s9", "tool_name": "bi_query", "state": "success"})
    )
    assert step_outcome_field(resumed, "completed") == {}

    reattached = TurnEventBus()
    reattached.seed_seq_counter(7)
    assert step_outcome_field(reattached, "completed") == {}

    fresh = TurnEventBus()
    assert step_outcome_field(fresh, "completed")["step_outcome"]["issue_steps"] == 0
