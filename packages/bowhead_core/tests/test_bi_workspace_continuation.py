"""Actual native loop continuation; only provider HTTP is substituted.

This tests conversation continuity, not durable resume admission. Production
reference runtimes still decline cards until the checkpoint has a durable owner.
"""

from __future__ import annotations

import copy
import json

import httpx2
import pytest
import respx
from bowhead_core.bi_workspace_continuation import BiWorkspaceContinuation
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import build_default_registry
from test_reference_continuation_boundary import bind_ambiguous_workspace, question_response, workspace_question
from test_skill_model_invocation import adapter_for, router_for
from test_skill_reference_runtime import claimed_case as _claimed_case
from test_skill_reference_runtime import receiver_case as _receiver_case
from test_skill_reference_runtime import runtime_case as _runtime_case
from test_skill_reference_runtime import start_case as _start_case

claimed_case, receiver_case, runtime_case, start_case = _claimed_case, _receiver_case, _runtime_case, _start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["agent_baseline"], indirect=True),
]


def arguments(case):
    ctx = copy.copy(case.ctx)
    bind_ambiguous_workspace(ctx)
    return {
        "profile_name": case.primary.binding.runtime_profile_name,
        "task": case.primary.source.input_document.text_content(),
        "parent_turn_id": case.runtime.turn_id,
        "parent_ctx": ctx,
        "parent_bus": TurnEventBus(),
        "exact_binding": case.primary.binding,
        "pre_minted_subagent_id": "sub_original_bi",
        "tool_deny": frozenset({"clarification_ask", "plan_propose", "subagent_spawn"}),
        "capture_bi_workspace_continuation": True,
    }


@pytest.mark.parametrize("runtime_limit", [None, 1, 2])
async def test_native_resume_keeps_original_tool_result_and_appends_answer(runtime_case, monkeypatch, runtime_limit):
    monkeypatch.setenv("BOWHEAD_PEER_ITER_TELEMETRY", "1")
    # Without a language_context, language decision v2 appends its fallback rule in the final tail
    # slot; this case asserts the clarification answer is the last message, so pin that path off.
    monkeypatch.setenv("FEATURE_LANGUAGE_DECISION_V2", "0")
    case = runtime_case
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=case.users)
    args = arguments(case)
    if runtime_limit is not None:
        from bowhead_core.agent_run_limits import AgentRunLimits, AgentRuntimeControls

        args["parent_ctx"].agent_runtime_budget = AgentRuntimeControls(system_step_limit=runtime_limit).budget(AgentRunLimits())
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return workspace_question(request)
        messages = body["messages"]
        assert len([message for message in messages if message.get("content") == args["task"]]) == 1
        original_call = next(message for message in messages if message.get("tool_calls"))
        assert original_call["tool_calls"][0]["id"] == "select-context"
        assert json.loads(original_call["tool_calls"][0]["function"]["arguments"]) == {"query": "revenue"}
        tool_results = [message for message in messages if message["role"] == "tool"]
        assert len(tool_results) == 1 and tool_results[0]["tool_call_id"] == "select-context"
        assert "project_a" in tool_results[0]["content"] and "project_b" in tool_results[0]["content"]
        assert messages[-1]["role"] == "user" and messages[-1]["content"] == "请使用 B 工作区。"
        usage = {"prompt_tokens": 13, "completion_tokens": 3}
        if body.get("stream"):
            events = [
                {"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            content = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
            return httpx2.Response(200, text=content, headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}], "usage": usage})

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            first = await runner.run(**args)
            continuation = first.bi_workspace_continuation
            assert continuation is not None, first.error
            assert first.tokens_input == 100 and first.tokens_output == 20
            assert first.final_answer == "" and len(first.tool_invocations) == 1
            # Round-trip private material as a future encrypted store will;
            # mutating the old result after capture cannot rewrite this snapshot.
            restored = BiWorkspaceContinuation.model_validate_json(continuation.model_dump_json())
            if runtime_limit is not None:
                assert restored.loop_budget.model_steps_used == 1
                assert restored.loop_budget.step_limit == runtime_limit
                args["parent_ctx"].agent_runtime_budget = AgentRuntimeControls(system_step_limit=100).budget(AgentRunLimits())
            first.tool_invocations[0].result["clarification"]["options"] = []
            args["parent_ctx"] = copy.copy(args["parent_ctx"])
            args["parent_ctx"].active_datarc_project = "project_b"
            args["parent_bus"] = TurnEventBus()
            resumed = await runner.run(**args, bi_workspace_continuation=restored, clarification_answer="请使用 B 工作区。")
            if runtime_limit == 1:
                assert resumed.completion == "partial" and resumed.stop_reason == "step_limit"
                assert resumed.step_usage == {"used": 1, "limit": 1}
                assert route.call_count == 1, "A resumed last-step question cannot replenish the saved budget"
                return
            assert resumed.ok and resumed.final_answer == "OK"
            assert resumed.subagent_id == first.subagent_id and resumed.profile == first.profile
            assert resumed.tool_invocations == [] and resumed.bi_workspace_continuation is None
            assert resumed.tokens_input == 13 and resumed.tokens_output == 3
            assert next(event for event in args["parent_bus"].snapshot() if event.event_type == "peer.iter").payload["iter"] == 1
            assert route.call_count == 2
    finally:
        await adapter.close()


