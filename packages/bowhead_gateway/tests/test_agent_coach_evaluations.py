"""Coach case-only proposals use the actual Owner review and suite writer."""

import sqlite3

import pytest
from bowhead_core.agent_eval_policy_store import AgentEvalPolicyDraft, AgentEvalPolicyStore, AgentEvalPolicySuite
from bowhead_core.agent_eval_store import AgentEvalStore
from bowhead_core.agent_revision_store import AgentRevisionForbiddenError
from bowhead_gateway.agent_coach_resources import AgentCoachResources
from bowhead_gateway.routes import agents
from starlette.requests import Request
from test_agents_routes import _enable_revisioned_agent
from test_agents_routes import app_client as app_client

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("with_history", [False, True], ids=["no_history", "recorded_history"])
async def test_case_reads_report_call_effects_without_claiming_history_absence(app_client, monkeypatch, with_history):
    from bowhead_core.agent_eval_product import AgentEvalProductStore

    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    created = await client.post("/api/v1/agents/osa_1/eval-suites", json={"name": "Quality", "cases": [{**case_spec(), "task": "Explain the total"}]})
    assert created.status_code == 200
    target = (await adapter.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    if with_history:
        store = AgentEvalStore(str(db_path))
        subject = await AgentEvalProductStore(str(db_path)).resolve_team_subject(
            team_id="team_1", agent_id="osa_1", model_config_hash="sha256:" + "1" * 64
        )
        run = await store.create_run(
            subject=subject.subject,
            suite_revision_id=target["revision_id"],
            trigger_kind="manual",
            idempotency_key="recorded-history",
            actor_user_id="user_1",
        )
        await store.mark_run_running(run.run_id)
        revision = await store.read_suite_revision(target["revision_id"])
        await store.record_case_result(
            run_id=run.run_id,
            case_revision_id=revision.cases[0].case_revision_id,
            attempt=1,
            status="passed",
            deterministic={"passed": True, "scorers": []},
            cost_microusd=0,
        )
        await store.mark_run_terminal(run.run_id, status="passed", summary={"case_count": 1})
    listed = await adapter.suites(agent_id="osa_1", offset=0, limit=10)
    content = await adapter.suite(target=target, offset=0, limit=10)
    proposed = await adapter.propose(
        target=target,
        cases=[{"id": "one", "task": "Check an empty answer", "failure_mode": "Fabricated total", "case_spec": case_spec()}],
        rationale="Cover missing data",
        request_id="case-effect",
        turn_id="turn_owner",
    )
    recovered = await adapter.get(agent_id="osa_1", proposal_id=proposed["proposal"]["id"])
    for result in (listed, content, proposed, recovered):
        assert result.get("executed_by_this_call") is False
        assert "evaluation" not in result
    pointer = {"kind": "eval_cases", "agent_id": "osa_1", "proposal_id": proposed["proposal"]["id"]}
    receipt = await adapter.resources.operations.get(pointer)
    assert receipt["view_action"] == {"tool": "agent_operation_open", "arguments": {"reference": pointer}}
    assert receipt["effects_applied_by_this_read"] is False
    if with_history:
        evidence = await adapter.call(
            "agent_eval_evidence_get",
            {
                "target": {"kind": "agent_eval_overview", "agent_id": "osa_1"},
                "offset": 0,
                "limit": 10,
                "fingerprint": None,
                "include_failure_excerpts": False,
            },
            turn_id="turn_owner",
        )
        assert evidence["items"][0]["id"] == run.run_id and evidence["items"][0]["status"] == "passed"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == int(with_history)
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 0


def case_spec():
    return {
        "case_key": "quality.empty",
        "fixture_ref": {},
        "expected_contract": {"allowed_terminal_statuses": ["completed"]},
        "scorer_config": {"critical": ["no_fabrication"], "max_tool_calls": 0},
        "effect_policy": {"mode": "no_effects"},
        "valid_until": None,
    }


async def setup(client, db_path, monkeypatch):
    _enable_revisioned_agent(db_path)
    # The HTTP fixture has no lifespan; no execution is requested in this test.
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: None)
    app = client._transport.app
    app.state.user_store = app.dependency_overrides[agents._store]()
    user = app.dependency_overrides[agents._current_user]()
    resource = AgentCoachResources(Request({"type": "http", "app": app}), user, "ws_1", "")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO workspaces(id,team_id,name,created_at,updated_at) VALUES('ws_1','team_1','Evaluation','2026-10-03','2026-10-03')")
        db.execute("INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_1','user_1','member','active','2026-10-03')")
    return resource.evaluations


