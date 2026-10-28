"""Sprint β G7 — billing admin endpoints.

GET    /api/v1/admin/llm-config              — read provider config (fingerprint only, P0-S-1)
POST   /api/v1/admin/llm-config/providers    — add provider
POST   /api/v1/admin/llm-config/rotate       — rotate key (force flag, P0-O-6 + audit-before-verify P0-S-2)
DELETE /api/v1/admin/llm-config/providers/{name}
GET    /api/v1/admin/credits                 — team credit balance + quota
PUT    /api/v1/admin/credits/quota           — adjust quota (with reason ≥10)

Security:
- P0-S-1: NEVER expose plaintext key prefix; sha256(key)[:16] only
- P0-S-2: audit `llm_config_rotate_attempt` BEFORE verify-ping
- P0-O-6: force flag bypasses verify-ping (for provider outage)
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite
from bowhead_core.audit_actions import AuditAction
from bowhead_core.credit_admin_store import cache_invalidate_team
from bowhead_core.team_llm_config import (
    TeamLlmConfigError,
    build_catalog,
    compatibility_provider_list,
    is_v2_config,
    migrate_legacy_config_to_v2,
    redact_config,
    validate_v2_config,
)
from bowhead_core.team_llm_flags import model_runtime_parameters_enabled, runtime_parameter_changes_admitted
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.error_handler import localize_error_message
from bowhead_gateway.routes.auth import _current_user, _require_team_role_dep, _store
from bowhead_gateway.team_llm_apply_service import (
    TeamLlmApplyError,
    apply_team_llm_v2,
)
from bowhead_gateway.team_llm_readiness_service import (
    ProviderIdentityRequiredError,
    catalog_verification_entries,
    evaluate_team_llm_readiness,
    readiness_response,
)

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter()
logger = logging.getLogger(__name__)


_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")


async def _track_credit_attention_acted(
    request: Request,
    store: UserStore,
    *,
    team_id: str,
    actor_user_id: str,
    action_type: str,
    result: dict[str, Any],
) -> None:
    metrics_log = getattr(request.app.state, "metrics_log", None)
    if metrics_log is None:
        return
    for item_id in result.get("attention_item_ids", []):
        item = await store.get_credit_attention_item(team_id, str(item_id))
        attrs: dict[str, Any] = {
            "team_id": team_id,
            "attention_item_id": item_id,
            "action_type": action_type,
            "linked_event_type": action_type,
        }
        if item:
            for key in (
                "dedupe_key",
                "policy_key",
                "user_id",
                "workspace_id",
                "primary_segment",
                "severity",
            ):
                value = item.get(key)
                if value is not None:
                    attrs[key] = value
        await metrics_log.track(
            "credit.attention.acted",
            user_id=actor_user_id,
            attrs=attrs,
        )


def _key_fingerprint(key: str) -> str:
    """P0-S-1: sha256-only fingerprint, NO plaintext prefix.

    Old broken: f"{key[:6]}...{digest[:8]}"
    """
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _llm_config_features(request: Request) -> dict[str, Any]:
    """Feature flags the model settings page reads; GET and PUT return the same set."""
    return {
        "model_discovery_v2": True,
        "model_runtime_parameters": model_runtime_parameters_enabled(),
        "capability_probes": True,
        "azure_openai": True,
        "custom_http": True,
        # Address guidance must follow deployment mode: local permits HTTP/internal networks/bare IPs;
        # saas requires public HTTPS. Without the mode, one fixed hint is false for half the deployments.
        "deployment_mode": ("saas" if getattr(request.app.state, "deployment_mode", "saas") == "saas" else "local"),
    }


# ─── Read endpoints ────────────────────────────────────────


@router.get("/api/v1/admin/llm-config")
async def get_llm_config(
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """Return per-team LLM config, providers redacted (fingerprint only)."""
    try:
        cfg, version = await store.get_team_llm_config_with_version(user.team_id)
    except Exception as exc:
        # P2-T-4: surface decrypt failure not as empty list
        logger.exception(
            "team llm config decrypt failed team_id=%s",
            user.team_id,
        )
        raise HTTPException(
            500,
            {"code": "config_decrypt_failed"},
        ) from exc
    effective_cfg = migrate_legacy_config_to_v2(cfg) if cfg else {}

    response = {
        "providers": compatibility_provider_list(effective_cfg or cfg),
        "config": redact_config(effective_cfg or cfg),
        "version": version,
        "migrated_from_legacy": bool(cfg and not is_v2_config(cfg) and effective_cfg),
        "features": _llm_config_features(request),
    }
    if model_runtime_parameters_enabled():
        from bowhead_router.runtime_parameters import PROFILES

        response["parameter_profiles"] = list(PROFILES.values())
    try:
        evaluation = await evaluate_team_llm_readiness(
            db_path=store.db_path,
            team_id=user.team_id,
            actor_user_id=user.id,
            current_config=effective_cfg or cfg,
            draft_config=effective_cfg or cfg,
            expected_version=version,
            enrollment_source="first_v2_read",
        )
    except Exception as exc:
        logger.exception(
            "team LLM V2 projection failed team_id=%s",
            user.team_id,
        )
        raise HTTPException(
            503,
            {"code": "llm_verification_projection_unavailable"},
        ) from exc
    projected = readiness_response(evaluation)
    response["provider_identities"] = evaluation.provider_ids
    response["verification"] = {
        "as_of": projected["as_of"],
        "entries": [
            {
                "provider_id": row["provider_id"],
                "model_id": row["model_id"],
                "capability": row["capability"],
                "fact_state": row["fact_state"],
                "attempt_state": row["attempt_state"],
                "evidence_id": row["evidence_id"],
                "observed_at": row["observed_at"],
                "fresh_until": row["fresh_until"],
                "safe_code": row["safe_code"],
                # An identity mismatch needs the remote's self-reported ID to be actionable, not just a mismatch message.
                "actual_model_id": row["actual_model_id"],
            }
            for row in projected["rows"]
        ],
    }
    catalog_entries = await catalog_verification_entries(
        db_path=store.db_path,
        team_id=user.team_id,
        config=effective_cfg or cfg,
        provider_ids=evaluation.provider_ids,
    )
    # Catalog capabilities include models not currently selected for production.
    by_key = {(row["provider_id"], row["model_id"], row["capability"]): row for row in response["verification"]["entries"]}
    by_key.update({(row["provider_id"], row["model_id"], row["capability"]): row for row in catalog_entries})
    response["verification"]["entries"] = list(by_key.values())
    response["migration"] = {
        "state": ("available" if evaluation.migration_eligible else "not_required"),
        "enrolled_config_version": (evaluation.enrollment.config_version),
        "baseline_state": "none",
    }
    return response


@router.get("/api/v1/llm/catalog")
async def get_llm_catalog(
    request: Request,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict:
    """Secret-free catalog for normal chat users."""
    from bowhead_gateway.turn_service import record_team_model_resolution

    cfg: dict[str, Any] = {}
    version = 0
    try:
        cfg, version = await store.get_team_llm_config_with_version(user.team_id)
    except Exception as exc:
        logger.warning(
            "member_catalog.team_config_read_failed team=%s error_type=%s",
            user.team_id,
            type(exc).__name__,
        )
        record_team_model_resolution(
            entrypoint="catalog",
            result="resolver_error",
            reason="config_load_failed",
        )
        return build_catalog(config=None, version=0)
    try:
        catalog = build_catalog(
            config=cfg,
            version=version,
        )
    except Exception as exc:
        logger.warning(
            "member_catalog.build_failed team=%s error_type=%s",
            user.team_id,
            type(exc).__name__,
        )
        record_team_model_resolution(
            entrypoint="catalog",
            result="resolver_error",
            reason="config_invalid",
        )
        return build_catalog(config=None, version=version)
    if catalog.get("providers"):
        record_team_model_resolution(
            entrypoint="catalog",
            result="team_safe_success",
            reason="resolved",
        )
    else:
        record_team_model_resolution(
            entrypoint="catalog",
            result="team_unavailable",
            reason="config_missing" if not cfg else "config_invalid",
        )
    return catalog


@router.get("/api/v1/admin/credits")
async def get_team_credits(
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    team = await store.get_team(user.team_id)
    if not team:
        raise HTTPException(404, {"code": "team_not_found"})
    return {
        "team_id": team.id,
        "team_credit_balance": team.team_credit_balance,
        "team_credit_quota": team.team_credit_quota,
        "tier_credits": team.tier_credits,
        "contract_expires_at": team.contract_expires_at,
    }


# ─── Mutation endpoints ────────────────────────────────────


class LLMProviderAddBody(BaseModel):
    provider_name: str = Field(..., min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_\-]+$")
    api_key: str = Field(..., min_length=20, max_length=300)
    role: str = Field("fallback", pattern=r"^(lead|narrative|fallback|judge)$")


class LLMRotateBody(BaseModel):
    provider_name: str = Field(..., min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_\-]+$")
    new_api_key: str = Field(..., min_length=20, max_length=300)
    force: bool = False  # P0-O-6 skip verify-ping when provider down


class LLMConfigApplyReadiness(BaseModel):
    draft_semantic_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    acceptance_tokens: list[str] = Field(
        default_factory=list,
        max_length=100,
    )
    migration_acceptance_token: str | None = Field(
        None,
        max_length=10000,
    )


class LLMConfigPutBody(BaseModel):
    config: dict[str, Any]
    expected_version: int | None = Field(None, ge=0)
    confirm_embed_reindex: bool = False
    readiness: LLMConfigApplyReadiness | None = None
    configuration_proposal_id: str | None = Field(None, min_length=1, max_length=100)


class LLMConfigValidateBody(BaseModel):
    config: dict[str, Any]
    expected_version: int | None = Field(None, ge=0)


class LLMProviderVerifyBody(BaseModel):
    """WS4 test-connection — probe a provider by VALUE (before it is saved).

    api_key is accepted at min_length 1 (not the 20 the add/rotate bodies enforce)
    so a short/garbage key returns a clean `unauthorized` result instead of a 422 —
    the whole point of the button is to tell the user their key is wrong."""

    provider_type: str = Field(..., pattern=r"^(openai_compatible|anthropic_compatible)$")
    base_url: str = Field(..., min_length=1, max_length=500)
    api_key: str = Field(..., min_length=1, max_length=500)
    model: str | None = Field(None, max_length=200)
    anthropic_version: str | None = Field(None, max_length=40)
    messages_path: str | None = Field(None, max_length=80)
    models: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class LLMProviderDraftVerifyBody(BaseModel):
    """Probe edited connection fields while retaining the saved secret."""

    provider_name: str = Field(..., min_length=1, max_length=40)
    provider_type: str = Field(..., pattern=r"^(openai_compatible|anthropic_compatible)$")
    base_url: str = Field(..., min_length=1, max_length=500)
    api_key: str | None = Field(None, max_length=500)
    model: str | None = Field(None, max_length=200)
    anthropic_version: str | None = Field(None, max_length=40)
    messages_path: str | None = Field(None, max_length=80)
    models: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    @field_validator("api_key")
    @classmethod
    def _reject_blank_secret(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("api_key must be omitted or non-empty")
        return value


class LLMProviderCapabilityVerifyBody(LLMProviderVerifyBody):
    capability: str = Field(..., pattern=r"^(chat|tool|vision|embed|rerank|image_gen|web_search)$")
    capability_protocol: str | None = Field(None, min_length=1, max_length=40)


class LLMProviderDraftCapabilityVerifyBody(LLMProviderDraftVerifyBody):
    capability: str = Field(..., pattern=r"^(chat|tool|vision|embed|rerank|image_gen|web_search)$")
    capability_protocol: str | None = Field(None, min_length=1, max_length=40)


class LLMProviderNameBody(BaseModel):
    provider_name: str = Field(..., min_length=1, max_length=40)


async def _verify_provider_request(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str | None,
    models: list[dict[str, Any]],
    anthropic_version: str,
    messages_path: str | None,
) -> dict[str, Any]:
    from bowhead_router import verify_provider_capabilities

    return await verify_provider_capabilities(
        provider_type=provider_type,
        base_url=base_url,
        api_key=api_key,
        models=models or ([{"id": model, "capabilities": ["chat"]}] if model else []),
        anthropic_version=anthropic_version,
        messages_path=messages_path,
    )


async def _verify_provider_capability_request(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    capability: str,
    capability_protocol: str | None,
    anthropic_version: str,
    messages_path: str | None,
) -> dict[str, Any]:
    """Run one capability probe through the existing guarded verifier."""
    from bowhead_router import verify_provider_capabilities

    return await verify_provider_capabilities(
        provider_type=provider_type,
        base_url=base_url,
        api_key=api_key,
        models=[
            {
                "id": model,
                "capabilities": [capability],
                "capability_protocol": capability_protocol,
            },
        ],
        anthropic_version=anthropic_version,
        messages_path=messages_path,
    )


async def _discover_provider_models_request(*, base_url: str, api_key: str) -> dict[str, Any]:
    from bowhead_router import discover_provider_models

    return await discover_provider_models(base_url=base_url, api_key=api_key)


def _no_ctrl(v: str) -> str:
    # B17 sanitize CTRL chars (shared by all reason fields)
    if any(c in v for c in "\r\n\t\0\v\f"):
        raise ValueError("reason contains invalid characters")
    return v


class CreditQuotaBody(BaseModel):
    new_quota: int = Field(..., ge=0, le=100_000_000)
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def _v_reason(cls, v: str) -> str:
        return _no_ctrl(v)


# ─── licensing §7.1 — entitlement-field setters (caller-team-bound; saas only) ───────────────────────


class TeamTierCreditsBody(BaseModel):
    tier_credits: dict[str, int]
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def _v_reason(cls, v: str) -> str:
        return _no_ctrl(v)

    @field_validator("tier_credits")
    @classmethod
    def _v_credits(cls, v: dict[str, int]) -> dict[str, int]:
        if not v:
            raise ValueError("tier_credits must be non-empty")
        for tier, amount in v.items():
            if not isinstance(amount, int) or amount < 0:
                raise ValueError(f"tier_credits[{tier!r}] must be a non-negative int")
        return v


def _reject_if_license_managed(request: Request) -> None:
    """§7.1: in local mode the entitlement fields are license-owned → admin must re-upload the license,
    not mutate the DB mirror. saas = the writable source of truth."""
    if getattr(request.app.state, "edition", "business") == "business" and getattr(request.app.state, "deployment_mode", "saas") == "local":
        raise HTTPException(409, {"code": "managed_by_license"})


def _reject_plaintext_full_config(config: dict[str, Any]) -> None:
    from bowhead_core.llm_config_crypto import encrypt_llm_config

    token = encrypt_llm_config(config)
    if token.startswith("PLAIN:") and os.environ.get("BOWHEAD_ALLOW_PLAINTEXT_LLM_CONFIG") != "1":
        raise HTTPException(
            500,
            {"code": "llm_config_encryption_required"},
        )


def _invalidate_llm_runtime(request: Request, team_id: str) -> None:
    for attr in ("team_router_registry", "team_kb_runtime_registry"):
        registry = getattr(request.app.state, attr, None)
        invalidate = getattr(registry, "invalidate", None)
        if callable(invalidate):
            invalidate(team_id)


def _reindex_statuses(request: Request) -> dict[str, dict[str, Any]]:
    statuses = getattr(request.app.state, "team_kb_reindex_tasks", None)
    if not isinstance(statuses, dict):
        statuses = {}
        request.app.state.team_kb_reindex_tasks = statuses
    return statuses


def _upsert_provider_key(cfg: dict[str, Any], provider_name: str, api_key: str, role: str) -> None:
    if is_v2_config(cfg):
        providers = cfg.get("providers") or {}
        spec = providers.get(provider_name)
        if not isinstance(spec, dict):
            raise HTTPException(409, {"code": "provider_not_in_v2_config"})
        spec["api_key"] = api_key
        return
    providers = cfg.get("providers", [])
    if not isinstance(providers, list):
        providers = []
    providers = [p for p in providers if not isinstance(p, dict) or p.get("provider_name") != provider_name]
    providers.append(
        {
            "provider_name": provider_name,
            "api_key": api_key,
            "role": role,
        }
    )
    cfg["providers"] = providers


def _rotate_provider_key(cfg: dict[str, Any], provider_name: str, api_key: str) -> bool:
    if is_v2_config(cfg):
        providers = cfg.get("providers") or {}
        spec = providers.get(provider_name)
        if isinstance(spec, dict):
            spec["api_key"] = api_key
            return True
        return False
    for p in cfg.get("providers", []):
        if isinstance(p, dict) and p.get("provider_name") == provider_name:
            p["api_key"] = api_key
            return True
    return False


def _delete_provider_key(cfg: dict[str, Any], provider_name: str) -> bool:
    if is_v2_config(cfg):
        providers = cfg.get("providers") or {}
        spec = providers.get(provider_name)
        if isinstance(spec, dict):
            spec.pop("api_key", None)
            spec.pop("secret_key", None)
            return True
        return False
    original = cfg.get("providers", [])
    if not isinstance(original, list):
        return False
    remaining = [p for p in original if not isinstance(p, dict) or p.get("provider_name") != provider_name]
    if len(remaining) == len(original):
        return False
    cfg["providers"] = remaining
    return True


def _first_model_id(spec: dict[str, Any]) -> str | None:
    """First model id from a v2 provider spec. `models` is list[str] (legacy) or
    list[{id, ...}] (WS1 obj-shape); return the first usable id or None."""
    models = spec.get("models")
    if not isinstance(models, list):
        return None
    for m in models:
        if isinstance(m, str) and m:
            return m
        if isinstance(m, dict) and m.get("id"):
            return str(m["id"])
    return None


async def _ping_rotated_provider(cfg: dict[str, Any], provider_name: str, candidate_key: str) -> bool:
    """Probe a v2 provider with a candidate key (WS4). Returns True only on a
    CONFIRMED successful connection. Advisory-only: a False result must never block
    the rotation (the audit-before-verify contract keeps rotate best-effort, and
    legacy list-shaped providers carry no base_url/type to probe → False)."""
    if not is_v2_config(cfg):
        return False
    providers = cfg.get("providers") or {}
    spec = providers.get(provider_name) if isinstance(providers, dict) else None
    if not isinstance(spec, dict):
        return False
    ptype = spec.get("type")
    base_url = spec.get("base_url")
    if not ptype or not base_url:
        return False
    model = _first_model_id(spec) if ptype == "anthropic_compatible" else None
    if ptype == "anthropic_compatible" and not model:
        return False  # can't probe anthropic messages without a model id
    from bowhead_router import verify_provider

    result = await verify_provider(
        provider_type=str(ptype),
        base_url=str(base_url),
        api_key=candidate_key,
        model=model,
        anthropic_version=str(spec.get("anthropic_version") or "2023-06-01"),
        messages_path=spec.get("messages_path"),
    )
    return result.ok


def _embed_signature(config: dict[str, Any], model_name: str | None) -> tuple[Any, ...] | None:
    if not model_name or not is_v2_config(config):
        return None
    kb = config.get("kb") or {}
    models = kb.get("models") or {}
    spec = models.get(model_name)
    if not isinstance(spec, dict):
        return None
    providers = config.get("providers") or {}
    provider = providers.get(spec.get("provider")) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        return (
            model_name,
            spec.get("provider"),
            spec.get("real_id"),
            spec.get("dim"),
            None,
        )
    real_id = spec.get("real_id") or spec.get("model")
    declared_model = next(
        (model for model in provider.get("models") or [] if isinstance(model, dict) and model.get("id") == real_id),
        {},
    )
    capability_transports = declared_model.get("capability_transports") if isinstance(declared_model, dict) else None
    transport_ref = spec.get("transport_ref") or (capability_transports.get("embed") if isinstance(capability_transports, dict) else None)
    transports = provider.get("transports")
    selected_transport = transports.get(transport_ref) if isinstance(transports, dict) and transport_ref else None
    request_overrides = declared_model.get("request_body_overrides") if isinstance(declared_model, dict) else None
    embed_override = request_overrides.get("embed") if isinstance(request_overrides, dict) else None
    transport_semantics = json.dumps(
        {
            "provider_type": provider.get("type"),
            "endpoint": provider.get("endpoint") or provider.get("base_url"),
            "api_version": provider.get("api_version"),
            "default_headers": provider.get("default_headers") or {},
            "deployment": (declared_model.get("deployment") if isinstance(declared_model, dict) else None),
            "transport_ref": transport_ref,
            "transport": selected_transport,
            "request_body_override": embed_override,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        model_name,
        spec.get("provider"),
        real_id,
        spec.get("dim"),
        transport_semantics,
    )


def _preserve_provider_secrets(old_config: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if not is_v2_config(incoming):
        return incoming
    merged = copy.deepcopy(incoming)
    old_provider_secrets = _provider_secret_map(old_config)
    new_providers = merged.get("providers") or {}
    if not isinstance(new_providers, dict):
        return merged
    for name, spec in new_providers.items():
        if not isinstance(spec, dict):
            continue
        # Redaction metadata is response-only and must never count as a draft
        # change or be persisted back into the encrypted provider config.
        spec.pop("fingerprint", None)
        spec.pop("has_secret", None)
        auth = spec.get("auth")
        if isinstance(auth, dict):
            auth.pop("fingerprint", None)
            auth.pop("secret_configured", None)
        auth_disabled = isinstance(auth, dict) and auth.get("type") == "none"
        if auth_disabled:
            spec.pop("api_key", None)
            spec.pop("secret_key", None)
            auth.pop("secret", None)
        elif "api_key" not in spec and old_provider_secrets.get(str(name)):
            spec["api_key"] = old_provider_secrets[str(name)]
    return merged


def _provider_secret_map(config: dict[str, Any]) -> dict[str, str]:
    secrets: dict[str, str] = {}
    providers = config.get("providers") if isinstance(config, dict) else None
    if isinstance(providers, dict):
        for name, spec in providers.items():
            if not isinstance(spec, dict):
                continue
            auth = spec.get("auth")
            key = spec.get("api_key") or spec.get("secret_key") or (auth.get("secret") if isinstance(auth, dict) else None)
            if isinstance(key, str) and key:
                secrets[str(name)] = key
    elif isinstance(providers, list):
        for spec in providers:
            if not isinstance(spec, dict):
                continue
            name = spec.get("name") or spec.get("provider_name") or spec.get("id")
            key = spec.get("api_key") or spec.get("secret_key")
            if isinstance(name, str) and isinstance(key, str) and name and key:
                secrets[name] = key
    return secrets


_CONFIG_PATH_RE = re.compile(r"^(?P<path>(?:providers|tiers|agents|kb|media|search)(?:\.[^\s]+)*)")


def _config_validation_issue(error: TeamLlmConfigError) -> dict[str, Any]:
    error_text = str(error)
    path_match = _CONFIG_PATH_RE.match(error_text)
    path = path_match.group("path").rstrip(":") if path_match else None
    lowered = error_text.lower()
    if "schema_version" in lowered:
        code = "invalid_schema"
    elif "ssrf" in lowered or "blocked" in lowered:
        code = "blocked_url"
    elif "unknown" in lowered or "references" in lowered:
        code = "unknown_reference"
    elif "required" in lowered or "non-empty" in lowered:
        code = "required"
    elif "unsupported" in lowered:
        code = "unsupported"
    elif "invalid" in lowered or "must be" in lowered:
        code = "invalid_value"
    else:
        code = "invalid_llm_config"
    return {
        "code": code,
        "path": path,
    }


def _raise_config_version_conflict(
    *,
    expected_version: int,
    current_version: int,
) -> None:
    raise HTTPException(
        409,
        {
            "code": "config_version_conflict",
            "expected_version": expected_version,
            "current_version": current_version,
        },
    )


def _config_changed_sections(
    old_config: dict[str, Any],
    new_config: dict[str, Any],
) -> list[str]:
    changed: list[str] = []
    if old_config.get("providers") != new_config.get("providers"):
        changed.append("providers")
    old_routing = old_config.get("tiers") if "tiers" in old_config else old_config.get("agents")
    new_routing = new_config.get("tiers") if "tiers" in new_config else new_config.get("agents")
    if old_routing != new_routing:
        changed.append("routing")
    for section in ("kb", "media", "search"):
        if old_config.get(section) != new_config.get(section):
            changed.append(section)
    return changed


def _embed_descriptor(
    config: dict[str, Any],
    model_name: str | None,
) -> dict[str, Any] | None:
    if not model_name:
        return None
    kb = config.get("kb")
    models = kb.get("models") if isinstance(kb, dict) else None
    spec = models.get(model_name) if isinstance(models, dict) else None
    if not isinstance(spec, dict):
        return {"name": model_name}
    return {
        "name": model_name,
        "provider": spec.get("provider"),
        "model": spec.get("real_id") or spec.get("model"),
        "dim": spec.get("dim"),
    }


def _preview_embed_signature(
    config: dict[str, Any],
    model_name: str | None,
) -> tuple[Any, ...] | None:
    return _embed_signature(config, model_name)


async def _config_change_impact(
    *,
    request: Request,
    team_id: str,
    old_config: dict[str, Any],
    new_config: dict[str, Any],
    changed_sections: list[str],
) -> dict[str, Any]:
    old_kb_value = old_config.get("kb")
    new_kb_value = new_config.get("kb")
    old_kb = old_kb_value if isinstance(old_kb_value, dict) else {}
    new_kb = new_kb_value if isinstance(new_kb_value, dict) else {}
    old_active = old_kb.get("active")
    new_active = new_kb.get("active")
    old_embed = old_active.get("embed") if isinstance(old_active, dict) else None
    new_embed = new_active.get("embed") if isinstance(new_active, dict) else None
    old_signature = _preview_embed_signature(old_config, old_embed)
    new_signature = _preview_embed_signature(new_config, new_embed)
    embedding_changed = old_embed != new_embed or old_signature != new_signature
    pending = old_kb.get("pending_embed") or {}
    already_pending = bool(
        pending.get("task_id") and pending.get("model") == new_embed and pending.get("state") not in {"failed", "succeeded", "superseded"}
    )
    active_file_count = 0
    if embedding_changed and new_embed and not already_pending:
        files_store = getattr(request.app.state, "kb_files_store", None)
        if files_store is not None and hasattr(
            files_store,
            "list_active_for_team_all_scopes",
        ):
            active_files = await files_store.list_active_for_team_all_scopes(team_id)
            active_file_count = len(active_files)

    return {
        "providers_changed": "providers" in changed_sections,
        "routing_changed": "routing" in changed_sections,
        "kb_changed": "kb" in changed_sections,
        "media_changed": "media" in changed_sections,
        "search_changed": "search" in changed_sections,
        "embedding": {
            "changed": embedding_changed,
            "from": _embed_descriptor(old_config, old_embed),
            "to": _embed_descriptor(new_config, new_embed),
            "already_pending": already_pending,
            "reindex_required": bool(embedding_changed and new_embed and active_file_count and not already_pending),
            "active_files": active_file_count,
        },
    }


async def _stage_embed_reindex_if_needed(
    *,
    request: Request,
    team_id: str,
    old_config: dict[str, Any],
    new_config: dict[str, Any],
    confirm_embed_reindex: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not is_v2_config(new_config):
        return new_config, None
    new_kb = new_config.get("kb") or {}
    new_active = new_kb.get("active") or {}
    new_embed = new_active.get("embed")
    if not new_embed:
        return new_config, None

    old_active = (old_config.get("kb") or {}).get("active") or {}
    old_embed = old_active.get("embed")

    # WS5 — idempotent stage: if a reindex to THIS exact embed model is already
    # staged/running, don't mint a second task. Preserve the in-flight pending
    # (same task_id) + keep serving the old embed, but still apply any other
    # config changes in new_config. Makes a repeated PUT / double-click safe
    # (no orphaned duplicate reindex racing the same target).
    existing_pending = (old_config.get("kb") or {}).get("pending_embed") or {}
    if (
        existing_pending.get("task_id")
        and existing_pending.get("model") == new_embed
        and existing_pending.get("state") not in {"failed", "succeeded", "superseded"}
    ):
        staged = copy.deepcopy(new_config)
        kb = staged.setdefault("kb", {})
        active = kb.setdefault("active", {})
        if old_embed:
            active["embed"] = old_embed
            old_index = (old_config.get("kb") or {}).get("index_version")
            if old_index:
                kb["index_version"] = old_index
            else:
                kb.pop("index_version", None)
        else:
            active.pop("embed", None)
            kb.pop("index_version", None)
        kb["pending_embed"] = copy.deepcopy(existing_pending)
        return staged, None

    old_sig = _embed_signature(old_config, old_embed)
    new_sig = _embed_signature(new_config, new_embed)
    retry_failed = confirm_embed_reindex and existing_pending.get("state") == "failed" and existing_pending.get("model") == new_embed
    if old_sig == new_sig and old_sig is not None and not retry_failed:
        return new_config, None

    files_store = getattr(request.app.state, "kb_files_store", None)
    active_files = []
    if files_store is not None and hasattr(files_store, "list_active_for_team_all_scopes"):
        active_files = await files_store.list_active_for_team_all_scopes(team_id)
    if not active_files:
        activated = copy.deepcopy(new_config)
        kb = activated.setdefault("kb", {})
        if retry_failed:
            kb.pop("pending_embed", None)
        if new_embed and not kb.get("index_version"):
            kb["index_version"] = f"kbidx_{uuid4().hex[:12]}"
        return activated, None

    if not confirm_embed_reindex:
        raise HTTPException(
            409,
            {
                "code": "embed_reindex_required",
                "active_files": len(active_files),
            },
        )

    staged = copy.deepcopy(new_config)
    kb = staged.setdefault("kb", {})
    active = kb.setdefault("active", {})
    target_index_version = f"kbidx_{uuid4().hex[:12]}"
    task_id = f"kb_reindex_{uuid4().hex[:12]}"
    if old_embed:
        active["embed"] = old_embed
        old_index = (old_config.get("kb") or {}).get("index_version")
        if old_index:
            kb["index_version"] = old_index
        else:
            kb.pop("index_version", None)
    else:
        active.pop("embed", None)
        kb.pop("index_version", None)
    pending = {
        "model": new_embed,
        "target_index_version": target_index_version,
        "task_id": task_id,
        "created_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
    }
    kb["pending_embed"] = pending
    return staged, {
        "state": "pending",
        "task_id": task_id,
        "target_index_version": target_index_version,
        "total_files": len(active_files),
    }


def _target_config_for_pending_embed(config: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    target = copy.deepcopy(config)
    kb = target.setdefault("kb", {})
    active = kb.setdefault("active", {})
    active["embed"] = pending["model"]
    kb["index_version"] = pending["target_index_version"]
    return target


async def _persist_reindex_failure(
    *,
    store: UserStore,
    team_id: str,
    task_id: str,
) -> None:
    for _attempt in range(3):
        cfg, version = await store.get_team_llm_config_with_version(team_id)
        pending = (cfg.get("kb") or {}).get("pending_embed") or {}
        if pending.get("task_id") != task_id:
            return
        pending["state"] = "failed"
        pending["error_code"] = "reindex_failed"
        pending.pop("error", None)
        pending["completed_at"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
        updated = await store.update_team_llm_config_if_version(
            team_id,
            cfg,
            expected_version=version,
        )
        if updated:
            return
        await asyncio.sleep(0)
    logger.warning(
        "failed to persist KB reindex failure after OCC retries team_id=%s task_id=%s",
        team_id,
        task_id,
    )


async def _activate_reindexed_embed(
    *,
    request: Request,
    store: UserStore,
    team_id: str,
    task_id: str,
) -> bool:
    for _attempt in range(3):
        cfg, version = await store.get_team_llm_config_with_version(team_id)
        kb = cfg.get("kb") or {}
        pending = kb.get("pending_embed") or {}
        if pending.get("task_id") != task_id:
            return False
        active = kb.setdefault("active", {})
        active["embed"] = pending["model"]
        kb["index_version"] = pending["target_index_version"]
        kb.pop("pending_embed", None)
        kb["last_embed_reindex_completed_at"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
        updated = await store.update_team_llm_config_if_version(
            team_id,
            cfg,
            expected_version=version,
        )
        if updated:
            _invalidate_llm_runtime(request, team_id)
            return True
        await asyncio.sleep(0)
    return False


async def _run_embed_reindex_task(
    *,
    request: Request,
    store: UserStore,
    team_id: str,
    task_id: str,
) -> None:
    statuses = _reindex_statuses(request)
    status = statuses.setdefault(task_id, {"task_id": task_id})
    status.update(
        {
            "team_id": team_id,
            "state": "running",
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "started_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        }
    )
    try:
        cfg, version = await store.get_team_llm_config_with_version(team_id)
        pending = (cfg.get("kb") or {}).get("pending_embed") or {}
        if pending.get("task_id") != task_id:
            status["state"] = "superseded"
            return
        target_config = _target_config_for_pending_embed(cfg, pending)
        status.update(
            {
                "target_index_version": pending.get("target_index_version"),
                "target_model": pending.get("model"),
            }
        )
        runtime_registry = getattr(request.app.state, "team_kb_runtime_registry", None)
        build_runtime = getattr(runtime_registry, "build_runtime_for_config", None)
        if not callable(build_runtime):
            raise RuntimeError("team KB runtime registry is not available")
        team_router = None
        team_router_registry = getattr(request.app.state, "team_router_registry", None)
        get_team_router = getattr(team_router_registry, "get", None)
        if callable(get_team_router):
            team_router = await get_team_router(team_id, target_config, version)
        runtime = build_runtime(target_config, version, router=team_router)
        if runtime is None or runtime.worker is None:
            raise RuntimeError("target embedding runtime is unavailable")
        files_store = getattr(request.app.state, "kb_files_store", None)
        if files_store is None or not hasattr(files_store, "list_active_for_team_all_scopes"):
            raise RuntimeError("KB files store is not available")
        reindex_file = getattr(runtime.worker, "reindex_file_chunks", None)
        if not callable(reindex_file):
            raise RuntimeError("target embedding worker does not support chunk-only reindex")
        processed_versions: dict[str, int] = {}

        async def _one(file: Any) -> None:
            kwargs = {}
            if getattr(file, "active_generation_id", None):
                source_runtime = await runtime_registry.get(team_id, cfg, version)
                kwargs["source_store"] = source_runtime.kb_store
            indexed = await reindex_file(file.file_id, file.version, **kwargs)
            if indexed:
                processed_versions[file.file_id] = file.version

        async def _delta_pass() -> bool:
            active_files = await files_store.list_active_for_team_all_scopes(team_id)
            pending_files = [file for file in active_files if processed_versions.get(file.file_id) != file.version]
            status["total"] = len(active_files)
            status["succeeded"] = len(active_files) - len(pending_files)
            status["processed"] = status["succeeded"]
            if not pending_files:
                return True
            results = await asyncio.gather(
                *(_one(file) for file in pending_files),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, BaseException)]
            status["failed"] = len(failures)
            if failures:
                raise RuntimeError(f"KB reindex failed for {len(failures)} file(s): {failures[0]}")
            return False

        for _round in range(10):
            if await _delta_pass():
                break

        gate = getattr(request.app.state, "team_kb_ingestion_gate", None)
        promotion = getattr(gate, "promotion", None)
        if not callable(promotion):
            raise RuntimeError("team KB ingestion gate is not available")
        async with promotion(team_id):
            for _round in range(10):
                if await _delta_pass():
                    break
            else:
                raise RuntimeError("KB reindex did not quiesce after 10 final delta rounds")
            activated = await _activate_reindexed_embed(
                request=request,
                store=store,
                team_id=team_id,
                task_id=task_id,
            )
        status["state"] = "succeeded" if activated else "superseded"
        status["completed_at"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
    except Exception:
        logger.exception(
            "KB reindex failed team_id=%s task_id=%s",
            team_id,
            task_id,
        )
        status["state"] = "failed"
        status["error_code"] = "reindex_failed"
        status["completed_at"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
        await _persist_reindex_failure(
            store=store,
            team_id=team_id,
            task_id=task_id,
        )
        _invalidate_llm_runtime(request, team_id)


def _start_embed_reindex_task(
    *,
    request: Request,
    store: UserStore,
    team_id: str,
    reindex: dict[str, Any] | None,
) -> None:
    if not reindex or not reindex.get("task_id"):
        return
    statuses = _reindex_statuses(request)
    task_id = str(reindex["task_id"])

    # WS5 — real cancel of the superseded task: a new reindex for this team means any
    # prior in-flight task is now targeting a stale embed model. Cancel it promptly
    # (the store-based supersede check only fires at the task's next config read, so
    # a task mid-gather would otherwise run to completion and race the new index).
    for other_id, st in list(statuses.items()):
        if other_id == task_id or st.get("team_id") != team_id:
            continue
        if st.get("state") in {"failed", "succeeded", "superseded", "cancelled"}:
            continue
        old_task = st.get("task")
        if old_task is not None and not old_task.done():
            old_task.cancel()
            st["state"] = "superseded"
            st["completed_at"] = datetime.now(tz=UTC).isoformat(timespec="seconds")

    statuses[task_id] = {
        "task_id": task_id,
        "team_id": team_id,
        "state": "queued",
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "total": reindex.get("total_files"),
        "target_index_version": reindex.get("target_index_version"),
        "queued_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
    }
    task = asyncio.create_task(
        _run_embed_reindex_task(
            request=request,
            store=store,
            team_id=team_id,
            task_id=task_id,
        ),
        name=f"team_kb_embed_reindex:{team_id}:{task_id}",
    )
    statuses[task_id]["task"] = task


async def resume_pending_embed_reindexes(request: Request, store: UserStore) -> int:
    """Restart team KB embed reindex tasks left pending across gateway restart."""
    list_pending = getattr(store, "list_teams_with_pending_embed_reindex", None)
    if not callable(list_pending):
        return 0
    count = 0
    for team_id, _cfg, _version, pending in await list_pending():
        if pending.get("state") in {"failed", "succeeded", "superseded"}:
            continue
        _start_embed_reindex_task(
            request=request,
            store=store,
            team_id=team_id,
            reindex={
                "task_id": pending.get("task_id"),
                "target_index_version": pending.get("target_index_version"),
                "total_files": pending.get("total_files"),
            },
        )
        count += 1
    return count


@router.post("/api/v1/admin/llm-config/validate")
async def validate_llm_config(
    body: LLMConfigValidateBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """Validate and summarize a team model-config draft without persisting it."""
    old_config, current_version = await store.get_team_llm_config_with_version(caller.team_id)
    if body.expected_version is not None and body.expected_version != current_version:
        _raise_config_version_conflict(
            expected_version=body.expected_version,
            current_version=current_version,
        )

    incoming_config = _preserve_provider_secrets(old_config, body.config)
    issues: list[dict[str, Any]] = []
    if not runtime_parameter_changes_admitted(old_config or {}, incoming_config):
        issues.append(
            {
                "code": "model_runtime_parameters_disabled",
                "path": "providers",
                "message": "Request-parameter editing is disabled. Keep the saved policy or ask the operator to enable it.",
            }
        )
    try:
        validate_v2_config(incoming_config, allow_unassigned_tiers=True)
    except TeamLlmConfigError as error:
        issue = _config_validation_issue(error)
        issue["message"] = (
            str(error)
            if "runtime_parameters" in str(error)
            else await localize_error_message(
                request,
                issue["code"],
            )
        )
        issues.append(issue)

    comparable_old = migrate_legacy_config_to_v2(old_config) if old_config and not is_v2_config(old_config) else old_config
    changed_sections = _config_changed_sections(
        comparable_old,
        incoming_config,
    )
    impact = await _config_change_impact(
        request=request,
        team_id=caller.team_id,
        old_config=comparable_old,
        new_config=incoming_config,
        changed_sections=changed_sections,
    )
    result = {
        "valid": not issues,
        "version": current_version,
        "issues": issues,
        "has_changes": bool(changed_sections),
        "changed_sections": changed_sections,
        "impact": impact,
    }
    if not issues:
        try:
            evaluation = await evaluate_team_llm_readiness(
                db_path=store.db_path,
                team_id=caller.team_id,
                actor_user_id=caller.id,
                current_config=comparable_old,
                draft_config=incoming_config,
                expected_version=current_version,
                enrollment_source="first_v2_read",
            )
            result["readiness"] = readiness_response(
                evaluation,
                team_id=caller.team_id,
                actor_user_id=caller.id,
                expected_version=current_version,
            )
            # Criterion is **blocking**, not may_apply. may_apply = not blocking and not
            # required_acceptances; using it treats awaiting user risk acceptance as invalid configuration.
            # FE Apply requires valid=true, so **any pending acceptance permanently disables the button**,
            # even after checking every box. 116 on 2026-08-15 reproduced this deadlock: unchanged original config
            # returned valid:false + issues:[] (invalid with no reasons).
            #
            # Pending acceptance is the **designed forward path**, not validation failure. Acceptance remains mandatory:
            # FE additionally requires every checkbox; server apply validates every acceptance token (RiskAcceptanceRequired),
            # so relaxing this cannot bypass confirmation.
            gate_ok = not evaluation.decision.blocking
            result["valid"] = bool(result["valid"] and (gate_ok or evaluation.migration_eligible))
        except ProviderIdentityRequiredError as error:
            result["valid"] = False
            result["issues"].append(
                {
                    "code": "provider_identity_required",
                    "path": f"providers.{error.runtime_key}",
                    "message": await localize_error_message(
                        request,
                        "provider_identity_required",
                    ),
                }
            )
        except Exception:
            logger.exception(
                "team LLM V2 readiness failed team_id=%s",
                caller.team_id,
            )
            result["valid"] = False
            result["issues"].append(
                {
                    "code": "llm_readiness_unavailable",
                    "path": "readiness",
                }
            )
    return result


@router.put("/api/v1/admin/llm-config", dependencies=[Depends(require_not_readonly)])
async def update_llm_config(
    body: LLMConfigPutBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """Save full team-scoped v2 LLM/KB model config."""
    old_config, current_version = await store.get_team_llm_config_with_version(caller.team_id)
    configuration_proposal = None
    if body.configuration_proposal_id:
        from bowhead_gateway.configuration_team_models import check_model_proposal

        configuration_proposal = await check_model_proposal(request, caller, body.configuration_proposal_id, body.config, current_version)
    if body.expected_version is not None and body.expected_version != current_version:
        _raise_config_version_conflict(
            expected_version=body.expected_version,
            current_version=current_version,
        )
    incoming_config = _preserve_provider_secrets(old_config, body.config)
    # The retry setting has its own endpoint; a full save that omits it keeps it.
    if "retry" not in incoming_config and isinstance(old_config, dict) and "retry" in old_config:
        incoming_config = {**incoming_config, "retry": old_config["retry"]}
    if not runtime_parameter_changes_admitted(old_config or {}, incoming_config):
        raise HTTPException(409, {"code": "model_runtime_parameters_disabled"})
    try:
        validate_v2_config(incoming_config, allow_unassigned_tiers=True)
    except TeamLlmConfigError as exc:
        raise HTTPException(
            400,
            {"code": "invalid_runtime_parameters", "message": str(exc)} if "runtime_parameters" in str(exc) else {"code": "invalid_llm_config"},
        ) from exc
    config_to_save, reindex = await _stage_embed_reindex_if_needed(
        request=request,
        team_id=caller.team_id,
        old_config=old_config,
        new_config=incoming_config,
        confirm_embed_reindex=body.confirm_embed_reindex,
    )
    _reject_plaintext_full_config(config_to_save)

    v2_apply_result = None
    if body.expected_version is None or body.readiness is None:
        raise HTTPException(
            409,
            {"code": "readiness_review_required"},
        )
    comparable_old = migrate_legacy_config_to_v2(old_config) if old_config and not is_v2_config(old_config) else old_config
    try:
        v2_apply_result = await apply_team_llm_v2(
            db_path=store.db_path,
            team_id=caller.team_id,
            actor_user_id=caller.id,
            current_config_for_readiness=comparable_old,
            config_to_save=config_to_save,
            expected_version=body.expected_version,
            draft_semantic_hash=(body.readiness.draft_semantic_hash),
            acceptance_tokens=(body.readiness.acceptance_tokens),
            migration_acceptance_token=(body.readiness.migration_acceptance_token),
            audit_ip=(request.client.host if request.client else None),
        )
    except TeamLlmApplyError as exc:
        status = (
            409
            if exc.code
            in {
                "config_version_conflict",
                "readiness_review_required",
                "readiness_review_stale",
                "risk_acceptance_required",
                "migration_acceptance_required",
            }
            else 422
        )
        raise HTTPException(status, {"code": exc.code}) from exc
    _invalidate_llm_runtime(request, caller.team_id)
    _start_embed_reindex_task(
        request=request,
        store=store,
        team_id=caller.team_id,
        reindex=reindex,
    )
    if v2_apply_result is None:
        await store.write_admin_audit_log(
            actor_user_id=caller.id,
            team_id=caller.team_id,
            action=AuditAction.TEAM_SETTINGS_CHANGED,
            target_kind="team_llm_config",
            target_id=caller.team_id,
            scope="team",
            payload={
                "sections": sorted(k for k in config_to_save if k != "providers") + (["providers"] if config_to_save.get("providers") else []),
                "schema_version": config_to_save.get("schema_version"),
                "confirm_embed_reindex": (body.confirm_embed_reindex),
                "reindex_state": (reindex or {}).get("state"),
            },
            ip=request.client.host if request.client else None,
        )

    if configuration_proposal is not None:
        from bowhead_gateway.routes.config_proposals import _draft_store

        transition = await _draft_store(request).transition(
            configuration_proposal.id, team_id=caller.team_id, owner_user_id=caller.id, to_status="applied", from_statuses=("proposed",)
        )
        if transition.outcome not in {"ok", "replay"}:
            raise HTTPException(409, {"code": "configuration_receipt_conflict"})

    cfg, version = await store.get_team_llm_config_with_version(caller.team_id)
    return {
        "ok": True,
        "version": version,
        "config": redact_config(cfg),
        "providers": compatibility_provider_list(cfg),
        "reindex": reindex,
        "apply_snapshot_id": (v2_apply_result.snapshot_id if v2_apply_result is not None else None),
        # The page replaces its state with this response; without the features it would drop to the
        # pre-V2 view after Apply ("editing draft", "no assignments yet") until a reload.
        "features": _llm_config_features(request),
    }


class LLMRetryPutBody(BaseModel):
    max_retries: int
    expected_version: int


@router.put("/api/v1/admin/llm-config/retry", dependencies=[Depends(require_not_readonly)])
async def update_llm_retry(
    body: LLMRetryPutBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """Save the team's single model retry setting.

    It changes no model assignment, so it needs no readiness review; the
    version check still rejects a concurrent configuration change.
    """
    config, current_version = await store.get_team_llm_config_with_version(caller.team_id)
    if body.expected_version != current_version:
        _raise_config_version_conflict(expected_version=body.expected_version, current_version=current_version)
    if not is_v2_config(config):
        raise HTTPException(
            409,
            {"code": "llm_config_not_configured", "message": "Save the model configuration on this page once before changing retries."},
        )
    updated = {**config, "retry": {"max_retries": body.max_retries}}
    try:
        validate_v2_config(updated, allow_unassigned_tiers=True)
    except TeamLlmConfigError as exc:
        raise HTTPException(400, {"code": "invalid_llm_retry", "message": str(exc)}) from exc
    if not await store.update_team_llm_config_if_version(caller.team_id, updated, expected_version=current_version):
        _raise_config_version_conflict(expected_version=body.expected_version, current_version=current_version + 1)
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.TEAM_SETTINGS_CHANGED,
        target_kind="team_llm_config",
        target_id=caller.team_id,
        scope="team",
        payload={"sections": ["retry"], "max_retries": body.max_retries, "previous": (config.get("retry") or {}).get("max_retries")},
        ip=request.client.host if request.client else None,
    )
    _invalidate_llm_runtime(request, caller.team_id)
    # The version this write produced, not a later one the page has not loaded.
    return {"ok": True, "version": current_version + 1, "retry": updated["retry"]}


@router.get("/api/v1/admin/llm-config/kb/reindex/{task_id}")
async def get_kb_reindex_status(
    task_id: str,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    status = _reindex_statuses(request).get(task_id)
    if status is not None and status.get("team_id") == caller.team_id:
        payload = {key: value for key, value in status.items() if key not in {"task", "team_id", "error", "error_code"}}
        error_code = status.get("error_code")
        if isinstance(error_code, str):
            payload["error"] = {
                "code": error_code,
                "message": await localize_error_message(
                    request,
                    error_code,
                ),
            }
        return payload
    cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    pending = (cfg.get("kb") or {}).get("pending_embed") or {}
    if pending.get("task_id") != task_id:
        raise HTTPException(404, {"code": "reindex_task_not_found"})
    payload = {
        "task_id": task_id,
        "state": pending.get("state", "pending"),
        "processed": None,
        "succeeded": None,
        "failed": None,
        "total": None,
        "target_index_version": pending.get("target_index_version"),
    }
    error_code = pending.get("error_code")
    if isinstance(error_code, str):
        payload["error"] = {
            "code": error_code,
            "message": await localize_error_message(
                request,
                error_code,
            ),
        }
    return payload


@router.post("/api/v1/admin/llm-config/providers", dependencies=[Depends(require_not_readonly)])
async def add_llm_provider(
    body: LLMProviderAddBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),  # WS8/D6: model config = creator/team_admin only
    store: UserStore = Depends(_store),
) -> dict:
    """Add or replace provider in team's llm_config (encrypted via Fernet)."""
    # Pre-audit
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.LLM_PROVIDER_ADD,
        target_kind="team_llm_config",
        target_id=caller.team_id,
        scope="team",
        payload={
            "provider_name": body.provider_name,
            "role": body.role,
            "fingerprint": _key_fingerprint(body.api_key),
        },
        ip=request.client.host if request.client else None,
    )

    # Read existing config, append/replace provider
    try:
        cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    except Exception:
        cfg = {"providers": []}
    _upsert_provider_key(cfg, body.provider_name, body.api_key, body.role)
    _reject_plaintext_full_config(cfg)

    await store.update_team_llm_config(caller.team_id, cfg)
    _invalidate_llm_runtime(request, caller.team_id)
    return {"ok": True, "provider_name": body.provider_name, "fingerprint": _key_fingerprint(body.api_key)}


