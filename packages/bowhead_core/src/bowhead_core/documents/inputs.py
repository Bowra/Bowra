"""Private, byte-exact input copies. Not an execution snapshot or Office parser.

Reserve the entire candidate set before create-only blob I/O. Replays use those
same identities and hashes; no partial set can become a runnable input set.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite
import structlog

from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_purge import purge_artifacts
from bowhead_core.artifact_store import ArtifactStore
from bowhead_core.chat_turn_source import chat_turn_user_source_projection
from bowhead_core.documents.request import DocumentRequestError, DocumentScope, verify_proposal_source
from bowhead_core.documents.retention import INTERNAL_PRODUCER, active_ref_sql
from bowhead_core.documents.store import _authorize, _digest, _job, _json
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
RECOVERY_SECONDS = 24 * 3600
log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FrozenInput:
    artifact_id: str
    source_artifact_id: str
    role: str
    sha256: str
    size_bytes: int


async def _authorized_inputs(
    db: aiosqlite.Connection, scope: DocumentScope, job_id: str, *, write: bool
) -> tuple[aiosqlite.Row, list[aiosqlite.Row]]:
    await _authorize(db, scope, write=write)
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT j.* FROM document_jobs j JOIN sessions s ON s.id=j.session_id
        WHERE j.id=? AND j.team_id=? AND j.workspace_id=? AND j.requested_by=? AND j.status='queued'
          AND s.user_id=j.requested_by AND s.workspace_id=j.workspace_id AND s.deleted_at IS NULL""",
        (job_id, *scope.sql_params()),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise DocumentRequestError("document_not_found")
    source = await _read_chat_turn_user_source_in_tx(
        db, turn_id=row["origin_turn_id"], expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id
    )
    if (
        source is None
        or source.session_id != row["session_id"]
        or _digest(_json(chat_turn_user_source_projection(source))) != row["user_source_sha256"]
    ):
        raise DocumentRequestError("document_source_unavailable")
    proposal = _job(row).proposal
    verify_proposal_source(proposal, source)
    from bowhead_core.documents.revision_admission import authorized_revision_inputs_in_tx, has_revision_inputs_in_tx

    if await has_revision_inputs_in_tx(db, job_id):
        return row, await authorized_revision_inputs_in_tx(db, job=row)
    originals = []
    for item in proposal.inputs:
        async with db.execute(
            """SELECT a.* FROM artifacts a WHERE a.id=? AND a.created_by=? AND a.deleted_at IS NULL AND a.status='ready'
            AND COALESCE(a.producer_kind,'')<>? AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)
            AND ((? IS NOT NULL AND EXISTS(SELECT 1 FROM turn_attachments ta WHERE ta.turn_id=? AND ta.artifact_id=a.id))
              OR (? IS NULL AND EXISTS(SELECT 1 FROM session_attachments sa WHERE sa.session_id=? AND sa.artifact_id=a.id AND sa.detached_at IS NULL)))""",
            (
                item.artifact_id,
                scope.user_id,
                INTERNAL_PRODUCER,
                item.current_message_position,
                source.turn_id,
                item.current_message_position,
                source.session_id,
            ),
        ) as cur:
            original = await cur.fetchone()
        if original is None:
            raise DocumentRequestError("document_input_unavailable")
        originals.append(original)
    return row, originals


async def _copies(db: aiosqlite.Connection, job_id: str) -> list[aiosqlite.Row]:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM document_input_copies WHERE job_id=? ORDER BY input_index", (job_id,)) as cur:
        return list(await cur.fetchall())


