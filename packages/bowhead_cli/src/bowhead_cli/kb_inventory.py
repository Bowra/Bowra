"""Bounded read-only source inventory; no Chroma client, schema or repair writes."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

import aiosqlite
import anyio
from bowhead_core.kb.files_store import _row_to_dto
from bowhead_core.kb.health import SourceRecord, check_storage_health, inspect_file_health
from bowhead_core.kb.schemas import KbFileDTO

if TYPE_CHECKING:
    from bowhead_core.artifact_store import ArtifactBackend


async def inventory_page(
    db_path: Path,
    backend: ArtifactBackend,
    *,
    team_id: str | None = None,
    after: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """Keyset pages describe a live scan, not a cross-database point-in-time snapshot."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    start = monotonic()
    files_path = db_path.parent / "kb_files.db"
    # mode=ro fails on missing databases; no initialize(), WAL pragma or migrations.
    async with aiosqlite.connect(f"{files_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row
        params: list[Any] = [after]
        where = "soft_deleted_at IS NULL AND file_id > ?"
        if team_id:
            where += " AND team_id = ?"
            params.append(team_id)
        async with db.execute(f"SELECT * FROM kb_files WHERE {where} ORDER BY file_id LIMIT ?", [*params, limit + 1]) as cursor:  # noqa: S608 - fixed SQL
            rows = await cursor.fetchall()
        files = [_row_to_dto(row) for row in rows[:limit]]
    sources: dict[str, SourceRecord] = {}
    async with aiosqlite.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        ids = list(dict.fromkeys(file.artifact_id for file in files))
        for offset in range(0, len(ids), 200):
            batch = ids[offset : offset + 200]
            placeholders = ",".join("?" for _ in batch)
            async with db.execute(
                f"SELECT id,path,status FROM artifacts WHERE id IN ({placeholders}) AND deleted_at IS NULL "  # noqa: S608 - placeholders
                "AND COALESCE(producer_kind,'') <> 'template_document_internal_v1'",
                batch,
            ) as cursor:
                sources.update({row[0]: SourceRecord(row[1], row[2]) for row in await cursor.fetchall()})
        async with db.execute(
            "SELECT path FROM artifacts WHERE deleted_at IS NULL AND status='ready' "
            "AND COALESCE(producer_kind,'') <> 'template_document_internal_v1' ORDER BY created_at DESC LIMIT 20"
        ) as cursor:
            recent = [row[0] for row in await cursor.fetchall()]
    healthy: bool | None = None
    health_lock = anyio.Lock()

    async def storage_health() -> bool:
        nonlocal healthy
        async with health_lock:
            if healthy is None:
                healthy = await check_storage_health(backend, recent)
            return healthy

    items: dict[str, dict[str, Any]] = {}
    limiter = anyio.CapacityLimiter(4)

    async def check(file: KbFileDTO) -> None:
        async with limiter:
            health = await inspect_file_health(file, source=sources.get(file.artifact_id), backend=backend, storage_health=storage_health)
            items[file.file_id] = {
                "file_id": file.file_id,
                "scope": file.scope,
                "mime_type": file.mime_type,
                "size_bytes": file.size_bytes,
                "parse_status": file.parse_status,
                "recorded_chunks": file.chunks_count,
                "cite_count": file.cite_count,
                "health": health.model_dump(),
            }

    async with anyio.create_task_group() as group:
        for file in files:
            group.start_soon(check, file)
    ordered = [items[file.file_id] for file in files]
    return {
        "schema_version": 1,
        "read_only": True,
        "consistency": "live_page",
        "team_id": team_id,
        "after": after,
        "has_more": len(rows) > limit,
        "next_after": files[-1].file_id if files and len(rows) > limit else None,
        "checked": len(files),
        "size_bytes": sum(file.size_bytes for file in files),
        "recorded_chunks": sum(file.chunks_count for file in files),
        "cited_files": sum(file.cite_count > 0 for file in files),
        "source_counts": dict(Counter(item["health"]["source_health"] for item in ordered)),
        "format_counts": dict(Counter(file.mime_type for file in files)),
        "binding_unverified": len(files),
        "index_checked": False,
        "rebuild_estimate_seconds": None,
        "duration_seconds": round(monotonic() - start, 3),
        "items": ordered,
    }
