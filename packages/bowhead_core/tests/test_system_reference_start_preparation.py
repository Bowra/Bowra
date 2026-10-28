"""Raw system references use the actual registered profile and current policy."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.agent_execution_binding import AgentExecutionBindingError
from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride, BuiltinSubagentOverrideStore
from bowhead_core.chat_reference_preparation import ReferenceStartPreparationError, prepare_reference_system_start_admission
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_reference_storage import ReferenceScope
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import PROFILES, SubagentRunner
from bowhead_core.tool_registry import build_default_registry
from test_reference_primary_reader import _prepare_case, _read
from test_reference_primary_runtime import _ctx, _rows, _runtime
from test_reference_start_admission import _assert_no_acceptance
from test_reference_start_admission import start_case as _start_case
from test_subagent import _router

from bowhead_core import agent_execution_binding as binding
from bowhead_core import chat_reference_preparation as preparation
from bowhead_router import LLMResponse

start_case = _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["system"], indirect=True)]


@pytest.fixture
async def system_case(start_case, request, monkeypatch):
    if getattr(request, "param", None) == "startup":
        from bowhead_core.agents.registry import AgentRegistry
        from bowhead_core.agents.turn_runner_v2_entry import bootstrap_multi_agent

        # The gateway registers all installed manifests into PROFILES at boot.
        # Restore the process singleton after the test, including new entries.
        for name in AgentRegistry.bootstrap().list_all():
            monkeypatch.setitem(PROFILES, name, PROFILES.get(name))
        bootstrap_multi_agent(build_default_registry())
    case = await _prepare_case(start_case)
    case.from_startup = getattr(request, "param", None) == "startup"
    # The old admission fixture creates an artificial system hire. Product
    # system Agents are system-required; this path must work with no such row.
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("DELETE FROM agent_assignment WHERE agent_source_kind='system'")
    case.request_scope = ReferenceRequestScope(
        team_id=case.fixture.team_id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
    )
    return case


async def prepare(case, **patch):
    return await prepare_reference_system_start_admission(
        case.fixture.db_path,
        **{
            "input_document": case.args["reference_start"].input_document,
            "scope": case.request_scope,
            "runner": case.runner,
            **patch,
        },
    )


async def override(case, *, previous=None, **changes):
    value = replace(previous or BuiltinSubagentOverride(team_id=case.fixture.team_id, name="compute"), **changes)
    return await BuiltinSubagentOverrideStore(str(case.fixture.db_path)).upsert(
        value,
        expected_version=previous.row_version if previous else None,
        actor_user_id=case.fixture.owner_id,
        audit_action="test_override",
    )


async def accept(case, admission):
    case.args.update(reference_start=admission, user_text=admission.input_document.display_text())
    await case.store.finalize_started_chat_submission(**case.args)
    case.scope = ReferenceScope(
        **case.request_scope.model_dump(exclude={"session_id"}), session_id="s_reference", policy_epoch=admission.policy_epoch
    )


@pytest.mark.parametrize("system_case", ["default", "startup"], indirect=True)
async def test_system_required_input_reaches_real_runtime_without_hiring(system_case):
    case = system_case
    document = case.args["reference_start"].input_document
    document.nodes[0] = document.nodes[0].model_copy(update={"label": "SOME OTHER DISPLAY NAME"})
    admission = await prepare(case)
    assert admission.input_document == document and admission.system_required
    assert admission.expected.agent_id == "system:compute" and admission.expected.assignment_id is None
    assert admission.system_revision.definition == case.args["reference_start"].system_revision.definition
    if case.from_startup:
        from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
        from bowhead_core.agents.registry import AgentRegistry
        from bowhead_core.subagent import _system_profile_snapshot

        assert admission.system_revision.definition == _system_profile_snapshot(manifest_to_profile(AgentRegistry.bootstrap().get("compute")))
    _assert_no_acceptance(case)
    await accept(case, admission)
    primary = await _read(case)
    assert primary.binding.revision_ref == document.nodes[0].resource.version
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_assignment WHERE agent_source_kind='system'").fetchone() == (0,)
    runtime = await _runtime(case)
    runner = SubagentRunner(
        router=_router(LLMResponse(content="System primary completed.")),
        tool_registry=build_default_registry(),
        team_subagent_store=case.fixture.users,
    )
    result = await runtime.run(runner=runner, parent_ctx=_ctx(case), parent_bus=TurnEventBus())
    assert result.ok and result.final_answer == "System primary completed."
    assert len(_rows(case, "turn_agent_execution")) == 1
    assert _rows(case, "turn_agent_execution")[0]["status"] == "completed"
    assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]


async def test_system_agent_rejects_mcp_prompt_before_runtime_preparation(system_case):
    case = system_case
    raw = case.args["reference_start"].input_document.model_dump(mode="python")
    raw["nodes"].insert(
        1,
        {
            "type": "reference",
            "node_id": "00000000-0000-4000-8000-000000000099",
            "resource": {
                "kind": "mcp_prompt",
                "source_id": "ots_prompts",
                "prompt_name": "profitability",
                "local_contract_revision": "sha256:" + "1" * 64,
                "prompt_contract_revision": "sha256:" + "2" * 64,
                "connection_binding_id": "mcpb_prompt",
                "connection_subject_fingerprint": "sha256:" + "3" * 64,
            },
            "label": "Profitability",
            "parameters": {"period": "FY2025"},
        },
    )
    document = type(case.args["reference_start"].input_document).model_validate(raw)
    with pytest.raises(ReferenceStartPreparationError, match="reference_start_unsupported"):
        await prepare(case, input_document=document)
    _assert_no_acceptance(case)


async def test_current_override_is_exact_and_later_disable_blocks_saved_primary(system_case):
    case = system_case
    changed = await override(case, prompt_append="Use quarterly periods.", max_iterations=2)
    case.ctx.builtin_subagent_overrides = {"compute": changed}
    projected = await case.runner.project_system_behavior_candidate(profile_name="compute", parent_ctx=case.ctx)
    document = case.args["reference_start"].input_document
    node = document.nodes[0]
    document.nodes[0] = node.model_copy(update={"resource": node.resource.model_copy(update={"version": projected.revision_ref})})
    admission = await prepare(case)
    assert admission.override_version == changed.row_version and admission.system_revision.definition["max_iterations"] == 2
    assert "Use quarterly periods." in admission.system_revision.definition["system_prompt"]
    await accept(case, admission)
    assert (await _read(case)).binding.revision_ref == projected.revision_ref
    await override(case, previous=changed, enabled=False)
    with pytest.raises(AgentExecutionBindingError, match="agent_not_runnable"):
        await _read(case)


async def test_registry_precedence_is_shared_without_mutating_the_callers_runner(system_case, monkeypatch, tmp_path):
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.agents.registry import AgentRegistry

    case = system_case
    monkeypatch.setenv("BOWHEAD_SUBAGENT_SPEC_RESOLVE_ENABLED", "1")
    manifests = tmp_path / "registered-system"
    manifests.mkdir()
    (manifests / "compute.yaml").write_text(
        "name: compute\nmodel: analyst\ntrust_class: read-mostly\nmax_iterations: 2\n"
        "max_output_tokens: 2000\ndescription: Registered test analyst\ntools:\n  allow: [bi_query]\n"
    )
    case.runner._registry = AgentRegistry.bootstrap(system_dir=manifests)
    case.runner._shadow_workspace_id = "ws_original_caller"
    expected = manifest_to_profile(case.runner._registry.get("compute", case.request_scope.workspace_id))
    monkeypatch.setitem(PROFILES, "compute", replace(PROFILES["compute"], max_iterations=1))
    projected = await case.runner.project_system_behavior_candidate(profile_name="compute", parent_ctx=case.ctx)
    document = case.args["reference_start"].input_document
    node = document.nodes[0]
    document.nodes[0] = node.model_copy(update={"resource": node.resource.model_copy(update={"version": projected.revision_ref})})
    admission = await prepare(case)
    assert admission.system_revision.definition["max_iterations"] == expected.max_iterations != 1
    assert admission.system_revision.definition["system_prompt"] == expected.system_prompt
    assert case.runner._shadow_workspace_id == "ws_original_caller"


@pytest.mark.parametrize("change", ["disabled", "version", "membership", "foreign_scope", "unknown_source", "lead", "foreign_database"])
async def test_invalid_candidate_rejected_before_behavior_compilation(system_case, change, monkeypatch, tmp_path):
    case = system_case
    document = case.args["reference_start"].input_document
    patch = {}
    if change == "disabled":
        await override(case, enabled=False)
    elif change == "membership":
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute("UPDATE team_members SET status='removed'")
    elif change == "foreign_scope":
        patch["scope"] = case.request_scope.model_copy(update={"workspace_id": "ws_unrelated"})
    elif change == "foreign_database":
        patch["runner"] = replace(case.runner, team_subagent_store=type("Store", (), {"db_path": tmp_path / "other.db"})())
    else:
        resource = document.nodes[0].resource
        object.__setattr__(
            resource,
            "version" if change == "version" else "id",
            "arv_stale" if change == "version" else "system:lead" if change == "lead" else "system:missing",
        )
    compiled = []

    def forbidden(**kwargs):
        compiled.append(kwargs)
        raise AssertionError("unauthorized compilation")

    monkeypatch.setattr(binding, "project_system_agent_revision", forbidden)
    with pytest.raises(ReferenceStartPreparationError):
        await prepare(case, **patch)
    assert compiled == []
    _assert_no_acceptance(case)


@pytest.mark.parametrize("change", ["override", "membership", "profile"])
async def test_candidate_changes_during_compilation_are_rejected(system_case, change, monkeypatch):
    case = system_case
    original = binding.project_system_agent_revision

    def changing(**kwargs):
        result = original(**kwargs)
        if change == "profile":
            monkeypatch.setitem(PROFILES, "compute", replace(PROFILES["compute"], max_iterations=1))
        else:
            with sqlite3.connect(case.fixture.db_path) as db:
                if change == "override":
                    db.execute(
                        "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',1,1,'now','now')",
                        (case.fixture.team_id,),
                    )
                else:
                    db.execute("UPDATE team_members SET status='removed'")
        return result

    monkeypatch.setattr(binding, "project_system_agent_revision", changing)
    with pytest.raises(ReferenceStartPreparationError):
        await prepare(case)
    _assert_no_acceptance(case)


async def test_missing_old_hire_does_not_become_system_required(system_case):
    case = system_case
    assert not case.args["reference_start"].system_required
    with pytest.raises(AgentExecutionBindingError, match="agent_assignment_inactive"):
        await case.store.finalize_started_chat_submission(**case.args)
    _assert_no_acceptance(case)


async def test_new_override_between_preparation_and_finalization_rejects_atomically(system_case):
    case = system_case
    admission = await prepare(case)
    await override(case, max_iterations=1)
    case.args["reference_start"] = admission
    with pytest.raises(AgentExecutionBindingError, match="agent_system_definition_changed"):
        await case.store.finalize_started_chat_submission(**case.args)
    _assert_no_acceptance(case)


async def test_input_is_detached_before_first_await(system_case, monkeypatch):
    case = system_case
    original = preparation.open_shared_ctx
    document = case.args["reference_start"].input_document
    expected = document.model_copy(deep=True)

    def changed(*args, **kwargs):
        document.nodes[0] = document.nodes[0].model_copy(update={"label": "Changed after await"})
        return original(*args, **kwargs)

    monkeypatch.setattr(preparation, "open_shared_ctx", changed)
    admission = await prepare(case)
    assert admission.input_document == expected and admission.input_document != document
