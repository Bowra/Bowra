"""Migration 0157 marks session titles as automatic or user-set without rewriting rows."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.session_store import SESSION_TITLE_SOURCE_COLUMN_DDL, SessionStore
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0157_session_title_source")
#: The frozen `1.0.0` baseline revision that existing deployments adopt before later revisions.
BASELINE = import_module("bowhead_core.migrations.versions.0001_initial")
pytestmark = pytest.mark.integration


def _columns(path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [row[1] for row in db.execute("PRAGMA table_info(sessions)")]


def _seed_session(db: sqlite3.Connection, session_id: str = "s_old", title: str | None = "Old title") -> None:
    db.execute(
        "INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at) VALUES(?,?,?,?,?,?)",
        (session_id, "w1", "u1", title, "2026-01-01", "2026-01-02"),
    )


def test_store_initializer_mirrors_the_migration_statement() -> None:
    assert SESSION_TITLE_SOURCE_COLUMN_DDL == MIGRATION.TITLE_SOURCE_COLUMN_DDL


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_rows_and_adds_title_source_only_to_core(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            _seed_session(db)
    before = snapshot(path)
    previous = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    installed = schema(path)
    if target == "core":
        old_rows = before.pop("sessions")
        new_rows = after.pop("sessions")
        assert len(old_rows) == len(new_rows) == 1
        assert _columns(path)[-1] == "title_source"
        with sqlite3.connect(path) as db:
            # Historical titles stay, and every existing row reads as automatic.
            assert db.execute("SELECT title,title_source FROM sessions").fetchone() == ("Old title", "auto")
        assert {row[1] for row in set(installed) - set(previous)} == {"sessions"}
    else:
        assert installed == previous
    assert after == before
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)


def test_empty_database_and_the_1_0_0_baseline_both_reach_head_with_title_source(tmp_path):
    empty = tmp_path / "empty.db"
    _upgrade(empty, "head")
    baseline = tmp_path / "baseline.db"
    _upgrade(baseline, BASELINE.revision)
    with sqlite3.connect(baseline) as db:
        _seed_session(db, "s_baseline", None)
    _upgrade(baseline, "head")
    _upgrade(baseline, "head")
    for path in (empty, baseline):
        assert "title_source" in _columns(path)
    with sqlite3.connect(baseline) as db:
        assert db.execute("SELECT title_source FROM sessions WHERE id='s_baseline'").fetchone() == ("auto",)
    with sqlite3.connect(empty) as a, sqlite3.connect(baseline) as b:
        query = "SELECT sql FROM sqlite_master WHERE name='sessions'"
        assert a.execute(query).fetchall() == b.execute(query).fetchall()


def test_title_source_rejects_unknown_values(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        _seed_session(db)
        db.execute("UPDATE sessions SET title_source='user' WHERE id='s_old'")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute("UPDATE sessions SET title_source='model' WHERE id='s_old'")


async def test_store_initializer_and_migration_share_the_column_in_either_order(tmp_path):
    """Dual ownership: a store-initialized database upgrades cleanly, and vice versa."""

    initialized_first = tmp_path / "initialized.db"
    _upgrade(initialized_first, MIGRATION.down_revision, "core")
    await SessionStore(initialized_first).initialize()
    assert "title_source" in _columns(initialized_first)
    _upgrade(initialized_first, MIGRATION.revision, "core")
    migrated_first = tmp_path / "migrated.db"
    _upgrade(migrated_first, "head", "core")
    await SessionStore(migrated_first).initialize()
    for path in (initialized_first, migrated_first):
        assert _columns(path).count("title_source") == 1


async def test_store_rename_marks_the_title_user_set_and_blocks_automatic_writes(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, "head", "core")
    store = SessionStore(path)
    await store.initialize()
    await store.ensure_session(session_id="s_store", workspace_id="w1", user_id="u1")
    assert await store.set_title(session_id="s_store", title="Automatic") is True
    assert await store.rename_session(user_id="u_other", session_id="s_store", title="Hijack") is None
    renamed = await store.rename_session(user_id="u1", session_id="s_store", title="Mine")
    assert renamed is not None and renamed.title == "Mine"
    assert await store.set_title(session_id="s_store", title="Automatic again") is False
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT title,title_source FROM sessions WHERE id='s_store'").fetchone() == ("Mine", "user")


async def test_touch_session_only_writes_automatic_titles(tmp_path):
    """touch_session reads the title before its UPDATE; a user-set row must keep its title."""

    path = tmp_path / "core.db"
    _upgrade(path, "head", "core")
    store = SessionStore(path)
    await store.initialize()
    for session_id in ("s_auto", "s_user"):
        await store.ensure_session(session_id=session_id, workspace_id="w1", user_id="u1")
    with sqlite3.connect(path) as db:
        # Simulate the read-then-write window: the row is user-set although the read saw no title.
        db.execute("UPDATE sessions SET title=NULL, title_source='user' WHERE id='s_user'")
        db.execute("UPDATE sessions SET title=NULL WHERE id='s_auto'")
    for session_id in ("s_auto", "s_user"):
        await store.touch_session(session_id=session_id, title_hint="Hint title")
    with sqlite3.connect(path) as db:
        rows = dict(db.execute("SELECT id,title FROM sessions WHERE id IN ('s_auto','s_user')").fetchall())
    assert rows == {"s_auto": "Hint title", "s_user": None}


def test_failed_installation_leaves_no_partial_column(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    execute = Connection.exec_driver_sql

    def interrupted(self, statement, *args, **kwargs):
        if "ADD COLUMN title_source" in statement:
            raise RuntimeError("injected migration failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", interrupted)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        _upgrade(path, MIGRATION.revision, "core")
    assert schema(path) == before
    assert "title_source" not in _columns(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
