"""HTTP boundary of Agent Eval run insights: canonical-Owner scope and stable errors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from typing import Any

import httpx2
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_comparison import AgentEvalComparisonStore
from bowhead_core.agent_eval_insights import AgentEvalInsightsError
from bowhead_core.agent_eval_plan_store import AgentEvalPlanStore
from bowhead_core.agent_eval_policy_store import AgentEvalPolicyDraft, AgentEvalPolicyStore, AgentEvalPolicySuite
from bowhead_core.user_store import User, UserStore
from bowhead_gateway.routes import agents
from bowhead_gateway.routes.auth import _current_user, _store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

_NOW = "2026-10-05T00:00:00+00:00"
_BASE = "/api/v1/agents/osa_1/release-candidates/arc_1"
_READS = (
    f"{_BASE}/comparison-estimate?plan_id=aep_1&plan_hash=sha256:{'1' * 64}",
    f"{_BASE}/comparisons/aex_1/case-changes",
    f"{_BASE}/comparisons/aex_1/cases/aec_1/answers",
)
_USERS = {
    "owner": User(id="owner_1", team_id="team_1", email="owner_1@example.com", status="active"),
    "team_admin": User(id="admin_1", team_id="team_1", email="admin_1@example.com", status="active"),
    "reviewer": User(id="member_1", team_id="team_1", email="member_1@example.com", status="active"),
    "other_team": User(id="outsider_1", team_id="team_2", email="outsider_1@example.com", status="active"),
}


def _seed(db_path) -> None:
    with sqlite3.connect(db_path) as db:
        for team_id in ("team_1", "team_2"):
            db.execute("INSERT INTO teams(id,name,status,created_at) VALUES(?,?,'active',?)", (team_id, team_id, _NOW))
        for user in _USERS.values():
            db.execute(
                "INSERT INTO users(id,team_id,email,status,created_at) VALUES(?,?,?,'active',?)",
                (user.id, user.team_id, user.email, _NOW),
            )
            role = "team_admin" if user.id == "admin_1" else "member"
            db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES(?,?,?,?,'active',?)",
                (f"tm_{user.id}", user.id, user.team_id, role, _NOW),
            )
        db.execute("UPDATE teams SET created_by_user_id='admin_1' WHERE id='team_1'")
        db.execute(
            """
            INSERT INTO team_subagent(id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,status,created_at,updated_at)
            VALUES('osa_1','team_1','finance-helper','Finance Helper','','[]','[]','team_wide','owner_1','enabled',?,?)
            """,
            (_NOW, _NOW),
        )


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-agent-eval-insights-jwt-secret")
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    _seed(db_path)
    user_store = UserStore(db_path)
    current = {"user": _USERS["owner"]}
    app = FastAPI()
    app.include_router(agents.router, prefix="/api/v1/agents")
    app.dependency_overrides[_current_user] = lambda: current["user"]
    app.dependency_overrides[_store] = lambda: user_store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http:
        yield http, current


def _code(response: httpx2.Response) -> str:
    return response.json()["detail"]["error"]["code"]


@pytest.mark.parametrize("path", (*_READS, "/api/v1/agents/osa_1/eval-runs/aer_1/cases/aec_1/answers"))
@pytest.mark.parametrize("actor", ["team_admin", "reviewer", "other_team"])
async def test_only_the_canonical_owner_can_read_insights(client, path, actor) -> None:
    http, current = client
    current["user"] = _USERS[actor]
    response = await http.get(path)
    # No team-admin or reviewer bypass, and another Team cannot learn the Agent exists.
    assert response.status_code == 404, response.text


async def test_owner_reads_are_private_and_report_usage_only(client) -> None:
    http, _current = client
    estimate = await http.get(_READS[0])
    assert estimate.status_code == 200, estimate.text
    assert estimate.headers["cache-control"] == "private, no-store"
    body = estimate.json()
    assert (body["status"], body["unavailable_reason"]) == ("unavailable", "plan_unavailable")
    leaked = [key for key in body if any(token in key for token in ("cost", "credit", "currency"))]
    assert leaked == []

    # The Owner passes the permission check; an unknown execution of this candidate is 404.
    for path in _READS[1:]:
        response = await http.get(path)
        assert response.status_code == 404
        assert _code(response) == "agent_eval_comparison_not_found"


@pytest.mark.parametrize(
    ("route_function", "path", "code"),
    [
        ("read_case_changes", _READS[1], "agent_eval_case_changes_unavailable"),
        ("read_comparison_estimate", _READS[0], "agent_eval_plan_version_conflict"),
    ],
)
async def test_unavailable_changes_and_plan_mismatch_are_conflicts(client, monkeypatch, route_function, path, code) -> None:
    http, _current = client

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AgentEvalInsightsError(code)

    monkeypatch.setattr(agents, route_function, refuse)
    response = await http.get(path)
    assert response.status_code == 409
    assert _code(response) == code


async def test_progress_is_attached_and_omitted_on_failure(monkeypatch) -> None:
    calls: list[str] = []

    async def progress(_db_path: str, execution_id: str) -> dict[str, Any] | None:
        calls.append(execution_id)
        return {"schema_version": 1} if execution_id == "aex_ok" else None

    monkeypatch.setattr(agents, "read_execution_progress", progress)
    assert (await agents._with_progress({"execution_id": "aex_ok"}, "db"))["progress"] == {"schema_version": 1}
    assert "progress" not in await agents._with_progress({"execution_id": "aex_broken"}, "db")


# ── a real queued release comparison, built through the production stores ─────


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


_H = {name: f"sha256:{name * 64}" for name in ("b", "c", "e", "9", "7", "6")}
_REAL_USERS = {
    "owner": User(id="user_owner", team_id="team_p", email="owner@example.com", status="active"),
    "team_admin": User(id="user_admin", team_id="team_p", email="admin@example.com", status="active"),
    "reviewer": User(id="user_member", team_id="team_p", email="member@example.com", status="active"),
    "other_team": User(id="user_far", team_id="team_far", email="far@example.com", status="active"),
}


def _suite(db: sqlite3.Connection, *, suite_id: str, revision_id: str, kind: str, managed: int) -> None:
    fixture_ref = {"classification": "internal", "content_hash": "sha256:" + "a" * 64, "kind": "dataset", "ref": "fixture", "version": "1"}
    case = {
        "case_key": f"case-{suite_id}",
        "effect_policy": {"mode": "no_effects"},
        "expected_contract": {},
        "fixture_ref": fixture_ref,
        "input": {"task": f"Task for {suite_id}"},
        "scorer_config": {},
        "valid_until": None,
    }
    definition = {"cases": [case], "definition": {}, "judge_policy": {"enabled": False}, "scorer_bundle_ref": "evalkit.core@1"}
    db.execute(
        """INSERT INTO agent_eval_suite(id,team_id,agent_id,agent_source_kind,suite_kind,name,visibility,state,row_version,
           created_by,created_at,updated_at,platform_managed) VALUES(?,'team_p','osa_real','team',?,?,?,'active',1,'user_owner',?,?,?)""",
        (suite_id, kind, kind, "team_evidence" if managed else "owner", _NOW, _NOW, managed),
    )
    db.execute(
        """INSERT INTO agent_eval_suite_revision(id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,
           judge_policy_json,case_count,case_set_hash,created_by,created_at)
           VALUES(?,?,1,'{}',?,'evalkit.core@1','{"enabled":false}',1,?,'user_owner',?)""",
        (revision_id, suite_id, _hash(definition), _hash([case]), _NOW),
    )
    db.execute(
        """INSERT INTO agent_eval_case_revision(id,suite_revision_id,case_key,case_hash,input_json,fixture_ref_json,
           expected_contract_json,scorer_config_json,effect_policy_json,created_at) VALUES(?,?,?,?,?,?,'{}','{}',?,?)""",
        (
            f"aec_{suite_id}",
            revision_id,
            case["case_key"],
            _hash(case),
            json.dumps(case["input"]),
            json.dumps(fixture_ref, sort_keys=True, separators=(",", ":")),
            json.dumps({"mode": "no_effects"}),
            _NOW,
        ),
    )


def _seed_real(db_path) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for team_id in ("team_p", "team_far"):
            db.execute("INSERT INTO teams(id,name,status,created_at) VALUES(?,?,'active',?)", (team_id, team_id, _NOW))
        for user in _REAL_USERS.values():
            db.execute("INSERT INTO users(id,team_id,email,status,created_at) VALUES(?,?,?,'active',?)", (user.id, user.team_id, user.email, _NOW))
            role = "team_admin" if user.id == "user_admin" else "member"
            db.execute(
                "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES(?,?,?,?,'active',?)",
                (f"tm_{user.id}", user.id, user.team_id, role, _NOW),
            )
        db.execute("UPDATE teams SET created_by_user_id='user_admin' WHERE id='team_p'")
        db.execute(
            "INSERT INTO workspaces(id,team_id,name,status,revision,created_at,updated_at) VALUES('ws_eval','team_p','ws_eval','active',1,?,?)",
            (_NOW, _NOW),
        )
        db.execute(
            """INSERT INTO workspace_members(workspace_id,user_id,role,invited_by,joined_at,last_active_at,status)
               VALUES('ws_eval','user_owner','admin','user_owner',?,?,'active')""",
            (_NOW, _NOW),
        )
        db.execute(
            """INSERT INTO team_subagent(id,team_id,name,display_name,description,tool_refs_json,skills_json,visibility,created_by,
               owner_user_id,status,definition_mode,created_at,updated_at)
               VALUES('osa_real','team_p','real-agent','Real Agent','','[]','[]','private','user_owner','user_owner','enabled','revisioned',?,?)""",
            (_NOW, _NOW),
        )
        for sequence, revision_id, prompt in ((1, "arv_real_base", "base"), (2, "arv_real_candidate", "candidate")):
            definition_json = json.dumps({"system_prompt": prompt})
            db.execute(
                """INSERT INTO team_subagent_revision(id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
                   definition_hash,dependency_fingerprint,capability_fingerprint,risk_compiler_version,risk_json,created_by,created_at)
                   VALUES(?,'team_p','osa_real',?,'authored',?,'{"display_name":"Real Agent"}',?,?,?,'risk-v1','{}','user_owner',?)""",
                (revision_id, sequence, definition_json, _hash(json.loads(definition_json)), _H["e"], _H["c"], _NOW),
            )
        db.execute(
            """INSERT INTO agent_safety_report(id,team_id,agent_id,subject_kind,subject_ref,content_hash,dependency_fingerprint,
               compiler_version,semantic_scanner_version,status,deterministic_json,semantic_json,safe_summary_json,created_by,created_at)
               VALUES('asr_real','team_p','osa_real','revision','arv_real_candidate',?,?,'safety-v1','none','pass','{}','{}','{}',
               'user_owner',?)""",
            (_hash({"definition": {"system_prompt": "candidate"}, "listing": {"display_name": "Real Agent"}}), _H["e"], _NOW),
        )
        db.execute(
            """INSERT INTO team_subagent_release(id,team_id,subagent_id,release_sequence,revision_id,audience_snapshot_json,audience_hash,
               capability_fingerprint,dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at)
               VALUES('arl_real_base','team_p','osa_real',1,'arv_real_base','{"effective_mode":"private"}',?,?,?,'{}','asr_real',
               'user_owner',?)""",
            (_H["b"], _H["c"], _H["e"], _NOW),
        )
        db.execute("UPDATE team_subagent SET live_release_id='arl_real_base',live_revision_id='arv_real_base',release_sequence=1 WHERE id='osa_real'")
        db.execute(
            """INSERT INTO team_subagent_working_copy(id,team_id,subagent_id,base_release_id,definition_json,listing_json,audience_json,
               validation_json,row_version,updated_by,created_at,updated_at)
               VALUES('awc_real','team_p','osa_real','arl_real_base','{"system_prompt":"candidate"}','{"display_name":"Real Agent"}',
               '{"effective_mode":"private"}','{}',1,'user_owner',?,?)""",
            (_NOW, _NOW),
        )
        _suite(db, suite_id="aes_platform", revision_id="aev_platform", kind="platform_contract", managed=1)
        _suite(db, suite_id="aes_owner", revision_id="aev_owner", kind="owner_quality", managed=0)


def _insert_candidate(db_path) -> None:
    """The candidate comes after the Policy, as in production (a Policy change rotates open Plans)."""
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        audience = {"effective_mode": "private", "group_ids": [], "schema_version": 1, "user_ids": []}
        db.execute(
            """INSERT INTO team_subagent_release_candidate(id,team_id,subagent_id,revision_id,base_live_release_id,target_audience_json,
               target_audience_hash,capability_fingerprint,dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,
               candidate_hash,state,submitted_by,submitted_at,updated_at)
               VALUES('arc_real','team_p','osa_real','arv_real_candidate','arl_real_base',?,?,?,?,'risk-v1','{}','asr_real',?,'prepared',
               'user_owner',?,?)""",
            (json.dumps(audience, sort_keys=True, separators=(",", ":")), _hash(audience), _H["c"], _H["e"], _H["9"], _NOW, _NOW),
        )
        db.execute(
            """INSERT INTO agent_name_claim(team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at)
               VALUES('team_p','*','real agent','agent','osa_real','osa_real',?)""",
            (_NOW,),
        )
        db.execute(
            """INSERT INTO agent_release_candidate_name_claim(team_id,candidate_id,agent_id,locale,normalized_key,claim_kind,state,claimed_at)
               VALUES('team_p','arc_real','osa_real','*','real agent','inherited','reserved',?)""",
            (_NOW,),
        )
        db.execute(
            """INSERT INTO agent_revision_compatibility(revision_id,team_id,agent_id,base_revision_id,compatibility_major,verdict,
               compiler_version,contract_hash,reason_codes_json,created_at)
               VALUES('arv_real_candidate','team_p','osa_real','arv_real_base',1,'compatible','compat-v1',?,'[]',?)""",
            (_H["7"], _NOW),
        )


async def _queued_comparison(db_path) -> str:
    draft = AgentEvalPolicyDraft.default(
        owner_suites=(AgentEvalPolicySuite(suite_revision_id="aev_owner", required=True, tier="standard", execution_scope="paired", attempts=3),),
    )
    policy = await AgentEvalPolicyStore(db_path).create_revision(
        team_id="team_p",
        agent_id="osa_real",
        actor_user_id="user_owner",
        expected_revision_id=None,
        draft=replace(draft, pass_thresholds={"critical": 1.0, "standard": 0.8}),
    )
    _insert_candidate(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT * FROM team_subagent_release_candidate WHERE id='arc_real'") as cursor:
            candidate = await cursor.fetchone()
        plan, control = await AgentEvalPlanStore(db_path).create_initial_plan_conn(
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
    started = await AgentEvalComparisonStore(db_path).start_release(
        team_id="team_p",
        agent_id="osa_real",
        candidate_id="arc_real",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash=_H["9"],
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes={"arv_real_base": _H["6"], "arv_real_candidate": _H["6"]},
        idempotency_key="gateway-insights",
    )
    return started.id


@pytest.fixture
async def real_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-agent-eval-insights-jwt-secret")
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    _seed_real(db_path)
    execution_id = await _queued_comparison(db_path)
    user_store = UserStore(db_path)
    current = {"user": _REAL_USERS["owner"]}
    app = FastAPI()
    app.include_router(agents.router, prefix="/api/v1/agents")
    app.dependency_overrides[_current_user] = lambda: current["user"]
    app.dependency_overrides[_store] = lambda: user_store
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http:
        yield http, current, execution_id


@pytest.mark.parametrize("actor", ["team_admin", "reviewer", "other_team"])
async def test_a_real_comparison_is_readable_only_by_its_owner(real_client, actor) -> None:
    http, current, execution_id = real_client
    base = f"/api/v1/agents/osa_real/release-candidates/arc_real/comparisons/{execution_id}"
    changes_path, answers_path = f"{base}/case-changes", f"{base}/cases/aec_aes_owner/answers"

    # Positive control: the Owner reaches the data (a queued comparison has no change table yet).
    changes = await http.get(changes_path)
    assert changes.status_code == 409 and _code(changes) == "agent_eval_case_changes_unavailable"
    answers = await http.get(answers_path)
    assert answers.status_code == 200, answers.text
    assert answers.json()["case_label"] == "Task for aes_owner"
    assert answers.json()["candidate"] == {"attempts": []} and answers.json()["baseline"] == {"attempts": []}

    current["user"] = _REAL_USERS[actor]
    for path in (changes_path, answers_path):
        denied = await http.get(path)
        assert denied.status_code == 404, denied.text
        assert "attempts" not in denied.text and "Task for" not in denied.text
