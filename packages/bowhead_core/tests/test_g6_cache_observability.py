"""perf G6 —— `peer.iter` 事件的 token 轨迹 emit contract。

原本这个文件还有 4 条用例，盯的是 η 压缩 / ψ-A 清理两条路的触发计数器。
**上下文工程 sprint 批 1-a 把那两条路整个删了**，用例随之删除
（CLAUDE.md：移除功能时干净删掉实现、入口、文档和失效测试，不为「旧功能不存在」新增断言）。

删除理由正是这个文件原来的 docstring 自己写的那句：

> η/ψ 重写消息史 → provider 前缀缓存从重写点失效（deepseek 命中 9× 实测）

也就是说，这两条路唯一的确定效果是**作废改写点之后的缓存**；而
`turn-latency` sprint 在 116 上实测它们近 2 小时窗口内触发 **0 次**（带 21 条正对照日志，
`docs/sprints/turn-latency-2026-08-21/spike-现状-2026-08-21.md:158-159`）。
一条从不触发、一旦触发就只会让上下文更贵的路，留着是负资产。

剩下的这条 `peer.iter` 用例与那两条路无关，独立成立，保留。
"""

from __future__ import annotations

import pytest
from bi_mock.main import app as bi_mock_app
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_bi_bridge import DatarcClient
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)


@pytest.fixture(autouse=True)
def _pin_fanout_on(monkeypatch):
    # _collect via bus.snapshot() requires fanout (as in test_subagent.py).
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")


@pytest.fixture
async def dc():
    import httpx2

    class _M(DatarcClient):
        def __init__(self) -> None:
            self._base_url = "http://bi-mock/"
            self._auth_key = "dev"
            self._timeout = 10
            self._http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=bi_mock_app),
                base_url="http://bi-mock/",
                headers={"Authorization": "Bearer dev"},
                timeout=10,
            )

    c = _M()
    try:
        yield c
    finally:
        await c.aclose()


# ── 3. peer.iter events carry cumulative token history ─────────────────────────


async def test_peer_iter_event_carries_token_trajectory(dc) -> None:
    """emit contract: peer.iter payload 含 tokens_in/out/cached(截至上一圈累计).

    iter 0 时三者恒 0(还没调过 LLM)——断言键存在与类型,轨迹语义由后续圈承载
    (throttle 2s 使 UT 无法稳定拿到 iter≥1 的事件,真值轨迹归 dogfood)。
    """
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
    router = Router(reg)
    for t in ("lead", "analyst", "echo"):
        router.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))

    runner = SubagentRunner(router=router, tool_registry=build_default_registry(), backend="legacy")
    bus = TurnEventBus()
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=dc)
    result = await runner.run(
        profile_name="compute",
        task="轨迹契约测试",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    assert result.ok
    iters = [e for e in bus.snapshot() if e.event_type == "peer.iter"]
    assert iters, "peer.iter 事件缺失"
    p = iters[0].payload
    for key in ("tokens_in", "tokens_out", "tokens_cached"):
        assert key in p, f"peer.iter payload 缺 {key}(G6 轨迹契约)"
        assert isinstance(p[key], int)
