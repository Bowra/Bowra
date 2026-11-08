"""A Router restart withdraws what the interrupted model attempt already published.

The shared Lead/helper model loop resets its accumulators, closes writing steps
started in the attempt and emits one durable ``llm.stream_restarted`` that tells
every view exactly which streamed characters and steps to drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.events import OrchestratorEvent, TurnEventBus, stream_restart_narrative_chars
from bowhead_core.subagent import SubagentRunner, current_stream_attempt
from bowhead_core.tool_registry import ToolRegistry

from bowhead_router import LLMChunk, LLMRequest, LLMResponse, Message, ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class _Router:
    chunks: list[LLMChunk]
    kwargs: list[dict[str, Any]] = field(default_factory=list)

    async def invoke(self, agent_type: str, req: LLMRequest, **kw: Any) -> LLMResponse:
        raise AssertionError("a restarted stream must not fall back to invoke")

    async def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
        self.kwargs.append(kw)
        for chunk in self.chunks:
            yield chunk


class _Bus(TurnEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[OrchestratorEvent] = []

    async def emit(self, event: OrchestratorEvent) -> bool:
        self.emitted.append(event)
        return await super().emit(event)


def _wire(payload: dict[str, Any]) -> dict[str, Any]:
    """The producer's payload without the bus sequence fields."""
    return {key: value for key, value in payload.items() if key not in {"seq", "evt_seq"}}


def _partial(call_id: str, text: str) -> LLMChunk:
    return LLMChunk(tool_call_partial={"tool_name": "bi_query", "tool_call_id": call_id, "partial_args_json": text})


async def _invoke(router: _Router, bus: _Bus, *, profile: str, subagent_id: str, stream_callback=None, restart_callback=None) -> LLMResponse:
    runner = SubagentRunner(router=router, tool_registry=ToolRegistry())  # type: ignore[arg-type]
    return await runner._stream_invoke(
        agent_type=profile,
        llm_req=LLMRequest(model="m", messages=[Message(role="user", content="x")]),
        stream_callback=stream_callback,
        stream_restart_callback=restart_callback,
        timeout=10.0,
        bus=bus,
        turn_id="t_restart",
        subagent_id=subagent_id,
        profile_name=profile,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lead_restart_resets_the_attempt_and_withdraws_its_output() -> None:
    first_call = ToolCall(id="call_old", name="bi_query", arguments={"q": "old"})
    final_call = ToolCall(id="call_new", name="bi_query", arguments={"q": "new"})
    router = _Router(
        [
            LLMChunk(delta="Checking "),
            LLMChunk(thinking_delta="plan"),
            _partial("call_old", '{"q": "o'),
            LLMChunk(tool_call_delta=first_call),
            LLMChunk(stream_restart=True),
            LLMChunk(delta="Sales were 42."),
            _partial("call_new", '{"q": "new"}'),
            LLMChunk(tool_call_delta=final_call),
            LLMChunk(finish_reason="tool_use"),
        ]
    )
    bus = _Bus()
    delivered: list[tuple[str, object]] = []
    restarts: list[object] = []

    async def on_delta(delta: str) -> None:
        delivered.append((delta, current_stream_attempt()))

    async def on_restart() -> int:
        restarts.append(current_stream_attempt())
        return 9

    response = await _invoke(router, bus, profile="lead", subagent_id="lead_root", stream_callback=on_delta, restart_callback=on_restart)

    assert router.kwargs[0]["allow_restart"] is True
    assert (response.content, response.thinking, response.tool_calls) == ("Sales were 42.", "", [final_call])
    # The callback sees one attempt token per attempt; the restart is reported for the first.
    assert [delta for delta, _ in delivered] == ["Checking ", "Sales were 42."]
    assert restarts == [delivered[0][1]] and delivered[0][1] is not delivered[1][1]
    assert current_stream_attempt() is None

    restarted = [event for event in bus.emitted if event.event_type == "llm.stream_restarted"]
    assert len(restarted) == 1
    assert restarted[0].parent_agent_id is None
    assert _wire(restarted[0].payload) == {
        "subagent_id": "lead_root",
        "profile": "lead",
        "discarded_narrative_chars": 9,
        "discarded_partial_answer_chars": 0,
        "discarded_step_ids": ["writing_call_old"],
    }
    assert stream_restart_narrative_chars(restarted[0].payload) == 9
    closes = [event for event in bus.emitted if event.event_type == "step_done"]
    assert [(event.payload["step_id"], event.payload["state"]) for event in closes] == [("writing_call_old", "success")]
    order = [(event.event_type, event.payload.get("step_id")) for event in bus.emitted if event.event_type != "step_progress"]
    assert order == [
        ("step_started", "writing_call_old"),
        ("step_done", "writing_call_old"),
        ("llm.stream_restarted", None),
        ("step_started", "writing_call_new"),
    ]
    # Only the retried attempt's writing step is left for the tool dispatch to close.
    assert bus._lead_writing_call_ids == {"call_new"}  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_helper_restart_withdraws_its_streamed_prose() -> None:
    router = _Router(
        [
            LLMChunk(delta="Draft "),
            LLMChunk(delta="text 😀"),
            LLMChunk(stream_restart=True),
            LLMChunk(delta="Final"),
            LLMChunk(finish_reason="stop"),
        ]
    )
    bus = _Bus()
    response = await _invoke(router, bus, profile="analyst", subagent_id="sub_1")

    assert response.content == "Final"
    prose = [event.payload["delta"] for event in bus.emitted if event.event_type == "partial_answer"]
    assert prose == ["Draft ", "text 😀", "Final"]
    restarted = [event for event in bus.emitted if event.event_type == "llm.stream_restarted"]
    assert len(restarted) == 1
    assert restarted[0].parent_agent_id == "sub_1"
    # Code points, as the views count them: the emoji is one character.
    assert _wire(restarted[0].payload) == {
        "subagent_id": "sub_1",
        "profile": "analyst",
        "parent_agent_id": "sub_1",
        "discarded_narrative_chars": 0,
        "discarded_partial_answer_chars": 12,
        "discarded_step_ids": [],
    }
    before = "".join(prose[:2])
    assert before[: len(before) - restarted[0].payload["discarded_partial_answer_chars"]] + prose[2] == "Final"
    assert stream_restart_narrative_chars(restarted[0].payload) == 0


def test_reference_primary_restart_withdraws_narrative_from_helper_scope() -> None:
    # A reference primary streams Lead narrative from a helper-scoped stream.
    payload = {"subagent_id": "ref", "parent_agent_id": "ref", "discarded_narrative_chars": 7, "discarded_partial_answer_chars": 0}
    assert stream_restart_narrative_chars(payload) == 7