async def test_case_proposal_roundtrip_preserves_owner_edits_and_original_scope(app_client, monkeypatch):
    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    empty = await adapter.suites(agent_id="osa_1", offset=0, limit=10)
    assert empty["missing_suite_action"] == "propose_initial_suite"
    contract = empty["scoring_contract"]
    assert contract["scorer_bundle_ref"] == "evalkit.core@1"
    assert "output_schema" in contract["expected_contract_schema"]["properties"]
    assert contract["scorer_config_schema"]["additionalProperties"] is False
    # Create the real suite through its existing product surface, without an AWC.
    response = await client.post(
        "/api/v1/agents/osa_1/eval-suites",
        json={"name": "Quality", "cases": [{**case_spec(), "case_key": "existing", "task": "Explain the result"}]},
    )
    assert response.status_code in (200, 201), response.text
    listed = await adapter.suites(agent_id="osa_1", offset=0, limit=10)
    target = listed["items"][0]["target"]
    read = await adapter.suite(target=target, offset=0, limit=1)
    assert read["case_count"] == 1 and read["next_offset"] is None
    assert read["content_role"] == "untrusted_evaluation_data" and read["executed_by_this_call"] is False
    cases = [{"id": "empty", "task": "Explain an empty result", "failure_mode": "Invented data", "case_spec": case_spec()}]
    intent = {"target": target, "cases": cases, "rationale": "Cover absent results", "request_id": "case-only", "turn_id": "turn_owner"}
    result = await adapter.propose(**intent)
    assert not result["applied"] and not result["independent_quality_proof"]
    saved = result["proposal"]
    from bowhead_core.coach_operation_reference import with_operation_reference

    pointer = with_operation_reference("agent_eval_case_propose", result)["operation_ref"]
    receipt = await adapter.resources.operations.get(pointer)
    assert receipt["domain_result"]["proposal"]["id"] == saved["id"]
    assert not receipt["effects_applied_by_this_read"]
    with pytest.raises(ValueError, match="coach_operation_unavailable"):
        await adapter.resources.operations.get({**pointer, "kind": "agent_change"})

    assert saved["base_working_copy_id"] is None and saved["patch"] == ()
    workbench = await client.get("/api/v1/agents/osa_1/version-workbench")
    assert workbench.status_code == 200, workbench.text
    displayed = next(item for item in workbench.json()["change_proposals"] if item["proposal_id"] == saved["id"])
    assert displayed["base"]["suite_revision_id"] == target["revision_id"]
    edited = {**case_spec(), "task": "Owner reviewed task", "scorer_config": {"max_tool_calls": 2}}
    accepted = await client.post(
        f"/api/v1/agents/osa_1/change-proposals/{saved['id']}/suggested-cases/accept",
        json={
            "expected_proposal_row_version": saved["row_version"],
            "expected_active_suite_revision_id": target["revision_id"],
            "cases": [{"suggested_case_id": "empty", "edited_case": edited}],
        },
    )
    assert accepted.status_code == 200, accepted.text
    revision = await AgentEvalStore(str(db_path)).read_suite_revision(accepted.json()["suite_revision_id"])
    assert revision.case_count == 2
    added = next(item for item in revision.cases if item.case_key == "quality.empty")
    assert added.input == {"task": edited["task"]} and added.scorer_config == edited["scorer_config"]
    recovered = await adapter.get(agent_id="osa_1", proposal_id=saved["id"])
    assert recovered["proposal"]["status"] == "accepted"
    assert (await adapter.propose(**intent))["proposal"] == recovered["proposal"]
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 0
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    with pytest.raises(AgentRevisionForbiddenError):
        await adapter.get(agent_id="osa_1", proposal_id=saved["id"])


