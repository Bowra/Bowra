"""Persistent scan cursor, fact cache, and bounded rebuild manifests. No startup DDL."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from bowhead_core.kb.files_store import _row_to_dto
from bowhead_core.kb.generations import now_iso

OBSERVATION_TTL_S = 300


def cached_source_state(file, *, index_version: str | None = None) -> dict[str, Any]:
    """Facts for display only; never an authorization or execution decision."""
    unknown = {"state": "unchecked", "reason": "not_checked", "checked_at": None}
    if file.source_absent:
        return {"state": "paused", "reason": "source_unavailable", "checked_at": None}
    try:
        item = json.loads(file.source_observation_json or "{}")
        age = (datetime.now(UTC) - datetime.fromisoformat(item["checked_at"])).total_seconds()
        if (
            not 0 <= age <= OBSERVATION_TTL_S
            or item["version"] != file.version
            or item["generation_id"] != file.active_generation_id
            or item["artifact_id"] != file.artifact_id
        ):
            return unknown
        if index_version is None or item.get("index_version") != index_version:
            return unknown
        if any(item.get(key) != getattr(file, key) for key in ("team_id", "workspace_id", "scope", "scope_id")):
            return unknown
        return {key: item[key] for key in ("state", "reason", "checked_at")}
    except (KeyError, TypeError, ValueError):
        return unknown


class KbMaintenanceStore:
    def __init__(self, path):
        self.path = str(path)

    async def scan_page(self, *, limit: int = 20):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT value FROM kb_maintenance_state WHERE key='source_scan'")).fetchone()
            after = row[0] if row else ""
            rows = await (
                await db.execute("SELECT * FROM kb_files WHERE soft_deleted_at IS NULL AND file_id>? ORDER BY file_id LIMIT ?", (after, limit))
            ).fetchall()
            return [_row_to_dto(row) for row in rows]

    async def checkpoint(self, after: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO kb_maintenance_state VALUES ('source_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (after,)
            )
            await db.commit()

    async def observe(self, file, decision, *, index_version: str):
        item = {
            "state": decision.state,
            "reason": decision.reason,
            "checked_at": decision.checked_at,
            "generation_id": file.active_generation_id,
            "artifact_id": file.artifact_id,
            "version": file.version,
            "index_version": index_version,
        }
        item.update({key: getattr(file, key) for key in ("team_id", "workspace_id", "scope", "scope_id")})
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE kb_files SET source_observation_json=? WHERE file_id=? AND team_id=? AND workspace_id=? AND scope=? AND scope_id=? AND version=? AND artifact_id=? AND active_generation_id IS ? AND soft_deleted_at IS NULL",
                (
                    json.dumps(item),
                    file.file_id,
                    file.team_id,
                    file.workspace_id,
                    file.scope,
                    file.scope_id,
                    file.version,
                    file.artifact_id,
                    file.active_generation_id,
                ),
            )
            await db.commit()

    async def create_job(self, *, team_id: str, workspace_id: str, user_id: str, items: list[dict]) -> str:
        job_id = "kbj_" + uuid.uuid4().hex
        stamp = now_iso()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            count = await (
                await db.execute("SELECT count(*) FROM kb_rebuild_jobs WHERE team_id=? AND state IN ('running','cancelling')", (team_id,))
            ).fetchone()
            if count[0] >= 1:
                raise ValueError("kb_rebuild_busy")
            await db.execute(
                "INSERT INTO kb_rebuild_jobs(job_id,team_id,workspace_id,user_id,items_json,state,created_at,updated_at) VALUES (?,?,?,?,?,'running',?,?)",
                (job_id, team_id, workspace_id, user_id, json.dumps(items), stamp, stamp),
            )
            await db.commit()
        return job_id

    async def latest_job_id(self, *, team_id: str, workspace_id: str, user_id: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT job_id FROM kb_rebuild_jobs WHERE team_id=? AND workspace_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1",
                    (team_id, workspace_id, user_id),
                )
            ).fetchone()
            return row[0] if row else None

    async def get_job(self, job_id: str):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM kb_rebuild_jobs WHERE job_id=?", (job_id,))).fetchone()
        if not row:
            return None
        item = dict(row)
        item["items"] = json.loads(item.pop("items_json"))
        item["outcomes"] = json.loads(item.pop("outcomes_json"))
        return item

    async def progress(self, job_id: str, cursor: int, outcomes: list[dict]):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE kb_rebuild_jobs SET cursor=?,outcomes_json=?,updated_at=? WHERE job_id=? AND state IN ('running','cancelling')",
                (cursor, json.dumps(outcomes), now_iso(), job_id),
            )
            await db.commit()

    async def state(self, job_id: str, state: str, *, expected: str = "running") -> bool:
        async with aiosqlite.connect(self.path) as db:
            result = await db.execute(
                "UPDATE kb_rebuild_jobs SET state=?,updated_at=? WHERE job_id=? AND state=?", (state, now_iso(), job_id, expected)
            )
            await db.commit()
            return result.rowcount == 1

    async def claim_file(self, job_id: str, position: int, file) -> bool:
        """Queue and persist the exact claimed version in the same transaction."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT items_json,cursor FROM kb_rebuild_jobs WHERE job_id=? AND state='running'", (job_id,))).fetchone()
            if not row or row[1] != position:
                return False
            items = json.loads(row[0])
            item = items[position]
            expected = item.get("claimed_version", item["version"])
            if file.version != expected:
                return False
            cur = await db.execute(
                """UPDATE kb_files SET parse_status='queued',parse_error=NULL,
                parse_progress_pct=0,parse_attempts=0,parse_heartbeat_at=?,version=version+1
                WHERE file_id=? AND version=? AND team_id=? AND workspace_id=? AND scope=? AND scope_id=?
                AND artifact_id=? AND soft_deleted_at IS NULL AND parse_status IN ('done','failed')""",
                (now_iso(), item["file_id"], expected, item["team_id"], item["workspace_id"], item["scope"], item["scope_id"], item["artifact_id"]),
            )
            if cur.rowcount != 1:
                return False
            item["claimed_version"] = expected + 1
            await db.execute("UPDATE kb_rebuild_jobs SET items_json=?,updated_at=? WHERE job_id=?", (json.dumps(items), now_iso(), job_id))
            await db.commit()
            return True

    async def interrupt_file(self, job_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT items_json,cursor FROM kb_rebuild_jobs WHERE job_id=?", (job_id,))).fetchone()
            if row:
                await self._interrupt_claim(db, *row)
            await db.commit()

    @staticmethod
    async def _interrupt_claim(db, raw, cursor):
        items = json.loads(raw)
        if cursor < len(items) and "claimed_version" in items[cursor]:
            item = items[cursor]
            await db.execute(
                "UPDATE kb_files SET parse_status='failed',parse_error='interrupted: rebuild requires explicit resume' WHERE file_id=? AND version=? AND parse_status IN ('queued','parsing','chunking','embedding')",
                (item["file_id"], item["claimed_version"]),
            )

    async def pause_interrupted(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute("SELECT items_json,cursor FROM kb_rebuild_jobs WHERE state IN ('running','paused','cancelled','cancelling')")
            ).fetchall()
            for row in rows:
                await self._interrupt_claim(db, *row)
            await db.execute("UPDATE kb_rebuild_jobs SET state='paused',updated_at=? WHERE state='running'", (now_iso(),))
            await db.execute("UPDATE kb_rebuild_jobs SET state='cancelled',updated_at=? WHERE state='cancelling'", (now_iso(),))
            await db.commit()

    async def resume(self, job_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await db.execute(
                "UPDATE kb_rebuild_jobs SET state='running',updated_at=? WHERE job_id=? AND state='paused' AND NOT EXISTS(SELECT 1 FROM kb_rebuild_jobs other WHERE other.team_id=kb_rebuild_jobs.team_id AND other.state IN ('running','cancelling'))",
                (now_iso(), job_id),
            )
            await db.commit()
            return result.rowcount == 1
