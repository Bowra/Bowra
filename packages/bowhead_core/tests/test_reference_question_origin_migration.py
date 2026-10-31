"""Original question-origin SQL proofs and atomic forward-only replacement.

The small relational proof fixture below contains synthetic authority facts,
not model invocations or claims of successful provider execution. The historical
attempt fixture is produced by the original runtime with a scripted provider.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_lead_delegation import _normal_lead
from test_reference_lead_delegation import lead_case as lead_case
from test_reference_lead_delegation import start_case as start_case
from test_reference_model_context_migration import snapshot
from test_reference_primary_runtime import _rows, _runtime
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0209_reference_question_origin")
pytestmark = pytest.mark.integration


def version(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT version_num FROM alembic_version").fetchone()[0]


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_origin_upgrade_preserves_data_and_other_schema(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after, new_schema = snapshot(path), schema(path)
    assert before == after
    assert version(path) == MIGRATION.revision
    changed = {MIGRATION.TABLE, MIGRATION.ATTEMPT_NAME, MIGRATION.QUESTION_NAME, *MIGRATION.CURRENT}
    assert [row for row in old_schema if row[1] not in changed] == [row for row in new_schema if row[1] not in changed]
    if target == "core":
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            for name, expected in {
                **MIGRATION.DDL,
                MIGRATION.ATTEMPT_NAME: MIGRATION.ATTEMPT_GUARD,
                MIGRATION.QUESTION_NAME: MIGRATION.QUESTION_DDL,
            }.items():
                installed = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()[0]
                assert MIGRATION._normalized(installed) == MIGRATION._normalized(expected)
    else:
        assert old_schema == new_schema
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after and schema(path) == new_schema


@pytest.mark.asyncio
async def test_original_runtime_attempt_survives_copy_without_rewriting_receipts(lead_case, monkeypatch, tmp_path):
    from bowhead_core.clarification_store import ClarificationStore

    case = lead_case
    case.runtime = await _runtime(case)
    await _normal_lead(case, monkeypatch, primary_needs_input=True, provide_card_store=True)
    card = _rows(case, "clarification_requests")[0]
    selected = json.loads(card["candidates_json"])[0]["value"]
    await ClarificationStore(case.fixture.db_path).resolve(card["request_id"], selected)
    assert await case.store.mark_chat_spawn_dispatch_terminal(turn_id="t_reference", owner_id=case.runtime.owner_id)
    assert await case.store.advance_turn_execution_generation(turn_id="t_reference", reference_clarification_id=card["request_id"]) == 2
    case.runtime = replace(case.runtime, generation=2, owner_id="original-resume")
    await case.runtime.claim_bi_resume(clarification_id=card["request_id"])
    await _normal_lead(case, monkeypatch, postdispatch_resume=True)
    historical = tmp_path / "historical.db"
    _historical_fixture(case.fixture.db_path, historical, MIGRATION.down_revision)
    before = snapshot(historical)
    assert len(before[MIGRATION.TABLE]) == 1
    _upgrade(historical, MIGRATION.revision)
    assert snapshot(historical) == before


@pytest.mark.parametrize("drift", [MIGRATION.TABLE, MIGRATION.ATTEMPT_NAME, MIGRATION.QUESTION_NAME, *MIGRATION.CURRENT, "dependent", "shadow"])
def test_origin_upgrade_rejects_schema_or_dependency_drift(tmp_path, drift):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if drift == MIGRATION.TABLE:
            db.execute(f"ALTER TABLE {MIGRATION.TABLE} ADD COLUMN unexpected TEXT")
        elif drift == "dependent":
            db.execute(f"CREATE TRIGGER unexpected_attempt_reader AFTER INSERT ON turns BEGIN SELECT COUNT(*) FROM {MIGRATION.TABLE}; END")
        elif drift == "shadow":
            db.execute(f"CREATE TABLE {MIGRATION.SHADOW}(unexpected TEXT)")
        else:
            db.execute(f"DROP TRIGGER {drift}")
    before, data = schema(path), snapshot(path)
    with pytest.raises(RuntimeError, match="Reference question origin .*drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before and snapshot(path) == data
    assert version(path) == MIGRATION.down_revision


@pytest.mark.parametrize("stage", ["copy", "drop", "rename", "model", "question", *MIGRATION.PROMPT_CURRENT])
def test_origin_copy_or_guard_interruption_restores_schema_and_ledger(tmp_path, monkeypatch, stage):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before, data = schema(path), snapshot(path)
    original = Connection.exec_driver_sql

    def interrupt(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if (
            stage == "copy"
            and sql.startswith(f"INSERT INTO {MIGRATION.SHADOW}")
            or stage == "drop"
            and sql == f"DROP TABLE {MIGRATION.TABLE}"
            or stage == "rename"
            and sql == f"ALTER TABLE {MIGRATION.SHADOW} RENAME TO {MIGRATION.TABLE}"
            or stage == "model"
            and sql == MIGRATION.DDL["trg_reference_model_context_scope"]
            or stage == "question"
            and sql == MIGRATION.QUESTION_DDL
            or stage in MIGRATION.PROMPT_CURRENT
            and sql == MIGRATION.DDL[stage]
        ):
            raise RuntimeError("Injected origin migration interruption")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", interrupt)
    with pytest.raises(RuntimeError, match="Injected origin migration interruption"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before and snapshot(path) == data
    assert version(path) == MIGRATION.down_revision


def _proof_database(*, origin=2, resume=3, prefix=1):
    """Exercise the exact SQL with synthetic relational facts and no receipts."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE turns(id TEXT PRIMARY KEY,session_id TEXT,status TEXT,execution_generation INTEGER);
        CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id TEXT,workspace_id TEXT,active_turn_id TEXT,deleted_at TEXT);
        CREATE TABLE workspaces(id TEXT PRIMARY KEY,team_id TEXT);
        CREATE TABLE chat_turn_user_sources(turn_id TEXT PRIMARY KEY,user_text TEXT);
        CREATE TABLE turn_agent_execution(id TEXT PRIMARY KEY,status TEXT);
        CREATE TABLE chat_reference_plans(turn_id TEXT PRIMARY KEY,plan_json TEXT,source_hash TEXT);
        CREATE TABLE chat_reference_resolutions(turn_id TEXT,resolution_seq INTEGER,execution_id TEXT,
            composition_digest TEXT,created_generation INTEGER,composition_json TEXT);
        CREATE TABLE chat_reference_evidence(turn_id TEXT,execution_id TEXT,resolution_seq INTEGER,kind TEXT,
            created_generation INTEGER,subagent_id TEXT,content_hash TEXT);
        CREATE TABLE chat_turn_spawn_dispatch(turn_id TEXT PRIMARY KEY,session_id TEXT,state TEXT,lease_kind TEXT,
            lease_execution_generation INTEGER,owner_id TEXT,cancel_requested_at TEXT,lease_expires_at TEXT);
        CREATE TABLE turn_events(turn_id TEXT,event_type TEXT,payload_json TEXT);
        CREATE TABLE clarification_requests(request_id TEXT PRIMARY KEY,turn_id TEXT,user_id TEXT,workspace_id TEXT,
            session_id TEXT,status TEXT,kind TEXT,token TEXT,tool TEXT,resume_context_json TEXT,created_at TEXT,
            rationale TEXT,candidates_json TEXT,multi_select INTEGER,chosen_value TEXT,chosen_values_json TEXT,
            answers_json TEXT,chosen_at TEXT,user_directive TEXT,edited_subs_json TEXT,injected_at TEXT,
            continuation_generation INTEGER,continuation_owner_id TEXT,dismissed_at TEXT,directive_cancelled_at TEXT);
        INSERT INTO turns VALUES('turn','session','running',3);
        INSERT INTO sessions VALUES('session','user','space','turn',NULL);
        INSERT INTO workspaces VALUES('space','team');
        INSERT INTO chat_turn_user_sources VALUES('turn','Synthetic original task');
        INSERT INTO turn_agent_execution VALUES('execution','running');
        INSERT INTO chat_reference_plans VALUES('turn','{"orchestration_contract":"lead-delegation.v1"}','source');
        INSERT INTO chat_reference_resolutions VALUES('turn',1,'execution','original',1,'{}');
        INSERT INTO chat_reference_resolutions VALUES('turn',2,'execution','delegation',2,'{"runtime_policy":"lead-delegation.v1","task_hash":"task-hash"}');
        INSERT INTO chat_reference_evidence VALUES('turn','execution',2,'delegate_started',2,'subagent','task-hash');
        INSERT INTO chat_turn_spawn_dispatch VALUES('turn','session','spawned','resume',3,'owner',NULL,'2999-01-01');
    """)
    db.execute("UPDATE turns SET execution_generation=?", (resume,))
    db.execute("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=?", (resume,))
    db.execute("UPDATE chat_reference_resolutions SET created_generation=? WHERE resolution_seq=2", (origin,))
    db.execute("UPDATE chat_reference_evidence SET created_generation=?", (origin,))
    contexts = []
    for index in range(1, prefix + 2):
        post = index == prefix + 1
        generation = origin if post else 1 if index == 1 else origin - 1
        marker = {
            "contract": "reference-lead-postdispatch.v1" if post else "reference-lead-predispatch.v1",
            "execution_id": "execution",
            "source_hash": "source",
            "composition_digest": "original",
        }
        if post:
            marker.update(delegation_digest="delegation", read_checkpoint_hash="sealed-hash")
        payload = {
            "session_id": "session",
            "user_id": "user",
            "workspace_id": "space",
            "team_id": "team",
            "user_text": "Synthetic original task",
            "reference_continuation": marker,
            "question_round": {
                "contract": "effect-free-questions.v1",
                "round": index,
                "generation": generation,
                "prior_request_id": f"question-{index - 1}" if index > 1 else None,
                "question_hashes": [f"hash-{index}"],
            },
        }
        if post:
            payload["reference_read_question"] = "sealed-synthetic-checkpoint"
        contexts.append(payload)
        db.execute(
            """INSERT INTO clarification_requests(request_id,turn_id,user_id,workspace_id,session_id,status,kind,
            token,tool,resume_context_json,created_at,rationale,candidates_json,multi_select,chosen_value,chosen_at)
            VALUES (?,'turn','user','space','session','resolved','ambiguous_turn','',NULL,?,?,'Question','[]',0,'Answer','2026-01-01')""",
            (f"question-{index}", json.dumps(payload, ensure_ascii=False), f"2026-01-0{index}"),
        )
        if not post:
            consumed = origin if index == prefix else origin - 1
            db.execute(
                "INSERT INTO turn_events VALUES('turn','turn.resumed',?)",
                (json.dumps({"execution_generation": consumed, "request_id": f"question-{index}"}),),
            )
    db.execute(MIGRATION.ATTEMPT_TABLE.replace(MIGRATION.SHADOW, MIGRATION.TABLE, 1))
    db.execute(MIGRATION.ATTEMPT_GUARD)
    db.commit()
    return db, f"question-{prefix + 1}"


def _insert_attempt(db, question, generation=3, owner="owner"):
    db.execute(f"INSERT INTO {MIGRATION.TABLE} VALUES('turn',?,'execution',?,?,'2026-01-04')", (question, generation, owner))


def _late_fence(db, generation):
    return db.execute(
        "SELECT " + MIGRATION._late_postdispatch_fence(str(generation), "'subagent'") + " FROM chat_reference_resolutions r WHERE r.resolution_seq=1"
    ).fetchone()[0]


@pytest.mark.parametrize(("origin", "resume", "prefix"), [(2, 3, 1), (4, 6, 1), (5, 7, 2)])
def test_late_origin_accepts_only_the_original_consumed_prefix(origin, resume, prefix):
    db, question = _proof_database(origin=origin, resume=resume, prefix=prefix)
    with db:
        _insert_attempt(db, question, resume)
        assert _late_fence(db, resume) == 1
        assert _late_fence(db, resume + 1) == 0
    db.close()


@pytest.mark.parametrize(
    ("sql", "parameters"),
    [
        ("DELETE FROM turn_events", ()),
        ("UPDATE turn_events SET payload_json=json_set(payload_json,'$.request_id','unrelated')", ()),
        ("UPDATE turn_events SET payload_json=json_set(payload_json,'$.execution_generation',1)", ()),
        ("UPDATE clarification_requests SET status='pending' WHERE request_id='question-1'", ()),
        ("UPDATE clarification_requests SET user_id='other' WHERE request_id='question-1'", ()),
        (
            "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.reference_continuation.source_hash','other') WHERE request_id='question-1'",
            (),
        ),
        (
            "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.reference_continuation.composition_digest','other') WHERE request_id='question-1'",
            (),
        ),
        (
            "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.question_round.round',3) WHERE request_id='question-2'",
            (),
        ),
        (
            "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.question_round.prior_request_id','missing') WHERE request_id='question-2'",
            (),
        ),
        (
            "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.reference_continuation.source_hash','other') WHERE request_id='question-2'",
            (),
        ),
        ("UPDATE chat_reference_evidence SET created_generation=1", ()),
        ("UPDATE chat_reference_evidence SET content_hash='other'", ()),
        ("UPDATE chat_reference_resolutions SET execution_id='other' WHERE resolution_seq=1", ()),
        ("UPDATE chat_turn_user_sources SET user_text='Changed original task'", ()),
        ("UPDATE sessions SET active_turn_id='other'", ()),
        ("UPDATE sessions SET deleted_at='2026-01-01'", ()),
        ("UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=4", ()),
        ("UPDATE chat_turn_spawn_dispatch SET owner_id='other'", ()),
        ("UPDATE chat_turn_spawn_dispatch SET cancel_requested_at='2026-01-01'", ()),
        ("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01'", ()),
    ],
    ids=[
        "no-notice",
        "wrong-notice-request",
        "wrong-notice-generation",
        "unanswered-prefix",
        "wrong-prefix-user",
        "wrong-prefix-source",
        "wrong-prefix-composition",
        "missing-prefix-round",
        "wrong-predecessor",
        "wrong-post-source",
        "wrong-start-generation",
        "wrong-start-task",
        "wrong-original-execution",
        "changed-source",
        "inactive-session",
        "deleted-session",
        "wrong-lease-generation",
        "wrong-owner",
        "cancelled",
        "expired",
    ],
)
def test_wrong_origin_proof_rejects_attempt_and_later_model_scope(sql, parameters):
    db, question = _proof_database()
    _insert_attempt(db, question)
    assert _late_fence(db, 3) == 1
    db.execute(sql, parameters)
    assert _late_fence(db, 3) == 0
    db.execute(f"DELETE FROM {MIGRATION.TABLE}")
    with pytest.raises(sqlite3.IntegrityError, match="postdispatch attempt mismatch"):
        _insert_attempt(db, question)
    db.close()


@pytest.mark.parametrize(("generation", "owner"), [(2, "owner"), (4, "owner"), (3, "other")])
def test_attempt_generation_and_owner_must_equal_the_current_worker(generation, owner):
    db, question = _proof_database()
    with pytest.raises(sqlite3.IntegrityError, match="postdispatch attempt mismatch"):
        _insert_attempt(db, question, generation, owner)
    db.close()


def test_exact_checkpoint_retirement_requires_attempt_and_preserves_late_scope():
    db, question = _proof_database()
    db.execute(MIGRATION.QUESTION_DDL)
    sql = "UPDATE clarification_requests SET resume_context_json=json_remove(resume_context_json,'$.reference_read_question','$.reference_continuation.read_checkpoint_hash') WHERE request_id=?"
    before = db.execute("SELECT resume_context_json FROM clarification_requests WHERE request_id=?", (question,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="clarification round identity is immutable"):
        db.execute(sql, (question,))
    _insert_attempt(db, question)
    db.execute(sql, (question,))
    after = db.execute("SELECT resume_context_json FROM clarification_requests WHERE request_id=?", (question,)).fetchone()[0]
    expected = json.loads(before)
    expected.pop("reference_read_question")
    expected["reference_continuation"].pop("read_checkpoint_hash")
    assert json.loads(after) == expected and _late_fence(db, 3) == 1
    db.close()


@pytest.mark.parametrize("change", ["round", "one-key", "answer", "expired", "owner", "generation", "scope", "predispatch", "missing-hash"])
def test_checkpoint_retirement_cannot_change_other_identity_or_bypass_lease(change):
    db, question = _proof_database()
    _insert_attempt(db, question)
    if change == "missing-hash":
        db.execute(
            "UPDATE clarification_requests SET resume_context_json=json_remove(resume_context_json,'$.reference_continuation.read_checkpoint_hash') WHERE request_id=?",
            (question,),
        )
    db.execute(MIGRATION.QUESTION_DDL)
    expression = "json_remove(resume_context_json,'$.reference_read_question','$.reference_continuation.read_checkpoint_hash')"
    additional = ""
    if change == "round":
        expression = f"json_set({expression},'$.question_round.round',3)"
    elif change == "one-key":
        expression = "json_remove(resume_context_json,'$.reference_read_question')"
    elif change == "answer":
        additional = ",chosen_value='Changed answer'"
    elif change == "expired":
        db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01'")
    elif change == "owner":
        db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='other'")
    elif change == "generation":
        db.execute("UPDATE turns SET execution_generation=4")
    elif change == "scope":
        additional = ",user_id='other'"
    elif change == "predispatch":
        question = "question-1"
    with pytest.raises(sqlite3.IntegrityError, match="clarification round identity is immutable"):
        db.execute(f"UPDATE clarification_requests SET resume_context_json={expression}{additional} WHERE request_id=?", (question,))
    db.close()


def test_old_postdispatch_fences_and_native_completion_proof_remain_exact():
    for name, (generation, subagent) in MIGRATION.GENERATIONS.items():
        assert MIGRATION.PAUSE._postdispatch_fence(generation, subagent) in MIGRATION.DDL[name]
    assert MIGRATION.COMPLETION.NATIVE_RESUME in MIGRATION.DDL["trg_reference_primary_completion"]
    assert all(MIGRATION.PROMPT.GENERATION in MIGRATION.DDL[name] for name in MIGRATION.PROMPT_CURRENT)


@pytest.mark.parametrize("change", ["scope", "notice", "order", "round"])
def test_two_lead_cards_require_the_original_first_card_as_well(change):
    db, question = _proof_database(origin=5, resume=6, prefix=2)
    _insert_attempt(db, question, 6)
    assert _late_fence(db, 6) == 1
    if change == "scope":
        db.execute("UPDATE clarification_requests SET workspace_id='other' WHERE request_id='question-1'")
    elif change == "notice":
        db.execute("DELETE FROM turn_events WHERE json_extract(payload_json,'$.request_id')='question-1'")
    elif change == "order":
        db.execute("UPDATE clarification_requests SET created_at='2026-01-04' WHERE request_id='question-1'")
    else:
        db.execute(
            "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.question_round.generation',1.0) WHERE request_id='question-1'"
        )
    assert _late_fence(db, 6) == 0
    db.execute(f"DELETE FROM {MIGRATION.TABLE}")
    with pytest.raises(sqlite3.IntegrityError, match="postdispatch attempt mismatch"):
        _insert_attempt(db, question, 6)
    db.close()


def test_metadata_recognition_preserves_expired_or_inactive_recovery_without_authorizing_it():
    db, question = _proof_database()
    _insert_attempt(db, question)
    proof = MIGRATION.late_origin_metadata_sql(plan="p", original="original", actual="actual", card="q", dispatch="d")
    query = f"""SELECT {proof} FROM chat_reference_plans p
        JOIN chat_reference_resolutions original ON original.turn_id=p.turn_id AND original.resolution_seq=1
        JOIN chat_reference_resolutions actual ON actual.turn_id=p.turn_id AND actual.resolution_seq=2
        JOIN clarification_requests q ON q.turn_id=p.turn_id AND q.request_id=?
        JOIN chat_turn_spawn_dispatch d ON d.turn_id=p.turn_id"""
    assert db.execute(query, (question,)).fetchone()[0] == 1
    db.execute("UPDATE sessions SET active_turn_id='other',deleted_at='2026-01-01'")
    db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01'")
    db.execute("UPDATE clarification_requests SET status='pending' WHERE request_id=?", (question,))
    assert db.execute(query, (question,)).fetchone()[0] == 1
    assert _late_fence(db, 3) == 0
    db.execute("DELETE FROM turn_events")
    assert db.execute(query, (question,)).fetchone()[0] == 0
    db.close()


def test_original_fixed_generation_fence_rejects_the_new_supported_origin():
    db, question = _proof_database()
    _insert_attempt(db, question)
    assert _late_fence(db, 3) == 1
    previous = MIGRATION.PAUSE._postdispatch_fence("3", "'subagent'")
    assert db.execute("SELECT " + previous + " FROM chat_reference_resolutions r WHERE resolution_seq=1").fetchone()[0] == 0
    db.close()


def _first_prompt_database(*, generation=3, prefix=2):
    """Create only synthetic predispatch authority, never model receipts."""
    db, post = _proof_database(origin=generation, resume=generation, prefix=prefix)
    db.execute("DELETE FROM clarification_requests WHERE request_id=?", (post,))
    db.execute("DELETE FROM chat_reference_resolutions WHERE resolution_seq=2")
    db.execute("DELETE FROM chat_reference_evidence")
    db.executescript("""
        ALTER TABLE chat_reference_plans ADD COLUMN retention_state TEXT DEFAULT 'available';
        ALTER TABLE turn_agent_execution ADD COLUMN started_at TEXT;
        UPDATE turn_agent_execution SET status='admitted';
        CREATE TABLE chat_reference_plan_retention(turn_id TEXT,available INTEGER,deadline REAL);
        INSERT INTO chat_reference_plan_retention VALUES('turn',1,julianday('now')+1);
        CREATE TABLE chat_reference_model_invocations(turn_id TEXT);
    """)
    if prefix == 3:
        db.execute(
            "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.question_round.generation',2) "
            "WHERE request_id='question-2'"
        )
        db.execute(
            "UPDATE turn_events SET payload_json=json_set(payload_json,'$.execution_generation',2) "
            "WHERE json_extract(payload_json,'$.request_id')='question-1'"
        )
    return db


def _first_prompt_fence(db, generation=3):
    return db.execute(
        "SELECT " + MIGRATION._prompt_first_generation() + " FROM chat_reference_resolutions c "
        "JOIN (SELECT ? AS created_generation) NEW WHERE c.resolution_seq=1",
        (generation,),
    ).fetchone()[0]


@pytest.mark.parametrize(("generation", "prefix"), [(3, 2), (5, 2), (4, 3)])
def test_first_prompt_expansion_accepts_original_consumed_lead_history(generation, prefix):
    db = _first_prompt_database(generation=generation, prefix=prefix)
    assert _first_prompt_fence(db, generation) == 1
    db.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM turn_events",
        "UPDATE turn_events SET payload_json=json_set(payload_json,'$.request_id','unrelated')",
        "UPDATE clarification_requests SET status='pending' WHERE request_id='question-1'",
        "UPDATE clarification_requests SET workspace_id='other' WHERE request_id='question-1'",
        "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.reference_continuation.source_hash','other') WHERE request_id='question-1'",
        "UPDATE clarification_requests SET resume_context_json=json_set(resume_context_json,'$.question_round.round',1) WHERE request_id='question-2'",
        "UPDATE turn_agent_execution SET status='running'",
        "UPDATE turn_agent_execution SET started_at='2026-01-01'",
        "INSERT INTO chat_reference_resolutions VALUES('turn',2,'execution','delegation',3,'{}')",
        "INSERT INTO chat_reference_model_invocations VALUES('turn')",
        "UPDATE chat_turn_spawn_dispatch SET lease_execution_generation=2",
        "UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01'",
        "UPDATE chat_reference_plan_retention SET available=0",
        "UPDATE sessions SET active_turn_id='other'",
    ],
    ids=[
        "no-notices",
        "wrong-request",
        "unanswered",
        "foreign-scope",
        "wrong-source",
        "duplicate-round",
        "primary-running",
        "primary-started",
        "already-delegated",
        "prior-model",
        "stale-lease",
        "expired-lease",
        "retention-lost",
        "inactive-session",
    ],
)
def test_first_prompt_sql_rejects_missing_or_changed_authority(sql):
    db = _first_prompt_database()
    assert _first_prompt_fence(db) == 1
    db.execute(sql)
    assert _first_prompt_fence(db) == 0
    db.close()
