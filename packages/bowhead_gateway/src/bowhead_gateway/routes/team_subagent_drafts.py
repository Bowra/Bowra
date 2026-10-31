"""Conversational Subagent draft HTTP API.

All draft reads are owner + active-team scoped. Missing, cross-team, and
cross-owner objects deliberately share the same 404 response.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import aiosqlite
from bowhead_core.agent_revision_store import (
    AgentAudience,
    AgentPublicationBlockedError,
    AgentRevisionError,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.subagent_authoring_catalog import (
    RISK_REGISTRY_VERSION,
    SubagentAuthoringCatalogService,
)
from bowhead_core.subagent_authoring_rollout import (
    resolve_subagent_authoring_capabilities,
)
from bowhead_core.subagent_draft_accounting import (
    SubagentDraftCreditAccountant,
    SubagentDraftCreditError,
)
from bowhead_core.subagent_draft_service import (
    TEXT_ONLY_CONFIRMATION,
    DraftServiceError,
    DraftServiceOutcome,
    SubagentDraftService,
    carry_forward_unmet_requirements,
    definition_fingerprint,
    reserved_team_subagent_names,
)
from bowhead_core.subagent_draft_store import (
    CanvasDraftSuggestion,
    SubagentDraft,
    SubagentDraftIdempotencyConflictError,
    SubagentDraftSchemaUnavailableError,
    SubagentDraftStore,
    SubagentDraftVersionConflictError,
)
from bowhead_core.team_subagent_store import (
    TeamSubagentDraftNotConsumableError,
    TeamSubagentNameConflictError,
    TeamSubagentOwnerInactiveError,
    TeamSubagentRevisionWriterRequiredError,
    TeamSubagentStore,
)
from bowhead_core.team_subagent_validation import (
    MAX_SYSTEM_PROMPT,
    OUTPUT_SCHEMA_DESCRIPTION,
    TeamSubagentDefinitionCandidate,
    TeamSubagentValidationError,
    validate_candidate,
)
from bowhead_core.team_subagent_validation import (
    validate_tool_refs as validate_catalog_tool_refs,
)
from bowhead_core.user_store import TeamSubagent, User, UserStore, _id, _now
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bowhead_gateway._entitlement_gate import (
    assert_feature_licensed,
    assert_not_readonly,
    is_feature_licensed_request,
    require_not_readonly,
)
from bowhead_gateway.agent_authoring_dependencies import draft_dependency_preflight
from bowhead_gateway.builtin_plugin_fields import (
    RESOURCE_SELECTION_KEYS,
    OmitAbsentBuiltinPlugins,
    has_resource_selection,
    validate_builtin_plugin_ids,
)
from bowhead_gateway.conditional_headers import entity_tags_match
from bowhead_gateway.routes.auth import _current_user, _store
from bowhead_gateway.subagent_draft_runtime import (
    build_subagent_draft_service,
    subagent_draft_hmac_key,
)

router = APIRouter()


class CreateDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source_mode: Literal["intent", "turn"] = "intent"
    purpose: str = Field(max_length=2000)
    source_turn_id: str | None = Field(default=None, max_length=160)
    locale: str = Field(default="en-US", max_length=20)


class CreateManualDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    source_mode: Literal["manual", "intent"]
    purpose: str = Field(default="", max_length=2000)
    locale: str = Field(default="en-US", max_length=20)


class ManualDefinitionBody(OmitAbsentBuiltinPlugins):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=32)
    display_name: str = Field(max_length=40)
    description: str = Field(max_length=200)
    system_prompt: str = Field(max_length=MAX_SYSTEM_PROMPT)
    tool_refs: list[str] = Field(max_length=60)
    skill_grants: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    capability_ids: list[str] = Field(default_factory=list, max_length=24)
    builtin_plugin_ids: list[str] | None = Field(default=None, max_length=16)

    @field_validator("builtin_plugin_ids")
    @classmethod
    def _builtin_plugin_ids(cls, value: list[str] | None) -> list[str] | None:
        return validate_builtin_plugin_ids(value)

    max_iterations: int = Field(default=12, ge=1, le=50, json_schema_extra={"readOnly": True})
    model: str | None = None
    output_schema: dict[str, Any] | None = Field(default=None, description=OUTPUT_SCHEMA_DESCRIPTION)
    output_schema_text: str = Field(default="", max_length=20000)
    visibility: Literal["creator_only"] = "creator_only"


class PatchManualDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    purpose: str = Field(max_length=2000)
    definition: ManualDefinitionBody
    text_only_confirmed: bool = False


class GenerateCanvasSuggestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    purpose: str = Field(min_length=1, max_length=2000)
    locale: str = Field(default="en-US", max_length=20)


class AcceptCanvasSuggestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    selected_fields: list[str] = Field(min_length=1, max_length=10)
    mutation_id: str = Field(min_length=36, max_length=36)


class DraftDefinitionBody(OmitAbsentBuiltinPlugins):
    model_config = ConfigDict(extra="forbid")

    skill_grants: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    capability_ids: list[str] = Field(default_factory=list, max_length=24)
    builtin_plugin_ids: list[str] | None = Field(default=None, max_length=16)

    @field_validator("builtin_plugin_ids")
    @classmethod
    def _builtin_plugin_ids(cls, value: list[str] | None) -> list[str] | None:
        return validate_builtin_plugin_ids(value)

    name: str
    display_name: str = Field(max_length=40)
    description: str = Field(max_length=200)
    system_prompt: str = Field(max_length=MAX_SYSTEM_PROMPT)
    model: str | None = None
    max_iterations: int = Field(default=12, ge=1, le=50, json_schema_extra={"readOnly": True})
    output_schema: dict[str, Any] | None = Field(default=None, description=OUTPUT_SCHEMA_DESCRIPTION)
    visibility: Literal["creator_only"] = "creator_only"


class PatchDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    definition: DraftDefinitionBody
    selected_tool_refs: list[str] = Field(max_length=60)
    selected_skills: list[str] = Field(default_factory=list, max_length=0)


class CreateFromDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 1
    mutation_id: str = Field(min_length=36, max_length=36)
    acknowledged_warning_codes: list[str] = Field(
        default_factory=list,
        max_length=20,
    )


class ClarifyDraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    answer: str = Field(min_length=1, max_length=2000)


def _error(
    status_code: int,
    code: str,
    **details: Any,
) -> HTTPException:
    payload: dict[str, Any] = {"code": code}
    if details:
        payload["details"] = details
    return HTTPException(status_code, {"error": payload})


def _require_unified_draft(team_id: str) -> None:
    if not resolve_subagent_authoring_capabilities(team_id).unified_draft:
        raise _error(409, "subagent_unified_draft_unavailable")


async def _active_space(request: Request, user: User) -> str:
    space_id = (request.headers.get("X-Space-Id") or "").strip()
    if not space_id:
        raise _error(400, "subagent_draft_space_required")
    store = getattr(request.app.state, "user_store", None)
    if not isinstance(store, UserStore):
        raise _error(503, "subagent_draft_store_unavailable")
    memberships = await store.list_workspaces_for_user(user.id, team_id=user.team_id)
    if not any(row.get("workspace_id") == space_id for row in memberships):
        raise _error(404, "subagent_draft_space_not_found")
    return space_id


def _etag(draft: SubagentDraft) -> str:
    return f'W/"{draft.id}:{draft.row_version}"'


def _expected_version(request: Request, draft: SubagentDraft) -> int:
    value = request.headers.get("If-Match")
    if value is None:
        raise _error(428, "subagent_draft_if_match_required")
    if not entity_tags_match(value, _etag(draft)):
        raise _error(
            409,
            "subagent_draft_version_conflict",
            current_version=draft.row_version,
            etag=_etag(draft),
        )
    return draft.row_version


def _submitted_version(request: Request, draft_id: str) -> int:
    """Read the exact version submitted by a conversion retry."""
    value = request.headers.get("If-Match")
    if value is None:
        raise _error(428, "subagent_draft_if_match_required")
    tag = value.strip().removeprefix("W/")
    if not (tag.startswith('"') and tag.endswith('"')):
        raise _error(409, "subagent_draft_version_conflict")
    identifier, separator, version = tag[1:-1].rpartition(":")
    if not separator or identifier != draft_id or not version.isdecimal():
        raise _error(409, "subagent_draft_version_conflict")
    return int(version)


async def _draft_store(request: Request) -> SubagentDraftStore:
    existing = getattr(request.app.state, "subagent_draft_store", None)
    if isinstance(existing, SubagentDraftStore):
        return existing
    user_store = getattr(request.app.state, "user_store", None)
    if not isinstance(user_store, UserStore):
        raise _error(503, "subagent_draft_store_unavailable")
    # During the migration window the draft tables may be absent, and without
    # a JWT keyring there is no HMAC key: both are temporary unavailability.
    try:
        store = SubagentDraftStore(
            user_store.db_path,
            hmac_key=subagent_draft_hmac_key(),
        )
        await store.initialize()
    except (SubagentDraftSchemaUnavailableError, RuntimeError) as exc:
        raise _error(503, "subagent_draft_store_unavailable") from exc
    request.app.state.subagent_draft_store = store
    return store


async def _accountant(request: Request) -> SubagentDraftCreditAccountant:
    existing = getattr(
        request.app.state,
        "subagent_draft_credit_accountant",
        None,
    )
    if isinstance(existing, SubagentDraftCreditAccountant):
        return existing
    user_store = getattr(request.app.state, "user_store", None)
    if not isinstance(user_store, UserStore):
        raise _error(503, "subagent_draft_credit_unavailable")
    accountant = SubagentDraftCreditAccountant(user_store.db_path)
    await accountant.initialize()
    request.app.state.subagent_draft_credit_accountant = accountant
    return accountant


async def _team_router(request: Request, team_id: str) -> Any:
    override = getattr(request.app.state, "subagent_draft_router", None)
    if override is not None:
        return override
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    resolved = await resolve_team_model_runtime(
        request.app.state,
        team_id,
        entrypoint="tool_continuation",
    )
    if not resolved.available:
        raise _error(409, "team_model_unconfigured")
    return resolved.router


async def _service(
    request: Request,
    user: User,
) -> SubagentDraftService:
    override = getattr(request.app.state, "subagent_draft_service", None)
    if override is not None:
        return override
    store = getattr(request.app.state, "user_store", None)
    if not isinstance(store, UserStore):
        raise _error(503, "subagent_draft_store_unavailable")
    # Ensure lazily-created stores are attached before the shared factory reads
    # app.state (normal lifespan startup already did this).
    await _draft_store(request)
    await _accountant(request)
    return build_subagent_draft_service(
        app_state=request.app.state,
        router=await _team_router(request, user.team_id),
    )


async def _owned_draft(
    request: Request,
    user: User,
    draft_id: str,
) -> SubagentDraft:
    draft = await (await _draft_store(request)).get_draft(
        draft_id,
        team_id=user.team_id,
        owner_user_id=user.id,
    )
    if draft is None:
        raise _error(404, "subagent_draft_not_found")
    return draft


def _draft_dto(
    draft: SubagentDraft,
    *,
    writable: bool = True,
) -> dict[str, Any]:
    empty_tools_allowed = resolve_subagent_authoring_capabilities(draft.team_id).text_only and any(
        warning.get("code") == TEXT_ONLY_CONFIRMATION for warning in draft.warnings
    )
    has_tools = bool(draft.definition and draft.definition.get("tool_refs"))
    active = draft.status in {
        "generating",
        "needs_clarification",
        "ready",
        "blocked",
    }
    return {
        "schema_version": 1,
        "kind": "team_subagent_draft_detail",
        "draft_id": draft.id,
        "draft_status": draft.status,
        "authoring_state": ("reviewing" if draft.status in {"ready", "blocked"} else draft.status),
        "source_mode": draft.source_mode,
        "entry_mode": "conversational",
        "purpose": draft.purpose_text,
        "goal_summary": draft.goal_summary,
        "definition": draft.definition,
        "tools": draft.recommendations or [],
        "skills": [],
        "assumptions": draft.assumptions,
        "warnings": draft.warnings,
        "clarification": draft.clarification,
        "validation": {
            "status": draft.validation_status,
            "issues": draft.validation_issues,
            "catalog_fingerprint": draft.catalog_fingerprint,
        },
        "mutation_id": draft.mutation_id,
        "version": draft.row_version,
        "etag": _etag(draft),
        "expires_at": draft.expires_at,
        "result": (
            {
                "definition_id": draft.consumed_definition_id,
                "consumed_at": draft.consumed_at,
            }
            if draft.status == "consumed"
            else None
        ),
        "actions": {
            "clarify": (writable and draft.status == "needs_clarification" and draft.business_clarification_count < 1),
            "edit": (writable and active and draft.status in {"ready", "blocked"}),
            "validate": (writable and active and draft.status in {"ready", "blocked"}),
            "create": (
                writable and draft.status == "ready" and draft.validation_status in {"valid", "warning"} and (has_tools or empty_tools_allowed)
            ),
            "discard": writable and active,
            "regenerate": (writable and draft.status == "expired" and draft.purpose_text is not None),
        },
    }


def _manual_draft_dto(draft: SubagentDraft, *, writable: bool) -> dict[str, Any]:
    active = draft.status in {"editing", "ready", "blocked"}
    capabilities = resolve_subagent_authoring_capabilities(draft.team_id)
    empty_tools_allowed = capabilities.text_only and any(warning.get("code") == TEXT_ONLY_CONFIRMATION for warning in draft.warnings)
    can_create = (
        writable
        and draft.status == "ready"
        and draft.validation_status in {"valid", "warning"}
        and (bool((draft.definition or {}).get("tool_refs")) or has_resource_selection(draft.definition) or empty_tools_allowed)
    )
    return {
        "schema_version": 2,
        "kind": "team_subagent_manual_draft_detail",
        "draft_id": draft.id,
        "draft_status": draft.status,
        "source_mode": draft.source_mode,
        "entry_mode": "canvas",
        "workspace_id": draft.workspace_id,
        "purpose": draft.purpose_text or "",
        "definition": draft.definition or {},
        "validation": {"status": draft.validation_status, "issues": draft.validation_issues},
        "warnings": draft.warnings,
        "mutation_id": draft.mutation_id,
        "result": ({"definition_id": draft.consumed_definition_id, "consumed_at": draft.consumed_at} if draft.status == "consumed" else None),
        "version": draft.row_version,
        "etag": _etag(draft),
        "updated_at": draft.updated_at,
        "expires_at": draft.expires_at,
        "actions": {
            "edit": writable and active,
            "validate": writable and active,
            "create": can_create,
            "discard": writable and active,
        },
    }


def _canvas_suggestion_dto(suggestion: CanvasDraftSuggestion) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "kind": "team_subagent_canvas_suggestion",
        "suggestion_id": suggestion.id,
        "draft_id": suggestion.draft_id,
        "base_version": suggestion.base_version,
        "suggestion_kind": suggestion.kind,
        "status": suggestion.status,
        "payload": suggestion.payload,
        "created_at": suggestion.created_at,
        "expires_at": suggestion.expires_at,
    }


async def _request_writable(request: Request) -> bool:
    try:
        await assert_not_readonly(request)
    except HTTPException:
        return False
    return True


@router.post(
    "",
    dependencies=[Depends(require_not_readonly)],
)
async def create_draft(
    body: CreateDraftBody | CreateManualDraftBody,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    if idempotency_key is None:
        raise _error(428, "idempotency_key_required")
    if isinstance(body, CreateManualDraftBody):
        _require_unified_draft(user.team_id)
        space_id = await _active_space(request, user)
        try:
            manual = await (await _draft_store(request)).create_manual_draft(
                team_id=user.team_id,
                owner_user_id=user.id,
                workspace_id=space_id,
                locale=body.locale,
                purpose=body.purpose,
                idempotency_key=idempotency_key,
                source_mode=body.source_mode,
            )
        except SubagentDraftIdempotencyConflictError as exc:
            raise _error(409, "idempotency_key_reused") from exc
        except ValueError as exc:
            raise _error(422, "subagent_draft_request_invalid") from exc
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["ETag"] = _etag(manual)
        response.status_code = status.HTTP_201_CREATED if manual.row_version == 1 else status.HTTP_200_OK
        return _manual_draft_dto(manual, writable=True)
    if body.source_mode == "turn" and not body.source_turn_id:
        raise _error(422, "source_turn_id_required")
    if body.source_mode == "intent" and body.source_turn_id is not None:
        raise _error(422, "source_turn_id_not_allowed")
    try:
        result = await (await _service(request, user)).create_intent_draft(
            team_id=user.team_id,
            owner_user_id=user.id,
            purpose=body.purpose,
            locale=body.locale,
            idempotency_key=idempotency_key,
            actor_active=True,
            writable=True,
            authoring_enabled=True,
            mcp_entitled=await is_feature_licensed_request(request, "mcp"),
            source_turn_id=body.source_turn_id,
        )
    except SubagentDraftCreditError as exc:
        raise _error(402, exc.code) from exc
    except DraftServiceError as exc:
        if exc.code in {
            "user_credit_exhausted",
            "team_pool_exhausted",
        }:
            raise _error(402, exc.code) from exc
        status_code = {
            "idempotency_key_reused": 409,
            "team_model_unconfigured": 409,
            "subagent_purpose_too_long": 422,
        }.get(exc.code, 503)
        raise _error(status_code, exc.code) from exc
    response.headers["Cache-Control"] = "no-store, private"
    if result.outcome is DraftServiceOutcome.REQUEST_IN_PROGRESS:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "1"
        return {
            "schema_version": 1,
            "kind": "subagent_draft_request_in_progress",
            "retry_after_ms": result.retry_after_ms,
            "expires_at": result.expires_at,
            "draft_id": None,
        }
    if result.outcome in {
        DraftServiceOutcome.PURPOSE_REQUIRED,
        DraftServiceOutcome.PURPOSE_INSUFFICIENT,
    }:
        raise _error(422, result.outcome.value)
    if result.outcome in {
        DraftServiceOutcome.MANUAL,
        DraftServiceOutcome.CANCELLED,
    }:
        return {
            "schema_version": 1,
            "kind": "subagent_draft_outcome",
            "outcome": result.outcome.value,
        }
    if result.draft is None:
        raise _error(503, "subagent_draft_result_missing")
    if result.draft.entry_mode == "canvas":
        raise _error(409, "subagent_draft_converted", draft_id=result.draft.id)
    response.status_code = (
        status.HTTP_200_OK
        if result.outcome is DraftServiceOutcome.REPLAY
        else (status.HTTP_202_ACCEPTED if result.outcome is DraftServiceOutcome.NEEDS_CLARIFICATION else status.HTTP_201_CREATED)
    )
    response.headers["ETag"] = _etag(result.draft)
    return _draft_dto(result.draft)


@router.get("")
async def list_drafts(
    request: Request,
    response: Response,
    schema_version: int = 1,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    if schema_version not in {1, 2}:
        raise _error(422, "subagent_draft_schema_unsupported")
    space_id = None
    if schema_version == 2:
        space_id = await _active_space(request, user)
    drafts = await (await _draft_store(request)).list_active_drafts(
        team_id=user.team_id,
        owner_user_id=user.id,
        entry_mode="canvas" if schema_version == 2 else "conversational",
        workspace_id=space_id,
    )
    response.headers["Cache-Control"] = "no-store, private"

    def draft_title(draft: SubagentDraft) -> str:
        definition = draft.definition or {}
        # The identifier (`name`) is internal and often generated ("agent-d9db64"); an unnamed draft gets the
        # client's "Untitled draft" label instead of it.
        candidates = (
            (definition.get("display_name"), draft.goal_summary, draft.purpose_text)
            if draft.entry_mode == "canvas"
            else (draft.goal_summary, draft.purpose_text, definition.get("display_name"))
        )
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    return {
        "schema_version": schema_version,
        "kind": "team_subagent_draft_list",
        "items": [
            {
                "draft_id": draft.id,
                "draft_status": draft.status,
                "source_mode": draft.source_mode,
                "title": draft_title(draft),
                "updated_at": draft.updated_at,
                "expires_at": draft.expires_at,
            }
            for draft in drafts
        ],
    }


@router.get("/{draft_id}")
async def get_draft(
    draft_id: str,
    request: Request,
    response: Response,
    schema_version: int = 1,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    draft = await _owned_draft(request, user, draft_id)
    if schema_version not in {1, 2}:
        raise _error(422, "subagent_draft_schema_unsupported")
    if schema_version == 2:
        space_id = await _active_space(request, user)
        if draft.entry_mode != "canvas" or draft.workspace_id != space_id:
            raise _error(404, "subagent_draft_not_found")
    elif draft.entry_mode == "canvas":
        raise _error(404, "subagent_draft_not_found")
    writable = await _request_writable(request)
    if draft.status == "expired":
        raise _error(
            410,
            "subagent_draft_expired",
            can_regenerate=(draft.entry_mode == "conversational" and draft.purpose_text is not None and writable),
            version=draft.row_version,
            etag=_etag(draft),
        )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["ETag"] = _etag(draft)
    if schema_version == 2:
        return _manual_draft_dto(
            draft,
            writable=writable and resolve_subagent_authoring_capabilities(user.team_id).unified_draft,
        )
    return _draft_dto(draft, writable=writable)


@router.post(
    "/{draft_id}/clarify",
    dependencies=[Depends(require_not_readonly)],
)
async def clarify_draft(
    draft_id: str,
    body: ClarifyDraftBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    draft = await _owned_draft(request, user, draft_id)
    if draft.entry_mode != "conversational":
        raise _error(404, "subagent_draft_not_found")
    expected = _expected_version(request, draft)
    try:
        result = await (await _service(request, user)).clarify_draft(
            draft_id=draft.id,
            team_id=user.team_id,
            owner_user_id=user.id,
            expected_version=expected,
            answer=body.answer,
            mcp_entitled=await is_feature_licensed_request(
                request,
                "mcp",
            ),
        )
    except SubagentDraftCreditError as exc:
        raise _error(402, exc.code) from exc
    except SubagentDraftVersionConflictError as exc:
        raise _error(
            409,
            "subagent_draft_version_conflict",
            current_version=exc.current_version,
        ) from exc
    except DraftServiceError as exc:
        status_code = {
            "subagent_draft_not_found": 404,
            "subagent_clarification_answer_invalid": 422,
            "subagent_draft_not_clarifiable": 409,
        }.get(exc.code, 503)
        raise _error(status_code, exc.code) from exc
    if result.draft is None:
        raise _error(503, "subagent_draft_result_missing")
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["ETag"] = _etag(result.draft)
    return _draft_dto(result.draft)


@router.post(
    "/{draft_id}/regenerate",
    dependencies=[Depends(require_not_readonly)],
)
async def regenerate_draft(
    draft_id: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    if idempotency_key is None:
        raise _error(428, "idempotency_key_required")
    draft = await _owned_draft(request, user, draft_id)
    if draft.entry_mode != "conversational":
        raise _error(404, "subagent_draft_not_found")
    _expected_version(request, draft)
    if draft.status != "expired":
        raise _error(409, "subagent_draft_not_expired")
    if draft.purpose_text is None:
        raise _error(
            410,
            "subagent_draft_expired",
            can_regenerate=False,
        )
    try:
        result = await (await _service(request, user)).create_intent_draft(
            team_id=user.team_id,
            owner_user_id=user.id,
            purpose=draft.purpose_text,
            locale=draft.locale,
            idempotency_key=idempotency_key,
            actor_active=True,
            writable=True,
            authoring_enabled=True,
            mcp_entitled=await is_feature_licensed_request(request, "mcp"),
            source_turn_id=draft.source_turn_id,
        )
    except SubagentDraftCreditError as exc:
        raise _error(402, exc.code) from exc
    except DraftServiceError as exc:
        if exc.code in {
            "user_credit_exhausted",
            "team_pool_exhausted",
        }:
            raise _error(402, exc.code) from exc
        raise _error(503, exc.code) from exc
    if result.outcome is DraftServiceOutcome.REQUEST_IN_PROGRESS:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "1"
        response.headers["Cache-Control"] = "no-store, private"
        return {
            "schema_version": 1,
            "kind": "subagent_draft_request_in_progress",
            "retry_after_ms": result.retry_after_ms,
            "expires_at": result.expires_at,
            "draft_id": None,
        }
    if result.draft is None:
        raise _error(503, "subagent_draft_result_missing")
    response.status_code = (
        status.HTTP_200_OK
        if result.outcome is DraftServiceOutcome.REPLAY
        else (status.HTTP_202_ACCEPTED if result.outcome is DraftServiceOutcome.NEEDS_CLARIFICATION else status.HTTP_201_CREATED)
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["ETag"] = _etag(result.draft)
    return _draft_dto(result.draft)


async def _review_update(
    *,
    body: PatchDraftBody,
    request: Request,
    user: User,
    draft: SubagentDraft,
    expected_version: int,
    persist: bool = True,
) -> SubagentDraft | dict[str, Any]:
    is_zh = draft.locale.casefold().startswith("zh")
    catalog = await SubagentAuthoringCatalogService().build(
        store=getattr(request.app.state, "user_store", None),
        team_id=user.team_id,
        actor_user_id=user.id,
        actor_active=True,
        mcp_entitled=await is_feature_licensed_request(request, "mcp"),
    )
    definition = {
        **body.definition.model_dump(),
        "tool_refs": list(body.selected_tool_refs),
        "visibility": "creator_only",
    }
    if draft.entry_mode == "canvas":
        definition["output_schema_text"] = (draft.definition or {}).get("output_schema_text", "")
    issues: list[dict[str, Any]] = []
    effective_tools = list(body.selected_tool_refs)
    catalog_fingerprint = catalog.fingerprint
    dependency_status = "not_run"
    warnings = [
        warning
        for warning in draft.warnings
        if not str(warning.get("code") or "").startswith(
            "subagent_risk_ack:",
        )
        and not ((body.selected_tool_refs or has_resource_selection(body.definition)) and warning.get("code") == TEXT_ONLY_CONFIRMATION)
    ]
    text_only = resolve_subagent_authoring_capabilities(user.team_id).text_only and any(
        warning.get("code") == TEXT_ONLY_CONFIRMATION for warning in warnings
    )
    validation_status: Literal["valid", "warning", "invalid"] = "warning" if text_only else "valid"
    draft_status: Literal["ready", "blocked"] = "ready"
    try:
        dependency = await draft_dependency_preflight(request, user, draft, definition, catalog.fingerprint)
        effective_tools = dependency["effective_tool_refs"]
        catalog_fingerprint = dependency["fingerprint"]
        try:
            validate_catalog_tool_refs(tuple(effective_tools), catalog=catalog, require_selectable=True)
            dependency_status = "passed"
        except TeamSubagentValidationError:
            dependency_status = "blocked"
        validate_candidate(
            TeamSubagentDefinitionCandidate(
                name=body.definition.name,
                display_name=body.definition.display_name,
                description=body.definition.description,
                system_prompt=body.definition.system_prompt,
                tool_refs=tuple(effective_tools),
                max_iterations=body.definition.max_iterations,
                skills=(),
                model=body.definition.model,
                output_schema=body.definition.output_schema,
                audience_mode="private",
            ),
            catalog=catalog,
            reserved_names=reserved_team_subagent_names(),
            operation="create",
            allow_empty_tools=text_only or has_resource_selection(body.definition),
        )
        existing = await request.app.state.user_store.get_team_subagent_by_name(
            user.team_id,
            body.definition.name,
            only_enabled=False,
        )
        if existing is not None:
            raise TeamSubagentValidationError(
                "subagent_name_conflict",
                "subagent name already exists",
                field="name",
            )
    except TeamSubagentValidationError as exc:
        if exc.field in {"skill_grants", "capability_ids", "builtin_plugin_ids", "tool_refs"}:
            dependency_status = "blocked"
        validation_status = "invalid"
        draft_status = "blocked"
        issues.append(
            {
                "schema_version": 1,
                "code": exc.code,
                "severity": "error",
                "field": exc.field,
                "tool_ref": exc.tool_ref,
                "message_key": f"subagent.validation.{exc.code}",
                "details": dict(exc.details),
            },
        )
    selected_tools = [catalog.tools_by_ref[tool_ref] for tool_ref in effective_tools if tool_ref in catalog.tools_by_ref]
    unmet = carry_forward_unmet_requirements(draft.validation_issues, selected_tools)
    if unmet:
        validation_status = "invalid"
        draft_status = "blocked"
        issues.extend(unmet)
    risky = sorted(tool.stable_ref for tool in selected_tools if tool.risk in {"external_write", "destructive"})
    if risky and validation_status != "invalid":
        code = _risk_acknowledgement_code(risky)
        warnings.append(
            {
                "code": code,
                "message": (
                    "所选工具可以修改外部系统，请确认后再创建。"
                    if is_zh
                    else "Selected tools can modify an external system. Confirm before creating."
                ),
                "risk_registry_version": RISK_REGISTRY_VERSION,
                "tool_count": len(risky),
            },
        )
        validation_status = "warning"
    original_recommendations = {
        str(recommendation.get("tool_ref")): recommendation for recommendation in draft.recommendations if recommendation.get("tool_ref")
    }
    recommendation_refs = set(original_recommendations)
    recommendation_refs.update(body.selected_tool_refs)
    recommendations: list[dict[str, Any]] = []
    for tool_ref in sorted(recommendation_refs):
        tool = catalog.tools_by_ref.get(tool_ref)
        if tool is None:
            continue
        original = original_recommendations.get(tool_ref, {})
        selected = tool_ref in body.selected_tool_refs
        was_selected = bool(original.get("selected")) or tool_ref in (draft.definition or {}).get("tool_refs", [])
        newly_selected = selected and not was_selected
        original_evidence = list(original.get("evidence") or [])
        evidence = ["user_selected"] if newly_selected else original_evidence
        reason = str(original.get("reason") or "")
        if newly_selected or (selected and not reason and "user_selected" in evidence):
            reason = "由你选择。" if is_zh else "Selected by you."
        elif not reason:
            reason = "根据当前目标建议。" if is_zh else "Suggested for this goal."
        recommendations.append(
            {
                "tool_ref": tool.stable_ref,
                # This route already localizes reasons using draft.locale; names use the same language axis. Builtin tools have both languages
                # in the catalog (tool_aliases.yaml); team tools without translations have empty display_name_en.
                "display_name": ((tool.display_name_zh or tool.display_name) if is_zh else (tool.display_name_en or tool.display_name)),
                "selected": selected,
                "risk": tool.risk,
                "source": {
                    "kind": tool.source_kind,
                    "display_name": tool.source_display_name,
                },
                "locked_reason": tool.for_selection().locked_reason,
                "approval_required": tool.approval_required,
                "evidence": evidence,
                "confidence": ("high" if newly_selected else str(original.get("confidence") or "low")),
                "tier": str(original.get("tier") or "recommended"),
                "reason": reason,
            },
        )
    review = {
        "definition": definition,
        "recommendations": recommendations,
        "assumptions": draft.assumptions,
        "warnings": warnings,
        "validation_issues": issues,
        "catalog_fingerprint": catalog_fingerprint,
        "dependency_validation": {"status": dependency_status, "fingerprint": catalog_fingerprint},
        "definition_fingerprint": definition_fingerprint(definition),
        "validation_status": validation_status,
        "status": draft_status,
    }
    if not persist:
        return review
    updated = await (await _draft_store(request)).update_review(
        draft.id,
        team_id=user.team_id,
        owner_user_id=user.id,
        expected_version=expected_version,
        definition=review["definition"],
        recommendations=review["recommendations"],
        assumptions=review["assumptions"],
        warnings=review["warnings"],
        validation_issues=review["validation_issues"],
        catalog_fingerprint=review["catalog_fingerprint"],
        definition_fingerprint=review["definition_fingerprint"],
        validation_status=review["validation_status"],
        status=review["status"],
    )
    if updated is None:
        raise _error(404, "subagent_draft_not_found")
    return updated


@router.post("/{draft_id}/suggestions", dependencies=[Depends(require_not_readonly)])
async def generate_canvas_suggestion(
    draft_id: str,
    body: GenerateCanvasSuggestionBody,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _require_unified_draft(user.team_id)
    if idempotency_key is None:
        raise _error(428, "idempotency_key_required")
    draft = await _owned_draft(request, user, draft_id)
    space_id = await _active_space(request, user)
    if draft.entry_mode != "canvas" or draft.workspace_id != space_id:
        raise _error(404, "subagent_draft_not_found")
    expected = _submitted_version(request, draft_id)
    try:
        result = await (await _service(request, user)).generate_canvas_suggestion(
            draft=draft,
            workspace_id=space_id,
            expected_version=expected,
            purpose=body.purpose,
            locale=body.locale,
            idempotency_key=idempotency_key,
            mcp_entitled=await is_feature_licensed_request(request, "mcp"),
        )
    except SubagentDraftCreditError as exc:
        raise _error(402, exc.code) from exc
    except DraftServiceError as exc:
        code = exc.code
        status_code = (
            409
            if code in {"idempotency_key_reused", "subagent_draft_version_conflict"}
            else 422
            if code in {"purpose_required", "subagent_purpose_invalid"}
            else 503
        )
        raise _error(status_code, code) from exc
    response.headers["Cache-Control"] = "no-store, private"
    if result.suggestion is None:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Retry-After"] = "1"
        return {
            "schema_version": 2,
            "kind": "subagent_canvas_suggestion_in_progress",
            "draft_id": draft_id,
            "retry_after_ms": result.retry_after_ms or 1000,
        }
    response.status_code = status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
    return _canvas_suggestion_dto(result.suggestion)


@router.get("/{draft_id}/suggestions")
async def list_canvas_suggestions(
    draft_id: str,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _require_unified_draft(user.team_id)
    draft = await _owned_draft(request, user, draft_id)
    space_id = await _active_space(request, user)
    if draft.entry_mode != "canvas" or draft.workspace_id != space_id:
        raise _error(404, "subagent_draft_not_found")
    suggestions = await (await _draft_store(request)).list_canvas_suggestions(
        draft_id,
        team_id=user.team_id,
        owner_user_id=user.id,
        workspace_id=space_id,
    )
    response.headers["Cache-Control"] = "no-store, private"
    return {"schema_version": 2, "kind": "team_subagent_canvas_suggestions", "items": [_canvas_suggestion_dto(item) for item in suggestions]}


@router.post("/{draft_id}/suggestions/{suggestion_id}/dismiss", dependencies=[Depends(require_not_readonly)])
async def dismiss_canvas_suggestion(
    draft_id: str,
    suggestion_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _require_unified_draft(user.team_id)
    draft = await _owned_draft(request, user, draft_id)
    space_id = await _active_space(request, user)
    if draft.entry_mode != "canvas" or draft.workspace_id != space_id:
        raise _error(404, "subagent_draft_not_found")
    store = await _draft_store(request)
    suggestion = await store.get_canvas_suggestion(
        draft_id,
        suggestion_id,
        team_id=user.team_id,
        owner_user_id=user.id,
        workspace_id=space_id,
    )
    if suggestion is None:
        raise _error(404, "subagent_draft_suggestion_not_found")
    if suggestion.status == "applied":
        raise _error(409, "subagent_draft_suggestion_already_applied")
    if suggestion.status == "pending" and not await store.dismiss_canvas_suggestion(
        draft_id,
        suggestion_id,
        team_id=user.team_id,
        owner_user_id=user.id,
        workspace_id=space_id,
    ):
        raise _error(409, "subagent_draft_suggestion_changed")
    return {"schema_version": 2, "kind": "team_subagent_canvas_suggestion_dismissed", "suggestion_id": suggestion_id}


@router.post("/{draft_id}/suggestions/{suggestion_id}/apply", dependencies=[Depends(require_not_readonly)])
async def apply_canvas_suggestion(
    draft_id: str,
    suggestion_id: str,
    body: AcceptCanvasSuggestionBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _require_unified_draft(user.team_id)
    draft = await _owned_draft(request, user, draft_id)
    space_id = await _active_space(request, user)
    if draft.entry_mode != "canvas" or draft.workspace_id != space_id:
        raise _error(404, "subagent_draft_not_found")
    expected = _submitted_version(request, draft_id)
    suggestion = await (await _draft_store(request)).get_canvas_suggestion(
        draft_id,
        suggestion_id,
        team_id=user.team_id,
        owner_user_id=user.id,
        workspace_id=space_id,
    )
    if suggestion is None or suggestion.kind != "definition":
        raise _error(404, "subagent_draft_suggestion_not_found")
    suggested = suggestion.payload.get("definition")
    allowed = {
        "name",
        "display_name",
        "description",
        "system_prompt",
        "tool_refs",
        "skill_grants",
        "capability_ids",
        "builtin_plugin_ids",
        "model",
        "output_schema_text",
    }
    selected = body.selected_fields
    if (
        not isinstance(suggested, dict)
        or len(set(selected)) != len(selected)
        or set(selected) - allowed
        or any(key not in suggested for key in selected)
    ):
        raise _error(422, "subagent_draft_suggestion_fields_invalid")
    merged = {
        "name": "",
        "display_name": "",
        "description": "",
        "system_prompt": "",
        "tool_refs": [],
        "max_iterations": 12,
        "model": "analyst",
        "output_schema": None,
        "output_schema_text": "",
        "visibility": "creator_only",
        **(draft.definition or {}),
    }
    merged.pop("skills", None)
    for key in selected:
        merged[key] = suggested[key]
        if key == "output_schema_text":
            merged["output_schema"] = suggested.get("output_schema")
    try:
        definition = ManualDefinitionBody.model_validate(merged).model_dump()
    except Exception as exc:
        raise _error(422, "subagent_draft_suggestion_fields_invalid") from exc
    purpose = draft.purpose_text or str(suggestion.payload.get("purpose") or "")
    try:
        updated, replayed = await (await _draft_store(request)).apply_canvas_suggestion(
            draft_id,
            suggestion_id,
            team_id=user.team_id,
            owner_user_id=user.id,
            workspace_id=space_id,
            expected_version=expected,
            definition=definition,
            purpose=purpose,
            selected_fields=selected,
            mutation_id=body.mutation_id,
        )
    except SubagentDraftVersionConflictError as exc:
        raise _error(409, "subagent_draft_version_conflict", current_version=exc.current_version) from exc
    except SubagentDraftIdempotencyConflictError as exc:
        raise _error(409, "subagent_draft_suggestion_already_applied") from exc
    except ValueError as exc:
        raise _error(404, "subagent_draft_suggestion_not_found") from exc
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["ETag"] = _etag(updated)
    result = _manual_draft_dto(updated, writable=True)
    result["replayed"] = replayed
    return result


@router.post(
    "/{draft_id}/manualize",
    dependencies=[Depends(require_not_readonly)],
)
async def convert_draft_to_manual(
    draft_id: str,
    body: PatchManualDraftBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    _require_unified_draft(user.team_id)
    draft = await _owned_draft(request, user, draft_id)
    space_id = await _active_space(request, user)
    expected = _submitted_version(request, draft.id)
    definition = body.definition.model_dump(exclude_unset=True)
    if len(json.dumps(definition, ensure_ascii=False)) > 60000:
        raise _error(422, "subagent_draft_definition_too_large")
    try:
        converted = await (await _draft_store(request)).convert_to_manual_draft(
            draft_id,
            team_id=user.team_id,
            owner_user_id=user.id,
            workspace_id=space_id,
            expected_version=expected,
            purpose=body.purpose,
            definition=definition,
            text_only_confirmed=body.text_only_confirmed,
        )
    except SubagentDraftVersionConflictError as exc:
        raise _error(409, "subagent_draft_version_conflict", current_version=exc.current_version) from exc
    except SubagentDraftIdempotencyConflictError as exc:
        raise _error(409, "subagent_draft_conversion_conflict") from exc
    except ValueError as exc:
        raise _error(409, "subagent_draft_not_convertible") from exc
    if converted is None:
        raise _error(404, "subagent_draft_not_found")
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["ETag"] = _etag(converted)
    return _manual_draft_dto(converted, writable=True)


@router.patch(
    "/{draft_id}",
    dependencies=[Depends(require_not_readonly)],
)
async def patch_draft(
    draft_id: str,
    body: PatchDraftBody | PatchManualDraftBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    draft = await _owned_draft(request, user, draft_id)
    if isinstance(body, PatchManualDraftBody):
        _require_unified_draft(user.team_id)
        space_id = await _active_space(request, user)
        if draft.entry_mode != "canvas" or draft.workspace_id != space_id:
            raise _error(404, "subagent_draft_not_found")
        expected = _expected_version(request, draft)
        definition = body.definition.model_dump(exclude_unset=True)
        if len(json.dumps(definition, ensure_ascii=False)) > 60000:
            raise _error(422, "subagent_draft_definition_too_large")
        try:
            updated_manual = await (await _draft_store(request)).save_manual_draft(
                draft_id,
                team_id=user.team_id,
                owner_user_id=user.id,
                workspace_id=space_id,
                expected_version=expected,
                purpose=body.purpose,
                definition=definition,
                text_only_confirmed=body.text_only_confirmed,
            )
        except SubagentDraftVersionConflictError as exc:
            raise _error(409, "subagent_draft_version_conflict", current_version=exc.current_version) from exc
        if updated_manual is None:
            raise _error(404, "subagent_draft_not_found")
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["ETag"] = _etag(updated_manual)
        return _manual_draft_dto(updated_manual, writable=True)
    if draft.entry_mode == "canvas":
        raise _error(404, "subagent_draft_not_found")
    expected = _expected_version(request, draft)
    try:
        updated = await _review_update(
            body=body,
            request=request,
            user=user,
            draft=draft,
            expected_version=expected,
        )
        if not isinstance(updated, SubagentDraft):
            raise _error(503, "subagent_draft_persist_failed")
    except SubagentDraftVersionConflictError as exc:
        raise _error(
            409,
            "subagent_draft_version_conflict",
            current_version=exc.current_version,
        ) from exc
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["ETag"] = _etag(updated)
    return _draft_dto(updated)


@router.post(
    "/{draft_id}/validate",
    dependencies=[Depends(require_not_readonly)],
)
async def validate_draft(
    draft_id: str,
    request: Request,
    response: Response,
    body: PatchDraftBody | None = None,
    user: User = Depends(_current_user),
) -> dict[str, Any]:
    draft = await _owned_draft(request, user, draft_id)
    if draft.entry_mode == "canvas":
        _require_unified_draft(user.team_id)
        if draft.workspace_id != await _active_space(request, user):
            raise _error(404, "subagent_draft_not_found")
        if body is not None:
            raise _error(422, "subagent_draft_save_before_validate")
        if draft.status not in {"editing", "ready", "blocked"} or draft.definition is None:
            raise _error(409, "subagent_draft_not_reviewable")
        definition = dict(draft.definition)
        schema_text = str(definition.get("output_schema_text") or "").strip()
        if schema_text:
            try:
                parsed_schema = json.loads(schema_text)
            except ValueError as exc:
                raise _error(422, "subagent_output_schema_invalid") from exc
            if not isinstance(parsed_schema, dict):
                raise _error(422, "subagent_output_schema_invalid")
            definition["output_schema"] = parsed_schema
        candidate = PatchDraftBody(
            schema_version=1,
            definition=DraftDefinitionBody.model_validate(
                {
                    **{key: definition[key] for key in RESOURCE_SELECTION_KEYS if key in definition},
                    **{
                        key: definition.get(key)
                        for key in (
                            "name",
                            "display_name",
                            "description",
                            "system_prompt",
                            "model",
                            "output_schema",
                            "visibility",
                        )
                    },
                }
            ),
            selected_tool_refs=list(definition.get("tool_refs") or []),
            selected_skills=[],
        )
        expected = _expected_version(request, draft)
        try:
            reviewed_manual = await _review_update(
                body=candidate,
                request=request,
                user=user,
                draft=draft,
                expected_version=expected,
            )
        except SubagentDraftVersionConflictError as exc:
            raise _error(409, "subagent_draft_version_conflict", current_version=exc.current_version) from exc
        if not isinstance(reviewed_manual, SubagentDraft):
            raise _error(503, "subagent_draft_persist_failed")
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["ETag"] = _etag(reviewed_manual)
        return _manual_draft_dto(reviewed_manual, writable=True)
    expected = _expected_version(request, draft)
    candidate = body
    if candidate is None:
        if draft.definition is None:
            raise _error(409, "subagent_draft_not_reviewable")
        candidate = PatchDraftBody(
            schema_version=1,
            definition=DraftDefinitionBody.model_validate(
                {
                    **{key: draft.definition[key] for key in RESOURCE_SELECTION_KEYS if key in draft.definition},
                    **{
                        key: draft.definition.get(key)
                        for key in (
                            "name",
                            "display_name",
                            "description",
                            "system_prompt",
                            "model",
                            "output_schema",
                            "visibility",
                        )
                    },
                },
            ),
            selected_tool_refs=list(
                draft.definition.get("tool_refs") or [],
            ),
            selected_skills=[],
        )
    try:
        reviewed = await _review_update(
            body=candidate,
            request=request,
            user=user,
            draft=draft,
            expected_version=expected,
            persist=body is None,
        )
    except SubagentDraftVersionConflictError as exc:
        raise _error(
            409,
            "subagent_draft_version_conflict",
            current_version=exc.current_version,
        ) from exc
    response.headers["Cache-Control"] = "no-store, private"
    if isinstance(reviewed, SubagentDraft):
        response.headers["ETag"] = _etag(reviewed)
        return _draft_dto(reviewed)
    response.headers["ETag"] = _etag(draft)
    return {
        "schema_version": 1,
        "kind": "team_subagent_draft_validation",
        "draft_id": draft.id,
        "persisted": False,
        "validation": {
            "status": reviewed["validation_status"],
            "issues": reviewed["validation_issues"],
            "catalog_fingerprint": reviewed["catalog_fingerprint"],
        },
        "definition_fingerprint": reviewed["definition_fingerprint"],
        "version": draft.row_version,
        "etag": _etag(draft),
    }


@router.post(
    "/{draft_id}/create",
    dependencies=[Depends(require_not_readonly)],
)
async def create_from_draft(
    draft_id: str,
    body: CreateFromDraftBody,
    request: Request,
    response: Response,
    user: User = Depends(_current_user),
    store: UserStore = Depends(_store),
) -> dict[str, Any]:
    draft = await _owned_draft(request, user, draft_id)
    if draft.entry_mode == "canvas":
        _require_unified_draft(user.team_id)
        if body.schema_version != 2:
            raise _error(422, "subagent_draft_schema_unsupported")
        if draft.workspace_id != await _active_space(request, user):
            raise _error(404, "subagent_draft_not_found")
    elif body.schema_version != 1:
        raise _error(422, "subagent_draft_schema_unsupported")
    if draft.status == "consumed" and draft.consumed_definition_id:
        if body.mutation_id != draft.mutation_id:
            raise _error(409, "subagent_draft_mutation_mismatch")
        existing = await store.get_team_subagent(
            user.team_id,
            draft.consumed_definition_id,
        )
        if existing is None:
            raise _error(409, "subagent_draft_result_missing")
        response.headers["Cache-Control"] = "no-store, private"
        return {
            "schema_version": body.schema_version,
            "kind": "team_subagent_created_from_draft",
            "replayed": True,
            "draft_id": draft.id,
            "definition_id": existing.id,
        }
    expected = _expected_version(request, draft)
    if (
        draft.status != "ready"
        or draft.validation_status not in {"valid", "warning"}
        or draft.definition is None
        or draft.definition_fingerprint is None
    ):
        raise _error(409, "subagent_draft_not_ready")
    if body.mutation_id != draft.mutation_id:
        raise _error(409, "subagent_draft_mutation_mismatch")
    definition = draft.definition
    tool_refs = list(definition.get("tool_refs") or [])
    if any(ref.startswith("teammcp.") for ref in tool_refs):
        await assert_feature_licensed(request, "mcp")
    catalog = await SubagentAuthoringCatalogService().build(
        store=store,
        team_id=user.team_id,
        actor_user_id=user.id,
        actor_active=True,
        mcp_entitled=await is_feature_licensed_request(request, "mcp"),
    )
    try:
        dependency = await draft_dependency_preflight(request, user, draft, definition, catalog.fingerprint)
    except TeamSubagentValidationError as exc:
        raise _error(422, exc.code, field=exc.field, **exc.details) from exc
    if dependency["fingerprint"] != draft.catalog_fingerprint:
        raise _error(
            409,
            "subagent_catalog_changed",
            current_catalog_fingerprint=dependency["fingerprint"],
        )
    try:
        validate_catalog_tool_refs(
            dependency["effective_tool_refs"],
            catalog=catalog,
            require_selectable=True,
        )
        validate_candidate(
            TeamSubagentDefinitionCandidate(
                name=str(definition["name"]),
                display_name=str(definition.get("display_name") or ""),
                description=str(definition.get("description") or ""),
                system_prompt=str(definition.get("system_prompt") or ""),
                tool_refs=tuple(dependency["effective_tool_refs"]),
                max_iterations=int(
                    definition.get("max_iterations") or 10,
                ),
                skills=(),
                model=definition.get("model"),
                output_schema=definition.get("output_schema"),
                audience_mode="private",
            ),
            catalog=catalog,
            reserved_names=reserved_team_subagent_names(),
            operation="create",
            allow_empty_tools=has_resource_selection(definition)
            or (
                resolve_subagent_authoring_capabilities(user.team_id).text_only
                and any(warning.get("code") == TEXT_ONLY_CONFIRMATION for warning in draft.warnings)
            ),
        )
    except TeamSubagentValidationError as exc:
        raise _error(422, exc.code, field=exc.field) from exc
    required_acknowledgements = {str(warning.get("code")) for warning in draft.warnings if warning.get("code")}
    if not required_acknowledgements.issubset(
        set(body.acknowledged_warning_codes),
    ):
        raise _error(
            422,
            "subagent_warning_acknowledgement_required",
            required=sorted(required_acknowledgements),
        )
    now = _now()
    subagent = TeamSubagent(
        id=_id("osa"),
        team_id=user.team_id,
        name=str(definition["name"]),
        display_name=str(definition.get("display_name") or ""),
        description=str(definition.get("description") or ""),
        system_prompt=str(definition.get("system_prompt") or ""),
        tool_refs=tool_refs,
        max_iterations=int(definition.get("max_iterations") or 10),
        skills=[],
        model=definition.get("model"),
        output_schema=definition.get("output_schema"),
        visibility="creator_only",
        created_by=user.id,
        status="enabled",
        created_at=now,
        updated_at=now,
    )
    origin_mode = {
        "intent": "intent_draft",
        "turn": "turn_draft",
        "manual": "manual",
    }[draft.source_mode]
    try:
        mutation_store = TeamSubagentStore(store.db_path)
        revision_kwargs: dict[str, Any] = {}
        if await mutation_store.release_semantics_active():
            from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler

            from bowhead_gateway.routes.agents import _revision_store

            exact_skills = await AgentDependencyCompiler().exact_grants_for_legacy_names(
                (),
                team_id=user.team_id,
                tool_refs=subagent.tool_refs,
            )
            revision_kwargs = {
                "revision_store": await _revision_store(request, user=user, store=store),
                "revision_definition": {
                    "max_iterations": subagent.max_iterations,
                    "model": subagent.model,
                    "output_schema": subagent.output_schema,
                    "skill_grants": _skill_grants_for_revision(definition, subagent.tool_refs)
                    or [grant.to_canonical_dict() for grant in exact_skills],
                    "capability_ids": definition.get("capability_ids", []),
                    **({"builtin_plugin_ids": definition["builtin_plugin_ids"]} if definition.get("builtin_plugin_ids") else {}),
                    "system_prompt": subagent.system_prompt,
                    "tool_refs": subagent.tool_refs,
                },
                "revision_listing": {
                    "description": subagent.description,
                    "display_name": subagent.display_name or subagent.name,
                },
                "revision_target_audience": AgentAudience("private"),
            }

        async def validate_source(db: aiosqlite.Connection) -> None:
            if not definition.get("skill_grants") and not definition.get("capability_ids"):
                return
            current_catalog = await SubagentAuthoringCatalogService().build(
                store=store,
                team_id=user.team_id,
                actor_user_id=user.id,
                actor_active=True,
                mcp_entitled=await is_feature_licensed_request(request, "mcp"),
            )
            current = await draft_dependency_preflight(request, user, draft, definition, current_catalog.fingerprint, connection=db)
            if current["fingerprint"] != draft.catalog_fingerprint:
                raise TeamSubagentDraftNotConsumableError(code="subagent_catalog_changed")

        await mutation_store.create_team_subagent(
            subagent,
            actor_user_id=user.id,
            audit_action=AuditAction.TEAM_SUBAGENT_CREATED,
            audit_payload={
                "source": "canvas_draft" if draft.entry_mode == "canvas" else "conversational_draft",
                "draft_id": draft.id,
                "origin_mode": origin_mode,
            },
            require_active_owner=True,
            origin_mode=origin_mode,
            source_draft_id=draft.id,
            source_draft_expected_version=expected,
            source_definition_fingerprint=draft.definition_fingerprint,
            source_draft_workspace_id=draft.workspace_id,
            source_dependency_check=validate_source,
            **revision_kwargs,
        )
    except TeamSubagentNameConflictError as exc:
        raise _error(409, "subagent_name_conflict") from exc
    except TeamSubagentOwnerInactiveError as exc:
        raise _error(403, "subagent_owner_inactive") from exc
    except TeamSubagentDraftNotConsumableError as exc:
        raise _error(409, exc.code) from exc
    except TeamSubagentRevisionWriterRequiredError as exc:
        raise _error(409, exc.code) from exc
    except AgentPublicationBlockedError as exc:
        raise _error(422, exc.code, reason_codes=list(exc.reason_codes), blocked_items=list(exc.blocked_items)) from exc
    except TeamSubagentValidationError as exc:
        raise _error(422, exc.code, field=exc.field, **exc.details) from exc
    except (AgentRevisionError, ValueError) as exc:
        raise _error(422, getattr(exc, "code", "agent_definition_invalid"), **(getattr(exc, "details", None) or {})) from exc
    response.status_code = status.HTTP_201_CREATED
    response.headers["Cache-Control"] = "no-store, private"
    return {
        "schema_version": body.schema_version,
        "kind": "team_subagent_created_from_draft",
        "replayed": False,
        "draft_id": draft.id,
        "definition_id": subagent.id,
        "visibility": "creator_only",
        "status": "enabled",
        "lifecycle_state": "active",
    }


@router.delete(
    "/{draft_id}",
    dependencies=[Depends(require_not_readonly)],
)
async def discard_draft(
    draft_id: str,
    request: Request,
    user: User = Depends(_current_user),
) -> Response:
    draft = await _owned_draft(request, user, draft_id)
    if draft.entry_mode == "canvas":
        _require_unified_draft(user.team_id)
        if draft.workspace_id != await _active_space(request, user):
            raise _error(404, "subagent_draft_not_found")
    expected = _expected_version(request, draft)
    try:
        discarded = await (await _draft_store(request)).discard_draft(
            draft.id,
            team_id=user.team_id,
            owner_user_id=user.id,
            expected_version=expected,
        )
    except SubagentDraftVersionConflictError as exc:
        raise _error(
            409,
            "subagent_draft_version_conflict",
            current_version=exc.current_version,
        ) from exc
    if discarded is None:
        raise _error(404, "subagent_draft_not_found")
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "no-store, private"},
    )


def _skill_grants_for_revision(definition: dict[str, Any], tool_refs: list[str]) -> list[dict[str, Any]]:
    """The draft's own Skill grants plus the Python sandbox grant a directly selected Tool requests.

    The dependency preflight derives the same grant, so the stored revision matches what was checked.
    """
    from bowhead_core.agent_dependency_compiler import with_python_sandbox_skill_grant

    grants = list(definition.get("skill_grants") or [])
    if not grants:
        return []
    return list(with_python_sandbox_skill_grant({"tool_refs": tool_refs, "skill_grants": grants})["skill_grants"])


def _risk_acknowledgement_code(tool_refs: list[str]) -> str:
    digest = hashlib.sha256("\0".join(tool_refs).encode()).hexdigest()[:12]
    return f"subagent_risk_ack:{RISK_REGISTRY_VERSION}:{digest}"


__all__ = ["router"]
