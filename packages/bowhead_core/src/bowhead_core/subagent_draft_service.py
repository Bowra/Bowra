"""Use-case orchestration for conversational Subagent draft creation."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import uuid4

import anyio

from bowhead_core.agents.manifest_security import lint_user_append
from bowhead_core.custom_subagent_store import RESERVED_NAMES
from bowhead_core.subagent import PROFILES
from bowhead_core.subagent_authoring_catalog import (
    AuthoringTool,
    SubagentAuthoringCatalogService,
    SubagentAuthoringCatalogSnapshot,
)
from bowhead_core.subagent_authoring_models import DraftBlueprint
from bowhead_core.subagent_authoring_rollout import resolve_subagent_authoring_capabilities
from bowhead_core.subagent_capability_resolver import (
    SubagentCapabilityResolver,
    UnmetRequirement,
)
from bowhead_core.subagent_draft_generator import (
    SubagentDraftGenerationError,
    SubagentDraftGenerator,
)
from bowhead_core.subagent_draft_store import (
    CanvasDraftSuggestion,
    DraftClaimOutcome,
    SubagentDraft,
    SubagentDraftCreate,
    SubagentDraftStore,
    SubagentDraftVersionConflictError,
)
from bowhead_core.subagent_purpose import (
    PurposeEvidence,
    PurposeGateOutcome,
    evaluate_purpose_evidence,
)
from bowhead_core.team_subagent_validation import (
    MAX_SYSTEM_PROMPT,
    TeamSubagentDefinitionCandidate,
    TeamSubagentValidationError,
    validate_candidate,
)
from bowhead_core.user_store import _id

logger = logging.getLogger(__name__)

# Server-issued review requirement, never accepted from generated warning codes.
TEXT_ONLY_CONFIRMATION = "subagent_text_only_confirmation"
# A required drafting capability has no selected tool. Issued server-side when generating the draft; after edits,
# carry_forward_unmet_requirements checks current selections to determine whether the gap was filled.
REQUIRED_CAPABILITY_UNMET = "subagent_required_capability_unmet"

_SYSTEM_RESERVED = {
    "compute",
    "modeler",
    "debate",
    "analyst",
    "echo",
    "lead",
    "consultant",
    "kb-analyst",
    "canvas-writer",
    "financial-analyst",
    "business-analyst",
    "compliance-analyst",
    "meta-agent",
    "narrative",
    "composer",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SECRET_MARKER_RE = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|x-api-key|access[_ -]?token|"
    r"client[_ -]?secret|password|mcp[_ -]?(?:token|header))\b"
    r"[\"']?\s*[:=]\s*[\"']?[a-z0-9_~+./=-]{4,}|"
    r"\bbearer\s+[a-z0-9._~+/-]{8,}|"
    r"\bauthorization\s*[:=]\s*basic\s+[a-z0-9+/=]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
)
_SECRET_TOKEN_RE = re.compile(r"[A-Za-z0-9_+/=-]{32,}")


class DraftServiceOutcome(StrEnum):
    PURPOSE_REQUIRED = "purpose_required"
    PURPOSE_INSUFFICIENT = "purpose_insufficient"
    MANUAL = "manual"
    CANCELLED = "cancelled"
    REQUEST_IN_PROGRESS = "request_in_progress"
    REPLAY = "replay"
    NEEDS_CLARIFICATION = "needs_clarification"
    DRAFT_READY = "draft_ready"
    DRAFT_BLOCKED = "draft_blocked"


class DraftServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DraftServiceResult:
    outcome: DraftServiceOutcome
    draft: SubagentDraft | None = None
    outcome_code: str | None = None
    retry_after_ms: int | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class CanvasGenerationResult:
    suggestion: CanvasDraftSuggestion | None = None
    retry_after_ms: int | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class DraftReviewMaterialization:
    definition: dict[str, Any]
    recommendations: list[dict[str, Any]]
    assumptions: list[str]
    warnings: list[dict[str, Any]]
    validation_issues: list[dict[str, Any]]
    catalog_fingerprint: str
    definition_fingerprint: str
    validation_status: str
    draft_status: str


class DefinitionStore(Protocol):
    async def get_team_subagent_by_name(
        self,
        team_id: str,
        name: str,
        *,
        only_enabled: bool = ...,
    ) -> Any: ...


class DraftUsageAccountant(Protocol):
    async def preflight(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
    ) -> None: ...

    async def settle(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        draft_id: str | None,
        input_tokens: int,
        output_tokens: int,
        attempts: int,
        model_usage: tuple[Any, ...] = (),
    ) -> None: ...

    async def prepare_settlement(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        draft_id: str | None,
        input_tokens: int,
        output_tokens: int,
        attempts: int,
        model_usage: tuple[Any, ...] = (),
    ) -> None: ...

    async def release(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        reason_code: str,
    ) -> None: ...


class NoopDraftUsageAccountant:
    async def preflight(self, **kwargs: Any) -> None:
        return None

    async def settle(self, **kwargs: Any) -> None:
        return None

    async def prepare_settlement(self, **kwargs: Any) -> None:
        return None

    async def release(self, **kwargs: Any) -> None:
        return None


class SubagentDraftService:
    def __init__(
        self,
        *,
        draft_store: SubagentDraftStore,
        definition_store: DefinitionStore,
        catalog_service: SubagentAuthoringCatalogService,
        generator: SubagentDraftGenerator,
        accountant: DraftUsageAccountant | None = None,
        capability_resolver: SubagentCapabilityResolver | None = None,
    ) -> None:
        self._drafts = draft_store
        self._definitions = definition_store
        self._catalog = catalog_service
        self._generator = generator
        self._accountant = accountant or NoopDraftUsageAccountant()
        self._resolver = capability_resolver or SubagentCapabilityResolver()

    async def discard_undelivered_draft(
        self,
        *,
        draft_id: str,
        team_id: str,
        owner_user_id: str,
        expected_version: int,
    ) -> SubagentDraft | None:
        """Scrub an undiscoverable Chat draft and close its request claim."""
        return await self._drafts.discard_draft(
            draft_id,
            team_id=team_id,
            owner_user_id=owner_user_id,
            expected_version=expected_version,
            request_outcome_code="delivery_failed",
        )

    async def create_chat_canvas_draft(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        workspace_id: str,
        source_turn_id: str,
        purpose: str,
        locale: str,
        idempotency_key: str,
    ) -> SubagentDraft:
        """Persist the admitted author's task before conversational generation."""
        if not resolve_subagent_authoring_capabilities(team_id).unified_draft:
            raise DraftServiceError("subagent_unified_draft_unavailable")
        return await self._drafts.create_manual_draft(
            team_id=team_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            locale=locale,
            purpose=purpose,
            idempotency_key=idempotency_key,
            source_mode="turn",
            source_turn_id=source_turn_id,
        )

    async def generate_canvas_suggestion(
        self,
        *,
        draft: SubagentDraft,
        workspace_id: str,
        expected_version: int,
        purpose: str,
        locale: str,
        idempotency_key: str,
        mcp_entitled: bool,
    ) -> CanvasGenerationResult:
        """Generate an auditable suggestion for an existing Canvas task."""
        if draft.entry_mode != "canvas" or draft.workspace_id != workspace_id:
            raise DraftServiceError("subagent_draft_not_found")
        gate = evaluate_purpose_evidence(
            purpose,
            PurposeEvidence(source_or_object=True, action=True, expected_result=True),
        )
        if gate.outcome is PurposeGateOutcome.PURPOSE_REQUIRED:
            raise DraftServiceError("purpose_required")
        if gate.outcome is not PurposeGateOutcome.SUFFICIENT:
            raise DraftServiceError("subagent_purpose_invalid")
        claim = await self._drafts.claim_request(
            team_id=draft.team_id,
            owner_user_id=draft.owner_user_id,
            idempotency_key=idempotency_key,
            request={
                "schema_version": 2,
                "draft_id": draft.id,
                "workspace_id": workspace_id,
                "base_version": expected_version,
                "purpose": gate.normalized_purpose,
                "locale": locale,
                "kind": "canvas_suggestion",
            },
        )
        if claim.outcome is DraftClaimOutcome.CONFLICT:
            raise DraftServiceError("idempotency_key_reused")
        if claim.outcome is DraftClaimOutcome.IN_PROGRESS:
            return CanvasGenerationResult(retry_after_ms=1000)
        if claim.outcome is DraftClaimOutcome.REPLAY:
            suggestion = await self._drafts.get_canvas_suggestion_for_request(
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                workspace_id=workspace_id,
                request_key_hash=claim.idempotency_key_hash,
            )
            if suggestion is None:
                raise DraftServiceError(claim.outcome_code or "subagent_draft_result_missing")
            return CanvasGenerationResult(suggestion=suggestion, replayed=True)
        if draft.row_version != expected_version:
            await self._drafts.complete_request(
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code="subagent_draft_version_conflict",
                result_draft_id=None,
                failed=True,
            )
            raise DraftServiceError("subagent_draft_version_conflict")
        try:
            await self._accountant.preflight(
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "")) or "subagent_draft_credit_preflight_failed"
            await self._drafts.complete_request(
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code=code,
                result_draft_id=None,
                failed=True,
            )
            raise
        generation, catalog, generation_error, catalog_error = await self._generate_with_catalog(
            team_id=draft.team_id,
            owner_user_id=draft.owner_user_id,
            purpose=gate.normalized_purpose,
            locale=locale,
            trace_id=claim.idempotency_key_hash[:16],
            mcp_entitled=mcp_entitled,
        )
        if generation_error is not None or catalog_error is not None or generation is None or catalog is None:
            code = generation_error.code if generation_error is not None else "subagent_draft_catalog_unavailable"
            if generation is not None:
                await self._settle_usage(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    request_key_hash=claim.idempotency_key_hash,
                    draft_id=draft.id,
                    generation=generation,
                )
            elif generation_error is not None and (generation_error.input_tokens or generation_error.output_tokens):
                await self._settle_usage(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    request_key_hash=claim.idempotency_key_hash,
                    draft_id=draft.id,
                    generation=generation_error,
                )
            else:
                await self._accountant.release(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    request_key_hash=claim.idempotency_key_hash,
                    reason_code=code,
                )
            await self._drafts.complete_request(
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code=code,
                result_draft_id=None,
                failed=True,
            )
            raise DraftServiceError(code)
        assessment = generation.assessment
        if assessment.outcome in {"insufficient", "ambiguous"}:
            kind: Literal["definition", "clarification"] = "clarification"
            payload = {
                "question": assessment.clarification_question
                or (
                    "请补充要处理的对象、动作和期望结果。"
                    if locale.casefold().startswith("zh")
                    else "Please add the object, action, and expected result."
                ),
            }
        else:
            if generation.blueprint is None:
                await self._settle_usage(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    request_key_hash=claim.idempotency_key_hash,
                    draft_id=draft.id,
                    generation=generation,
                )
                await self._drafts.complete_request(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    idempotency_key_hash=claim.idempotency_key_hash,
                    lease_token=claim.lease_token,
                    outcome_code="subagent_draft_schema_invalid",
                    result_draft_id=None,
                    failed=True,
                )
                raise DraftServiceError("subagent_draft_schema_invalid")
            try:
                review = await self.materialize_blueprint(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    purpose=gate.normalized_purpose,
                    locale=locale,
                    blueprint=generation.blueprint,
                    mcp_entitled=mcp_entitled,
                    catalog=catalog,
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, DraftServiceError) else "subagent_draft_materialization_failed"
                await self._settle_usage(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    request_key_hash=claim.idempotency_key_hash,
                    draft_id=draft.id,
                    generation=generation,
                )
                await self._drafts.complete_request(
                    team_id=draft.team_id,
                    owner_user_id=draft.owner_user_id,
                    idempotency_key_hash=claim.idempotency_key_hash,
                    lease_token=claim.lease_token,
                    outcome_code=code,
                    result_draft_id=None,
                    failed=True,
                )
                raise DraftServiceError(code) from exc
            kind = "definition"
            definition = {key: value for key, value in review.definition.items() if key != "skills"}
            definition["output_schema_text"] = (
                json.dumps(definition["output_schema"], ensure_ascii=False, indent=2) if definition.get("output_schema") else ""
            )
            payload = {
                "definition": definition,
                "recommendations": review.recommendations,
                "assumptions": review.assumptions,
                "warnings": review.warnings,
                "validation_issues": review.validation_issues,
            }
        try:
            suggestion = await self._drafts.record_canvas_suggestion(
                draft.id,
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                workspace_id=workspace_id,
                request_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                base_version=expected_version,
                kind=kind,
                payload=payload,
            )
        except Exception as exc:
            await self._settle_usage(
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
                draft_id=draft.id,
                generation=generation,
            )
            await self._drafts.complete_request(
                team_id=draft.team_id,
                owner_user_id=draft.owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code="subagent_draft_delivery_failed",
                result_draft_id=None,
                failed=True,
            )
            raise DraftServiceError("subagent_draft_delivery_failed") from exc
        await self._settle_usage(
            team_id=draft.team_id,
            owner_user_id=draft.owner_user_id,
            request_key_hash=claim.idempotency_key_hash,
            draft_id=draft.id,
            generation=generation,
        )
        return CanvasGenerationResult(suggestion=suggestion)

    async def create_intent_draft(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        purpose: str,
        locale: str,
        idempotency_key: str,
        actor_active: bool,
        writable: bool,
        authoring_enabled: bool,
        mcp_entitled: bool,
        source_turn_id: str | None = None,
    ) -> DraftServiceResult:
        if not actor_active:
            raise DraftServiceError("team_membership_required")
        if not writable:
            raise DraftServiceError("team_readonly")
        if not authoring_enabled:
            raise DraftServiceError("subagent_authoring_disabled")

        # This deterministic pass only stops cases that need no semantic model:
        # absent/generic, manual, cancel, or over-limit. Any non-generic purpose
        # proceeds to the model's evidence assessment.
        gate = evaluate_purpose_evidence(
            purpose,
            PurposeEvidence(
                source_or_object=True,
                action=True,
                expected_result=True,
            ),
        )
        if gate.outcome is PurposeGateOutcome.PURPOSE_REQUIRED:
            return DraftServiceResult(
                DraftServiceOutcome.PURPOSE_REQUIRED,
                outcome_code=gate.outcome.value,
            )
        if gate.outcome is PurposeGateOutcome.MANUAL:
            return DraftServiceResult(
                DraftServiceOutcome.MANUAL,
                outcome_code=gate.outcome.value,
            )
        if gate.outcome is PurposeGateOutcome.CANCELLED:
            return DraftServiceResult(
                DraftServiceOutcome.CANCELLED,
                outcome_code=gate.outcome.value,
            )
        if gate.outcome is PurposeGateOutcome.TOO_LONG:
            raise DraftServiceError("subagent_purpose_too_long")

        source_mode = "turn" if source_turn_id else "intent"
        canonical_request = {
            "schema_version": 1,
            "source_mode": source_mode,
            "purpose": gate.normalized_purpose,
            "source_turn_id": source_turn_id,
            "locale": locale,
        }
        claim = await self._drafts.claim_request(
            team_id=team_id,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            request=canonical_request,
        )
        if claim.outcome is DraftClaimOutcome.CONFLICT:
            raise DraftServiceError("idempotency_key_reused")
        if claim.outcome is DraftClaimOutcome.IN_PROGRESS:
            return DraftServiceResult(
                DraftServiceOutcome.REQUEST_IN_PROGRESS,
                retry_after_ms=1000,
                expires_at=claim.expires_at,
            )
        if claim.outcome is DraftClaimOutcome.REPLAY:
            draft = (
                await self._drafts.get_draft(
                    claim.result_draft_id,
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                )
                if claim.result_draft_id
                else None
            )
            if draft is not None:
                return DraftServiceResult(
                    DraftServiceOutcome.REPLAY,
                    draft=draft,
                    outcome_code=claim.outcome_code,
                    expires_at=claim.expires_at,
                )
            outcome = _purpose_outcome(claim.outcome_code)
            return DraftServiceResult(
                outcome,
                outcome_code=claim.outcome_code,
                expires_at=claim.expires_at,
            )

        try:
            await self._accountant.preflight(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "")) or ("subagent_draft_credit_preflight_failed")
            await self._drafts.complete_request(
                team_id=team_id,
                owner_user_id=owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code=code,
                result_draft_id=None,
                failed=True,
            )
            raise
        generation, catalog, generation_error, catalog_error = await self._generate_with_catalog(
            team_id=team_id,
            owner_user_id=owner_user_id,
            purpose=gate.normalized_purpose,
            locale=locale,
            trace_id=claim.idempotency_key_hash[:16],
            mcp_entitled=mcp_entitled,
        )
        if generation_error is not None:
            if generation_error.input_tokens or generation_error.output_tokens:
                await self._settle_usage(
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    request_key_hash=claim.idempotency_key_hash,
                    draft_id=None,
                    generation=generation_error,
                )
            else:
                await self._accountant.release(
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    request_key_hash=claim.idempotency_key_hash,
                    reason_code=generation_error.code,
                )
            await self._drafts.complete_request(
                team_id=team_id,
                owner_user_id=owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code=generation_error.code,
                result_draft_id=None,
                failed=True,
            )
            raise DraftServiceError(generation_error.code) from generation_error
        if generation is None:
            raise DraftServiceError("subagent_draft_generation_missing")
        if catalog_error is not None or catalog is None:
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
                draft_id=None,
                generation=generation,
            )
            code = "subagent_draft_catalog_unavailable"
            await self._drafts.complete_request(
                team_id=team_id,
                owner_user_id=owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code=code,
                result_draft_id=None,
                failed=True,
            )
            raise DraftServiceError(code) from catalog_error

        assessment = generation.assessment
        if assessment.outcome == "insufficient":
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
                draft_id=None,
                generation=generation,
            )
            await self._drafts.complete_request(
                team_id=team_id,
                owner_user_id=owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code="purpose_insufficient",
                result_draft_id=None,
            )
            return DraftServiceResult(
                DraftServiceOutcome.PURPOSE_INSUFFICIENT,
                outcome_code="purpose_insufficient",
                expires_at=claim.expires_at,
            )

        draft_id = _id("sad")
        mutation_id = str(uuid4())
        if assessment.outcome == "ambiguous":
            draft = await self._drafts.create_draft(
                SubagentDraftCreate(
                    id=draft_id,
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    source_mode=source_mode,
                    source_turn_id=source_turn_id,
                    status="needs_clarification",
                    validation_status="unknown",
                    locale=locale,
                    purpose_text=gate.normalized_purpose,
                    goal_summary=None,
                    definition=None,
                    recommendations=None,
                    assumptions=[],
                    warnings=[],
                    clarification={
                        "schema_version": 1,
                        "question": assessment.clarification_question or "Please clarify the main data source or expected result.",
                        "answered": False,
                    },
                    validation_issues=[],
                    catalog_fingerprint=None,
                    definition_fingerprint=None,
                    idempotency_key_hash=claim.idempotency_key_hash,
                    mutation_id=mutation_id,
                    business_clarification_count=0,
                    outcome_reason_code=assessment.reason_code,
                ),
            )
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
                draft_id=draft.id,
                generation=generation,
            )
            await self._drafts.complete_request(
                team_id=team_id,
                owner_user_id=owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code="needs_clarification",
                result_draft_id=draft.id,
            )
            return DraftServiceResult(
                DraftServiceOutcome.NEEDS_CLARIFICATION,
                draft=draft,
                expires_at=draft.expires_at,
            )

        blueprint = generation.blueprint
        if blueprint is None:
            raise DraftServiceError("subagent_draft_schema_invalid")
        try:
            review = await self.materialize_blueprint(
                team_id=team_id,
                owner_user_id=owner_user_id,
                purpose=gate.normalized_purpose,
                locale=locale,
                blueprint=blueprint,
                mcp_entitled=mcp_entitled,
                catalog=catalog,
            )
        except Exception as exc:
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
                draft_id=None,
                generation=generation,
            )
            code = exc.code if isinstance(exc, DraftServiceError) else "subagent_draft_materialization_failed"
            await self._drafts.complete_request(
                team_id=team_id,
                owner_user_id=owner_user_id,
                idempotency_key_hash=claim.idempotency_key_hash,
                lease_token=claim.lease_token,
                outcome_code=code,
                result_draft_id=None,
                failed=True,
            )
            if isinstance(exc, DraftServiceError):
                raise
            raise DraftServiceError(code) from exc
        try:
            draft = await self._drafts.create_draft(
                SubagentDraftCreate(
                    id=draft_id,
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    source_mode=source_mode,
                    source_turn_id=source_turn_id,
                    status=review.draft_status,
                    validation_status=review.validation_status,
                    locale=locale,
                    purpose_text=gate.normalized_purpose,
                    goal_summary=blueprint.goal_summary,
                    definition=review.definition,
                    recommendations=review.recommendations,
                    assumptions=review.assumptions,
                    warnings=review.warnings,
                    clarification=None,
                    validation_issues=review.validation_issues,
                    catalog_fingerprint=review.catalog_fingerprint,
                    definition_fingerprint=review.definition_fingerprint,
                    idempotency_key_hash=claim.idempotency_key_hash,
                    mutation_id=mutation_id,
                    outcome_reason_code=assessment.reason_code,
                ),
            )
        except Exception as exc:
            # create_draft commits before re-reading the result. A failure is
            # therefore commit-uncertain: keep the request assessing so retry
            # can first recover a row by idempotency hash instead of replacing
            # it with a terminal failure and orphaning a committed draft.
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=claim.idempotency_key_hash,
                draft_id=None,
                generation=generation,
            )
            raise DraftServiceError(
                "subagent_draft_persistence_failed",
            ) from exc
        await self._settle_usage(
            team_id=team_id,
            owner_user_id=owner_user_id,
            request_key_hash=claim.idempotency_key_hash,
            draft_id=draft.id,
            generation=generation,
        )
        await self._drafts.complete_request(
            team_id=team_id,
            owner_user_id=owner_user_id,
            idempotency_key_hash=claim.idempotency_key_hash,
            lease_token=claim.lease_token,
            outcome_code=("draft_ready" if draft.status == "ready" else "draft_blocked"),
            result_draft_id=draft.id,
        )
        return DraftServiceResult(
            (DraftServiceOutcome.DRAFT_READY if draft.status == "ready" else DraftServiceOutcome.DRAFT_BLOCKED),
            draft=draft,
            expires_at=draft.expires_at,
        )

    async def clarify_draft(
        self,
        *,
        draft_id: str,
        team_id: str,
        owner_user_id: str,
        expected_version: int | None,
        answer: str,
        mcp_entitled: bool,
    ) -> DraftServiceResult:
        normalized_answer = " ".join(answer.split())
        if not normalized_answer or len(normalized_answer) > 2000:
            raise DraftServiceError("subagent_clarification_answer_invalid")
        draft = await self._drafts.get_draft(
            draft_id,
            team_id=team_id,
            owner_user_id=owner_user_id,
        )
        if draft is None:
            raise DraftServiceError("subagent_draft_not_found")
        if expected_version is not None and draft.row_version != expected_version:
            raise SubagentDraftVersionConflictError(draft.row_version)
        if draft.status != "needs_clarification" or draft.business_clarification_count != 0 or draft.purpose_text is None:
            raise DraftServiceError("subagent_draft_not_clarifiable")

        request_key_hash = hashlib.sha256(
            (f"subagent-draft-clarification-v1\0{draft.id}\0{draft.row_version}\0{normalized_answer}").encode(),
        ).hexdigest()
        claim_version = draft.row_version
        await self._accountant.preflight(
            team_id=team_id,
            owner_user_id=owner_user_id,
            request_key_hash=request_key_hash,
        )
        try:
            claimed = await self._drafts.begin_clarification(
                draft.id,
                team_id=team_id,
                owner_user_id=owner_user_id,
                expected_version=claim_version,
                answer=normalized_answer,
            )
        except BaseException:
            await self._accountant.release(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=request_key_hash,
                reason_code="subagent_draft_clarification_not_claimed",
            )
            raise
        if claimed is None:
            await self._accountant.release(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=request_key_hash,
                reason_code="subagent_draft_not_found",
            )
            raise DraftServiceError("subagent_draft_not_found")

        generation, catalog, generation_error, catalog_error = await self._generate_with_catalog(
            team_id=team_id,
            owner_user_id=owner_user_id,
            purpose=draft.purpose_text,
            locale=draft.locale,
            trace_id=request_key_hash[:16],
            mcp_entitled=mcp_entitled,
            clarification_answer=normalized_answer,
        )
        if generation_error is not None:
            if generation_error.input_tokens or generation_error.output_tokens:
                await self._settle_usage(
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    request_key_hash=request_key_hash,
                    draft_id=draft.id,
                    generation=generation_error,
                )
            else:
                await self._accountant.release(
                    team_id=team_id,
                    owner_user_id=owner_user_id,
                    request_key_hash=request_key_hash,
                    reason_code=generation_error.code,
                )
            failed = await self._finish_failed_clarification(
                draft=claimed,
                reason_code=generation_error.code,
            )
            return DraftServiceResult(DraftServiceOutcome.DRAFT_BLOCKED, draft=failed)
        if generation is None:
            raise DraftServiceError("subagent_draft_generation_missing")
        if catalog_error is not None or catalog is None:
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=request_key_hash,
                draft_id=draft.id,
                generation=generation,
            )
            failed = await self._finish_failed_clarification(
                draft=claimed,
                reason_code="subagent_draft_catalog_unavailable",
            )
            return DraftServiceResult(DraftServiceOutcome.DRAFT_BLOCKED, draft=failed)

        if generation.assessment.outcome != "clear" or generation.blueprint is None:
            reason_code = generation.assessment.reason_code or "subagent_clarification_insufficient"
            finished = await self._drafts.finish_clarification(
                draft.id,
                team_id=team_id,
                owner_user_id=owner_user_id,
                expected_version=claimed.row_version,
                definition=None,
                recommendations=None,
                assumptions=[],
                warnings=[],
                validation_issues=[
                    _issue(
                        "subagent_clarification_insufficient",
                        field="clarification",
                    ),
                ],
                catalog_fingerprint=None,
                definition_fingerprint=None,
                validation_status="invalid",
                status="blocked",
                outcome_reason_code=reason_code,
            )
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=request_key_hash,
                draft_id=draft.id,
                generation=generation,
            )
            return DraftServiceResult(
                DraftServiceOutcome.DRAFT_BLOCKED,
                draft=finished,
            )

        try:
            review = await self.materialize_blueprint(
                team_id=team_id,
                owner_user_id=owner_user_id,
                purpose=draft.purpose_text,
                locale=draft.locale,
                blueprint=generation.blueprint,
                mcp_entitled=mcp_entitled,
                catalog=catalog,
            )
            finished = await self._drafts.finish_clarification(
                draft.id,
                team_id=team_id,
                owner_user_id=owner_user_id,
                expected_version=claimed.row_version,
                definition=review.definition,
                recommendations=review.recommendations,
                assumptions=review.assumptions,
                warnings=review.warnings,
                validation_issues=review.validation_issues,
                catalog_fingerprint=review.catalog_fingerprint,
                definition_fingerprint=review.definition_fingerprint,
                validation_status=review.validation_status,
                status=review.draft_status,
                outcome_reason_code=generation.assessment.reason_code,
            )
        except Exception as exc:
            await self._settle_usage(
                team_id=team_id,
                owner_user_id=owner_user_id,
                request_key_hash=request_key_hash,
                draft_id=draft.id,
                generation=generation,
            )
            code = exc.code if isinstance(exc, DraftServiceError) else "subagent_draft_materialization_failed"
            try:
                failed = await self._finish_failed_clarification(
                    draft=claimed,
                    reason_code=code,
                )
            except Exception:
                logger.exception(
                    "subagent_draft.clarification_failure_finalize_failed draft=%s",
                    draft.id,
                )
                if isinstance(exc, DraftServiceError):
                    raise
                raise DraftServiceError(code) from exc
            return DraftServiceResult(DraftServiceOutcome.DRAFT_BLOCKED, draft=failed)
        await self._settle_usage(
            team_id=team_id,
            owner_user_id=owner_user_id,
            request_key_hash=request_key_hash,
            draft_id=draft.id,
            generation=generation,
        )
        if finished is None:
            raise DraftServiceError("subagent_draft_not_found")
        return DraftServiceResult(
            (DraftServiceOutcome.DRAFT_READY if finished.status == "ready" else DraftServiceOutcome.DRAFT_BLOCKED),
            draft=finished,
            expires_at=finished.expires_at,
        )

    async def _generate_with_catalog(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        purpose: str,
        locale: str,
        trace_id: str,
        mcp_entitled: bool,
        clarification_answer: str | None = None,
    ) -> tuple[
        Any | None,
        SubagentAuthoringCatalogSnapshot | None,
        SubagentDraftGenerationError | None,
        Exception | None,
    ]:
        """Load the live catalog in parallel without exposing it to the model."""
        generation: Any | None = None
        catalog: SubagentAuthoringCatalogSnapshot | None = None
        generation_error: SubagentDraftGenerationError | None = None
        catalog_error: Exception | None = None

        async def generate() -> None:
            nonlocal generation, generation_error
            try:
                generation = await self._generator.generate(
                    purpose=purpose,
                    locale=locale,
                    trace_id=trace_id,
                    clarification_answer=clarification_answer,
                )
            except SubagentDraftGenerationError as exc:
                generation_error = exc

        async def load_catalog() -> None:
            nonlocal catalog, catalog_error
            try:
                catalog = await self._catalog.build(
                    store=self._definitions,
                    team_id=team_id,
                    actor_user_id=owner_user_id,
                    actor_active=True,
                    mcp_entitled=mcp_entitled,
                )
            except Exception as exc:
                catalog_error = exc

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(generate)
            task_group.start_soon(load_catalog)
        return generation, catalog, generation_error, catalog_error

    async def _finish_failed_clarification(
        self,
        *,
        draft: SubagentDraft,
        reason_code: str,
    ) -> SubagentDraft:
        finished = await self._drafts.finish_clarification(
            draft.id,
            team_id=draft.team_id,
            owner_user_id=draft.owner_user_id,
            expected_version=draft.row_version,
            definition=None,
            recommendations=None,
            assumptions=[],
            warnings=[],
            validation_issues=[
                _issue(reason_code, field="clarification"),
            ],
            catalog_fingerprint=None,
            definition_fingerprint=None,
            validation_status="unknown",
            status="blocked",
            outcome_reason_code=reason_code,
        )
        if finished is None:
            raise DraftServiceError("subagent_draft_not_found")
        return finished

    async def materialize_blueprint(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        purpose: str,
        blueprint: DraftBlueprint,
        mcp_entitled: bool,
        catalog: SubagentAuthoringCatalogSnapshot | None = None,
        locale: str = "en-US",
    ) -> DraftReviewMaterialization:
        # A generated slug is a human-readable identifier, often a long
        # hyphenated phrase. Scan its explicit secret markers, but do not run
        # entropy detection over it as if it were a credential.
        if _SECRET_MARKER_RE.search(blueprint.slug_seed) or _contains_secret_pattern(
            blueprint.model_dump_json(exclude={"slug_seed"}),
        ):
            raise DraftServiceError(
                "subagent_draft_secret_pattern_rejected",
            )
        snapshot = catalog or await self._catalog.build(
            store=self._definitions,
            team_id=team_id,
            actor_user_id=owner_user_id,
            actor_active=True,
            mcp_entitled=mcp_entitled,
        )
        text_only = (
            blueprint.resource_mode == "provided_text"
            and not blueprint.capability_intents
            and resolve_subagent_authoring_capabilities(team_id).text_only
        )
        # Some needs are met by a built-in Plugin rather than a Tool (sending email: the Email Plugin), so the
        # suggestion recommends that Plugin and the Tool resolver doesn't report them as unmet.
        plugin_ids = sorted(
            {
                _PLUGIN_FOR_CAPABILITY[intent.capability_class]
                for intent in blueprint.capability_intents
                if intent.capability_class in _PLUGIN_FOR_CAPABILITY
            }
        )
        resolution = self._resolver.resolve_with_coverage(
            [intent for intent in blueprint.capability_intents if intent.capability_class not in _PLUGIN_FOR_CAPABILITY],
            snapshot,
        )
        recommendations = resolution.recommendations
        selected_refs = sorted(recommendation.tool_ref for recommendation in recommendations if recommendation.selected)
        name = await self._available_name(
            team_id=team_id,
            slug_seed=blueprint.slug_seed,
            purpose=purpose,
        )
        definition = {
            "name": name,
            "display_name": blueprint.display_name,
            "description": blueprint.description,
            "system_prompt": _render_system_prompt(blueprint, locale=locale),
            "tool_refs": selected_refs,
            **({"builtin_plugin_ids": plugin_ids} if plugin_ids else {}),
            "max_iterations": blueprint.max_iterations_hint,
            "skills": [],
            "model": "analyst",
            "output_schema": None,
            "visibility": "creator_only",
        }
        issues: list[dict[str, Any]] = []
        warnings = [warning.model_dump() for warning in blueprint.warnings if warning.code != TEXT_ONLY_CONFIRMATION]
        if text_only:
            warnings.append(
                {
                    "code": TEXT_ONLY_CONFIRMATION,
                    "message": (
                        "我确认这个助手只处理我提供的文字，不需要查询数据、访问外部系统、读取文件或执行脚本。"
                        if locale.casefold().startswith("zh")
                        else "I confirm this assistant only works with text I provide and needs no data queries, external services, file access, or scripts."
                    ),
                }
            )
        validation_status = "warning" if text_only else "valid"
        draft_status = "ready"
        try:
            validate_candidate(
                TeamSubagentDefinitionCandidate(
                    name=definition["name"],
                    display_name=definition["display_name"],
                    description=definition["description"],
                    system_prompt=definition["system_prompt"],
                    tool_refs=tuple(definition["tool_refs"]),
                    max_iterations=definition["max_iterations"],
                    skills=(),
                    model=definition["model"],
                    output_schema=None,
                    audience_mode="private",
                ),
                catalog=snapshot,
                reserved_names=reserved_team_subagent_names(),
                operation="draft",
                allow_empty_tools=text_only,
            )
        except TeamSubagentValidationError as exc:
            validation_status = "invalid"
            draft_status = "blocked"
            issues.append(_validation_issue(exc))
        # No selected tool for a required capability means the created agent cannot perform it (observed on 116:
        # retrieval absent, only charting and report writing left, yet validation passed).
        for unmet in resolution.unmet_required:
            validation_status = "invalid"
            draft_status = "blocked"
            issues.append(
                unmet_requirement_issue(unmet, catalog=snapshot, locale=locale),
            )
        return DraftReviewMaterialization(
            definition=definition,
            recommendations=[recommendation.to_dict() for recommendation in recommendations],
            assumptions=list(blueprint.assumptions),
            warnings=warnings,
            validation_issues=issues,
            catalog_fingerprint=snapshot.fingerprint,
            definition_fingerprint=definition_fingerprint(definition),
            validation_status=validation_status,
            draft_status=draft_status,
        )

    async def _available_name(
        self,
        *,
        team_id: str,
        slug_seed: str,
        purpose: str,
    ) -> str:
        base = _slug(slug_seed)
        if base in reserved_team_subagent_names():
            base = f"{base[:23]}-agent"
        candidate = base
        for ordinal in range(6):
            existing = await self._definitions.get_team_subagent_by_name(
                team_id,
                candidate,
                only_enabled=False,
            )
            if existing is None and candidate not in reserved_team_subagent_names():
                return candidate
            suffix = hashlib.sha256(
                f"{purpose}\0{ordinal}".encode(),
            ).hexdigest()[:6]
            candidate = f"{base[:25]}-{suffix}"
        raise DraftServiceError("subagent_name_unavailable")

    async def _settle_usage(
        self,
        *,
        team_id: str,
        owner_user_id: str,
        request_key_hash: str,
        draft_id: str | None,
        generation: Any,
    ) -> None:
        usage = {
            "team_id": team_id,
            "owner_user_id": owner_user_id,
            "request_key_hash": request_key_hash,
            "draft_id": draft_id,
            "input_tokens": generation.input_tokens,
            "output_tokens": generation.output_tokens,
            "attempts": generation.attempts,
            "model_usage": getattr(generation, "model_usage", ()),
        }
        try:
            prepare = getattr(
                self._accountant,
                "prepare_settlement",
                None,
            )
            if prepare is not None:
                await prepare(**usage)
            await self._accountant.settle(**usage)
        except Exception:
            # The draft/result is durable and must not be reported as absent.
            # The concrete accountant keeps settlement_pending usage for the
            # bounded maintenance retry; legacy/fake accountants remain valid.
            logger.exception(
                "subagent_draft.usage_settle_failed request=%s",
                request_key_hash[:12],
            )


