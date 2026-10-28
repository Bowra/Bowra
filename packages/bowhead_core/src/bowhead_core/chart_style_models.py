"""Domain records and concurrency tokens for team chart-style authoring.

These objects describe presentation-only state.  They intentionally do not
import chart specifications, renderer options, or generation code.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final

from bowhead_core.theme_compiler import canonical_json

STYLE_SCHEMA_VERSION: Final = 2
TOKEN_SCHEMA_VERSION: Final = 2
RESOLVER_POLICY_VERSION: Final = "chart-style/2.0"
VALIDATION_POLICY_VERSION: Final = "chart-style-validation/1"
MAX_TEAM_CHART_STYLES: Final = 12


class ChartStyleDomainError(ValueError):
    """Stable error raised by the chart-style domain and transaction layer."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 409,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class TeamChartStyle:
    id: str
    team_id: str
    lifecycle: str
    active_revision_id: str | None
    active_name_normalized: str | None
    fallback_builtin_preset_id: str
    row_version: int
    create_request_id: str | None
    create_request_hash: str | None
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TeamChartStyleDraft:
    style_id: str
    team_id: str
    schema_version: int
    based_on_revision_id: str | None
    base_preset_id: str
    base_preset_revision: str
    base_preset_checksum: str
    authoring_base_snapshot: dict[str, Any]
    authoring_base_snapshot_checksum: str
    name: str
    description: str
    overrides: dict[str, Any]
    row_version: int
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str

    @property
    def source(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "description": self.description,
            "overrides": self.overrides,
        }


@dataclass(frozen=True, slots=True)
class TeamChartStyleRevision:
    id: str
    team_id: str
    style_id: str
    sequence: int
    schema_version: int
    token_schema_version: int
    resolver_policy_version: str
    name: str
    name_normalized: str
    description: str
    base_preset_id: str
    base_preset_revision: str
    base_preset_checksum: str
    authoring_base_snapshot: dict[str, Any]
    authoring_base_snapshot_checksum: str
    overrides: dict[str, Any]
    resolved_snapshot: dict[str, Any]
    snapshot_checksum: str
    revision_checksum: str
    validation_policy_version: str
    validation: dict[str, Any]
    operation: str
    source_revision_id: str | None
    created_by: str
    created_at: str

    @property
    def source(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "description": self.description,
            "overrides": self.overrides,
        }


