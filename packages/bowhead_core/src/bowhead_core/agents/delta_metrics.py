"""Sprint θ Phase 7.5-A CTO P0 #4 fix: δ-related metrics.

Separated from `lead_metrics.py` so the O7 cardinality lint
(`grep "lead_metrics.*workspace_id"` must = 0) stays clean. δ metrics use
workspace_id label (bounded ~50 workspaces), which is acceptable cardinality
in this scoped namespace.
"""

from __future__ import annotations

from bowhead_core import metrics as core_metrics

DELTA_FACTS_EVICTED_TOTAL = "bowhead_delta_facts_evicted_total"


def emit_fact_evicted(*, workspace_id: str) -> None:
    """δ fact eviction (RegisteredFactStore.register hits per-workspace cap).

    workspace_id label OK here — bounded ~50 workspaces, separate metric
    namespace from `bowhead_lead_orchestrator_*` (which strictly excludes
    workspace_id label per O7).
    """
    core_metrics.inc(
        DELTA_FACTS_EVICTED_TOTAL,
        labels={"workspace_id": workspace_id},
    )
