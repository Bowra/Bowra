"""Real migrated SQLite + canonical SessionStore source + ArtifactStore inputs.

No mock admission, fake worker or generation-success assertion. Mutations remove
source/ACL guards and fail receipt/link writes to prove the actual transaction.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config
from bowhead_core._db import open_shared_ctx
from bowhead_core.artifact_store import Artifact, ArtifactStore, LocalBackend
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.documents.request import DocumentProposal, DocumentRequestError, DocumentScope, InputRoleProposal, UserQuote
from bowhead_core.documents.store import DocumentStore
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx
from bowhead_core.terminal_protocol_store import TerminalProtocolStore

from bowhead_core import migrations

pytestmark = pytest.mark.integration
TEXT = "这是模板，保持格式替换本季度文字，并给我源文档和PDF。"
SCOPE = DocumentScope(team_id="team_doc", workspace_id="ws_doc", user_id="u_doc")


@dataclass
class Context:
    path: Path
    store: DocumentStore
    sessions: SessionStore
    artifacts: ArtifactStore
    artifact: Artifact
    source: ChatTurnUserSource
    proposal: DocumentProposal

    async def submit(self, *, command_id="command_one", scope=SCOPE, proposal=None, turn_id="t_doc"):
        return await self.store.submit_from_turn(scope=scope, turn_id=turn_id, command_id=command_id, proposal=proposal or self.proposal)


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("document_schema") / "core.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    return path


@pytest.fixture
async def ctx(tmp_path, monkeypatch, migrated_db) -> Context:
    path = tmp_path / "core.db"
    # A complete real Alembic database, isolated per test; not hand-built DDL.
    with sqlite3.connect(migrated_db) as source_db, sqlite3.connect(path) as target_db:
        source_db.backup(target_db)
    async with open_shared_ctx(str(path)) as db:
        await db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_doc','Document fixture','2026-09-06')")
        for uid in ("u_doc", "u_other"):
            await db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES (?,'team_doc',?,'2026-09-06')", (uid, f"{uid}@example.test"))
            await db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES (?,?,'team_doc','member','2026-09-06')", (f"om_{uid}", uid)
            )
        for wid in ("ws_doc", "ws_other"):
            await db.execute(
                "INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES (?,'team_doc',?,'2026-09-06','2026-09-06')", (wid, wid)
            )
            for uid in ("u_doc", "u_other"):
                await db.execute("INSERT INTO workspace_members(workspace_id,user_id,joined_at) VALUES (?,?,'2026-09-06')", (wid, uid))
        await db.commit()
    sessions = SessionStore(path)
    await sessions.ensure_session(session_id="s_doc", workspace_id="ws_doc", user_id="u_doc")
    await sessions.record_turn(turn_id="t_doc", session_id="s_doc", user_text=TEXT)
    artifacts = ArtifactStore(db_path=path, backend=LocalBackend(tmp_path / "blobs"))
    artifact = await artifacts.put_bytes(
        content=b"synthetic metadata-only input", filename="fixture.pptx", mime="application/octet-stream", created_by="u_doc"
    )
    await sessions.record_turn_attachments(turn_id="t_doc", artifact_ids=[artifact.id])
    await sessions.pin_session_attachments(session_id="s_doc", artifact_ids=[artifact.id])
    source = ChatTurnUserSource(
        turn_id="t_doc",
        session_id="s_doc",
        user_id="u_doc",
        workspace_id="ws_doc",
        user_text=TEXT,
        attachment_ids=(artifact.id,),
        attachment_refs=({"fileId": artifact.id},),
        parts=None,
    )
    await _save_source(path, source)
    quote = UserQuote(start=0, text=TEXT)
    proposal = DocumentProposal(
        evidence=quote,
        inputs=(InputRoleProposal(artifact_id=artifact.id, role="template", evidence=quote, current_message_position=1),),
        requested_outputs=("source", "pdf"),
    )
    return Context(path, DocumentStore(path), sessions, artifacts, artifact, source, proposal)


async def _save_source(path, source) -> None:
    async with open_shared_ctx(str(path)) as db:
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at="2026-09-06")
        await db.commit()


async def _sql(ctx, sql, args=()):
    async with open_shared_ctx(str(ctx.path)) as db:
        cur = await db.execute(sql, args)
        rows = await cur.fetchall()
        await db.commit()
        return rows


async def _assert_empty(ctx):
    for table in ("document_jobs", "document_command_receipts", "document_job_links", "document_job_events"):
        assert (await _sql(ctx, f"SELECT count(*) FROM {table}"))[0][0] == 0


async def test_committed_proposal_is_discoverable_without_reading_or_mutating_input(ctx, monkeypatch) -> None:
    before = await _sql(ctx, "SELECT * FROM artifacts")
    read = AsyncMock(side_effect=AssertionError("admission must not read the blob"))
    monkeypatch.setattr(ctx.artifacts.backend, "read", read)
    monkeypatch.setattr(ctx.artifacts.backend, "read_bounded", read)
    receipt = await ctx.submit()
    fresh = DocumentStore(ctx.path)
    jobs = await fresh.list_for_session(scope=SCOPE, session_id="s_doc")
    assert len(jobs) == 1 and jobs[0].id == receipt.job_id and receipt.job_id.startswith("djob_")
    assert jobs[0].status == "queued" and jobs[0].phase == "resolving"
    assert jobs[0].proposal == ctx.proposal and len(jobs[0].user_source_sha256) == 64
    assert await fresh.get(scope=SCOPE, job_id=receipt.job_id) == jobs[0]
    assert await _sql(ctx, "SELECT * FROM artifacts") == before
    assert await _sql(ctx, "SELECT type,payload_json FROM document_job_events") == [("job.created", "{}")]
    read.assert_not_awaited()


async def test_concurrent_retries_and_lost_response_return_one_durable_receipt(ctx, monkeypatch) -> None:
    results = await asyncio.gather(*(ctx.submit() for _ in range(4)))
    assert results == [results[0]] * 4
    await _sql(ctx, "UPDATE turns SET status='completed' WHERE id='t_doc'")
    ctx.store = DocumentStore(ctx.path)
    assert await ctx.submit() == results[0]
    for table in ("document_jobs", "document_command_receipts", "document_job_links", "document_job_events"):
        assert (await _sql(ctx, f"SELECT count(*) FROM {table}"))[0][0] == 1


async def test_reused_command_with_different_payload_conflicts_without_changes(ctx) -> None:
    receipt = await ctx.submit()
    before = await _sql(ctx, "SELECT * FROM document_jobs")
    with pytest.raises(DocumentRequestError, match="command_conflict"):
        await ctx.submit(proposal=ctx.proposal.model_copy(update={"requested_outputs": ("source",)}))
    with pytest.raises(DocumentRequestError, match="command_conflict"):
        await ctx.store.cancel(scope=SCOPE, job_id=receipt.job_id, command_id="command_one", expected_proposal_revision=1)
    assert await _sql(ctx, "SELECT * FROM document_jobs") == before


@pytest.mark.parametrize("table", ["document_command_receipts", "document_job_links", "document_job_events"])
async def test_admission_failure_rolls_back_all_domain_rows(ctx, table) -> None:
    await _sql(ctx, f"CREATE TRIGGER fault_document BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'controlled fault'); END")
    with pytest.raises(aiosqlite.IntegrityError, match="controlled fault"):
        await ctx.submit()
    await _assert_empty(ctx)
    await _sql(ctx, "DROP TRIGGER fault_document")
    assert (await ctx.submit()).job_id


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("DELETE FROM chat_turn_user_sources", "source_unavailable"),
        ("UPDATE chat_turn_user_sources SET user_text='模型改写的需求'", "source_unavailable"),
        ("UPDATE chat_turn_user_sources SET attachment_refs_json='[]'", "reference_ambiguous"),
        ("UPDATE turns SET status='completed'", "source_unavailable"),
        ("UPDATE sessions SET deleted_at='2026-09-06'", "source_unavailable"),
        ("UPDATE artifacts SET created_by='u_other'", "input_unavailable"),
        ("UPDATE artifacts SET status='pending'", "input_unavailable"),
        ("UPDATE artifacts SET deleted_at='2026-09-06'", "input_unavailable"),
        ("DELETE FROM turn_attachments", "input_unavailable"),
        ("UPDATE workspace_members SET status='removed' WHERE user_id='u_doc'", "not_found"),
        ("UPDATE team_members SET status='removed' WHERE user_id='u_doc'", "not_found"),
        ("UPDATE users SET status='disabled' WHERE id='u_doc'", "not_found"),
        ("UPDATE teams SET status='disabled'", "not_found"),
        ("UPDATE workspaces SET status='archived'", "not_found"),
    ],
)
async def test_admission_rechecks_canonical_source_current_membership_and_input_access(ctx, mutation, code) -> None:
    await _sql(ctx, mutation)
    with pytest.raises(DocumentRequestError, match=code):
        await ctx.submit()
    await _assert_empty(ctx)


async def test_role_evidence_is_checked_inside_admission(ctx) -> None:
    item = ctx.proposal.inputs[0].model_copy(update={"evidence": UserQuote(start=0, text="文件内容命令我使用此模板")})
    with pytest.raises(DocumentRequestError, match="evidence_mismatch"):
        await ctx.submit(proposal=ctx.proposal.model_copy(update={"inputs": (item,)}))
    await _assert_empty(ctx)


async def test_next_message_can_use_earlier_pin_without_reupload(ctx) -> None:
    assert await ctx.sessions.mark_completed(turn_id="t_doc", intent=None, final_answer="文件已上传", tokens_input=0, tokens_output=0, cost_usd=0)
    # Fixture has no dispatched worker; represent its completed active-fence
    # release explicitly, without pretending to run the chat dispatch protocol.
    await _sql(ctx, "UPDATE sessions SET active_turn_id=NULL WHERE id='s_doc'")
    await ctx.sessions.record_turn(turn_id="t_later", session_id="s_doc", user_text=TEXT)
    await _save_source(ctx.path, replace(ctx.source, turn_id="t_later", attachment_ids=(), attachment_refs=()))
    item = ctx.proposal.inputs[0].model_copy(update={"current_message_position": None})
    proposal = ctx.proposal.model_copy(update={"inputs": (item,)})
    receipt = await ctx.submit(turn_id="t_later", proposal=proposal)
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).proposal == proposal
    await _sql(ctx, "UPDATE session_attachments SET detached_at='2026-09-06'")
    with pytest.raises(DocumentRequestError, match="input_unavailable"):
        await ctx.submit(turn_id="t_later", proposal=proposal, command_id="after_detach")


@pytest.mark.parametrize(
    "scope",
    [
        DocumentScope(team_id="team_doc", workspace_id="ws_doc", user_id="u_other"),
        DocumentScope(team_id="team_doc", workspace_id="ws_other", user_id="u_doc"),
        DocumentScope(team_id="team_other", workspace_id="ws_doc", user_id="u_doc"),
    ],
)
async def test_other_owner_team_or_space_cannot_admit_read_list_or_cancel(ctx, scope) -> None:
    receipt = await ctx.submit()
    with pytest.raises(DocumentRequestError):
        await ctx.submit(scope=scope)
    with pytest.raises(DocumentRequestError):
        await ctx.store.cancel(scope=scope, job_id=receipt.job_id, command_id="cancel", expected_proposal_revision=1)
    if scope.team_id == "team_doc":
        assert await ctx.store.get(scope=scope, job_id=receipt.job_id) is None
        assert await ctx.store.list_for_session(scope=scope, session_id="s_doc") == []
    else:
        with pytest.raises(DocumentRequestError):
            await ctx.store.get(scope=scope, job_id=receipt.job_id)


async def test_membership_revocation_also_blocks_idempotent_replay(ctx) -> None:
    await ctx.submit()
    await _sql(ctx, "UPDATE team_members SET status='removed' WHERE user_id='u_doc'")
    with pytest.raises(DocumentRequestError, match="not_found"):
        await ctx.submit()


@pytest.mark.parametrize(
    "scope",
    [
        DocumentScope(team_id="team_doc", workspace_id="ws_other", user_id="u_doc"),
        DocumentScope(team_id="team_doc", workspace_id="ws_doc", user_id="u_other"),
    ],
)
async def test_same_command_key_is_independent_for_another_authorized_owner_or_space(ctx, scope) -> None:
    original = await ctx.submit()
    await ctx.sessions.ensure_session(session_id="s_other", workspace_id=scope.workspace_id, user_id=scope.user_id)
    await ctx.sessions.record_turn(turn_id="t_other", session_id="s_other", user_text=TEXT)
    artifact = await ctx.artifacts.put_bytes(
        content=b"second synthetic input", filename="other.pptx", mime="application/octet-stream", created_by=scope.user_id
    )
    await ctx.sessions.record_turn_attachments(turn_id="t_other", artifact_ids=[artifact.id])
    source = replace(
        ctx.source,
        turn_id="t_other",
        session_id="s_other",
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        attachment_ids=(artifact.id,),
        attachment_refs=({"fileId": artifact.id},),
    )
    await _save_source(ctx.path, source)
    proposal = ctx.proposal.model_copy(update={"inputs": (ctx.proposal.inputs[0].model_copy(update={"artifact_id": artifact.id}),)})
    other = await ctx.submit(scope=scope, turn_id="t_other", proposal=proposal)
    assert other.job_id != original.job_id
    assert await ctx.submit(scope=scope, turn_id="t_other", proposal=proposal) == other
    assert await ctx.store.get(scope=scope, job_id=original.job_id) is None


async def test_concurrent_cancel_only_appends_one_transition(ctx) -> None:
    receipt = await ctx.submit()
    results = await asyncio.gather(
        *(ctx.store.cancel(scope=SCOPE, job_id=receipt.job_id, command_id="cancel", expected_proposal_revision=1) for _ in range(4))
    )
    assert results == [receipt] * 4
    assert await _sql(ctx, "SELECT count(*) FROM document_job_events WHERE type='job.cancelled'") == [(1,)]


async def test_cancel_is_atomic_and_idempotent(ctx, monkeypatch) -> None:
    receipt = await ctx.submit()
    await _sql(
        ctx,
        "CREATE TRIGGER fault_cancel BEFORE INSERT ON document_command_receipts WHEN NEW.command_kind='cancel' BEGIN SELECT RAISE(ABORT,'controlled fault'); END",
    )
    args = {"scope": SCOPE, "job_id": receipt.job_id, "command_id": "cancel", "expected_proposal_revision": 1}
    with pytest.raises(aiosqlite.IntegrityError, match="controlled fault"):
        await ctx.store.cancel(**args)
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).status == "queued"
    assert len(await _sql(ctx, "SELECT * FROM document_job_events")) == 1
    await _sql(ctx, "DROP TRIGGER fault_cancel")
    assert await ctx.store.cancel(**args) == await ctx.store.cancel(**args) == receipt
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).status == "cancelled"
    events = await _sql(ctx, "SELECT type,payload_json FROM document_job_events ORDER BY event_no")
    assert [row[0] for row in events] == ["job.created", "job.cancelled"]
    assert json.loads(events[1][1]) == {"reason": "user_requested"}
    assert await ctx.submit() == receipt


async def test_stale_cancel_does_not_change_the_request(ctx) -> None:
    receipt = await ctx.submit()
    with pytest.raises(DocumentRequestError, match="revision_conflict"):
        await ctx.store.cancel(scope=SCOPE, job_id=receipt.job_id, command_id="cancel", expected_proposal_revision=2)
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).status == "queued"


@pytest.mark.parametrize("entry", ["user", "cascade"])
async def test_real_session_deletion_cancels_and_retains_request_and_events(ctx, entry) -> None:
    receipt = await ctx.submit()
    lifecycle = SessionLifecycleCoordinator(ctx.sessions, TerminalProtocolStore(ctx.path))
    result = await lifecycle.soft_delete(
        session_id="s_doc", actor_audit_ref="u_doc", request_id="delete", expected_user_id="u_doc" if entry == "user" else None
    )
    assert result.changed
    assert await _sql(ctx, "SELECT status,cancel_reason FROM document_jobs") == [("cancelled", "session_deleted")]
    assert len(await _sql(ctx, "SELECT * FROM document_job_events")) == 2
    assert len(await _sql(ctx, "SELECT * FROM document_job_links")) == 1
    assert await ctx.store.get(scope=SCOPE, job_id=receipt.job_id) is None
    assert await ctx.store.list_for_session(scope=SCOPE, session_id="s_doc") == []
    assert (await lifecycle.revive(session_id="s_doc", actor_audit_ref="u_doc", request_id="revive", expected_user_id="u_doc")).changed
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).status == "cancelled"


async def test_turn_completion_and_space_archive_do_not_cancel_existing_proposal(ctx) -> None:
    receipt = await ctx.submit()
    await _sql(ctx, "UPDATE turns SET status='completed'")
    await _sql(ctx, "UPDATE workspaces SET status='archived'")
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).status == "queued"
    assert await ctx.submit() == receipt
    assert await ctx.store.cancel(scope=SCOPE, job_id=receipt.job_id, command_id="cancel", expected_proposal_revision=1) == receipt


async def test_failed_job_cancel_event_rolls_back_session_visibility_too(ctx) -> None:
    receipt = await ctx.submit()
    await _sql(
        ctx,
        "CREATE TRIGGER fault_cancel_event BEFORE INSERT ON document_job_events WHEN NEW.type='job.cancelled' BEGIN SELECT RAISE(ABORT,'controlled fault'); END",
    )
    lifecycle = SessionLifecycleCoordinator(ctx.sessions, TerminalProtocolStore(ctx.path))
    with pytest.raises(aiosqlite.IntegrityError, match="controlled fault"):
        await lifecycle.soft_delete(session_id="s_doc", actor_audit_ref="u_doc", request_id="delete", expected_user_id="u_doc")
    assert await _sql(ctx, "SELECT deleted_at FROM sessions WHERE id='s_doc'") == [(None,)]
    assert (await ctx.store.get(scope=SCOPE, job_id=receipt.job_id)).status == "queued"


async def test_bounded_read_projection_has_no_side_effects_and_includes_cancelled_jobs(ctx) -> None:
    receipts = [await ctx.submit(command_id=f"command_{i}") for i in range(3)]
    await ctx.store.cancel(scope=SCOPE, job_id=receipts[1].job_id, command_id="cancel", expected_proposal_revision=1)
    before = await _sql(ctx, "SELECT * FROM document_job_events")
    found = []
    while page := await ctx.store.list_for_session(scope=SCOPE, session_id="s_doc", after_id=found[-1].id if found else "", limit=1):
        found.extend(page)
        assert len(found) <= 3
    assert {job.id for job in found} == {receipt.job_id for receipt in receipts}
    assert sum(job.status == "cancelled" for job in found) == 1
    assert await _sql(ctx, "SELECT * FROM document_job_events") == before


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE document_jobs SET request_json='{}'",
        "UPDATE document_jobs SET user_source_sha256=printf('%064d',0)",
        "UPDATE document_jobs SET requested_by='u_other'",
        "UPDATE document_jobs SET session_id='s_other'",
        "UPDATE document_jobs SET idempotency_key='new'",
        "DELETE FROM document_jobs",
        "UPDATE document_command_receipts SET result_json='{}'",
        "DELETE FROM document_job_links",
        "UPDATE document_job_events SET payload_json='{}'",
        "INSERT OR REPLACE INTO document_jobs SELECT * FROM document_jobs",
        "INSERT OR REPLACE INTO document_command_receipts SELECT * FROM document_command_receipts",
        "INSERT OR REPLACE INTO document_job_links SELECT * FROM document_job_links",
        "INSERT OR REPLACE INTO document_job_events SELECT * FROM document_job_events",
    ],
)
async def test_committed_identity_and_audit_cannot_be_rewritten(ctx, statement) -> None:
    await ctx.submit()
    before = await _sql(ctx, "SELECT * FROM document_jobs")
    with pytest.raises(aiosqlite.IntegrityError):
        await _sql(ctx, statement)
    assert await _sql(ctx, "SELECT * FROM document_jobs") == before
