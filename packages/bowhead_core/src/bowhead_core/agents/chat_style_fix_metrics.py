"""Sprint chat-style-fix Phase A5 — observability counters + SLO histograms.

Mirror of ``tool_finding_metrics.py`` API. Wraps the canonical
``bowhead_core.metrics.inc()`` / ``observe()`` so call sites can stay terse +
this module is the single edit point for label / counter-name changes.

Per tech-design.md §I-3 SLO table:
  * headline_emit_latency_ms_p95          ≤ 300 ms
  * header_insight_haiku_latency_ms_p95   ≤ 1500 ms

13 counters + 2 histograms total.
"""

from __future__ import annotations

from bowhead_core.metrics import inc, observe

# ─── Headline counters (Phase A2) ───────────────────────────────────────


def inc_headline_emitted() -> None:
    """Scanner captured `<headline>...</headline>` — happy path."""
    inc("headline_emitted_total")


def inc_headline_missing() -> None:
    """Turn ended without scanner observing any headline at all."""
    inc("headline_missing_total")


def inc_headline_truncated() -> None:
    """R-2 — body > _MAX_HEADLINE_BUFFER without close tag."""
    inc("headline_truncated_total")


def inc_headline_missing_close() -> None:
    """R-3 — open emitted but stream ended without close tag."""
    inc("headline_missing_close_total")


def inc_headline_duplicate() -> None:
    """R-4 — second `<headline>` open observed (FIRST-WINS, second ignored)."""
    inc("headline_duplicate_total")


def inc_headline_disorder() -> None:
    """R-1 — `<headline>` open emitted BEFORE response_kind decision locked."""
    inc("headline_disorder_before_response_kind_total")


def observe_headline_emit_latency_ms(latency_ms: float) -> None:
    """SLO ≤ 300ms p95 — first-byte → headline event SSE emit."""
    observe("headline_emit_latency_ms", float(latency_ms))


# ─── header_insight counters (Phase A3) ──────────────────────────────────


def inc_header_insight_empty(reason: str = "bail_path") -> None:
    """Haiku call skipped (no insights) or returned empty string."""
    inc("header_insight_empty_total", labels={"reason": reason})


def inc_header_insight_truncated() -> None:
    """Haiku returned > 200 chars — backend hard-truncate."""
    inc("header_insight_truncated_total")


def observe_header_insight_haiku_latency_ms(latency_ms: float) -> None:
    """SLO ≤ 1500ms p95 — haiku micro-LLM round-trip."""
    observe("header_insight_haiku_latency_ms", float(latency_ms))


# ─── tool_call.kind counter (Phase A4) ───────────────────────────────────
# Note (post-ship-pre review): originally defined `inc_tool_kind_field_missing`
# as a "missing kind field" sentinel — removed as dead code since the backend
# always emits `kind` via `lookup_kind(tool_name)` (fallback "compute"). The
# only path that could fire it would be FE-side trace replay, not in scope.
