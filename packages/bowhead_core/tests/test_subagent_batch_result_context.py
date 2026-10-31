"""Native result transport across the Datarc batch and per-call context copies."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from bowhead_core.datarc_selection import accepted_datarc_selection
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentResult, SubagentRunner
from bowhead_core.subagent_return_tool import CTX_PAYLOAD_ATTR, consume_returned_payload, register_subagent_return_tool
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_NONE
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from test_datarc_selection import context, router

from bowhead_router import LLMResponse, ToolCall, ToolSchema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PROFILE = "batch-result-test"


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    registry = ToolRegistry()
    register_subagent_return_tool(registry)

    async def probe(_args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, bool]:
        assert accepted_datarc_selection(ctx).datarc_workspace_id == "1"
        ctx._psi_compose_attempted = True
        ctx._batch_only_value = True
        ctx.active_datarc_workspace_name = "Handler-local value"
        return {"ok": True}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="result_probe", description="Observe a scoped call.", input_schema={"type": "object"}),
            handler=probe,
            effect_spec=TOOL_EFFECT_NONE,
        )
    )
    monkeypatch.setitem(
        PROFILES,
        PROFILE,
        SubagentProfile(
            name=PROFILE,
            llm_agent_type="analyst",
            tool_whitelist=("result_probe", "subagent_return"),
            system_prompt="Return the requested result.",
            max_iterations=3,
        ),
    )
    return registry


def response(summary: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id="probe", name="result_probe", arguments={}),
            ToolCall(id="return", name="subagent_return", arguments={"payload": {"delivery_surface": "chat", "exec_summary": summary}}),
        ],
    )


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
@pytest.mark.parametrize("role", [None, "lead"], ids=["no-role-swap", "role-swap"])
async def test_native_payload_and_compose_marker_cross_batch_without_scope_leaks(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch, parallel: bool, role: str | None
) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1" if parallel else "0")
    ctx = context()
    ctx.role = role
    observed = []

    def observe_consume(scoped: ToolExecutionContext) -> dict[str, Any] | None:
        observed.append(
            (
                getattr(scoped, "_psi_compose_attempted", False),
                hasattr(scoped, "_batch_only_value"),
                scoped.active_datarc_workspace_name,
                accepted_datarc_selection(scoped).datarc_workspace_id,
                scoped.role,
            )
        )
        payload = consume_returned_payload(scoped)
        assert consume_returned_payload(scoped) is None
        return payload

    monkeypatch.setattr("bowhead_core.subagent_return_tool.consume_returned_payload", observe_consume)
    runner = SubagentRunner(router=router([response("Accepted result.")]), tool_registry=registry)
    result = await runner.run(
        profile_name=PROFILE, task="Return a short answer.", parent_turn_id=ctx.turn_id, parent_ctx=ctx, parent_bus=TurnEventBus()
    )
    assert result.ok and result.payload is not None
    assert result.payload["exec_summary"] == "Accepted result."
    assert observed == [(True, False, "Source A", "1", role)]
    assert not hasattr(ctx, CTX_PAYLOAD_ATTR)
    assert not hasattr(ctx, "_psi_compose_attempted")
    assert ctx.active_datarc_workspace_name == "Source A"
    returned = [inv for inv in result.tool_invocations if inv.tool_name == "subagent_return"]
    assert len(returned) == 1 and returned[0].result["__terminal"] is True


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_first_batch_keeps_profile_output_schema(registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch, parallel: bool) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1" if parallel else "0")
    monkeypatch.setitem(
        PROFILES,
        PROFILE,
        replace(PROFILES[PROFILE], output_schema={"type": "object", "properties": {"exec_summary": {"const": "Approved result."}}}),
    )
    ctx = context()
    runner = SubagentRunner(router=router([response("Rejected result."), response("Approved result.")]), tool_registry=registry)
    result = await runner.run(
        profile_name=PROFILE, task="Return the approved result.", parent_turn_id=ctx.turn_id, parent_ctx=ctx, parent_bus=TurnEventBus()
    )
    returned = [inv for inv in result.tool_invocations if inv.tool_name == "subagent_return"]
    assert len(returned) == 2
    assert returned[0].result["ok"] is False and returned[0].result["_schema_retry"] is True
    assert returned[1].result["ok"] is True
    assert result.ok and result.payload is not None
    assert result.payload["exec_summary"] == "Approved result."


async def test_sibling_helpers_keep_their_own_return_channels(registry: ToolRegistry) -> None:
    ctx = context()

    async def run(summary: str) -> SubagentResult:
        runner = SubagentRunner(router=router([response(summary)]), tool_registry=registry)
        return await runner.run(profile_name=PROFILE, task=summary, parent_turn_id=ctx.turn_id, parent_ctx=ctx, parent_bus=TurnEventBus())

    results = await asyncio.gather(run("First result."), run("Second result."))
    assert all(result.ok for result in results)
    assert [result.payload["exec_summary"] for result in results] == ["First result.", "Second result."]
    assert consume_returned_payload(ctx) is None
    assert not hasattr(ctx, "_psi_compose_attempted")
    assert not hasattr(ctx, "_batch_only_value")


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_lead_spawn_handlers_receive_finalized_batch_policy(registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch, parallel: bool) -> None:
    monkeypatch.setenv("BOWHEAD_CHI_PARALLEL_EXEC", "1" if parallel else "0")
    observed = []

    async def spawn(_args: dict[str, Any], ctx: ToolExecutionContext) -> dict[str, bool]:
        observed.append((ctx._chat_direct_spawn_count, ctx._lead_spawn_batch_seq))
        return {"ok": True}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="subagent_spawn", description="Observe the dispatch boundary.", input_schema={"type": "object"}),
            handler=spawn,
            effect_spec=TOOL_EFFECT_NONE,
        )
    )
    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead", llm_agent_type="lead", tool_whitelist=("subagent_spawn",), system_prompt="Complete both tasks.", max_iterations=3
        ),
    )
    ctx = context()
    runner = SubagentRunner(
        router=router(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="first", name="subagent_spawn", arguments={"profile": PROFILE, "task": "First task."}),
                        ToolCall(id="second", name="subagent_spawn", arguments={"profile": PROFILE, "task": "Second task."}),
                    ],
                ),
                LLMResponse(content="Both tasks completed."),
            ]
        ),
        tool_registry=registry,
    )
    result = await runner.run(profile_name="lead", task="Complete both tasks.", parent_turn_id=ctx.turn_id, parent_ctx=ctx, parent_bus=TurnEventBus())
    assert result.ok and result.final_answer == "Both tasks completed."
    assert observed == [(2, 1), (2, 1)]
