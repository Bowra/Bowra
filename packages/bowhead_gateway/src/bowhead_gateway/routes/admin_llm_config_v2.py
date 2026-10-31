"""Flag-gated V2 team LLM verification control-plane routes."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from bowhead_core.audit_actions import AuditAction
from bowhead_core.team_llm_batch_store import (
    IdempotencyConflict,
    PlanExpired,
    PlanNotFound,
    TeamLlmBatchStore,
    TeamLlmBatchStoreError,
    VerificationPlanItem,
)
from bowhead_core.team_llm_config import (
    TeamLlmConfigError,
    validate_provider_draft,
)
from bowhead_core.team_llm_control_token import (
    ControlTokenError,
    issue_control_token,
    verify_control_token,
)
from bowhead_core.team_llm_evidence import (
    TeamLlmSignatureKeyError,
    canonical_json,
    compute_config_signature,
)
from bowhead_core.team_llm_flags import model_runtime_parameters_enabled, runtime_parameter_changes_admitted
from bowhead_core.team_llm_verification_store import (
    ProviderIdentityConflict,
    TeamLlmVerificationStore,
)
from bowhead_router.context_policy import MAX_SAFE_TOKEN_INTEGER, parse_context_policy
from bowhead_router.outbound import learned_adaptations
from bowhead_router.probe import NotTestable
from bowhead_router.transport import is_sensitive_query_name
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bowhead_gateway._entitlement_gate import require_not_readonly
from bowhead_gateway.routes.auth import _require_team_role_dep, _store
from bowhead_router import (
    CompatibleProbeRegistry,
    build_provider_transport_plan,
    discover_provider_model_candidates,
)

if TYPE_CHECKING:
    from bowhead_core.user_store import User, UserStore

router = APIRouter(prefix="/api/v1/admin/llm-config")
logger = logging.getLogger(__name__)

_REQUIRE_TEAM_ADMIN = _require_team_role_dep("owner", "team_admin")
_CAPABILITIES = {"chat", "tool", "vision", "embed", "rerank"}


class V2ContextPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        True,
        strict=True,
        description="Automatically summarize older context when this model needs room. Defaults to on. When off, oversized requests omit older completed context without a summary; saved conversation history stays intact. Required input must still fit.",
    )
    trigger_input_tokens: int | None = Field(
        None,
        gt=0,
        le=MAX_SAFE_TOKEN_INTEGER,
        strict=True,
        description="Optional earlier input-token trigger. Null uses the automatic safe threshold; a saved value cannot raise that threshold. Must exceed the configured recent-token target.",
        json_schema_extra={"x-bowhead-field": {"unit": "tokens"}},
    )
    keep_recent_tokens: int | None = Field(
        None,
        gt=0,
        le=MAX_SAFE_TOKEN_INTEGER,
        strict=True,
        description="Target tokens of recent original content after compaction. Null selects automatically. Required original evidence and complete tool pairs take precedence; must be below the configured trigger.",
        json_schema_extra={"x-bowhead-field": {"unit": "tokens"}},
    )

    @model_validator(mode="after")
    def validate_policy(self) -> V2ContextPolicy:
        parse_context_policy(self.model_dump())
        return self


class V2ModelDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Exact model identifier accepted by the provider. Surrounding whitespace is removed; an empty identifier is invalid.",
    )
    capabilities: list[str] = Field(
        ...,
        min_length=1,
        max_length=5,
        description="Capabilities this model supports. Only declare capabilities the provider can actually serve; each capability appears once.",
    )
    declaration_origin: str = Field("v2_explicit", max_length=40)
    public_name: str | None = Field(
        None,
        max_length=120,
        description="Optional public model alias. It does not replace the provider model identifier or an Azure deployment name.",
    )
    deployment: str | None = Field(
        None, max_length=200, description="Azure deployment name for this model. It must identify a deployment in the selected connection."
    )
    capability_transports: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from declared model capabilities to existing transport templates. Authentication and template editing stay in the connection editor.",
    )
    request_body_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Static request-body overrides by capability. Protected request fields and credentials are forbidden; original protocol and collision validation applies.",
    )
    outbound_params: dict[str, dict[str, Any]] | None = Field(
        None,
        description="Model-level request parameter policy. Unset parameters inherit the connection policy; runtime parameters may further specialize it.",
    )
    context_window: int | None = Field(
        None,
        gt=0,
        description="Declared context capacity in tokens. Catalog suggestions are estimates until explicitly saved; an absent declaration stays unset.",
        json_schema_extra={"x-bowhead-field": {"unit": "tokens"}},
    )
    max_output_tokens: int | None = Field(
        None,
        gt=0,
        strict=True,
        description="Declared maximum output budget in tokens. An absent declaration inherits the connection budget; thinking and request limits also apply.",
        json_schema_extra={"x-bowhead-field": {"unit": "tokens"}},
    )
    context_policy: V2ContextPolicy | None = Field(
        None,
        description="Model context preferences. Omission or reset enables automatic compaction with automatic token targets; explicit off and saved numeric preferences survive unrelated edits.",
    )
    runtime_parameters: dict[str, Any] | None = Field(
        None,
        description="Versioned model request policies for declared Chat, Tool and Vision capabilities. Select a matching profile before setting thinking options.",
    )
    # Model-level API surface: Responses API batch 0. None/omitted = chat_completions.
    # Must travel in v2 drafts; otherwise a switched model's verification evidence stays stale under the new signature
    # (tech-design §8.1; unset is not persisted with model_dump(exclude_none=True)).
    api_surface: Literal["chat_completions", "responses"] | None = Field(
        None,
        description="OpenAI-compatible request API: Chat Completions or Responses. Changing it requires the original model verification and review.",
    )

    @field_validator("id")
    @classmethod
    def _normalize_model_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model id must be non-empty")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        if any(capability not in _CAPABILITIES for capability in value):
            raise ValueError("unsupported model capability")
        if len(set(value)) != len(value):
            raise ValueError("model capabilities must be unique")
        return value


class V2ProviderDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str | None = Field(
        None,
        pattern=r"^llmp_[0-9a-f]{32}$",
    )
    runtime_key: str = Field(
        ...,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    display_name: str | None = Field(
        None, max_length=120, description="Name shown for this Team connection. Renaming it does not change the saved connection identity."
    )
    type: str = Field(
        ...,
        pattern=r"^(openai_compatible|anthropic_compatible|azure_openai|custom_http)$",
        description="Connection protocol used to send model requests. It determines which address, authentication and model options apply.",
    )
    base_url: str = Field(
        "",
        max_length=500,
        description="Base address of the model service. Use the service address without credentials or query parameters; deployment URL rules apply.",
    )
    endpoint: str | None = Field(
        None, max_length=500, description="Azure service endpoint used by this connection. The deployment name is selected separately for each model."
    )
    api_version: str | None = Field(
        None, max_length=40, description="Azure API version sent with requests. Use a version supported by the configured service."
    )
    api_key: str | None = Field(None, max_length=500)
    auth: dict[str, Any] = Field(default_factory=dict)
    transports: dict[str, Any] = Field(default_factory=dict)
    models: list[V2ModelDeclaration] = Field(
        default_factory=list,
        max_length=1000,
        description="Models declared on this connection. Preserve models and capabilities that are not part of the requested change.",
    )
    outbound_params: dict[str, dict[str, Any]] | None = Field(
        None, description="Connection-level request parameter defaults inherited by its models. Model policies can override these defaults."
    )
    context_window: int | None = Field(
        None,
        gt=0,
        description="Declared connection context capacity in tokens. Leave unset when unknown; model-specific declarations take precedence.",
        json_schema_extra={"x-bowhead-field": {"unit": "tokens"}},
    )
    max_output_tokens: int | None = Field(
        None,
        gt=0,
        strict=True,
        description="Connection-level output token budget inherited by models without their own declaration. This is separate from context capacity.",
        json_schema_extra={"x-bowhead-field": {"unit": "tokens"}},
    )

    @model_validator(mode="after")
    def _api_surface_requires_openai_compatible(self) -> V2ProviderDraft:
        # Only openai_compatible consumes the surface (review R1-P2-8). Other types would declare responses
        # but silently run chat, with falsely verified evidence under the new signature.
        if self.type != "openai_compatible":
            offender = next((m.id for m in self.models if m.api_surface == "responses"), None)
            if offender is not None:
                raise ValueError(f"models[{offender}].api_surface is only supported for openai_compatible providers")
        return self

    default_headers: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = Field(
        60,
        ge=1,
        le=300,
        description="Maximum time in seconds for a request through this connection. The remaining turn and model-call budgets may end it earlier.",
        json_schema_extra={"x-bowhead-field": {"unit": "seconds"}},
    )
    messages_path: str | None = Field(
        None, max_length=200, description="Custom Messages API path for an Anthropic-compatible connection. An absent path uses the adapter default."
    )
    anthropic_version: str | None = Field(
        None,
        max_length=40,
        description="Anthropic protocol version header used by the connection. Preserve it unless the service requires a different version.",
    )

    @field_validator("api_key")
    @classmethod
    def _reject_blank_secret(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("api_key must be omitted or non-empty")
        return value

    @field_validator("default_headers")
    @classmethod
    def _bound_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("too many default headers")
        if any(not key.strip() or len(key) > 80 or not isinstance(item, str) or len(item) > 500 for key, item in value.items()):
            raise ValueError("invalid default header")
        return value

    def provider_config(
        self,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "type": self.type,
            "api_key": self.api_key if api_key is None else api_key,
            "default_headers": dict(self.default_headers),
            "timeout_sec": self.timeout_sec,
        }
        if self.type == "azure_openai":
            config["endpoint"] = str(self.endpoint or self.base_url).strip()
            config["api_version"] = self.api_version
            config["auth"] = dict(self.auth)
        else:
            config["base_url"] = self.base_url.strip()
        if self.type == "custom_http":
            config["auth"] = dict(self.auth)
            config["transports"] = self.transports
            if config["auth"].get("type") == "none":
                config.pop("api_key", None)
        if self.messages_path is not None:
            config["messages_path"] = self.messages_path
        if self.anthropic_version is not None:
            config["anthropic_version"] = self.anthropic_version
        if self.models:
            config["models"] = [declaration.model_dump(exclude_none=True) for declaration in self.models]
        if self.outbound_params is not None:
            config["outbound_params"] = self.outbound_params
        if self.context_window is not None:
            config["context_window"] = self.context_window
        if self.max_output_tokens is not None:
            config["max_output_tokens"] = self.max_output_tokens
        return config


class V2ProviderPrecheckBody(V2ProviderDraft):
    reserve_provider_id: bool = True


class V2VerificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(..., pattern=r"^llmp_[0-9a-f]{32}$")
    model_id: str = Field(..., min_length=1, max_length=200)
    capability: str

    @field_validator("model_id")
    @classmethod
    def _normalize_model_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model id must be non-empty")
        return normalized

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, value: str) -> str:
        if value not in _CAPABILITIES:
            raise ValueError("unsupported model capability")
        return value


class ParameterPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_config_version: int = Field(..., ge=0)
    provider_draft: V2ProviderDraft
    model_id: str = Field(..., min_length=1, max_length=200)
    capability: Literal["chat", "tool", "vision"]


@router.post("/parameter-preview", dependencies=[Depends(require_not_readonly)])
async def preview_runtime_parameters(
    body: ParameterPreviewBody,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Compile an authorized draft; no identity reservation, writes or HTTP."""
    if not model_runtime_parameters_enabled():
        raise HTTPException(404, {"code": "model_runtime_parameters_disabled"})
    from bowhead_router.parameter_preview import parameter_preview
    from bowhead_router.runtime_parameters import RuntimeParametersError, model_spec

    saved, version = await store.get_team_llm_config_with_version(user.team_id)
    if version != body.base_config_version:
        raise HTTPException(409, {"code": "config_version_conflict", "current_version": version})
    provider = body.provider_draft.provider_config(api_key="synthetic-preview-key")
    saved_providers = saved.get("providers")
    baseline = saved_providers.get(body.provider_draft.runtime_key) if isinstance(saved_providers, dict) else None
    for key in ("outbound_params", "context_window", "max_output_tokens"):
        if isinstance(baseline, dict) and key not in provider and key in baseline:
            provider[key] = baseline[key]
    model = model_spec(provider, body.model_id)
    if body.capability not in (model.get("capabilities") or []):
        raise HTTPException(422, {"code": "undeclared_capability"})
    try:
        validate_provider_draft(body.provider_draft.runtime_key, provider)
        preview = parameter_preview(provider, body.model_id, body.capability)
    except (TeamLlmConfigError, RuntimeParametersError) as exc:
        # Runtime-policy errors contain only bounded field names and prescribed
        # recovery text. Other validation errors may contain secret URLs.
        message = (
            str(exc)
            if isinstance(exc, RuntimeParametersError) or "runtime_parameters" in str(exc)
            else "Check the provider address, transport and parameter field types."
        )
        raise HTTPException(422, {"code": "invalid_runtime_parameters", "message": message}) from exc
    preview["base_config_version"] = version
    preview["state"] = "draft"
    preview["transport"] = {"adapter": provider["type"], "api_surface": model.get("api_surface", "chat_completions")}
    return preview


