"""Phase 1.6 G3 — `narrative.delta` emit contract (Lead-scope).

Two layers:

1. **Unit layer** — exercises the production `_emit_narrative_delta` helper
   directly against a stub bus. This is the single chokepoint the production
   stream callback also routes through, so test drift cannot happen
   (per `feedback_ut_must_assert_emit_contract`).

2. **Integration layer** — reconstructs the scanner pipeline that wraps the
   helper, asserting marker strip + multi-chunk accumulation produce the
   right narrative.delta sequence end-to-end.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents.lead_orchestrator import _emit_narrative_delta
from bowhead_core.agents.prompts.peer_block import PeerEntry
from bowhead_core.agents.response_kind_scanner import (
    feed as scanner_feed,
)
from bowhead_core.agents.response_kind_scanner import (
    init_per_turn_scanner_state,
)

if TYPE_CHECKING:
    from bowhead_core.events import OrchestratorEvent


@dataclass
class _StubBus:
    """Stand-in for TurnEventBus — records emitted events."""

    events: list[OrchestratorEvent] = field(default_factory=list)

    async def emit(self, event: OrchestratorEvent) -> None:
        # Mirror real bus.emit auto-populate seq.
        if event.seq < 0:
            event.seq = len(self.events) + 1
        self.events.append(event)


class _StubCtx:
    response_kind_pre_flight: str | None = None


class _StubSD:
    def __init__(self) -> None:
        self.received: list[str] = []

    async def feed(self, chunk: str) -> None:
        self.received.append(chunk)


# ─── Unit layer — `_emit_narrative_delta` chokepoint ─────────────────────


@pytest.mark.asyncio
async def test_emit_helper_fires_for_non_empty_safe_text() -> None:
    bus = _StubBus()
    await _emit_narrative_delta(bus, "t_u1", "好的，我先查 Q3 数据")
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev.event_type == "narrative.delta"
    assert ev.turn_id == "t_u1"
    assert ev.payload == {"card_id": "t_u1", "delta": "好的，我先查 Q3 数据"}


@pytest.mark.asyncio
async def test_emit_helper_replaces_internal_routing_profile() -> None:
    bus = _StubBus()
    bus._actor_scoped_prompt_peers = (PeerEntry(name="gina", display_name="数据查询师", description="", source="team"),)

    await _emit_narrative_delta(bus, "t_public", "已派发 gina 查询数据")

    assert bus.events[0].payload["delta"] == "已派发 数据查询师 查询数据"


@pytest.mark.asyncio
async def test_emit_helper_skips_empty_string() -> None:
    bus = _StubBus()
    await _emit_narrative_delta(bus, "t_u2", "")
    assert bus.events == []


@pytest.mark.asyncio
async def test_emit_helper_skips_whitespace_only() -> None:
    bus = _StubBus()
    await _emit_narrative_delta(bus, "t_u3", "   \n\t  ")
    assert bus.events == []


@pytest.mark.asyncio
async def test_emit_helper_swallows_bus_exception() -> None:
    """Bus emit failure must NOT propagate — streaming pipeline cannot break."""

    class _ExplodingBus:
        async def emit(self, event: OrchestratorEvent) -> None:
            raise RuntimeError("bus down")

    # No raise.
    await _emit_narrative_delta(_ExplodingBus(), "t_u4", "some text")


# ─── Integration layer — scanner pipeline → emit helper ──────────────────


def _build_gated_cb(sd: _StubSD, ctx: _StubCtx, bus: _StubBus, turn_id: str):
    """Reconstruct the scanner-gated callback shape from lead_orchestrator.
    Calls the SAME `_emit_narrative_delta` helper as production — only the
    closure plumbing is faked.
    """
    _state = init_per_turn_scanner_state()

    async def _scanner_gated_stream_cb(delta: str) -> None:
        if os.environ.get("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1") != "1":
            await sd.feed(delta)
            return
        nonlocal _state
        safe_text, _state = scanner_feed(_state, delta)
        if _state.decision and _state.decision != "absent":
            with contextlib.suppress(Exception):
                ctx.response_kind_pre_flight = _state.decision
        if safe_text:
            await _emit_narrative_delta(bus, turn_id, safe_text)
            await sd.feed(safe_text)

    return _scanner_gated_stream_cb


@pytest.mark.asyncio
async def test_pipeline_single_chunk_marker_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    sd, ctx, bus = _StubSD(), _StubCtx(), _StubBus()
    cb = _build_gated_cb(sd, ctx, bus, turn_id="t_1")
    await cb("<response_kind>chat</response_kind>好的，我先查 Q3 数据")
    narr = [e for e in bus.events if e.event_type == "narrative.delta"]
    assert len(narr) == 1
    assert narr[0].payload["delta"] == "好的，我先查 Q3 数据"
    assert sd.received == ["好的，我先查 Q3 数据"]


@pytest.mark.asyncio
async def test_pipeline_multi_chunk_accumulates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    sd, ctx, bus = _StubSD(), _StubCtx(), _StubBus()
    cb = _build_gated_cb(sd, ctx, bus, turn_id="t_2")
    for chunk in ["这是没有任何", "标记的纯散文", "回答内容"]:
        await cb(chunk)
    narr = [e for e in bus.events if e.event_type == "narrative.delta"]
    combined = "".join(e.payload["delta"] for e in narr)
    assert combined == "这是没有任何标记的纯散文回答内容"
    assert "".join(sd.received) == combined


@pytest.mark.asyncio
async def test_pipeline_marker_only_chunks_emit_zero_narrative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker-only fragments must produce ZERO narrative.delta events —
    tighter than 'whatever emits is non-whitespace'."""
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    sd, ctx, bus = _StubSD(), _StubCtx(), _StubBus()
    cb = _build_gated_cb(sd, ctx, bus, turn_id="t_3")
    # Just the marker, no prose
    await cb("<response_kind>")
    await cb("chat</response_kind>")
    narr = [e for e in bus.events if e.event_type == "narrative.delta"]
    assert narr == [], f"expected zero narrative.delta on marker-only chunks, got {narr}"


@pytest.mark.asyncio
async def test_pipeline_marker_split_no_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker split across chunks → safe text only, no marker fragments leak."""
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    sd, ctx, bus = _StubSD(), _StubCtx(), _StubBus()
    cb = _build_gated_cb(sd, ctx, bus, turn_id="t_4")
    for chunk in ["<resp", "onse_kind>", "canvas", "</response_kind>", "之后正文"]:
        await cb(chunk)
    narr = [e for e in bus.events if e.event_type == "narrative.delta"]
    combined = "".join(e.payload["delta"] for e in narr)
    assert "response_kind" not in combined.lower()
    assert combined == "之后正文"
    assert ctx.response_kind_pre_flight == "canvas"


@pytest.mark.asyncio
async def test_pipeline_card_id_matches_turn_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    sd, ctx, bus = _StubSD(), _StubCtx(), _StubBus()
    cb = _build_gated_cb(sd, ctx, bus, turn_id="t_card_match")
    await cb("一些散文")
    narr = [e for e in bus.events if e.event_type == "narrative.delta"]
    assert len(narr) == 1
    assert narr[0].payload["card_id"] == "t_card_match"
    assert narr[0].turn_id == "t_card_match"
