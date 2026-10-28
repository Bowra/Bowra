"""Sprint θ Phase 8e: Cardinality lint test.

Per tech-design §0.6 O7 fix + 7.5-A CTO P0 #4:
- workspace_id label NOT in any `bowhead_lead_orchestrator_*` metric label set
- Cardinality budget: 50 workspace × ~12 tool × 3 outcome = 1800 series would
  blow Prom storage at multi-tenant scale. Move workspace_id to trace events DB.

Implementation: grep `lead_metrics.*workspace_id` in source — must be 0 occurrences.
δ-related metrics (delta_metrics module) DO use workspace_id (bounded ~50, OK).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOWHEAD_CORE_SRC = _REPO_ROOT / "packages" / "bowhead_core" / "src"


def _grep_callsites(pattern: str, paths: list[Path]) -> list[tuple[Path, int, str]]:
    """Helper: find regex matches across paths. Returns (file, line, content)."""
    pat = re.compile(pattern)
    hits: list[tuple[Path, int, str]] = []
    for p in paths:
        if not p.is_dir():
            continue
        for f in p.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            try:
                for lineno, line in enumerate(
                    f.read_text(encoding="utf-8").splitlines(),
                    start=1,
                ):
                    if pat.search(line):
                        hits.append((f, lineno, line.strip()))
            except (UnicodeDecodeError, OSError):
                continue
    return hits


def test_lead_metrics_no_workspace_id_label() -> None:
    """Cardinality lint: no `lead_metrics.*workspace_id` callsites.

    O7 invariant — workspace_id MUST NOT appear in any bowhead_lead_orchestrator_*
    metric label set. Use trace events DB for high-cardinality workspace dimension.

    Carve-out: `agents.delta_metrics` is separate module for δ-specific metrics
    (workspace_id-bounded, ~50 workspaces, acceptable cardinality).
    """
    callsites = _grep_callsites(
        r"\blead_metrics\.[a-zA-Z_]+\([^)]*workspace_id",
        paths=[_BOWHEAD_CORE_SRC],
    )
    # Filter out test files (lint shouldn't catch test helpers)
    real_callsites = [(f, ln, c) for (f, ln, c) in callsites if "test_" not in f.name and "tests/" not in str(f)]
    assert not real_callsites, (
        "`lead_metrics.*workspace_id` found in production code — violates "
        "O7 cardinality invariant. Move to delta_metrics or trace events:\n"
        + "\n".join(f"  {f.relative_to(_REPO_ROOT)}:{ln}  {c}" for f, ln, c in real_callsites)
    )


def test_delta_metrics_module_carve_out_is_separate() -> None:
    """δ metrics legitimately use workspace_id label — verify they live in
    `agents/delta_metrics.py` (separate module from lead_metrics)."""
    delta_metrics_path = _BOWHEAD_CORE_SRC / "bowhead_core" / "agents" / "delta_metrics.py"
    assert delta_metrics_path.is_file(), (
        "Phase 7.5-A CTO P0 #4 fix: emit_fact_evicted moved to delta_metrics.py (separate from lead_metrics namespace). File missing."
    )
    content = delta_metrics_path.read_text(encoding="utf-8")
    # Sanity check: delta_metrics SHOULD have workspace_id usage
    assert "workspace_id" in content, "delta_metrics.py expected to use workspace_id (it's the carve-out module for δ-related metrics)."


def test_lead_metrics_outcome_enum_matches_alert_promql() -> None:
    """Sanity: `outcome` label values in code match alert PromQL filter values."""
    lead_metrics_py = _BOWHEAD_CORE_SRC / "bowhead_core" / "agents" / "lead_metrics.py"
    content = lead_metrics_py.read_text(encoding="utf-8")
    # Critical enum values referenced by alerts/lead_orchestrator.yaml:
    required_outcomes = [
        "lead_internal_error",  # not "error" per O3 fix
        "hop_cap_exceeded",
        "wallclock_exceeded",
        "cost_cap_exceeded",
        "peer_depth_exceeded",
        "single_write_leaf_violation",  # I3 runtime guard
    ]
    for outcome in required_outcomes:
        assert f'"{outcome}"' in content, (
            f"LeadOutcome enum missing '{outcome}' — alert PromQL in infra/prometheus/alerts/lead_orchestrator.yaml expects this."
        )
