"""Adopt released self-managed fields without losing values or row identities."""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import migrations

MIGRATION = importlib.import_module("bowhead_core.migrations.versions.0219_retire_unused_tables")
_FIELDS = {
    "skill_instances": [("import_status", "TEXT", "blocked"), ("import_report_json", "TEXT", '{ "kept": true }')],
    "memory_candidates": [
        ("memory_type", "TEXT", "procedural"),
        ("supersedes_json", "TEXT", '["mem_original"]'),
        ("origin", "TEXT NOT NULL DEFAULT 'dreaming_light'", "feedback_draft"),
    ],
    "memory_pack_snapshots": [("executor_block", "TEXT", "retained executor context")],
}


def _config(path):
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    return config


@pytest.fixture(scope="module")
def previous(tmp_path_factory):
    path = tmp_path_factory.mktemp("runtime-columns") / "previous.db"
    command.upgrade(_config(path), MIGRATION.down_revision)
    return path


@pytest.mark.parametrize("mode", ["missing", "all", "partial"])
def test_known_runtime_fields_and_optional_private_agents_survive_revision(previous, tmp_path, mode):
    path = tmp_path / "bowhead.db"
    with closing(sqlite3.connect(previous)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    existing = {}
    with closing(sqlite3.connect(path)) as db:
        for table, fields in _FIELDS.items():
            selected = fields if mode == "all" else fields[-1:] if mode == "partial" else []
            for name, ddl, value in selected:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                existing[table, name] = value
        db.execute(
            "INSERT INTO skill_instances(rowid,id,user_id,name,source_type,source_path,created_at,updated_at) VALUES (57,'skill','old-user','method','user_upload','relative/path','t','t')"
        )
        db.execute(
            "INSERT INTO memory_candidates(rowid,id,user_id,kind,key,value_json,created_at,updated_at) VALUES (57,'candidate','old-user','preference','kept','{}','t','t')"
        )
        db.execute(
            "INSERT INTO memory_pack_snapshots(rowid,turn_id,user_id,content,char_count,pack_version,created_at) VALUES (57,'turn','old-user','kept',4,'v1','t')"
        )
        for (table, name), value in existing.items():
            db.execute(f"UPDATE {table} SET {name}=?", (value,))
        for ddl in MIGRATION._CUSTOM_SCHEMA:
            db.execute(ddl)
        db.execute(
            "INSERT INTO custom_subagents(id,user_id,name,display_name,description,system_prompt,created_at,updated_at) VALUES ('legacy-agent','old-user','personal-method','Private method','Kept','Keep original prompt','t','t')"
        )
        db.commit()
    command.upgrade(_config(path), MIGRATION.revision)
    with closing(sqlite3.connect(path)) as db:
        for table, fields in _FIELDS.items():
            columns = list(db.execute(f'PRAGMA table_info("{table}")'))
            assert [row[1] for row in columns][-len(fields) :] == [field[0] for field in fields]
            assert all(row[2] == "TEXT" for row in columns[-len(fields) :])
            names = ",".join(name for name, _ddl, _value in fields)
            actual = db.execute(f'SELECT rowid,{names} FROM "{table}"').fetchone()
            expected = tuple(existing.get((table, name), "dreaming_light" if name == "origin" else None) for name, _ddl, _value in fields)
            assert actual == (57, *expected)
        assert db.execute("SELECT system_prompt FROM custom_subagents WHERE id='legacy-agent'").fetchone() == ("Keep original prompt",)
        assert db.execute("SELECT name FROM sqlite_schema WHERE name='trg_account_retired_custom_subagents_insert'").fetchone()
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_unreviewed_runtime_column_type_aborts_before_retirement(previous, tmp_path):
    path = tmp_path / "bowhead.db"
    with closing(sqlite3.connect(previous)) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
        target.execute("ALTER TABLE skill_instances ADD COLUMN import_status INTEGER")
    with pytest.raises(RuntimeError, match="Known runtime column drift"):
        command.upgrade(_config(path), MIGRATION.revision)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
        assert db.execute("SELECT name FROM sqlite_schema WHERE name='artifact_stdout'").fetchone()
