"""Collaboration storage upgrades preserve historical work and roll back drift."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import TARGET_FILES

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0165_agent_collaboration")


def config(path: Path, target: str = "core") -> Config:
    result = Config()
    result.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    result.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    result.attributes["bowhead_target"] = target
    return result


@pytest.fixture(scope="module")
def previous_core(tmp_path_factory):
    path = tmp_path_factory.mktemp("collaboration_migration") / "core.db"
    command.upgrade(config(path), MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams(id,name,created_at) VALUES ('team_prior','Existing team','2026-10-09')")
    return path


def copy_core(previous_core, tmp_path):
    path = tmp_path / "core.db"
    with sqlite3.connect(previous_core) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    return path


def test_previous_revision_upgrades_without_backfill_and_repeat_upgrade_is_safe(previous_core, tmp_path):
    path = copy_core(previous_core, tmp_path)
    command.upgrade(config(path), "head")
    command.upgrade(config(path), "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM teams WHERE id='team_prior'").fetchone() == ("Existing team",)
        for name in ("turn", "participant", "operation"):
            assert db.execute(f"SELECT COUNT(*) FROM agent_collaboration_{name}").fetchone() == (0,)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_upgrade_rolls_back_all_new_tables_and_keeps_existing_data(previous_core, tmp_path):
    path = copy_core(previous_core, tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE INDEX idx_agent_collaboration_pending ON turns(id)")
    with pytest.raises(Exception, match="already exists"):
        command.upgrade(config(path), MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_collaboration_%'").fetchall() == []
        assert db.execute("SELECT name FROM teams WHERE id='team_prior'").fetchone() == ("Existing team",)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.parametrize("target", tuple(TARGET_FILES))
def test_all_targets_advance_and_only_core_receives_collaboration_tables(tmp_path, target):
    path = tmp_path / "database.db"
    command.upgrade(config(path, target), "head")
    with sqlite3.connect(path) as db:
        names = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_collaboration_%'").fetchall()
        assert len(names) == (3 if target == "core" else 0)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (migrations.chain_head(),)
