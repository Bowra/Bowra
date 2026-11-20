"""Append the mutation protocol without changing data or historical split bytes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.sqlite_domain_upgrade import SPLIT_REVISION, SPLIT_TABLES, run_domain_upgrade, table_evidence
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.sqlite_upgrade import database_head
from test_sqlite_domain_upgrade import _advance
from test_sqlite_domain_upgrade import directory as directory
from test_sqlite_domain_upgrade import legacy_source as legacy_source


def evidence(directory):
    result = {}
    for domain, filename in DOMAIN_TARGET_FILES.items():
        with closing(sqlite3.connect(directory / filename)) as db:
            # Rows the split copied must survive; tables later revisions append start empty.
            result[domain] = {table: table_evidence(db, table) for table in SPLIT_TABLES[domain]}
    return result


def test_append_preserves_every_domain_row_and_restart_is_idempotent(directory):
    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    before = evidence(directory)
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert evidence(directory) == before
    assert all(database_head(directory / filename) == chain_head() for filename in DOMAIN_TARGET_FILES.values())
    with closing(sqlite3.connect(directory / "bowhead_execution.db")) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='trg_domain_mutation_turns_delete'").fetchone() == (1,)
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='trg_domain_mutation_document_executions_update'").fetchone()[0]
        assert "NOT EXISTS (SELECT 1 FROM document_draft_files WHERE job_id = OLD.job_id)" in sql
    state = (directory / "sqlite-layout.json").read_bytes()
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert (directory / "sqlite-layout.json").read_bytes() == state


def test_interrupted_trigger_replacement_rolls_back_and_resumes(directory, monkeypatch):
    from bowhead_core import sqlite_mutation_guards

    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    before = evidence(directory)
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        old_schema = db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' ORDER BY name").fetchall()
    original = sqlite_mutation_guards.mutation_triggers

    def interrupted(domain):
        for statement in original(domain):
            yield statement
            raise RuntimeError("interrupted mutation trigger replacement")

    monkeypatch.setattr(sqlite_mutation_guards, "mutation_triggers", interrupted)
    with pytest.raises(RuntimeError, match="interrupted mutation trigger replacement"):
        upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert database_head(directory / "bowhead.db") == SPLIT_REVISION
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        assert db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' ORDER BY name").fetchall() == old_schema
    assert evidence(directory) == before
    monkeypatch.setattr(sqlite_mutation_guards, "mutation_triggers", original)
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    assert json.loads((directory / "sqlite-layout.json").read_text())["status"] == "ready"
    assert evidence(directory) == before


def test_chain_head_leaves_space_revision_and_grant_usage_unfenced(directory):
    from bowhead_core.sqlite_mutation_guards import mutation_triggers

    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    expected = {statement.split('"')[1]: statement for statement in mutation_triggers("core")}
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        for table, column in (("workspaces", "revision"), ("builtin_tool_user_grant", "last_used_at")):
            name = f"trg_domain_mutation_{table}_update"
            sql = db.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).fetchone()[0]
            assert f'"{column}"' not in sql
            assert '"status"' in sql
            assert sql == expected[name]


class _Bind:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def exec_driver_sql(self, sql: str):
        return self.db.execute(sql)


def _core_with_triggers(path, *, unfenced: dict[str, set[str]]) -> sqlite3.Connection:
    from bowhead_core.sqlite_domain_runtime import _AUTHORITY

    db = sqlite3.connect(path, isolation_level=None)
    db.create_function("bowhead_domain_mutation_v2", 2, lambda table, operation: 1)
    for table in ("workspaces", "builtin_tool_user_grant"):
        columns = list(_AUTHORITY["core"][table])
        db.execute(f'CREATE TABLE "{table}"({",".join(f"{column!r} TEXT" for column in columns)})'.replace("'", '"'))
        fenced = [column for column in columns if column not in unfenced.get(table, set())]
        names = ",".join(f'"{column}"' for column in fenced)
        changed = " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in fenced)
        db.execute(
            f'CREATE TRIGGER "trg_domain_mutation_{table}_update" BEFORE UPDATE OF {names} ON "{table}" WHEN {changed} '
            f"BEGIN SELECT bowhead_domain_mutation_v2('{table}','update'); END"
        )
    return db


def _run_0230(monkeypatch, db: sqlite3.Connection, target: str = "core") -> None:
    import importlib
    from types import SimpleNamespace

    migration = importlib.import_module("bowhead_core.migrations.versions.0230_unfence_routine_metadata")
    context = SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": target}), get_bind=lambda: _Bind(db))
    monkeypatch.setattr(migration, "context", context)
    migration.upgrade()


def _update_triggers(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND name LIKE '%_update' ORDER BY name").fetchall()


def test_0230_upgrades_deployed_fences_and_matches_a_fresh_install(tmp_path, monkeypatch):
    deployed = _core_with_triggers(
        tmp_path / "deployed.db", unfenced={"workspaces": {"name", "name_key", "description", "last_synced_at", "updated_at"}}
    )
    fresh = _core_with_triggers(
        tmp_path / "fresh.db",
        unfenced={
            "workspaces": {"name", "name_key", "description", "last_synced_at", "updated_at", "revision"},
            "builtin_tool_user_grant": {"last_used_at"},
        },
    )
    fresh_before = _update_triggers(fresh)
    with closing(deployed), closing(fresh):
        assert all('"revision"' in sql or '"last_used_at"' in sql for _, sql in _update_triggers(deployed))
        _run_0230(monkeypatch, deployed)
        _run_0230(monkeypatch, fresh)
        assert _update_triggers(deployed) == _update_triggers(fresh) == fresh_before
        _run_0230(monkeypatch, deployed, target="execution")
        assert _update_triggers(deployed) == fresh_before


def test_0230_interrupted_replacement_keeps_the_deployed_fences(tmp_path, monkeypatch):
    deployed = _core_with_triggers(
        tmp_path / "deployed.db", unfenced={"workspaces": {"name", "name_key", "description", "last_synced_at", "updated_at"}}
    )
    with closing(deployed):
        before = _update_triggers(deployed)
        import importlib

        migration = importlib.import_module("bowhead_core.migrations.versions.0230_unfence_routine_metadata")
        original = migration.update_trigger
        calls = []

        def interrupted(table):
            calls.append(table)
            if len(calls) == 2:
                raise RuntimeError("interrupted fence replacement")
            return original(table)

        monkeypatch.setattr(migration, "update_trigger", interrupted)
        with pytest.raises(RuntimeError, match="interrupted fence replacement"):
            _run_0230(monkeypatch, deployed)
        assert _update_triggers(deployed) == before


def test_chain_head_fences_user_settings_only_under_privacy_and_access_keys(directory):
    from bowhead_core.sqlite_mutation_guards import mutation_triggers

    run_domain_upgrade(directory, _advance, target_head=SPLIT_REVISION)
    upgrade(f"sqlite:///{directory / 'bowhead.db'}")
    expected = {statement.split('"')[1]: statement for statement in mutation_triggers("core") if ' ON "user_settings"' in statement}
    with closing(sqlite3.connect(directory / "bowhead.db")) as db:
        installed = dict(
            db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='user_settings' AND name GLOB 'trg_domain_mutation_*'")
        )
    assert installed == expected
    assert all("'memory_dreaming_paused'" in sql and "kb_file_scope" not in sql for sql in installed.values())


def _settings_with_fences(path, *, scoped: bool) -> sqlite3.Connection:
    """A core file with ``user_settings`` fenced as deployed before ``0231`` or as a fresh install generates it."""
    from bowhead_core.sqlite_domain_runtime import _AUTHORITY
    from bowhead_core.sqlite_mutation_guards import mutation_triggers

    db = sqlite3.connect(path, isolation_level=None)
    db.create_function("bowhead_domain_mutation_v2", 2, lambda table, operation: 1)
    db.execute("CREATE TABLE user_settings(user_id TEXT,key TEXT,value TEXT,updated_at TEXT,PRIMARY KEY(user_id,key))")
    if scoped:
        statements = [statement for statement in mutation_triggers("core") if ' ON "user_settings"' in statement]
    else:
        # The 0221 shape: unconditional insert and delete fences, an update fence on any changed column.
        columns = _AUTHORITY["core"]["user_settings"]
        names = ",".join(f'"{column}"' for column in columns)
        changed = " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in columns)
        statements = [
            f'CREATE TRIGGER "trg_domain_mutation_user_settings_{suffix}" BEFORE {operation} ON "user_settings"{condition} '
            f"BEGIN SELECT bowhead_domain_mutation_v2('user_settings','{suffix}'); END"
            for suffix, operation, condition in (
                ("delete", "DELETE", ""),
                ("insert", "INSERT", ""),
                ("update", f"UPDATE OF {names}", f" WHEN {changed}"),
            )
        ]
    for statement in statements:
        db.execute(statement)
    return db


def _settings_triggers(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='user_settings' ORDER BY name").fetchall()


def _run_0231(monkeypatch, db: sqlite3.Connection, target: str = "core") -> None:
    import importlib
    from types import SimpleNamespace

    migration = importlib.import_module("bowhead_core.migrations.versions.0231_user_settings_key_fence")
    context = SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": target}), get_bind=lambda: _Bind(db))
    monkeypatch.setattr(migration, "context", context)
    migration.upgrade()


def test_0231_scopes_deployed_fences_and_matches_a_fresh_install(tmp_path, monkeypatch):
    deployed = _settings_with_fences(tmp_path / "deployed.db", scoped=False)
    fresh = _settings_with_fences(tmp_path / "fresh.db", scoped=True)
    fresh_before = _settings_triggers(fresh)
    with closing(deployed), closing(fresh):
        assert all('"key" IN' not in sql for _, sql in _settings_triggers(deployed))
        assert 'WHEN OLD."user_id" IS NOT NEW."user_id"' in dict(_settings_triggers(deployed))["trg_domain_mutation_user_settings_update"]
        _run_0231(monkeypatch, deployed)
        _run_0231(monkeypatch, fresh)
        assert _settings_triggers(deployed) == _settings_triggers(fresh) == fresh_before
        _run_0231(monkeypatch, deployed, target="execution")
        assert _settings_triggers(deployed) == fresh_before
        deployed.execute("INSERT INTO user_settings VALUES('u','kb_file_scope','team','t')")
        assert deployed.execute("SELECT value FROM user_settings").fetchall() == [("team",)]


def test_0231_interrupted_replacement_keeps_the_deployed_fences(tmp_path, monkeypatch):
    import importlib

    deployed = _settings_with_fences(tmp_path / "deployed.db", scoped=False)
    with closing(deployed):
        before = _settings_triggers(deployed)
        migration = importlib.import_module("bowhead_core.migrations.versions.0231_user_settings_key_fence")
        original = migration.trigger
        calls = []

        def interrupted(suffix):
            calls.append(suffix)
            if len(calls) == 3:
                raise RuntimeError("interrupted fence replacement")
            return original(suffix)

        monkeypatch.setattr(migration, "trigger", interrupted)
        with pytest.raises(RuntimeError, match="interrupted fence replacement"):
            _run_0231(monkeypatch, deployed)
        assert _settings_triggers(deployed) == before