def sha256_json(value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def draft_etag(draft: TeamChartStyleDraft | None) -> str | None:
    if draft is None:
        return None
    digest = sha256_json(draft.source).removeprefix("sha256:")
    return f'"chart-style-draft:{draft.style_id}:{draft.row_version}:{digest}"'


def head_etag(style: TeamChartStyle) -> str:
    active_revision = style.active_revision_id or "none"
    return f'"chart-style-head:{style.id}:{style.row_version}:{active_revision}"'


def resource_etag(style: TeamChartStyle, draft: TeamChartStyleDraft | None) -> str:
    digest = sha256_json(
        {
            "styleId": style.id,
            "headRowVersion": style.row_version,
            "activeRevisionId": style.active_revision_id,
            "draftRowVersion": draft.row_version if draft else None,
            "draftContentChecksum": sha256_json(draft.source) if draft else None,
        }
    ).removeprefix("sha256:")
    return f'"chart-style-resource:{style.id}:{digest}"'


def chart_style_state(style: TeamChartStyle, draft: TeamChartStyleDraft | None) -> str:
    if style.active_revision_id is None:
        return "draft_only"
    suffix = "editing" if draft is not None else "clean"
    return f"{style.lifecycle}_{suffix}"


def _decision(allowed: bool, reason: str | None = None) -> dict[str, Any]:
    return {"allowed": allowed, "reason": None if allowed else reason}


def operation_capabilities(
    style: TeamChartStyle,
    draft: TeamChartStyleDraft | None,
    *,
    default_style_id: str | None = None,
    policy_minimum_blocked: bool = False,
    limit_reached: bool = False,
) -> dict[str, dict[str, Any]]:
    """Project UI guidance from the authoritative lifecycle state."""

    published = style.active_revision_id is not None
    has_draft = draft is not None
    is_active = style.lifecycle == "active"
    create_draft = (
        _decision(False, "never_published")
        if not published
        else _decision(False, "draft_exists")
        if has_draft
        else _decision(False, "limit_reached")
        if limit_reached
        else _decision(True)
    )
    draft_decision = _decision(True) if has_draft else _decision(False, "draft_required")
    restore = _decision(False, "never_published") if not published else _decision(False, "clean_state_required") if has_draft else _decision(True)
    activate = (
        _decision(False, "never_published")
        if not published
        else _decision(False, "already_active")
        if is_active
        else _decision(False, "limit_reached")
        if limit_reached
        else _decision(True)
    )
    deactivate = (
        _decision(False, "never_published")
        if not published
        else _decision(False, "already_inactive")
        if not is_active
        else _decision(False, "default_style")
        if default_style_id == style.id
        else _decision(False, "policy_minimum")
        if policy_minimum_blocked
        else _decision(True)
    )
    return {
        "createDraft": create_draft,
        "saveDraft": draft_decision,
        "discardDraft": draft_decision,
        "publish": draft_decision,
        "restore": restore,
        "activate": activate,
        "deactivate": deactivate,
        "deleteStyle": _decision(not published and has_draft, "published_style" if published else "draft_required"),
    }


def revision_to_dict(revision: TeamChartStyleRevision | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    return {
        "schemaVersion": revision.schema_version,
        "id": revision.id,
        "styleId": revision.style_id,
        "sequence": revision.sequence,
        "tokenSchemaVersion": revision.token_schema_version,
        "resolverPolicyVersion": revision.resolver_policy_version,
        "name": revision.name,
        "description": revision.description,
        "basePreset": {
            "id": revision.base_preset_id,
            "revision": revision.base_preset_revision,
            "checksum": revision.base_preset_checksum,
        },
        "authoringBaseSnapshot": revision.authoring_base_snapshot,
        "authoringBaseSnapshotChecksum": revision.authoring_base_snapshot_checksum,
        "overrides": revision.overrides,
        "resolvedSnapshot": revision.resolved_snapshot,
        "snapshotChecksum": revision.snapshot_checksum,
        "revisionChecksum": revision.revision_checksum,
        "validationPolicyVersion": revision.validation_policy_version,
        "validation": revision.validation,
        "operation": revision.operation,
        "sourceRevisionId": revision.source_revision_id,
        "createdBy": revision.created_by,
        "createdAt": revision.created_at,
    }


def detail_to_dict(
    style: TeamChartStyle,
    draft: TeamChartStyleDraft | None,
    revision: TeamChartStyleRevision | None,
    *,
    capabilities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "style": {
            "schemaVersion": STYLE_SCHEMA_VERSION,
            "id": style.id,
            "lifecycle": style.lifecycle,
            "activeRevisionId": style.active_revision_id,
            "fallbackBuiltInPresetId": style.fallback_builtin_preset_id,
            "rowVersion": style.row_version,
            "createdBy": style.created_by,
            "createdAt": style.created_at,
            "updatedBy": style.updated_by,
            "updatedAt": style.updated_at,
        },
        "state": chart_style_state(style, draft),
        "activeRevision": revision_to_dict(revision),
        "draft": None
        if draft is None
        else {
            "source": draft.source,
            "basedOnRevisionId": draft.based_on_revision_id,
            "basePreset": {
                "id": draft.base_preset_id,
                "revision": draft.base_preset_revision,
                "checksum": draft.base_preset_checksum,
            },
            "rowVersion": draft.row_version,
            "updatedBy": draft.updated_by,
            "updatedAt": draft.updated_at,
        },
        "headEtag": head_etag(style),
        "draftEtag": draft_etag(draft),
        "resourceEtag": resource_etag(style, draft),
        "capabilities": capabilities,
    }


__all__ = [
    "MAX_TEAM_CHART_STYLES",
    "RESOLVER_POLICY_VERSION",
    "STYLE_SCHEMA_VERSION",
    "TOKEN_SCHEMA_VERSION",
    "VALIDATION_POLICY_VERSION",
    "ChartStyleDomainError",
    "TeamChartStyle",
    "TeamChartStyleDraft",
    "TeamChartStyleRevision",
    "chart_style_state",
    "detail_to_dict",
    "draft_etag",
    "head_etag",
    "operation_capabilities",
    "resource_etag",
    "revision_to_dict",
    "sha256_json",
]
