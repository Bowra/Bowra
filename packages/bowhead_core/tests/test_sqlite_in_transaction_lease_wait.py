"""A refused authority lease waits inside a transaction only where no stale read and no cycle can follow.

116 (1.7.62, 2026-11-22): beside admin writes, transactions that read core failed at once while an
authority write held core for a few milliseconds: a Lead start, a finished Turn's completion check, a
sandbox upload. Waiting inside ``BEGIN IMMEDIATE`` is unsafe, because SQLite starts a snapshot of every
read-only attached file at ``BEGIN IMMEDIATE``, before the authorizer takes that domain's lease. Writer
transactions therefore take their writer lock through a gate write, so an attached snapshot starts at its
first read, after the lease. See docs/sprints/authority-snapshot-wait-2026-11-22/.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from bowhead_core.sqlite_domain_runtime import DomainConnection
from test_sqlite_domain_runtime import domain_files as domain_files
from test_sqlite_domain_runtime import managed

from bowhead_core import sqlite_domain_runtime


def _hold(
    core_path: Path, domain: str, statements: list[str], seconds: float, *, commit: bool, leases: dict | None = None
) -> tuple[threading.Thread, threading.Event, list]:
    """Run ``statements`` in a writer transaction on its own thread, keep it open ``seconds``, then end it."""
    holding = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        db = managed(core_path, domain)
        try:
            db.execute("BEGIN IMMEDIATE").close()
            for sql in statements:
                with db.execute(sql) as cursor:
                    cursor.fetchall()
            if leases is not None:
                leases.update(db._leases)
            holding.set()
            threading.Event().wait(seconds)
            db.commit() if commit else db.rollback()
        except BaseException as error:  # noqa: BLE001 -- reported to the test thread
            errors.append(error)
            holding.set()
        finally:
            db.close()

    thread = threading.Thread(target=run)
    thread.start()
    assert holding.wait(5)
    assert not errors, errors
    return thread, holding, errors


def _seed(core_path: Path) -> None:
    core = managed(core_path, "core")
    execution = managed(core_path, "execution")
    try:
        core.execute("INSERT INTO users VALUES('u','active',10)").close()
        core.commit()
        execution.execute("INSERT INTO sessions(id,user_id) VALUES('s','u')").close()
        execution.commit()
    finally:
        execution.close()
        core.close()


def _hold_execution_exclusive(core_path: Path, seconds: float) -> threading.Thread:
    # A fenced execution write that reads no other domain: only execution's exclusive lease.
    leases: dict = {}
    thread, _, _ = _hold(core_path, "execution", ["UPDATE sessions SET deleted_at='x' WHERE id='s'"], seconds, commit=False, leases=leases)
    assert leases == {"execution": 2}, leases
    return thread


def _status(db: DomainConnection) -> str:
    with db.execute("SELECT status FROM users WHERE id='u'") as cursor:
        return cursor.fetchone()[0]


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_: float) -> None:
        pytest.fail("This lease must fail at once, without waiting")

    monkeypatch.setattr(sqlite_domain_runtime.time, "sleep", fail)


def test_writer_transaction_holds_the_writer_lock_and_sees_an_authority_change_committed_after_begin(domain_files: Path) -> None:
    """The gap closed: before, a revocation committed between BEGIN IMMEDIATE and the first core read went unseen."""
    _seed(domain_files)
    execution = managed(domain_files, "execution")
    other = managed(domain_files, "execution")
    core = managed(domain_files, "core")
    try:
        execution.execute("BEGIN IMMEDIATE").close()
        assert execution.bowhead_transaction_mode == "immediate"
        # The writer lock is taken at BEGIN; a second writer that cannot wait gets no transaction.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        assert not other.in_transaction
        # Nothing fences core yet, so the revocation commits; the open transaction must see it.
        core.execute("UPDATE users SET status='deleted' WHERE id='u'").close()
        core.commit()
        assert _status(execution) == "deleted"
        execution.rollback()
    finally:
        core.close()
        other.close()
        execution.close()


@pytest.mark.parametrize("commit", [False, True])
def test_a_first_shared_lease_waits_for_a_short_authority_write(domain_files: Path, monkeypatch: pytest.MonkeyPatch, commit: bool) -> None:
    _seed(domain_files)
    waits: list[dict] = []
    monkeypatch.setattr(sqlite_domain_runtime.sqlite_lease_wait, "record_wait", lambda **fields: waits.append(fields))
    holder, _, errors = _hold(domain_files, "core", ["UPDATE users SET status='suspended' WHERE id='u'"], 0.2, commit=commit)
    execution = managed(domain_files, "execution")
    try:
        execution.execute("BEGIN IMMEDIATE").close()
        started = time.monotonic()
        status = _status(execution)
        waited = time.monotonic() - started
        execution.rollback()
    finally:
        execution.close()
        holder.join(5)
    assert not errors
    # It read after the holder ended: the committed change, or the row as it was.
    assert status == ("suspended" if commit else "active")
    assert 0.05 < waited < sqlite_domain_runtime.IN_TRANSACTION_LEASE_WAIT_SECONDS
    assert [(w["lease_domain"], w["lease_mode"], w["outcome"]) for w in waits] == [("core", 1, "proceeded")]


def test_a_hold_longer_than_the_wait_still_fails_as_busy(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(domain_files)
    monkeypatch.setattr(sqlite_domain_runtime, "IN_TRANSACTION_LEASE_WAIT_SECONDS", 0.05)
    holder, _, _ = _hold(domain_files, "core", ["UPDATE users SET status='suspended' WHERE id='u'"], 0.5, commit=False)
    execution = managed(domain_files, "execution")
    try:
        execution.execute("BEGIN IMMEDIATE").close()
        with pytest.raises(sqlite3.OperationalError, match="authority lease is busy") as caught:
            _status(execution)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        execution.rollback()
        assert not execution.in_transaction
    finally:
        execution.close()
        holder.join(5)


def test_a_connection_holding_an_exclusive_lease_never_waits_for_a_shared_one(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(domain_files)
    holder = _hold_execution_exclusive(domain_files, 0.5)
    _no_sleep(monkeypatch)
    core = managed(domain_files, "core")
    try:
        core.execute("BEGIN IMMEDIATE").close()
        core.execute("UPDATE users SET status='suspended' WHERE id='u'").close()
        assert core._leases.get("core") == 2
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            core.execute("SELECT user_id FROM sessions")
        core.rollback()
    finally:
        core.close()
        holder.join(5)


def _hold_core_shared(core_path: Path, seconds: float) -> tuple[threading.Thread, list]:
    # An execution writer transaction that read core holds core's shared lease until it ends.
    thread, _, errors = _hold(core_path, "execution", ["SELECT status FROM users WHERE id='u'"], seconds, commit=True)
    return thread, errors


def test_an_authority_write_waits_for_the_readers_of_its_own_domain(domain_files: Path) -> None:
    _seed(domain_files)
    reader, errors = _hold_core_shared(domain_files, 0.2)
    core = managed(domain_files, "core")
    try:
        core.execute("BEGIN IMMEDIATE").close()
        started = time.monotonic()
        core.execute("UPDATE users SET status='suspended' WHERE id='u'").close()
        waited = time.monotonic() - started
        core.commit()
    finally:
        core.close()
        reader.join(5)
    assert not errors
    assert 0.05 < waited < sqlite_domain_runtime.IN_TRANSACTION_LEASE_WAIT_SECONDS


@pytest.mark.parametrize(
    "begin",
    [
        # Holding another domain's lease: waiting could close a cycle with a writer of that domain.
        # (It reads a column: ``count(*)`` reads none, so SQLite asks the authorizer about no read.)
        lambda db: (db.execute("BEGIN IMMEDIATE").close(), db.execute("SELECT user_id FROM sessions").fetchall()),
        # Without the writer lock another connection could commit to core meanwhile.
        lambda db: db.execute("BEGIN").close(),
    ],
    ids=["holding_another_lease", "without_the_writer_lock"],
)
def test_an_authority_write_fails_at_once_where_waiting_is_unsafe(
    domain_files: Path, monkeypatch: pytest.MonkeyPatch, begin: Callable[[DomainConnection], object]
) -> None:
    _seed(domain_files)
    reader, _ = _hold_core_shared(domain_files, 0.5)
    _no_sleep(monkeypatch)
    core = managed(domain_files, "core")
    try:
        begin(core)
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            core.execute("UPDATE users SET status='suspended' WHERE id='u'")
        core.rollback()
    finally:
        core.close()
        reader.join(5)


def test_a_writer_gate_that_cannot_get_the_writer_lock_leaves_no_transaction(domain_files: Path) -> None:
    first = managed(domain_files, "execution")
    second = managed(domain_files, "execution")
    try:
        first.execute("BEGIN IMMEDIATE").close()
        with pytest.raises(sqlite3.OperationalError):
            second.execute("BEGIN IMMEDIATE")
        assert not second.in_transaction
        first.rollback()
        second.execute("BEGIN IMMEDIATE").close()
        assert second.in_transaction
        second.rollback()
    finally:
        second.close()
        first.close()


def test_without_the_gate_table_begin_immediate_pins_and_never_waits(domain_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file without ``alembic_version`` (no admitted layout) keeps BEGIN IMMEDIATE and today's fail-at-once rule."""
    _seed(domain_files)
    with closing(sqlite3.connect(domain_files.with_name("bowhead_execution.db"))) as raw:
        raw.execute("DROP TABLE alembic_version")
    holder, _, _ = _hold(domain_files, "core", ["UPDATE users SET status='suspended' WHERE id='u'"], 0.5, commit=False)
    _no_sleep(monkeypatch)
    execution = managed(domain_files, "execution")
    try:
        assert not execution._writer_gate
        execution.execute("BEGIN IMMEDIATE").close()
        with pytest.raises(sqlite3.OperationalError, match="authority lease"):
            _status(execution)
        execution.rollback()
    finally:
        execution.close()
        holder.join(5)
