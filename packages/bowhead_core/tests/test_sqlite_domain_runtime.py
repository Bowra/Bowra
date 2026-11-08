"""Cross-domain security predicates retain SQLite transaction semantics."""

from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from bowhead_core.sqlite_domain_runtime import (
    _TRIGGERS,
    _VIEWS,
    DomainConnection,
    compile_domain_trigger,
    compile_domain_view,
    domain_authority_triggers,
    guard_connection,
)
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES


@pytest.fixture
def domain_files(tmp_path: Path) -> Path:
    definitions = {
        "core": (
            "CREATE TABLE users(id TEXT PRIMARY KEY,status TEXT,balance INTEGER)",
            "CREATE TABLE workspaces(id TEXT PRIMARY KEY,team_id TEXT)",
            "CREATE TABLE account_deletions(operation_id TEXT,user_id TEXT,state TEXT,purge_after TEXT,lease_owner TEXT,lease_expires_at TEXT)",
            "CREATE TABLE account_deletion_rows(operation_id TEXT,table_name TEXT,row_key TEXT)",
            "CREATE TABLE memory_turn_requests(turn_id TEXT)",
            "CREATE TABLE team_reference_retention_policies(team_id TEXT,version INTEGER,days INTEGER)",
            "CREATE TABLE team_builtin_subagent_override(team_id TEXT,name TEXT,enabled INTEGER,row_version INTEGER)",
            "CREATE TABLE custom_subagents(id TEXT PRIMARY KEY,user_id TEXT,name TEXT,display_name TEXT,description TEXT,system_prompt TEXT,allowed_tools TEXT,auto_read_skills TEXT,max_iterations INTEGER,status TEXT,created_at TEXT,updated_at TEXT)",
            "CREATE TABLE skill_instances(id TEXT PRIMARY KEY,user_id TEXT,enabled INTEGER,import_status TEXT,import_report_json TEXT)",
        ),
        "execution": (
            "CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id TEXT,deleted_at TEXT,workspace_id TEXT)",
            "CREATE TABLE turns(id TEXT PRIMARY KEY,session_id TEXT)",
            "CREATE TABLE turn_events(id TEXT PRIMARY KEY,turn_id TEXT)",
            "CREATE TABLE turn_attachments(artifact_id TEXT)",
            "CREATE TABLE turn_cancel_receipts(turn_id TEXT,turn_erased INTEGER)",
        ),
        "content": ("CREATE TABLE artifacts(id TEXT PRIMARY KEY)", "CREATE TABLE artifact_purge_claims(artifact_id TEXT)"),
        "notifications": ("CREATE TABLE notifications(id TEXT PRIMARY KEY)",),
        "observability": ("CREATE TABLE llm_request_records(turn_id TEXT,session_id TEXT)", "CREATE TABLE llm_request_chunks(turn_id TEXT)"),
    }
    for domain, statements in definitions.items():
        with sqlite3.connect(tmp_path / DOMAIN_TARGET_FILES[domain]) as db:
            db.execute("PRAGMA journal_mode=WAL")
            for sql in statements:
                db.execute(sql)
            for name in {
                "execution": (
                    "trg_turn_attachments_purge_insert",
                    "trg_account_deletion_no_session",
                    "trg_account_deletion_no_turn",
                    "trg_turn_delete_cancel_receipt_tombstone",
                    "trg_memory_turn_requests_turn_delete",
                    "trg_llm_request_turn_delete",
                    "trg_llm_request_session_delete",
                )
            }.get(domain, ()):
                db.execute(compile_domain_trigger(name, _TRIGGERS[name]["source"]))
            # The isolated fixture deliberately contains only a subset of business tables.
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            for statement in domain_authority_triggers(domain):
                if any(f' ON "{table}" ' in statement for table in tables):
                    db.execute(statement)
    return tmp_path / "bowhead.db"


