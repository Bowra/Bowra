"""Real contention diagnostics preserve errors without exporting database values."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
import structlog

from bowhead_core import sqlite_diagnostics as diagnostics


class CapturedLog:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def error(self, event: str, **fields: object) -> None:
        assert not structlog.contextvars.get_contextvars(), "Diagnostic emission must isolate unrelated request values"
        self.events.append({"event": event, **fields})


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> CapturedLog:
    result = CapturedLog()
    monkeypatch.setattr(diagnostics, "log", result)
    return result


def database(path: Path) -> diagnostics.DiagnosticConnection:
    connection = sqlite3.connect(path, timeout=0.01, factory=diagnostics.DiagnosticConnection)
    connection.execute("CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY)")
    connection.execute("INSERT OR IGNORE INTO users VALUES('first')")
    connection.execute("INSERT OR IGNORE INTO users VALUES('second')")
    connection.commit()
    return connection


def test_sql_redaction_handles_literals_comments_and_dynamic_identifiers() -> None:
    sql = """SELECT id, 'private''literal', 1.3e-8, X'736563726574', :private_parameter
        FROM main."users" JOIN "private_table" ON users.id="private_column"
        WHERE password='private-token' -- private_comment
        AND id IN (SELECT id FROM users /* private_block */)"""
    result = diagnostics.summarize_statement(sql)
    assert result.template is not None
    assert "private" not in result.template
    assert "736563726574" not in result.template
    assert result.tables == ("users",)
    assert result.fingerprint
    assert (
        diagnostics.summarize_statement("SELECT id FROM users WHERE id='alice'").fingerprint
        == diagnostics.summarize_statement("SELECT id FROM users WHERE id='bob'").fingerprint
    )


@pytest.mark.parametrize("sql", ["SELECT 'unterminated_secret", "SELECT /* unterminated_secret", "SELECT " + "private" * 4000])
def test_unsafe_or_oversized_sql_is_omitted(sql: str) -> None:
    result = diagnostics.summarize_statement(sql)
    assert result.template is None
    assert result.fingerprint is None
    assert result.reason


def test_same_file_contention_reports_table_without_values(tmp_path: Path, captured: CapturedLog) -> None:
    path = tmp_path / "bowhead.db"
    owner = database(path)
    contender = sqlite3.connect(path, timeout=0.01, factory=diagnostics.DiagnosticConnection)
    owner.execute("PRAGMA journal_mode=WAL")
    owner.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError) as failure:
            contender.execute("INSERT INTO users(id) VALUES(?)", ("private-parameter-token",))
        assert str(failure.value) == "database is locked"
        assert failure.value.bowhead_diagnostic_id
        event = captured.events[-1]
        assert event["database"] == "bowhead.db"
        assert event["tables"] == ("users",)
        assert event["sqlite_primary_errorcode"] == sqlite3.SQLITE_BUSY
        assert event["sqlite_errorname"] == "SQLITE_BUSY"
        assert event["phase"] == "execute"
        assert event["busy_timeout_ms"] == 10
        assert "private-parameter-token" not in str(event)
        assert str(tmp_path) not in str(event)
        assert "test_same_file_contention" in str(event["callsite"])
    finally:
        owner.rollback()
        owner.close()
        contender.close()


def test_begin_failure_does_not_claim_a_table_is_locked(tmp_path: Path, captured: CapturedLog) -> None:
    path = tmp_path / "bowhead.db"
    owner = database(path)
    contender = sqlite3.connect(path, timeout=0.01, factory=diagnostics.DiagnosticConnection)
    owner.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            contender.execute("BEGIN IMMEDIATE;")
        event = captured.events[-1]
        assert event["phase"] == "begin"
        assert event["tables"] == ()
        assert event["transaction_id"]
        assert not event["in_transaction"]
    finally:
        owner.rollback()
        owner.close()
        contender.close()


def test_other_database_can_commit_while_first_writer_is_held(tmp_path: Path, captured: CapturedLog) -> None:
    owner = database(tmp_path / "bowhead.db")
    independent = database(tmp_path / "bowhead_execution.db")
    owner.execute("BEGIN IMMEDIATE")
    try:
        independent.execute("INSERT INTO users VALUES('independent')")
        independent.commit()
        assert independent.execute("SELECT COUNT(*) FROM users").fetchone() == (3,)
        assert not captured.events
    finally:
        owner.rollback()
        owner.close()
        independent.close()


def test_commit_contention_identifies_completed_statement_and_open_transaction(tmp_path: Path, captured: CapturedLog) -> None:
    path = tmp_path / "bowhead.db"
    reader = database(path)
    writer = sqlite3.connect(path, timeout=0.01, factory=diagnostics.DiagnosticConnection)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM users").fetchall()
    try:
        writer.execute("INSERT INTO users VALUES('third')")
        with pytest.raises(sqlite3.OperationalError):
            writer.commit()
        event = captured.events[-1]
        assert event["phase"] == "commit"
        assert event["transaction_mode"] == "implicit"
        assert event["transaction_id"]
        assert event["in_transaction"]
        assert event["statement_completed"]
        assert "third" not in str(event)
    finally:
        reader.rollback()
        writer.rollback()
        reader.close()
        writer.close()


def test_fetch_failure_keeps_its_own_cursor_statement(tmp_path: Path, captured: CapturedLog) -> None:
    connection = database(tmp_path / "bowhead.db")

    def fail() -> int:
        raise ValueError("private-result")

    connection.create_function("fail", 0, fail)
    try:
        cursor = connection.execute("SELECT id FROM users UNION ALL SELECT fail()")
        connection.execute("SELECT 42")
        with pytest.raises(sqlite3.OperationalError):
            cursor.fetchall()
        event = captured.events[-1]
        assert event["phase"] == "fetch"
        assert event["tables"] == ("users",)
        assert "private-result" not in str(event)
    finally:
        connection.close()


async def test_async_worker_preserves_only_safe_request_correlation(tmp_path: Path, captured: CapturedLog) -> None:
    path = tmp_path / "bowhead.db"
    owner = database(path)
    owner.execute("BEGIN IMMEDIATE")
    tokens = structlog.contextvars.bind_contextvars(request_id="request-safe", private_user_content="private-context")
    try:
        async with diagnostics.connect(path, timeout=0.01) as contender:
            with pytest.raises(sqlite3.OperationalError):
                await contender.execute("INSERT INTO users VALUES(?)", ("private-bind",))
        event = captured.events[-1]
        assert event["request_id"] == "request-safe"
        assert "test_async_worker" in str(event["callsite"])
        assert "private-context" not in str(event)
        assert "private-bind" not in str(event)
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
        owner.rollback()
        owner.close()


async def test_connection_failure_omits_customer_path(tmp_path: Path, captured: CapturedLog) -> None:
    with pytest.raises(sqlite3.OperationalError) as failure:
        async with diagnostics.connect(tmp_path / "private-customer" / "bowhead.db"):
            pytest.fail("Missing parent must not connect")
    assert failure.value.bowhead_diagnostic_id
    assert captured.events[-1]["phase"] == "connect"
    assert "private-customer" not in str(captured.events)


def test_logging_failure_does_not_replace_sqlite_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenLog:
        def error(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("logging failed")

    monkeypatch.setattr(diagnostics, "log", BrokenLog())
    connection = database(tmp_path / "bowhead.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            connection.execute("SELECT * FROM missing_table")
    finally:
        connection.close()


def test_redaction_failure_emits_a_safe_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: CapturedLog) -> None:
    def broken(_sql: str | None) -> diagnostics.StatementSummary:
        raise ValueError("private-parser-diagnostic")

    monkeypatch.setattr(diagnostics, "summarize_statement", broken)
    connection = database(tmp_path / "bowhead.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT 'private-literal' FROM missing_table")
        assert captured.events[-1]["sql_unavailable_reason"] == "redaction_failed"
        assert captured.events[-1]["sql_template"] is None
        assert "private" not in str(captured.events)
    finally:
        connection.close()


def test_expected_integrity_failure_does_not_create_error_noise(tmp_path: Path, captured: CapturedLog) -> None:
    connection = database(tmp_path / "bowhead.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO users VALUES('first')")
        assert not captured.events
    finally:
        connection.close()


def test_connection_release_clears_previous_statement_context(tmp_path: Path) -> None:
    connection = database(tmp_path / "bowhead.db")
    try:
        connection.execute("SELECT 'private-value' FROM users").fetchall()
        diagnostics.clear_diagnostics(connection)
        snapshot = diagnostics.diagnostic_snapshot(connection)
        assert snapshot["sql_template"] is None
        assert snapshot["tables"] == ()
        assert snapshot["transaction_id"] is None
    finally:
        connection.close()


def test_one_exception_is_reported_only_once(tmp_path: Path, captured: CapturedLog) -> None:
    connection = database(tmp_path / "bowhead.db")
    failure = sqlite3.OperationalError("private-exception")
    try:
        connection._report(failure, "execute", time.monotonic())
        connection._report(failure, "execute", time.monotonic())
        assert len(captured.events) == 1
        assert captured.events[0]["sqlite_errorcode"] is None
        assert "private-exception" not in str(captured.events)
    finally:
        connection.close()


def test_busy_snapshot_keeps_extended_error_code(tmp_path: Path, captured: CapturedLog) -> None:
    path = tmp_path / "bowhead.db"
    reader = database(path)
    reader.execute("PRAGMA journal_mode=WAL")
    writer = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT id FROM users").fetchall()
    try:
        writer.execute("INSERT INTO users VALUES('writer-committed')")
        writer.commit()
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO users VALUES('stale-snapshot')")
        event = captured.events[-1]
        assert event["sqlite_primary_errorcode"] == sqlite3.SQLITE_BUSY
        assert event["sqlite_errorcode"] == sqlite3.SQLITE_BUSY_SNAPSHOT
        assert event["sqlite_errorname"] == "SQLITE_BUSY_SNAPSHOT"
        assert "stale-snapshot" not in str(event)
    finally:
        reader.rollback()
        reader.close()
        writer.close()


def test_redaction_failure_does_not_prevent_a_successful_begin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = database(tmp_path / "bowhead.db")

    def broken(_sql: str | None) -> diagnostics.StatementSummary:
        raise ValueError("redaction failed")

    monkeypatch.setattr(diagnostics, "summarize_statement", broken)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert connection.in_transaction
        connection.rollback()
    finally:
        connection.close()


def test_registered_authority_table_is_attributed_during_contention(tmp_path: Path, captured: CapturedLog) -> None:
    path = tmp_path / "bowhead.db"
    owner = sqlite3.connect(path, timeout=0.01, factory=diagnostics.DiagnosticConnection)
    owner.execute("CREATE TABLE account_deletions(user_id TEXT)").close()
    owner.commit()
    contender = sqlite3.connect(path, timeout=0.01, factory=diagnostics.DiagnosticConnection)
    try:
        owner.execute("BEGIN IMMEDIATE").close()
        with pytest.raises(sqlite3.OperationalError):
            contender.execute("UPDATE account_deletions SET user_id=?", ("private-user-binding",))
        event = captured.events[-1]
        assert event["tables"] == ("account_deletions",)
        assert "account_deletions" in event["sql_template"]
        assert "private-user-binding" not in str(event)
    finally:
        owner.rollback()
        owner.close()
        contender.close()