async def test_unsupported_fixture_proposal_has_no_persistence_side_effect(app_client, monkeypatch):
    from bowhead_core.agent_eval_store import AgentEvalStoreError

    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    spec = {**case_spec(), "fixture_ref": {"kind": "unsupported_business_fixture"}}
    with pytest.raises(AgentEvalStoreError, match="product_case_mode_unavailable") as rejected:
        await adapter.propose(
            target={"kind": "agent_eval_suite", "agent_id": "osa_1", "suite_id": "aes_unavailable", "revision_id": "aev_unavailable"},
            cases=[{"id": "one", "task": "Use a fixture", "failure_mode": "Missing input", "case_spec": spec}],
            rationale="Require real data",
            request_id="one",
            turn_id="turn_owner",
        )
    assert rejected.value.details["case_id"] == "one"
    assert "fixture_ref" in rejected.value.details["message"]
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0


async def test_saved_case_edit_uses_exact_read_target_and_existing_owner_adoption(app_client, monkeypatch):
    from bowhead_core.agent_coach import EvalCaseProposalInput
    from bowhead_core.agent_eval_store import AgentEvalStoreError

    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    response = await client.post(
        "/api/v1/agents/osa_1/eval-suites", json={"name": "Quality", "cases": [{**case_spec(), "task": "Original question"}]}
    )
    assert response.status_code == 200, response.text
    target = (await adapter.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    original = (await adapter.suite(target=target, offset=0, limit=10))["items"][0]
    args = EvalCaseProposalInput.model_validate(
        {
            "target": target,
            "request_id": "edit-existing",
            "rationale": "Clarify the period",
            "cases": [
                {
                    "operation": "edit",
                    "id": "clarify",
                    "case_revision_id": original["case_revision_id"],
                    "case_hash": original["case_hash"],
                    "changes": {"task": "Report the margin for this period"},
                    "failure_mode": "Ambiguous period",
                }
            ],
        }
    ).model_dump()
    # The real tool serializer must retain omission, rather than injecting null contracts.
    assert args["cases"][0]["changes"] == {"task": "Report the margin for this period"}
    invalid = {**args, "request_id": "invalid-edit", "cases": [{**args["cases"][0], "changes": {"scorer_config": {"critical": ["invented_scorer"]}}}]}
    with pytest.raises(AgentEvalStoreError, match="scoring_contract_invalid"):
        await adapter.call("agent_eval_case_propose", invalid, turn_id="turn_owner")
    saved = (await adapter.call("agent_eval_case_propose", args, turn_id="turn_owner"))["proposal"]
    workbench = (await client.get("/api/v1/agents/osa_1/version-workbench")).json()
    displayed = next(item for item in workbench["change_proposals"] if item["proposal_id"] == saved["id"])
    suggestion = displayed["suggested_cases"][0]
    assert suggestion["edit"]["before"]["task"] == "Original question"
    assert suggestion["case_spec"]["scorer_config"] == case_spec()["scorer_config"]
    accepted = await client.post(
        f"/api/v1/agents/osa_1/change-proposals/{saved['id']}/suggested-cases/accept",
        json={
            "expected_proposal_row_version": saved["row_version"],
            "expected_active_suite_revision_id": target["revision_id"],
            "cases": [{"suggested_case_id": "clarify", "edited_case": {**suggestion["case_spec"], "task": suggestion["task"]}}],
        },
    )
    assert accepted.status_code == 200, accepted.text
    updated = await AgentEvalStore(str(db_path)).read_suite_revision(accepted.json()["suite_revision_id"])
    assert updated.case_count == 1 and updated.cases[0].case_key == original["case_key"]
    assert updated.cases[0].input["task"] == suggestion["task"]
    assert (await adapter.call("agent_eval_case_propose", args, turn_id="turn_owner"))["proposal"]["status"] == "accepted"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == 0
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    with pytest.raises(AgentRevisionForbiddenError):
        await adapter.call("agent_eval_case_propose", args, turn_id="turn_owner")


async def test_invalid_owner_case_shape_names_the_case_and_field(app_client, monkeypatch):
    from bowhead_core.agent_change_proposal_store import AgentChangeProposalError

    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    spec = {**case_spec(), "case_key": "bad key with spaces"}
    with pytest.raises(AgentChangeProposalError, match="agent_change_suggested_case_invalid") as rejected:
        await adapter.propose(
            target={"kind": "initial_agent_eval_suite", "agent_id": "osa_1", "name": "Quality"},
            cases=[{"id": "spaces", "task": "Report the margin", "failure_mode": "Wrong margin", "case_spec": spec}],
            rationale="Bad key",
            request_id="bad-key",
            turn_id="turn_owner",
        )
    details = rejected.value.details
    assert details["rule"] == "case_spec_invalid"
    assert details["case_id"] == "spaces"
    assert [problem["field"] for problem in details["problems"]] == ["case_key"]


@pytest.mark.parametrize(
    "case_change",
    [
        {"scorer_config": {"type": "exact_match", "expected": ["15.0%"]}},
        {"expected_contract": {"assertions": ["Correct profit margin"]}},
        {"expected_contract": {"output_schema": {"type": "invalid"}}},
        {"scorer_config": {"critical": ["invented_scorer"]}},
    ],
)
async def test_unsupported_scoring_rejects_coach_proposal_and_owner_adoption(app_client, monkeypatch, case_change):
    from bowhead_core.agent_eval_store import AgentEvalStoreError

    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    spec = {**case_spec(), **case_change}
    with pytest.raises(AgentEvalStoreError, match="agent_eval_scoring_contract_invalid") as rejected:
        await adapter.propose(
            target={"kind": "initial_agent_eval_suite", "agent_id": "osa_1", "name": "Quality"},
            cases=[{"id": "margin", "task": "Report the margin", "failure_mode": "Wrong margin", "case_spec": spec}],
            rationale="Test the numeric requirement",
            request_id="scoring-invalid",
            turn_id="turn_owner",
        )
    # Coach must learn which case failed and why, or it can only retry blindly.
    assert rejected.value.details["case_id"] == "margin"
    assert rejected.value.details["case_index"] == 0
    assert rejected.value.details["message"]
    with pytest.raises(AgentEvalStoreError, match="agent_eval_scoring_contract_invalid"):
        agents.AgentEvalCaseBody(task="Report the margin", **spec).domain()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_eval_suite WHERE suite_kind='owner_quality'").fetchone()[0] == 0
    invalid_case = {**spec, "task": "Report the margin"}
    response = await client.post("/api/v1/agents/osa_1/eval-suites", json={"name": "Quality", "cases": [invalid_case]})
    assert response.status_code == 422, response.text
    proposal = (
        await adapter.propose(
            target={"kind": "initial_agent_eval_suite", "agent_id": "osa_1", "name": "Quality"},
            cases=[{"id": "margin", "task": "Report the margin", "failure_mode": "Wrong margin", "case_spec": case_spec()}],
            rationale="Review actual scoring",
            request_id="scoring-valid",
            turn_id="turn_owner",
        )
    )["proposal"]
    response = await client.post(
        f"/api/v1/agents/osa_1/change-proposals/{proposal['id']}/suggested-cases/accept",
        json={
            "expected_proposal_row_version": proposal["row_version"],
            "expected_active_suite_revision_id": None,
            "cases": [{"suggested_case_id": "margin", "edited_case": invalid_case}],
        },
    )
    assert response.status_code == 422, response.text
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_suite WHERE suite_kind='owner_quality'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_change_proposal_decision").fetchone()[0] == 0


async def test_evidence_reads_are_bounded_redacted_and_preserve_unknown_cost(app_client, monkeypatch):
    import json

    from bowhead_core.agent_change_proposal_store import AgentChangeProposalError
    from bowhead_core.agent_coach import EvalEvidenceInput
    from bowhead_core.agent_eval_product import AgentEvalProductStore

    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    response = await client.post(
        "/api/v1/agents/osa_1/eval-suites",
        json={"name": "Quality", "cases": [{**case_spec(), "task": "Explain the result"}]},
    )
    assert response.status_code == 200, response.text
    suite = (await adapter.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    store = AgentEvalStore(str(db_path))
    subject = await AgentEvalProductStore(str(db_path)).resolve_team_subject(
        team_id="team_1", agent_id="osa_1", model_config_hash="sha256:" + "1" * 64
    )
    run = await store.create_run(
        subject=subject.subject, suite_revision_id=suite["revision_id"], trigger_kind="manual", idempotency_key="evidence", actor_user_id="user_1"
    )
    await store.mark_run_running(run.run_id)
    revision = await store.read_suite_revision(suite["revision_id"])
    await store.record_case_result(
        run_id=run.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="failed",
        cost_microusd=None,
        deterministic={
            "passed": False,
            "scorers": [
                {
                    "name": "output_schema",
                    "passed": False,
                    "detail": "Password=hunter2 contact user@example.com https://private.example/data",
                    "data": {"raw_output": "DO NOT EXPOSE"},
                }
            ],
        },
        trace_evidence_ref="trace_private",
    )
    target = {"kind": "agent_eval_run", "agent_id": "osa_1", "run_id": run.run_id}
    args = EvalEvidenceInput(target=target, limit=1).model_dump()
    first = await adapter.call("agent_eval_evidence_get", args, turn_id="turn_owner")
    assert first["cost"]["unknown_cost_attempts"] == 1 and first["cost"]["total_cost_microusd"] is None
    assert first["items"][0]["status"] == "failed"
    assert "failure_excerpt" not in first["items"][0]["scorers"][0]
    excerpt = await adapter.call("agent_eval_evidence_get", {**args, "include_failure_excerpts": True}, turn_id="turn_owner")
    text = json.dumps(excerpt)
    for secret in ("hunter2", "user@example.com", "private.example", "DO NOT EXPOSE", "trace_private"):
        assert secret not in text
    overview = await adapter.call(
        "agent_eval_evidence_get", EvalEvidenceInput(target={"kind": "agent_eval_overview", "agent_id": "osa_1"}).model_dump(), turn_id="turn_owner"
    )
    assert overview["items"][0]["target"] == target and not overview["executed_by_this_call"]
    await store.record_case_result(
        run_id=run.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=2,
        status="passed",
        deterministic={"passed": True},
        cost_microusd=500,
    )
    with pytest.raises(AgentChangeProposalError, match="page_changed"):
        await adapter.call("agent_eval_evidence_get", {**args, "offset": 1, "fingerprint": first["fingerprint"]}, turn_id="turn_owner")
    fresh = await adapter.call("agent_eval_evidence_get", args, turn_id="turn_owner")
    assert fresh["next_offset"] == 1 and fresh["cost"]["known_cost_microusd"] == 500 and fresh["cost"]["total_cost_microusd"] is None
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM workspace_members WHERE workspace_id='ws_1' AND user_id='user_1'")
    with pytest.raises(ValueError, match="scope"):
        await adapter.call("agent_eval_evidence_get", args, turn_id="turn_owner")


async def initial_proposal(adapter, request_id="initial-suite"):
    target = {"kind": "initial_agent_eval_suite", "agent_id": "osa_1", "name": "Empty-result quality"}
    cases = [{"id": "empty", "task": "Explain an empty result", "failure_mode": "Invented records", "case_spec": case_spec()}]
    intent = {"target": target, "cases": cases, "rationale": "Cover missing input", "request_id": request_id, "turn_id": "turn_owner"}
    return intent, (await adapter.propose(**intent))["proposal"]


def initial_acceptance(proposal):
    return {
        "expected_proposal_row_version": proposal["row_version"],
        "expected_active_suite_revision_id": None,
        "cases": [{"suggested_case_id": "empty", "edited_case": {**case_spec(), "task": "Owner-reviewed empty input"}}],
    }


async def test_initial_suite_waits_for_owner_adoption_and_recovers_exact_result(app_client, monkeypatch):
    from bowhead_core.agent_coach import EvalCaseProposalInput

    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    intent, proposal = await initial_proposal(adapter)
    EvalCaseProposalInput.model_validate({key: value for key, value in intent.items() if key != "turn_id"})
    assert proposal["initial_suite_name"] == "Empty-result quality" and proposal["base_suite_revision_id"] is None
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_suite").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE agent_change_proposal SET initial_suite_name='Changed' WHERE id=?", (proposal["id"],))
    workbench = (await client.get("/api/v1/agents/osa_1/version-workbench")).json()
    assert workbench["change_proposals"][0]["base"]["initial_suite_name"] == proposal["initial_suite_name"]
    url = f"/api/v1/agents/osa_1/change-proposals/{proposal['id']}/suggested-cases/accept"
    accepted = await client.post(url, json=initial_acceptance(proposal))
    assert accepted.status_code == 200, accepted.text
    revision = await AgentEvalStore(str(db_path)).read_suite_revision(accepted.json()["suite_revision_id"])
    assert revision.case_count == 1 and revision.cases[0].input == {"task": "Owner-reviewed empty input"}
    suite = await AgentEvalStore(str(db_path)).read_suite(revision.suite_id)
    assert suite.visibility == "owner" and suite.state == "active" and suite.name == proposal["initial_suite_name"]
    with pytest.raises(Exception, match="idempotency_conflict"):
        await adapter.propose(**{**intent, "target": {**intent["target"], "name": "Different suite"}})
    replay = (await adapter.propose(**intent))["proposal"]
    assert replay["id"] == proposal["id"] and replay["status"] == "accepted"
    assert replay["case_target_suite_revision_id"] == revision.suite_revision_id
    assert (await client.post(url, json=initial_acceptance(proposal))).status_code == 409
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_suite").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == 0


async def test_initial_suite_rolls_back_with_failed_decision_and_rejects_another_winner(app_client, monkeypatch):
    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    intent, proposal = await initial_proposal(adapter)
    _, competing = await initial_proposal(adapter, "concurrent-request")
    url = f"/api/v1/agents/osa_1/change-proposals/{proposal['id']}/suggested-cases/accept"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TRIGGER fail_initial_adoption BEFORE INSERT ON agent_change_proposal_decision BEGIN SELECT RAISE(ABORT,'Injected decision failure'); END"
        )
    # ASGI propagates this deliberately injected storage failure.
    with pytest.raises(Exception, match="Injected decision failure"):
        await client.post(url, json=initial_acceptance(proposal))
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_suite").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_eval_suite_revision").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_initial_adoption")
    assert (await client.post(url, json=initial_acceptance(proposal))).status_code == 200
    other = await client.post(f"/api/v1/agents/osa_1/change-proposals/{competing['id']}/suggested-cases/accept", json=initial_acceptance(competing))
    assert other.status_code == 409 and "initial_suite_conflict" in other.text
    # Existing results are recoverable; a fresh proposal cannot ignore the new suite.
    assert (await adapter.propose(**intent))["proposal"]["status"] == "accepted"
    with pytest.raises(Exception, match="initial_suite_conflict"):
        await initial_proposal(adapter, "new-request")


