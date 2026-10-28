"""Real publication metadata, current SQLite ACLs and the actual input preparer."""

from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_references import InputDocument, ReferenceNode
from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadataCache, build_reference_receiver_metadata
from bowhead_core.tool_registry import build_default_registry
from test_reference_start_preparation import activate, prepare, raw_document
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

from bowhead_core import chat_reference_catalog as catalog
from bowhead_core import reference_receiver_metadata as metadata

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


def release_cells(monkeypatch):
    original = catalog.reference_entry_support
    monkeypatch.setattr(
        catalog,
        "reference_entry_support",
        lambda entry: [
            cell.model_copy(update={"adapter_id": "initial-agent-skill", "adapter_version": 1, "supported": True, "reason_code": None})
            if entry == "ordinary" and cell.resource_kind in {"agent", "skill"}
            else cell
            for cell in original(entry)
        ],
    )


@pytest.mark.parametrize("receiver_case", ["admission_office_unused_sop"], indirect=True)
@pytest.mark.parametrize("entry_mode", ["ordinary", "plan"])
@pytest.mark.parametrize("selected", [False, True], ids=["picker", "selected-token"])
async def test_standalone_agent_requires_current_available_baseline(receiver_case, monkeypatch, entry_mode, selected):
    case = receiver_case
    activate(case)
    index = await build_reference_receiver_metadata(case.case.fixture.db_path)
    registry = build_default_registry()
    installed = case.sources.builtin_index()
    agent = next(node.resource for node in raw_document(case, skills=False).nodes if isinstance(node, ReferenceNode))
    access = AccessControlStore(case.case.fixture.db_path)

    async def access_factory():
        return access, await case.sources.access_factory()

    async def choice():
        result = await catalog.read_reference_catalog(
            case.case.fixture.db_path,
            team_id=case.scope.team_id,
            user_id=case.scope.user_id,
            query=catalog.ReferenceCatalogRequest(
                workspace_id=case.scope.workspace_id,
                trigger="@",
                entry_mode=entry_mode,
                selected_refs=[agent] if selected else [],
            ),
            builtin_skill_index=installed,
            builtin_skill_access=access_factory,
            receiver_index=index,
            initial_writer_available=True,
            tool_registry=registry,
        )
        assert {group.id for group in result.groups} == {"team_agents", "system_agents"}
        assert "receiver_metadata" not in result.model_dump_json()
        return next(item for group in result.groups for item in group.items if item.resource == agent)

    def no_files(*args, **kwargs):
        raise AssertionError("package IO during discovery")

    monkeypatch.setattr(Path, "read_bytes", no_files)
    monkeypatch.setattr(Path, "read_text", no_files)
    assert (await choice()).availability == "baseline"
    current = installed
    installed = replace(
        current,
        items=tuple(replace(item, revision="sha256:" + "f" * 64) if item.id == case.selected.id else item for item in current.items),
    )
    assert (await choice()).availability == "unavailable"
    installed = current
    assert (await choice()).availability == "baseline"
    await access.create_or_update_resource_grant(
        team_id=case.scope.team_id,
        resource_kind="skill",
        resource_id="unused",
        principal_type="user",
        principal_id=case.scope.user_id,
        permission="use",
        effect="deny",
    )
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    assert (await choice()).availability == "unavailable"


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_sop", "admission_extension_optional_unused_invoke"], indirect=True)
async def test_exact_current_choice_matches_actual_preparation_without_request_time_package_reads(receiver_case, monkeypatch):
    case = receiver_case
    activate(case)
    release_cells(monkeypatch)
    db_path = case.case.fixture.db_path
    index = await build_reference_receiver_metadata(db_path)
    agent = next(node.resource for node in raw_document(case, skills=False).nodes if isinstance(node, ReferenceNode))
    assert index.find(case.scope.team_id, agent) is not None
    assert index.find(case.scope.team_id, agent.model_copy(update={"release_id": "arl_other"})) is None
    assert index.find("team_other", agent) is None
    resource = case.optional or case.selected
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                *[node.model_dump(mode="python") for node in raw_document(case, skills=False).nodes],
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000091",
                    "resource": resource.model_dump(mode="python"),
                    "label": "method",
                    "parameters": {},
                },
            ],
        }
    )
    expected = await prepare(case, document=document)
    access = AccessControlStore(db_path)

    async def access_factory():
        return access, await case.sources.access_factory()

    async def read(*, trigger="/", selected_refs=None, **patch):
        return await catalog.read_reference_catalog(
            db_path,
            team_id=case.scope.team_id,
            user_id=case.scope.user_id,
            query=catalog.ReferenceCatalogRequest(
                workspace_id=case.scope.workspace_id, trigger=trigger, selected_refs=[agent] if selected_refs is None else selected_refs, **patch
            ),
            builtin_skill_index=case.sources.builtin_index(),
            builtin_skill_access=access_factory,
            receiver_index=index,
            initial_writer_available=True,
        )

    def no_files(*args, **kwargs):
        raise AssertionError("package IO during discovery")

    monkeypatch.setattr(Path, "read_bytes", no_files)
    monkeypatch.setattr(Path, "read_text", no_files)
    monkeypatch.setattr("bowhead_core.skill_read_material._capture", no_files)
    result = await read()
    chosen = next(item for group in result.groups for item in group.items if item.resource == resource)
    assert chosen.availability == ("readonly_extension" if case.optional else "baseline")
    assert chosen.availability in expected.required_skill_modes
    assert chosen.reason_code is None
    replacement = await read(trigger="@", selected_refs=[agent, resource])
    assert next(item for group in replacement.groups for item in group.items if item.resource == agent).availability == "baseline"
    assert all(key not in result.model_dump_json() for key in ("receiver_metadata", "metadata_entry_json", "system_prompt", "BODY_"))
    # Query filters do not remove hidden closure members from compatibility checks.
    assert (
        next(item for group in (await read(query=chosen.label)).groups for item in group.items if item.resource == resource).availability
        == chosen.availability
    )
    await access.create_or_update_resource_grant(
        team_id=case.scope.team_id,
        resource_kind="skill",
        resource_id="unused",
        principal_type="user",
        principal_id=case.scope.user_id,
        permission="use",
        effect="deny",
    )
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    assert next(item for group in (await read()).groups for item in group.items if item.resource == resource).availability == "unavailable"
    if case.optional:
        # An extension cannot use shadow mode to bypass a denied unselected baseline.
        monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
        assert next(item for group in (await read()).groups for item in group.items if item.resource == resource).availability == "unavailable"


