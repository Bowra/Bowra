"""Turn visibility D7/FR5/FR6: code streams for every agent while it is written.

A code Tool is recognised by the surface name its run step uses and the language it declares
(`code_language` in tool_aliases.yaml); `skill_invoke` streams once its arguments name `python_sandbox`.
A helper's writing step carries its agent so the trace nests it there, and streamed code is combined
into a few progress events instead of one per provider delta. Lead's query Tools keep their stream;
helpers' queries are not code and are not streamed.
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

from bowhead_core import subagent as subagent_module
from bowhead_router import LLMChunk, LLMRequest, LLMResponse, Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@dataclass
class _Router:
    chunks: list[LLMChunk] = field(default_factory=list)

    async def invoke(self, agent_type: str, req: LLMRequest, **kw: Any) -> LLMResponse:
        return LLMResponse(content="", tool_calls=[])

    def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
        async def _gen() -> AsyncIterator[LLMChunk]:
            for chunk in self.chunks:
                yield chunk

        return _gen()


def _partial(tool: str, call_id: str, text: str) -> LLMChunk:
    return LLMChunk(tool_call_partial={"tool_name": tool, "tool_call_id": call_id, "partial_args_json": text})


async def _stream(chunks: list[LLMChunk], *, profile: str, subagent_id: str) -> tuple[TurnEventBus, list[OrchestratorEvent]]:
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())
    runner = SubagentRunner(router=_Router([*chunks, LLMChunk(finish_reason="tool_use", usage=TokenUsage())]), tool_registry=ToolRegistry())  # type: ignore[arg-type]
    await runner._stream_invoke(
        agent_type=profile,
        llm_req=LLMRequest(model="m", messages=[Message(role="user", content="x")]),
        stream_callback=lambda d: asyncio.sleep(0),
        timeout=10.0,
        bus=bus,
        turn_id="t_code",
        subagent_id=subagent_id,
        profile_name=profile,
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_code", payload={}))
    await consumer
    return bus, captured


def _writing(captured: list[OrchestratorEvent], event_type: str) -> list[OrchestratorEvent]:
    return [e for e in captured if e.event_type == event_type and str(e.payload.get("step_id", "")).startswith("writing_")]


async def test_a_helper_streams_its_code_attributed_to_itself_in_few_events() -> None:
    code = '{"intent": "生成看板", "code": "import json\\nprint(1)\\n"}'
    deltas = [code[i : i + 2] for i in range(0, len(code), 2)]
    bus, captured = await _stream([_partial("python_exec", "call_a", d) for d in deltas], profile="compute", subagent_id="peer_compute")

    [started] = _writing(captured, "step_started")
    assert started.payload["tool_name"] == "python_sandbox", "the run step's surface name, so both rows agree"
    assert started.payload["code_language"] == "python"
    assert started.payload["subagent_id"] == "peer_compute"
    assert started.parent_agent_id == "peer_compute"
    progress = _writing(captured, "step_progress")
    assert len(progress) < len(deltas), "deltas are combined, not one event each"
    assert "".join(e.payload["content_delta"] for e in progress) == code, "the last text is sent before the call runs"
    assert all(e.parent_agent_id == "peer_compute" for e in progress)
    assert bus._writing_call_ids == {"call_a": "peer_compute"}  # type: ignore[attr-defined]


async def test_updates_go_out_once_the_interval_or_size_is_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagent_module, "_WRITING_FLUSH_SECONDS", 0.0)
    _, captured = await _stream([_partial("python_exec", "call_t", d) for d in ("ab", "cd", "ef")], profile="compute", subagent_id="peer_compute")
    assert [e.payload["content_delta"] for e in _writing(captured, "step_progress")] == ["ab", "cd", "ef"]

    big = "x" * subagent_module._WRITING_FLUSH_CHARS
    monkeypatch.setattr(subagent_module, "_WRITING_FLUSH_SECONDS", 3600.0)
    _, captured = await _stream([_partial("python_exec", "call_s", d) for d in (big, "y", "z")], profile="compute", subagent_id="peer_compute")
    assert [e.payload["content_delta"] for e in _writing(captured, "step_progress")] == [big, "yz"]


async def test_skill_invoke_streams_once_its_arguments_name_python_sandbox() -> None:
    args = ['{"na', 'me": "python_', 'sandbox", "script": "runner.py", ', '"input": {"code": "print(1)"}}']
    _, captured = await _stream([_partial("skill_invoke", "call_s", d) for d in args], profile="lead", subagent_id="lead_root")
    [started] = _writing(captured, "step_started")
    assert (started.payload["tool_name"], started.payload["code_language"], started.parent_agent_id) == ("python_sandbox", "python", None)
    assert "subagent_id" not in started.payload, "Lead's writing step stays at the Lead level"
    assert "".join(e.payload["content_delta"] for e in _writing(captured, "step_progress")) == "".join(args)


async def test_calls_that_do_not_write_code_are_not_streamed() -> None:
    other_skill = [_partial("skill_invoke", "call_o", d) for d in ('{"name": "pdf_reader", ', '"input": {"path": "a.pdf"}}')]
    helper_query = [_partial("bi_query", "call_q", '{"queries": ["GMV"]}')]
    unnamed = [_partial("skill_invoke", "call_u", '{"input": {"code": "print(1)"')]
    for chunks, profile in ((other_skill, "compute"), (helper_query, "analyst"), (unnamed, "compute"), (other_skill, "lead")):
        bus, captured = await _stream(chunks, profile=profile, subagent_id="peer_x" if profile != "lead" else "lead_root")
        assert _writing(captured, "step_started") == [], (profile, chunks[0].tool_call_partial)
        assert not getattr(bus, "_writing_call_ids", None)


async def test_lead_query_tools_keep_their_stream() -> None:
    _, captured = await _stream([_partial("bi_query", "call_bi", '{"queries": ["GMV"]}')], profile="lead", subagent_id="lead_root")
    [started] = _writing(captured, "step_started")
    assert started.payload["header_zh"] == "写查询 (SQL)"
    assert "code_language" not in started.payload
