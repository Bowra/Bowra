"""kb-knowledge-tables v2 Phase A Step 1: schema v1→v2 migration UT.

Per docs/kb-knowledge-tables-v2-tech-design.md §A.5. Covers:
  - PRAGMA sniff + idempotent ADD COLUMN
  - V1 marker backfill
  - user_corrected_fields_json → edited_by_user_json rename
  - Re-run is no-op
  - Read-only / partial-corruption error path (degrade, don't crash)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.kb.extracted_record_store import (
    V2_MIGRATION_ROW_COUNT_THRESHOLD,
    KbExtractedRecordStore,
    migrate_v1_to_v2,
)

if TYPE_CHECKING:
    from pathlib import Path

# Raw V1-shape DDL — used to construct a "pre-migration" DB that mimics what
# prod 101 / a V1-era SQLite file looks like before the v2 migration runs.
# Must NOT use the live _SCHEMA (which is now v2-shaped).
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_extracted_records (
    record_id                   TEXT PRIMARY KEY,
    doc_type_id                 TEXT NOT NULL,
    file_id                     TEXT NOT NULL,
    workspace_id                TEXT NOT NULL,
    fields_json                 TEXT NOT NULL DEFAULT '{}',
    verify_status               TEXT NOT NULL DEFAULT 'ready',
    user_corrected_fields_json  TEXT NOT NULL DEFAULT '[]',
    extractor_model             TEXT NOT NULL,
    extraction_duration_ms      INTEGER,
    extracted_at                TEXT NOT NULL,
    last_modified_at            TEXT NOT NULL,
    last_modified_by            TEXT NOT NULL,
    soft_deleted_at             TEXT
)
"""


async def _make_v1_store(tmp_path: Path) -> tuple[KbExtractedRecordStore, Path]:
    """Build a fresh **V1-shape** SQLite (pre-migration) + insert 2 sample rows
    via raw SQL. The live KbExtractedRecordStore is now v2; we bypass it here
    to simulate an existing prod 101 DB that needs migration.
    """
    db_path = tmp_path / "rec.db"
    # 1. Create V1-shape table (raw SQL — bypass v2-shaped _SCHEMA).
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(_V1_SCHEMA)
        await db.commit()
        # 2. Insert 2 V1-shape rows directly.
        import json as _json

        rows = [
            (
                "rec_v1_a",
                "dt1",
                "f1",
                "ws1",
                _json.dumps(
                    {
                        "合同金额": {
                            "value": 5_000_000,
                            "source_snippet": "总金额 5,000,000",
                            "source_page": 1,
                            "confidence": 0.92,
                        },
                    },
                    ensure_ascii=False,
                ),
                "ready",
                "[]",
                "qwen-plus",
                1000,
                "2026-05-20T00:00:00+00:00",
                "2026-05-20T00:00:00+00:00",
                "user1",
            ),
            (
                "rec_v1_b",
                "dt1",
                "f2",
                "ws1",
                _json.dumps(
                    {
                        "客户名": {"value": "ACME Corp", "source_snippet": "", "confidence": 0.8},
                    },
                    ensure_ascii=False,
                ),
                "needs_review",
                "[]",
                "qwen-plus",
                1200,
                "2026-05-20T00:00:01+00:00",
                "2026-05-20T00:00:01+00:00",
                "user1",
            ),
        ]
        for r in rows:
            await db.execute(
                "INSERT INTO kb_extracted_records "
                "(record_id, doc_type_id, file_id, workspace_id, fields_json, "
                " verify_status, user_corrected_fields_json, extractor_model, "
                " extraction_duration_ms, extracted_at, last_modified_at, "
                " last_modified_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                r,
            )
        await db.commit()
    # Returned store is constructed but not initialized — caller calls migrate.
    store = KbExtractedRecordStore(db_path)
    return store, db_path


async def _columns(db_path: Path) -> set[str]:
    async with aiosqlite.connect(str(db_path)) as db, db.execute("SELECT name FROM pragma_table_info('kb_extracted_records')") as cur:
        rows = await cur.fetchall()
    return {row[0] for row in rows}


# ─── Step 1 UTs (tech-design §A.5 UT table, 5 tests) ─────────


async def test_migrate_v1_to_v2_adds_schema_version_and_legacy_columns(tmp_path):
    """ADD COLUMN schema_version + fields_json_legacy land on a V1-shape table."""
    _, db_path = await _make_v1_store(tmp_path)
    cols_before = await _columns(db_path)
    assert "schema_version" not in cols_before
    assert "fields_json_legacy" not in cols_before

    await migrate_v1_to_v2(db_path)

    cols_after = await _columns(db_path)
    assert "schema_version" in cols_after
    assert "fields_json_legacy" in cols_after