@router.post("/api/v1/admin/llm-config/providers/verify", dependencies=[Depends(require_not_readonly)])
async def verify_llm_provider(
    body: LLMProviderVerifyBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),  # WS8/D6: model config = creator/team_admin only
    store: UserStore = Depends(_store),
) -> dict:
    """WS4 test-connection: probe a provider BY VALUE before it is saved.

    Never persists. The probe applies the deployment-mode outbound URL policy:
    local deployments may use enterprise intranet services; SaaS accepts only
    public HTTPS destinations. openai → GET /models, anthropic → POST
    /v1/messages max_tokens:1.
    """

    # OPS audit (best-effort). Fingerprint only — the plaintext key is never logged.
    try:
        await store.write_admin_audit_log(
            actor_user_id=caller.id,
            team_id=caller.team_id,
            action=AuditAction.LLM_PROVIDER_VERIFY,
            target_kind="team_llm_config",
            target_id=caller.team_id,
            scope="team",
            payload={
                "provider_type": body.provider_type,
                "base_url": body.base_url,
                "fingerprint": _key_fingerprint(body.api_key),
            },
            ip=request.client.host if request.client else None,
        )
    except Exception:
        logger.warning("llm_provider_verify audit write failed", exc_info=True)

    return await _verify_provider_request(
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        models=body.models,
        anthropic_version=body.anthropic_version or "2023-06-01",
        messages_path=body.messages_path,
    )


