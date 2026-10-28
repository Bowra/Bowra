"""Gateway-owned runtime configuration for conversational Subagent drafts."""

from __future__ import annotations

from typing import Any

from bowhead_core.subagent_authoring_catalog import (
    SubagentAuthoringCatalogService,
)
from bowhead_core.subagent_draft_generator import SubagentDraftGenerator
from bowhead_core.subagent_draft_service import SubagentDraftService


def subagent_draft_hmac_key() -> bytes:
    """Derive the active purpose-specific key from the JWT signing keyring."""
    from bowhead_gateway.routes.auth import derive_jwt_secret_keys

    keys = derive_jwt_secret_keys("subagent-draft-hmac")
    if not keys:
        raise RuntimeError("subagent draft HMAC key unavailable")
    _key_id, secret = keys[0]
    return bytes.fromhex(secret)


def build_subagent_draft_service(
    *,
    app_state: Any,
    router: Any,
) -> SubagentDraftService:
    """Build the request/turn-scoped service from verified shared stores."""
    draft_store = getattr(app_state, "subagent_draft_store", None)
    definition_store = getattr(app_state, "user_store", None)
    accountant = getattr(
        app_state,
        "subagent_draft_credit_accountant",
        None,
    )
    if draft_store is None or definition_store is None or accountant is None:
        raise RuntimeError("subagent_draft_runtime_unavailable")
    return SubagentDraftService(
        draft_store=draft_store,
        definition_store=definition_store,
        catalog_service=SubagentAuthoringCatalogService(),
        generator=SubagentDraftGenerator(router),
        accountant=accountant,
    )


__all__ = [
    "build_subagent_draft_service",
    "subagent_draft_hmac_key",
]