def managed(core_path: Path, domain: str) -> DomainConnection:
    db = sqlite3.connect(core_path.with_name(DOMAIN_TARGET_FILES[domain]), factory=DomainConnection, uri=True, timeout=0)
    guard_connection(db, core_path, domain)
    return db


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_initial_statement_retries_authority_preparation_with_fresh_scope(
    domain_files: Path, monkeypatch: pytest.MonkeyPatch, cached: bool, mixed: bool
) -> None:
    import fcntl

    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','before')").close()
    execution.commit()
    execution.close()
    if mixed:
        content = managed(domain_files, "content")
        content.execute("INSERT INTO artifacts(id) VALUES('before')").close()
        content.commit()
        content.close()
    sql = "SELECT s.user_id,a.id FROM sessions s CROSS JOIN artifacts a" if mixed else "SELECT user_id FROM sessions"
    if cached:
        with core.execute(sql) as cursor:
            assert cursor.fetchall() == ([("before", "before")] if mixed else [("before",)])
    core.execute("PRAGMA busy_timeout=1000").close()
    ready, blocked = threading.Event(), threading.Event()
    errors = []
    flock = fcntl.flock

    def observe_conflict(fd: int, operation: int) -> None:
        try:
            flock(fd, operation)
        except BlockingIOError:
            blocked.set()
            raise

    def revoke() -> None:
        writer = managed(domain_files, "content" if mixed else "execution")
        try:
            writer.execute(
                "UPDATE artifacts SET id='after' WHERE id='before'" if mixed else "UPDATE sessions SET user_id='after' WHERE id='s'"
            ).close()
            ready.set()
            assert blocked.wait(5), "Reader did not reach the held authority lease"
            if mixed:
                followup = managed(domain_files, "execution")
                try:
                    followup.execute("UPDATE sessions SET user_id='after' WHERE id='s'").close()
                    followup.commit()
                finally:
                    followup.close()
            writer.commit()
        except BaseException as error:
            errors.append(error)
            ready.set()
        finally:
            writer.close()

    monkeypatch.setattr(fcntl, "flock", observe_conflict)
    worker = threading.Thread(target=revoke)
    worker.start()
    try:
        assert ready.wait(5)
        with core.execute(sql) as cursor:
            assert cursor.fetchall() == ([("after", "after")] if mixed else [("after",)])
        assert blocked.is_set()
        assert not core._leases
    finally:
        blocked.set()
        worker.join(timeout=5)
        core.close()
    assert not worker.is_alive()
    assert not errors