async def test_initial_suite_partial_adoption_keeps_original_lineage_and_owner(app_client, monkeypatch):
    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    target = {"kind": "initial_agent_eval_suite", "agent_id": "osa_1", "name": "Quality"}
    cases = [
        {"id": key, "task": "Explain absent input", "failure_mode": "Invented records", "case_spec": {**case_spec(), "case_key": key}}
        for key in ("empty", "missing")
    ]
    proposal = (await adapter.propose(target=target, cases=cases, rationale="Missing input", request_id="partial", turn_id="turn_owner"))["proposal"]
    url = f"/api/v1/agents/osa_1/change-proposals/{proposal['id']}/suggested-cases/accept"
    first = await client.post(url, json=initial_acceptance(proposal))
    assert first.status_code == 200, first.text
    current = (await adapter.get(agent_id="osa_1", proposal_id=proposal["id"]))["proposal"]
    assert current["status"] == "open" and current["case_target_suite_revision_id"] == first.json()["suite_revision_id"]
    second_body = {
        **initial_acceptance(current),
        "expected_active_suite_revision_id": first.json()["suite_revision_id"],
        "cases": [{"suggested_case_id": "missing", "edited_case": {**case_spec(), "case_key": "missing", "task": "Explain missing input"}}],
    }
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    assert (await client.post(url, json=second_body)).status_code == 404
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_1' WHERE id='osa_1'")
    second = await client.post(url, json=second_body)
    assert second.status_code == 200, second.text
    final = (await adapter.get(agent_id="osa_1", proposal_id=proposal["id"]))["proposal"]
    assert final["status"] == "accepted" and final["case_target_suite_revision_id"] == second.json()["suite_revision_id"]
    revision = await AgentEvalStore(str(db_path)).read_suite_revision(second.json()["suite_revision_id"])
    assert revision.case_count == 2 and revision.sequence == 2
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_suite").fetchone()[0] == 1


