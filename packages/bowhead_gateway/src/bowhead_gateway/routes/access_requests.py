"""Access-request API — content access-request + approval (content-permissions wave 2).

pull-direction of sharing: a viewer who can't open a shared report files a
request; the owner (∪ team-admin) approves → a read grant is created.

**Uniform intake (anti-enumeration invariant).** The synchronous path of ``POST ""``
only reads the requester's own history (cooldown, rate limit) and always inserts a
``shadow`` placeholder, so every submit returns the same ``202 {status, request_id}``
whether or not the resource exists; a background task promotes the placeholder to a real
request only when the requester is allowed to ask for that resource. Requests cover
reports, team Skills and team Agents. See docs/sprints/access-requests/.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core.access_request_store import DecisionDetails
from bowhead_core.audit_actions import AuditAction
from bowhead_core.subagent_creator_trigger import subagent_creator_enabled
from bowhead_core.subagent_write_boundary import reject_obsolete_team_subagent_write
from bowhead_core.workflow_creator_trigger import workflow_creator_enabled
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from bowhead_core import share_service
from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _current_user

if TYPE_CHECKING:
    from bowhead_core.user_store import User

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_MESSAGE = 280
_RATE_LIMIT = 20  # requests/hour per requester (soft guideline; caps flood/badge-spam)
_RATE_WINDOW = 3600


# ── approver side (GET list + decide) ───────────────────────────────────────


async def _is_admin(user_store: Any, user: User) -> bool:
    # SP-AUTH-3: use current team only (get_team_role returns "owner" for owners).
    return (await user_store.get_team_role(user.id, user.team_id)) in {"owner", "team_admin"}


def _report_link(resource_id: str) -> str:
    return f"/?session={resource_id}"


async def _resolve_owner_nudge(notify: Any, team_id: str, request_id: str, reason: str) -> None:
    """Best-effort clear the owner's open 'access_requested' nudge. Used on the
    add_grant-failed recovery path so the badge can't dangle on a terminal row."""
    if notify is None:
        return
    try:
        await notify.resolve_access_requested(team_id, request_id, reason=reason)
    except Exception:
        logger.warning("access_request.resolve_owner_nudge_failed id=%s", request_id, exc_info=True)


