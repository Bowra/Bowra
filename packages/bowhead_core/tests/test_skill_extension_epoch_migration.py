"""New request evidence uses current authority; old evidence remains immutable."""

import json
import sqlite3
from importlib import import_module

import pytest
import respx
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_read_extensions import SkillExtensionPolicyCheck
from bowhead_core.subagent import SubagentRunner
from test_chat_reference_storage import _historical_fixture, _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case
from test_skill_reference_invocation import tools_for
from test_skill_reference_runtime import runtime_case as _runtime_case

start_case, receiver_case, claimed_case, runtime_case = _start_case, _receiver_case, _claimed_case, _runtime_case
MIGRATION = import_module("bowhead_core.migrations.versions.0082_extension_policy_epoch")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_migration_preserves_all_rows_and_only_changes_core_evidence_guards(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    current = schema(path)
    assert snapshot(path) == before
    if target == "core":
        assert all(row in current for row in previous if row[1] != MIGRATION.SCOPE_TRIGGER)
        assert {row[1] for row in current} - {row[1] for row in previous} == {MIGRATION.TRIGGER}
    else:
        assert current == previous
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == current and snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)


@pytest.mark.parametrize("drift", ["name_collision", "changed_guard"])
def test_drift_never_partially_changes_accepted_request_contract(tmp_path, drift):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        if drift == "name_collision":
            db.execute(f"CREATE TABLE {MIGRATION.TRIGGER}(x TEXT)")
        else:
            db.execute(f"DROP TRIGGER {MIGRATION.SCOPE_TRIGGER}")
            db.execute(f"CREATE TRIGGER {MIGRATION.SCOPE_TRIGGER} BEFORE INSERT ON chat_reference_extension_checks BEGIN SELECT 1; END")
    before = schema(path)
    with pytest.raises(RuntimeError, match="schema drift"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before


def test_failure_after_scope_replacement_restores_all_old_guards(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    before = schema(path)
    original = Connection.exec_driver_sql

    def fault(self, sql, *args, **kwargs):
        result = original(self, sql, *args, **kwargs)
        if sql.strip().startswith(f"CREATE TRIGGER {MIGRATION.SCOPE_TRIGGER}"):
            raise RuntimeError("post-DDL failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fault)
    with pytest.raises(RuntimeError, match="post-DDL failure"):
        _upgrade(path, MIGRATION.revision)
    assert schema(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_case", ["team"], indirect=True)
@pytest.mark.parametrize("receiver_case", ["admission_extension_unused_invoke"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["extension"], indirect=True)
async def test_nonempty_v1_evidence_receipts_and_retained_ciphertext_survive_upgrade(runtime_case, tmp_path, monkeypatch):
    from bowhead_core.agent_behavior_contract import canonical_fingerprint

    case = runtime_case
    registry, _, _ = tools_for(case.base.fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    try:
        runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
            assert (await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())).ok
    finally:
        await adapter.close()
    path = tmp_path / "previous.db"
    _historical_fixture(case.path, path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        # Fixture import only: reconstruct actual 0081 JSON v1, without inventing
        # new authority for historical requests. Restore its exact old guards.
        guards = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='chat_reference_extension_checks'").fetchall()
        for name, _ in guards:
            db.execute(f'DROP TRIGGER "{name}"')
        saved = db.execute("SELECT invocation_id,policy_check_json FROM chat_reference_extension_checks").fetchall()
        assert saved
        for invocation, raw in saved:
            value = json.loads(raw)
            value.pop("current_access_epoch")
            value["schema_version"] = 1
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
            digest = canonical_fingerprint(value)
            assert SkillExtensionPolicyCheck.model_validate_json(encoded).digest == digest
            db.execute(
                "UPDATE chat_reference_extension_checks SET policy_check_json=?,policy_check_digest=? WHERE invocation_id=?",
                (encoded, digest, invocation),
            )
        for _, sql in guards:
            db.execute(sql)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    before = snapshot(path)
    assert all(
        before[table]
        for table in (
            "chat_reference_resolutions",
            "chat_reference_content_snapshots",
            "chat_reference_model_invocations",
            "chat_reference_extension_checks",
        )
    )
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before


@pytest.mark.parametrize("version,epoch", [(1, 5), (2, None), (2, True), (2, -1), (3, 5)])
def test_evidence_decoder_rejects_mixed_or_ambiguous_epoch_contract(version, epoch):
    from pydantic import ValidationError

    value = {
        "schema_version": 2,
        "current_access_epoch": 5,
        "receiver_digest": "sha256:" + "a" * 64,
        "original_extension_digest": "sha256:" + "b" * 64,
        "current_revision_ref": "rev_1",
        "current_release_id": "release_1",
        "current_definition_hash": "sha256:" + "c" * 64,
        "current_extension_policy_digest": "sha256:" + "d" * 64,
        "closure_digest": "sha256:" + "e" * 64,
    }
    SkillExtensionPolicyCheck.model_validate(value)
    value.update(schema_version=version, current_access_epoch=epoch)
    with pytest.raises(ValidationError):
        SkillExtensionPolicyCheck.model_validate(value)
