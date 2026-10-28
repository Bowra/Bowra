from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from bowhead_core.agent_capability_catalog import (
    CapabilityCatalog,
    CapabilityDriftCompiler,
    CapabilityReadinessContext,
    LegacyCapabilitySource,
    detect_prompt_protocols,
)
from bowhead_core.agents.agent_runner_adapter import default_prompt_loader
from bowhead_core.agents.capability_roles import agent_capabilities
from bowhead_core.agents.exceptions import ManifestValidationError
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.system_agent_marketplace import SYSTEM_AGENT_LISTINGS


@pytest.mark.unit
def test_compute_evidence_requires_explicit_execution_support() -> None:
    assert "cap_compute" not in agent_capabilities(("skill_read", "skill_invoke"))
    assert "cap_compute" in agent_capabilities(("python_sandbox",))
    assert "cap_compute" in agent_capabilities(("python_exec",))


@pytest.mark.unit
def test_system_evidence_uses_effective_team_skill_and_prompt_overrides() -> None:
    from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride

    manifest = AgentRegistry.bootstrap().get("compute")
    compiler = CapabilityDriftCompiler(CapabilityCatalog.bootstrap())
    baseline = compiler.compile_system(manifest=manifest, listing_capability_ids=("cap_compute",))
    changed = compiler.compile_system(
        manifest=manifest,
        listing_capability_ids=("cap_compute",),
        override=BuiltinSubagentOverride(
            team_id="team_a", name="compute", skills=["pdf"], prompt_append="Use the team evidence policy.", row_version=3
        ),
    )
    assert "pdf" in changed.source.skill_refs
    assert changed.source.prompt_fingerprint != baseline.source.prompt_fingerprint
    assert changed.source.source_version.endswith(":override:3")
    assert changed.evidence_fingerprint != baseline.evidence_fingerprint


def test_catalog_loads_versioned_definitions_and_reviewed_image_implementation() -> None:
    catalog = CapabilityCatalog.bootstrap()

    assert len(catalog.definitions) == 14
    assert len(catalog.implementations) == 10
    assert catalog.get_definition("cap_report", 1).outputs[0].exact_ref == "report.document.v1"
    assert catalog.latest_definition("cap_presentation").listing["zh-CN"].name == "制作演示文稿"  # type: ignore[union-attr]
    assert {item.exact_ref for item in catalog.implementations if item.review_state == "reviewed"} == {
        "capi_image_generation_native@1",
        "capi_template_document_native@1",
    }
    report_implementations = catalog.implementations_for(catalog.get_definition("cap_report", 1))
    assert [item.exact_ref for item in report_implementations] == ["capi_report_canvas@1"]
    assert report_implementations[0].output_grant is not None
    assert report_implementations[0].output_grant.id == "report-writer-grant"
    assert report_implementations[0].lead_role_ids == ()
    assert catalog.get_implementation("capi_data_query_native", 1).lead_role_ids == ("data",)
    assert catalog.get_implementation("capi_compute_python_native", 1).lead_role_ids == ("compute",)
    assert catalog.get_implementation("capi_knowledge_search_native", 1).lead_role_ids == ("knowledge",)
    image = catalog.get_implementation("capi_image_generation_native", 1)
    assert image.required_tools == ("image_generate",)
    assert image.lead_role_ids == ("image",)
    assert image.eval_suite == "image-deliverable-parity-v1"
    catalog.validate_reviewed_evidence()
    assert catalog.fingerprint.startswith("sha256:")


def test_reviewed_implementation_cannot_borrow_another_implementations_parity_suite(
    tmp_path: Path,
) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_visualize", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_image_generation_native", 1).model_dump(mode="json")
    implementation["id"] = "capi_image_generation_unbound"
    (tmp_path / "invalid.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation],
            },
        ),
        encoding="utf-8",
    )

    catalog = CapabilityCatalog.bootstrap(tmp_path)

    with pytest.raises(ManifestValidationError, match="no exact executable parity suite"):
        catalog.validate_reviewed_evidence()


def test_catalog_keeps_multiple_exact_definition_versions_resolvable(tmp_path: Path) -> None:
    definition = CapabilityCatalog.bootstrap().get_definition("cap_analysis", 1).model_dump(mode="json")
    newer = {**definition, "version": 2}
    (tmp_path / "catalog-v1.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "definitions": [definition], "implementations": []}),
        encoding="utf-8",
    )
    (tmp_path / "catalog-v2.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "definitions": [newer], "implementations": []}),
        encoding="utf-8",
    )

    catalog = CapabilityCatalog.bootstrap(tmp_path)

    assert catalog.get_definition("cap_analysis", 1).version == 1
    assert catalog.get_definition("cap_analysis", 2).version == 2
    assert catalog.latest_definition("cap_analysis").version == 2  # type: ignore[union-attr]


