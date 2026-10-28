"""Recover durable Terminal Protocol post-decision projections.

The decision row and its effect intent commit in one transaction.  Everything
in this module is therefore a replay-safe projection: it may run in the HTTP
request, after an acknowledgement loss, or after a process restart, but it may
never choose a different decision or open a generation.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from bowhead_core.terminal_protocol import canonical_digest_v1
from bowhead_core.terminal_protocol_store import (
    DecisionAccessRevokedError,
)
from bowhead_core.terminal_runtime_factory import TerminalPreparedDecision

log = logging.getLogger(__name__)


class TerminalClarificationEffectRecovery:
    """Apply due clarification projections and settle their durable intents."""

    def __init__(
        self,
        *,
        app: Any,
        protocol_store: Any,
        authorizer: Any | None = None,
    ) -> None:
        self._app = app
        self._protocol_store = protocol_store
        self._authorizer = authorizer
        self._owner = "gateway-terminal-clarification-recovery"

    async def __call__(self, decision_id: str) -> int:
        try:
            decision, preflight = await self._protocol_store.rehydrate_decision_effect_source(
                decision_id=decision_id,
            )
        except DecisionAccessRevokedError as exc:
            return await self._protocol_store.skip_decision_effects(
                decision_id=decision_id,
                code=getattr(exc, "reason_code", "access_revoked"),
            )
        if decision.context.source_kind.value != "clarification":
            raise RuntimeError("terminal_decision_effect_kind_changed")
        clarification_store = getattr(
            self._app.state,
            "clarification_store",
            None,
        )
        if clarification_store is None:
            raise RuntimeError("clarification_store_unavailable")
        clarification = await clarification_store.get(decision.context.source_id)
        if clarification is None:
            return await self._protocol_store.skip_decision_effects(
                decision_id=decision_id,
                code="dependency_missing",
            )
        from bowhead_gateway.terminal_decision_authorization import (
            TerminalDecisionAuthorizer,
        )

        authorizer = self._authorizer or TerminalDecisionAuthorizer(app=self._app)
        try:
            await authorizer.authorize(
                TerminalPreparedDecision(decision, preflight),
                workspace_id=clarification.workspace_id,
            )
        except DecisionAccessRevokedError as exc:
            return await self._protocol_store.skip_decision_effects(
                decision_id=decision_id,
                code=getattr(exc, "reason_code", "access_revoked"),
            )
        claimed = await self._protocol_store.claim_decision_effects(
            decision_id=decision_id,
            owner=self._owner,
            expected_authz_snapshot_digest=(preflight.authz_snapshot_digest),
        )
        applied_count = 0
        for effect in claimed:
            applied = False
            code = "clarification_projection_failed"
            try:
                if effect.plan.effect_kind != "clarification_projection":
                    code = "terminal_decision_effect_kind_unsupported"
                else:
                    if effect.plan.target_ref != decision.context.source_id:
                        raise RuntimeError("terminal_decision_effect_source_identity_changed")
                    payload = _decode_effect_payload(effect.plan)
                    await self._apply_clarification_projection(
                        decision_id=decision_id,
                        request_id=effect.plan.target_ref,
                        payload=payload,
                    )
                    applied = True
            except Exception as exc:
                log.warning(
                    "terminal_decision_effect.clarification_projection_failed",
                    extra={
                        "decision_id": decision_id,
                        "effect_id": effect.plan.effect_id,
                        "error": type(exc).__name__,
                    },
                )
            settled = await self._protocol_store.settle_decision_effect(
                effect,
                applied=applied,
                code=None if applied else code,
            )
            if not settled:
                raise RuntimeError("terminal_decision_effect_claim_changed")
            if applied:
                applied_count += 1
        return applied_count

    async def _apply_clarification_projection(
        self,
        *,
        decision_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> None:
        clarification_store = getattr(
            self._app.state,
            "clarification_store",
            None,
        )
        if clarification_store is None:
            raise RuntimeError("clarification_store_unavailable")
        clarification = await clarification_store.get(request_id)
        if clarification is None or clarification.status != "resolved":
            raise RuntimeError("terminal_decision_source_projection_missing")

        values = list(clarification.chosen_values or ([clarification.chosen_value] if clarification.chosen_value else []))
        primary = clarification.chosen_value or (values[0] if values else "")
        skipped = primary == "__bowhead_clarification_skip__"

        await _project_tool_context_choice(
            self._app,
            clarification,
            primary=primary,
            remember=bool(payload.get("remember", True)),
        )
        if clarification.kind == "task_plan" and not skipped:
            await _project_task_plan_memories(
                self._app,
                clarification,
                decision_id=decision_id,
                values=values,
            )


def _decode_effect_payload(plan: Any) -> dict[str, Any]:
    payload = json.loads(plan.payload_json)
    if not isinstance(payload, dict):
        raise RuntimeError("terminal_decision_effect_payload_malformed")
    if canonical_digest_v1(payload) != plan.payload_digest:
        raise RuntimeError("terminal_decision_effect_payload_changed")
    return payload


async def _project_tool_context_choice(
    app: Any,
    clarification: Any,
    *,
    primary: str,
    remember: bool,
) -> None:
    if clarification.kind != "tool_context_select" or not primary:
        return
    # Reuse the route's single connector-choice implementation.  It only reads
    # request.app; the small namespace keeps recovery independent of HTTP.
    from bowhead_gateway.routes.clarifications import (
        _commit_tool_context_choice_effect,
    )

    await _commit_tool_context_choice_effect(
        SimpleNamespace(app=app),
        clarification,
        user_id=clarification.user_id,
        primary=primary,
        remember=remember,
        strict=True,
    )


async def _project_task_plan_memories(
    app: Any,
    clarification: Any,
    *,
    decision_id: str,
    values: list[str],
) -> None:
    labels = [
        str(item.get("title") or "").strip()
        for item in (clarification.edited_subs or [])
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    ]
    if not labels:
        for value in values:
            candidate = next(
                (item for item in clarification.candidates if item.value == value),
                None,
            )
            labels.append(str(getattr(candidate, "label", None) or value))

    user_store = getattr(app.state, "user_memory_store", None)
    project_store = getattr(app.state, "project_memory_store", None)
    if user_store is None and project_store is None:
        raise RuntimeError("terminal_decision_memory_projection_unavailable")
    for index, label in enumerate(labels):
        key = f"{decision_id}:{index}"
        if user_store is not None:
            await user_store.upsert_operational_memory(
                user_id=clarification.user_id,
                kind="task_plan_pick_event",
                key=key,
                value={"label": label},
                source="terminal_decision_effect",
            )
        if project_store is not None:
            await project_store.upsert(
                workspace_id=clarification.workspace_id,
                kind="task_plan_pick_event",
                key=key,
                value={"label": label},
                source="terminal_decision_effect",
            )


__all__ = ["TerminalClarificationEffectRecovery"]
