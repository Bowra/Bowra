"""Immutable, closed schema for revision-bound Agent behavior.

The descriptor is derived server-side.  It is not an authorization grant and
none of its derived fields are accepted from Agent authoring payloads.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AGENT_BEHAVIOR_SCHEMA_VERSION = 1
AGENT_BEHAVIOR_COMPILER_VERSION = "agent-behavior-v1"

LeadRoleId = Literal["data", "compute", "deck", "knowledge", "canvas_edit", "image"]
LEAD_ROLE_IDS: tuple[LeadRoleId, ...] = (
    "data",
    "compute",
    "deck",
    "knowledge",
    "canvas_edit",
    "image",
)

Sha256Ref = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
CapabilityExactRef = Annotated[str, Field(pattern=r"^cap_[a-z][a-z0-9_]{1,63}@[1-9][0-9]*$")]
ImplementationExactRef = Annotated[str, Field(pattern=r"^capi_[a-z][a-z0-9_]{1,95}@[1-9][0-9]*$")]
ArtifactContractExactRef = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}\.v[1-9][0-9]*$")]
ComponentExactRef = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}@[1-9][0-9]*$")]
ReasonCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:@/-]{0,255}$")]


class BehaviorPromptModuleRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: int = Field(ge=1)
    sha256: Sha256Ref

    @property
    def exact_ref(self) -> str:
        return f"{self.id}@{self.version}"


class AgentBehaviorDescriptor(BaseModel):
    """Canonical behavior projection frozen beside an exact Agent revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = AGENT_BEHAVIOR_SCHEMA_VERSION
    compiler_version: Literal["agent-behavior-v1"] = AGENT_BEHAVIOR_COMPILER_VERSION
    source_definition_hash: Sha256Ref
    primary_capability_ref: CapabilityExactRef | None = None
    effective_capability_refs: tuple[CapabilityExactRef, ...] = Field(max_length=64)
    implementation_refs: tuple[ImplementationExactRef, ...] = Field(max_length=128)
    eligible_lead_role_ids: tuple[LeadRoleId, ...] = Field(max_length=len(LEAD_ROLE_IDS))
    output_contract_refs: tuple[ArtifactContractExactRef, ...] = Field(max_length=128)
    adapter_refs: tuple[ComponentExactRef, ...] = Field(max_length=64)
    prompt_module_refs: tuple[BehaviorPromptModuleRef, ...] = Field(max_length=64)
    prompt_module_fingerprint: Sha256Ref
    dependency_fingerprint: Sha256Ref
    capability_fingerprint: Sha256Ref
    behavior_fingerprint: Sha256Ref
    effective_model_role: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    verification_state: Literal["blocked", "shadow", "reviewed"]
    reason_codes: tuple[ReasonCode, ...] = Field(max_length=256)

    @field_validator(
        "effective_capability_refs",
        "implementation_refs",
        "eligible_lead_role_ids",
        "output_contract_refs",
        "adapter_refs",
        "reason_codes",
    )
    @classmethod
    def _values_are_sorted_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("behavior descriptor values must be sorted and unique")
        return value

    @field_validator("prompt_module_refs")
    @classmethod
    def _prompt_modules_are_sorted_and_unique(
        cls,
        value: tuple[BehaviorPromptModuleRef, ...],
    ) -> tuple[BehaviorPromptModuleRef, ...]:
        keys = tuple((item.id, item.version) for item in value)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("behavior prompt modules must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _fingerprints_and_primary_are_consistent(self) -> AgentBehaviorDescriptor:
        if self.primary_capability_ref is not None and self.primary_capability_ref not in self.effective_capability_refs:
            raise ValueError("primary Capability must be effective")
        if self.prompt_module_fingerprint != prompt_module_fingerprint(self.prompt_module_refs):
            raise ValueError("prompt module fingerprint mismatch")
        if self.behavior_fingerprint != behavior_fingerprint(self):
            raise ValueError("behavior fingerprint mismatch")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def prompt_module_fingerprint(refs: tuple[BehaviorPromptModuleRef, ...]) -> str:
    return canonical_fingerprint([item.model_dump(mode="json") for item in refs])


def behavior_fingerprint(descriptor: AgentBehaviorDescriptor) -> str:
    return canonical_fingerprint(descriptor.model_dump(mode="json", exclude={"behavior_fingerprint"}))


def canonical_fingerprint(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
