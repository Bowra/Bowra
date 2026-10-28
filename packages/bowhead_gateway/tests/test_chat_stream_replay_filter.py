"""Regression: /chat/stream replay must not re-terminate a resumed turn.

A turn paused for clarification/approval persists a ``done`` event carrying a
non-terminal status. On a resume reconnect the FE (use-turn-runner) treats ANY
replayed ``done`` as terminal (``markTerminal``) and disarms its reconnect +
stall-watchdog → the resumed pass (often minutes long) gets stranded and the
user sees "点击 clarification 没返回".

``_replay_rows_for_stream`` drops that stale paused-done — but ONLY when later
events exist, so a genuinely-paused turn (hydrate while awaiting the click) still
replays the marker and settles its HITL card.

Mirrors prod turn t_f2f6a88ad3cc (session s_ec371aac3594): a 210s resume with
51s/71s silent gaps whose full report was generated + persisted but never
delivered to the browser.
"""

from types import SimpleNamespace

import pytest
from bowhead_gateway.routes.chat import (
    _PAUSED_DONE_STATUSES,
    _TERMINAL_TURN_STATUSES,
    _replay_rows_for_stream,
)


def _row(event_type: str, status: str | None = None, seq: int = 0) -> SimpleNamespace:
    payload: dict[str, object] = {"seq": seq}
    if status is not None:
        payload["status"] = status
    return SimpleNamespace(
        event_type=event_type,
        occurred_at=f"2026-06-18T03:00:{seq:02d}+00:00",
        payload=payload,
    )


def _shape(rows: list[SimpleNamespace]) -> list[tuple[str, object]]:
    return [(r.event_type, (r.payload or {}).get("status")) for r in rows]


def test_resumed_turn_drops_stale_paused_done() -> None:
    past = [
        _row("turn_started", seq=1),
        _row("clarification_required", seq=2),
        _row("done", status="clarification_pending", seq=3),  # stale once resumed
        _row("clarification.resolved", seq=4),
        _row("turn_started", seq=5),
        _row("final_answer", seq=6),
        _row("done", status="completed", seq=7),
    ]
    assert _shape(_replay_rows_for_stream(past)) == [
        ("turn_started", None),
        ("clarification_required", None),
        # paused done dropped — would otherwise terminate the FE stream
        ("clarification.resolved", None),
        ("turn_started", None),
        ("final_answer", None),
        ("done", "completed"),  # terminal done preserved
    ]


def test_paused_done_as_last_event_is_kept() -> None:
    # Turn currently paused (page reload while the clarification card is up).
    # Keep the marker so the FE settles the card instead of spinning forever.
    past = [
        _row("turn_started", seq=1),
        _row("clarification_required", seq=2),
        _row("done", status="clarification_pending", seq=3),
    ]
    kept = _replay_rows_for_stream(past)
    assert len(kept) == 3
    assert _shape(kept)[-1] == ("done", "clarification_pending")


def test_approval_pending_paused_done_also_dropped_after_resume() -> None:
    past = [
        _row("turn_started", seq=1),
        _row("done", status="approval_pending", seq=2),  # stale once resumed
        _row("turn_started", seq=3),
        _row("done", status="completed", seq=4),
    ]
    assert _shape(_replay_rows_for_stream(past)) == [
        ("turn_started", None),
        ("turn_started", None),
        ("done", "completed"),
    ]


def test_completed_turn_replays_unchanged() -> None:
    past = [
        _row("turn_started", seq=1),
        _row("final_answer", seq=2),
        _row("done", status="completed", seq=3),
    ]
    assert _replay_rows_for_stream(past) == past


def test_failed_terminal_pair_replays_diagnostics_unchanged() -> None:
    diagnostics = {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "chat_turn_failed",
        "occurrence_id": "0123456789abcdef0123456789abcdef",
        "turn_id": "t_failed",
        "safe_detail": "phase=chat; failure_class=timeout",
        "retryable": True,
    }
    past = [
        SimpleNamespace(
            event_type="turn_failed",
            occurred_at="2026-07-29T12:00:01+00:00",
            payload={
                "reason": "wallclock_exceeded",
                "diagnostics": diagnostics,
            },
        ),
        SimpleNamespace(
            event_type="done",
            occurred_at="2026-07-29T12:00:02+00:00",
            payload={
                "status": "error",
                "reason_code": "wallclock_exceeded",
                "diagnostics": diagnostics,
            },
        ),
    ]

    kept = _replay_rows_for_stream(past)
    assert kept == past
    assert kept[0].payload["diagnostics"] == diagnostics
    assert kept[1].payload["diagnostics"] == diagnostics


