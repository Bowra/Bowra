"""Tests for state_lifecycle.py — η-#5.

Covers all 6 hooks across 3 paths:
- DefaultStateLifecycle (flag on, persist)
- NoopStateLifecycle (flag off, no-op)
- Pre-η HITL fallback via ClarificationStore (P0-#2 fix)
- Error semantics: on_hitl_pause raises; others swallow
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.state import (
    HitlState,
    HotStateOverflowError,
    PendingAction,
    _default_state,
)
from bowhead_core.state_lifecycle import (
    DefaultStateLifecycle,
    NoopStateLifecycle,
    _reconstruct_state_with_hitl,
    build_lifecycle,
)


def _make_session_store_with_turns(*, turn_with_hot: bool = False, hot_payload: str | None = None):
    sess = MagicMock()
    last_turn = MagicMock()
    if turn_with_hot:
        last_turn.hot_state_json = hot_payload or '{"current_user_message":"hi","active_hitl":null,"pending_actions":[],"recent_context":[]}'
    else:
        last_turn.hot_state_json = None
    sess.list_turns = AsyncMock(return_value=[last_turn])
    sess.update_turn_hot_state = AsyncMock()
    return sess


# ─── DefaultStateLifecycle.on_turn_start ──────────────────


@pytest.mark.asyncio
async def test_on_turn_start_hydrate_from_hot_json() -> None:
    sess = _make_session_store_with_turns(
        turn_with_hot=True,
        hot_payload='{"current_user_message":"hello","active_hitl":null,"pending_actions":[],"recent_context":["entry-1"]}',
    )
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = await lifecycle.on_turn_start(turn_id="t_001", session_id="s_001")
    assert state["current_user_message"] == "hello"
    assert state["recent_context"] == ["entry-1"]


@pytest.mark.asyncio
async def test_on_turn_start_no_history_returns_default() -> None:
    sess = MagicMock()
    sess.list_turns = AsyncMock(return_value=[])  # no turns yet
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = await lifecycle.on_turn_start(turn_id="t_001", session_id="s_new")
    assert state["current_user_message"] == ""
    assert state["recent_context"] == []


@pytest.mark.asyncio
async def test_on_turn_start_pre_eta_hitl_fallback() -> None:
    """P0-#2: pre-η session paused on HITL has no hot_state_json but
    ClarificationStore has active records → reconstruct."""
    sess = _make_session_store_with_turns(turn_with_hot=False)
    clarif = MagicMock()
    candidate = MagicMock()
    candidate.value = "option_A"
    clarif_record = MagicMock()
    clarif_record.request_id = "c_pre_eta"
    clarif_record.kind = "options"
    clarif_record.rationale = "Pick a path"
    clarif_record.candidates = [candidate]
    clarif.active_for_session = AsyncMock(return_value=[clarif_record])

    lifecycle = DefaultStateLifecycle(session_store=sess, clarification_store=clarif)
    state = await lifecycle.on_turn_start(turn_id="t_001", session_id="s_pre_eta")
    assert state["active_hitl"] is not None
    assert state["active_hitl"].clarification_id == "c_pre_eta"
    assert state["active_hitl"].kind == "options"
    assert state["active_hitl"].options == ["option_A"]


@pytest.mark.asyncio
async def test_on_turn_start_hydrate_failure_swallows() -> None:
    """On_turn_start: hydrate exceptions swallowed, return default state."""
    sess = MagicMock()
    sess.list_turns = AsyncMock(side_effect=RuntimeError("db down"))
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = await lifecycle.on_turn_start(turn_id="t", session_id="s")
    assert state["current_user_message"] == ""  # default


# ─── DefaultStateLifecycle.on_turn_end ────────────────────


@pytest.mark.asyncio
async def test_on_turn_end_persists_hot_state() -> None:
    sess = _make_session_store_with_turns()
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = _default_state()
    state["current_user_message"] = "u-msg"
    await lifecycle.on_turn_end(turn_id="t_001", state=state)
    sess.update_turn_hot_state.assert_called_once()
    args = sess.update_turn_hot_state.call_args
    assert args.kwargs["turn_id"] == "t_001"
    assert "u-msg" in args.kwargs["hot_state_json"]


@pytest.mark.asyncio
async def test_on_turn_end_swallows_save_failure() -> None:
    """Save failure on success path is non-fatal."""
    sess = MagicMock()
    sess.list_turns = AsyncMock(return_value=[])
    sess.update_turn_hot_state = AsyncMock(side_effect=RuntimeError("disk full"))
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = _default_state()
    # Should NOT raise
    await lifecycle.on_turn_end(turn_id="t", state=state)


# ─── DefaultStateLifecycle.on_hitl_pause (CRITICAL — must raise) ───


@pytest.mark.asyncio
async def test_on_hitl_pause_persists_and_propagates_failure() -> None:
    """on_hitl_pause MUST raise on save failure (data loss critical)."""
    sess = MagicMock()
    sess.list_turns = AsyncMock(return_value=[])
    sess.update_turn_hot_state = AsyncMock(side_effect=RuntimeError("disk full"))
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = _default_state()
    hitl = HitlState(clarification_id="c1", kind="token", prompt="?")
    with pytest.raises(RuntimeError, match="disk full"):
        await lifecycle.on_hitl_pause(turn_id="t", state=state, hitl=hitl)


@pytest.mark.asyncio
async def test_on_hitl_pause_raises_when_method_missing() -> None:
    """Round 3 P0-2 fix: missing update_turn_hot_state path raises NotImplementedError.
    Previously test only covered side_effect=RuntimeError; missing-method path was untested."""
    # spec= without update_turn_hot_state — only list_turns
    sess = MagicMock(spec=["list_turns"])
    sess.list_turns = AsyncMock(return_value=[])
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = _default_state()
    hitl = HitlState(clarification_id="c1", kind="token", prompt="?")
    with pytest.raises(NotImplementedError, match="update_turn_hot_state"):
        await lifecycle.on_hitl_pause(turn_id="t", state=state, hitl=hitl)


@pytest.mark.asyncio
async def test_on_hitl_pause_updates_state_active_hitl() -> None:
    sess = _make_session_store_with_turns()
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = _default_state()
    hitl = HitlState(clarification_id="c1", kind="options", prompt="?", options=["a"])
    await lifecycle.on_hitl_pause(turn_id="t", state=state, hitl=hitl)
    assert state["active_hitl"] is hitl
    sess.update_turn_hot_state.assert_called_once()


# ─── DefaultStateLifecycle.on_state_overflow ──────────────


@pytest.mark.asyncio
async def test_on_state_overflow_drops_oldest_pending_action() -> None:
    sess = _make_session_store_with_turns()
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = _default_state()
    state["pending_actions"] = [
        PendingAction(action_type="add_chart", payload={"a": 1}, submitted_at="2026-05-08T00:00Z"),
        PendingAction(action_type="canvas_add_section", payload={}, submitted_at="2026-05-08T00:01Z"),
    ]
    err = HotStateOverflowError("over cap")
    await lifecycle.on_state_overflow(state, err)
    # Oldest dropped
    assert len(state["pending_actions"]) == 1
    assert state["pending_actions"][0].action_type == "canvas_add_section"


@pytest.mark.asyncio
async def test_on_state_overflow_pinned_alone_reraises() -> None:
    """No pending_actions to drop → re-raise (anti-metric)."""
    sess = _make_session_store_with_turns()
    lifecycle = DefaultStateLifecycle(session_store=sess)
    state = _default_state()
    state["pending_actions"] = []  # nothing to drop
    err = HotStateOverflowError("pinned alone too big")
    with pytest.raises(HotStateOverflowError):
        await lifecycle.on_state_overflow(state, err)


# ─── NoopStateLifecycle ───────────────────────────────────


@pytest.mark.asyncio
async def test_noop_lifecycle_returns_none() -> None:
    """Round 3 P0-1 fix: Noop returns None so contextvar stays None and
    subagent skips compression on flag-off path."""
    noop = NoopStateLifecycle()
    state = await noop.on_turn_start(turn_id="t", session_id="s")
    assert state is None


@pytest.mark.asyncio
async def test_noop_all_hooks_no_error() -> None:
    noop = NoopStateLifecycle()
    state = _default_state()
    # All hooks safe to call (no-op)
    await noop.on_turn_end("t", state)
    await noop.on_turn_error("t", state, RuntimeError())
    await noop.on_hitl_pause("t", state, HitlState(clarification_id="c", kind="t", prompt="?"))
    await noop.on_turn_cancelled("t", state)
    await noop.on_state_overflow(state, HotStateOverflowError("x"))


# ─── _reconstruct_state_with_hitl ──────────────────────────


def test_reconstruct_state_basic() -> None:
    record = MagicMock()
    record.request_id = "c_001"
    record.kind = "token"
    record.rationale = "ambiguous metric"
    cand = MagicMock()
    cand.value = "metric_A"
    record.candidates = [cand]
    state = _reconstruct_state_with_hitl(record)
    assert state["active_hitl"] is not None
    assert state["active_hitl"].clarification_id == "c_001"
    assert state["active_hitl"].options == ["metric_A"]
    assert state["active_hitl"].metadata["reconstructed_from_clarification_store"] is True


# ─── build_lifecycle factory ───────────────────────────────


def test_build_lifecycle_enabled() -> None:
    sess = MagicMock()
    lc = build_lifecycle(session_store=sess, enabled=True)
    assert isinstance(lc, DefaultStateLifecycle)


def test_build_lifecycle_disabled_returns_noop() -> None:
    sess = MagicMock()
    lc = build_lifecycle(session_store=sess, enabled=False)
    assert isinstance(lc, NoopStateLifecycle)
