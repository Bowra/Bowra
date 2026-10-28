"""Chat turn feedback signals D3/D5/D6 through the real Lead done chokepoint.

Design: ``docs/design/chat-turn-feedback-signals-tech-design.md``. The chat invariant is that live,
persisted and replayed data agree, so each case captures the live bus ring and the persisted
``turn_events`` rows (the replay source) and the turn row, and compares them field by field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bowhead_core.agents.lead_orchestrator import _emit_event
from bowhead_core.events import EventBusRegistry, OrchestratorEvent
from bowhead_core.published_diagnostics import build_chat_turn_failure
from bowhead_core.session_store import SessionStore
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore


async def _fixture(tmp_path: Path, turn_id: str) -> tuple[SessionStore, TurnLogStore, EventBusRegistry, Any]:
    from bowhead_cli.migrate import TARGET_FILES, upgrade

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    session_id = f"s_{turn_id}"
    await store.ensure_session(session_id=session_id, workspace_id="ws1", user_id="u1")
    await store.record_turn(turn_id=turn_id, session_id=session_id, user_text="question")
    registry = EventBusRegistry(default_log_sink=OrderedTurnLogSink(log_store))
    bus = await registry.get_or_create(turn_id, exclusive_producer=True, persist_before_live=True)
    bus._pi_turn_state = {  # type: ignore[attr-defined]
        "intent": None,
        "final_answer": "",
        "tokens_input": 0,
        "tokens_output": 0,
        "cost_usd": 0.0,
        "error": None,
        "error_kind": None,
    }

    async def persist(**kwargs: Any) -> bool:
        if kwargs["status_value"] in ("error", "failed"):
            return await store.mark_failed(
                turn_id=kwargs["turn_id"],
                error=kwargs["error"] or "",
                error_kind=kwargs["error_kind"] or "internal",
                intent=kwargs["intent"],
                final_answer=kwargs["final_answer"],
            )
        return await store.mark_completed(
            turn_id=kwargs["turn_id"],
            intent=kwargs["intent"],
            final_answer=kwargs["final_answer"],
            tokens_input=kwargs["tokens_input"],
            tokens_output=kwargs["tokens_output"],
            cost_usd=kwargs["cost_usd"],
        )

    bus._pi_persist_turn_state = persist  # type: ignore[attr-defined]
    return store, log_store, registry, bus


async def _step(
    bus: Any, turn_id: str, event_type: str, step_id: str, *, state: str | None = None, tool: str = "bi_query", owner: str | None = None
) -> None:
    payload: dict[str, Any] = {"card_id": turn_id, "step_id": step_id, "tool_name": tool, "call_id": f"call_{step_id}"}
    if state is not None:
        payload["state"] = state
    await bus.emit(OrchestratorEvent(event_type=event_type, turn_id=turn_id, payload=payload, parent_agent_id=owner))  # type: ignore[arg-type]


def _live(bus: Any, event_type: str) -> list[dict[str, Any]]:
    return [event.payload for event in bus.snapshot() if event.event_type == event_type]


async def _persisted(log_store: TurnLogStore, turn_id: str, event_type: str) -> list[dict[str, Any]]:
    return [event.payload for event in await log_store.list_events(turn_id, event_types=(event_type,))]


@pytest.mark.asyncio
async def test_lead_failure_publishes_one_failure_statement_and_one_recovery_action(tmp_path: Path) -> None:
    """D3 + D5 on a Lead failure path: no fallback answer, identical recovery action live and persisted."""

    turn_id = "t_feedback_failure"
    store, log_store, registry, bus = await _fixture(tmp_path, turn_id)
    # A carried system failure sentence (the pre-D3 producers put one here) must not surface.
    bus._pi_turn_state["final_answer"] = "本次分析未能完成。请根据错误提示重试。"  # type: ignore[attr-defined]
    bus._pi_turn_state.update({"error": "wallclock_exceeded", "error_kind": "timeout"})  # type: ignore[attr-defined]
    failure = build_chat_turn_failure(reason_code="wallclock_exceeded", turn_id=turn_id)

    await _emit_event(bus, turn_id, "turn_failed", {"reason": failure.reason_code, "diagnostics": failure.diagnostics})
    await _emit_event(
        bus,
        turn_id,
        "done",
        {
            "turn_id": turn_id,
            "status": "error",
            "reason_code": failure.reason_code,
            "error": failure.reason_code,
            "error_kind": "timeout",
            "diagnostics": failure.diagnostics,
        },
    )

    assert _live(bus, "final_answer") == []
    assert await _persisted(log_store, turn_id, "final_answer") == []
    turn = await store.get_turn(turn_id)
    assert turn is not None and turn.status == "failed"
    assert turn.final_answer in ("", None)
    assert turn.error_kind == "timeout"

    live_failed, live_done = _live(bus, "turn_failed"), _live(bus, "done")
    stored_failed = await _persisted(log_store, turn_id, "turn_failed")
    stored_done = await _persisted(log_store, turn_id, "done")
    assert len(live_failed) == len(stored_failed) == len(live_done) == len(stored_done) == 1
    actions = {
        live_failed[0]["recovery_action"],
        stored_failed[0]["recovery_action"],
        live_done[0]["recovery_action"],
        stored_done[0]["recovery_action"],
        failure.recovery_action,
    }
    assert actions == {"edit_and_resend"}
    # D6 also rides on the failed done; a failed turn never claims an affected answer.
    assert live_done[0]["step_outcome"] == stored_done[0]["step_outcome"]
    assert live_done[0]["step_outcome"]["answer_affected"] is False
    await registry.close(turn_id, expected=bus)


@pytest.mark.asyncio
async def test_completed_done_carries_the_same_step_outcome_live_and_persisted(tmp_path: Path) -> None:
    """D6: the structural outcome on the live done equals the persisted done (the replay source).

    The fixture makes the old local count (2 issue rows) and the resolved count diverge: one failure
    is recovered by a later same-tool success, one partial batch stays unresolved.
    """

    turn_id = "t_feedback_steps"
    store, log_store, registry, bus = await _fixture(tmp_path, turn_id)
    await _step(bus, turn_id, "step_started", "s1")
    await _step(bus, turn_id, "step_done", "s1", state="failure")
    await _step(bus, turn_id, "step_started", "s2")
    await _step(bus, turn_id, "step_done", "s2", state="success")
    await _step(bus, turn_id, "step_started", "s3", tool="bi_query_batch")
    await _step(bus, turn_id, "step_done", "s3", state="partial", tool="bi_query_batch")
    bus._final_answer_emitted = True  # type: ignore[attr-defined]  # the answer already streamed

    await _emit_event(bus, turn_id, "done", {"turn_id": turn_id, "status": "completed"})

    live_done = _live(bus, "done")
    stored_done = await _persisted(log_store, turn_id, "done")
    expected = {
        "schema_version": 1,
        "issue_steps": 2,
        "recovered_steps": 1,
        "unresolved_steps": 1,
        "answer_affected": True,
    }
    assert live_done[0]["step_outcome"] == stored_done[0]["step_outcome"] == expected
    # A completed done has no failure, so it carries no recovery action.
    assert "recovery_action" not in live_done[0]
    await registry.close(turn_id, expected=bus)
