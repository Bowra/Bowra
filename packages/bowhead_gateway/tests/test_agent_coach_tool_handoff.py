"""Tool handoffs retain the original owner target without inventing creation."""

import json
import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.agent_coach import ToolRequirementCreateInput
from bowhead_core.user_store import TeamToolSource
from bowhead_gateway.agent_coach_tool_handoff import AgentCoachToolHandoff
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_resources import resources as resources
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.unit
REQUIREMENT = {
    "workflow": "openapi",
    "operation": "Read customer records",
    "reason": "The Agent needs permitted customer data",
    "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}},
    "output_schema": {"type": "object"},
    "required": True,
    "acceptable_alternatives": [],
    "acceptance_cases": ["An authorized customer can be read"],
    "source_url_hint": "https://user:private@example.test/openapi?token=private#private",
}


def call(resources, operation, **args):
    return _run(AgentCoachToolHandoff(resources).call(operation, args, turn_id="t_handoff"))


def create(resources, saved_draft, **updates):
    draft = saved_draft[3]
    args = {"parent": {"kind": "agent_draft", "id": draft.id, "version": draft.row_version}, "requirement": REQUIREMENT, "request_id": "original"}
    return call(resources, "agent_tool_requirement_create", **ToolRequirementCreateInput.model_validate({**args, **updates}).model_dump())


def tool_source(resources, creator_id):
    source = TeamToolSource(
        id="ots_123456789abc",
        team_id=resources.user.team_id,
        name="Customer Records",
        base_url="https://example.test",
        created_by=creator_id,
        selected_op_ids=["records"],
        created_at="2026-10-03",
        updated_at="2026-10-03",
        openapi_spec_json=json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Customer Records", "version": "1"},
                "paths": {"/records": {"get": {"operationId": "records", "responses": {"200": {"description": "ok"}}}}},
            }
        ),
    )
    _run(resources.store.create_team_tool_source(source))
    return source


def test_requirement_replays_original_identity_and_keeps_a_changed_parent(resources, saved_draft):
    first = create(resources, saved_draft)
    assert first["ok"] and first["parent_status"]["baseline_matches"]
    assert first["requirement"]["source_url_hint"] == "https://example.test/openapi"
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_subagent_draft SET row_version=row_version+1 WHERE id=?", (saved_draft[3].id,))
    replay = create(resources, saved_draft)
    assert replay["requirement_id"] == first["requirement_id"]
    assert replay["original_parent"] == first["original_parent"]
    assert replay["original_parent_context"] == first["original_parent_context"]
    assert not replay["parent_status"]["baseline_matches"] and not replay["parent_status"]["binding_available"]
    with pytest.raises(ValueError, match="idempotency_conflict"):
        create(resources, saved_draft, requirement={**REQUIREMENT, "operation": "Another operation"})
    with pytest.raises(ValueError, match="parent_changed"):
        create(resources, saved_draft, request_id="stale_new_request")
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM coach_tool_requirement").fetchone()[0] == 1
        raw = db.execute("SELECT requirement_json FROM coach_tool_requirement").fetchone()[0]
        assert "token=" not in raw and "user:private" not in raw


def test_open_retains_one_view_request_and_requires_current_admin(resources, saved_draft, monkeypatch):
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR", "1")
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_members SET role='team_admin' WHERE user_id=?", (resources.user.id,))
    first = create(resources, saved_draft)
    args = {"requirement_id": first["requirement_id"]}
    opened = call(resources, "agent_tool_requirement_open", **args)
    assert opened["onboarding_state"] == "view_requested" and not opened["resource_created"]
    assert call(resources, "agent_tool_requirement_open", **args)["onboarding_operation_id"] == opened["onboarding_operation_id"]
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE teams SET created_by_user_id=? WHERE id=?", (saved_draft[5].id, resources.user.team_id))
        db.execute("UPDATE team_members SET role='member' WHERE user_id=?", (resources.user.id,))
    assert call(resources, "agent_tool_requirement_get", **args)["next_action"] == "team_admin_onboarding"
    with pytest.raises(ValueError, match="team_admin_required"):
        call(resources, "agent_tool_requirement_open", **args)


def test_later_chat_must_reauthorize_current_session_and_owner(resources, saved_draft):
    first = create(resources, saved_draft)
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute(
            "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES(?,?,?,?,?)",
            ("s_later", resources.user.id, resources.workspace_id, "2026-10-03", "2026-10-03"),
        )
    later = replace(resources, session_id="s_later")
    assert call(later, "agent_tool_requirement_get", requirement_id=first["requirement_id"])["original_parent"] == first["original_parent"]
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE sessions SET user_id=? WHERE id='s_later'", (saved_draft[5].id,))
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        call(later, "agent_tool_requirement_get", requirement_id=first["requirement_id"])


