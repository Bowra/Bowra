"""Canonical terminal event contract for a settled approval effect."""

from __future__ import annotations

from collections.abc import Mapping

from bowhead_core.terminal_protocol import (
    APPROVAL_EXECUTION_FINAL_TEXT,
    ActorScope,
    PhaseScope,
    StagedTerminalEvent,
    approval_execution_event_payload,
    canonical_digest_v1,
)


def approval_terminal_event_id(decision_id: str, kind: str) -> str:
    if any(not isinstance(value, str) or not value for value in (decision_id, kind)):
        raise ValueError("approval terminal event identity is invalid")
    return f"evt_{kind}_{canonical_digest_v1({'decision_id': decision_id, 'kind': kind})[:32]}"


def build_approval_required_events(
    *,
    decision_id: str,
    approval_id: str,
    ok: bool,
    result_digest: str | None,
    effect_settlement_payload: Mapping[str, object],
) -> tuple[StagedTerminalEvent, ...]:
    """Build the exact normal/recovery required manifest in canonical order."""

    if not isinstance(effect_settlement_payload, Mapping):
        raise TypeError("approval effect settlement payload is invalid")
    required = [
        StagedTerminalEvent.from_payload(
            event_id=approval_terminal_event_id(decision_id, "effect_settlement"),
            event_type="effect_settlement",
            actor_scope=ActorScope.ROOT,
            phase_scope=PhaseScope.IN_GENERATION,
            payload=dict(effect_settlement_payload),
        ),
        StagedTerminalEvent.from_payload(
            event_id=approval_terminal_event_id(decision_id, "approval_executed"),
            event_type="approval.executed",
            actor_scope=ActorScope.ROOT,
            phase_scope=PhaseScope.IN_GENERATION,
            payload=approval_execution_event_payload(
                approval_id=approval_id,
                ok=ok,
                result_digest=result_digest,
            ),
        ),
    ]
    if ok:
        required.append(
            StagedTerminalEvent.from_payload(
                event_id=approval_terminal_event_id(decision_id, "final_answer"),
                event_type="final_answer",
                actor_scope=ActorScope.ROOT,
                phase_scope=PhaseScope.IN_GENERATION,
                payload={"text": APPROVAL_EXECUTION_FINAL_TEXT},
            )
        )
    else:
        required.append(
            StagedTerminalEvent.from_payload(
                event_id=approval_terminal_event_id(decision_id, "turn_failed"),
                event_type="turn_failed",
                actor_scope=ActorScope.ROOT,
                phase_scope=PhaseScope.IN_GENERATION,
                payload={
                    "message_key": "turn.failed",
                    "reason_code": "terminal_dependency_missing",
                },
            )
        )
    return tuple(required)


__all__ = [
    "approval_terminal_event_id",
    "build_approval_required_events",
]
