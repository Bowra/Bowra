from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bowhead_core.agent_artifact_capability import ArtifactCapabilityRegistry
from bowhead_core.agent_capability_catalog import CapabilityCatalog
from bowhead_core.agent_capability_composition import (
    CapabilityCompilationContext,
    CapabilityCompositionCompiler,
    ExactToolBinding,
)
from bowhead_core.agent_dependency_compiler import ExactSkillGrant


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


def _reviewed_catalog(
    tmp_path: Path,
    capability_id: str = "cap_analysis",
    implementation_id: str | None = None,
) -> CapabilityCatalog:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition(capability_id, 1).model_dump(mode="json")
    source = (
        current.get_implementation(implementation_id, 1)
        if implementation_id
        else current.implementations_for(current.get_definition(capability_id, 1))[0]
    )
    implementation = source.model_dump(mode="json")
    implementation["review_state"] = "reviewed"
    implementation["eval_suite"] = "contract-eval-v1"
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


def _context(**overrides: object) -> CapabilityCompilationContext:
    values: dict[str, object] = {
        "tools": (),
        "skills": (),
        "connections": (),
        "available_checks": ("model_ready",),
        "available_output_grants": (),
    }
    values.update(overrides)
    return CapabilityCompilationContext(**values)  # type: ignore[arg-type]


def test_embedded_and_workflow_sources_share_one_normalized_graph(tmp_path: Path) -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_catalog(tmp_path),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    graph = _analysis_graph()
    workflow = ExactSkillGrant(
        kind="team",
        id="workflow-1",
        name="quarterly-review",
        permissions=("read",),
        revision="sha256:" + "1" * 64,
        distribution="reference",
    )

    embedded = compiler.compile_embedded(graph, revision="agent-revision-1", context=_context())
    with pytest.raises(ValueError, match="not present in the exact compilation scope"):
        compiler.compile_workflow_skill(graph, workflow_skill=workflow, context=_context())
    workflow_compiled = compiler.compile_workflow_skill(graph, workflow_skill=workflow, context=_context(skills=(workflow,)))

    assert embedded.status == "ready"
    assert workflow_compiled.status == "ready"
    assert embedded.normalized_graph.fingerprint == workflow_compiled.normalized_graph.fingerprint
    assert embedded.fingerprint != workflow_compiled.fingerprint
    assert embedded.bindings[0].primary.implementation_ref == "capi_analysis_reasoning@1"


def test_fallback_cannot_expand_the_primary_tool_scope(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    primary = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    primary.update({"review_state": "reviewed", "eval_suite": "analysis-v1"})
    wider = {
        **primary,
        "id": "capi_analysis_wider",
        "priority": 90,
        "required_tools": ["extra_tool"],
    }
    (tmp_path / "catalog.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [primary, wider],
            },
        ),
        encoding="utf-8",
    )
    compiler = CapabilityCompositionCompiler(
        catalog=CapabilityCatalog.bootstrap(tmp_path),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )

    result = compiler.compile_embedded(
        _analysis_graph(),
        revision="agent-revision-1",
        context=_context(tools=(ExactToolBinding(ref="extra_tool", revision="sha256:" + "1" * 64),)),
    )

    assert result.status == "ready"
    assert result.bindings[0].primary.implementation_ref == "capi_analysis_reasoning@1"
    assert result.bindings[0].fallbacks == ()


def test_shadow_implementation_is_never_dispatch_eligible() -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=CapabilityCatalog.bootstrap(),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )

    result = compiler.compile_embedded(_analysis_graph(), revision="agent-revision-1", context=_context())

    assert result.status == "blocked"
    assert result.bindings == ()
    assert result.reason_codes == ("no_reviewed_implementation:cap_analysis@1",)


def test_single_selection_reuses_implementation_selection_without_a_graph(tmp_path: Path) -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_catalog(tmp_path),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )

    result = compiler.compile_single(
        {"id": "cap_analysis", "version": 1},
        revision="arv_single_1",
        effect_envelope="read",
        context=_context(),
    )

    assert result.status == "ready"
    assert result.source_kind == "single"
    assert result.bindings[0].node_id == "primary"
    assert result.bindings[0].primary.implementation_ref == "capi_analysis_reasoning@1"


