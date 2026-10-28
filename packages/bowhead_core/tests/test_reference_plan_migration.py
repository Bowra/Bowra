"""Existing installations retain data while adding the exact Plan step fence."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
REVISION = import_module("bowhead_core.migrations.versions.0133_reference_plan_steps")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_reference_plan_upgrade_keeps_existing_data(tmp_path, target):
    path = tmp_path / "existing.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        if target == "core":
            db.execute(
                "INSERT INTO plans(id,session_id,workspace_id,user_id,title,created_at,updated_at,spec_json) VALUES('plan_old','s','w','u','Existing','2026-10-01','2026-10-01','')"
            )
        before = [line for line in db.iterdump() if line.startswith("INSERT INTO") and '"alembic_version"' not in line]
    command.upgrade(config, REVISION.revision)
    command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.revision,)
        assert [line for line in db.iterdump() if line.startswith("INSERT INTO") and '"alembic_version"' not in line] == before
        if target == "core":
            assert db.execute("SELECT COUNT(*) FROM chat_reference_plan_step_scope").fetchone() == (0,)
            db.execute("UPDATE plans SET spec_json='{}' WHERE id='plan_old'")
            assert db.execute("SELECT COUNT(*) FROM chat_reference_plan_step_scope").fetchone() == (0,)


def test_reference_plan_upgrade_refuses_constraint_drift(tmp_path):
    path = tmp_path / "drift.db"
    config = _config(path, "core")
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER trg_reference_model_context_scope")
    with pytest.raises(RuntimeError, match="schema drift"):
        command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.down_revision,)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='chat_reference_plan_step_scope'").fetchone() is None
