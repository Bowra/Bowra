"""Each transformation of model-visible content leaves one change record for the next send."""

from __future__ import annotations

import pytest
from bowhead_core.context_assembly import guard_window
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import CHAT_DIRECT_DEMOTED_DIRECTIVE, SubagentRunner, demote_chat_direct
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSchema, ToolSpec, build_default_registry
from bowhead_router.errors import LLMTargetFailure, ProviderFailureKind, StreamingTargetsExhaustedError
from bowhead_router.request_record import _PENDING_CHANGES, request_change_scope
from bowhead_router.types import Message, ToolCall

from bowhead_core import context_assembly as ca
from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, TokenUsage

pytestmark = pytest.mark.unit


def _pending() -> list:
    pending = _PENDING_CHANGES.get()
    assert pending is not None, "no request_change_scope is active"
    return pending


class _SendRouter(Router):
    """Takes the run's pending changes at each send, as `record_request` does."""

    def __init__(self, reg: ProviderRegistry, *, overflow_first: bool = False) -> None:
        super().__init__(reg)
        self.sends: list[list] = []
        self.overflow_first = overflow_first

    def _take(self) -> None:
        pending = _pending()
        self.sends.append(list(pending))
        pending.clear()
        if self.overflow_first and len(self.sends) == 1:
            raise StreamingTargetsExhaustedError(
                [
                    LLMTargetFailure(
                        provider="p1",
                        model="mock",
                        provider_family="mock",
                        mode="stream",
                        target_index=1,
                        attempt=1,
                        kind=ProviderFailureKind.CONTEXT_OVERFLOW,
                        retryable=False,
                        status_code=400,
                        upstream_message="maximum context length is 1000 tokens. However, you requested 1200 tokens",
                    )
                ]
            )

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self._take()
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self._take()
        async for chunk in super().stream(agent_type, req, **kw):
            yield chunk


def _router(**kw) -> _SendRouter:
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="answer", usage=TokenUsage(input_tokens=10, output_tokens=5))] * 3
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _SendRouter(reg, **kw)
    for agent_type in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock")))
    return router


def _ctx() -> ToolExecutionContext:
    return ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]


async def _run(router: _SendRouter, **kw) -> None:
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    await runner.run(profile_name="compute", task="TASK", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=_ctx(), **kw)


def _kinds(changes) -> list[str]:
    return [change.kind for change in changes]


async def test_dropped_history_turns_reach_the_next_send():
    router = _router()
    router.set_context_windows({("p1", "mock"): 64_000})
    big = "x " * 12_000
    history = [m for i in range(3) for m in (Message(role="user", content=f"H{i} " + big), Message(role="assistant", content=f"A{i} " + big))]

    await _run(router, history=history)

    assert _kinds(router.sends[0]) == ["history_turns_dropped"]
    detail = router.sends[0][0].detail
    assert (detail["turns"], detail["messages"], detail["window"]) == (2, 4, 64_000)


async def test_cleared_tool_results_name_their_tool_calls(monkeypatch):
    def clear(msgs, *, cleared_ids=None, **_kw):
        if cleared_ids is not None:
            cleared_ids.append("call_7")
        return 500

    monkeypatch.setattr(ca, "guard_window", clear)
    router = _router()

    await _run(router)

    assert _kinds(router.sends[0]) == ["tool_result_cleared"]
    assert router.sends[0][0].detail["tool_call_ids"] == ["call_7"]


async def test_overflow_shrink_is_one_record_on_the_retry(monkeypatch):
    shrinking = {"on": False}

    def clear(msgs, *, cleared_ids=None, **_kw):
        if not shrinking["on"]:
            return 0
        cleared_ids.append("call_3")
        shrinking["on"] = False
        return 900

    monkeypatch.setattr(ca, "guard_window", clear)
    router = _router(overflow_first=True)
    original = router._take

    def take_then_arm() -> None:
        try:
            original()
        finally:
            shrinking["on"] = len(router.sends) == 1

    router._take = take_then_arm  # type: ignore[method-assign]

    await _run(router)

    assert len(router.sends) == 2
    assert _kinds(router.sends[1]) == ["overflow_shrink"]
    detail = router.sends[1][0].detail
    assert (detail["upstream_maximum"], detail["upstream_requested"], detail["cleared_tool_call_ids"]) == (1000, 1200, ["call_3"])


