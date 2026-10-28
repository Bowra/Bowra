"""0080 retains historical revisions without inventing extension permission."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_revision_store import AgentReleaseStaleError, AgentRevisionStore
from bowhead_core.agent_runtime_extension_store import read_published_extension_policy_in_connection
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError
from test_agent_revision_store import _candidate_hash_of, _Clock, _definition, _Validator
from test_agent_runtime_extension_publication import native_binding, save, seed_release, setup
from test_agent_runtime_extensions import policy
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0080_agent_runtime_extensions")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_every_old_table_and_is_repeatable(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, old_schema = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    after, new_schema = snapshot(path), schema(path)
    assert all(after[name] == rows for name, rows in before.items())
    assert all(row in new_schema for row in old_schema)
    if target == "core":
        assert set(after) - set(before) == {MIGRATION.TABLE}
        assert after[MIGRATION.TABLE] == []
        assert set(MIGRATION.OBJECT_NAMES).issubset({row[1] for row in new_schema})
    else:
        assert after == before and new_schema == old_schema
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == after and schema(path) == new_schema
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)


def test_name_collision_cannot_partially_install_migration(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE trg_agent_extensions_execution_insert(x TEXT)")
    before = schema(path)
    with pytest.raises(RuntimeError, match="schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


def test_post_ddl_failure_rolls_back_table_and_all_triggers(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.startswith("CREATE TRIGGER trg_agent_extensions_execution_insert"):
            raise RuntimeError("post-DDL failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="post-DDL failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
async def test_historical_policy_is_not_backfilled_or_approved_by_old_writer(tmp_path):
    current = setup(tmp_path)
    value = {**_definition(), **policy()}
    working = await save(current, value, audience="teamwide")
    pending = await seed_release(current, working)
    assert pending.state == "pending"
    path = tmp_path / "previous.db"
    _historical_fixture(current.path, path, MIGRATION.down_revision)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    after = snapshot(path)
    assert all(after[name] == rows for name, rows in before.items())
    assert after[MIGRATION.TABLE] == []
    async with open_shared_ctx(path) as db:
        await db.execute("BEGIN")
        with pytest.raises(AgentRuntimeExtensionError, match="unproven"):
            await read_published_extension_policy_in_connection(
                db, team_id="team_release", agent_id="osa_release", revision_ref=pending.revision_id, definition=value
            )
    # An old worker cannot bypass the new proof table using ordinary release or
    # candidate INSERTs. Use previously valid rows from the real prior schema.
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        candidate = dict(db.execute("SELECT * FROM team_subagent_release_candidate WHERE id=?", (pending.candidate_id,)).fetchone())
        candidate["id"] = "arc_legacy_policy"
        db.execute("SAVEPOINT candidate_probe")
        try:
            # Free the ordinary one-active-candidate slot; its uniqueness check
            # must not accidentally stand in for the extension proof fence.
            db.execute("UPDATE team_subagent_release_candidate SET state='withdrawn',row_version=row_version+1 WHERE id=?", (pending.candidate_id,))
            with pytest.raises(sqlite3.IntegrityError, match="publication proof required"):
                db.execute(
                    "INSERT INTO team_subagent_release_candidate(" + ",".join(candidate) + ") VALUES(" + ",".join("?" for _ in candidate) + ")",
                    tuple(candidate.values()),
                )
        finally:
            db.execute("ROLLBACK TO SAVEPOINT candidate_probe")
            db.execute("RELEASE SAVEPOINT candidate_probe")
        release = dict(db.execute("SELECT * FROM team_subagent_release LIMIT 1").fetchone())
        release.update(
            id="arl_legacy_policy",
            revision_id=pending.revision_id,
            release_sequence=2,
            capability_fingerprint=candidate["capability_fingerprint"],
            dependency_fingerprint=candidate["dependency_fingerprint"],
            risk_json=candidate["risk_json"],
            safety_report_id=candidate["safety_report_id"],
            audience_snapshot_json=candidate["target_audience_json"],
            audience_hash=candidate["target_audience_hash"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="publication proof required"):
            db.execute(
                "INSERT INTO team_subagent_release(" + ",".join(release) + ") VALUES(" + ",".join("?" for _ in release) + ")", tuple(release.values())
            )
    assert snapshot(path) == after
    # The current application only ever runs on head (the container upgrades head
    # before start); later revisions add tables its writers need (e.g. 0123).
    _upgrade(path)
    # The current application marks the old candidate stale with a finite reason
    # so its Owner can create a newly validated candidate instead of getting 500.
    store = AgentRevisionStore(str(path), validator=_Validator(), clock=_Clock())
    with sqlite3.connect(path) as db:
        version = db.execute("SELECT row_version FROM team_subagent_release_candidate WHERE id=?", (pending.candidate_id,)).fetchone()[0]
    with pytest.raises(AgentReleaseStaleError) as rejected:
        await store.approve_candidate(
            team_id="team_release",
            candidate_id=pending.candidate_id,
            actor_user_id="user_admin",
            expected_row_version=version,
            expected_candidate_hash=_candidate_hash_of(path, pending.candidate_id),
        )
    assert "agent_runtime_extension_unproven" in rejected.value.reason_codes
    case = SimpleNamespace(path=path, store=store)
    replacement = await seed_release(case, working)
    assert replacement.revision_id != pending.revision_id and replacement.state == "pending"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_revision_extensions WHERE revision_ref=?", (pending.revision_id,)).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM team_subagent_revision_extensions WHERE revision_ref=?", (replacement.revision_id,)).fetchone() == (
            1,
        )
        assert db.execute("SELECT COUNT(*) FROM team_subagent_release WHERE revision_id=?", (pending.revision_id,)).fetchone() == (0,)


@pytest.mark.asyncio
async def test_old_unproved_live_and_bound_revision_cannot_dispatch_after_upgrade(tmp_path):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore

    current = setup(tmp_path)
    working = await save(current, {**_definition(), **policy()})
    await seed_release(current, working)
    binding = await native_binding(current)
    path = tmp_path / "previous-live.db"
    _historical_fixture(current.path, path, MIGRATION.down_revision)
    _upgrade(path, MIGRATION.revision)
    before = snapshot(path)
    store = AgentExecutionBindingStore(path)
    for key in ("extensions-binding", "new-dispatch"):
        with pytest.raises(AgentExecutionBindingError, match="agent_runtime_extension_unproven"):
            await store.admit_team_execution(
                team_id="team_release",
                user_id="user_owner",
                turn_id="t_extensions",
                profile_name="release-agent",
                agent_id="osa_release",
                dispatch_key=key,
            )
    assert snapshot(path) == before
    # Independently exercise the database guard with the exact previously valid
    # execution shape, without going through the new application reader.
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM turn_agent_execution WHERE id=?", (binding.execution_id,)).fetchone())
        row.update(id="aex_old_worker", dispatch_key="old-worker-dispatch")
        with pytest.raises(sqlite3.IntegrityError, match="execution proof required"):
            db.execute("INSERT INTO turn_agent_execution(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
    assert snapshot(path) == before
