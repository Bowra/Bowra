"""Restart-safe execution attachment for durable Terminal decisions."""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from bowhead_core.language_context import resume_language_context
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.terminal_protocol import (
    DecisionAction,
    DecisionSourceKind,
    TerminalScope,
)
from bowhead_core.terminal_protocol_store import (
    DecisionAuthorizationDeniedError,
    DecisionSourceNotReadyError,
)
from bowhead_core.terminal_runtime_factory import TerminalPreparedDecision
from bowhead_core.types import TurnRequest


class TerminalPendingExecutionRecovery:
    """Rebuild approved/resolved N+1 work from authoritative source rows."""

    def __init__(
        self,
        *,
        app: Any,
        factory: Any,
        protocol_store: Any,
        approval_runner: Any,
        authorizer: Any,
    ) -> None:
        self._app = app
        self._factory = factory
        self._protocol_store = protocol_store
        self._approval_runner = approval_runner
        self._authorizer = authorizer

    async def __call__(self, pending: Any, _fence: Any) -> bool:
        context = pending.decision.context
        rehydrated = await self._protocol_store.read_source_decision(
            team_id=context.scope.team_id,
            session_id=context.scope.session_id,
            turn_id=context.scope.turn_id,
            source_kind=context.source_kind,
            source_id=context.source_id,
            action=context.action,
            principals=context.principals,
        )
        if rehydrated is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        decision, preflight = rehydrated
        prepared = TerminalPreparedDecision(decision, preflight)
        source = await self._source_record(prepared)
        if source is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        await self._authorizer.authorize(
            prepared,
            workspace_id=source.workspace_id,
        )
        if context.source_kind is DecisionSourceKind.CLARIFICATION and context.action is DecisionAction.RESUME:
            return await self._resume_clarification(prepared)
        if context.source_kind is DecisionSourceKind.APPROVAL and context.action is DecisionAction.APPROVE:
            return await self._execute_approval(prepared)
        return False

    async def _source_record(self, prepared: TerminalPreparedDecision) -> Any:
        context = prepared.decision.context
        if context.source_kind is DecisionSourceKind.CLARIFICATION:
            store = getattr(self._app.state, "clarification_store", None)
        elif context.source_kind is DecisionSourceKind.APPROVAL:
            store = getattr(self._app.state, "approval_store", None)
        else:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        if store is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        return await store.get(context.source_id)

    async def _resume_clarification(
        self,
        prepared: TerminalPreparedDecision,
    ) -> bool:
        clarification_store = getattr(
            self._app.state,
            "clarification_store",
            None,
        )
        orchestrator = getattr(self._app.state, "orchestrator", None)
        if clarification_store is None or orchestrator is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        context = prepared.decision.context
        clarification = await clarification_store.get(context.source_id)
        if clarification is None or clarification.status != "resolved":
            raise DecisionAuthorizationDeniedError("dependency_missing")

        from bowhead_core.space_governance import SpaceOperation

        from bowhead_gateway.access_context import build_access_context
        from bowhead_gateway.datarc_context import resolve_datarc_turn_context
        from bowhead_gateway.turn_service import (
            build_resume_turn_request,
            spawn_turn,
        )

        access = await build_access_context(
            user_store=getattr(self._app.state, "user_store", None),
            access_store=getattr(
                self._app.state,
                "access_control_store",
                None,
            ),
            user_id=context.principals.requester_ref,
            workspace_id=clarification.workspace_id,
            operation=SpaceOperation.EXECUTE,
            expected_team_id=context.scope.team_id,
        )
        if access.team_id != context.scope.team_id:
            raise DecisionAuthorizationDeniedError("access_revoked")
        from bowhead_gateway.datarc_context import clarification_runtime_context, recovered_datarc_source, resolve_datarc_recovery_context

        original_context = clarification_runtime_context(clarification, db_path=getattr(clarification_store, "db_path", None))
        source_evidence = original_context.get("datarc_selection")
        selected_project = (
            clarification.chosen_value
            if clarification.kind == "tool_context_select" and clarification.chosen_value != "__bowhead_clarification_skip__"
            else None
        )
        if source_evidence is not None:
            datarc, member_uid = await resolve_datarc_recovery_context(
                self._app,
                team_id=context.scope.team_id,
                user_id=context.principals.requester_ref,
                workspace_id=clarification.workspace_id,
                session_id=context.scope.session_id,
                evidence=source_evidence,
                selected_project=selected_project,
            )
        else:
            datarc = await resolve_datarc_turn_context(
                self._app,
                team_id=context.scope.team_id,
                workspace_id=clarification.workspace_id,
                user_id=context.principals.requester_ref,
                session_id=context.scope.session_id,
                pinned_project=(
                    clarification.chosen_value
                    if clarification.kind == "tool_context_select" and clarification.chosen_value != "__bowhead_clarification_skip__"
                    else None
                ),
            )
            member_uid = await _resolve_recovery_member_uid(
                self._app,
                user_id=context.principals.requester_ref,
                datarc_client=datarc.datarc_client,
                datarc_workspace_id=datarc.datarc_project_for_calls,
            )
        team_router, team_kb = await resolve_terminal_team_runtime(
            self._app.state,
            context.scope.team_id,
        )
        turn_request = build_resume_turn_request(
            clarification,
            source_db_path=getattr(clarification_store, "db_path", None),
            datarc_client=datarc.datarc_client,
            datarc_unavailable=datarc.datarc_unavailable,
            vector_client=datarc.vector_client,
            team_router=team_router,
            team_kb_retriever=team_kb,
        )
        if turn_request is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")

        from bowhead_gateway.routes.chat import (
            _make_datarc_context_switcher,
        )
        from bowhead_gateway.ws_authz import (
            ws_membership_enforce_enabled,
        )

        datarc_context_switcher = _make_datarc_context_switcher(
            SimpleNamespace(app=self._app),
            context.principals.requester_ref,
            context.scope.session_id,
            workspace_id=clarification.workspace_id,
            binding=datarc.connection_binding,
            ws_enforce=ws_membership_enforce_enabled(),
            log=logging.getLogger(__name__),
        )
        expected_identity = (
            context.scope.session_id,
            context.principals.requester_ref,
            clarification.workspace_id,
            context.scope.turn_id,
        )
        cached_identity = (
            turn_request.session_id,
            turn_request.user_id,
            turn_request.workspace_id,
            turn_request.turn_id,
        )
        if cached_identity != expected_identity:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        durable_locale = await self._protocol_store.read_persisted_turn_presentation_locale(
            team_id=context.scope.team_id,
            turn_id=context.scope.turn_id,
        )
        from bowhead_core.datarc_helper_switch_continuation import DatarcHelperSwitchResolution
        from bowhead_core.datarc_switch_continuation import DatarcSwitchResolution

        outcome_reader = getattr(clarification_store, "get_resolution_outcome", None)
        outcome = await outcome_reader(clarification.request_id) if callable(outcome_reader) else None
        persistence = (outcome or {}).get("tool_context_persistence")

        turn_request = replace(
            turn_request,
            session_id=context.scope.session_id,
            user_id=context.principals.requester_ref,
            workspace_id=clarification.workspace_id,
            turn_id=context.scope.turn_id,
            team_id=context.scope.team_id,
            member_uid=member_uid,
            access_context=access.as_dict(),
            active_datarc_project=datarc.active_datarc_project,
            datarc_binding=datarc.connection_binding,
            datarc_source_evidence=recovered_datarc_source(datarc, context.principals.requester_ref, context.scope.team_id, member_uid)
            if source_evidence is not None
            else None,
            datarc_switch_resolution=DatarcSwitchResolution.from_card(
                clarification,
                source=recovered_datarc_source(datarc, context.principals.requester_ref, context.scope.team_id, member_uid),
                persistence=persistence,
            )
            if source_evidence is not None
            else None,
            datarc_helper_switch_resolution=DatarcHelperSwitchResolution.from_card(
                clarification,
                source=recovered_datarc_source(datarc, context.principals.requester_ref, context.scope.team_id, member_uid),
                persistence=persistence,
            )
            if source_evidence is not None
            else None,
            active_datarc_workspace_name=(datarc.active_datarc_workspace_name or ""),
            accessible_datarc_workspaces=(datarc.accessible_datarc_workspaces or ()),
            datarc_absence_reason=datarc.datarc_absence_reason or "",
            datarc_context_switcher=datarc_context_switcher,
            # Same rule as the live web resume (clarifications.py `config_lite_supported=_sched_job is None`):
            # a clarification raised by a scheduled run stays unattended after a restart, so it never gains
            # web-only capabilities such as a member's personal Google grant.
            config_lite_supported=await _resume_is_attended(self._app, context.scope.turn_id),
            terminal_auth_fence_vector_digest=(prepared.preflight.authz_snapshot_digest),
            # An ordinary Lead checkpoint seals its own language (none when i18n
            # is off); a locale fallback would fail its restore check.
            language_context=turn_request.ordinary_lead_checkpoint.language_context()
            if turn_request.ordinary_lead_checkpoint is not None
            else resume_language_context(
                clarification.resume_context,
                presentation_locale=durable_locale,
            ),
        )
        runtime = await self._factory.open_decision_runtime(
            prepared,
            turn_request,
        )
        turn_request = replace(turn_request, terminal_runtime=runtime)
        await spawn_turn(
            orchestrator,
            turn_request,
            user_store=getattr(self._app.state, "user_store", None),
            team_id=context.scope.team_id,
            entitlements=getattr(self._app.state, "entitlements", None),
            name_prefix="terminal_recovery_resume",
        )
        return True

    async def _execute_approval(
        self,
        prepared: TerminalPreparedDecision,
    ) -> bool:
        approval_store = getattr(self._app.state, "approval_store", None)
        if approval_store is None:
            raise DecisionAuthorizationDeniedError("dependency_missing")
        approval = await approval_store.get(prepared.decision.context.source_id)
        if approval is None or approval.status != "approved":
            raise DecisionAuthorizationDeniedError("dependency_missing")
        executor = await build_terminal_approval_executor(
            self._app,
            approval_store=approval_store,
            approval=approval,
            team_id=prepared.decision.context.scope.team_id,
            effect_user_id=(prepared.decision.context.principals.effect_principal_ref),
        )
        await self._approval_runner.spawn(
            prepared=prepared,
            approval=approval,
            legacy_executor=executor,
        )
        return True


