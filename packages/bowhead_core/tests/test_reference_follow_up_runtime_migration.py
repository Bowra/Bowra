"""0092 admits only queue-proven reference follow-up dispatches."""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module(
    "bowhead_core.migrations.versions.0092_reference_follow_up_runtime",
)
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_replaces_only_core_runtime_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)

    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)

    assert snapshot(path) == before
    changed = set(MIGRATION.CURRENT) if target == "core" else set()
    assert [row for row in schema(path) if row[1] not in changed] == [row for row in previous if row[1] not in changed]
    assert len(schema(path)) == len(previous)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            actual = dict(
                db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in MIGRATION.DDL) + ")",
                    tuple(MIGRATION.DDL),
                ).fetchall()
            )
            assert {name: MIGRATION._normalized(sql) for name, sql in actual.items()} == {
                name: MIGRATION._normalized(sql) for name, sql in MIGRATION.DDL.items()
            }


@pytest.mark.parametrize("failure", ["drift", "create"])
def test_runtime_guard_replacement_is_atomic(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    changed_name = next(iter(MIGRATION.CURRENT))
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute(f"DROP TRIGGER {changed_name}")
            db.execute(
                f"CREATE TRIGGER {changed_name} BEFORE INSERT ON chat_reference_resolutions BEGIN SELECT 1; END",
            )
    before, previous = snapshot(path), schema(path)
    original = Connection.exec_driver_sql
    injected_name = next(reversed(MIGRATION.CURRENT))

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "create" and statement.strip().startswith(
            f"CREATE TRIGGER {injected_name} ",
        ):
            raise RuntimeError("injected follow-up runtime migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before
    assert schema(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


def test_every_follow_up_trigger_requires_parent_and_child_turns_in_queue_session() -> None:
    for sql in MIGRATION.DDL.values():
        normalized = MIGRATION._normalized(sql)
        assert "join turns qp on qp.id=q.accepted_parent_turn_id" in normalized
        assert "join turns qc on qc.id=q.materialized_turn_id" in normalized
        assert "qp.session_id=q.session_id" in normalized
        assert "qc.session_id=q.session_id" in normalized