async def test_unchanged_sends_carry_no_change_records():
    router = _router()
    await _run(router)
    assert router.sends == [[]]


def test_guard_window_reports_the_cleared_tool_call_ids():
    messages = [
        Message(role="user", content="q"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="t", arguments={})]),
        Message(role="tool", content="r" * 400, name="t", tool_call_id="c1"),
        Message(role="assistant", content="next"),
    ]
    cleared_ids: list[str] = []
    assert guard_window(messages, budget_tokens=100, estimate=len, cleared_ids=cleared_ids) > 0
    assert cleared_ids == ["c1"]


def test_chat_direct_demotion_records_the_rewritten_tool_calls():
    class _Inv:
        tool_name = "subagent_spawn"
        result = {"final_answer": "peer", "_chat_direct_delivery": {"text": "peer"}}

    inv = _Inv()
    serialize = lambda i: str(i.result)  # noqa: E731
    messages = [Message(role="tool", content=serialize(inv), name="subagent_spawn", tool_call_id="spawn_1")]

    with request_change_scope():
        assert demote_chat_direct([inv], messages, serialize) == 1
        changes = list(_pending())

    assert inv.result["directive"] == CHAT_DIRECT_DEMOTED_DIRECTIVE
    assert _kinds(changes) == ["chat_direct_demoted"]
    assert changes[0].detail == {"tool_call_ids": ["spawn_1"]}


async def _execute(name: str, result: dict) -> list:
    async def handler(_args, _ctx):
        return result

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name=name, description="", input_schema={}), handler=handler))
    runner = SubagentRunner(router=_router(), tool_registry=registry)  # type: ignore[arg-type]
    ctx = _ctx()
    ctx.role = "lead"
    with request_change_scope():
        await runner._execute_tool(ToolCall(id="call_kb", name=name, arguments={}), ctx, TurnEventBus(), "t", "lead_root", running_profile="lead")
        return list(_pending())


async def test_a_capped_tool_result_records_sizes_and_hashes_not_content():
    changes = await _execute("kb_search", {"hits": [{"text": f"secret {i}"} for i in range(8)]})

    assert _kinds(changes) == ["tool_result_capped"]
    detail = changes[0].detail
    assert detail["tool_call_id"] == "call_kb" and detail["tool_name"] == "kb_search"
    assert detail["full_bytes"] > detail["capped_bytes"] and detail["full_sha256"] != detail["capped_sha256"]
    assert "secret" not in repr(detail)


async def test_a_cap_that_changes_nothing_records_nothing():
    assert await _execute("kb_search", {"hits": [{"text": "one"}]}) == []


async def test_parallel_tool_calls_share_the_run_list():
    import asyncio

    async def handler(_args, _ctx):
        return {"hits": [{"text": str(i)} for i in range(8)]}

    registry = ToolRegistry()
    registry.register(ToolSpec(schema=ToolSchema(name="kb_search", description="", input_schema={}), handler=handler))
    runner = SubagentRunner(router=_router(), tool_registry=registry)  # type: ignore[arg-type]
    ctx = _ctx()
    ctx.role = "lead"
    with request_change_scope():
        await asyncio.gather(
            *(
                asyncio.create_task(
                    runner._execute_tool(
                        ToolCall(id=f"call_{i}", name="kb_search", arguments={}), ctx, TurnEventBus(), "t", "lead_root", running_profile="lead"
                    )
                )
                for i in range(3)
            )
        )
        changes = list(_pending())

    assert sorted(change.detail["tool_call_id"] for change in changes) == ["call_0", "call_1", "call_2"]
