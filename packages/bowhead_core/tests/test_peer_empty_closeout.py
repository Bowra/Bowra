"""A peer with verified tool results gets one bounded chance to commit an empty response."""

from __future__ import annotations

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner
from bowhead_core.subagent_return_tool import register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import LLMChunk, LLMRequest, LLMResponse, TokenUsage, ToolCall, ToolSchema


class ScriptedRouter:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.requests: list[LLMRequest] = []

    async def invoke(self, _agent_type: str, request: LLMRequest, **_kwargs: object) -> LLMResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def stream(self, _agent_type: str, request: LLMRequest, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        response = self.responses.pop(0)
        if response.content:
            yield LLMChunk(delta=response.content)
        for call in response.tool_calls:
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason=response.finish_reason, usage=response.usage)


@pytest.mark.unit
async def test_empty_peer_response_preserves_verified_result_and_limits_closeout_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = SubagentProfile(
        name="empty-closeout-test",
        llm_agent_type="analyst",
        tool_whitelist=("verified_read", "subagent_return"),
        system_prompt="Read data and return verified findings.",
        max_iterations=4,
        max_output_tokens=8000,
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    registry = ToolRegistry()
    read_count = 0

    async def verified_read(_arguments: dict[str, object], _ctx: ToolExecutionContext) -> dict[str, object]:
        nonlocal read_count
        read_count += 1
        return {"ok": True, "value": 42}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="verified_read", description="Read verified data", input_schema={"type": "object"}),
            handler=verified_read,
        )
    )
    register_subagent_return_tool(registry)
    router = ScriptedRouter(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="read", name="verified_read", arguments={})]),
            LLMResponse(content="", finish_reason="stop", usage=TokenUsage(output_tokens=8000)),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="read_again", name="verified_read", arguments={}),
                    ToolCall(
                        id="return",
                        name="subagent_return",
                        arguments={"payload": {"delivery_surface": "chat", "exec_summary": "The verified value is 42."}},
                    ),
                ],
                finish_reason="tool_use",
            ),
        ]
    )
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=profile.name,
        task="Read the value",
        parent_turn_id="t_empty_closeout",
        parent_bus=TurnEventBus(),
        parent_ctx=ToolExecutionContext(
            workspace_id="ws_test",
            member_uid="m_test",
            user_id="u_test",
            turn_id="t_empty_closeout",
            session_id="s_test",
            datarc=None,  # type: ignore[arg-type]
        ),
    )

    assert result.ok
    assert result.payload is not None
    assert result.payload["exec_summary"] == "The verified value is 42."
    assert [call.tool_name for call in result.tool_invocations] == ["verified_read", "subagent_return"]
    assert read_count == 1
    assert [schema.name for schema in router.requests[-1].tools] == ["subagent_return"]
    assert any("verified findings" in message.content for message in router.requests[-1].messages if message.role == "user")