def test_engineering_handoff_does_not_open_an_unsupported_installer(resources, saved_draft, monkeypatch):
    monkeypatch.setenv("BOWHEAD_CCA_TOOL_CREATOR", "1")
    first = create(resources, saved_draft, requirement={**REQUIREMENT, "workflow": "engineering"}, parent=None)
    assert first["next_action"] == "engineering_delivery" and first["original_parent"] is None
    reference = {"kind": "tool_requirement", "requirement_id": first["requirement_id"], "observation_id": None}
    assert first["view_action"] == {"tool": "agent_operation_open", "arguments": {"reference": reference}}
    rejected = call(resources, "agent_tool_requirement_open", requirement_id=first["requirement_id"])
    assert rejected == {
        "ok": False,
        "error": "tool_engineering_delivery_required",
        "operation_ref": reference,
        "view_action": first["view_action"],
    }
    assert call(resources, "agent_tool_requirement_open", requirement_id=first["requirement_id"]) == rejected
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT onboarding_operation_id,onboarding_requested_at FROM coach_tool_requirement").fetchall() == [(None, None)]
        db.execute("DELETE FROM workspace_members WHERE user_id=? AND workspace_id=?", (resources.user.id, resources.workspace_id))
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        call(resources, "agent_tool_requirement_open", requirement_id=first["requirement_id"])


def test_reconcile_observes_another_creators_real_tool_without_claiming_creation(resources, saved_draft):
    first = create(resources, saved_draft)
    source = tool_source(resources, saved_draft[5].id)
    page = _run(resources.search(kind="tool", query="records", offset=0, limit=50))
    assert page["items"], page
    reference = next(item["reference"] for item in page["items"] if source.id in item["reference"]["id"])
    args = {"requirement_id": first["requirement_id"], "reference": reference, "request_id": "observed"}
    observed = call(resources, "agent_tool_requirement_reconcile", **args)
    assert observed["observation"]["current_status"] == "available", observed
    assert observed["observation"]["connection_id"] == source.id
    assert observed["observation"]["creation_receipt"] == "not_available"
    assert observed["binding_suggestion"]["target"] == first["original_parent"]
    assert not observed["resource_created"] and not observed["parent_bound"] and observed["business_trial"] == "not_run"
    assert call(resources, "agent_tool_requirement_reconcile", **args)["observation"]["id"] == observed["observation"]["id"]
    _run(resources.store.update_team_tool_source_meta(resources.user.team_id, source.id, status="disabled"))
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_subagent_draft SET row_version=row_version+1 WHERE id=?", (saved_draft[3].id,))
    unavailable = call(resources, "agent_tool_requirement_get", requirement_id=first["requirement_id"], observation_id=observed["observation"]["id"])
    assert unavailable["observation"]["reference"] is None and unavailable["observation"]["current_status"] == "unavailable_or_changed"
    assert "binding_suggestion" not in unavailable
    assert not unavailable["parent_status"]["baseline_matches"]


def test_nonexistent_tool_cannot_supply_a_success_callback(resources, saved_draft):
    first = create(resources, saved_draft)
    with pytest.raises(ValueError, match="unavailable_or_unselectable"):
        call(
            resources,
            "agent_tool_requirement_reconcile",
            requirement_id=first["requirement_id"],
            reference={"kind": "tool", "id": "fabricated", "revision": "fabricated"},
            request_id="fake",
        )
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM coach_tool_observation").fetchone()[0] == 0


def test_personal_skill_parent_keeps_exact_hash_and_current_owner(resources, saved_draft, tmp_path, monkeypatch):
    from bowhead_core.skill_instance_store import SkillInstanceStore
    from bowhead_core.skill_upload import validate_and_save_generated

    monkeypatch.setattr("bowhead_gateway.agent_coach_skills._user_dir_for", lambda user_id: tmp_path / user_id)
    root = tmp_path / resources.user.id
    saved = validate_and_save_generated(
        body_md="---\nname: parent-method\ndescription: A personal method\n---\nRead customer records.\n",
        scripts=None,
        user_id=resources.user.id,
        dest_root=root,
        existing_user_skills={},
    )
    instances = SkillInstanceStore(resources.store.db_path)
    _run(instances.initialize())
    _run(instances.upsert(user_id=resources.user.id, name=saved.name, source_type="user_generated", source_path=str(saved.source_dir)))
    target = _run(resources.skills.service.inspect(name=saved.name))["target"]
    requirement = create(resources, saved_draft, parent=target)
    assert requirement["original_parent"] == target
    (saved.source_dir / "SKILL.md").write_text(saved.body_md + "New manual instructions")
    current = call(resources, "agent_tool_requirement_get", requirement_id=requirement["requirement_id"])
    assert not current["parent_status"]["baseline_matches"] and current["original_parent"] == target
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE skill_instances SET user_id=? WHERE name=?", (saved_draft[5].id, saved.name))
    with pytest.raises(ValueError, match="parent_unavailable"):
        call(resources, "agent_tool_requirement_get", requirement_id=requirement["requirement_id"])


