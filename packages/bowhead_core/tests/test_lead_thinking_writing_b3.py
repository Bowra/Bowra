"""Sprint chat-redesign p0-followup B3 — lead_thinking_writing tool set extension.

Audit doc: docs/chat-redesign-prd-gap-audit-2026-05-23.md §3 P1-2.
PRD ref: §4.1.2 + §5.3 (Phase 2/3+ extends partial-tool detect to slow KB /
web queries so user sees "Lead 正在写..." for ALL slow tools, not just
python_sandbox + bi_query).

Pre-B3: subagent.py:2082 hardcoded set `{python_sandbox, bi_query}` → only
those two tools triggered lead_thinking_writing step_started. KB / web
search large queries → user saw nothing while Lead typed args (P8 痛点
solved 60%).

Post-B3: set extended to include kb_search + web_search. Future slow tools
land via the same set + header dictionary (no new wiring needed once tool
adapter supports tool_call_partial chunks).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_router.types import TokenUsage

from bowhead_router import LLMChunk, LLMRequest, LLMResponse, Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class _FakeRouter:
    """Yields preset LLMChunks; mimics provider partial tool_call stream."""

    stream_chunks: list[LLMChunk] = field(default_factory=list)
    invoke_response: LLMResponse | None = None

    async def invoke(self, agent_type: str, req: LLMRequest, **kw: Any) -> LLMResponse:
        return self.invoke_response or LLMResponse(content="", tool_calls=[])

    def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
        return _aiter(self.stream_chunks)


async def _aiter(chunks: list[LLMChunk]) -> AsyncIterator[LLMChunk]:
    for c in chunks:
        yield c


def _make_runner(router: _FakeRouter) -> SubagentRunner:
    from bowhead_core.tool_registry import ToolRegistry

    return SubagentRunner(router=router, tool_registry=ToolRegistry())  # type: ignore[arg-type]


async def _drain_capture(bus: TurnEventBus, captured: list) -> asyncio.Task:
    """Spawn a consumer that captures every event until bus.done sentinel."""

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    return asyncio.create_task(_consume())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kb_search_partial_emits_lead_thinking_writing() -> None:
    """KB search large query → step_started with step_type=lead_thinking_writing
    + header_zh "查知识库 (KB)".
    """
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(
                tool_call_partial={
                    "tool_name": "kb_search",
                    "tool_call_id": "call_kb_1",
                    "partial_args_json": '{"query": "公司年度战略目',
                }
            ),
            LLMChunk(
                tool_call_partial={
                    "tool_name": "kb_search",
                    "tool_call_id": "call_kb_1",
                    "partial_args_json": '"标 2024"}',
                }
            ),
            LLMChunk(finish_reason="tool_use", usage=TokenUsage()),
        ]
    )
    runner = _make_runner(router)
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []
    consumer = await _drain_capture(bus, captured)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=lambda d: asyncio.sleep(0),  # noop callback
        timeout=10.0,
        bus=bus,
        turn_id="t_kb",
        subagent_id="lead_root",
        profile_name="lead",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_kb", payload={}))
    await consumer

    started = [e for e in captured if e.event_type == "step_started" and e.payload.get("step_type") == "lead_thinking_writing"]
    assert len(started) == 1, f"expected 1 lead_thinking_writing event, got {len(started)}: {[e.payload for e in started]}"
    ev = started[0]
    assert ev.payload["tool_name"] == "kb_search"
    assert ev.payload["header_zh"] == "查知识库 (KB)"
    assert ev.payload["step_id"] == "writing_call_kb_1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_web_search_partial_emits_lead_thinking_writing() -> None:
    """web_search complex query → step_started with header_zh "搜网络 (Web)"."""
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(
                tool_call_partial={
                    "tool_name": "web_search",
                    "tool_call_id": "call_web_1",
                    "partial_args_json": '{"queries": ["2024 中国医药',
                }
            ),
            LLMChunk(
                tool_call_partial={
                    "tool_name": "web_search",
                    "tool_call_id": "call_web_1",
                    "partial_args_json": '行业增长率"]}',
                }
            ),
            LLMChunk(finish_reason="tool_use", usage=TokenUsage()),
        ]
    )
    runner = _make_runner(router)
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []
    consumer = await _drain_capture(bus, captured)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=lambda d: asyncio.sleep(0),
        timeout=10.0,
        bus=bus,
        turn_id="t_web",
        subagent_id="lead_root",
        profile_name="lead",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_web", payload={}))
    await consumer

    started = [e for e in captured if e.event_type == "step_started" and e.payload.get("step_type") == "lead_thinking_writing"]
    assert len(started) == 1
    ev = started[0]
    assert ev.payload["tool_name"] == "web_search"
    assert ev.payload["header_zh"] == "搜网络 (Web)"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_python_sandbox_bi_query_still_emit() -> None:
    """B3 regression: extending the set must not break the original two
    tools (python_sandbox + bi_query) firing.
    """
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(
                tool_call_partial={
                    "tool_name": "bi_query",
                    "tool_call_id": "call_bi_1",
                    "partial_args_json": '{"queries": ["GMV"]}',
                }
            ),
            LLMChunk(finish_reason="tool_use", usage=TokenUsage()),
        ]
    )
    runner = _make_runner(router)
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []
    consumer = await _drain_capture(bus, captured)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=lambda d: asyncio.sleep(0),
        timeout=10.0,
        bus=bus,
        turn_id="t_bi",
        subagent_id="lead_root",
        profile_name="lead",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_bi", payload={}))
    await consumer

    started = [e for e in captured if e.event_type == "step_started" and e.payload.get("step_type") == "lead_thinking_writing"]
    assert len(started) == 1
    assert started[0].payload["tool_name"] == "bi_query"
    assert started[0].payload["header_zh"] == "写查询 (SQL)"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_slow_tool_does_not_emit_lead_thinking_writing() -> None:
    """skill_read / add_chart etc. should NOT trigger lead_thinking_writing
    (visibility too noisy). Only the slow-tool set in B3.
    """
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(
                tool_call_partial={
                    "tool_name": "skill_read",
                    "tool_call_id": "call_skill_1",
                    "partial_args_json": '{"path": "/skills/x.md"}',
                }
            ),
            LLMChunk(finish_reason="tool_use", usage=TokenUsage()),
        ]
    )
    runner = _make_runner(router)
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []
    consumer = await _drain_capture(bus, captured)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=lambda d: asyncio.sleep(0),
        timeout=10.0,
        bus=bus,
        turn_id="t_skill",
        subagent_id="lead_root",
        profile_name="lead",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_skill", payload={}))
    await consumer

    started = [e for e in captured if e.event_type == "step_started" and e.payload.get("step_type") == "lead_thinking_writing"]
    assert len(started) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peer_profile_does_not_emit_lead_thinking_writing() -> None:
    """Even when slow tool partial → if profile_name != "lead" (peer
    subagent), no emit (peer subagents don't expose partial code per
    tech-design §2.2.4).
    """
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(
                tool_call_partial={
                    "tool_name": "kb_search",
                    "tool_call_id": "call_kb_peer",
                    "partial_args_json": '{"query": "x"}',
                }
            ),
            LLMChunk(finish_reason="tool_use", usage=TokenUsage()),
        ]
    )
    runner = _make_runner(router)
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []
    consumer = await _drain_capture(bus, captured)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="compute",
        llm_req=req,
        stream_callback=lambda d: asyncio.sleep(0),
        timeout=10.0,
        bus=bus,
        turn_id="t_peer",
        subagent_id="peer_1",
        profile_name="compute",  # peer, NOT lead
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_peer", payload={}))
    await consumer

    started = [e for e in captured if e.event_type == "step_started" and e.payload.get("step_type") == "lead_thinking_writing"]
    assert len(started) == 0, "peer profile must not emit lead_thinking_writing"
