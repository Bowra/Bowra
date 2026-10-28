"""Actual SQLite/ACL metadata discovery, without reserving Agent execution."""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from bowhead_core.chat_reference_catalog import (
    ReferenceCatalogError,
    ReferenceCatalogGroup,
    ReferenceCatalogItem,
    ReferenceCatalogRequest,
    _catalog_response,
    read_reference_catalog,
    reference_entry_support,
)
from bowhead_core.reference_receiver_metadata import build_reference_receiver_metadata
from bowhead_core.tool_registry import build_default_registry
from test_agent_admission_transaction import _team_admission_case
from test_agent_execution_binding import _create_team_agent, _setup

from bowhead_core import chat_reference_catalog as catalog_module

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def catalog_case(tmp_path):
    from bowhead_core.skill_instance_store import SkillInstanceStore

    case = await _team_admission_case(tmp_path)
    await SkillInstanceStore(case.fixture.db_path).initialize()
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at='2026-09-13T00:00:00+00:00' WHERE singleton=1")
    return case


async def _read(case, **patch):
    fixture = case.fixture
    query = ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="@", **patch)
    return await read_reference_catalog(fixture.db_path, team_id=fixture.team_id, user_id=fixture.member_id, query=query)


async def test_hired_agent_uses_immutable_listing_and_exact_cycle_without_execution(catalog_case):
    case = catalog_case
    with sqlite3.connect(case.fixture.db_path) as db:
        before = db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone()
    result = await _read(case, session_id="s_m3b", query="eXaCt")
    (item,) = result.groups[0].items
    assert item.label == "Exact Agent" and item.summary == "Safe exact execution v1"
    assert item.resource.version == case.expected.revision_ref
    assert item.resource.assignment_id == case.expected.assignment_id
    assert item.resource.release_id == case.expected.release_id
    assert item.availability == "unavailable" and item.reason_code == "unsupported_entry"
    assert result.groups[1].error == "catalog_adapter_unavailable"
    assert "legacy prompt" not in result.model_dump_json()
    assert all(not cell.supported for cell in result.entry_support)
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution").fetchone() == before
        assert db.execute("SELECT COUNT(*) FROM chat_reference_plans").fetchone() == (0,)
    assert (await _read(case, query="missing")).groups[0].items == []


