from __future__ import annotations

import json
import sqlite3

import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.chat_reference_catalog import ReferenceCatalogRequest, read_reference_catalog
from bowhead_core.chat_references import AgentResourceRef, CapabilityResourceRef, SkillResourceRef
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataCache, build_reference_mcp_metadata
from bowhead_core.reference_receiver_metadata import (
    ReferenceReceiverMetadata,
    ReferenceReceiverMetadataIndex,
    ReferenceTeamToolBaseline,
    TeamReferenceReceiverMetadata,
)
from bowhead_core.skill_instance_store import SkillInstanceStore
from test_agent_admission_transaction import _team_admission_case

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Finance", "version": "1"},
    "paths": {
        "/margin/{year}": {
            "get": {
                "operationId": "getMargin",
                "summary": "Read margin",
                "parameters": [
                    {
                        "name": "year",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer", "minimum": 2000},
                    }
                ],
            },
            "post": {
                "operationId": "setMargin",
                "summary": "Write margin",
            },
        }
    },
}


def _insert_source(
    db_path,
    *,
    team_id: str,
    source_id: str,
    name: str = "Finance API",
    kind: str = "openapi",
    auth_type: str = "none",
    auth_ref: str | None = None,
    spec: dict | None = None,
    tools: list[dict] | None = None,
    prompts: list[dict] | None = None,
    selected: list[str] | None = None,
    visibility: str = "team_wide",
    created_by: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO team_tool_source(
              id,team_id,name,kind,openapi_spec_json,selected_op_ids_json,base_url,
              auth_type,auth_ref,tools_schema_json,visibility,created_by,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                team_id,
                name,
                kind,
                json.dumps(spec) if spec is not None else None,
                json.dumps(selected or []),
                f"https://{source_id}.example/api",
                auth_type,
                auth_ref,
                json.dumps(tools or []),
                visibility,
                created_by,
                "enabled",
                "2026-09-16T00:00:00+00:00",
                "2026-09-16T00:00:00+00:00",
            ),
        )
        if prompts is not None:
            db.execute(
                "UPDATE team_tool_source SET prompts_schema_json=? WHERE id=?",
                (json.dumps(prompts), source_id),
            )


@pytest.fixture
async def mcp_case(tmp_path):
    case = await _team_admission_case(tmp_path)
    await SkillInstanceStore(case.fixture.db_path).initialize()
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at='2026-09-16T00:00:00+00:00' WHERE singleton=1")
    return case


async def test_snapshot_separates_local_contract_subject_and_remote_version(mcp_case):
    fixture = mcp_case.fixture
    _insert_source(
        fixture.db_path,
        team_id=fixture.team_id,
        source_id="ots_finance",
        spec=_SPEC,
        selected=["getMargin", "setMargin"],
        created_by=fixture.owner_id,
    )
    index = await build_reference_mcp_metadata(fixture.db_path)
    (source,) = index.for_team(fixture.team_id)
    assert index.ready and not index.incomplete_teams
    assert source.label == "Finance API" and source.resource is not None
    assert source.resource.connection_binding_id == "ots_finance"
    assert source.resource.local_contract_revision.startswith("sha256:")
    assert source.resource.connection_subject_fingerprint.startswith("sha256:")
    assert source.remote_version.status == "unknown" and source.remote_version.value is None
    assert [operation.reviewed_read_contract for operation in source.operations] == [True, False]
    read, write = source.operations
    assert read.resource is not None and read.parameter_schema()["properties"]["year"]["minimum"] == 2000
    assert write.resource is not None


async def test_authenticated_subject_is_not_forged_from_ciphertext(mcp_case):
    fixture = mcp_case.fixture
    _insert_source(
        fixture.db_path,
        team_id=fixture.team_id,
        source_id="ots_auth",
        auth_type="bearer",
        auth_ref="FERNET:gAAAA-secret-token-ciphertext",
        spec=_SPEC,
        selected=["getMargin"],
    )
    first = await build_reference_mcp_metadata(fixture.db_path)
    (source,) = first.for_team(fixture.team_id)
    assert source.resource is None and source.operations[0].resource is None
    assert "secret-token" not in repr(first)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_tool_source SET auth_ref='FERNET:rotated' WHERE id='ots_auth'")
    second = await build_reference_mcp_metadata(fixture.db_path)
    assert second.for_team(fixture.team_id) == first.for_team(fixture.team_id)


