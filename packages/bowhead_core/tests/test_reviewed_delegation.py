"""Reviewed helper observations use the caller's existing review and evidence scope."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.events import OrchestratorEvent
from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_delivery_review import (
    ReviewedDeliveryResult,
    delivery_summary_input,
    failed_calls,
    requires_delivery_review,
    review_guidance,
)
from bowhead_core.tool_outcome import classify_tool_outcome
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_core.types import ToolInvocation
from test_agent_collaboration_runner import RunnerFixture, receipts
from test_agent_collaboration_runner import collab as collab

from bowhead_router import LLMResponse, ToolCall

pytestmark = pytest.mark.integration


async def test_native_helper_review_sets_only_current_run_witness(collab, monkeypatch):
    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    requests = 0

    async def rejected(args, ctx):
        return {"ok": False, "error_kind": "invalid_input"}

    async def provider(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return LLMResponse(content="", tool_calls=[ToolCall(id="rejected", name="probe", arguments={})])
        payload = {"delivery_surface": "chat", "exec_summary": "The requested invalid probe input was rejected."}
        if requests == 3:
            context = next(item["review_context"] for item in receipts(request) if "review_context" in item)
            failed = context["failed_calls"][0]["call_id"]
            payload["delivery_review"] = {
                "requirements": [
                    {
                        "requirement": "Observe the requested rejection",
                        "expected_outcome": "failure",
                        "status": "fulfilled",
                        "evidence_call_ids": [failed],
                    }
                ],
                "issues": [
                    {
                        "call_id": failed,
                        "resolution": "expected",
                        "evidence_call_ids": [failed],
                        "explanation": "The requested invalid input was rejected before the probe handler performed business work.",
                    }
                ],
            }
        return LLMResponse(content="", tool_calls=[ToolCall(id=f"return-{requests}", name="subagent_return", arguments={"payload": payload})])

    harness = RunnerFixture(collab, provider)
    harness.register("probe", rejected)
    register_subagent_return_tool(harness.registry)
    result = await harness.run(collab.requester)
    assert not harness.provider.failures, harness.provider.failures
    assert requests == 3 and result.ok and result.completion == "complete"
    assert result.reviewed_delivery_complete is True
    assert len(failed_calls(result.tool_invocations)) == 1


@pytest.mark.parametrize("kind", ["reviewed", "plain", "partial", "unknown"])
async def test_native_spawn_marks_reviewed_failures_without_reclassifying_delegation(kind):
    class Child:
        async def run(self, **kwargs):
            return SubagentResult(
                profile="compute",
                subagent_id="sub_child",
                ok=True,
                final_answer="The requested observation was delivered.",
                payload={"delivery_surface": "chat", "exec_summary": "The requested observation was delivered."},
                completion="partial" if kind == "partial" else "complete",
                ordinary_completion_reason="tool_unknown" if kind == "unknown" else "completed",
                reviewed_delivery_complete=kind != "plain",
                tool_invocations=[ToolInvocation("bi_query", {}, call_id="child_failed", result={"ok": False})],
            )

    registry = build_default_registry()
    register_subagent_tool(registry)
    context = ToolExecutionContext(
        workspace_id="ws_test", member_uid="m", user_id="u", turn_id="t", datarc=None, subagent_runner=Child(), parent_bus=SimpleNamespace(emit=_emit)
    )
    invocation = await registry.execute("subagent_spawn", {"profile": "compute", "task": "Observe rejection", "delegation_role": "helper"}, context)
    assert invocation.error is None
    assert isinstance(invocation.result, ReviewedDeliveryResult) is (kind == "reviewed")
    assert ("_chat_direct_delivery" in invocation.result) is False
    assert classify_tool_outcome(invocation.result).state == ("partial" if kind == "partial" else "success")
    assert requires_delivery_review([invocation]) is (kind in {"reviewed", "partial"})
    assert not requires_delivery_review([ToolInvocation("subagent_spawn", {}, call_id="plain", result=dict(ReviewedDeliveryResult(ok=True)))])


async def _emit(event):
    return None


@pytest.mark.parametrize("budget_enabled", [False, True])
@pytest.mark.parametrize("retained_source", [False, True])
async def test_lead_reviews_current_delegation_observation_and_preserves_raw_child_failure(collab, monkeypatch, budget_enabled, retained_source):
    from bowhead_core.agents.lead_orchestrator import _LEAD_TOOL_DENY, _emit_event

    monkeypatch.setenv("FEATURE_TOOL_DELIVERY_REVIEW", "1")
    monkeypatch.setenv("BOWHEAD_PSI_CAP_ENABLED", "1" if budget_enabled else "0")
    requests = 0
    live = []

    async def child(args, context):
        if retained_source:
            context.knowledge_source_uses["kbf_retained"] = "generation_current"
        for event, payload in [
            ("step_started", {"step_id": "child_query", "tool_name": "bi_query", "subagent_id": "child"}),
            ("step_done", {"step_id": "child_query", "tool_name": "bi_query", "subagent_id": "child", "state": "failure"}),
            ("lead.subagent_summary", {"subagent_id": "child", "profile": "compute", "status": "succeeded_with_warnings"}),
        ]:
            await context.parent_bus.emit(OrchestratorEvent(event_type=event, turn_id=context.turn_id, payload=payload))
        return ReviewedDeliveryResult(ok=True, final_answer="The requested rejection was observed.", padding="x" * 6000)

    async def provider(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return LLMResponse(content="", tool_calls=[ToolCall(id="delegation", name="subagent_spawn", arguments={})])
        if requests == 2:
            return LLMResponse(content="The requested rejection was observed.")
        if requests == 3:
            guidance = next(message.content for message in request.messages if "review_context (JSON data): " in message.content)
            context = json.loads(guidance.split("review_context (JSON data): ", 1)[1])
            assert not context["failed_calls"]
            success = context["successful_calls"][0]["call_id"]
            assert "child_failed" not in guidance
            review = {
                "requirements": [
                    {
                        "requirement": "Observe the requested rejection",
                        "status": "fulfilled",
                        "expected_outcome": "success",
                        "evidence_call_ids": [success],
                    }
                ],
                "issues": [],
            }
            return LLMResponse(
                content="", tool_calls=[ToolCall(id="review", name="review_delivery", arguments={"payload": {"delivery_review": review}})]
            )
        return LLMResponse(content="The requested rejection was observed.")

    harness = RunnerFixture(collab, provider)
    harness.register("subagent_spawn", child)
    register_subagent_return_tool(harness.registry)
    from bowhead_core.events import TurnEventBus

    async def collect(event):
        live.append(event)

    harness.bus = TurnEventBus(log_sink=collect, persist_before_live=True)
    context_changes = {}
    if retained_source:
        from bowhead_core.agent_knowledge_policy import KnowledgeScope

        worker = collab.worker
        source = SimpleNamespace(
            file_id="kbf_retained",
            team_id=worker.team_id,
            scope="space",
            scope_id=worker.workspace_id,
            soft_deleted_at=None,
            active_generation_id="generation_current",
        )
        context_changes = {
            "knowledge_scope": KnowledgeScope(
                schema_version=1,
                user_id=worker.user_id,
                team_id=worker.team_id,
                workspace_id=worker.workspace_id,
                file_ids=(source.file_id,),
                bindings=(),
            ),
            "knowledge_source_uses": {source.file_id: source.active_generation_id},
            "kb_files_store": SimpleNamespace(get=AsyncMock(return_value=source)),
            "kb_file_authorizer": AsyncMock(return_value=True),
        }
    result = await harness.run(collab.lead, tool_deny=_LEAD_TOOL_DENY, context_changes=context_changes)
    assert not harness.provider.failures, harness.provider.failures
    assert requests == 4 and result.completion == "complete" and result.reviewed_delivery_complete
    original = result.tool_invocations[0]
    assert classify_tool_outcome(original.result).state == "success"
    if retained_source:
        assert original.result["knowledge_provenance"]["sources"] == {"kbf_retained": "generation_current"}
    assert not failed_calls(result.tool_invocations)
    if budget_enabled:
        assert isinstance(original.result_full, ReviewedDeliveryResult)
    await _emit_event(harness.bus, collab.worker.turn_id, "done", {"status": "completed", **delivery_summary_input(result)})
    done = next(event.payload for event in live if event.event_type == "done")
    assert done["step_outcome"]["answer_affected"] is False
    assert done["step_outcome"]["issue_steps"] == 1
    assert done["step_outcome"]["unresolved_steps"] == 1
    assert "_delivery_completion" not in done


def test_readback_guidance_names_original_granted_python_binding():
    calls = [ToolInvocation("bi_query", {}, call_id="mixed", result={"ok": True, "completion": "partial"})]
    guidance = review_guidance(calls)
    assert 'inputs={"rows":{"artifact_id":"<original reference>"}}' in guidance
    assert "INPUTS['rows']['path']" in guidance
    assert "without querying BI again" in guidance
    assert "Never cite child call IDs" in guidance


def test_checkpoint_codec_does_not_restore_current_native_review_authority():
    from bowhead_core.subagent_resume import decode_material, encode_material
    from bowhead_core.tool_outcome import invocation_result
    from bowhead_router.types import Message

    result = ReviewedDeliveryResult(ok=True, final_answer="A prior observation.")
    invocation = ToolInvocation("subagent_spawn", {}, call_id="dispatch", result=result)
    invocation.result_full = result
    messages = [
        Message(role="user", content="Observe the rejection."),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="dispatch", name="subagent_spawn", arguments={})]),
        Message(role="tool", name="subagent_spawn", tool_call_id="dispatch", content=json.dumps(result)),
    ]
    assert requires_delivery_review([invocation])
    encoded, _ = encode_material(messages, [], max_sealed=600_000)
    restored = decode_material(encoded).messages
    assert len(restored) == 3
    replayed_result = json.loads(restored[-1].content)
    replayed = ToolInvocation("subagent_spawn", {}, call_id=restored[-1].tool_call_id, result=replayed_result)
    assert not isinstance(invocation_result(replayed), ReviewedDeliveryResult)
    assert not requires_delivery_review([replayed])
