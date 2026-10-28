"""Coach CI schema evolution preserves historic comparison provenance."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_eval_policy_plan_store import _comparison_context, _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0143_coach_ci_delegation")


@pytest.mark.parametrize("target", list(TARGET_FILES))
async def test_internal_ci_migration_preserves_historical_origin(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
        plan, _control = await _comparison_context(path)
        with sqlite3.connect(path) as db:
            db.execute(
                """INSERT INTO agent_eval_comparison_execution(
                   id,team_id,agent_id,candidate_id,plan_id,run_kind,selection_json,selection_hash,
                   status,idempotency_hash,request_hash,actor_kind,actor_ref,requested_at)
                   VALUES('aex_historical','team_p','osa_policy','arc_policy',?,'release','{}',?,
                          'queued',?,?,'user','user_owner','2026-10-01')""",
                (plan.id, "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64),
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    for name, rows in before.items():
        if name == "agent_eval_comparison_execution":
            assert [row[:-2] for row in after[name]] == rows
            assert all(row[-2:] == (None, None) for row in after[name])
        else:
            assert after[name] == rows
    shape = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == shape
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision


def test_internal_ci_migration_failure_rolls_back_added_columns_and_tables(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    monkeypatch.setattr(MIGRATION, "_DDL", (*MIGRATION._DDL[:-1], "invalid sql"))
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}),
                get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute),
            ),
        )
        with pytest.raises(sqlite3.OperationalError):
            MIGRATION.upgrade()
    assert schema(path) == before
