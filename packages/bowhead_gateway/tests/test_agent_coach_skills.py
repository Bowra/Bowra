"""Coach Skill writes use current Chat scope and explicit resource grants."""

import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_gateway.agent_coach_skills import AgentCoachSkills
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_resources import resources as resources
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.unit
BODY = "---\nname: coached-method\ndescription: Summarize supplied evidence\n---\nRead the evidence.\n"


@pytest.fixture
def skill_adapter(resources, monkeypatch, tmp_path):
    monkeypatch.setattr("bowhead_gateway.agent_coach_skills._user_dir_for", lambda user_id: tmp_path / user_id)
    _run(resources.request.app.state.skill_instance_store.initialize())
    return AgentCoachSkills(resources)


def call(adapter, operation, args):
    return _run(adapter.call(operation, args, turn_id="t_current"))


def test_personal_save_and_exact_receipt_without_agent_draft(skill_adapter):
    adapter = skill_adapter
    target = call(adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    assert target["id"] is None and target["revision"] is None
    saved = call(adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "one"})
    assert saved["ok"] and saved["result"]["team_published"] is False, saved
    assert saved["import_inspection"] in {"complete", "not_completed"}
    read = call(adapter, "agent_skill_read", {"target": saved["result"]["target"], "file": "SKILL.md"})
    assert read["content"] == BODY and read["untrusted_content"]
    receipt = call(adapter, "agent_skill_operation_get", {"operation_id": saved["operation_id"]})
    assert receipt["result"] == saved["result"]


@pytest.mark.parametrize("kind,identity", [("skill", "coached-method"), ("builtin_capability", "skill_invoke")])
def test_current_explicit_write_denial_blocks_new_save(skill_adapter, kind, identity):
    adapter = skill_adapter
    target = call(adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    resource = adapter.resources
    _run(
        AccessControlStore(resource.store.db_path).create_or_update_resource_grant(
            team_id=resource.user.team_id,
            resource_kind=kind,
            resource_id=identity,
            principal_type="user",
            principal_id=resource.user.id,
            permission="write",
            effect="deny",
        )
    )
    with pytest.raises(ValueError, match="resource_grant_denied"):
        call(adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "denied"})
    assert not (adapter.service.root / "coached-method").exists()
    with sqlite3.connect(resource.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM coach_skill_operation").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["workspace_members", "team_members", "users"])
def test_revoked_scope_never_uses_cached_authority(skill_adapter, table):
    adapter = skill_adapter
    target = call(adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    with sqlite3.connect(adapter.resources.store.db_path) as db:
        db.execute(f"UPDATE {table} SET status='removed'")
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        call(adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "revoked"})
    assert not (adapter.service.root / "coached-method").exists()


def test_another_chat_cannot_replace_the_bound_scope(skill_adapter):
    adapter = AgentCoachSkills(replace(skill_adapter.resources, session_id="s_foreign"))
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        call(adapter, "agent_skill_inspect", {"name": "coached-method"})


def test_later_owned_chat_recovers_original_receipt_with_fresh_permissions(skill_adapter):
    resource = skill_adapter.resources
    with sqlite3.connect(resource.store.db_path) as db:
        for identity in ("s_skill_original", "s_skill_resume"):
            db.execute(
                "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES(?,?,?,'2026-10-03','2026-10-03')",
                (identity, resource.user.id, resource.workspace_id),
            )
    original = AgentCoachSkills(replace(resource, session_id="s_skill_original"))
    target = call(original, "agent_skill_inspect", {"name": "coached-method"})["target"]
    saved = call(original, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "original"})
    later = AgentCoachSkills(replace(resource, session_id="s_skill_resume"))
    recovered = call(later, "agent_skill_operation_get", {"operation_id": saved["operation_id"]})
    assert recovered["result"] == saved["result"] and recovered["source_session_id"] == "s_skill_original"
    assert call(later, "agent_skill_cancel", {"operation_id": saved["operation_id"]})["cancellation_status"] == "already_applied"
    with sqlite3.connect(resource.store.db_path) as db:
        db.execute("UPDATE workspace_members SET status='removed'")
    for operation in ("agent_skill_operation_get", "agent_skill_cancel"):
        with pytest.raises(ValueError, match="reference_scope_unavailable"):
            call(later, operation, {"operation_id": saved["operation_id"]})


def test_save_inspection_is_separate_from_original_receipt_and_survives_later_edit(skill_adapter):
    adapter = skill_adapter
    target = call(adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]
    original = call(
        adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "inspection-original"}
    )
    receipt = call(adapter, "agent_skill_operation_get", {"operation_id": original["operation_id"]})
    assert receipt["result"] == original["result"]
    assert receipt["result"]["import_status_at_save"] == "pending"
    inspection = receipt["current_inspection"]
    assert inspection["status"] == "read_current_target" and inspection["target"] == original["result"]["target"]
    assert inspection["inspection_status"] == "complete"
    assert inspection["trial"] == inspection["trigger_verification"] == "not_run"
    assert any(f["subject"] == "trigger_examples" for f in inspection["import_review"]["findings"])
    updated = call(
        adapter,
        "agent_skill_save",
        {"target": inspection["target"], "changes": [{"path": "SKILL.md", "content": BODY + "Preserve scope.\n"}], "request_id": "inspection-newer"},
    )
    assert updated["state"] == "succeeded"
    historical = call(adapter, "agent_skill_operation_get", {"operation_id": original["operation_id"]})
    assert historical["result"] == original["result"]
    assert historical["current_inspection"] == {"status": "target_changed", "target": inspection["target"]}


def test_optional_inspection_failure_preserves_successful_save_and_original_receipt(skill_adapter, monkeypatch):
    from bowhead_core.coach_skill_authoring import CoachSkillAuthoring

    adapter = skill_adapter
    target = call(adapter, "agent_skill_inspect", {"name": "coached-method"})["target"]

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("Synthetic inspection outage")

    monkeypatch.setattr(CoachSkillAuthoring, "inspect", unavailable)
    saved = call(
        adapter, "agent_skill_save", {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "inspection-unavailable"}
    )
    assert saved["state"] == "succeeded"
    assert saved["current_inspection"]["status"] == "unavailable"
    recovered = call(adapter, "agent_skill_operation_get", {"operation_id": saved["operation_id"]})
    assert recovered["result"] == saved["result"]
    assert (adapter.service.root / "coached-method" / "SKILL.md").read_text() == BODY
