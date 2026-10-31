"""Versioned structured models for conversational Subagent authoring."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PurposeAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["insufficient", "ambiguous", "clear"]
    evidence_source_or_object: bool
    evidence_action: bool
    evidence_expected_result: bool
    clarification_question: str | None = Field(default=None, max_length=300)
    reason_code: str = Field(max_length=80)


class GeneratedSystemPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(max_length=500)
    goals: list[str] = Field(min_length=1, max_length=6)
    boundaries: list[str] = Field(min_length=1, max_length=8)
    evidence_rules: list[str] = Field(min_length=1, max_length=6)
    failure_behavior: list[str] = Field(min_length=1, max_length=6)
    output_expectations: list[str] = Field(min_length=1, max_length=6)


class CapabilityIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(pattern=r"^ci_[a-z0-9]{1,12}$")
    capability_class: Literal[
        "data_discovery",
        "data_query",
        "isolated_compute",
        "artifact_chart",
        "artifact_report",
        "public_web_research",
        "public_web_fetch",
        "knowledge_search",
        "file_read",
        "team_domain_read",
        "team_domain_write",
        # Sending results by email: met by the built-in Email Plugin, not by a Tool pick.
        "email_send",
    ]
    operation: Literal[
        "read",
        "compute",
        "artifact_write",
        "external_write",
    ]
    object_terms: list[str] = Field(max_length=5)
    source_preference: Literal["native", "team", "any"]
    necessity: Literal["required", "recommended"]
    reason: str = Field(max_length=160)
    confidence: float = Field(ge=0, le=1)


class BlueprintWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=80)
    message: str = Field(max_length=240)


class DraftBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose_assessment: PurposeAssessment
    goal_summary: str = Field(max_length=500)
    display_name: str = Field(max_length=40)
    slug_seed: str = Field(max_length=80)
    description: str = Field(max_length=200)
    system_prompt: GeneratedSystemPrompt
    resource_mode: Literal["tools", "provided_text"] = "tools"
    capability_intents: list[CapabilityIntent] = Field(max_length=12)
    max_iterations_hint: int = Field(default=12, ge=1, le=20)
    assumptions: list[str] = Field(max_length=8)
    warnings: list[BlueprintWarning] = Field(max_length=8)

    @model_validator(mode="after")
    def validate_resource_mode(self) -> DraftBlueprint:
        if self.resource_mode == "provided_text" and self.capability_intents:
            raise ValueError("provided_text drafts must not declare tool capability intents")
        return self


__all__ = [
    "BlueprintWarning",
    "CapabilityIntent",
    "DraftBlueprint",
    "GeneratedSystemPrompt",
    "PurposeAssessment",
]
