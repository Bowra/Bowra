"""Original-source and publication checks shared by file reads and reuse."""

from bowhead_core._db import open_shared_ctx


async def published_file_identity(path, *, artifact_id: str, user_id: str):
    async with (
        open_shared_ctx(path) as db,
        db.execute(
            """SELECT f.job_id,f.name FROM document_draft_files f JOIN document_executions e ON e.job_id=f.job_id
            JOIN document_jobs j ON j.id=f.job_id
            JOIN document_draft_files parent ON parent.job_id=f.job_id AND parent.name='candidate.pptx'
            JOIN artifacts source ON source.id=parent.artifact_id
            WHERE f.artifact_id=? AND j.requested_by=? AND e.state='published'
            AND source.deleted_at IS NULL AND source.status='ready'
            AND NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=source.id)""",
            (artifact_id, user_id),
        ) as cursor,
    ):
        return await cursor.fetchone()
