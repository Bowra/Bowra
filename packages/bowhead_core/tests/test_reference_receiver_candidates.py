"""Skill-first metadata uses the real Lead roster, current ACLs and full closure.

No candidate is a grant or a persisted receiver selection. Actual admission is
still a separate boundary; these tests do not claim a no-@ execution occurred.
"""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agents.lead_orchestrator import _gather_lead_peers
from bowhead_core.chat_references import AgentResourceRef, CapabilityResourceRef, InputDocument, ReferenceNode
from bowhead_core.reference_receiver_candidates import (
    read_reference_receiver_candidates,
    select_reference_receiver,
)
from bowhead_core.reference_receiver_metadata import build_reference_receiver_metadata
from bowhead_router.usage import current_usage_context
from test_reference_start_admission import _assert_no_acceptance
from test_reference_start_preparation import activate, raw_document
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

from bowhead_core import chat_reference_catalog as catalog
from bowhead_router import LLMResponse

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


def skill_document(case, resources=None):
    return InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {"type": "text", "text": "分析盈利能力，保持我的参数。"},
                *[
                    {
                        "type": "reference",
                        "node_id": f"00000000-0000-4000-8000-{i:012d}",
                        "resource": resource.model_dump(mode="python"),
                        "label": "财务方法",
                        "parameters": {},
                    }
                    for i, resource in enumerate(resources or [case.optional or case.selected], start=1)
                ],
            ],
        }
    )


async def discovery(case):
    activate(case)
    peers = await _gather_lead_peers(
        registry=None,
        workspace_id=case.scope.workspace_id,
        custom_store=None,
        user_id=case.scope.user_id,
        team_id=case.scope.team_id,
        team_subagent_store=case.case.runner.team_subagent_store,
    )
    assert [(peer.source, peer.agent_id, peer.name) for peer in peers] == [("team", "osa_exact", "exact-agent")]
    access = AccessControlStore(case.case.fixture.db_path)

    async def access_factory():
        return access, await case.sources.access_factory()

    kwargs = {
        "input_document": skill_document(case),
        "scope": case.scope,
        "lead_peers": peers,
        "builtin_skill_index": case.sources.builtin_index(),
        "builtin_skill_access": access_factory,
        "receiver_index": await build_reference_receiver_metadata(case.case.fixture.db_path),
    }

    async def read(**patch):
        return await read_reference_receiver_candidates(case.case.fixture.db_path, **{**kwargs, **patch})

    return read, kwargs, access


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop", "admission_extension_optional_unused_invoke"], indirect=True)
async def test_skill_only_candidates_preserve_input_and_do_not_depend_on_catalog_pages(receiver_case, monkeypatch):
    case = receiver_case
    read, kwargs, _ = await discovery(case)
    original = kwargs["input_document"].model_dump(mode="json")

    def no_files(*args, **kwargs):
        raise AssertionError("package IO during receiver discovery")

    monkeypatch.setattr(Path, "read_bytes", no_files)
    monkeypatch.setattr(Path, "read_text", no_files)
    monkeypatch.setattr(catalog, "PAGE_SIZE", 1)
    result = await read()
    assert len(result.candidates) == 1
    choice = result.candidates[0]
    agent = next(node.resource for node in raw_document(case, skills=False).nodes if isinstance(node, ReferenceNode))
    assert choice.resource == agent and choice.profile_name == "exact-agent"
    assert choice.required_skill_modes == (("baseline", "readonly_extension") if case.optional else ("baseline",))
    assert result.for_choice(agent, input_document=kwargs["input_document"], scope=case.scope) == choice
    assert kwargs["input_document"].model_dump(mode="json") == original
    assert all(not isinstance(node.resource, AgentResourceRef) for node in kwargs["input_document"].nodes if isinstance(node, ReferenceNode))
    _assert_no_acceptance(case.case)
    # A later edition or a different hire cycle cannot reuse this planning result.
    edited = kwargs["input_document"].model_copy(deep=True)
    edited.nodes[-1].parameters["year"] = 2025
    for resource, document, scope in [
        (agent, edited, case.scope),
        (agent, kwargs["input_document"], case.scope.model_copy(update={"workspace_id": "ws_other"})),
        (agent.model_copy(update={"assignment_id": "aas_other"}), kwargs["input_document"], case.scope),
    ]:
        with pytest.raises(ValueError, match="reference_receiver_"):
            result.for_choice(resource, input_document=document, scope=scope)


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True)
async def test_lead_receiver_selection_is_exact_and_fails_closed(receiver_case):
    case = receiver_case
    read, kwargs, _ = await discovery(case)
    discovered = await read()
    first = discovered.candidates[0]
    second = replace(
        first,
        resource=first.resource.model_copy(
            update={
                "id": "osa_second",
                "assignment_id": "aas_second",
            }
        ),
        label="Second analyst",
    )
    candidates = replace(discovered, candidates=(first, second))

    class Router:
        def __init__(self, content):
            self.content = content
            self.calls = []

        async def invoke(self, agent_type, request):
            self.calls.append((agent_type, request, current_usage_context()))
            return LLMResponse(content=self.content)

    router = Router('{"candidate_index":1}')
    selection_scope = {
        "team_id": case.scope.team_id,
        "user_id": case.scope.user_id,
        "workspace_id": case.scope.workspace_id,
    }
    selected = await select_reference_receiver(
        candidates,
        task_text=kwargs["input_document"].text_content(),
        router=router,
        trace_id="reference-receiver:test",
        **selection_scope,
    )
    assert selected == second.resource
    assert router.calls[0][0] == "lead"
    request = router.calls[0][1]
    usage = router.calls[0][2]
    assert (usage.team_id, usage.user_id, usage.workspace_id, usage.reason) == (
        case.scope.team_id,
        case.scope.user_id,
        case.scope.workspace_id,
        "reference_receiver_selection",
    )
    assert request.response_format == {"type": "json_object"}
    assert request.metadata == {"prompt_version": "reference-receiver-selection@1", "candidate_count": 2}
    assert first.resource.id not in request.messages[0].content
    for invalid in ('{"candidate_index":true}', '{"candidate_index":2}', '{"candidate_index":0,"reason":"x"}', "not-json"):
        with pytest.raises(ValueError, match="reference_receiver_selection_invalid"):
            await select_reference_receiver(
                candidates,
                task_text="盈利分析",
                router=Router(invalid),
                trace_id="reference-receiver:test",
                **selection_scope,
            )
    no_call = Router("invalid")
    assert (
        await select_reference_receiver(
            discovered,
            task_text="盈利分析",
            router=no_call,
            trace_id="reference-receiver:test",
            **selection_scope,
        )
        == first.resource
    )
    assert no_call.calls == []


