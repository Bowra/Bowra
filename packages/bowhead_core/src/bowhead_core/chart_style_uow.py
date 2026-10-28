"""Atomic lifecycle operations for team chart-style authoring."""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.chart_style import KEY_TEAM_CHART_STYLE_POLICY, default_team_chart_style_policy
from bowhead_core.chart_style_materializer import (
    built_in_authoring_base,
    evaluate_chart_style_candidate,
    normalize_authoring_source,
    normalize_style_name,
    revision_checksum,
)
from bowhead_core.chart_style_models import (
    MAX_TEAM_CHART_STYLES,
    RESOLVER_POLICY_VERSION,
    STYLE_SCHEMA_VERSION,
    TOKEN_SCHEMA_VERSION,
    VALIDATION_POLICY_VERSION,
    ChartStyleDomainError,
    TeamChartStyle,
    TeamChartStyleDraft,
    TeamChartStyleRevision,
    detail_to_dict,
    draft_etag,
    head_etag,
    operation_capabilities,
    resource_etag,
    sha256_json,
)
from bowhead_core.chart_style_read_service import custom_style_projection
from bowhead_core.chart_style_registry_generated import BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM
from bowhead_core.chart_style_store import (
    ChartStyleStore,
    current_revision,
    draft_by_style,
    registry_version,
    revision_by_id,
    style_by_id,
)
from bowhead_core.chart_style_v2 import (
    chart_style_policy_etag_v2,
    is_custom_chart_style_id,
    normalize_team_chart_style_policy_v2,
    parse_team_chart_style_policy_v2,
)
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.theme_compiler import canonical_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ChartStyleMutationResult:
    detail: dict[str, Any]
    registry_version: int
    response_etag: str
    performed: bool = True

    def body(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "registryVersion": self.registry_version,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
        }


@dataclass(frozen=True, slots=True)
class ChartStylePolicyMutationResult:
    policy: dict[str, Any]
    policy_etag: str
    registry_version: int
    before_raw: str | None

    def body(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "etag": self.policy_etag,
            "registryVersion": self.registry_version,
            "builtInRegistryChecksum": BUILT_IN_CHART_STYLE_REGISTRY_CHECKSUM,
        }


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


def _style_id() -> str:
    return f"tcs_{secrets.token_hex(12)}"


def _revision_id() -> str:
    return f"tcsr_{secrets.token_hex(12)}"


def _require_request_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ChartStyleDomainError(
            "chart_style_invalid_request_id",
            status_code=400,
        ) from exc
    return str(parsed)


def _policy_projection(raw: str | None) -> tuple[str, list[str], dict[str, Any]]:
    if raw is None:
        policy = default_team_chart_style_policy()
        return policy["defaultPresetId"], list(policy["enabledPresetIds"]), policy
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChartStyleDomainError("chart_style_policy_corrupt", status_code=409) from exc
    if not isinstance(value, dict):
        raise ChartStyleDomainError("chart_style_policy_corrupt", status_code=409)
    if value.get("version") == 2:
        default_id = value.get("defaultStyleId")
        enabled = value.get("enabledStyleIds")
    elif value.get("version") == 1:
        default_id = value.get("defaultPresetId")
        enabled = value.get("enabledPresetIds")
    else:
        raise ChartStyleDomainError("chart_style_policy_corrupt", status_code=409)
    if (
        not isinstance(default_id, str)
        or not default_id
        or not isinstance(enabled, list)
        or any(not isinstance(item, str) or not item for item in enabled)
        or len(enabled) < 2
        or len(enabled) != len(set(enabled))
        or default_id not in enabled
    ):
        raise ChartStyleDomainError("chart_style_policy_corrupt", status_code=409)
    return default_id, list(enabled), value


def _require_precondition(if_match: str | None, current: str, *, style: TeamChartStyle) -> None:
    if if_match is None:
        raise ChartStyleDomainError("chart_style_precondition_required", status_code=428)
    if if_match != current:
        raise ChartStyleDomainError(
            "chart_style_precondition_failed",
            status_code=412,
            details={
                "currentEtag": current,
                "headSummary": {
                    "styleId": style.id,
                    "lifecycle": style.lifecycle,
                    "activeRevisionId": style.active_revision_id,
                    "rowVersion": style.row_version,
                },
            },
        )


