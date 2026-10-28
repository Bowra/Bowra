"""Private draft state and exact-byte publication; no model or Office parsing.

The immutable admission journal stays intact. This one-to-one execution record
is the authoritative product status; admission cancellation fences it atomically.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import PurePosixPath

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import ArtifactStore
from bowhead_core.documents.executor import WorkerResult
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_core.documents.retention import INTERNAL_PRODUCER
from bowhead_core.documents.store import _authorize, _command_id, _digest, _json
from bowhead_core.rate_card import RATE_CARD_VERSION

ACTIVE = ("preparing", "planning", "rendering")
PUBLIC_PRODUCER = "template_document_v1"


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _scope(db, scope: DocumentScope, job_id: str, *, write: bool):
    await _authorize(db, scope, write=write)
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT j.*,COALESCE(dc.instruction,u.user_text) AS request_text,w.status AS space_status,wm.role AS space_role FROM document_jobs j JOIN sessions s ON s.id=j.session_id
        JOIN chat_turn_user_sources u ON u.turn_id=j.origin_turn_id JOIN workspaces w ON w.id=j.workspace_id
        JOIN workspace_members wm ON wm.workspace_id=j.workspace_id AND wm.user_id=j.requested_by
        LEFT JOIN document_continuations dc ON dc.job_id=j.id
        WHERE j.id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=?
          AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id AND s.deleted_at IS NULL""",
        (job_id, *scope.sql_params()),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise DocumentRequestError("document_not_found")
    return row


async def _execution(db, job_id: str):
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM document_executions WHERE job_id=?", (job_id,)) as cursor:
        return await cursor.fetchone()


