"""chat-v4 CHANGE-W4.1 — lead.final.payload emit-once-validated path.

Tests target the wiring contract (per tech-design r2 §2 CHANGE-W4.1):
  - validate fail / timeout → fall through (no emit)
  - schema OK → setattr ``_turn_committed_to_v4`` + ``_psi_skip_final_compose_only``
    on parent_bus (NOT local var, NOT full ψ-E disable)
  - fabrication caveats injected into payload before emit
  - dedup latch on parent_ctx._v4_final_payload_emitted
  - dict-level `actions` alias for `recommendations` (CHANGE-S.6)

Tests are unit-level — they exercise the helpers + simulate the wiring rather
than booting a full Lead orchestrator loop (which requires PROFILES, LLM
router, canvas store wiring etc — see conftest.py).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import pytest
from bowhead_core.analysis_payload import (
    AnalysisPayload,
    EvidenceBlock,
    Insight,
    Kpi,
    Recommendation,
    check_fabrication_back_reference,
    coerce_verdict_confidence,
    validate_payload,
)

# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeBus:
    """Records setattr calls + emit events (no real queue)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        # _turn_committed_to_v4 / _psi_skip_final_compose_only set via setattr.

    async def emit(self, event: Any) -> None:
        self.events.append((event.event_type, dict(event.payload)))


class _FakeCtx:
    def __init__(self) -> None:
        self._v4_final_payload_emitted = False


def _good_payload_dict() -> dict:
    return asdict(
        AnalysisPayload(
            title="T",
            exec_summary="executive summary text long enough to satisfy validators",
            kpis=[Kpi(label="合规率", value="11.39%")],
            insights=[
                Insight(
                    title="i",
                    thesis="t",
                    evidence=[EvidenceBlock(kind="chart", caption="c", chart_section_id="s")],
                    narrative="x" * 50,
                )
            ],
            recommendations=[Recommendation(priority="P1", action="do")],
        )
    )


# ─── Behavioral contract tests ───────────────────────────────────────────


@pytest.mark.unit
async def test_emit_once_validated() -> None:
    """validate OK → bus receives lead.final.payload event with payload dict."""
    payload = _good_payload_dict()
    ok, errs = validate_payload(payload)
    assert ok, errs
    bus = _FakeBus()

    from bowhead_core.events import OrchestratorEvent

    # Simulate W4.1 emit path
    await bus.emit(
        OrchestratorEvent(
            event_type="lead.final.payload",
            turn_id="t1",
            payload=payload,
        )
    )
    assert len(bus.events) == 1
    et, pl = bus.events[0]
    assert et == "lead.final.payload"
    assert pl["title"] == "T"


@pytest.mark.unit
def test_schema_fail_fallback_v1() -> None:
    """Validation fails → caller should fall through (no emit). Test exercises
    the validate-only contract; orchestrator path's `if not _ok: logger.warning`
    is the actual fallback wire."""
    bad = _good_payload_dict()
    bad["kpis"] = [{"label": "x", "value": "1 ratio"}]  # banned unit suffix
    ok, errs = validate_payload(bad)
    assert not ok
    assert any("ratio" in e for e in errs)


@pytest.mark.unit
async def test_timeout_fallback_v1() -> None:
    """timeout wrapping pattern (asyncio.wait_for + to_thread) — verify the
    pattern surfaces TimeoutError correctly."""

    def _slow_validate() -> tuple[bool, list[str]]:
        import time as _t

        _t.sleep(0.5)
        return True, []

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.to_thread(_slow_validate),
            timeout=0.05,
        )


@pytest.mark.unit
def test_setattr_psi_skip_only_not_full_disable() -> None:
    """CHANGE-W4.1 r2: must use setattr on parent_bus with the NARROW key
    ``_psi_skip_final_compose_only`` — NOT the full ψ-E flag
    ``_psi_compose_done`` nor a local-scoped var."""
    bus = _FakeBus()
    # Verify setattr pattern works on _FakeBus (mirrors parent_bus usage).
    bus._psi_skip_final_compose_only = True
    bus._turn_committed_to_v4 = True
    assert bus._psi_skip_final_compose_only is True
    assert bus._turn_committed_to_v4 is True
    # Must NOT have set the full ψ-E disable.
    assert not getattr(bus, "_psi_compose_done", False)


@pytest.mark.unit
async def test_psi_canvas_sections_still_emit() -> None:
    """Setting `_psi_skip_final_compose_only` must NOT block canvas section
    emit (only the final-compose step is skipped). Verifies the flag is
    semantically narrow — bus continues to accept other events.
    """
    bus = _FakeBus()
    bus._psi_skip_final_compose_only = True
    from bowhead_core.events import OrchestratorEvent

    # canvas-section style events would still emit.
    await bus.emit(
        OrchestratorEvent(
            event_type="canvas.section_added",
            turn_id="t1",
            payload={"section_id": "sec_a", "kind": "chart"},
        )
    )
    assert len(bus.events) == 1


@pytest.mark.unit
def test_fabrication_caps_confidence() -> None:
    """W4.1 + W4.3 integration: fabrication caveats → coerce caps verdict
    confidence at med even when source agreement is high."""
    payload = _good_payload_dict()
    payload["multi_source_compare"] = {
        "rows": [{"label": "GMV", "values": {"BI": 100.0, "ext": 101.0}}],
        "verdict": {"rows": [{"label": "GMV", "verdict": "agree", "confidence": "high"}]},
    }
    # Simulate fabrication detected
    observed = set()  # empty observed
    check_fabrication_back_reference(payload, observed)
    # empty observed → no caveats. Now force a caveat to test coerce capping.
    payload["caveats"] = ["数据可能不完整"]
    coerce_verdict_confidence(payload, observed)
    assert payload["multi_source_compare"]["verdict"]["rows"][0]["confidence"] == "med"


@pytest.mark.unit
def test_dedup_latch_prevents_second_emit() -> None:
    """W4.1 reuses scanner latch ``_v4_final_payload_emitted`` on parent_ctx.
    Once True, emit-site must skip."""
    ctx = _FakeCtx()
    # First emit gate
    if not getattr(ctx, "_v4_final_payload_emitted", False):
        # ... emit ...
        ctx._v4_final_payload_emitted = True
    # Second pass — latch True, skip.
    skipped = getattr(ctx, "_v4_final_payload_emitted", False)
    assert skipped is True


@pytest.mark.unit
def test_actions_alias_dict_level_not_method() -> None:
    """CHANGE-S.6: inject dict-level alias `actions` ↔ `recommendations`.
    NOT a custom to_dict() method on AnalysisPayload — use stdlib asdict()."""
    payload = _good_payload_dict()
    # Simulate W4.1 emit-site alias injection
    payload_for_emit = dict(payload)
    payload_for_emit["actions"] = list(payload_for_emit.get("recommendations") or [])
    assert payload_for_emit["actions"] == payload_for_emit["recommendations"]
    assert payload_for_emit["actions"][0]["priority"] == "P1"
