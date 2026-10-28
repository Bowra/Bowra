"""0066 is an immutable, core-only expansion with no guessed historical usage."""

from __future__ import annotations

import sqlite3

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_admission_transaction import _team_admission_case
from test_chat_reference_storage import _historical_fixture, _upgrade

pytestmark = pytest.mark.integration
_OLD = "0065_reference_primary_execution"
_NEW = "0066_reference_runtime"


@pytest.mark.asyncio
async def test_upgrade_preserves_old_tables_without_backfill(tmp_path):
    # Real admission writes on the current schema, then the rows are exported into
    # the actual 0065 schema: current writers require the current schema (e.g.
    # ``turn_agent_execution.resolved_definition_json`` from 0123).
    case = await _team_admission_case(tmp_path)
    await case.store.admit_team_execution(**case.args)
    path = tmp_path / "legacy.db"
    _historical_fixture(case.fixture.db_path, path, _OLD)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == (1,)
        tables = [
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'")
        ]
        before = {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    _upgrade(path, _NEW)
    _upgrade(path, _NEW)
    with sqlite3.connect(path) as db:
        assert {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in tables} == before
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_evidence").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("target", [t for t in TARGET_FILES if t != "core"])
def test_non_core_targets_advance_without_runtime_objects(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, _OLD, target)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    _upgrade(path, _NEW, target)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall() == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (_NEW,)


@pytest.mark.parametrize(
    "object_name",
    [
        "chat_reference_resolutions",
        "idx_chat_reference_evidence_expiry",
        "trg_reference_primary_completion",
        "trg_chat_reference_evidence_no_active_delete",
    ],
)
def test_drift_is_detected_before_any_ddl(tmp_path, object_name):
    path = tmp_path / "bowhead.db"
    _upgrade(path, _OLD)
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {object_name}(x TEXT)")
        before = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    with pytest.raises(RuntimeError, match="reference runtime schema drift"):
        _upgrade(path, _NEW)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == before
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (_OLD,)
