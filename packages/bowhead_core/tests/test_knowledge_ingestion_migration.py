"""Upgrade intent and membership authority without inventing historical consent."""

import json
import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot

MIGRATION = import_module("bowhead_core.migrations.versions.0202_knowledge_ingestion")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_advance_without_rewriting_existing_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    # The authority adds empty metadata; historical rows never become explicit actions.
    assert set(after) - set(before) <= {"kb_membership_operations"}
    for table in before:
        assert before[table] == after[table]
    _upgrade(path, MIGRATION.revision, target)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_new_upload_is_file_only_and_cannot_rewrite_its_intent(tmp_path):
    path = tmp_path / "bowhead.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        # Historical data remains untouched, including absent consent metadata.
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,kind) VALUES ('art_old','old.md','text/markdown',1,'upload','old','now','user_upload')"
        )
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT knowledge_intent_json FROM artifacts WHERE id='art_old'").fetchone() == (None,)
        db.execute(
            "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,kind) VALUES ('art_new','new.md','text/markdown',1,'upload','new','now','user_upload')"
        )
        raw = db.execute("SELECT knowledge_intent_json FROM artifacts WHERE id='art_new'").fetchone()[0]
        assert json.loads(raw) == {"schema_version": 1, "mode": "file_only", "origin": "ordinary_upload"}
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE artifacts SET knowledge_intent_json=NULL WHERE id='art_new'")
        for value in [
            "{}",
            '{"schema_version":true,"mode":"file_only","origin":"ordinary_upload"}',
            '{"schema_version":2,"mode":"file_only","origin":"ordinary_upload"}',
            '{"schema_version":1,"mode":"file_only","origin":"ordinary_upload","confirmed":true}',
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE artifacts SET knowledge_intent_json=? WHERE id='art_old'", (value,))
