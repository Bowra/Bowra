"""Exact reviewed document intent, without granting document access."""

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentRevisionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["document_revision"]
    version: Literal[1]
    session_id: str = Field(pattern=r"^s_[A-Za-z0-9_]{1,126}$")
    job_id: str = Field(pattern=r"^djob_[A-Za-z0-9_]{1,123}$")
    draft_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        # Literal comparison alone would treat True and 1.0 as version 1.
        if type(value) is not int:
            raise ValueError("document revision version must be an integer")
        return value


def document_reuse_enabled() -> bool:
    """Gate new revision/checkpoint writers; persisted readers remain available."""
    return os.environ.get("FEATURE_TEMPLATE_DOCUMENT_REUSE", "1") == "1"


DOCUMENT_REVISION_ADMISSION_ERRORS = frozenset(
    {
        "document_not_found",
        "document_revision_conflict",
        "document_input_unavailable",
        "document_draft_unavailable",
        "document_revision_inputs_unsupported",
        "document_revision_unavailable",
    }
)
