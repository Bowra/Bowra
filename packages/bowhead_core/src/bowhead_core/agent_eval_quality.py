"""Read-only answer-criteria projections shared by Owner and Coach surfaces."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from bowhead_core.agent_eval_points import case_points


def _document(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def answer_quality(expected: object, judge: object) -> dict[str, Any]:
    """Describe recorded advisory judgments without inferring a release or execution verdict."""
    contract, evidence = _document(expected), _document(judge)
    recorded = evidence.get("points")
    verdicts = {row["id"]: row for row in recorded if isinstance(row, dict) and isinstance(row.get("id"), str)} if isinstance(recorded, list) else {}
    points = []
    for point in case_points(contract):
        if point.get("kind") not in {"data_fact", "judgment"}:
            continue
        row = verdicts.get(point["id"], {})
        met = row.get("met") if isinstance(row.get("met"), bool) else None
        reason = row.get("reason") or evidence.get("failure_code")
        points.append(
            {
                "id": point["id"],
                "kind": point["kind"],
                "met": met,
                "advisory": True,
                "calibrated": row.get("calibrated") if isinstance(row.get("calibrated"), bool) else None,
                "reason": reason if isinstance(reason, str) else ("not_graded" if met is None else None),
            }
        )
    return quality_summary(points) | {"points": points}


def quality_summary(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Count point assessments; ungraded points are never counted as met."""
    met = sum(point.get("met") is True for point in points)
    unmet = sum(point.get("met") is False for point in points)
    ungraded = len(points) - met - unmet
    state = "not_configured" if not points else "ungraded" if ungraded == len(points) else "partial" if ungraded else "graded"
    return {"state": state, "met": met, "unmet": unmet, "ungraded": ungraded, "point_assessments": len(points)}
