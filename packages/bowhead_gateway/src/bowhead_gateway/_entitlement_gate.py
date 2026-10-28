"""Gateway-side entitlement adapters (licensing sprint, tech-design d3 §3.2/§3.7).

Translates the core ``EntitlementDenied`` (pure-python, no FastAPI) into HTTP — keeping ``bowhead_core`` web-framework
-free. The decision logic lives in ``EntitlementResolver``; this module only maps deny → HTTPException and stamps the
grace/health header.

INV-1 (fail-CLOSED-by-mode): a missing resolver fails OPEN in ``saas``/test (matches the existing ``getattr(...,None)``
guards) but DENIES in ``local``. Community always requires its availability resolver, independent of outbound URL mode.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bowhead_core.entitlements import EntitlementDenied
from fastapi import HTTPException, Request, Response

# NOTE: Request/Response are imported at RUNTIME above (NOT under TYPE_CHECKING) on purpose:
# require_feature_licensed's inner dep is introspected by FastAPI, and with `from __future__ import annotations`
# the string annotation must resolve against module globals — a TYPE_CHECKING-only import would make FastAPI
# treat `request` as a query param (422 on every feature-gated route). Do not move them into the block below.
if TYPE_CHECKING:
    from bowhead_core.entitlements import EntitlementResolver, EntitlementSnapshot
    from fastapi import FastAPI


def _resolver(request: Request) -> EntitlementResolver | None:
    return getattr(request.app.state, "entitlements", None)


def _deployment_mode(request: Request) -> str:
    return getattr(request.app.state, "deployment_mode", "saas")


def _requires_resolver(state: object) -> bool:
    return getattr(state, "edition", "business") == "community" or getattr(state, "deployment_mode", "saas") == "local"


def _denied_to_http(exc: EntitlementDenied) -> HTTPException:
    """Machine-readable nested-dict detail (the only shape the FE branches on — §3.2)."""
    return HTTPException(
        status_code=exc.http_status,
        detail={"error": {"code": exc.reason}},
    )


def seat_denied_to_http(exc: EntitlementDenied) -> HTTPException:
    """Adapt a seat-gate ``EntitlementDenied`` (raised from inside ``create_user``'s funnel, §4.2) to HTTP.

    Shared by the three create_user callers (register / oauth first-login / datarc-SSO) so all surface the
    same nested-dict ``{"error":{"code","message"}}`` shape (seat_exceeded 403, or a frozen-team reason). In
    shadow mode the resolver never raises, so these wrappers stay inert until ``ENFORCE=1``.
    """
    return _denied_to_http(exc)


def _stamp_health(response: Response | None, snap: EntitlementSnapshot | None) -> None:
    """§3.7 WARN transport — surface the entitlement health (+ grace deadline) on the 200 turn response.

    In SHADOW mode (``ENFORCE=0``) ``assert_can_run_turn`` returns the would-deny snapshot (the turn still
    ran), so this can stamp a deny-health on a 200 — that is INTENTIONAL observability (see
    test_shadow_mode_allows_but_stamps_real_health). The FE never honors a deny-state on a 200 (it treats
    that as a shadow leak → active, no banner; see lib/entitlement-store.ts), so the ENFORCE=0 no-op holds
    at the consumer without suppressing the observability signal here.
    """
    if response is None or snap is None:
        return
    health = f"{snap.health};degraded" if snap.degraded else snap.health
    response.headers["X-Entitlement-Health"] = health
    if snap.grace_until:
        response.headers["X-Entitlement-Grace-Until"] = snap.grace_until


async def gate_turn_http(request: Request, team_id: str | None, response: Response | None = None) -> None:
    """Pre-flight turn gate for HTTP entries (chat.send §3.3①, clarifications.resume_turn §3.3②).

    Raises ``HTTPException`` (402/403) on deny — placed ABOVE ``create_task`` so a denied turn costs zero LLM.
    On allow, stamps ``X-Entitlement-Health`` on ``response`` if provided (grace WARN, §3.7).
    """
    ent = _resolver(request)
    if ent is None:
        if _requires_resolver(request.app.state):
            raise HTTPException(
                status_code=503,
                detail={"error": {"code": "entitlement_unavailable"}},
            )
        return  # saas/test: fail-open, consistent with the existing app.state getattr guards
    try:
        snap = await ent.assert_can_run_turn(team_id)
    except EntitlementDenied as exc:
        raise _denied_to_http(exc) from exc
    _stamp_health(response, snap)


async def _resolve_team_id(request: Request, ent: EntitlementResolver) -> str | None:
    """Resolve the team from the AUTHENTICATED user — never the ``X-Bowhead-User-Id`` / dev_default_user
    fallback (§5.2/§7.3 SEC-8). Best-effort: an auth failure returns ``None``, which the caller treats
    per-mode (saas → allow, local → deny). Imported lazily to avoid the auth ↔ _entitlement_gate cycle.
    """
    from bowhead_gateway.routes.auth import _current_user

    try:
        user = await _current_user(request)
    except HTTPException:
        return None  # no real auth → None (saas allows, local denies — INV-1)
    team_id = getattr(user, "team_id", None)
    if team_id:
        return team_id
    uid = getattr(user, "id", "") or ""
    return await ent.resolve_team_for_user(uid) if uid else None


async def assert_feature_licensed(request: Request, feature: str) -> None:
    """Core of the feature gate (§5.2) — raises 403/503 on deny, returns on allow. Usable inline (e.g. the
    ``scope=='audit'`` branch of export.csv) as well as via the ``require_feature_licensed`` dependency.

    INV-2: this is NOT authz — every route keeps its own authz dep; this only answers "did the team buy it".
    ``is_feature_licensed`` returns True in shadow AND in saas (Phase-1 saas doesn't gate per-team), so on 101
    this is a no-op; per-team gating bites only in local/on-prem. INV-1: local + no resolver/team = deny.
    """
    ent = _resolver(request)
    mode = _deployment_mode(request)
    if ent is None:
        if _requires_resolver(request.app.state):
            raise HTTPException(
                status_code=503,
                detail={"error": {"code": "entitlement_unavailable"}},
            )
        return  # saas/test: fail-open
    team_id = await _resolve_team_id(request, ent)
    if mode == "local" and not team_id and getattr(request.app.state, "edition", "business") != "community":
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "needs_license"}},
        )
    if getattr(request.app.state, "edition", "business") == "community":
        try:
            await ent.assert_can_run_turn(team_id)
        except EntitlementDenied as exc:
            raise _denied_to_http(exc) from exc
    if not await ent.is_feature_licensed(team_id, feature):
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "feature_unlicensed",
                    # Code-only: the localized copy comes from config/i18n/errors via the
                    # error handler. A hardcoded ``message`` would short-circuit that and
                    # ship Chinese to an English session. ``details`` remains the finite,
                    # public contract for localized UI guidance — clients must never infer
                    # a feature from a human-readable message.
                    "details": {"feature": feature},
                }
            },
        )


# §5.1 current feature keys (each has a runtime consumer): kb · scheduling · mcp · custom_branding · audit_export.


async def is_feature_licensed_request(request: Request, feature: str) -> bool:
    """Boolean variant of :func:`assert_feature_licensed` — same INV-1/INV-2 semantics, never raises.

    For read-side filtering (e.g. the connector-preset catalog: don't advertise presets a team can't
    create). saas/test fail-open → True; local + no resolver/team → deny → False; else the resolver's
    per-team answer (True in saas/shadow, real check in local/on-prem).
    """
    ent = _resolver(request)
    mode = _deployment_mode(request)
    if ent is None:
        return not _requires_resolver(request.app.state)
    team_id = await _resolve_team_id(request, ent)
    if mode == "local" and not team_id:
        return False
    return await ent.is_feature_licensed(team_id, feature)


def require_feature_licensed(feature: str):
    """FastAPI dependency wrapper around :func:`assert_feature_licensed` (§5.2)."""

    async def _dep(request: Request) -> None:
        await assert_feature_licensed(request, feature)

    return _dep


async def assert_not_readonly(request: Request) -> None:
    """Read-only gate (§7.5) — deny a state-mutating route when the team is frozen (health ∉
    {active, grace}: over_grace/suspended/needs_license/tamper, or local clock_tamper).

    Opt-in per-route, same resolution + shadow + INV-1 semantics as :func:`assert_feature_licensed`.
    The deny check IS the turn allow-check (a write on a frozen team is denied identically to a turn),
    so it reuses ``assert_can_run_turn`` (shadow-aware via the resolver's ``_raise_or_shadow``: in shadow
    it logs ``shadow_would_deny`` and allows). On 101 (saas, active) this is a no-op. grace still WRITES
    (the app works fully with a warning) — only the read-only family is blocked.
    """
    ent = _resolver(request)
    mode = _deployment_mode(request)
    if ent is None:
        if _requires_resolver(request.app.state):
            raise HTTPException(
                status_code=503,
                detail={"error": {"code": "entitlement_unavailable"}},
            )
        return  # saas/test: fail-open
    team_id = await _resolve_team_id(request, ent)
    if mode == "local" and not team_id and getattr(request.app.state, "edition", "business") != "community":
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "needs_license"}},
        )
    try:
        await ent.assert_can_run_turn(team_id)  # same allow-health projection as the turn gate
    except EntitlementDenied as exc:
        raise _denied_to_http(exc) from exc


async def require_not_readonly(request: Request) -> None:
    """FastAPI dependency form of :func:`assert_not_readonly` (§7.5). Bare (no factory) — attach via
    ``dependencies=[Depends(require_not_readonly)]`` on every state-mutating non-GET route of a touched
    router. An unclassified mutation route is a ship-blocker, NOT silently writable (LENS3-1)."""
    await assert_not_readonly(request)


def feature_license_checker(app: FastAPI, feature: str) -> Callable[[str], Awaitable[bool]]:
    """Read the live resolver for task scopes, including after license replacement."""

    async def check(team_id: str) -> bool:
        resolver = getattr(app.state, "entitlements", None)
        mode = getattr(app.state, "deployment_mode", "saas")
        if resolver is None:
            return not _requires_resolver(app.state)
        if mode == "local" and not team_id:
            return False
        return await resolver.is_feature_licensed(team_id, feature)

    return check
