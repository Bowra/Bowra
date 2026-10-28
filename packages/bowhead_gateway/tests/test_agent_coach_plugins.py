"""Plugin composition uses real discovery, grants, compiler and current roles."""

import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.team_plugin_store import TeamPluginError
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_resources import resources as resources
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.unit


def administrator(resources):
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?", (resources.user.team_id, resources.user.id))


def exact_method(resources):
    page = _run(resources.search(kind="skill", query="agent-authoring", offset=0, limit=50))
    item = next(item for item in page["items"] if item["reference"]["id"] == "builtin:agent-authoring")
    return _run(resources.get(reference=item["reference"]))["resource"]["exact_read_grant"]


def create(resources, *, key="one"):
    return _run(
        resources.plugins.call(
            "agent_plugin_save",
            {
                "target": None,
                "patch": {"name": "Shared methods", "skill_grants": [exact_method(resources)]},
                "expected_impact": None,
                "request_id": key,
            },
            turn_id="t_original",
        )
    )


def test_real_compiler_preserves_exact_grant_and_focused_edit(resources):
    administrator(resources)
    saved = create(resources)
    assert saved["ok"] and saved["dependency_validation"]["status"] == "ready", saved
    assert not saved["agent_bound"] and not saved["executed"]
    inspected = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": saved["result_target"]["id"]}, turn_id="t_later"))
    before = inspected["plugin"]
    args = {"target": inspected["target"], "expected_impact": inspected["impact"]["fingerprint"], "patch": {"description": "Clarified purpose"}}
    preview = _run(resources.plugins.call("agent_plugin_validate", args, turn_id="t_later"))
    assert not preview["persisted"] and preview["candidate"]["skill_grants"] == before["skill_grants"]
    report = preview["validation_report"]
    assert report["checks"]["dependencies"]["status"] == "passed"
    assert report["checks"]["business_trial"]["status"] == "not_run" and report["execution_id"] is None
    assert report["trial_support"]["status"] == "no_standalone_executor"
    changed = _run(resources.plugins.call("agent_plugin_save", {**args, "request_id": "edit"}, turn_id="t_later"))
    assert changed["result_target"]["revision"] == 2
    current = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": before["id"]}, turn_id="t_later"))
    assert current["plugin"]["name"] == before["name"] and current["plugin"]["skill_grants"] == before["skill_grants"]
    assert create(resources)["operation_id"] == saved["operation_id"]
    with pytest.raises(ValueError, match="plugin_name_conflict"):
        create(resources, key="duplicate_name")
    recovered = _run(resources.plugins.call("agent_plugin_operation_get", {"operation_id": saved["operation_id"]}, turn_id="t_recovery"))
    assert not recovered["current_result_matches"]


def test_older_skill_revision_is_saved_at_the_validated_current_revision(resources):
    administrator(resources)
    grant = exact_method(resources)
    older = {**grant, "revision": "sha256:" + "0" * 64}
    assert older["revision"] != grant["revision"]
    saved = _run(
        resources.plugins.call(
            "agent_plugin_save",
            {"target": None, "patch": {"name": "Shared methods", "skill_grants": [older]}, "expected_impact": None, "request_id": "older"},
            turn_id="t_original",
        )
    )
    assert saved["ok"] and saved["dependency_validation"]["status"] == "ready", saved
    inspected = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": saved["result_target"]["id"]}, turn_id="t_later"))
    assert inspected["plugin"]["skill_grants"] == [grant]


def test_current_administrator_and_chat_access_are_required(resources, saved_draft):
    with pytest.raises(ValueError, match="team_admin_required"):
        create(resources)
    administrator(resources)
    saved = create(resources)
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE teams SET created_by_user_id=? WHERE id=?", (saved_draft[5].id, resources.user.team_id))
        db.execute("UPDATE team_members SET role='member' WHERE team_id=? AND user_id=?", (resources.user.team_id, resources.user.id))
    with pytest.raises(ValueError, match="team_admin_required"):
        create(resources)
    assert (
        _run(resources.plugins.call("agent_plugin_operation_get", {"operation_id": saved["operation_id"]}, turn_id="t_later"))["state"] == "succeeded"
    )
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        _run(
            replace(resources, session_id="s_someone_else").plugins.call(
                "agent_plugin_operation_get", {"operation_id": saved["operation_id"]}, turn_id="t_later"
            )
        )


def test_private_skills_cannot_be_shared(resources):
    administrator(resources)
    grant = exact_method(resources)
    # An older Skill revision is rebased to the current one (agent-skill-gates D2), so only kind is checked here.
    for changed, error in [
        ({**grant, "skill_ref": {**grant["skill_ref"], "kind": "user"}}, "capability_skill_invalid"),
    ]:
        with pytest.raises((ValueError, TeamPluginError), match=error):
            _run(
                resources.plugins.call(
                    "agent_plugin_save",
                    {"target": None, "patch": {"name": "Unusable", "skill_grants": [changed]}, "expected_impact": None, "request_id": "invalid"},
                    turn_id="t_original",
                )
            )
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM coach_plugin_operation").fetchone()[0] == 0


def test_changed_tool_revision_is_not_silently_bound(resources):
    administrator(resources)
    page = _run(resources.search(kind="tool", query="web_search", offset=0, limit=50))
    reference = next(item["reference"] for item in page["items"] if item["reference"]["id"] == "web_search")
    args = {
        "target": None,
        "patch": {"name": "Research tools", "tools": [{**reference, "revision": "stale"}]},
        "expected_impact": None,
        "request_id": "one",
    }
    with pytest.raises(ValueError, match="unavailable_or_changed"):
        _run(resources.plugins.call("agent_plugin_save", args, turn_id="t_original"))
    result = _run(resources.plugins.call("agent_plugin_save", {**args, "patch": {**args["patch"], "tools": [reference]}}, turn_id="t_original"))
    assert result["ok"], result


def test_method_cannot_gain_invocation_without_scripts(resources):
    administrator(resources)
    grant = {**exact_method(resources), "permissions": ["invoke", "read"]}
    args = {"target": None, "patch": {"name": "Uninvokable", "skill_grants": [grant]}, "expected_impact": None}
    preview = _run(resources.plugins.call("agent_plugin_validate", args, turn_id="t_original"))
    assert preview["dependency_validation"]["status"] == "blocked"
    assert "skill_not_invokable" in preview["dependency_validation"]["reason_codes"]
    saved = _run(resources.plugins.call("agent_plugin_save", {**args, "request_id": "one"}, turn_id="t_original"))
    assert not saved["ok"] and not saved["persisted"]


def test_implied_wrapper_permissions_block_shared_save(resources):
    from bowhead_core.access_control import AccessControlStore

    administrator(resources)
    grant = exact_method(resources)
    _run(
        AccessControlStore(resources.store.db_path).create_or_update_resource_grant(
            team_id=resources.user.team_id,
            resource_kind="tool",
            resource_id="skill_read",
            principal_type="user",
            principal_id=resources.user.id,
            permission="use",
            effect="deny",
        )
    )
    saved = _run(
        resources.plugins.call(
            "agent_plugin_save",
            {"target": None, "patch": {"name": "Wrapper denied", "skill_grants": [grant]}, "expected_impact": None, "request_id": "one"},
            turn_id="t_original",
        )
    )
    assert not saved["ok"] and saved["error"] == "plugin_dependencies_blocked"
    assert saved["dependency_validation"]["reason_codes"] == ["plugin_tool_unavailable"]