def test_shared_plugin_gap_resumes_with_original_target_and_current_admin(resources, saved_draft):
    from bowhead_core.team_plugin_store import TeamPluginStore
    from test_agent_coach_plugins import administrator, exact_method

    grant = exact_method(resources)
    bundle = _run(
        TeamPluginStore(resources.store.db_path).create(
            team_id=resources.user.team_id,
            actor_user_id=resources.user.id,
            name="Shared evidence",
            description="Read supplied evidence",
            tool_refs=[],
            skill_grants=[grant],
        )
    )
    inspected = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": bundle["id"]}, turn_id="t_original"))
    requirement = create(resources, saved_draft, parent=inspected["target"])
    assert requirement["original_parent"] == inspected["target"]
    assert requirement["parent_status"]["baseline_matches"] and not requirement["parent_status"]["binding_available"]
    source = tool_source(resources, saved_draft[5].id)
    page = _run(resources.search(kind="tool", query="records", offset=0, limit=50))
    reference = next(item["reference"] for item in page["items"] if source.id in item["reference"]["id"])
    observed = call(
        resources, "agent_tool_requirement_reconcile", requirement_id=requirement["requirement_id"], reference=reference, request_id="observed"
    )
    assert observed["binding_suggestion"]["supported_binding_action"] == "agent_plugin_validate"
    assert not observed["binding_suggestion"]["ready_to_propose"]
    administrator(resources)
    resumed = call(
        resources, "agent_tool_requirement_get", requirement_id=requirement["requirement_id"], observation_id=observed["observation"]["id"]
    )
    assert resumed["binding_suggestion"]["ready_to_propose"]
    assert resumed["original_parent"] == inspected["target"] and not resumed["parent_bound"]
    assert "shared_impact_review" in resumed["binding_suggestion"]["requires"]
    fresh = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": bundle["id"]}, turn_id="t_resumed"))
    saved = _run(
        resources.plugins.call(
            "agent_plugin_save",
            {
                "target": fresh["target"],
                "expected_impact": fresh["impact"]["fingerprint"],
                "patch": {"tools": [reference]},
                "request_id": "complete-binding",
            },
            turn_id="t_resumed",
        )
    )
    assert saved["result_target"]["revision"] == bundle["revision"] + 1
    current = _run(TeamPluginStore(resources.store.db_path).get(resources.user.team_id, bundle["id"]))
    assert current["tool_refs"] == [reference["id"]] and current["skill_grants"] == [grant]
    stale = call(resources, "agent_tool_requirement_get", requirement_id=requirement["requirement_id"], observation_id=observed["observation"]["id"])
    assert stale["original_parent"] == inspected["target"]
    assert not stale["parent_status"]["baseline_matches"] and not stale["binding_suggestion"]["ready_to_propose"]
    replay = create(resources, saved_draft, parent=inspected["target"])
    assert replay["requirement_id"] == requirement["requirement_id"]
    with pytest.raises(ValueError, match="parent_changed"):
        create(resources, saved_draft, parent=inspected["target"], request_id="old-baseline-new-intent")


def test_plugin_parent_rechecks_current_team_and_resource_access(resources, saved_draft):
    from bowhead_core.team_plugin_store import TeamPluginStore
    from test_agent_coach_plugins import administrator

    administrator(resources)
    bundle = _run(
        TeamPluginStore(resources.store.db_path).create(
            team_id=resources.user.team_id,
            actor_user_id=resources.user.id,
            name="Shared tools",
            description="Read supplied pages",
            tool_refs=["web_fetch"],
            skill_grants=[],
        )
    )
    target = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": bundle["id"]}, turn_id="t_original"))["target"]
    requirement = create(resources, saved_draft, parent=target)
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_capability SET team_id='another_team' WHERE id=?", (bundle["id"],))
    with pytest.raises(ValueError, match="plugin_unavailable"):
        call(resources, "agent_tool_requirement_get", requirement_id=requirement["requirement_id"])