@pytest.mark.parametrize("receiver_case", ["admission_extension_unused_invoke"], indirect=True)
async def test_receiver_cache_expires_and_cannot_match_a_different_database(receiver_case, monkeypatch):
    case = receiver_case
    activate(case)
    now = [10.0]
    monkeypatch.setattr(metadata, "monotonic", lambda: now[0])
    cache = ReferenceReceiverMetadataCache(case.case.fixture.db_path)
    assert not cache.current().items
    await cache.refresh()
    assert cache.current().items
    now[0] = 40.0
    assert not cache.current().items
    await cache.refresh()
    assert cache.current().items
    release_cells(monkeypatch)
    agent = next(node.resource for node in raw_document(case, skills=False).nodes if isinstance(node, ReferenceNode))
    access = AccessControlStore(case.case.fixture.db_path)

    async def access_factory():
        return access, await case.sources.access_factory()

    result = await catalog.read_reference_catalog(
        case.case.fixture.db_path,
        team_id=case.scope.team_id,
        user_id=case.scope.user_id,
        query=catalog.ReferenceCatalogRequest(workspace_id=case.scope.workspace_id, trigger="/", selected_refs=[agent]),
        builtin_skill_index=case.sources.builtin_index(),
        builtin_skill_access=access_factory,
        receiver_index=replace(cache.current(), db_path=Path("other.sqlite")),
        initial_writer_available=True,
    )
    assert all(item.availability == "unavailable" for group in result.groups for item in group.items)


@pytest.mark.parametrize("receiver_case", ["admission_optional_unused_invoke"], indirect=True)
async def test_registered_system_receiver_uses_its_exact_grants_for_method_selection(receiver_case, monkeypatch):
    from bowhead_core.system_agent_reference_metadata import build_system_agent_reference_metadata
    from test_system_reference_baseline import configure
    from test_system_reference_baseline import prepare as prepare_system

    case = receiver_case
    activate(case)
    release_cells(monkeypatch)
    document, profile = await configure(case, monkeypatch)
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                *[node.model_dump(mode="python") for node in document.nodes],
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000092",
                    "resource": case.selected.model_dump(mode="python"),
                    "label": "method",
                    "parameters": {},
                },
            ],
        }
    )
    admitted = await prepare_system(case, document)
    assert admitted.required_skill_modes == ("baseline",)
    agent = next(node.resource for node in document.nodes if isinstance(node, ReferenceNode) and node.resource.kind == "agent")
    index = await build_system_agent_reference_metadata(case.case.fixture.db_path, case.case.runner)
    access = AccessControlStore(case.case.fixture.db_path)

    async def access_factory():
        return access, await case.sources.access_factory()

    result = await catalog.read_reference_catalog(
        case.case.fixture.db_path,
        team_id=case.scope.team_id,
        user_id=case.scope.user_id,
        query=catalog.ReferenceCatalogRequest(workspace_id=case.scope.workspace_id, trigger="/", selected_refs=[agent]),
        builtin_skill_index=case.sources.builtin_index(),
        builtin_skill_access=access_factory,
        system_agent_index=index,
        initial_writer_available=True,
    )
    assert next(item for group in result.groups for item in group.items if item.resource == case.selected).availability == "baseline"