@pytest.mark.parametrize("change", ["task", "subagent", "user", "tools", "history", "answer", "budget", "policy", "attachment"])
async def test_native_resume_rejects_changed_receiver_before_http(runtime_case, change):
    from bowhead_router.types import Message

    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=runtime_case.users)
    args = arguments(runtime_case)
    if change == "attachment":
        args["attachment_context"] = "Original file data"
    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
            first = await runner.run(**args)
            assert first.bi_workspace_continuation is not None
            args["bi_workspace_continuation"] = first.bi_workspace_continuation
            args["clarification_answer"] = "Use B"
            args["parent_bus"] = TurnEventBus()
            if change == "task":
                args["task"] += "new task"
            elif change == "subagent":
                args["pre_minted_subagent_id"] = "sub_foreign"
            elif change == "user":
                args["parent_ctx"].user_id = "u_foreign"
            elif change == "tools":
                args["tool_deny"] |= {"bi_query"}
            elif change == "history":
                args["history"] = [Message(role="user", content="Lead replanned the task")]
            elif change == "attachment":
                args["attachment_context"] = "Replacement file data"
            elif change == "answer":
                args["clarification_answer"] = " "
            else:
                snapshot = first.bi_workspace_continuation
                update = (
                    {"max_iterations": 1 if snapshot.max_iterations is None else snapshot.max_iterations + 1}
                    if change == "budget"
                    else {"policy_fingerprint": "sha256:" + "0" * 64}
                )
                args["bi_workspace_continuation"] = snapshot.model_copy(update=update)
            with pytest.raises(ValueError):
                await runner.run(**args)
            assert route.call_count == 1
    finally:
        await adapter.close()


async def test_no_checkpoint_after_multiple_initial_bi_calls(runtime_case):
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=runtime_case.users)

    def respond(request):
        body = workspace_question(request).json()
        calls = body["choices"][0]["message"]["tool_calls"]
        second = copy.deepcopy(calls[0])
        second["id"] = "second-context"
        second["function"]["arguments"] = '{"query":"profit"}'
        calls.append(second)
        return httpx2.Response(200, json=body)

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await runner.run(**arguments(runtime_case))
            assert len(result.tool_invocations) == 2 and result.bi_workspace_continuation is None
            assert route.call_count == 1
    finally:
        await adapter.close()


async def test_second_workspace_question_does_not_reset_the_pause_budget(runtime_case):
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=runtime_case.users)
    args = arguments(runtime_case)
    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=question_response)
            first = await runner.run(**args)
            assert first.bi_workspace_continuation is not None
            # A skipped/invalidated selection can still reach the real gate.
            # The native adapter must not mint another "first" checkpoint.
            args["parent_bus"] = TurnEventBus()
            second = await runner.run(**args, bi_workspace_continuation=first.bi_workspace_continuation, clarification_answer="暂不选择")
            assert len(second.tool_invocations) == 1
            assert second.tool_invocations[0].result["error_kind"] == "needs_clarification"
            assert second.bi_workspace_continuation is None and route.call_count == 2
    finally:
        await adapter.close()


@pytest.mark.parametrize("extra", [{"capture_bi_workspace_continuation": False}, {"extra_instructions": "Keep this extra context"}])
async def test_capture_declines_when_conversation_contract_is_not_supported(runtime_case, extra):
    adapter = adapter_for("openai")
    runner = SubagentRunner(router=router_for(adapter), tool_registry=build_default_registry(), team_subagent_store=runtime_case.users)
    args = arguments(runtime_case) | extra
    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=workspace_question)
            result = await runner.run(**args)
            assert result.bi_workspace_continuation is None
            assert len(result.tool_invocations) == 1 and route.call_count == 1
    finally:
        await adapter.close()