def test_legacy_peer_error_is_not_replayed_as_a_root_terminal() -> None:
    peer_error = SimpleNamespace(
        event_type="error",
        occurred_at="2026-07-29T12:00:01+00:00",
        payload={
            "parent_agent_id": "sub_legacy",
            "message": "subagent_internal_error",
        },
    )
    completed = _row("done", status="completed", seq=2)

    assert _replay_rows_for_stream([peer_error, completed]) == [completed]


def test_canonical_status_drops_stale_root_terminal_and_keeps_latest_match() -> None:
    past = [
        _row("turn_started", seq=1),
        _row("done", status="completed", seq=2),
        _row("done", status="error", seq=3),
        _row("done", status="completed", seq=4),
    ]

    assert _shape(
        _replay_rows_for_stream(
            past,
            canonical_turn_status="completed",
            validate_terminal_status=True,
        ),
    ) == [
        ("turn_started", None),
        ("done", "completed"),
    ]


def test_running_or_unavailable_store_drops_unproved_root_terminals() -> None:
    progress = _row("partial_answer", seq=1)
    stale_done = _row("done", status="completed", seq=2)
    root_error = _row("error", seq=3)

    for canonical_status in ("running", None):
        assert _replay_rows_for_stream(
            [progress, stale_done, root_error],
            canonical_turn_status=canonical_status,
            validate_terminal_status=True,
        ) == [progress]


def test_failed_wire_alias_matches_canonical_failed_status() -> None:
    old_root_error = _row("error", seq=1)
    latest_done = _row("done", status="error", seq=2)

    assert _replay_rows_for_stream(
        [old_root_error, latest_done],
        canonical_turn_status="failed",
        validate_terminal_status=True,
    ) == [latest_done]


def test_canonical_completed_drops_stale_failed_semantic_pair() -> None:
    final_answer = _row("final_answer", seq=1)
    stale_failed = _row("turn_failed", seq=2)
    stale_done = _row("done", status="error", seq=3)
    corrected_done = _row("done", status="completed", seq=4)

    assert _replay_rows_for_stream(
        [final_answer, stale_failed, stale_done, corrected_done],
        canonical_turn_status="completed",
        validate_terminal_status=True,
    ) == [final_answer, corrected_done]


def test_running_or_unavailable_store_drops_stale_semantic_terminal() -> None:
    progress = _row("partial_answer", seq=1)
    stale_failed = _row("turn_failed", seq=2)

    for canonical_status in ("running", None):
        assert _replay_rows_for_stream(
            [progress, stale_failed],
            canonical_turn_status=canonical_status,
            validate_terminal_status=True,
        ) == [progress]


@pytest.mark.parametrize(
    ("wire_status", "canonical_status"),
    [
        ("clarification_pending", "clarification_pending"),
        ("approval_pending", "awaiting_approval"),
    ],
)
def test_canonical_pause_survives_later_conflicting_terminal(
    wire_status: str,
    canonical_status: str,
) -> None:
    required = _row("clarification_required", seq=1)
    canonical_pause = _row("done", status=wire_status, seq=2)
    stale_completed = _row("done", status="completed", seq=3)

    assert _replay_rows_for_stream(
        [required, canonical_pause, stale_completed],
        canonical_turn_status=canonical_status,
        validate_terminal_status=True,
    ) == [required, canonical_pause]


def test_two_paused_dones_only_trailing_terminal_kept() -> None:
    # Defensive: clarify then approve in one turn. Every paused done with later
    # events is dropped; the terminal completed done survives.
    past = [
        _row("done", status="clarification_pending", seq=1),
        _row("turn_started", seq=2),
        _row("done", status="approval_pending", seq=3),
        _row("turn_started", seq=4),
        _row("done", status="completed", seq=5),
    ]
    assert _shape(_replay_rows_for_stream(past)) == [
        ("turn_started", None),
        ("turn_started", None),
        ("done", "completed"),
    ]


def test_pause_statuses_cover_both_hitl_kinds() -> None:
    assert "clarification_pending" in _PAUSED_DONE_STATUSES
    assert "approval_pending" in _PAUSED_DONE_STATUSES


def test_terminal_statuses_cover_all_non_paused_ends() -> None:
    """#9 (dingsiyan 2026-07-07): the stream-reconnect synthetic-terminal gate in
    chat.stream() must cover EVERY truly-terminal status, not just completed/failed —
    else a cancelled/aborted turn whose ``done`` row was lost (best-effort log_sink)
    sticks reconnecting until the 10-min give-up. Paused HITL statuses stay OUT: the
    turn may resume, so the stream must keep subscribing instead of short-circuiting."""
    assert {"completed", "failed", "cancelled", "aborted"} <= _TERMINAL_TURN_STATUSES
    assert _TERMINAL_TURN_STATUSES.isdisjoint(_PAUSED_DONE_STATUSES)


def test_empty_log_is_noop() -> None:
    assert _replay_rows_for_stream([]) == []
