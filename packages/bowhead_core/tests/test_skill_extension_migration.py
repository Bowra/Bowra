"""Add extension composition/evidence without rewriting any historical records."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case

claimed_case, receiver_case, start_case = _claimed_case, _receiver_case, _start_case

MIGRATION = import_module("bowhead_core.migrations.versions.0081_skill_read_extensions")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_migration_preserves_old_data_and_only_replaces_expected_core_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after, current = snapshot(path), schema(path)
    assert all(after[name] == rows for name, rows in before.items())
    if target == "core":
        assert set(after) - set(before) == {MIGRATION.TABLE} and after[MIGRATION.TABLE] == []
        assert all(row in current for row in old if row[1] not in MIGRATION.REPLACEMENTS)
        assert set(MIGRATION.OBJECT_NAMES).issubset({row[1] for row in current})
    else:
        assert after == before and current == old
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after and schema(path) == current
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)


@pytest.mark.parametrize("drift", ["name_collision", "changed_guard"])
def test_schema_drift_never_partially_installs_permissions(tmp_path, drift):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if drift == "name_collision":
            db.execute("CREATE TABLE trg_reference_extension_invocation_proof(x TEXT)")
        else:
            db.execute("DROP TRIGGER trg_reference_model_context_scope")
            db.execute("CREATE TRIGGER trg_reference_model_context_scope BEFORE INSERT ON chat_reference_model_contexts BEGIN SELECT 1; END")
    before = schema(path)
    with pytest.raises(RuntimeError, match="schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before


def test_failure_after_guard_replacement_restores_original_schema(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.strip().startswith("CREATE TRIGGER trg_reference_bi_continuation_scope"):
            raise RuntimeError("post-DDL failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="post-DDL failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["canonical_input"], indirect=True)
async def test_nonempty_original_composition_and_ciphertext_are_unchanged(claimed_case, tmp_path):
    from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
    from test_chat_reference_storage import _historical_fixture

    assert (await claim_reference_skill_primary_for_worker(**claimed_case.args)).outcome == "claimed"
    path = tmp_path / "previous.db"
    _historical_fixture(claimed_case.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    assert before["chat_reference_resolutions"] and before["chat_reference_content_snapshots"]
    _upgrade(path, MIGRATION.revision)
    after = snapshot(path)
    assert all(after[name] == rows for name, rows in before.items())
    assert after[MIGRATION.TABLE] == []
