from __future__ import annotations

from bowhead_core.agent_behavior_materializer import AgentBehaviorMaterializer
from bowhead_core.agent_behavior_parity import IMAGE_DELIVERABLE_PARITY_V1
from bowhead_core.agent_capability_catalog import CapabilityCatalog
from bowhead_core.agent_dependency_compiler import (
    AgentDependencyCompilation,
    AgentRiskAssessment,
    ExactSkillRuntimeScope,
    ResolvedToolDependency,
)
from bowhead_core.subagent import SubagentProfile, _has_image_deliverable_authority

_HASH = "sha256:" + "a" * 64


def _reviewed_image_descriptor():
    materializer = AgentBehaviorMaterializer()
    return materializer.compile_system_revision(
        revision_id="arv_image_parity",
        definition={
            "behavior_catalog_fingerprint": materializer.catalog.fingerprint,
            "llm_agent_type": "analyst",
            "name": "marketing-analyst",
            "primary_capability_ref": "cap_visualize@1",
            "tool_whitelist": ["image_generate", "subagent_return"],
        },
        source_definition_hash=_HASH,
    )


def _team_image_dependency() -> AgentDependencyCompilation:
    tool = ResolvedToolDependency(
        stable_ref="image_generate",
        revision="native-authoring-v1",
        source_kind="native",
        risk="artifact_write",
        risk_source="native_registry",
        approval_required=False,
        capability_classes=("artifact_report",),
        connection_ref=None,
        operation_id=None,
        method=None,
        available=True,
        reason_codes=(),
    )
    return AgentDependencyCompilation(
        status="ready",
        reason_codes=(),
        dependency_fingerprint="sha256:" + "b" * 64,
        capability_fingerprint="sha256:" + "c" * 64,
        tools=(tool,),
        skills=(),
        skill_scope=ExactSkillRuntimeScope(read_names=(), invoke_names=()),
        risk=AgentRiskAssessment(
            level="medium",
            data="none",
            effect="internal_artifact_write",
            automation="interactive",
            scope="current_principal",
            reversibility="reversible",
            reasons=(),
            unknown_dependencies=(),
            approval_required=False,
            runtime_confirmation_required=False,
        ),
    )


def _profile(
    *,
    name: str,
    descriptor=None,
    legacy: bool = False,
) -> SubagentProfile:
    return SubagentProfile(
        name=name,
        llm_agent_type="analyst",
        tool_whitelist=("image_generate", "subagent_return"),
        system_prompt="Generate an image.",
        primary_capability_ref="cap_visualize@1",
        behavior_descriptor=descriptor,
        legacy_image_deliverable_authority=legacy,
    )


def test_reviewed_image_implementation_has_exact_executable_suite() -> None:
    catalog = CapabilityCatalog.bootstrap()
    implementation = catalog.get_implementation("capi_image_generation_native", 1)

    assert implementation.eval_suite == IMAGE_DELIVERABLE_PARITY_V1.id
    assert IMAGE_DELIVERABLE_PARITY_V1.implementation_ref == implementation.exact_ref
    assert IMAGE_DELIVERABLE_PARITY_V1.assertion_ids == (
        "artifact_provenance_required",
        "design_background_excluded",
        "feature_off_behavior_preserved",
        "llm_deliverable_claim_ignored",
        "routing_name_invariant",
    )


def test_system_image_descriptor_is_reviewed_and_exact() -> None:
    descriptor = _reviewed_image_descriptor()

    assert descriptor.verification_state == "reviewed"
    assert descriptor.primary_capability_ref == "cap_visualize@1"
    assert descriptor.implementation_refs == ("capi_image_generation_native@1",)
    assert descriptor.eligible_lead_role_ids == ("image",)
    assert descriptor.output_contract_refs == ("visual.artifact.v1",)


def test_team_image_descriptor_uses_the_same_reviewed_contract() -> None:
    descriptor = AgentBehaviorMaterializer().compile_team_revision(
        revision_id="arv_team_image_parity",
        definition={"primary_capability_ref": "cap_visualize@1"},
        source_definition_hash=_HASH,
        dependency=_team_image_dependency(),
    )

    assert descriptor.verification_state == "reviewed"
    assert descriptor.implementation_refs == ("capi_image_generation_native@1",)
    assert descriptor.eligible_lead_role_ids == ("image",)


def test_image_authority_is_name_invariant_when_reader_is_enabled(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    descriptor = _reviewed_image_descriptor()

    assert _has_image_deliverable_authority(
        _profile(name="marketing-analyst", descriptor=descriptor),
    )
    assert _has_image_deliverable_authority(
        _profile(name="team-custom-visual-maker", descriptor=descriptor),
    )
    assert not _has_image_deliverable_authority(
        _profile(name="marketing-analyst", descriptor=None, legacy=True),
    )


def test_feature_off_preserves_compiled_legacy_behavior(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "0")

    assert _has_image_deliverable_authority(
        _profile(name="any-routing-name", legacy=True),
    )
    assert not _has_image_deliverable_authority(
        _profile(name="marketing-analyst", legacy=False),
    )
