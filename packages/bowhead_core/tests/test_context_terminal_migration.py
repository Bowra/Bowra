"""Maintenance upgrades retain prior Terminal bytes and ordinary answer constraints."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.events import OrchestratorEvent
from test_chat_reference_storage import _upgrade
from test_skill_invocation_migration import schema
from test_terminal_runtime import _create_db, _runtime

MIGRATION = import_module("bowhead_core.migrations.versions.0207_context_maintenance_terminal")
pytestmark = pytest.mark.integration


def prior_ledger_schema(path):
    """Express a real ordinary receipt using the unchanged pre-upgrade columns."""
    recorded = MIGRATION._Statements()
    import_module("bowhead_core.migrations.versions.0008_terminal_protocol_v1")._create_protocol_tables(recorded)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for table in MIGRATION.TABLES:
            objects = db.execute(
                "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL AND name NOT LIKE 'trg_context_maintenance_%'",
                (table,),
            ).fetchall()
            columns = ",".join(row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[1] != "operation_kind")
            db.execute(f"CREATE TEMP TABLE saved AS SELECT {columns} FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(next(sql for sql in recorded.values if f"CREATE TABLE IF NOT EXISTS {table} (" in sql))
            db.execute(f"INSERT INTO {table} SELECT * FROM saved")
            db.execute("DROP TABLE saved")
            for (sql,) in objects:
                db.execute(sql)
        db.execute("UPDATE alembic_version SET version_num=?", (MIGRATION.down_revision,))


def table_snapshot(db, table):
    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    return columns, db.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()


async def test_upgrade_preserves_real_receipt_digests_and_replay(tmp_path):
    path = _create_db(tmp_path)
    runtime = await _runtime(path)
    await runtime.bus.emit(OrchestratorEvent("final_answer", "t_1", {"text": "Verified answer."}))
    await runtime.bus.emit(OrchestratorEvent("done", "t_1", {"status": "completed"}))
    receipt = runtime.bus.receipt
    prior_ledger_schema(path)
    with sqlite3.connect(path) as db:
        before = {table: table_snapshot(db, table) for table in MIGRATION.TABLES}
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        for table, (columns, rows) in before.items():
            assert db.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall() == rows
            assert db.execute(f"SELECT operation_kind FROM {table}").fetchall() == [(None,)] * len(rows)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    confirmed = await runtime.store.confirm_commit(candidate_id=receipt.candidate_id, candidate_projection_digest=receipt.candidate_projection_digest)
    assert confirmed.receipt == receipt
    installed = schema(path)
    _upgrade(path, MIGRATION.revision)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        original = dict(db.execute("SELECT * FROM turn_terminal_candidate_variants WHERE status='completed'").fetchone())
        for kind in (None, "context_maintenance", "unknown"):
            values = {**original, "variant_key": "forged", "final_answer_mode": "none", "final_answer_event_id": None, "final_answer_digest": None}
            if kind is None:
                values.pop("operation_kind")
            else:
                values["operation_kind"] = kind
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    f"INSERT INTO turn_terminal_candidate_variants({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_context_terminal_upgrade_is_target_scoped(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    if target != "core":
        assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("failed_table", MIGRATION.TABLES)
def test_context_terminal_rebuild_failure_restores_both_tables(tmp_path, monkeypatch, failed_table):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql == f"DROP TABLE {failed_table}":
            raise RuntimeError("injected terminal rebuild failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="injected terminal rebuild failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