def test_catalog_rejects_duplicate_exact_versions(tmp_path: Path) -> None:
    definition = CapabilityCatalog.bootstrap().get_definition("cap_analysis", 1).model_dump(mode="json")
    bundle = yaml.safe_dump({"schema_version": 1, "definitions": [definition], "implementations": []})
    (tmp_path / "a.yaml").write_text(bundle, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(bundle, encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="duplicate capability definition"):
        CapabilityCatalog.bootstrap(tmp_path)


def test_catalog_rejects_implementation_with_unknown_definition(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.implementations[0].model_dump(mode="json")
    implementation["capability"] = {"id": "cap_missing", "version": 1}
    (tmp_path / "invalid.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="references unknown definition"):
        CapabilityCatalog.bootstrap(tmp_path)


def test_catalog_rejects_ambiguous_implementation_priority(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    competing = {**implementation, "id": "capi_analysis_competing"}
    (tmp_path / "invalid.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation, competing],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="ambiguous implementation priority"):
        CapabilityCatalog.bootstrap(tmp_path)


def test_catalog_rejects_effect_risk_understatement(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    implementation["effect_class"] = "external_write"
    (tmp_path / "invalid.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="risk_floor cannot be lower"):
        CapabilityCatalog.bootstrap(tmp_path)


@pytest.mark.parametrize("lead_roles", [["compute", "compute"], ["owner_defined_role"]])
def test_catalog_rejects_invalid_platform_lead_roles(tmp_path: Path, lead_roles: list[str]) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    implementation["lead_role_ids"] = lead_roles
    (tmp_path / "invalid.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError):
        CapabilityCatalog.bootstrap(tmp_path)


def test_catalog_rejects_conflicting_prompt_module_identity(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    first = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    first["prompt_modules"] = [
        {"id": "analysis-protocol", "version": 1, "sha256": "sha256:" + "a" * 64},
    ]
    second = {
        **first,
        "id": "capi_analysis_reasoning_alt",
        "priority": first["priority"] - 1,
        "prompt_modules": [
            {"id": "analysis-protocol", "version": 1, "sha256": "sha256:" + "b" * 64},
        ],
    }
    (tmp_path / "invalid.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [first, second],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="conflicting Prompt Module identity"):
        CapabilityCatalog.bootstrap(tmp_path)


def test_system_drift_compile_is_deterministic_and_does_not_claim_verification() -> None:
    catalog = CapabilityCatalog.bootstrap()
    manifest = AgentRegistry.bootstrap().get("compute")
    compiler = CapabilityDriftCompiler(catalog)

    first = compiler.compile_system(
        manifest=manifest,
        listing_capability_ids=tuple(SYSTEM_AGENT_LISTINGS["compute"]["capabilities"]),
        prompt_text=default_prompt_loader(manifest),
    )
    second = compiler.compile_system(
        manifest=manifest,
        listing_capability_ids=tuple(reversed(SYSTEM_AGENT_LISTINGS["compute"]["capabilities"])),
        prompt_text=default_prompt_loader(manifest),
    )
    payload = first.to_consumer_dict(locale="zh-CN")

    assert first.evidence_fingerprint == second.evidence_fingerprint
    changed_prompt = compiler.compile_system(
        manifest=manifest,
        listing_capability_ids=tuple(SYSTEM_AGENT_LISTINGS["compute"]["capabilities"]),
        prompt_text=default_prompt_loader(manifest) + "\nadditional contract text",
    )
    assert changed_prompt.evidence_fingerprint != first.evidence_fingerprint
    assert "evidence_fingerprint" not in payload
    assert payload["verification_status"] == "derived_unverified"
    assert {item["verification_status"] for item in payload["capabilities"]} == {"derived_unverified"}
    assert payload["drift"]["implicit_team_tool_overlay"] == {
        "status": "drift",
        "reason_codes": ["implicit_team_tool_overlay"],
    }
    assert payload["drift"]["unverified_outputs"] == {"status": "drift", "formats": ["pptx"]}
    assert "cap_presentation" in payload["drift"]["listing_vs_manifest"]["dependency_without_listing"]
    assert payload["drift"]["prompt_duplication"]["status"] == "drift"


@pytest.mark.parametrize("agent_name", sorted(SYSTEM_AGENT_LISTINGS))
def test_every_system_agent_compiles_without_leaking_raw_dependencies(agent_name: str) -> None:
    manifest = AgentRegistry.bootstrap().get(agent_name)
    report = CapabilityDriftCompiler(CapabilityCatalog.bootstrap()).compile_system(
        manifest=manifest,
        listing_capability_ids=tuple(SYSTEM_AGENT_LISTINGS[agent_name]["capabilities"]),
        prompt_text=default_prompt_loader(manifest),
    )

    serialized = json.dumps(report.to_consumer_dict(), ensure_ascii=False)

    for raw_ref in (*manifest.tools.allow, *manifest.skills.framework, *manifest.skills.sop_lazy):
        assert raw_ref not in serialized


def test_team_projection_hides_raw_dependencies_and_preserves_unknown_legacy_claims() -> None:
    compiler = CapabilityDriftCompiler(CapabilityCatalog.bootstrap())
    source = LegacyCapabilitySource(
        agent_id="osa_1",
        source_kind="team",
        source_version="7",
        listing_capability_ids=("cap_skill", "cap_data_query"),
        tool_refs=("bi_query",),
        skill_refs=("private-secret-method",),
        prompt_protocols=("sandbox_protocol_in_agent_prompt",),
    )

    report = compiler.compile(source)
    payload = report.to_consumer_dict(locale="zh-CN")
    serialized = json.dumps(payload, ensure_ascii=False)
    by_id = {item["id"]: item for item in payload["capabilities"]}

    assert by_id["cap_data_query"]["availability"] == "degraded"
    assert by_id["cap_data_query"]["name"] == "查询与分析数据"
    assert by_id["cap_skill"]["definition_status"] == "unknown"
    assert by_id["cap_skill"]["availability"] == "unknown"
    assert payload["legacy_direct_dependencies"] == {
        "status": "derived_unverified",
        "tool_count": 1,
        "skill_count": 1,
    }
    assert "bi_query" not in serialized
    assert "private-secret-method" not in serialized
    assert "tool_refs" not in serialized
    assert "skill_refs" not in serialized


def test_v2_evidence_separates_definition_implementation_and_actor_readiness() -> None:
    report = CapabilityDriftCompiler(CapabilityCatalog.bootstrap()).compile(
        LegacyCapabilitySource(
            agent_id="osa_1",
            source_kind="team",
            source_version="7",
            listing_capability_ids=("cap_data_query",),
            tool_refs=(),
            skill_refs=(),
        ),
    )

    payload = report.to_consumer_dict(
        readiness=CapabilityReadinessContext(
            agent_runnable=True,
            checked_at="2026-09-07T10:00:00+00:00",
        ),
        include_management=True,
    )
    capability = payload["capabilities"][0]

    assert payload["schema_version"] == 2
    assert capability["definition"] == {"lifecycle": "active", "version": 1}
    assert capability["implementation_evidence"] == {
        "status": "shadow",
        "reviewed_at": None,
        "evidence_level": "derived_unverified",
    }
    assert capability["readiness"] == {
        "state": "unknown",
        "checked_at": "2026-09-07T10:00:00+00:00",
        "reason_codes": ["implementation_not_reviewed"],
    }
    assert capability["management"] == {
        "implementation_ref": "capi_data_query_native@1",
        "implementation_lifecycle": "active",
        "missing_dependencies": [{"kind": "tool", "ref": "bi_query"}],
        "unresolved_dependencies": [
            {
                "kind": "connection",
                "ref": "datarc",
                "reason_code": "connection_readiness_not_evaluated",
            },
        ],
    }


def test_reviewed_catalog_evidence_does_not_upgrade_a_legacy_binding_to_ready(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    implementation.update(
        {
            "review_state": "reviewed",
            "reviewed_at": "2026-09-07T09:00:00Z",
            "eval_suite": "analysis-v1",
        },
    )
    (tmp_path / "reviewed.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation],
            },
        ),
        encoding="utf-8",
    )
    report = CapabilityDriftCompiler(CapabilityCatalog.bootstrap(tmp_path)).compile(
        LegacyCapabilitySource(
            agent_id="osa_reviewed",
            source_kind="team",
            source_version="1",
            listing_capability_ids=("cap_analysis",),
            tool_refs=(),
            skill_refs=(),
        ),
    )

    capability = report.to_consumer_dict(
        readiness=CapabilityReadinessContext(
            agent_runnable=True,
            checked_at="2026-09-07T10:00:00+00:00",
        ),
    )["capabilities"][0]

    assert capability["implementation_evidence"] == {
        "status": "reviewed",
        "reviewed_at": "2026-09-07T09:00:00+00:00",
        "evidence_level": "catalog_reviewed",
    }
    assert capability["readiness"]["state"] == "unknown"
    assert capability["readiness"]["reason_codes"] == ["no_exact_capability_binding"]


def test_runtime_routing_and_drift_compiler_share_capability_inference() -> None:
    catalog = CapabilityCatalog.bootstrap()
    source = LegacyCapabilitySource(
        agent_id="osa_powerbi",
        source_kind="team",
        source_version="3",
        listing_capability_ids=(),
        tool_refs=("teammcp.ibt_powerbi.pbi_execute_dax",),
        skill_refs=(),
    )

    runtime_ids = agent_capabilities(source.tool_refs)
    report = CapabilityDriftCompiler(catalog).compile(source)

    assert runtime_ids == frozenset(report.inferred_capability_ids)
    assert "cap_data_query" in runtime_ids


def test_prompt_protocol_detection_returns_only_bounded_codes() -> None:
    secret = "internal-secret-sentence"
    result = detect_prompt_protocols(
        f'{secret}; call skill_invoke(name="canvas-pptx", inputs={{}}); '
        'then skill_invoke(name="python_sandbox", inputs={}); finally write_report(...)',
    )

    assert result == (
        "artifact_export_protocol_in_agent_prompt",
        "report_protocol_in_agent_prompt",
        "sandbox_protocol_in_agent_prompt",
    )
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("stored", ["python_sandbox", "python_exec"])
def test_a_contract_tool_is_met_by_the_name_the_runtime_runs(stored: str) -> None:
    """The compute contract declares ``python_exec``; the create flow only offers ``python_sandbox`` and the
    runtime runs either. Neither may be reported as a missing dependency (116 dogfood 2026-09-26)."""
    report = CapabilityDriftCompiler(CapabilityCatalog.bootstrap()).compile(
        LegacyCapabilitySource(
            agent_id="osa_1",
            source_kind="team",
            source_version="1",
            listing_capability_ids=("cap_compute",),
            tool_refs=(stored,),
            skill_refs=(),
        ),
    )
    payload = report.to_consumer_dict(
        readiness=CapabilityReadinessContext(agent_runnable=True, checked_at="2026-09-26T00:00:00+00:00"),
        include_management=True,
    )
    [capability] = [item for item in payload["capabilities"] if item["management"]["implementation_ref"] == "capi_compute_python_native@1"]
    assert capability["management"]["missing_dependencies"] == []


def test_a_renamed_tool_meets_the_contract_that_names_its_current_name() -> None:
    """Stored old names (``bi_ask_question``) run as ``bi_query``; the report must not call ``bi_query`` missing."""
    report = CapabilityDriftCompiler(CapabilityCatalog.bootstrap()).compile(
        LegacyCapabilitySource(
            agent_id="osa_1",
            source_kind="team",
            source_version="1",
            listing_capability_ids=("cap_data_query",),
            tool_refs=("bi_ask_question",),
            skill_refs=(),
        ),
    )
    payload = report.to_consumer_dict(
        readiness=CapabilityReadinessContext(agent_runnable=True, checked_at="2026-09-26T00:00:00+00:00"),
        include_management=True,
    )
    [capability] = [item for item in payload["capabilities"] if item["management"]["implementation_ref"] == "capi_data_query_native@1"]
    assert capability["management"]["missing_dependencies"] == []


def test_a_truly_missing_sandbox_is_reported_under_the_name_the_tools_page_lists() -> None:
    report = CapabilityDriftCompiler(CapabilityCatalog.bootstrap()).compile(
        LegacyCapabilitySource(
            agent_id="osa_1",
            source_kind="team",
            source_version="1",
            listing_capability_ids=("cap_compute",),
            tool_refs=(),
            skill_refs=(),
        ),
    )
    payload = report.to_consumer_dict(
        readiness=CapabilityReadinessContext(agent_runnable=True, checked_at="2026-09-26T00:00:00+00:00"),
        include_management=True,
    )
    missing = [dep for item in payload["capabilities"] for dep in item["management"]["missing_dependencies"]]
    assert {"kind": "tool", "ref": "python_sandbox"} in missing
    assert all(dep["ref"] != "python_exec" for dep in missing)
