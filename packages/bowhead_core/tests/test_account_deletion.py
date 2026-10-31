"""Account identity isolation, exact deadlines, and migrated historical users."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
import test_document_store as document_fixtures
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from bowhead_core.account_deletion import AccountDeletionError, adopt_deleted_accounts, request_account_deletion
from bowhead_core.user_store import UserStore

from bowhead_core import migrations

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 16, tzinfo=UTC)
document_ctx = document_fixtures.ctx
migrated_db = document_fixtures.migrated_db


@pytest.fixture(scope="session")
def account_schema(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("account-schema") / "core.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    return path


@pytest.fixture
def store(tmp_path: Path, account_schema: Path) -> UserStore:
    path = tmp_path / "core.db"
    with sqlite3.connect(account_schema) as source, sqlite3.connect(path) as target:
        source.backup(target)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active',?)", (NOW.isoformat(),))
        db.execute(
            "INSERT INTO users(id,team_id,email,status,created_at,last_active_at,password_hash) "
            "VALUES ('usr_old','team_1','member@example.com','active','2020-01-01','2020-01-01','old-hash')"
        )
    return UserStore(str(path))


@pytest.fixture
async def service(store: UserStore, tmp_path: Path):
    from unittest.mock import AsyncMock

    from bowhead_core.account_deletion_ledger import AccountDeletionLedger
    from bowhead_core.account_deletion_service import AccountDeletionService
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
    from bowhead_core.session_store import SessionStore
    from bowhead_core.terminal_protocol_store import TerminalProtocolStore

    ledger = AccountDeletionLedger(tmp_path / "outside-backup" / "deletions.db", secret="synthetic-instance-secret")
    await ledger.initialize()
    return AccountDeletionService(
        store,
        lifecycle=SessionLifecycleCoordinator(SessionStore(store.db_path), TerminalProtocolStore(store.db_path)),
        artifacts=ArtifactStore(db_path=store.db_path, backend=LocalBackend(tmp_path / "blobs")),
        ledger=ledger,
        secret="synthetic-instance-secret",
        external_cleanup=AsyncMock(),
    )


async def test_retirement_releases_email_and_keeps_original_deadline(store: UserStore) -> None:
    first = await request_account_deletion(store, "usr_old", now=NOW)
    repeated = await request_account_deletion(store, "usr_old", now=NOW + timedelta(days=5))
    assert first == repeated
    assert datetime.fromisoformat(first.purge_after) == NOW + timedelta(days=30)
    assert await store.get_user_by_email("member@example.com") is None
    old = await store.get_user("usr_old")
    assert old is not None and old.status == "deleted" and old.password_hash is None
    new = await store.create_user(team_id="team_1", email="member@example.com", password="New-password-123!")
    assert new.id != old.id
    assert new.status == "active"
    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError, match="cannot be revived"):
            await db.execute("UPDATE users SET status='active' WHERE id='usr_old'")
        with pytest.raises(aiosqlite.IntegrityError, match="account is deleted"):
            await db.execute("INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES ('s_late','usr_old','ws_1','t','t')")


async def test_last_team_owner_failure_does_not_release_email(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE teams SET created_by_user_id='usr_old' WHERE id='team_1'")
        await db.commit()
    with pytest.raises(AccountDeletionError, match="account_transfer_required") as caught:
        await request_account_deletion(store, "usr_old", now=NOW)
    assert caught.value.team_ids == ("team_1",)
    user = await store.get_user_by_email("member@example.com")
    assert user is not None and user.status == "active"


@pytest.mark.parametrize("status", ["deleted", "legacy_deleted", "removed"])
@pytest.mark.parametrize("audited", [False, True])
async def test_historical_adoption_uses_only_real_deletion_time(store: UserStore, status: str, audited: bool) -> None:
    audit_time = NOW - timedelta(days=15)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET status=? WHERE id='usr_old'", (status,))
        if audited:
            await db.execute(
                "INSERT INTO user_audit_log(id,user_id,event_type,created_at) VALUES ('audit','usr_old','user_delete_request',?)",
                (audit_time.isoformat(),),
            )
        await db.commit()
    assert await adopt_deleted_accounts(store, now=NOW) == 1
    assert await adopt_deleted_accounts(store, now=NOW + timedelta(days=1)) == 0
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT requested_at,purge_after,time_source FROM account_deletions") as cur:
        row = await cur.fetchone()
    assert row is not None
    expected = audit_time if audited else NOW
    assert abs((datetime.fromisoformat(row[0]) - expected).total_seconds()) < 0.001
    assert abs((datetime.fromisoformat(row[1]) - expected - timedelta(days=30)).total_seconds()) < 0.001
    assert row[2] == ("audit" if audited else "adoption")
    assert await store.get_user_by_email("member@example.com") is None


async def test_due_erasure_removes_content_and_preserves_same_email_new_identity(store: UserStore, service) -> None:
    from bowhead_core.session_store import SessionStore

    sessions = SessionStore(store.db_path)
    session = await sessions.ensure_session(session_id="s_old", workspace_id="ws_1", user_id="usr_old")
    await sessions.record_turn(turn_id="turn_old", session_id=session.id, user_text="private old content")
    old_file = await service.artifacts.put_bytes(
        content=b"private old file", filename="old.txt", mime="text/plain", created_by="usr_old", kind="user_upload", scope="user"
    )
    retired_at = datetime.now(UTC) - timedelta(days=31)
    service.clock = lambda: retired_at
    await service.request("usr_old")
    new = await store.create_user(team_id="team_1", email="member@example.com", password="New-password-123!")
    new_file = await service.artifacts.put_bytes(
        content=b"new file", filename="new.txt", mime="text/plain", created_by=new.id, kind="user_upload", scope="user"
    )
    assert await service.run_once() == {"completed": 0, "failed": 0}
    assert Path(old_file.path).exists()
    service.clock = lambda: datetime.now(UTC)
    result = await service.run_once()
    assert result == {"completed": 1, "failed": 0}
    assert not Path(old_file.path).exists()
    assert Path(new_file.path).read_bytes() == b"new file"
    assert (await store.get_user_by_email("member@example.com")).id == new.id
    async with aiosqlite.connect(store.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM sessions WHERE user_id='usr_old'")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM turns WHERE id='turn_old'")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT state FROM account_deletions")).fetchone())[0] == "completed"


async def test_restore_replays_retirement_without_touching_new_identity(store: UserStore, service, tmp_path: Path) -> None:
    backup = tmp_path / "before.db"
    with sqlite3.connect(store.db_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    await service.request("usr_old")
    with sqlite3.connect(backup) as source, sqlite3.connect(store.db_path) as target:
        source.backup(target)
    assert (await store.get_user("usr_old")).status == "active"
    await service.ledger.replay(store)
    assert (await store.get_user("usr_old")).status == "deleted"
    assert await store.get_user_by_email("member@example.com") is None


@pytest.mark.parametrize("closed", [False, True], ids=["running-protocol", "committed-protocol"])
async def test_protocol_accounts_are_erased_without_inventing_outcomes(tmp_path: Path, closed: bool) -> None:
    from unittest.mock import AsyncMock

    from bowhead_core.account_deletion_ledger import AccountDeletionLedger
    from bowhead_core.account_deletion_service import AccountDeletionService
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.events import OrchestratorEvent
    from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
    from bowhead_core.session_store import SessionStore
    from bowhead_core.terminal_protocol import EffectClass
    from test_terminal_runtime import _create_db, _runtime, _seed_billing_user

    path = _create_db(tmp_path)
    _seed_billing_user(path)
    runtime = await _runtime(path)
    if closed:
        await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "private protocol answer"}))
        await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed", "intent": "lookup"}))
    else:
        attempt = await runtime.store.begin_effect_attempt(
            runtime.execution,
            attempt_id="effect_unknown",
            effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
            provider="synthetic-provider",
            action_ref="opaque-action",
            idempotency_or_fence_token="opaque-token",
            non_retriable=True,
        )
        attempt = await runtime.store.mark_effect_dispatched(runtime.execution, attempt)
        await runtime.store.mark_effect_unknown(attempt)
    users = UserStore(path, terminal_auth_fence_enabled=True)
    ledger = AccountDeletionLedger(tmp_path / "external.db", secret="synthetic-secret")
    await ledger.initialize()
    now = datetime.now(UTC)
    service = AccountDeletionService(
        users,
        lifecycle=SessionLifecycleCoordinator(SessionStore(path), runtime.store),
        artifacts=ArtifactStore(db_path=path, backend=LocalBackend(tmp_path / "blobs")),
        ledger=ledger,
        secret="synthetic-secret",
        external_cleanup=AsyncMock(),
        clock=lambda: now - timedelta(days=31),
    )
    await service.request("u_1")
    assert await service.run_once() == {"completed": 0, "failed": 0}
    service.clock = lambda: now
    assert await service.run_once() == {"completed": 1, "failed": 0}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_generations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM turn_events").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        receipts = db.execute("SELECT identity_kind,state FROM account_protocol_erasure_receipts").fetchall()
        assert ("generation", "closed") in receipts
        if not closed:
            assert ("effect", "unknown") in receipts


async def test_storage_failure_is_retried_without_false_completion(store: UserStore, service) -> None:
    from unittest.mock import AsyncMock

    file = await service.artifacts.put_bytes(
        content=b"held", filename="held.txt", mime="text/plain", created_by="usr_old", kind="user_upload", scope="user"
    )
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    service.clock = lambda: now
    delete = service.artifacts.backend.delete
    service.artifacts.backend.delete = AsyncMock(side_effect=OSError("synthetic storage outage"))
    assert await service.run_once() == {"completed": 0, "failed": 1}
    assert Path(file.path).exists()
    service.artifacts.backend.delete = delete
    service.clock = lambda: now + timedelta(minutes=6)
    assert await service.run_once() == {"completed": 1, "failed": 0}
    assert not Path(file.path).exists()


async def test_unreviewed_schema_blocks_erasure_before_blob_deletion(store: UserStore, service) -> None:
    file = await service.artifacts.put_bytes(
        content=b"held", filename="held.txt", mime="text/plain", created_by="usr_old", kind="user_upload", scope="user"
    )
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("CREATE TABLE new_personal_content(user_id TEXT,secret TEXT)")
        await db.commit()
    service.clock = lambda: now
    assert await service.run_once() == {"completed": 0, "failed": 1}
    assert Path(file.path).exists()
    async with aiosqlite.connect(store.db_path) as db:
        assert (await (await db.execute("SELECT last_code FROM account_deletions")).fetchone())[0] == "account_erasure_unreviewed_table"


@pytest.mark.parametrize("with_canvas", [False, True])
async def test_document_journals_and_frozen_inputs_follow_due_account_erasure(document_ctx, tmp_path: Path, with_canvas: bool) -> None:
    from unittest.mock import AsyncMock

    from bowhead_core.account_deletion_ledger import AccountDeletionLedger
    from bowhead_core.account_deletion_service import AccountDeletionService
    from bowhead_core.documents.inputs import DocumentInputStore
    from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
    from bowhead_core.terminal_protocol_store import TerminalProtocolStore
    from test_document_store import SCOPE, _sql

    ctx = document_ctx
    if with_canvas:
        from test_document_canvas_inputs import admit_canvas, freeze_canvas, setup_canvas

        canvas = await setup_canvas(ctx)
        receipt, _ = await admit_canvas(ctx, canvas)
        await freeze_canvas(ctx, canvas, receipt.job_id)
    else:
        receipt = await ctx.submit()
    await DocumentInputStore(ctx.artifacts).freeze(scope=SCOPE, job_id=receipt.job_id)
    input_paths = [row[0] for row in await _sql(ctx, "SELECT path FROM document_input_copies")]
    input_paths += [row[0] for row in await _sql(ctx, "SELECT path FROM document_canvas_inputs")]
    with pytest.raises(aiosqlite.IntegrityError, match="retained"):
        await _sql(ctx, "DELETE FROM document_jobs WHERE id=?", (receipt.job_id,))
    ledger = AccountDeletionLedger(tmp_path / "outside.db", secret="synthetic-secret")
    await ledger.initialize()
    users = UserStore(ctx.path)
    now = datetime.now(UTC)
    service = AccountDeletionService(
        users,
        lifecycle=SessionLifecycleCoordinator(ctx.sessions, TerminalProtocolStore(str(ctx.path))),
        artifacts=ctx.artifacts,
        ledger=ledger,
        secret="synthetic-secret",
        external_cleanup=AsyncMock(),
        clock=lambda: now - timedelta(days=31),
    )
    await service.request(SCOPE.user_id)
    service.clock = lambda: now
    assert await service.run_once() == {"completed": 1, "failed": 0}
    for table in (
        "document_jobs",
        "document_input_sets",
        "document_input_copies",
        "document_canvas_inputs",
        "document_artifact_refs",
        "document_command_receipts",
    ):
        assert (await _sql(ctx, f"SELECT COUNT(*) FROM {table}"))[0][0] == 0
    assert not Path(ctx.artifact.path).exists()
    for path in input_paths:
        assert not Path(path).exists()


async def test_s3_erasure_removes_versions_without_touching_neighbor_keys() -> None:
    from bowhead_core.artifact_store import S3Backend

    versions = {("objects/old", "v1"), ("objects/old", "v2"), ("objects/old-neighbor", "v1")}
    markers: set[tuple[str, str]] = set()

    class Client:
        def get_paginator(self, operation):
            assert operation == "list_object_versions"
            return self

        def paginate(self, **kwargs):
            return [
                {
                    "Versions": [{"Key": key, "VersionId": version} for key, version in versions],
                    "DeleteMarkers": [{"Key": key, "VersionId": version} for key, version in markers],
                }
            ]

        def delete_object(self, *, Bucket, Key, VersionId=None):
            if VersionId is None:
                markers.add((Key, "new-marker"))
            else:
                versions.discard((Key, VersionId))
                markers.discard((Key, VersionId))

    backend = S3Backend("https://synthetic.invalid", "bucket", "synthetic-key", "synthetic-secret", _client=Client())
    await backend.erase("objects/old")
    assert versions == {("objects/old-neighbor", "v1")}
    assert not markers


async def test_retired_owner_is_rejected_before_writing_any_bytes(store: UserStore, service) -> None:
    from unittest.mock import AsyncMock

    await service.request("usr_old")
    service.artifacts.backend.put = AsyncMock()
    with pytest.raises(aiosqlite.IntegrityError, match="account is deleted"):
        await service.artifacts.put_bytes(content=b"late secret", filename="late.txt", mime="text/plain", created_by="usr_old")
    service.artifacts.backend.put.assert_not_awaited()


async def test_upload_crossing_retirement_keeps_durable_erasure_identity(store: UserStore, service) -> None:
    import asyncio

    written, finish = asyncio.Event(), asyncio.Event()
    original = service.artifacts.backend.put
    paths: list[str] = []

    async def paused(**kwargs):
        path = await original(**kwargs)
        paths.append(path)
        written.set()
        await finish.wait()
        return path

    service.artifacts.backend.put = paused
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    upload = asyncio.create_task(
        service.artifacts.put_bytes(content=b"private in-flight bytes", filename="pending.txt", mime="text/plain", created_by="usr_old")
    )
    await written.wait()
    try:
        await service.request("usr_old")
    finally:
        finish.set()
    with pytest.raises(aiosqlite.IntegrityError, match="account is deleted"):
        await upload
    assert Path(paths[0]).exists()
    service.clock = lambda: now
    assert await service.run_once() == {"completed": 1, "failed": 0}
    assert not Path(paths[0]).exists()


async def test_concurrent_workers_do_not_erase_twice(store: UserStore, service) -> None:
    import asyncio
    import copy

    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    service.clock = lambda: now
    other = copy.copy(service)
    other.owner = "other-worker"
    results = await asyncio.gather(service.run_once(), other.run_once())
    assert sum(result["completed"] for result in results) == 1
    assert sum(result["failed"] for result in results) == 0


async def test_latest_valid_historical_deletion_is_not_shortened(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET status='deleted' WHERE id='usr_old'")
        for index, age in enumerate((90, 10)):
            await db.execute(
                "INSERT INTO user_audit_log(id,user_id,event_type,created_at) VALUES (?,'usr_old','user_delete_request',?)",
                (f"audit_{index}", (NOW - timedelta(days=age)).isoformat()),
            )
        await db.commit()
    await adopt_deleted_accounts(store, now=NOW)
    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT purge_after FROM account_deletions")).fetchone()
    assert abs((datetime.fromisoformat(row[0]) - (NOW + timedelta(days=20))).total_seconds()) < 0.001


async def test_post_commit_verification_failure_retries_without_recreating_content(store: UserStore, service) -> None:
    from unittest.mock import AsyncMock

    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    artifact = await service.artifacts.put_bytes(content=b"secret", filename="old.txt", mime="text/plain", created_by="usr_old")
    await service.request("usr_old")
    service.clock = lambda: now
    finish = service._finish_verification
    service._finish_verification = AsyncMock(side_effect=AccountDeletionError("account_storage_checkpoint_pending"))
    assert await service.run_once() == {"completed": 0, "failed": 1}
    assert not Path(artifact.path).exists()
    async with aiosqlite.connect(store.db_path) as db:
        assert await (await db.execute("SELECT state,completed_at FROM account_deletions")).fetchone() == ("verifying", None)
        assert await (await db.execute("SELECT count(*) FROM artifacts")).fetchone() == (0,)
    service._finish_verification = finish
    service.clock = lambda: now + timedelta(minutes=6)
    assert await service.run_once() == {"completed": 1, "failed": 0}


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_incremental_migration_preserves_existing_data_without_starting_erasure(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    # Start right before the account-deletion revision so existing rows predate it.
    command.upgrade(config, ScriptDirectory.from_config(config).get_revision("0179_account_deletion").down_revision)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team_existing','Existing','active','2020-01-01')")
            db.execute(
                "INSERT INTO users(id,team_id,email,status,created_at) VALUES ('usr_existing','team_existing','old@example.com','deleted','2020-01-01')"
            )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)
        if target == "core":
            assert db.execute("SELECT email,status FROM users").fetchone() == ("old@example.com", "deleted")
            assert db.execute("SELECT count(*) FROM account_deletions").fetchone() == (0,)
        if target == "files":
            assert db.execute("SELECT count(*) FROM account_retention_holds").fetchone() == (0,)


async def test_retirement_immediately_hides_shared_reports_but_retains_source(store: UserStore, service) -> None:
    from bowhead_core.canvas_store import CanvasStore

    canvases = CanvasStore(store.db_path)
    canvas = await canvases.ensure_canvas(session_id="session_report", workspace_id="ws_1", user_id="usr_old", title="Private report")
    assert await canvases.get_canvas(canvas.id) is not None
    assert await canvases.list_for_user(user_id="usr_viewer", session_ids=["session_report"])
    await service.request("usr_old")
    assert await canvases.get_canvas(canvas.id) is None
    assert await canvases.get_canvas_by_session("session_report") is None
    assert await canvases.read_canvas_projection_state(canvas.id) is None
    assert await canvases.list_for_user(user_id="usr_viewer", session_ids=["session_report"]) == []
    assert await canvases.count_by_workspace_for_user(user_id="usr_viewer", session_ids=["session_report"]) == {}
    async with aiosqlite.connect(store.db_path) as db:
        assert await (await db.execute("SELECT title FROM canvases WHERE id=?", (canvas.id,))).fetchone() == ("Private report",)


async def test_audit_redaction_requires_due_exact_account_authority(store: UserStore, service) -> None:
    assert await store.write_admin_audit_log(
        actor_user_id="usr_old",
        team_id="team_1",
        action="profile_update",
        scope="user",
        target_kind="user",
        target_id="usr_old",
        payload={"email": "member@example.com"},
        ip="192.0.2.1",
        user_agent="Synthetic browser",
    )
    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await db.execute("UPDATE admin_audit_log SET payload_json='{}'")
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    assert await service.run_once() == {"completed": 0, "failed": 0}
    async with aiosqlite.connect(store.db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await db.execute("UPDATE admin_audit_log SET actor_user_id='retired',target_id=NULL,payload_json='{}',ip=NULL,user_agent=NULL")
    service.clock = lambda: now
    assert await service.run_once() == {"completed": 1, "failed": 0}
    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT actor_user_id,target_id,payload_json,ip,user_agent,action FROM admin_audit_log")).fetchone()
        assert row == ("retired", None, "{}", None, None, "profile_update")
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await db.execute("UPDATE admin_audit_log SET action='forged'")


async def test_last_space_administrator_must_handoff_before_retirement(store: UserStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES ('ws_owned','team_1','Owned','active','t','t')")
        await db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at,status) VALUES ('ws_owned','usr_old','ws_admin','t','active')"
        )
        await db.commit()
    with pytest.raises(AccountDeletionError, match="account_space_transfer_required") as caught:
        await request_account_deletion(store, "usr_old", now=NOW)
    assert caught.value.team_ids == ("team_1",)
    assert (await store.get_user_by_email("member@example.com")).status == "active"
    other = await store.create_user(team_id="team_1", email="other@example.com", password="Synthetic-password-123!")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at,status) VALUES ('ws_owned',?,'ws_admin','t','active')", (other.id,)
        )
        await db.commit()
    await request_account_deletion(store, "usr_old", now=NOW)
    assert (await store.get_user("usr_old")).status == "deleted"


async def test_historical_tombstone_invites_use_original_principal_not_reused_mailbox(store: UserStore, service) -> None:
    now = datetime.now(UTC)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET status='legacy_deleted',email='usr_old@deleted.invalid' WHERE id='usr_old'")
        await db.execute(
            "INSERT INTO team_invite_tokens(id,team_id,invited_email,token_hash,status,created_at,expires_at,consumed_by_user_id) "
            "VALUES ('invite_old','team_1','member@example.com','old-hash','consumed','t','t','usr_old')"
        )
        await db.commit()
    await adopt_deleted_accounts(store, now=now - timedelta(days=31))
    new = await store.create_user(team_id="team_1", email="member@example.com", password="Synthetic-password-123!")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO team_invite_tokens(id,team_id,invited_email,token_hash,status,created_at,expires_at,consumed_by_user_id) "
            "VALUES ('invite_new','team_1','member@example.com','new-hash','consumed','t','t',?)",
            (new.id,),
        )
        await db.commit()
    service.clock = lambda: now
    assert await service.run_once() == {"completed": 1, "failed": 0}
    async with aiosqlite.connect(store.db_path) as db:
        assert await (await db.execute("SELECT id FROM team_invite_tokens")).fetchall() == [("invite_new",)]


async def test_report_grants_follow_session_identity_at_erasure(store: UserStore, service) -> None:
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.session_store import SessionStore

    await SessionStore(store.db_path).ensure_session(session_id="s_shared", workspace_id="ws_1", user_id="usr_old")
    access = AccessControlStore(store.db_path)
    await access.create_or_update_resource_grant(
        team_id="team_1", resource_kind="report", resource_id="s_shared", principal_type="team", principal_id="team_1", permission="read"
    )
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    service.clock = lambda: now
    assert await service.run_once() == {"completed": 1, "failed": 0}
    async with aiosqlite.connect(store.db_path) as db:
        assert await (
            await db.execute("SELECT count(*) FROM resource_grants WHERE resource_kind='report' AND resource_id='s_shared'")
        ).fetchone() == (0,)


async def test_reused_mailbox_does_not_claim_other_principal_invites_or_earlier_mail(store: UserStore, service) -> None:
    now = datetime.now(UTC)
    other = await store.create_user(team_id="team_1", email="other@example.com", password="Synthetic-password-123!")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET created_at=? WHERE id='usr_old'", ((now - timedelta(days=60)).isoformat(),))
        await db.execute(
            "INSERT INTO team_invite_tokens(id,team_id,invited_email,token_hash,status,created_at,expires_at,consumed_by_user_id) "
            "VALUES ('invite_other','team_1','member@example.com','other-hash','consumed','t','t',?)",
            (other.id,),
        )
        for identity, age in [("mail_previous", 90), ("mail_current", 40)]:
            await db.execute(
                "INSERT INTO smtp_outbox(id,to_email,subject,body,purpose,status,created_at) VALUES (?,'member@example.com','Synthetic','Synthetic','verify','sent',?)",
                (identity, (now - timedelta(days=age)).isoformat()),
            )
        await db.commit()
    await service.request("usr_old")
    async with aiosqlite.connect(store.db_path) as db:
        selected = await (await db.execute("SELECT table_name,json_extract(row_key,'$[0]') FROM account_deletion_rows")).fetchall()
    assert ("team_invite_tokens", "invite_other") not in selected
    assert ("smtp_outbox", "mail_previous") not in selected
    assert ("smtp_outbox", "mail_current") in selected


async def test_erasure_waits_for_inflight_upload_before_completion(store: UserStore, service) -> None:
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    original = service.artifacts.backend.put
    paths = []

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        path = await original(**kwargs)
        paths.append(path)
        return path

    service.artifacts.backend.put = delayed
    upload = asyncio.create_task(
        service.artifacts.put_bytes(content=b"In-flight account bytes", filename="pending.txt", mime="text/plain", created_by="usr_old")
    )
    await entered.wait()
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    service.clock = lambda: now
    try:
        assert await service.run_once() == {"completed": 0, "failed": 1}
    finally:
        release.set()
        with pytest.raises(aiosqlite.IntegrityError, match="account is deleted"):
            await upload
    service.clock = lambda: now + timedelta(minutes=6)
    assert await service.run_once() == {"completed": 1, "failed": 0}
    assert paths and not Path(paths[0]).exists()


async def test_cancelled_upload_keeps_erasure_locked_until_provider_finishes(store: UserStore, service) -> None:
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    original = service.artifacts.backend.put
    paths = []

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        path = await original(**kwargs)
        paths.append(path)
        return path

    service.artifacts.backend.put = delayed
    upload = asyncio.create_task(
        service.artifacts.put_bytes(content=b"Cancelled provider write", filename="cancelled.txt", mime="text/plain", created_by="usr_old")
    )
    await entered.wait()
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    upload.cancel()
    await asyncio.sleep(0)
    service.clock = lambda: now
    try:
        assert not upload.done()
        assert await service.run_once() == {"completed": 0, "failed": 1}
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await upload
    service.clock = lambda: now + timedelta(minutes=6)
    assert await service.run_once() == {"completed": 1, "failed": 0}
    assert paths and not Path(paths[0]).exists()


async def test_account_retirement_fences_known_and_unseen_chat_sessions_atomically(store: UserStore, service) -> None:
    from bowhead_core.session_store import SessionStore

    sessions = SessionStore(store.db_path)
    now = datetime.now(UTC).timestamp()
    assert await sessions.register_chat_auth_session_generation(user_id="usr_old", auth_session_generation_hash="a" * 64, issued_at_epoch=now - 60)

    async def failed_receipt(_deletion):
        raise OSError("Synthetic ledger unavailable")

    with pytest.raises(OSError, match="Synthetic ledger"):
        await request_account_deletion(store, "usr_old", record_intent=failed_receipt)
    assert (await store.get_user_by_email("member@example.com")).status == "active"
    assert await sessions.is_chat_auth_session_generation_active(user_id="usr_old", auth_session_generation_hash="a" * 64)
    await service.request("usr_old")
    assert not await sessions.is_chat_auth_session_generation_active(user_id="usr_old", auth_session_generation_hash="a" * 64)
    assert not await sessions.register_chat_auth_session_generation(
        user_id="usr_old", auth_session_generation_hash="b" * 64, issued_at_epoch=now - 30
    )


async def test_retired_owner_cannot_write_unpublished_report_export(store: UserStore, service) -> None:
    await service.request("usr_old")
    with pytest.raises(aiosqlite.IntegrityError, match="account is deleted"):
        await service.artifacts.stage_export_bytes(
            content=b"Private report bytes",
            filename="report.pdf",
            mime="application/pdf",
            canvas_id=None,
            created_by="usr_old",
            session_id=None,
            producer_kind="canvas_export_v2",
        )
    assert list(service.artifacts.backend.root_dir.rglob("*")) == []


async def test_native_timeout_preserves_writer_lock_until_provider_ack(store: UserStore, service) -> None:
    import asyncio

    import anyio

    entered, release = asyncio.Event(), asyncio.Event()
    original = service.artifacts.backend.put
    paths = []

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        path = await original(**kwargs)
        paths.append(path)
        return path

    service.artifacts.backend.put = delayed

    scopes = []

    async def request():
        with anyio.move_on_after(3600) as scope:
            scopes.append(scope)
            await service.artifacts.put_bytes(content=b"Native timeout bytes", filename="timeout.txt", mime="text/plain", created_by="usr_old")
        assert scope.cancel_called

    upload = asyncio.create_task(request())
    await entered.wait()
    now = datetime.now(UTC)
    service.clock = lambda: now - timedelta(days=31)
    await service.request("usr_old")
    scopes[0].deadline = anyio.current_time() - 1
    await asyncio.sleep(0)
    service.clock = lambda: now
    try:
        assert not upload.done()
        assert await service.run_once() == {"completed": 0, "failed": 1}
    finally:
        release.set()
        await upload
    service.clock = lambda: now + timedelta(minutes=6)
    assert await service.run_once() == {"completed": 1, "failed": 0}
    assert paths and not Path(paths[0]).exists()


@pytest.mark.parametrize("audit_time", ["0000-01-01", "not-a-timestamp", (NOW + timedelta(days=1)).isoformat()])
async def test_invalid_or_future_historical_audit_starts_fresh_retention(store: UserStore, audit_time: str) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET status='legacy_deleted' WHERE id='usr_old'")
        await db.execute(
            "INSERT INTO user_audit_log(id,user_id,event_type,created_at) VALUES ('audit_invalid','usr_old','user_delete_request',?)", (audit_time,)
        )
        await db.commit()
    assert await adopt_deleted_accounts(store, now=NOW) == 1
    async with aiosqlite.connect(store.db_path) as db:
        row = await (await db.execute("SELECT requested_at,purge_after,time_source FROM account_deletions")).fetchone()
    assert row == (NOW.isoformat(), (NOW + timedelta(days=30)).isoformat(), "adoption")


async def test_due_erasure_removes_the_accounts_llm_request_records(store: UserStore, service) -> None:
    import hashlib

    from bowhead_core.chat_reference_storage import ReferenceCipher
    from bowhead_core.llm_request_record import write_request_records
    from bowhead_core.session_store import SessionStore
    from bowhead_router.invocation import canonical_transport_bytes
    from bowhead_router.request_record import RequestChange, RequestRecordEntry
    from bowhead_router.usage import UsageContext
    from cryptography.fernet import Fernet

    sessions = SessionStore(store.db_path)
    await sessions.ensure_session(session_id="s_old", workspace_id="ws_1", user_id="usr_old")
    await sessions.record_turn(turn_id="turn_old", session_id="s_old", user_text="private old content")
    body = canonical_transport_bytes({"model": "m", "messages": [{"role": "user", "content": "private old content"}]})
    entry = RequestRecordEntry(
        usage=UsageContext(team_id="team_1", user_id="usr_old", workspace_id="ws_1", turn_id="turn_old", reason="turn"),
        provider="p",
        model="m",
        family="openai",
        mode="invoke",
        canonical_body=body,
        request_digest=hashlib.sha256(body).hexdigest(),
        usage_event_id=None,
        subagent_id=None,
        created_at=datetime.now(UTC),
        changes=(RequestChange(kind="tool_result_capped", detail={"tool_call_id": "c1"}),),
    )
    async with aiosqlite.connect(store.db_path) as db:
        assert await write_request_records(db, ReferenceCipher(Fernet.generate_key().decode()), [entry]) == 1
        await db.commit()

    service.clock = lambda: datetime.now(UTC) - timedelta(days=31)
    await service.request("usr_old")
    service.clock = lambda: datetime.now(UTC)
    assert await service.run_once() == {"completed": 1, "failed": 0}

    async with aiosqlite.connect(store.db_path) as db:
        for table in ("llm_request_records", "llm_request_chunks", "llm_request_changes"):
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == 0, table
