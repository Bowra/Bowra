"""Exercise real migrated domain stores and crash-safe cross-domain publication."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.notification_store import NotificationStore
from bowhead_core.scheduled_store import ScheduledJobStore
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domains import DOMAIN_TABLES, DOMAIN_TARGET_FILES, INFRASTRUCTURE_TABLES
from bowhead_core.subagent_draft_accounting import SubagentDraftCreditAccountant
from core_legacy_database import upgrade as upgrade_legacy
from cryptography.fernet import Fernet


@pytest.fixture(scope="module")
def domain_directory(tmp_path_factory):
    directory = tmp_path_factory.mktemp("domain-stores")
    core = directory / "bowhead.db"
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
        upgrade_legacy(f"sqlite+aiosqlite:///{core}")
        with sqlite3.connect(core) as connection:
            connection.execute("INSERT INTO teams(id,name,status,created_at,team_credit_balance) VALUES ('team_1','Team','active','t',100)")
            connection.execute("INSERT INTO users(id,team_id,email,credit_balance,created_at) VALUES ('u_1','team_1','u1@example.test',100,'t')")
            connection.execute("INSERT INTO users(id,team_id,email,created_at) VALUES ('u_retire','team_1','retire@example.test','t')")
            connection.execute("INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES ('tm_1','u_1','team_1','member','t')")

            connection.execute(
                "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at,revision) VALUES ('ws_1','team_1','Space','active','t','t',1)"
            )
            connection.execute(
                "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents) VALUES ('s_1','ws_1','u_1','Session','t','t','active',0,0,'[]')"
            )
            connection.execute(
                "INSERT INTO terminal_auth_scope_epochs(scope_kind,team_id,scope_ref,epoch,row_version,updated_at) VALUES ('user_account','__global__','u_1',5,5,'t')"
            )
            connection.execute(
                "INSERT INTO terminal_auth_scope_epochs(scope_kind,team_id,scope_ref,epoch,row_version,updated_at) VALUES ('session_visibility','team_1','s_1',7,7,'t')"
            )
            connection.execute("INSERT INTO workspace_members(workspace_id,user_id,joined_at) VALUES ('ws_1','u_1','t')")
        upgrade(f"sqlite+aiosqlite:///{core}")
        yield directory


@pytest.mark.asyncio
async def test_domain_stores_initialize_without_recreating_tables_and_write_owners(domain_directory):
    core = str(domain_directory / "bowhead.db")
    sessions = SessionStore(core)
    canvases = CanvasStore(core)
    artifacts = ArtifactStore(core, LocalBackend(domain_directory / "blobs"))
    scheduled = ScheduledJobStore(core)
    metrics = MetricsLog(core)
    notifications = NotificationStore(core)
    for store in (sessions, canvases, artifacts, scheduled, metrics, notifications):
        await store.initialize()
    assert Path(sessions.db_path).name == "bowhead_execution.db"
    assert Path(artifacts.db_path).name == "bowhead_content.db"
    assert Path(notifications.db_path).name == "bowhead_notifications.db"
    assert await sessions.set_title(session_id="s_1", title="Updated")
    canvas = await canvases.ensure_canvas(session_id="s_1", workspace_id="ws_1", user_id="u_1")
    assert await canvases.get_canvas(canvas.id) is not None
    artifact = await artifacts.put_bytes(content=b"hello", filename="hello.txt", mime="text/plain", created_by="u_1", session_id="s_1")
    assert await artifacts.get(artifact.id) is not None
    await metrics.track("domain.test", user_id="u_1")
    assert len(await metrics.query_recent(name="domain.test")) == 1
    await notifications.write(
        team_id="team_1",
        category="system",
        type="domain.test",
        priority="normal",
        title="Test",
        body=None,
        subject_type=None,
        subject_id=None,
        actor_user_id="u_1",
        action_url=None,
        actions=None,
        metadata=None,
        dedupe_key="domain.test",
        recipients=[],
    )
    with sqlite3.connect(domain_directory / "bowhead_execution.db") as connection:
        assert connection.execute("SELECT artifact_id FROM session_attachments WHERE session_id='s_1'").fetchone() == (artifact.id,)
    with sqlite3.connect(core) as connection:
        assert connection.execute("SELECT count(*) FROM account_owned_artifact_writes").fetchone() == (0,)
    from bowhead_core.artifact_purge import purge_artifacts

    assert await artifacts.soft_delete(artifact.id, user_id="u_1")
    purged = await purge_artifacts(core, artifacts.backend, [artifact.id], user_id="u_1")
    assert purged.rows_removed == 1
    with sqlite3.connect(domain_directory / "bowhead_execution.db") as connection:
        assert connection.execute("SELECT count(*) FROM session_attachments WHERE artifact_id=?", (artifact.id,)).fetchone() == (0,)
    for domain, tables in DOMAIN_TABLES.items():
        with sqlite3.connect(domain_directory / DOMAIN_TARGET_FILES[domain]) as connection:
            actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert actual - INFRASTRUCTURE_TABLES - {"sqlite_sequence"} == set(tables)


@pytest.mark.asyncio
async def test_draft_usage_replay_repairs_interrupted_execution_publication(domain_directory, monkeypatch):
    import bowhead_core.subagent_draft_accounting as accounting

    accountant = SubagentDraftCreditAccountant(domain_directory / "bowhead.db")
    await accountant.initialize()
    identity = {"team_id": "team_1", "owner_user_id": "u_1", "request_key_hash": "domain-draft-request"}
    await accountant.preflight(**identity)
    original = accounting.open_shared_ctx
    fail_publish = True

    @asynccontextmanager
    async def interrupted(path):
        if Path(path).name == "bowhead_execution.db" and fail_publish:
            raise RuntimeError("execution publication interrupted")
        async with original(path) as connection:
            yield connection

    monkeypatch.setattr(accounting, "open_shared_ctx", interrupted)
    usage = {**identity, "draft_id": None, "input_tokens": 700, "output_tokens": 0, "attempts": 1}
    with pytest.raises(RuntimeError, match="publication interrupted"):
        await accountant.settle(**usage)
    with sqlite3.connect(domain_directory / "bowhead.db") as connection:
        state, usage_id = connection.execute("SELECT state,usage_event_id FROM team_subagent_draft_usage").fetchone()
        balance = connection.execute("SELECT credit_balance FROM users WHERE id='u_1'").fetchone()
    assert state == "settled" and usage_id
    fail_publish = False
    assert await accountant.settle_pending() == 1
    assert await accountant.settle_pending() == 0
    with sqlite3.connect(domain_directory / "bowhead_execution.db") as connection:
        assert connection.execute("SELECT id,tokens_input FROM llm_usage_events WHERE id=?", (usage_id,)).fetchall() == [(usage_id, 700)]
    with sqlite3.connect(domain_directory / "bowhead.db") as connection:
        assert connection.execute("SELECT credit_balance FROM users WHERE id='u_1'").fetchone() == balance


@pytest.mark.asyncio
async def test_auth_epoch_authorities_preserve_local_revocation_transactions(domain_directory):
    import aiosqlite
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.terminal_auth_epochs import bump_terminal_auth_epoch
    from bowhead_core.terminal_protocol import AuthFenceScopeKind
    from bowhead_core.terminal_protocol_store import TerminalProtocolStore
    from bowhead_core.terminal_runtime_factory import terminal_auth_resolve_context_values

    core_path = str(domain_directory / "bowhead.db")
    execution_path = str(domain_directory / "bowhead_execution.db")
    store = TerminalProtocolStore(core_path)
    context = terminal_auth_resolve_context_values(team_id="team_1", session_id="s_1", user_id="u_1", resource_ref="t_1")
    vector = await store.ensure_auth_scope_epochs(context)
    epochs = {entry.scope.scope_kind: entry.epoch for entry in vector.entries}
    assert epochs[AuthFenceScopeKind.USER_ACCOUNT] == 5
    assert epochs[AuthFenceScopeKind.SESSION_VISIBILITY] == 7
    with sqlite3.connect(execution_path) as connection:
        assert connection.execute("SELECT DISTINCT scope_kind FROM terminal_auth_scope_epochs").fetchall() == [("session_visibility",)]
    async with open_shared_ctx(core_path) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        await bump_terminal_auth_epoch(connection, scope_kind=AuthFenceScopeKind.USER_ACCOUNT, team_id="__global__", scope_ref="u_1")
        await connection.rollback()
    assert await store.read_auth_fence_vector(context) == vector
    async with open_shared_ctx(core_path) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        await bump_terminal_auth_epoch(connection, scope_kind=AuthFenceScopeKind.USER_ACCOUNT, team_id="__global__", scope_ref="u_1")
        await connection.commit()
        with pytest.raises(aiosqlite.IntegrityError, match="must increase"):
            await connection.execute("UPDATE terminal_authority_scope_epochs SET epoch=1 WHERE scope_kind='user_account'")
        await connection.rollback()
    changed = await store.read_auth_fence_vector(context)
    assert next(entry.epoch for entry in changed.entries if entry.scope.scope_kind is AuthFenceScopeKind.USER_ACCOUNT) == 6
    async with open_shared_ctx(execution_path) as connection:
        await connection.execute("BEGIN IMMEDIATE")
        await bump_terminal_auth_epoch(connection, scope_kind=AuthFenceScopeKind.SESSION_VISIBILITY, team_id="team_1", scope_ref="s_1")
        await connection.rollback()
    assert await store.read_auth_fence_vector(context) == changed


@pytest.mark.asyncio
async def test_retirement_core_fence_survives_deferred_execution_cleanup(domain_directory, monkeypatch):
    import bowhead_core.account_deletion as retirement
    from bowhead_core.user_store import UserStore

    core = domain_directory / "bowhead.db"
    execution = domain_directory / "bowhead_execution.db"
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(str(execution)) as connection:
        await connection.execute(
            "INSERT INTO chat_auth_session_generations(user_id,auth_session_generation_hash,state,created_at) VALUES (?,?,'active','t')",
            ("u_retire", "a" * 64),
        )
        await connection.execute(
            "INSERT INTO scheduled_jobs(id,user_id,workspace_id,name,cron,query,created_at,updated_at) VALUES ('job_retire','u_retire','ws_1','Job','0 * * * *','Query','t','t')"
        )
        await connection.commit()
    original = retirement.diagnostic_connect

    @asynccontextmanager
    async def interrupted(path):
        if Path(path).name == "bowhead_execution.db":
            raise RuntimeError("execution cleanup interrupted")
        async with original(path) as connection:
            yield connection

    monkeypatch.setattr(retirement, "diagnostic_connect", interrupted)
    store = UserStore(core)
    deleted = await retirement.request_account_deletion(store, "u_retire")
    assert deleted.user_id == "u_retire"
    with sqlite3.connect(core) as connection:
        assert connection.execute("SELECT status FROM users WHERE id='u_retire'").fetchone() == ("deleted",)
        assert connection.execute(
            "SELECT epoch FROM terminal_authority_scope_epochs WHERE scope_kind='user_account' AND scope_ref='u_retire'"
        ).fetchone() == (1,)
    with sqlite3.connect(execution) as connection:
        assert connection.execute("SELECT state FROM chat_auth_session_generations WHERE user_id='u_retire'").fetchone() == ("active",)
    monkeypatch.setattr(retirement, "diagnostic_connect", original)
    assert await retirement.adopt_deleted_accounts(store) == 0
    with sqlite3.connect(execution) as connection:
        assert connection.execute("SELECT state FROM chat_auth_session_generations WHERE user_id='u_retire'").fetchone() == ("invalidated",)
        assert connection.execute("SELECT status FROM scheduled_jobs WHERE id='job_retire'").fetchone() == ("paused",)


@pytest.mark.asyncio
async def test_document_freeze_and_approval_repair_across_domains(domain_directory, monkeypatch):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.documents.delivery import DocumentDeliveryStore
    from bowhead_core.documents.executor import WorkerResult
    from bowhead_core.documents.inputs import DocumentInputStore
    from bowhead_core.documents.request import DocumentProposal, DocumentScope, InputRoleProposal, UserQuote
    from bowhead_core.documents.store import DocumentStore
    from bowhead_core.session_store import _insert_chat_turn_user_source_in_tx

    scope = DocumentScope(team_id="team_1", workspace_id="ws_1", user_id="u_1")
    core = domain_directory / "bowhead.db"
    sessions = SessionStore(core)
    text = "Use this template and produce the edited source document and PDF."
    await sessions.record_turn(turn_id="t_domain_document", session_id="s_1", user_text=text)
    artifacts = ArtifactStore(str(core), LocalBackend(domain_directory / "document-blobs"))
    await artifacts.initialize()
    artifact = await artifacts.put_bytes(
        content=b"opaque original", filename="input.pptx", mime="application/octet-stream", created_by="u_1", session_id="s_1"
    )
    await sessions.record_turn_attachments(turn_id="t_domain_document", artifact_ids=[artifact.id])
    source = ChatTurnUserSource(
        turn_id="t_domain_document",
        session_id="s_1",
        user_id="u_1",
        workspace_id="ws_1",
        user_text=text,
        attachment_ids=(artifact.id,),
        attachment_refs=({"fileId": artifact.id},),
        parts=None,
    )
    async with open_shared_ctx(sessions.db_path) as execution:
        await _insert_chat_turn_user_source_in_tx(execution, source=source, created_at="2026-11-03")
        await execution.commit()
    quote = UserQuote(start=0, text=text)
    proposal = DocumentProposal(
        evidence=quote,
        inputs=(InputRoleProposal(artifact_id=artifact.id, role="template", evidence=quote, current_message_position=1),),
        requested_outputs=("source", "pdf"),
    )
    receipt = await DocumentStore(core).submit_from_turn(scope=scope, turn_id=source.turn_id, command_id="domain-command", proposal=proposal)
    frozen = await DocumentInputStore(artifacts).freeze(scope=scope, job_id=receipt.job_id)
    assert len(frozen) == 1
    from bowhead_core.documents.retention import DocumentArtifactInUse

    with pytest.raises(DocumentArtifactInUse):
        await artifacts.soft_delete(frozen[0].artifact_id, user_id="u_1")
    delivery = DocumentDeliveryStore(artifacts)
    owner = await delivery.claim(scope=scope, job_id=receipt.job_id)
    assert owner
    await delivery.transition(scope=scope, job_id=receipt.job_id, owner=owner, state="planning", image_id="sha256:fixture")
    from bowhead_core.documents import usage as usage_module
    from bowhead_router.invocation import PhysicalInvocationSpec
    from bowhead_router.types import TokenUsage

    spec = PhysicalInvocationSpec(
        "domain-provider",
        "domain-model",
        "test.v1",
        "invoke",
        "a" * 64,
        credit_rates={"input": "2", "cached_input": "0.25", "output": "4"},
        usage_family="openai",
    )
    first = usage_module.DocumentModelHook(str(core), scope, receipt.job_id, owner, 1)
    await first.begin(spec)
    with sqlite3.connect(core) as connection:
        original_balance = connection.execute("SELECT credit_balance FROM users WHERE id='u_1'").fetchone()[0]
    deduct = usage_module._deduct_credit_with_stable_id_in_tx

    async def unavailable(*args, **kwargs):
        raise OSError("core settlement unavailable")

    monkeypatch.setattr(usage_module, "_deduct_credit_with_stable_id_in_tx", unavailable)
    with pytest.raises(OSError, match="settlement unavailable"):
        await first.complete(usage=TokenUsage(800, 100, 600), response_digest="b" * 64)
    with sqlite3.connect(core) as connection:
        assert connection.execute("SELECT credit_balance FROM users WHERE id='u_1'").fetchone()[0] == original_balance
    monkeypatch.setattr(usage_module, "_deduct_credit_with_stable_id_in_tx", deduct)
    assert await first.complete(usage=TokenUsage(800, 100, 600), response_digest="b" * 64)
    await delivery.transition(scope=scope, job_id=receipt.job_id, owner=owner, state="rendering")
    await delivery.transition(scope=scope, job_id=receipt.job_id, owner=owner, state="planning", error_code="planning_invalid_response")
    second = usage_module.DocumentModelHook(str(core), scope, receipt.job_id, owner, 2)
    await second.begin(spec)
    snapshot = usage_module._usage_snapshot
    snapshots = 0

    async def fail_acknowledgement(db, job_id):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 3:
            raise OSError("execution acknowledgement interrupted")
        return await snapshot(db, job_id)

    monkeypatch.setattr(usage_module, "_usage_snapshot", fail_acknowledgement)
    with pytest.raises(OSError, match="acknowledgement interrupted"):
        await second.complete(usage=TokenUsage(100, 100, 0), response_digest="c" * 64)
    monkeypatch.setattr(usage_module, "_usage_snapshot", snapshot)
    await usage_module.settle_pending_usage(str(core))
    assert await second.complete(usage=TokenUsage(100, 100, 0), response_digest="c" * 64)
    assert not await second.complete(usage=TokenUsage(101, 100, 0), response_digest="c" * 64)
    with sqlite3.connect(core) as connection:
        assert connection.execute("SELECT credit_balance FROM users WHERE id='u_1'").fetchone()[0] == original_balance - 2
        assert connection.execute("SELECT count(*),sum(delta) FROM credit_ledger WHERE id LIKE ?", (f"document:{receipt.job_id}:%",)).fetchone() == (
            2,
            -2,
        )
        assert connection.execute(
            "SELECT count(*),sum(tokens_cached) FROM credit_model_usage WHERE ledger_id LIKE ?", (f"document:{receipt.job_id}:%",)
        ).fetchone() == (2, 600)
    with sqlite3.connect(sessions.db_path) as connection:
        assert connection.execute("SELECT settled_credits FROM document_executions WHERE job_id=?", (receipt.job_id,)).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM document_usage_receipts WHERE job_id=? AND settled_at IS NULL", (receipt.job_id,)
        ).fetchone() == (0,)
    await delivery.transition(scope=scope, job_id=receipt.job_id, owner=owner, state="rendering")
    result = WorkerResult(
        {"kind": "draft", "manifest": {"semantic_status": "unapproved"}, "preview": {"native_powerpoint_certified": False}},
        {"candidate.pptx": b"source", "candidate.pdf": b"%PDF-source", "page-1.png": b"preview"},
        "sha256:fixture",
    )
    await delivery.save_draft(scope=scope, job_id=receipt.job_id, owner=owner, result=result)
    draft = await delivery.get(scope=scope, job_id=receipt.job_id)
    source_id = draft["draft"]["files"]["candidate.pptx"]["artifact_id"]
    assert await artifacts.get(source_id) is None
    promote = delivery._publish_confirmed_artifacts

    async def interrupted(**kwargs):
        raise RuntimeError("promotion interrupted after approval")

    monkeypatch.setattr(delivery, "_publish_confirmed_artifacts", interrupted)
    with pytest.raises(RuntimeError, match="promotion interrupted"):
        await delivery.confirm(scope=scope, job_id=receipt.job_id, command_id="domain-confirm", draft_digest=draft["draft_digest"])
    assert await artifacts.get(source_id) is None
    monkeypatch.setattr(delivery, "_publish_confirmed_artifacts", promote)
    assert await delivery.repair_confirmed_publications() == 1
    assert await delivery.repair_confirmed_publications() == 0
    published = await delivery.get(scope=scope, job_id=receipt.job_id)
    assert published["state"] == "published"
    assert (await artifacts.get(source_id)).kind == "deliverable"
    assert (await delivery.read_file(scope=scope, job_id=receipt.job_id, name="candidate.pptx", preview=False))[0] == b"source"
    # Repair a crash after content promotion without undoing a user's detach.
    pdf_id = draft["draft"]["files"]["candidate.pdf"]["artifact_id"]
    async with open_shared_ctx(sessions.db_path) as connection:
        await connection.execute("UPDATE session_attachments SET detached_at='2026-11-03' WHERE session_id='s_1' AND artifact_id=?", (source_id,))
        await connection.execute("DELETE FROM session_attachments WHERE session_id='s_1' AND artifact_id=?", (pdf_id,))
        await connection.commit()
    async with open_shared_ctx(core) as connection:
        await connection.execute("UPDATE workspace_members SET status='inactive' WHERE workspace_id='ws_1' AND user_id='u_1'")
        await connection.commit()
    assert await delivery.repair_confirmed_publications() == 0
    async with open_shared_ctx(core) as connection:
        await connection.execute("UPDATE workspace_members SET status='active' WHERE workspace_id='ws_1' AND user_id='u_1'")
        await connection.commit()
    async with open_shared_ctx(artifacts.db_path) as connection:
        await connection.execute("UPDATE artifacts SET filename='user-renamed.pdf' WHERE id=?", (pdf_id,))
        await connection.commit()
    assert await delivery.repair_confirmed_publications() == 1
    assert (await artifacts.get(pdf_id)).filename == "user-renamed.pdf"
    with sqlite3.connect(sessions.db_path) as connection:
        assert connection.execute(
            "SELECT detached_at FROM session_attachments WHERE session_id='s_1' AND artifact_id=?", (source_id,)
        ).fetchone() == ("2026-11-03",)
        assert connection.execute("SELECT detached_at FROM session_attachments WHERE session_id='s_1' AND artifact_id=?", (pdf_id,)).fetchone() == (
            None,
        )
    assert await delivery.repair_confirmed_publications() == 0
    assert await artifacts.soft_delete(source_id, user_id="u_1")
    async with open_shared_ctx(sessions.db_path) as connection:
        await connection.execute("DELETE FROM session_attachments WHERE session_id='s_1' AND artifact_id=?", (pdf_id,))
        await connection.commit()
    assert await delivery.repair_confirmed_publications() == 0
    assert await artifacts.get(source_id) is None


@pytest.mark.asyncio
async def test_core_business_store_accepts_resolved_execution_peer_path(domain_directory):
    from bowhead_core.agent_assignment_store import AgentAssignmentStore
    from bowhead_core.registered_fact_store import RegisteredFactStore

    peer_path = SessionStore(domain_directory / "bowhead.db").db_path
    assignments = AgentAssignmentStore(peer_path)
    await assignments.initialize()
    facts = RegisteredFactStore(peer_path)
    saved = await facts.upsert(
        workspace_id="ws_1",
        fact_key="domain.peer.path",
        fact_type="text",
        value_json='"persisted in core"',
        source="test",
        user_id="u_1",
        source_session_id="s_1",
    )
    loaded = await RegisteredFactStore(domain_directory / "bowhead.db").get(workspace_id="ws_1", fact_key="domain.peer.path", user_id="u_1")
    assert loaded is not None and loaded.fact_id == saved.fact_id
    assert loaded.value_json == '"persisted in core"'


@pytest.mark.asyncio
async def test_draft_maintenance_does_not_resurrect_pruned_settled_usage(domain_directory, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from bowhead_core.user_store import UserStore

    monkeypatch.setenv("BOWHEAD_LLM_USAGE_RETENTION_DAYS", "30")
    historical = datetime.now(UTC) - timedelta(days=180)
    core = domain_directory / "bowhead.db"
    accountant = SubagentDraftCreditAccountant(core, clock=SimpleNamespace(now=lambda: historical))
    identity = {"team_id": "team_1", "owner_user_id": "u_1", "request_key_hash": "historical-draft-request"}
    await accountant.preflight(**identity)
    await accountant.settle(**identity, draft_id=None, input_tokens=700, output_tokens=0, attempts=1)
    users = UserStore(core)
    assert await users.rollup_llm_usage_daily() > 0
    assert await users.prune_llm_usage_events(older_than=(datetime.now(UTC) - timedelta(days=30)).isoformat()) == 1
    with sqlite3.connect(core) as connection:
        balance = connection.execute("SELECT credit_balance FROM users WHERE id='u_1'").fetchone()
        usage_id = connection.execute(
            "SELECT usage_event_id FROM team_subagent_draft_usage WHERE request_key_hash=?", (identity["request_key_hash"],)
        ).fetchone()[0]
    assert await SubagentDraftCreditAccountant(core).settle_pending() == 0
    with sqlite3.connect(domain_directory / "bowhead_execution.db") as connection:
        assert connection.execute("SELECT count(*) FROM llm_usage_events WHERE id=?", (usage_id,)).fetchone() == (0,)
    with sqlite3.connect(core) as connection:
        assert connection.execute("SELECT credit_balance FROM users WHERE id='u_1'").fetchone() == balance
        assert connection.execute(
            "SELECT sum(calls) FROM llm_usage_daily WHERE day=? AND reason='subagent_draft_generation'", (historical.date().isoformat(),)
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_reference_skill_scope_accepts_domain_peers_and_rejects_other_installations(domain_directory, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_admission import admit_reference_start_in_connection
    from bowhead_core.chat_reference_scope import ReferenceRequestScope
    from bowhead_core.chat_turn_source import ChatTurnUserSource

    core = domain_directory / "bowhead.db"
    bindings = AgentExecutionBindingStore(core)
    source = ChatTurnUserSource(
        turn_id="t_reference_scope",
        session_id="s_1",
        user_id="u_1",
        workspace_id="ws_1",
        user_text="Read this method",
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
    )
    scope = ReferenceRequestScope(team_id="team_1", user_id="u_1", workspace_id="ws_1", session_id="s_1")
    admission = SimpleNamespace(
        orchestration_contract=None,
        has_attachments=False,
        automatic_receiver=None,
        capability=None,
        input_document=SimpleNamespace(nodes=[]),
        skill_reads=SimpleNamespace(scope=scope, items=()),
        skill_sources=SimpleNamespace(db_path=core),
    )
    admission.snapshot = lambda: admission

    def accepted(*args):
        raise RuntimeError("skill installation boundary accepted")

    monkeypatch.setattr("bowhead_core.chat_reference_admission.reference_start_receiver", accepted)
    async with open_shared_ctx(bindings.db_path) as connection:
        with pytest.raises(RuntimeError, match="installation boundary accepted"):
            await admit_reference_start_in_connection(
                connection,
                admission=admission,
                source=source,
                team_id="team_1",
                bindings=bindings,
                expires_at="2099-01-01",
                requested_session_id="s_1",
            )
        admission.skill_sources.db_path = tmp_path / "other-installation" / "bowhead.db"
        with pytest.raises(ValueError, match="reference_skill_preparation_scope_mismatch"):
            await admit_reference_start_in_connection(
                connection,
                admission=admission,
                source=source,
                team_id="team_1",
                bindings=bindings,
                expires_at="2099-01-01",
                requested_session_id="s_1",
            )


@pytest.mark.parametrize("staged", [False, True])
async def test_new_artifact_scope_is_inserted_without_excluding_history_reader(domain_directory, staged, monkeypatch):
    from bowhead_core._db import open_shared_ctx

    core = str(domain_directory / "bowhead.db")
    artifacts = ArtifactStore(core, LocalBackend(domain_directory / "concurrent-blobs"))
    await artifacts.initialize()
    from contextlib import closing

    from bowhead_core.sqlite_domain_runtime import connect_sync

    from bowhead_core import artifact_store

    resolve = artifact_store._resolve_write_workspace_id

    async def protected_scope(db, **options):
        workspace = await resolve(db, **options)
        with closing(connect_sync(SessionStore(core).db_path, timeout=0)) as retire:
            with pytest.raises(sqlite3.OperationalError, match="authority lease"):
                retire.execute("UPDATE sessions SET deleted_at='retiring' WHERE id='s_1'")
            retire.rollback()
        return workspace

    monkeypatch.setattr(artifact_store, "_resolve_write_workspace_id", protected_scope)
    async with open_shared_ctx(SessionStore(core).db_path) as reader:
        await reader.execute("BEGIN")
        async with reader.execute("SELECT id FROM artifacts LIMIT 1") as cursor:
            await cursor.fetchall()
        if staged:
            output = await artifacts.stage_export_bytes(
                content=b"staged concurrent output",
                filename="output.txt",
                mime="text/plain",
                canvas_id=None,
                created_by="u_1",
                session_id="s_1",
                producer_kind="canvas_export",
            )
            await artifacts.commit_staged_artifacts((output,))
            artifact = output.artifact
        else:
            artifact = await artifacts.put_bytes(
                content=b"concurrent output", filename="output.txt", mime="text/plain", created_by="u_1", session_id="s_1"
            )
        assert artifact.workspace_id == "ws_1"
        await reader.rollback()
    assert (await artifacts.get(artifact.id)).workspace_id == "ws_1"
