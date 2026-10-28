"""Sprint chat-v4 W3.2 — UT for `peer_dispatched` payload extensions.

Covers:
- spawn_id in payload == subagent_id minted by runner.run (pre-mint contract).
- profile field added.
- rationale full text, NOT truncated to 200 chars.
- parent_ctx_summary KEPT for back-compat.
"""

from __future__ import annotations

import asyncio

import pytest
from bi_mock.main import app as bi_mock_app
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import (
    SubagentResult,
    SubagentRunner,
    register_subagent_tool,
)
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_bi_bridge import DatarcClient
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
    for t in ("lead", "modeler", "analyst", "echo", "judge"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


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


@pytest.fixture(autouse=True)
def _pin_fanout_on(monkeypatch):
    # fix/sse-fanout-bus: _drain uses snapshot() (fanout only); pin ON to isolate external env.
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")


async def _drain(bus: TurnEventBus) -> list[OrchestratorEvent]:
    return bus.snapshot()


@pytest.mark.asyncio
async def test_spawn_id_in_payload(dc) -> None:
    """peer_dispatched.payload must carry spawn_id == agent_spawned.subagent_id."""
    router = _router(LLMResponse(content="done"))
    tr = build_default_registry()
    register_subagent_tool(tr)
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_parent",
        datarc=dc,
        subagent_runner=runner,
        parent_bus=bus,
    )

    await tr.execute(
        "subagent_spawn",
        {
            "profile": "compute",
            "task": "dummy",
            "rationale": "因果归因 多维拆解",
        },
        ctx,
    )

    events = await _drain(bus)
    pd = [e for e in events if e.event_type == "peer_dispatched"]
    sp = [e for e in events if e.event_type == "agent_spawned"]
    assert len(pd) == 1
    # `agent_spawned` may fire from multiple sites (subagent.run + completion
    # echo); pick the one with the same id as peer_dispatched.
    assert len(sp) >= 1
    # Pre-mint contract — peer_dispatched.spawn_id matches the eventual
    # agent_spawned.subagent_id (FE keys on this single id across both events).
    pd_payload = pd[0].payload or {}
    assert pd_payload.get("spawn_id"), "spawn_id missing from peer_dispatched"
    spawn_ids = {(e.payload or {}).get("subagent_id") for e in sp}
    assert pd_payload["spawn_id"] in spawn_ids
    # subagent_id alias also present (back-compat for older FE)
    assert pd_payload.get("subagent_id") == pd_payload["spawn_id"]


@pytest.mark.asyncio
async def test_rationale_full_text_in_payload(dc) -> None:
    """rationale string must NOT be 200-char-truncated (I-W3-7)."""
    router = _router(LLMResponse(content="done"))
    tr = build_default_registry()
    register_subagent_tool(tr)
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
        subagent_runner=runner,
        parent_bus=bus,
    )
    long_rationale = (
        "做归因分析 因为用户问的是为什么 Q3 GMV 同比下滑 8%, 需要拆 "
        "渠道 / 品牌 / 品类 / 时间 / 客群 5 维, 每维 top-5 贡献度 + "
        "对比同期 + 寻找最大负贡献项. 再 cross-join 找异常区段."
    ) * 3  # ~600 chars
    assert len(long_rationale) > 200
    await tr.execute(
        "subagent_spawn",
        {"profile": "compute", "task": "dummy", "rationale": long_rationale},
        ctx,
    )
    events = await _drain(bus)
    pd = next(e for e in events if e.event_type == "peer_dispatched")
    payload = pd.payload or {}
    assert payload.get("rationale") == long_rationale  # full text, no truncation
    # parent_ctx_summary kept for back-compat — still 200-truncated
    assert payload.get("parent_ctx_summary") == long_rationale[:200]


@pytest.mark.asyncio
async def test_profile_field_in_payload(dc) -> None:
    """profile field added so FE can render `.is-{profile}` class."""
    router = _router(LLMResponse(content="done"))
    tr = build_default_registry()
    register_subagent_tool(tr)
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t",
        datarc=dc,
        subagent_runner=runner,
        parent_bus=bus,
    )
    await tr.execute(
        "subagent_spawn",
        {"profile": "compute", "task": "dummy", "rationale": "x"},
        ctx,
    )
    events = await _drain(bus)
    pd = next(e for e in events if e.event_type == "peer_dispatched")
    payload = pd.payload or {}
    assert payload.get("profile") == "compute"
    assert payload.get("peer_name") == "compute"  # alias kept


