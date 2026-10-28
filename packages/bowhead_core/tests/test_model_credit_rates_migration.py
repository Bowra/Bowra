"""V2 pricing starts at creation; historical work keeps its original version."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
REVISION = import_module("bowhead_core.migrations.versions.0173_model_credit_rates")
TARGETS = ("core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix")


@pytest.mark.parametrize("target", TARGETS)
def test_upgrade_preserves_old_work_and_advances_every_target(tmp_path, target):
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        if target == "core":
            db.execute("INSERT INTO teams(id,name,created_at) VALUES('team_old','Old','2026-10-01')")
            db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES('user_old','team_old','old@example.com','2026-10-01')")
            db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_old','team_old','Old','2026-10-01','2026-10-01')")
            db.execute(
                "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES('sess_old','user_old','ws_old','2026-10-01','2026-10-01')"
            )
            db.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,started_at,status) VALUES('turn_old','sess_old',1,'Old','2026-10-01','completed')"
            )
        elif target == "matrix":
            pass
    command.upgrade(config, REVISION.revision)
    command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.revision,)
        if target == "core":
            assert db.execute("SELECT billing_rate_card_version FROM turns WHERE id='turn_old'").fetchone() == (None,)
            db.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,started_at,status) VALUES('turn_new','sess_old',2,'New','2026-10-14','running')"
            )
            assert db.execute("SELECT billing_rate_card_version FROM turns WHERE id='turn_new'").fetchone() == ("v2",)
            assert "credit_model_usage" in {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        elif target == "matrix":
            assert "billing_rate_card_version" in {row[1] for row in db.execute("PRAGMA table_info(kb_matrix_runs)")}
