"""确定性兜底穿真 loop：一轮批量派 ≥2 个不同 peer → 一个活都不执行，直接出哨兵。

harness 照 `test_lead_sentinel_break.py`（scripted MockAdapter → 真
`SubagentRunner.run`）—— 纯函数单测（test_multi_peer_gate.py）证明不了
「hot path 真的会中止这一轮」，那 8 行必须自己被穿过。

★ 最要紧的断言是**没有任何 tool 被执行**：兜底的全部价值就在「此刻一个活都还没跑」，
拦晚了就变成「跑了一半再弹卡」，那是工具描述里明确不要的形态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from bowhead_core.agents.plan_propose_tool import multi_peer_sentinel
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner, ToolInvocation
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

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
    for t in ("lead", "compute", "modeler", "analyst"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


@dataclass
class _StubDatarc:
    async def aclose(self) -> None:
        pass


def _ctx_with_gate(*, gate: Any) -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_gate_test",
        datarc=_StubDatarc(),  # type: ignore[arg-type]
    )
    if gate is not None:
        ctx._round_tool_calls_gate = gate  # type: ignore[attr-defined]
    return ctx


async def _lead_gate(tool_calls: list[Any], profile_name: str) -> dict[str, Any] | None:
    """与 lead_orchestrator 里装的那个闭包同形。"""
    if profile_name != "lead":
        return None
    return multi_peer_sentinel(tool_calls)


def _two_peer_round() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id="c1", name="subagent_spawn", arguments={"profile": "financial-analyst", "task": "财务"}),
            ToolCall(id="c2", name="subagent_spawn", arguments={"profile": "business-analyst", "task": "业务"}),
        ],
    )


async def _run(runner: SubagentRunner, ctx: ToolExecutionContext, profile: str = "lead") -> Any:
    return await runner.run(
        profile_name=profile,
        task="做一份尽调",
        parent_turn_id="t_gate_test",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )


def _runner_recording(executed: list[str]) -> SubagentRunner:
    runner = SubagentRunner(
        router=_router(_two_peer_round(), LLMResponse(content="不该走到这一轮", tool_calls=[])),
        tool_registry=build_default_registry(),
        backend="legacy",
    )

    async def _stub_exec(tc: ToolCall, *args: Any, **kwargs: Any) -> ToolInvocation:
        executed.append(tc.name)
        return ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result={"ok": True}, error=None)

    runner._execute_tool = _stub_exec  # type: ignore[method-assign]
    return runner


@pytest.mark.asyncio
async def test_gate_aborts_the_round_before_any_tool_runs() -> None:
    """★正身：两个不同 peer 的那一轮 —— 零执行 + 哨兵落进 invocations。"""
    executed: list[str] = []
    result = await _run(_runner_recording(executed), _ctx_with_gate(gate=_lead_gate))

    assert executed == [], f"闸的意义就是一个活都没跑，实际跑了: {executed}"
    sentinels = [i for i in result.tool_invocations if isinstance(i.result, dict) and i.result.get("_plan_propose_pending")]
    assert len(sentinels) == 1, f"应恰好合成一条哨兵，实得 {[i.tool_name for i in result.tool_invocations]}"
    assert sentinels[0].result["source"] == "multi_peer_gate"
    # Record blocked tool names, or later review cannot establish what was blocked.
    assert sorted(sentinels[0].arguments["blocked"]) == ["subagent_spawn", "subagent_spawn"]


@pytest.mark.asyncio
async def test_no_gate_installed_executes_as_before() -> None:
    """负对照：没装闸（planning / off / resume 轮）→ 逐字节走老路，两个 spawn 照常执行。

    突变：把 hot path 的闸改成无条件拦 → 本条红。
    """
    executed: list[str] = []
    await _run(_runner_recording(executed), _ctx_with_gate(gate=None))
    assert executed == ["subagent_spawn", "subagent_spawn"]


@pytest.mark.asyncio
async def test_gate_is_lead_only() -> None:
    """负对照：parent_ctx 是所有 profile 共用的，非 lead 不许被拦。

    ⚠ 这条**不能**拿「工具有没有执行」当判据：`subagent_spawn` 本来就不在 compute
    的白名单里，不管有没有闸都不会执行 —— 那样写会为错误的理由绿（第一版就是这么错的）。
    真正的可观测量是：①闸确实被问到了（拿到 profile_name，证明 hot path 把名字传下来了）
    ②非 lead 时**没有哨兵**落进 invocations。
    """
    seen: list[str] = []

    async def _recording_gate(tool_calls: list[Any], profile_name: str) -> dict[str, Any] | None:
        seen.append(profile_name)
        return await _lead_gate(tool_calls, profile_name)

    result = await _run(_runner_recording([]), _ctx_with_gate(gate=_recording_gate), profile="compute")
    assert seen and seen[0] == "compute", f"hot path 没把 profile 名传给闸: {seen}"
    sentinels = [i for i in result.tool_invocations if isinstance(i.result, dict) and i.result.get("_plan_propose_pending")]
    assert sentinels == [], "非 lead 的 profile 被拦了"


@pytest.mark.asyncio
async def test_gate_exception_does_not_break_the_turn() -> None:
    """闸自己抛错 → 降级成「没装闸」，绝不能弄挂一个 turn。"""

    async def _boom(tool_calls: list[Any], profile_name: str) -> dict[str, Any] | None:
        raise RuntimeError("gate blew up")

    executed: list[str] = []
    await _run(_runner_recording(executed), _ctx_with_gate(gate=_boom))
    assert executed == ["subagent_spawn", "subagent_spawn"]


@pytest.mark.asyncio
async def test_declined_gate_lets_the_round_run_no_empty_turn() -> None:
    """★★116/1.1.3 P0 的正身回归钉：闸命中条件、但出卡路径判「不是项目」→
    **这一轮的 tool_calls 必须照常执行**，不能把 turn 交空。

    第一版是「先中止再问」：命中就丢掉模型选好的 tool_calls，等 author_plan 回话
    说「不是项目」时已经无可回退 —— 真机上用户拿到的是
    「本次没有产出可展示的内容」（9.9 秒，无卡无答案）。
    不变量：**会终止 turn 的闸，判据必须在终止那一刻已决**。

    突变：把闸改成「条件命中就无条件返哨兵」→ executed 变空、本条红。
    """
    executed: list[str] = []

    async def _declining_gate(tool_calls: list[Any], profile_name: str) -> dict[str, Any] | None:
        # Production shape: criterion matches (two distinct peers), but no plan is produced by the card path → allow.
        assert multi_peer_sentinel(tool_calls) is not None, "前提：条件确实命中，否则测不到那条路"
        return None

    result = await _run(_runner_recording(executed), _ctx_with_gate(gate=_declining_gate))
    assert executed == ["subagent_spawn", "subagent_spawn"], f"放行后本轮必须照常跑，实际: {executed}"
    sentinels = [i for i in result.tool_invocations if isinstance(i.result, dict) and i.result.get("_plan_propose_pending")]
    assert sentinels == [], "没出计划就不该留哨兵"
