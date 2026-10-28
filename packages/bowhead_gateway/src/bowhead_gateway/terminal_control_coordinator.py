"""System-owned Terminal Protocol controls for expiry/revocation surfaces."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def expire_due_terminal_decisions(
    app: Any,
    *,
    ttl_hours: float = 24.0,
    limit: int = 100,
) -> int:
    """Expire due v1 sources through source CAS + N+1 control receipts."""

    factory = getattr(app.state, "terminal_runtime_factory", None)
    protocol_store = getattr(app.state, "terminal_protocol_store", None)
    clarification_store = getattr(app.state, "clarification_store", None)
    approval_store = getattr(app.state, "approval_store", None)
    if factory is None or protocol_store is None:
        return 0
    if ttl_hours <= 0 or not 1 <= limit <= 100:
        raise ValueError("terminal expiry scan bounds are invalid")

    from bowhead_core.terminal_protocol import (
        DecisionAction,
        DecisionPrincipalSet,
        DecisionSourceKind,
    )

    candidates: list[tuple[Any, DecisionSourceKind]] = []
    clarifications: list[Any] = []
    if clarification_store is not None:
        try:
            clarifications = list(
                await clarification_store.list_v1_expiry_candidates(
                    ttl_hours=ttl_hours,
                    limit=limit,
                )
            )
        except Exception as exc:
            logger.warning(
                "terminal clarification expiry scan deferred error=%s",
                type(exc).__name__,
            )
    approvals: list[Any] = []
    if approval_store is not None:
        try:
            approvals = list(
                await approval_store.list_v1_expiry_candidates(
                    ttl_hours=ttl_hours,
                    limit=limit,
                )
            )
        except Exception as exc:
            logger.warning(
                "terminal approval expiry scan deferred error=%s",
                type(exc).__name__,
            )
    for index in range(max(len(clarifications), len(approvals))):
        if index < len(clarifications):
            candidates.append(
                (
                    clarifications[index],
                    DecisionSourceKind.CLARIFICATION,
                )
            )
        if index < len(approvals):
            candidates.append((approvals[index], DecisionSourceKind.APPROVAL))

    completed = 0
    for item, source_kind in candidates:
        if completed >= limit:
            break
        source_id = item.request_id if source_kind is DecisionSourceKind.CLARIFICATION else item.id
        actor = "system:clarification-expiry" if source_kind is DecisionSourceKind.CLARIFICATION else "system:approval-expiry"
        try:
            team_id, session_id, version = await protocol_store.read_persisted_turn_scope(
                turn_id=item.turn_id,
            )
            if version != 1:
                continue
            prepared = await factory.record_source_decision(
                team_id=team_id,
                session_id=session_id,
                turn_id=item.turn_id,
                source_kind=source_kind,
                source_id=source_id,
                action=DecisionAction.EXPIRE,
                principals=DecisionPrincipalSet(
                    item.user_id,
                    actor,
                    item.user_id,
                ),
            )
            await factory.commit_decision_control(
                prepared,
                reason_code="decision_expired",
                message_key="turn.decision_expired",
            )
            completed += 1
        except Exception as exc:
            logger.warning(
                "terminal expiry deferred source=%s error=%s",
                source_id,
                type(exc).__name__,
            )
    return completed


async def revoke_inaccessible_terminal_decisions(
    app: Any,
    *,
    limit: int = 100,
) -> int:
    """Convert paused v1 sources that lost access into stable revoke receipts."""

    factory = getattr(app.state, "terminal_runtime_factory", None)
    protocol_store = getattr(app.state, "terminal_protocol_store", None)
    if factory is None or protocol_store is None:
        return 0
    from bowhead_core.terminal_protocol import (
        DecisionAction,
        DecisionPrincipalSet,
        DecisionSourceKind,
    )
    from bowhead_core.terminal_protocol_store import (
        DecisionAccessRevokedError,
        DecisionAuthorizationDeniedError,
    )

    from bowhead_gateway.terminal_decision_authorization import (
        TerminalDecisionAuthorizer,
    )

    authorizer = TerminalDecisionAuthorizer(app=app)
    if not 2 <= limit <= 100:
        raise ValueError("terminal revoke scan bounds are invalid")
    cursors = getattr(app.state, "terminal_revoke_scan_cursors", None)
    if not isinstance(cursors, dict):
        cursors = {"clarification": None, "approval": None}
        app.state.terminal_revoke_scan_cursors = cursors
    candidates: list[tuple[Any, DecisionSourceKind, str]] = []
    clarification_quota = max(1, limit // 2)
    approval_quota = max(1, limit - clarification_quota)
    clarification_store = getattr(app.state, "clarification_store", None)
    if clarification_store is not None:
        clarifications = await _read_rotating_pending_page(
            clarification_store.list_v1_pending_candidates,
            limit=clarification_quota,
            after_id=cursors.get("clarification"),
            identity=lambda item: item.request_id,
        )
        if clarifications:
            cursors["clarification"] = clarifications[-1].request_id
        candidates.extend((item, DecisionSourceKind.CLARIFICATION, item.request_id) for item in clarifications)
    approval_store = getattr(app.state, "approval_store", None)
    if approval_store is not None:
        approvals = await _read_rotating_pending_page(
            approval_store.list_v1_pending_candidates,
            limit=approval_quota,
            after_id=cursors.get("approval"),
            identity=lambda item: item.id,
        )
        if approvals:
            cursors["approval"] = approvals[-1].id
        candidates.extend((item, DecisionSourceKind.APPROVAL, item.id) for item in approvals)

    completed = 0
    for item, _source_kind, _source_id in candidates[:limit]:
        try:
            team_id, session_id, version = await protocol_store.read_persisted_turn_scope(turn_id=item.turn_id)
            if version != 1:
                continue
            await authorizer.authorize_pending_source(
                team_id=team_id,
                session_id=session_id,
                workspace_id=item.workspace_id,
                requester_ref=item.user_id,
            )
            continue
        except (
            DecisionAuthorizationDeniedError,
            DecisionAccessRevokedError,
        ) as denial:
            try:
                denial_reason = getattr(
                    denial,
                    "reason_code",
                    "access_revoked",
                )
                prepared = await factory.record_source_decision(
                    team_id=team_id,
                    session_id=session_id,
                    turn_id=item.turn_id,
                    source_kind=DecisionSourceKind.SYSTEM_CONTROL,
                    source_id="__auto_control__",
                    action=DecisionAction.REVOKE,
                    principals=DecisionPrincipalSet(
                        item.user_id,
                        "system:authorization-revocation",
                        item.user_id,
                    ),
                    source_values={
                        "denial_reason_code": denial_reason,
                    },
                )
                await factory.commit_decision_control(
                    prepared,
                    reason_code=(
                        "terminal_dependency_missing" if denial_reason in {"dependency_missing", "entitlement_denied"} else "access_revoked"
                    ),
                    message_key=("turn.failed" if denial_reason in {"dependency_missing", "entitlement_denied"} else "turn.access_revoked"),
                )
                completed += 1
            except Exception as exc:
                logger.warning(
                    "terminal authorization revoke deferred source=%s error=%s",
                    _source_id,
                    type(exc).__name__,
                )
        except Exception as exc:
            logger.warning(
                "terminal authorization revoke deferred source=%s error=%s",
                _source_id,
                type(exc).__name__,
            )
    return completed


async def _read_rotating_pending_page(
    reader: Any,
    *,
    limit: int,
    after_id: str | None,
    identity: Any,
) -> list[Any]:
    """Read one fair page, wrapping once without duplicating an item."""

    page = list(await reader(limit=limit, after_id=after_id))
    if after_id is None or len(page) >= limit:
        return page
    seen = {identity(item) for item in page}
    wrapped = await reader(limit=limit - len(page), after_id=None)
    page.extend(item for item in wrapped if identity(item) not in seen)
    return page[:limit]


__all__ = [
    "expire_due_terminal_decisions",
    "revoke_inaccessible_terminal_decisions",
]
