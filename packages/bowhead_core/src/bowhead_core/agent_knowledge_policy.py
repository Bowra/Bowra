"""Versioned Agent source ceilings; these narrow access and never grant it.

Schema 1 has two modes: ``context_readable`` (what the invoker can use in the current context)
and ``selected_files`` (at most 64 files). Schema 2 has one mode, ``selected_knowledge_bases``
(at most 32 knowledge bases, behind ``FEATURE_KNOWLEDGE_BASES``): when the Agent reads, it may use
the files that are in those knowledge bases at that moment and that the invoker can use, so files
added later count. Schema 1 policies keep their canonical value and fingerprint. Images that
predate schema 2 reject a schema 2 policy, so such an Agent fails closed after an image rollback.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Mapping
from functools import cache
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_serializer, model_validator

from bowhead_core.kb.retrieval_policy import MAX_TOP_K

MAX_SELECTED_FILES = 64
MAX_SELECTED_KNOWLEDGE_BASES = 32
FileIdentity = Annotated[str, Field(pattern=r"^kbf_[A-Za-z0-9_-]{1,128}$")]
#: ``kbase_`` and 24 hexadecimal digits, for defaults (derived) and named knowledge bases (random) alike.
KnowledgeBaseIdentity = Annotated[str, Field(pattern=r"^kbase_[0-9a-f]{24}$")]
KNOWLEDGE_BASE_MODE = "selected_knowledge_bases"

_GENERIC_NEXT_STEP = (
    "Check the Agent's source selection and current file access. Retain the restriction when retrying; use a compatible source-policy reader."
)
#: Codes whose field and next step differ from the generic ones. Never name a knowledge base here:
#: an unknown and an unusable one must read the same (no existence leak).
_GUIDANCE: dict[str, tuple[str, str]] = {
    "agent_knowledge_base_unavailable": (
        "knowledge_access.knowledge_base_ids",
        "Some selected knowledge bases are not available to you. Remove them or choose again from the knowledge bases you can use, then save.",
    ),
    "agent_knowledge_bases_preparing": (
        "knowledge_access.mode",
        "Knowledge bases are still being prepared for Agents in this Team. Try again in a few minutes, "
        "or use selected files or all knowledge the user can use for now.",
    ),
    "agent_knowledge_bases_unavailable": (
        "knowledge_access.mode",
        "Knowledge base search is not available for this Team right now. Ask a Team administrator to check the knowledge model settings, "
        "then try again, or use selected files or all knowledge the user can use for now.",
    ),
    "agent_knowledge_bases_disabled": (
        "knowledge_access.mode",
        "Selected knowledge bases are turned off on this deployment. Choose selected files or all knowledge the user can use, "
        "or ask an administrator to turn knowledge bases on.",
    ),
}
_SELECTION_NEXT_STEP = (
    f"Choose at most {MAX_SELECTED_KNOWLEDGE_BASES} knowledge bases from the list of knowledge bases you can use, "
    "and send their IDs only with the selected knowledge bases mode."
)


class KnowledgeAccessError(ValueError):
    def __init__(self, code: str = "agent_knowledge_access_invalid", *, field: str | None = None, next_step: str | None = None) -> None:
        self.code = code
        default_field, default_next_step = _GUIDANCE.get(code, ("knowledge_access", _GENERIC_NEXT_STEP))
        self.details = {"field": field or default_field, "next_step": next_step or default_next_step}
        super().__init__(code)


def source_bindings_enabled() -> bool:
    return os.environ.get("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "1") == "1"


def knowledge_base_mode_enabled() -> bool:
    """Whether Agents may select knowledge bases: source bindings and ``FEATURE_KNOWLEDGE_BASES`` are both on."""
    from bowhead_core.kb.knowledge_base_store import knowledge_bases_enabled

    return source_bindings_enabled() and knowledge_bases_enabled()


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
    schema_version: StrictInt = Field(ge=1, le=2)
    mode: Literal["context_readable", "selected_files", "selected_knowledge_bases"]
    file_ids: tuple[FileIdentity, ...] | None = Field(default=None, max_length=MAX_SELECTED_FILES)
    knowledge_base_ids: tuple[KnowledgeBaseIdentity, ...] | None = Field(default=None, max_length=MAX_SELECTED_KNOWLEDGE_BASES)
    defaults: KnowledgeDefaults = Field(default_factory=KnowledgeDefaults)

    @model_validator(mode="after")
    def selection(self) -> Self:
        if (self.schema_version == 2) != (self.mode == KNOWLEDGE_BASE_MODE):
            raise ValueError("Selected knowledge bases are schema 2; the other modes are schema 1")
        if self.mode == "selected_files":
            if self.file_ids is None or len(set(self.file_ids)) != len(self.file_ids):
                raise ValueError("Selected files require unique identities, including an explicit empty selection")
            if "knowledge_base_ids" in self.model_fields_set:
                raise ValueError("Selected files must omit knowledge_base_ids")
        elif self.mode == KNOWLEDGE_BASE_MODE:
            if self.knowledge_base_ids is None or len(set(self.knowledge_base_ids)) != len(self.knowledge_base_ids):
                raise ValueError("Selected knowledge bases require unique identities, including an explicit empty selection")
            if "file_ids" in self.model_fields_set:
                raise ValueError("Selected knowledge bases must omit file_ids")
        elif "file_ids" in self.model_fields_set or "knowledge_base_ids" in self.model_fields_set:
            raise ValueError("Current-context reading must omit file_ids and knowledge_base_ids")
        return self

    def canonical_value(self) -> dict:
        value: dict[str, Any] = {"schema_version": self.schema_version, "mode": self.mode}
        if self.file_ids is not None:
            value["file_ids"] = sorted(self.file_ids)
        if self.knowledge_base_ids is not None:
            value["knowledge_base_ids"] = sorted(self.knowledge_base_ids)
        if self.defaults.top_k is not None:
            value["defaults"] = {"top_k": self.defaults.top_k}
        return value

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.canonical_value())


def fingerprint(value: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _drop_unusable(value: dict, key: str) -> None:
    """Drop an optional selection that cannot apply to the mode and selects nothing (omitted, null or empty)."""
    if key in value and (value[key] is None or value[key] == []):
        del value[key]


def normalize_knowledge_access(raw: Any) -> Any:
    """Normalize equivalent forms before strict validation; never into a wider policy.

    - A missing or null ``knowledge_base_ids`` with the selected knowledge bases mode is an
      explicit empty selection, which reads nothing.
    - A single knowledge base ID becomes a list; IDs are stripped and duplicates removed.
    - A null or empty selection field the mode does not use (``file_ids`` with knowledge bases,
      ``knowledge_base_ids`` with the other modes) is dropped.

    Everything else is validated as sent: a policy that selects something its mode cannot
    honor, or with a schema version that does not match its mode, is refused.
    """
    if not isinstance(raw, dict):
        return raw
    value = dict(raw)
    if value.get("mode") != KNOWLEDGE_BASE_MODE:
        _drop_unusable(value, "knowledge_base_ids")
        return value
    _drop_unusable(value, "file_ids")
    ids = value.get("knowledge_base_ids")
    if ids is None:
        ids = []
    elif isinstance(ids, str):
        ids = [ids]
    if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
        ids = list(dict.fromkeys(item.strip() for item in ids))
    value["knowledge_base_ids"] = ids
    return value


def require_supported_knowledge_access(definition: Mapping) -> KnowledgeAccess | None:
    """Read retained policies regardless of current admission; malformed is never absent.

    This is the shared entry point: every reader and writer validates here, after
    :func:`normalize_knowledge_access`. A refused knowledge base selection names its field.
    """
    if "knowledge_access" not in definition:
        return None
    try:
        return KnowledgeAccess.model_validate_json(json.dumps(normalize_knowledge_access(definition["knowledge_access"]), allow_nan=False))
    except ValidationError as exc:
        if any(error.get("loc", ())[:1] == ("knowledge_base_ids",) for error in exc.errors()):
            raise KnowledgeAccessError(field="knowledge_access.knowledge_base_ids", next_step=_SELECTION_NEXT_STEP) from None
        raise KnowledgeAccessError() from None
    except (TypeError, ValueError):
        raise KnowledgeAccessError() from None


def require_policy_preserved(previous: Mapping, candidate: Mapping) -> None:
    require_supported_knowledge_access(previous)
    if "knowledge_access" in previous and "knowledge_access" not in candidate:
        raise KnowledgeAccessError("agent_knowledge_access_preservation_required")


def _intersect(first: tuple[str, ...] | None, second: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Intersect two ceilings; None is no ceiling."""
    if first is None and second is None:
        return None
    if first is None or second is None:
        return tuple(sorted(first if second is None else second))
    return tuple(sorted(set(first).intersection(second)))


