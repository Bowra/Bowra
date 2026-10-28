"""Sprint θ Phase 8a: Lead orchestrator Prometheus metrics.

Per tech-design §0.6 §4.5 / O3 / O7:
- 1 outcome metric (succeeded / *_exceeded / lead_internal_error /
  clarification_abort / user_cancel / single_write_leaf_violation)
- 1 cost histogram (token_cost_per_turn by turn_class)

Cardinality control (per O7 + cardinality lint test):
- workspace_id NOT in any label set (50 workspace × tools × outcomes = explosion)
- workspace_id info goes to trace events DB (high-cardinality), not Prom

Alert names (PromQL non-overlap fix per O3):
- error rate alert: outcome="lead_internal_error" (NOT "error")
- cap-hit budget alert: outcome=~".*_exceeded"
"""

from __future__ import annotations

from typing import Literal

from bowhead_core import metrics as core_metrics

# -- Metric names (constants for typo safety + alert rule reuse) --

LEAD_OUTCOME_TOTAL = "bowhead_lead_orchestrator_outcome_total"
# report-agent-authorship α v1 (2026-06-24): count "latest report wins" fold events
# (a report-worthy turn folded prior report blocks to superseded). Low-cardinality
# counter only — the per-turn detail (user/session/n_folded) lives in the structured
# log `alpha.fold_prior`, NOT in Prom labels (workspace_id-class cardinality rule).
LEAD_CANVAS_LATEST_WINS_FOLD_TOTAL = "bowhead_lead_orchestrator_canvas_latest_wins_fold_total"
# DELTA_FACTS_* moved to agents/delta_metrics.py per CTO P0 #4 (cardinality separation)


# -- Type aliases (cardinality bounded enums) --

LeadOutcome = Literal[
    "succeeded",
    "hop_cap_exceeded",
    "peer_depth_exceeded",
    "wallclock_exceeded",
    "cost_cap_exceeded",
    "lead_internal_error",  # r2.1: NOT "error" — alert PromQL targets this exact value
    "clarification_abort",
    "user_cancel",
    "single_write_leaf_violation",  # I3 runtime guard
]

TurnClass = Literal["chat", "concept", "data", "composite", "canvas", "clarification_abort"]


# -- emit helpers (low-cardinality, no workspace_id label) --


def emit_outcome(*, outcome: LeadOutcome, turn_class: TurnClass = "chat") -> None:
    """Emit per turn end. outcome enum is canonical for alert PromQL."""
    core_metrics.inc(
        LEAD_OUTCOME_TOTAL,
        labels={"outcome": outcome, "turn_class": turn_class},
    )


def emit_canvas_latest_wins_fold() -> None:
    """report-agent-authorship α v1: a report-worthy turn folded prior report blocks
    (BOWHEAD_CANVAS_LATEST_WINS). Bare counter — no labels (cardinality rule); the
    per-turn detail is in the `alpha.fold_prior` structured log."""
    core_metrics.inc(LEAD_CANVAS_LATEST_WINS_FOLD_TOTAL)


# NOTE (Phase 7.5-A CTO P0 #4): emit_fact_evicted moved to delta_metrics.py
# to keep `grep "lead_metrics.*workspace_id"` cardinality lint clean.
# `bowhead_delta_*` metrics live in their own module per separation of concern.