async def test_listing_with_a_blank_name_is_shown_by_routing_name_not_hidden(catalog_case):
    """116 (1.3.48): an Agent published with an empty display name was hidden from @ and the group said it
    "could not be shown for now", while the marketplace showed it by routing name. Same Agent, same rule."""
    case = catalog_case
    with sqlite3.connect(case.fixture.db_path) as db:
        agent_id, routing_name, revision_id = db.execute(
            "SELECT id, name, live_revision_id FROM team_subagent WHERE team_id=? AND live_revision_id IS NOT NULL", (case.fixture.team_id,)
        ).fetchone()
        (listing_json,) = db.execute("SELECT listing_json FROM team_subagent_revision WHERE id=?", (revision_id,)).fetchone()
        listing = json.loads(listing_json)
        listing["display_name"] = ""
        # Revisions are immutable in production; this test fabricates one published with a blank name.
        for (trigger,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='team_subagent_revision'").fetchall():
            db.execute(f'DROP TRIGGER "{trigger}"')
        db.execute("UPDATE team_subagent_revision SET listing_json=? WHERE id=?", (json.dumps(listing), revision_id))
    result = await _read(case)
    group = result.groups[0]
    assert group.error is None
    assert [(item.id, item.label) for item in group.items] == [(agent_id, routing_name)]


async def test_discovery_reads_metadata_only_and_never_writes(catalog_case, monkeypatch):
    original_open = catalog_module.open_shared_ctx
    forbidden = []

    def authorize(action, table, column, _database, _trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) or (
            action == sqlite3.SQLITE_READ and column in {"system_prompt", "definition_json", "descriptor_json", "auth_ref", "plan_json"}
        ):
            forbidden.append((action, table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @asynccontextmanager
    async def guarded_open(*args, **kwargs):
        async with original_open(*args, **kwargs) as db:
            await db.set_authorizer(authorize)
            yield db

    monkeypatch.setattr(catalog_module, "open_shared_ctx", guarded_open)
    result = await _read(catalog_case)
    assert len(result.groups[0].items) == 1 and result.groups[0].items[0].resource is not None
    assert forbidden == []


@pytest.mark.parametrize("changed", ["membership", "user", "workspace", "space_member", "session_owner", "session_deleted", "team"])
async def test_current_scope_is_checked_even_for_empty_or_unimplemented_groups(catalog_case, changed):
    case = catalog_case
    fixture = case.fixture
    with sqlite3.connect(fixture.db_path) as db:
        statements = {
            "membership": ("UPDATE team_members SET status='removed' WHERE user_id=?", fixture.member_id),
            "user": ("UPDATE users SET status='disabled' WHERE id=?", fixture.member_id),
            "workspace": ("UPDATE workspaces SET status='archived' WHERE id=?", fixture.workspace_id),
            "space_member": ("UPDATE workspace_members SET status='removed' WHERE user_id=?", fixture.member_id),
            "session_owner": ("UPDATE sessions SET user_id=? WHERE id='s_m3b'", fixture.owner_id),
            "session_deleted": ("UPDATE sessions SET deleted_at=? WHERE id='s_m3b'", "2026-09-13T00:00:00+00:00"),
            "team": ("UPDATE teams SET status='suspended' WHERE id=?", fixture.team_id),
        }
        sql, parameter = statements[changed]
        db.execute(sql, (parameter,))
    with pytest.raises(ReferenceCatalogError, match="reference_scope_unavailable"):
        await _read(case, session_id="s_m3b")


@pytest.mark.parametrize("changed", ["assignment", "security_deny", "owner"])
async def test_discovery_uses_current_assignment_and_canonical_access(catalog_case, changed):
    case = catalog_case
    assert len((await _read(case)).groups[0].items) == 1
    with sqlite3.connect(case.fixture.db_path) as db:
        if changed == "assignment":
            db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        elif changed == "owner":
            db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (case.fixture.owner_id,))
        else:
            db.execute(
                "INSERT INTO resource_grants(id,team_id,resource_kind,resource_id,principal_type,principal_id,permission,effect,created_at,updated_at) "
                "VALUES ('rg_catalog',?,'team_subagent','osa_exact','user',?,'use','deny','2026-09-13','2026-09-13')",
                (case.fixture.team_id, case.fixture.member_id),
            )
    assert (await _read(case)).groups[0].items == []


async def test_missing_cutover_is_not_compatibility_permission(catalog_case):
    with sqlite3.connect(catalog_case.fixture.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=NULL")
    result = await _read(catalog_case)
    assert result.groups[0].items == [] and result.groups[0].error == "unsupported_binding"


async def test_missing_descriptor_preserves_discovery_but_never_invents_reference(tmp_path):
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    # A valid revision created before descriptor backfill. Never delete an
    # immutable descriptor just to manufacture this supported incomplete state.
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at='2026-09-13T00:00:00+00:00'")
    (item,) = (await _read(SimpleNamespace(fixture=fixture))).groups[0].items
    assert item.label == "Exact Agent" and item.resource is None and item.reason_code == "unsupported_binding"


async def test_cursor_is_bound_to_scope_query_selection_and_current_metadata(catalog_case):
    first = await _read(catalog_case)
    cursor = base64.urlsafe_b64encode(
        json.dumps(
            {"generation": first.catalog_generation, "last": json.dumps(["team_agents", "agent", "team", "osa_exact"], separators=(",", ":"))}
        ).encode()
    ).decode()
    assert (await _read(catalog_case, cursor=cursor)).groups[0].items == []
    for patch in ({"query": "exact"}, {"entry_mode": "steer"}, {"selected_refs": [first.groups[0].items[0].resource]}):
        with pytest.raises(ReferenceCatalogError, match="reference_catalog_cursor_invalid"):
            await _read(catalog_case, cursor=cursor, **patch)
    with sqlite3.connect(catalog_case.fixture.db_path) as db:
        db.execute("UPDATE agent_assignment SET personal_state='disabled'")
    with pytest.raises(ReferenceCatalogError, match="reference_catalog_cursor_invalid"):
        await _read(catalog_case, cursor=cursor)


async def test_group_failure_does_not_hide_other_group_status(catalog_case):
    with sqlite3.connect(catalog_case.fixture.db_path) as db:
        db.execute("ALTER TABLE agent_assignment RENAME TO test_unavailable_assignment")
    result = await _read(catalog_case)
    assert [(group.id, group.error) for group in result.groups] == [
        ("team_agents", "catalog_metadata_unavailable"),
        ("system_agents", "catalog_adapter_unavailable"),
    ]


async def test_slash_and_entry_support_publish_only_released_cells(catalog_case, monkeypatch):
    for flag in ("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "BOWHEAD_SKILL_MULTIRUNTIME", "BOWHEAD_SKILL_INVOKE_COMMAND"):
        monkeypatch.setenv(flag, "1")
    fixture = catalog_case.fixture
    response = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=fixture.workspace_id, trigger="/"),
    )
    assert [group.id for group in response.groups] == ["skills", "capabilities", "mcp"]
    assert response.groups[0].error == "catalog_adapter_unavailable"
    assert response.groups[1].error is None
    assert any(item.id == "cap_visualize" for item in response.groups[1].items)
    assert response.groups[2].error == "catalog_adapter_unavailable"
    for mode in ("ordinary", "steer", "plan", "resume"):
        support = reference_entry_support(mode)
        assert len(support) == 7
        released = {(cell.resource_kind, cell.authorization_mode) for cell in support if cell.supported}
        assert (
            released
            == {
                "ordinary": {
                    ("agent", "baseline"),
                    ("capability", "baseline"),
                    ("mcp_source", "baseline"),
                    ("mcp_tool", "baseline"),
                    ("mcp_prompt", "baseline"),
                    ("skill", "baseline"),
                    ("skill", "readonly_extension"),
                },
                "steer": {
                    ("agent", "baseline"),
                    ("capability", "baseline"),
                    ("mcp_source", "baseline"),
                    ("mcp_tool", "baseline"),
                    ("mcp_prompt", "baseline"),
                    ("skill", "baseline"),
                    ("skill", "readonly_extension"),
                },
                "plan": {("agent", "baseline"), ("skill", "baseline"), ("skill", "readonly_extension")},
                "resume": set(),
            }[mode]
        )
        assert {cell.authorization_mode for cell in support if cell.resource_kind == "skill"} == {"baseline", "readonly_extension"}


@pytest.mark.parametrize("entry_mode", ["steer", "plan"])
async def test_released_catalog_projects_an_exact_agent(catalog_case, entry_mode):
    fixture = catalog_case.fixture
    response = await read_reference_catalog(
        fixture.db_path,
        team_id=fixture.team_id,
        user_id=fixture.member_id,
        query=ReferenceCatalogRequest(
            workspace_id=fixture.workspace_id,
            trigger="@",
            entry_mode=entry_mode,
        ),
        initial_writer_available=True,
        capability_writer_available=True,
        receiver_index=await build_reference_receiver_metadata(fixture.db_path),
        tool_registry=build_default_registry(),
    )
    (agent,) = response.groups[0].items
    if entry_mode == "plan":
        # This published fixture uses bi_query, whose effect contract is unknown.
        assert agent.availability == "unavailable" and agent.reason_code == "unsupported_binding"
    else:
        assert agent.availability == "baseline" and agent.reason_code is None
    expected = {
        ("agent", "baseline"),
        ("skill", "baseline"),
        ("skill", "readonly_extension"),
    }
    if entry_mode == "steer":
        expected.add(("capability", "baseline"))
    assert {(cell.resource_kind, cell.authorization_mode) for cell in response.entry_support if cell.supported} == expected


async def test_plan_catalog_requires_explicit_team_receiver_and_readonly_grants():
    from dataclasses import replace

    from bowhead_core.agent_dependency_compiler import ExactSkillGrant
    from bowhead_core.chat_reference_catalog import SkillCatalogItem
    from bowhead_core.chat_references import AgentResourceRef
    from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadata
    from test_reference_skill_compatibility import method, resource

    skill = resource("method")
    grant = ExactSkillGrant("builtin", skill.id, "method", ("read",), skill.version, "reference")
    agent = ReferenceCatalogItem(
        id="osa_analyst",
        label="Analyst",
        summary="Analysis",
        reason_code="unsupported_entry",
        resource=AgentResourceRef(kind="agent", source_kind="team", id="osa_analyst", version="arv_1", release_id="arl_1", assignment_id="asa_1"),
        receiver_metadata=ReferenceReceiverMetadata((grant,)),
    )
    metadata = method("method")
    methods = ReferenceCatalogGroup(
        id="skills",
        items=[
            SkillCatalogItem(
                id=skill.id,
                label="Method",
                summary="Read-only method",
                resource=skill,
                read_only_parameter_schema=json.loads(metadata.entry_json)["parameter_schema"],
                reason_code="unsupported_entry",
                metadata_name="method",
                metadata_entry_json=metadata.entry_json,
                metadata_strict_access=True,
            )
        ],
    )
    args = {"team_id": "team_one", "user_id": "u_one", "locale": "en-US", "epoch": 0, "initial_writer_available": True}
    for candidate, expected in (
        (agent, "baseline"),
        (agent.model_copy(update={"receiver_metadata": ReferenceReceiverMetadata((replace(grant, permissions=("invoke", "read")),))}), "unavailable"),
        (
            agent.model_copy(
                update={
                    "source_kind": "system",
                    "id": "system:analyst",
                    "resource": AgentResourceRef(kind="agent", source_kind="system", id="system:analyst", version="sar_1"),
                }
            ),
            "unavailable",
        ),
    ):
        response = _catalog_response(
            **args,
            query=ReferenceCatalogRequest(workspace_id="ws_one", trigger="@", entry_mode="plan"),
            groups=[ReferenceCatalogGroup(id="team_agents", items=[candidate])],
            compatibility_groups=[methods],
        )
        assert response.groups[0].items[0].availability == expected
    for selection, expected in (([], "unavailable"), ([agent.resource], "baseline")):
        response = _catalog_response(
            **args,
            query=ReferenceCatalogRequest(workspace_id="ws_one", trigger="/", entry_mode="plan", selected_refs=selection),
            groups=[methods],
            compatibility_groups=[ReferenceCatalogGroup(id="team_agents", items=[agent]), methods],
        )
        assert response.groups[0].items[0].availability == expected


@pytest.mark.parametrize("case", ["readonly", "missing_registry", "missing_tool", "unknown_effect", "approval"])
async def test_plan_catalog_uses_the_same_native_tool_effect_contract_as_send(case):
    from dataclasses import replace

    from bowhead_core.chat_reference_plan import require_readonly_definition
    from bowhead_core.chat_references import AgentResourceRef
    from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadata

    definition = {"tool_refs": ["attachment_read"], "skill_grants": []}
    registry = build_default_registry()
    if case == "missing_tool":
        definition["tool_refs"] = ["missing_tool"]
    if case in {"unknown_effect", "approval"}:
        spec = registry.get("attachment_read")
        registry.register(replace(spec, **({"effect_spec": None} if case == "unknown_effect" else {"needs_approval": True})))
    receiver = ReferenceReceiverMetadata.from_definition(definition)
    assert receiver.tool_refs == tuple(definition["tool_refs"])
    agent = ReferenceCatalogItem(
        id="osa_reader",
        label="Reader",
        summary="Read confirmed attachments",
        reason_code="unsupported_entry",
        resource=AgentResourceRef(kind="agent", source_kind="team", id="osa_reader", version="arv_1", release_id="arl_1", assignment_id="asa_1"),
        receiver_metadata=receiver,
    )
    if case == "readonly":
        require_readonly_definition(definition, registry)
    elif case != "missing_registry":
        with pytest.raises(ValueError, match="reference_plan_readonly_baseline_required"):
            require_readonly_definition(definition, registry)
    response = _catalog_response(
        team_id="team_one",
        user_id="u_one",
        locale="en-US",
        epoch=0,
        initial_writer_available=True,
        tool_registry=None if case == "missing_registry" else registry,
        query=ReferenceCatalogRequest(workspace_id="ws_one", trigger="@", entry_mode="plan"),
        groups=[ReferenceCatalogGroup(id="team_agents", items=[agent])],
    )
    assert response.groups[0].items[0].availability == ("baseline" if case == "readonly" else "unavailable")


async def test_metadata_paging_is_bounded_stable_and_scope_bound():
    # Pure pagination over finite metadata: SQL loading/access is exercised above.
    items = [
        ReferenceCatalogItem(id=f"osa_{i:02}", label=f"分析师 {i:02}", summary="利润 PROFIT", resource=None, reason_code="unsupported_binding")
        for i in range(25)
    ]
    groups = [ReferenceCatalogGroup(id="team_agents", items=items)]
    query = ReferenceCatalogRequest(workspace_id="ws_test", trigger="@", query="pRoFiT")
    args = {"team_id": "team_one", "user_id": "user_one", "query": query, "locale": "zh-CN", "epoch": 0, "groups": groups}
    first = _catalog_response(**args)
    assert len(first.groups[0].items) == 20 and first.next_cursor
    second = _catalog_response(
        **{
            **args,
            "query": query.model_copy(update={"cursor": first.next_cursor}),
            "groups": [ReferenceCatalogGroup(id="team_agents", items=list(reversed(items)))],
        }
    )
    assert len(second.groups[0].items) == 5 and second.next_cursor is None
    assert [item.id for item in first.groups[0].items + second.groups[0].items] == [item.id for item in items]
    for patch in ({"user_id": "user_two"}, {"team_id": "team_two"}, {"locale": "en-US"}, {"epoch": 1}):
        with pytest.raises(ReferenceCatalogError, match="reference_catalog_cursor_invalid"):
            _catalog_response(**{**args, "query": query.model_copy(update={"cursor": first.next_cursor}), **patch})
    chinese = _catalog_response(**{**args, "query": query.model_copy(update={"query": "分析师 24"})})
    assert [item.id for item in chinese.groups[0].items] == ["osa_24"]


def test_agent_item_says_created_by_me_only_for_the_viewers_own_agent() -> None:
    """Novice walkthrough: the @ picker labelled the creator's private Agent "Team Agent"."""
    base = {"id": "osa_mine", "label": "周报助手", "summary": "", "resource": None, "reason_code": "resource_unavailable"}
    assert ReferenceCatalogItem(**base, created_by_me=True).model_dump(mode="json")["created_by_me"] is True
    # Additive to the v1 contract: other Agents keep their exact shape.
    assert "created_by_me" not in ReferenceCatalogItem(**base).model_dump(mode="json")
