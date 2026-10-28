"""Coach tools and the Owner HTTP entry preserve the same proposal boundaries."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_gateway.agent_coach_resources import AgentCoachResources
from bowhead_gateway.routes import agents
from starlette.requests import Request
from test_agents_routes import _enable_revisioned_agent
from test_agents_routes import app_client as app_client

from bowhead_skill import default_catalog

pytestmark = pytest.mark.unit


async def working_agent(client, db_path):
    _enable_revisioned_agent(db_path)
    response = await client.patch(
        "/api/v1/agents/osa_1/working-copy",
        json={
            "definition": {"system_prompt": "Initial private instructions.", "tool_refs": [], "skill_grants": [], "max_iterations": 10},
            "listing": {"display_name": "Finance Helper", "description": "Owner draft"},
            "audience": {"schema_version": 1, "effective_mode": "private", "user_ids": [], "group_ids": []},
            "expected_row_version": None,
        },
    )
    assert response.status_code == 200, response.text
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT id,row_version,base_release_id FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()
    return {"kind": "agent_working_copy", "agent_id": "osa_1", "working_copy_id": row[0], "version": row[1], "base_release_id": row[2]}


def model_output():
    return SimpleNamespace(
        content=json.dumps(
            {
                "verdict": "edit",
                "operations": [{"op": "replace", "path": "/definition/system_prompt", "value": "Improved private instructions."}],
                "atomic_groups": [{"id": "behavior", "operation_indexes": [0]}],
                "suggested_cases": [],
                "rationale": "Clarify the task.",
            }
        )
    )


async def test_owner_retry_returns_original_proposal_without_paid_regeneration(app_client, monkeypatch):
    client, db_path = app_client
    await working_agent(client, db_path)
    model = SimpleNamespace(invoke=AsyncMock(return_value=model_output()))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(model, None)))
    kwargs = {"headers": {"Idempotency-Key": "exact-intent"}, "json": {"goal": "Clarify the instructions"}}
    first = await client.post("/api/v1/agents/osa_1/change-proposals/coach", **kwargs)
    assert first.status_code == 201, first.text
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1")
    replay = await client.post("/api/v1/agents/osa_1/change-proposals/coach", **kwargs)
    assert replay.status_code == 201 and replay.json()["proposal_id"] == first.json()["proposal_id"], replay.text
    conflict = await client.post("/api/v1/agents/osa_1/change-proposals/coach", headers=kwargs["headers"], json={"goal": "A different goal"})
    assert conflict.status_code == 409
    assert model.invoke.await_count == 1


async def test_owner_apply_with_unknown_group_names_the_failed_rule_and_groups(app_client, monkeypatch):
    client, db_path = app_client
    target = await working_agent(client, db_path)
    model = SimpleNamespace(invoke=AsyncMock(return_value=model_output()))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(model, None)))
    created = await client.post("/api/v1/agents/osa_1/change-proposals/coach", headers={"Idempotency-Key": "groups"}, json={"goal": "Clarify"})
    assert created.status_code == 201, created.text
    proposal = created.json()

    applied = await client.post(
        f"/api/v1/agents/osa_1/change-proposals/{proposal['proposal_id']}/apply",
        json={
            "selected_group_ids": ["tone"],
            "expected_proposal_row_version": proposal["row_version"],
            "expected_working_copy_row_version": target["version"],
        },
    )

    assert applied.status_code == 422, applied.text
    error = applied.json()["detail"]["error"]
    assert error["code"] == "agent_change_groups_invalid"
    assert error["details"]["rule"] == "selected_group_unknown"
    assert error["details"]["group_ids"] == ["tone"] and error["details"]["known_group_ids"] == ["behavior"]
    assert "not groups of this proposal" in error["details"]["message"]


async def test_owner_merges_into_a_draft_that_still_matches_the_released_version(app_client, monkeypatch):
    # 1.5.93 on 116: with no saved draft (the draft matched the release) the merge button stayed disabled.
    client, db_path = app_client
    await working_agent(client, db_path)
    model = SimpleNamespace(invoke=AsyncMock(return_value=model_output()))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(model, None)))
    created = await client.post("/api/v1/agents/osa_1/change-proposals/coach", headers={"Idempotency-Key": "no-draft"}, json={"goal": "Clarify"})
    assert created.status_code == 201, created.text
    proposal = created.json()
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM team_subagent_working_copy WHERE subagent_id='osa_1'")
        live_release_id = db.execute("SELECT live_release_id FROM team_subagent WHERE id='osa_1'").fetchone()[0]
    url = f"/api/v1/agents/osa_1/change-proposals/{proposal['proposal_id']}/apply"
    body = {"selected_group_ids": ["behavior"], "expected_proposal_row_version": proposal["row_version"]}

    both = await client.post(url, json={**body, "expected_working_copy_row_version": 1, "expected_live_release_id": live_release_id})
    assert both.status_code == 422, both.text
    stale = await client.post(url, json={**body, "expected_live_release_id": "arl_older"})
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "agent_working_copy_base_stale", stale.text
    applied = await client.post(url, json={**body, "expected_live_release_id": live_release_id})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "accepted" and applied.json()["working_copy"]["base_release_id"] == live_release_id
    with sqlite3.connect(db_path) as db:
        stored = db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()[0]
    assert json.loads(stored)["system_prompt"] == "Improved private instructions."
    # A draft that appeared since the page loaded is not merged into blind.
    again = await client.post(url, json={**body, "expected_live_release_id": live_release_id})
    assert again.status_code == 409, again.text


async def test_provider_timeout_is_durable_unknown_not_an_automatic_retry(app_client, monkeypatch):
    client, db_path = app_client
    await working_agent(client, db_path)
    model = SimpleNamespace(invoke=AsyncMock(side_effect=TimeoutError))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(model, None)))
    kwargs = {"headers": {"Idempotency-Key": "uncertain"}, "json": {"goal": "Clarify the instructions"}}
    for _ in range(2):
        result = await client.post("/api/v1/agents/osa_1/change-proposals/coach", **kwargs)
        assert result.status_code == 409, result.text
        assert result.json()["detail"]["error"]["code"] == "agent_coach_generation_unknown"
    assert model.invoke.await_count == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state FROM agent_coach_generation_request").fetchone()[0] == "unknown"
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0


async def test_owner_generation_rejects_unavailable_case_modes_without_regeneration(app_client, monkeypatch):
    from test_agent_coach_evaluations import case_spec

    client, db_path = app_client
    await working_agent(client, db_path)
    payload = json.loads(model_output().content)
    payload["suggested_cases"] = [
        {
            "id": "fixture",
            "task": "Read the fixture",
            "failure_mode": "Unavailable adapter",
            "covers_group_ids": ["behavior"],
            "case_spec": {**case_spec(), "fixture_ref": {"kind": "dataset", "ref": "dataset_synthetic", "version": "v1"}},
        }
    ]
    model = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(payload))))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(model, None)))
    kwargs = {"headers": {"Idempotency-Key": "unavailable-fixture"}, "json": {"goal": "Use reviewed evidence"}}
    rejected = await client.post("/api/v1/agents/osa_1/change-proposals/coach", **kwargs)
    assert rejected.json()["detail"]["error"]["code"] == "agent_eval_product_case_mode_unavailable", rejected.text
    replay = await client.post("/api/v1/agents/osa_1/change-proposals/coach", **kwargs)
    assert replay.json()["detail"]["error"]["code"] == "agent_coach_generation_failed", replay.text
    assert model.invoke.await_count == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state FROM agent_coach_generation_request").fetchone()[0] == "failed"
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0


async def test_owner_edit_during_generation_preserves_original_baseline(app_client, monkeypatch):
    client, db_path = app_client
    await working_agent(client, db_path)

    async def concurrent_edit(*_):
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1")
        return model_output()

    model = SimpleNamespace(invoke=AsyncMock(side_effect=concurrent_edit))
    monkeypatch.setattr(agents, "resolve_team_router", AsyncMock(return_value=(model, None)))
    kwargs = {"headers": {"Idempotency-Key": "stale"}, "json": {"goal": "Clarify the instructions"}}
    result = await client.post("/api/v1/agents/osa_1/change-proposals/coach", **kwargs)
    assert result.status_code == 409, result.text
    assert "version_conflict" in result.json()["detail"]["error"]["code"]
    replay = await client.post("/api/v1/agents/osa_1/change-proposals/coach", **kwargs)
    assert replay.json()["detail"]["error"]["code"] == "agent_coach_generation_failed"
    assert model.invoke.await_count == 1


async def test_owner_revocation_before_provider_boundary_prevents_generation(app_client, monkeypatch):
    client, db_path = app_client
    await working_agent(client, db_path)
    model = SimpleNamespace(invoke=AsyncMock(return_value=model_output()))

    async def revoke(*_, **__):
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
        return model, None

    monkeypatch.setattr(agents, "resolve_team_router", revoke)
    result = await client.post("/api/v1/agents/osa_1/change-proposals/coach", headers={"Idempotency-Key": "revoked"}, json={"goal": "Clarify"})
    assert result.status_code == 404, result.text
    model.invoke.assert_not_awaited()


async def test_tool_change_writes_the_draft_recoverably_and_bound_to_exact_target(app_client, monkeypatch):
    client, db_path = app_client
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    intent = {
        "target": target,
        "operations": [{"op": "replace", "path": "/definition/system_prompt", "value": "Proposed instructions."}],
        "atomic_groups": [{"id": "behavior", "operation_indexes": [0]}],
        "suggested_cases": [],
    }
    validated = await resources.authoring.validate_change(**intent)
    assert validated["validation_kind"] == "proposal_structure" and validated["safety_check"] == "not_run"
    proposal = await resources.authoring.propose_change(**intent, rationale="Clarify", request_id="edit", turn_id="turn_owner")
    assert proposal["effect"] == "draft_updated" and proposal["applied_by_this_call"] and not proposal["released"]
    assert proposal["proposal"]["status"] == "accepted" and proposal["working_copy_version"] == target["version"] + 1
    assert proposal["target"] == {**target, "version": target["version"] + 1}
    # Coach words the result with released numbers only and tests the change with the receipt's own target.
    with sqlite3.connect(db_path) as db:
        live = db.execute(
            "SELECT rel.release_sequence FROM team_subagent agent JOIN team_subagent_release rel ON rel.id=agent.live_release_id WHERE agent.id='osa_1'"
        ).fetchone()[0]
    assert (proposal["released_version"], proposal["release_as"]) == (f"v{live}", f"v{live + 1}")
    assert proposal["test_change"] == {"kind": "accepted_agent_proposal", "agent_id": "osa_1", "proposal_id": proposal["proposal"]["id"]}
    assert "agent_ci_request" in proposal["message"] and f"Release as v{live + 1}" in proposal["message"]
    with sqlite3.connect(db_path) as db:
        stored = db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()[0]
        assert json.loads(stored)["system_prompt"] == "Proposed instructions."
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1")
    replay = await resources.authoring.propose_change(**intent, rationale="Clarify", request_id="edit", turn_id="turn_owner")
    assert replay["proposal"]["id"] == proposal["proposal"]["id"] and not replay["applied_by_this_call"]
    assert replay["working_copy_version"] == proposal["working_copy_version"]
    read = await resources.authoring.get_change(agent_id="osa_1", proposal_id=proposal["proposal"]["id"])
    assert read["proposal"] == replay["proposal"]
    from bowhead_core.coach_operation_reference import with_operation_reference

    pointer = with_operation_reference("agent_change_propose", proposal)["operation_ref"]
    receipt = await resources.operations.get(pointer)
    assert receipt["domain_result"]["proposal"] == read["proposal"]
    assert not receipt["effects_applied_by_this_read"]


async def test_tool_requirement_tracks_the_owned_working_copy_and_rejects_new_owner(app_client, monkeypatch):
    from bowhead_core.agent_revision_store import AgentRevisionForbiddenError

    client, db_path = app_client
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    saved = await resources.tool_handoff.call(
        "agent_tool_requirement_create",
        {
            "parent": target,
            "requirement": {"workflow": "engineering", "operation": "Read the required dataset", "input_schema": {}},
            "request_id": "one",
        },
        turn_id="t_original",
    )
    assert saved["original_parent"] == target and saved["original_parent_context"]["audience"]["effective_mode"] == "private"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1 WHERE subagent_id='osa_1'")
    read = await resources.tool_handoff.call("agent_tool_requirement_get", {"requirement_id": saved["requirement_id"]}, turn_id="t_later")
    assert not read["parent_status"]["baseline_matches"] and read["original_parent"] == target
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    with pytest.raises(AgentRevisionForbiddenError):
        await resources.tool_handoff.call("agent_tool_requirement_get", {"requirement_id": saved["requirement_id"]}, turn_id="t_later")


async def test_canonical_resource_patch_preserves_other_fields_and_writes_no_release(app_client, monkeypatch):
    from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler
    from bowhead_core.team_plugin_store import TeamPluginStore

    client, db_path = app_client
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    grants = await AgentDependencyCompiler().exact_grants_for_legacy_names(("agent-authoring",), team_id="team_1")
    exact = [grant.to_canonical_dict() for grant in grants]
    plugin = await TeamPluginStore(str(db_path)).create(
        team_id="team_1", actor_user_id="user_1", name="Authoring methods", description="Shared methods", tool_refs=[], skill_grants=exact
    )
    values = {
        "tool_refs": ["skill_read"],
        "skill_grants": exact,
        "capability_ids": [plugin["id"]],
        "model": "analyst",
        "max_iterations": 12,
        "output_schema": {"type": "object"},
    }
    operations = [{"op": "add", "path": "/definition/" + field, "value": value} for field, value in values.items()]
    intent = {
        "target": target,
        "operations": operations,
        "atomic_groups": [{"id": "resources", "operation_indexes": list(range(6))}],
        "suggested_cases": [],
    }
    preview = await resources.authoring.validate_change(**intent)
    assert preview["validation_kind"] == "proposal_structure" and preview["evaluation"] == "not_run"
    with sqlite3.connect(db_path) as db:
        before = json.loads(db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()[0])
        release_count = db.execute("SELECT count(*) FROM team_subagent_release").fetchone()[0]
    receipt = await resources.authoring.propose_change(**intent, rationale="Bind the exact methods", request_id="bindings", turn_id="turn_one")
    proposal = receipt["proposal"]
    with sqlite3.connect(db_path) as db:
        after = json.loads(db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()[0])
        assert db.execute("SELECT count(*) FROM team_subagent_release").fetchone()[0] == release_count
    assert all(after[field] == value for field, value in values.items())
    assert after["system_prompt"] == before["system_prompt"]
    assert not {"tool_refs_json", "skill_refs_json", "capability_refs_json"}.intersection(after)
    replay = await resources.authoring.propose_change(**intent, rationale="Bind the exact methods", request_id="bindings", turn_id="turn_one")
    assert replay["proposal"]["id"] == proposal["id"] and replay["proposal"]["status"] == "accepted"


async def test_combined_case_proposals_reject_unavailable_product_fixtures_before_persistence(app_client, monkeypatch):
    from bowhead_core.agent_eval_store import AgentEvalStoreError
    from test_agent_coach_evaluations import case_spec

    client, db_path = app_client
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    intent = {
        "target": target,
        "operations": [{"op": "replace", "path": "/definition/system_prompt", "value": "Use reviewed evidence."}],
        "atomic_groups": [{"id": "behavior", "operation_indexes": [0]}],
        "suggested_cases": [
            {
                "id": "fixture-case",
                "task": "Read the fixture",
                "failure_mode": "Missing adapter",
                "covers_group_ids": ["behavior"],
                "case_spec": {**case_spec(), "fixture_ref": {"kind": "unsupported_business_fixture"}},
            }
        ],
    }
    with pytest.raises(AgentEvalStoreError, match="product_case_mode_unavailable"):
        await resources.authoring.validate_change(**intent)
    with pytest.raises(AgentEvalStoreError, match="product_case_mode_unavailable"):
        await resources.authoring.propose_change(**intent, rationale="Review the fixture", request_id="fixture", turn_id="turn_owner")
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_eval_suite").fetchone()[0] == 0


async def test_combined_cases_keep_independent_owner_decisions_after_the_draft_write(app_client, monkeypatch):
    from test_agent_coach_evaluations import case_spec

    client, db_path = app_client
    target = await working_agent(client, db_path)
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: None)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
        original = db.execute("SELECT definition_json FROM team_subagent_working_copy").fetchone()[0]
    created = await client.post(
        "/api/v1/agents/osa_1/eval-suites", json={"name": "Quality", "cases": [{**case_spec(), "case_key": "existing", "task": "Review the result"}]}
    )
    assert created.status_code in (200, 201), created.text
    suite = (await resources.evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    intent = {
        "target": target,
        "operations": [{"op": "replace", "path": "/definition/system_prompt", "value": "Proposed instructions."}],
        "atomic_groups": [{"id": "behavior", "operation_indexes": [0]}],
        "suggested_cases": [
            {
                "id": "reviewed",
                "task": "Explain missing results",
                "failure_mode": "Invented records",
                "covers_group_ids": ["behavior"],
                "case_spec": case_spec(),
            },
            {"id": "later", "task": "Review ambiguous input", "failure_mode": "Unstated assumptions", "covers_group_ids": ["behavior"]},
        ],
    }
    await resources.authoring.validate_change(**intent)
    proposal = (await resources.authoring.propose_change(**intent, rationale="Cover edge cases", request_id="cases", turn_id="turn_owner"))[
        "proposal"
    ]
    accepted = await client.post(
        f"/api/v1/agents/osa_1/change-proposals/{proposal['id']}/suggested-cases/accept",
        json={
            "expected_proposal_row_version": proposal["row_version"],
            "expected_active_suite_revision_id": suite["revision_id"],
            "cases": [{"suggested_case_id": "reviewed", "edited_case": {**case_spec(), "task": "Owner-reviewed missing results"}}],
        },
    )
    assert accepted.status_code == 200, accepted.text
    read = (await resources.authoring.get_change(agent_id="osa_1", proposal_id=proposal["id"]))["proposal"]
    assert {item["id"]: item["decision_status"] for item in read["suggested_cases"]} == {"reviewed": "accepted", "later": "pending"}
    with sqlite3.connect(db_path) as db:
        definition = json.loads(db.execute("SELECT definition_json FROM team_subagent_working_copy").fetchone()[0])
        assert definition == {**json.loads(original), "system_prompt": "Proposed instructions."}
        kinds = sorted(row[0] for row in db.execute("SELECT decision_kind FROM agent_change_proposal_decision"))
        assert kinds == ["cases_accept", "patch_accept"]


async def test_invalid_builtin_plugin_selection_blocks_dependencies_instead_of_failing(app_client, monkeypatch):
    from bowhead_core.system_plugins import BuiltinPluginSelectionError

    from bowhead_gateway import builtin_plugin_fields

    client, db_path = app_client
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")

    def unselectable(_definition):
        # A built-in Plugin that stopped being selectable after the proposal was written.
        raise BuiltinPluginSelectionError("builtin_plugin_not_selectable")

    monkeypatch.setattr(builtin_plugin_fields, "expand_selected_builtin_plugins", unselectable)
    validated = await resources.authoring.validate_change(
        target=target,
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Proposed instructions."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
        suggested_cases=[],
    )
    assert validated["dependency_validation"]["status"] == "blocked"
    assert validated["dependency_validation"]["reason_codes"] == ["agent_dependencies_unavailable_or_changed"]


async def _coach_resources(client, db_path, monkeypatch):
    target = await working_agent(client, db_path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Authoring','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    return target, AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")


async def test_coach_without_edit_rights_writes_nothing(app_client, monkeypatch):
    client, db_path = app_client
    target, resources = await _coach_resources(client, db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    result = await resources.authoring.propose_change(
        target=target,
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Coach instructions."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
        suggested_cases=[],
        rationale="Clarify",
        request_id="edit",
        turn_id="turn_owner",
    )
    assert result["ok"] is False and result["error"] == "agent_edit_permission_required" and not result["written"]
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0


async def test_coach_adding_a_skill_without_its_required_skills_writes_nothing(app_client, monkeypatch):
    client, db_path = app_client
    target, resources = await _coach_resources(client, db_path, monkeypatch)
    client._transport.app.state.skill_catalog = default_catalog()
    catalog = (await client.get("/api/v1/agents/authoring-catalog")).json()["skills"]
    method = next(item for item in catalog if item["name"] == "analyst-method-roe-dupont")
    framework = next(item for item in catalog if item["name"] == "analyst-framework")
    change = {
        "target": target,
        "atomic_groups": [{"id": "skills", "operation_indexes": [0]}],
        "suggested_cases": [],
        "rationale": "Add ROE analysis",
        "turn_id": "turn_owner",
    }
    result = await resources.authoring.propose_change(
        **change, operations=[{"op": "replace", "path": "/definition/skill_grants", "value": [method["grant"]]}], request_id="alone"
    )
    assert result == {
        "ok": False,
        "error": "agent_skill_dependency_missing",
        "missing_skills": ["analyst-framework"],
        "written": False,
        "message": result["message"],
    }
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0
    written = await resources.authoring.propose_change(
        **change,
        operations=[{"op": "replace", "path": "/definition/skill_grants", "value": [method["grant"], framework["grant"]]}],
        request_id="together",
    )
    assert written["effect"] == "draft_updated" and written["applied_by_this_call"]
