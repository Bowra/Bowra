"""Coach edit JSON guards preserve historical proposals across all database targets."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_revision_store import _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0196_coach_case_edits")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_existing_data_and_other_targets_are_preserved(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
        with sqlite3.connect(path) as db:
            db.execute(
                """INSERT INTO agent_change_proposal(
                id,team_id,agent_id,source_type,source_ref,base_document_json,base_document_hash,
                patch_json,patch_hash,atomic_groups_json,suggested_cases_json,rationale,provenance_json,
                status,row_version,created_by_kind,created_by_id,created_at
            ) VALUES('acp_historical','team_release','osa_release','coach','historical','{}',?,
                     '[]',?,'[]','[]','Historical suggestion','{}','open',1,'user','user_owner','2026-10-01')""",
                ("sha256:" + "1" * 64, "sha256:" + "2" * 64),
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    installed = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_guard_installation_is_atomic(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.startswith("CREATE TRIGGER " + MIGRATION.TRIGGER):
            raise RuntimeError("Injected guard installation failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="Injected"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
