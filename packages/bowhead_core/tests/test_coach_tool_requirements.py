"""Durable Tool handoffs retain immutable identity without creating resources."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.coach_tool_requirements import CoachToolRequirements
from test_agent_revision_store import _db, _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0140_coach_tool_requirements")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_requirements_migration_preserves_history_and_all_targets(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {name: after[name] for name in before} == before
    assert ("coach_tool_requirement" in after) == (target == "core")
    assert ("coach_tool_observation" in after) == (target == "core")
    structure = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == structure
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision


def test_failed_migration_restores_the_previous_schema(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    monkeypatch.setattr(MIGRATION, "_DDL", (*MIGRATION._DDL[:2], "invalid sql"))
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


async def allow(db, parent, exact):
    assert db.in_transaction
    return {"baseline_matches": True, "current_target": parent, "binding_available": False}


async def setup(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    scope = ReferenceRequestScope(team_id="team_release", user_id="user_owner", workspace_id="ws_original", session_id="s_original")
    return CoachToolRequirements(str(path), scope, allow)


class LostReply(BaseException):
    pass


async def test_committed_request_survives_response_loss_and_changed_intent_is_rejected(tmp_path, monkeypatch):
    store = await setup(tmp_path)
    args = {
        "parent": None,
        "requirement": {"workflow": "engineering", "operation": "Read customer records"},
        "request_id": "one",
        "turn_id": "t_original",
    }
    commit = aiosqlite.Connection.commit

    async def lose_response(db):
        await commit(db)
        raise LostReply()

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "commit", lose_response)
        with pytest.raises(LostReply):
            await store.create(**args)
    result = await store.create(**args)
    assert result["ok"] and not result["resource_created"]
    assert (await store.get(requirement_id=result["requirement_id"]))["requirement"] == args["requirement"]
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await store.create(**{**args, "requirement": {**args["requirement"], "operation": "Change customer records"}})
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT count(*) FROM coach_tool_requirement").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE coach_tool_requirement SET requirement_json='{}'")


async def test_observation_recovery_does_not_reverify_or_rebind_old_intent(tmp_path):
    store = await setup(tmp_path)
    created = await store.create(parent=None, requirement={"workflow": "openapi"}, request_id="one", turn_id="t_original")
    calls = []

    async def verify(db, reference):
        calls.append(reference)

    args = {
        "requirement_id": created["requirement_id"],
        "reference": {"kind": "tool", "id": "actual-ref", "revision": "actual-revision"},
        "request_id": "observe",
        "turn_id": "t_original",
        "verify": verify,
    }
    observed = await store.observe(**args)
    repeated = await store.observe(**args)
    assert observed["observation"]["id"] == repeated["observation"]["id"] and len(calls) == 1
    assert not repeated["parent_bound"]
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await store.observe(**{**args, "reference": {**args["reference"], "id": "different"}})
    with sqlite3.connect(store.db_path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE coach_tool_observation SET reference_json='{}'")
    other = CoachToolRequirements(store.db_path, store.scope.model_copy(update={"user_id": "user_member"}), allow)
    with pytest.raises(ValueError, match="unavailable"):
        await other.get(requirement_id=created["requirement_id"])
