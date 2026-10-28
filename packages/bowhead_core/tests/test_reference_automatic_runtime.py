"""Automatic source -> original primary -> retained methods -> native model receipt."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from cryptography.fernet import Fernet
from test_reference_automatic_admission import prepare_auto
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case
from test_skill_reference_claim import facts, restore
from test_skill_reference_runtime import expire, rows

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize(
        "receiver_case",
        ["admission_optional_unused_sop", "admission_optional_unused_invoke", "admission_extension_optional_unused_invoke"],
        indirect=True,
    ),
]


@pytest.fixture
async def automatic_case(receiver_case):
    fixture, start = receiver_case, receiver_case.case
    admission = await prepare_auto(fixture)
    await start.store.finalize_started_chat_submission(**start.args)
    scope = fixture.scope.model_copy(update={"session_id": "s_reference"})
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    args = {
        "sources": fixture.sources,
        "cipher": cipher,
        "scope": scope,
        "turn_id": "t_reference",
        "owner_id": "auto-worker",
        "expected_generation": 1,
    }
    return SimpleNamespace(fixture=fixture, path=start.fixture.db_path, scope=scope, cipher=cipher, args=args, admission=admission)


async def test_concurrent_claim_retains_automatic_origin_and_one_material_snapshot(automatic_case):
    case = automatic_case
    claims = await asyncio.gather(*(claim_reference_skill_primary_for_worker(**case.args) for _ in range(2)))
    assert sorted(claim.outcome for claim in claims) == ["already_running", "claimed"]
    assert claims[0].primary.binding.execution_id == claims[1].primary.binding.execution_id
    composition, prepared, rendered = await restore(case)
    assert composition.node_id is None and composition.receiver_source == "lead"
    assert composition.content.node_id is None and composition.content.receiver_source == "lead"
    assert composition.resource == case.admission.automatic_receiver
    assert len(composition.members) == 2 and len(prepared.baseline.packages) == 3
    assert b"BODY_support" in rendered.content
    value = json.loads(rows(case, "chat_reference_resolutions")[0]["composition_json"])
    assert "node_id" not in value and "node_id" not in value["content"]
    assert len(rows(case, "chat_reference_content_snapshots")) == 1
    assert rows(case, "chat_reference_evidence") == rows(case, "chat_reference_model_invocations") == []


async def test_actual_runtime_injects_methods_and_records_the_same_primary(automatic_case):
    case, fixture = automatic_case, automatic_case.fixture
    claim = await claim_reference_skill_primary_for_worker(**case.args)
    scope = ReferenceScope(**case.scope.model_dump(), policy_epoch=case.admission.policy_epoch)
    runtime = ReferencePrimaryRuntime(
        scope=scope, turn_id="t_reference", owner_id="auto-worker", bindings=fixture.case.bindings, cipher=case.cipher, skill_sources=fixture.sources
    )
    ctx = ToolExecutionContext(**scope.identity(), turn_id="t_reference", member_uid=scope.user_id, datarc=None, session_store=fixture.case.store)
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=fixture.case.fixture.users)
    selected = "BODY_optional_method" if fixture.optional else "BODY_method"

    def respond(request):
        text = json.dumps(json.loads(request.content), ensure_ascii=False).replace("\\n", "\n")
        assert selected in text and "BODY_support" in text
        assert case.admission.input_document.text_content() in text
        assert "NOT_SELECTED_MANIFEST" not in text
        calls = rows(case, "chat_reference_model_invocations")
        assert len(calls) == 1 and calls[0]["execution_id"] == claim.primary.binding.execution_id and calls[0]["status"] == "pending"
        return success("openai", "invoke")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
            assert result.ok and result.final_answer == "OK" and route.call_count == 1
            with pytest.raises(TerminalDurabilityAbort):
                await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
            assert route.call_count == 1
        assert rows(case, "turn_agent_execution")[0]["status"] == "completed"
        assert rows(case, "chat_reference_model_invocations")[0]["status"] == "completed"
        assert [item["kind"] for item in rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
        source = json.loads(rows(case, "chat_turn_user_sources")[0]["input_document_json"])
        assert source == case.admission.input_document.model_dump(mode="json")
    finally:
        await adapter.close()


async def test_composition_failure_rolls_back_owner_materials_and_execution(automatic_case):
    case = automatic_case
    before = facts(case)
    with sqlite3.connect(case.path) as db:
        db.execute(
            "CREATE TRIGGER fail_auto_composition BEFORE INSERT ON chat_reference_resolutions BEGIN SELECT RAISE(ABORT,'injected auto composition failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected auto composition failure"):
        await claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before
    with sqlite3.connect(case.path) as db:
        db.execute("DROP TRIGGER fail_auto_composition")
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"


async def test_expired_claim_closes_original_execution_without_model_replay(automatic_case):
    case = automatic_case
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    before = rows(case, "turn_agent_execution")[0]
    expire(case)
    assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == ["t_reference"]
    assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == []
    assert rows(case, "turn_agent_execution")[0]["id"] == before["id"]
    assert rows(case, "turn_agent_execution")[0]["status"] == "failed"
    assert rows(case, "chat_reference_model_invocations") == rows(case, "chat_reference_evidence") == []


@pytest.mark.parametrize("guard", ["trg_reference_automatic_composition", "trg_reference_automatic_material"])
async def test_missing_contract_prevents_partial_claim(automatic_case, guard):
    case = automatic_case
    before = facts(case)
    with sqlite3.connect(case.path) as db:
        db.execute(f"DROP TRIGGER {guard}")
    with pytest.raises(ValueError):
        await claim_reference_skill_primary_for_worker(**case.args)
    assert facts(case) == before


async def test_baseline_revocation_after_claim_stops_the_provider_request(automatic_case):
    case, fixture = automatic_case, automatic_case.fixture
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "claimed"
    scope = ReferenceScope(**case.scope.model_dump(), policy_epoch=case.admission.policy_epoch)
    runtime = ReferencePrimaryRuntime(
        scope=scope, turn_id="t_reference", owner_id="auto-worker", bindings=fixture.case.bindings, cipher=case.cipher, skill_sources=fixture.sources
    )
    ctx = ToolExecutionContext(**scope.identity(), turn_id="t_reference", member_uid=scope.user_id, datarc=None, session_store=fixture.case.store)
    await fixture.sources.instances.upsert(
        user_id=scope.user_id, name="unused", source_type="builtin", source_path=str(fixture.root / "unused"), enabled=False
    )
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=fixture.case.fixture.users)
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\..*").respond(200, json={})
            with pytest.raises(TerminalDurabilityAbort):
                await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
            assert route.call_count == 0
        assert rows(case, "chat_reference_model_invocations") == []
    finally:
        await adapter.close()
