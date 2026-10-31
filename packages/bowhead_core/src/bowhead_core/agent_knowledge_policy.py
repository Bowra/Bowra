"""Versioned Agent source ceilings; these narrow access and never grant it."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from bowhead_core.kb.retrieval_policy import MAX_TOP_K

MAX_SELECTED_FILES = 64
FileIdentity = Annotated[str, Field(pattern=r"^kbf_[A-Za-z0-9_-]{1,128}$")]


class KnowledgeAccessError(ValueError):
    def __init__(self, code: str = "agent_knowledge_access_invalid") -> None:
        self.code = code
        self.details = {
            "field": "knowledge_access",
            "next_step": "Check the Agent's source selection and current file access. Retain the restriction when retrying; use a compatible source-policy reader.",
        }
        super().__init__(code)


def source_bindings_enabled() -> bool:
    return os.environ.get("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "1") == "1"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class KnowledgeDefaults(_Record):
    top_k: StrictInt | None = Field(default=None, ge=1, le=MAX_TOP_K)

    @model_validator(mode="before")
    @classmethod
    def reject_null(cls, value):
        if isinstance(value, dict) and "top_k" in value and value["top_k"] is None:
            raise ValueError("Omit top_k to use the Team default")
        return value


class KnowledgeAccess(_Record):
    schema_version: StrictInt = Field(ge=1, le=1)
    mode: Literal["context_readable", "selected_files"]
    file_ids: tuple[FileIdentity, ...] | None = Field(default=None, max_length=MAX_SELECTED_FILES)
    defaults: KnowledgeDefaults = Field(default_factory=KnowledgeDefaults)

    @model_validator(mode="after")
    def selection(self) -> Self:
        if self.mode == "selected_files":
            if self.file_ids is None or len(set(self.file_ids)) != len(self.file_ids):
                raise ValueError("Selected files require unique identities, including an explicit empty selection")
        elif "file_ids" in self.model_fields_set:
            raise ValueError("Current-context reading must omit file_ids")
        return self

    def canonical_value(self) -> dict:
        value = {"schema_version": 1, "mode": self.mode}
        if self.file_ids is not None:
            value["file_ids"] = sorted(self.file_ids)
        if self.defaults.top_k is not None:
            value["defaults"] = {"top_k": self.defaults.top_k}
        return value

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.canonical_value())


def fingerprint(value: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_supported_knowledge_access(definition: Mapping) -> KnowledgeAccess | None:
    """Read retained policies regardless of current admission; malformed is never absent."""
    if "knowledge_access" not in definition:
        return None
    try:
        return KnowledgeAccess.model_validate_json(json.dumps(definition["knowledge_access"], allow_nan=False))
    except (TypeError, ValueError):
        raise KnowledgeAccessError() from None


def require_policy_preserved(previous: Mapping, candidate: Mapping) -> None:
    require_supported_knowledge_access(previous)
    if "knowledge_access" in previous and "knowledge_access" not in candidate:
        raise KnowledgeAccessError("agent_knowledge_access_preservation_required")


class KnowledgeScope(_Record):
    schema_version: StrictInt = Field(ge=1, le=1)
    user_id: str
    team_id: str
    workspace_id: str
    file_ids: tuple[FileIdentity, ...] | None
    bindings: tuple[tuple[str, str], ...] = Field(max_length=64)
    top_k: StrictInt | None = Field(default=None, ge=1, le=MAX_TOP_K)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))

    def require_context(self, ctx) -> None:
        if (self.user_id, self.team_id, self.workspace_id) != (ctx.user_id, ctx.team_id, ctx.workspace_id):
            raise KnowledgeAccessError("knowledge_source_context_changed")

    def permits(self, file_id: str) -> bool:
        return self.file_ids is None or file_id in self.file_ids


def bind_knowledge_scope(ctx, policy: KnowledgeAccess | None, *, revision: str) -> KnowledgeScope | None:
    parent = ctx.knowledge_scope
    if parent is not None:
        parent.require_context(ctx)
    if policy is None:
        return parent
    selected = policy.file_ids
    if parent is not None and parent.file_ids is not None:
        selected = parent.file_ids if selected is None else tuple(sorted(set(parent.file_ids).intersection(selected)))
    bindings = tuple(sorted(set((parent.bindings if parent else ()) + ((revision, policy.fingerprint),))))
    return KnowledgeScope(
        schema_version=1,
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        workspace_id=ctx.workspace_id,
        file_ids=None if selected is None else tuple(sorted(selected)),
        bindings=bindings,
        top_k=policy.defaults.top_k if policy.defaults.top_k is not None else parent.top_k if parent else None,
    )


async def require_knowledge_migration(db) -> None:
    try:
        async with db.execute("SELECT version_num FROM alembic_version") as cursor:
            row = await cursor.fetchone()
        if row is None or int(str(row[0]).split("_", 1)[0]) < 199:
            raise ValueError()
    except Exception:
        raise KnowledgeAccessError("agent_knowledge_access_migration_required") from None


def knowledge_access_expanded(previous: Mapping, candidate: Mapping) -> bool:
    before = require_supported_knowledge_access(previous)
    after = require_supported_knowledge_access(candidate)
    return bool(
        before is not None
        and before.file_ids is not None
        and (after is None or after.file_ids is None or not set(after.file_ids).issubset(before.file_ids))
    )
