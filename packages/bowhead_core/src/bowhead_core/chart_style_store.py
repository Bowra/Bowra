"""Team-scoped repository helpers for chart-style authoring state."""

from __future__ import annotations

import base64
import binascii
import json
from typing import TYPE_CHECKING, Any

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.chart_style_models import (
    ChartStyleDomainError,
    TeamChartStyle,
    TeamChartStyleDraft,
    TeamChartStyleRevision,
    chart_style_state,
    draft_etag,
    head_etag,
    resource_etag,
    revision_to_dict,
)

if TYPE_CHECKING:
    from pathlib import Path


def _json_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("stored chart-style JSON must be an object")
    return value


def _style(row: aiosqlite.Row) -> TeamChartStyle:
    return TeamChartStyle(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        lifecycle=str(row["lifecycle"]),
        active_revision_id=str(row["active_revision_id"]) if row["active_revision_id"] is not None else None,
        active_name_normalized=str(row["active_name_normalized"]) if row["active_name_normalized"] is not None else None,
        fallback_builtin_preset_id=str(row["fallback_builtin_preset_id"]),
        row_version=int(row["row_version"]),
        create_request_id=str(row["create_request_id"]) if row["create_request_id"] is not None else None,
        create_request_hash=str(row["create_request_hash"]) if row["create_request_hash"] is not None else None,
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_by=str(row["updated_by"]),
        updated_at=str(row["updated_at"]),
    )


