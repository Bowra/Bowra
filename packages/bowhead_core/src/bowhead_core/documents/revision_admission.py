"""Immutable successor preparation and retained private-input lineage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite

from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.document_revision_target import document_reuse_enabled
from bowhead_core.documents.request import DocumentProposal, DocumentRequestError, InputRoleProposal, UserQuote
from bowhead_core.documents.revision import RevisionInput, _review_inputs

if TYPE_CHECKING:
    from bowhead_core.document_revision_target import DocumentRevisionTarget


@dataclass(frozen=True, slots=True)
class PreparedRevision:
    target: DocumentRevisionTarget
    instruction: str
    inputs: tuple[RevisionInput, ...]

    def as_json(self) -> dict:
        return {"target": self.target.model_dump(mode="json"), "instruction": self.instruction}


async def prepare_revision_in_tx(
    connection: aiosqlite.Connection, *, source: ChatTurnUserSource, evidence: UserQuote
) -> tuple[DocumentProposal, PreparedRevision]:
    if not document_reuse_enabled():
        raise DocumentRequestError("document_capability_unavailable")
    evidence.verify(source.user_text)
    inherited, inputs = await _review_inputs(connection, source)
    instruction = "Revise the supplied current draft. Preserve content not explicitly changed.\n\nRevision request:\n" + source.user_text
    if len(instruction) > 8000:
        raise DocumentRequestError("document_answer_limit")
    proposal = DocumentProposal(
        evidence=evidence,
        inputs=tuple(InputRoleProposal(artifact_id=item.artifact_id, role=item.role, evidence=evidence) for item in inputs),
        requested_outputs=inherited.requested_outputs,
    )
    return proposal, PreparedRevision(source.document_revision_target, instruction, inputs)


async def commit_revision_in_tx(connection: aiosqlite.Connection, *, revision: PreparedRevision, job_id: str, now: str) -> None:
    """Link exact retained inputs before supersession in the admission transaction."""
    await connection.execute(
        "INSERT INTO document_revisions(parent_job_id,job_id,draft_digest,instruction) VALUES (?,?,?,?)",
        (revision.target.job_id, job_id, revision.target.draft_digest, revision.instruction),
    )
    for index, item in enumerate(revision.inputs):
        await connection.execute(
            """INSERT INTO document_revision_inputs(job_id,input_index,artifact_id,role,filename,mime,path,sha256,size_bytes)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (job_id, index, item.artifact_id, item.role, item.filename, item.mime, item.path, item.sha256, item.size_bytes),
        )
    await connection.execute(
        "UPDATE document_jobs SET status='cancelled',phase='terminal',cancel_reason='user_requested',updated_at=? WHERE id=?",
        (now, revision.target.job_id),
    )


async def authorized_revision_inputs_in_tx(connection: aiosqlite.Connection, *, job: aiosqlite.Row) -> list[dict]:
    """Read only the retained private inputs of this admitted successor."""
    async with connection.execute(
        """SELECT i.*,a.created_by,a.workspace_id FROM document_revision_inputs i
        JOIN document_jobs j ON j.id=i.job_id
        JOIN artifacts a ON a.id=i.artifact_id
        WHERE i.job_id=? AND j.requested_by=? AND j.workspace_id=? AND j.team_id=? AND j.session_id=?
          AND a.created_by=j.requested_by AND a.workspace_id=j.workspace_id
          AND a.status='ready' AND a.deleted_at IS NULL AND a.path=i.path AND a.size=i.size_bytes AND a.mime=i.mime
          AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims c WHERE c.artifact_id=a.id)
        ORDER BY i.input_index""",
        (job["id"], job["requested_by"], job["workspace_id"], job["team_id"], job["session_id"]),
    ) as cursor:
        rows = list(await cursor.fetchall())
    proposal = DocumentProposal.model_validate_json(json.dumps(json.loads(job["request_json"])["proposal"]))
    if len(rows) != len(proposal.inputs) or any(
        row["artifact_id"] != item.artifact_id or row["role"] != item.role for row, item in zip(rows, proposal.inputs, strict=True)
    ):
        raise DocumentRequestError("document_input_unavailable")
    return [dict(row) | {"id": row["artifact_id"], "revision_sha256": row["sha256"]} for row in rows]


async def has_revision_inputs_in_tx(connection: aiosqlite.Connection, job_id: str) -> bool:
    async with connection.execute("SELECT 1 FROM document_revision_inputs WHERE job_id=? LIMIT 1", (job_id,)) as cursor:
        return await cursor.fetchone() is not None