async def _resume_is_attended(app: Any, turn_id: str) -> bool:
    """A resumed clarification is attended unless its turn came from a scheduled run."""
    from bowhead_gateway.routes.clarifications import scheduled_run_for_turn

    try:
        return await scheduled_run_for_turn(app, turn_id) is None
    except Exception:
        logging.getLogger(__name__).warning("terminal_resume.scheduled_lookup_failed turn=%s", turn_id, exc_info=True)
        return False  # fail closed: unattended


async def _resolve_recovery_member_uid(
    app: Any,
    *,
    user_id: str,
    datarc_client: Any,
    datarc_workspace_id: str,
) -> str:
    if not datarc_workspace_id:
        return ""
    user_store = getattr(app.state, "user_store", None)
    resolver = getattr(app.state, "member_resolver", None)
    if user_store is None or resolver is None or datarc_client is None:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    user = await user_store.get_user(user_id)
    email = getattr(user, "email", None)
    if not email or not getattr(user, "email_verified", False):
        raise DecisionAuthorizationDeniedError("access_revoked")
    return await resolver.resolve(
        email,
        datarc_workspace_id,
        datarc=datarc_client,
    )


async def resolve_terminal_team_runtime(
    app_state: Any,
    team_id: str,
) -> tuple[Any, Any]:
    """Resolve the current target-team router/KB without process fallbacks."""

    user_store = getattr(app_state, "user_store", None)
    router_registry = getattr(app_state, "team_router_registry", None)
    kb_registry = getattr(app_state, "team_kb_runtime_registry", None)
    if user_store is None or router_registry is None or kb_registry is None:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    try:
        raw, version = await user_store.get_team_llm_config_with_version(team_id)
    except Exception as exc:
        raise DecisionSourceNotReadyError("terminal recovery team runtime is unavailable") from exc
    from bowhead_core.team_llm_config import migrate_legacy_config_to_v2

    try:
        config = migrate_legacy_config_to_v2(
            raw,
            loaded_config=getattr(
                app_state,
                "routing_config_loaded",
                None,
            ),
        )
    except Exception as exc:
        raise DecisionAuthorizationDeniedError("dependency_missing") from exc
    if not config:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    try:
        router = await router_registry.get(team_id, config, version)
        kb_runtime = await kb_registry.get(team_id, config, version)
    except Exception as exc:
        raise DecisionSourceNotReadyError("terminal recovery team runtime is unavailable") from exc
    retriever = getattr(kb_runtime, "retriever", None)
    if router is None or retriever is None:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    return router, retriever


