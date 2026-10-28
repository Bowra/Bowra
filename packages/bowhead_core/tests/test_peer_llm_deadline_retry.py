"""peer 的一次 LLM 调用撞上我们自己的单次时限：只重发这一次，别把整个 run 判死。

116 dogfood 2026-09-25 `t_239e8b6cf3dc`：compute 跑完 6 圈、落了 10 个文件，第 7 次调用吐着内容
被 300s 时限掐掉 ⇒ 报 `subagent_internal_error`、Lead 收到「别重试、告诉用户 insufficient_data」，
Lead 没照做、从头重派一个 compute 重做了一遍（56 万输入 token）。
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from bowhead_core.agents.lead_orchestrator import _public_turn_failure_reason
from bowhead_core.events import TurnEventBus
from bowhead_core.published_diagnostics import normalize_chat_peer_failure_code
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMChunk,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
    ToolCall,
)


class _HangRouter(Router):
    """第 `hang_on` 次调用（1 起算）先吐一小段思考再卡住，其余照脚本走。"""

    def __init__(self, reg: ProviderRegistry, *, hang_on: set[int]) -> None:
        super().__init__(reg)
        self.calls = 0
        self.hang_on = hang_on

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self.calls += 1
        if self.calls in self.hang_on:
            await asyncio.sleep(30)
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self.calls += 1
        if self.calls in self.hang_on:
            yield LLMChunk(thinking_delta="想" * 7)
            await asyncio.sleep(30)
        async for chunk in super().stream(agent_type, req, **kw):
            yield chunk


async def _run(monkeypatch: pytest.MonkeyPatch, *, hang_on: set[int], first_tool_call: bool, wallclock: str = "900"):
    monkeypatch.setenv("BOWHEAD_SUBAGENT_LLM_TIMEOUT_SEC", "0.3")
    monkeypatch.setenv("BOWHEAD_PEER_WALLCLOCK_SEC", wallclock)
    adapter = MockAdapter(provider_name="p1")
    answer = LLMResponse(content="直答", usage=TokenUsage(input_tokens=10, output_tokens=5))
    # Call a nonexistent tool first: once messages contain a tool result, the next peer iteration streams (the production deadline-hit path).
    tool_turn = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="no_such_tool", arguments={})],
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    adapter.scripted_responses = ([tool_turn] if first_tool_call else []) + [answer] * 4
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _HangRouter(reg, hang_on=hang_on)
    for t in ("lead", "analyst", "compute", "narrative", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    result = await runner.run(profile_name="compute", task="时限闸", parent_turn_id="t", parent_bus=TurnEventBus(), parent_ctx=ctx)
    return router, result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streamed_call_that_hits_the_deadline_is_resent_once_and_the_run_succeeds(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="bowhead_core.subagent")
    router, result = await _run(monkeypatch, hang_on={2}, first_tool_call=True)

    assert result.ok, result.error
    assert router.calls == 3, "工具圈 1 次 + 撞时限 1 次 + 重发 1 次"
    assert any("subagent.llm_deadline_retry" in r.getMessage() for r in caplog.records)
    # Log what was emitted when the deadline hit; stream_visible=True alone could not explain those 300s.
    diag = [r.getMessage() for r in caplog.records if "llm.stream_deadline_exceeded" in r.getMessage()]
    assert diag and "thinking_chars=7" in diag[0] and "chunks=1" in diag[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_streamed_first_call_deadline_is_resent_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """peer 第一圈走非流式，时限是外层 wait_for 直接抛的 TimeoutError（没有类型化投影）。"""
    router, result = await _run(monkeypatch, hang_on={1}, first_tool_call=False)

    assert result.ok, result.error
    assert router.calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_deadline_fails_as_a_timeout_not_an_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    router, result = await _run(monkeypatch, hang_on={2, 3}, first_tool_call=True)

    assert router.calls == 3, "只重发一次"
    assert not result.ok
    assert result.error == "peer_llm_deadline_exceeded"
    # This code must map to user-facing execution timeout, not internal error.
    assert normalize_chat_peer_failure_code(result.error) == "peer_execution_timeout"
    assert _public_turn_failure_reason(result.error) == "peer_dispatch_timeout"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_resend_when_the_wallclock_budget_cannot_fit_another_full_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    router, result = await _run(monkeypatch, hang_on={2}, first_tool_call=True, wallclock="0.5")

    assert router.calls == 2
    assert not result.ok
    assert result.error == "peer_llm_deadline_exceeded"


@pytest.mark.unit
def test_other_peer_errors_still_map_to_internal() -> None:
    """负对照：只有时限那一路改了口径。"""
    assert normalize_chat_peer_failure_code("subagent_internal_error") == "peer_internal_error"
