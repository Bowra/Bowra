"""turn-latency spike 2026-08-21 —— Lead 也发逐圈遥测。

📏 起因：整轮 72.9 秒里 peer 占 44 秒，**剩下约 29 秒在 Lead 里而完全不可观测**
（116 `t_94f886fbf442`）。原代码用 `profile.name != "lead"` 把 Lead 排除，
理由写的是「its own metrics live on Lead path」——**那句话今天是假的**：
`summary.llm_count` 硬编码 1，其 TODO 引用的 `lead_llm_call_count` 全仓不存在，
Lead 路径也没有任何逐圈/耗时指标。

⚠ Lead 与 peer 跑同一个循环，所以这是「停止排除」而不是新建埋点；
`payload.profile` 本来就区分得开，因此复用 `peer.iter` 而不另起事件名
（另起 = 每次分析都要 union 两个名字，将来必有人只查一个）。
"""

from __future__ import annotations

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)


def _router() -> Router:
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = [
        LLMResponse(
            content="直答",
            usage=TokenUsage(input_tokens=100, output_tokens=50, cached_tokens=30),
        )
    ]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "analyst", "echo"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


async def _run(profile_name: str) -> list:
    runner = SubagentRunner(router=_router(), tool_registry=build_default_registry(), backend="legacy")
    bus = TurnEventBus()
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)  # type: ignore[arg-type]
    await runner.run(
        profile_name=profile_name,
        task="遥测契约测试",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    return [e for e in bus.snapshot() if e.event_type == "peer.iter"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lead_emits_iter_telemetry() -> None:
    """Lead 必须发逐圈遥测——这是本次改动的**唯一目的**。

    突变：把 `profile.name != "lead" and` 加回去 ⇒ 本条红。
    """
    iters = await _run("lead")
    assert iters, "Lead 没发 peer.iter —— 那 29 秒仍然不可观测"
    assert iters[0].payload["profile"] == "lead", "载荷必须自报是谁，否则两条流混在一起分不开"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lead_iter_payload_has_the_same_shape_as_peer() -> None:
    """同一个事件名 ⇒ 载荷形状必须一致，否则分析端要写两套解析。"""
    iters = await _run("lead")
    p = iters[0].payload
    for key in ("subagent_id", "profile", "iter", "elapsed_s", "n_invocations", "tokens_in", "tokens_out", "tokens_cached"):
        assert key in p, f"Lead 的 peer.iter 载荷缺 {key}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peer_still_emits() -> None:
    """★正对照：peer 侧原有行为不许被这次改动碰坏。

    没有它，把判据写成「只有 lead 才发」也能让上面两条过。
    """
    iters = await _run("compute")
    assert iters, "peer 的遥测被改坏了"
    assert iters[0].payload["profile"] == "compute"
