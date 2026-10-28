"""Single @ source uses the original baseline and the actual model/tool owners."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_read_access import ReferenceSkillReadAccess, SkillReadAccessError
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import SubagentRunner
from test_reference_start_admission import _assert_no_acceptance
from test_reference_start_preparation import activate, prepare, raw_document
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_read_access import receiver
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import facts, restore
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case
from test_skill_reference_invocation import tools_for
from test_skill_reference_runtime import expire, rows
from test_skill_reference_runtime import runtime_case as _runtime_case

claimed_case, receiver_case, start_case, runtime_case = _claimed_case, _receiver_case, _start_case, _runtime_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.mark.parametrize("receiver_case", ["admission_empty", "admission_unused_invoke"], indirect=True)
async def test_single_agent_admission_requires_the_new_composition_reader(receiver_case):
    fixture = receiver_case
    activate(fixture)
    document = raw_document(fixture, skills=False)
    prepared = await prepare(fixture, document=document)
    with sqlite3.connect(fixture.case.fixture.db_path) as db:
        db.execute("DROP TRIGGER trg_reference_primary_baseline_composition")
    fixture.case.args.update(reference_start=prepared, user_text=document.display_text())
    with pytest.raises(ValueError, match="reference_primary_baseline_migration_required"):
        await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_single_agent_rechecks_every_original_source_at_admission(receiver_case):
    fixture = receiver_case
    activate(fixture)
    document = raw_document(fixture, skills=False)
    prepared = await prepare(fixture, document=document)
    await fixture.sources.instances.upsert(
        user_id=fixture.scope.user_id, name="unused", source_type="builtin", source_path=str(fixture.root / "unused"), enabled=False
    )
    fixture.case.args.update(reference_start=prepared, user_text=document.display_text())
    with pytest.raises(ValueError):
        await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    _assert_no_acceptance(fixture.case)


@pytest.mark.parametrize("receiver_case", ["admission", "admission_unused_sop", "admission_unused_invoke", "admission_empty"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
async def test_single_agent_retains_the_complete_baseline_without_selected_methods(claimed_case):
    case = claimed_case
    result = await claim_reference_skill_primary_for_worker(**case.args)
    assert result.outcome == "claimed"
    composition, prepared, rendered = await restore(case)
    assert composition.runtime_policy == "initial-primary-baseline.v1"
    assert composition.members == prepared.closure.roots == prepared.closure.items == ()
    assert rendered.document()["skills"] == []
    assert len(prepared.baseline.packages) == len(prepared.receiver.grants)
    assert len(composition.members) == 0 and composition.content.node_id == composition.node_id
    assert len([node for node in result.primary.source.input_document.nodes if node.type == "reference"]) == 1
    before = facts(case)
    assert (await claim_reference_skill_primary_for_worker(**case.args)).outcome == "already_running"
    assert facts(case) == before
    for package in prepared.baseline.packages:
        (case.fixture.root / package.proof.name / "SKILL.md").unlink()
    assert await restore(case) == (composition, prepared, rendered)


async def access_for(case):
    access = await ReferenceSkillReadAccess.bind(
        sources=case.base.fixture.sources,
        cipher=case.runtime.cipher,
        scope=case.runtime.scope,
        turn_id=case.runtime.turn_id,
        owner_id=case.runtime.owner_id,
        generation=1,
    )
    case.ctx.reference_skill_required, case.ctx.reference_skill_access = True, access
    return access


@pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
async def test_unselected_sop_uses_original_arguments_and_files(runtime_case):
    case = runtime_case
    access = await access_for(case)
    (case.base.fixture.root / "unused" / "SKILL.md").unlink()
    with receiver(access.subagent_id):
        result = await access.read_tool({"name": "unused", "args": "profit"}, case.ctx)
        assert "BASELINE_SOP_unused profit" in result["body"]
        assert "selected_method" not in result
        guide = await access.read_tool({"name": "unused", "file": "references/guide.md"}, case.ctx)
        assert "BASELINE_GUIDE_unused" in json.dumps(guide)
        with pytest.raises(SkillReadAccessError):
            await access.read_tool({"name": "unknown"}, case.ctx)


@pytest.mark.parametrize("receiver_case", ["admission_empty"], indirect=True)
@pytest.mark.parametrize("change", ["subagent", "context", "execution", "generation", "expired"])
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
async def test_empty_baseline_still_requires_the_exact_live_receiver(runtime_case, change):
    case = runtime_case
    access = await access_for(case)
    assert access.handles == ()
    subagent = access.subagent_id
    if change == "subagent":
        subagent = "helper"
    elif change == "context":
        case.ctx.workspace_id = "other"
    elif change == "execution":
        access = replace(access, execution_id="other")
        subagent = access.subagent_id
        case.ctx.reference_skill_access = access
    elif change == "generation":
        access = replace(access, generation=2)
        case.ctx.reference_skill_access = access
    else:
        expire(case)
    with receiver(subagent), pytest.raises(SkillReadAccessError):
        await access.read_model_input(ctx=case.ctx)
    assert rows(case, "chat_reference_model_invocations") == []


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke", "admission_empty"], indirect=True)
@pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True)
async def test_single_agent_actual_model_loop_preserves_baseline_and_physical_receipts(runtime_case, monkeypatch):
    case = runtime_case
    registry, calls, _ = tools_for(case.base.fixture.root, monkeypatch)
    invocation = bool(case.primary.binding.definition["skill_grants"])
    original = None
    if invocation:
        script = case.base.fixture.root / "unused/scripts/runner.py"
        original = script.read_bytes()
        script.unlink()
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=registry, team_subagent_store=case.users)
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        text = json.dumps(body, ensure_ascii=False)
        assert "BODY_method" not in text and "BODY_support" not in text
        assert "Required selected method data" not in text
        assert case.primary.source.input_document.text_content() in text
        if invocation and len(sent) == 1:
            return httpx2.Response(
                200,
                json={
                    "id": "original-baseline",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "original-invoke",
                                        "type": "function",
                                        "function": {
                                            "name": "skill_invoke",
                                            "arguments": json.dumps({"name": "unused", "script": "runner.py", "input": {}}),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )
        if invocation:
            assert "CALCULATED 42" in text
        return success("openai", "stream" if body.get("stream") else "invoke")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await case.runtime.run(runner=runner, parent_ctx=case.ctx, parent_bus=TurnEventBus())
            assert result.ok and result.final_answer == "OK"
            assert route.call_count == (2 if invocation else 1)
        assert len(calls) == (1 if invocation else 0)
        if invocation:
            assert calls[0][2] == original and not calls[0][1].exists()
        assert len(rows(case, "chat_reference_model_contexts")) == len(sent)
        assert [row["status"] for row in rows(case, "chat_reference_model_invocations")] == ["completed"] * len(sent)
        assert rows(case, "turn_agent_execution")[0]["status"] == "completed"
        assert [row["kind"] for row in rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
        assert case.ctx.reference_skill_access is None and case.ctx.skill_runtime_grants is None
    finally:
        await adapter.close()
