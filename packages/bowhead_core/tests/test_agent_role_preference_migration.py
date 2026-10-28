"""M3-13D personal Agent role-preference migration contract."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module(
    "bowhead_core.migrations.versions.0053_agent_role_preference",
)


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(migrations.__file__).resolve().parent),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_0053_upgrade_is_empty_repeatable_and_at_head(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head; later historical revisions (e.g. 0063 table creation) may not rerun on upgraded DBs.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is always true and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT count(*) FROM agent_role_preference",
        ).fetchone() == (0,)
        assert {row[1] for row in db.execute("PRAGMA table_info(agent_role_preference)")} == {
            "team_id",
            "user_id",
            "role_id",
            "assignment_id",
            "assignment_cycle_started_at",
            "cleared_at",
            "row_version",
            "created_at",
            "updated_at",
        }


@pytest.mark.parametrize(
    "target",
    [
        "files",
        "doc_types",
        "extracted_records",
        "extracted_rows",
        "per_row_jobs",
        "team_templates",
        "matrix",
    ],
)
def test_0053_is_noop_for_non_core_targets(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='agent_role_preference'",
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("kind", ["table", "index", "trigger"])
def test_0053_rejects_same_name_schema_drift(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.revision)
    command.stamp(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if kind == "table":
            db.execute(
                "ALTER TABLE agent_role_preference ADD COLUMN unrelated TEXT",
            )
        elif kind == "index":
            db.execute("DROP INDEX idx_agent_role_preference_assignment")
            db.execute(
                "CREATE INDEX idx_agent_role_preference_assignment ON agent_role_preference(role_id)",
            )
        else:
            db.execute("DROP TRIGGER trg_agent_role_preference_update_guard")
            db.execute(
                "CREATE TRIGGER trg_agent_role_preference_update_guard "
                "BEFORE UPDATE ON agent_role_preference WHEN 0 "
                "BEGIN SELECT RAISE(ABORT, 'disabled'); END",
            )

    with pytest.raises(RuntimeError, match="Agent role preference schema drift"):
        command.upgrade(config, "head")
