"""Exact private-draft revision authority inside the caller's transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.document_revision_target import DocumentRevisionTarget
from bowhead_core.documents.request import DocumentProposal, DocumentRequestError, DocumentScope


@dataclass(frozen=True, slots=True)
class RevisionInput:
    artifact_id: str
    role: str
    filename: str
    mime: str
    path: str
    sha256: str
    size_bytes: int


async def _target_scope(connection: aiosqlite.Connection, source: ChatTurnUserSource) -> DocumentScope:
    target = source.document_revision_target
    if target is None or target.session_id != source.session_id:
        raise DocumentRequestError("document_not_found")
    if source.attachment_ids or source.attachment_refs or (source.parts is not None and len(source.parts) > 1):
        raise DocumentRequestError("document_revision_inputs_unsupported")
    async with connection.execute(
        """SELECT w.team_id FROM sessions s JOIN workspaces w ON w.id=s.workspace_id
        WHERE s.id=? AND s.user_id=? AND s.workspace_id=? AND s.deleted_at IS NULL""",
        (source.session_id, source.user_id, source.workspace_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise DocumentRequestError("document_not_found")
    return DocumentScope(team_id=row[0], workspace_id=source.workspace_id, user_id=source.user_id)


async def _review_inputs(connection: aiosqlite.Connection, source: ChatTurnUserSource) -> tuple[DocumentProposal, tuple[RevisionInput, ...]]:
    scope = await _target_scope(connection, source)
    target = source.document_revision_target
    return await retained_review_inputs_in_tx(
        connection,
        scope=scope,
        session_id=source.session_id,
        job_id=target.job_id,
        draft_digest=target.draft_digest,
        exclude_turn_id=source.turn_id,
    )


async def retained_review_inputs_in_tx(
    connection: aiosqlite.Connection,
    *,
    scope: DocumentScope,
    session_id: str,
    job_id: str,
    draft_digest: str,
    exclude_turn_id: str | None = None,
) -> tuple[DocumentProposal, tuple[RevisionInput, ...]]:
    """Shared authorized read for revision availability and exact admission."""
    from bowhead_core.documents.delivery import _execution, _scope
    from bowhead_core.documents.store import _digest, _job

    job = await _scope(connection, scope, job_id, write=True)
    if job["session_id"] != session_id or job["origin_turn_id"] == exclude_turn_id:
        raise DocumentRequestError("document_not_found")
    execution = await _execution(connection, job_id)
    if (
        job["status"] != "queued"
        or execution is None
        or execution["state"] != "review"
        or execution["draft_digest"] != draft_digest
        or not execution["draft_json"]
        or _digest(execution["draft_json"]) != draft_digest
    ):
        raise DocumentRequestError("document_revision_conflict")
    async with connection.execute(
        """SELECT f.*,a.mime FROM document_draft_files f JOIN artifacts a ON a.id=f.artifact_id
        WHERE f.job_id=? AND f.name='candidate.pptx' AND f.verified_at IS NOT NULL
          AND a.created_by=? AND a.workspace_id=? AND a.status='ready' AND a.deleted_at IS NULL
          AND a.producer_kind='template_document_internal_v1' AND a.path=f.path AND a.size=f.size_bytes
          AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)""",
        (job_id, scope.user_id, scope.workspace_id),
    ) as cursor:
        candidate = await cursor.fetchone()
    if candidate is None:
        raise DocumentRequestError("document_draft_unavailable")
    declared = json.loads(execution["draft_json"]).get("files", {}).get("candidate.pptx")
    if declared != {"artifact_id": candidate["artifact_id"], "sha256": candidate["sha256"]}:
        raise DocumentRequestError("document_draft_unavailable")
    async with connection.execute(
        """SELECT c.*,a.mime AS artifact_mime FROM document_input_copies c
        JOIN document_input_sets f ON f.job_id=c.job_id JOIN artifacts a ON a.id=c.artifact_id
        JOIN document_artifact_refs r ON r.owner_id=c.job_id AND r.artifact_id=c.artifact_id
        WHERE c.job_id=? AND f.frozen_at IS NOT NULL AND c.verified_at IS NOT NULL
          AND c.cleanup_claimed_at IS NULL AND r.released_at IS NULL
          AND a.created_by=? AND a.workspace_id=? AND a.status='ready' AND a.deleted_at IS NULL
          AND a.path=c.path AND a.size=c.size_bytes AND a.mime=c.mime
          AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)
        ORDER BY c.input_index""",
        (job_id, scope.user_id, scope.workspace_id),
    ) as cursor:
        copies = list(await cursor.fetchall())
    inherited = _job(job).proposal
    if len(copies) != len(inherited.inputs) or sum(item["role"] == "template" for item in copies) != 1:
        raise DocumentRequestError("document_input_unavailable")
    inputs = tuple(
        RevisionInput(
            artifact_id=candidate["artifact_id"] if copy["role"] == "template" else copy["artifact_id"],
            role=copy["role"],
            filename=copy["filename"],
            mime=candidate["mime"] if copy["role"] == "template" else copy["mime"],
            path=candidate["path"] if copy["role"] == "template" else copy["path"],
            sha256=candidate["sha256"] if copy["role"] == "template" else copy["sha256"],
            size_bytes=candidate["size_bytes"] if copy["role"] == "template" else copy["size_bytes"],
        )
        for copy in copies
    )
    return inherited, inputs


async def validate_revision_target_in_tx(connection: aiosqlite.Connection, *, source: ChatTurnUserSource) -> None:
    """Validate new Chat admission without committing or changing its predecessor.

    Exact receipt replay is the caller's separate authorized read path. It must
    not call this new-admission validator after a predecessor is superseded.
    """
    if source.document_revision_target is None:
        return
    await _review_inputs(connection, source)


_MAX_LINEAGE_DEPTH = 256


async def draft_version_in_tx(connection: aiosqlite.Connection, job_id: str) -> int:
    """1-based draft number of ``job_id``: the revision edges in its lineage plus one.

    Revisions (``document_revisions``) start a new draft number; recovery continuations
    (``document_continuations``) carry the same draft forward and do not count.
    """
    async with connection.execute(
        """WITH RECURSIVE chain(job_id, revisions, depth) AS (
             SELECT ?, 0, 0
             UNION ALL
             SELECT COALESCE(r.parent_job_id, c.parent_job_id),
                    chain.revisions + (r.parent_job_id IS NOT NULL),
                    chain.depth + 1
             FROM chain
             LEFT JOIN document_revisions r ON r.job_id = chain.job_id
             LEFT JOIN document_continuations c ON c.job_id = chain.job_id
             WHERE (r.parent_job_id IS NOT NULL OR c.parent_job_id IS NOT NULL) AND chain.depth < ?
           )
           SELECT MAX(revisions) FROM chain""",
        (job_id, _MAX_LINEAGE_DEPTH),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0] or 0) + 1


def draft_display_title(draft_json: str | None) -> str | None:
    """The draft's file name as the Canvas shows it: output stem plus the editable source's extension."""
    if not draft_json:
        return None
    try:
        draft = json.loads(draft_json)
    except ValueError:
        return None
    if not isinstance(draft, dict):
        return None
    stem = draft.get("output_stem")
    if not isinstance(stem, str) or not stem.strip():
        return None
    stem = stem.strip()
    files = draft.get("files")
    names = [name for name in files if isinstance(name, str)] if isinstance(files, dict) else []
    source = next((name for name in names if not name.lower().endswith(".pdf")), None)
    extension = source.rsplit(".", 1)[1].lower() if source and "." in source else None
    if extension and extension.isalnum() and not stem.lower().endswith(f".{extension}"):
        return f"{stem}.{extension}"
    return stem


async def revision_receipt_in_tx(
    connection: aiosqlite.Connection,
    *,
    target: DocumentRevisionTarget,
    session_id: str,
    user_id: str,
    workspace_id: str,
) -> dict | None:
    """Display facts of the exact draft a revision Turn revises: ``{title, version}``.

    Read-only and scoped to the requesting user's job in the requesting Turn's session; returns ``None`` when the
    draft is not the targeted one or has no displayable name. It grants no document access.
    """
    async with connection.execute(
        """SELECT e.draft_json, e.draft_digest FROM document_jobs j JOIN document_executions e ON e.job_id = j.id
        WHERE j.id = ? AND j.session_id = ? AND j.session_id = ? AND j.requested_by = ? AND j.workspace_id = ?""",
        (target.job_id, target.session_id, session_id, user_id, workspace_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row[1] != target.draft_digest:
        return None
    title = draft_display_title(row[0])
    if title is None:
        return None
    return {"title": title[:200], "version": await draft_version_in_tx(connection, target.job_id)}