@pytest.mark.asyncio
async def test_spawn_tool_execution_stamps_exact_parent_step_correlation(dc) -> None:
    """The real Lead tool path threads call/step ids into peer events."""
    router = _router(LLMResponse(content="done"))
    tr = build_default_registry()
    register_subagent_tool(tr)
    runner = SubagentRunner(router=router, tool_registry=tr, backend="legacy")
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_parent",
        datarc=dc,
        subagent_runner=runner,
        parent_bus=bus,
    )

    await runner._execute_tool(
        ToolCall(
            id="call_spawn_1",
            name="subagent_spawn",
            arguments={"profile": "compute", "task": "dummy"},
        ),
        ctx,
        bus,
        "t_parent",
        "lead_parent",
        running_profile="lead",
    )

    events = await _drain(bus)
    started = next(e for e in events if e.event_type == "step_started" and (e.payload or {}).get("call_id") == "call_spawn_1")
    dispatched = next(e for e in events if e.event_type == "peer_dispatched")
    summary = next(e for e in events if e.event_type == "lead.subagent_summary")
    assert dispatched.payload["call_id"] == "call_spawn_1"
    assert dispatched.payload["parent_step_id"] == started.payload["step_id"]
    assert summary.payload["call_id"] == "call_spawn_1"
    assert summary.payload["parent_step_id"] == started.payload["step_id"]


@pytest.mark.asyncio
async def test_parallel_spawn_correlations_do_not_cross_siblings(dc) -> None:
    """Task-local correlation survives two overlapping Lead spawn calls."""

    class _BarrierBus(TurnEventBus):
        def __init__(self) -> None:
            super().__init__()
            self._started_count = 0
            self._both_started = asyncio.Event()

        async def emit(self, event: OrchestratorEvent) -> None:
            if event.event_type == "step_started":
                self._started_count += 1
                if self._started_count == 2:
                    self._both_started.set()
                else:
                    await self._both_started.wait()
            await super().emit(event)

    class _PeerRunner:
        async def run(self, **kwargs) -> SubagentResult:
            await asyncio.sleep(0)
            return SubagentResult(
                profile=str(kwargs["profile_name"]),
                subagent_id=str(kwargs["pre_minted_subagent_id"]),
                ok=True,
                final_answer=str(kwargs["task"]),
            )

    tr = build_default_registry()
    register_subagent_tool(tr)
    executor = SubagentRunner(
        router=_router(LLMResponse(content="unused")),
        tool_registry=tr,
        backend="legacy",
    )
    bus = _BarrierBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="aaa95ad475254d8e83566a13135e58fa",
        user_id="u",
        turn_id="t_parent",
        datarc=dc,
        subagent_runner=_PeerRunner(),
        parent_bus=bus,
    )

    await asyncio.gather(
        executor._execute_tool(
            ToolCall(
                id="call_a",
                name="subagent_spawn",
                arguments={"profile": "compute", "task": "peer a"},
            ),
            ctx,
            bus,
            "t_parent",
            "lead_parent",
            running_profile="lead",
        ),
        executor._execute_tool(
            ToolCall(
                id="call_b",
                name="subagent_spawn",
                arguments={"profile": "compute", "task": "peer b"},
            ),
            ctx,
            bus,
            "t_parent",
            "lead_parent",
            running_profile="lead",
        ),
    )

    events = await _drain(bus)
    started = {event.payload["call_id"]: event.payload["step_id"] for event in events if event.event_type == "step_started"}
    dispatched = {event.payload["call_id"]: event.payload["parent_step_id"] for event in events if event.event_type == "peer_dispatched"}
    summaries = {event.payload["call_id"]: event.payload["parent_step_id"] for event in events if event.event_type == "lead.subagent_summary"}
    assert started == {"call_a": "step_call_a", "call_b": "step_call_b"}
    assert dispatched == started
    assert summaries == started


# Sprint phase-plan-removal — test_ctx_stashes_last_spawn_id deleted.
# `_v4_last_peer_dispatched_spawn_id` was the correlation stash for the now-
# deleted phase.update emits; with phase.update gone the slot has no
# consumer, so the stash itself was removed (see subagent.py + lead_transient.py).