async def _count_toward_limit(db: aiosqlite.Connection, *, team_id: str) -> int:
    async with db.execute(
        "SELECT COUNT(*) FROM team_chart_styles s WHERE s.team_id=? AND "
        "(s.lifecycle='active' OR EXISTS(SELECT 1 FROM team_chart_style_drafts d "
        "WHERE d.team_id=s.team_id AND d.style_id=s.id))",
        (team_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0])


async def _ensure_capacity(db: aiosqlite.Connection, *, team_id: str) -> None:
    if await _count_toward_limit(db, team_id=team_id) >= MAX_TEAM_CHART_STYLES:
        raise ChartStyleDomainError(
            "chart_style_limit_reached",
            details={"maximum": MAX_TEAM_CHART_STYLES},
        )


async def _bump_registry(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    actor_id: str,
    now: str,
) -> int:
    await db.execute(
        "INSERT INTO team_chart_style_registry_state(team_id,registry_version,updated_by,updated_at) "
        "VALUES (?,1,?,?) ON CONFLICT(team_id) DO UPDATE SET "
        "registry_version=team_chart_style_registry_state.registry_version+1,"
        "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (team_id, actor_id, now),
    )
    return await registry_version(db, team_id=team_id)


async def _style_or_404(db: aiosqlite.Connection, *, team_id: str, style_id: str) -> TeamChartStyle:
    style = await style_by_id(db, team_id=team_id, style_id=style_id)
    if style is None:
        raise ChartStyleDomainError("chart_style_not_found", status_code=404)
    return style


async def _draft_or_conflict(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    style_id: str,
) -> TeamChartStyleDraft:
    draft = await draft_by_style(db, team_id=team_id, style_id=style_id)
    if draft is None:
        raise ChartStyleDomainError("chart_style_draft_required")
    return draft


async def _insert_revision(
    db: aiosqlite.Connection,
    *,
    style: TeamChartStyle,
    source: dict[str, Any],
    base_preset_id: str,
    base_preset_revision: str,
    base_preset_checksum: str,
    authoring_base_snapshot: dict[str, Any],
    authoring_base_snapshot_checksum: str,
    evaluation: dict[str, Any],
    operation: str,
    source_revision_id: str | None,
    actor_id: str,
    now: str,
) -> TeamChartStyleRevision:
    snapshot = evaluation["candidateSnapshot"]
    snapshot_checksum = evaluation["candidateSnapshotChecksum"]
    if not isinstance(snapshot, dict) or not isinstance(snapshot_checksum, str):
        raise ChartStyleDomainError(
            "chart_style_publish_blocked",
            status_code=400,
            details={"validation": evaluation["validation"]},
        )
    name, normalized_name = normalize_style_name(source["name"])
    if style.lifecycle == "active":
        async with db.execute(
            "SELECT id FROM team_chart_styles WHERE team_id=? AND lifecycle='active' AND active_name_normalized=? AND id<>? LIMIT 1",
            (style.team_id, normalized_name, style.id),
        ) as cursor:
            if await cursor.fetchone() is not None:
                raise ChartStyleDomainError("chart_style_name_conflict")
    async with db.execute(
        "SELECT COALESCE(MAX(sequence),0)+1 FROM team_chart_style_revisions WHERE team_id=? AND style_id=?",
        (style.team_id, style.id),
    ) as cursor:
        row = await cursor.fetchone()
    sequence = int(row[0])
    revision_id = _revision_id()
    validation = {
        **evaluation["validation"],
        "validationFingerprint": evaluation["validationFingerprint"],
    }
    checksum = revision_checksum(
        source=source,
        base_preset_id=base_preset_id,
        base_preset_revision=base_preset_revision,
        base_preset_checksum=base_preset_checksum,
        authoring_base_snapshot=authoring_base_snapshot,
        authoring_base_snapshot_checksum=authoring_base_snapshot_checksum,
        resolved_snapshot=snapshot,
        snapshot_checksum=snapshot_checksum,
        validation=validation,
    )
    await db.execute(
        "INSERT INTO team_chart_style_revisions("
        "id,team_id,style_id,sequence,schema_version,token_schema_version,resolver_policy_version,"
        "name,name_normalized,description,base_preset_id,base_preset_revision,base_preset_checksum,"
        "authoring_base_snapshot_json,authoring_base_snapshot_checksum,overrides_json,"
        "resolved_snapshot_json,snapshot_checksum,revision_checksum,validation_policy_version,"
        "validation_json,operation,source_revision_id,created_by,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            revision_id,
            style.team_id,
            style.id,
            sequence,
            STYLE_SCHEMA_VERSION,
            TOKEN_SCHEMA_VERSION,
            RESOLVER_POLICY_VERSION,
            name,
            normalized_name,
            source["description"],
            base_preset_id,
            base_preset_revision,
            base_preset_checksum,
            canonical_json(authoring_base_snapshot),
            authoring_base_snapshot_checksum,
            canonical_json(source["overrides"]),
            canonical_json(snapshot),
            snapshot_checksum,
            checksum,
            VALIDATION_POLICY_VERSION,
            canonical_json(validation),
            operation,
            source_revision_id,
            actor_id,
            now,
        ),
    )
    revision = await revision_by_id(
        db,
        team_id=style.team_id,
        style_id=style.id,
        revision_id=revision_id,
    )
    if revision is None:  # pragma: no cover - SQLite returned a successful insert
        raise RuntimeError("chart-style revision insert was not readable")
    return revision


