"""Versioned navigation metadata. Authorization belongs to original callers."""

from __future__ import annotations

import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_pooled_read_ctx, open_shared_ctx
from bowhead_core.kb.schemas import KbFileDTO
from bowhead_core.kb.source_description import GENERATOR_VERSION


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class NavigationConflict(ValueError):
    """The source or navigation input changed before a guarded write."""


class SourceNavigationStore:
    """Use the migrated files database without runtime schema creation."""

    def __init__(self, path: str | Path, *, connection: aiosqlite.Connection | None = None) -> None:
        self.path = str(path)
        self._read_connection = connection

    @staticmethod
    async def _current(db: aiosqlite.Connection, file: KbFileDTO) -> None:
        row = await (
            await db.execute(
                "SELECT version,membership_revision,active_generation_id,team_id,soft_deleted_at,source_absent FROM kb_files WHERE file_id=?",
                (file.file_id,),
            )
        ).fetchone()
        if not row or tuple(row[:4]) != (file.version, file.membership_revision, file.active_generation_id, file.team_id) or row[4] or row[5]:
            raise NavigationConflict("knowledge_source_changed")

    @staticmethod
    async def ensure_base(db: aiosqlite.Connection, file: KbFileDTO, generation: str | None = None) -> None:
        await db.execute(
            "INSERT INTO kb_source_navigation(file_id,generation_id,membership_revision) VALUES (?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET generation_id=excluded.generation_id,membership_revision=excluded.membership_revision,"
            "revision=kb_source_navigation.revision+1,labels_override=0 WHERE generation_id IS NOT excluded.generation_id OR membership_revision<>excluded.membership_revision",
            (file.file_id, generation if generation is not None else file.active_generation_id, file.membership_revision),
        )

    async def revision(self, file_id: str) -> int:
        async with open_shared_ctx(self.path) as db:
            row = await (await db.execute("SELECT revision FROM kb_source_navigation WHERE file_id=?", (file_id,))).fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def _published(db: aiosqlite.Connection, files: list[KbFileDTO]) -> set[str]:
        if not files:
            return set()
        current = {file.file_id: file for file in files}
        generations = [file.active_generation_id for file in files if file.active_generation_id]
        if not generations:
            return set()
        rows = await (
            await db.execute(
                "SELECT generation_id,file_id,artifact_id,expected_chunk_count FROM kb_file_generations WHERE state='published' AND published_at IS NOT NULL AND generation_id IN ("
                + ",".join("?" for _ in generations)
                + ")",
                generations,
            )
        ).fetchall()
        return {
            row[0]
            for row in rows
            if row[1] in current
            and row[0] == current[row[1]].active_generation_id
            and row[2] == current[row[1]].artifact_id
            and row[3] == current[row[1]].chunks_count
            and row[3] > 0
        }

    async def published_generations(self, files: list[KbFileDTO]) -> set[str]:
        if len(files) > 256:
            raise ValueError("knowledge_catalog_input_invalid")
        async with nullcontext(self._read_connection) if self._read_connection is not None else open_pooled_read_ctx(self.path) as db:
            return await self._published(db, files)

    async def manual_needs_review(self, file: KbFileDTO) -> bool:
        async with open_shared_ctx(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT 1 FROM kb_source_descriptions WHERE file_id=? AND team_id=? AND method='manual' AND length(text)>0 AND (generation_id IS NOT ? OR membership_revision<>? OR (generation_id IS NULL AND source_version<>?)) LIMIT 1",
                    (file.file_id, file.team_id, file.active_generation_id, file.membership_revision, file.version),
                )
            ).fetchone()
            return row is not None

    async def file_wide_descriptions(self, file: KbFileDTO) -> list[dict[str, Any]]:
        async with open_shared_ctx(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM kb_source_descriptions WHERE file_id=? AND team_id=? AND membership_revision=? AND generation_id IS ? "
                    "AND (generation_id IS NOT NULL OR source_version=?) AND method IN ('manual','standalone') "
                    "ORDER BY CASE method WHEN 'manual' THEN 0 WHEN 'standalone' THEN 1 ELSE 2 END,updated_at DESC,id LIMIT 16",
                    (file.file_id, file.team_id, file.membership_revision, file.active_generation_id, file.version),
                )
            ).fetchall()
        return [
            {
                **dict(row),
                "topics": json.loads(row["topics_json"]),
                "chunks": json.loads(row["chunks_json"]),
                "matrix": json.loads(row["matrix_json"]) if row["matrix_json"] else None,
            }
            for row in rows
        ]

    async def labels(self, file: KbFileDTO) -> list[str]:
        async with open_shared_ctx(self.path) as db:
            rows = await (
                await db.execute(
                    "SELECT category_id FROM kb_source_category_files WHERE file_id=? AND generation_id IS ? AND membership_revision=? ORDER BY category_id",
                    (file.file_id, file.active_generation_id, file.membership_revision),
                )
            ).fetchall()
            return [row[0] for row in rows]

    async def metadata(self, files: list[KbFileDTO], workspace_id: str) -> dict[str, dict[str, Any]]:
        """Batch only already-authorized files; Matrix bodies require a second read."""
        if not files:
            return {}
        placeholders = ",".join("?" for _ in files)
        ids = [file.file_id for file in files]
        result = {identity: {"revision": 0, "descriptions": [], "labels": [], "labels_override": False} for identity in ids}
        async with nullcontext(self._read_connection) if self._read_connection is not None else open_pooled_read_ctx(self.path) as db:
            db.row_factory = aiosqlite.Row
            for row in await (
                await db.execute(f"SELECT file_id,revision,labels_override FROM kb_source_navigation WHERE file_id IN ({placeholders})", ids)
            ).fetchall():
                result[row["file_id"]]["revision"] = row["revision"]
                result[row["file_id"]]["labels_override"] = bool(row["labels_override"])
            rows = await (
                await db.execute(
                    "WITH candidates AS (SELECT id,file_id,team_id,membership_revision,generation_id,source_version,method,revision,coverage,provider,model,matrix_json,"
                    "CASE WHEN method='matrix' THEN NULL ELSE text END AS text,CASE WHEN method='matrix' THEN NULL ELSE topics_json END AS topics_json,"
                    "ROW_NUMBER() OVER(PARTITION BY file_id ORDER BY CASE method WHEN 'manual' THEN 0 WHEN 'standalone' THEN 1 ELSE 2 END,updated_at DESC,id) AS position "
                    f"FROM kb_source_descriptions WHERE file_id IN ({placeholders}) AND (method<>'matrix' OR matrix_workspace_id=?)) "
                    "SELECT * FROM candidates WHERE position<=16 ORDER BY file_id,position LIMIT 256",
                    [*ids, workspace_id],
                )
            ).fetchall()
            current = {file.file_id: file for file in files}
            for row in rows:
                file = current[row["file_id"]]
                if (row["team_id"], row["membership_revision"], row["generation_id"]) != (
                    file.team_id,
                    file.membership_revision,
                    file.active_generation_id,
                ):
                    continue
                if file.active_generation_id is None and row["source_version"] != file.version:
                    continue
                result[file.file_id]["descriptions"].append(
                    {
                        **dict(row),
                        "matrix": json.loads(row["matrix_json"]) if row["matrix_json"] else None,
                        "topics": json.loads(row["topics_json"]) if row["topics_json"] else [],
                    }
                )
            for row in await (await db.execute(f"SELECT * FROM kb_source_category_files WHERE file_id IN ({placeholders})", ids)).fetchall():
                file = current[row["file_id"]]
                if (row["generation_id"], row["membership_revision"]) == (file.active_generation_id, file.membership_revision):
                    result[file.file_id]["labels"].append(row["category_id"])
        return result

    async def matrix_bodies(self, identities: list[str]) -> dict[str, dict[str, Any]]:
        """Read only candidates whose original Matrix authority was just validated."""
        if not identities:
            return {}
        async with nullcontext(self._read_connection) if self._read_connection is not None else open_pooled_read_ctx(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT id,revision,text,topics_json FROM kb_source_descriptions WHERE method='matrix' AND id IN ("
                    + ",".join("?" for _ in identities)
                    + ")",
                    identities,
                )
            ).fetchall()
            return {row["id"]: {"revision": row["revision"], "text": row["text"], "topics": json.loads(row["topics_json"])} for row in rows}

    async def categories(
        self, team_id: str, user_id: str, workspace_id: str, *, identities: list[str] | None = None, query: str = ""
    ) -> list[dict[str, Any]]:
        if identities == []:
            return []
        ids = list(dict.fromkeys(identities or []))
        if len(ids) > 2048:
            raise ValueError("knowledge_category_input_invalid")
        params: list[Any] = [team_id, team_id, user_id, workspace_id]
        predicate = ""
        if identities is not None:
            predicate += " AND id IN (" + ",".join("?" for _ in ids) + ")"
            params.extend(ids)
        if query:
            predicate += " AND (instr(lower(name),lower(?))>0 OR instr(lower(aliases_json),lower(?))>0)"
            params.extend([query, query])
        async with nullcontext(self._read_connection) if self._read_connection is not None else open_pooled_read_ctx(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM kb_source_categories WHERE team_id=? AND ((scope='team' AND scope_id=?) OR (scope='personal' AND scope_id=?) OR (scope='space' AND scope_id=?))"
                    + predicate
                    + " ORDER BY name,id LIMIT ?",
                    (*params, len(ids) if identities is not None else 100),
                )
            ).fetchall()
        return [{**dict(row), "aliases": json.loads(row["aliases_json"])} for row in rows]

    async def save_category(
        self,
        *,
        team_id: str,
        scope: str,
        scope_id: str,
        name: str,
        aliases: list[str],
        parent_id: str | None,
        actor_user_id: str,
        category_id: str | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if parent_id:
                parent = await (
                    await db.execute("SELECT team_id,scope,scope_id,parent_id FROM kb_source_categories WHERE id=?", (parent_id,))
                ).fetchone()
                if not parent or tuple(parent[:3]) != (team_id, scope, scope_id) or parent[3] or parent_id == category_id:
                    raise NavigationConflict("knowledge_category_parent_invalid")
            if (
                parent_id
                and category_id
                and await (await db.execute("SELECT 1 FROM kb_source_categories WHERE parent_id=? LIMIT 1", (category_id,))).fetchone()
            ):
                raise NavigationConflict("knowledge_category_parent_invalid")
            if category_id:
                row = await (
                    await db.execute("SELECT team_id,scope,scope_id,revision FROM kb_source_categories WHERE id=?", (category_id,))
                ).fetchone()
                if not row or tuple(row) != (team_id, scope, scope_id, revision):
                    raise NavigationConflict("knowledge_category_changed")
                if parent_id and await (await db.execute("SELECT 1 FROM kb_source_categories WHERE parent_id=? LIMIT 1", (category_id,))).fetchone():
                    raise NavigationConflict("knowledge_category_parent_invalid")
                await db.execute(
                    "UPDATE kb_source_categories SET name=?,aliases_json=?,parent_id=?,revision=revision+1,actor_user_id=? WHERE id=?",
                    (name, _json(aliases), parent_id, actor_user_id, category_id),
                )
            else:
                category_id = _id("kbcat_")
                await db.execute(
                    "INSERT INTO kb_source_categories(id,team_id,scope,scope_id,name,aliases_json,parent_id,actor_user_id) VALUES (?,?,?,?,?,?,?,?)",
                    (category_id, team_id, scope, scope_id, name, _json(aliases), parent_id, actor_user_id),
                )
            await db.commit()
        return {"id": category_id, "revision": (revision + 1) if revision is not None else 1}

    @staticmethod
    async def _write_description(
        db: aiosqlite.Connection,
        file: KbFileDTO,
        *,
        method: str,
        origin_key: str,
        text: str,
        actor_user_id: str,
        topics: list[str] | None = None,
        chunks: list[str] | None = None,
        coverage: str = "sampled",
        provider: str | None = None,
        model: str | None = None,
        matrix: dict[str, Any] | None = None,
    ) -> None:
        await db.execute(
            "INSERT INTO kb_source_descriptions(id,file_id,team_id,membership_revision,generation_id,source_version,origin_key,method,schema_version,generator_version,revision,text,"
            "topics_json,chunks_json,coverage,provider,model,matrix_workspace_id,matrix_json,actor_user_id,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,1,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id,origin_key) DO UPDATE SET membership_revision=excluded.membership_revision,generation_id=excluded.generation_id,source_version=excluded.source_version,"
            "schema_version=excluded.schema_version,generator_version=excluded.generator_version,revision=kb_source_descriptions.revision+1,text=excluded.text,topics_json=excluded.topics_json,chunks_json=excluded.chunks_json,coverage=excluded.coverage,"
            "provider=excluded.provider,model=excluded.model,matrix_workspace_id=excluded.matrix_workspace_id,matrix_json=excluded.matrix_json,actor_user_id=excluded.actor_user_id,updated_at=excluded.updated_at",
            (
                _id("kbdesc_"),
                file.file_id,
                file.team_id,
                file.membership_revision,
                file.active_generation_id,
                file.version,
                origin_key,
                method,
                GENERATOR_VERSION if method == "standalone" else None,
                text,
                _json(topics or []),
                _json(chunks or []),
                coverage,
                provider,
                model,
                matrix.get("workspace_id") if matrix else None,
                _json(matrix) if matrix else None,
                actor_user_id,
                _now(),
            ),
        )

    async def edit(
        self,
        file: KbFileDTO,
        *,
        revision: int,
        actor_user_id: str,
        manual_text: str | None = None,
        edit_text: bool = False,
        category_ids: list[str] | None = None,
    ) -> int:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._current(db, file)
            await self.ensure_base(db, file)
            current = await (await db.execute("SELECT revision FROM kb_source_navigation WHERE file_id=?", (file.file_id,))).fetchone()
            if current[0] != revision:
                raise NavigationConflict("knowledge_navigation_changed")
            if edit_text:
                if manual_text:
                    await self._write_description(
                        db, file, method="manual", origin_key="manual", text=manual_text, actor_user_id=actor_user_id, coverage="manual"
                    )
                else:
                    await db.execute("DELETE FROM kb_source_descriptions WHERE file_id=? AND method='manual'", (file.file_id,))
                await db.execute("UPDATE kb_source_navigation SET revision=revision+1 WHERE file_id=?", (file.file_id,))
            if category_ids is not None:
                await db.execute("UPDATE kb_source_navigation SET labels_override=1 WHERE file_id=?", (file.file_id,))
                await db.execute("DELETE FROM kb_source_category_files WHERE file_id=?", (file.file_id,))
                await db.executemany(
                    "INSERT INTO kb_source_category_files VALUES (?,?,?,?)",
                    [(file.file_id, cid, file.active_generation_id, file.membership_revision) for cid in category_ids],
                )
            await db.commit()
            return revision + int(edit_text)

    async def cache_matrix(
        self,
        file: KbFileDTO,
        *,
        text: str,
        topics: list[str],
        matrix: dict[str, Any],
        actor_user_id: str,
        chunks: list[str],
        provider: str | None,
        model: str | None,
    ) -> None:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._current(db, file)
            origin = f"matrix:{matrix['matrix_id']}:{matrix['column_id']}"
            previous = await (
                await db.execute(
                    "SELECT text,topics_json,matrix_json FROM kb_source_descriptions WHERE file_id=? AND origin_key=?", (file.file_id, origin)
                )
            ).fetchone()
            if previous and tuple(previous) == (text, _json(topics), _json(matrix)):
                return
            await self._write_description(
                db,
                file,
                method="matrix",
                origin_key=origin,
                text=text,
                topics=topics,
                chunks=chunks,
                matrix=matrix,
                actor_user_id=actor_user_id,
                provider=provider,
                model=model,
                coverage="matrix_result",
            )
            await db.commit()

    @staticmethod
    async def enqueue_in(
        db: aiosqlite.Connection,
        file: KbFileDTO,
        *,
        generation_id: str,
        fingerprint: str,
        admission_key: str,
        actor_user_id: str,
        workspace_id: str,
        manual_revision: int,
        source_version: int | None = None,
    ) -> str:
        identity = _id("kbjob_")
        stamp = _now()
        await db.execute(
            "INSERT INTO kb_source_description_jobs(id,team_id,file_id,admission_key,actor_user_id,workspace_id,generation_id,source_version,membership_revision,"
            "manual_revision,assignment_fingerprint,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?) "
            "ON CONFLICT(team_id,admission_key) DO NOTHING",
            (
                identity,
                file.team_id,
                file.file_id,
                admission_key,
                actor_user_id,
                workspace_id,
                generation_id,
                source_version if source_version is not None else file.version,
                file.membership_revision,
                manual_revision,
                fingerprint,
                stamp,
                stamp,
            ),
        )
        row = await (
            await db.execute(
                "SELECT id,file_id,generation_id,actor_user_id,workspace_id,membership_revision,manual_revision,source_version,assignment_fingerprint FROM kb_source_description_jobs WHERE team_id=? AND admission_key=?",
                (file.team_id, admission_key),
            )
        ).fetchone()
        if tuple(row[1:]) != (
            file.file_id,
            generation_id,
            actor_user_id,
            workspace_id,
            file.membership_revision,
            manual_revision,
            source_version if source_version is not None else file.version,
            fingerprint,
        ):
            raise NavigationConflict("knowledge_generation_action_conflict")
        return row[0]

    async def enqueue(self, file: KbFileDTO, *, fingerprint: str, admission_key: str, actor_user_id: str, workspace_id: str, revision: int) -> str:
        if not file.active_generation_id:
            raise NavigationConflict("knowledge_generation_unverified")
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._current(db, file)
            if file.active_generation_id not in await self._published(db, [file]):
                raise NavigationConflict("knowledge_generation_unverified")
            await self.ensure_base(db, file)
            current = await (await db.execute("SELECT revision FROM kb_source_navigation WHERE file_id=?", (file.file_id,))).fetchone()
            if current[0] != revision:
                raise NavigationConflict("knowledge_navigation_changed")
            identity = await self.enqueue_in(
                db,
                file,
                generation_id=file.active_generation_id,
                fingerprint=fingerprint,
                admission_key=admission_key,
                actor_user_id=actor_user_id,
                workspace_id=workspace_id,
                manual_revision=revision,
            )
            await db.commit()
            return identity

    async def latest_job(self, file_id: str) -> dict[str, Any] | None:
        async with open_shared_ctx(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM kb_source_description_jobs WHERE file_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (file_id,))
            ).fetchone()
            return dict(row) if row else None

    async def claim(self, owner: str) -> dict[str, Any] | None:
        stamp = time.time()
        async with open_shared_ctx(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE kb_source_description_jobs SET state='pending',lease_owner=NULL,lease_until=NULL WHERE state='claimed' AND lease_until<?",
                (stamp,),
            )
            await db.execute(
                "UPDATE kb_source_description_jobs SET state='failed',error_code='outcome_unknown',updated_at=? WHERE state='dispatching' AND lease_until<?",
                (_now(), stamp),
            )
            await db.execute(
                "UPDATE kb_source_description_jobs AS queued SET state='superseded',error_code='prior_outcome_unknown',updated_at=? WHERE queued.state='pending' AND EXISTS (SELECT 1 FROM kb_source_description_jobs AS prior WHERE prior.file_id=queued.file_id AND prior.generation_id=queued.generation_id AND prior.assignment_fingerprint=queued.assignment_fingerprint AND prior.error_code='outcome_unknown' AND prior.updated_at>=queued.created_at)",
                (_now(),),
            )
            row = await (
                await db.execute(
                    "UPDATE kb_source_description_jobs SET state='claimed',lease_owner=?,lease_until=?,updated_at=? WHERE id=(SELECT queued.id FROM kb_source_description_jobs AS queued WHERE queued.state='pending' AND NOT EXISTS (SELECT 1 FROM kb_source_description_jobs AS active WHERE active.file_id=queued.file_id AND active.generation_id=queued.generation_id AND active.membership_revision=queued.membership_revision AND active.assignment_fingerprint=queued.assignment_fingerprint AND active.state IN ('claimed','dispatching')) ORDER BY queued.created_at,queued.id LIMIT 1) RETURNING *",
                    (owner, stamp + 300, _now()),
                )
            ).fetchone()
            await db.commit()
            return dict(row) if row else None

    async def dispatch(self, job: dict[str, Any], attempts: int) -> bool:
        async with open_shared_ctx(self.path) as db:
            cursor = await db.execute(
                "UPDATE kb_source_description_jobs SET state='dispatching',reserved_attempts=?,updated_at=? WHERE id=? AND state='claimed' AND lease_owner=? AND lease_until>?",
                (attempts, _now(), job["id"], job["lease_owner"], time.time()),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def finish(self, job: dict[str, Any], state: str, *, error: str | None = None, usage: dict[str, Any] | None = None) -> None:
        async with open_shared_ctx(self.path) as db:
            await db.execute(
                "UPDATE kb_source_description_jobs SET state=?,error_code=?,usage_json=?,updated_at=? WHERE id=? AND lease_owner=? AND state IN ('claimed','dispatching')",
                (state, error, _json(usage or {}), _now(), job["id"], job["lease_owner"]),
            )
            await db.commit()

    async def adopt(
        self,
        job: dict[str, Any],
        file: KbFileDTO,
        *,
        text: str,
        topics: list[str],
        chunks: list[str],
        provider: str,
        model: str,
        usage: dict[str, Any],
    ) -> None:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._current(db, file)
            current = await (await db.execute("SELECT revision FROM kb_source_navigation WHERE file_id=?", (file.file_id,))).fetchone()
            if not current or current[0] != job["manual_revision"]:
                raise NavigationConflict("knowledge_navigation_changed")
            claim = await (await db.execute("SELECT state,lease_owner FROM kb_source_description_jobs WHERE id=?", (job["id"],))).fetchone()
            if not claim or tuple(claim) != ("dispatching", job["lease_owner"]):
                raise NavigationConflict("knowledge_generation_claim_changed")
            await self._write_description(
                db,
                file,
                method="standalone",
                origin_key="standalone",
                text=text,
                topics=topics,
                chunks=chunks,
                actor_user_id=job["actor_user_id"],
                provider=provider,
                model=model,
            )
            await db.execute(
                "UPDATE kb_source_description_jobs SET state='ready',usage_json=?,updated_at=? WHERE id=?", (_json(usage), _now(), job["id"])
            )
            await db.commit()
