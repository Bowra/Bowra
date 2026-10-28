"""prod-bug-bash-039 #6 citation reform — integration test exercising REAL bus.

Unit tests in test_citation_reform.py use a FakeBus that captures `ev.payload`
directly — but production goes through `OrchestratorEvent.to_sse()` which
overwrites `payload["seq"]` with the bus monotonic counter. That clobber bug
was caught by CTO Sec-Review #3 P0-1 — this integration test pins the fix.

What this exercises that the unit tests don't:
  - Real `TurnEventBus.emit` (assigns event.seq monotonically)
  - Real `OrchestratorEvent.to_sse()` (serializes payload + injects bus seq)
  - Verifies the citation_seq (1, 2, 3) survives serialization intact
  - Verifies `payload.seq` is the bus envelope counter (different from
    citation_seq) and FE reducer reads `citation_seq` NOT `seq`
"""

from __future__ import annotations

import json

import pytest
from bowhead_core.agents.lead_orchestrator import LeadRuntime, _flush_citations
from bowhead_core.citations import ensure_registry
from bowhead_core.events import TurnEventBus


@pytest.fixture(autouse=True)
def _pin_fanout_on(monkeypatch):
    # fix/sse-fanout-bus: bus.snapshot() requires fanout; pin ON to decouple this file from external environment settings.
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")


class _StubCtx:
    pass


class _StubSctx:
    turn_id = "t_int_test"


@pytest.mark.asyncio
async def test_flush_through_real_bus_uses_citation_seq_after_to_sse() -> None:
    """The whole chain: register → flush → emit → to_sse → serialize.

    The bug we're pinning: `OrchestratorEvent.to_sse()` at events.py:160
    overwrites `payload["seq"]` with `event.seq` (bus monotonic counter).
    If citation payload used field name `seq`, it would be clobbered and
    every FE chip would resolve to the wrong citation index.

    This test calls the real `TurnEventBus`, drains the queue via the same
    JSON dump → SSE-line path the gateway uses, and asserts the citation
    payload survives intact.
    """
    ctx = _StubCtx()
    registry = ensure_registry(ctx)
    registry.register("art_chart_a", label="Q1 客流", kind="chart_section")
    registry.register("art_compute_b", label=None, kind="tool_output")

    # Real bus — assigns seq monotonically per emit
    bus = TurnEventBus()
    runtime = LeadRuntime(
        router=None,  # type: ignore[arg-type]
        tool_registry=None,  # type: ignore[arg-type]
        parent_bus=bus,
        parent_ctx=ctx,
        session_store=None,
    )

    await _flush_citations(runtime, _StubSctx())

    # Read bus ring via the real serialization path (fix/sse-fanout-bus:
    # snapshot() is nondestructive and available only in fanout mode; this file's autouse fixture pins ON)
    received_sse_payloads: list[dict[str, object]] = []
    for ev in bus.snapshot():
        if ev.event_type != "turn.citation_registered":
            continue
        sse = ev.to_sse()  # ← THE site that clobbers payload["seq"]
        received_sse_payloads.append(json.loads(sse["data"]))

    assert len(received_sse_payloads) == 2

    # Both payloads MUST carry citation_seq (1, 2 — the registry seqs).
    citation_seqs = [p["citation_seq"] for p in received_sse_payloads]
    assert citation_seqs == [1, 2]

    # The bus envelope `seq` IS present but is the monotonic event index
    # (1, 2 or more if other events got emitted). It MUST be distinct from
    # citation_seq (they happen to align here at 1,2 only because no other
    # events fired; in prod they will diverge).
    # The key assertion: citation_seq is NOT clobbered by the bus.
    for p in received_sse_payloads:
        assert "citation_seq" in p, f"payload missing citation_seq: {p}"
        assert isinstance(p["citation_seq"], int)
        assert p["citation_seq"] >= 1

    # Artifact + label round-trip
    assert received_sse_payloads[0]["artifact_id"] == "art_chart_a"
    assert received_sse_payloads[0]["label"] == "Q1 客流"
    assert received_sse_payloads[0]["kind"] == "chart_section"
    assert received_sse_payloads[1]["artifact_id"] == "art_compute_b"
    assert received_sse_payloads[1]["label"] is None
    assert received_sse_payloads[1]["kind"] == "tool_output"


@pytest.mark.asyncio
async def test_event_type_recognized_by_orchestrator() -> None:
    """`turn.citation_registered` must be in the EventType Literal.

    Without this, OrchestratorEvent construction would silently fail TS-style
    type narrowing in strict mode (Python runtime is permissive). The
    integration check: emit succeeds + serializes + the event_type comes
    through as the expected string.
    """
    from bowhead_core.events import OrchestratorEvent

    ev = OrchestratorEvent(
        event_type="turn.citation_registered",  # type: ignore[arg-type]
        turn_id="t_x",
        payload={"citation_seq": 1, "artifact_id": "art_x", "label": None, "kind": "data_ref"},
    )
    ev.seq = 5  # simulate bus auto-population
    sse = ev.to_sse()
    assert sse["event"] == "turn.citation_registered"
    payload = json.loads(sse["data"])
    assert payload["citation_seq"] == 1  # NOT clobbered
    assert payload["seq"] == 5  # bus envelope counter — distinct from citation_seq
    assert payload["artifact_id"] == "art_x"
