from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from bowhead_core.agent_artifact_capability import ArtifactCapabilityRegistry
from bowhead_core.agent_behavior_compiler import AgentBehaviorCompiler
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor
from bowhead_core.agent_capability_catalog import CapabilityCatalog
from bowhead_core.agent_capability_composition import CapabilityCompilationContext, CapabilityCompositionCompiler
from bowhead_core.agent_dependency_compiler import AgentDependencyCompilation, AgentRiskAssessment, ExactSkillRuntimeScope
from pydantic import ValidationError

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64


def _analysis_graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "fixed",
        "effect_envelope": "read",
        "max_steps": 4,
        "max_cost_units": 100,
        "max_duration_seconds": 60,
        "inputs": [],
        "outputs": [{"name": "result", "contract": {"contract": "findings", "version": 1}}],
        "nodes": [
            {
                "id": "analyze",
                "kind": "capability",
                "capability": {"id": "cap_analysis", "version": 1},
                "parameters": {},
            },
        ],
        "edges": [
            {
                "source": {"node": "analyze", "port": "findings.v1"},
                "target": {"graph_output": "result"},
                "contract": {"contract": "findings", "version": 1},
            },
        ],
    }


def _dependency(*, status: str = "ready") -> AgentDependencyCompilation:
    return AgentDependencyCompilation(
        status=status,  # type: ignore[arg-type]
        reason_codes=(),
        dependency_fingerprint=_HASH_A,
        capability_fingerprint=_HASH_B,
        tools=(),
        skills=(),
        skill_scope=ExactSkillRuntimeScope(read_names=(), invoke_names=()),
        risk=AgentRiskAssessment(
            level="low",
            data="none",
            effect="read",
            automation="interactive",
            scope="user",
            reversibility="reversible",
            reasons=(),
            unknown_dependencies=(),
            approval_required=False,
            runtime_confirmation_required=False,
        ),
    )


def _catalog_with_reviewed_analysis(tmp_path: Path, *, lead_roles: tuple[str, ...]) -> CapabilityCatalog:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    implementation.update(
        {
            "eval_suite": "analysis-contract-v1",
            "lead_role_ids": list(lead_roles),
            "review_state": "reviewed",
        },
    )
    (tmp_path / "catalog.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation],
            },
        ),
        encoding="utf-8",
    )
    return CapabilityCatalog.bootstrap(tmp_path)


def _compile_composition(catalog: CapabilityCatalog):
    return CapabilityCompositionCompiler(
        catalog=catalog,
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    ).compile_embedded(
        _analysis_graph(),
        revision="arv_behavior_test",
        context=CapabilityCompilationContext(
            tools=(),
            skills=(),
            connections=(),
            available_checks=("model_ready",),
            available_output_grants=(),
        ),
    )


def test_compiler_derives_reviewed_role_and_canonical_fingerprints(tmp_path: Path) -> None:
    catalog = _catalog_with_reviewed_analysis(tmp_path, lead_roles=("compute",))
    composition = _compile_composition(catalog)
    compiler = AgentBehaviorCompiler(catalog)

    first = compiler.compile(
        source_definition_hash=_HASH_B,
        dependency=_dependency(),
        compositions=(composition,),
        primary_capability_ref="cap_analysis@1",
        effective_model_role="analyst",
    )
    second = compiler.compile(
        source_definition_hash=_HASH_B,
        dependency=_dependency(),
        compositions=(composition,),
        primary_capability_ref="cap_analysis@1",
        effective_model_role="analyst",
    )

    assert first == second
    assert first.verification_state == "reviewed"
    assert first.effective_capability_refs == ("cap_analysis@1",)
    assert first.implementation_refs == ("capi_analysis_reasoning@1",)
    assert first.eligible_lead_role_ids == ("compute",)
    assert first.output_contract_refs == ("findings.v1",)
    assert first.behavior_fingerprint.startswith("sha256:")
    assert AgentBehaviorDescriptor.model_validate_json(first.canonical_json()) == first


def test_primary_declaration_cannot_turn_shadow_evidence_into_a_role() -> None:
    catalog = CapabilityCatalog.bootstrap()
    composition = _compile_composition(catalog)

    descriptor = AgentBehaviorCompiler(catalog).compile(
        source_definition_hash=_HASH_B,
        dependency=_dependency(),
        compositions=(composition,),
        primary_capability_ref="cap_analysis@1",
        effective_model_role="analyst",
    )

    assert descriptor.verification_state == "blocked"
    assert descriptor.effective_capability_refs == ()
    assert descriptor.eligible_lead_role_ids == ()
    assert "primary_capability_unproven:cap_analysis@1" in descriptor.reason_codes


def test_descriptor_rejects_extra_fields_and_fingerprint_tampering(tmp_path: Path) -> None:
    catalog = _catalog_with_reviewed_analysis(tmp_path, lead_roles=("compute",))
    descriptor = AgentBehaviorCompiler(catalog).compile(
        source_definition_hash=_HASH_B,
        dependency=_dependency(),
        compositions=(_compile_composition(catalog),),
        primary_capability_ref="cap_analysis@1",
        effective_model_role="analyst",
    )
    payload = json.loads(descriptor.canonical_json())

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentBehaviorDescriptor.model_validate({**payload, "owner_supplied_role": "deck"})
    with pytest.raises(ValidationError, match="behavior fingerprint mismatch"):
        AgentBehaviorDescriptor.model_validate({**payload, "effective_model_role": "writer"})


def test_duplicate_composition_input_is_idempotent(tmp_path: Path) -> None:
    catalog = _catalog_with_reviewed_analysis(tmp_path, lead_roles=("compute",))
    composition = _compile_composition(catalog)
    compiler = AgentBehaviorCompiler(catalog)
    arguments = {
        "source_definition_hash": _HASH_B,
        "dependency": _dependency(),
        "primary_capability_ref": "cap_analysis@1",
        "effective_model_role": "analyst",
    }

    once = compiler.compile(compositions=(composition,), **arguments)
    duplicated = compiler.compile(compositions=(composition, composition), **arguments)

    assert duplicated == once


def test_malformed_primary_capability_fails_closed_without_echoing_input() -> None:
    descriptor = AgentBehaviorCompiler(CapabilityCatalog.bootstrap()).compile(
        source_definition_hash=_HASH_B,
        dependency=_dependency(),
        compositions=(),
        primary_capability_ref="CAP ANALYSIS@1",
        effective_model_role="analyst",
    )

    assert descriptor.verification_state == "blocked"
    assert descriptor.primary_capability_ref is None
    assert "primary_capability_malformed" in descriptor.reason_codes
    assert all("CAP ANALYSIS" not in code for code in descriptor.reason_codes)
