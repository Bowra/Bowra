"""Resource completion returns to the original parent through existing proposals."""

import copy
import sqlite3
from dataclasses import replace

import pytest
from bowhead_core.agent_coach import ResourceBindingContext
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_draft_bindings import activate
from test_agent_coach_lifecycle import compose_resources
from test_agent_coach_resources import resources as resources
from test_agent_coach_skills import skill_adapter as skill_adapter
from test_agents_routes import app_client as app_client
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.integration


def prepared(resources, saved_draft, skill_adapter):
    activate(resources)
    composed = compose_resources(resources, saved_draft, skill_adapter)
    draft = saved_draft[3]
    skill = composed["skill"]["result"]["target"]
    plugin = composed["plugin"]["result_target"]
    tool = composed["observed"]["observation"]["reference"]
    patch = {**composed["patch"], "tool_refs": ["web_fetch", tool["id"]]}
    context = ResourceBindingContext.model_validate(
        {
            "original_parent": {"kind": "agent_draft", "id": draft.id, "version": draft.row_version},
            "resources": [
                {
                    "reference": {"kind": "skill", "source_kind": "user", "id": skill["id"], "revision": skill["revision"]},
                    "source_operation": {"kind": "skill_save", "operation_id": composed["skill"]["operation_id"]},
                },
                {
                    "reference": {"kind": "capability", "id": plugin["id"], "revision": str(plugin["revision"])},
                    "source_operation": {"kind": "plugin_save", "operation_id": composed["plugin"]["operation_id"]},
                },
                {
                    "reference": tool,
                    "source_operation": {
                        "kind": "tool_requirement",
                        "requirement_id": composed["requirement"]["requirement_id"],
                        "observation_id": composed["observed"]["observation"]["id"],
                    },
                },
            ],
        }
    ).model_dump()
    return patch, context


def propose(adapter, patch, context, request="return"):
    return _run(adapter.propose(patch, request_id=request, turn_id="t_return", resource_context=context))


def test_resource_return_is_a_reviewed_suggestion_and_recovers_after_parent_changes(resources, saved_draft, skill_adapter):
    patch, context = prepared(resources, saved_draft, skill_adapter)
    adapter, store, users, draft, owner, _, client = saved_draft
    newer = _run(
        store.save_manual_draft(
            draft.id,
            team_id=owner.team_id,
            owner_user_id=owner.id,
            workspace_id="ws_a",
            expected_version=draft.row_version,
            purpose=draft.purpose_text,
            definition={**draft.definition, "description": "Newer Owner input"},
            text_only_confirmed=False,
        )
    )
    current = replace(adapter, editor=replace(adapter.editor, expected_version=newer.row_version))
    receipt = propose(current, patch, context)
    assert receipt["ok"], receipt
    proposal = receipt["proposal"]
    assert proposal["payload"]["resource_context"] == context
    assert proposal["base_version"] == newer.row_version
    assert proposal["payload"]["definition"]["description"] == "Newer Owner input"
    original = _run(store.get_draft(draft.id, team_id=owner.team_id, owner_user_id=owner.id))
    assert original.definition["skill_grants"] == []
    applied = client.post(
        f"/api/v1/team-subagents/drafts/{draft.id}/suggestions/{proposal['id']}/apply",
        headers={"X-Space-Id": "ws_a", "If-Match": f'W/"{draft.id}:{newer.row_version}"'},
        json={
            "schema_version": 2,
            "selected_fields": ["skill_grants", "capability_ids", "tool_refs"],
            "mutation_id": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["definition"]["description"] == "Newer Owner input"
    assert applied.json()["definition"]["skill_grants"] == patch["skill_grants"]
    with sqlite3.connect(users.db_path) as db:
        db.execute("UPDATE team_tool_source SET status='disabled'")
    replay = propose(current, patch, context)
    assert replay["ok"] and replay["proposal"]["id"] == proposal["id"], replay
    changed = copy.deepcopy(context)
    changed["resources"][0]["source_operation"] = None
    assert propose(current, patch, changed)["error"] == "idempotency_key_reused"


@pytest.mark.parametrize("failure", ["wrong_parent", "wrong_receipt", "unbound", "stale", "denied", "duplicate"])
def test_invalid_child_results_never_create_a_binding_suggestion(resources, saved_draft, skill_adapter, failure):
    patch, context = prepared(resources, saved_draft, skill_adapter)
    if failure == "wrong_parent":
        context["original_parent"]["id"] = "sad_other"
    elif failure == "wrong_receipt":
        context["resources"][0]["source_operation"] = context["resources"][1]["source_operation"]
    elif failure == "unbound":
        patch["skill_grants"] = []
    elif failure == "stale":
        context["resources"][0]["reference"]["revision"] = "sha256:" + "0" * 64
    elif failure == "denied":
        with sqlite3.connect(resources.store.db_path) as db:
            db.execute("UPDATE team_tool_source SET status='disabled'")
    else:
        context["resources"].append(context["resources"][0])
    result = propose(saved_draft[0], patch, context)
    assert not result["ok"], result
    store, draft, owner = saved_draft[1], saved_draft[3], saved_draft[4]
    assert _run(store.list_canvas_suggestions(draft.id, team_id=owner.team_id, owner_user_id=owner.id, workspace_id="ws_a")) == []


def test_authorized_resource_can_be_bound_without_claiming_another_creators_private_receipt(resources, saved_draft, skill_adapter):
    patch, context = prepared(resources, saved_draft, skill_adapter)
    for item in context["resources"]:
        item["source_operation"] = None
    result = propose(saved_draft[0], patch, context)
    assert result["ok"], result
    assert all(item["source_operation"] is None for item in result["proposal"]["payload"]["resource_context"]["resources"])


async def test_existing_agent_return_preserves_original_provenance_and_replays_before_revalidation(app_client, monkeypatch):
    from bowhead_gateway.agent_coach_resources import AgentCoachResources
    from bowhead_gateway.routes import agents
    from starlette.requests import Request
    from test_agent_coach_proposals import working_agent

    client, db_path = app_client
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    page = await resources.search(kind="tool", query="web_fetch", offset=0, limit=50)
    reference = next(item["reference"] for item in page["items"] if item["reference"]["id"] == "web_fetch")
    context = ResourceBindingContext.model_validate({"original_parent": target, "resources": [{"reference": reference}]}).model_dump()
    intent = {
        "target": target,
        "operations": [{"op": "replace", "path": "/definition/tool_refs", "value": ["web_fetch"]}],
        "atomic_groups": [{"id": "resources", "operation_indexes": [0]}],
        "suggested_cases": [],
        "rationale": "Bind the selected Tool",
        "request_id": "bind",
        "turn_id": "t_return",
        "resource_context": context,
    }
    wrong = copy.deepcopy(intent)
    wrong["request_id"] = "unbound"
    wrong["operations"][0]["value"] = []
    with pytest.raises(ValueError, match="resource_binding_not_in_candidate"):
        await resources.authoring.propose_change(**wrong)
    first = await resources.authoring.propose_change(**intent)
    assert first["proposal"]["provenance"]["resource_context"] == context
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1")
    replay = await resources.authoring.propose_change(**intent)
    assert replay["proposal"]["id"] == first["proposal"]["id"]
    context["resources"][0]["source_operation"] = {"kind": "skill_save", "operation_id": "csw_" + "0" * 64}
    from bowhead_core.agent_change_proposal_store import AgentChangeProposalError

    with pytest.raises(AgentChangeProposalError, match="idempotency_conflict"):
        await resources.authoring.propose_change(**intent)
