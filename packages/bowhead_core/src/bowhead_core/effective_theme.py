"""Shared theme render contracts and the Community product-default resolver."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Final, Protocol

from bowhead_core.legacy_theme_registry import legacy_m1_compiled_scheme
from bowhead_core.theme_compiler import product_default_compiled_scheme
from bowhead_core.theme_config import resolve_effective_theme

RESOLVER_SCHEMA_VERSION: Final = 2


@dataclass(frozen=True, slots=True)
class ResolvedThemeBundle:
    payload: dict[str, Any]
    revision_id: str
    revision_number: int
    revision_checksum: str


@dataclass(frozen=True, slots=True)
class ThemeStateSnapshot:
    """Immutable identity used to detect organization policy races."""

    active_revision_id: str
    fallback_revision_id: str
    version: tuple[str, str, str]


class ThemeResolver(Protocol):
    async def read_state(self, team_id: str) -> ThemeStateSnapshot | None: ...

    async def revision_checksum(self, team_id: str, revision_id: str) -> str | None: ...

    async def resolve(
        self,
        *,
        team_id: str,
        team_policy: dict[str, Any],
        user_config: dict[str, Any],
        allow_custom: bool,
        render_safe_mode: bool,
    ) -> ResolvedThemeBundle: ...


class DefaultThemeResolver:
    """Resolve Bowra defaults and personal choices without organization branding."""

    async def read_state(self, team_id: str) -> None:
        return None

    async def revision_checksum(self, team_id: str, revision_id: str) -> None:
        return None

    async def resolve(
        self,
        *,
        team_id: str,
        team_policy: dict[str, Any],
        user_config: dict[str, Any],
        allow_custom: bool,
        render_safe_mode: bool,
    ) -> ResolvedThemeBundle:
        return resolve_legacy_theme(
            team_id=team_id,
            team_policy={"version": 1},
            user_config=user_config,
            application_name="Bowra",
        )


def resolve_legacy_theme(
    *,
    team_id: str,
    team_policy: dict[str, Any],
    user_config: dict[str, Any],
    application_name: str = "Bowhead",
) -> ResolvedThemeBundle:
    resolved = resolve_effective_theme(team_policy, user_config)
    if resolved["source"]["colorProfile"] == "product":
        # No team or member chose a color: render the product default (SaaS Blue since 1.5.0).
        compiled = product_default_compiled_scheme()
    else:
        compiled = legacy_m1_compiled_scheme(resolved["effective"]["colorProfile"]["id"])
    resolved["effective"]["colorScheme"] = public_color_scheme(compiled)
    resolved["brand"] = brand_payload(
        application_name=application_name,
        logo_alt=application_name,
        logo_light_artifact_id=None,
        logo_dark_artifact_id=None,
        mark_artifact_id=None,
        favicon_artifact_id=None,
    )
    resolved["schemaVersion"] = RESOLVER_SCHEMA_VERSION
    resolved["team"] = {
        "id": team_id,
        "themeRevision": None,
        "revisionId": None,
        "configurationType": "legacy",
    }
    return ResolvedThemeBundle(
        payload=resolved,
        revision_id="legacy",
        revision_number=0,
        revision_checksum=compiled["checksum"],
    )


def public_color_scheme(compiled: dict[str, Any]) -> dict[str, Any]:
    return {
        "tokenSchemaVersion": compiled["tokenSchemaVersion"],
        "light": copy.deepcopy(compiled["light"]),
        "dark": copy.deepcopy(compiled["dark"]),
        "checksum": compiled["checksum"],
    }


def brand_payload(
    *,
    application_name: str,
    logo_alt: str,
    logo_light_artifact_id: str | None,
    logo_dark_artifact_id: str | None,
    mark_artifact_id: str | None,
    favicon_artifact_id: str | None,
) -> dict[str, Any]:
    initial = next(iter(application_name), "B").upper()

    def logo(artifact_id: str | None) -> dict[str, Any]:
        if artifact_id is None:
            return {"kind": "text", "text": application_name}
        return _asset(artifact_id)

    return {
        "applicationName": application_name,
        "logoAlt": logo_alt,
        "logoOnLight": logo(logo_light_artifact_id),
        "logoOnDark": logo(logo_dark_artifact_id),
        "mark": (_asset(mark_artifact_id) if mark_artifact_id else {"kind": "generatedInitial", "text": initial}),
        "favicon": (
            _asset(favicon_artifact_id or mark_artifact_id)
            if favicon_artifact_id or mark_artifact_id
            else {"kind": "generatedInitial", "text": initial}
        ),
    }


def _asset(artifact_id: str) -> dict[str, Any]:
    return {
        "kind": "asset",
        "artifactId": artifact_id,
        "path": f"/api/v1/branding/assets/{artifact_id}",
    }