async def test_dismissing_initial_cases_creates_no_suite(app_client, monkeypatch):
    client, db_path = app_client
    adapter = await setup(client, db_path, monkeypatch)
    intent, proposal = await initial_proposal(adapter)
    response = await client.post(
        f"/api/v1/agents/osa_1/change-proposals/{proposal['id']}/suggested-cases/dismiss",
        json={"expected_proposal_row_version": proposal["row_version"], "suggested_case_ids": ["empty"]},
    )
    assert response.status_code == 200, response.text
    assert (await adapter.propose(**intent))["proposal"]["status"] == "dismissed"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_suite").fetchone()[0] == 0


async def test_clearing_the_last_cases_preserves_history_and_updates_the_test_policy(app_client, monkeypatch):
    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    body = {"name": "Quality", "cases": [{**case_spec(), "case_key": "existing", "task": "Explain the result"}]}
    saved = await client.post("/api/v1/agents/osa_1/eval-suites", json=body)
    assert saved.status_code == 200, saved.text
    suite = next(item for item in saved.json()["suites"] if item["suite_kind"] == "owner_quality")
    identity = {"team_id": "team_1", "agent_id": "osa_1", "actor_user_id": "user_1"}
    policies = AgentEvalPolicyStore(db_path)
    initial = await policies.create_revision(
        **identity,
        expected_revision_id=None,
        draft=AgentEvalPolicyDraft.default(
            owner_suites=(
                AgentEvalPolicySuite(
                    suite_revision_id=suite["revision"]["suite_revision_id"],
                    required=True,
                    tier="standard",
                    execution_scope="paired",
                    attempts=3,
                ),
            )
        ),
    )
    assert any(item["suite_kind"] == "owner_quality" for item in initial.policy["suite_items"])

    cleared = await client.post("/api/v1/agents/osa_1/eval-suites", json={"suite_id": suite["suite_id"], "name": "Quality", "cases": []})
    assert cleared.status_code == 200, cleared.text
    assert all(item["suite_kind"] == "platform_contract" for item in cleared.json()["suites"])
    reread = await client.get("/api/v1/agents/osa_1/eval-suites")
    assert reread.json()["suites"] == cleared.json()["suites"]
    store = AgentEvalStore(db_path)
    assert (await store.read_suite(suite["suite_id"])).state == "archived"
    historical = await store.read_suite_revision(suite["revision"]["suite_revision_id"])
    assert historical.case_count == 1
    current = await policies.current(**identity)
    assert current.id != initial.id
    assert [item["suite_kind"] for item in current.policy["suite_items"]] == ["platform_contract"]

    refused = await client.post(
        "/api/v1/agents/osa_1/eval-runs",
        json={"suite_revision_id": historical.suite_revision_id, "workspace_id": "ws_1"},
        headers={"Idempotency-Key": "removed-cases"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"]["code"] == "agent_eval_suite_revision_not_found"

    added = await client.post("/api/v1/agents/osa_1/eval-suites", json=body)
    assert added.status_code == 200, added.text
    new_suite = next(item for item in added.json()["suites"] if item["suite_kind"] == "owner_quality")
    assert new_suite["suite_id"] != suite["suite_id"]
    assert new_suite["revision"]["case_count"] == 1
    assert any(item["suite_kind"] == "owner_quality" for item in (await policies.current(**identity)).policy["suite_items"])


async def test_saving_no_cases_without_a_suite_is_a_noop(app_client, monkeypatch):
    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    response = await client.post("/api/v1/agents/osa_1/eval-suites", json={"name": "Empty", "cases": []})
    assert response.status_code == 200, response.text
    assert all(item["suite_kind"] == "platform_contract" for item in response.json()["suites"])
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_suite WHERE suite_kind='owner_quality'").fetchone() == (0,)


@pytest.mark.parametrize("graded", [True, False])
async def test_standalone_run_pins_the_judge_only_for_answer_criteria(app_client, monkeypatch, graded):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    point = {"id": "p_ab12", "kind": "judgment", "text": "The total is 300", "source": "owner", "state": "confirmed"}
    response = await client.post(
        "/api/v1/agents/osa_1/eval-suites",
        json={
            "name": "Totals",
            "cases": [
                {
                    **case_spec(),
                    "task": "Add 100 and 200",
                    "expected_contract": {"points": [point] if graded else []},
                }
            ]
            + (
                [
                    {
                        **case_spec(),
                        "case_key": "quality.held",
                        "task": "Hidden task",
                        "expected_contract": {"held_out": True, "points": [{**point, "id": "p_cd34", "text": "Hidden criterion"}]},
                    }
                ]
                if graded
                else []
            ),
        },
    )
    assert response.status_code == 200, response.text
    manager = response.json()
    suite = next(item for item in manager["suites"] if item["suite_kind"] == "owner_quality")
    store = AgentEvalStore(db_path)

    async def prepare(**kwargs):
        return await store.read_run(kwargs["run_id"])

    runtime = SimpleNamespace(
        model_hash=AsyncMock(side_effect=lambda **kwargs: "sha256:" + ("a" if kwargs["role"] == "judge" else "b") * 64), prepare_and_schedule=prepare
    )
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: runtime)
    body = {
        "suite_revision_id": suite["revision"]["suite_revision_id"],
        "workspace_id": "ws_1",
        "target": "live",
        "expected_revision_id": manager["target"]["revision_id"],
    }
    endpoint = "/api/v1/agents/osa_1/eval-runs"
    stale = await client.post(endpoint, json=body | {"expected_revision_id": "arv_changed"}, headers={"Idempotency-Key": "stale-target"})
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "agent_eval_subject_changed"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == 0
    if graded:
        from bowhead_core.agent_eval_service import AgentEvalServiceError

        original_hash = runtime.model_hash.side_effect

        def unavailable_hash(**kwargs):
            if kwargs["role"] == "judge":
                raise AgentEvalServiceError("agent_eval_model_unavailable", details={"model_role": "judge", "model_reason": "role_unassigned"})
            return original_hash(**kwargs)

        runtime.model_hash.side_effect = unavailable_hash
        unavailable = await client.post(endpoint, json=body, headers={"Idempotency-Key": "missing-judge"})
        assert unavailable.status_code == 422
        assert unavailable.json()["detail"]["error"]["details"]["model_role"] == "judge"
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == 0
        runtime.model_hash.side_effect = original_hash
    started = await client.post(endpoint, json=body, headers={"Idempotency-Key": "same-intent"})
    assert started.status_code == 202, started.text
    repeated = await client.post(endpoint, json=body, headers={"Idempotency-Key": "same-intent"})
    assert repeated.json()["run_id"] == started.json()["run_id"]
    run = await store.read_run(started.json()["run_id"])
    assert run.judge_role_ref == ("judge" if graded else None)
    assert run.judge_model_fingerprint == ("sha256:" + "a" * 64 if graded else None)
    assert any(call.kwargs["role"] == "judge" for call in runtime.model_hash.await_args_list) is graded
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == 1

    if graded:
        import aiosqlite
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.agent_change_proposal_store import _require_owner_conn
        from bowhead_core.agent_coach_evidence import AgentCoachEvidence

        async def authorize(db, agent_id):
            await _require_owner_conn(db, team_id="team_1", agent_id=agent_id, actor_user_id="user_1")

        async def reauthorize(agent_id):
            async with open_shared_ctx(str(db_path)) as db:
                db.row_factory = aiosqlite.Row
                await authorize(db, agent_id)

        case_id = suite["revision"]["cases"][0]["case_revision_id"]
        await store.mark_run_running(run.run_id)
        await store.record_case_result(
            run_id=run.run_id,
            case_revision_id=case_id,
            attempt=1,
            status="passed",
            deterministic={"passed": True},
            judge={"points": [{"id": "p_ab12", "met": False, "quote": "Total: 999", "blocking": False}]},
        )
        hidden = next(item for item in suite["revision"]["cases"] if item["expected_contract"].get("held_out"))
        await store.record_case_result(
            run_id=run.run_id,
            case_revision_id=hidden["case_revision_id"],
            attempt=1,
            status="passed",
            deterministic={"passed": True},
            judge={"points": [{"id": "p_cd34", "met": True, "reason": "Hidden cause"}]},
        )
        adapter = AgentCoachEvidence(str(db_path), "team_1", "user_1", authorize, reauthorize)
        evidence = await adapter.get(
            target={"kind": "agent_eval_run", "agent_id": "osa_1", "run_id": run.run_id},
            offset=0,
            limit=5,
            fingerprint=None,
            include_failure_excerpts=False,
        )
        assert len(evidence["items"]) == 1 and evidence["held_out"]["cases"] == 1
        assert all(text not in str(evidence) for text in ("p_cd34", "Hidden criterion", "Hidden cause", "Hidden task"))
        quality = evidence["items"][0]["quality"]
        assert evidence["items"][0]["status"] == "passed"
        assert quality["state"] == "graded" and quality["unmet"] == 1
        assert quality["points"][0]["advisory"] is True
        assert "Total: 999" not in str(evidence)
        read = await client.get(f"{endpoint}/{run.run_id}/cases/{case_id}/answers")
        assert read.status_code == 200, read.text
        assert read.headers["cache-control"] == "private, no-store"
        assert read.json()["attempts"][0]["points"][0]["met"] is False
