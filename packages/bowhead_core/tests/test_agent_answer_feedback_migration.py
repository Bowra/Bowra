"""0185 adds the answer feedback table to core only and leaves existing rows alone."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_revision_store import _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0185_agent_answer_feedback")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_the_feedback_table_without_touching_existing_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {name: rows for name, rows in after.items() if name in before} == before
    assert set(after) - set(before) <= {"agent_answer_feedback"}
    shape = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == shape
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if target != "core":
            assert "agent_answer_feedback" not in tables
            return
        assert "agent_answer_feedback" in tables
        assert db.execute("SELECT COUNT(*) FROM agent_answer_feedback").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_one_thumbs_down_per_turn_with_valid_fields_and_it_goes_with_its_turn(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    # Autocommit, so the foreign-key pragma takes effect between statements.
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES('t_1','s_1',1,'q','completed','2026-10-19')")

        def insert(score=-1, reasons='["inaccurate"]', comment=None, handled_at=None, action=None):
            db.execute(
                "INSERT INTO agent_answer_feedback(turn_id,team_id,agent_id,user_id,score,reason_codes,comment,created_at,updated_at,"
                "owner_handled_at,owner_handled_action) VALUES('t_1','team_1','osa_1','user_1',?,?,?,'2026-10-19','2026-10-19',?,?)",
                (score, reasons, comment, handled_at, action),
            )

        insert()
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            insert()
        db.execute("DELETE FROM agent_answer_feedback")
        for bad in (
            {"score": 1},  # thumbs-down only
            {"reasons": '{"a":1}'},  # reasons are a JSON array
            {"comment": ""},  # no empty comment; absent is NULL
            {"comment": "x" * 501},
            {"handled_at": "2026-10-20"},  # handled needs its action, and the reverse
            {"action": "dismissed"},
            {"handled_at": "2026-10-20", "action": "deleted"},
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                insert(**bad)
        insert(comment="x" * 500, handled_at="2026-10-20", action="coach")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM turns WHERE id='t_1'")
        assert db.execute("SELECT COUNT(*) FROM agent_answer_feedback").fetchone()[0] == 0


def test_rollback_restores_a_snapshot_instead_of_downgrading():
    # Production never runs downgrade (database migration contract); rollback restores the paired archive.
    with pytest.raises(NotImplementedError):
        MIGRATION.downgrade()
