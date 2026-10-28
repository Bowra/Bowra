"""0188 lets a suite return to an earlier definition as a new current revision."""

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_revision_store import _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0188_agent_eval_suite_revision_restore")
_NOW = "2026-10-19T00:00:00+00:00"
_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_TABLE = "agent_eval_suite_revision"


def _insert_revision(db, row_id: str, sequence: int, definition_hash: str) -> None:
    db.execute(
        """INSERT INTO agent_eval_suite_revision(id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,
               judge_policy_json,case_count,case_set_hash,created_by,created_at)
           VALUES(?,'aes_1',?,'{}',?,'evalkit.core@1','{"enabled":false,"rubric_ref":null}',1,?,'user_owner',?)""",
        (row_id, sequence, definition_hash, definition_hash, _NOW),
    )


def _seed_suite(path) -> None:
    """A suite saved as A then B, with a case per revision and a cascading child row."""
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO agent_eval_suite(id,team_id,agent_id,agent_source_kind,suite_kind,name,visibility,state,row_version,
                   created_by,created_at,updated_at) VALUES('aes_1','team_release','osa_release','team','owner_quality','Quality',
                   'owner','active',1,'user_owner',?,?)""",
            (_NOW, _NOW),
        )
        for sequence, (revision_id, definition_hash) in enumerate((("aev_1", _HASH_A), ("aev_2", _HASH_B)), start=1):
            _insert_revision(db, revision_id, sequence, definition_hash)
            db.execute(
                """INSERT INTO agent_eval_case_revision(id,suite_revision_id,case_key,case_hash,input_json,fixture_ref_json,
                       expected_contract_json,scorer_config_json,effect_policy_json,valid_until,created_at)
                   VALUES(?,?,'case.1',?,'{"task":"Sales last month"}','{}','{}','{}','{"mode":"no_effects"}',NULL,?)""",
                (f"aec_{sequence}", revision_id, definition_hash, _NOW),
            )
        # ON DELETE CASCADE: the rebuild must not take this review with the old table.
        db.execute(
            """INSERT INTO agent_eval_suite_review(suite_revision_id,prompt_version,team_id,agent_id,result_json,created_at)
               VALUES('aev_1','suite-review@1','team_release','osa_release','{}',?)""",
            (_NOW,),
        )


def _definition_unique(path) -> bool:
    with sqlite3.connect(path) as db:
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (_TABLE,)).fetchone()[0]
    return "unique(suite_id,definition_hash)" in "".join(sql.lower().split())


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_drops_only_the_definition_unique_and_keeps_every_row(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
        _seed_suite(path)
        assert _definition_unique(path)
    before_rows, before_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before_rows
    after_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after_schema
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision
    if target != "core":
        assert after_schema == before_schema
        return
    assert not _definition_unique(path)
    # Only the table and its autoindexes change; every trigger, including other tables' guards, is kept.
    changed = {item[1] for item in set(before_schema) ^ set(after_schema)}
    assert changed <= {_TABLE} | {f"sqlite_autoindex_{_TABLE}_{n}" for n in range(1, 5)}
    triggers = [item for item in after_schema if item[0] == "trigger"]
    assert triggers == [item for item in before_schema if item[0] == "trigger"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute("PRAGMA foreign_keys=ON")
        # Saving A again appends revision 3, which is the current one.
        _insert_revision(db, "aev_3", 3, _HASH_A)
        current = db.execute("SELECT id FROM agent_eval_suite_revision WHERE suite_id='aes_1' ORDER BY sequence DESC LIMIT 1").fetchone()
        assert current[0] == "aev_3"
        # Sequence and id identity are still unique, and the revision guards still hold.
        sql = "".join(db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (_TABLE,)).fetchone()[0].lower().split())
        assert "unique(suite_id,sequence)" in sql and "unique(suite_id,id)" in sql
        for sequence in (3, 9):
            with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
                _insert_revision(db, f"aev_{sequence + 1}", sequence, _HASH_B)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE agent_eval_suite_revision SET definition_hash=? WHERE id='aev_1'", (_HASH_B,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM agent_eval_suite_revision WHERE id='aev_1'")


def test_an_unknown_revision_table_shape_is_refused(tmp_path):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE agent_eval_suite_revision ADD COLUMN surprise TEXT")
    with pytest.raises(Exception, match="does not match the 0037 schema"):
        _upgrade(path, MIGRATION.revision, "core")
    assert _definition_unique(path)


def test_rollback_restores_a_snapshot_instead_of_downgrading():
    with pytest.raises(NotImplementedError):
        MIGRATION.downgrade()


def _upgrade_on_connection(path, monkeypatch, *, open_transaction: bool = False) -> int:
    """Run 0188 on a connection that has foreign key enforcement on; return the pragma afterwards."""
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            if open_transaction:
                # Any write opens the transaction in which SQLite ignores the pragma.
                connection.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
            fake = SimpleNamespace(config=SimpleNamespace(attributes={"bowhead_target": "core"}), get_bind=lambda: connection)
            monkeypatch.setattr(MIGRATION, "context", fake)
            try:
                MIGRATION.upgrade()
            finally:
                state = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
            connection.commit()
    finally:
        engine.dispose()
    return state


def _children(path):
    with sqlite3.connect(path) as db:
        return (
            db.execute("SELECT id,suite_revision_id FROM agent_eval_case_revision ORDER BY id").fetchall(),
            db.execute("SELECT suite_revision_id,prompt_version FROM agent_eval_suite_review").fetchall(),
        )


def test_upgrade_on_a_connection_with_foreign_keys_on_keeps_children_and_the_pragma(tmp_path, monkeypatch):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    _seed(path)
    _seed_suite(path)
    children = _children(path)
    assert children == ([("aec_1", "aev_1"), ("aec_2", "aev_2")], [("aev_1", "suite-review@1")])

    assert _upgrade_on_connection(path, monkeypatch) == 1

    assert not _definition_unique(path)
    # Enforcement was suspended for the drop: no cascade took the review, no RESTRICT blocked the cases.
    assert _children(path) == children
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM agent_eval_suite_revision ORDER BY sequence").fetchall() == [("aev_1",), ("aev_2",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_foreign_keys_that_cannot_be_suspended_refuse_the_upgrade_and_stay_on(tmp_path, monkeypatch):
    path = tmp_path / TARGET_FILES["core"]
    _upgrade(path, MIGRATION.down_revision, "core")
    _seed(path)
    _seed_suite(path)
    children = _children(path)
    with pytest.raises(RuntimeError, match="could not suspend foreign key enforcement"):
        _upgrade_on_connection(path, monkeypatch, open_transaction=True)
    assert _definition_unique(path)
    assert _children(path) == children