def _draft(row: aiosqlite.Row) -> TeamChartStyleDraft:
    return TeamChartStyleDraft(
        style_id=str(row["style_id"]),
        team_id=str(row["team_id"]),
        schema_version=int(row["schema_version"]),
        based_on_revision_id=str(row["based_on_revision_id"]) if row["based_on_revision_id"] is not None else None,
        base_preset_id=str(row["base_preset_id"]),
        base_preset_revision=str(row["base_preset_revision"]),
        base_preset_checksum=str(row["base_preset_checksum"]),
        authoring_base_snapshot=_json_object(str(row["authoring_base_snapshot_json"])),
        authoring_base_snapshot_checksum=str(row["authoring_base_snapshot_checksum"]),
        name=str(row["name"]),
        description=str(row["description"]),
        overrides=_json_object(str(row["overrides_json"])),
        row_version=int(row["row_version"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_by=str(row["updated_by"]),
        updated_at=str(row["updated_at"]),
    )


def _revision(row: aiosqlite.Row) -> TeamChartStyleRevision:
    return TeamChartStyleRevision(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        style_id=str(row["style_id"]),
        sequence=int(row["sequence"]),
        schema_version=int(row["schema_version"]),
        token_schema_version=int(row["token_schema_version"]),
        resolver_policy_version=str(row["resolver_policy_version"]),
        name=str(row["name"]),
        name_normalized=str(row["name_normalized"]),
        description=str(row["description"]),
        base_preset_id=str(row["base_preset_id"]),
        base_preset_revision=str(row["base_preset_revision"]),
        base_preset_checksum=str(row["base_preset_checksum"]),
        authoring_base_snapshot=_json_object(str(row["authoring_base_snapshot_json"])),
        authoring_base_snapshot_checksum=str(row["authoring_base_snapshot_checksum"]),
        overrides=_json_object(str(row["overrides_json"])),
        resolved_snapshot=_json_object(str(row["resolved_snapshot_json"])),
        snapshot_checksum=str(row["snapshot_checksum"]),
        revision_checksum=str(row["revision_checksum"]),
        validation_policy_version=str(row["validation_policy_version"]),
        validation=_json_object(str(row["validation_json"])),
        operation=str(row["operation"]),
        source_revision_id=str(row["source_revision_id"]) if row["source_revision_id"] is not None else None,
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
    )


async def _one(db: aiosqlite.Connection, sql: str, values: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with db.execute(sql, values) as cursor:
        return await cursor.fetchone()


async def style_by_id(db: aiosqlite.Connection, *, team_id: str, style_id: str) -> TeamChartStyle | None:
    row = await _one(
        db,
        "SELECT * FROM team_chart_styles WHERE team_id=? AND id=?",
        (team_id, style_id),
    )
    return _style(row) if row is not None else None


async def draft_by_style(db: aiosqlite.Connection, *, team_id: str, style_id: str) -> TeamChartStyleDraft | None:
    row = await _one(
        db,
        "SELECT * FROM team_chart_style_drafts WHERE team_id=? AND style_id=?",
        (team_id, style_id),
    )
    return _draft(row) if row is not None else None


async def revision_by_id(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    style_id: str,
    revision_id: str,
) -> TeamChartStyleRevision | None:
    row = await _one(
        db,
        "SELECT * FROM team_chart_style_revisions WHERE team_id=? AND style_id=? AND id=?",
        (team_id, style_id, revision_id),
    )
    return _revision(row) if row is not None else None


async def current_revision(
    db: aiosqlite.Connection,
    *,
    style: TeamChartStyle,
) -> TeamChartStyleRevision | None:
    if style.active_revision_id is None:
        return None
    return await revision_by_id(
        db,
        team_id=style.team_id,
        style_id=style.id,
        revision_id=style.active_revision_id,
    )


async def registry_version(db: aiosqlite.Connection, *, team_id: str) -> int:
    row = await _one(
        db,
        "SELECT registry_version FROM team_chart_style_registry_state WHERE team_id=?",
        (team_id,),
    )
    return int(row["registry_version"]) if row is not None else 0


def _encode_cursor(values: list[Any]) -> str:
    raw = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str, *, length: int) -> list[Any]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ChartStyleDomainError("chart_style_invalid_cursor", status_code=400) from exc
    if not isinstance(decoded, list) or len(decoded) != length or any(not isinstance(item, (str, int)) for item in decoded):
        raise ChartStyleDomainError("chart_style_invalid_cursor", status_code=400)
    return decoded


class ChartStyleStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def detail(self, *, team_id: str, style_id: str) -> tuple[TeamChartStyle, TeamChartStyleDraft | None, TeamChartStyleRevision | None]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            style = await style_by_id(db, team_id=team_id, style_id=style_id)
            if style is None:
                raise ChartStyleDomainError("chart_style_not_found", status_code=404)
            draft = await draft_by_style(db, team_id=team_id, style_id=style_id)
            revision = await current_revision(db, style=style)
            return style, draft, revision

    async def list_styles(self, *, team_id: str, limit: int, after: str | None) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ChartStyleDomainError("chart_style_invalid_limit", status_code=400)
        where = "team_id=?"
        values: list[Any] = [team_id]
        if after:
            lifecycle, updated_at, style_id = _decode_cursor(after, length=3)
            if not all(isinstance(item, str) for item in (lifecycle, updated_at, style_id)):
                raise ChartStyleDomainError("chart_style_invalid_cursor", status_code=400)
            where += " AND (lifecycle>? OR (lifecycle=? AND (updated_at<? OR (updated_at=? AND id<?))))"
            values.extend((lifecycle, lifecycle, updated_at, updated_at, style_id))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM team_chart_styles WHERE {where} ORDER BY lifecycle ASC,updated_at DESC,id DESC LIMIT ?",
                (*values, limit + 1),
            ) as cursor:
                rows = await cursor.fetchall()
            styles = [_style(row) for row in rows[:limit]]
            items: list[dict[str, Any]] = []
            for style in styles:
                draft = await draft_by_style(db, team_id=team_id, style_id=style.id)
                revision = await current_revision(db, style=style)
                items.append(
                    {
                        "id": style.id,
                        "lifecycle": style.lifecycle,
                        "state": chart_style_state(style, draft),
                        "name": draft.name if draft is not None else revision.name if revision is not None else "",
                        "activeRevisionId": style.active_revision_id,
                        "fallbackBuiltInPresetId": style.fallback_builtin_preset_id,
                        "headEtag": head_etag(style),
                        "draftEtag": draft_etag(draft),
                        "resourceEtag": resource_etag(style, draft),
                        "updatedAt": style.updated_at,
                    }
                )
        next_cursor = None
        if len(rows) > limit and styles:
            tail = styles[-1]
            next_cursor = _encode_cursor([tail.lifecycle, tail.updated_at, tail.id])
        return {"items": items, "nextCursor": next_cursor}

    async def list_revisions(
        self,
        *,
        team_id: str,
        style_id: str,
        limit: int,
        after: str | None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ChartStyleDomainError("chart_style_invalid_limit", status_code=400)
        where = "team_id=? AND style_id=?"
        values: list[Any] = [team_id, style_id]
        if after:
            sequence, revision_id = _decode_cursor(after, length=2)
            if type(sequence) is not int or not isinstance(revision_id, str):
                raise ChartStyleDomainError("chart_style_invalid_cursor", status_code=400)
            where += " AND (sequence<? OR (sequence=? AND id<?))"
            values.extend((sequence, sequence, revision_id))
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if await style_by_id(db, team_id=team_id, style_id=style_id) is None:
                raise ChartStyleDomainError("chart_style_not_found", status_code=404)
            async with db.execute(
                f"SELECT * FROM team_chart_style_revisions WHERE {where} ORDER BY sequence DESC,id DESC LIMIT ?",
                (*values, limit + 1),
            ) as cursor:
                rows = await cursor.fetchall()
        revisions = [_revision(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and revisions:
            tail = revisions[-1]
            next_cursor = _encode_cursor([tail.sequence, tail.id])
        return {"items": [revision_to_dict(item) for item in revisions], "nextCursor": next_cursor}

    async def revision(self, *, team_id: str, style_id: str, revision_id: str) -> TeamChartStyleRevision:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            revision = await revision_by_id(db, team_id=team_id, style_id=style_id, revision_id=revision_id)
            if revision is None:
                raise ChartStyleDomainError("chart_style_not_found", status_code=404)
            return revision

    async def registry_version(self, *, team_id: str) -> int:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            return await registry_version(db, team_id=team_id)


__all__ = [
    "ChartStyleStore",
    "current_revision",
    "draft_by_style",
    "registry_version",
    "revision_by_id",
    "style_by_id",
]
