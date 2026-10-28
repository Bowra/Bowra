"""Published branding bootstrap for public startup pages.

The team boundary comes exclusively from ``PublicAccessContext``.  Public
pages never accept a team selector and never call the authenticated effective
theme route.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bowhead_core.theme_branding_flags import (
    is_team_branding_render_safe_mode,
)
from bowhead_core.theme_config import (
    KEY_TEAM_THEME_POLICY,
    ThemeConfigError,
    parse_team_theme_policy,
)

if TYPE_CHECKING:
    from bowhead_core.effective_theme import ResolvedThemeBundle
    from fastapi import FastAPI

    from bowhead_gateway.public_entry_context import PublicAccessContext

logger = logging.getLogger(__name__)


async def resolve_public_branding(
    app: FastAPI,
    context: PublicAccessContext | None,
) -> dict[str, Any] | None:
    """Resolve a render-only public bundle from a trusted, bound entry."""

    bundle = await resolve_public_branding_bundle(app, context)
    return bundle.payload if bundle is not None else None


async def resolve_public_branding_bundle(
    app: FastAPI,
    context: PublicAccessContext | None,
) -> ResolvedThemeBundle | None:
    """Resolve the trusted bundle, retaining revision identity for durable jobs."""

    if context is None or context.team_id is None or context.entry_id is None:
        return None
    resolver = getattr(app.state, "effective_theme_resolver", None)
    if resolver is None:
        return None

    try:
        allow_custom = await _is_custom_branding_licensed(app, context.team_id)
        team_policy = await _legacy_team_policy(app, context.team_id)
        bundle = await resolver.resolve(
            team_id=context.team_id,
            team_policy=team_policy,
            user_config={"version": 1},
            allow_custom=allow_custom,
            render_safe_mode=is_team_branding_render_safe_mode(),
        )
    except Exception:
        logger.exception(
            "public_branding.resolve_failed",
            extra={
                "team_id": context.team_id,
                "entry_id": context.entry_id,
            },
        )
        return None
    return bundle


async def _is_custom_branding_licensed(app: FastAPI, team_id: str) -> bool:
    entitlements = getattr(app.state, "entitlements", None)
    deployment_mode = getattr(app.state, "deployment_mode", "saas")
    if entitlements is None:
        return deployment_mode != "local"
    try:
        return bool(await entitlements.is_feature_licensed(team_id, "custom_branding"))
    except Exception:
        logger.exception(
            "public_branding.entitlement_failed",
            extra={"team_id": team_id},
        )
        return False


async def _legacy_team_policy(app: FastAPI, team_id: str) -> dict[str, Any]:
    store = getattr(app.state, "team_settings_store", None)
    if store is None:
        return {"version": 1}
    raw = await store.get(team_id, KEY_TEAM_THEME_POLICY)
    if raw is None:
        return {"version": 1}
    try:
        return parse_team_theme_policy(raw)
    except ThemeConfigError:
        return {"version": 1}


__all__ = ["resolve_public_branding", "resolve_public_branding_bundle"]