class KnowledgeScope(_Record):
    """The effective ceilings of one Agent execution, the intersection of every enclosing policy.

    ``file_ids`` and ``knowledge_base_ids`` are separate ceilings, each None when no policy sets it;
    a file must pass both. Schema 2 marks a scope with a knowledge base ceiling. The field is left
    out of the dump while unset, so fingerprints and retained material of earlier scopes are unchanged.
    """

    schema_version: StrictInt = Field(ge=1, le=2)
    user_id: str
    team_id: str
    workspace_id: str
    file_ids: tuple[FileIdentity, ...] | None
    bindings: tuple[tuple[str, str], ...] = Field(max_length=64)
    top_k: StrictInt | None = Field(default=None, ge=1, le=MAX_TOP_K)
    knowledge_base_ids: tuple[KnowledgeBaseIdentity, ...] | None = None

    @model_validator(mode="after")
    def version(self) -> Self:
        if (self.knowledge_base_ids is not None) != (self.schema_version == 2):
            raise ValueError("A knowledge base ceiling is schema 2")
        return self

    @model_serializer(mode="wrap")
    def _omit_unset_ceiling(self, handler):
        data = handler(self)
        if self.knowledge_base_ids is None and isinstance(data, dict):
            data.pop("knowledge_base_ids", None)
        return data

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))

    def require_context(self, ctx) -> None:
        if (self.user_id, self.team_id, self.workspace_id) != (ctx.user_id, ctx.team_id, ctx.workspace_id):
            raise KnowledgeAccessError("knowledge_source_context_changed")

    def permits_file_id(self, file_id: str) -> bool:
        """The file ceiling alone, for checks made before the file is read."""
        return self.file_ids is None or file_id in self.file_ids

    def permits(self, file: Any) -> bool:
        """Both ceilings: the file is selected (when files are) and in a selected knowledge base (when knowledge bases are).

        The knowledge base is the file's stored one, or its scope's default when none is stored; the
        caller's own access check refuses a stored knowledge base of another home. A bare file ID
        passes only without a knowledge base ceiling, because its knowledge base is unknown.
        """
        if isinstance(file, str):
            return self.knowledge_base_ids is None and self.permits_file_id(file)
        if not self.permits_file_id(str(getattr(file, "file_id", "") or "")):
            return False
        if self.knowledge_base_ids is None:
            return True
        from bowhead_core.kb.knowledge_base_store import knowledge_base_key

        key = knowledge_base_key(file)
        return bool(key) and key in self.knowledge_base_ids

    def narrowed_by(self, current: KnowledgeScope | None) -> KnowledgeScope:
        """This retained scope with each ceiling intersected with ``current``'s (a resumed helper)."""
        if current is None:
            return self
        bases = _intersect(self.knowledge_base_ids, current.knowledge_base_ids)
        return self.model_copy(
            update={
                "file_ids": _intersect(self.file_ids, current.file_ids),
                "knowledge_base_ids": bases,
                "schema_version": 2 if bases is not None else 1,
            }
        )


