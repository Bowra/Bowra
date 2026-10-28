"""Acceptances gain immutable content proof without invented historical proof."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_revision_store import _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0144_proposal_content_receipt")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_content_receipt_preserves_historical_decisions(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
        with sqlite3.connect(path) as db:
            db.execute(
                """INSERT INTO agent_change_proposal(id,team_id,agent_id,source_type,source_ref,base_document_json,
                       base_document_hash,patch_json,patch_hash,atomic_groups_json,suggested_cases_json,rationale,provenance_json,
                       status,row_version,created_by_kind,created_by_id,created_at)
                       VALUES('acp_old','team_release','osa_release','coach','old','{}',?,'[]',?,'[]','[]','Historical','{}',
                              'accepted',2,'user','user_owner','2026-10-01')""",
                ("sha256:" + "a" * 64, "sha256:" + "b" * 64),
            )
            db.execute(
                """INSERT INTO agent_change_proposal_decision(id,proposal_id,decision_kind,selected_group_ids_json,
                       selected_case_ids_json,result_working_copy_id,result_working_copy_version,result_working_copy_hash,actor_user_id,created_at)
                       VALUES('acpd_old','acp_old','patch_accept','[]','[]','awc_old',2,?,'user_owner','2026-10-01')""",
                ("sha256:" + "c" * 64,),
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    for name, rows in before.items():
        if name == "agent_change_proposal_decision":
            assert [row[:-1] for row in after[name]] == rows
            assert all(row[-1] is None for row in after[name])
        else:
            assert after[name] == rows
    shape = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == shape
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        if target == "core":
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute("UPDATE agent_change_proposal_decision SET result_content_hash=?", ("sha256:" + "d" * 64,))


def test_partial_content_receipt_install_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    monkeypatch.setattr(MIGRATION, "_DDL", (*MIGRATION._DDL[:1], "invalid sql"))
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}), get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute)
            ),
        )
        with pytest.raises(sqlite3.OperationalError):
            MIGRATION.upgrade()
    assert schema(path) == before
