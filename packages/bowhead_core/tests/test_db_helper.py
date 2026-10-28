"""UT for Sprint ω Phase 0 `_db.py` shared SQLite helper."""

from __future__ import annotations

import tempfile
from pathlib import Path

import aiosqlite
import pytest
from bowhead_core._db import (
    _reset_bootstrap_for_test,
    bootstrap_shared_db,
    open_shared_ctx,
    phase0_self_test,
)


@pytest.fixture
def tmp_db_path():
    """Fresh sqlite path per test; resets bootstrap-done module-level dict."""
    _reset_bootstrap_for_test()
    with tempfile.TemporaryDirectory() as td:
        yield str(Path(td) / "bowhead.db")
    _reset_bootstrap_for_test()


@pytest.mark.asyncio
async def test_bootstrap_creates_phase0_probe_table(tmp_db_path: str) -> None:
    """r3.1 P0 fix #2 — _phase0_probe must exist after bootstrap so the
    self-test INSERT doesn't raise `no such table` (Phase 0 image DOA bug)."""
    await bootstrap_shared_db(tmp_db_path)
    async with aiosqlite.connect(tmp_db_path) as conn:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_phase0_probe'")
        row = await cur.fetchone()
    assert row is not None, "bootstrap_shared_db must CREATE _phase0_probe"


@pytest.mark.asyncio
async def test_bootstrap_sets_wal(tmp_db_path: str) -> None:
    """journal_mode=WAL is persistent on the file; verify it sticks."""
    await bootstrap_shared_db(tmp_db_path)
    async with aiosqlite.connect(tmp_db_path) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        row = await cur.fetchone()
    assert row is not None
    assert row[0].lower() == "wal", f"expected WAL, got {row[0]!r}"


@pytest.mark.asyncio
async def test_bootstrap_idempotent_in_process(tmp_db_path: str) -> None:
    """Second call within same process is a no-op (module-state cache)."""
    await bootstrap_shared_db(tmp_db_path)
    # No exception, no side effect — just runs through and returns.
    await bootstrap_shared_db(tmp_db_path)
    await bootstrap_shared_db(tmp_db_path)


@pytest.mark.asyncio
async def test_open_shared_ctx_applies_pragmas(tmp_db_path: str) -> None:
    """Per-conn safety and deletion pragmas are applied."""
    await bootstrap_shared_db(tmp_db_path)
    async with open_shared_ctx(tmp_db_path) as conn:
        cur = await conn.execute("PRAGMA busy_timeout")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 5000, f"busy_timeout should be 5000, got {row[0]}"

        cur = await conn.execute("PRAGMA synchronous")
        row = await cur.fetchone()
        assert row is not None
        # synchronous=NORMAL is integer 1 in SQLite (0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA)
        assert row[0] == 1, f"synchronous should be NORMAL(1), got {row[0]}"

        cur = await conn.execute("PRAGMA foreign_keys")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1, f"foreign_keys should be ON(1), got {row[0]}"

        cur = await conn.execute("PRAGMA secure_delete")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1, f"secure_delete should be ON(1), got {row[0]}"


@pytest.mark.asyncio
async def test_open_shared_ctx_closes_on_body_exception(tmp_db_path: str) -> None:
    """Body raises → finally clause closes conn (lines 87-90 of _db.py)."""
    await bootstrap_shared_db(tmp_db_path)
    conn_ref = []

    with pytest.raises(RuntimeError, match="boom"):
        async with open_shared_ctx(tmp_db_path) as conn:
            conn_ref.append(conn)
            raise RuntimeError("boom")

    closed_conn = conn_ref[0]
    with pytest.raises((Exception,)):
        await closed_conn.execute("SELECT 1")


@pytest.mark.asyncio
async def test_open_shared_ctx_closes_on_pragma_failure(tmp_db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """r3 Eng P1 catch — if a PRAGMA raises mid-setup (try/except block at
    lines 80-86), the connection MUST be closed before re-raising. Without
    the try/except + conn.close(), the socket leaks.
    """
    await bootstrap_shared_db(tmp_db_path)

    # Monkeypatch aiosqlite.Connection.execute to raise on the first call
    # (which is `PRAGMA busy_timeout=5000`).
    real_execute = aiosqlite.Connection.execute
    call_count = {"n": 0}
    closed_conn_ref: list[aiosqlite.Connection] = []

    async def fake_execute(self, sql, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Capture the connection before raising so we can verify it's closed
            closed_conn_ref.append(self)
            raise aiosqlite.OperationalError("simulated pragma fail")
        return await real_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fake_execute)

    with pytest.raises(aiosqlite.OperationalError, match="simulated pragma fail"):
        async with open_shared_ctx(tmp_db_path):
            # Should never reach here — pragma fails during setup
            raise AssertionError("body should not run on pragma failure")

    # Connection was opened then closed before re-raise — verify by trying
    # to use it post-context (should fail since closed).
    assert len(closed_conn_ref) == 1
    closed = closed_conn_ref[0]
    monkeypatch.undo()  # restore real execute so we can probe the closed conn
    with pytest.raises((Exception,)):
        await closed.execute("SELECT 1")


@pytest.mark.asyncio
async def test_phase0_self_test_passes_on_fresh_db(tmp_db_path: str) -> None:
    """Standard success path — no exception."""
    await bootstrap_shared_db(tmp_db_path)
    await phase0_self_test(tmp_db_path, ["turn_log_store", "canvas_store", "session_store"])


@pytest.mark.asyncio
async def test_phase0_self_test_pk_collision_resilient(tmp_db_path: str) -> None:
    """r3.1 P0 Eng catch — partial-fail leftover row must not crash next boot.

    Scenario: prior boot's self-test inserted row but crashed before DELETE
    (e.g., one of N stores raised mid-loop). On reboot, plain `INSERT INTO`
    would hit `UNIQUE constraint failed: _phase0_probe.k` → uvicorn exits →
    docker restart loop → image DOA.

    `INSERT OR REPLACE` defends against this.
    """
    await bootstrap_shared_db(tmp_db_path)
    # Simulate stale row from prior boot's partial fail
    async with aiosqlite.connect(tmp_db_path) as conn:
        await conn.execute(
            "INSERT INTO _phase0_probe (k, ts) VALUES (?, ?)",
            ("turn_log_store", 0.0),
        )
        await conn.commit()
    # Self-test should NOT crash on the stale row
    await phase0_self_test(tmp_db_path, ["turn_log_store", "canvas_store"])
    # And probe table is empty post-test (DELETE ran)
    async with aiosqlite.connect(tmp_db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM _phase0_probe")
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0, "probe table must be empty after self-test"


@pytest.mark.asyncio
async def test_pragma_table_info_with_param(tmp_db_path: str) -> None:
    """r3.1 fix #1 verification: PRAGMA sniff pattern used by canvas_store
    schema_version ALTER works with `?` placeholder."""
    await bootstrap_shared_db(tmp_db_path)
    async with open_shared_ctx(tmp_db_path) as conn:
        await conn.execute("CREATE TABLE foo (a INT, b INT)")
        cur = await conn.execute("SELECT 1 FROM pragma_table_info(?) WHERE name = ?", ("foo", "b"))
        row = await cur.fetchone()
        assert row is not None and row[0] == 1
        # Column absent → no row
        cur = await conn.execute("SELECT 1 FROM pragma_table_info(?) WHERE name = ?", ("foo", "z"))
        row = await cur.fetchone()
        assert row is None
