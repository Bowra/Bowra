"""Typed read-only pointers to existing Coach domain receipts, never grants."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from bowhead_core.types import ToolInvocation


class _Reference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SkillSaveReference(_Reference):
    kind: Literal["skill_save"]
    operation_id: str = Field(pattern=r"^csw_[0-9a-f]{64}$")


class PluginSaveReference(_Reference):
    kind: Literal["plugin_save"]
    operation_id: str = Field(pattern=r"^cpw_[0-9a-f]{64}$")


class ToolRequirementReference(_Reference):
    kind: Literal["tool_requirement"]
    requirement_id: str = Field(pattern=r"^ctrq_[0-9a-f]{32}$")
    observation_id: str | None = Field(default=None, pattern=r"^ctro_[0-9a-f]{32}$")


class AgentProposalReference(_Reference):
    kind: Literal["agent_change", "eval_cases"]
    agent_id: str = Field(pattern=r"^osa_[A-Za-z0-9_-]+$", max_length=160)
    proposal_id: str = Field(pattern=r"^acp_[A-Za-z0-9_-]+$", max_length=160)


class CIDelegationReference(_Reference):
    kind: Literal["ci_delegation"]
    agent_id: str = Field(pattern=r"^osa_[A-Za-z0-9_-]+$", max_length=160)
    delegation_id: str = Field(pattern=r"^acig_[0-9a-f]{32}$")


class DraftRecordReference(_Reference):
    kind: Literal["draft_record"]
    draft_id: str = Field(pattern=r"^sad_[A-Za-z0-9_]{1,76}$")


class DraftSuggestionReference(_Reference):
    kind: Literal["draft_suggestion"]
    draft_id: str = Field(pattern=r"^sad_[A-Za-z0-9_]{1,76}$")
    suggestion_id: str = Field(pattern=r"^sas_[0-9a-f]{12}$")


CoachOperationReference = Annotated[
    SkillSaveReference
    | PluginSaveReference
    | ToolRequirementReference
    | AgentProposalReference
    | CIDelegationReference
    | DraftRecordReference
    | DraftSuggestionReference,
    Field(discriminator="kind"),
]
OPERATION_REFERENCE = TypeAdapter(CoachOperationReference)


def operation_receipts(invocations: list[ToolInvocation]) -> list[dict[str, Any]]:
    """Project original domain pointers, including uncertain effects, without content."""
    effects = {
        "agent_draft_start": "draft_record",
        "agent_authoring_propose": "draft_suggestion",
        "agent_change_propose": "agent_change",
        "agent_eval_case_propose": "eval_cases",
        "agent_skill_save": "skill_save",
        "agent_plugin_save": "plugin_save",
        "agent_tool_requirement_create": "tool_requirement",
        "agent_tool_requirement_reconcile": "tool_requirement",
        "agent_ci_request": "ci_delegation",
        "agent_ci_prepare": "ci_delegation",
        "agent_ci_start": "ci_delegation",
        "agent_ci_cancel": "ci_delegation",
    }
    receipts: list[dict[str, Any]] = []
    for invocation in invocations:
        result = invocation.result
        if invocation.tool_name not in effects or not isinstance(result, dict):
            continue
        try:
            reference = OPERATION_REFERENCE.validate_python(result.get("operation_ref"))
        except ValidationError:
            continue
        if reference.kind != effects[invocation.tool_name]:
            continue
        receipts.append(
            {
                "tool_name": invocation.tool_name,
                "operation_ref": reference.model_dump(exclude_none=True),
                "reported_ok": invocation.error is None and result.get("ok") is True,
            }
        )
    return receipts[:20]


class OperationReadInput(_Reference):
    reference: CoachOperationReference


def with_operation_reference(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Project only a domain-produced identity; errors without receipts stay errors."""
    reference = None
    if tool_name.startswith("agent_skill_") and result.get("operation_id"):
        reference = {"kind": "skill_save", "operation_id": result["operation_id"]}
    elif tool_name.startswith("agent_plugin_") and result.get("operation_id"):
        reference = {"kind": "plugin_save", "operation_id": result["operation_id"]}
    elif tool_name.startswith("agent_tool_requirement_") and result.get("requirement_id"):
        reference = {
            "kind": "tool_requirement",
            "requirement_id": result["requirement_id"],
            "observation_id": (result.get("observation") or {}).get("id"),
        }
    elif tool_name.startswith("agent_ci_") and isinstance(result.get("delegation"), dict):
        value = result["delegation"]
        reference = {"kind": "ci_delegation", "agent_id": value.get("agent_id"), "delegation_id": value.get("delegation_id")}
    elif tool_name in {"agent_change_propose", "agent_change_get", "agent_eval_case_propose", "agent_eval_case_proposal_get"} and isinstance(
        result.get("proposal"), dict
    ):
        value = result["proposal"]
        reference = {
            "kind": "eval_cases" if tool_name.startswith("agent_eval_") else "agent_change",
            "agent_id": value.get("agent_id"),
            "proposal_id": value.get("id"),
        }
    elif tool_name in {"agent_authoring_propose", "agent_authoring_proposal_get"} and isinstance(result.get("proposal"), dict):
        value = result["proposal"]
        reference = {"kind": "draft_suggestion", "draft_id": value.get("draft_id"), "suggestion_id": value.get("id")}
    elif tool_name == "agent_draft_start" and result.get("ok") and isinstance(result.get("target"), dict):
        reference = {"kind": "draft_record", "draft_id": result["target"].get("id")}
    if reference is None:
        return result
    try:
        canonical = OPERATION_REFERENCE.validate_python(reference).model_dump()
    except ValidationError:
        # A partial/unknown effect must not acquire an invented receipt pointer.
        return result
    return {**result, "operation_ref": canonical}
