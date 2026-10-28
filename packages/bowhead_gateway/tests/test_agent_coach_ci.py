"""Coach CI uses explicit Owner consent and recovers real stored executions."""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.coach_ci_delegation import CoachCIError
from bowhead_gateway.agent_coach_resources import AgentCoachResources
from bowhead_gateway.routes import agents
from starlette.requests import Request
from test_agent_coach_proposals import working_agent
from test_agents_routes import _PassingSemanticScanner
from test_agents_routes import app_client as app_client

pytestmark = pytest.mark.unit


class _Settings:
    def __init__(self, values: dict[str, str]):
        self.values = values

    async def get(self, _user_id: str, key: str) -> str | None:
        return self.values.get(key)


async def setup(client, path, monkeypatch, *, after_edit: str = "ask"):
    target = await working_agent(client, path)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    app.state.user_settings_store = _Settings({"agent_coach_after_edit": after_edit})
    user = app.dependency_overrides[agents._current_user]()
    resources = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_ci", "s_ci")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO workspaces(id,team_id,name,status,revision,created_at,updated_at) VALUES('ws_ci','team_1','Evaluation','active',1,'2026-10-03','2026-10-03')"
        )
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_ci','user_1','admin','active','2026-10-03')")
        db.execute(
            "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES('s_ci','user_1','ws_ci','2026-10-03','2026-10-03')"
        )
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_ci','s_ci',1,'Improve this Agent','2026-10-03')")
    proposal = (
        await resources.authoring.propose_change(
            target=target,
            operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Explain the monthly result."}],
            atomic_groups=[{"id": "instructions", "operation_indexes": [0]}],
            suggested_cases=[],
            rationale="Clarify",
            request_id="edit",
            turn_id="t_ci",
        )
    )["proposal"]
    assert proposal["status"] == "accepted"
    # Persist a real safety report using a controlled semantic fixture; no provider runs.
    monkeypatch.setattr(agents, "RouterAgentSemanticSafetyScanner", _PassingSemanticScanner)
    with sqlite3.connect(path) as db:
        working = db.execute("SELECT id,row_version FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()
    safety = await client.post("/api/v1/agents/osa_1/safety-checks", json={"working_copy_id": working[0], "working_copy_row_version": working[1]})
    assert safety.status_code == 200, safety.text
    args = {
        "target": {"kind": "accepted_agent_proposal", "agent_id": "osa_1", "proposal_id": proposal["id"]},
        "budget": {"max_provider_calls": 12, "max_comparisons": 2, "duration_seconds": 600},
        "request_id": "ci",
        "turn_id": "t_ci",
    }
    if after_edit == "check":
        runtime = SimpleNamespace(db_path=str(path), model_hash=AsyncMock(return_value="sha256:" + "a" * 64), prepare_and_schedule=AsyncMock())
        monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: runtime)
    pending = (await resources.ci.request(**args))["delegation"]
    target = {"kind": "agent_ci_delegation", "agent_id": "osa_1", "delegation_id": pending["delegation_id"]}
    return resources, args, pending, target


