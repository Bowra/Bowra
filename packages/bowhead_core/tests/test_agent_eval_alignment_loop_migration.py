"""0186 adds the alignment-loop tables and rebuilds point examples so they can hold chat Turn answers."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_revision_store import _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0186_agent_eval_alignment_loop")
NEW_TABLES = {"agent_eval_alignment_loop", "agent_eval_alignment_question", "agent_eval_suite_review"}
_NOW = "2026-10-19T00:00:00+00:00"
_HASH = "sha256:" + "0" * 64


def _seed_examples(path) -> None:
    """One example from each writer that existed before 0186, under a real suite and case."""
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO agent_eval_suite(id,team_id,agent_id,agent_source_kind,suite_kind,name,visibility,state,row_version,
                   created_by,created_at,updated_at) VALUES('aes_1','team_release','osa_release','team','owner_quality','Quality',
                   'owner','active',1,'user_owner',?,?)""",
            (_NOW, _NOW),
        )
        db.execute(
            """INSERT INTO agent_eval_suite_revision(id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,
                   judge_policy_json,case_count,case_set_hash,created_by,created_at)
               VALUES('aev_1','aes_1',1,'{}',?,'evalkit.core@1','{"enabled":false,"rubric_ref":null}',1,?,'user_owner',?)""",
            (_HASH, _HASH, _NOW),
        )
        db.execute(
            """INSERT INTO agent_eval_case_revision(id,suite_revision_id,case_key,case_hash,input_json,fixture_ref_json,
                   expected_contract_json,scorer_config_json,effect_policy_json,valid_until,created_at)
               VALUES('aec_1','aev_1','case.1',?,'{"task":"Sales last month"}','{}','{}','{}','{"mode":"no_effects"}',NULL,?)""",
            (_HASH, _NOW),
        )
        for row_id, kind, ref, text, met in (
            ("aepx_marked", "good", "attempt:aer_1:aec_1:1", None, 1),
            ("aepx_coach", "bad", "coach:aepx_coach", "No period stated.", 0),
            ("aepx_wrongly", "judged_wrongly", "attempt:aer_1:aec_1:2", None, 0),
        ):
            db.execute(
                """INSERT INTO agent_eval_point_example(id,team_id,agent_id,suite_id,point_id,case_revision_id,kind,answer_ref,
                       answer_text,expected_met,graded_text_hash,graded_met,created_by,created_at)
                   VALUES(?,'team_release','osa_release','aes_1','p_0a1b','aec_1',?,?,?,?,?,?,'user_owner',?)""",
                (row_id, kind, ref, text, met, _HASH, met, _NOW),
            )


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_adds_loop_tables_and_keeps_every_example(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
        _seed_examples(path)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    # The rebuilt example table gains three columns at the end; every old value is kept.
    unchanged = {name: rows for name, rows in after.items() if name in before and name != "agent_eval_point_example"}
    assert unchanged == {name: rows for name, rows in before.items() if name != "agent_eval_point_example"}
    assert set(after) - set(before) <= NEW_TABLES
    shape = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == shape
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if target != "core":
            assert not NEW_TABLES & tables
            return
        assert tables >= NEW_TABLES
        assert [row[:17] for row in after["agent_eval_point_example"]] == before["agent_eval_point_example"]
        sources = dict(db.execute("SELECT id,label_source FROM agent_eval_point_example"))
        assert sources == {"aepx_marked": "marked_good", "aepx_coach": "coach_edit", "aepx_wrongly": "judged_wrongly"}
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='index' AND tbl_name='agent_eval_point_example'")}
        assert {
            "idx_agent_eval_point_example_point",
            "uq_agent_eval_point_example_answer",
            "idx_agent_eval_point_example_turn",
            "idx_agent_eval_point_example_source_turn",
        } <= indexes
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_an_unknown_example_table_shape_is_refused(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE agent_eval_point_example ADD COLUMN surprise TEXT")
    with pytest.raises(Exception, match="does not match the 0183 schema"):
        _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        # The savepoint rolled the new tables back with the refused rebuild.
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert not NEW_TABLES & tables


def test_turn_examples_name_their_turn_and_question_and_go_with_it(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES('t_1','s_1',1,'q','completed',?)", (_NOW,))

        def insert(row_id, ref, *, task="Sales last month?", turn="t_1", source="owner_alignment", text=None):
            db.execute(
                """INSERT INTO agent_eval_point_example(id,team_id,agent_id,suite_id,point_id,case_revision_id,kind,answer_ref,
                       answer_text,expected_met,created_by,created_at,task_text,label_source,turn_id)
                   VALUES(?,'team_1','osa_1','aes_1','p_0a1b','aec_1','good',?,?,1,'user_1',?,?,?,?)""",
                (row_id, ref, text, _NOW, task, source, turn),
            )

        insert("aepx_turn", "turn:t_1")
        for bad in (
            {"row_id": "aepx_a", "ref": "turn:t_1", "task": None},  # the Turn's question is required
            {"row_id": "aepx_b", "ref": "turn:t_2"},  # the ref names its own Turn
            {"row_id": "aepx_c", "ref": "turn:t_1", "turn": None},
            {"row_id": "aepx_d", "ref": "attempt:aer_1:aec_1:1", "source": "owner_label"},
            {"row_id": "aepx_e", "ref": "chat:t_1", "turn": None},
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                insert(**bad)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM turns WHERE id='t_1'")
        assert db.execute("SELECT COUNT(*) FROM agent_eval_point_example").fetchone()[0] == 0


def test_question_rows_keep_label_and_answer_source_consistent(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA foreign_keys=OFF")

        def insert(row_index, ref, *, text=None, turn=None, label=None, via=None, at=None, reason="recent"):
            db.execute(
                """INSERT INTO agent_eval_alignment_question(id,loop_id,point_id,point_text_hash,card_seq,row_index,answer_ref,
                       turn_id,answer_text,task_text,answer_excerpt,graded_prompt_version,graded_model_fingerprint,pick_reason,
                       owner_label,label_via,labelled_at,created_at)
                   VALUES(?,'aeal_1','p_0a1b',?,1,?,?,?,?,'q','a','agent-eval-judge@2',?,?,?,?,?,?)""",
                (f"aeaq_{row_index}", _HASH, row_index, ref, turn, text, _HASH, reason, label, via, at, _NOW),
            )

        insert(1, "coach:aeaq_1", text="A counterexample.", reason="counterexample", label="right", via="rest_right", at=_NOW)
        for row in (
            {"row_index": 2, "ref": "coach:aeaq_2"},  # a generated answer is stored with the row
            {"row_index": 3, "ref": "turn:t_9"},  # a Turn row names its Turn
            {"row_index": 4, "ref": "attempt:a:b:1", "label": "right"},  # a label says how it was given and when
            {"row_index": 5, "ref": "attempt:a:b:1", "reason": "guess"},
            {"row_index": 6, "ref": "attempt:a:b:1"},  # at most five rows per card
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                insert(**row)


def test_rollback_restores_a_snapshot_instead_of_downgrading():
    with pytest.raises(NotImplementedError):
        MIGRATION.downgrade()


def test_a_loop_from_member_feedback_stores_no_comment_or_member(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.revision, "core")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")

        def insert(row_id, source, words, by, origin):
            db.execute(
                """INSERT INTO agent_eval_alignment_loop(id,team_id,agent_id,suite_id,owner_id,session_id,words_source,owner_words,words_by,
                       origin_turn_id,judge_prompt_version,judge_model_fingerprint,pinned_at,state,created_at,updated_at)
                   VALUES(?,'team_1','osa_1','aes_1',?,'s_1',?,?,?,?,'agent-eval-judge@2',?,?,'aligning',?,?)""",
                (row_id, row_id, source, words, by, origin, _HASH, _NOW, _NOW, _NOW),
            )

        insert("aeal_owner", "owner", "No period.", "user_1", None)
        insert("aeal_member", "member_feedback", None, None, "t_1")
        for bad in (
            ("aeal_a", "member_feedback", "Copied comment", None, "t_1"),
            ("aeal_b", "member_feedback", None, "user_2", "t_1"),
            ("aeal_c", "member_feedback", None, None, None),
            ("aeal_d", "owner", None, "user_1", None),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                insert(*bad)
