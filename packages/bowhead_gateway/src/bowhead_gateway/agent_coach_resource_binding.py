"""Verify resource-return associations before saving an ordinary Agent proposal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.agent_coach import ResourceBindingContext

if TYPE_CHECKING:
    from bowhead_gateway.agent_coach_resources import AgentCoachResources


async def validate_resource_binding(resources: AgentCoachResources, *, target: dict, definition: dict, context: dict) -> None:
    """Association is audit evidence, never resource creation or binding authority."""
    context = ResourceBindingContext.model_validate(context).model_dump()
    original = context["original_parent"]
    identity_keys = ("kind", "id") if target["kind"] == "agent_draft" else ("kind", "agent_id", "working_copy_id")
    if any(original.get(key) != target.get(key) for key in identity_keys) or original["version"] > target["version"]:
        raise ValueError("resource_binding_parent_mismatch")
    seen = set()
    for item in context["resources"]:
        reference = item["reference"]
        identity = (reference["kind"], reference.get("source_kind"), reference["id"])
        if identity in seen:
            raise ValueError("resource_binding_duplicate")
        seen.add(identity)
        result = await resources.get(reference=reference)
        if not result.get("ok"):
            raise ValueError("resource_binding_unavailable_or_changed")
        detail = result["resource"]
        kind = reference["kind"]
        if kind == "tool":
            selected = detail.get("can_select") is True and reference["id"] in definition.get("tool_refs", [])
        elif kind == "capability":
            selected = reference["id"] in definition.get("capability_ids", [])
        else:
            grant = detail["exact_read_grant"]
            selected = any(
                value.get("skill_ref") == grant["skill_ref"] and value.get("revision") == grant["revision"]
                for value in definition.get("skill_grants", [])
            )
        if not selected:
            raise ValueError("resource_binding_not_in_candidate")
        source = item["source_operation"]
        if source is None:
            # An administrator may create a resource whose private receipt the Owner cannot read.
            # Current resource authorization is sufficient; no creation provenance is claimed.
            continue
        receipt = (await resources.operations.get(source))["domain_result"]
        if source["kind"] == "skill_save" and kind == "skill" and reference.get("source_kind") == "user":
            saved = (receipt.get("result") or {}).get("target") or {}
            matches = receipt.get("outcome") == "succeeded" and (saved.get("id"), saved.get("revision")) == (reference["id"], reference["revision"])
        elif source["kind"] == "plugin_save" and kind == "capability":
            saved = receipt.get("result_target") or {}
            matches = receipt.get("state") == "succeeded" and (saved.get("id"), str(saved.get("revision"))) == (
                reference["id"],
                reference["revision"],
            )
        elif source["kind"] == "tool_requirement" and kind == "tool":
            observation = receipt.get("observation") or {}
            observed = observation.get("reference") or {}
            matches = observation.get("current_status") == "available" and observed == {
                key: value for key, value in reference.items() if value is not None
            }
            parent = receipt.get("original_parent")
            if parent is not None and parent != original:
                matches = False
        else:
            matches = False
        if not matches:
            raise ValueError("resource_binding_receipt_mismatch")
    await resources._access()
