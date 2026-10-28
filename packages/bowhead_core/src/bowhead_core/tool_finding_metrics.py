"""Sprint chat-v3 Phase A · `tool_finding` metrics (real API).

Uses `bowhead_core.metrics.inc()` / `observe()` per r2-review fix (closes
"wrong API names" P0). Module 5 counters + 2 histograms.
"""

from __future__ import annotations

from bowhead_core.metrics import inc, observe


def emit_finding_ok(
    *,
    tool_name: str,
    category: str,
    finding_chars: int,
    latency_ms: float,
    cost_usd: float,
) -> None:
    """Successful finding emit — counter + 2 histograms."""
    inc(
        "tool_finding_emit_total",
        labels={"tool_name": tool_name, "category": category},
    )
    observe("tool_finding_chars", float(finding_chars))
    observe("tool_finding_latency_ms", float(latency_ms))
    observe("tool_finding_micro_cost_usd", float(cost_usd))


def emit_finding_skipped(reason: str) -> None:
    """Skipped — track reason: ``category`` | ``cap`` | ``disabled`` | ``no_preview``."""
    inc("tool_finding_skipped_total", labels={"reason": reason})


def emit_finding_error(kind: str) -> None:
    """LLM call error — track kind: ``timeout`` | ``llm_fail`` | ``other``."""
    inc("tool_finding_error_total", labels={"kind": kind})
