"""SQLite publication point for KB index generations; no startup DDL or backfill."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from bowhead_core.kb.ingestion import KnowledgeAction
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.sqlite_domain_runtime import connect as diagnostic_connect


def generation_writes_enabled() -> bool:
    return os.environ.get("FEATURE_KB_TRUST_V2", "0").strip().lower() in {"1", "true", "yes", "on"}


def use_guard_enabled() -> bool:
    # Observation-only deployments must explicitly opt into the new read policy.
    return generation_writes_enabled() and os.environ.get("FEATURE_KB_USE_GUARD", "0").strip().lower() in {"1", "true", "yes", "on"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class KbGenerationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def get(self, generation_id: str | None) -> dict[str, Any] | None:
        if not generation_id:
            return None
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM kb_file_generations WHERE generation_id=?", (generation_id,))).fetchone()
            return dict(row) if row else None

    async def begin(self, file: KbFileDTO, sha256: str, count: int, index_version: str) -> str:
        generation_id = "kbg_" + uuid.uuid4().hex
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                "INSERT INTO kb_file_generations VALUES (?,?,?,?,?,?,'writing',?,NULL)",
                (generation_id, file.file_id, file.artifact_id, sha256, count, index_version, now_iso()),
            )
            await db.commit()
        return generation_id

    async def publish(
        self,
        file: KbFileDTO,
        generation_id: str,
        *,
        layer: str,
        duration_ms: int,
        summary_json: str | None,
        description_fingerprint: str | None = None,
        description_action: KnowledgeAction | None = None,
    ) -> bool:
        """CAS the generation and file together; losers cannot replace newer work."""
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            generation = await (
                await db.execute(
                    "SELECT expected_chunk_count FROM kb_file_generations WHERE generation_id=? AND file_id=? AND artifact_id=? AND state='writing'",
                    (generation_id, file.file_id, file.artifact_id),
                )
            ).fetchone()
            if not generation:
                return False
            stamp = now_iso()
            updated = await db.execute(
                "UPDATE kb_files SET active_generation_id=?, version=version+1, parse_status='done', parse_progress_pct=100, "
                "parse_error=NULL, chunks_count=?, parse_layer=?, parse_duration_ms=?, summary_json=?, parse_heartbeat_at=?, source_binding_json=NULL "
                "WHERE file_id=? AND version=? AND artifact_id=? AND scope=? AND scope_id=? AND workspace_id=? AND team_id=? "
                "AND soft_deleted_at IS NULL AND active_generation_id IS ? AND parse_attempts=?",
                (
                    generation_id,
                    generation[0],
                    layer,
                    duration_ms,
                    summary_json,
                    stamp,
                    file.file_id,
                    file.version,
                    file.artifact_id,
                    file.scope,
                    file.scope_id,
                    file.workspace_id,
                    file.team_id,
                    file.active_generation_id,
                    file.parse_attempts,
                ),
            )
            if updated.rowcount != 1:
                await db.rollback()
                return False
            await db.execute("UPDATE kb_file_generations SET state='published',published_at=? WHERE generation_id=?", (stamp, generation_id))
            from bowhead_core.kb.source_navigation_store import SourceNavigationStore

            await SourceNavigationStore.ensure_base(db, file, generation_id)
            admission = None
            if description_fingerprint and description_action is not None:
                description_action.require(user_id=description_action.actor_user_id, team_id=file.team_id)
                admission = (description_action.actor_user_id, description_action.workspace_id)
            elif description_fingerprint and file.ingestion_action_id:
                admission = await (
                    await db.execute(
                        "SELECT actor_user_id,workspace_id FROM kb_membership_operations WHERE id=? AND file_id=? AND team_id=? AND membership_revision=? AND state='applied'",
                        (file.ingestion_action_id, file.file_id, file.team_id, file.membership_revision),
                    )
                ).fetchone()
            if admission:
                navigation = await (await db.execute("SELECT revision FROM kb_source_navigation WHERE file_id=?", (file.file_id,))).fetchone()
                await SourceNavigationStore.enqueue_in(
                    db,
                    file,
                    generation_id=generation_id,
                    fingerprint=description_fingerprint,
                    admission_key=f"publication:{file.file_id}:{file.membership_revision}:{generation_id}",
                    actor_user_id=admission[0],
                    workspace_id=admission[1],
                    manual_revision=navigation[0],
                    source_version=file.version + 1,
                )
            await db.commit()
            return True

    async def mark_absent(self, file: KbFileDTO) -> None:
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                "UPDATE kb_files SET source_absent=1, source_binding_json=NULL WHERE file_id=? AND artifact_id=? AND active_generation_id IS ?",
                (file.file_id, file.artifact_id, file.active_generation_id),
            )
            await db.commit()

    async def bind_source(self, file: KbFileDTO, identity: str, sha256: str) -> bool:
        binding = json.dumps({"artifact_id": file.artifact_id, "identity": identity, "sha256": sha256}, sort_keys=True)
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            result = await db.execute(
                "UPDATE kb_files SET source_binding_json=?, source_absent=0 WHERE file_id=? AND artifact_id=? "
                "AND active_generation_id IS ? AND version=? AND soft_deleted_at IS NULL",
                (binding, file.file_id, file.artifact_id, file.active_generation_id, file.version),
            )
            await db.commit()
            return result.rowcount == 1

    async def restore_source(self, file: KbFileDTO, artifact_id: str) -> bool:
        """Called only after matching the complete upload against the published SHA."""
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            result = await db.execute(
                "UPDATE kb_files SET artifact_id=?, source_binding_json=NULL WHERE file_id=? AND artifact_id=? "
                "AND active_generation_id IS ? AND version=? AND scope=? AND scope_id=? AND workspace_id=? AND team_id=? AND soft_deleted_at IS NULL",
                (
                    artifact_id,
                    file.file_id,
                    file.artifact_id,
                    file.active_generation_id,
                    file.version,
                    file.scope,
                    file.scope_id,
                    file.workspace_id,
                    file.team_id,
                ),
            )
            await db.commit()
            return result.rowcount == 1

    async def claim_orphans(self, file: KbFileDTO, *, index_version: str, before: str) -> list[str]:
        """Only unpublished writes older than the maximum parse lifetime; bounded and retryable."""
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            rows = await (
                await db.execute(
                    "UPDATE kb_file_generations SET state='abandoned' WHERE generation_id IN ("
                    "SELECT g.generation_id FROM kb_file_generations g JOIN kb_files f ON f.file_id=g.file_id "
                    "WHERE g.file_id=? AND g.index_version=? AND g.state IN ('writing','abandoned') "
                    "AND g.created_at<? AND g.generation_id IS NOT f.active_generation_id ORDER BY g.created_at LIMIT 20) RETURNING generation_id",
                    (file.file_id, index_version, before),
                )
            ).fetchall()
            await db.commit()
            return [row[0] for row in rows]

    async def forget_orphan(self, generation_id: str) -> None:
        async with diagnostic_connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("DELETE FROM kb_file_generations WHERE generation_id=? AND state='abandoned'", (generation_id,))
            await db.commit()
