"""Authoring checks preserve current dependencies and distinct execution evidence."""

import json
import sqlite3

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError
from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_gateway.agent_coach_resources import AgentCoachResources
from bowhead_gateway.routes import agents
from starlette.requests import Request
from test_agent_coach_proposals import working_agent
from test_agents_routes import app_client as app_client

pytestmark = pytest.mark.unit


@pytest.fixture
async def validation_context(app_client, monkeypatch):
    client, db_path = app_client
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    resource = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    return resource, target, db_path


def intent(target, field="system_prompt", value="Proposed instructions."):
    return {
        "target": target,
        "operations": [{"op": "add", "path": "/definition/" + field, "value": value}],
        "atomic_groups": [{"id": "behavior", "operation_indexes": [0]}],
        "suggested_cases": [],
    }


async def test_validation_binds_candidate_and_dependency_evidence_without_execution(validation_context):
    resource, target, path = validation_context
    result = await resource.authoring.validate_change(**intent(target))
    report = result["validation_report"]
    assert report["original_target"] == target
    assert report["checks"]["dependencies"]["status"] == "passed"
    assert report["checks"]["dependencies"]["fingerprint"].startswith("sha256:")
    assert report["checks"]["business_trial"]["status"] == "not_run"
    assert report["checks"]["safety_check"]["status"] == "not_run"
    assert report["executed"] is False and report["execution_id"] is None and report["release_authorized"] is False
    assert report["trial_support"]["entry_tool"] == "agent_ci_request"
    assert "candidate_document" not in result
    changed = await resource.authoring.validate_change(**intent(target, value="Different proposed instructions."))
    assert changed["validation_report"]["candidate_hash"] != report["candidate_hash"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0
        saved = json.loads(db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()[0])
        assert saved["system_prompt"] == "Initial private instructions."


@pytest.mark.parametrize("missing", ["tool", "skill", "plugin"])
async def test_current_dependency_failure_is_not_reported_as_structural_or_trial_success(validation_context, missing):
    resource, target, _ = validation_context
    if missing == "tool":
        request = intent(target, "tool_refs", ["missing_tool"])
    elif missing == "skill":
        grants = await AgentDependencyCompiler().exact_grants_for_legacy_names(("agent-authoring",), team_id="team_1")
        grant = grants[0].to_canonical_dict()
        # An older revision is rebased, not blocked; a Skill that is gone still blocks.
        grant["skill_ref"] = {"kind": "team", "id": "oskill_missing", "name": "missing-skill"}
        request = intent(target, "skill_grants", [grant])
    else:
        request = intent(target, "capability_ids", ["tcap_missing"])
    result = await resource.authoring.validate_change(**request)
    assert result["validation_report"]["checks"]["structure"]["status"] == "passed"
    assert result["validation_report"]["checks"]["dependencies"]["status"] == "blocked"
    assert not result["executed"] and result["dependency_validation"]["reason_codes"]
    # Coach is told which item is unavailable, not only that something is.
    if missing == "tool":
        assert result["dependency_validation"]["details"] == {"rule": "tool_unavailable", "tool_refs": ["missing_tool"]}
    elif missing == "skill":
        assert result["dependency_validation"]["details"] == {"rule": "skill_unavailable", "skill_name": "missing-skill"}


async def test_draft_dependency_preflight_names_the_unavailable_item(validation_context):
    from types import SimpleNamespace

    from bowhead_core.team_subagent_validation import TeamSubagentValidationError
    from bowhead_gateway.agent_authoring_dependencies import draft_dependency_preflight

    resource, _, _ = validation_context
    definition = {"name": "draft-agent", "tool_refs": ["missing_tool"], "builtin_plugin_ids": ["builtin:web-research"]}
    draft = SimpleNamespace(workspace_id="ws_1")
    with pytest.raises(TeamSubagentValidationError) as rejected:
        await draft_dependency_preflight(resource.request, resource.user, draft, definition, "sha256:catalog")
    assert rejected.value.code == "subagent_dependencies_unavailable_or_changed"
    details = rejected.value.details
    assert details["reason"] == "plugin_dependencies_unavailable"
    assert details["rule"] == "tool_unavailable"
    assert "missing_tool" in details["tool_refs"]


@pytest.mark.parametrize("change", ["working_copy", "permission", "plugin"])
async def test_change_during_preflight_cannot_return_current_pass(validation_context, monkeypatch, change):
    from bowhead_gateway import agent_authoring_dependencies

    resource, target, path = validation_context
    request = intent(target)
    if change == "plugin":
        plugin = await TeamPluginStore(str(path)).create(
            team_id="team_1", actor_user_id="user_1", name="Web reading", description="Read supplied pages", tool_refs=["web_fetch"], skill_grants=[]
        )
        request = intent(target, "capability_ids", [plugin["id"]])
    compile_resources = agent_authoring_dependencies.compile_authoring_resources

    async def race(*args, **kwargs):
        result = await compile_resources(*args, **kwargs)
        with sqlite3.connect(path) as db:
            if change == "working_copy":
                db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1")
            elif change == "permission":
                db.execute("UPDATE workspace_members SET status='removed'")
            else:
                db.execute("UPDATE team_capability SET status='disabled'")
        return result

    monkeypatch.setattr(agent_authoring_dependencies, "compile_authoring_resources", race)
    if change == "plugin":
        result = await resource.authoring.validate_change(**request)
        assert result["validation_report"]["checks"]["dependencies"]["status"] == "blocked"
    else:
        with pytest.raises((AgentChangeProposalError, ValueError, PermissionError)):
            await resource.authoring.validate_change(**request)


async def test_removed_optional_resource_fields_compile_as_empty(validation_context):
    resource, target, _ = validation_context
    request = intent(target)
    request["operations"] = [{"op": "remove", "path": "/definition/skill_grants"}]
    result = await resource.authoring.validate_change(**request)
    assert result["validation_report"]["checks"]["dependencies"]["status"] == "passed"
