"""M3-8 exact Capability attempt and Artifact grant contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_artifact_capability import (
    ArtifactCapabilityRegistry,
    ArtifactGenerationAttempt,
    ArtifactGenerationGrant,
    ArtifactWriteAttempt,
    ArtifactWriteGrant,
    CanvasArtifactFacade,
    ExactComponentRef,
    artifact_generation_denial,
    artifact_write_denial,
)
from bowhead_core.agent_capability_catalog import CapabilityCatalog
from bowhead_core.agent_capability_composition import (
    CapabilityCompilationContext,
    CapabilityCompositionCompiler,
    ExactToolBinding,
)
from bowhead_core.agent_capability_execution import (
    CapabilityExecutionError,
    CapabilityExecutionSelection,
    CapabilityExecutionStore,
)
from bowhead_core.agent_dependency_compiler import ExactSkillGrant
from bowhead_core.agent_execution_binding import (
    AgentExecutionBindingError,
    AgentExecutionBindingStore,
)
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore

_HASH = "sha256:" + "1" * 64


async def _admitted_system_execution(tmp_path: Path):
    db_path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    users = UserStore(db_path)
    sessions = SessionStore(db_path)
    await users.initialize()
    await sessions.initialize()
    team = await users.create_team(name="M3-8", max_seats=2)
    user = await users.create_user(team_id=team.id, email="m3-8@example.com")
    workspace_id = await users.create_local_workspace(
        team_id=team.id,
        name="M3-8 Space",
        creator_user_id=user.id,
    )
    session = await sessions.ensure_session(
        session_id="s_m3_8",
        workspace_id=workspace_id,
        user_id=user.id,
    )
    turn = await sessions.record_turn(
        turn_id="t_m3_8",
        session_id=session.id,
        user_text="compile exact capability",
    )
    binding = await AgentExecutionBindingStore(db_path).admit_system_execution(
        team_id=team.id,
        user_id=user.id,
        turn_id=turn.id,
        profile_name="compute",
        dispatch_key="call:m3-8",
        definition={
            "description": "M3-8 exact execution",
            "display_name": "Compute",
            "max_iterations": 3,
            "name": "compute",
            "system_prompt": "bounded",
            "tool_refs": [],
        },
        manifest_fingerprint=_HASH,
        override_version=None,
        system_required=True,
        enforce_current_override=False,
    )
    return db_path, team, user, session, turn, binding


def _selection(**overrides: object) -> CapabilityExecutionSelection:
    values: dict[str, object] = {
        "composition_node_id": "analysis",
        "attempt_generation": 1,
        "capability_id": "cap_analysis",
        "capability_version": 1,
        "implementation_id": "capi_analysis_reasoning",
        "implementation_version": 1,
        "adapter_id": None,
        "prompt_module_fingerprint": _HASH,
        "dependency_fingerprint": _HASH,
        "input_artifact_refs": ("art_input",),
        "output_contract": "findings.v1",
    }
    values.update(overrides)
    return CapabilityExecutionSelection(**values)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_exact_selection_is_idempotent_versioned_and_terminal(
    tmp_path: Path,
) -> None:
    db_path, _team, _user, _session, _turn, binding = await _admitted_system_execution(
        tmp_path,
    )
    store = CapabilityExecutionStore(db_path)

    first = await store.select(
        agent_execution_id=binding.execution_id,
        expected_agent_capability_fingerprint=binding.capability_fingerprint,
        selection=_selection(),
    )
    repeated = await store.select(
        agent_execution_id=binding.execution_id,
        expected_agent_capability_fingerprint=binding.capability_fingerprint,
        selection=_selection(),
    )
    assert repeated.execution_id == first.execution_id
    assert first.status == "selected"

    with pytest.raises(
        CapabilityExecutionError,
        match="capability_execution_identity_conflict",
    ):
        await store.select(
            agent_execution_id=binding.execution_id,
            expected_agent_capability_fingerprint=binding.capability_fingerprint,
            selection=_selection(implementation_id="capi_other"),
        )
    with pytest.raises(
        CapabilityExecutionError,
        match="capability_agent_execution_unavailable",
    ):
        await store.select(
            agent_execution_id=binding.execution_id,
            expected_agent_capability_fingerprint="sha256:" + "2" * 64,
            selection=_selection(attempt_generation=2),
        )

    execution_store = AgentExecutionBindingStore(db_path)
    with pytest.raises(
        CapabilityExecutionError,
        match="capability_agent_execution_not_running",
    ):
        await store.mark_running(first.execution_id)
    await execution_store.mark_running(binding.execution_id)
    await store.mark_running(first.execution_id)
    with pytest.raises(
        AgentExecutionBindingError,
        match="agent_capability_execution_active",
    ):
        await execution_store.mark_terminal(
            binding.execution_id,
            status="completed",
        )
    await store.mark_terminal(first.execution_id, status="completed")
    completed = await store.read(first.execution_id)
    assert completed is not None and completed.status == "completed"
    with pytest.raises(
        CapabilityExecutionError,
        match="capability_execution_attempt_terminal",
    ):
        await store.select(
            agent_execution_id=binding.execution_id,
            expected_agent_capability_fingerprint=binding.capability_fingerprint,
            selection=_selection(),
        )
    await execution_store.mark_terminal(binding.execution_id, status="completed")


def _reviewed_report_catalog(tmp_path: Path) -> CapabilityCatalog:
    tmp_path.mkdir()
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_report", 1).model_dump(mode="json")
    implementation = current.get_implementation(
        "capi_report_canvas",
        1,
    ).model_dump(mode="json")
    implementation["review_state"] = "reviewed"
    implementation["eval_suite"] = "report-contract-v1"
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


def _report_graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "fixed",
        "effect_envelope": "internal_artifact_write",
        "max_steps": 1,
        "max_cost_units": 10,
        "max_duration_seconds": 30,
        "inputs": [
            {
                "name": "findings",
                "contract": {"contract": "findings", "version": 1},
            },
        ],
        "outputs": [
            {
                "name": "report",
                "contract": {"contract": "report.document", "version": 1},
            },
        ],
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


@pytest.mark.anyio
async def test_canvas_plan_capability_ledger_and_write_grant_share_exact_id(
    tmp_path: Path,
) -> None:
    db_path, team, user, session, turn, binding = await _admitted_system_execution(
        tmp_path,
    )
    artifact_registry = ArtifactCapabilityRegistry.bootstrap()
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_report_catalog(tmp_path / "catalog"),
        artifacts=artifact_registry,
    )
    tool_revision = "sha256:" + "3" * 64
    compiled = compiler.compile_embedded(
        _report_graph(),
        revision=binding.revision_ref,
        context=CapabilityCompilationContext(
            tools=tuple(
                ExactToolBinding(ref=name, revision=tool_revision)
                for name in ("add_chart", "add_image", "read_report", "remove_chart", "write_report")
            ),
            skills=(),
            connections=(),
            available_checks=("canvas_ready", "report_target_ready"),
            available_output_grants=("report-writer-grant@1",),
        ),
    )
    assert compiled.status == "ready"
    plan = compiled.bindings[0].primary
    selected = await CapabilityExecutionStore(db_path).select(
        agent_execution_id=binding.execution_id,
        expected_agent_capability_fingerprint=binding.capability_fingerprint,
        selection=CapabilityExecutionSelection.from_plan(
            composition_node_id="report",
            plan=plan,
            input_artifact_refs=("art_findings",),
            output_contract="report.document.v1",
        ),
    )
    facade_plan = CanvasArtifactFacade(artifact_registry).plan(
        ExactComponentRef(id="canvas-report", version=1),
        "artifact_apply_patch",
    )
    assert selected.adapter_id == facade_plan.adapter_ref == "canvas-report@1"

    now = datetime.now(UTC)
    grant = ArtifactWriteGrant(
        team_id=team.id,
        user_id=user.id,
        session_id=session.id,
        turn_id=turn.id,
        agent_id=binding.agent_id,
        agent_revision=binding.revision_ref,
        execution_id=binding.execution_id,
        capability_execution_id=selected.execution_id,
        canvas_id="canvas_m3_8",
        canvas_generation=1,
        allowed_operations=("append_owned",),
        allowed_section_kinds=("analysis",),
        max_sections=2,
        max_bytes=1000,
        expires_at=now + timedelta(minutes=5),
    )
    attempt = ArtifactWriteAttempt(
        team_id=team.id,
        user_id=user.id,
        session_id=session.id,
        turn_id=turn.id,
        agent_id=binding.agent_id,
        agent_revision=binding.revision_ref,
        execution_id=binding.execution_id,
        capability_execution_id=selected.execution_id,
        canvas_id="canvas_m3_8",
        canvas_generation=1,
        operation="append_owned",
        section_kind="analysis",
        section_count=1,
        payload_bytes=100,
        owns_target_section=True,
        expected_revision="rev-1",
        current_revision="rev-1",
        now=now,
    )
    assert artifact_write_denial(grant, attempt) is None
    assert (
        artifact_write_denial(
            grant,
            replace(attempt, capability_execution_id="capx_other"),
        )
        == "artifact_grant_scope_mismatch"
    )


def _reviewed_office_catalog(
    tmp_path: Path,
    *,
    capability_id: str,
    skill_name: str,
) -> CapabilityCatalog:
    tmp_path.mkdir()
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition(capability_id, 1).model_dump(mode="json")
    skill_ref = f"skill://{skill_name}@{_HASH}"
    implementation = {
        "id": f"capi_test_{skill_name}_skill",
        "version": 1,
        "capability": {"id": capability_id, "version": 1},
        "lifecycle": "active",
        "required_tools": ["skill_read", "skill_invoke"],
        "optional_tools": [],
        "required_skills": [
            {"ref": skill_ref, "permission": "read"},
            {"ref": skill_ref, "permission": "invoke"},
        ],
        "required_connections": [],
        "risk_floor": "internal_artifact_write",
        "effect_class": "internal_artifact_write",
        "availability_checks": ["sandbox_ready"],
        "eval_suite": "office-contract-v1",
        "priority": 100,
        "owner": "platform-artifacts",
        "review_state": "reviewed",
    }
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


def _single_node_graph(
    *,
    capability_id: str,
    input_contract: str,
    output_contract: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "fixed",
        "effect_envelope": "internal_artifact_write",
        "max_steps": 1,
        "max_cost_units": 10,
        "max_duration_seconds": 30,
        "inputs": [
            {
                "name": "source",
                "contract": {"contract": input_contract, "version": 1},
            },
        ],
        "outputs": [
            {
                "name": "result",
                "contract": {"contract": output_contract, "version": 1},
            },
        ],
        "nodes": [
            {
                "id": "office-output",
                "kind": "capability",
                "capability": {"id": capability_id, "version": 1},
                "parameters": {},
            },
        ],
        "edges": [
            {
                "source": {"graph_input": "source"},
                "target": {"node": "office-output", "port": f"{input_contract}.v1"},
                "contract": {"contract": input_contract, "version": 1},
            },
            {
                "source": {"node": "office-output", "port": f"{output_contract}.v1"},
                "target": {"graph_output": "result"},
                "contract": {"contract": output_contract, "version": 1},
            },
        ],
    }


@pytest.mark.parametrize(
    ("capability_id", "skill_name", "input_contract", "output_contract", "extension"),
    [
        (
            "cap_presentation",
            "pptx",
            "presentation.brief",
            "presentation.document",
            ".pptx",
        ),
        (
            "cap_document",
            "docx",
            "document.source",
            "document.file",
            ".docx",
        ),
        (
            "cap_spreadsheet",
            "xlsx",
            "spreadsheet.source",
            "spreadsheet.document",
            ".xlsx",
        ),
    ],
)
@pytest.mark.anyio
async def test_office_generation_contract_is_capability_attempt_scoped(
    tmp_path: Path,
    capability_id: str,
    skill_name: str,
    input_contract: str,
    output_contract: str,
    extension: str,
) -> None:
    db_path, team, user, _session, turn, binding = await _admitted_system_execution(
        tmp_path,
    )
    compiler = CapabilityCompositionCompiler(
        catalog=_reviewed_office_catalog(
            tmp_path / "catalog",
            capability_id=capability_id,
            skill_name=skill_name,
        ),
        artifacts=ArtifactCapabilityRegistry.bootstrap(),
    )
    skill = ExactSkillGrant(
        kind="builtin",
        id=f"skill-{skill_name}",
        name=skill_name,
        permissions=("invoke", "read"),
        revision=_HASH,
        distribution="reference",
    )
    compiled = compiler.compile_embedded(
        _single_node_graph(
            capability_id=capability_id,
            input_contract=input_contract,
            output_contract=output_contract,
        ),
        revision=binding.revision_ref,
        context=CapabilityCompilationContext(
            tools=(
                ExactToolBinding(ref="skill_read", revision=_HASH),
                ExactToolBinding(ref="skill_invoke", revision=_HASH),
            ),
            skills=(skill,),
            connections=(),
            available_checks=("sandbox_ready",),
            available_output_grants=(),
        ),
    )
    assert compiled.status == "ready"
    selected = await CapabilityExecutionStore(db_path).select(
        agent_execution_id=binding.execution_id,
        expected_agent_capability_fingerprint=binding.capability_fingerprint,
        selection=CapabilityExecutionSelection.from_plan(
            composition_node_id="office-output",
            plan=compiled.bindings[0].primary,
            input_artifact_refs=("art_source",),
            output_contract=f"{output_contract}.v1",
        ),
    )
    now = datetime.now(UTC)
    grant = ArtifactGenerationGrant(
        team_id=team.id,
        user_id=user.id,
        turn_id=turn.id,
        agent_id=binding.agent_id,
        agent_revision=binding.revision_ref,
        execution_id=binding.execution_id,
        capability_execution_id=selected.execution_id,
        input_artifact_ids=("art_source",),
        allowed_extensions=(extension,),
        workdir="/sandbox/capx",
        validators=("office_structure",),
        max_files=1,
        expires_at=now + timedelta(minutes=5),
    )
    attempt = ArtifactGenerationAttempt(
        team_id=team.id,
        user_id=user.id,
        turn_id=turn.id,
        agent_id=binding.agent_id,
        agent_revision=binding.revision_ref,
        execution_id=binding.execution_id,
        capability_execution_id=selected.execution_id,
        input_artifact_ids=("art_source",),
        output_paths=(f"/sandbox/capx/result{extension}",),
        validators=("office_structure",),
        now=now,
    )
    assert artifact_generation_denial(grant, attempt) is None
    assert (
        artifact_generation_denial(
            grant,
            replace(attempt, capability_execution_id="capx_other"),
        )
        == "artifact_generation_scope_mismatch"
    )