def _purpose_outcome(code: str | None) -> DraftServiceOutcome:
    if code == "purpose_required":
        return DraftServiceOutcome.PURPOSE_REQUIRED
    if code == "purpose_insufficient":
        return DraftServiceOutcome.PURPOSE_INSUFFICIENT
    raise DraftServiceError(code or "subagent_draft_request_failed")


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode().casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    if not slug:
        slug = "agent"
    if not slug[0].isalpha():
        slug = f"agent-{slug}"
    slug = slug[:32].rstrip("-")
    if len(slug) < 2:
        slug = f"{slug}x"
    return slug


def _safe_line(value: str, *, limit: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _CONTROL_RE.sub(" ", normalized)
    return " ".join(normalized.split())[:limit]


# Built-in Plugins that meet a capability need on their own.
_PLUGIN_FOR_CAPABILITY = {"email_send": "builtin:email"}

# Section headings of a generated system prompt, in the language the owner reviews the draft in.
_PROMPT_HEADINGS: dict[str, tuple[str, str, str, str, str, str]] = {
    "zh": ("角色", "目标", "边界", "证据要求", "无法完成时", "输出要求"),
    "en": ("Role", "Goals", "Boundaries", "Evidence rules", "Failure behavior", "Output expectations"),
    "de": ("Rolle", "Ziele", "Grenzen", "Belegregeln", "Wenn etwas nicht geht", "Erwartete Ausgabe"),
    "fr": ("Rôle", "Objectifs", "Limites", "Règles de preuve", "En cas d’échec", "Résultat attendu"),
}


def _prompt_headings(locale: str) -> tuple[str, str, str, str, str, str]:
    return _PROMPT_HEADINGS.get(locale.casefold().split("-")[0], _PROMPT_HEADINGS["en"])


def _render_system_prompt(blueprint: Any, *, locale: str = "en-US") -> str:
    prompt = blueprint.system_prompt
    role, goals, boundaries, evidence, failure, output = _prompt_headings(locale)
    colon = "：" if locale.casefold().startswith("zh") else ":"
    sections = [
        f"{role}{colon}{'' if colon == '：' else ' '}{_safe_line(prompt.role, limit=500)}",
        f"{goals}{colon}",
        *(f"- {_safe_line(value, limit=500)}" for value in prompt.goals),
        f"{boundaries}{colon}",
        *(f"- {_safe_line(value, limit=500)}" for value in prompt.boundaries),
        f"{evidence}{colon}",
        *(f"- {_safe_line(value, limit=500)}" for value in prompt.evidence_rules),
        f"{failure}{colon}",
        *(f"- {_safe_line(value, limit=500)}" for value in prompt.failure_behavior),
        f"{output}{colon}",
        *(f"- {_safe_line(value, limit=500)}" for value in prompt.output_expectations),
    ]
    # Same limit as the write side (MAX_SYSTEM_PROMPT). Previously hardcoded at 4000 with silent truncation:
    # validation and routing allowed longer prompts, but this alone cut generated paragraphs without anyone knowing.
    rendered = "\n".join(sections)
    if len(rendered) > MAX_SYSTEM_PROMPT:
        logger.warning(
            "subagent_draft.rendered_prompt_truncated rendered=%d kept=%d",
            len(rendered),
            MAX_SYSTEM_PROMPT,
        )
        rendered = rendered[:MAX_SYSTEM_PROMPT]
    if _contains_secret_pattern(rendered):
        raise DraftServiceError("subagent_draft_secret_pattern_rejected")
    try:
        lint_user_append(rendered)
    except Exception as exc:
        raise DraftServiceError(
            "subagent_draft_prompt_security_rejected",
        ) from exc
    return rendered


def _contains_secret_pattern(value: str) -> bool:
    if _SECRET_MARKER_RE.search(value):
        return True
    for match in _SECRET_TOKEN_RE.finditer(value):
        token = match.group(0)
        if not any(character.isalpha() for character in token):
            continue
        if not (any(character.isdigit() for character in token) or any(character in "+/=" for character in token)):
            continue
        counts = {character: token.count(character) for character in set(token)}
        entropy = -sum((count / len(token)) * math.log2(count / len(token)) for count in counts.values())
        if entropy >= 3.5:
            return True
    return False


def definition_fingerprint(definition: dict[str, Any]) -> str:
    payload = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validation_issue(
    exc: TeamSubagentValidationError,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "code": exc.code,
        "severity": "error",
        "field": exc.field,
        "tool_ref": exc.tool_ref,
        "message_key": f"subagent.validation.{exc.code}",
        "details": {},
    }


def reserved_team_subagent_names() -> set[str]:
    return set(RESERVED_NAMES) | _SYSTEM_RESERVED | set(PROFILES)


def _issue(code: str, *, field: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "code": code,
        "severity": "error",
        "field": field,
        "tool_ref": None,
        "message_key": f"subagent.validation.{code}",
        "details": {},
    }


_CAPABILITY_LABELS: dict[str, tuple[str, str]] = {
    "data_discovery": ("查看可用数据", "discover available data"),
    "data_query": ("查询数据", "query data"),
    "isolated_compute": ("运行计算", "run computations"),
    "artifact_chart": ("生成图表", "create charts"),
    "artifact_report": ("撰写报告", "write reports"),
    "public_web_research": ("搜索公开网页", "search the public web"),
    "public_web_fetch": ("读取指定网页", "read a given web page"),
    "knowledge_search": ("检索知识库", "search the knowledge base"),
    "file_read": ("读取文件", "read files"),
    "team_domain_read": ("读取团队接入的外部系统", "read a connected team system"),
    "team_domain_write": ("修改团队接入的外部系统", "change a connected team system"),
}


def _tool_names(refs: tuple[str, ...], catalog: SubagentAuthoringCatalogSnapshot, *, is_zh: bool) -> str:
    by_ref = catalog.tools_by_ref
    names: list[str] = []
    for ref in refs:
        tool = by_ref.get(ref)
        if tool is None:
            continue
        names.append((tool.display_name_zh or tool.display_name) if is_zh else (tool.display_name_en or tool.display_name))
    return ("、" if is_zh else ", ").join(names)


def unmet_requirement_issue(
    unmet: UnmetRequirement,
    *,
    catalog: SubagentAuthoringCatalogSnapshot,
    locale: str,
) -> dict[str, Any]:
    """User-readable issue for a required need that ended with no selected tool.

    The message names the need, why nothing was selected, and what to do next;
    ``details`` keeps the machine facts the edit path uses to re-check it.
    """
    is_zh = locale.casefold().startswith("zh")
    label_zh, label_en = _CAPABILITY_LABELS.get(unmet.capability_class, (unmet.capability_class, unmet.capability_class))
    names = _tool_names(unmet.candidate_tool_refs, catalog, is_zh=is_zh)
    if is_zh:
        need = f"必需能力「{label_zh}」（{unmet.reason}）没有选中任何工具"
        if unmet.cause == "no_matching_tool":
            detail = "当前团队可用的工具里没有能完成这项需求的。请在工具列表中手动选择合适的工具；如果团队还没有接入对应的数据或工具，请联系团队管理员接入后重新生成草稿。"
        elif unmet.cause == "tool_locked":
            detail = f"对应的工具（{names}）目前处于锁定状态，无法选择，锁定原因见工具列表。请联系团队管理员开通后再勾选。"
        else:
            detail = f"找到了可能合适的工具（{names}），但没有自动勾选。请在工具列表中确认并勾选。"
        message = f"{need}：{detail}"
    else:
        need = f"Required capability “{label_en}” ({unmet.reason}) has no selected tool"
        if unmet.cause == "no_matching_tool":
            detail = "none of the tools available to this team can do it. Pick a suitable tool in the tool list, or ask a team admin to connect the data or tool and then regenerate the draft."
        elif unmet.cause == "tool_locked":
            detail = f"the matching tools ({names}) are locked and cannot be selected; the tool list shows why. Ask a team admin to enable them, then select them."
        else:
            detail = f"possible tools were found ({names}) but not selected automatically. Review and select them in the tool list."
        message = f"{need}: {detail}"
    return {
        "schema_version": 1,
        "code": REQUIRED_CAPABILITY_UNMET,
        "severity": "error",
        "field": "tool_refs",
        "tool_ref": None,
        "message_key": f"subagent.validation.{REQUIRED_CAPABILITY_UNMET}",
        "message": message,
        "details": {
            "intent_id": unmet.intent_id,
            "capability_class": unmet.capability_class,
            "object_terms": list(unmet.object_terms),
            "cause": unmet.cause,
            "candidate_tool_refs": list(unmet.candidate_tool_refs),
        },
    }


def carry_forward_unmet_requirements(
    previous_issues: list[dict[str, Any]],
    selected_tools: list[AuthoringTool],
) -> list[dict[str, Any]]:
    """Keep unmet-requirement issues the current selection still does not cover.

    Editing a draft re-runs validation from scratch; without this an unrelated
    edit would silently clear the unmet need. A need counts as covered once any
    selected tool offers its capability class or is one of its candidates.
    """
    selected_refs = {tool.stable_ref for tool in selected_tools}
    selected_classes: set[str] = set()
    for tool in selected_tools:
        selected_classes.update(tool.capability_classes)
    still_open: list[dict[str, Any]] = []
    for issue in previous_issues:
        if issue.get("code") != REQUIRED_CAPABILITY_UNMET:
            continue
        details = issue.get("details") or {}
        if details.get("capability_class") in selected_classes:
            continue
        if selected_refs & set(details.get("candidate_tool_refs") or []):
            continue
        still_open.append(issue)
    return still_open


__all__ = [
    "DraftReviewMaterialization",
    "DraftServiceError",
    "DraftServiceOutcome",
    "DraftServiceResult",
    "DraftUsageAccountant",
    "NoopDraftUsageAccountant",
    "REQUIRED_CAPABILITY_UNMET",
    "SubagentDraftService",
    "carry_forward_unmet_requirements",
    "definition_fingerprint",
    "reserved_team_subagent_names",
    "unmet_requirement_issue",
]
