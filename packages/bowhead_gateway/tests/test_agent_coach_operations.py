"""Common receipt reads preserve real domain scope and do not repeat effects."""

import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.coach_operation_reference import OperationReadInput, with_operation_reference
from pydantic import ValidationError
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_plugins import administrator
from test_agent_coach_plugins import create as create_plugin
from test_agent_coach_resources import resources as resources
from test_agent_coach_skills import BODY
from test_agent_coach_skills import call as skill_call
from test_agent_coach_skills import skill_adapter as skill_adapter
from test_agent_coach_tool_handoff import create as create_requirement
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.unit


def reference(tool, result):
    return with_operation_reference(tool, result)["operation_ref"]


def later_chat(resources):
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute(
            "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES('s_receipt_later',?,?, '2026-10-03','2026-10-03')",
            (resources.user.id, resources.workspace_id),
        )
    return replace(resources, session_id="s_receipt_later")


def test_original_resource_receipts_compose_across_a_later_chat(resources, saved_draft, skill_adapter):
    administrator(resources)
    plugin = create_plugin(resources)
    target = skill_call(skill_adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    skill = skill_call(
        skill_adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "save"}
    )
    requirement = create_requirement(resources, saved_draft)
    refs = [reference("agent_plugin_save", plugin), reference("agent_skill_save", skill), reference("agent_tool_requirement_create", requirement)]
    resumed = later_chat(resources)
    for pointer, original in zip(refs, (plugin, skill, requirement), strict=True):
        read = _run(resumed.operations.get(pointer))
        assert read["ok"] and read["read_status"] == "available"
        assert read["operation_ref"] == pointer
        assert not read["effects_applied_by_this_read"]
        assert read["retention"] == {"policy": "source_domain", "expires_at": None}
        if "operation_id" in original:
            assert read["domain_result"]["operation_id"] == original["operation_id"]
        else:
            assert read["domain_result"]["requirement_id"] == original["requirement_id"]
    with sqlite3.connect(resources.store.db_path) as db:
        for table in ("coach_plugin_operation", "coach_skill_operation", "coach_tool_requirement"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        db.execute("UPDATE workspace_members SET status='removed'")
    for pointer in refs:
        with pytest.raises(ValueError, match="reference_scope_unavailable"):
            _run(resumed.operations.get(pointer))


def test_saved_draft_and_original_suggestion_have_independent_read_identity(resources, saved_draft):
    adapter, _, _, draft, _, _, _ = saved_draft
    proposed = _run(adapter.propose({"description": "Clear purpose"}, request_id="suggest", turn_id="t_original"))
    suggestion_ref = reference("agent_authoring_propose", proposed)
    started = _run(resources.authoring.start_draft(purpose="Summarize supplied notes", locale="en-US", request_id="draft", turn_id="t_original"))
    draft_ref = reference("agent_draft_start", started)
    resumed = later_chat(resources)
    read = _run(resumed.operations.get(suggestion_ref))
    assert read["domain_result"]["proposal"]["id"] == proposed["proposal"]["id"]
    assert read["domain_result"]["proposal"]["draft_id"] == draft.id
    assert read["retention"]["expires_at"] == proposed["proposal"]["expires_at"]
    saved = _run(resumed.operations.get(draft_ref))
    assert saved["domain_result"]["draft"]["draft_id"] == started["target"]["id"]
    assert not any(saved["domain_result"]["draft"]["actions"].values())
    with pytest.raises(ValueError, match="unavailable"):
        _run(resumed.operations.get({**suggestion_ref, "draft_id": started["target"]["id"]}))
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        _run(replace(resources, session_id="s_foreign").operations.get(draft_ref))


def test_unknown_skill_save_is_readable_without_recovery_or_import(skill_adapter, monkeypatch):
    adapter = skill_adapter
    target = skill_call(adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    from bowhead_core.coach_skill_authoring import CoachSkillAuthoring

    async def lost_after_prepare(self, operation_id):
        raise RuntimeError("Interrupted before publication")

    monkeypatch.setattr(CoachSkillAuthoring, "_recover", lost_after_prepare)
    interrupted = skill_call(
        adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "unknown"}
    )
    pointer = reference("agent_skill_save", interrupted)
    with sqlite3.connect(adapter.resources.store.db_path) as db:
        operation_id, state = db.execute("SELECT id,state FROM coach_skill_operation").fetchone()
    assert state == "prepared"
    read = _run(adapter.resources.operations.get(pointer))
    assert read["ok"] and read["domain_result"]["outcome"] == "unknown" and not read["domain_result"]["ok"]
    assert not (adapter.service.root / "coached-method").exists()
    with sqlite3.connect(adapter.resources.store.db_path) as db:
        assert db.execute("SELECT state FROM coach_skill_operation WHERE id=?", (operation_id,)).fetchone()[0] == "prepared"


@pytest.mark.parametrize("extra", [{"recover": True}, {"user_id": "other"}, {"kind": "arbitrary"}, {"operation_id": "../outside"}])
def test_operation_read_cannot_smuggle_effects_or_authority(extra):
    with pytest.raises(ValidationError):
        OperationReadInput.model_validate({"reference": {"kind": "skill_save", "operation_id": "csw_" + "a" * 64, **extra}})


def test_canvas_read_reauthorizes_original_record_and_has_no_write_options(resources, saved_draft):
    from bowhead_gateway.routes import agent_coach

    _, _, users, draft, owner, other, client = saved_draft
    client.app.include_router(agent_coach.router, prefix="/api/v1/agent-coach")
    pointer = {"kind": "draft_record", "draft_id": draft.id}
    body = {"reference": pointer, "workspace_id": "ws_a"}
    url = "/api/v1/agent-coach/operations/read"
    read = client.post(url, json=body)
    assert read.status_code == 200, read.text
    assert read.headers["cache-control"] == "no-store, private"
    assert read.json()["operation_ref"] == pointer
    assert not read.json()["effects_applied_by_this_read"]
    assert not any(read.json()["domain_result"]["draft"]["actions"].values())
    for extra in ({"recover": True}, {"user_id": other.id}, {"retry": True}):
        assert client.post(url, json={**body, **extra}).status_code == 422
    for changed in ({"workspace_id": "ws_other"}, {"session_id": "s_other"}, {"reference": {**pointer, "draft_id": "sad_missing"}}):
        denied = client.post(url, json={**body, **changed})
        assert denied.status_code == 404
        assert denied.json() == {"detail": {"error": {"code": "coach_operation_unavailable"}}}
        assert denied.headers["cache-control"] == "no-store, private"
    with sqlite3.connect(users.db_path) as db:
        db.execute("UPDATE workspace_members SET status='removed' WHERE user_id=?", (owner.id,))
    assert client.post(url, json=body).status_code == 404
    from types import SimpleNamespace

    client.app.state.settings = SimpleNamespace(auth_mode="prod")
    client.app.dependency_overrides.pop(agent_coach._current_user)
    assert client.post(url, json=body).status_code == 401


def test_canvas_unknown_save_read_does_not_reconcile(skill_adapter, saved_draft, monkeypatch):
    from bowhead_core.coach_skill_authoring import CoachSkillAuthoring
    from bowhead_gateway.routes import agent_coach

    async def interrupted(self, operation_id):
        raise RuntimeError("Interrupted publication")

    monkeypatch.setattr(CoachSkillAuthoring, "_recover", interrupted)
    target = skill_call(skill_adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    original = skill_call(
        skill_adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "view"}
    )
    client = saved_draft[-1]
    client.app.include_router(agent_coach.router, prefix="/api/v1/agent-coach")
    read = client.post("/api/v1/agent-coach/operations/read", json={"workspace_id": "ws_a", "reference": reference("agent_skill_save", original)})
    assert read.status_code == 200, read.text
    assert read.json()["domain_result"]["outcome"] == "unknown"
    assert not (skill_adapter.service.root / "coached-method").exists()