class DocumentInputStore:
    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    async def freeze(self, *, scope: DocumentScope, job_id: str) -> tuple[FrozenInput, ...]:
        """Bounded opaque bytes, no model or role/format approval. Retry is safe.

        Blob/DB failures propagate; a retry resumes the persisted candidate set.
        """
        async with open_shared_ctx(self.artifacts.db_path) as db:
            await db.execute("BEGIN")
            job, originals = await _authorized_inputs(db, scope, job_id, write=True)
            copies = await _copies(db, job_id)
        if not copies:
            identities = []
            total = 0
            for original in originals:
                content = await self.artifacts.backend.read_bounded(original["path"], limit=MAX_INPUT_BYTES)
                total += len(content)
                if total > MAX_TOTAL_BYTES:
                    raise DocumentRequestError("document_inputs_too_large")
                identity = (len(content), hashlib.sha256(content).hexdigest())
                revision_sha = dict(original).get("revision_sha256")
                if revision_sha is not None and identity != (original["size_bytes"], revision_sha):
                    raise DocumentRequestError("document_source_hash_mismatch")
                async with (
                    open_shared_ctx(self.artifacts.db_path) as db,
                    db.execute(
                        """SELECT c.size_bytes,c.sha256 FROM document_continuations a LEFT JOIN document_input_copies c
                    ON c.job_id=a.parent_job_id AND c.source_artifact_id=? WHERE a.job_id=?""",
                        (original["id"], job_id),
                    ) as cursor,
                ):
                    inherited = await cursor.fetchone()
                if inherited is not None and identity != tuple(inherited):
                    raise DocumentRequestError("document_input_changed")
                identities.append(identity)
                del content
            async with open_shared_ctx(self.artifacts.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                _, current = await _authorized_inputs(db, scope, job_id, write=True)
                copies = await _copies(db, job_id)
                if not copies:
                    if any(a["path"] != b["path"] for a, b in zip(originals, current, strict=True)):
                        raise DocumentRequestError("document_input_changed")
                    now = datetime.now(UTC).isoformat()
                    await db.execute(
                        "INSERT INTO document_input_sets(job_id,team_id,workspace_id,owner_user_id,request_hash,input_count,created_at) VALUES (?,?,?,?,?,?,?)",
                        (job_id, *scope.sql_params(), job["request_hash"], len(originals), now),
                    )
                    for index, (original, (size, sha), item) in enumerate(zip(originals, identities, _job(job).proposal.inputs, strict=True)):
                        aid = f"art_{uuid.uuid4().hex}"
                        path = self.artifacts.backend.immutable_upload_path(uuid.uuid4().hex)
                        await db.execute(
                            """INSERT INTO artifacts(id,filename,mime,size,source,path,created_by,created_at,kind,scope,inject,trust,producer_kind,status,workspace_id)
                            VALUES (?,?,?,?,'document_input',?,?,?,'tool_data','user','list','untrusted',?,'document_staging',?)""",
                            # Space isolation batch 3: `DocumentScope` already has the authoritative workspace_id;
                            # no need to infer it through backfill (this path has no session/turn/canvas to infer from).
                            (
                                aid,
                                original["filename"],
                                original["mime"],
                                size,
                                path,
                                scope.user_id,
                                now,
                                INTERNAL_PRODUCER,
                                scope.workspace_id or None,
                            ),
                        )
                        await db.execute(
                            """INSERT INTO document_input_copies(artifact_id,job_id,input_index,source_artifact_id,source_path,path,filename,mime,size_bytes,sha256,role,cleanup_after)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                aid,
                                job_id,
                                index,
                                original["id"],
                                original["path"],
                                path,
                                original["filename"],
                                original["mime"],
                                size,
                                sha,
                                item.role,
                                time.time() + RECOVERY_SECONDS,
                            ),
                        )
                        await db.execute(
                            """INSERT INTO document_artifact_refs(team_id,workspace_id,owner_user_id,owner_kind,owner_id,ref_key,artifact_id,artifact_sha256,retention_class)
                            VALUES (?,?,?,'job',?,?,?,?,'execution')""",
                            (*scope.sql_params(), job_id, f"input:{original['id']}", aid, sha),
                        )
                    copies = await _copies(db, job_id)
                    await db.commit()
            # Reservation is durable before any candidate PUT. Recovery below
            # rereads the exact SHA; no collection of file buffers spans SQL.
        for copy in copies:
            await self._check(scope, job_id, write=True)
            try:
                content = await self.artifacts.backend.read_bounded(copy["path"], limit=MAX_INPUT_BYTES)
            except FileNotFoundError:
                if copy["verified_at"] is not None:
                    raise DocumentRequestError("document_frozen_input_missing") from None
                current = await self._check(scope, job_id, write=True)
                if current[copy["input_index"]]["path"] != copy["source_path"]:
                    raise DocumentRequestError("document_input_changed")
                content = await self.artifacts.backend.read_bounded(copy["source_path"], limit=MAX_INPUT_BYTES)
                self._verify(copy, content, code="document_source_hash_mismatch")
                await self._check(scope, job_id, write=True)
                await self.artifacts.put_account_owned_immutable(user_id=scope.user_id, path=copy["path"], content=content, mime=copy["mime"])
                # False/unknown PUT responses never establish byte identity.
                content = await self.artifacts.backend.read_bounded(copy["path"], limit=MAX_INPUT_BYTES)
            self._verify(copy, content)
            async with open_shared_ctx(self.artifacts.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await _authorized_inputs(db, scope, job_id, write=True)
                await db.execute(
                    "UPDATE document_input_copies SET verified_at=? WHERE artifact_id=? AND verified_at IS NULL AND cleanup_claimed_at IS NULL",
                    (datetime.now(UTC).isoformat(), copy["artifact_id"]),
                )
                await db.execute("UPDATE artifacts SET status='ready' WHERE id=? AND status='document_staging'", (copy["artifact_id"],))
                await db.commit()
        async with open_shared_ctx(self.artifacts.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await _authorized_inputs(db, scope, job_id, write=True)
            await db.execute(
                "UPDATE document_input_sets SET frozen_at=? WHERE job_id=? AND frozen_at IS NULL", (datetime.now(UTC).isoformat(), job_id)
            )
            await db.commit()
        return tuple(
            FrozenInput(
                artifact_id=copy["artifact_id"],
                source_artifact_id=copy["source_artifact_id"],
                role=copy["role"],
                sha256=copy["sha256"],
                size_bytes=copy["size_bytes"],
            )
            for copy in copies
        )

    async def _check(self, scope: DocumentScope, job_id: str, *, write: bool) -> list[aiosqlite.Row]:
        async with open_shared_ctx(self.artifacts.db_path) as db:
            await db.execute("BEGIN")
            _, originals = await _authorized_inputs(db, scope, job_id, write=write)
            return originals

    @staticmethod
    def _verify(copy: aiosqlite.Row, content: bytes, *, code: str = "document_frozen_hash_mismatch") -> None:
        if len(content) != copy["size_bytes"] or hashlib.sha256(content).hexdigest() != copy["sha256"]:
            raise DocumentRequestError(code)

    async def read(self, *, scope: DocumentScope, job_id: str, artifact_id: str) -> bytes:
        async with open_shared_ctx(self.artifacts.db_path) as db:
            await db.execute("BEGIN")
            await _authorized_inputs(db, scope, job_id, write=False)
            async with db.execute(
                """SELECT c.* FROM document_input_copies c JOIN document_input_sets f ON f.job_id=c.job_id
                JOIN artifacts a ON a.id=c.artifact_id JOIN document_artifact_refs r ON r.artifact_id=c.artifact_id AND r.owner_id=c.job_id
                WHERE c.job_id=? AND c.artifact_id=? AND f.frozen_at IS NOT NULL AND c.verified_at IS NOT NULL
                  AND c.cleanup_claimed_at IS NULL AND r.released_at IS NULL AND a.status='ready' AND a.deleted_at IS NULL
                  AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)""",
                (job_id, artifact_id),
            ) as cur:
                copy = await cur.fetchone()
            if copy is None:
                raise DocumentRequestError("document_input_unavailable")
        content = await self.artifacts.backend.read_bounded(copy["path"], limit=MAX_INPUT_BYTES)
        self._verify(copy, content)
        await self._check(scope, job_id, write=False)
        return content


async def cleanup_document_inputs(artifacts: ArtifactStore, *, now: float, limit: int = 200) -> int:
    """Feature-independent, bounded reaper with durable late-PUT tombstones.

    Cancelled candidates are reclaimable after 24h; queued work never loses its
    references due to age alone. Repeat exact-key deletion after metadata purge
    catches delayed create-only writes without making a candidate usable again.
    """
    if not 1 <= limit <= 200:
        raise ValueError("invalid document cleanup limit")
    async with open_shared_ctx(artifacts.db_path) as db:
        if await active_ref_sql(db) == "0":
            return 0
        async with db.execute(
            "SELECT artifact_id FROM document_input_copies WHERE cleanup_after<=? ORDER BY cleanup_after,artifact_id LIMIT ?", (now, limit)
        ) as cur:
            ids = [row[0] for row in await cur.fetchall()]
    removed = 0
    for aid in ids:
        async with open_shared_ctx(artifacts.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT c.path,c.cleanup_claimed_at,j.status,j.updated_at FROM document_input_copies c
                JOIN document_jobs j ON j.id=c.job_id WHERE c.artifact_id=? AND c.cleanup_after<=?""",
                (aid, now),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                continue
            await db.execute("UPDATE document_input_copies SET cleanup_after=? WHERE artifact_id=?", (now + RECOVERY_SECONDS, aid))
            if row[2] != "cancelled" or datetime.fromisoformat(row[3]).timestamp() + RECOVERY_SECONDS > now:
                await db.commit()
                continue
            async with db.execute(
                """SELECT 1 FROM document_revision_inputs i JOIN document_jobs j ON j.id=i.job_id
                WHERE i.artifact_id=? AND j.status='queued' LIMIT 1""",
                (aid,),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    await db.commit()
                    continue
            timestamp = datetime.fromtimestamp(now, UTC).isoformat()
            if row[1] is None:
                await db.execute("UPDATE document_input_copies SET cleanup_claimed_at=? WHERE artifact_id=?", (timestamp, aid))
                await db.execute("UPDATE document_artifact_refs SET released_at=? WHERE artifact_id=? AND released_at IS NULL", (timestamp, aid))
                await db.execute("UPDATE artifacts SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (timestamp, aid))
            await db.commit()
        result = await purge_artifacts(artifacts.db_path, artifacts.backend, [aid], user_id=None)
        removed += result.rows_removed
        async with (
            open_shared_ctx(artifacts.db_path) as db,
            db.execute("SELECT 1 FROM artifact_purge_claims WHERE artifact_id=? AND path=? AND completed_at IS NOT NULL", (aid, row[0])) as cur,
        ):
            complete = await cur.fetchone() is not None
        if complete and result.rows_removed == 0:
            try:
                await artifacts.backend.delete(row[0])
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001 - durable tombstone retries next sweep
                log.warning("document.input_cleanup_failed", artifact_id=aid, error_type=type(exc).__name__)
    return removed
