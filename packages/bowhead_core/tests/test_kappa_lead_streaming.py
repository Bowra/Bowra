"""Sprint κ Phase B · SubagentRunner streaming wire integration tests.

Per tech-design-r3 §1.2 + §3.2:
- _stream_invoke pushes text deltas to callback in real-time
- accumulates tool_call deltas into LLMResponse.tool_calls
- streaming path gated on profile_name="lead" + tool_results in messages
- non-streaming path used otherwise (no callback invoked)
- streaming + LLM emits tool_call → text deltas already pushed; tool_calls
  surface for next iteration (P1-F test from κ-7 review)
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.subagent import SubagentRunner
from bowhead_router.errors import (
    LLMTargetFailure,
    ProviderFailureKind,
    StreamingInterruptedError,
)
from bowhead_router.types import TokenUsage

from bowhead_router import (
    LLMChunk,
    LLMRequest,
    LLMResponse,
    Message,
    ToolCall,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ─── Fake router ─────────────────────────────────────────────────────────────


@dataclass
class _FakeRouter:
    """Yields preset LLMChunks for stream(); returns preset LLMResponse for invoke()."""

    stream_chunks: list[LLMChunk] = field(default_factory=list)
    invoke_response: LLMResponse | None = None
    invoke_call_count: int = 0
    stream_call_count: int = 0

    async def invoke(self, agent_type: str, req: LLMRequest, **kw: Any) -> LLMResponse:
        self.invoke_call_count += 1
        return self.invoke_response or LLMResponse(content="", tool_calls=[])

    def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
        self.stream_call_count += 1
        return _aiter(self.stream_chunks)


async def _aiter(chunks: list[LLMChunk]) -> AsyncIterator[LLMChunk]:
    for c in chunks:
        yield c


def _make_runner(router: _FakeRouter) -> SubagentRunner:
    from bowhead_core.tool_registry import ToolRegistry

    return SubagentRunner(router=router, tool_registry=ToolRegistry())  # type: ignore[arg-type]


# ─── _stream_invoke direct tests ─────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_text_deltas_pushed_to_callback() -> None:
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(delta="Hello "),
            LLMChunk(delta="world"),
            LLMChunk(finish_reason="stop", usage=TokenUsage(input_tokens=5, output_tokens=2)),
        ]
    )
    runner = _make_runner(router)
    captured: list[str] = []

    async def cb(d: str) -> None:
        captured.append(d)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    resp = await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=cb,
        timeout=10.0,
    )
    assert captured == ["Hello ", "world"]
    assert resp.content == "Hello world"
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_accumulates_tool_call_deltas() -> None:
    tc = ToolCall(id="t1", name="bi_query", arguments={"q": "GMV"})
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(delta="Let me check..."),
            LLMChunk(tool_call_delta=tc),
            LLMChunk(finish_reason="tool_use"),
        ]
    )
    runner = _make_runner(router)
    captured: list[str] = []

    async def cb(d: str) -> None:
        captured.append(d)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    resp = await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=cb,
        timeout=10.0,
    )
    # Text deltas pushed in real-time
    assert captured == ["Let me check..."]
    # Tool calls accumulated for caller (becomes resp.tool_calls)
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "bi_query"
    assert resp.finish_reason == "tool_use"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_callback_exception_logged_not_propagated() -> None:
    """Callback raising should log warn but not abort the stream."""
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(delta="A"),
            LLMChunk(delta="B"),
            LLMChunk(finish_reason="stop"),
        ]
    )
    runner = _make_runner(router)

    calls = 0

    async def cb(d: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("downstream failed")

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    resp = await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=cb,
        timeout=10.0,
    )
    # Both chunks attempted; first raised but second still fed
    assert calls == 2
    # Final content has both deltas
    assert resp.content == "AB"


# ─── Idle timeout ────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_interruption_after_visible_chunk_never_falls_back() -> None:
    """Once text is visible, invoke fallback would duplicate or splice the answer."""

    async def interrupted_stream() -> AsyncIterator[LLMChunk]:
        yield LLMChunk(delta="A")
        raise StreamingInterruptedError(
            [
                LLMTargetFailure(
                    provider="p",
                    model="m",
                    provider_family="openai_compatible",
                    mode="stream",
                    target_index=1,
                    attempt=1,
                    kind=ProviderFailureKind.TIMEOUT,
                    retryable=True,
                    first_visible_chunk_emitted=True,
                )
            ]
        )

    @dataclass
    class _SlowRouter:
        invoke_calls: int = 0

        def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
            return interrupted_stream()

        async def invoke(self, *_a: Any, **_kw: Any) -> LLMResponse:
            self.invoke_calls += 1
            return LLMResponse(content="fallback ok", tool_calls=[])

    router = _SlowRouter()
    runner = _make_runner(router)  # type: ignore[arg-type]
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    async def cb(_d: str) -> None:
        pass

    with pytest.raises(StreamingInterruptedError):
        await runner._stream_invoke(
            agent_type="lead",
            llm_req=req,
            stream_callback=cb,
            timeout=10.0,
        )
    assert router.invoke_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_whole_call_timeout_before_visible_falls_back() -> None:
    """The orchestrator deadline follows the same pre-visible fallback contract."""

    async def stalled_stream() -> AsyncIterator[LLMChunk]:
        await asyncio.Event().wait()
        yield LLMChunk(delta="unreachable")

    @dataclass
    class _SlowRouter:
        invoke_calls: int = 0

        def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
            return stalled_stream()

        async def invoke(self, *_a: Any, **_kw: Any) -> LLMResponse:
            self.invoke_calls += 1
            return LLMResponse(content="fallback ok", tool_calls=[])

    router = _SlowRouter()
    runner = _make_runner(router)  # type: ignore[arg-type]
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    resp = await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=None,
        timeout=0.01,
    )

    assert resp.content == "fallback ok"
    assert router.invoke_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_whole_call_timeout_after_visible_never_falls_back() -> None:
    """The whole-call deadline must not splice a fallback onto visible output."""

    async def partially_stalled_stream() -> AsyncIterator[LLMChunk]:
        yield LLMChunk(delta="A")
        await asyncio.Event().wait()

    @dataclass
    class _SlowRouter:
        invoke_calls: int = 0

        def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
            return partially_stalled_stream()

        async def invoke(self, *_a: Any, **_kw: Any) -> LLMResponse:
            self.invoke_calls += 1
            return LLMResponse(content="fallback ok", tool_calls=[])

    router = _SlowRouter()
    runner = _make_runner(router)  # type: ignore[arg-type]
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    captured: list[str] = []

    async def cb(delta: str) -> None:
        captured.append(delta)

    with pytest.raises(StreamingInterruptedError):
        await runner._stream_invoke(
            agent_type="lead",
            llm_req=req,
            stream_callback=cb,
            timeout=0.01,
        )

    assert captured == ["A"]
    assert router.invoke_calls == 0


# ─── Heuristic: streaming gated on tool_results in context (P1-F) ───────────


# ─── stream-liveness ④: actual streaming-path gate ─────────────────────
#
# ❗These two tests originally exercised nothing: they recomputed `any(m.role == "tool" ...)` locally
# and asserted that variable without touching production code. Thus the streaming-path gating
# contract was never actually tested; changing the gate would not fail them.
# Now drive actual `runner.run()` and inspect `_FakeRouter` call counts to identify the path.


def _stream_ctx():
    from bowhead_core.tool_registry import ToolExecutionContext

    return ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
    )


async def _run_lead(router: _FakeRouter, *, profile: str, with_callback: bool) -> None:
    from bowhead_core.events import TurnEventBus

    runner = _make_runner(router)
    seen: list[str] = []

    async def _cb(delta: str) -> None:
        seen.append(delta)

    with contextlib.suppress(Exception):
        await runner.run(
            profile_name=profile,
            task="你好，用一句话自我介绍",
            parent_turn_id="t",
            parent_bus=TurnEventBus(),
            parent_ctx=_stream_ctx(),
            stream_callback=_cb if with_callback else None,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lead_first_iteration_streams_even_without_tool_results() -> None:
    """Lead streams a first-iteration chat answer even without earlier tool results."""
    router = _FakeRouter(stream_chunks=[LLMChunk(delta="你好")])
    await _run_lead(router, profile="lead", with_callback=True)

    assert router.stream_call_count >= 1, "纯聊天轮没走流式（narrative.delta 会恒 0）"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lead_without_callback_still_uses_invoke() -> None:
    """★正对照：没有 stream_callback ⇒ 照旧非流式（不是"永远流式"）。"""
    router = _FakeRouter(invoke_response=LLMResponse(content="你好", tool_calls=[]))
    await _run_lead(router, profile="lead", with_callback=False)

    assert router.stream_call_count == 0
    assert router.invoke_call_count >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_peer_first_iteration_still_does_not_stream() -> None:
    """★正对照：peer 侧**保持原样**——第一轮（无 tool 结果）仍走非流式。

    本次只解 Lead 那一半；peer 几乎必用工具，放开它只是白白扩大爆炸半径。
    突变：把 peer 那一半的 `any(m.role == "tool" ...)` 也去掉 ⇒ 本条红。
    """
    router = _FakeRouter(invoke_response=LLMResponse(content="ok", tool_calls=[]))
    await _run_lead(router, profile="compute", with_callback=False)

    assert router.stream_call_count == 0, "peer 第一轮不该流式（本次未放开）"


# ─── φ Phase B1 + dogfood s_3e854: PEER prose streams via the bus ────────────


class _CapturingBus:
    """Minimal TurnEventBus stand-in capturing emitted OrchestratorEvents."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_peer_emits_partial_answer_via_bus_no_callback() -> None:
    """dogfood s_3e854 (dead-air fix): a peer (compute/analyst/consultant/...) has
    NO gateway stream_callback, but must still surface its prose live — via
    `partial_answer` events carrying parent_agent_id=subagent_id (φ Phase B1).
    Before the fix the peer never entered _stream_invoke at all (gate was
    profile=="lead"), so the user saw minutes of dead air."""
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(delta="正在分析 "),
            LLMChunk(delta="份额变化"),
            LLMChunk(finish_reason="stop"),
        ]
    )
    runner = _make_runner(router)
    bus = _CapturingBus()
    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])

    resp = await runner._stream_invoke(
        agent_type="compute",
        llm_req=req,
        stream_callback=None,  # peer scope — no gateway callback
        timeout=10.0,
        bus=bus,
        turn_id="t_1",
        subagent_id="sub_abc",
        profile_name="compute",
    )
    # prose still accumulates despite the absent callback (Optional guard)
    assert resp.content == "正在分析 份额变化"
    # peer prose surfaced via bus partial_answer, tagged with the peer's id
    pa = [e for e in bus.events if e.event_type == "partial_answer"]
    assert [e.payload["delta"] for e in pa] == ["正在分析 ", "份额变化"]
    assert all(e.parent_agent_id == "sub_abc" for e in pa)
    assert all(e.payload["parent_agent_id"] == "sub_abc" for e in pa)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_invoke_lead_does_not_double_emit_partial_answer() -> None:
    """The Lead path must NOT emit peer-scope partial_answer (would double the
    chat answer — CTO review P0-2): Lead streams via stream_callback only."""
    router = _FakeRouter(
        stream_chunks=[
            LLMChunk(delta="A"),
            LLMChunk(delta="B"),
            LLMChunk(finish_reason="stop"),
        ]
    )
    runner = _make_runner(router)
    bus = _CapturingBus()
    captured: list[str] = []

    async def cb(d: str) -> None:
        captured.append(d)

    req = LLMRequest(model="m", messages=[Message(role="user", content="x")])
    await runner._stream_invoke(
        agent_type="lead",
        llm_req=req,
        stream_callback=cb,
        timeout=10.0,
        bus=bus,
        turn_id="t_1",
        subagent_id="sub_lead",
        profile_name="lead",
    )
    assert captured == ["A", "B"]  # Lead streams via its callback
    assert [e for e in bus.events if e.event_type == "partial_answer"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runner_calls_the_boundary_callback_after_prose_that_ends_in_tool_calls() -> None:
    """Real SubagentRunner: a call that streams prose and then tool calls reaches
    `stream_on_tool_call` before the next LLM call, so the Lead's marker filter can
    release its held tail in narrative order (the Lead wires `flush` into this callback).
    """
    from bowhead_core.agents.response_kind_scanner import feed, flush, init_per_turn_scanner_state
    from bowhead_core.events import TurnEventBus

    class _TwoCallRouter(_FakeRouter):
        def stream(self, agent_type: str, req: LLMRequest, **kw: Any) -> AsyncIterator[LLMChunk]:
            self.stream_call_count += 1
            if self.stream_call_count == 1:
                return _aiter(
                    [
                        LLMChunk(delta="<response_kind>chat</response_kind>check if a <"),
                        LLMChunk(tool_call_delta=ToolCall(id="t1", name="bi_query", arguments={"q": "GMV"})),
                        LLMChunk(finish_reason="tool_use"),
                    ]
                )
            return _aiter([LLMChunk(delta="done"), LLMChunk(finish_reason="stop")])

    router = _TwoCallRouter()
    runner = _make_runner(router)
    timeline: list[str] = []
    state = init_per_turn_scanner_state()

    async def _cb(delta: str) -> None:
        nonlocal state
        safe, state = feed(state, delta)
        if safe:
            timeline.append(safe)

    async def _boundary(*_args: Any, **_kwargs: Any) -> None:
        nonlocal state
        tail, state = flush(state)
        timeline.append(f"|{tail}|")

    with contextlib.suppress(Exception):
        await runner.run(
            profile_name="lead",
            task="比较一下",
            parent_turn_id="t",
            parent_bus=TurnEventBus(),
            parent_ctx=_stream_ctx(),
            stream_callback=_cb,
            stream_on_tool_call=_boundary,
        )

    # The runner's own condition (`resp.tool_calls and resp.content`) fired the callback once,
    # after the first call's prose and before the second call's text.
    assert timeline == ["check if a ", "|<|", "done"], timeline
    assert router.stream_call_count == 2