async def test_schema_and_route_change_exact_contract_but_display_change_does_not(mcp_case):
    fixture = mcp_case.fixture
    _insert_source(
        fixture.db_path,
        team_id=fixture.team_id,
        source_id="ots_finance",
        spec=_SPEC,
        selected=["getMargin"],
    )
    initial = (await build_reference_mcp_metadata(fixture.db_path)).for_team(fixture.team_id)[0]
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_tool_source SET name='Renamed',tool_overrides_json='{}' WHERE id='ots_finance'")
    renamed = (await build_reference_mcp_metadata(fixture.db_path)).for_team(fixture.team_id)[0]
    assert renamed.resource == initial.resource
    changed_spec = json.loads(json.dumps(_SPEC))
    changed_spec["paths"]["/margin/{year}"]["get"]["parameters"][0]["schema"]["minimum"] = 2020
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_tool_source SET openapi_spec_json=? WHERE id='ots_finance'", (json.dumps(changed_spec),))
    changed = (await build_reference_mcp_metadata(fixture.db_path)).for_team(fixture.team_id)[0]
    assert changed.resource.local_contract_revision != initial.resource.local_contract_revision
    assert changed.operations[0].resource.operation_contract_revision != initial.operations[0].resource.operation_contract_revision
    assert changed.resource.connection_subject_fingerprint == initial.resource.connection_subject_fingerprint


async def test_remote_mcp_hint_remains_unreviewed_and_unsupported_schema_has_no_tool_ref(mcp_case):
    fixture = mcp_case.fixture
    _insert_source(
        fixture.db_path,
        team_id=fixture.team_id,
        source_id="ots_remote",
        kind="mcp",
        tools=[
            {
                "name": "search",
                "description": "Search",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            },
            {
                "name": "unsupported",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
            },
        ],
    )
    source = (await build_reference_mcp_metadata(fixture.db_path)).for_team(fixture.team_id)[0]
    assert source.resource is not None
    assert source.operations[0].resource is not None and not source.operations[0].reviewed_read_contract
    assert source.operations[1].resource is None and source.operations[1].parameter_schema() is None


async def test_mcp_prompt_cache_projects_exact_metadata_without_expanding_content(mcp_case):
    fixture = mcp_case.fixture
    with sqlite3.connect(fixture.db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(team_tool_source)")}
        if "prompts_schema_json" not in columns:
            db.execute(
                "ALTER TABLE team_tool_source ADD COLUMN prompts_schema_json TEXT NOT NULL DEFAULT '[]'",
            )
    _insert_source(
        fixture.db_path,
        team_id=fixture.team_id,
        source_id="ots_prompts",
        kind="mcp",
        tools=[],
        prompts=[
            {
                "name": "profitability",
                "title": "Profitability",
                "description": "Explain profitability",
                "arguments": [
                    {
                        "name": "period",
                        "description": "Reporting period",
                        "required": True,
                    },
                    {
                        "name": "locale",
                        "description": "",
                        "required": False,
                    },
                ],
            },
        ],
    )

    index = await build_reference_mcp_metadata(fixture.db_path)

    assert index.ready and index.prompt_cache_ready
    (source,) = index.for_team(fixture.team_id)
    (prompt,) = source.prompts
    assert prompt.id.startswith("mcpprompt.ots_prompts.")
    assert prompt.prompt_name == "profitability"
    assert prompt.label == "Profitability"
    assert prompt.resource is not None
    assert prompt.resource.prompt_name == "profitability"
    assert prompt.resource.prompt_contract_revision.startswith("sha256:")
    assert prompt.parameter_schema() == {
        "type": "object",
        "properties": {
            "period": {
                "type": "string",
                "description": "Reporting period",
            },
            "locale": {"type": "string"},
        },
        "required": ["period"],
        "additionalProperties": False,
    }
    access = AccessControlStore(fixture.db_path)
    epoch = await access.get_policy_epoch(fixture.team_id)

    async def access_factory():
        return access, AccessContext(
            user_id=fixture.member_id,
            team_id=fixture.team_id,
            workspace_id=fixture.workspace_id,
            workspace_role="member",
            team_role="member",
            policy_epoch=epoch,
        )

    catalog = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(
            workspace_id=fixture.workspace_id,
            trigger="/",
        ),
        builtin_skill_access=access_factory,
        mcp_index=index,
        initial_writer_available=True,
    )
    prompt_item = next(item for item in catalog.groups[2].items if item.kind == "mcp_prompt")
    assert prompt_item.resource == prompt.resource
    assert prompt_item.availability == "unavailable"
    assert prompt_item.reason_code == "unsupported_entry"
    prompt_support = next(cell for cell in catalog.entry_support if cell.resource_kind == "mcp_prompt")
    assert not prompt_support.supported

    for revision, expected in ((prompt.resource.prompt_contract_revision, [prompt_item]), ("sha256:" + "0" * 64, [])):
        exact = await read_reference_catalog(
            fixture.db_path,
            team_id=fixture.team_id,
            user_id=fixture.member_id,
            query=ReferenceCatalogRequest(
                workspace_id=fixture.workspace_id,
                trigger="/",
                exact_resource=prompt.resource.model_copy(update={"prompt_contract_revision": revision}),
            ),
            builtin_skill_access=access_factory,
            mcp_index=index,
            initial_writer_available=True,
        )
        assert [item for group in exact.groups for item in group.items] == expected
        assert exact.next_cursor is None


