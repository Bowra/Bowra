"""δ lifecycle hooks — adapter pattern for turn_runner integration.

Mirrors η ``state_lifecycle.py``: thin adapter so the hot-path turn_runner
calls a few well-defined hooks instead of reaching into δ internals. All hooks
are flag-gated; off-state returns no-op so the legacy path is bit-for-bit
identical to pre-δ.

Hooks the runner calls:

- ``on_pre_llm`` — return active_facts block to splice into system prompt.
- ``on_post_subagent_return`` — Confidence Gate intercept (stub for δ-#7;
  full activation requires LLM-emitted ``ConfidenceJudgment`` JSON in payload,
  landing post δ-#8 eval calibration).
- ``on_post_turn`` — fire-and-forget auto_learner classify on user_directive
  (HITL resolve text or top-level user input). Result writes RegisteredFact.

A "noop" lifecycle (returned when master flag off) returns empty / None for
every hook so the integration site can call unconditionally.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from bowhead_core.auto_learner import classify
from bowhead_core.confidence_gate import (
    DEFAULT_THRESHOLD,
    ConfidenceJudgment,
    load_threshold_for_model,
)
from bowhead_core.confidence_gate import (
    gate as _gate_decision,
)
from bowhead_core.provenance_flags import (
    is_feature_enabled,
    is_provenance_hitl_enabled,
)
from bowhead_core.provenance_prompts import (
    build_active_facts_block,
)
from bowhead_core.verification_provenance import (
    ProvenanceVerification,
    post_llm_verify_provenance,
)

if TYPE_CHECKING:
    from bowhead_core.registered_fact_store import RegisteredFactStore
    from bowhead_router import Router

logger = logging.getLogger(__name__)


# δ-#8.5 Backend P0-1 + SRE P0-3: hold strong refs to fire-and-forget classifier
# tasks so CPython doesn't GC them mid-await. Tasks self-discard on completion.
# Exposed via ``pending_tasks`` for shutdown hooks (await-and-gather on SIGTERM).
_BG_TASKS: set[asyncio.Task[Any]] = set()


def pending_tasks() -> set[asyncio.Task[Any]]:
    """Strong-ref view of in-flight background classifier tasks.

    Caller (orchestrator shutdown / process-exit handler) can
    ``await asyncio.gather(*pending_tasks(), return_exceptions=True)``
    with a timeout to drain before closing the event loop.
    """
    return set(_BG_TASKS)


def _track_task(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


@dataclass
class GateOutcome:
    """Confidence Gate decision payload returned by ``on_post_subagent_return``.

    Fields:
        action: "use_silent" | "use_with_surface" | "trigger_hitl" | None (no judge emitted).
        judgment: source ConfidenceJudgment (None when no judge data found in payload).
        threshold: per-model threshold used.
    """

    action: str | None
    judgment: ConfidenceJudgment | None
    threshold: int


class DeltaLifecycleProtocol(Protocol):
    """Interface; both real and Noop implementations conform."""

    def is_enabled(self) -> bool: ...

    async def on_pre_llm(
        self,
        *,
        workspace_id: str,
        user_text: str,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> str: ...

    async def on_post_subagent_return(
        self,
        payload: dict[str, Any],
        *,
        model: str = "",
    ) -> GateOutcome: ...

    async def on_post_turn(
        self,
        *,
        directive: str,
        workspace_id: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        hitl_ctx: dict[str, Any] | None = None,
    ) -> asyncio.Task[Any] | None: ...

    async def on_post_verify(
        self,
        text: str,
        *,
        workspace_id: str,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> ProvenanceVerification: ...


class NoopDeltaLifecycle:
    """All hooks return empty / None when master flag off."""

    def is_enabled(self) -> bool:
        return False

    async def on_pre_llm(self, **_kw: Any) -> str:
        return ""

    async def on_post_subagent_return(
        self,
        payload: dict[str, Any],
        *,
        model: str = "",
    ) -> GateOutcome:
        return GateOutcome(action=None, judgment=None, threshold=DEFAULT_THRESHOLD)

    async def on_post_turn(self, **_kw: Any) -> asyncio.Task[Any] | None:
        return None

    async def on_post_verify(self, text: str, **_kw: Any) -> ProvenanceVerification:
        return ProvenanceVerification(passed=True)


class DefaultDeltaLifecycle:
    """Real hooks. Constructed when master flag on."""

    def __init__(
        self,
        *,
        fact_store: RegisteredFactStore,
        router: Router | None = None,
        on_modification_request: Any = None,
    ) -> None:
        self._fact_store = fact_store
        self._router = router
        #: Track B batch 1a: pass fact to caller when classified as modification_request.
        #: This class **does not touch memory**; turn finalization decides writes and coordination with the other capture paths.
        self._on_modification_request = on_modification_request

    def is_enabled(self) -> bool:
        return True

    async def on_pre_llm(
        self,
        *,
        workspace_id: str,
        user_text: str,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        try:
            return await build_active_facts_block(
                workspace_id=workspace_id,
                user_text=user_text,
                fact_store=self._fact_store,
                project_id=project_id,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("delta.lifecycle.on_pre_llm_failed err=%s", e)
            return ""

    async def on_post_subagent_return(
        self,
        payload: dict[str, Any],
        *,
        model: str = "",
    ) -> GateOutcome:
        """Parse ``confidence_judgment`` from subagent_return payload, gate it.

        Payload schema (LLM emits):
            {"confidence_judgment": {"claim": "...", "proposed_source": "[llm-std:]",
                                     "confidence": 8, "concern": "..."}}

        Missing / malformed → action=None (caller falls through to legacy path).
        Returns ``GateOutcome`` with action + threshold for caller to inspect /
        emit metrics / trigger HITL.
        """
        if not is_feature_enabled("HITL_CONFIRM"):
            return GateOutcome(action=None, judgment=None, threshold=DEFAULT_THRESHOLD)
        threshold = load_threshold_for_model(model) if model else DEFAULT_THRESHOLD
        raw = payload.get("confidence_judgment") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return GateOutcome(action=None, judgment=None, threshold=threshold)
        try:
            judgment = ConfidenceJudgment(
                claim=str(raw.get("claim") or "")[:500],
                proposed_source=str(raw.get("proposed_source") or "")[:200],
                confidence=int(raw.get("confidence") or 0),
                concern=str(raw["concern"]) if raw.get("concern") else None,
            )
        except (TypeError, ValueError) as e:
            logger.warning("delta.lifecycle.bad_conf_judgment err=%s", e)
            return GateOutcome(action=None, judgment=None, threshold=threshold)
        action = _gate_decision(judgment, threshold=threshold)
        return GateOutcome(action=action, judgment=judgment, threshold=threshold)

    async def on_post_turn(
        self,
        *,
        directive: str,
        workspace_id: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        hitl_ctx: dict[str, Any] | None = None,
    ) -> asyncio.Task[Any] | None:
        """Fire-and-forget classify+write. Returns the Task for testability;
        production caller can ignore. ``directive`` empty / router missing → None.
        """
        if not directive or not directive.strip():
            return None
        if self._router is None:
            return None
        from bowhead_core.terminal_llm_invocation import (
            create_detached_auxiliary_task,
        )

        task = create_detached_auxiliary_task(
            self._classify_and_write(
                directive=directive,
                workspace_id=workspace_id,
                session_id=session_id,
                turn_id=turn_id,
                project_id=project_id,
                user_id=user_id,
                hitl_ctx=hitl_ctx,
            ),
            reason="delta_auto_learner",
        )
        # δ-#8.5: track strong ref + self-discard on completion. Without this,
        # CPython can GC the task before classify resolves (esp. under GC
        # pressure with many fire-and-forget tasks), causing "Task was destroyed
        # but it is pending" warnings AND silent dropped fact writes.
        return _track_task(task)

    async def _classify_and_write(
        self,
        *,
        directive: str,
        workspace_id: str,
        session_id: str | None,
        turn_id: str | None,
        project_id: str | None,
        user_id: str | None,
        hitl_ctx: dict[str, Any] | None,
    ) -> None:
        if self._router is None:  # pragma: no cover — guarded above
            return
        # ★ r3 (2026-09-09, user decision to delete): the four old categories
        # （data_correction / formula_definition / concept_definition / preference）
        # were **removed entirely**, along with default-off `FEATURE_DELTA_AUTO_LEARN_FACTS`.
        #
        # Removed because the path was **misplaced**, not broken: `scope=project` became
        # `user_id IS NULL`, shared workspace scope (`registered_fact_store._scope_filter`),
        # meaning **model-decided, unconfirmed writes directly visible to the team**.
        # Under the personal/team domains as two write entries design, that was the sole door that should not exist.
        # A disabled flag is insufficient: someone will reopen the door without knowing
        # the team-visible scope behind it. Design authority: tech-design §1.4.
        #
        # ⚠ Safe removal: `on_post_turn` was never called; 116 `registered_facts` still has 0 rows
        # (verified 2026-09-08). **Retain** `RegisteredFactStore`: the reader
        # (`build_active_facts_block` system-prompt injection) remains live, now without production writers;
        # **recorded in tech-design §1.4b for a separate batch** (touches the hot prompt-assembly path).
        # ★★ This check must precede `classify()`. Checking afterward blocks only downstream writes,
        # while `classify` itself is **a paid LLM call** (echo tier, concurrency 20, 4000-character
        # directive), plus `_sanitize_directive` on every user message.
        #
        # ⚠ This is the **second occurrence**: the old four-category gate occupied this position; r3 removed it
        # with its flag, leaving `classify()` unguarded. The personal-memory write gate
        # (`auto_capture_enabled`) is **farther downstream**, wasting a classification call every turn
        # while writes remain disabled. Before removing a gate, inspect what it protects.
        #
        # This embodies two write entries: the team-visible path **is removed**;
        # personal memory uses its own `FEATURE_MEMORY_AUTO_CAPTURE`.
        from bowhead_core.memory_auto_capture import auto_capture_enabled

        if not auto_capture_enabled():
            return

        try:
            fact = await classify(
                directive,
                router=self._router,
                hitl_ctx=hitl_ctx,
                source_session_id=session_id,
                source_turn_id=turn_id,
            )
        except Exception as e:
            logger.warning("delta.lifecycle.classify_failed err=%s", e)
            return
        if fact is None:
            return
        # ★ Pass outward instead of writing here: memory store / drafter / taint / **collision rules**
        # belong to turn finalization; pushing them into lifecycle drags four contexts through the call chain,
        # while collisions (one statement triggering remember / nl_intent / this path)
        # should stay alongside the other two capture paths to remain visible.
        from bowhead_core.auto_learner import MODIFICATION_REQUEST

        # Classifier can output only this category (`_VALID_TYPES`). Retain this check as an **assertion**:
        # adding categories without branches here stops here immediately instead of silently
        # writing to another scope through the old branch, as the four former categories did.
        if fact.fact_type == MODIFICATION_REQUEST:
            logger.info(
                "delta.lifecycle.modification_request key=%s conf=%s",
                fact.fact_key or "-",
                fact.confidence,
            )
            if self._on_modification_request is not None:
                try:
                    await self._on_modification_request(fact)
                except Exception as e:  # noqa: BLE001 - this must not fail turn finalization
                    logger.warning("delta.lifecycle.modification_request_hook_failed err=%s", e)
            return

    async def on_post_verify(
        self,
        text: str,
        *,
        workspace_id: str,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> ProvenanceVerification:
        try:
            return await post_llm_verify_provenance(
                text,
                workspace_id=workspace_id,
                fact_store=self._fact_store,
                project_id=project_id,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("delta.lifecycle.post_verify_failed err=%s", e)
            return ProvenanceVerification(passed=True)


def build_delta_lifecycle(
    *,
    fact_store: RegisteredFactStore | None = None,
    router: Router | None = None,
    enabled: bool | None = None,
    on_modification_request: Any = None,
) -> DeltaLifecycleProtocol:
    """Build lifecycle. Master flag off OR fact_store missing → Noop.

    Caller patterns mirror η ``build_lifecycle``:
        delta = build_delta_lifecycle(fact_store=orch.fact_store, router=orch.router)
        block = await delta.on_pre_llm(...)
    """
    use = enabled if enabled is not None else is_provenance_hitl_enabled()
    if not use or fact_store is None:
        return NoopDeltaLifecycle()
    return DefaultDeltaLifecycle(fact_store=fact_store, router=router, on_modification_request=on_modification_request)