@pytest.mark.unit
@pytest.mark.parametrize("closeout", ["text", "empty", "write", "exhausted"])
async def test_coach_empty_closeout_is_bounded_and_never_repeats_saved_effect(monkeypatch: pytest.MonkeyPatch, closeout: str) -> None:
    monkeypatch.setitem(
        PROFILES,
        "agent-coach",
        SubagentProfile(
            name="agent-coach",
            llm_agent_type="analyst",
            tool_whitelist=("save_once",),
            system_prompt="Save the requested personal Skill and report its original receipt.",
            max_iterations=1 if closeout == "exhausted" else 4,
        ),
    )
    registry = ToolRegistry()
    saves = []

    async def save_once(_args: dict, _ctx: ToolExecutionContext) -> dict:
        saves.append("operation-original")
        return {"ok": True, "state": "succeeded", "operation_id": "operation-original", "effect": "personal_skill_saved"}

    registry.register(ToolSpec(schema=ToolSchema(name="save_once", description="Save once", input_schema={"type": "object"}), handler=save_once))
    router = ScriptedRouter(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="save", name="save_once", arguments={})]),
            LLMResponse(content="", finish_reason="stop", usage=TokenUsage(output_tokens=4000)),
            LLMResponse(
                content="Saved: operation-original; triggering is unverified." if closeout == "text" else "",
                tool_calls=[ToolCall(id="repeat", name="save_once", arguments={})] if closeout == "write" else [],
            ),
        ]
    )
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name="agent-coach",
        task="Create a personal Skill",
        parent_turn_id="t_coach_closeout",
        parent_bus=TurnEventBus(),
        parent_ctx=ToolExecutionContext(
            workspace_id="ws_test", member_uid="m_test", user_id="u_test", turn_id="t_coach_closeout", session_id="s_test", datarc=None
        ),
    )
    assert saves == ["operation-original"]
    assert len(router.requests) == (2 if closeout == "exhausted" else 3)
    if closeout == "exhausted":
        assert not result.final_answer
        return
    assert router.requests[-1].tools == []
    assert any("operation-original" in message.content for message in router.requests[-1].messages)
    assert any("original operation receipts" in message.content for message in router.requests[-1].messages)
    # The dispatch wrapper, not the runner, maps empty content to peer_empty_result.
    assert bool(result.final_answer) == (closeout == "text")
    if closeout == "text":
        assert "operation-original" in result.final_answer


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ["recovered", "dispatch", "empty", "length", "budget", "after_tool"])
async def test_lead_empty_answer_has_one_safe_retry_and_never_claims_success(monkeypatch, outcome):
    monkeypatch.setitem(
        PROFILES,
        "lead",
        SubagentProfile(
            name="lead",
            llm_agent_type="lead",
            system_prompt="Answer the request.",
            tool_whitelist=("read_once",),
            max_iterations=0 if outcome == "budget" else 4,
        ),
    )
    registry = ToolRegistry()
    reads = []

    async def read_once(_args, _ctx):
        reads.append(True)
        return {"ok": True, "value": 42}

    registry.register(ToolSpec(schema=ToolSchema(name="read_once", description="Read", input_schema={"type": "object"}), handler=read_once))
    responses = [LLMResponse(content="")]
    if outcome in {"after_tool", "dispatch"}:
        responses = [
            LLMResponse(content="", tool_calls=[ToolCall(id="read", name="read_once", arguments={})]),
            LLMResponse(content="The value is 42."),
        ]
        if outcome == "dispatch":
            responses.insert(0, LLMResponse(content=""))
    elif outcome != "budget":
        responses.append(
            LLMResponse(content="Recovered answer." if outcome == "recovered" else "", finish_reason="length" if outcome == "length" else "stop")
        )
    router = ScriptedRouter(responses)
    bus = TurnEventBus()
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name="lead",
        task="Answer the question",
        parent_turn_id="t_lead_empty",
        parent_bus=bus,
        parent_ctx=ToolExecutionContext(
            workspace_id="ws_test", user_id="u_test", member_uid="m_test", turn_id="t_lead_empty", session_id="s_test", datarc=None
        ),
    )
    assert len(router.requests) == (1 if outcome == "budget" else 3 if outcome == "dispatch" else 2)
    assert reads == ([True] if outcome in {"after_tool", "dispatch"} else [])
    assert result.ok is (outcome in {"recovered", "after_tool", "dispatch"})
    if not result.ok:
        assert result.error == ("lead_output_exhausted" if outcome == "length" else "lead_empty_answer")
        assert not result.final_answer
    timings = [e.payload for e in bus.snapshot() if e.event_type == "llm.call_timing"]
    assert sum(t.get("empty_answer_retry", False) for t in timings) == (0 if outcome in {"budget", "after_tool"} else 1)


@pytest.mark.unit
async def test_lead_empty_answer_recovery_preserves_cancellation(monkeypatch):
    import asyncio

    monkeypatch.setitem(
        PROFILES, "lead", SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=(), system_prompt="Answer.", max_iterations=4)
    )

    class CancelledRouter(ScriptedRouter):
        async def invoke(self, *args, **kwargs):
            if self.requests:
                raise asyncio.CancelledError
            return await super().invoke(*args, **kwargs)

        async def stream(self, *args, **kwargs):
            if self.requests:
                raise asyncio.CancelledError
            async for chunk in super().stream(*args, **kwargs):
                yield chunk

    router = CancelledRouter([LLMResponse(content="")])
    with pytest.raises(asyncio.CancelledError):
        await SubagentRunner(router=router, tool_registry=ToolRegistry()).run(
            profile_name="lead",
            task="Answer",
            parent_turn_id="t_cancel_empty",
            parent_bus=TurnEventBus(),
            parent_ctx=ToolExecutionContext(
                workspace_id="ws_test", user_id="u_test", member_uid="m_test", turn_id="t_cancel_empty", session_id="s_test", datarc=None
            ),
        )
    assert len(router.requests) == 1
