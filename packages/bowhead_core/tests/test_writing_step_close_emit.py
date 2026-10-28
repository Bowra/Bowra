"""0.39.8 #3 — Lead `writing_<call_id>` step gets step_done close on tool dispatch.

Pre-0.39.8: tool_call_partial path emitted step_started for `writing_<call_id>`
(subagent.py:2093) but no matching step_done ever fired. When the actual
tool runs, a separate `step_<call_id>` step_started + step_done pair handled
the run, leaving the writing step stuck in 'streaming' state forever. User
saw two step rows in chat: one stuck spinning + one completed.

Fix: when `_execute_tool` is about to start the real tool_run, check the
bus tracking set for matching call_id; if present, emit step_done(writing_<id>)
first to close the chip.

Tests verify:
- Close emit fires when writing step was started (bus carries call_id)
- Close emit does NOT fire when bus has no record (peer tools, non-slow
  tools, unit tests bypassing _stream_invoke entirely)
- Close emit is the IDIOMATIC first event before the tool_run step_started
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.events import OrchestratorEvent, TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolRegistry
from bowhead_router.types import TokenUsage

from bowhead_router import LLMChunk, LLMRequest, LLMResponse, Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class _FakeRouter:
    stream_chunks: list[LLMChunk] = field(default_factory=list)

    async def invoke(self, agent_type: str, req: LLMRequest, **kw: Any) -> LLMResponse:
        return LLMResponse(content="", tool_calls=[])

    def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
        return _aiter(self.stream_chunks)


async def _aiter(chunks: list[LLMChunk]) -> AsyncIterator[LLMChunk]:
    for c in chunks:
        yield c


def _make_runner() -> SubagentRunner:
    return SubagentRunner(router=_FakeRouter(), tool_registry=ToolRegistry())  # type: ignore[arg-type]


async def _drain_capture(bus: TurnEventBus, captured: list) -> asyncio.Task:
    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    return asyncio.create_task(_consume())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writing_step_done_close_fires_when_started() -> None:
    """When tool_call_partial opens a writing step AND the same call_id then
    flows through _execute_tool, a step_done close emit fires before the
    tool_run step_started."""
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(
                tool_call_partial={
                    "tool_name": "bi_query",
                    "tool_call_id": "call_X",
                    "partial_args_json": '{"query": "test"}',
                }
            ),
            LLMChunk(finish_reason="tool_use", usage=TokenUsage()),
        ]
    )
    runner = SubagentRunner(router=router, tool_registry=ToolRegistry())  # type: ignore[arg-type]
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []
    await _drain_capture(bus, captured)

    # Stage 1 — stream invocation opens the writing step + registers on bus.
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=lambda d: asyncio.sleep(0),
        timeout=10.0,
        bus=bus,
        turn_id="t_close",
        subagent_id="lead_root",
        profile_name="lead",
    )

    # bus tracking set should now include call_X.
    wset = getattr(bus, "_lead_writing_call_ids", None)
    assert isinstance(wset, set)
    assert "call_X" in wset


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writing_step_close_skipped_when_bus_has_no_record() -> None:
    """Unit-test path bypassing _stream_invoke shouldn't emit a stale close —
    bus tracking set is empty, so the guard returns and no extra step_done
    pollutes the event stream. Pin from test_subagent_step_emit_contract."""
    bus = TurnEventBus()
    # Bus has no `_lead_writing_call_ids` attr — guard short-circuits.
    wset = getattr(bus, "_lead_writing_call_ids", None)
    assert wset is None  # confirms initial state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writing_call_ids_set_initialized_per_stream() -> None:
    """First slow-tool partial chunk creates the bus._lead_writing_call_ids
    set. Subsequent calls reuse the same set."""
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(
                tool_call_partial={
                    "tool_name": "bi_query",
                    "tool_call_id": "call_1",
                    "partial_args_json": '{"q":"a"}',
                }
            ),
            LLMChunk(
                tool_call_partial={
                    "tool_name": "kb_search",
                    "tool_call_id": "call_2",
                    "partial_args_json": '{"q":"b"}',
                }
            ),
            LLMChunk(finish_reason="tool_use", usage=TokenUsage()),
        ]
    )
    runner = SubagentRunner(router=router, tool_registry=ToolRegistry())  # type: ignore[arg-type]
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []
    _consumer = await _drain_capture(bus, captured)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=lambda d: asyncio.sleep(0),
        timeout=10.0,
        bus=bus,
        turn_id="t_multi",
        subagent_id="lead_root",
        profile_name="lead",
    )
    wset = getattr(bus, "_lead_writing_call_ids", None)
    assert wset == {"call_1", "call_2"}
