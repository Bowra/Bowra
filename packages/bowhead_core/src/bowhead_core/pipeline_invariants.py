"""Sprint omicron Phase A — per-turn pipeline invariant check.

INV-1..INV-8 per PRD r4 §1. Pure logic — takes already-computed inputs,
returns set of violated invariant IDs. Caller (run_turn finally) emits
metrics + decides retry (Phase B+) / logs.

INV-1 / 5 / 8 trigger conditions are documented inline. INV-2/3/4 are
monitor-only and computed by Phase F dogfood post-hoc (not here).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# INV-5 detection: [ref:N] markers in final_answer / narrative
_REF_MARKER_RE = re.compile(r"\[ref:\d+\]")

# INV-8 detection: hard-coded numbers without bi.* backing. Tightened per
# Eng-r3 P0-3 (exclude literal 元/个/CNY: too common in casual prose). Word boundary
# `\b` not used (CJK boundary undefined); rely on explicit suffixes.
_INV8_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:亿|万|%)")


@dataclass
class InvariantResult:
    """Outcome of per-turn invariant check."""

    violated: list[str] = field(default_factory=list)
    bi_tool_count: int = 0
    peer_dispatched: int = 0
    cite_count: int = 0
    chart_count: int = 0
    artifact_count: int = 0

    def __bool__(self) -> bool:
        return bool(self.violated)


def check_invariants(
    *,
    tool_invocations: list[Any],
    final_answer_text: str | None,
    canvas_section_kinds: dict[str, int] | None = None,
    artifact_count: int = 0,
    has_final_answer_event: bool = True,
) -> InvariantResult:
    """Run all P0 invariants and return violations.

    Args:
        tool_invocations: list of ToolInvocation objects (Lead profile, not nested)
        final_answer_text: chat-bubble text emitted to user
        canvas_section_kinds: kind → count (e.g. {"narrative": 2, "chart": 1})
        artifact_count: number of artifacts persisted this turn (session-linked)
        has_final_answer_event: True if final_answer event was emitted to bus

    Returns:
        InvariantResult with `violated` set populated. Caller emits metrics.
    """
    canvas_section_kinds = canvas_section_kinds or {}
    final_answer_text = final_answer_text or ""

    # bi.* tool count (excluding bi_schema — that's metadata-only, not data fetch)
    bi_tool_count = sum(
        1 for inv in tool_invocations if getattr(inv, "tool_name", "").startswith("bi_") and getattr(inv, "tool_name", "") != "bi_schema"
    )
    peer_dispatched = sum(1 for inv in tool_invocations if getattr(inv, "tool_name", "") == "subagent_spawn")
    cite_count = len(_REF_MARKER_RE.findall(final_answer_text))
    chart_count = canvas_section_kinds.get("chart", 0)

    out = InvariantResult(
        bi_tool_count=bi_tool_count,
        peer_dispatched=peer_dispatched,
        cite_count=cite_count,
        chart_count=chart_count,
        artifact_count=artifact_count,
    )

    # INV-1: Lead called bi.* AND no peer dispatched
    # (Lead self-handled a data query that should have been delegated to compute peer)
    if bi_tool_count >= 1 and peer_dispatched == 0:
        out.violated.append("INV-1")

    # INV-5: Lead self-answer + bi.* + final_answer has no [ref:N]
    # (citation not emitted — user sees bare numbers without source anchor)
    if bi_tool_count >= 1 and peer_dispatched == 0 and cite_count == 0:
        out.violated.append("INV-5")

    # INV-6: done emit but final_answer event not emitted (silent done)
    if not has_final_answer_event:
        out.violated.append("INV-6")

    # INV-8: final_answer contains hardcoded business numbers but no bi.* called
    # (likely LLM hallucinated from training data — must trigger retry / warn)
    if bi_tool_count == 0 and final_answer_text and _INV8_NUMBER_RE.search(final_answer_text):
        out.violated.append("INV-8")

    return out


def emit_invariant_metrics(inv_result: InvariantResult) -> None:
    """No-op stub; prometheus metrics removed (project no longer uses Prometheus)."""