async def _fence(db, scope: DocumentScope, job_id: str, owner: str):
    job = await _scope(db, scope, job_id, write=True)
    execution = await _execution(db, job_id)
    if (
        job["status"] != "queued"
        or execution is None
        or execution["owner"] != owner
        or (execution["state"] not in ACTIVE)
        or (execution["lease_until"] <= time.time())
    ):
        raise DocumentRequestError("document_execution_fenced")
    async with db.execute("SELECT 1 FROM document_turn_dispatches WHERE job_id=?", (job_id,)) as cursor:
        turn_owned = await cursor.fetchone() is not None
    if turn_owned:
        from bowhead_core.document_chat_continuation import DocumentTurnOwner
        from bowhead_core.documents.continuation import validate_document_worker_in_tx

        async with db.execute(
            "SELECT attempt_no,creation_generation,owner_id FROM document_planning_attempts WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1",
            (job_id,),
        ) as cursor:
            attempt = await cursor.fetchone()
        if attempt is None or attempt["owner_id"] != owner:
            raise DocumentRequestError("document_execution_fenced")
        if attempt["attempt_no"] == 2:
            async with db.execute(
                "SELECT 1 FROM clarification_document_continuations WHERE job_id=? AND consumed_generation=? AND consumed_owner_id=?",
                (job_id, attempt["creation_generation"], owner),
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise DocumentRequestError("document_execution_fenced")
        await validate_document_worker_in_tx(
            db,
            worker=DocumentTurnOwner(
                team_id=scope.team_id,
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                session_id=job["session_id"],
                turn_id=job["origin_turn_id"],
                generation=attempt["creation_generation"],
                owner_id=owner,
            ),
            job_id=job_id,
        )
    return execution


async def _projection_with_version(db, job, execution) -> dict:
    """`_projection` plus the "draft N" number every authorized job read returns."""
    from bowhead_core.documents.planning_instruction import clarification_evidence_sources_in_tx
    from bowhead_core.documents.revision import draft_version_in_tx

    value = {**_projection(job, execution), "draft_version": await draft_version_in_tx(db, job["id"])}
    if "draft" in value:
        sources = await clarification_evidence_sources_in_tx(db, job=job)
        if sources:
            value["clarification_evidence_sources"] = sources
    return value


def _projection(job, execution) -> dict:
    from bowhead_core.space_governance import normalize_space_role

    value = {
        "id": job["id"],
        "state": "queued" if job["status"] == "queued" else "cancelled",
        "origin_turn_id": job["origin_turn_id"],
        "request_text": job["request_text"],
        "can_confirm": job["space_status"] == "active" and normalize_space_role(job["space_role"]) in {"admin", "member"},
        "can_revise": False,
        "usage": {"settled_credits": execution["settled_credits"] if execution else 0, "pending": False, "settlement_pending": False},
    }
    if execution:
        value.update(
            state=execution["state"],
            error_code=execution["error_code"],
            question=execution["question"],
            draft_digest=execution["draft_digest"],
            # Last execution change; for a review draft this is when the draft was produced (Canvas toolbar time).
            updated_at=execution["updated_at"],
        )
        if execution["draft_json"] and execution["state"] in {"review", "published"}:
            value["draft"] = json.loads(execution["draft_json"])
    return value


class DocumentDeliveryStore:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts
        self.path = artifacts.db_path

    async def claim(self, *, scope: DocumentScope, job_id: str) -> str | None:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            job = await _scope(db, scope, job_id, write=True)
            if job["status"] != "queued":
                return None
            async with db.execute("SELECT 1 FROM document_turn_dispatches WHERE job_id=?", (job_id,)) as cursor:
                if await cursor.fetchone():
                    return None
            previous = await _execution(db, job_id)
            if previous is not None:
                # Do not reissue a model call whose outcome may be unknown.
                if previous["state"] in ACTIVE and previous["lease_until"] <= time.time():
                    await db.execute(
                        "UPDATE document_executions SET state='failed',error_code='document_execution_interrupted',updated_at=? WHERE job_id=?",
                        (_now(), job_id),
                    )
                    await db.commit()
                return None
            async with db.execute(
                """SELECT j.team_id FROM document_executions e JOIN document_jobs j ON j.id=e.job_id
                WHERE e.state IN ('preparing','planning','rendering') AND e.lease_until>?""",
                (time.time(),),
            ) as cursor:
                busy = [row[0] for row in await cursor.fetchall()]
            if len(busy) >= 2 or scope.team_id in busy:
                return None
            owner, now = uuid.uuid4().hex, _now()
            await db.execute(
                """INSERT INTO document_executions(job_id,state,owner,lease_until,rate_card_version,created_at,updated_at)
                VALUES (?,'preparing',?,?,?,?,?)""",
                (job_id, owner, time.time() + 600, RATE_CARD_VERSION, now, now),
            )
            await db.commit()
            return owner

    async def recover_expired(self) -> None:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """UPDATE document_executions SET state='failed',error_code='document_execution_interrupted',updated_at=?
              WHERE state IN ('preparing','planning','rendering') AND lease_until<=?
                AND NOT EXISTS(SELECT 1 FROM document_turn_dispatches d WHERE d.job_id=document_executions.job_id)""",
                (_now(), time.time()),
            )
            await db.commit()

    async def cleanup(self) -> None:
        """Bounded daily private-file cleanup; permanent identities survive purge."""
        from bowhead_core.artifact_purge import purge_artifacts

        now = time.time()
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT f.artifact_id,f.path FROM document_draft_files f JOIN document_executions e ON e.job_id=f.job_id
                WHERE f.cleanup_after<=? AND julianday(e.updated_at)<julianday('now','-1 day') AND (
                  e.state IN ('failed','cancelled') OR (e.state='published' AND f.name LIKE 'page-%.png'
                    AND NOT EXISTS(SELECT 1 FROM document_draft_files p JOIN artifacts a ON a.id=p.artifact_id
                      WHERE p.job_id=f.job_id AND p.name='candidate.pptx')))
                AND NOT EXISTS(SELECT 1 FROM document_revision_inputs i JOIN document_jobs j ON j.id=i.job_id
                  WHERE i.artifact_id=f.artifact_id AND j.status='queued')
                ORDER BY f.cleanup_after,f.artifact_id LIMIT 100""",
                (now,),
            ) as cursor:
                files = await cursor.fetchall()
            for artifact_id, _ in files:
                await db.execute("UPDATE document_draft_files SET cleanup_after=? WHERE artifact_id=?", (now + 86400, artifact_id))
                await db.execute("UPDATE artifacts SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (_now(), artifact_id))
            await db.commit()
        for artifact_id, path in files:
            await purge_artifacts(str(self.path), self.artifacts.backend, [artifact_id], user_id=None)
            async with (
                open_shared_ctx(self.path) as db,
                db.execute(
                    "SELECT 1 FROM artifact_purge_claims WHERE artifact_id=? AND path=? AND completed_at IS NOT NULL", (artifact_id, path)
                ) as cursor,
            ):
                completed = await cursor.fetchone() is not None
            if completed:
                # A cancelled in-flight immutable PUT may have arrived late.
                with suppress(FileNotFoundError):
                    await self.artifacts.backend.delete(path)

    async def transition(self, *, scope: DocumentScope, job_id: str, owner: str, state: str, **values) -> None:
        permitted = {"image_id", "context_json", "response", "error_code", "question"}
        if not set(values) <= permitted or state not in {"planning", "rendering", "failed", "question"}:
            raise ValueError("Invalid document transition")
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            previous = await _fence(db, scope, job_id, owner)
            allowed = {"preparing": {"planning", "failed"}, "planning": {"rendering", "failed"}, "rendering": {"planning", "question", "failed"}}
            if state not in allowed[previous["state"]]:
                raise DocumentRequestError("document_execution_fenced")
            assignments = ",".join(f"{key}=?" for key in values)
            await db.execute(
                f"""UPDATE document_executions SET state=?,updated_at=?,lease_until=CASE
                  WHEN ? IN ('planning','rendering') AND EXISTS(SELECT 1 FROM document_turn_dispatches d WHERE d.job_id=document_executions.job_id)
                  THEN ? ELSE lease_until END{"," if assignments else ""}{assignments} WHERE job_id=?""",
                (state, _now(), state, time.time() + 600, *values.values(), job_id),
            )
            await db.commit()

    async def fail(self, *, job_id: str, owner: str, code: str) -> None:
        """Content-free terminalization also works after revocation."""
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """UPDATE document_executions SET state='failed',error_code=?,updated_at=?
                WHERE job_id=? AND owner=? AND state IN ('preparing','planning','rendering')""",
                (code, _now(), job_id, owner),
            )
            await db.commit()

    async def save_draft(self, *, scope: DocumentScope, job_id: str, owner: str, result: WorkerResult) -> None:
        if result.record.get("kind") != "draft" or not {"candidate.pptx", "candidate.pdf"} <= set(result.files):
            raise DocumentRequestError("document_draft_invalid")
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            execution = await _fence(db, scope, job_id, owner)
            if execution["state"] != "rendering" or execution["image_id"] != result.image_id:
                raise DocumentRequestError("document_execution_fenced")
            async with db.execute("SELECT 1 FROM document_draft_files WHERE job_id=?", (job_id,)) as cursor:
                if await cursor.fetchone():
                    raise DocumentRequestError("document_draft_conflict")
            async with db.execute("SELECT filename FROM document_input_copies WHERE job_id=? AND role='template'", (job_id,)) as cursor:
                template = await cursor.fetchone()
            stem = PurePosixPath(template[0]).stem if template else "Presentation"
            output_stem = "".join(char for char in stem if char.isprintable() and char not in "/\\")[:120] + " - Updated"
            files = []
            for name, content in result.files.items():
                artifact_id = "art_" + uuid.uuid4().hex
                path = self.artifacts.backend.immutable_upload_path(uuid.uuid4().hex)
                mime = (
                    "image/png"
                    if name.endswith(".png")
                    else ("application/pdf" if name.endswith(".pdf") else "application/vnd.openxmlformats-officedocument.presentationml.presentation")
                )
                sha = hashlib.sha256(content).hexdigest()
                await db.execute(
                    """INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,kind,scope,inject,trust,producer_kind,status,workspace_id)
                    VALUES (?,?,?,?,'template_document',?,?,?,'tool_data','user','list','untrusted',?,'document_staging',?)""",
                    (artifact_id, name, mime, len(content), path, scope.user_id, _now(), INTERNAL_PRODUCER, scope.workspace_id),
                )
                await db.execute(
                    "INSERT INTO document_draft_files(job_id,name,artifact_id,path,sha256,size_bytes) VALUES (?,?,?,?,?,?)",
                    (job_id, name, artifact_id, path, sha, len(content)),
                )
                files.append((name, artifact_id, path, mime, sha))
            await db.commit()
        for name, artifact_id, path, mime, sha in files:
            await self.artifacts.put_account_owned_immutable(user_id=scope.user_id, path=path, content=result.files[name], mime=mime)
            content = await self.artifacts.backend.read_bounded(path, limit=32 * 1024 * 1024)
            if hashlib.sha256(content).hexdigest() != sha:
                raise DocumentRequestError("document_draft_hash_mismatch")
            async with open_shared_ctx(self.path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await _fence(db, scope, job_id, owner)
                await db.execute("UPDATE document_draft_files SET verified_at=? WHERE job_id=? AND name=?", (_now(), job_id, name))
                await db.execute("UPDATE artifacts SET status='ready' WHERE id=?", (artifact_id,))
                await db.commit()
        payload = {
            **result.record,
            "output_stem": output_stem,
            "image_id": result.image_id,
            "files": {name: {"artifact_id": aid, "sha256": sha} for name, aid, _, _, sha in files},
        }
        encoded = _json(payload)
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await _fence(db, scope, job_id, owner)
            await db.execute(
                "UPDATE document_executions SET state='review',draft_json=?,draft_digest=?,updated_at=? WHERE job_id=?",
                (encoded, _digest(encoded), _now(), job_id),
            )
            await db.commit()

    async def get(self, *, scope: DocumentScope, job_id: str) -> dict:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN")
            job = await _scope(db, scope, job_id, write=False)
            execution = await _execution(db, job_id)
            # "Draft N" for the Canvas and the Composer revision bar; counts revisions only.
            value = await _projection_with_version(db, job, execution)
            from bowhead_core.document_revision_target import document_reuse_enabled
            from bowhead_core.documents.revision import retained_review_inputs_in_tx

            if document_reuse_enabled() and value["can_confirm"] and value["state"] == "review" and value.get("draft_digest"):
                try:
                    await retained_review_inputs_in_tx(
                        db,
                        scope=scope,
                        session_id=job["session_id"],
                        job_id=job_id,
                        draft_digest=value["draft_digest"],
                    )
                except DocumentRequestError:
                    pass
                else:
                    value["can_revise"] = True
            async with db.execute(
                """SELECT c.state,r.job_id,r.settled_at FROM document_model_calls c LEFT JOIN document_usage_receipts r
                ON r.job_id=c.job_id AND r.attempt_no=c.attempt_no AND r.call_no=c.call_no WHERE c.job_id=?""",
                (job_id,),
            ) as cursor:
                calls = await cursor.fetchall()
            value["usage"] = {
                "settled_credits": execution["settled_credits"] if execution else 0,
                "pending": any((row[1] is None and row[0] in {"dispatched", "unknown"}) or (row[1] is not None and row[2] is None) for row in calls),
                "settlement_pending": any(row[1] is not None and row[2] is None for row in calls),
            }
            return value

    async def read_file(self, *, scope: DocumentScope, job_id: str, name: str, preview: bool) -> tuple[bytes, str]:
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN")
            await _scope(db, scope, job_id, write=False)
            execution = await _execution(db, job_id)
            allowed = ("review", "published") if preview else ("published",)
            if execution is None or execution["state"] not in allowed or (preview and not name.endswith(".png")):
                raise DocumentRequestError("document_not_found")
            async with db.execute(
                """SELECT f.*,a.mime FROM document_draft_files f JOIN artifacts a ON a.id=f.artifact_id
                WHERE f.job_id=? AND f.name=? AND f.verified_at IS NOT NULL AND a.status='ready' AND a.deleted_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)
                AND EXISTS(SELECT 1 FROM document_draft_files parent JOIN artifacts source ON source.id=parent.artifact_id
                  WHERE parent.job_id=f.job_id AND parent.name='candidate.pptx' AND source.deleted_at IS NULL AND source.status='ready'
                  AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=source.id))""",
                (job_id, name),
            ) as cursor:
                file = await cursor.fetchone()
            if file is None:
                raise DocumentRequestError("document_not_found")
        try:
            content = await self.artifacts.backend.read_bounded(file["path"], limit=32 * 1024 * 1024)
        except FileNotFoundError:
            raise DocumentRequestError("document_draft_unavailable") from None
        if len(content) != file["size_bytes"] or hashlib.sha256(content).hexdigest() != file["sha256"]:
            raise DocumentRequestError("document_draft_hash_mismatch")
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN")
            await _scope(db, scope, job_id, write=False)
            current = await _execution(db, job_id)
            if current["state"] not in allowed:
                raise DocumentRequestError("document_not_found")
            async with db.execute(
                """SELECT count(*) FROM document_draft_files f JOIN artifacts a ON a.id=f.artifact_id
                WHERE f.job_id=? AND f.name IN (?, 'candidate.pptx') AND a.status='ready' AND a.deleted_at IS NULL
                  AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)""",
                (job_id, name),
            ) as cursor:
                if (await cursor.fetchone())[0] != (1 if name == "candidate.pptx" else 2):
                    raise DocumentRequestError("document_not_found")
        return content, file["mime"]

    async def confirm(self, *, scope: DocumentScope, job_id: str, command_id: str, draft_digest: str) -> dict:
        _command_id(command_id)
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN")
            job = await _scope(db, scope, job_id, write=False)
            async with db.execute("SELECT * FROM document_confirmations WHERE job_id=?", (job_id,)) as cursor:
                previous = await cursor.fetchone()
            if previous:
                if previous["draft_digest"] != draft_digest:
                    raise DocumentRequestError("document_revision_conflict")
                return await _projection_with_version(db, job, await _execution(db, job_id))
            execution = await _execution(db, job_id)
            if execution is None or execution["state"] != "review" or execution["draft_digest"] != draft_digest:
                raise DocumentRequestError("document_revision_conflict")
            async with db.execute(
                "SELECT job_id FROM document_confirmations WHERE team_id=? AND workspace_id=? AND user_id=? AND command_id=?",
                (*scope.sql_params(), command_id),
            ) as cursor:
                existing = await cursor.fetchone()
                if existing is not None and existing[0] != job_id:
                    raise DocumentRequestError("document_command_conflict")
            async with db.execute("SELECT * FROM document_draft_files WHERE job_id=?", (job_id,)) as cursor:
                files = list(await cursor.fetchall())
        # No model calls or conversion after the user confirms. Recheck all
        # preview/source bytes, then serialize current authority and publication.
        for file in files:
            try:
                content = await self.artifacts.backend.read_bounded(file["path"], limit=32 * 1024 * 1024)
            except FileNotFoundError:
                raise DocumentRequestError("document_draft_unavailable") from None
            if len(content) != file["size_bytes"] or hashlib.sha256(content).hexdigest() != file["sha256"]:
                raise DocumentRequestError("document_draft_hash_mismatch")
        async with open_shared_ctx(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            job = await _scope(db, scope, job_id, write=True)
            execution = await _execution(db, job_id)
            if execution["state"] == "published" and execution["draft_digest"] == draft_digest:
                return await _projection_with_version(db, job, execution)
            if job["status"] != "queued" or execution["state"] != "review" or execution["draft_digest"] != draft_digest:
                raise DocumentRequestError("document_revision_conflict")
            async with db.execute(
                "SELECT job_id FROM document_confirmations WHERE team_id=? AND workspace_id=? AND user_id=? AND command_id=?",
                (*scope.sql_params(), command_id),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    raise DocumentRequestError("document_command_conflict")
            async with db.execute(
                """SELECT count(*) FROM document_draft_files f JOIN artifacts a ON a.id=f.artifact_id
                WHERE f.job_id=? AND f.verified_at IS NOT NULL AND a.status='ready' AND a.deleted_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)""",
                (job_id,),
            ) as cursor:
                if (await cursor.fetchone())[0] != len(files):
                    raise DocumentRequestError("document_draft_unavailable")
            await db.execute(
                "INSERT INTO document_confirmations(job_id,team_id,workspace_id,user_id,command_id,draft_digest,created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, *scope.sql_params(), command_id, draft_digest, _now()),
            )
            await db.execute("UPDATE document_executions SET state='published',updated_at=? WHERE job_id=?", (_now(), job_id))
            source_id = next(file["artifact_id"] for file in files if file["name"] == "candidate.pptx")
            for file in files:
                if file["name"] not in {"candidate.pptx", "candidate.pdf"}:
                    continue
                await db.execute(
                    """UPDATE artifacts SET producer_kind=?,kind='deliverable',scope='session',inject='stub',trust='trusted',
                    created_by_turn=?,derived_from=?,filename=? WHERE id=? AND status='ready' AND deleted_at IS NULL""",
                    (
                        PUBLIC_PRODUCER,
                        job["origin_turn_id"],
                        source_id if file["name"].endswith(".pdf") else None,
                        json.loads(execution["draft_json"])["output_stem"] + (".pdf" if file["name"].endswith(".pdf") else ".pptx"),
                        file["artifact_id"],
                    ),
                )
                await db.execute(
                    "INSERT INTO session_attachments(session_id,artifact_id,pinned_at) VALUES (?,?,?)",
                    (job["session_id"], file["artifact_id"], _now()),
                )
            await db.commit()
        return await self.get(scope=scope, job_id=job_id)
