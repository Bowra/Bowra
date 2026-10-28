"""Sprint ω Phase 5 — admin metrics rollup endpoint.

Per r3.1 §2.9: replaces NoOp Prometheus counters with metrics_log (SQLite)
event queries. Provides aggregated rollup for ω-introduced events:

- `response_kind_scanner_decision` — Phase 2 scanner outcome distribution
- `subagent_payload_truncated`     — Phase 7 budget enforcement (later)
- `tool_result_lineage_write`      — Phase 7 clearing audit (later)
- `frozen_corpus_replay_diff`      — Phase 9 ship-pre gate (later)

Auth: `Depends(require_team_admin)` per r3.1 SRE P0-SRE-2. Unauth endpoint
leaks per-user attrs + spoofs rollback signal — gated to admin-domain only.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from bowhead_gateway.routes._deps import require_team_admin

if TYPE_CHECKING:
    from bowhead_core.user_store import User

router = APIRouter(tags=["admin-metrics"])


@router.get("/admin/metrics/rollup")
async def metrics_rollup(
    request: Request,
    name: str = Query(..., description="Metric event name (dot-namespaced)"),
    hours: int = Query(24, ge=1, le=720, description="Lookback window"),
    me: User = Depends(require_team_admin),
) -> dict[str, Any]:
    """Rollup event counts grouped by selected attrs over recent window.

    Example: GET /admin/metrics/rollup?name=response_kind_scanner_decision&hours=6
    returns:
        {
          "name": "response_kind_scanner_decision",
          "hours": 6,
          "total": 142,
          "by_attrs": {
            "kind=chat,match_mode=clean": 76,
            "kind=canvas,match_mode=clean": 53,
            "kind=absent,match_mode=absent": 13
          }
        }

    Used by ops to detect ω-F rollback trigger (e.g. absent rate > 30% over 6h
    → flip BOWHEAD_OMEGA_RESPONSE_KIND_V3=0).
    """
    metrics_log = getattr(request.app.state, "metrics_log", None)
    if metrics_log is None:
        raise HTTPException(status_code=503, detail="metrics_log not initialized")

    rows = await metrics_log.query_recent(name=name, hours=hours, limit=10000)
    by_attrs: Counter[str] = Counter()
    for row in rows:
        attrs = row.get("attrs") or {}
        # Stable key: sort attr keys + drop noisy ones (turn_id is high-cardinality)
        kv = sorted((k, v) for k, v in attrs.items() if k not in {"turn_id", "session_id"})
        key = ",".join(f"{k}={v}" for k, v in kv) or "(no_attrs)"
        by_attrs[key] += 1
    return {
        "name": name,
        "hours": hours,
        "total": len(rows),
        "by_attrs": dict(by_attrs.most_common()),
    }
