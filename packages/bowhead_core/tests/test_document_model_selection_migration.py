"""Selected planner identities are additive; historical dispatches stay exact."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_document_planning_continuation import planning
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0194_document_model_selection")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_selection_upgrade_preserves_all_targets_and_repeats(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {k: v for k, v in after.items() if k != "alembic_version"} == {k: v for k, v in before.items() if k != "alembic_version"}
    if target == "core":
        with sqlite3.connect(path) as db:
            assert "model_selection_json" in {row[1] for row in db.execute("PRAGMA table_info(document_dispatches)")}
        assert [row for row in schema(path) if row[1] != "document_dispatches"] == [row for row in old_schema if row[1] != "document_dispatches"]
    else:
        assert schema(path) == old_schema
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after


async def test_historical_dispatch_stays_null_and_all_evidence_survives(ctx, tmp_path):
    await planning(ctx)
    path = tmp_path / "historical.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    after = snapshot(path)
    assert {k: v for k, v in after.items() if k not in {"alembic_version", "document_dispatches"}} == {
        k: v for k, v in before.items() if k not in {"alembic_version", "document_dispatches"}
    }
    assert [row[:-1] for row in after["document_dispatches"]] == before["document_dispatches"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT model_selection_json FROM document_dispatches").fetchall() == [(None,)]
        with pytest.raises(sqlite3.IntegrityError, match="journal is retained"):
            db.execute("UPDATE document_dispatches SET model_selection_json=?", ('{"provider":"team","model":"changed"}',))