def test_single_selection_never_promotes_shadow_implementation() -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=CapabilityCatalog.bootstrap(),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )

    result = compiler.compile_single(
        {"id": "cap_analysis", "version": 1},
        revision="arv_single_1",
        effect_envelope="read",
        context=_context(),
    )

    assert result.status == "blocked"
    assert result.bindings == ()
    assert result.reason_codes == ("no_reviewed_implementation:cap_analysis@1",)


def test_single_selection_blocks_external_write_without_approval_path(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    implementation.update(
        {
            "review_state": "reviewed",
            "eval_suite": "analysis-v1",
            "risk_floor": "external_write",
            "effect_class": "external_write",
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
    compiler = CapabilityCompositionCompiler(
        catalog=CapabilityCatalog.bootstrap(tmp_path),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )

    result = compiler.compile_single(
        {"id": "cap_analysis", "version": 1},
        revision="arv_single_1",
        effect_envelope="external_write",
        context=_context(),
    )

    assert result.status == "blocked"
    assert result.bindings[0].primary.implementation_ref == "capi_analysis_reasoning@1"
    assert result.reason_codes == ("approval_checkpoint_required:primary:cap_analysis@1",)


def test_graph_rejects_typed_mismatch_cycle_and_dead_nodes(tmp_path: Path) -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_catalog(tmp_path),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    mismatch = _analysis_graph()
    mismatch["edges"][0]["contract"] = {"contract": "other", "version": 1}  # type: ignore[index]
    with pytest.raises(ValueError, match="source contract mismatch"):
        compiler.normalize(mismatch)

    cycle = _analysis_graph()
    cycle["nodes"].append(  # type: ignore[union-attr]
        {
            "id": "approval",
            "kind": "approval_checkpoint",
            "approval_policy": "human-review",
            "inputs": [{"contract": "findings", "version": 1}],
            "outputs": [{"contract": "evidence", "version": 1}],
        },
    )
    cycle["edges"].extend(  # type: ignore[union-attr]
        [
            {
                "source": {"node": "analyze", "port": "findings.v1"},
                "target": {"node": "approval", "port": "findings.v1"},
                "contract": {"contract": "findings", "version": 1},
            },
            {
                "source": {"node": "approval", "port": "evidence.v1"},
                "target": {"node": "analyze", "port": "evidence.v1"},
                "contract": {"contract": "evidence", "version": 1},
            },
        ],
    )
    with pytest.raises(ValueError, match="acyclic"):
        compiler.normalize(cycle)

    dead = _analysis_graph()
    dead["nodes"].append(  # type: ignore[union-attr]
        {
            "id": "unused",
            "kind": "capability",
            "capability": {"id": "cap_analysis", "version": 1},
            "parameters": {},
        },
    )
    with pytest.raises(ValueError, match="do not contribute"):
        compiler.normalize(dead)


def test_dynamic_zone_requires_compatible_contracts() -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=CapabilityCatalog.bootstrap(),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    graph = _analysis_graph()
    graph["mode"] = "bounded_dynamic"
    graph["nodes"] = [
        {
            "id": "choose",
            "kind": "dynamic_zone",
            "allowed_capabilities": [
                {"id": "cap_analysis", "version": 1},
                {"id": "cap_compute", "version": 1},
            ],
            "max_steps": 2,
            "max_cost_units": 100,
            "max_duration_seconds": 60,
        },
    ]
    graph["edges"] = [
        {
            "source": {"node": "choose", "port": "findings.v1"},
            "target": {"graph_output": "result"},
            "contract": {"contract": "findings", "version": 1},
        },
    ]

    with pytest.raises(ValueError, match="dynamic zone contracts are incompatible"):
        compiler.normalize(graph)

    graph["nodes"][0]["allowed_capabilities"] = [{"id": "cap_analysis", "version": 1}]  # type: ignore[index]
    graph["nodes"][0]["max_steps"] = 5  # type: ignore[index]
    with pytest.raises(ValueError, match="exceeds the composition budget"):
        compiler.normalize(graph)
    graph["nodes"][0]["max_steps"] = 2  # type: ignore[index]
    graph["mode"] = "hybrid"
    with pytest.raises(ValueError, match="requires a fixed node"):
        compiler.normalize(graph)


def test_capability_parameters_are_checked_against_closed_definition_schema(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    definition["parameters_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"depth": {"type": "integer", "minimum": 1, "maximum": 3}},
        "required": ["depth"],
    }
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    implementation.update({"review_state": "reviewed", "eval_suite": "analysis-v1"})
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
    compiler = CapabilityCompositionCompiler(
        catalog=CapabilityCatalog.bootstrap(tmp_path),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    graph = _analysis_graph()
    graph["nodes"][0]["parameters"] = {"undeclared": True}  # type: ignore[index]

    with pytest.raises(ValueError, match="parameters do not match"):
        compiler.normalize(graph)
    graph["nodes"][0]["parameters"] = {"depth": 4}  # type: ignore[index]
    with pytest.raises(ValueError, match="parameters do not match"):
        compiler.normalize(graph)
    graph["nodes"][0]["parameters"] = {"depth": 2}  # type: ignore[index]
    assert compiler.compile_embedded(graph, revision="agent-revision-1", context=_context()).status == "ready"


def test_exact_skill_implementation_produces_per_plan_runtime_scope(tmp_path: Path) -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_catalog(tmp_path, "cap_compute", "capi_compute_python_skill"),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    graph = {
        "schema_version": 1,
        "mode": "fixed",
        "effect_envelope": "compute",
        "max_steps": 4,
        "max_cost_units": 100,
        "max_duration_seconds": 60,
        "inputs": [{"name": "request", "contract": {"contract": "compute.request", "version": 1}}],
        "outputs": [{"name": "result", "contract": {"contract": "findings", "version": 1}}],
        "nodes": [
            {
                "id": "compute",
                "kind": "capability",
                "capability": {"id": "cap_compute", "version": 1},
                "parameters": {},
            },
        ],
        "edges": [
            {
                "source": {"graph_input": "request"},
                "target": {"node": "compute", "port": "compute.request.v1"},
                "contract": {"contract": "compute.request", "version": 1},
            },
            {
                "source": {"node": "compute", "port": "findings.v1"},
                "target": {"graph_output": "result"},
                "contract": {"contract": "findings", "version": 1},
            },
        ],
    }
    revision = "sha256:e2d7f394067841d22b54d89f8f5e2af296a1e2a883bc41640dba8cdfda707e9b"
    skill = ExactSkillGrant(
        kind="builtin",
        id="builtin:python_sandbox",
        name="python_sandbox",
        permissions=("invoke", "read"),
        revision=revision,
        distribution="reference",
    )
    tools = (
        ExactToolBinding(ref="skill_invoke", revision="native-authoring-v1"),
        ExactToolBinding(ref="skill_read", revision="sha256:" + "2" * 64),
    )

    result = compiler.compile_embedded(
        graph,
        revision="agent-revision-1",
        context=_context(tools=tools, skills=(skill,), available_checks=("sandbox_ready",)),
    )

    assert result.status == "ready"
    plan = result.bindings[0].primary
    assert plan.skill_scope.read_names == ("python_sandbox",)
    assert plan.skill_scope.invoke_names == ("python_sandbox",)
    assert [item.ref for item in plan.tools] == ["skill_invoke", "skill_read"]


@pytest.mark.unit
def test_plan_uses_only_required_skill_permissions(tmp_path: Path) -> None:
    _reviewed_catalog(tmp_path, "cap_compute", "capi_compute_python_skill")
    path = tmp_path / "catalog.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    implementation = data["implementations"][0]
    implementation["required_skills"] = [requirement for requirement in implementation["required_skills"] if requirement["permission"] == "read"]
    assert len(implementation["required_skills"]) == 1
    requirement = implementation["required_skills"][0]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    skill = ExactSkillGrant(
        kind="builtin",
        id="builtin:python_sandbox",
        name="python_sandbox",
        revision=requirement["ref"].split("@", 1)[1],
        permissions=("invoke", "read"),
        distribution="reference",
    )
    compiler = CapabilityCompositionCompiler(catalog=CapabilityCatalog.bootstrap(tmp_path), artifacts=ArtifactCapabilityRegistry.bootstrap())
    result = compiler.compile_single(
        {"id": "cap_compute", "version": 1},
        revision="arv_read_only",
        effect_envelope="compute",
        context=_context(
            tools=tuple(ExactToolBinding(ref=ref, revision="native-v1") for ref in ("skill_read", "skill_invoke")),
            skills=(skill,),
            available_checks=("sandbox_ready",),
        ),
    )
    assert result.status == "ready", result.reason_codes
    plan = result.bindings[0].primary
    assert plan.skills[0].permissions == ("read",)
    assert plan.skill_scope.read_names == ("python_sandbox",)
    assert plan.skill_scope.invoke_names == ()


def test_report_plan_contains_adapter_delegates_prompt_and_exact_output_grant(tmp_path: Path) -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_catalog(tmp_path, "cap_report"),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    graph = {
        "schema_version": 1,
        "mode": "fixed",
        "effect_envelope": "internal_artifact_write",
        "max_steps": 4,
        "max_cost_units": 100,
        "max_duration_seconds": 60,
        "inputs": [{"name": "findings", "contract": {"contract": "findings", "version": 1}}],
        "outputs": [{"name": "report", "contract": {"contract": "report.document", "version": 1}}],
        "nodes": [
            {
                "id": "report",
                "kind": "capability",
                "capability": {"id": "cap_report", "version": 1},
                "parameters": {},
            },
        ],
        "edges": [
            {
                "source": {"graph_input": "findings"},
                "target": {"node": "report", "port": "findings.v1"},
                "contract": {"contract": "findings", "version": 1},
            },
            {
                "source": {"node": "report", "port": "report.document.v1"},
                "target": {"graph_output": "report"},
                "contract": {"contract": "report.document", "version": 1},
            },
        ],
    }
    tools = tuple(
        ExactToolBinding(ref=name, revision="sha256:" + str(index) * 64)
        for index, name in enumerate(("add_chart", "add_image", "read_report", "remove_chart", "write_report"), start=1)
    )

    result = compiler.compile_embedded(
        graph,
        revision="agent-revision-1",
        context=_context(
            tools=tools,
            available_checks=("canvas_ready", "report_target_ready"),
            available_output_grants=("report-writer-grant@1",),
        ),
    )

    assert result.status == "ready"
    plan = result.bindings[0].primary
    assert [item.ref for item in plan.tools] == ["add_chart", "add_image", "read_report", "remove_chart", "write_report"]
    assert plan.artifact_adapter_ref == "canvas-report@1"
    assert plan.artifact_contract_fingerprint is not None
    assert plan.output_grant_ref == "report-writer-grant@1"
    assert plan.prompt_bundle_fingerprint is not None
    assert plan.fingerprint.startswith("sha256:")


def test_effect_envelope_and_exact_dependencies_fail_closed(tmp_path: Path) -> None:
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_catalog(tmp_path, "cap_report"),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    graph = _analysis_graph()
    graph["nodes"][0]["capability"] = {"id": "cap_report", "version": 1}  # type: ignore[index]

    with pytest.raises(ValueError, match="source port is unknown"):
        compiler.normalize(graph)

    report_graph = {
        "schema_version": 1,
        "mode": "fixed",
        "effect_envelope": "read",
        "max_steps": 4,
        "max_cost_units": 100,
        "max_duration_seconds": 60,
        "inputs": [{"name": "findings", "contract": {"contract": "findings", "version": 1}}],
        "outputs": [{"name": "report", "contract": {"contract": "report.document", "version": 1}}],
        "nodes": [
            {
                "id": "report",
                "kind": "capability",
                "capability": {"id": "cap_report", "version": 1},
                "parameters": {},
            },
        ],
        "edges": [
            {
                "source": {"graph_input": "findings"},
                "target": {"node": "report", "port": "findings.v1"},
                "contract": {"contract": "findings", "version": 1},
            },
            {
                "source": {"node": "report", "port": "report.document.v1"},
                "target": {"graph_output": "report"},
                "contract": {"contract": "report.document", "version": 1},
            },
        ],
    }
    result = compiler.compile_embedded(report_graph, revision="agent-revision-1", context=_context())
    assert result.status == "blocked"
    assert result.bindings == ()
    assert any(reason.startswith("effect_envelope_exceeded") for reason in result.reason_codes)
    assert any(reason.startswith("tool_dependency_unavailable") for reason in result.reason_codes)


def test_external_effect_requires_a_downstream_approval_checkpoint(tmp_path: Path) -> None:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_team_write", 1).model_dump(mode="json")
    implementation = current.get_implementation("capi_analysis_reasoning", 1).model_dump(mode="json")
    implementation.update(
        {
            "id": "capi_team_write_test",
            "capability": {"id": "cap_team_write", "version": 1},
            "risk_floor": "external_write",
            "effect_class": "external_write",
            "review_state": "reviewed",
            "eval_suite": "team-write-contract-v1",
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
    compiler = CapabilityCompositionCompiler(
        catalog=CapabilityCatalog.bootstrap(tmp_path),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    graph = {
        "schema_version": 1,
        "mode": "fixed",
        "effect_envelope": "external_write",
        "max_steps": 3,
        "max_cost_units": 100,
        "max_duration_seconds": 60,
        "inputs": [{"name": "command", "contract": {"contract": "team.command", "version": 1}}],
        "outputs": [{"name": "receipt", "contract": {"contract": "team.receipt", "version": 1}}],
        "nodes": [
            {
                "id": "write",
                "kind": "capability",
                "capability": {"id": "cap_team_write", "version": 1},
                "parameters": {},
            },
        ],
        "edges": [
            {
                "source": {"graph_input": "command"},
                "target": {"node": "write", "port": "team.command.v1"},
                "contract": {"contract": "team.command", "version": 1},
            },
            {
                "source": {"node": "write", "port": "team.receipt.v1"},
                "target": {"graph_output": "receipt"},
                "contract": {"contract": "team.receipt", "version": 1},
            },
        ],
    }
    without_approval = compiler.compile_embedded(graph, revision="agent-revision-1", context=_context())
    assert without_approval.status == "blocked"
    assert without_approval.reason_codes == ("approval_checkpoint_required:write:cap_team_write@1",)

    graph["nodes"].append(  # type: ignore[union-attr]
        {
            "id": "approval",
            "kind": "approval_checkpoint",
            "approval_policy": "team-write-approval",
            "inputs": [{"contract": "team.receipt", "version": 1}],
            "outputs": [{"contract": "team.receipt", "version": 1}],
        },
    )
    graph["edges"][1] = {  # type: ignore[index]
        "source": {"node": "write", "port": "team.receipt.v1"},
        "target": {"node": "approval", "port": "team.receipt.v1"},
        "contract": {"contract": "team.receipt", "version": 1},
    }
    graph["edges"].append(  # type: ignore[union-attr]
        {
            "source": {"node": "approval", "port": "team.receipt.v1"},
            "target": {"graph_output": "receipt"},
            "contract": {"contract": "team.receipt", "version": 1},
        },
    )
    with_approval = compiler.compile_embedded(graph, revision="agent-revision-1", context=_context())
    assert with_approval.status == "ready"

    graph["outputs"].append(  # type: ignore[union-attr]
        {"name": "unapproved", "contract": {"contract": "team.receipt", "version": 1}},
    )
    graph["edges"].append(  # type: ignore[union-attr]
        {
            "source": {"node": "write", "port": "team.receipt.v1"},
            "target": {"graph_output": "unapproved"},
            "contract": {"contract": "team.receipt", "version": 1},
        },
    )
    bypass = compiler.compile_embedded(graph, revision="agent-revision-1", context=_context())
    assert bypass.status == "blocked"
    assert bypass.reason_codes == ("approval_checkpoint_required:write:cap_team_write@1",)
