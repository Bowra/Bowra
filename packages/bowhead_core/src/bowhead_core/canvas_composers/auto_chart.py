"""Deliver unreferenced sandbox CSVs as complete native Tables.

Only admitted peers and unconsumed, unreferenced CSV artifacts qualify. The
shared chart Tool owns exact field handling and source/persistence boundaries.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.types import ToolInvocation

logger = logging.getLogger(__name__)

# Feature flag — default ON; flip env to "0" to disable cluster-wide.
_FLAG_ENV = "BOWHEAD_IOTA_AUTO_CHART_FALLBACK"

# Per-turn cap to prevent spam (PRD §4.C.3).
DEFAULT_PER_TURN_CAP = 2

# I-ι1 / I-ι3 invariant: only peers we WANT auto-charting are listed here.
# canvas-writer never runs sandbox. NOTE (subagent-unification fleet retrofit
# 2026-06-01): kb-analyst, compliance-analyst, and consultant DO now have
# skill_invoke (python_sandbox), but are intentionally NOT in this set:
#   - kb-analyst is a document-research analyst — its evidence is [ref:N] KB
#     citations + any explicit add_chart it chooses; we don't want a
#     numeric sandbox byproduct auto-charted onto canvas behind its back.
#   - consultant's deliverable is a downloadable .pptx deck, not canvas charts.
# Excluding them is fail-SAFE (auto-chart is a fallback, not load-bearing): they
# can still chart explicitly via add_chart. Add a name here only if that
# peer should get the forgot-to-chart safety-net for sandbox-derived numerics.
#
# IMPORTANT: this whitelist is a deliberate WANT-list, not a derived sandbox-capability
# list. (Defer to ι+1: derive from registry + an explicit `auto_chart: true` manifest flag.)
_AUTO_CHART_PEERS: frozenset[str] = frozenset(
    {
        "compute",
        "financial-analyst",
        "business-analyst",
    }
)


def _flag_enabled() -> bool:
    """Read env each call so test/runtime flips take effect without restart."""
    return os.environ.get(_FLAG_ENV, "1") != "0"


def peer_supports_auto_chart(profile_name: str) -> bool:
    """Whitelist of peer profiles allowed to emit auto-chart fallback.

    Per PRD I-ι1 / I-ι3: only peers with sandbox / SQL in tools.allow.
    """
    return profile_name in _AUTO_CHART_PEERS


def should_auto_chart(
    artifact_meta: dict[str, Any],
    *,
    consumed_artifact_ids: set[str],
    chart_referenced_artifact_ids: set[str],
) -> bool:
    """Admit CSV fallback delivery without classifying dimensions or measures."""
    if not _flag_enabled():
        return False
    if artifact_meta.get("mime") != "text/csv":
        return False
    aid = artifact_meta.get("artifact_id")
    if not aid:
        return False
    if aid in consumed_artifact_ids:
        return False
    return aid not in chart_referenced_artifact_ids


def collect_auto_chart_candidates(
    invs: list[ToolInvocation],
    *,
    cap: int = DEFAULT_PER_TURN_CAP,
) -> list[dict[str, Any]]:
    """Walk inv list, return add_chart payloads (≤ cap).

    Two-pass:
      1. Build {consumed, referenced} artifact_id sets.
      2. Scan sandbox outputs for AND-gate hits.
    """
    if not _flag_enabled():
        return []

    consumed: set[str] = set()
    referenced: set[str] = set()

    for inv in invs:
        args = inv.arguments if isinstance(inv.arguments, dict) else {}
        sandbox_args = _sandbox_arguments(inv)
        inputs = sandbox_args.get("inputs") if sandbox_args is not None else None
        if isinstance(inputs, dict):
            for spec in inputs.values():
                if isinstance(spec, dict) and spec.get("artifact_id"):
                    consumed.add(str(spec["artifact_id"]))
        # add_chart referenced data_artifact_id
        if inv.tool_name == "add_chart":
            aid = args.get("data_artifact_id")
            if aid:
                referenced.add(str(aid))

    candidates: list[dict[str, Any]] = []
    for inv in invs:
        if inv.error or not inv.result:
            continue
        if _sandbox_arguments(inv) is None:
            continue
        # Only the sandbox Skill shares the native sandbox output protocol.
        result = inv.result if isinstance(inv.result, dict) else {}
        outputs = result.get("outputs") or []
        if not isinstance(outputs, list):
            continue
        for out in outputs:
            if not isinstance(out, dict):
                continue
            preview = out.get("preview")
            if not isinstance(preview, dict):
                continue
            cols = preview.get("columns") or []
            rows = preview.get("rows") or []
            if not isinstance(cols, list) or not isinstance(rows, list):
                continue
            meta = {
                "artifact_id": out.get("artifact_id"),
                "mime": out.get("mime"),
                "row_count": len(rows),
                "filename": out.get("name") or "",
            }
            if should_auto_chart(
                meta,
                consumed_artifact_ids=consumed,
                chart_referenced_artifact_ids=referenced,
            ):
                candidates.append(
                    {
                        "data_artifact_id": meta["artifact_id"],
                        "title": meta["filename"] or "auto-chart",
                        "force_kind": "table",
                        "columns": "all",
                        "auto_charted": True,
                    }
                )
                # Mark this artifact as referenced so a subsequent same-turn
                # sandbox output with the same artifact_id won't auto-chart
                # again (intra-batch dedup; matches end-state once the caller
                # appends the auto-chart ToolInvocation back into invs).
                referenced.add(str(meta["artifact_id"]))
                if len(candidates) >= cap:
                    logger.info("auto_chart.cap_hit cap=%d", cap)
                    return candidates
    return candidates


def _sandbox_arguments(inv: ToolInvocation) -> dict[str, Any] | None:
    """Recognize sandbox producers and unwrap the Skill's nested inputs."""
    args = inv.arguments if isinstance(inv.arguments, dict) else {}
    if inv.tool_name in {"python_exec", "python_sandbox"}:
        return args
    if inv.tool_name == "skill_invoke" and args.get("name") == "python_sandbox":
        return args["input"] if isinstance(args.get("input"), dict) else {}
    return None
