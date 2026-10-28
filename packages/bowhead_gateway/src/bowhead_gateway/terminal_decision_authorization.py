"""Current-authorization resolver for restart-safe Terminal decisions."""

from __future__ import annotations

from typing import Any

from bowhead_core.terminal_protocol import (
    DecisionAction,
    DecisionSourceKind,
    is_web_cancel_decision,
)
from bowhead_core.terminal_protocol_store import (
    DecisionAccessRevokedError,
    DecisionAuthorizationDeniedError,
)
from bowhead_core.terminal_runtime_factory import TerminalPreparedDecision


async def authorize_web_cancel_access(
    *,
    app: Any,
    team_id: str,
    session_id: str,
    requester_ref: str,
    workspace_id: str,
) -> None:
    """Re-check the current owner ACL for a safe-stop operation.

    Web cancellation deliberately does not require the run-turn entitlement:
    a frozen licence must prevent new work, not trap already-running or
    durably-paused work.  Both the live route and restart recovery call this
    exact resolver so they cannot disagree after a crash.
    """

    user_store = getattr(app.state, "user_store", None)
    session_store = getattr(app.state, "session_store", None)
    if user_store is None or session_store is None:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    session = await session_store.get_session(session_id)
    user = await user_store.get_user(requester_ref)
    role = await user_store.get_team_role(requester_ref, team_id)
    if (
        session is None
        or session.user_id != requester_ref
        or session.workspace_id != workspace_id
        or session.deleted_at is not None
        or user is None
        or user.status != "active"
        or role is None
    ):
        raise DecisionAccessRevokedError("web cancel access changed")

    from bowhead_core.space_governance import SpaceOperation

    from bowhead_gateway.access_context import (
        AccessContextError,
        build_access_context,
    )

    try:
        access = await build_access_context(
            user_store=user_store,
            access_store=getattr(
                app.state,
                "access_control_store",
                None,
            ),
            user_id=requester_ref,
            workspace_id=workspace_id,
            operation=SpaceOperation.EXECUTE,
            expected_team_id=team_id,
        )
    except AccessContextError as exc:
        if exc.status_code >= 500:
            raise DecisionAuthorizationDeniedError("dependency_missing") from exc
        raise DecisionAccessRevokedError("web cancel workspace access changed") from exc
    if access.team_id != team_id:
        raise DecisionAccessRevokedError("web cancel workspace team changed")


class TerminalDecisionAuthorizer:
    """Re-check concrete ACL semantics immediately before first N+1 work."""

    def __init__(self, *, app: Any) -> None:
        self._app = app

    async def authorize(
        self,
        prepared: TerminalPreparedDecision,
        *,
        workspace_id: str,
    ) -> None:
        context = prepared.decision.context
        user_store = getattr(self._app.state, "user_store", None)
        session_store = getattr(self._app.state, "session_store", None)
        if user_store is None or session_store is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        session = await session_store.get_session(context.scope.session_id)
        if (
            session is None
            or session.user_id != context.principals.requester_ref
            or session.workspace_id != workspace_id
            or session.deleted_at is not None
        ):
            raise DecisionAccessRevokedError("decision Session visibility changed")

        principals = {
            context.principals.requester_ref,
            context.principals.actor_ref,
            context.principals.effect_principal_ref,
        }
        for principal in principals:
            if principal.startswith("system:"):
                continue
            user = await user_store.get_user(principal)
            role = await user_store.get_team_role(
                principal,
                context.scope.team_id,
            )
            if user is None or user.status != "active" or role is None:
                raise DecisionAccessRevokedError("decision team membership changed")

        if context.source_kind is DecisionSourceKind.APPROVAL:
            actor_role = await user_store.get_team_role(
                context.principals.actor_ref,
                context.scope.team_id,
            )
            effect_role = await user_store.get_team_role(
                context.principals.effect_principal_ref,
                context.scope.team_id,
            )
            if actor_role not in {"owner", "team_admin"} or effect_role not in {
                "owner",
                "team_admin",
            }:
                raise DecisionAccessRevokedError("approval administrator permission changed")

        from bowhead_core.space_governance import SpaceOperation

        from bowhead_gateway.access_context import (
            AccessContextError,
            build_access_context,
        )

        try:
            if context.source_kind is DecisionSourceKind.APPROVAL and context.principals.requester_ref != context.principals.effect_principal_ref:
                requester_access = await build_access_context(
                    user_store=user_store,
                    access_store=getattr(
                        self._app.state,
                        "access_control_store",
                        None,
                    ),
                    user_id=context.principals.requester_ref,
                    workspace_id=workspace_id,
                    operation=SpaceOperation.EXECUTE,
                    expected_team_id=context.scope.team_id,
                )
                if requester_access.team_id != context.scope.team_id:
                    raise DecisionAccessRevokedError("approval requester workspace access changed")
            access = await build_access_context(
                user_store=user_store,
                access_store=getattr(
                    self._app.state,
                    "access_control_store",
                    None,
                ),
                user_id=context.principals.effect_principal_ref,
                workspace_id=workspace_id,
                operation=SpaceOperation.EXECUTE,
                expected_team_id=context.scope.team_id,
            )
        except AccessContextError as exc:
            if exc.status_code >= 500:
                raise RuntimeError("decision_authorization_temporarily_unavailable") from exc
            raise DecisionAccessRevokedError("decision workspace access changed") from exc
        if access.team_id != context.scope.team_id:
            raise DecisionAccessRevokedError("decision workspace team changed")

        entitlements = getattr(self._app.state, "entitlements", None)
        if entitlements is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        from bowhead_core.entitlements import EntitlementDenied

        try:
            await entitlements.assert_can_run_turn(context.scope.team_id)
        except EntitlementDenied as exc:
            raise DecisionAuthorizationDeniedError("entitlement_denied") from exc

    async def authorize_pending_source(
        self,
        *,
        team_id: str,
        session_id: str,
        workspace_id: str,
        requester_ref: str,
    ) -> None:
        """Re-check a paused source before any user decision exists."""

        user_store = getattr(self._app.state, "user_store", None)
        session_store = getattr(self._app.state, "session_store", None)
        if user_store is None or session_store is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        session = await session_store.get_session(session_id)
        user = await user_store.get_user(requester_ref)
        role = await user_store.get_team_role(requester_ref, team_id)
        if (
            session is None
            or session.user_id != requester_ref
            or session.workspace_id != workspace_id
            or session.deleted_at is not None
            or user is None
            or user.status != "active"
            or role is None
        ):
            raise DecisionAccessRevokedError("pending source access changed")
        from bowhead_core.space_governance import SpaceOperation

        from bowhead_gateway.access_context import (
            AccessContextError,
            build_access_context,
        )

        try:
            access = await build_access_context(
                user_store=user_store,
                access_store=getattr(
                    self._app.state,
                    "access_control_store",
                    None,
                ),
                user_id=requester_ref,
                workspace_id=workspace_id,
                operation=SpaceOperation.EXECUTE,
                expected_team_id=team_id,
            )
        except AccessContextError as exc:
            if exc.status_code >= 500:
                raise RuntimeError("pending_source_authorization_temporarily_unavailable") from exc
            raise DecisionAccessRevokedError("pending source authorization changed") from exc
        if access.team_id != team_id:
            raise DecisionAccessRevokedError("pending source workspace team changed")
        entitlements = getattr(self._app.state, "entitlements", None)
        if entitlements is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        from bowhead_core.entitlements import EntitlementDenied

        try:
            await entitlements.assert_can_run_turn(team_id)
        except EntitlementDenied as exc:
            raise DecisionAuthorizationDeniedError("entitlement_denied") from exc


