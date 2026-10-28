"""0069 preserves prior data and gates only the new mixed composition contract."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade, assert_rows_survive_upgrade

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0069_skill_reference_composition")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_and_repeat_preserve_previous_schema_and_data(tmp_path, target):
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
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        current = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        if target == "core":
            assert [row for row in current if row[1] != MIGRATION.TRIGGER] == schema
            assert len(current) == len(schema) + 1
        else:
            assert current == schema


def test_drift_is_rejected_before_schema_changes(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {MIGRATION.TRIGGER}(x TEXT)")
        before = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    with pytest.raises(RuntimeError, match="reference skill composition schema drift"):
        _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
async def test_populated_previous_release_materials_remain_readable(tmp_path):
    import test_chat_reference_storage as storage

    case = await storage.case.__wrapped__(tmp_path)
    async with storage.open_shared_ctx(case.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await storage._plan(db, case)
        await storage._content(db, case)
        await db.commit()
    path = tmp_path / "previous.db"
    storage._historical_fixture(case.path, path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT * FROM chat_reference_plans").fetchall()
        contents = db.execute("SELECT * FROM chat_reference_content_snapshots").fetchall()
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM chat_reference_plans").fetchall() == before
        assert db.execute("SELECT * FROM chat_reference_content_snapshots").fetchall() == contents
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions").fetchone() == (0,)
    # Current readers run only at head (container startup runs `upgrade head`; chat sources gained
    # document_revision_target_json in 0147); the 0069 row assertions are complete above.
    _upgrade(path)
    async with storage.open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        assert await storage.ChatReferenceStore.get_plan_in_connection(db, scope=case.scope, turn_id="t_ref") == case.plan
        descriptor, ciphertext = await case.store.get_encrypted_content_in_connection(
            db, scope=case.scope, turn_id="t_ref", content_key=case.descriptor.content_key
        )
        assert descriptor == case.descriptor
        assert (
            case.cipher.open(scope=case.scope, purpose="content", identity=f"t_ref/{descriptor.content_key}", ciphertext=ciphertext) == case.content
        )
        await db.rollback()
    with sqlite3.connect(path) as db:
        assert_rows_survive_upgrade(
            {"chat_reference_plans": before, "chat_reference_content_snapshots": contents},
            {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in ("chat_reference_plans", "chat_reference_content_snapshots")},
        )
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions").fetchone() == (0,)
