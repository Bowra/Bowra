"""Original-job Canvas input metadata and create-only private Artifact freezing."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import aiosqlite
import structlog
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore
from bowhead_core.documents.canvas_material import FILENAME, MAX_BYTES
from bowhead_core.documents.request import DocumentRequestError, DocumentScope, Identity
from bowhead_core.documents.retention import INTERNAL_PRODUCER

log = structlog.get_logger(__name__)


class CanvasSectionIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    section_id: Identity
    rev: Annotated[int, Field(ge=1)]


class CanvasInputManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    canvas_id: Identity
    session_id: Identity
    projection_token: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    sections: Annotated[tuple[CanvasSectionIdentity, ...], Field(min_length=1, max_length=64)]
    source_refs: tuple[dict[str, Any], ...]
    artifact_closure: tuple[str, ...]
    accessible_sources: tuple[str, ...]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    size_bytes: Annotated[int, Field(ge=1, le=MAX_BYTES)]


@dataclass(frozen=True)
class CanvasInputAdmission:
    manifest: CanvasInputManifest
    artifact_id: str
    path: str
    source_job_id: str | None = None


async def validate_current_canvas_in_tx(db: aiosqlite.Connection, *, scope: DocumentScope, manifest: CanvasInputManifest, section_ids) -> None:
    from bowhead_core.canvas_export_projection import build_canvas_content_projection
    from bowhead_core.canvas_store import _row_to_canvas, _row_to_section

    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM canvases WHERE id=? AND session_id=? AND user_id=? AND workspace_id=?",
        (manifest.canvas_id, manifest.session_id, scope.user_id, scope.workspace_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise DocumentRequestError("document_canvas_unavailable")
    canvas = _row_to_canvas(row)
    async with db.execute("SELECT * FROM canvas_sections WHERE canvas_id=? ORDER BY seq,id", (canvas.id,)) as cursor:
        sections = [_row_to_section(item) for item in await cursor.fetchall()]
    projection = build_canvas_content_projection(title=canvas.title, sections=sections)
    visible = [item for item in projection.sections if not item.quarantined]
    selected = [item for item in visible if section_ids is None or item.section_id in section_ids]
    if (
        projection.projection_token != manifest.projection_token
        or tuple((item.section_id, item.rev) for item in selected) != tuple((item.section_id, item.rev) for item in manifest.sections)
        or (section_ids is not None and len(selected) != len(section_ids))
    ):
        raise DocumentRequestError("document_canvas_changed")


async def read_canvas_input_in_tx(db: aiosqlite.Connection, *, scope: DocumentScope, job_id: str, write: bool = False) -> aiosqlite.Row | None:
    from bowhead_core.documents.store import _authorize

    await _authorize(db, scope, write=write)
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT j.request_json,j.status FROM document_jobs j JOIN sessions s ON s.id=j.session_id
        WHERE j.id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=?
          AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id AND s.deleted_at IS NULL""",
        (job_id, *scope.sql_params()),
    ) as cursor:
        job = await cursor.fetchone()
    if job is None or (write and job["status"] != "queued"):
        raise DocumentRequestError("document_not_found")
    async with db.execute(
        """SELECT c.*,j.session_id,j.request_json,a.status AS artifact_status,a.deleted_at,a.path AS artifact_path,
        a.size AS artifact_size,a.created_by,a.workspace_id,r.released_at,p.artifact_id AS purging
        FROM document_canvas_inputs c JOIN document_jobs j ON j.id=c.job_id
        JOIN sessions s ON s.id=j.session_id JOIN artifacts a ON a.id=c.artifact_id
        JOIN document_artifact_refs r ON r.owner_id=c.job_id AND r.ref_key='canvas' AND r.artifact_id=c.artifact_id
        LEFT JOIN artifact_purge_claims p ON p.artifact_id=c.artifact_id
        WHERE c.job_id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=?
          AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id AND s.deleted_at IS NULL""",
        (job_id, *scope.sql_params()),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None and json.loads(job["request_json"]).get("canvas_input") is not None:
        raise DocumentRequestError("document_canvas_input_unavailable")
    return row


def validate_canvas_row(row: aiosqlite.Row, scope: DocumentScope, *, frozen: bool) -> CanvasInputManifest:
    from bowhead_core.documents.store import _json

    manifest = CanvasInputManifest.model_validate_json(row["manifest_json"])
    if (
        _json(manifest.model_dump(mode="json")) != row["manifest_json"]
        or json.loads(row["request_json"]).get("canvas_input") != manifest.model_dump(mode="json")
        or manifest.session_id != row["session_id"]
        or row["created_by"] != scope.user_id
        or row["workspace_id"] != scope.workspace_id
        or row["artifact_path"] != row["path"]
        or row["artifact_size"] != manifest.size_bytes
        or row["sha256"] != manifest.sha256
        or row["size_bytes"] != manifest.size_bytes
        or row["deleted_at"] is not None
        or row["released_at"] is not None
        or row["purging"] is not None
        or row["cleanup_claimed_at"] is not None
        or (frozen and (row["verified_at"] is None or row["artifact_status"] != "ready"))
        or (not frozen and row["artifact_status"] not in {"ready", "document_staging"})
    ):
        raise DocumentRequestError("document_canvas_input_unavailable")
    return manifest


async def inherit_canvas_input_in_tx(db: aiosqlite.Connection, *, scope: DocumentScope, job_id: str) -> CanvasInputAdmission | None:
    row = await read_canvas_input_in_tx(db, scope=scope, job_id=job_id)
    if row is None:
        return None
    manifest = validate_canvas_row(row, scope, frozen=True)
    return CanvasInputAdmission(manifest, row["artifact_id"], row["path"], job_id)


async def commit_canvas_input_in_tx(
    db: aiosqlite.Connection, *, scope: DocumentScope, job_id: str, admission: CanvasInputAdmission, now: str
) -> None:
    from bowhead_core.documents.store import _json

    manifest = CanvasInputManifest.model_validate_json(admission.manifest.model_dump_json())
    if admission.source_job_id is None:
        await db.execute(
            """INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,kind,scope,inject,trust,producer_kind,status,workspace_id)
            VALUES (?,?,?,?,'document_canvas_input',?,?,?,'tool_data','user','list','untrusted',?,'document_staging',?)""",
            (
                admission.artifact_id,
                FILENAME,
                "application/json",
                manifest.size_bytes,
                admission.path,
                scope.user_id,
                now,
                INTERNAL_PRODUCER,
                scope.workspace_id,
            ),
        )
    await db.execute(
        """INSERT INTO document_canvas_inputs(job_id,artifact_id,path,sha256,size_bytes,manifest_json,source_job_id,verified_at,cleanup_after)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            job_id,
            admission.artifact_id,
            admission.path,
            manifest.sha256,
            manifest.size_bytes,
            _json(manifest.model_dump(mode="json")),
            admission.source_job_id,
            now if admission.source_job_id else None,
            time.time() + 86400,
        ),
    )
    await db.execute(
        """INSERT INTO document_artifact_refs(team_id,workspace_id,owner_user_id,owner_kind,owner_id,ref_key,artifact_id,artifact_sha256,retention_class)
        VALUES (?,?,?,'job',?,'canvas',?,?,'execution')""",
        (*scope.sql_params(), job_id, admission.artifact_id, manifest.sha256),
    )


class CanvasInputStore:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    async def get(
        self, *, scope: DocumentScope, job_id: str, frozen: bool = True, write: bool = False
    ) -> tuple[aiosqlite.Row, CanvasInputManifest] | None:
        async with open_shared_ctx(self.artifacts.db_path) as db:
            await db.execute("BEGIN")
            row = await read_canvas_input_in_tx(db, scope=scope, job_id=job_id, write=write)
            if row is None:
                return None
            return row, validate_canvas_row(row, scope, frozen=frozen)

    async def freeze(
        self,
        *,
        scope: DocumentScope,
        job_id: str,
        materialize: Callable[[CanvasInputManifest], Awaitable[bytes]],
        authorize: Callable[[CanvasInputManifest], Awaitable[None]],
    ) -> None:
        from bowhead_core.documents.authoring_protocol import digest

        material = await self.get(scope=scope, job_id=job_id, frozen=False, write=True)
        if material is None:
            return
        row, manifest = material
        await authorize(manifest)
        try:
            content = await self.artifacts.backend.read_bounded(row["path"], limit=MAX_BYTES)
        except FileNotFoundError:
            if row["verified_at"] is not None:
                raise DocumentRequestError("document_frozen_input_missing") from None
            content = await materialize(manifest)
            if len(content) != manifest.size_bytes or digest(content) != manifest.sha256:
                raise DocumentRequestError("document_canvas_changed")
            await authorize(manifest)
            await self.artifacts.put_account_owned_immutable(user_id=scope.user_id, path=row["path"], content=content, mime="application/json")
            content = await self.artifacts.backend.read_bounded(row["path"], limit=MAX_BYTES)
        if len(content) != manifest.size_bytes or digest(content) != manifest.sha256:
            raise DocumentRequestError("document_frozen_hash_mismatch")
        await authorize(manifest)
        async with open_shared_ctx(self.artifacts.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await read_canvas_input_in_tx(db, scope=scope, job_id=job_id, write=True)
            if current is None:
                raise DocumentRequestError("document_canvas_input_unavailable")
            validate_canvas_row(current, scope, frozen=False)
            now = datetime.now(UTC).isoformat()
            await db.execute("UPDATE document_canvas_inputs SET verified_at=? WHERE job_id=? AND verified_at IS NULL", (now, job_id))
            await db.execute("UPDATE artifacts SET status='ready' WHERE id=? AND status='document_staging'", (row["artifact_id"],))
            await db.commit()

    async def read(self, *, scope: DocumentScope, job_id: str) -> bytes | None:
        from bowhead_core.documents.authoring_protocol import digest

        material = await self.get(scope=scope, job_id=job_id)
        if material is None:
            return None
        row, manifest = material
        content = await self.artifacts.backend.read_bounded(row["path"], limit=MAX_BYTES)
        if len(content) != manifest.size_bytes or digest(content) != manifest.sha256:
            raise DocumentRequestError("document_frozen_hash_mismatch")
        await self.get(scope=scope, job_id=job_id)
        return content


async def cleanup_canvas_inputs(artifacts: ArtifactStore, *, now: float, limit: int = 200) -> int:
    """Release cancelled job references without reclaiming a revision's input.

    Original purge claims keep exact late-PUT tombstones. A shared Artifact is
    eligible only after every retaining job has released its own reference.
    """
    from bowhead_core.artifact_purge import purge_artifacts

    if not 1 <= limit <= 200:
        raise ValueError("Invalid Canvas input cleanup limit")
    async with open_shared_ctx(artifacts.db_path) as db:
        async with db.execute("SELECT 1 FROM sqlite_schema WHERE name='document_canvas_inputs' AND type='table'") as cursor:
            if await cursor.fetchone() is None:
                return 0
        async with db.execute(
            "SELECT job_id FROM document_canvas_inputs WHERE cleanup_after<=? ORDER BY cleanup_after,job_id LIMIT ?", (now, limit)
        ) as cursor:
            jobs = [row[0] for row in await cursor.fetchall()]
    removed = 0
    timestamp = datetime.fromtimestamp(now, UTC).isoformat()
    for job_id in jobs:
        async with open_shared_ctx(artifacts.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT c.artifact_id,c.path,c.cleanup_claimed_at,j.status,j.updated_at
                FROM document_canvas_inputs c JOIN document_jobs j ON j.id=c.job_id
                WHERE c.job_id=? AND c.cleanup_after<=?""",
                (job_id, now),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                continue
            await db.execute("UPDATE document_canvas_inputs SET cleanup_after=? WHERE job_id=?", (now + 86400, job_id))
            if row[3] != "cancelled" or datetime.fromisoformat(row[4]).timestamp() + 86400 > now:
                await db.commit()
                continue
            artifact_id, path = row[:2]
            if row[2] is None:
                await db.execute("UPDATE document_canvas_inputs SET cleanup_claimed_at=? WHERE job_id=?", (timestamp, job_id))
                await db.execute(
                    "UPDATE document_artifact_refs SET released_at=? WHERE owner_kind='job' AND owner_id=? AND ref_key='canvas' AND released_at IS NULL",
                    (timestamp, job_id),
                )
            await db.execute(
                """UPDATE artifacts SET deleted_at=? WHERE id=? AND deleted_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM document_artifact_refs WHERE artifact_id=artifacts.id AND released_at IS NULL)""",
                (timestamp, artifact_id),
            )
            await db.commit()
        result = await purge_artifacts(artifacts.db_path, artifacts.backend, [artifact_id], user_id=None)
        removed += result.rows_removed
        async with (
            open_shared_ctx(artifacts.db_path) as db,
            db.execute(
                "SELECT 1 FROM artifact_purge_claims WHERE artifact_id=? AND path=? AND completed_at IS NOT NULL", (artifact_id, path)
            ) as cursor,
        ):
            complete = await cursor.fetchone() is not None
        if complete and result.rows_removed == 0:
            try:
                await artifacts.backend.delete(path)
            except FileNotFoundError:
                pass
            except Exception as error:  # noqa: BLE001 - retain the exact-key tombstone for the next sweep
                log.warning("document.canvas_cleanup_failed", artifact_id=artifact_id, error_type=type(error).__name__)
    return removed
