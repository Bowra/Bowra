"""Migration 0156 adds nullable all-or-none attempt usage columns to Agent Eval results."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0156_agent_eval_attempt_usage")
#: The frozen `1.0.0` baseline revision that existing deployments adopt before later revisions.
BASELINE = import_module("bowhead_core.migrations.versions.0001_initial")
USAGE_COLUMNS = ("tokens_input", "tokens_output", "tokens_cached", "model_call_count")
pytestmark = pytest.mark.integration

_HASH = "sha256:" + "a" * 64


def _columns(path) -> list[str]:
    with sqlite3.connect(path) as db:
        return [row[1] for row in db.execute("PRAGMA table_info(agent_eval_case_result)")]


_SEED_TABLES = ("agent_eval_suite", "agent_eval_suite_revision", "agent_eval_case_revision", "agent_eval_run", "agent_eval_case_result")


def _seed_result(db: sqlite3.Connection, *, attempt: int = 1, usage: tuple | None = None) -> None:
    """Insert one result row; only insert-scope triggers are lifted, then restored byte-for-byte.

    The seeded owner/suite/run graph is minimal, so the business admission triggers that guard
    real writers are suspended while it is inserted. Column CHECKs and immutability triggers stay.
    """
    placeholders = ",".join("?" * len(_SEED_TABLES))
    lifted = db.execute(
        f"SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ({placeholders}) AND sql LIKE '%BEFORE INSERT%'",
        _SEED_TABLES,
    ).fetchall()
    for name, _sql in lifted:
        db.execute(f"DROP TRIGGER {name}")
    try:
        _insert_result(db, attempt=attempt, usage=usage)
    finally:
        for _name, sql in lifted:
            db.execute(sql)


def _insert_result(db: sqlite3.Connection, *, attempt: int, usage: tuple | None) -> None:
    if db.execute("SELECT 1 FROM teams WHERE id='team_u'").fetchone() is None:
        db.execute("INSERT INTO teams(id,name,created_at) VALUES('team_u','Usage team','2026-01-01')")
        db.execute("INSERT INTO users(id,team_id,email,created_at) VALUES('usr_u','team_u','u@example.com','2026-01-01')")
        db.execute(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES('tm_u','usr_u','team_u','member','active','2026-01-01')"
        )
        db.execute(
            """INSERT INTO team_subagent(id,team_id,name,display_name,description,tool_refs_json,skills_json,visibility,
               created_by,status,created_at,updated_at)
               VALUES('osa_u','team_u','usage-agent','Usage Agent','','[]','[]','private','usr_u','enabled','2026-01-01','2026-01-01')"""
        )
        db.execute(
            """INSERT INTO agent_eval_suite(id,team_id,agent_id,agent_source_kind,suite_kind,name,visibility,state,
               created_by,created_at,updated_at) VALUES('aes_u','team_u','osa_u','team','owner_quality','Usage','owner','active',
               'usr_u','2026-01-01','2026-01-01')"""
        )
        db.execute(
            """INSERT INTO agent_eval_suite_revision(id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,
               judge_policy_json,case_count,case_set_hash,created_by,created_at)
               VALUES('aev_u','aes_u',1,'{}',?,'evalkit.core@1','{}',1,?,'usr_u','2026-01-01')""",
            (_HASH, _HASH),
        )
        db.execute(
            """INSERT INTO agent_eval_case_revision(id,suite_revision_id,case_key,case_hash,input_json,fixture_ref_json,
               expected_contract_json,scorer_config_json,effect_policy_json,created_at)
               VALUES('aec_u','aev_u','quality.1',?,'{}','{}','{}','{}','{}','2026-01-01')""",
            (_HASH,),
        )
        db.execute(
            """INSERT INTO agent_eval_run(id,team_id,agent_id,agent_source_kind,revision_ref,capability_fingerprint,
               dependency_fingerprint,model_config_hash,subject_fingerprint,suite_revision_id,case_set_hash,scorer_bundle_ref,
               trigger_kind,status,idempotency_key,created_by,created_at,started_at)
               VALUES('aer_u','team_u','osa_u','team','arv_u',?,?,?,?,'aev_u',?,'evalkit.core@1','manual','running','k','usr_u',
               '2026-01-01','2026-01-01')""",
            (_HASH, _HASH, _HASH, _HASH, _HASH),
        )
    columns = "run_id,case_revision_id,attempt,status,deterministic_json,created_at"
    values: tuple = ("aer_u", "aec_u", attempt, "passed", "{}", "2026-01-01")
    if usage is not None:
        columns += "," + ",".join(USAGE_COLUMNS)
        values = (*values, *usage)
    db.execute(f"INSERT INTO agent_eval_case_result({columns}) VALUES({','.join('?' * len(values))})", values)


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_rows_and_adds_usage_columns_only_to_core(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            _seed_result(db)
    before = snapshot(path)
    previous = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    installed = schema(path)
    if target == "core":
        # The historical row survives unchanged apart from the appended null usage.
        old_rows = before.pop("agent_eval_case_result")
        new_rows = after.pop("agent_eval_case_result")
        assert len(old_rows) == len(new_rows) == 1
        assert _columns(path)[-4:] == list(USAGE_COLUMNS)
        with sqlite3.connect(path) as db:
            assert db.execute(f"SELECT {','.join(USAGE_COLUMNS)} FROM agent_eval_case_result").fetchone() == (None, None, None, None)
        assert {row[1] for row in set(installed) - set(previous)} == {"agent_eval_case_result"}
    else:
        assert installed == previous
    assert after == before
    # Repeat upgrade is a no-op.
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)


def test_empty_database_and_the_1_0_0_baseline_both_reach_head_with_usage_columns(tmp_path):
    empty = tmp_path / "empty.db"
    _upgrade(empty, "head")
    baseline = tmp_path / "baseline.db"
    _upgrade(baseline, BASELINE.revision)
    with sqlite3.connect(baseline) as db:
        db.execute("INSERT INTO teams(id,name,created_at) VALUES('team_old','Old team','2026-01-01')")
    _upgrade(baseline, "head")
    _upgrade(baseline, "head")
    for path in (empty, baseline):
        assert _columns(path)[-4:] == list(USAGE_COLUMNS)
    with sqlite3.connect(baseline) as db:
        assert db.execute("SELECT name FROM teams WHERE id='team_old'").fetchone() == ("Old team",)
    with sqlite3.connect(empty) as a, sqlite3.connect(baseline) as b:
        query = "SELECT sql FROM sqlite_master WHERE name='agent_eval_case_result'"
        assert a.execute(query).fetchall() == b.execute(query).fetchall()


@pytest.mark.parametrize(
    "usage",
    [
        (10, None, None, None),
        (10, 5, 0, None),
        (None, None, None, 2),
        (10, 5, None, 2),
        (-1, 5, 0, 2),
        (10, 5, 0, -1),
    ],
)
def test_partial_or_negative_usage_is_rejected(tmp_path, usage):
    path = tmp_path / "core.db"
    _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        # Positive control: the same seed path accepts a complete usage row.
        _seed_result(db, attempt=1, usage=(1, 1, 0, 1))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _seed_result(db, attempt=2, usage=usage)


def test_complete_usage_is_accepted_and_the_row_stays_immutable(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        _seed_result(db, attempt=1, usage=(120, 30, 40, 3))
        _seed_result(db, attempt=2, usage=None)
        assert db.execute(f"SELECT attempt,{','.join(USAGE_COLUMNS)} FROM agent_eval_case_result ORDER BY attempt").fetchall() == [
            (1, 120, 30, 40, 3),
            (2, None, None, None, None),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE agent_eval_case_result SET tokens_input=1,tokens_output=1,tokens_cached=0,model_call_count=1 WHERE attempt=2")


def test_failed_installation_leaves_no_partial_columns(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    execute = Connection.exec_driver_sql

    def interrupted(self, statement, *args, **kwargs):
        if "ADD COLUMN model_call_count" in statement:
            raise RuntimeError("injected migration failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", interrupted)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        _upgrade(path, MIGRATION.revision, "core")
    assert schema(path) == before
    assert "tokens_input" not in _columns(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