@router.post("/api/v1/admin/llm-config/providers/verify-capability", dependencies=[Depends(require_not_readonly)])
async def verify_llm_provider_capability(
    body: LLMProviderCapabilityVerifyBody,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
) -> dict[str, Any]:
    """Probe exactly one declared capability after the connection check."""
    del caller
    if not body.model:
        raise HTTPException(422, {"code": "model_required"})
    return await _verify_provider_capability_request(
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        capability=body.capability,
        capability_protocol=body.capability_protocol,
        anthropic_version=body.anthropic_version or "2023-06-01",
        messages_path=body.messages_path,
    )


@router.post("/api/v1/admin/llm-config/providers/discover-models", dependencies=[Depends(require_not_readonly)])
async def discover_llm_provider_models(body: LLMProviderVerifyBody, caller: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    """Optional ``/models`` discovery; never gates capability verification."""
    del caller
    return await _discover_provider_models_request(base_url=body.base_url, api_key=body.api_key)


@router.post(
    "/api/v1/admin/llm-config/providers/verify-draft",
    dependencies=[Depends(require_not_readonly)],
)
async def verify_draft_llm_provider(
    body: LLMProviderDraftVerifyBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """Probe draft connection fields with a new key or the saved key.

    The candidate is never persisted. Omitting ``api_key`` keeps the secret
    server-side while still testing edits to base URL, protocol, path, or model.
    """
    cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    if cfg and not is_v2_config(cfg):
        cfg = migrate_legacy_config_to_v2(cfg)
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    saved = providers.get(body.provider_name) if isinstance(providers, dict) else None
    if not isinstance(saved, dict):
        raise HTTPException(404, {"code": "provider_not_configured"})

    saved_key = saved.get("api_key") or saved.get("secret_key")
    api_key = body.api_key or saved_key
    if not isinstance(api_key, str) or not api_key:
        raise HTTPException(
            409,
            {
                "code": "provider_not_testable",
                "missing_fields": ["api_key"],
            },
        )

    secret_source = "draft" if body.api_key else "saved"
    try:
        await store.write_admin_audit_log(
            actor_user_id=caller.id,
            team_id=caller.team_id,
            action=AuditAction.LLM_PROVIDER_VERIFY,
            target_kind="team_llm_config",
            target_id=caller.team_id,
            scope="team",
            payload={
                "provider_name": body.provider_name,
                "provider_type": body.provider_type,
                "base_url": body.base_url,
                "fingerprint": _key_fingerprint(api_key),
                "secret_source": secret_source,
            },
            ip=request.client.host if request.client else None,
        )
    except Exception:
        logger.warning("draft llm_provider_verify audit write failed", exc_info=True)

    return await _verify_provider_request(
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=api_key,
        model=body.model,
        models=body.models,
        anthropic_version=body.anthropic_version or "2023-06-01",
        messages_path=body.messages_path,
    )


@router.post(
    "/api/v1/admin/llm-config/providers/verify-draft-capability",
    dependencies=[Depends(require_not_readonly)],
)
async def verify_draft_llm_provider_capability(
    body: LLMProviderDraftCapabilityVerifyBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Probe one draft capability while keeping an omitted saved secret server-side."""
    if not body.model:
        raise HTTPException(422, {"code": "model_required"})
    cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    saved = providers.get(body.provider_name) if isinstance(providers, dict) else None
    saved_key = saved.get("api_key") or saved.get("secret_key") if isinstance(saved, dict) else None
    api_key = body.api_key or saved_key
    if not isinstance(api_key, str) or not api_key:
        raise HTTPException(409, {"code": "provider_not_testable", "missing_fields": ["api_key"]})
    return await _verify_provider_capability_request(
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=api_key,
        model=body.model,
        capability=body.capability,
        capability_protocol=body.capability_protocol,
        anthropic_version=body.anthropic_version or "2023-06-01",
        messages_path=body.messages_path,
    )


@router.post("/api/v1/admin/llm-config/providers/discover-draft-models", dependencies=[Depends(require_not_readonly)])
async def discover_draft_llm_provider_models(
    body: LLMProviderDraftVerifyBody, caller: User = Depends(_REQUIRE_TEAM_ADMIN), store: UserStore = Depends(_store)
) -> dict[str, Any]:
    cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    saved = (cfg.get("providers") or {}).get(body.provider_name) if isinstance(cfg, dict) else None
    api_key = body.api_key or (saved.get("api_key") if isinstance(saved, dict) else None)
    if not isinstance(api_key, str) or not api_key:
        raise HTTPException(409, {"code": "provider_not_testable", "missing_fields": ["api_key"]})
    return await _discover_provider_models_request(base_url=body.base_url, api_key=api_key)


@router.post(
    "/api/v1/admin/llm-config/providers/verify-saved",
    dependencies=[Depends(require_not_readonly)],
)
async def verify_saved_llm_provider(
    body: LLMProviderNameBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    """Probe a saved provider without returning its stored secret to the client."""
    cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    if cfg and not is_v2_config(cfg):
        cfg = migrate_legacy_config_to_v2(cfg)
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    spec = providers.get(body.provider_name) if isinstance(providers, dict) else None
    if not isinstance(spec, dict):
        raise HTTPException(404, {"code": "provider_not_configured"})

    provider_type = spec.get("type")
    base_url = spec.get("base_url")
    api_key = spec.get("api_key") or spec.get("secret_key")
    missing_fields = [
        name
        for name, value in (
            ("type", provider_type),
            ("base_url", base_url),
            ("api_key", api_key),
        )
        if not isinstance(value, str) or not value
    ]
    if missing_fields:
        raise HTTPException(
            409,
            {
                "code": "provider_not_testable",
                "missing_fields": missing_fields,
            },
        )
    if provider_type not in {"openai_compatible", "anthropic_compatible"}:
        raise HTTPException(409, {"code": "provider_type_unsupported"})

    model = _first_model_id(spec) if provider_type == "anthropic_compatible" else None
    if provider_type == "anthropic_compatible" and not model:
        raise HTTPException(
            409,
            {
                "code": "provider_not_testable",
                "missing_fields": ["model"],
            },
        )

    try:
        await store.write_admin_audit_log(
            actor_user_id=caller.id,
            team_id=caller.team_id,
            action=AuditAction.LLM_PROVIDER_VERIFY,
            target_kind="team_llm_config",
            target_id=caller.team_id,
            scope="team",
            payload={
                "provider_name": body.provider_name,
                "provider_type": provider_type,
                "base_url": base_url,
                "fingerprint": _key_fingerprint(str(api_key)),
                "saved_secret": True,
            },
            ip=request.client.host if request.client else None,
        )
    except Exception:
        logger.warning("saved llm_provider_verify audit write failed", exc_info=True)

    stored_models = spec.get("models")
    models = [item for item in stored_models if isinstance(item, dict)] if isinstance(stored_models, list) else []
    return await _verify_provider_request(
        provider_type=str(provider_type),
        base_url=str(base_url),
        api_key=str(api_key),
        model=model,
        models=models,
        anthropic_version=str(spec.get("anthropic_version") or "2023-06-01"),
        messages_path=spec.get("messages_path"),
    )


@router.post("/api/v1/admin/llm-config/rotate", dependencies=[Depends(require_not_readonly)])
async def rotate_llm_key(
    body: LLMRotateBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),  # WS8/D6: model config = creator/team_admin only
    store: UserStore = Depends(_store),
) -> dict:
    """G7 rotate key with audit-before-verify (P0-S-2) + force flag (P0-O-6)."""
    new_fingerprint = _key_fingerprint(body.new_api_key)

    # P0-S-2: audit BEFORE verify-ping (so attacker key-probing leaves trace)
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.LLM_CONFIG_ROTATE_ATTEMPT,
        target_kind="team_llm_config",
        target_id=caller.team_id,
        scope="team",
        payload={
            "provider_name": body.provider_name,
            "new_fingerprint": new_fingerprint,
            "force": body.force,
            "verify_started": True,
        },
        ip=request.client.host if request.client else None,
    )

    # Read config first — the verify-ping needs the provider's base_url/type, and
    # a missing provider must 404 regardless of the flag.
    try:
        cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    except Exception:
        cfg = {"providers": []}

    # P0-O-6: force=true skips verify-ping. Otherwise probe the provider with the
    # NEW key through the SSRF-guarded client (WS4). Verification is ADVISORY —
    # it only sets verified_by_ping; a failed/undeterminable ping never blocks the
    # rotation (matches audit-before-verify; legacy providers have no base_url).
    verified = False
    enforce_ping = os.environ.get("BOWHEAD_LLM_ROTATE_ENFORCE_PING", "1") == "1"
    if not body.force and enforce_ping:
        verified = await _ping_rotated_provider(cfg, body.provider_name, body.new_api_key)

    # Apply rotation
    if not _rotate_provider_key(cfg, body.provider_name, body.new_api_key):
        raise HTTPException(404, {"code": "provider_not_configured"})
    _reject_plaintext_full_config(cfg)
    await store.update_team_llm_config(caller.team_id, cfg)
    _invalidate_llm_runtime(request, caller.team_id)

    # Post-success audit (critical D-3=B tier)
    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.LLM_CONFIG_ROTATE,
        target_kind="team_llm_config",
        target_id=caller.team_id,
        scope="team",
        payload={
            "provider_name": body.provider_name,
            "new_fingerprint": new_fingerprint,
            "verified_by_ping": verified,
            "force": body.force,
        },
        ip=request.client.host if request.client else None,
    )

    return {
        "ok": True,
        "provider_name": body.provider_name,
        "new_fingerprint": new_fingerprint,
        "verified_by_ping": verified,
    }


@router.delete("/api/v1/admin/llm-config/providers", dependencies=[Depends(require_not_readonly)])
async def delete_llm_provider(
    body: LLMProviderNameBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),  # WS8/D6: model config = creator/team_admin only
    store: UserStore = Depends(_store),
) -> dict:
    cfg = await store.get_team_llm_config_decrypted(caller.team_id)
    if not _delete_provider_key(cfg, body.provider_name):
        raise HTTPException(404, {"code": "provider_not_configured"})
    _reject_plaintext_full_config(cfg)
    await store.update_team_llm_config(caller.team_id, cfg)
    _invalidate_llm_runtime(request, caller.team_id)

    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.LLM_PROVIDER_DELETE,
        target_kind="team_llm_config",
        target_id=caller.team_id,
        scope="team",
        payload={"provider_name": body.provider_name},
        ip=request.client.host if request.client else None,
    )
    return {"ok": True}


@router.put("/api/v1/admin/credits/quota", dependencies=[Depends(require_not_readonly)])
async def change_credit_quota(
    body: CreditQuotaBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    team = await store.get_team(caller.team_id)
    if not team:
        raise HTTPException(404, {"code": "team_not_found"})
    old_quota = team.team_credit_quota
    old_balance = team.team_credit_balance
    used_credits = max(0, old_quota - old_balance)
    new_balance = max(0, min(body.new_quota, body.new_quota - used_credits))

    # Preserve consumed credits: raising quota adds usable team pool, lowering quota caps remaining pool.
    async with aiosqlite.connect(store.db_path) as db:
        cur = await db.execute(
            "UPDATE teams SET team_credit_quota = ?, team_credit_balance = ? WHERE id = ?",
            (body.new_quota, new_balance, caller.team_id),
        )
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, {"code": "team_not_found"})

    await store.write_admin_audit_log(
        actor_user_id=caller.id,
        team_id=caller.team_id,
        action=AuditAction.CREDIT_QUOTA_CHANGE,
        target_kind="team",
        target_id=caller.team_id,
        scope="team",
        payload={"old_quota": old_quota, "new_quota": body.new_quota, "old_balance": old_balance, "new_balance": new_balance, "reason": body.reason},
        ip=request.client.host if request.client else None,
    )
    try:
        acted_result = await store.mark_credit_attention_acted(
            team_id=caller.team_id,
            actor_user_id=caller.id,
            action_type="increase_team_quota",
            policy_keys=["team_pool_critical", "team_pool_warning"],
        )
        await _track_credit_attention_acted(
            request,
            store,
            team_id=caller.team_id,
            actor_user_id=caller.id,
            action_type="increase_team_quota",
            result=acted_result,
        )
    except Exception as exc:
        logger.warning(
            "credit_attention.mark_acted_failed",
            extra={"team_id": caller.team_id, "action_type": "increase_team_quota", "err": str(exc)},
        )
    await cache_invalidate_team(caller.team_id)

    return {
        "ok": True,
        "old_quota": old_quota,
        "new_quota": body.new_quota,
        "old_balance": old_balance,
        "new_balance": new_balance,
    }


# ─── §7.1 entitlement-field setters — all mutate caller.team_id (no path param → no cross-team hole),
#     atomic UPDATE+audit via audit_critical_action, blocked in local (license-managed) ───────────────


@router.post("/api/v1/admin/team/tier-credits", dependencies=[Depends(require_not_readonly)])
async def set_team_tier_credits(
    body: TeamTierCreditsBody,
    request: Request,
    caller: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict:
    _reject_if_license_managed(request)
    team = await store.get_team(caller.team_id)
    if not team:
        raise HTTPException(404, {"code": "team_not_found"})
    rc = await store.audit_critical_action(
        action=AuditAction.TEAM_TIER_CREDITS_CHANGED,
        actor_user_id=caller.id,
        team_id=caller.team_id,
        scope="team",
        payload={"old": team.tier_credits, "new": body.tier_credits, "reason": body.reason},
        main_op_sql="UPDATE teams SET tier_credits_json = ? WHERE id = ?",
        main_op_params=(json.dumps(body.tier_credits), caller.team_id),
        target_kind="team",
        target_id=caller.team_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    if rc == 0:
        raise HTTPException(404, {"code": "team_not_found"})
    return {"ok": True, "old_tier_credits": team.tier_credits, "new_tier_credits": body.tier_credits}


# ─── §6.5 on-prem license upload + activation ───────────────────────────────────────────────────────
