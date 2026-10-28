"""0117 lets a published draft be deleted while immutable history rows still name it."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_agent_eval_policy_plan_store import _Clock, _comparison_context, _ready_candidate, _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0117_working_copy_history_refs")
pytestmark = pytest.mark.integration


def _working_copy_refs(db: sqlite3.Connection, table: str) -> list[tuple]:
    return [row for row in db.execute(f"PRAGMA foreign_key_list({table})").fetchall() if row[2] == "team_subagent_working_copy"]


def _dependents(db: sqlite3.Connection, table: str) -> list[tuple]:
    return db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') ORDER BY type,name",
        (table,),
    ).fetchall()


def _seed_history(path: Path) -> None:
    """A control row (via the real plan store) and a taxonomy suggestion, both naming the draft."""
    _seed(path)
    import asyncio

    asyncio.run(_comparison_context(path))
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """
            INSERT INTO agent_taxonomy_suggestion(
              id,team_id,agent_id,working_copy_id,source_hash,taxonomy_version,suggestion_json,
              confidence,state,created_by,created_at,updated_at
            ) VALUES('ats_history','team_p','osa_policy','awc_policy',?,'v1','{}',0.5,'pending','user_owner',
                     '2026-09-14T10:00:00+00:00','2026-09-14T10:00:00+00:00')
            """,
            ("sha256:" + "a" * 64,),
        )
        db.commit()


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_drops_only_the_two_draft_references_and_is_idempotent(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before_rows = snapshot(path)
    before_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before_rows
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        if target == "core":
            for table in (MIGRATION.CONTROL, MIGRATION.SUGGESTION):
                assert _working_copy_refs(db, table) == []
                # Every other parent reference survives.
                assert db.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            changed = {name for kind, name, _sql in schema(path)} ^ {name for kind, name, _sql in before_schema}
            assert changed == set()
    if target != "core":
        assert schema(path) == before_schema
    after_schema = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == after_schema and snapshot(path) == before_rows


def test_history_rows_indexes_and_guards_survive_the_rebuild(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    _seed_history(path)
    before = snapshot(path)
    with sqlite3.connect(path) as db:
        guards = {table: _dependents(db, table) for table in (MIGRATION.CONTROL, MIGRATION.SUGGESTION)}
        assert db.execute(f"SELECT COUNT(*) FROM {MIGRATION.CONTROL}").fetchone() == (1,)

    _upgrade(path, MIGRATION.revision)

    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for table, expected in guards.items():
            assert [(kind, name, MIGRATION._canonical(sql or "")) for kind, name, sql in _dependents(db, table)] == [
                (kind, name, MIGRATION._canonical(sql or "")) for kind, name, sql in expected
            ]
        # The immutable-history guards still hold after the rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"DELETE FROM {MIGRATION.CONTROL}")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"DELETE FROM {MIGRATION.SUGGESTION}")
        # The draft itself can now be deleted while both history rows still name it.
        db.execute("DELETE FROM team_subagent_working_copy WHERE id='awc_policy'")
        assert db.execute(f"SELECT source_working_copy_id FROM {MIGRATION.CONTROL}").fetchone() == ("awc_policy",)
        assert db.execute(f"SELECT working_copy_id FROM {MIGRATION.SUGGESTION}").fetchone() == ("awc_policy",)


@pytest.mark.parametrize("failure", ["copy", "drift"])
def test_failed_rebuild_leaves_the_old_schema_and_rows(tmp_path, monkeypatch, failure):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    _seed_history(path)
    if failure == "drift":
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER trg_agent_eval_candidate_control_immutable_delete")
            db.commit()
    before, before_schema = snapshot(path), schema(path)
    original = Connection.exec_driver_sql

    def fail(self, statement, *args, **kwargs):
        result = original(self, statement, *args, **kwargs)
        if failure == "copy" and statement.startswith(f"INSERT INTO {MIGRATION.SUGGESTION}_0117"):
            original(self, f"DELETE FROM {MIGRATION.SUGGESTION}_0117")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError):
        _upgrade(path, MIGRATION.revision)
    # The control table was already rebuilt when the suggestion copy failed; the SAVEPOINT must undo it.
    assert schema(path) == before_schema
    assert snapshot(path) == before


@pytest.mark.asyncio
async def test_private_activation_removes_the_published_draft(tmp_path):
    """The user-visible failure this revision fixes: activating an unedited candidate returned 500.

    The draft here equals what goes live — the normal case — so activation deletes it. Before 0117
    that DELETE hit the control row's RESTRICT reference and the whole activation rolled back.
    """
    path = tmp_path / "core.db"
    _upgrade(path)
    _seed(path)
    service, plan, snapshot_row = await _ready_candidate(path, clock=_Clock(), audience_mode="private")
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE team_subagent_working_copy SET audience_json=? WHERE id='awc_policy'",
            ('{"effective_mode":"private","group_ids":[],"schema_version":1,"user_ids":[]}',),
        )
        db.execute(
            """
            INSERT INTO agent_taxonomy_suggestion(
              id,team_id,agent_id,working_copy_id,source_hash,taxonomy_version,suggestion_json,
              confidence,state,created_by,created_at,updated_at
            ) VALUES('ats_live','team_p','osa_policy','awc_policy',?,'v1','{}',0.5,'pending','user_owner',
                     '2026-09-14T10:00:00+00:00','2026-09-14T10:00:00+00:00')
            """,
            ("sha256:" + "b" * 64,),
        )
        db.commit()

    result = await service.activate_private(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot_row.id,
        snapshot_hash=snapshot_row.snapshot_hash,
        override_id=None,
        override_hash=None,
        idempotency_key="activate-published-draft",
    )

    assert result.state == "activated"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_working_copy WHERE id='awc_policy'").fetchone() == (0,)
        assert db.execute("SELECT state FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == ("activated",)
        assert db.execute(f"SELECT COUNT(*) FROM {MIGRATION.CONTROL} WHERE source_working_copy_id='awc_policy'").fetchone() == (1,)
