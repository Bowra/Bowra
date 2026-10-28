"""Real Owner HTTP catalog and normal working-copy policy writes."""

import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.user_store import TeamSkill, UserStore
from test_agents_routes import _enable_revisioned_agent
from test_agents_routes import app_client as _app_client
from test_skill_read_resolution import package

from bowhead_core import agent_dependency_compiler
from bowhead_gateway import agent_optional_skill_catalog

app_client = _app_client
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def case(app_client, tmp_path, monkeypatch):
    client, path = app_client
    _enable_revisioned_agent(path, tool_refs=("bi_query",))
    root = tmp_path / "builtin-optional"
    package(root / "dupont")
    monkeypatch.setattr(agent_dependency_compiler, "builtin_catalog_dir", lambda: root)
    monkeypatch.setattr(agent_optional_skill_catalog, "builtin_catalog_dir", lambda: root)
    personal_root = package(tmp_path / "personal")
    personal = await SkillInstanceStore(path).upsert(user_id="user_1", name="dupont", source_type="user_upload", source_path=str(personal_root))
    team_root = package(tmp_path / "team")
    team = TeamSkill(
        id="oskill_dupont",
        team_id="team_1",
        name="dupont",
        source_path=str(team_root),
        created_by="user_1",
        approval="approved",
        approved_sha256=bundle_sha256(team_root),
        visibility="team_wide",
    )
    await UserStore(path).create_team_skill(team)
    return SimpleNamespace(client=client, path=path, root=root, personal=personal, team=team)


async def read(case, **params):
    return await case.client.get("/api/v1/agents/osa_1/optional-skill-catalog", params={"q": "dupont", **params})


async def test_three_same_named_origins_remain_exact_and_paginate_without_baseline_grants(case):
    offset, items = 0, []
    while offset is not None:
        response = await read(case, offset=offset, limit=1)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        value = response.json()
        assert value["agent_id"] == "osa_1" and len(value["items"]) == 1
        items.extend(value["items"])
        offset = value["next_offset"]
    assert [(item["resource"]["source_kind"], item["resource"]["id"]) for item in items] == [
        ("builtin", "builtin:dupont"),
        ("team", case.team.id),
        ("user", case.personal.id),
    ]
    assert items[-1]["allowed_audiences"] == ["private"]
    assert all(item["resource"]["version"].startswith("sha256:") for item in items)
    assert all(set(item) == {"resource", "label", "description", "allowed_audiences"} for item in items)
    assert str(case.root) not in str(items) and "SELECTED_METHOD" not in str(items)


@pytest.mark.parametrize("change", ["owner", "archived", "quarantined"])
async def test_management_role_cannot_supply_another_owners_or_inactive_agents_catalog(case, change):
    with sqlite3.connect(case.path) as db:
        if change == "owner":
            db.execute("UPDATE team_subagent SET owner_user_id='user_2' WHERE id='osa_1'")
            db.execute("UPDATE team_members SET role='team_admin' WHERE team_id='team_1' AND user_id='user_1'")
        else:
            db.execute("UPDATE team_subagent SET lifecycle_state=? WHERE id='osa_1'", (change,))
    response = await read(case)
    assert response.status_code == 404 and "items" not in response.json()


@pytest.mark.parametrize("kind", ["builtin", "team", "user"])
async def test_current_denial_is_hard_even_when_legacy_authorization_is_shadow(case, monkeypatch, kind):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
    await AccessControlStore(case.path).create_or_update_resource_grant(
        team_id="team_1",
        resource_kind="team_skill" if kind == "team" else "skill",
        resource_id=case.team.id if kind == "team" else "dupont",
        principal_type="user",
        principal_id="user_1",
        permission="use",
        effect="deny",
    )
    response = await read(case)
    assert response.status_code == 200, response.text
    assert not any(item["resource"]["source_kind"] == kind for item in response.json()["items"])


