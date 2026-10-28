"""One strict decoder for every shared clarification resume constructor."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bowhead_core.document_chat_continuation import DocumentPlanningCheckpointRef

RESUME_CONTEXT_VERSION_KEY = "__schema_version"
LEGACY_RESUME_CONTEXT_MAX_VERSION = 2
DOCUMENT_RESUME_CONTEXT_VERSION = 3
ORDINARY_LEAD_RESUME_CONTEXT_VERSION = 4
Identity = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class UnsupportedResumeContext(ValueError):
    def __init__(self) -> None:
        super().__init__("clarification_resume_unavailable")


@dataclass(frozen=True, slots=True)
class LegacyResumeContext:
    version: int
    snapshot: dict[str, Any]


class DocumentPlanningContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["document_planning"]
    version: Literal[1]
    checkpoint: DocumentPlanningCheckpointRef
    runtime_snapshot_digest: Digest
    creation_generation: int = Field(ge=1)
    creation_owner_id: Identity

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Document continuation version must be an integer")
        return value


class DocumentResumeContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[3] = Field(alias=RESUME_CONTEXT_VERSION_KEY)
    team_id: Identity
    user_id: Identity
    workspace_id: Identity
    session_id: Identity
    turn_id: Identity
    user_text: str = Field(min_length=1, max_length=1_000_000)
    continuation: DocumentPlanningContinuation

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Document resume version must be an integer")
        return value


class OrdinaryLeadContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["ordinary_lead"]
    contract: Literal["ordinary-lead-question.v1"]
    card_id: Identity
    creation_generation: Literal[1]
    creation_owner_id: Identity
    source_hash: Digest
    question_hash: Digest
    checkpoint_hash: Digest
    checkpoint_ciphertext: str = Field(min_length=1, max_length=524288, repr=False)

    @field_validator("creation_generation", mode="before")
    @classmethod
    def exact_generation(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Ordinary continuation generation must be an integer")
        return value


class OrdinaryLeadResumeContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[4] = Field(alias=RESUME_CONTEXT_VERSION_KEY)
    team_id: Identity
    user_id: Identity
    workspace_id: Identity
    session_id: Identity
    turn_id: Identity
    user_text: str = Field(min_length=1, max_length=1_000_000)
    questions: list[dict[str, Any]] = Field(min_length=1, max_length=5)
    continuation: OrdinaryLeadContinuation

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Ordinary resume version must be an integer")
        return value


ResumeContext = LegacyResumeContext | DocumentResumeContext | OrdinaryLeadResumeContext


def resume_context_version(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    raw = value.get(RESUME_CONTEXT_VERSION_KEY, 0)
    return raw if type(raw) is int and raw >= 0 else None


def decode_resume_context(value: object) -> ResumeContext:
    """Recognize exact document snapshots without interpreting them as Lead state."""
    version = resume_context_version(value)
    if version is None:
        raise UnsupportedResumeContext()
    if version <= LEGACY_RESUME_CONTEXT_MAX_VERSION:
        # The new discriminator cannot hide in an old marker. Otherwise an old
        # producer could turn a retained planning checkpoint into a Lead restart.
        if "continuation" in value:
            raise UnsupportedResumeContext()
        return LegacyResumeContext(version=version, snapshot=deepcopy(value))
    if version not in (DOCUMENT_RESUME_CONTEXT_VERSION, ORDINARY_LEAD_RESUME_CONTEXT_VERSION):
        raise UnsupportedResumeContext()
    try:
        model = DocumentResumeContext if version == DOCUMENT_RESUME_CONTEXT_VERSION else OrdinaryLeadResumeContext
        return model.model_validate(value)
    except ValidationError as error:
        raise UnsupportedResumeContext() from error


def decode_clarification_context(card: Any) -> ResumeContext:
    """Bind a document discriminator to its actual shared card shape and scope."""
    decoded = decode_resume_context(card.resume_context)
    if isinstance(decoded, (DocumentResumeContext, OrdinaryLeadResumeContext)) and (
        card.kind != "ambiguous_turn"
        or card.token
        or card.tool
        or card.multi_select
        or card.candidates
        or (card.turn_id, card.user_id, card.workspace_id, card.session_id)
        != (decoded.turn_id, decoded.user_id, decoded.workspace_id, decoded.session_id)
    ):
        raise UnsupportedResumeContext()
    if isinstance(decoded, OrdinaryLeadResumeContext) and decoded.continuation.card_id != card.request_id:
        raise UnsupportedResumeContext()
    return decoded
