"""Authenticated native question actions reuse the Web lifecycle operation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from bowhead_core.entitlements import EntitlementDenied
from bowhead_core.space_governance import SpaceOperation
from fastapi import HTTPException

from bowhead_gateway._entitlement_gate import _denied_to_http, _requires_resolver
from bowhead_gateway.access_context import AccessContextError, build_access_context

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext
    from fastapi import FastAPI


class GatewayClarificationActions:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def dismiss(self, request_id: str | None, ctx: ToolExecutionContext) -> dict[str, Any]:
        try:
            return await self._dismiss(request_id, ctx)
        except (HTTPException, AccessContextError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else exc.code
            if isinstance(detail, dict):
                detail = detail.get("error", detail)
                detail = detail.get("code") if isinstance(detail, dict) else detail
            code = detail if isinstance(detail, str) and re.fullmatch(r"[a-z0-9_:-]{1,120}", detail) else "clarification_dismiss_unavailable"
            return {
                "ok": False,
                "error": code,
                "directive": "Cancellation was not confirmed. Report this condition; do not claim the question was cancelled.",
            }

    async def _dismiss(self, request_id: str | None, ctx: ToolExecutionContext) -> dict[str, Any]:
        from bowhead_core.clarification_resume_context import is_legacy_ordinary_question

        from bowhead_gateway.routes.clarifications import dismiss_legacy_clarification

        if any(not isinstance(value, str) or not value for value in (ctx.user_id, ctx.team_id, ctx.workspace_id, ctx.session_id)):
            raise HTTPException(403, "clarification_scope_unavailable")
        state = self.app.state
        store = state.clarification_store
        sessions = state.session_store
        session = await sessions.get_session(ctx.session_id)
        if session is None or session.user_id != ctx.user_id or session.workspace_id != ctx.workspace_id or getattr(session, "deleted_at", None):
            raise HTTPException(404, "clarification_not_found")
        await build_access_context(
            user_store=state.user_store,
            access_store=getattr(state, "access_control_store", None),
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            expected_team_id=ctx.team_id,
            operation=SpaceOperation.WRITE,
        )
        entitlements = getattr(state, "entitlements", None)
        if entitlements is None and _requires_resolver(state):
            raise HTTPException(503, "entitlement_unavailable")
        if entitlements is not None:
            try:
                await entitlements.assert_can_run_turn(ctx.team_id)
            except EntitlementDenied as exc:
                raise _denied_to_http(exc) from exc
        if request_id is None:
            pending = await store.get_pending_for_session(session_id=ctx.session_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
            if len(pending) != 1:
                return {
                    "ok": False,
                    "error": "clarification_target_ambiguous" if pending else "clarification_not_found",
                    "targets": [{"request_id": item.request_id, "turn_id": item.turn_id, "question": item.rationale} for item in pending[:16]],
                    "directive": "No question was cancelled. Ask the user to identify the intended pending question when targets are ambiguous.",
                }
            pre = pending[0]
        else:
            pre = await store.get(request_id)
        if pre is None or pre.user_id != ctx.user_id or pre.workspace_id != ctx.workspace_id or pre.session_id != ctx.session_id:
            raise HTTPException(404, "clarification_not_found")
        context = pre.resume_context if isinstance(pre.resume_context, dict) else {}
        continuation = context.get("continuation")
        if not is_legacy_ordinary_question(pre) and (
            context.get("__schema_version") != 4 or not isinstance(continuation, dict) or continuation.get("kind") != "ordinary_lead"
        ):
            raise HTTPException(409, "clarification_dismiss_not_supported")
        result = await dismiss_legacy_clarification(self.app, store, pre)
        dismissed = result is not None and result.status == "dismissed"
        return {
            "ok": dismissed,
            "dismissed": dismissed,
            "request_id": pre.request_id,
            "turn_id": pre.turn_id,
            "status": result.status if result is not None else "unavailable",
            **({"error": "clarification_already_answered"} if not dismissed else {}),
            "directive": "The original question is durably dismissed; do not resume its computation."
            if dismissed
            else "Cancellation did not win. The question was already handled; do not claim it was cancelled.",
        }