@pytest.mark.parametrize("change", ["pending", "disabled", "foreign", "no_readonly_entry"])
async def test_ineligible_personal_methods_cannot_be_advertised(case, change):
    with sqlite3.connect(case.path) as db:
        column, value = {
            "pending": ("import_status", "pending"),
            "disabled": ("enabled", 0),
            "foreign": ("user_id", "user_2"),
            "no_readonly_entry": ("description", "Changed"),
        }[change]
        db.execute(f"UPDATE skill_instances SET {column}=? WHERE id=?", (value, case.personal.id))
    if change == "no_readonly_entry":
        from pathlib import Path

        (Path(case.personal.source_path) / "SKILL.md").write_text("---\nname: dupont\ndescription: Plain SOP\n---\nSOP\n")
    response = await read(case)
    assert response.status_code == 200, response.text
    assert not any(item["resource"]["source_kind"] == "user" for item in response.json()["items"])


async def test_authority_change_during_material_eligibility_rejects_the_entire_response(case, monkeypatch):
    original = agent_dependency_compiler.AgentDependencyCompiler.resolve_optional_skill_read

    async def interleave(self, *args, **kwargs):
        value = await original(self, *args, **kwargs)
        await AccessControlStore(case.path).bump_policy_epoch("team_1")
        return value

    monkeypatch.setattr(agent_dependency_compiler.AgentDependencyCompiler, "resolve_optional_skill_read", interleave)
    response = await read(case)
    assert response.status_code == 409 and "items" not in response.json()


async def test_disabled_builtin_preference_is_not_replaced_by_same_named_team_method(case):
    await SkillInstanceStore(case.path).upsert(
        user_id="user_1", name="dupont", source_type="builtin", source_path=str(case.root / "dupont"), enabled=False
    )
    response = await read(case)
    assert response.status_code == 200, response.text
    assert [item["resource"]["source_kind"] for item in response.json()["items"]] == ["team"]


async def test_owner_can_save_exact_optional_method_through_existing_policy_writer(case):
    item = next(item for item in (await read(case)).json()["items"] if item["resource"]["source_kind"] == "user")
    detail = (await case.client.get("/api/v1/agents/osa_1/working-copy")).json()
    source = detail["live"]
    policy = {
        "schema_version": 1,
        "skill_read": {
            "enabled": True,
            "selection_sources": ["user_explicit"],
            "allowed_sources": ["user"],
            "resource_allowlist": [{key: value for key, value in item["resource"].items() if key != "kind"}],
            "require_read_only_contract": True,
            "max_selected": 2,
        },
    }
    response = await case.client.patch(
        "/api/v1/agents/osa_1/working-copy",
        json={
            "definition": {**source["definition"], "runtime_extensions": policy},
            "listing": source["listing"],
            "audience": source["audience"],
            "expected_row_version": None,
        },
    )
    assert response.status_code == 200, response.text
    saved = (await case.client.get("/api/v1/agents/osa_1/working-copy")).json()
    assert saved["working_copy"]["definition"]["runtime_extensions"] == policy
    assert saved["working_copy"]["definition"]["skill_grants"] == source["definition"]["skill_grants"]
    assert saved["working_copy"]["definition"]["tool_refs"] == source["definition"]["tool_refs"]
    assert saved["live"] == source


async def test_baseline_selection_does_not_open_the_optional_read_entry(case):
    from pathlib import Path

    (Path(case.personal.source_path) / "SKILL.md").write_text("---\nname: dupont\ndescription: Plain baseline method\n---\nMethod\n")
    store = UserStore(case.path)
    user = await store.get_user("user_1")
    baseline = await agent_optional_skill_catalog.optional_skill_catalog(
        store=store, user=user, agent_id=None, query="dupont", offset=0, limit=50, include_source_identity=True, baseline_selection=True
    )
    assert any(item["resource"]["id"] == case.personal.id for item in baseline["items"])
    optional = (await read(case)).json()
    assert not any(item["resource"]["id"] == case.personal.id for item in optional["items"])
    await AccessControlStore(case.path).create_or_update_resource_grant(
        team_id="team_1", resource_kind="skill", resource_id="dupont", principal_type="user", principal_id="user_1", permission="use", effect="deny"
    )
    denied = await agent_optional_skill_catalog.optional_skill_catalog(
        store=store, user=user, agent_id=None, query="dupont", offset=0, limit=50, baseline_selection=True
    )
    assert not any(item["resource"]["id"] == case.personal.id for item in denied["items"])
