"""StateLifecycleHook + default impl — Sprint η.

Per tech-design §1.1 + tech-review-log P0-3/4/6:

6 hooks (start / end / error / hitl_pause / cancelled / state_overflow), each
with explicit error semantics:

| Hook | Error semantic | Persist required? |
|---|---|---|
| on_turn_start | log + fall back to default state | 读 only |
| on_turn_end | log + swallow (re-derive from digest possible) | nice-to-have |
| on_turn_error | log + swallow (turn already on error path) | nice-to-have |
| on_hitl_pause | **raise + fail turn** (data loss critical) | **MUST persist** |
| on_turn_cancelled | log + swallow | nice-to-have |
| on_state_overflow | drop oldest pending_action; if pinned alone > → re-raise | log + abort |

Pre-η HITL fallback (P0-#2): hydrate 时若 hot_state_json 不存在但 ClarificationStore
有 active for session → reconstruct HitlState.

Caller (turn_runner.py η-#6 task) wires lifecycle.on_turn_start at run_turn entry,
on_turn_end on success, on_turn_error / on_hitl_pause / on_turn_cancelled in
exception/HITL paths.

flag-gated by `state.is_state_lifecycle_enabled()`. Off → use NoopStateLifecycle (return
default state, no persist). On → DefaultStateLifecycle.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bowhead_core.state import (
    BowheadState,
    HitlState,
    HotStateOverflowError,
    _default_state,
    deserialize_hot,
    serialize_hot,
)

logger = logging.getLogger(__name__)


class StateLifecycleHook(Protocol):
    """6-event lifecycle protocol — adapter for turn_runner.py.

    Implementations: ``DefaultStateLifecycle`` (flag on, persist to PG) or
    ``NoopStateLifecycle`` (flag off, return default).

    NEW-P1-A fix (round 2): on_turn_start accepts optional user_id / workspace_id
    so DefaultStateLifecycle can pass them to ClarificationStore.active_for_session
    for cross-session leak-防护. Caller (turn_runner) should pass from authenticated
    TurnRequest.
    """

    async def on_turn_start(
        self,
        turn_id: str,
        session_id: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> BowheadState: ...
    async def on_turn_end(self, turn_id: str, state: BowheadState) -> None: ...
    async def on_turn_error(
        self,
        turn_id: str,
        state: BowheadState,
        err: Exception,
    ) -> None: ...
    async def on_hitl_pause(
        self,
        turn_id: str,
        state: BowheadState,
        hitl: HitlState,
    ) -> None: ...
    async def on_turn_cancelled(
        self,
        turn_id: str,
        state: BowheadState,
    ) -> None: ...
    async def on_state_overflow(
        self,
        state: BowheadState,
        error: HotStateOverflowError,
    ) -> None: ...


# ─── DefaultStateLifecycle ────────────────────────────────


class DefaultStateLifecycle:
    """Flag-on impl. Persists HOT state JSON to ``turns.hot_state_json``;
    falls back to ``ClarificationStore`` for pre-η paused HITL (P0-#2 fix).

    Stores typed loose (Any) to avoid hard import dependencies; runtime checks
    via hasattr.
    """

    def __init__(
        self,
        *,
        session_store: Any,
        clarification_store: Any | None = None,
    ) -> None:
        self.session_store = session_store
        self.clarification_store = clarification_store

    async def on_turn_start(
        self,
        turn_id: str,
        session_id: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> BowheadState:
        """Hydrate from turns.hot_state_json; fallback to ClarificationStore for pre-η HITL.

        NEW-P1-A fix: pass ``user_id`` / ``workspace_id`` (from authenticated
        TurnRequest in turn_runner) to ``active_for_session`` for cross-session
        leak-防护.
        """
        try:
            last_turn = await self._get_last_turn_for_session(session_id)
            if last_turn and getattr(last_turn, "hot_state_json", None):
                payload = last_turn.hot_state_json
                state = deserialize_hot(payload)
                logger.debug(
                    "eta.state_hydrate_from_hot_json turn=%s session=%s",
                    turn_id,
                    session_id,
                )
                return state
            # Pre-η HITL fallback (P0-#2)
            if self.clarification_store is not None and hasattr(self.clarification_store, "active_for_session"):
                active = await self.clarification_store.active_for_session(
                    session_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                if active:
                    logger.info(
                        "eta.hitl_reconstruct_from_clarification session=%s requests=%d",
                        session_id,
                        len(active),
                    )
                    return _reconstruct_state_with_hitl(active[0])
        except Exception as e:
            logger.warning(
                "eta.state_hydrate_failed turn=%s session=%s err=%s",
                turn_id,
                session_id,
                e,
            )
        return _default_state()

    async def on_turn_end(self, turn_id: str, state: BowheadState) -> None:
        """Persist HOT state. log + swallow on failure (turn 已 success, lost state non-fatal)."""
        try:
            payload = serialize_hot(state)
            await self._save_hot_state(turn_id, payload)
        except Exception as e:
            logger.warning("eta.state_save_failed turn=%s err=%s", turn_id, e)

    async def on_turn_error(
        self,
        turn_id: str,
        state: BowheadState,
        err: Exception,
    ) -> None:
        """Persist on error path. log + swallow on save failure."""
        try:
            payload = serialize_hot(state)
            await self._save_hot_state(turn_id, payload)
        except Exception as e:
            logger.warning(
                "eta.state_save_on_error_failed turn=%s err=%s save_err=%s",
                turn_id,
                err,
                e,
            )

    async def on_hitl_pause(
        self,
        turn_id: str,
        state: BowheadState,
        hitl: HitlState,
    ) -> None:
        """**Critical** — write failure must raise (HITL state would be lost else).

        ⚠ **WIRING GAP** (round 2 NEW-P1-B): Currently NOT called from turn_runner
        HITL pause path. Production HITL flow still relies on ClarificationStore.put
        + on_turn_start hydrate via _reconstruct_state_with_hitl on next turn.
        Wiring this hook into turn_runner._run_inner term-resolver clarify branch
        deferred to **η+1** sprint. Until then this method's contract is
        verified only by unit test (test_state_lifecycle); production HITL state
        persistence is via the existing pre-η path (ClarificationStore).
        """
        # Update state.active_hitl
        state["active_hitl"] = hitl
        payload = serialize_hot(state)
        # NO try/except: write failure raises, turn fails
        await self._save_hot_state(turn_id, payload)
        logger.info(
            "eta.hitl_state_persisted turn=%s clarification=%s",
            turn_id,
            hitl.clarification_id,
        )

    async def on_turn_cancelled(
        self,
        turn_id: str,
        state: BowheadState,
    ) -> None:
        """Cancellation path. Persist for potential resume; swallow on failure."""
        try:
            payload = serialize_hot(state)
            await self._save_hot_state(turn_id, payload)
        except Exception as e:
            logger.warning(
                "eta.state_save_on_cancel_failed turn=%s err=%s",
                turn_id,
                e,
            )

    async def on_state_overflow(
        self,
        state: BowheadState,
        error: HotStateOverflowError,
    ) -> None:
        """Graceful degrade: drop oldest pending_action; if pinned alone > → re-raise."""
        actions = state.get("pending_actions") or []
        if actions:
            evicted = actions.pop(0)
            logger.warning(
                "eta.state_overflow_drop_action action_type=%s",
                getattr(evicted, "action_type", "?"),
            )
            return
        # Pinned alone exceeds — re-raise (anti-metric per PRD §4.3)
        logger.error("eta.state_overflow_unrecoverable err=%s", error)
        raise error

    # ─── helpers ────────────────────────────────────────

    async def _get_last_turn_for_session(self, session_id: str) -> Any:
        """Find latest turn for session via session_store."""
        if hasattr(self.session_store, "list_turns"):
            turns = await self.session_store.list_turns(session_id=session_id)
            return turns[-1] if turns else None
        return None

    async def _save_hot_state(self, turn_id: str, payload: str) -> None:
        """Persist hot_state_json column. session_store impl-dependent.

        η-#7.5 P1-3 fix: raise NotImplementedError when session_store missing
        the method (silent log → broken HITL contract). Caller decides:
          - on_turn_end / on_turn_error / on_turn_cancelled: catch + warn
          - on_hitl_pause: MUST propagate (contract).
        """
        if hasattr(self.session_store, "update_turn_hot_state"):
            await self.session_store.update_turn_hot_state(turn_id=turn_id, hot_state_json=payload)
            return
        # No fallback — raise so on_hitl_pause failure is visible (P1-3)
        raise NotImplementedError(
            "session_store.update_turn_hot_state missing — η state cannot persist; "
            "either upgrade to SessionStore impl with update_turn_hot_state OR set "
            "BOWHEAD_HOT_WARM_COLD_ENABLED=false to fall back to legacy"
        )


# ─── NoopStateLifecycle (flag off path) ────────────────────


class NoopStateLifecycle:
    """When BOWHEAD_HOT_WARM_COLD_ENABLED=false. Returns default state, no persist."""

    async def on_turn_start(
        self,
        turn_id: str,
        session_id: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> BowheadState | None:
        """Round 3 P0-1 fix: return None so contextvar stays None and subagent
        skips compression entirely on flag-off path. (Default state non-None
        kept compression running, breaking the flag-off rollback contract.)
        """
        return None  # type: ignore[return-value]

    async def on_turn_end(self, turn_id: str, state: BowheadState) -> None:
        return None

    async def on_turn_error(
        self,
        turn_id: str,
        state: BowheadState,
        err: Exception,
    ) -> None:
        return None

    async def on_hitl_pause(
        self,
        turn_id: str,
        state: BowheadState,
        hitl: HitlState,
    ) -> None:
        return None

    async def on_turn_cancelled(
        self,
        turn_id: str,
        state: BowheadState,
    ) -> None:
        return None

    async def on_state_overflow(
        self,
        state: BowheadState,
        error: HotStateOverflowError,
    ) -> None:
        return None


# ─── helpers ────────────────────────────────────────────────


def _reconstruct_state_with_hitl(clarification: Any) -> BowheadState:
    """Build BowheadState with active_hitl from a ClarificationRequest record (P0-#2)."""
    state = _default_state()
    # Map ClarificationRequest fields to HitlState
    options: list[str] | None = None
    if hasattr(clarification, "candidates") and clarification.candidates:
        options = [getattr(c, "value", str(c)) for c in clarification.candidates]
    state["active_hitl"] = HitlState(
        clarification_id=getattr(clarification, "request_id", ""),
        kind=getattr(clarification, "kind", "token"),
        prompt=getattr(clarification, "rationale", ""),
        options=options,
        metadata={"reconstructed_from_clarification_store": True},
    )
    return state


def build_lifecycle(
    *,
    session_store: Any,
    clarification_store: Any | None = None,
    enabled: bool,
) -> StateLifecycleHook:
    """Factory — picks Default vs Noop based on flag."""
    if enabled:
        return DefaultStateLifecycle(
            session_store=session_store,
            clarification_store=clarification_store,
        )
    return NoopStateLifecycle()
