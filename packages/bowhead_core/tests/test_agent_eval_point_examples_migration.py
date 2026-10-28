"""0183 adds the point calibration tables to core only and leaves existing rows alone."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_revision_store import _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0183_agent_eval_point_examples")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_the_point_tables_without_touching_existing_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {name: rows for name, rows in after.items() if name in before} == before
    assert set(after) - set(before) <= {"agent_eval_point_example", "agent_eval_point_calibration"}
    shape = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == shape
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if target != "core":
            assert not {"agent_eval_point_example", "agent_eval_point_calibration"} & tables
            return
        assert {"agent_eval_point_example", "agent_eval_point_calibration"} <= tables
        assert db.execute("SELECT COUNT(*) FROM agent_eval_point_example").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_example_rows_keep_their_kind_and_answer_source_consistent(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")

        def insert(row_id, kind, answer_ref, answer_text, expected_met):
            db.execute(
                """INSERT INTO agent_eval_point_example(id,team_id,agent_id,suite_id,point_id,case_revision_id,kind,
                       answer_ref,answer_text,expected_met,created_by,created_at)
                   VALUES(?,'team_1','osa_1','aes_1','p_0a1b','aec_1',?,?,?,?,'user_1','2026-10-18T00:00:00+00:00')""",
                (row_id, kind, answer_ref, answer_text, expected_met),
            )

        insert("aepx_good", "good", "coach:aepx_good", "Up on last year.", 1)
        insert("aepx_wrongly", "judged_wrongly", "attempt:aer_1:aec_1:1", None, 1)
        for row in (
            ("aepx_good_unmet", "good", "coach:aepx_x", "text", 0),  # a good example is met
            ("aepx_bad_met", "bad", "coach:aepx_y", "text", 1),  # a bad example is not
            ("aepx_coach_no_text", "bad", "coach:aepx_z", None, 0),  # Coach's answer is stored with it
            ("aepx_attempt_text", "good", "attempt:aer_1:aec_1:2", "text", 1),  # an attempt's answer is read from its Turn
            ("bad_prefix", "good", "attempt:aer_1:aec_1:3", None, 1),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                insert(*row)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            insert("aepx_again", "judged_wrongly", "attempt:aer_1:aec_1:1", None, 1)
