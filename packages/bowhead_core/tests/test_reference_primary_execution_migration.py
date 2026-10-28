"""Expand the binding ledger without guessing historical primary executions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core._db import open_shared_ctx
from test_agent_admission_transaction import _team_admission_case
from test_chat_reference_storage import _historical_fixture, _upgrade

pytestmark = pytest.mark.integration
_OLD = "0064_team_skill_import_provenance"
_NEW = "0065_reference_primary_execution"


@pytest.mark.asyncio
async def test_upgrade_preserves_a_real_legacy_binding_and_is_idempotent(tmp_path: Path) -> None:
    # Real store/admission writers produce the binding on the current schema; the
    # rows are then exported into the actual 0064 schema. Current writers require
    # the current schema (e.g. ``turn_agent_execution.resolved_definition_json``
    # from 0123), so they cannot run directly on a 0064 database.
    case = await _team_admission_case(tmp_path)
    binding = await case.store.admit_team_execution(**case.args)
    async with open_shared_ctx(str(case.fixture.db_path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await case.store.insert_root_binding_in_connection(
            db, binding=binding, user_id=case.fixture.member_id, assignment_id=case.expected.assignment_id, binding_source="mention"
        )
        await db.commit()
    path = tmp_path / "legacy.db"
    _historical_fixture(case.fixture.db_path, path, _OLD)
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT * FROM turn_agent_binding").fetchone()
        assert original is not None
        assert "primary_execution_id" not in {row[1] for row in db.execute("PRAGMA table_info(turn_agent_binding)")}
    _upgrade(path, _NEW)
    _upgrade(path, _NEW)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM turn_agent_binding").fetchone() == (*original, None)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (_NEW,)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_turn_agent_binding_primary_execution'").fetchone()
        assert any(
            row[2:5] == ("turn_agent_execution", "primary_execution_id", "id") for row in db.execute("PRAGMA foreign_key_list(turn_agent_binding)")
        )


@pytest.mark.parametrize("target", [target for target in TARGET_FILES if target != "core"])
def test_other_database_targets_only_advance_revision(tmp_path: Path, target: str) -> None:
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, _OLD, target)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    _upgrade(path, _NEW, target)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall() == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (_NEW,)


@pytest.mark.parametrize("drift", ["column", "object"])
def test_migration_refuses_drift_before_adding_any_schema(tmp_path: Path, drift: str) -> None:
    path = tmp_path / "bowhead.db"
    _upgrade(path, _OLD)
    with sqlite3.connect(path) as db:
        if drift == "column":
            db.execute("ALTER TABLE turn_agent_binding ADD COLUMN primary_execution_id INTEGER")
        else:
            db.execute("CREATE INDEX idx_turn_agent_binding_primary_execution ON turn_agent_binding(created_at)")
        before = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    with pytest.raises(RuntimeError, match="reference primary binding .* drift"):
        _upgrade(path, _NEW)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (_OLD,)
