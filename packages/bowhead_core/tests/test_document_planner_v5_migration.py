"""Versioned planner envelopes preserve old schemas, rows and read semantics."""

import json
import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.documents.continuation import DocumentRuntimeSnapshot, _validated_context
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_document_planning_continuation import planning
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0151_document_planner_v5")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_planner_envelope_upgrade_preserves_all_target_schemas_and_repeats(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {k: v for k, v in after.items() if k != "alembic_version"} == {k: v for k, v in before.items() if k != "alembic_version"}
    assert schema(path) == old_schema
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after


async def test_upgrade_preserves_retained_v4_runtime_and_context_bytes(ctx, tmp_path):
    original = await planning(ctx)
    path = tmp_path / "historical.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    after = snapshot(path)
    assert {k: v for k, v in after.items() if k != "alembic_version"} == {k: v for k, v in before.items() if k != "alembic_version"}
    with sqlite3.connect(path) as db:
        runtime = DocumentRuntimeSnapshot.model_validate_json(db.execute("SELECT runtime_snapshot_json FROM document_turn_dispatches").fetchone()[0])
        instruction, context = db.execute("SELECT instruction,context_json FROM document_planning_attempts").fetchone()
    assert runtime == original.snapshot
    assert _validated_context(json.loads(context), instruction, profile=runtime.planner_profile) == original.context
