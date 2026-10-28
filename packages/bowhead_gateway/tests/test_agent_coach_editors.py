"""Resource editor opening uses current domain authorization and original identities."""

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_resources import resources as resources
from test_agent_coach_skills import BODY, call
from test_agent_coach_skills import skill_adapter as skill_adapter
from test_agent_coach_working_copy import live_agent as live_agent
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def original_personal_path(skill_adapter, monkeypatch):
    from bowhead_core.access_control import AccessControlStore

    skill_adapter.resources.request.app.state.access_control_store = AccessControlStore(skill_adapter.resources.store.db_path)
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    monkeypatch.setattr("bowhead_gateway.routes.skills._user_dir_for", lambda uid: skill_adapter.service.root.parent / uid)


def personal(resources, skill_adapter):
    target = call(skill_adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    saved = call(skill_adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "editor"})
    page = _run(resources.search(kind="skill", query="coached-method", offset=0, limit=50))
    return next(item["reference"] for item in page["items"] if item["reference"]["id"] == saved["result"]["target"]["id"])


def test_editor_reads_current_same_identity_and_reauthorizes_scope(resources, saved_draft, skill_adapter):
    from bowhead_gateway.routes import agent_coach

    reference = personal(resources, skill_adapter)
    client = saved_draft[-1]
    client.app.include_router(agent_coach.router, prefix="/api/v1/agent-coach")
    parent = {"kind": "agent_draft", "id": saved_draft[3].id, "version": saved_draft[3].row_version}
    body = {"reference": reference, "parent": parent, "workspace_id": resources.workspace_id}
    read = client.post("/api/v1/agent-coach/resources/editor", json=body)
    assert read.status_code == 200, read.text
    value = read.json()
    assert value["target"]["skill"]["id"] == reference["id"]
    assert value["target"]["skill"]["content_sha256"] == reference["revision"].removeprefix("sha256:")
    assert value["read_only"] is False
    assert read.headers["cache-control"] == "no-store, private"
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE workspace_members SET status='removed'")
    denied = client.post("/api/v1/agent-coach/resources/editor", json=body)
    assert denied.status_code == 404
    assert "coached-method" not in denied.text


