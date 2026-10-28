"""HTTP boundary of the Owner's comparison refreshing a test plan that is behind the latest saved test cases."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_plan_store import AgentEvalPlanStore
from bowhead_core.agent_eval_policy_store import AgentEvalPolicyDraft, AgentEvalPolicyStore, AgentEvalPolicySuite
from bowhead_core.agent_eval_store import EvalCaseDraft, append_eval_suite_cases_conn
from bowhead_core.agent_version_automation import AgentVersionAutomationStore
from bowhead_core.agent_version_service import AgentVersionError
from bowhead_core.openapi_store import OpenApiStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import agents, team_openapi
from bowhead_gateway.routes.auth import _current_user, _store
from fastapi import FastAPI, HTTPException
from test_agent_eval_run_insights_routes import _H, _NOW, _REAL_USERS, _insert_candidate, _seed_real

pytestmark = pytest.mark.integration

_COMPARISONS = "/api/v1/agents/osa_real/release-candidates/arc_real/comparisons"


async def _prepared(db_path) -> None:
    draft = AgentEvalPolicyDraft.default(
        owner_suites=(AgentEvalPolicySuite(suite_revision_id="aev_owner", required=True, tier="standard", execution_scope="paired", attempts=3),),
    )
    policy = await AgentEvalPolicyStore(db_path).create_revision(
        team_id="team_p", agent_id="osa_real", actor_user_id="user_owner", expected_revision_id=None, draft=draft
    )
    _insert_candidate(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT * FROM team_subagent_release_candidate WHERE id='arc_real'") as cursor:
            candidate = await cursor.fetchone()
        await AgentEvalPlanStore(db_path).create_initial_plan_conn(
            db,
            candidate=candidate,
            policy=policy,
            actor_user_id="user_owner",
            workspace_id="ws_eval",
            source_working_copy_id="awc_real",
            source_working_copy_version=1,
            source_working_copy_hash="sha256:working-copy",
            created_at=_NOW,
        )
        await db.commit()


async def _save_case(db_path, *, case_key: str, valid_until: str | None = None) -> str:
    """Save a case into the Owner suite after preparing; the evaluation policy follows it."""
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT id FROM agent_eval_suite_revision WHERE suite_id='aes_owner' ORDER BY sequence DESC LIMIT 1") as cursor:
            current = await cursor.fetchone()
        appended = await append_eval_suite_cases_conn(
            db,
            team_id="team_p",
            agent_id="osa_real",
            expected_suite_revision_id=str(current["id"]),
            cases=(
                EvalCaseDraft(
                    case_key=case_key,
                    input={"task": "Added after preparing"},
                    fixture_ref={},
                    expected_contract={"allowed_terminal_statuses": ["completed"]},
                    scorer_config={},
                    effect_policy={"mode": "no_effects"},
                    valid_until=valid_until,
                ),
            ),
            actor_user_id="user_owner",
        )
        await db.commit()
    return appended.suite_revision_id


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-agent-eval-plan-refresh-jwt-secret")
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    _seed_real(db_path)
    await _prepared(db_path)
    current = {"user": _REAL_USERS["owner"]}
    app = FastAPI()
    app.include_router(agents.router, prefix="/api/v1/agents")
    app.dependency_overrides[_current_user] = lambda: current["user"]
    app.dependency_overrides[_store] = lambda: UserStore(db_path)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http:
        yield http, current, db_path


def _comparison_body(workbench: dict) -> dict:
    """What the Owner workbench sends for "Run comparison"."""
    return {
        "candidate_row_version": workbench["candidate_identity"]["row_version"],
        "expected_control_row_version": workbench["control_row_version"],
        "candidate_hash": workbench["candidate_identity"]["candidate_hash"],
        "plan_id": workbench["active_plan_identity"]["id"],
        "plan_hash": workbench["active_plan_identity"]["hash"],
        "run_kind": "release",
    }


def _stub_model_services(monkeypatch) -> None:
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: object())
    monkeypatch.setattr(agents, "_comparison_model_hashes", AsyncMock(return_value=({"arv_real_base": _H["6"], "arv_real_candidate": _H["6"]}, None)))
    monkeypatch.setattr(agents, "_schedule_comparison", AsyncMock())


def _expected_behind_details(workbench: dict) -> dict:
    return {
        "candidate_id": "arc_real",
        "current_policy_revision_id": workbench["current_eval_policy_revision_id"],
        "plan_id": workbench["active_plan_identity"]["id"],
        "plan_policy_revision_id": workbench["active_plan_identity"]["policy_revision_id"],
    }


def _plans(db_path) -> list[tuple[str, int, str, str]]:
    with sqlite3.connect(db_path) as db:
        return db.execute(
            "SELECT id,sequence,policy_revision_id,created_by FROM agent_eval_plan WHERE candidate_id='arc_real' ORDER BY sequence"
        ).fetchall()


async def test_a_plan_behind_the_saved_cases_offers_the_comparison_not_a_separate_update(client) -> None:
    http, _current, db_path = client
    await _save_case(db_path, case_key="quality.2")
    behind = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()
    assert behind["evaluation_plan_behind"] is True
    assert behind["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}
    assert all(action["kind"] != "update_evaluation_plan" for action in behind["secondary_actions"])
    # The separate update route is gone; the comparison is the one place the plan is refreshed.
    gone = await http.post("/api/v1/agents/osa_real/release-candidates/arc_real/evaluation-plans", json={})
    assert gone.status_code in {404, 405}


async def test_the_owner_comparison_refreshes_a_behind_plan_and_records_it(client, monkeypatch) -> None:
    http, _current, db_path = client
    _stub_model_services(monkeypatch)
    await _save_case(db_path, case_key="quality.2")
    behind = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()
    old_plan = behind["active_plan_identity"]["id"]

    started = await http.post(_COMPARISONS, json={**_comparison_body(behind), "refresh_plan": True}, headers={"Idempotency-Key": "compare-refresh"})
    assert started.status_code == 202, started.text
    new_plan = started.json()["plan_id"]
    assert new_plan != old_plan

    # The refresh is explicit in the record: a new plan row by the Owner on the current policy, and the
    # comparison audit naming the plan it replaced.
    plans = _plans(db_path)
    assert [row[0] for row in plans] == [old_plan, new_plan]
    assert plans[1][1:] == (2, behind["current_eval_policy_revision_id"], "user_owner")
    with sqlite3.connect(db_path) as db:
        payloads = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM admin_audit_log WHERE action='agent_eval_comparison_started'")]
    assert payloads == [
        {
            "candidate_id": "arc_real",
            "execution_id": started.json()["execution_id"],
            "plan_id": new_plan,
            "replaced_plan_id": old_plan,
            "run_kind": "release",
        }
    ]

    after = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()
    assert after["evaluation_plan_behind"] is False
    assert after["active_plan_identity"]["id"] == new_plan

    # Resending the same request replays the same run and compiles nothing new.
    replay = await http.post(_COMPARISONS, json={**_comparison_body(behind), "refresh_plan": True}, headers={"Idempotency-Key": "compare-refresh"})
    assert replay.status_code == 202, replay.text
    assert replay.json()["execution_id"] == started.json()["execution_id"]
    assert len(_plans(db_path)) == 2


async def test_without_refresh_a_behind_plan_is_never_used(client, monkeypatch) -> None:
    http, _current, db_path = client
    _stub_model_services(monkeypatch)
    await _save_case(db_path, case_key="quality.2")
    behind = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()

    refused = await http.post(_COMPARISONS, json=_comparison_body(behind), headers={"Idempotency-Key": "compare-behind"})
    assert refused.status_code == 409, refused.text
    error = refused.json()["detail"]["error"]
    assert error["code"] == "agent_eval_plan_behind"
    assert error["details"] == _expected_behind_details(behind)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_comparison_execution").fetchone() == (0,)
    assert len(_plans(db_path)) == 1


@pytest.mark.parametrize("actor", ["team_admin", "reviewer", "other_team"])
async def test_only_the_agent_owner_refreshes_the_plan(client, monkeypatch, actor) -> None:
    http, current, db_path = client
    _stub_model_services(monkeypatch)
    await _save_case(db_path, case_key="quality.2")
    body = {**_comparison_body((await http.get("/api/v1/agents/osa_real/version-workbench")).json()), "refresh_plan": True}

    current["user"] = _REAL_USERS[actor]
    denied = await http.post(_COMPARISONS, json=body, headers={"Idempotency-Key": f"refresh-{actor}"})
    # Team roles never bypass the Agent Owner, and nothing is written.
    assert denied.status_code == 422, denied.text
    assert denied.json()["detail"]["error"]["code"] == "agent_eval_comparison_forbidden"
    assert len(_plans(db_path)) == 1


async def test_an_expired_case_is_named_when_the_run_refreshes_the_plan(client, monkeypatch) -> None:
    http, _current, db_path = client
    _stub_model_services(monkeypatch)
    suite_revision_id = await _save_case(db_path, case_key="quality.9", valid_until="2026-01-01T00:00:00+00:00")
    body = {**_comparison_body((await http.get("/api/v1/agents/osa_real/version-workbench")).json()), "refresh_plan": True}

    expired = await http.post(_COMPARISONS, json=body, headers={"Idempotency-Key": "refresh-expired"})
    assert expired.status_code == 422, expired.text
    error = expired.json()["detail"]["error"]
    assert error["code"] == "agent_eval_fixture_expired"
    assert error["details"]["case_key"] == "quality.9"
    assert error["details"]["suite_revision_id"] == suite_revision_id
    assert error["details"]["valid_until"] == "2026-01-01T00:00:00+00:00"
    assert len(_plans(db_path)) == 1


@pytest.mark.parametrize("action", ["activate", "submit"])
async def test_release_routes_return_the_plan_behind_refusal_with_its_details(client, monkeypatch, action) -> None:
    http, _current, _db_path = client
    details = {"candidate_id": "arc_real", "current_policy_revision_id": "aepr_new", "plan_id": "aep_old", "plan_policy_revision_id": "aepr_old"}
    refuse = AsyncMock(side_effect=AgentVersionError("agent_eval_plan_behind", details=details))
    service = SimpleNamespace(activate_private=refuse, submit_for_review=refuse)
    monkeypatch.setattr(agents, "_version_service", AsyncMock(return_value=service))

    refused = await http.post(
        f"/api/v1/agents/osa_real/release-candidates/arc_real/{action}",
        json={
            "candidate_hash": _H["9"],
            "candidate_row_version": 1,
            "plan_id": "aep_old",
            "plan_hash": _H["b"],
            "snapshot_id": "aes_snapshot",
            "snapshot_hash": _H["c"],
        },
        headers={"Idempotency-Key": f"{action}-behind"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == {"code": "agent_eval_plan_behind", "details": details}
    refuse.assert_awaited_once()


async def test_a_ci_comparison_refuses_a_plan_behind_the_saved_cases(client, monkeypatch) -> None:
    """Team API `eval:run`: the pipeline gets the same refusal and can't update the plan itself."""
    http, _current, db_path = client
    _stub_model_services(monkeypatch)
    key, _token = await OpenApiStore(str(db_path)).create_key(
        team_id="team_p", name="CI", scopes=["agent_versions:write"], expires_at="2099-01-01T00:00:00+00:00"
    )
    grant = await AgentVersionAutomationStore(str(db_path)).create_grant(
        team_id="team_p",
        agent_id="osa_real",
        actor_user_id="user_owner",
        principal_id=key["id"],
        scopes=["eval:run"],
        repositories=["acme/agent"],
        refs=["refs/heads/main"],
        expires_at="2099-01-01T00:00:00+00:00",
        evaluation_workspace_id="ws_eval",
    )
    await _save_case(db_path, case_key="quality.2")
    behind = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()
    monkeypatch.setattr(team_openapi, "_automation_actor", AsyncMock(return_value=(None, {"effective_owner_user_id": "user_owner"})))
    monkeypatch.setattr(team_openapi, "_log_agent_version_call", AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(user_store=UserStore(db_path))))
    body = team_openapi.AutomationComparisonBody(
        grant_id=grant["id"],
        repository="acme/agent",
        ref="refs/heads/main",
        **{key: value for key, value in _comparison_body(behind).items() if key != "run_kind"},
    )

    with pytest.raises(HTTPException) as refused:
        await team_openapi.start_automation_agent_comparison("osa_real", "arc_real", body, request, "ci-behind", {**key, "team_id": "team_p"})
    assert refused.value.status_code == 409
    assert refused.value.detail == {"code": "agent_eval_plan_behind", **_expected_behind_details(behind)}
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_comparison_execution").fetchone() == (0,)


async def test_a_draft_edited_after_preparing_offers_prepare_from_latest_draft(client, monkeypatch) -> None:
    http, _current, db_path = client
    _stub_model_services(monkeypatch)
    prepared = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()
    assert prepared["draft_ahead_of_candidate"] is False
    assert all(action["kind"] != "prepare_from_latest_draft" for action in prepared["secondary_actions"])

    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent_working_copy SET row_version=2 WHERE id='awc_real'")
    ahead = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()
    assert ahead["draft_ahead_of_candidate"] is True
    assert {"enabled": True, "kind": "prepare_from_latest_draft"} in ahead["secondary_actions"]

    # While a comparison runs, replacing waits for it (cancel first).
    started = await http.post(_COMPARISONS, json=_comparison_body(ahead), headers={"Idempotency-Key": "compare-ahead"})
    assert started.status_code == 202, started.text
    running = (await http.get("/api/v1/agents/osa_real/version-workbench")).json()
    assert {"enabled": False, "kind": "prepare_from_latest_draft"} in running["secondary_actions"]
