"""Publication changes only the live optional-policy intersection, not the Agent pin."""

import json
import sqlite3
from dataclasses import replace

import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.skill_read_resolution import SkillReadResolutionError
from bowhead_core.subagent import SubagentRunner
from test_skill_extension_runtime import publish_policy, tool_response
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import restore
from test_skill_reference_claim import start_case as _start_case
from test_skill_reference_invocation import tools_for
from test_skill_reference_runtime import rows
from test_skill_reference_runtime import runtime_case as _runtime_case

start_case, receiver_case, claimed_case, runtime_case = _start_case, _receiver_case, _claimed_case, _runtime_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_extension_unused_invoke"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["extension"], indirect=True),
]


@pytest.mark.parametrize("point", ["before_dispatch", "between_calls"])
@pytest.mark.parametrize("operation", ["read", "invoke"])
async def test_republished_allowed_method_keeps_original_agent_and_records_new_authority(runtime_case, monkeypatch, point, operation):
    case = runtime_case
    original = rows(case, "chat_reference_resolutions")
    registry, calls, _ = tools_for(case.base.fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
    sent, published = [], []

    async def publish():
        published.append(await publish_policy(case, enabled=True, expand=True))

    async def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            if point == "between_calls":
                await publish()
            return (
                tool_response("skill_read", {"name": "optional_method"}, stream=body.get("stream", False))
                if operation == "read"
                else tool_response("skill_invoke", {"name": "unused", "script": "runner.py", "input": {}}, stream=body.get("stream", False))
            )
        return success("openai", "stream" if body.get("stream") else "invoke")

    try:
        if point == "before_dispatch":
            await publish()
        with respx.mock() as mock:
            mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
        assert result.ok and result.final_answer == "OK", result.error
        assert len(sent) == 2
        assert [call[0] for call in calls] == (["unused"] if operation == "invoke" else [])
        assert rows(case, "chat_reference_resolutions") == original
        assert all("A new role for future executions." not in json.dumps(body) and "BODY_optional_method" in json.dumps(body) for body in sent)
        expected = "BODY_optional_method" if operation == "read" else "CALCULATED 42"
        assert expected in [m for m in sent[-1]["messages"] if m["role"] == "tool"][-1]["content"]
        proofs = [json.loads(row["policy_check_json"]) for row in rows(case, "chat_reference_extension_checks")]
        with sqlite3.connect(case.path) as db:
            epoch = db.execute("SELECT epoch FROM access_policy_state WHERE team_id=?", (case.runtime.scope.team_id,)).fetchone()[0]
        assert epoch > case.runtime.scope.policy_epoch
        assert proofs[-1]["schema_version"] == 2 and proofs[-1]["current_access_epoch"] == epoch
        assert proofs[-1]["current_revision_ref"] == published[0].revision_id != case.primary.binding.revision_ref
        assert all(p["original_extension_digest"] == json.loads(original[0]["composition_json"])["extension_digest"] for p in proofs)
        if point == "between_calls":
            assert proofs[0]["current_access_epoch"] == case.runtime.scope.policy_epoch
        assert all(row["status"] == "completed" for row in rows(case, "chat_reference_model_invocations"))
    finally:
        await adapter.close()


@pytest.mark.parametrize("change", ["optional_disabled", "baseline_disabled", "explicit_deny", "shadow_deny", "membership", "role", "source_path"])
async def test_new_epoch_still_requires_all_current_authority(runtime_case, monkeypatch, change):
    case = runtime_case
    await publish_policy(case, enabled=True)
    fixture = case.base.fixture
    if change in {"optional_disabled", "baseline_disabled"}:
        name = "optional_method" if change == "optional_disabled" else "unused"
        await fixture.sources.instances.upsert(
            user_id=case.runtime.scope.user_id, name=name, source_type="builtin", source_path=str(fixture.root / name), enabled=False
        )
    elif change == "source_path":
        original_index = fixture.sources.builtin_index()
        changed = replace(
            original_index,
            items=tuple(replace(item, relative_path="replaced_method") if item.name == "optional_method" else item for item in original_index.items),
        )
        fixture.sources.builtin_index = lambda: changed
    else:
        if change == "shadow_deny":
            monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
        with sqlite3.connect(case.path) as db:
            if change in {"explicit_deny", "shadow_deny"}:
                db.execute(
                    "INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,principal_type,principal_id,permission,effect,created_at,updated_at) "
                    "VALUES('rg_epoch_deny',?,'skill','optional_method','user',?,'use','deny','2026-09-15','2026-09-15')",
                    (case.runtime.scope.team_id, case.runtime.scope.user_id),
                )
            elif change == "membership":
                db.execute(
                    "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?",
                    (case.runtime.scope.workspace_id, case.runtime.scope.user_id),
                )
            else:
                db.execute(
                    "UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?",
                    (case.runtime.scope.team_id, case.runtime.scope.user_id),
                )
    registry, calls, _ = tools_for(fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    try:
        runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
            with pytest.raises(TerminalDurabilityAbort):
                await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert route.call_count == 0
        assert calls == [] and rows(case, "chat_reference_extension_checks") == [] and rows(case, "chat_reference_model_invocations") == []
    finally:
        await adapter.close()


async def test_retained_source_comparison_does_not_change_old_proof_or_accept_future_epoch(runtime_case):
    case = runtime_case
    _, prepared, _ = await restore(case.base)
    original = prepared.closure.items[0].proof
    await publish_policy(case, enabled=True)
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(SkillReadResolutionError):
            await case.base.fixture.sources.revalidate_in_connection(db, original)
        await case.base.fixture.sources.revalidate_retained_in_connection(db, original)
        with pytest.raises(SkillReadResolutionError):
            await case.base.fixture.sources.revalidate_retained_in_connection(db, replace(original, policy_epoch=original.policy_epoch + 100))
    assert prepared.closure.items[0].proof == original


async def test_extension_admission_requires_current_evidence_schema(runtime_case):
    from bowhead_core.skill_read_receiver import SkillReadReceiverError, require_prepared_primary_skill_baseline

    case = runtime_case
    _, prepared, _ = await restore(case.base)
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("DROP TRIGGER trg_reference_extension_epoch_check")
        with pytest.raises(SkillReadReceiverError, match="migration_required"):
            await require_prepared_primary_skill_baseline(db, case.primary, prepared.closure, sources=case.base.fixture.sources)
        await db.rollback()


async def test_sql_rejects_a_stale_access_epoch_before_any_physical_request(runtime_case, monkeypatch):
    from bowhead_core import skill_read_extensions

    case = runtime_case
    original_check = skill_read_extensions.require_current_extension_policy

    async def stale(*args, **kwargs):
        current = await original_check(*args, **kwargs)
        return current.model_copy(update={"current_access_epoch": current.current_access_epoch - 1})

    monkeypatch.setattr(skill_read_extensions, "require_current_extension_policy", stale)
    registry, _, _ = tools_for(case.base.fixture.root, monkeypatch)
    adapter = adapter_for("openai")
    try:
        runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(return_value=success("openai", "invoke"))
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert not result.ok and route.call_count == 0
        assert rows(case, "chat_reference_extension_checks") == [] and rows(case, "chat_reference_model_invocations") == []
    finally:
        await adapter.close()
