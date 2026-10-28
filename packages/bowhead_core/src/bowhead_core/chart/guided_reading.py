"""Closed, source-dependent reading order for existing native figure children."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_guide_declaration(plan: dict[str, Any]) -> dict[str, Any]:
    """Steps already declare the action; avoid requiring a redundant model field."""
    interactions = plan.get("interactions", [])
    if (
        "guide_steps" not in plan
        or not isinstance(interactions, list)
        or any(isinstance(item, dict) and item.get("preset") == "guided_steps" for item in interactions)
    ):
        return plan
    return {**plan, "interactions": [*interactions, {"preset": "guided_steps"}]}


def focus_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the existing focus compiler responsible only for its own presets."""
    result = dict(plan)
    interactions = [item for item in plan.get("interactions", []) if not isinstance(item, dict) or item.get("preset") != "guided_steps"]
    if interactions:
        result["interactions"] = interactions
    else:
        result.pop("interactions", None)
    return result


def has_focus(plan: Mapping[str, Any]) -> bool:
    return bool(set(focus_plan(plan)) & {"bindings", "view_bindings", "interactions"})


def validate_guides(plan: Mapping[str, Any], spec: Mapping[str, Any] | None = None, *, projected: bool = False) -> None:
    from bowhead_core.chart.presentation_plan import ChartPlanError

    def fail(code: str, message: str) -> None:
        raise ChartPlanError(code, "/presentation_plan/guide_steps", message)

    interactions = plan.get("interactions", [])
    if not isinstance(interactions, list) or len(interactions) > 3 or ("interactions" in plan and not interactions):
        fail("plan_shape", "Use at most three native interaction declarations")
    guides = [item for item in interactions if isinstance(item, dict) and item.get("preset") == "guided_steps"]
    if not guides and "guide_steps" not in plan:
        return
    if len(guides) != 1 or guides[0] != {"preset": "guided_steps"}:
        fail("plan_shape", "Reading steps require exactly one closed guided_steps declaration")
    steps = plan.get("guide_steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 4:
        fail("plan_shape", "Declare one to four reading steps")
    unavailable = projected and (
        plan.get("dataset", {}).get("availability") == "unavailable"
        or any(note.get("availability") == "unavailable" for note in plan.get("evidence_notes", []))
    )
    views = {view["id"] for view in spec.get("views", [])} if spec is not None else None
    if views is not None and len(views) < 2:
        fail("target_invalid", "Reading steps require at least two declared native views")
    for step in steps:
        if not isinstance(step, dict) or not {"title", "view_id"} <= set(step) or set(step) - {"title", "view_id", "annotation_id"}:
            fail(
                "static_representation_missing",
                "Steps support a title, native view ID and an optional observed annotation ID",
            )
        if not isinstance(step["title"], str) or len(step["title"]) > 80 or (not unavailable and not step["title"].strip()):
            fail("plan_shape", "Use a nonempty reading title of at most 80 Unicode characters")
        if not isinstance(step["view_id"], str) or not step["view_id"] or (views is not None and step["view_id"] not in views):
            fail("target_invalid", "Every reading step must target an existing view in this root")
        annotations = plan.get("annotations", [])
        if "annotation_id" in step and (
            not isinstance(annotations, list)
            or not any(
                isinstance(item, dict) and item.get("id") == step["annotation_id"] and item.get("view_id") == step["view_id"] for item in annotations
            )
        ):
            fail("target_invalid", "A reading annotation must exist in its declared root-local view")
