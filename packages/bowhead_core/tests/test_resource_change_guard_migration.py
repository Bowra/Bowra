"""WP5 resource-change guard migration contract."""

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
    "bowhead_core.migrations.versions.0054_resource_change_guard",
)


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_0054_upgrade_is_repeatable_and_at_head(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head; later historical revisions (e.g. 0063 table creation) may not be rerunnable on an upgraded database.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is tautological and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")

    with sqlite3.connect(path) as db:
        assert {row[1] for row in db.execute("PRAGMA table_info(resource_change_receipt)")} == {
            "team_id",
            "actor_user_id",
            "idempotency_key",
            "resource_kind",
            "resource_id",
            "action",
            "request_digest",
            "impact_digest",
            "response_json",
            "created_at",
        }
        assert {row[1] for row in db.execute("PRAGMA table_info(resource_refresh_candidate)")} == {
            "team_id",
            "actor_user_id",
            "resource_id",
            "candidate_digest",
            "resource_state_digest",
            "candidate_json",
            "expires_at",
            "created_at",
        }


@pytest.mark.parametrize(
    "target",
    ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"],
)
def test_0054_is_noop_for_non_core_targets(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='resource_change_receipt'").fetchone() is None


@pytest.mark.parametrize("name", ["resource_change_receipt", "resource_refresh_candidate"])
def test_0054_rejects_same_name_table_drift(tmp_path: Path, name: str) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.revision)
    command.stamp(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute(f"ALTER TABLE {name} ADD COLUMN unrelated TEXT")

    with pytest.raises(RuntimeError, match="Resource change guard schema drift"):
        command.upgrade(config, "head")