async def resolve_terminal_followup_request(
    app: Any,
    template: Any,
    parent_scope: TerminalScope,
) -> TurnRequest:
    """Rebuild every live-only Gateway dependency before child claim."""

    if not isinstance(template, dict):
        raise DecisionAuthorizationDeniedError("dependency_missing")
    intent = template.get("execution_intent")
    if not isinstance(intent, dict):
        raise DecisionAuthorizationDeniedError("dependency_missing")
    turn_id = template.get("turn_id")
    text = template.get("user_text")
    workspace_id = intent.get("workspace_id")
    user_id = intent.get("user_id")
    if any(not isinstance(value, str) or not value for value in (turn_id, workspace_id, user_id)) or not isinstance(text, str):
        raise DecisionAuthorizationDeniedError("dependency_missing")
    attachments = intent.get("attachments", [])
    if not isinstance(attachments, list) or any(not isinstance(value, str) for value in attachments):
        raise DecisionAuthorizationDeniedError("dependency_missing")
    member_uid_snapshot = intent.get("member_uid")
    presentation_locale = intent.get("presentation_locale")
    source_auth_digest = intent.get("source_authz_snapshot_digest")
    source_steer_ids = intent.get("source_steer_ids")
    source_steer_digests = intent.get("source_steer_request_digests")
    if (
        not isinstance(member_uid_snapshot, str)
        or not isinstance(presentation_locale, str)
        or not presentation_locale
        or not isinstance(source_auth_digest, str)
        or not source_auth_digest
        or not isinstance(source_steer_ids, list)
        or not source_steer_ids
        or any(not isinstance(value, str) or not value for value in source_steer_ids)
        or not isinstance(source_steer_digests, list)
        or len(source_steer_digests) != len(source_steer_ids)
        or any(not isinstance(value, str) or not value for value in source_steer_digests)
    ):
        raise DecisionAuthorizationDeniedError("dependency_missing")

    from bowhead_gateway.access_context import build_access_context
    from bowhead_gateway.channel_language import resolve_web_followup_turn_language
    from bowhead_gateway.datarc_context import resolve_datarc_turn_context
    from bowhead_gateway.terminal_decision_authorization import (
        TerminalDecisionAuthorizer,
    )

    await TerminalDecisionAuthorizer(app=app).authorize_pending_source(
        team_id=parent_scope.team_id,
        session_id=parent_scope.session_id,
        workspace_id=workspace_id,
        requester_ref=user_id,
    )
    access = await build_access_context(
        user_store=getattr(app.state, "user_store", None),
        access_store=getattr(app.state, "access_control_store", None),
        user_id=user_id,
        workspace_id=workspace_id,
        operation=SpaceOperation.EXECUTE,
        expected_team_id=parent_scope.team_id,
    )
    if access.team_id != parent_scope.team_id:
        raise DecisionAuthorizationDeniedError("access_revoked")
    source_evidence = intent.get("datarc_selection")
    if source_evidence is not None:
        from bowhead_gateway.datarc_context import resolve_datarc_recovery_context

        datarc, member_uid = await resolve_datarc_recovery_context(
            app,
            team_id=parent_scope.team_id,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=parent_scope.session_id,
            evidence=source_evidence,
        )
    else:
        datarc = await resolve_datarc_turn_context(
            app,
            team_id=parent_scope.team_id,
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=parent_scope.session_id,
            pinned_project=(
                intent.get("active_datarc_project")
                if isinstance(intent.get("active_datarc_project"), str) and intent.get("active_datarc_project")
                else None
            ),
        )
        member_uid = await _resolve_recovery_member_uid(
            app,
            user_id=user_id,
            datarc_client=datarc.datarc_client,
            datarc_workspace_id=datarc.datarc_project_for_calls,
        )
    if member_uid != member_uid_snapshot:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    team_router, team_kb = await resolve_terminal_team_runtime(
        app.state,
        parent_scope.team_id,
    )
    factory = getattr(app.state, "terminal_runtime_factory", None)
    if factory is None:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    current_digest = await factory.prepare_web_auth_fence(
        team_id=parent_scope.team_id,
        session_id=parent_scope.session_id,
        user_id=user_id,
    )
    planning_mode = intent.get("planning_mode")
    # The follow-up continues the parent's conversation: keep the parent Turn's viewer zone.
    from bowhead_core.model_clock import known_time_zone_name

    viewer_time_zone = known_time_zone_name(intent.get("time_zone"))
    language_context = await resolve_web_followup_turn_language(
        app,
        user_id=user_id,
        workspace_id=workspace_id,
        team_id=parent_scope.team_id,
        session_id=parent_scope.session_id,
        message_text=text,
        interface_language=presentation_locale,
    )
    # The follow-up always has a resolved language context, so it carries the zone.
    language_context = language_context.with_time_zone(viewer_time_zone)
    return TurnRequest(
        session_id=parent_scope.session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        member_uid=member_uid or "",
        text=text,
        turn_id=turn_id,
        team_id=parent_scope.team_id,
        planning_mode=(planning_mode if isinstance(planning_mode, str) else None),
        language_context=language_context,
        attachments=list(attachments),
        access_context=access.as_dict(),
        datarc_client=datarc.datarc_client,
        datarc_unavailable=datarc.datarc_unavailable,
        vector_client=datarc.vector_client,
        active_datarc_project=datarc.active_datarc_project,
        datarc_binding=datarc.connection_binding,
        datarc_source_evidence=source_evidence,
        active_datarc_workspace_name=(datarc.active_datarc_workspace_name or ""),
        accessible_datarc_workspaces=(datarc.accessible_datarc_workspaces or ()),
        datarc_absence_reason=datarc.datarc_absence_reason or "",
        team_router=team_router,
        team_kb_retriever=team_kb,
        resume_turn_id=parent_scope.turn_id,
        # A queued follow-up is the member's own web message (web auth fence, web language): attended.
        config_lite_supported=True,
        terminal_auth_fence_vector_digest=current_digest,
    )