async def test_check_after_edit_setting_starts_the_check_on_coachs_own_write(app_client, monkeypatch):
    client, path = app_client
    resources, args, started, target = await setup(client, path, monkeypatch, after_edit="check")
    assert started["state"] == "authorized" and started["budget"] == {"max_provider_calls": 200, "max_comparisons": 1, "duration_seconds": 1800}
    assert len(started["executions"]) == 1 and started["executions"][0]["run_kind"] == "release"
    replay = await resources.ci.request(**args)
    assert replay["next_action"] == "check_running" and replay["replayed"]
    assert replay["delegation"]["delegation_id"] == started["delegation_id"]
    # Asking again for the same write under a new request id gets the same check, not another budget.
    again = await resources.ci.request(**{**args, "request_id": "ci_again"})
    assert again["delegation"]["delegation_id"] == started["delegation_id"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM coach_ci_delegation").fetchone()[0] == 1
    # A check on another Turn's change is not covered by the standing setting.
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_other','s_ci',2,'Check again','2026-10-03')")
    other = await resources.ci.request(**{**args, "turn_id": "t_other", "request_id": "ci2"})
    assert other["next_action"] == "owner_review_ci_budget" and other["delegation"]["state"] == "requested"


async def test_a_new_standing_check_supersedes_the_previous_one(app_client, monkeypatch):
    client, path = app_client
    resources, args, first, target = await setup(client, path, monkeypatch, after_edit="check")
    # A check the Owner asked for on another Turn is theirs to stop; the setting never supersedes it.
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_manual','s_ci',2,'Check this','2026-10-03')")
    manual = (await resources.ci.request(**{**args, "turn_id": "t_manual", "request_id": "manual"}))["delegation"]
    assert manual["state"] == "requested"
    with sqlite3.connect(path) as db:
        working = db.execute("SELECT id,row_version,base_release_id FROM team_subagent_working_copy WHERE subagent_id='osa_1'").fetchone()
    second = await resources.authoring.propose_change(
        target={
            "kind": "agent_working_copy",
            "agent_id": "osa_1",
            "working_copy_id": working[0],
            "version": working[1],
            "base_release_id": working[2],
        },
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Explain the quarterly result."}],
        atomic_groups=[{"id": "instructions", "operation_indexes": [0]}],
        suggested_cases=[],
        rationale="Clarify again",
        request_id="edit2",
        turn_id="t_ci",
    )
    rerun = await resources.ci.request(**{**args, "request_id": "ci2", "target": {**args["target"], "proposal_id": second["proposal_id"]}})
    assert rerun["next_action"] == "check_running"
    stopped = await resources.ci.get(target)
    assert stopped["state"] == "revoked" and stopped["executions"][0]["status"] == "cancelled"
    kept = await resources.ci.get({**target, "delegation_id": manual["delegation_id"]})
    assert kept["state"] == "requested"
    with sqlite3.connect(path) as db:
        states = dict(db.execute("SELECT id,state FROM team_subagent_release_candidate").fetchall())
    assert states[first["preparation"]["candidate_id"]] == "withdrawn"
    assert states[rerun["delegation"]["preparation"]["candidate_id"]] == "prepared"


async def test_estimate_over_the_cap_waits_for_the_owner(app_client, monkeypatch):
    async def over_cap(_db, row, *, has_execution):
        return {"status": "available", "may_exhaust_call_cap": True, "model_call_cap": row["max_provider_calls"]}

    monkeypatch.setattr("bowhead_core.agent_eval_insights.coach_estimate_conn", over_cap)
    client, path = app_client
    resources, args, pending, _ = await setup(client, path, monkeypatch, after_edit="check")
    assert pending["state"] == "requested" and pending["executions"] == []


async def test_owner_confirmation_precedes_actual_preparation_and_comparison(app_client, monkeypatch):
    client, path = app_client
    resources, args, pending, target = await setup(client, path, monkeypatch)
    assert pending["state"] == "requested" and pending["executions"] == []
    with pytest.raises(CoachCIError, match="not_authorized"):
        await resources.ci.prepare(target)
    url = f"/api/v1/agents/osa_1/coach-ci/{pending['delegation_id']}"
    assert (await client.post(url + "/authorize", json={"expected_version": 1, "confirm_budget": False})).status_code == 422
    approved = await client.post(url + "/authorize", json={"expected_version": 1, "confirm_budget": True})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "authorized" and approved.json()["preparation"] is None
    # Use the actual comparison writer and materialized jobs. Only scheduling is
    # controlled because the HTTP fixture has no worker lifespan or real model.
    runtime = SimpleNamespace(db_path=str(path), model_hash=AsyncMock(return_value="sha256:" + "a" * 64), prepare_and_schedule=AsyncMock())
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: runtime)
    started = await client.post(url + "/start")
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["execution"]["origin_kind"] == "internal_coach"
    assert body["execution"]["coach_delegation_id"] == pending["delegation_id"]
    assert runtime.prepare_and_schedule.await_count == len(body["execution"]["runs"])
    runtime.model_hash.side_effect = RuntimeError("Model configuration removed")
    replay = await client.post(url + "/start")
    assert replay.status_code == 202, replay.text
    assert replay.json()["replayed"] and replay.json()["execution"]["execution_id"] == body["execution"]["execution_id"]
    assert runtime.prepare_and_schedule.await_count == len(body["execution"]["runs"])
    assert (await resources.ci.request(**args))["delegation"]["delegation_id"] == pending["delegation_id"]
    listing = (await client.get("/api/v1/agents/osa_1/coach-ci")).json()
    assert len(listing["items"]) == 1 and "context" not in listing["items"][0]
    stopped = await client.post(url + "/cancel")
    assert stopped.status_code == 200 and stopped.json()["state"] == "revoked", stopped.text
    assert (await client.get(url)).json()["executions"][0]["status"] == "cancelled"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_comparison_execution").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM agent_eval_snapshot").fetchone()[0] == 0


async def test_original_scope_and_consent_changes_are_not_retargeted(app_client, monkeypatch):
    client, path = app_client
    resources, _args, pending, target = await setup(client, path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES('ws_other','team_1','Other','active','2026-10-03','2026-10-03')"
        )
        db.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_other','user_1','admin','active','2026-10-03')"
        )
    wrong = replace(resources, workspace_id="ws_other", session_id="")
    with pytest.raises(CoachCIError, match="not_found"):
        await wrong.ci.get(target)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=row_version+1 WHERE subagent_id='osa_1'")
    url = f"/api/v1/agents/osa_1/coach-ci/{pending['delegation_id']}"
    stale = await client.post(url + "/authorize", json={"expected_version": 1, "confirm_budget": True})
    assert stale.status_code == 422 and stale.json()["detail"]["error"]["code"] == "coach_ci_context_changed", stale.text
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    assert (await client.get(url)).status_code == 404
    assert (await client.post(url + "/cancel")).status_code == 404


async def test_common_ci_receipt_read_never_authorizes_or_prepares(app_client, monkeypatch):
    from bowhead_core.coach_operation_reference import with_operation_reference

    client, path = app_client
    resources, _, pending, _ = await setup(client, path, monkeypatch)
    pointer = with_operation_reference("agent_ci_request", {"ok": True, "delegation": pending})["operation_ref"]
    recovered = await resources.operations.get(pointer)
    assert recovered["ok"] and recovered["domain_result"]["delegation"]["state"] == "requested"
    assert recovered["domain_result"]["delegation"]["preparation"] is None
    assert recovered["domain_result"]["delegation"]["executions"] == []
    assert not recovered["effects_applied_by_this_read"]
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        await replace(resources, session_id="s_foreign").operations.get(pointer)
