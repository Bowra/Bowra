"""Exact document planning references carried by shared clarification recovery.

These values identify retained server records; parsing never grants execution.
Domain evidence and frozen model/language content stay in document storage.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Identity = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DocumentPlanningCheckpointRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    job_id: str = Field(pattern=r"^djob_[A-Za-z0-9_]{1,123}$")
    attempt_no: int = Field(ge=1, le=2)
    checkpoint_digest: Digest
    source_hash: Digest


class DocumentTurnOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    team_id: Identity
    user_id: Identity
    workspace_id: Identity
    session_id: Identity
    turn_id: Identity
    generation: int = Field(ge=1)
    owner_id: Identity


class DocumentResumeReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    card_id: Identity
    checkpoint: DocumentPlanningCheckpointRef
    generation: int = Field(ge=1)


class DocumentPlanningAttemptRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    job_id: str = Field(pattern=r"^djob_[A-Za-z0-9_]{1,123}$")
    attempt_no: int = Field(ge=1, le=2)
    worker: DocumentTurnOwner
