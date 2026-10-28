"""Load the named Gateway extension from the packaged build identity.

The manifest is a build input, not a deployment or request preference. Missing
Business code is an installation error; it never selects Community implicitly.
Audit, Organization API, branding and identity backends use explicit adapters.
This does not yet constitute a complete standalone Community application.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactStore
    from bowhead_core.user_store import UserStore
    from fastapi import APIRouter, FastAPI, Request, Response

    from bowhead_gateway.edition_identity import IdentityAdapter
    from bowhead_gateway.routes._deps import CreditAdminPrincipal

BUILD_MANIFEST_PATH = Path(__file__).with_name("build_manifest.json")


class AuditCsvExporter(Protocol):
    async def __call__(
        self,
        *,
        request: Request,
        principal: CreditAdminPrincipal,
        store: UserStore,
        from_ts: str | None,
        to_ts: str | None,
    ) -> Response: ...


class EntitlementInitializer(Protocol):
    async def __call__(self, app: FastAPI, user_store: UserStore, mode: str) -> None: ...


class BrandingInitializer(Protocol):
    async def __call__(self, app: FastAPI, artifact_store: ArtifactStore) -> None: ...


@dataclass(frozen=True)
class GatewayExtensions:
    """The fixed extension contracts required by extracted Business handlers."""

    routers: tuple[APIRouter, ...] = ()
    audit_csv_exporter: AuditCsvExporter | None = None
    organization_api_scopes: frozenset[str] = frozenset()
    organization_api_reference: tuple[dict[str, Any], ...] = ()
    branding_initializer: BrandingInitializer | None = None
    identity: IdentityAdapter | None = None
    entitlement_initializer: EntitlementInitializer | None = None


def read_build_edition() -> str:
    """Read and validate the fixed packaged edition identity."""
    manifest = json.loads(BUILD_MANIFEST_PATH.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "edition"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["edition"] not in ("community", "business")
    ):
        raise ValueError("Unsupported Gateway build manifest")
    return manifest["edition"]


def load_gateway_extensions() -> GatewayExtensions:
    """Resolve only the adapter named by the validated, packaged manifest."""
    if read_build_edition() == "community":
        return GatewayExtensions()

    module = import_module("bowhead_enterprise.gateway")
    extensions = module.gateway_extensions()
    if not isinstance(extensions, GatewayExtensions):
        raise TypeError("Business adapter returned an incompatible Gateway contract")
    return extensions


def install_gateway_extensions(app: FastAPI) -> None:
    """Register extension routes once during application construction."""
    extensions = load_gateway_extensions()
    app.state.edition = read_build_edition()
    app.state.gateway_extensions = extensions
    for router in extensions.routers:
        app.include_router(router)


async def initialize_branding_services(app: FastAPI, artifact_store: ArtifactStore) -> None:
    """Initialize the fixed edition's resolver before serving public HTML."""
    extensions: GatewayExtensions = app.state.gateway_extensions
    if extensions.branding_initializer is not None:
        await extensions.branding_initializer(app, artifact_store)
        return

    from bowhead_core.effective_theme import DefaultThemeResolver

    app.state.effective_theme_resolver = DefaultThemeResolver()


async def initialize_entitlement_services(app: FastAPI, user_store: UserStore, mode: str) -> None:
    """Select Community availability or the fixed Business purchase policy."""
    extensions: GatewayExtensions = app.state.gateway_extensions
    if extensions.entitlement_initializer is not None:
        await extensions.entitlement_initializer(app, user_store, mode)
        return
    if app.state.edition != "community":
        raise RuntimeError("Business entitlement adapter is unavailable")
    from bowhead_core.entitlements import CommunityResolver

    app.state.entitlements = CommunityResolver(mode=mode, user_store=user_store)