@router.get("")
async def list_access_requests(
    request: Request,
    scope: str = "pending",
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Requests this user can decide (owner ∪ team-admin). Each carries the requester's
    full team identity + a re-resolved label + prior-denied count — the approver
    can_manage the resource, so no anti-enumeration concern on this side.

    ``scope=mine`` lists the caller's own requests instead; any other non-pending scope is
    the approver history."""
    if scope == "mine":
        return await _list_mine(request, user)
    acr_store = request.app.state.access_request_store
    user_store = request.app.state.user_store
    canvas_store = getattr(request.app.state, "canvas_store", None)
    is_admin = await _is_admin(user_store, user)
    rows = await acr_store.list_for_approver(
        team_id=user.team_id,
        user_id=user.id,
        is_admin=is_admin,
        scope=("pending" if scope == "pending" else "history"),
        limit=200,
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        res = await share_service.resolve_shareable(
            kind=row.kind,
            resource_id=row.resource_id,
            team_id=user.team_id,
            user_store=user_store,
            canvas_store=canvas_store,
            subagent_enabled=subagent_creator_enabled(),
            workflow_enabled=workflow_creator_enabled(),
        )
        requester = await user_store.get_user(row.requester_id)
        prior = (
            await acr_store.prior_denied_count(team_id=user.team_id, kind=row.kind, resource_id=row.resource_id, requester_id=row.requester_id)
            if row.status == "pending"
            else 0
        )
        items.append(
            {
                "id": row.id,
                "kind": row.kind,
                "resource_id": row.resource_id,
                "label": res.label if res else "(已删除)",
                "requester_id": row.requester_id,
                "requester_name": getattr(requester, "display_name", None) or row.requester_id,
                "requester_email": getattr(requester, "email", None),
                "message": row.message,
                "status": row.status,
                "prior_denied": prior,
                "created_at": row.created_at,
                "decided_by": row.decided_by,
                "decided_at": row.decided_at,
                "decision_note": row.decision_note,
                "granted_permission": row.granted_permission,
                "grant_expires_at": row.grant_expires_at,
                "expires_at": row.expires_at,
                "withdrawn_at": row.withdrawn_at,
            }
        )
    return {"items": items}


# ── out-of-band auto-resolve hooks (called from grant/delete sites) ──────────


async def resolve_request_after_grant(
    app: Any,
    *,
    team_id: str,
    kind: str,
    resource_id: str,
    requester_id: str,
    decided_by: str,
) -> None:
    """A grant for (requester, resource) arrived out-of-band (direct share / admin
    console) → auto-resolve that requester's pending request + notify. Best-effort:
    never blocks the grant op."""
    try:
        acr_store = getattr(app.state, "access_request_store", None)
        notify = getattr(app.state, "notification_service", None)
        if acr_store is None:
            return
        row = await acr_store.resolve_pending_for_grant(
            team_id=team_id, kind=kind, resource_id=resource_id, requester_id=requester_id, decided_by=decided_by
        )
        if row is not None and notify is not None:
            await notify.resolve_access_requested(team_id, row.id, reason="granted")
            await notify.emit_access_decided(row, approved=True, action_url=_report_link(resource_id) if kind == "report" else None)
        if row is not None:
            await _refresh_owner_digest(app, team_id, row.owner_id)
    except Exception:
        logger.warning("access_request.resolve_after_grant_failed kind=%s", kind, exc_info=True)


async def cascade_requests_for_resource(
    app: Any,
    *,
    team_id: str,
    kind: str,
    resource_id: str,
    decided_by: str,
    gone: bool,
) -> None:
    """Resource deleted (gone=True) or went team_wide (gone=False) → stale ALL its
    pending requests, resolve owner notifications, notify each requester
    (已不存在 / 已可访问). Best-effort."""
    try:
        acr_store = getattr(app.state, "access_request_store", None)
        notify = getattr(app.state, "notification_service", None)
        if acr_store is None:
            return
        if gone:
            # A deleted resource must look exactly like a missing one to the requester: the
            # request turns into a shadow row (pending until its original expiry, then the
            # ordinary expired notice); approvers' items are cleared now.
            demoted = await acr_store.demote_to_shadow_for_resource(team_id=team_id, kind=kind, resource_id=resource_id)
            for row in demoted:
                if notify is not None:
                    await notify.resolve_access_requested(team_id, row.id, reason="deleted")
            for owner_id in {row.owner_id for row in demoted if row.owner_id}:
                await _refresh_owner_digest(app, team_id, owner_id)
        rows = await acr_store.stale_pending_for_resource(team_id=team_id, kind=kind, resource_id=resource_id)
        if notify is None:
            return
        for row in rows:
            await notify.resolve_access_requested(team_id, row.id, reason="deleted" if gone else "team_wide")
            if gone:
                await notify.emit_access_decided(row, approved=False, stale=True)
            else:
                await notify.emit_access_decided(row, approved=True, action_url=_report_link(resource_id) if kind == "report" else None)
        for owner_id in {row.owner_id for row in rows if row.owner_id}:
            await _refresh_owner_digest(app, team_id, owner_id)
    except Exception:
        logger.warning("access_request.cascade_failed kind=%s", kind, exc_info=True)


# ── intake, requester views and decisions ────────────────────────────────────

# D2: fixed minimum grant per kind; the owner role is never grantable through a request.
GRANTED_PERMISSION = {"report": "read", "team_skill": "use", "team_subagent": "use"}
PENDING_TTL = timedelta(days=14)  # D3
GRANT_EXPIRY_DAYS = 30  # D3 opt-in "revoke after 30 days"
DENY_COOLDOWN = timedelta(days=7)  # D6
REPEAT_DENY_COOLDOWN = timedelta(days=30)  # D6: from the third denial on
REPEAT_DENY_THRESHOLD = 3
STOP_REQUESTS_COOLDOWN = timedelta(days=30)  # D6: approver "stop accepting for 30 days"
DAILY_RATE_LIMIT = 50
DAILY_RATE_WINDOW = 86_400
ESCALATE_AFTER = timedelta(hours=72)  # D5
RELEASE_CLAIM_TIMEOUT = timedelta(minutes=15)
# team_subagent is excluded: only the current Agent Owner can change its audience, so
# escalating to team admins would ask people who cannot act.
ESCALATION_KINDS = ("report", "team_skill")
DIGEST_THRESHOLD = 3
SWEEP_ACTOR = "system:access-request-sweeper"


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> None:
    """Run request follow-up work after the response, keeping a strong task reference.

    Every handler that must not reveal whether a resource exists schedules the
    same background work for real and shadow rows; only the background task branches."""
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    # Same shape as AccessRequestStore._now so string comparisons stay ordered.
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _seconds_until(iso_value: str, now: datetime) -> int:
    try:
        return max(1, int((datetime.fromisoformat(iso_value) - now).total_seconds()))
    except ValueError:
        return 1


def _coded(status_code: int, code: str, **extra: Any) -> HTTPException:
    """A coded error (``{detail: {code, ...}}``); the gateway error handler adds the
    localized catalog message. ``retry_after`` is mirrored into the Retry-After header."""
    headers = {"Retry-After": str(extra["retry_after"])} if "retry_after" in extra else None
    return HTTPException(status_code, detail={"code": code, **extra}, headers=headers)


class AccessRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # D4: one resource per request. D7: reason optional, max 280 chars.
    kind: Literal["report", "team_skill", "team_subagent"]
    resource_id: str = Field(min_length=1, max_length=200)
    message: str | None = Field(default=None, max_length=_MAX_MESSAGE)


class DecideDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "deny"]
    note: str | None = Field(default=None, max_length=_MAX_MESSAGE)
    # D3: permanent by default; the approver may opt into "revoke after 30 days".
    grant_expires_in_days: Literal[30] | None = None
    # D6: "stop accepting this person's requests for this resource for 30 days" (deny only).
    stop_requests: bool = False


def requester_state(row: Any, *, now: datetime | None = None) -> str:
    """Requester-facing state. Never exposes ``stale`` / ``approve_failed`` (they would tell
    the requester that the resource was deleted or that a grant failed): both read as
    ``expired``, exactly like an unanswered or shadow request. ``approved_pending`` (an
    Agent approval waiting for its release to go live) reads as approved-but-not-yet-active.
    A time-limited grant that has run out also reads as ``expired`` so the requester can
    ask again."""
    if row.status == "approved" and now is not None and _grant_has_expired(row, now):
        return "expired"
    if row.status in {"pending", "approved", "denied", "withdrawn", "approved_pending"}:
        return str(row.status)
    return "expired"


def _grant_has_expired(row: Any, now: datetime) -> bool:
    expires_at = getattr(row, "grant_expires_at", None)
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= now
    except (TypeError, ValueError):
        return False


def _requester_view(row: Any, *, cooldown_until: str | None, now: datetime, revoked: bool = False) -> dict[str, Any]:
    state = "expired" if revoked else requester_state(row, now=now)
    decided = state in {"approved", "denied", "approved_pending"}
    return {
        "request_id": row.id,
        "kind": row.kind,
        "resource_id": row.resource_id,
        "state": state,
        "message": row.message,
        # D9: the decision note verbatim; never who decided.
        "note": row.decision_note if decided else None,
        "created_at": row.created_at,
        "expires_at": row.expires_at if state == "pending" else None,
        "decided_at": row.decided_at if decided else None,
        "granted_permission": row.granted_permission if state in {"approved", "approved_pending"} else None,
        "grant_expires_at": row.grant_expires_at if state == "approved" else None,
        "retry_after": _seconds_until(cooldown_until, now) if cooldown_until and state == "denied" else None,
        "retry_at": cooldown_until if cooldown_until and state == "denied" else None,
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def request_access(
    body: AccessRequestDTO,
    request: Request,
    user: User = Depends(_current_user),
) -> Response:
    """Submit a request. Only the requester's own history decides the sync
    outcome: 409 during a cooldown on this resource (the requester already learned from
    the denial that it exists), 429 once their hourly/daily budget is spent, otherwise
    the same ``202 {status, request_id}`` for existing and missing resources."""
    app = request.app
    acr_store = app.state.access_request_store
    user_store = app.state.user_store
    now = _utcnow()
    resource_id = body.resource_id.strip()
    cooldown_until = await acr_store.active_cooldown_until(
        team_id=user.team_id, kind=body.kind, resource_id=resource_id, requester_id=user.id, now=_iso(now)
    )
    if cooldown_until is not None:
        raise _coded(409, "access_request_cooldown", retry_after=_seconds_until(cooldown_until, now), retry_at=cooldown_until)
    for bucket, limit, window in ((f"acr:{user.id}", _RATE_LIMIT, _RATE_WINDOW), (f"acr:day:{user.id}", DAILY_RATE_LIMIT, DAILY_RATE_WINDOW)):
        if not await user_store.rate_limit_check(bucket, limit=limit, window_s=window):
            epoch = int(time.time())
            raise _coded(429, "access_request_rate_limited", retry_after=max(1, (epoch // window + 1) * window - epoch))
    row, _created = await acr_store.enqueue_once(
        team_id=user.team_id,
        kind=body.kind,
        resource_id=resource_id,
        requester_id=user.id,
        owner_id=None,
        message=body.message,
        shadow=True,
        expires_at=_iso(now + PENDING_TTL),
    )
    # Always schedule promotion, created or deduplicated: promotion is CAS-safe (a no-op for
    # rows that are already real), it recovers a placeholder whose earlier promotion was lost,
    # and the synchronous work stays identical for existing and missing resources.
    _spawn(promote_request(app, row))
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": "submitted", "request_id": row.id})


class _Requestable:
    def __init__(self, res: Any, owner_id: str) -> None:
        self.res = res
        self.owner_id = owner_id


async def _resolve_requestable(app: Any, *, team_id: str, requester_id: str, kind: str, resource_id: str) -> _Requestable | None:
    """The resource and its approver when ``requester_id`` may ask for it; None otherwise
    (missing, cross-team, own resource, already accessible, not grantable)."""
    user_store = app.state.user_store
    access_store = getattr(app.state, "access_control_store", None)
    if access_store is None:
        return None
    res = await share_service.resolve_shareable(
        kind=kind,
        resource_id=resource_id,
        team_id=team_id,
        user_store=user_store,
        canvas_store=getattr(app.state, "canvas_store", None),
        subagent_enabled=subagent_creator_enabled(),
        workflow_enabled=workflow_creator_enabled(),
    )
    if res is None:
        return None
    if kind == "team_subagent":
        from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

        decision = await TeamSubagentAccessResolver(user_store.db_path).resolve(team_id=team_id, actor_user_id=requester_id, subagent_id=resource_id)
        if decision is None or decision.owner_user_id is None:
            return None
        resolution = decision.resolution
        # Only "not in the audience" is fixable by a request; disabled, archived or
        # Owner-less Agents and security denies are not.
        if resolution.can_manage or resolution.can_consume or resolution.denial_reason != "audience_not_allowed":
            return None
        return _Requestable(res, decision.owner_user_id)
    if not res.owner_id or res.owner_id == requester_id:
        return None
    granted = await share_service.grants_for_viewer(access_store, team_id=team_id, user_id=requester_id, kind=kind)
    if kind == "team_skill":
        if share_service.is_visible(
            owner_id=res.owner_id, visibility=res.visibility, resource_id=resource_id, viewer_id=requester_id, granted_ids=granted
        ):
            return None
        try:
            await share_service.ensure_team_skill_grantable(user_store, team_id=team_id, resource_id=resource_id)
        except share_service.SkillApprovalRequiredError:
            return None
        return _Requestable(res, res.owner_id)
    if resource_id in granted:
        return None
    return _Requestable(res, res.owner_id)


async def promote_request(app: Any, row: Any) -> None:
    """Background half of the intake: promote the shadow placeholder to a real
    request (owner routing, notification, audit) when it is requestable. Silent otherwise."""
    if not getattr(row, "shadow", False) or row.status != "pending":
        return  # already real (or decided): nothing to promote
    try:
        acr_store = app.state.access_request_store
        target = await _resolve_requestable(app, team_id=row.team_id, requester_id=row.requester_id, kind=row.kind, resource_id=row.resource_id)
        if target is None:
            _metric_inc("bowhead_access_request_submitted_total", {"kind": row.kind, "outcome": "shadow"})
            return
        approvers = await _approvers_for(app, row, target)
        if not approvers:
            # Nobody but the requester could act on it: a real request would sit unseen until
            # it expired. It stays a placeholder instead, which the requester cannot tell apart
            # from a request for a missing resource (pending, then expired), so the outcome
            # leaks neither the resource nor who may approve it. A later submit retries routing.
            _metric_inc("bowhead_access_request_submitted_total", {"kind": row.kind, "outcome": "no_approver"})
            return
        if not await acr_store.promote_shadow(request_id=row.id, team_id=row.team_id, owner_id=target.owner_id):
            return
        row.owner_id = target.owner_id
        row.shadow = False
        _metric_inc("bowhead_access_request_submitted_total", {"kind": row.kind, "outcome": "routed"})
        await _notify_new_request(app, row, target, approvers)
        await _system_audit(
            app,
            actor_user_id=row.requester_id,
            action=AuditAction.ACCESS_REQUEST_SUBMITTED,
            row=row,
            payload={"label": target.res.label, "message_present": bool(row.message)},
        )
    except Exception:
        logger.warning("access_request.promote_failed kind=%s", getattr(row, "kind", "?"), exc_info=True)


async def _is_active_member(user_store: Any, team_id: str, user_id: str | None) -> bool:
    if not user_id:
        return False
    return (await user_store.get_team_role(user_id, team_id)) is not None


async def _team_admins(user_store: Any, team_id: str) -> set[str]:
    return set(await user_store.list_team_users_by_roles(team_id, {"owner", "team_admin"}, include_creator=True))


async def _approvers_for(app: Any, row: Any, target: _Requestable) -> set[str]:
    """D5: who is asked to decide a new request. The resource owner while they are an active
    member; otherwise report / team_skill requests go to the team's other owners/team_admins.
    An Agent's audience is Owner-managed (team roles never decide it), so an Agent request
    whose Owner left has no approver. Empty means nobody but the requester could act."""
    user_store = app.state.user_store
    owner_id = target.owner_id
    if owner_id and owner_id != row.requester_id and await _is_active_member(user_store, row.team_id, owner_id):
        return {owner_id}
    if row.kind == "team_subagent":
        return set()
    return await _team_admins(user_store, row.team_id) - {row.requester_id}


async def _notify_new_request(app: Any, row: Any, target: _Requestable, approvers: set[str]) -> None:
    """Deliver to ``approvers`` (see ``_approvers_for``). From the ``DIGEST_THRESHOLD``-th
    pending request on, an owner gets one digest item instead of one item per request."""
    notify = getattr(app.state, "notification_service", None)
    if notify is None:
        return
    acr_store = app.state.access_request_store
    owner_id = target.owner_id
    if approvers != {owner_id}:
        await notify.emit_access_requested(row, target.res, recipients=approvers)
        return
    pending = await acr_store.pending_ids_for_owner(team_id=row.team_id, owner_id=owner_id)
    folded = len(pending) >= DIGEST_THRESHOLD
    await notify.emit_access_requested(row, target.res, recipients=set() if folded else {owner_id})
    if folded:
        await notify.refresh_access_digest(team_id=row.team_id, approver_id=owner_id, pending_request_ids=pending, threshold=DIGEST_THRESHOLD)


async def _refresh_owner_digest(app: Any, team_id: str, owner_id: str | None) -> None:
    notify = getattr(app.state, "notification_service", None)
    acr_store = getattr(app.state, "access_request_store", None)
    if notify is None or acr_store is None or not owner_id:
        return
    try:
        if not await _is_active_member(app.state.user_store, team_id, owner_id):
            return
        pending = await acr_store.pending_ids_for_owner(team_id=team_id, owner_id=owner_id)
        await notify.refresh_access_digest(team_id=team_id, approver_id=owner_id, pending_request_ids=pending, threshold=DIGEST_THRESHOLD)
    except Exception:
        logger.warning("access_request.digest_refresh_failed", exc_info=True)


def _metric_inc(name: str, labels: dict[str, str]) -> None:
    try:
        from bowhead_core import metrics as _m

        _m.inc(name, labels=labels)
    except Exception:  # noqa: S110 — metrics are advisory
        pass


async def _system_audit(app: Any, *, actor_user_id: str, action: str, row: Any, payload: dict[str, Any]) -> None:
    """Lifecycle audit for real requests only (shadow rows are never audited)."""
    if getattr(row, "shadow", False):
        return
    try:
        await app.state.user_store.write_admin_audit_log(
            actor_user_id=actor_user_id,
            team_id=row.team_id,
            action=action,
            target_kind=row.kind,
            target_id=row.resource_id,
            target_team_id=row.team_id,
            scope="team",
            payload={"via": "access_request", "request_id": row.id, "requester_id": row.requester_id, **payload},
        )
    except Exception:
        return


async def _list_mine(request: Request, user: User) -> dict[str, Any]:
    """scope=mine: the caller's own requests, shadow rows included and indistinguishable;
    no resource label, no approver identity (D9)."""
    await reconcile_agent_release_requests(request.app, team_id=user.team_id, requester_id=user.id, close_pending_with_access=False)
    acr_store = request.app.state.access_request_store
    now = _utcnow()
    rows = await acr_store.list_for_requester(team_id=user.team_id, requester_id=user.id, limit=200)
    items = []
    for row in rows:
        cooldown = row.cooldown_until if row.cooldown_until and row.cooldown_until > _iso(now) else None
        items.append(_requester_view(row, cooldown_until=cooldown, now=now))
    return {"items": items}


@router.get("/status", response_model=None)
async def access_request_status(
    request: Request,
    kind: Literal["report", "team_skill", "team_subagent"] = Query(...),
    resource_id: str = Query(..., min_length=1, max_length=200),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """The caller's latest request for one resource. Missing and inaccessible resources
    answer identically (shadow rows), so this cannot be used to probe existence."""
    # Requester reads only settle their own approved_pending rows: the pending-with-access
    # sweep runs in the sweeper and on Agent route events, so read timing never depends on it.
    await reconcile_agent_release_requests(request.app, team_id=user.team_id, requester_id=user.id, close_pending_with_access=False)
    acr_store = request.app.state.access_request_store
    now = _utcnow()
    rid = resource_id.strip()
    row = await acr_store.latest_for_requester(team_id=user.team_id, kind=kind, resource_id=rid, requester_id=user.id)
    cooldown = await acr_store.active_cooldown_until(team_id=user.team_id, kind=kind, resource_id=rid, requester_id=user.id, now=_iso(now))
    if row is None or row.status == "withdrawn":
        return {"state": "none", "request": None, "retry_after": _seconds_until(cooldown, now) if cooldown else None}
    # An approved request whose access was removed since (revoked grant, Agent audience
    # shrunk) reads as expired so the requester can ask again. Only approved rows reach this
    # check, and they exist only for resources the requester already learned about.
    revoked = row.status == "approved" and requester_state(row, now=now) == "approved" and await _access_was_removed(request.app, row)
    view = _requester_view(row, cooldown_until=cooldown, now=now, revoked=revoked)
    return {"state": view["state"], "request": view, "retry_after": view["retry_after"]}


async def _access_was_removed(app: Any, row: Any) -> bool:
    """True when the requester of an approved request could request this resource again,
    i.e. the resource still exists and they no longer have access to it."""
    try:
        target = await _resolve_requestable(app, team_id=row.team_id, requester_id=row.requester_id, kind=row.kind, resource_id=row.resource_id)
    except Exception:
        logger.warning("access_request.revocation_check_failed kind=%s", row.kind, exc_info=True)
        return False
    return target is not None


@router.post("/{request_id}/withdraw", response_model=None)
async def withdraw_access_request(
    request_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    """Requester withdraws a pending request. Someone else's request → 404."""
    app = request.app
    acr_store = app.state.access_request_store
    row, transitioned = await acr_store.withdraw_once(request_id=request_id, team_id=user.team_id, requester_id=user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if not transitioned:
        if row.status == "withdrawn":
            return {"status": "withdrawn"}
        raise _coded(409, "access_request_not_pending")
    # Same synchronous work for real and shadow rows; the follow-up branches in the background.
    _spawn(_after_withdraw(app, row))
    return {"status": "withdrawn"}


async def _after_withdraw(app: Any, row: Any) -> None:
    """Withdrawal does not notify approvers; it only clears their open items (real rows)."""
    if row.shadow:
        return
    notify = getattr(app.state, "notification_service", None)
    try:
        if notify is not None:
            await notify.resolve_access_requested(row.team_id, row.id, reason="withdrawn")
        await _refresh_owner_digest(app, row.team_id, row.owner_id)
        await _system_audit(app, actor_user_id=row.requester_id, action=AuditAction.ACCESS_REQUEST_WITHDRAWN, row=row, payload={})
    except Exception:
        logger.warning("access_request.withdraw_follow_up_failed id=%s", row.id, exc_info=True)


async def _resolve_for_approver(request: Request, user: User, row: Any) -> tuple[Any | None, bool]:
    """(resolved resource or None if gone, caller may decide)."""
    user_store = request.app.state.user_store
    res = await share_service.resolve_shareable(
        kind=row.kind,
        resource_id=row.resource_id,
        team_id=user.team_id,
        user_store=user_store,
        canvas_store=getattr(request.app.state, "canvas_store", None),
        subagent_enabled=subagent_creator_enabled(),
        workflow_enabled=workflow_creator_enabled(),
    )
    if res is None:
        return None, False
    if row.kind == "team_subagent":
        from bowhead_core.team_subagent_access import TeamSubagentAccessResolver

        # Audience is Owner-managed: team roles are not an Owner bypass (role matrix).
        decision = await TeamSubagentAccessResolver(user_store.db_path).resolve(
            team_id=user.team_id, actor_user_id=user.id, subagent_id=row.resource_id
        )
        if decision is None:
            return None, False
        return res, decision.resolution.can_manage
    return res, await share_service.can_manage_shares(user_store, user, res.owner_id)


@router.post("/{request_id}/decide", dependencies=[Depends(require_not_readonly)], response_model=None)
async def decide_access_request(
    request_id: str,
    body: DecideDTO,
    request: Request,
    user: User = Depends(_current_user),
) -> Response | dict[str, Any]:
    """Decide: optional note (D9, shown verbatim to the requester), optional
    30-day grant expiry (report / team_skill only), optional 30-day stop on deny (D6).
    Non-approvers, shadow rows and other teams' requests all answer 404."""
    app = request.app
    acr_store = app.state.access_request_store
    user_store = app.state.user_store
    notify = getattr(app.state, "notification_service", None)

    row = await acr_store.get(request_id, team_id=user.team_id)
    if row is None or row.shadow or row.requester_id == user.id:
        # Nobody decides their own request, whatever their role.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    res, can_decide = await _resolve_for_approver(request, user, row)
    if res is None:
        # Resource gone: the request becomes a shadow row, so the requester sees exactly what a
        # missing resource shows (pending until its original expiry, then expired).
        if row.status == "pending" and row.expires_at:
            await acr_store.demote_to_shadow_for_resource(team_id=user.team_id, kind=row.kind, resource_id=row.resource_id)
        else:
            await acr_store.set_decision_once(request_id=request_id, team_id=user.team_id, status="stale", decided_by=user.id)
        await _resolve_owner_nudge(notify, user.team_id, request_id, "stale")
        await _refresh_owner_digest(app, user.team_id, row.owner_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "resource not found")
    if not can_decide:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if body.action == "deny" and body.grant_expires_in_days is not None:
        raise _coded(422, "access_request_decision_invalid")
    if body.action == "approve" and body.stop_requests:
        raise _coded(422, "access_request_decision_invalid")
    if row.kind == "team_subagent" and body.grant_expires_in_days is not None:
        raise _coded(422, "access_request_grant_expiry_unsupported")
    if body.action == "approve" and row.kind == "team_subagent":
        from bowhead_gateway.routes.team_subagents import _require_tool_entitlement

        agent = await user_store.get_team_subagent(user.team_id, row.resource_id)
        await _require_tool_entitlement(request, list(getattr(agent, "tool_refs", None) or []))
        from bowhead_gateway.routes.team_subagents import _mutation_store

        if await _mutation_store(request, user_store).release_semantics_active():
            return await _decide_release_managed(request, user, row, res, body)

    now = _utcnow()
    approve = body.action == "approve"
    cooldown_until = None
    if not approve:
        denials = 1 + await acr_store.prior_denied_count(
            team_id=user.team_id, kind=row.kind, resource_id=row.resource_id, requester_id=row.requester_id
        )
        span = STOP_REQUESTS_COOLDOWN if body.stop_requests else (REPEAT_DENY_COOLDOWN if denials >= REPEAT_DENY_THRESHOLD else DENY_COOLDOWN)
        cooldown_until = _iso(now + span)
    grant_expires_at = _iso(now + timedelta(days=GRANT_EXPIRY_DAYS)) if approve and body.grant_expires_in_days else None
    details = DecisionDetails(
        note=(body.note or "").strip() or None,
        granted_permission=GRANTED_PERMISSION.get(row.kind) if approve else None,
        grant_expires_at=grant_expires_at,
        cooldown_until=cooldown_until,
    )
    target = "approved" if approve else "denied"
    row2, ok = await acr_store.set_decision_once(request_id=request_id, team_id=user.team_id, status=target, decided_by=user.id, decision=details)
    if not ok:
        return {"status": row2.status if row2 else "unknown"}

    if approve:
        failure = await _apply_grant(request, user, row2, grant_expires_at)
        if failure is not None:
            await acr_store.mark_approve_failed(request_id=request_id, team_id=user.team_id)
            await _resolve_owner_nudge(notify, user.team_id, request_id, "failed")
            await _refresh_owner_digest(app, user.team_id, row.owner_id)
            status_code, code = failure
            if code is None:
                raise HTTPException(status_code, "grant failed" if status_code == 500 else "not found")
            raise _coded(status_code, code)

    if notify is not None:
        try:
            action_url = _report_link(row.resource_id) if approve and row.kind == "report" else None
            await notify.emit_access_decided(row2, approved=approve, action_url=action_url)
            if approve and row.kind != "team_subagent":
                await notify.emit_access_admin_decided(row2, res.owner_id or "", res.label)
            await notify.resolve_access_requested(user.team_id, request_id, reason=body.action)
        except Exception:
            logger.warning("access_request.notify_failed id=%s action=%s", request_id, body.action, exc_info=True)
    await _refresh_owner_digest(app, user.team_id, row.owner_id)
    try:
        await user_store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=AuditAction.ACCESS_REQUEST_APPROVED if approve else AuditAction.ACCESS_REQUEST_DENIED,
            target_kind=row.kind,
            target_id=row.resource_id,
            target_team_id=user.team_id,
            scope="team",
            payload={
                "via": "access_request",
                "request_id": row.id,
                "requester_id": row.requester_id,
                "label": res.label,
                "granted_permission": details.granted_permission,
                "grant_expires_at": grant_expires_at,
                "note_present": details.note is not None,
                "cooldown_until": cooldown_until,
                "stop_requests": body.stop_requests,
            },
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.warning("access_request.audit_failed id=%s", request_id, exc_info=True)
    return {"status": target}


async def _decide_release_managed(request: Request, user: User, row: Any, res: Any, body: DecideDTO) -> dict[str, Any]:
    """Approve an Agent request after the release cutover through the normal publication
    path (``access_request_agent_release``).

    The request is claimed first (CAS ``pending`` → ``approved_pending`` with the claim
    sentinel), so a concurrent withdraw, deny or expiry cannot race the draft write and a
    second approval never overwrites it. If the draft cannot be saved the claim is reverted
    to ``pending``; otherwise it stays ``approved_pending`` until the Owner publishes, or
    becomes ``approved`` if the live audience already includes the requester."""
    from bowhead_core.access_request_agent_release import AgentReleaseApprovalError, request_audience_release
    from bowhead_core.access_request_store import RELEASE_CLAIM

    from bowhead_gateway.routes.agents import _raise_release_http, _revision_store

    app = request.app
    acr_store = app.state.access_request_store
    user_store = app.state.user_store
    details = DecisionDetails(
        note=(body.note or "").strip() or None,
        granted_permission=GRANTED_PERMISSION["team_subagent"],
        release_candidate_id=RELEASE_CLAIM,
    )
    claimed, ok = await acr_store.set_decision_once(
        request_id=row.id, team_id=user.team_id, status="approved_pending", decided_by=user.id, decision=details
    )
    if not ok:
        return {"status": claimed.status if claimed else "unknown"}  # idempotent
    try:
        outcome = await request_audience_release(
            await _revision_store(request, user=user, store=user_store),
            team_id=user.team_id,
            agent_id=row.resource_id,
            owner_user_id=user.id,
            requester_id=row.requester_id,
        )
    except AgentReleaseApprovalError as exc:
        await acr_store.revert_release_claim(request_id=row.id)
        raise _coded(409, exc.code) from None
    except HTTPException:
        await acr_store.revert_release_claim(request_id=row.id)
        raise
    except Exception as exc:
        await acr_store.revert_release_claim(request_id=row.id)
        _raise_release_http(exc)  # the publication path's own reason; the request is pending again
    target = "approved" if outcome.state == "noop" else "approved_pending"
    row2 = await acr_store.finalize_release_claim(request_id=row.id, status=target, candidate_id=outcome.candidate_id)
    if row2 is None:
        # The claim was settled elsewhere meanwhile (e.g. the stale-claim recovery): report the
        # current state and leave notifications to whoever settled it.
        current = await acr_store.get(row.id, team_id=user.team_id)
        return {"status": current.status if current else "unknown"}
    notify = getattr(app.state, "notification_service", None)
    if notify is not None:
        try:
            await notify.emit_access_decided(row2, approved=True, outcome=target)
            await notify.resolve_access_requested(user.team_id, row.id, reason="approve")
        except Exception:
            logger.warning("access_request.notify_failed id=%s action=approve", row.id, exc_info=True)
    await _refresh_owner_digest(app, user.team_id, row.owner_id)
    try:
        await user_store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=AuditAction.ACCESS_REQUEST_APPROVED,
            target_kind=row.kind,
            target_id=row.resource_id,
            target_team_id=user.team_id,
            scope="team",
            payload={
                "via": "access_request",
                "request_id": row.id,
                "requester_id": row.requester_id,
                "label": res.label,
                "granted_permission": details.granted_permission,
                "note_present": details.note is not None,
                "release_state": outcome.state,
                "release_candidate_id": outcome.candidate_id,
            },
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.warning("access_request.audit_failed id=%s", row.id, exc_info=True)
    return {"status": target}


async def _notify_release_outcome(app: Any, row: Any, outcome: str) -> None:
    """Notifications for an Agent request whose release outcome was just settled."""
    notify = getattr(app.state, "notification_service", None)
    if notify is None:
        return
    if outcome == "approved_pending":
        await notify.emit_access_decided(row, approved=True, outcome="approved_pending")
        await notify.resolve_access_requested(row.team_id, row.id, reason="approve")
        return
    if outcome == "approved":
        await notify.emit_access_decided(row, approved=True, outcome="approved")
        await notify.resolve_access_requested(row.team_id, row.id, reason="granted")
        await _refresh_owner_digest(app, row.team_id, row.owner_id)
        return
    # Requester side: the same neutral "no longer active" notice and /status value as any
    # request that did not take effect (D9: no reason, no approver).
    await notify.emit_access_request_expired(row)
    await notify.resolve_access_requested(row.team_id, row.id, reason="failed")
    agent = await app.state.user_store.get_team_subagent(row.team_id, row.resource_id)
    label = (getattr(agent, "display_name", None) or getattr(agent, "name", None) or row.resource_id) if agent else row.resource_id
    await notify.emit_access_request_release_failed(row, label=label)


async def recover_stale_release_claims(app: Any, *, now: datetime) -> dict[str, int]:
    """Claims older than ``RELEASE_CLAIM_TIMEOUT``: settle from the publication receipt when the
    publish committed; revert to pending only when it never did."""
    from bowhead_core.access_request_agent_release import settle_stale_claim
    from bowhead_core.agent_revision_store import AgentRevisionStore

    acr_store = app.state.access_request_store
    revision_store = AgentRevisionStore(app.state.user_store.db_path, validator=None)  # type: ignore[arg-type]
    counts: dict[str, int] = {}
    for row in await acr_store.list_stale_release_claims(decided_before=_iso(now - RELEASE_CLAIM_TIMEOUT)):
        try:
            outcome = await settle_stale_claim(acr_store, revision_store, row)
        except Exception:
            logger.warning("access_request.stale_claim_failed id=%s", row.id, exc_info=True)
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome != "reverted":
            settled = await acr_store.get(row.id)
            if settled is not None:
                try:
                    await _notify_release_outcome(app, settled, outcome)
                except Exception:
                    logger.warning("access_request.stale_claim_notify_failed id=%s", row.id, exc_info=True)
    return counts


async def reconcile_agent_release_requests(
    app: Any,
    *,
    team_id: str | None = None,
    agent_id: str | None = None,
    requester_id: str | None = None,
    close_pending_with_access: bool = True,
) -> int:
    """Settle ``approved_pending`` Agent requests (release live → approved; candidate
    rejected / stale / withdrawn or release without the requester → approve_failed) and
    notify. Best-effort."""
    acr_store = getattr(app.state, "access_request_store", None)
    user_store = getattr(app.state, "user_store", None)
    if acr_store is None or user_store is None:
        return 0
    try:
        from bowhead_core.access_request_agent_release import reconcile_release_pending
        from bowhead_core.agent_revision_store import AgentRevisionStore

        settled = await reconcile_release_pending(
            acr_store,
            user_store.db_path,
            team_id=team_id,
            agent_id=agent_id,
            requester_id=requester_id,
            close_pending_with_access=close_pending_with_access,
            # Working-copy writes only; no publication validator is needed for the cleanup.
            revision_store=AgentRevisionStore(user_store.db_path, validator=None),  # type: ignore[arg-type]
        )
        for row, outcome in settled:
            await _notify_release_outcome(app, row, outcome)
        return len(settled)
    except Exception:
        logger.warning("access_request.release_reconcile_failed", exc_info=True)
        return 0


async def _apply_grant(request: Request, user: User, row: Any, grant_expires_at: str | None) -> tuple[int, str | None] | None:
    """Create the fixed minimum grant. Returns None on success, else (status, code)."""
    app = request.app
    user_store = app.state.user_store
    access_store = app.state.access_control_store
    if row.kind == "team_subagent":
        from bowhead_core.team_subagent_store import (
            TeamSubagentAudienceForbiddenError,
            TeamSubagentAudienceInvalidError,
        )

        from bowhead_gateway.routes.team_subagents import _mutation_store

        # Pre-cutover only: release-managed Agents were routed to _decide_release_managed.
        # If the cutover lands between that check and this write, the Store refuses
        # (TeamSubagentRevisionWriterRequiredError) and the generic failure below applies.
        try:
            result = await _mutation_store(request, user_store).add_team_subagent_audience_user(
                user.team_id,
                row.resource_id,
                user_id=row.requester_id,
                access_store=access_store,
                actor_user_id=user.id,
                audit_action=AuditAction.SUBAGENT_AUDIENCE_CHANGED,
                audit_payload={"via": "access_request", "request_id": row.id},
            )
        except TeamSubagentAudienceInvalidError:
            return 409, "access_request_requester_inactive"
        except TeamSubagentAudienceForbiddenError:
            return 404, None
        except Exception:
            logger.warning("access_request.audience_add_failed id=%s", row.id, exc_info=True)
            return 500, None
        return None if result is not None else (404, None)
    # Agent audience writes never reach the generic grant path (they returned above).
    reject_obsolete_team_subagent_write(row.kind)
    try:
        await share_service.add_grant(
            access_store,
            kind=row.kind,
            resource_id=row.resource_id,
            team_id=user.team_id,
            principal_type="user",
            principal_id=row.requester_id,
            actor_id=user.id,
            user_store=user_store,
            expires_at=grant_expires_at,
        )
    except share_service.SkillApprovalRequiredError:
        return 409, "access_request_skill_not_grantable"
    except Exception:
        logger.warning("access_request.add_grant_failed id=%s", row.id, exc_info=True)
        return 500, None
    return None


# ── background lifecycle: expiry, escalation, member removal ─────────────────


async def sweep_access_requests_once(app: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Hourly job: expire pending requests past ``expires_at`` (real and shadow alike, so
    the requester cannot tell them apart) and escalate 72h-old real requests to team
    owner/team_admin."""
    counts = {"expired": 0, "escalated": 0}
    acr_store = getattr(app.state, "access_request_store", None)
    if acr_store is None:
        return counts
    notify = getattr(app.state, "notification_service", None)
    user_store = app.state.user_store
    now = now or _utcnow()
    # Rows written by the retired report-only intake keep its semantics: they never expire
    # and are not escalated again (that intake notified every admin). Only the marker is set.
    counts["legacy_marked"] = await acr_store.mark_legacy_rows(now=_iso(now))
    # An abandoned Agent release claim is settled from its publication receipt, or reverted.
    await recover_stale_release_claims(app, now=now)
    expired = await acr_store.expire_due(now=_iso(now))
    owners: set[tuple[str, str]] = set()
    for row in expired:
        counts["expired"] += 1
        if notify is not None:
            try:
                if not row.shadow:
                    await notify.resolve_access_requested(row.team_id, row.id, reason="expired")
                if not row.legacy_origin:
                    # Legacy-era rows are hidden from the requester (they exist only for
                    # resources that resolved), so their expiry is not announced either.
                    await notify.emit_access_request_expired(row)
            except Exception:
                logger.warning("access_request.expire_notify_failed id=%s", row.id, exc_info=True)
        await _system_audit(app, actor_user_id=SWEEP_ACTOR, action=AuditAction.ACCESS_REQUEST_EXPIRED, row=row, payload={})
        if not row.shadow and row.owner_id:
            owners.add((row.team_id, row.owner_id))
    for team_id, owner_id in owners:
        await _refresh_owner_digest(app, team_id, owner_id)

    due = await acr_store.due_escalations(created_before=_iso(now - ESCALATE_AFTER), kinds=ESCALATION_KINDS)
    for row in due:
        if not await acr_store.mark_escalated(request_id=row.id, now=_iso(now)):
            continue
        counts["escalated"] += 1
        if notify is None:
            continue
        try:
            res = await share_service.resolve_shareable(
                kind=row.kind,
                resource_id=row.resource_id,
                team_id=row.team_id,
                user_store=user_store,
                canvas_store=getattr(app.state, "canvas_store", None),
                subagent_enabled=subagent_creator_enabled(),
                workflow_enabled=workflow_creator_enabled(),
            )
            if res is None:
                continue
            recipients = await _team_admins(user_store, row.team_id) - {row.owner_id or "", row.requester_id}
            await notify.emit_access_request_escalated(row, label=res.label, recipients=recipients)
        except Exception:
            logger.warning("access_request.escalate_failed id=%s", row.id, exc_info=True)
    counts["release_settled"] = await reconcile_agent_release_requests(app)
    if counts["expired"] or counts["escalated"]:
        logger.info("access_request_sweeper.tick expired=%s escalated=%s", counts["expired"], counts["escalated"])
    return counts


async def void_requests_for_member(app: Any, *, user_id: str, team_id: str | None) -> int:
    """A member left the team (``team_id``) or deleted the account (``team_id=None``):
    their pending requests become stale and approvers' open items are cleared. No
    notification to the departed requester."""
    try:
        acr_store = getattr(app.state, "access_request_store", None)
        if acr_store is None:
            return 0
        rows = await acr_store.void_pending_for_requester(requester_id=user_id, team_id=team_id)
        notify = getattr(app.state, "notification_service", None)
        for row in rows:
            if notify is not None and not row.shadow:
                await notify.resolve_access_requested(row.team_id, row.id, reason="requester_left")
        for team, owner_id in {(row.team_id, row.owner_id) for row in rows if not row.shadow and row.owner_id}:
            await _refresh_owner_digest(app, team, owner_id)
        return len(rows)
    except Exception:
        logger.warning("access_request.void_for_member_failed", exc_info=True)
        return 0
