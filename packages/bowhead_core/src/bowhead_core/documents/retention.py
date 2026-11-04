"""Storage retention is independent of feature flags and never grants access."""

from __future__ import annotations

import aiosqlite

from bowhead_core.sqlite_readable_schema import readable_table_exists

INTERNAL_PRODUCER = "template_document_internal_v1"
ACTIVE_REF_SQL = "EXISTS(SELECT 1 FROM document_artifact_refs dr WHERE dr.artifact_id=artifacts.id AND dr.released_at IS NULL)"


async def active_ref_sql(db: aiosqlite.Connection) -> str:
    """Pre-migration legacy stores have no document writers or references."""
    exists = await readable_table_exists(db, "document_artifact_refs")
    if not exists:
        return "0"
    retained = ACTIVE_REF_SQL
    if await readable_table_exists(db, "document_revision_inputs"):
        retained = (
            "("
            + retained
            + """ OR EXISTS(SELECT 1 FROM document_revision_inputs ri JOIN document_jobs rj ON rj.id=ri.job_id
            WHERE ri.artifact_id=artifacts.id AND rj.status='queued'))"""
        )
    if not await readable_table_exists(db, "document_draft_files"):
        return retained
    return (
        "("
        + retained
        + """ OR EXISTS(
        SELECT 1 FROM document_draft_files f JOIN document_executions e ON e.job_id=f.job_id
        WHERE f.artifact_id=artifacts.id AND (e.state IN ('preparing','planning','rendering','review')
          OR (e.state='published' AND f.name LIKE 'page-%.png' AND EXISTS(
            SELECT 1 FROM document_draft_files p JOIN artifacts a ON a.id=p.artifact_id
            WHERE p.job_id=f.job_id AND p.name=CASE (SELECT profile FROM document_dispatches WHERE job_id=f.job_id) WHEN 'docx-script-worker-v1' THEN 'candidate.docx' ELSE 'candidate.pptx' END)))))"""
    )


class DocumentArtifactInUse(ValueError):
    def __init__(self) -> None:
        super().__init__("artifact_in_use")
