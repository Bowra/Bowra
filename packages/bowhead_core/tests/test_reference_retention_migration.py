"""0067 preserves historical records and adds atomic deletion/late-write gates."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0067_reference_retention")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_records_and_other_targets(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    with sqlite3.connect(path) as db:
        names = [
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'alembic_version' AND name NOT LIKE 'sqlite_%'")
        ]
        before = {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in names}
        schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    with sqlite3.connect(path) as db:
        assert {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in names} == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target != "core":
            assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
        else:
            assert db.execute("SELECT type FROM sqlite_master WHERE name='trg_reference_session_delete'").fetchone() == ("trigger",)


@pytest.mark.parametrize("name", ["trg_reference_session_delete", "trg_reference_plan_parent_delete", "trg_chat_reference_evidence_lifecycle"])
def test_drift_fails_before_any_schema_change(tmp_path, name):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {name}(x TEXT)")
        before = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    with pytest.raises(RuntimeError, match="reference retention schema drift"):
        _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
async def test_populated_previous_release_keeps_data_until_maintenance(tmp_path):
    import test_chat_reference_storage as storage
    from bowhead_core.chat_reference_retention import ReferenceRetentionResult, purge_expired_reference_material
    from test_reference_retention import _facts, _seed

    case = await storage.case.__wrapped__(tmp_path)
    await _seed(case)
    previous = tmp_path / "previous.db"
    storage._historical_fixture(case.path, previous, MIGRATION.down_revision)
    case.path = previous
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (case.now, case.scope.session_id))
    before = _facts(case.path)
    _upgrade(case.path, MIGRATION.revision)
    assert _facts(case.path) == before  # Migration does not hide a data backfill.
    # Execute today's cleaner only after today's schema has been installed.
    _upgrade(case.path)
    assert await purge_expired_reference_material(case.path) == ReferenceRetentionResult(1, 1)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT ciphertext,retention_state FROM chat_reference_content_snapshots").fetchone() == (None, "purged")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