def test_new_versions_retain_identity_and_resource_denial_clears_access(resources, skill_adapter):
    from bowhead_core.access_control import AccessControlStore

    reference = personal(resources, skill_adapter)
    target = call(skill_adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    call(
        skill_adapter,
        "agent_skill_save",
        {"target": target, "changes": [{"path": "SKILL.md", "content": BODY + "New instructions.\n"}], "request_id": "newer"},
    )
    with pytest.raises(ValueError, match="unavailable_or_changed"):
        _run(resources.editors.read(reference, exact=True))
    current = _run(resources.editors.read(reference))
    assert current["reference"]["id"] == reference["id"] and current["reference"]["revision"] != reference["revision"]
    _run(
        AccessControlStore(resources.store.db_path).create_or_update_resource_grant(
            team_id=resources.user.team_id,
            resource_kind="skill",
            resource_id="coached-method",
            principal_type="user",
            principal_id=resources.user.id,
            permission="write",
            effect="deny",
        )
    )
    with pytest.raises(ValueError, match="unavailable_or_changed"):
        _run(resources.editors.read(reference))


def test_read_only_entitlement_keeps_authorized_editor_visible(resources, skill_adapter, monkeypatch):
    reference = personal(resources, skill_adapter)

    async def readonly(request):
        raise HTTPException(403, "Read only")

    monkeypatch.setattr("bowhead_gateway.agent_coach_editors.assert_not_readonly", readonly)
    assert _run(resources.editors.read(reference))["read_only"] is True


def test_missing_original_parent_or_recreated_resource_does_not_open_another_editor(resources, saved_draft, skill_adapter):
    reference = personal(resources, skill_adapter)
    with pytest.raises((ValueError, HTTPException)):
        _run(resources.editors.read(reference, {"kind": "agent_draft", "id": "sad_missing", "version": 1}))
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE skill_instances SET id='sk_recreated' WHERE id=?", (reference["id"],))
    with pytest.raises(ValueError, match="unavailable_or_changed"):
        _run(resources.editors.read(reference))


@pytest.mark.parametrize("visibility,editable", [("creator_only", True), ("team_wide", False)])
def test_team_editor_uses_original_content_governance(resources, saved_draft, monkeypatch, tmp_path, visibility, editable):
    from bowhead_core.skill_upload import bundle_sha256
    from bowhead_gateway.routes import team_skills

    monkeypatch.setattr(team_skills, "_TEAM_SKILLS_ROOT", tmp_path / "team")
    client = saved_draft[-1]
    client.app.include_router(team_skills.router, prefix="/api/v1/team-skills")
    response = client.post("/api/v1/team-skills", json={"body_md": BODY, "visibility": visibility})
    assert response.status_code == 200, response.text
    identity = response.json()["id"]
    skill = _run(resources.store.get_team_skill(resources.user.team_id, identity))
    reference = {"kind": "skill", "source_kind": "team", "id": identity, "revision": "sha256:" + bundle_sha256(Path(skill.source_path))}
    value = _run(resources.editors.read(reference, exact=True))
    assert value["target"]["kind"] == "team" and value["target"]["skill"]["id"] == identity
    assert value["target"]["canEdit"] is editable and value["read_only"] is not editable
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_skill SET team_id='team_other' WHERE id=?", (identity,))
    with pytest.raises(HTTPException):
        _run(resources.editors.read(reference))


def test_builtin_editor_keeps_exact_source_and_is_read_only(resources):
    page = _run(resources.search(kind="skill", query="", offset=0, limit=50))
    reference = next(item["reference"] for item in page["items"] if item["reference"].get("source_kind") == "builtin")
    value = _run(resources.editors.read(reference, exact=True))
    assert value["read_only"] and value["target"]["skill"]["source_type"] == "builtin"
    assert value["reference"] == reference


def test_plugin_editor_preserves_exact_identity_current_role_and_original_writer(resources, saved_draft):
    from bowhead_core.team_plugin_store import TeamPluginStore
    from bowhead_gateway.routes import team_plugins
    from test_agent_coach_plugins import administrator, create

    administrator(resources)
    result = create(resources)
    reference = {"kind": "capability", "id": result["result_target"]["id"], "revision": "1"}
    parent = {"kind": "agent_draft", "id": saved_draft[3].id, "version": saved_draft[3].row_version}
    view = _run(resources.editors.read(reference, parent, exact=True))
    assert view["editor_kind"] == "plugin" and not view["read_only"]
    plugin = view["target"]["plugin"]
    client = saved_draft[-1]
    client.app.include_router(team_plugins.router, prefix="/api/v1/team-capabilities")
    body = {key: plugin[key] for key in ("name", "description", "tool_refs", "skill_grants", "row_version")}
    changed = client.put(f"/api/v1/team-capabilities/{plugin['id']}", json={**body, "description": "Updated original Plugin"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    stale = client.put(f"/api/v1/team-capabilities/{plugin['id']}", json={**body, "description": "Stale edit"})
    assert stale.status_code == 409
    with pytest.raises(ValueError, match="unavailable_or_changed"):
        _run(resources.editors.read(reference, exact=True))
    current = _run(resources.editors.read(reference))
    assert current["reference"]["revision"] == "2" and current["target"]["plugin"]["description"] == "Updated original Plugin"
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_members SET role='member' WHERE user_id=?", (resources.user.id,))
    assert _run(resources.editors.read(reference))["read_only"] is True
    denied = client.put(f"/api/v1/team-capabilities/{plugin['id']}", json={**body, "row_version": 2})
    assert denied.status_code == 403
    _run(
        TeamPluginStore(resources.store.db_path).set_enabled(
            team_id=resources.user.team_id, plugin_id=plugin["id"], expected_row_version=2, enabled=False
        )
    )
    with pytest.raises(HTTPException):
        _run(resources.editors.read(reference))


def test_agent_editor_reads_current_owner_baseline_without_creating_or_replacing_it(resources, live_agent, saved_draft):
    from dataclasses import replace

    identity, live = live_agent
    reference = next(item["reference"] for item in _run(resources.search(kind="agent", query=identity, offset=0, limit=50))["items"])
    original = _run(resources.editors.read(reference, exact=True))
    assert original["target"]["agent_id"] == identity
    assert original["editor_kind"] == "agent" and not original["read_only"]
    assert original["target"]["detail"]["working_copy"] is None
    opened = _run(resources.authoring.open_working_copy(agent_id=identity, expected_live_release_id=live["release_id"]))
    with pytest.raises(ValueError, match="unavailable_or_changed"):
        _run(resources.editors.read(reference, exact=True))
    current = _run(resources.editors.read(reference))
    assert current["target"]["detail"]["working_copy"]["id"] == opened["target"]["working_copy_id"]
    assert current["reference"] != reference
    other = saved_draft[5]
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,joined_at) VALUES (?,?,'member','2026-10-03')",
            (resources.workspace_id, other.id),
        )
    with pytest.raises(ValueError, match="unavailable_or_changed"):
        _run(replace(resources, user=other).editors.read(reference))
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_subagent SET lifecycle_state='archived' WHERE id=?", (identity,))
    assert _run(resources.editors.read(reference))["read_only"] is True