@pytest.mark.parametrize("boundary", ["transaction", "cursor", "callback"])
def test_preparation_retry_never_restarts_an_existing_snapshot_or_callback(
    domain_files: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from bowhead_core import sqlite_domain_runtime

    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    calls = []

    def no_wait(_: float) -> None:
        pytest.fail("An established snapshot or executing callback must fail without retry")

    def read_scope() -> object:
        calls.append("called")
        return core._scalar("SELECT user_id FROM sessions", ())

    monkeypatch.setattr(sqlite_domain_runtime.time, "sleep", no_wait)
    core.execute("PRAGMA busy_timeout=1000").close()
    execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
    try:
        if boundary == "transaction":
            core.execute("BEGIN").close()
        elif boundary == "cursor":
            cursor = core.execute("SELECT 1")
        else:
            core.create_function("read_scope", 0, read_scope)
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            core.execute("SELECT read_scope()" if boundary == "callback" else "SELECT user_id FROM sessions")
        assert calls == (["called"] if boundary == "callback" else [])
        if boundary == "cursor":
            cursor.close()
    finally:
        core.close()
        execution.close()


def test_preparation_retry_obeys_busy_timeout_and_keeps_diagnostics(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core import sqlite_domain_runtime

    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    core.execute("PRAGMA busy_timeout=25").close()
    execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
    elapsed = [0.0]

    def advance(delay: float) -> None:
        assert not core._leases
        elapsed[0] += delay

    monkeypatch.setattr(sqlite_domain_runtime.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(sqlite_domain_runtime.time, "sleep", advance)
    try:
        with pytest.raises(sqlite3.OperationalError, match="authority lease") as caught:
            core.execute("SELECT user_id FROM sessions")
        assert elapsed[0] == pytest.approx(0.025)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert caught.value.bowhead_diagnostic_id
    finally:
        core.close()
        execution.close()


def test_unmanaged_writer_fails_closed(domain_files: Path) -> None:
    with (
        sqlite3.connect(domain_files.with_name(DOMAIN_TARGET_FILES["execution"])) as raw,
        pytest.raises(sqlite3.OperationalError, match="bowhead_domain_(predicate|authority)_v1"),
    ):
        raw.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')")


def test_predicate_sees_own_uncommitted_local_rows(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        # Seed a blocked user before installing the session admission row.
        core.execute("INSERT INTO account_deletions(user_id) VALUES('blocked')").close()
        core.commit()
        execution.execute("BEGIN").close()
        # An existing session can predate the account-erasure request.
        execution.execute("DROP TRIGGER trg_account_deletion_no_session").close()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','blocked')").close()
        with pytest.raises(sqlite3.IntegrityError, match="account is deleted"):
            execution.execute("INSERT INTO turns(id,session_id) VALUES('t','s')")
        assert execution.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
    finally:
        execution.close()
        core.close()


def test_authority_revocation_fenced_but_balance_write_allowed(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        core.execute("INSERT INTO users VALUES('u','active',10)").close()
        core.commit()
        execution.execute("BEGIN IMMEDIATE").close()
        with execution.execute("SELECT status FROM users WHERE id='u'") as cursor:
            cursor.fetchall()
        core.execute("UPDATE users SET balance=balance-1 WHERE id='u'").close()
        core.commit()
        with pytest.raises(sqlite3.OperationalError, match="authority lease") as caught:
            core.execute("UPDATE users SET status='deleted' WHERE id='u'")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        core.rollback()
        execution.commit()
        core.execute("UPDATE users SET status='deleted' WHERE id='u'").close()
        core.commit()
    finally:
        execution.close()
        core.close()


def test_independent_domain_writers_share_gate(domain_files: Path) -> None:
    execution = managed(domain_files, "execution")
    content = managed(domain_files, "content")
    try:
        execution.execute("BEGIN IMMEDIATE").close()
        content.execute("BEGIN IMMEDIATE").close()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        content.execute("INSERT INTO artifacts VALUES('a')").close()
        execution.commit()
        content.commit()
    finally:
        execution.close()
        content.close()


def test_cached_foreign_read_fenced_before_snapshot(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        for _ in range(2):
            core.execute("BEGIN").close()
            with core.execute("SELECT id FROM sessions") as cursor:
                cursor.fetchall()
            assert core._leases.get("execution") == 1
            with pytest.raises(sqlite3.OperationalError, match="authority lease"):
                execution.execute("INSERT INTO sessions(id,user_id) VALUES('blocked','u')").close()
            execution.rollback()
            core.commit()
            execution.execute("BEGIN").close()
            execution.commit()
        # Pool reset cannot silently remove the mandatory authorizer.
        core.set_authorizer(None)
        execution.execute("BEGIN").close()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            core.execute("SELECT id FROM sessions")
        execution.rollback()
    finally:
        execution.close()
        core.close()


def test_autocommit_cursor_holds_lease_until_exhausted(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
        cursor = core.execute("SELECT id FROM sessions")
        assert core._leases.get("execution") == 1
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            execution.execute("INSERT INTO sessions(id,user_id) VALUES('blocked','u')").close()
        execution.rollback()
        assert cursor.fetchall() == [("s",)]
        assert not core._leases
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('allowed','u')").close()
        execution.rollback()
        cursor.close()
    finally:
        core.close()
        execution.close()


def seed_turn(core: DomainConnection, execution: DomainConnection) -> None:
    execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
    execution.execute("INSERT INTO turns VALUES('t','s')").close()
    execution.execute("INSERT INTO turn_cancel_receipts VALUES('t',0)").close()
    execution.commit()
    core.execute("INSERT INTO memory_turn_requests VALUES('t')").close()
    core.commit()


def test_native_local_trigger_write_rolls_back_with_outer_abort(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        seed_turn(core, execution)
        execution.execute("CREATE TRIGGER reject_turn_delete AFTER DELETE ON turns BEGIN SELECT RAISE(ABORT,'reject delete'); END").close()
        with pytest.raises(sqlite3.IntegrityError, match="reject delete"):
            execution.execute("DELETE FROM turns WHERE id='t'")
        assert execution.execute("SELECT turn_erased FROM turn_cancel_receipts").fetchone()[0] == 0
        execution.rollback()
        assert core.execute("SELECT count(*) FROM memory_turn_requests").fetchone()[0] == 1
    finally:
        execution.close()
        core.close()


def test_cleanup_runs_only_after_commit_and_respects_savepoint_rollback(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        seed_turn(core, execution)
        execution.execute("SAVEPOINT deletion").close()
        execution.execute("DELETE FROM turns WHERE id='t'").close()
        execution.execute("ROLLBACK TO deletion").close()
        execution.execute("RELEASE deletion").close()
        assert core.execute("SELECT count(*) FROM memory_turn_requests").fetchone()[0] == 1
        core.commit()
        execution.execute("DELETE FROM turns WHERE id='t'").close()
        assert core.execute("SELECT count(*) FROM memory_turn_requests").fetchone()[0] == 1
        core.commit()
        execution.commit()
        assert core.execute("SELECT count(*) FROM memory_turn_requests").fetchone()[0] == 0
    finally:
        execution.close()
        core.close()


def test_compiler_rejects_drift_and_preserves_simple_native_body() -> None:
    for name, spec in _TRIGGERS.items():
        sql = compile_domain_trigger(name, spec["source"])
        assert "bowhead_domain_" in sql
        with pytest.raises(ValueError, match="Unreviewed"):
            compile_domain_trigger(name, spec["source"].replace("CREATE TRIGGER", "CREATE  TRIGGER", 1))
    for name, spec in _VIEWS.items():
        assert "JOIN workspaces" not in compile_domain_view(name, spec["source"])
    assert compile_domain_trigger("local_trigger", "SELECT 1") == "SELECT 1"


def test_mixed_unrelated_writers_do_not_share_an_exclusive_gate(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    content = managed(domain_files, "content")
    try:
        core.execute("INSERT INTO users VALUES('u','active',10)").close()
        core.commit()
        execution.execute("BEGIN IMMEDIATE").close()
        with execution.execute("SELECT user_id FROM account_deletions") as cursor:
            cursor.fetchall()
        execution.execute("INSERT INTO turn_events VALUES('event','turn')").close()
        core.execute("UPDATE users SET balance=balance-1 WHERE id='u'").close()
        content.execute("INSERT INTO artifacts VALUES('artifact')").close()
        assert execution._leases.get("core") == 1
        assert core._leases.get("core", 0) == 0
        assert content._leases.get("content") == 2
        execution.commit()
        core.commit()
        content.commit()
    finally:
        execution.close()
        core.close()
        content.close()


def test_content_purge_cannot_race_attachment_admission(domain_files: Path) -> None:
    execution = managed(domain_files, "execution")
    content = managed(domain_files, "content")
    try:
        execution.execute("INSERT INTO turn_attachments VALUES('artifact')").close()
        assert execution._leases.get("content") == 1
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            content.execute("INSERT INTO artifact_purge_claims VALUES('artifact')")
        content.rollback()
        execution.rollback()
        content.execute("INSERT INTO artifact_purge_claims VALUES('artifact')").close()
        content.commit()
        with pytest.raises(sqlite3.IntegrityError):
            execution.execute("INSERT INTO turn_attachments VALUES('artifact')")
    finally:
        execution.close()
        content.close()


def test_authority_gate_applies_across_processes(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        core.execute("INSERT INTO users VALUES('u','active',10)").close()
        core.commit()
        execution.execute("BEGIN").close()
        with execution.execute("SELECT status FROM users") as cursor:
            cursor.fetchall()
        script = """
import sqlite3,sys
from pathlib import Path
from bowhead_core.sqlite_domain_runtime import DomainConnection,guard_connection
path=Path(sys.argv[1])
db=sqlite3.connect(path,factory=DomainConnection,uri=True,timeout=0)
guard_connection(db,path,'core')
try:
    db.execute("UPDATE users SET status='deleted' WHERE id='u'")
except sqlite3.OperationalError as error:
    print(error.sqlite_errorcode)
finally:
    db.close()
"""
        result = subprocess.run([sys.executable, "-c", script, str(domain_files)], check=True, capture_output=True, text=True, timeout=10)
        assert result.stdout.strip().endswith(str(sqlite3.SQLITE_BUSY))
        assert core.execute("SELECT status FROM users").fetchone()[0] == "active"
    finally:
        execution.close()
        core.close()


def test_custom_cursor_cannot_disable_guard_hooks(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="DomainCursor"):
            core.cursor(factory=sqlite3.Cursor)
    finally:
        core.close()


@pytest.mark.asyncio
async def test_encoded_file_uri_retains_domain_guards_and_read_only_mode(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.sqlite_domain_runtime import connect
    from bowhead_core.sqlite_domains import DOMAIN_LAYOUT

    from bowhead_core import sqlite_domains

    directory = domain_files.parent / "encoded space"
    directory.mkdir()
    for filename in DOMAIN_TARGET_FILES.values():
        source = domain_files.with_name(filename)
        if source.exists():
            original = sqlite3.connect(source)
            copy = sqlite3.connect(directory / filename)
            try:
                original.backup(copy)
            finally:
                original.close()
                copy.close()
    (directory / "sqlite-layout.json").write_text(json.dumps({"format": 1, "layout": DOMAIN_LAYOUT, "status": "ready"}))
    # Isolate URI decoding from coordinator state verification, which has its own tests.
    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda path: Path(path).parent == directory)
    uri = (directory / "bowhead.db").as_uri() + "?mode=ro"
    assert "%20" in uri
    async with connect(uri, uri=True) as connection:
        async with connection.execute("SELECT id FROM sessions") as cursor:
            assert await cursor.fetchall() == []
        assert await connection._execute(lambda: isinstance(connection._conn, DomainConnection))
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await connection.execute("INSERT INTO users(id,status) VALUES('u','active')")


def test_factory_rejects_unfinished_domain_layout(domain_files: Path) -> None:
    from bowhead_core.sqlite_domain_runtime import connect
    from bowhead_core.sqlite_domains import DOMAIN_LAYOUT

    (domain_files.parent / "sqlite-layout.json").write_text(json.dumps({"format": 1, "layout": DOMAIN_LAYOUT, "status": "copying"}))
    with pytest.raises(RuntimeError):
        connect(domain_files)


def test_guard_udf_contention_preserves_busy_code_and_diagnostic_reference(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from bowhead_core import sqlite_diagnostics

    events = []
    monkeypatch.setattr(sqlite_diagnostics, "log", SimpleNamespace(error=lambda _event, **fields: events.append(fields)))
    execution = managed(domain_files, "execution")
    content = managed(domain_files, "content")
    try:
        content.execute("INSERT INTO artifact_purge_claims VALUES('artifact')").close()
        with pytest.raises(sqlite3.OperationalError) as caught:
            execution.execute("INSERT INTO turn_attachments VALUES('artifact')")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert caught.value.bowhead_diagnostic_id
        assert "test_sqlite_domain_runtime.py:" in events[-1]["callsite"]
        assert "test_guard_udf_contention" in events[-1]["callsite"]
    finally:
        execution.close()
        content.close()


def test_pool_reset_closes_live_read_cursors_before_releasing_fences(domain_files: Path) -> None:
    from bowhead_core.sqlite_domain_runtime import reset_domain_connection

    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
        cursor = core.execute("SELECT id FROM sessions")
        assert core._leases.get("execution") == 1
        reset_domain_connection(core)
        assert not core._leases
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s2','u')").close()
        execution.commit()
    finally:
        core.close()
        execution.close()


@pytest.mark.parametrize(
    ("domain", "sql"),
    [
        ("core", "INSERT INTO users(id,status) VALUES('u','active')"),
        ("content", "INSERT INTO artifact_purge_claims VALUES('artifact')"),
    ],
)
def test_unmanaged_authority_writes_fail_closed(domain_files: Path, domain: str, sql: str) -> None:
    with (
        sqlite3.connect(domain_files.with_name(DOMAIN_TARGET_FILES[domain])) as raw,
        pytest.raises(sqlite3.OperationalError, match="bowhead_domain_authority_v1"),
    ):
        raw.execute(sql)


def test_managed_authority_fence_does_not_cover_credit_balance(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    try:
        core.execute("INSERT INTO users VALUES('u','active',10)").close()
        core.commit()
    finally:
        core.close()
    with sqlite3.connect(domain_files) as raw:
        # This ordinary write does not affect any security predicate.
        raw.execute("UPDATE users SET balance=9 WHERE id='u'")
        with pytest.raises(sqlite3.OperationalError, match="bowhead_domain_authority_v1"):
            raw.execute("UPDATE users SET status='deleted' WHERE id='u'")


def test_sync_factory_registers_authority_guards_and_readonly_uri(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.sqlite_domain_runtime import connect_sync

    from bowhead_core import sqlite_domains

    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    connection = connect_sync(domain_files)
    try:
        assert isinstance(connection, DomainConnection)
        connection.execute("INSERT INTO users VALUES('sync-user','active',10)").close()
        connection.commit()
        with connection.execute("SELECT id FROM sessions") as cursor:
            assert cursor.fetchall() == []
    finally:
        connection.close()
    readonly = connect_sync(domain_files.as_uri() + "?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            readonly.execute("UPDATE users SET status='deleted' WHERE id='sync-user'")
    finally:
        readonly.close()


def test_explicit_snapshot_release_preserves_callbacks_and_diagnostics(domain_files: Path) -> None:
    from bowhead_core.sqlite_domain_runtime import release_domain_snapshots

    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()

        def callback(*_args):
            return sqlite3.SQLITE_OK

        core.set_authorizer(callback)
        cursor = core.execute("SELECT id FROM sessions")
        core.commit()
        assert core._leases.get("execution") == 1
        original_sql = core.bowhead_recent_sql
        release_domain_snapshots(core)
        assert core._external_authorizer is callback
        assert core.bowhead_recent_sql == original_sql
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s2','u')").close()
        execution.commit()
        core.execute("BEGIN").close()
        with pytest.raises(sqlite3.ProgrammingError, match="inside a transaction"):
            release_domain_snapshots(core)
        core.rollback()
    finally:
        execution.close()
        core.close()


def test_session_cleanup_removes_chunks_for_previously_orphaned_turns(domain_files: Path) -> None:
    execution = managed(domain_files, "execution")
    observability = managed(domain_files, "observability")
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
        # Simulate source commit followed by a crash before ordinary cleanup.
        observability.execute("INSERT INTO llm_request_records VALUES('old-turn','s')").close()
        observability.execute("INSERT INTO llm_request_chunks VALUES('old-turn')").close()
        observability.commit()
        execution.execute("UPDATE sessions SET deleted_at='deleted' WHERE id='s'").close()
        execution.commit()
        with observability.execute("SELECT count(*) FROM llm_request_records") as cursor:
            assert cursor.fetchone()[0] == 0
        with observability.execute("SELECT count(*) FROM llm_request_chunks") as cursor:
            assert cursor.fetchone()[0] == 0
    finally:
        execution.close()
        observability.close()


def test_admitted_sync_open_cannot_recreate_missing_domain(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.sqlite_domain_runtime import connect_sync

    from bowhead_core import sqlite_domains

    # Simulate a cached ready-state record followed by deletion before open.
    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    missing = domain_files.with_name(DOMAIN_TARGET_FILES["execution"])
    missing.unlink()
    with pytest.raises(sqlite3.OperationalError) as caught:
        connect_sync(missing)
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN
    assert not missing.exists()


@pytest.mark.parametrize("mode", ["rwc", "memory"])
def test_admitted_domain_uri_rejects_creating_modes(domain_files: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    from bowhead_core.sqlite_domain_runtime import connect_sync

    from bowhead_core import sqlite_domains

    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    with pytest.raises(ValueError, match="mode=ro or mode=rw"):
        connect_sync(domain_files.as_uri() + "?mode=" + mode, uri=True)


def test_builtin_override_disable_is_fenced_for_execution_snapshot(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        core.execute("INSERT INTO team_builtin_subagent_override VALUES('team','compute',1,1)").close()
        core.commit()
        execution.execute("BEGIN IMMEDIATE").close()
        with execution.execute("SELECT enabled FROM team_builtin_subagent_override WHERE team_id='team'") as cursor:
            assert cursor.fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError) as caught:
            core.execute("UPDATE team_builtin_subagent_override SET enabled=0,row_version=2 WHERE team_id='team'")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        core.rollback()
        execution.rollback()
        core.execute("UPDATE team_builtin_subagent_override SET enabled=0,row_version=2 WHERE team_id='team'").close()
        core.commit()
        with sqlite3.connect(domain_files) as raw, pytest.raises(sqlite3.OperationalError, match="bowhead_domain_authority_v1"):
            raw.execute("UPDATE team_builtin_subagent_override SET enabled=1")
    finally:
        execution.close()
        core.close()


def test_observability_scope_guards_validate_owner_and_rollback_partial_payloads(domain_files: Path) -> None:
    from bowhead_core.sqlite_domain_runtime import domain_additional_triggers

    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    observability = managed(domain_files, "observability")
    try:
        core.execute("INSERT INTO users VALUES('u','active',10)").close()
        core.execute("INSERT INTO workspaces VALUES('ws','team')").close()
        core.commit()
        execution.execute("INSERT INTO sessions(id,user_id,workspace_id) VALUES('s','u','ws')").close()
        execution.execute("INSERT INTO turns VALUES('t','s')").close()
        execution.commit()
        for column in ("id", "user_id", "workspace_id", "team_id"):
            observability.execute(f'ALTER TABLE llm_request_records ADD COLUMN "{column}" TEXT').close()
        observability.execute("CREATE TABLE llm_request_changes(id TEXT,record_id TEXT)").close()
        for statement in domain_additional_triggers("observability"):
            observability.execute(statement).close()
        insert = "INSERT INTO llm_request_records(id,turn_id,session_id,user_id,workspace_id,team_id) VALUES(?,?,?,?,?,?)"
        observability.execute(insert, ("record", "t", "s", "u", "ws", "team")).close()
        observability.execute("INSERT INTO llm_request_chunks VALUES('t')").close()
        observability.execute("INSERT INTO llm_request_changes VALUES('change','record')").close()
        # A later malformed statement must discard the earlier ciphertext batch.
        with pytest.raises(sqlite3.IntegrityError, match="source is no longer available"):
            observability.execute(insert, ("wrong-owner", "t", "s", "other-user", "ws", "team"))
        assert not observability.in_transaction
        with observability.execute("SELECT count(*) FROM llm_request_chunks") as cursor:
            assert cursor.fetchone()[0] == 0
        observability.execute(insert, ("record", "t", "s", "u", "ws", "team")).close()
        observability.execute("INSERT INTO llm_request_chunks VALUES('t')").close()
        with pytest.raises(sqlite3.OperationalError):
            core.execute("INSERT INTO account_deletions(user_id) VALUES('u')")
        core.rollback()
        observability.commit()
        core.execute("INSERT INTO account_deletions(user_id) VALUES('u')").close()
        core.commit()
        for sql, parameters in (
            (insert, ("late", "t", "s", "u", "ws", "team")),
            ("INSERT INTO llm_request_chunks VALUES(?)", ("t",)),
            ("INSERT INTO llm_request_changes VALUES(?,?)", ("late-change", "record")),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="source is no longer available"):
                observability.execute(sql, parameters)
            assert not observability.in_transaction
        with (
            sqlite3.connect(domain_files.with_name(DOMAIN_TARGET_FILES["observability"])) as raw,
            pytest.raises(sqlite3.OperationalError, match="bowhead_domain_request_turn_scope_v1"),
        ):
            raw.execute("INSERT INTO llm_request_chunks VALUES('t')")
    finally:
        observability.close()
        execution.close()
        core.close()


def test_authorizer_reset_reclaims_profiles_and_reprepares_security_checks(domain_files: Path) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        for number in range(30):
            with core.execute(f"SELECT id FROM sessions -- unique-statement-{number}") as cursor:
                cursor.fetchall()
        assert len(core._profiles) >= 30
        core.set_authorizer(None)
        assert not core._profiles
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        with pytest.raises(sqlite3.OperationalError) as caught:
            core.execute("SELECT id FROM sessions -- unique-statement-0")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    finally:
        execution.close()
        core.close()


@pytest.mark.parametrize(
    ("table", "column", "changed"),
    [
        ("custom_subagents", "status", "disabled"),
        ("custom_subagents", "system_prompt", "Updated private instructions"),
        ("skill_instances", "import_status", "pending"),
        ("skill_instances", "import_report_json", '{"review":"updated"}'),
    ],
)
def test_legacy_optional_agent_and_skill_import_authorities_are_fenced(domain_files: Path, table: str, column: str, changed: str) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        core.execute(
            "INSERT INTO custom_subagents(id,user_id,status,system_prompt) VALUES('item','u','active','Original private instructions')"
        ).close()
        core.execute("INSERT INTO skill_instances VALUES('item','u',1,'cleared','{}')").close()
        core.commit()
        execution.execute("BEGIN").close()
        with execution.execute(f'SELECT "{column}" FROM "{table}" WHERE id=?', ("item",)) as cursor:
            original = cursor.fetchone()[0]
        with pytest.raises(sqlite3.OperationalError) as caught:
            core.execute(f'UPDATE "{table}" SET "{column}"=? WHERE id=?', (changed, "item"))
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        core.rollback()
        execution.rollback()
        core.execute(f'UPDATE "{table}" SET "{column}"=? WHERE id=?', (changed, "item")).close()
        core.commit()
        with core.execute(f'SELECT "{column}" FROM "{table}" WHERE id=?', ("item",)) as cursor:
            assert cursor.fetchone()[0] == changed
        with sqlite3.connect(domain_files) as raw, pytest.raises(sqlite3.OperationalError, match="bowhead_domain_authority_v1"):
            raw.execute(f'UPDATE "{table}" SET "{column}"=? WHERE id=?', (original, "item"))
    finally:
        execution.close()
        core.close()


@pytest.mark.parametrize("target", ["core", "observability"])
def test_deferred_cleanup_cannot_recreate_missing_target(domain_files: Path, target: str) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        seed_turn(core, execution)
        missing = domain_files.with_name(DOMAIN_TARGET_FILES[target])
        missing.unlink()
        execution.execute("DELETE FROM turns WHERE id='t'").close()
        execution.commit()
        assert not missing.exists(), "Ordinary deferred cleanup must never recreate a missing managed database"
        assert execution.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
    finally:
        execution.close()
        core.close()


@pytest.mark.parametrize("pending", [False, True])
def test_collected_foreign_cursor_releases_its_native_snapshot(domain_files: Path, pending: bool) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        execution.executemany("INSERT INTO sessions(id,user_id) VALUES(?,?)", [("s1", "u"), ("s2", "u")]).close()
        execution.commit()
        cursor = core.execute("SELECT id FROM sessions ORDER BY id" if pending else "SELECT id FROM sessions WHERE id='s1'")
        assert cursor.fetchone() == ("s1",)
        del cursor
        gc.collect()
        execution.execute("UPDATE sessions SET user_id='other'").close()
        execution.commit()
        with sqlite3.connect(domain_files.with_name(DOMAIN_TARGET_FILES["execution"])) as raw:
            assert raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        core.close()
        execution.close()


@pytest.mark.parametrize("boundary", ["transaction", "other_cursor"])
def test_collected_cursor_preserves_other_live_authority_boundaries(domain_files: Path, boundary: str) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
        if boundary == "transaction":
            core.execute("BEGIN").close()
        else:
            retained = core.execute("SELECT id FROM sessions")
        abandoned = core.execute("SELECT id FROM sessions")
        del abandoned
        gc.collect()
        with pytest.raises(sqlite3.OperationalError) as caught:
            execution.execute("UPDATE sessions SET user_id='other'")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        execution.rollback()
        if boundary == "transaction":
            core.rollback()
        else:
            retained.close()
        execution.execute("UPDATE sessions SET user_id='other'").close()
        execution.commit()
    finally:
        core.close()
        execution.close()


@pytest.mark.parametrize("target", ["cursor", "connection"])
def test_failed_foreign_thread_close_preserves_live_authority_lease(domain_files: Path, target: str) -> None:
    core = managed(domain_files, "core")
    execution = managed(domain_files, "execution")
    errors: list[BaseException] = []
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
        cursor = core.execute("SELECT id FROM sessions")

        def close() -> None:
            try:
                (cursor if target == "cursor" else core).close()
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=close)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], sqlite3.ProgrammingError)
        with pytest.raises(sqlite3.OperationalError):
            execution.execute("UPDATE sessions SET user_id='other'")
        execution.rollback()
        assert cursor.fetchall() == [("s",)]
        cursor.close()
        execution.execute("UPDATE sessions SET user_id='other'").close()
        execution.commit()
    finally:
        core.close()
        execution.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["loop", "worker", "cycle"])
async def test_async_collected_cursor_closes_on_its_owner_worker(domain_files: Path, monkeypatch: pytest.MonkeyPatch, collection: str) -> None:
    from bowhead_core.sqlite_domain_runtime import connect

    from bowhead_core import sqlite_domains

    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    execution = managed(domain_files, "execution")
    execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
    execution.commit()
    released = asyncio.Event()
    loop = asyncio.get_running_loop()
    owner_threads: list[int] = []
    original_settle = DomainConnection._settle
    try:
        async with connect(domain_files) as connection:
            raw = await connection._execute(lambda: connection._conn)
            owner_thread = await connection._execute(threading.get_ident)
            cursor = await connection._execute(raw.execute, "SELECT id FROM sessions")
            # Drop the aiosqlite worker's last result reference before collecting on the loop.
            await connection._execute(lambda: None)

            def observe_settle(self: DomainConnection) -> None:
                original_settle(self)
                if self is raw and not self._leases:
                    owner_threads.append(threading.get_ident())
                    loop.call_soon_threadsafe(released.set)

            monkeypatch.setattr(DomainConnection, "_settle", observe_settle)
            if collection == "worker":
                holder = [cursor]
                del cursor
                await connection._execute(holder.clear)
            else:
                if collection == "cycle":
                    cursor.review_cycle = cursor
                del cursor
                gc.collect()
            await asyncio.wait_for(released.wait(), timeout=2)
            assert owner_threads and all(identity == owner_thread for identity in owner_threads)
            execution.execute("UPDATE sessions SET user_id='other'").close()
            execution.commit()
    finally:
        execution.close()


@pytest.mark.asyncio
async def test_collected_cycle_keeps_its_fence_until_deferred_native_close(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.sqlite_domain_runtime import connect

    from bowhead_core import sqlite_domains

    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    execution = managed(domain_files, "execution")
    pending: list[sqlite3.Cursor] = []
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
        async with connect(domain_files) as connection:
            raw = await connection._execute(lambda: connection._conn)
            raw._defer_cursor_close = pending.append
            cursor = await connection._execute(raw.execute, "SELECT id FROM sessions")
            await connection._execute(lambda: None)
            cursor.review_cycle = cursor
            del cursor
            gc.collect()
            assert len(pending) == 1

            def probe() -> None:
                with raw.execute("SELECT 1") as local:
                    local.fetchall()

            await connection._execute(probe)
            with pytest.raises(sqlite3.OperationalError):
                execution.execute("UPDATE sessions SET user_id='other'")
            execution.rollback()
            await connection._execute(pending.pop().close)
            execution.execute("UPDATE sessions SET user_id='other'").close()
            execution.commit()
    finally:
        execution.close()


@pytest.mark.asyncio
async def test_deferred_cursor_close_failure_preserves_authority_fence(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from bowhead_core.sqlite_domain_runtime import DomainCursor, connect

    from bowhead_core import sqlite_domain_runtime, sqlite_domains

    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    warning = Mock()
    monkeypatch.setattr(sqlite_domain_runtime._LOG, "warning", warning)
    execution = managed(domain_files, "execution")
    try:
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
        async with connect(domain_files) as connection:
            cursor = await connection.execute("SELECT id FROM sessions")
            original_close = DomainCursor.close

            def failed_close(self: DomainCursor) -> None:
                if self is cursor._cursor:
                    raise sqlite3.ProgrammingError("Synthetic native close failure")
                original_close(self)

            with monkeypatch.context() as patch:
                patch.setattr(DomainCursor, "close", failed_close)
                await connection._close_abandoned_cursor(cursor._cursor)
            warning.assert_called_once_with("sqlite.cursor_cleanup_deferred", exception_type="ProgrammingError", diagnostic_id=None)
            assert not getattr(cursor._cursor, "_domain_closed", False)
            with pytest.raises(sqlite3.OperationalError) as caught:
                execution.execute("UPDATE sessions SET user_id='other'")
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            execution.rollback()
            assert await cursor.fetchall() == [("s",)]
            await cursor.close()
            execution.execute("UPDATE sessions SET user_id='other'").close()
            execution.commit()
    finally:
        execution.close()
