"""Account privacy cleanup verifies every migrated file before completing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bowhead_core.account_deletion_ledger import AccountDeletionLedger
from bowhead_core.account_deletion_service import AccountDeletionService
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_runtime import connect
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from bowhead_core.user_store import UserStore
from test_sqlite_domain_stores import domain_directory as domain_directory


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_account_erasure_removes_cross_domain_rows_and_keeps_other_owners(domain_directory: Path, monkeypatch, interrupt: bool) -> None:
    core = domain_directory / "bowhead.db"
    user_id = "u_erasure_retry" if interrupt else "u_erasure_ok"
    session_id = "s_" + user_id
    turn_id = "t_" + user_id
    async with connect(core) as db:
        await db.execute(
            "INSERT INTO users(id,team_id,email,created_at) VALUES (?,'team_1',?,'2020-01-01')",
            (user_id, user_id + "@example.test"),
        )
        await db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,created_at) VALUES (?, ?,'team_1','member','t')", ("tm_" + user_id, user_id)
        )
        await db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES ('ws_1',?,'member','t')", (user_id,))
        await db.commit()
    users = UserStore(core)
    sessions = SessionStore(core)
    terminal = TerminalProtocolStore(core)
    session = await sessions.ensure_session(session_id=session_id, workspace_id="ws_1", user_id=user_id)
    await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text="Private retained data")
    artifacts = ArtifactStore(core, LocalBackend(domain_directory / "erasure-blobs"))
    artifact = await artifacts.put_bytes(
        content=b"private content", filename="private.txt", mime="text/plain", created_by=user_id, session_id=session.id
    )
    ledger = AccountDeletionLedger(domain_directory / "privacy-ledger" / "account-deletions.db", secret="synthetic-instance-secret")
    await ledger.initialize()
    now = datetime.now(UTC)
    service = AccountDeletionService(
        users,
        lifecycle=SessionLifecycleCoordinator(sessions, terminal),
        artifacts=artifacts,
        ledger=ledger,
        secret="synthetic-instance-secret",
        external_cleanup=AsyncMock(),
        clock=lambda: now - timedelta(days=31),
    )
    deletion = await service.request(user_id)
    service.clock = lambda: now
    if interrupt:
        import bowhead_core.account_deletion_service as erasure

        original_erase = erasure.erase_rows
        failed = False

        async def interrupted_erase(db, operation_id, primary, *, domain=None):
            nonlocal failed
            if domain == "execution" and not failed:
                failed = True
                raise RuntimeError("simulated interruption between committed domains")
            await original_erase(db, operation_id, primary, domain=domain)

        monkeypatch.setattr(erasure, "erase_rows", interrupted_erase)
        await service.run_once()
        assert failed
        async with connect(core) as db:
            async with db.execute("SELECT state FROM account_deletions WHERE operation_id=?", (deletion.operation_id,)) as cursor:
                assert (await cursor.fetchone())[0] != "completed"
            async with db.execute("SELECT COUNT(*) FROM account_deletion_rows WHERE operation_id=?", (deletion.operation_id,)) as cursor:
                assert (await cursor.fetchone())[0] > 0
        service.clock = lambda: now + timedelta(minutes=6)
    await service.run_once()
    async with connect(core) as db:
        async with db.execute("SELECT state,last_code FROM account_deletions WHERE operation_id=?", (deletion.operation_id,)) as cursor:
            assert await cursor.fetchone() == ("completed", None)
        async with db.execute("SELECT status FROM users WHERE id='u_1'") as cursor:
            assert await cursor.fetchone() == ("active",)
    for domain, table, key, identity in (
        ("execution", "sessions", "id", session.id),
        ("execution", "turns", "id", turn_id),
        ("content", "artifacts", "id", artifact.id),
        ("core", "terminal_authority_scope_epochs", "scope_ref", user_id),
    ):
        async with (
            connect(domain_database_path(core, domain)) as db,
            db.execute(f'SELECT 1 FROM main."{table}" WHERE "{key}"=?', (identity,)) as cursor,
        ):
            assert await cursor.fetchone() is None
    with pytest.raises(FileNotFoundError):
        await artifacts.backend.read(artifact.path)


@pytest.mark.asyncio
async def test_frozen_diagnostic_payload_ownership_survives_missing_turn(tmp_path: Path, monkeypatch) -> None:
    import aiosqlite
    import bowhead_core.account_erasure as erasure

    policy = {
        "tables": {
            "llm_request_records": {"columns": ["id", "turn_id", "user_id"], "owner_columns": ["user_id"]},
            "llm_request_chunks": {"columns": ["turn_id", "chunk_hash", "ciphertext"], "owner_columns": []},
            "llm_request_changes": {"columns": ["id", "record_id", "detail_ciphertext"], "owner_columns": []},
        }
    }
    monkeypatch.setattr(erasure, "_POLICY", policy)
    async with aiosqlite.connect(tmp_path / "orphan.db") as db:
        await db.executescript(
            "CREATE TABLE account_deletion_rows(operation_id TEXT,table_name TEXT,row_key TEXT,PRIMARY KEY(operation_id,table_name,row_key));"
            "CREATE TABLE llm_request_records(id TEXT PRIMARY KEY,turn_id TEXT,user_id TEXT);"
            "CREATE TABLE llm_request_chunks(turn_id TEXT,chunk_hash TEXT,ciphertext TEXT,PRIMARY KEY(turn_id,chunk_hash));"
            "CREATE TABLE llm_request_changes(id TEXT PRIMARY KEY,record_id TEXT,detail_ciphertext TEXT);"
            "INSERT INTO llm_request_records VALUES ('record','deleted-turn','retired-user');"
            "INSERT INTO llm_request_chunks VALUES ('deleted-turn','hash','opaque ciphertext');"
            "INSERT INTO llm_request_changes VALUES ('change','record','opaque ciphertext');"
        )
        await erasure.freeze_rows(db, "erase", "retired-user")
        async with db.execute("SELECT table_name,row_key FROM account_deletion_rows ORDER BY table_name") as cursor:
            assert await cursor.fetchall() == [
                ("llm_request_changes", '["change"]'),
                ("llm_request_chunks", '["deleted-turn","hash"]'),
                ("llm_request_records", '["record"]'),
            ]
