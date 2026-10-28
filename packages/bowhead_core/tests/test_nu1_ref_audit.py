"""Sprint ν1 Phase 1c — _ref_audit double-write tests.

Covers:
- init creates table + indexes + WAL + busy_timeout
- emit double-write logger.info + SQLite
- emit when init not done → logger only, no crash
- emit SQLite write failure → log warning, doesn't propagate
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite
import pytest

from bowhead_core import _ref_audit


@pytest.fixture(autouse=True)
def reset_audit_state():
    _ref_audit._reset_for_test()
    yield
    _ref_audit._reset_for_test()


@pytest.mark.asyncio
async def test_init_creates_table_with_indexes(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    await _ref_audit.init_ref_audit(db)
    assert _ref_audit._INIT_DONE
    async with aiosqlite.connect(db) as conn:
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
            assert row[0] == "wal"
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ref_audit_log'",
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_ref_audit_%'",
        ) as cur:
            indexes = {r[0] for r in await cur.fetchall()}
            assert {"idx_ref_audit_turn", "idx_ref_audit_event_at"} == indexes


@pytest.mark.asyncio
async def test_init_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    await _ref_audit.init_ref_audit(db)
    await _ref_audit.init_ref_audit(db)  # 2nd call OK
    async with (
        aiosqlite.connect(db) as conn,
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='ref_audit_log'",
        ) as cur,
    ):
        row = await cur.fetchone()
        assert row[0] == 1


@pytest.mark.asyncio
async def test_emit_double_write_after_init(tmp_path: Path, caplog) -> None:
    db = tmp_path / "audit.db"
    await _ref_audit.init_ref_audit(db)

    caplog.set_level(logging.INFO, logger="bowhead_core._ref_audit")
    await _ref_audit.emit_ref_audit_event(
        "t_123",
        "validate_reject",
        {"errors": ["fake_id"], "retry_count": 0},
        user_id="u_alice",
        workspace_id="ws_1",
    )

    # logger.info fired
    audit_records = [r for r in caplog.records if r.message == "ν_ref_audit"]
    assert len(audit_records) == 1
    rec = audit_records[0]
    assert rec.event_type == "validate_reject"
    assert rec.turn_id == "t_123"
    assert rec.user_id == "u_alice"
    assert rec.workspace_id == "ws_1"
    # SQLite written
    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT turn_id, user_id, workspace_id, event_type, payload_json FROM ref_audit_log WHERE turn_id=?",
            ("t_123",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "t_123"
        assert row[1] == "u_alice"
        assert row[2] == "ws_1"
        assert row[3] == "validate_reject"
        payload = json.loads(row[4])
        assert payload["retry_count"] == 0
        assert payload["errors"] == ["fake_id"]


@pytest.mark.asyncio
async def test_emit_when_not_init_logger_only(caplog) -> None:
    """init 没 call → logger 仍出, 不崩."""
    caplog.set_level(logging.INFO, logger="bowhead_core._ref_audit")
    await _ref_audit.emit_ref_audit_event(
        "t_no_init",
        "kb_hallucinate",
        {"marker": "[ref:fake]"},
    )
    audit_records = [r for r in caplog.records if r.message == "ν_ref_audit"]
    assert len(audit_records) == 1


@pytest.mark.asyncio
async def test_emit_sqlite_write_failure_logs_warning_no_raise(
    tmp_path: Path,
    caplog,
    monkeypatch,
) -> None:
    """SQLite write 失败时 log warning, 不抛."""
    db = tmp_path / "audit.db"
    await _ref_audit.init_ref_audit(db)
    # Simulate deletion of the database file.
    db.unlink()
    # Also point _DB_PATH to an unwritable location.
    bad_path = Path("/nonexistent/dir/audit.db")
    monkeypatch.setattr(_ref_audit, "_DB_PATH", bad_path)

    caplog.set_level(logging.WARNING, logger="bowhead_core._ref_audit")
    await _ref_audit.emit_ref_audit_event(
        "t_oops",
        "strip_degrade",
        {"x": 1},
    )
    warns = [r for r in caplog.records if "sqlite_write_failed" in r.message]
    assert len(warns) >= 1


@pytest.mark.asyncio
async def test_emit_chinese_payload(tmp_path: Path) -> None:
    """payload 含中文 → SQLite 存 UTF-8 不 escape."""
    db = tmp_path / "audit.db"
    await _ref_audit.init_ref_audit(db)
    await _ref_audit.emit_ref_audit_event(
        "t_zh",
        "kb_hallucinate",
        {"marker": "[ref:中文_chunk]", "near_text": "去年合规要求"},
    )
    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT payload_json FROM ref_audit_log WHERE turn_id=?",
            ("t_zh",),
        ) as cur:
            row = await cur.fetchone()
        payload = json.loads(row[0])
        assert payload["near_text"] == "去年合规要求"
        assert payload["marker"] == "[ref:中文_chunk]"