async def test_migrate_v1_to_v2_backfills_v1_marker(tmp_path):
    """Existing rows get schema_version=1 after migrate; nothing else mutates."""
    _, db_path = await _make_v1_store(tmp_path)
    await migrate_v1_to_v2(db_path)

    async with (
        aiosqlite.connect(str(db_path)) as db,
        db.execute("SELECT schema_version, fields_json_legacy FROM kb_extracted_records ORDER BY file_id") as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 2
    for row in rows:
        sv, legacy = row[0], row[1]
        assert sv == 1, "V1 marker must be backfilled to existing rows"
        assert legacy is None, "fields_json_legacy stays NULL until edit fires"


async def test_migrate_v1_to_v2_renames_user_corrected_to_edited_by_user(tmp_path):
    """Column rename idempotent + correct."""
    _, db_path = await _make_v1_store(tmp_path)
    cols_before = await _columns(db_path)
    assert "user_corrected_fields_json" in cols_before
    assert "edited_by_user_json" not in cols_before

    await migrate_v1_to_v2(db_path)

    cols_after = await _columns(db_path)
    assert "edited_by_user_json" in cols_after
    assert "user_corrected_fields_json" not in cols_after


async def test_migrate_v1_to_v2_idempotent_rerun(tmp_path):
    """2nd call is no-op; no errors, no duplicate ALTER, content unchanged."""
    _, db_path = await _make_v1_store(tmp_path)
    await migrate_v1_to_v2(db_path)
    cols_after_1 = await _columns(db_path)

    # 2nd run should be no-op.
    await migrate_v1_to_v2(db_path)
    cols_after_2 = await _columns(db_path)

    assert cols_after_1 == cols_after_2, "column set unchanged on re-run"

    async with (
        aiosqlite.connect(str(db_path)) as db,
        db.execute("SELECT COUNT(*), MIN(schema_version), MAX(schema_version) FROM kb_extracted_records") as cur,
    ):
        row = await cur.fetchone()
    assert row[0] == 2
    assert row[1] == 1
    assert row[2] == 1


async def test_migrate_v1_to_v2_degrades_on_unwritable_db(tmp_path, caplog):
    """Path that can't be opened (read-only / disk full / missing) → log+audit, no raise."""
    # Force a write failure: target a non-existent directory that aiosqlite can't create.
    bad_path = tmp_path / "nonexistent_subdir" / "ro.db"

    # Without BOWHEAD_KB_MIGRATION_STRICT=1 we must NOT raise.
    caplog.set_level(logging.ERROR)
    await migrate_v1_to_v2(bad_path)  # would raise without the try/except wrap
    assert any("kb v1→v2 migration failed" in rec.message for rec in caplog.records), "expected error log when DB path can't be opened"


async def test_migrate_v1_to_v2_handles_missing_table_gracefully(tmp_path, caplog):
    """If kb_extracted_records table doesn't exist (init never ran), migrate logs +
    skips, no exception, no schema mutation."""
    db_path = tmp_path / "empty.db"
    # Create an empty SQLite DB but DON'T run initialize().
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("CREATE TABLE noise (x INT)")
        await db.commit()

    caplog.set_level(logging.INFO)
    await migrate_v1_to_v2(db_path)
    assert any("table absent" in rec.message for rec in caplog.records), "expected info log when target table missing"

    # Verify no kb_extracted_records-related columns leaked into the noise table.
    async with aiosqlite.connect(str(db_path)) as db, db.execute("SELECT name FROM pragma_table_info('noise')") as cur:
        noise_cols = {row[0] for row in await cur.fetchall()}
    assert noise_cols == {"x"}


async def test_migrate_strict_mode_raises(tmp_path, monkeypatch):
    """When BOWHEAD_KB_MIGRATION_STRICT=1, exceptions bubble (no degrade)."""
    monkeypatch.setenv("BOWHEAD_KB_MIGRATION_STRICT", "1")
    bad_path = tmp_path / "nonexistent_subdir" / "ro.db"
    with pytest.raises(Exception):  # noqa: B017
        await migrate_v1_to_v2(bad_path)


async def test_migrate_emits_audit_with_duration_ms(tmp_path, monkeypatch):
    """Success path emits kb.migrate_v1_to_v2_done with duration_ms + row_count."""
    captured: list[tuple[str, dict]] = []

    def _spy(event_type: str, payload: dict) -> None:
        captured.append((event_type, payload))

    monkeypatch.setattr(
        "bowhead_core.kb.extracted_record_store.emit_audit",
        _spy,
    )
    _, db_path = await _make_v1_store(tmp_path)
    await migrate_v1_to_v2(db_path)

    done_events = [(k, p) for k, p in captured if k == "kb.migrate_v1_to_v2_done"]
    assert len(done_events) == 1, f"expected 1 done event, got {captured!r}"
    payload = done_events[0][1]
    assert "duration_ms" in payload and isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0
    assert payload["row_count"] == 2


# ─── Bonus invariant: threshold constant exists + sane ─────────


def test_v2_migration_row_count_threshold_is_10k():
    """Pre-ship gate constant — guards against accidental change."""
    assert V2_MIGRATION_ROW_COUNT_THRESHOLD == 10_000