class TerminalPendingDecisionRevocation:
    """Converge a durable action that lost authorization before first open."""

    def __init__(self, *, protocol_store: Any, factory: Any) -> None:
        self._store = protocol_store
        self._factory = factory

    async def __call__(
        self,
        pending: Any,
        _fence: Any,
        denial_reason_code: str,
    ) -> bool:
        decision, preflight = await self._store.supersede_pending_decision_with_revoke(
            pending.decision,
            denial_reason_code=denial_reason_code,
        )
        await self._factory.commit_decision_control(
            TerminalPreparedDecision(decision, preflight),
            reason_code=("terminal_dependency_missing" if denial_reason_code in {"dependency_missing", "entitlement_denied"} else "access_revoked"),
            message_key=("turn.failed" if denial_reason_code in {"dependency_missing", "entitlement_denied"} else "turn.access_revoked"),
        )
        return True


class TerminalPendingControlAuthorization:
    """Authorize an actor control again before recovery commits N+1."""

    def __init__(self, *, app: Any) -> None:
        self._app = app
        self._authorizer = TerminalDecisionAuthorizer(app=app)

    async def __call__(self, decision: Any, preflight: Any) -> None:
        context = decision.context
        if context.source_kind is DecisionSourceKind.CLARIFICATION:
            store = getattr(self._app.state, "clarification_store", None)
        elif context.source_kind is DecisionSourceKind.APPROVAL:
            store = getattr(self._app.state, "approval_store", None)
        elif context.source_kind is DecisionSourceKind.SYSTEM_CONTROL and context.action is DecisionAction.CANCEL and is_web_cancel_decision(context):
            session_store = getattr(
                self._app.state,
                "session_store",
                None,
            )
            if session_store is None:
                raise RuntimeError("terminal_control_authorization_source_unavailable")
            session = await session_store.get_session(context.scope.session_id)
            if session is None or session.user_id != context.principals.requester_ref or session.deleted_at is not None:
                raise DecisionAuthorizationDeniedError("access_revoked")
            await authorize_web_cancel_access(
                app=self._app,
                team_id=context.scope.team_id,
                session_id=context.scope.session_id,
                requester_ref=context.principals.requester_ref,
                workspace_id=session.workspace_id,
            )
            return
        else:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        if store is None:
            raise RuntimeError("terminal_control_authorization_source_unavailable")
        source = await store.get(context.source_id)
        if source is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        await self._authorizer.authorize(
            TerminalPreparedDecision(decision, preflight),
            workspace_id=source.workspace_id,
        )


__all__ = [
    "TerminalDecisionAuthorizer",
    "TerminalPendingControlAuthorization",
    "TerminalPendingDecisionRevocation",
    "authorize_web_cancel_access",
]
