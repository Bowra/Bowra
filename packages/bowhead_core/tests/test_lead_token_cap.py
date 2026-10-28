"""W1-B — Lead per-turn token 硬顶(安全兜底)合成终止测试.

覆盖 tech-design-wave1.md r2 W1-B 验收:
- cap>0 且 tokens 超限 → 合成终止(return, 非抛异常): ok=False + error 前缀
  token_cap_exhausted(经 lead_orchestrator not_ok 路 → turn_failed{reason} 可区分,
  绝非 lead_internal_error) + metric 触发; no system answer prose (chat turn feedback D3:
  the failure card states token_cap_exhausted from its public reason).
- cap=0(默认) → 永不触发, lead 正常完成.

用 scripted MockAdapter 驱动真 lead loop(与 test_lead_sentinel_break 同 harness).
token 计数点在 iter 顶(tokens_in/out 反映上一 iter), 故需 iter0 消耗 tokens 且带
tool_calls 续跑, iter1 顶触发.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.metrics import get_registry
from bowhead_core.subagent import SubagentRunner, ToolInvocation
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.types import TokenUsage

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    ToolCall,
)


def _router(*rs: LLMResponse) -> Router:
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = list(rs)
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "compute", "modeler", "analyst", "echo", "judge", "verifier", "web_search"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


@dataclass
class _StubDatarc:
    async def aclose(self) -> None:
        pass


def _ctx(turn_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id=turn_id,
        datarc=_StubDatarc(),  # type: ignore[arg-type]
    )


async def _benign_exec(tc: ToolCall, *args: Any, **kwargs: Any) -> ToolInvocation:
    """任何 tool → 良性非终态结果(让 lead loop 续跑到下一 iter)."""
    return ToolInvocation(
        tool_name=tc.name,
        arguments=tc.arguments,
        result={"ok": True},
        error=None,
    )


@pytest.mark.asyncio
async def test_lead_token_cap_synthetic_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_TURN_TOKEN_HARD_CAP", "100")
    get_registry().reset()
    # iter0: tool_calls (no break at :1959) + usage 200 tokens → accumulate tokens.
    # iter1 start: 200 ≥ 100 → synthetic termination before iter1's LLM call.
    router = _router(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="subagent_spawn", arguments={})],
            usage=TokenUsage(input_tokens=150, output_tokens=50),
        ),
        LLMResponse(content="should not reach (cap trips at iter1 top)", tool_calls=[]),
    )
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        backend="legacy",
    )
    runner._execute_tool = _benign_exec  # type: ignore[method-assign]
    result = await runner.run(
        profile_name="lead",
        task="分析",
        parent_turn_id="t_tokencap",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx("t_tokencap"),
    )
    assert result.ok is False
    assert result.error and result.error.startswith("token_cap_exhausted")
    # Crucial: distinct error, never lead_internal_error (see tech-design P1-1).
    assert "lead_internal_error" not in (result.error or "")
    assert result.final_answer == ""
    c = get_registry().counter("turn_token_hard_cap_triggered_total")
    assert any(("profile", "lead") in k for k in c._values)


@pytest.mark.asyncio
async def test_lead_token_cap_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_TURN_TOKEN_HARD_CAP", raising=False)
    # cap=0 (default): never triggers regardless of usage (guard `_token_hard_cap > 0`).
    router = _router(
        LLMResponse(
            content="最终答案",
            tool_calls=[],
            usage=TokenUsage(input_tokens=9999, output_tokens=9999),
        ),
    )
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        backend="legacy",
    )
    result = await runner.run(
        profile_name="lead",
        task="分析",
        parent_turn_id="t_tokencap2",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx("t_tokencap2"),
    )
    assert not (result.error or "").startswith("token_cap_exhausted")


@pytest.mark.asyncio
async def test_lead_token_cap_inf_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env=inf(env_float 放行 inf)不得让 int(inf) 抛 OverflowError 崩 turn —— 视作 off."""
    monkeypatch.setenv("BOWHEAD_TURN_TOKEN_HARD_CAP", "inf")
    router = _router(
        LLMResponse(
            content="最终答案",
            tool_calls=[],
            usage=TokenUsage(input_tokens=9999, output_tokens=9999),
        ),
    )
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        backend="legacy",
    )
    result = await runner.run(
        profile_name="lead",
        task="分析",
        parent_turn_id="t_inf",
        parent_bus=TurnEventBus(),
        parent_ctx=_ctx("t_inf"),
    )
    # No crash (OverflowError/cap trigger); bounded validation treats inf as off
    assert not (result.error or "").startswith("token_cap_exhausted")
    assert "OverflowError" not in (result.error or "")