def bind_knowledge_scope(ctx, policy: KnowledgeAccess | None, *, revision: str) -> KnowledgeScope | None:
    parent = ctx.knowledge_scope
    if parent is not None:
        parent.require_context(ctx)
    if policy is None:
        return parent
    bases = _intersect(parent.knowledge_base_ids if parent else None, policy.knowledge_base_ids)
    bindings = tuple(sorted(set((parent.bindings if parent else ()) + ((revision, policy.fingerprint),))))
    return KnowledgeScope(
        schema_version=2 if bases is not None else 1,
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        workspace_id=ctx.workspace_id,
        file_ids=_intersect(parent.file_ids if parent else None, policy.file_ids),
        bindings=bindings,
        top_k=policy.defaults.top_k if policy.defaults.top_k is not None else parent.top_k if parent else None,
        knowledge_base_ids=bases,
    )


@cache
def _revision_number(module: str) -> int:
    """The number of a migration revision, read from its module (no revision literal outside migrations)."""
    revision = importlib.import_module(module).revision
    return int(str(revision).split("_", 1)[0])


def required_revision_number(policy: KnowledgeAccess | None) -> int:
    """Source bindings need revision 0199; a knowledge base selection also needs the knowledge base tables of 0239."""
    if policy is not None and policy.knowledge_base_ids is not None:
        return _revision_number("bowhead_core.migrations.versions.0239_knowledge_bases")
    return _revision_number("bowhead_core.migrations.versions.0199_knowledge_source_bindings")


async def require_knowledge_migration(db, policy: KnowledgeAccess | None = None) -> None:
    try:
        async with db.execute("SELECT version_num FROM alembic_version") as cursor:
            row = await cursor.fetchone()
        if row is None or int(str(row[0]).split("_", 1)[0]) < required_revision_number(policy):
            raise ValueError()
    except Exception:
        raise KnowledgeAccessError("agent_knowledge_access_migration_required") from None


def require_knowledge_base_mode(policy: KnowledgeAccess | None) -> None:
    """A knowledge base selection needs ``FEATURE_KNOWLEDGE_BASES``; off, it is refused and never widened."""
    if policy is not None and policy.knowledge_base_ids is not None and not knowledge_base_mode_enabled():
        raise KnowledgeAccessError("agent_knowledge_bases_disabled")


async def require_knowledge_admission(db, policy: KnowledgeAccess | None) -> None:
    """Admission of a policy for a save or a new execution: the migration it needs, and its switch."""
    require_knowledge_base_mode(policy)
    await require_knowledge_migration(db, policy)


def _ceiling_widened(before: tuple[str, ...] | None, after: tuple[str, ...] | None) -> bool:
    return before is not None and (after is None or not set(after).issubset(before))


def knowledge_access_expanded(previous: Mapping, candidate: Mapping) -> bool:
    """Whether the candidate can read something the previous policy could not, for release review.

    Each ceiling of the previous policy counts on its own: removing it or adding to it widens. So
    switching from selected files to selected knowledge bases is an expansion (files added to them
    later are read), and so is adding a knowledge base or leaving selected knowledge bases for
    selected files or for all knowledge the user can use. Removing knowledge bases narrows, and
    switching from all knowledge the user can use to any selection narrows too.
    """
    before = require_supported_knowledge_access(previous)
    after = require_supported_knowledge_access(candidate)
    if before is None:
        return False
    return _ceiling_widened(before.file_ids, after.file_ids if after else None) or _ceiling_widened(
        before.knowledge_base_ids, after.knowledge_base_ids if after else None
    )
