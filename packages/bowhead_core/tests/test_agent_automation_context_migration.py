"""Upgrade every database target without inventing historical CI provenance."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_eval_policy_plan_store import _comparison_context, _seed
from test_agent_owner_eval_foundation_migration import _rows, _schema, _upgrade

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0137_agent_automation_context")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_existing_rows_and_is_repeatable(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, schema = _rows(path), _schema(path)
    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)
    after = _rows(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        if target == "core":
            assert "agent_version_automation_preparation" in after
            assert {"client_intent_hash", "automation_context_json"} <= {
                row[1] for row in db.execute("PRAGMA table_info(agent_eval_comparison_execution)")
            }
        else:
            assert schema == _schema(path)
    for name, rows in before.items():
        assert [row[: len(rows[0])] for row in after[name]] == rows if rows else after[name] == []


@pytest.mark.asyncio
async def test_historical_automation_execution_remains_unproven(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    _seed(path)
    plan, _ = await _comparison_context(path)
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO agent_eval_comparison_execution(id,team_id,agent_id,candidate_id,plan_id,run_kind,selection_json,
            selection_hash,status,idempotency_hash,request_hash,actor_kind,actor_ref,automation_grant_id,generation,row_version,requested_at)
            VALUES('aex_legacy','team_p','osa_policy','arc_policy',?,'release','{}','sha256:selection','queued','sha256:key','sha256:request','automation','oak_legacy','avag_legacy',1,1,'2026-09-14T10:00:00+00:00')""",
            (plan.id,),
        )
        before = db.execute("SELECT * FROM agent_eval_comparison_execution").fetchone()
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT * FROM agent_eval_comparison_execution").fetchone()
        assert after[: len(before)] == before
        assert after[-2:] == (None, None)
        assert db.execute("SELECT COUNT(*) FROM agent_version_automation_preparation").fetchone()[0] == 0


def test_failed_installation_leaves_no_partial_columns_or_triggers(tmp_path):
    from sqlalchemy.exc import OperationalError

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE agent_version_automation_preparation(collision TEXT)")
    before = _schema(path)
    with pytest.raises(OperationalError, match="already exists"):
        _upgrade(path, MIGRATION.revision)
    assert _schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision
        db.execute("DROP TABLE agent_version_automation_preparation")
    _upgrade(path, MIGRATION.revision)