async def build_terminal_approval_executor(
    app: Any,
    *,
    approval_store: Any,
    approval: Any,
    team_id: str,
    effect_user_id: str,
) -> Any:
    from bowhead_core.approval_executor import ApprovalExecutor

    from bowhead_gateway.datarc_context import resolve_datarc_turn_context

    datarc = await resolve_datarc_turn_context(
        app,
        team_id=team_id,
        workspace_id=approval.workspace_id,
        user_id=effect_user_id,
        pinned_project=approval.datarc_workspace_id,
    )
    member_uid = await _resolve_recovery_member_uid(
        app,
        user_id=effect_user_id,
        datarc_client=datarc.datarc_client,
        datarc_workspace_id=datarc.datarc_project_for_calls,
    )
    orchestrator = getattr(app.state, "orchestrator", None)
    registry = getattr(app.state, "tool_registry", None)
    if registry is None and orchestrator is not None:
        registry = orchestrator.tool_registry
    if registry is None:
        raise DecisionAuthorizationDeniedError("dependency_missing")
    team_router, _team_kb = await resolve_terminal_team_runtime(
        app.state,
        team_id,
    )
    return ApprovalExecutor(
        approval_store=approval_store,
        tool_registry=registry,
        datarc=datarc.datarc_client,
        active_datarc_project=datarc.active_datarc_project,
        datarc_binding=datarc.connection_binding,
        approver_member_uid=member_uid or None,
        datarc_unavailable=datarc.datarc_unavailable,
        vector_client=datarc.vector_client,
        event_registry=(orchestrator.event_registry if orchestrator else None),
        turn_log_store=getattr(app.state, "turn_log_store", None),
        user_store=getattr(app.state, "user_store", None),
        access_control_store=getattr(
            app.state,
            "access_control_store",
            None,
        ),
        notification_service=getattr(
            app.state,
            "notification_service",
            None,
        ),
        target_team_id=team_id,
        team_router=team_router,
    )


__all__ = [
    "TerminalPendingExecutionRecovery",
    "build_terminal_approval_executor",
    "resolve_terminal_followup_request",
    "resolve_terminal_team_runtime",
]
