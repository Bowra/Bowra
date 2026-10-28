"""Real source preparation keeps the Lead's choice out of the user document."""

import json
import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.chat_reference_preparation import ReferenceStartPreparationError
from bowhead_core.chat_references import AgentResourceRef, ReferenceNode
from bowhead_core.reference_receiver_candidates import prepare_reference_receiver_choice
from test_reference_receiver_candidates import discovery, skill_document
from test_reference_start_admission import _assert_no_acceptance
from test_reference_start_preparation import prepare
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


async def chosen(case, candidates, document):
    return await prepare_reference_receiver_choice(
        case.case.fixture.db_path,
        candidates=candidates,
        resource=candidates.candidates[0].resource,
        input_document=document,
        scope=case.scope,
        sources=case.sources,
        runner=case.case.runner,
    )


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop", "admission_extension_optional_unused_invoke"], indirect=True)
async def test_chosen_receiver_uses_actual_preparation_without_fabricating_a_user_mention(receiver_case):
    case = receiver_case
    read, kwargs, _ = await discovery(case)
    candidates = await read()
    document = kwargs["input_document"]
    original = document.model_dump(mode="json")
    result = await chosen(case, candidates, document)
    assert result.automatic_receiver == candidates.candidates[0].resource
    assert result.input_document.model_dump(mode="json") == original == document.model_dump(mode="json")
    assert len(result.skill_reads.items) == 2
    assert any(b"BODY_support" in item.material.content for item in result.skill_reads.items)
    explicit = document.model_copy(deep=True)
    explicit.nodes.append(
        ReferenceNode(
            type="reference",
            node_id="00000000-0000-4000-8000-000000000099",
            resource=result.automatic_receiver,
            label="Analyst",
            parameters={},
        )
    )
    explicit_result = await prepare(case, document=explicit)
    assert result.skill_reads == explicit_result.skill_reads
    assert result.expected == explicit_result.expected
    assert result.required_skill_modes == explicit_result.required_skill_modes
    assert result.snapshot().automatic_receiver == result.automatic_receiver
    assert all(not isinstance(node.resource, AgentResourceRef) for node in result.input_document.nodes if isinstance(node, ReferenceNode))
    _assert_no_acceptance(case.case)
    # The same finalizer keeps the automatic origin out of the user source.
    case.case.args.update(reference_start=result, user_text=result.input_document.display_text())
    receipt = await case.case.store.finalize_started_chat_submission(**case.case.args)
    assert receipt.outcome == "started"
    with sqlite3.connect(case.case.fixture.db_path) as db:
        assert db.execute("SELECT binding_source FROM turn_agent_binding WHERE turn_id='t_reference'").fetchone() == ("lead",)
        assert db.execute("SELECT dispatch_source FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == ("lead",)
        assert json.loads(db.execute("SELECT input_document_json FROM chat_turn_user_sources WHERE turn_id='t_reference'").fetchone()[0]) == original


@pytest.mark.parametrize("receiver_case", ["admission_extension_optional_unused_invoke"], indirect=True)
async def test_discovery_is_not_a_reusable_permission_ticket(receiver_case):
    case = receiver_case
    read, kwargs, access = await discovery(case)
    candidates = await read()
    assert candidates.candidates
    await access.create_or_update_resource_grant(
        team_id=case.scope.team_id,
        resource_kind="skill",
        resource_id="unused",
        principal_type="user",
        principal_id=case.scope.user_id,
        permission="use",
        effect="deny",
    )
    with pytest.raises(ReferenceStartPreparationError):
        await chosen(case, candidates, kwargs["input_document"])
    _assert_no_acceptance(case.case)


@pytest.mark.parametrize("receiver_case", ["admission_unused_invoke"], indirect=True)
async def test_actual_choice_preparation_requires_selected_method_parameters(receiver_case):
    case = receiver_case
    read, _, _ = await discovery(case)
    document = skill_document(case, [case.selected, case.child])
    candidates = await read(input_document=document)
    assert candidates.candidates
    with pytest.raises(ReferenceStartPreparationError):
        await chosen(case, candidates, document)
    for node in document.nodes:
        if isinstance(node, ReferenceNode):
            node.parameters["year"] = 2026
    candidates = await read(input_document=document)
    result = await chosen(case, candidates, document)
    assert result.input_document == document
    # An automatic choice must not replace an explicit mention already in input.
    explicit = result.input_document.model_copy(deep=True)
    explicit.nodes.append(
        ReferenceNode(
            type="reference",
            node_id="00000000-0000-4000-8000-000000000099",
            resource=result.automatic_receiver,
            label="Analyst",
            parameters={},
        )
    )
    with pytest.raises(ValueError, match="reference_start_unsupported"):
        replace(result, input_document=explicit).snapshot()


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_invoke"], indirect=True)
async def test_system_choice_uses_the_actual_registered_revision_and_full_baseline(receiver_case, monkeypatch):
    from bowhead_core.agents.lead_orchestrator import _gather_lead_peers
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata
    from test_system_reference_baseline import configure

    case = receiver_case
    read, kwargs, _ = await discovery(case)
    await configure(case, monkeypatch)
    peers = await _gather_lead_peers(
        registry=AgentRegistry.bootstrap(),
        workspace_id=case.scope.workspace_id,
        custom_store=None,
        user_id=case.scope.user_id,
        team_id=case.scope.team_id,
    )
    index = await build_system_agent_reference_metadata(case.case.fixture.db_path, case.case.runner)
    candidates = await read(lead_peers=[peer for peer in peers if peer.agent_id == "system:compute"], system_agent_index=index)
    assert len(candidates.candidates) == 1
    result = await chosen(case, candidates, kwargs["input_document"])
    assert result.system_required and result.automatic_receiver.source_kind == "system"
    assert result.system_revision.revision_ref == candidates.candidates[0].resource.version
    assert result.input_document == kwargs["input_document"]
    assert len(result.skill_reads.items) == 2
    _assert_no_acceptance(case.case)
    case.case.args.update(reference_start=result, user_text=result.input_document.display_text())
    assert (await case.case.store.finalize_started_chat_submission(**case.case.args)).outcome == "started"
    with sqlite3.connect(case.case.fixture.db_path) as db:
        assert db.execute("SELECT binding_source,agent_source_kind FROM turn_agent_binding WHERE turn_id='t_reference'").fetchone() == (
            "lead",
            "system",
        )
        assert json.loads(
            db.execute("SELECT input_document_json FROM chat_turn_user_sources WHERE turn_id='t_reference'").fetchone()[0]
        ) == result.input_document.model_dump(mode="json")


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True)
async def test_changed_package_cannot_reuse_a_metadata_candidate(receiver_case):
    case = receiver_case
    read, kwargs, _ = await discovery(case)
    candidates = await read()
    assert candidates.candidates
    (case.root / "method" / "method.md").write_text("Changed source after candidate discovery")
    with pytest.raises(ReferenceStartPreparationError):
        await chosen(case, candidates, kwargs["input_document"])
    _assert_no_acceptance(case.case)


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True)
async def test_choice_checks_the_original_input_before_preparing_files(receiver_case, monkeypatch):
    case = receiver_case
    read, kwargs, _ = await discovery(case)
    candidates = await read()
    document = kwargs["input_document"].model_copy(deep=True)
    document.nodes[-1].parameters["year"] = 2025

    async def no_prepare(*args, **kwargs):
        raise AssertionError("changed input reached source preparation")

    monkeypatch.setattr(case.sources, "prepare", no_prepare)
    with pytest.raises(ValueError, match="reference_receiver_input_changed"):
        await chosen(case, candidates, document)
    _assert_no_acceptance(case.case)
