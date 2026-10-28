"""Copy migration preserves historical calls, receipts and charge identities."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_router.types import TokenUsage
from test_chat_reference_storage import _historical_fixture, _upgrade, assert_rows_survive_upgrade
from test_document_store import ctx as ctx
from test_document_store import migrated_db as migrated_db
from test_document_usage import SPEC, _hook
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0149_document_planning_attempts")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_planning_upgrade_preserves_all_targets_and_repeats(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert_rows_survive_upgrade(before, snapshot(path))
    current = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == current
    if target != "core":
        assert current == old_schema


@pytest.mark.parametrize("outcome", ["completed", "unknown", "late_known", "pending_settlement"])
async def test_planning_upgrade_retains_historical_usage_and_charge_ids(ctx, tmp_path, monkeypatch, outcome):
    from bowhead_core.documents import usage

    _, hook = await _hook(ctx)
    await hook.begin(SPEC)
    if outcome in {"unknown", "late_known"}:
        await hook.unknown(reason_code="transport_disconnected")
    if outcome == "pending_settlement":

        async def fail(*args, **kwargs):
            raise OSError("Settlement unavailable")

        monkeypatch.setattr(usage, "settle_document_usage", fail)
        with pytest.raises(OSError):
            await hook.complete(usage=TokenUsage(600, 200), response_digest="b" * 64)
    elif outcome != "unknown":
        await hook.complete(usage=TokenUsage(600, 200), response_digest="b" * 64)
    path = tmp_path / "historical.db"
    _historical_fixture(ctx.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert_rows_survive_upgrade(before, snapshot(path))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT attempt_no,state FROM document_model_calls").fetchone() == (
            1,
            "unknown" if outcome in {"unknown", "late_known"} else "completed",
        )
        assert db.execute("SELECT count(*) FROM document_turn_dispatches").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM document_planning_attempts").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        charges = db.execute("SELECT id FROM credit_ledger WHERE reason='template_document'").fetchall()
        assert charges == ([(f"document:{hook.job_id}:1",)] if outcome in {"completed", "late_known"} else [])


@pytest.mark.parametrize(
    "failure_point", ["DROP TABLE document_usage_receipts", "DROP TABLE document_model_calls", "DROP TRIGGER trg_document_execution_identity"]
)
def test_planning_copy_failure_restores_schema_and_version(tmp_path, monkeypatch, failure_point):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if statement == failure_point:
            raise RuntimeError("Injected planning migration failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="Injected planning"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.down_revision


@pytest.mark.parametrize("drift", ["missing_guard", "incoming_fk", "external_trigger"])
def test_planning_upgrade_rejects_unreviewed_schema_dependencies(tmp_path, drift):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if drift == "missing_guard":
            db.execute("DROP TRIGGER trg_document_usage_identity")
        elif drift == "incoming_fk":
            db.execute(
                "CREATE TABLE unreviewed(job_id TEXT,call_no INTEGER,FOREIGN KEY(job_id,call_no) REFERENCES document_model_calls(job_id,call_no))"
            )
        else:
            db.execute("CREATE TRIGGER unreviewed AFTER UPDATE ON document_jobs BEGIN SELECT count(*) FROM document_model_calls; END")
    before = schema(path)
    with pytest.raises(RuntimeError, match="drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before


def test_planning_copy_preserves_foreign_keys_when_connection_enforces_them(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    original = Connection.exec_driver_sql

    def enforcing(self, statement, *args, **kwargs):
        if statement == "SAVEPOINT document_planning_attempts":
            original(self, "PRAGMA foreign_keys=ON")
            assert original(self, "PRAGMA foreign_keys").scalar() == 1
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", enforcing)
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        refs = db.execute("PRAGMA foreign_key_list(document_usage_receipts)").fetchall()
        assert all(row[2] == "document_model_calls" for row in refs)
