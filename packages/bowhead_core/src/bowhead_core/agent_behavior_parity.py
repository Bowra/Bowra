"""Code-owned parity evidence required by reviewed Capability implementations.

The registry is deliberately small and closed.  An ``eval_suite`` string in a
Capability manifest is not sufficient evidence on its own: every reviewed
implementation must point at one exact suite here, and CI owns the executable
assertions named by that suite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentBehaviorParitySuite:
    id: str
    implementation_ref: str
    assertion_ids: tuple[str, ...]


IMAGE_DELIVERABLE_PARITY_V1 = AgentBehaviorParitySuite(
    id="image-deliverable-parity-v1",
    implementation_ref="capi_image_generation_native@1",
    assertion_ids=(
        "artifact_provenance_required",
        "design_background_excluded",
        "feature_off_behavior_preserved",
        "llm_deliverable_claim_ignored",
        "routing_name_invariant",
    ),
)

AGENT_BEHAVIOR_PARITY_SUITES: dict[str, AgentBehaviorParitySuite] = {
    IMAGE_DELIVERABLE_PARITY_V1.id: IMAGE_DELIVERABLE_PARITY_V1,
    "document-admission-parity-v1": AgentBehaviorParitySuite(
        id="document-admission-parity-v1",
        implementation_ref="capi_template_document_native@1",
        assertion_ids=("canonical_request_required", "exact_admission_atomic", "no_publication_from_receipt", "feature_off_fenced"),
    ),
}


__all__ = [
    "AGENT_BEHAVIOR_PARITY_SUITES",
    "AgentBehaviorParitySuite",
    "IMAGE_DELIVERABLE_PARITY_V1",
]
