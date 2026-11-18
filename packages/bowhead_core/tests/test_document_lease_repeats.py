"""Document pipeline writes repeat when a running Turn holds the authority lease.

The execution file's document tables are fenced, so a write failed at once with "SQLite
authority lease is busy" and the document job failed or stalled (census T6,
docs/sprints/fenced-write-census-2026-11-17). Each transaction now runs through
``run_sqlite_authority_write`` and repeats as a whole after a full rollback.
"""

import re
import sqlite3

import pytest
from bowhead_core.documents.delivery import DocumentDeliveryStore
from bowhead_core.documents.executor import WorkerResult
from bowhead_core.documents.inputs import DocumentInputStore
from bowhead_core.sqlite_diagnostics import DiagnosticCursor
from bowhead_core.sqlite_domains import domain_database_path
from test_document_store import SCOPE
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db

from bowhead_core import sqlite_domain_runtime

pytestmark = pytest.mark.integration


def _lose_lease(monkeypatch: pytest.MonkeyPatch, table: str, times: int) -> list[int]:
    """Fail the next ``times`` writes to ``table`` with the runtime's marked lease conflict."""
    write = re.compile(rf"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b.*\b{table}\b", re.IGNORECASE | re.DOTALL)
    attempts: list[int] = []
    original = DiagnosticCursor.execute

    def execute(self, sql, parameters=(), /):
        if write.search(sql):
            attempts.append(len(attempts) + 1)
            if len(attempts) <= times:
                raise sqlite_domain_runtime._busy()
        return original(self, sql, parameters)

    monkeypatch.setattr(DiagnosticCursor, "execute", execute)
    return attempts


def _count(ctx, sql: str, *params) -> int:
    with sqlite3.connect(domain_database_path(ctx.path, "execution")) as db:
        return db.execute(sql, params).fetchone()[0]


async def test_submit_claim_transition_and_draft_repeat_after_a_lost_lease(ctx, monkeypatch):
    submitted = _lose_lease(monkeypatch, "document_jobs", 1)
    receipt = await ctx.submit()
    assert submitted[:2] == [1, 2]
    assert _count(ctx, "SELECT count(*) FROM document_jobs WHERE id=?", receipt.job_id) == 1
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    delivery = DocumentDeliveryStore(ctx.artifacts)

    claimed = _lose_lease(monkeypatch, "document_executions", 1)
    owner = await delivery.claim(scope=SCOPE, job_id=receipt.job_id)
    assert owner is not None and claimed == [1, 2]
    assert _count(ctx, "SELECT count(*) FROM document_executions WHERE job_id=? AND owner=?", receipt.job_id, owner) == 1

    moved = _lose_lease(monkeypatch, "document_executions", 1)
    await delivery.transition(scope=SCOPE, job_id=receipt.job_id, owner=owner, state="planning", image_id="sha256:test")
    assert moved == [1, 2]
    await delivery.transition(scope=SCOPE, job_id=receipt.job_id, owner=owner, state="rendering")

    drafted = _lose_lease(monkeypatch, "document_draft_files", 1)
    result = WorkerResult(
        {"kind": "draft", "manifest": {"semantic_status": "unapproved"}, "preview": {"native_powerpoint_certified": False}},
        {"candidate.pptx": b"external worker candidate", "candidate.pdf": b"%PDF-external worker fixture", "page-1.png": b"external preview fixture"},
        "sha256:test",
    )
    await delivery.save_draft(scope=SCOPE, job_id=receipt.job_id, owner=owner, result=result)

    assert drafted[:2] == [1, 2]
    draft = await delivery.get(scope=SCOPE, job_id=receipt.job_id)
    assert draft["state"] == "review"
    assert _count(ctx, "SELECT count(*) FROM document_draft_files WHERE job_id=?", receipt.job_id) == 3
    assert _count(ctx, "SELECT count(*) FROM document_draft_files WHERE job_id=? AND verified_at IS NULL", receipt.job_id) == 0

    confirmed = _lose_lease(monkeypatch, "document_confirmations", 1)
    published = await delivery.confirm(scope=SCOPE, job_id=receipt.job_id, command_id="confirm", draft_digest=draft["draft_digest"])
    assert published["state"] == "published" and confirmed == [1, 2]
    assert _count(ctx, "SELECT count(*) FROM document_confirmations WHERE job_id=?", receipt.job_id) == 1


async def test_a_failed_terminalization_repeats_after_a_lost_lease(ctx, monkeypatch):
    receipt = await ctx.submit()
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    delivery = DocumentDeliveryStore(ctx.artifacts)
    owner = await delivery.claim(scope=SCOPE, job_id=receipt.job_id)

    attempts = _lose_lease(monkeypatch, "document_executions", 1)
    await delivery.fail(job_id=receipt.job_id, owner=owner, code="document_execution_failed")

    assert attempts == [1, 2]
    assert (await delivery.get(scope=SCOPE, job_id=receipt.job_id))["state"] == "failed"