class ChartStyleUnitOfWork:
    def __init__(self, db_path: str | Path, *, now_fn: Callable[[], str] = _utc_now) -> None:
        self.db_path = str(db_path)
        self.store = ChartStyleStore(db_path)
        self._now_fn = now_fn

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            else:
                await db.commit()

    async def _detail(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        style_id: str,
    ) -> dict[str, Any]:
        style = await _style_or_404(db, team_id=team_id, style_id=style_id)
        draft = await draft_by_style(db, team_id=team_id, style_id=style_id)
        revision = await current_revision(db, style=style)
        raw_policy = await TeamSettingsStore.get_with_connection(db, team_id, KEY_TEAM_CHART_STYLE_POLICY)
        default_style_id, enabled, _ = _policy_projection(raw_policy)
        limit_reached = await _count_toward_limit(db, team_id=team_id) >= MAX_TEAM_CHART_STYLES
        policy_minimum = style.id in enabled and len(enabled) <= 2
        capabilities = operation_capabilities(
            style,
            draft,
            default_style_id=default_style_id,
            policy_minimum_blocked=policy_minimum,
            limit_reached=limit_reached and style.lifecycle == "inactive" and draft is None,
        )
        return detail_to_dict(style, draft, revision, capabilities=capabilities)

    async def detail(self, *, team_id: str, style_id: str) -> dict[str, Any]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await self._detail(db, team_id=team_id, style_id=style_id)

    async def set_policy_v2(
        self,
        *,
        team_id: str,
        actor_id: str,
        policy: Any,
        if_match: str | None,
    ) -> ChartStylePolicyMutationResult:
        normalized = normalize_team_chart_style_policy_v2(canonical_json(policy))
        parsed = parse_team_chart_style_policy_v2(normalized)
        now = self._now_fn()
        async with self._transaction() as db:
            before_raw = await TeamSettingsStore.get_with_connection(
                db,
                team_id,
                KEY_TEAM_CHART_STYLE_POLICY,
            )
            current_etag = chart_style_policy_etag_v2(before_raw)
            if if_match is None:
                raise ChartStyleDomainError(
                    "chart_style_policy_precondition_required",
                    status_code=428,
                )
            if if_match != current_etag:
                raise ChartStyleDomainError(
                    "chart_style_policy_precondition_failed",
                    status_code=412,
                    details={"currentEtag": current_etag},
                )
            for style_id in parsed["enabledStyleIds"]:
                if not is_custom_chart_style_id(style_id):
                    continue
                projection, _ = await custom_style_projection(
                    db,
                    team_id=team_id,
                    style_id=style_id,
                )
                if projection is None:
                    raise ChartStyleDomainError(
                        "chart_style_not_found",
                        status_code=404,
                    )
            await TeamSettingsStore.set_with_connection(
                db,
                team_id,
                KEY_TEAM_CHART_STYLE_POLICY,
                normalized,
                actor_id,
                updated_at=now,
            )
            version = await _bump_registry(
                db,
                team_id=team_id,
                actor_id=actor_id,
                now=now,
            )
            return ChartStylePolicyMutationResult(
                policy=parsed,
                policy_etag=chart_style_policy_etag_v2(normalized),
                registry_version=version,
                before_raw=before_raw,
            )

    async def create(
        self,
        *,
        team_id: str,
        actor_id: str,
        request_id: str,
        base_preset_id: str,
        source: Any,
    ) -> ChartStyleMutationResult:
        normalized_request_id = _require_request_id(request_id)
        normalized_source = normalize_authoring_source(source)
        request_hash = sha256_json({"basePresetId": base_preset_id, "normalizedSource": normalized_source})
        authoring_base, preset_revision, preset_checksum = built_in_authoring_base(base_preset_id)
        authoring_checksum = sha256_json(authoring_base)
        now = self._now_fn()
        async with self._transaction() as db:
            async with db.execute(
                "SELECT * FROM team_chart_styles WHERE team_id=? AND created_by=? AND create_request_id=?",
                (team_id, actor_id, normalized_request_id),
            ) as cursor:
                existing_row = await cursor.fetchone()
            if existing_row is not None:
                existing = TeamChartStyle(**dict(existing_row))
                if existing.create_request_hash != request_hash:
                    raise ChartStyleDomainError("chart_style_idempotency_conflict")
                detail = await self._detail(db, team_id=team_id, style_id=existing.id)
                response_etag = detail["draftEtag"] or detail["headEtag"]
                return ChartStyleMutationResult(
                    detail,
                    await registry_version(db, team_id=team_id),
                    response_etag,
                    performed=False,
                )
            await _ensure_capacity(db, team_id=team_id)
            style_id = _style_id()
            await db.execute(
                "INSERT INTO team_chart_styles("
                "id,team_id,lifecycle,fallback_builtin_preset_id,row_version,create_request_id,create_request_hash,"
                "created_by,created_at,updated_by,updated_at) VALUES (?,?,?,?,1,?,?,?,?,?,?)",
                (
                    style_id,
                    team_id,
                    "active",
                    base_preset_id,
                    normalized_request_id,
                    request_hash,
                    actor_id,
                    now,
                    actor_id,
                    now,
                ),
            )
            await db.execute(
                "INSERT INTO team_chart_style_drafts("
                "style_id,team_id,schema_version,based_on_revision_id,base_preset_id,base_preset_revision,"
                "base_preset_checksum,authoring_base_snapshot_json,authoring_base_snapshot_checksum,"
                "name,description,overrides_json,row_version,created_by,created_at,updated_by,updated_at"
                ") VALUES (?,?,2,NULL,?,?,?,?,?,?,?, ?,1,?,?,?,?)",
                (
                    style_id,
                    team_id,
                    base_preset_id,
                    preset_revision,
                    preset_checksum,
                    canonical_json(authoring_base),
                    authoring_checksum,
                    normalized_source["name"],
                    normalized_source["description"],
                    canonical_json(normalized_source["overrides"]),
                    actor_id,
                    now,
                    actor_id,
                    now,
                ),
            )
            detail = await self._detail(db, team_id=team_id, style_id=style_id)
            return ChartStyleMutationResult(detail, await registry_version(db, team_id=team_id), str(detail["draftEtag"]))

    async def create_draft(
        self,
        *,
        team_id: str,
        actor_id: str,
        style_id: str,
        if_match: str | None,
    ) -> ChartStyleMutationResult:
        now = self._now_fn()
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, head_etag(style), style=style)
            if style.active_revision_id is None:
                raise ChartStyleDomainError("chart_style_never_published")
            if await draft_by_style(db, team_id=team_id, style_id=style_id) is not None:
                raise ChartStyleDomainError("chart_style_draft_exists")
            if style.lifecycle == "inactive":
                await _ensure_capacity(db, team_id=team_id)
            revision = await current_revision(db, style=style)
            if revision is None:
                raise ChartStyleDomainError("chart_style_active_revision_changed")
            await db.execute(
                "INSERT INTO team_chart_style_drafts("
                "style_id,team_id,schema_version,based_on_revision_id,base_preset_id,base_preset_revision,"
                "base_preset_checksum,authoring_base_snapshot_json,authoring_base_snapshot_checksum,"
                "name,description,overrides_json,row_version,created_by,created_at,updated_by,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                (
                    style.id,
                    team_id,
                    STYLE_SCHEMA_VERSION,
                    revision.id,
                    revision.base_preset_id,
                    revision.base_preset_revision,
                    revision.base_preset_checksum,
                    canonical_json(revision.authoring_base_snapshot),
                    revision.authoring_base_snapshot_checksum,
                    revision.name,
                    revision.description,
                    canonical_json(revision.overrides),
                    actor_id,
                    now,
                    actor_id,
                    now,
                ),
            )
            detail = await self._detail(db, team_id=team_id, style_id=style_id)
            return ChartStyleMutationResult(detail, await registry_version(db, team_id=team_id), str(detail["draftEtag"]))

    async def replace_draft(
        self,
        *,
        team_id: str,
        actor_id: str,
        style_id: str,
        if_match: str | None,
        source: Any,
    ) -> ChartStyleMutationResult:
        normalized = normalize_authoring_source(source)
        now = self._now_fn()
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            draft = await _draft_or_conflict(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, str(draft_etag(draft)), style=style)
            cursor = await db.execute(
                "UPDATE team_chart_style_drafts SET name=?,description=?,overrides_json=?,"
                "row_version=row_version+1,updated_by=?,updated_at=? "
                "WHERE team_id=? AND style_id=? AND row_version=?",
                (
                    normalized["name"],
                    normalized["description"],
                    canonical_json(normalized["overrides"]),
                    actor_id,
                    now,
                    team_id,
                    style_id,
                    draft.row_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ChartStyleDomainError("chart_style_precondition_failed", status_code=412)
            detail = await self._detail(db, team_id=team_id, style_id=style_id)
            return ChartStyleMutationResult(detail, await registry_version(db, team_id=team_id), str(detail["draftEtag"]))

    async def discard_draft(
        self,
        *,
        team_id: str,
        style_id: str,
        if_match: str | None,
    ) -> ChartStyleMutationResult:
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            draft = await _draft_or_conflict(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, str(draft_etag(draft)), style=style)
            if style.active_revision_id is None:
                raise ChartStyleDomainError("chart_style_never_published")
            await db.execute(
                "DELETE FROM team_chart_style_drafts WHERE team_id=? AND style_id=? AND row_version=?",
                (team_id, style_id, draft.row_version),
            )
            detail = await self._detail(db, team_id=team_id, style_id=style_id)
            return ChartStyleMutationResult(detail, await registry_version(db, team_id=team_id), str(detail["headEtag"]))

    async def preview(self, *, team_id: str, style_id: str, source: Any) -> dict[str, Any]:
        style, draft, revision = await self.store.detail(team_id=team_id, style_id=style_id)
        base_owner = draft or revision
        if base_owner is None:
            raise ChartStyleDomainError("chart_style_draft_required")
        return evaluate_chart_style_candidate(
            source=source,
            authoring_base_snapshot=base_owner.authoring_base_snapshot,
            authoring_base_snapshot_checksum=base_owner.authoring_base_snapshot_checksum,
        )

    async def publish(
        self,
        *,
        team_id: str,
        actor_id: str,
        style_id: str,
        if_match: str | None,
        acknowledged_validation_fingerprint: str | None,
    ) -> ChartStyleMutationResult:
        now = self._now_fn()
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            draft = await _draft_or_conflict(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, str(draft_etag(draft)), style=style)
            if draft.based_on_revision_id != style.active_revision_id:
                raise ChartStyleDomainError("chart_style_active_revision_changed")
            evaluation = evaluate_chart_style_candidate(
                source=draft.source,
                authoring_base_snapshot=draft.authoring_base_snapshot,
                authoring_base_snapshot_checksum=draft.authoring_base_snapshot_checksum,
            )
            if evaluation["validation"]["blockers"]:
                raise ChartStyleDomainError(
                    "chart_style_publish_blocked",
                    status_code=400,
                    details={"validation": evaluation["validation"]},
                )
            if evaluation["validation"]["warnings"] and acknowledged_validation_fingerprint != evaluation["validationFingerprint"]:
                raise ChartStyleDomainError(
                    "chart_style_warning_ack_required",
                    details={"validationFingerprint": evaluation["validationFingerprint"], "validation": evaluation["validation"]},
                )
            revision = await _insert_revision(
                db,
                style=style,
                source=evaluation["normalizedAuthoringSource"],
                base_preset_id=draft.base_preset_id,
                base_preset_revision=draft.base_preset_revision,
                base_preset_checksum=draft.base_preset_checksum,
                authoring_base_snapshot=draft.authoring_base_snapshot,
                authoring_base_snapshot_checksum=draft.authoring_base_snapshot_checksum,
                evaluation=evaluation,
                operation="publish",
                source_revision_id=None,
                actor_id=actor_id,
                now=now,
            )
            cursor = await db.execute(
                "UPDATE team_chart_styles SET active_revision_id=?,active_name_normalized=?,"
                "row_version=row_version+1,updated_by=?,updated_at=? "
                "WHERE team_id=? AND id=? AND row_version=?",
                (revision.id, revision.name_normalized, actor_id, now, team_id, style.id, style.row_version),
            )
            if cursor.rowcount != 1:
                raise ChartStyleDomainError("chart_style_active_revision_changed")
            await db.execute(
                "DELETE FROM team_chart_style_drafts WHERE team_id=? AND style_id=? AND row_version=?",
                (team_id, style.id, draft.row_version),
            )
            version = await _bump_registry(db, team_id=team_id, actor_id=actor_id, now=now)
            detail = await self._detail(db, team_id=team_id, style_id=style.id)
            return ChartStyleMutationResult(detail, version, str(detail["headEtag"]))

    async def restore(
        self,
        *,
        team_id: str,
        actor_id: str,
        style_id: str,
        revision_id: str,
        if_match: str | None,
        acknowledged_validation_fingerprint: str | None,
    ) -> ChartStyleMutationResult:
        now = self._now_fn()
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, head_etag(style), style=style)
            if style.active_revision_id is None:
                raise ChartStyleDomainError("chart_style_never_published")
            if await draft_by_style(db, team_id=team_id, style_id=style_id) is not None:
                raise ChartStyleDomainError("chart_style_draft_requires_resolution")
            target = await revision_by_id(db, team_id=team_id, style_id=style_id, revision_id=revision_id)
            if target is None:
                raise ChartStyleDomainError("chart_style_not_found", status_code=404)
            evaluation = evaluate_chart_style_candidate(
                source=target.source,
                authoring_base_snapshot=target.authoring_base_snapshot,
                authoring_base_snapshot_checksum=target.authoring_base_snapshot_checksum,
            )
            if evaluation["validation"]["blockers"]:
                raise ChartStyleDomainError(
                    "chart_style_restore_blocked",
                    status_code=400,
                    details={"validation": evaluation["validation"]},
                )
            if evaluation["validation"]["warnings"] and acknowledged_validation_fingerprint != evaluation["validationFingerprint"]:
                raise ChartStyleDomainError(
                    "chart_style_warning_ack_required",
                    details={"validationFingerprint": evaluation["validationFingerprint"], "validation": evaluation["validation"]},
                )
            restored = await _insert_revision(
                db,
                style=style,
                source=evaluation["normalizedAuthoringSource"],
                base_preset_id=target.base_preset_id,
                base_preset_revision=target.base_preset_revision,
                base_preset_checksum=target.base_preset_checksum,
                authoring_base_snapshot=target.authoring_base_snapshot,
                authoring_base_snapshot_checksum=target.authoring_base_snapshot_checksum,
                evaluation=evaluation,
                operation="restore",
                source_revision_id=target.id,
                actor_id=actor_id,
                now=now,
            )
            await db.execute(
                "UPDATE team_chart_styles SET active_revision_id=?,active_name_normalized=?,"
                "row_version=row_version+1,updated_by=?,updated_at=? "
                "WHERE team_id=? AND id=? AND row_version=?",
                (restored.id, restored.name_normalized, actor_id, now, team_id, style.id, style.row_version),
            )
            version = await _bump_registry(db, team_id=team_id, actor_id=actor_id, now=now)
            detail = await self._detail(db, team_id=team_id, style_id=style.id)
            return ChartStyleMutationResult(detail, version, str(detail["headEtag"]))

    async def deactivate(
        self,
        *,
        team_id: str,
        actor_id: str,
        style_id: str,
        if_match: str | None,
    ) -> ChartStyleMutationResult:
        now = self._now_fn()
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, head_etag(style), style=style)
            if style.active_revision_id is None:
                raise ChartStyleDomainError("chart_style_never_published")
            if style.lifecycle == "inactive":
                raise ChartStyleDomainError("chart_style_already_inactive")
            raw_policy = await TeamSettingsStore.get_with_connection(db, team_id, KEY_TEAM_CHART_STYLE_POLICY)
            default_id, enabled, policy = _policy_projection(raw_policy)
            if default_id == style.id:
                raise ChartStyleDomainError("chart_style_default_cannot_deactivate")
            if style.id in enabled:
                remaining = [item for item in enabled if item != style.id]
                if len(remaining) < 2:
                    raise ChartStyleDomainError("chart_style_policy_minimum")
                if policy["version"] != 2:
                    raise ChartStyleDomainError("chart_style_policy_corrupt")
                policy["enabledStyleIds"] = remaining
                await TeamSettingsStore.set_with_connection(
                    db,
                    team_id,
                    KEY_TEAM_CHART_STYLE_POLICY,
                    canonical_json(policy),
                    actor_id,
                    updated_at=now,
                )
            await db.execute(
                "UPDATE team_chart_styles SET lifecycle='inactive',row_version=row_version+1,updated_by=?,updated_at=? "
                "WHERE team_id=? AND id=? AND row_version=?",
                (actor_id, now, team_id, style.id, style.row_version),
            )
            version = await _bump_registry(db, team_id=team_id, actor_id=actor_id, now=now)
            detail = await self._detail(db, team_id=team_id, style_id=style.id)
            return ChartStyleMutationResult(detail, version, str(detail["headEtag"]))

    async def activate(
        self,
        *,
        team_id: str,
        actor_id: str,
        style_id: str,
        if_match: str | None,
    ) -> ChartStyleMutationResult:
        now = self._now_fn()
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, head_etag(style), style=style)
            if style.active_revision_id is None:
                raise ChartStyleDomainError("chart_style_never_published")
            if style.lifecycle == "active":
                raise ChartStyleDomainError("chart_style_already_active")
            if await draft_by_style(db, team_id=team_id, style_id=style_id) is None:
                await _ensure_capacity(db, team_id=team_id)
            if style.active_name_normalized is None:
                raise ChartStyleDomainError("chart_style_active_revision_changed")
            async with db.execute(
                "SELECT id FROM team_chart_styles WHERE team_id=? AND lifecycle='active' AND active_name_normalized=? AND id<>? LIMIT 1",
                (team_id, style.active_name_normalized, style.id),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    raise ChartStyleDomainError("chart_style_name_conflict")
            await db.execute(
                "UPDATE team_chart_styles SET lifecycle='active',row_version=row_version+1,updated_by=?,updated_at=? "
                "WHERE team_id=? AND id=? AND row_version=?",
                (actor_id, now, team_id, style.id, style.row_version),
            )
            version = await _bump_registry(db, team_id=team_id, actor_id=actor_id, now=now)
            detail = await self._detail(db, team_id=team_id, style_id=style.id)
            return ChartStyleMutationResult(detail, version, str(detail["headEtag"]))

    async def delete_draft_only(
        self,
        *,
        team_id: str,
        style_id: str,
        if_match: str | None,
    ) -> None:
        async with self._transaction() as db:
            style = await _style_or_404(db, team_id=team_id, style_id=style_id)
            draft = await draft_by_style(db, team_id=team_id, style_id=style_id)
            _require_precondition(if_match, resource_etag(style, draft), style=style)
            if style.active_revision_id is not None or draft is None:
                raise ChartStyleDomainError("chart_style_delete_requires_draft_only")
            await db.execute(
                "DELETE FROM team_chart_styles WHERE team_id=? AND id=? AND row_version=?",
                (team_id, style.id, style.row_version),
            )


__all__ = [
    "ChartStyleMutationResult",
    "ChartStylePolicyMutationResult",
    "ChartStyleUnitOfWork",
]