@pytest.mark.parametrize("receiver_case", ["admission_extension_optional_unused_invoke"], indirect=True)
async def test_candidates_require_one_current_receiver_for_every_selected_method(receiver_case, monkeypatch):
    case = receiver_case
    read, kwargs, access = await discovery(case)
    document = skill_document(case, [case.selected, case.optional])
    assert len((await read(input_document=document)).candidates) == 1
    assert not (await read(lead_peers=[])).candidates
    peer = kwargs["lead_peers"][0]
    assert not (await read(lead_peers=[replace(peer, agent_id="osa_other")])).candidates
    assert not (await read(lead_peers=[replace(peer, name="stale-profile-name")])).candidates
    assert not (await read(receiver_index=None)).candidates
    assert not (await read(receiver_index=replace(kwargs["receiver_index"], db_path=Path("other.db")))).candidates
    with pytest.raises(ValueError, match="roster_invalid"):
        await read(lead_peers=[peer, peer])
    unknown = case.optional.model_copy(update={"version": "sha256:" + "f" * 64})
    assert not (await read(input_document=skill_document(case, [case.selected, unknown]))).candidates
    await access.create_or_update_resource_grant(
        team_id=case.scope.team_id,
        resource_kind="skill",
        resource_id="unused",
        principal_type="user",
        principal_id=case.scope.user_id,
        permission="use",
        effect="deny",
    )
    assert not (await read(input_document=document)).candidates
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
    assert not (await read(input_document=document)).candidates


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True)
@pytest.mark.parametrize("change", ["assignment", "membership", "agent"])
async def test_current_roster_and_scope_revocation_override_cached_candidates(receiver_case, change):
    case = receiver_case
    read, _, _ = await discovery(case)
    assert (await read()).candidates
    with sqlite3.connect(case.case.fixture.db_path) as db:
        db.execute(
            {
                "assignment": "UPDATE agent_assignment SET personal_state='disabled'",
                "membership": "UPDATE workspace_members SET status='removed'",
                "agent": "UPDATE team_subagent SET status='disabled'",
            }[change]
        )
    if change == "membership":
        with pytest.raises(catalog.ReferenceCatalogError, match="reference_scope_unavailable"):
            await read()
    else:
        assert not (await read()).candidates


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_invoke"], indirect=True)
async def test_separate_system_receivers_cannot_collectively_satisfy_a_single_primary(receiver_case, monkeypatch):
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import PROFILES
    from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata
    from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
    from test_system_reference_baseline import configure

    case = receiver_case
    read, _, _ = await discovery(case)
    _, profile = await configure(case, monkeypatch)
    for name, required_names in [("compute", {"method", "support"}), ("consultant", {"unused"})]:
        grants = tuple(grant for grant in profile.skill_runtime_grants if grant.name in required_names)
        monkeypatch.setitem(
            PROFILES,
            name,
            replace(
                profile,
                name=name,
                skill_runtime_grants=grants,
                skill_runtime_scope=ExactSkillRuntimeScope(
                    read_names=tuple(sorted(grant.name for grant in grants)),
                    invoke_names=tuple(sorted(grant.name for grant in grants if "invoke" in grant.permissions)),
                ),
            ),
        )
    peers = await _gather_lead_peers(
        registry=AgentRegistry.bootstrap(),
        workspace_id=case.scope.workspace_id,
        custom_store=None,
        user_id=case.scope.user_id,
        team_id=case.scope.team_id,
    )
    peers = [peer for peer in peers if peer.agent_id in {"system:compute", "system:consultant"}]
    assert len(peers) == 2
    index = await build_system_agent_reference_metadata(case.case.fixture.db_path, case.case.runner)

    async def choices(resources):
        return await read(input_document=skill_document(case, resources), lead_peers=peers, system_agent_index=index)

    assert [item.resource.id for item in (await choices([case.selected])).candidates] == ["system:compute"]
    assert [item.resource.id for item in (await choices([case.unused])).candidates] == ["system:consultant"]
    assert not (await choices([case.selected, case.unused])).candidates
    assert not (await read(lead_peers=[], system_agent_index=index)).candidates


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True)
async def test_awaiting_access_cannot_change_captured_user_input_or_lead_roster(receiver_case):
    case = receiver_case
    read, kwargs, _ = await discovery(case)
    original = kwargs["input_document"].model_copy(deep=True)
    access_factory = kwargs["builtin_skill_access"]

    async def racing_access():
        kwargs["input_document"].nodes[-1].parameters["year"] = 2025
        kwargs["lead_peers"].clear()
        return await access_factory()

    result = await read(builtin_skill_access=racing_access)
    assert len(result.candidates) == 1
    choice = result.candidates[0]
    assert result.for_choice(choice.resource, input_document=original, scope=case.scope) == choice
    with pytest.raises(ValueError, match="reference_receiver_input_changed"):
        result.for_choice(choice.resource, input_document=kwargs["input_document"], scope=case.scope)


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop"], indirect=True)
async def test_discovery_rejects_non_skill_input_before_access(receiver_case):
    case = receiver_case
    read, _, _ = await discovery(case)

    async def no_access():
        raise AssertionError("unsupported input reached authorization")

    for document in [
        raw_document(case, skills=False),
        InputDocument.model_validate({"schema_version": 1, "nodes": [{"type": "text", "text": "hello"}]}),
    ]:
        with pytest.raises(ValueError, match="reference_receiver_input_unsupported"):
            await read(input_document=document, builtin_skill_access=no_access)


@pytest.mark.parametrize("receiver_case", ["admission_capability_skill"], indirect=True)
async def test_automatic_receiver_must_satisfy_capability_and_skill_together(receiver_case):
    case = receiver_case
    read, _, _ = await discovery(case)
    document = skill_document(case)
    document.nodes.insert(
        0,
        ReferenceNode(
            type="reference",
            node_id="00000000-0000-4000-8000-000000000099",
            resource=CapabilityResourceRef(kind="capability", id="cap_visualize", version=1),
            label="Create visuals",
            parameters={},
        ),
    )
    result = await read(input_document=document)
    assert len(result.candidates) == 1
    assert result.candidates[0].required_skill_modes == ("baseline",)
    document.nodes[0] = document.nodes[0].model_copy(update={"resource": CapabilityResourceRef(kind="capability", id="cap_analysis", version=1)})
    assert not (await read(input_document=document)).candidates