async def test_catalog_projects_current_visibility_and_requires_mcp_writer_readiness(mcp_case, monkeypatch):
    fixture = mcp_case.fixture
    _insert_source(
        fixture.db_path,
        team_id=fixture.team_id,
        source_id="ots_finance",
        spec=_SPEC,
        selected=["getMargin", "setMargin"],
        created_by=fixture.member_id,
    )
    _insert_source(
        fixture.db_path,
        team_id=fixture.team_id,
        source_id="ots_private",
        name="Private API",
        spec=_SPEC,
        selected=["getMargin"],
        visibility="creator_only",
        created_by=fixture.owner_id,
    )
    index = await build_reference_mcp_metadata(fixture.db_path)
    access = AccessControlStore(fixture.db_path)
    epoch = await access.get_policy_epoch(fixture.team_id)

    async def access_factory():
        return access, AccessContext(
            user_id=fixture.member_id,
            team_id=fixture.team_id,
            workspace_id=fixture.workspace_id,
            workspace_role="member",
            team_role="member",
            policy_epoch=epoch,
        )

    result = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/"),
        builtin_skill_access=access_factory,
        mcp_index=index,
    )
    mcp = result.groups[2]
    assert mcp.error is None
    assert [item.kind for item in mcp.items] == ["mcp_source", "mcp_tool", "mcp_tool"]
    assert all(item.availability == "unavailable" for item in mcp.items)
    assert mcp.items[0].reason_code == "unsupported_entry"
    assert mcp.items[1].reason_code == "unsupported_entry"
    assert mcp.items[2].reason_code == "unsupported_binding"
    assert all(not cell.supported for cell in result.entry_support if cell.resource_kind.startswith("mcp_"))
    assert "ots_private" not in result.model_dump_json()

    enabled_agent_skill_entry = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/"),
        builtin_skill_access=access_factory,
        mcp_index=index,
        initial_writer_available=True,
    )
    enabled_mcp = enabled_agent_skill_entry.groups[2]
    assert [(item.availability, item.reason_code) for item in enabled_mcp.items] == [
        ("unavailable", "unsupported_entry"),
        ("unavailable", "unsupported_entry"),
        ("unavailable", "unsupported_binding"),
    ]

    from bowhead_core import chat_reference_catalog as catalog

    original_support = catalog.reference_entry_support

    def mcp_support(entry_mode):
        return [
            cell.model_copy(
                update={
                    "adapter_id": "initial-mcp-read",
                    "adapter_version": 1,
                    "supported": True,
                    "reason_code": None,
                }
            )
            if entry_mode == "ordinary" and cell.resource_kind in {"mcp_source", "mcp_tool"}
            else cell
            for cell in original_support(entry_mode)
        ]

    monkeypatch.setattr(catalog, "reference_entry_support", mcp_support)
    agent = AgentResourceRef(
        kind="agent",
        source_kind="team",
        id=mcp_case.expected.agent_id,
        version=mcp_case.expected.revision_ref,
        assignment_id=mcp_case.expected.assignment_id,
        release_id=mcp_case.expected.release_id,
    )
    receiver_index = ReferenceReceiverMetadataIndex(
        fixture.db_path,
        (
            TeamReferenceReceiverMetadata(
                fixture.team_id,
                agent.id,
                agent.version,
                agent.release_id,
                ReferenceReceiverMetadata(
                    (),
                    team_tools=(
                        ReferenceTeamToolBaseline(
                            source_id="ots_finance",
                            operation_name="teammcp.ots_finance.getMargin",
                            source_revision="2026-09-16T00:00:00+00:00",
                            risk="read",
                            approval_required=False,
                        ),
                    ),
                ),
            ),
        ),
    )
    no_agent_entry = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/", selected_refs=[agent]),
        builtin_skill_access=access_factory,
        receiver_index=receiver_index,
        mcp_index=index,
    )
    assert all(item.availability == "unavailable" for item in no_agent_entry.groups[2].items)
    released = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/", selected_refs=[agent]),
        builtin_skill_access=access_factory,
        receiver_index=receiver_index,
        mcp_index=index,
        initial_writer_available=True,
        mcp_writer_available=True,
    )
    assert [(item.availability, item.reason_code) for item in released.groups[2].items] == [
        ("baseline", None),
        ("baseline", None),
        ("unavailable", "unsupported_binding"),
    ]
    selected_tool = released.groups[2].items[1].resource
    assert selected_tool is not None
    blocked_by_skill = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(
            workspace_id=fixture.workspace_id,
            trigger="/",
            selected_refs=[
                agent,
                SkillResourceRef(
                    kind="skill",
                    source_kind="builtin",
                    id="builtin:dupont",
                    version="sha256:" + "a" * 64,
                ),
            ],
        ),
        builtin_skill_access=access_factory,
        receiver_index=receiver_index,
        mcp_index=index,
        initial_writer_available=True,
        mcp_writer_available=True,
    )
    assert all(item.availability == "unavailable" for item in blocked_by_skill.groups[2].items)
    blocked_capability = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(
            workspace_id=fixture.workspace_id,
            trigger="/",
            selected_refs=[agent, selected_tool],
        ),
        builtin_skill_access=access_factory,
        receiver_index=receiver_index,
        mcp_index=index,
        initial_writer_available=True,
        capability_writer_available=True,
        mcp_writer_available=True,
    )
    assert all(
        item.availability == "unavailable"
        for group in blocked_capability.groups
        for item in group.items
        if isinstance(getattr(item, "resource", None), CapabilityResourceRef)
    )
    duplicate = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/", selected_refs=[agent, selected_tool]),
        builtin_skill_access=access_factory,
        receiver_index=receiver_index,
        mcp_index=index,
        initial_writer_available=True,
        mcp_writer_available=True,
    )
    assert next(item for item in duplicate.groups[2].items if item.resource == selected_tool).availability == "unavailable"
    agent_choices = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="@", selected_refs=[selected_tool]),
        builtin_skill_access=access_factory,
        receiver_index=receiver_index,
        mcp_index=index,
        initial_writer_available=True,
        mcp_writer_available=True,
    )
    assert next(item for item in agent_choices.groups[0].items if item.resource == agent).availability == "baseline"
    stale_receiver = ReferenceReceiverMetadataIndex(
        fixture.db_path,
        (
            TeamReferenceReceiverMetadata(
                fixture.team_id,
                agent.id,
                agent.version,
                agent.release_id,
                ReferenceReceiverMetadata(
                    (),
                    team_tools=(
                        ReferenceTeamToolBaseline(
                            source_id="ots_finance",
                            operation_name="teammcp.ots_finance.getMargin",
                            source_revision="2026-09-15T00:00:00+00:00",
                            risk="read",
                            approval_required=False,
                        ),
                    ),
                ),
            ),
        ),
    )
    stale = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/", selected_refs=[agent]),
        builtin_skill_access=access_factory,
        receiver_index=stale_receiver,
        mcp_index=index,
        initial_writer_available=True,
        mcp_writer_available=True,
    )
    assert all(item.availability == "unavailable" for item in stale.groups[2].items)

    monkeypatch.setattr(catalog, "resource_grants_enforce_enabled", lambda: True)
    await access.create_or_update_resource_grant(
        team_id=fixture.team_id,
        resource_kind="team_tool_op",
        resource_id="teammcp.ots_finance.getMargin",
        principal_type="user",
        principal_id=fixture.member_id,
        permission="use",
        effect="deny",
        created_by=fixture.owner_id,
    )
    epoch = await access.get_policy_epoch(fixture.team_id)
    denied = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/", selected_refs=[agent]),
        builtin_skill_access=access_factory,
        receiver_index=receiver_index,
        mcp_index=index,
        initial_writer_available=True,
        mcp_writer_available=True,
    )
    assert [(item.kind, item.availability, item.reason_code) for item in denied.groups[2].items] == [
        ("mcp_source", "unavailable", "unsupported_binding"),
        ("mcp_tool", "unavailable", "unsupported_binding"),
    ]

    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_tool_source SET status='disabled',updated_at='2026-09-16T00:01:00+00:00' WHERE id='ots_finance'")
    disabled = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/"),
        builtin_skill_access=access_factory,
        mcp_index=index,
        initial_writer_available=True,
        mcp_writer_available=True,
    )
    assert disabled.groups[2].items == []
    assert disabled.groups[2].error == "catalog_metadata_unavailable"
    assert all(not cell.supported for cell in disabled.entry_support if cell.resource_kind in {"mcp_source", "mcp_tool"})


async def test_expired_cache_fails_closed(mcp_case):
    cache = ReferenceMcpMetadataCache(mcp_case.fixture.db_path)
    assert not cache.current().ready and cache.current().items == ()
    await cache.refresh()
    assert cache.current().ready
