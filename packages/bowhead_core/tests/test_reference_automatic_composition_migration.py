"""0086 adds automatic receiver material while preserving old compositions."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import restore
from test_skill_reference_claim import start_case as _start_case

claimed_case, receiver_case, start_case = _claimed_case, _receiver_case, _start_case
MIGRATION = import_module("bowhead_core.migrations.versions.0086_reference_auto_composition")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_previous_records_and_unrelated_schema(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    changed = set(MIGRATION.PREVIOUS) | {MIGRATION.TRIGGER, MIGRATION.CONTENT_TRIGGER} if target == "core" else set()
    assert [r for r in schema(path) if r[1] not in changed] == [r for r in previous if r[1] not in changed]
    assert len(schema(path)) == len(previous) + (2 if target == "core" else 0)


@pytest.mark.parametrize("failure", ["drift", *MIGRATION.REPLACEMENTS, MIGRATION.TRIGGER, MIGRATION.CONTENT_TRIGGER])
def test_drift_and_interrupted_guard_changes_are_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {next(iter(MIGRATION.PREVIOUS))}")
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement.strip().startswith(f"CREATE TRIGGER {failure} "):
            raise RuntimeError("injected composition migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    before, previous = snapshot(path), schema(path)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before and schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission", "admission_unused_invoke"], indirect=True)
async def test_old_explicit_composition_and_materials_remain_readable(claimed_case, tmp_path):
    case = claimed_case
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    expected = await restore(case)
    assert expected[0].receiver_source is None and expected[0].node_id is not None
    with sqlite3.connect(case.path) as db:
        saved = db.execute("SELECT composition_json FROM chat_reference_resolutions").fetchone()[0]
        assert "receiver_source" not in saved
    path = tmp_path / "previous.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    # Current reads/writes run only at head (container startup runs `upgrade head`); assertions for this revision are already complete above.
    _upgrade(path)
    case.path = path
    assert await restore(case) == expected
