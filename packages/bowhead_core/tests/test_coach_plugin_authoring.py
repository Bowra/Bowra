"""Plugin saves keep domain revisions and receipts in one authorized transaction."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.coach_plugin_authoring import CoachPluginAuthoring
from test_agent_revision_store import _db, _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0141_coach_plugin_operations")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_plugin_operations_migration_preserves_history(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {name: after[name] for name in before} == before
    assert ("coach_plugin_operation" in after) == (target == "core")
    structure = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == structure
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision


def test_partial_plugin_migration_rolls_back(tmp_path, monkeypatch):
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


async def allow(db, write):
    assert db.in_transaction
    return True


async def validate(candidate, observed_tools):
    return {"status": "ready", "fingerprint": "test_compilation"}


def setup(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    scope = ReferenceRequestScope(team_id="team_release", user_id="user_owner", workspace_id="ws_original", session_id="s_original")
    return CoachPluginAuthoring(str(path), scope, allow, validate)


def create_args():
    return {
        "target": None,
        "patch": {"name": "Research", "tools": [{"kind": "tool", "id": "web_search", "revision": "observed"}]},
        "expected_impact": None,
        "request_id": "create",
        "turn_id": "t_original",
    }


class LostReply(BaseException):
    pass


async def test_lost_reply_replays_without_new_plugin_or_revision(tmp_path, monkeypatch):
    service = setup(tmp_path)
    commit = aiosqlite.Connection.commit

    async def lose_reply(db):
        await commit(db)
        raise LostReply()

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "commit", lose_reply)
        with pytest.raises(LostReply):
            await service.save(**create_args())
    original = await service.save(**create_args())
    assert (await service.get(operation_id=original["operation_id"]))["current_result_matches"]
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await service.save(**{**create_args(), "patch": {**create_args()["patch"], "name": "Changed intent"}})
    inspected = await service.inspect(plugin_id=original["result_target"]["id"])
    edited = await service.save(
        target=inspected["target"],
        patch={"description": "A focused description edit"},
        expected_impact=inspected["impact"]["fingerprint"],
        request_id="edit",
        turn_id="t_original",
    )
    assert edited["result_target"]["revision"] == 2
    fresh = await service.inspect(plugin_id=edited["result_target"]["id"])
    unchanged = await service.save(
        target=fresh["target"],
        patch={"description": "A focused description edit"},
        expected_impact=fresh["impact"]["fingerprint"],
        request_id="same_contents",
        turn_id="t_later",
    )
    assert unchanged["effect"] == "unchanged" and unchanged["result_target"] == edited["result_target"]
    assert (await service.save(**create_args()))["result_target"]["revision"] == 1
    assert not (await service.get(operation_id=original["operation_id"]))["current_result_matches"]
    with sqlite3.connect(service.db_path) as db:
        assert db.execute("SELECT count(*) FROM team_capability").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM team_capability_revision").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE coach_plugin_operation SET result_json='{}'")


async def test_failure_before_receipt_rolls_back_domain_revision(tmp_path):
    service = setup(tmp_path)
    with sqlite3.connect(service.db_path) as db:
        db.execute(
            "CREATE TRIGGER reject_test_receipt BEFORE INSERT ON coach_plugin_operation BEGIN SELECT RAISE(ABORT, 'Receipt storage interrupted'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
        await service.save(**create_args())
    with sqlite3.connect(service.db_path) as db:
        db.execute("DROP TRIGGER reject_test_receipt")
    with sqlite3.connect(service.db_path) as db:
        assert db.execute("SELECT count(*) FROM team_capability").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM coach_plugin_operation").fetchone()[0] == 0
    assert (await service.save(**create_args()))["state"] == "succeeded"


async def test_update_requires_exact_target_impact_and_current_authority(tmp_path):
    from test_agent_change_proposal_store import _working

    service = setup(tmp_path)
    original = await service.save(**create_args())
    inspected = await service.inspect(plugin_id=original["result_target"]["id"])
    await _working(service.db_path)
    with sqlite3.connect(service.db_path) as db:
        db.execute(
            "UPDATE team_subagent_working_copy SET definition_json=json_set(definition_json,'$.capability_ids',json_array(?))",
            (inspected["target"]["id"],),
        )
    intent = {
        "target": inspected["target"],
        "patch": {"description": "Shared update"},
        "expected_impact": inspected["impact"]["fingerprint"],
        "request_id": "update",
        "turn_id": "t_original",
    }
    with pytest.raises(ValueError, match="impact_changed"):
        await service.save(**intent)
    fresh = await service.inspect(plugin_id=inspected["target"]["id"])
    assert fresh["impact"]["affected_agent_count"] == 1
    updated = await service.save(**{**intent, "expected_impact": fresh["impact"]["fingerprint"]})
    with pytest.raises(ValueError, match="version_conflict"):
        await service.save(**{**intent, "request_id": "another"})

    async def denied(db, write):
        if write:
            raise ValueError("plugin_team_admin_required")
        return False

    service.authorize = denied
    with pytest.raises(ValueError, match="team_admin_required"):
        await service.save(**create_args())
    assert (await service.get(operation_id=updated["operation_id"]))["state"] == "succeeded"
