import sqlite3
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from test_office_upload_confirmation_migration import _config

REVISION = import_module("bowhead_core.migrations.versions.0174_slack_oauth_entry")
pytestmark = pytest.mark.integration


def test_slack_entry_migration_rolls_back_partial_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.exec_driver_sql("CREATE TABLE slack_oauth_states(state TEXT PRIMARY KEY)")
        monkeypatch.setattr(REVISION, "context", SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": "core"}), get_bind=lambda: db))
        with pytest.raises(OperationalError):
            REVISION.upgrade()
        assert [row[1] for row in db.exec_driver_sql("PRAGMA table_info(slack_oauth_states)")] == ["state"]
    engine.dispose()


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_slack_entry_migration_preserves_legacy_rows_and_advances_all_targets(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if target == "core":
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("INSERT INTO teams(id,name,created_at) VALUES('team_old','Old','2026-10-14')")
            db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES('usr_old','team_old','old@example.com','2026-10-14')")
            db.execute(
                "INSERT INTO slack_connections(id,team_id,created_by_user_id,updated_by_user_id,created_at,updated_at) VALUES('slc_old','team_old','usr_old','usr_old','2026-10-14','2026-10-14')"
            )
            db.execute("INSERT INTO slack_oauth_states(state,connection_id,expires_at,created_at) VALUES('old','slc_old','2099-01-01','2026-10-14')")
            db.execute(
                "INSERT INTO personal_slack_oauth_states(state,connection_id,owner_user_id,team_id,expires_at,created_at) VALUES('old','slc_old','usr_old','team_old','2099-01-01','2026-10-14')"
            )
    command.upgrade(config, REVISION.revision)
    command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.revision,)
        if target == "core":
            assert db.execute("SELECT state,connection_id,expires_at,created_at,redirect_uri FROM slack_oauth_states").fetchone() == (
                "old",
                "slc_old",
                "2099-01-01",
                "2026-10-14",
                None,
            )
            assert db.execute("SELECT state,connection_id,owner_user_id,team_id,redirect_uri FROM personal_slack_oauth_states").fetchone() == (
                "old",
                "slc_old",
                "usr_old",
                "team_old",
                None,
            )
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        else:
            assert (
                db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                == before
            )
