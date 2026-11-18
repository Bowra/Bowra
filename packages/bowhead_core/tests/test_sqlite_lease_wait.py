"""A statement that waits for a cross-file authority lease is visible in the logs."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from bowhead_core.sqlite_domain_runtime import DomainConnection
from test_sqlite_domain_runtime import domain_files as domain_files
from test_sqlite_domain_runtime import managed

from bowhead_core import sqlite_lease_wait


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append({"event": event, **fields})

    def named(self, event: str) -> list[dict[str, object]]:
        return [entry for entry in self.events if entry["event"] == event]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    recorder = _Recorder()
    monkeypatch.setattr(sqlite_lease_wait, "log", recorder)
    sqlite_lease_wait.flush_summary()
    recorder.events.clear()
    yield recorder
    sqlite_lease_wait.flush_summary()


def _seed(core_path: Path) -> None:
    core = managed(core_path, "core")
    core.execute("INSERT INTO users(id,status,balance) VALUES('u1','active',0)").close()
    core.commit()
    core.close()


def _hold_core_exclusively(core_path: Path, seconds: float, held: threading.Event, errors: list[BaseException]) -> None:
    writer = None
    try:
        writer = managed(core_path, "core")
        # A protected column write takes core's lease exclusively until commit.
        writer.execute("UPDATE users SET status='changed' WHERE id='u1'").close()
        held.set()
        time.sleep(seconds)
        writer.commit()
    except BaseException as error:
        errors.append(error)
        held.set()
    finally:
        if writer is not None:
            writer.close()


def _read_core_from(reader: DomainConnection) -> list[tuple[str]]:
    with reader.execute("SELECT id FROM users") as cursor:
        return cursor.fetchall()


def _contended_read(core_path: Path, *, hold_seconds: float, busy_timeout_ms: int) -> tuple[list[tuple[str]] | BaseException, list[BaseException]]:
    # Opened before the holder: fixture connections use timeout=0, so two concurrent opens can meet WAL recovery.
    reader = managed(core_path, "content")
    reader.execute(f"PRAGMA busy_timeout={busy_timeout_ms}").close()
    held, errors = threading.Event(), []
    worker = threading.Thread(target=_hold_core_exclusively, args=(core_path, hold_seconds, held, errors))
    worker.start()
    try:
        assert held.wait(5) and not errors, errors
        try:
            outcome: list[tuple[str]] | BaseException = _read_core_from(reader)
        except sqlite3.OperationalError as error:
            outcome = error
    finally:
        reader.close()
        worker.join(5)
    return outcome, errors


def test_a_read_that_waits_for_the_lease_names_its_wait_and_the_holder(domain_files: Path, recorded: _Recorder) -> None:
    _seed(domain_files)
    rows, errors = _contended_read(domain_files, hold_seconds=0.2, busy_timeout_ms=5000)
    assert rows == [("u1",)] and not errors
    [event] = recorded.named("sqlite.lease_wait")
    assert event["lease_domain"] == "core" and event["lease_mode"] == "shared"
    assert event["outcome"] == "proceeded"
    assert isinstance(event["waited_ms"], float) and event["waited_ms"] >= 100
    assert isinstance(event["attempts"], int) and event["attempts"] >= 1
    assert event["operation"] == "select" and event["database"] == "bowhead_content.db"
    assert "_read_core_from" in str(event["callsite"])
    assert event["holder_known"] is True
    assert "_hold_core_exclusively" in str(event["holder_callsite"])
    assert isinstance(event["holder_held_ms"], float) and event["holder_held_ms"] >= 0
    # The holder is forgotten once it commits.
    assert sqlite_lease_wait.holder_of("core") is None


def test_a_statement_that_did_not_wait_writes_nothing(domain_files: Path, recorded: _Recorder) -> None:
    _seed(domain_files)
    reader = managed(domain_files, "content")
    try:
        assert _read_core_from(reader) == [("u1",)]
    finally:
        reader.close()
    sqlite_lease_wait.flush_summary()
    assert recorded.events == []


def test_a_short_wait_appears_only_in_the_summary(domain_files: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_lease_wait, "LOG_THRESHOLD_MS", 60_000.0)
    _seed(domain_files)
    rows, errors = _contended_read(domain_files, hold_seconds=0.05, busy_timeout_ms=5000)
    assert rows == [("u1",)] and not errors
    assert recorded.named("sqlite.lease_wait") == []
    sqlite_lease_wait.flush_summary()
    [summary] = recorded.named("sqlite.lease_wait_summary")
    assert summary["lease_domain"] == "core" and "_read_core_from" in str(summary["callsite"])
    assert summary["count"] == 1 and summary["timeouts"] == 0
    assert isinstance(summary["total_ms"], float) and summary["total_ms"] > 0
    assert summary["max_ms"] == summary["total_ms"]


def test_a_wait_that_runs_out_is_logged_and_counted(domain_files: Path, recorded: _Recorder) -> None:
    _seed(domain_files)
    outcome, errors = _contended_read(domain_files, hold_seconds=0.3, busy_timeout_ms=30)
    assert isinstance(outcome, sqlite3.OperationalError) and "authority lease" in str(outcome)
    assert not errors
    [event] = recorded.named("sqlite.lease_wait")
    assert event["outcome"] == "timed_out" and event["busy_timeout_ms"] == 30
    sqlite_lease_wait.flush_summary()
    [summary] = recorded.named("sqlite.lease_wait_summary")
    assert summary["timeouts"] == 1


def test_the_holder_is_forgotten_on_rollback_and_close(domain_files: Path) -> None:
    _seed(domain_files)
    writer = managed(domain_files, "core")
    writer.execute("UPDATE users SET status='changed' WHERE id='u1'").close()
    holder = sqlite_lease_wait.holder_of("core")
    assert holder is not None and holder.connection_id == writer.bowhead_connection_id
    writer.rollback()
    assert sqlite_lease_wait.holder_of("core") is None
    writer.execute("UPDATE users SET status='changed' WHERE id='u1'").close()
    assert sqlite_lease_wait.holder_of("core") is not None
    writer.close()
    assert sqlite_lease_wait.holder_of("core") is None


def test_another_connection_cannot_forget_the_current_holder() -> None:
    sqlite_lease_wait.note_exclusive("core", "conn_a", "a.py:1:hold")
    try:
        sqlite_lease_wait.clear_exclusive("core", "conn_b")
        holder = sqlite_lease_wait.holder_of("core")
        assert holder is not None and holder.connection_id == "conn_a"
    finally:
        sqlite_lease_wait.clear_exclusive("core", "conn_a")
    assert sqlite_lease_wait.holder_of("core") is None


def _record(callsite: str, waited_ms: float, outcome: str = "proceeded") -> None:
    sqlite_lease_wait.record_wait(
        lease_domain="core",
        lease_mode=1,
        waited_ms=waited_ms,
        attempts=1,
        outcome=outcome,
        fields={"callsite": callsite},
        holder=None,
        holder_seen_at=None,
    )


def test_the_summary_aggregates_per_callsite_and_folds_the_rest(recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_lease_wait, "LOG_THRESHOLD_MS", 60_000.0)
    monkeypatch.setattr(sqlite_lease_wait, "SUMMARY_MAX_KEYS", 2)
    for callsite, waits in {"a.py:1:a": (5.0, 7.0), "b.py:1:b": (40.0,), "c.py:1:c": (1.0,), "d.py:1:d": (2.0,)}.items():
        for waited in waits:
            _record(callsite, waited)
    sqlite_lease_wait.flush_summary()
    lines = {entry["callsite"]: entry for entry in recorded.named("sqlite.lease_wait_summary")}
    assert set(lines) == {"b.py:1:b", "a.py:1:a", "other"}
    assert lines["a.py:1:a"]["count"] == 2 and lines["a.py:1:a"]["total_ms"] == 12.0 and lines["a.py:1:a"]["max_ms"] == 7.0
    assert lines["other"]["count"] == 2 and lines["other"]["total_ms"] == 3.0 and lines["other"]["lease_domain"] == "*"
    assert all(entry["callsites_folded"] == 2 for entry in lines.values())


def test_a_window_is_written_by_the_first_wait_after_it_ends(recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(sqlite_lease_wait.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sqlite_lease_wait, "LOG_THRESHOLD_MS", 60_000.0)
    _record("a.py:1:a", 3.0)
    clock[0] += sqlite_lease_wait.SUMMARY_WINDOW_S - 1
    _record("a.py:1:a", 4.0)
    assert recorded.named("sqlite.lease_wait_summary") == []
    clock[0] += 2
    _record("a.py:1:a", 5.0)
    [first] = recorded.named("sqlite.lease_wait_summary")
    assert first["count"] == 2 and first["total_ms"] == 7.0
    sqlite_lease_wait.flush_summary()
    assert recorded.named("sqlite.lease_wait_summary")[-1]["count"] == 1


async def _pooled_read(path: str) -> list[tuple[str]]:
    from bowhead_core import _db

    async with (
        _db.open_shared_ctx(path) as db,
        db.execute("SELECT id FROM artifacts WHERE NOT EXISTS(SELECT 1 FROM users WHERE status='gone')") as cursor,
    ):
        return [tuple(row) for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_a_pooled_async_read_names_the_code_that_awaited_it(domain_files: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES

    from bowhead_core import _db, sqlite_domains

    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    _seed(domain_files)
    content = managed(domain_files, "content")
    content.execute("INSERT INTO artifacts(id) VALUES('art_1')").close()
    content.commit()
    content.close()
    held, errors = threading.Event(), []
    worker = threading.Thread(target=_hold_core_exclusively, args=(domain_files, 0.2, held, errors))
    worker.start()
    try:
        assert await asyncio.to_thread(held.wait, 5)
        tokens = structlog.contextvars.bind_contextvars(http_request_id="http-request-1", private_value="not-logged")
        try:
            assert await _pooled_read(str(domain_files.with_name(DOMAIN_TARGET_FILES["content"]))) == [("art_1",)]
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
    finally:
        await asyncio.to_thread(worker.join, 5)
        await _db.close_pooled_readers()
    assert not errors
    [event] = recorded.named("sqlite.lease_wait")
    assert "_pooled_read" in str(event["callsite"]) and event["outcome"] == "proceeded"
    assert "_hold_core_exclusively" in str(event["holder_callsite"])
    # Matches the request's gateway.http_request_completed line; unrelated context values stay out.
    assert event["http_request_id"] == "http-request-1"
    assert "not-logged" not in str(event)


@pytest.mark.parametrize("broken", ["callsite", "note", "clear"])
def test_a_failing_holder_registry_never_fails_a_statement_or_keeps_a_lease(domain_files: Path, monkeypatch: pytest.MonkeyPatch, broken: str) -> None:
    from bowhead_core import sqlite_domain_runtime

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("registry unavailable")

    target = {
        "callsite": (sqlite_domain_runtime, "_lease_callsite"),
        "note": (sqlite_lease_wait, "note_exclusive"),
        "clear": (sqlite_lease_wait, "clear_exclusive"),
    }
    _seed(domain_files)
    monkeypatch.setattr(*target[broken], fail)
    writer = managed(domain_files, "core")
    try:
        # The exclusive lease is taken inside the guard UDF; a raising registry must not fail the write.
        writer.execute("UPDATE users SET status='changed' WHERE id='u1'").close()
        writer.commit()
        assert not writer._leases
        # And the lease was unlocked: another connection takes it at once (timeout=0).
        other = managed(domain_files, "core")
        try:
            other.execute("UPDATE users SET status='again' WHERE id='u1'").close()
            other.commit()
        finally:
            other.close()
    finally:
        writer.close()
    monkeypatch.undo()
    sqlite_lease_wait.clear_exclusive("core", writer.bowhead_connection_id)


def test_a_wait_never_names_a_lease_an_earlier_statement_was_refused(
    domain_files: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bowhead_core import sqlite_domain_runtime

    _seed(domain_files)
    reader = managed(domain_files, "content")
    reader.execute("PRAGMA busy_timeout=2000").close()
    # Left over from an earlier statement's refusal.
    reader._lease_blocked = ("observability", 2)
    original = DomainConnection._acquire
    refusals = [3]

    def refuse_without_naming(self: DomainConnection, domain: str, mode: int) -> None:
        # A refusal that does not go through the naming path, as the lost-lease raise does.
        if self is reader and refusals[0] > 0:
            refusals[0] -= 1
            raise sqlite_domain_runtime._busy()
        original(self, domain, mode)

    monkeypatch.setattr(DomainConnection, "_acquire", refuse_without_naming)
    monkeypatch.setattr(sqlite_lease_wait, "LOG_THRESHOLD_MS", 0.0)
    try:
        assert _read_core_from(reader) == [("u1",)]
    finally:
        reader.close()
    [event] = recorded.named("sqlite.lease_wait")
    assert event["attempts"] == 3
    assert event["lease_domain"] == "unavailable"


def test_a_window_closed_after_a_quiet_spell_reports_only_its_own_span(recorded: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [5_000.0]
    monkeypatch.setattr(sqlite_lease_wait.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sqlite_lease_wait, "LOG_THRESHOLD_MS", 60_000.0)
    _record("a.py:1:a", 3.0)
    clock[0] += 3 * 3600
    _record("a.py:1:a", 4.0)
    [closed] = recorded.named("sqlite.lease_wait_summary")
    assert closed["window_seconds"] == sqlite_lease_wait.SUMMARY_WINDOW_S
    clock[0] += 10
    sqlite_lease_wait.flush_summary()
    flushed = recorded.named("sqlite.lease_wait_summary")[-1]
    assert flushed["count"] == 1 and flushed["window_seconds"] == 10.0


@pytest.mark.asyncio
async def test_a_wait_inside_a_request_deadline_times_out_with_the_deadline(
    domain_files: Path, recorded: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A save a person triggers has 30 s in total (sqlite_request_deadline); a lease wait inside it ends with
    # that deadline, and the event reports the lowered busy timeout. A short deadline stands in for 30 s.
    import asyncio

    from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
    from bowhead_core.sqlite_request_deadline import request_write_deadline

    from bowhead_core import _db, sqlite_domains

    monkeypatch.setattr(sqlite_domains, "domain_layout_active", lambda _path: True)
    _seed(domain_files)
    content = managed(domain_files, "content")
    content.execute("INSERT INTO artifacts(id) VALUES('art_1')").close()
    content.commit()
    content.close()
    held, errors = threading.Event(), []
    worker = threading.Thread(target=_hold_core_exclusively, args=(domain_files, 1.5, held, errors))
    worker.start()
    try:
        assert await asyncio.to_thread(held.wait, 5)
        started = time.monotonic()
        with request_write_deadline(0.4), pytest.raises(sqlite3.OperationalError, match="authority lease"):
            await _pooled_read(str(domain_files.with_name(DOMAIN_TARGET_FILES["content"])))
        elapsed = time.monotonic() - started
    finally:
        await asyncio.to_thread(worker.join, 5)
        await _db.close_pooled_readers()
    assert not errors
    assert elapsed < 1.2, "The wait must end with the request deadline, not the connection's own minute"
    [event] = recorded.named("sqlite.lease_wait")
    assert event["outcome"] == "timed_out"
    assert isinstance(event["busy_timeout_ms"], int) and event["busy_timeout_ms"] <= 400
    assert isinstance(event["waited_ms"], float) and 200 <= event["waited_ms"] <= 1200