class V2VerificationPlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_config_version: int = Field(..., ge=0)
    provider_drafts: list[V2ProviderDraft] = Field(
        ...,
        min_length=1,
        max_length=100,
    )
    items: list[V2VerificationItem] = Field(
        ...,
        min_length=1,
        max_length=100,
    )


class V2CreateBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(..., pattern=r"^lvplan_[0-9a-f]{32}$")
    confirmation_token: str = Field(..., min_length=20, max_length=10000)
    provider_drafts: list[V2ProviderDraft] = Field(
        ...,
        min_length=1,
        max_length=100,
    )


def require_model_discovery_v2() -> None:
    """Fail closed without revealing a disabled control plane."""


def _require_provider_type_enabled(provider_type: str) -> None:
    pass


def _verification_store(store: UserStore) -> TeamLlmVerificationStore:
    return TeamLlmVerificationStore(store.db_path)


def _batch_store(store: UserStore) -> TeamLlmBatchStore:
    return TeamLlmBatchStore(store.db_path)


def _safe_url(request_url: str) -> str:
    parsed = urlsplit(request_url)
    query = [
        (
            key,
            "***" if is_sensitive_query_name(key) else value,
        )
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query, doseq=True),
            "",
        )
    )


def _error(
    status_code: int,
    code: str,
    *,
    http_status: int | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {
        "code": code,
        "message_key": f"settings.models.errors.{code}",
    }
    if http_status is not None:
        detail["http_status"] = http_status
    return HTTPException(status_code=status_code, detail=detail)


async def _resolve_identity(
    *,
    user: User,
    store: UserStore,
    draft: V2ProviderDraft,
    reserve_new: bool,
) -> str | None:
    verification_store = _verification_store(store)
    if draft.provider_id:
        identity = await verification_store.get_provider_identity(
            team_id=user.team_id,
            provider_id=draft.provider_id,
        )
        if identity is None or identity.state == "deleted" or identity.runtime_key != draft.runtime_key:
            raise _error(404, "provider_identity_not_found")
        return identity.provider_id

    saved, _version = await store.get_team_llm_config_with_version(user.team_id)
    if draft.runtime_key in (saved.get("providers") or {}):
        try:
            identity = await verification_store.ensure_legacy_provider_identity(
                team_id=user.team_id,
                runtime_key=draft.runtime_key,
            )
        except TeamLlmSignatureKeyError as exc:
            raise _error(503, "signature_key_unavailable") from exc
        return identity.provider_id
    if not reserve_new:
        return None
    try:
        identity = await verification_store.reserve_provider_identity(
            team_id=user.team_id,
            runtime_key=draft.runtime_key,
            actor_user_id=user.id,
        )
    except ProviderIdentityConflict as exc:
        raise _error(409, exc.code) from exc
    return identity.provider_id


async def _materialize_provider_drafts(
    *,
    user: User,
    store: UserStore,
    drafts: list[V2ProviderDraft],
) -> dict[str, dict[str, Any]]:
    saved, _version = await store.get_team_llm_config_with_version(user.team_id)
    saved_providers = saved.get("providers") or {}
    resolved: dict[str, dict[str, Any]] = {}
    runtime_keys: set[str] = set()
    for draft in drafts:
        _require_provider_type_enabled(draft.type)
        if draft.runtime_key in runtime_keys:
            raise _error(422, "duplicate_provider_draft")
        runtime_keys.add(draft.runtime_key)
        provider_id = await _resolve_identity(
            user=user,
            store=store,
            draft=draft,
            reserve_new=False,
        )
        if provider_id is None:
            raise _error(422, "provider_identity_required")
        if provider_id in resolved:
            raise _error(422, "duplicate_provider_draft")
        saved_provider = saved_providers.get(draft.runtime_key) if isinstance(saved_providers, dict) else None
        saved_secret = None
        if isinstance(saved_provider, dict):
            saved_auth = saved_provider.get("auth")
            saved_secret = (
                saved_provider.get("api_key")
                or saved_provider.get("secret_key")
                or (saved_auth.get("secret") if isinstance(saved_auth, dict) else None)
            )
        api_key = draft.api_key or saved_secret
        auth_type = str((draft.auth or {}).get("type") or "bearer")
        if auth_type != "none" and (not isinstance(api_key, str) or not api_key):
            raise _error(422, "api_key_required")
        provider = draft.provider_config(api_key=api_key)
        if not runtime_parameter_changes_admitted(saved, {"providers": {draft.runtime_key: provider}}):
            raise _error(409, "model_runtime_parameters_disabled")
        if isinstance(saved_provider, dict):
            for field in ("outbound_params", "context_window", "max_output_tokens"):
                if field not in provider and field in saved_provider:
                    provider[field] = saved_provider[field]
        try:
            validate_provider_draft(draft.runtime_key, provider)
        except TeamLlmConfigError as exc:
            code = "blocked_by_security_policy" if "blocked by deployment mode" in str(exc) else "invalid_provider_config"
            raise _error(422, code) from exc
        resolved[provider_id] = {
            "runtime_key": draft.runtime_key,
            "display_name": draft.display_name or draft.runtime_key,
            "config": provider,
        }
    return resolved


def _plan_request_hash(
    *,
    base_config_version: int,
    items: list[VerificationPlanItem],
) -> str:
    payload = {
        "base_config_version": base_config_version,
        "items": [item.as_dict() for item in items],
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _build_plan_items(
    *,
    providers: dict[str, dict[str, Any]],
    requested_items: list[V2VerificationItem],
) -> list[VerificationPlanItem]:
    registry = CompatibleProbeRegistry()
    seen: set[tuple[str, str, str]] = set()
    result: list[VerificationPlanItem] = []
    for requested in requested_items:
        key = (
            requested.provider_id,
            requested.model_id,
            requested.capability,
        )
        if key in seen:
            raise _error(422, "duplicate_verification_item")
        seen.add(key)
        provider_context = providers.get(requested.provider_id)
        if provider_context is None:
            raise _error(422, "provider_draft_required")
        plan = registry.plan(
            provider_context["config"],
            provider_id=requested.provider_id,
            provider_display_name=provider_context["display_name"],
            model_id=requested.model_id,
            capability=requested.capability,  # type: ignore[arg-type]
        )
        signature = compute_config_signature(
            plan.transport,
            provider_id=requested.provider_id,
            model_id=requested.model_id,
            capability=requested.capability,  # type: ignore[arg-type]
            testability_contract_version=plan.testability_contract_version,
            probe_contract_version=plan.probe_contract_version,
            probe_execution_policy_version=(plan.probe_execution_policy_version),
        )
        result.append(
            VerificationPlanItem(
                provider_id=requested.provider_id,
                provider_display_name=provider_context["display_name"],
                model_id=requested.model_id,
                capability=requested.capability,
                config_signature=signature,
                testability=("testable" if plan.provider_request_count else "not_testable"),
                testability_contract_version=(plan.testability_contract_version),
                probe_contract_version=plan.probe_contract_version,
                probe_execution_policy_version=(plan.probe_execution_policy_version),
                provider_request_count=plan.provider_request_count,
                safe_code=plan.safe_code if isinstance(plan, NotTestable) else None,
            )
        )
    return result


def _encode_evidence_cursor(observed_at: str, evidence_id: str) -> str:
    raw = json.dumps(
        [observed_at, evidence_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_evidence_cursor(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(f"{value}{padding}").decode())
    except Exception as exc:
        raise _error(422, "invalid_cursor") from exc
    if not isinstance(decoded, list) or len(decoded) != 2 or not all(isinstance(item, str) for item in decoded):
        raise _error(422, "invalid_cursor")
    return decoded[0], decoded[1]


@router.post(
    "/providers/precheck",
    dependencies=[
        Depends(require_model_discovery_v2),
        Depends(require_not_readonly),
    ],
)
async def precheck_provider(
    body: V2ProviderPrecheckBody,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Validate structure/targets and optionally reserve a stable identity."""

    provider = body.provider_config()
    _require_provider_type_enabled(body.type)
    try:
        validate_provider_draft(body.runtime_key, provider)
    except TeamLlmConfigError as exc:
        code = "blocked_by_security_policy" if "blocked by deployment mode" in str(exc) else "invalid_provider_config"
        raise _error(422, code) from exc

    provider_id = await _resolve_identity(
        user=user,
        store=store,
        draft=body,
        reserve_new=body.reserve_provider_id,
    )
    first_model = body.models[0] if body.models else None
    purposes: list[tuple[str, str, str | None]] = []
    if body.type == "openai_compatible" and first_model is None:
        purposes.extend(
            [
                ("chat", "probe_chat", None),
                ("models", "discovery", None),
                ("embed", "probe_embed", None),
            ]
        )
    elif first_model is not None:
        for capability in first_model.capabilities:
            if capability in {"chat", "tool", "vision", "embed"}:
                purposes.append(
                    (
                        capability,
                        ("probe_embed" if capability == "embed" else f"probe_{capability}"),
                        first_model.id,
                    )
                )
    if body.type == "openai_compatible" and first_model is not None:
        purposes.append(("models", "discovery", None))
    previews = []
    for label, purpose, model_id in purposes:
        plan = build_provider_transport_plan(
            provider,
            purpose=purpose,  # type: ignore[arg-type]
            model_id=model_id,
            capability=label if label != "models" else None,
        )
        previews.append(
            {
                "purpose": label,
                "method": plan.method,
                "safe_url": _safe_url(plan.request_url),
            }
        )
    warnings = []
    if (body.auth or {}).get("type", "bearer") != "none" and not body.api_key:
        warnings.append("secret_required_unless_saved")
    if body.type in {"azure_openai", "custom_http"} and not body.models:
        warnings.append("model_required")
    return {
        "ok": True,
        "provider_id": provider_id,
        "discovery": {
            "supported": body.type == "openai_compatible",
        },
        "request_previews": previews,
        "warnings": warnings,
    }


@router.post(
    "/providers/{provider_id}/discover-models",
    dependencies=[
        Depends(require_model_discovery_v2),
        Depends(require_not_readonly),
    ],
)
async def discover_models(
    provider_id: str,
    body: V2ProviderDraft,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Return isolated candidates; never mutate the Draft or Evidence."""

    if body.provider_id and body.provider_id != provider_id:
        raise _error(409, "provider_identity_mismatch")
    _require_provider_type_enabled(body.type)
    body_with_path_id = body.model_copy(update={"provider_id": provider_id})
    resolved_id = await _resolve_identity(
        user=user,
        store=store,
        draft=body_with_path_id,
        reserve_new=False,
    )
    if resolved_id != provider_id:
        raise _error(404, "provider_identity_not_found")
    if body.type != "openai_compatible":
        raise _error(422, "discovery_not_supported")

    saved, _version = await store.get_team_llm_config_with_version(user.team_id)
    saved_provider = (saved.get("providers") or {}).get(body.runtime_key) or {}
    saved_auth = saved_provider.get("auth")
    api_key = body.api_key or saved_provider.get("api_key") or (saved_auth.get("secret") if isinstance(saved_auth, dict) else None)
    if not isinstance(api_key, str) or not api_key:
        raise _error(422, "api_key_required")
    provider = body.provider_config(api_key=api_key)
    try:
        validate_provider_draft(body.runtime_key, provider)
    except TeamLlmConfigError as exc:
        code = "discovery_blocked_by_security_policy" if "blocked by deployment mode" in str(exc) else "invalid_provider_config"
        raise _error(422, code) from exc

    result = await discover_provider_model_candidates(
        base_url=body.base_url,
        api_key=api_key,
        default_headers=body.default_headers,
        timeout_sec=min(body.timeout_sec, 30),
    )
    if not result["ok"]:
        code = str(result["code"])
        status_by_code = {
            "discovery_response_too_large": 413,
            "discovery_timeout": 504,
            "discovery_blocked_by_security_policy": 422,
            "discovery_auth_failed": 502,
            "discovery_endpoint_not_found": 502,
            "discovery_rate_limited": 502,
            "discovery_tls_error": 502,
            "discovery_dns_error": 502,
            "discovery_invalid_response": 502,
        }
        raise _error(
            status_by_code.get(code, 502),
            code,
            http_status=result.get("http_status"),
        )

    added_ids = {model.id for model in body.models}
    candidates = [
        {
            **candidate,
            "already_added": candidate["model_id"] in added_ids,
            "capability_state": "unknown",
        }
        for candidate in result["candidates"]
    ]
    return {
        "ok": True,
        "provider_id": provider_id,
        "discovered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "candidates": candidates,
        "returned_count": result["returned_count"],
        "truncated": result["truncated"],
        "limit": result["limit"],
    }


@router.post(
    "/verification-plans",
    dependencies=[
        Depends(require_model_discovery_v2),
        Depends(require_not_readonly),
    ],
)
async def create_verification_plan(
    body: V2VerificationPlanBody,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Plan exact probes and request count without contacting a Provider."""

    _saved, current_version = await store.get_team_llm_config_with_version(user.team_id)
    if current_version != body.base_config_version:
        raise _error(409, "config_version_conflict")
    try:
        providers = await _materialize_provider_drafts(
            user=user,
            store=store,
            drafts=body.provider_drafts,
        )
        items = _build_plan_items(
            providers=providers,
            requested_items=body.items,
        )
    except TeamLlmSignatureKeyError as exc:
        raise _error(503, "signature_key_unavailable") from exc
    request_hash = _plan_request_hash(
        base_config_version=body.base_config_version,
        items=items,
    )
    now = datetime.now(UTC)
    plan = await _batch_store(store).create_plan(
        team_id=user.team_id,
        actor_user_id=user.id,
        request_hash=request_hash,
        items=items,
        now=now,
    )
    expires_at_epoch = int(plan.expires_at.timestamp())
    try:
        token = issue_control_token(
            {
                "team_id": user.team_id,
                "actor_user_id": user.id,
                "plan_id": plan.plan_id,
                "base_config_version": body.base_config_version,
                "request_hash": request_hash,
                "item_count": len(items),
                "provider_request_count": plan.provider_request_count,
                "nonce": secrets.token_urlsafe(12),
            },
            purpose="verification-plan",
            expires_at_epoch=expires_at_epoch,
        )
    except TeamLlmSignatureKeyError as exc:
        raise _error(503, "signature_key_unavailable") from exc
    return {
        "plan_id": plan.plan_id,
        "confirmation_token": token,
        "expires_at": plan.expires_at.isoformat().replace("+00:00", "Z"),
        "item_count": len(items),
        "provider_request_count": plan.provider_request_count,
        "max_concurrency": 3,
        "items": [{key: value for key, value in item.as_dict().items() if key != "config_signature"} for item in items],
        "notices": [
            "provider_may_charge",
            "no_business_data",
            "tool_will_not_execute",
        ],
    }


@router.post(
    "/verification-batches",
    status_code=202,
    dependencies=[
        Depends(require_model_discovery_v2),
        Depends(require_not_readonly),
    ],
)
async def create_verification_batch(
    body: V2CreateBatchBody,
    request: Request,
    response: Response,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
    ),
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """Consume one confirmed Plan and start its process-owned runner."""

    try:
        token = verify_control_token(
            body.confirmation_token,
            purpose="verification-plan",
        )
    except (ControlTokenError, TeamLlmSignatureKeyError) as exc:
        raise _error(409, "invalid_confirmation_token") from exc
    if token.get("team_id") != user.team_id or token.get("actor_user_id") != user.id or token.get("plan_id") != body.plan_id:
        raise _error(409, "invalid_confirmation_token")
    base_config_version = token.get("base_config_version")
    if not isinstance(base_config_version, int):
        raise _error(409, "invalid_confirmation_token")
    _saved, current_version = await store.get_team_llm_config_with_version(user.team_id)
    if current_version != base_config_version:
        raise _error(409, "config_version_conflict")
    batch_store = _batch_store(store)
    plan = await batch_store.get_plan(
        team_id=user.team_id,
        plan_id=body.plan_id,
    )
    if plan is None or plan.actor_user_id != user.id or plan.request_hash != token.get("request_hash"):
        raise _error(409, "invalid_confirmation_token")
    providers = await _materialize_provider_drafts(
        user=user,
        store=store,
        drafts=body.provider_drafts,
    )
    requested_items = [
        V2VerificationItem(
            provider_id=item.provider_id,
            model_id=item.model_id,
            capability=item.capability,
        )
        for item in plan.items
    ]
    rebuilt = _build_plan_items(
        providers=providers,
        requested_items=requested_items,
    )
    request_hash = _plan_request_hash(
        base_config_version=base_config_version,
        items=rebuilt,
    )
    if request_hash != plan.request_hash:
        raise _error(409, "verification_plan_changed")
    runner_id = f"llmvr_{uuid4().hex}"
    try:
        batch = await batch_store.create_batch(
            team_id=user.team_id,
            actor_user_id=user.id,
            plan_id=body.plan_id,
            base_config_version=base_config_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            probe_context={"providers": providers},
            runner_id=runner_id,
        )
    except IdempotencyConflict as exc:
        raise _error(409, exc.code) from exc
    except (PlanExpired, PlanNotFound) as exc:
        raise _error(409, exc.code) from exc
    except TeamLlmBatchStoreError as exc:
        raise _error(503, exc.code) from exc
    if batch.replayed:
        response.status_code = 200
    else:
        try:
            await store.write_admin_audit_log(
                actor_user_id=user.id,
                team_id=user.team_id,
                action=AuditAction.LLM_VERIFICATION_BATCH_STARTED,
                target_kind="llm_verification_batch",
                target_id=batch.batch_id,
                scope="team",
                payload={
                    "batch_id": batch.batch_id,
                    "item_count": len(plan.items),
                    "provider_request_count": (plan.provider_request_count),
                },
                ip=(request.client.host if request.client else None),
            )
        except Exception:
            logger.warning(
                "verification Batch start audit failed",
                exc_info=True,
            )
        request.app.state.llm_verification_runner.start(
            batch_id=batch.batch_id,
            team_id=user.team_id,
            runner_id=runner_id,
            initiated_by=user.id,
        )
    return {
        "batch_id": batch.batch_id,
        "plan_id": batch.plan_id,
        "state": batch.state,
        "revision": batch.revision,
        "replayed": batch.replayed,
    }


@router.get(
    "/verification-batches/{batch_id}",
    dependencies=[Depends(require_model_discovery_v2)],
)
async def get_verification_batch(
    batch_id: str,
    after_revision: int | None = Query(None, ge=0),
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    snapshot = await _batch_store(store).get_batch(
        team_id=user.team_id,
        batch_id=batch_id,
        after_revision=after_revision,
    )
    if snapshot is None:
        raise _error(404, "verification_batch_not_found")
    return snapshot


@router.get(
    "/verification-batches",
    dependencies=[Depends(require_model_discovery_v2)],
)
async def list_verification_batches(
    state: str = Query("active", pattern=r"^active$"),
    limit: int = Query(10, ge=1, le=10),
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    del state
    batches = await _batch_store(store).list_active_batches(
        team_id=user.team_id,
        limit=limit,
    )
    return {"batches": batches}


@router.post(
    "/verification-batches/{batch_id}/cancel",
    dependencies=[
        Depends(require_model_discovery_v2),
        Depends(require_not_readonly),
    ],
)
async def cancel_verification_batch(
    batch_id: str,
    request: Request,
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    try:
        batch = await _batch_store(store).cancel_batch(
            team_id=user.team_id,
            batch_id=batch_id,
        )
    except PlanNotFound as exc:
        raise _error(404, "verification_batch_not_found") from exc
    request.app.state.llm_verification_runner.cancel(batch_id)
    try:
        snapshot = await _batch_store(store).get_batch(
            team_id=user.team_id,
            batch_id=batch_id,
        )
        await store.write_admin_audit_log(
            actor_user_id=user.id,
            team_id=user.team_id,
            action=AuditAction.LLM_VERIFICATION_BATCH_CANCELLED,
            target_kind="llm_verification_batch",
            target_id=batch_id,
            scope="team",
            payload={
                "batch_id": batch_id,
                "state": batch.state,
                "counts": (snapshot["counts"] if snapshot else {}),
            },
            ip=request.client.host if request.client else None,
        )
    except Exception:
        logger.warning(
            "verification Batch cancel audit failed",
            exc_info=True,
        )
    return {
        "batch_id": batch.batch_id,
        "state": batch.state,
        "revision": batch.revision,
        "request_may_have_reached_provider": True,
    }


@router.get("/context-window-catalog")
async def context_window_catalog(user: User = Depends(_REQUIRE_TEAM_ADMIN)) -> dict[str, Any]:
    """Reviewed context windows the model form prefills (design agent-release-beginner-experience §8).

    Static, reviewed data: documented windows by model id, and the default used for models the
    catalog does not know. Nothing team-specific is returned.
    """
    from bowhead_router.model_capabilities import DEFAULT_CONTEXT_WINDOW, KNOWN_CONTEXT_WINDOWS, output_budget_defaults

    del user
    return {"default": DEFAULT_CONTEXT_WINDOW, "models": dict(KNOWN_CONTEXT_WINDOWS), "output_budget_defaults": output_budget_defaults()}


@router.get("/outbound-adaptations")
async def list_outbound_adaptations(
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    """运行时**自动适配**掉的出站参数（设置页的「已自动适配」态读它）。

    这是**观测**不是配置：模型服务拒绝了某个参数，运行时丢掉它重试并记住
    （见 `docs/sprints/llm-outbound-params-2026-08/tech-design.md` §3.5）。
    页面据此提示，并提供「固化为配置」让人拍板 —— 我们绝不代写配置。

    只返回参数名与时间。地址、凭据、provider 的报错正文一律不出现在这里。
    记忆按**传输身份**（协议 + 主机 + 模型）保存，所以只能查到本团队配置里真实存在的
    (来源, 模型) 组合 —— 遍历的是这个团队自己的配置，查不到别人的东西。
    """

    saved, _version = await store.get_team_llm_config_with_version(user.team_id)
    providers = (saved or {}).get("providers")
    items: list[dict[str, Any]] = []
    if isinstance(providers, dict):
        for name, spec in providers.items():
            if not isinstance(spec, dict):
                continue
            adapter_type = str(spec.get("type") or "")
            base_url = str(spec.get("base_url") or spec.get("endpoint") or "")
            if not adapter_type or not base_url:
                continue
            for model in spec.get("models") or []:
                model_id = str(model.get("id") or "") if isinstance(model, dict) else str(model or "")
                if not model_id:
                    continue
                for adaptation in learned_adaptations(
                    adapter_type=adapter_type,
                    base_url=base_url,
                    model=model_id,
                ):
                    items.append(
                        {
                            "provider": str(name),
                            "model_id": model_id,
                            "param": adaptation.param,
                            "since": adaptation.since.isoformat(),
                        }
                    )
    return {"items": items}


@router.get(
    "/verification-evidence",
    dependencies=[Depends(require_model_discovery_v2)],
)
async def list_verification_evidence(
    provider_id: str | None = Query(None),
    model_id: str | None = Query(None, max_length=200),
    capability: str | None = Query(None, pattern=r"^(chat|tool|vision|embed|rerank)$"),
    cursor: str | None = Query(None, max_length=1000),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(_REQUIRE_TEAM_ADMIN),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    rows = await _batch_store(store).list_evidence(
        team_id=user.team_id,
        provider_id=provider_id,
        model_id=model_id,
        capability=capability,
        before=_decode_evidence_cursor(cursor),
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = (
        _encode_evidence_cursor(
            visible[-1]["observed_at"],
            visible[-1]["evidence_id"],
        )
        if has_more and visible
        else None
    )
    return {
        "items": visible,
        "next_cursor": next_cursor,
    }
