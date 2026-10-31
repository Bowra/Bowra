"""Exact evidence remains historical after its release context changes."""

import sqlite3

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, _require_owner_conn
from bowhead_core.agent_coach_evidence import AgentCoachEvidence
from bowhead_core.agent_eval_store import AgentEvalStore, EvalCaseDraft, EvalRunSubject
from bowhead_core.agent_revision_store import AgentRevisionForbiddenError
from bowhead_core.agent_version_service import read_agent_version_workbench_conn, release_check_outcome, release_tab_path
from test_agent_eval_policy_plan_store import _comparison_context, _finish_passing_comparison, _seed, _upgrade

pytestmark = pytest.mark.unit


async def history_fixture(path):
    _upgrade(path)
    _seed(path)
    store = AgentEvalStore(str(path))
    subject = EvalRunSubject(
        team_id="team_p",
        agent_id="osa_policy",
        agent_source_kind="team",
        revision_ref="arv_policy_base",
        release_id="arl_policy_base",
        capability_fingerprint="sha256:" + "c" * 64,
        dependency_fingerprint="sha256:" + "e" * 64,
        model_config_hash="sha256:" + "a" * 64,
    )
    revisions = ["aev_owner"]
    for index in range(2):
        revision = await store.create_suite_revision(
            suite_id="aes_owner",
            definition={},
            scorer_bundle_ref="evalkit.core@1",
            judge_policy={"enabled": False},
            cases=(
                EvalCaseDraft(
                    case_key="question",
                    input={"task": f"Question {index}"},
                    fixture_ref={},
                    expected_contract={},
                    scorer_config={},
                    effect_policy={"mode": "no_effects"},
                ),
            ),
            actor_user_id="user_owner",
        )
        revisions.append(revision.suite_revision_id)
    for index, revision in enumerate(["aev_platform", *revisions[:2]]):
        run = await store.create_run(
            subject=subject, suite_revision_id=revision, trigger_kind="manual", idempotency_key=f"history-{index}", actor_user_id="user_owner"
        )
        await store.mark_run_running(run.run_id)
        saved = await store.read_suite_revision(revision)
        for case in saved.cases:
            await store.record_case_result(
                run_id=run.run_id,
                case_revision_id=case.case_revision_id,
                attempt=1,
                status="failed" if index == 0 else "passed",
                deterministic={"passed": index != 0, "critical_failure": False, "scorers": []},
            )
        await store.mark_run_terminal(run.run_id, status="failed" if index == 0 else "passed", summary={})
    return store, subject, revisions


async def test_overview_keeps_suite_identity_across_revisions_and_bounds_page_claims(tmp_path):
    path = tmp_path / "core.db"
    _, _, revisions = await history_fixture(path)
    adapter = reader(path)
    args = {
        "target": {"kind": "agent_eval_overview", "agent_id": "osa_policy"},
        "offset": 0,
        "limit": 1,
        "fingerprint": None,
        "include_failure_excerpts": False,
    }
    first = await adapter.get(**args)
    assert first["coverage"] == {
        "scope": "agent_history",
        "total_runs": 3,
        "offset": 0,
        "returned_runs": 1,
        "complete": False,
        "includes_answer_quality": False,
    }
    assert first["items"][0]["suite_id"] == "aes_owner"
    assert first["items"][0]["suite_name"] == "owner_quality"
    assert first["items"][0]["suite_revision_id"] == revisions[1]
    rest = await adapter.get(**{**args, "offset": first["next_offset"], "limit": 2, "fingerprint": first["fingerprint"]})
    assert rest["coverage"] == {
        "scope": "agent_history",
        "total_runs": 3,
        "offset": 1,
        "returned_runs": 2,
        "complete": False,
        "includes_answer_quality": False,
    }
    assert rest["next_offset"] is None
    assert rest["items"][0]["suite_id"] == "aes_owner"
    assert rest["items"][0]["suite_revision_id"] == revisions[0]
    assert rest["items"][1]["suite_id"] == "aes_platform"
    assert rest["items"][1]["status"] == "failed"
    complete = await adapter.get(**{**args, "limit": 20})
    assert complete["coverage"]["complete"]
    assert complete["coverage"]["includes_answer_quality"] is False
    assert all("quality" not in item for item in complete["items"])
    assert revisions[2] not in {item["suite_revision_id"] for item in complete["items"]}
    assert not complete["executed_by_this_call"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM turn_agent_execution").fetchone()[0] == 0
        assert db.execute("SELECT row_version FROM team_subagent_working_copy WHERE id='awc_policy'").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["suite_name", "new_run"])
async def test_overview_restarts_pagination_when_visible_history_metadata_changes(tmp_path, change):
    path = tmp_path / "core.db"
    store, subject, _ = await history_fixture(path)
    adapter = reader(path)
    args = {
        "target": {"kind": "agent_eval_overview", "agent_id": "osa_policy"},
        "offset": 0,
        "limit": 1,
        "fingerprint": None,
        "include_failure_excerpts": False,
    }
    first = await adapter.get(**args)
    if change == "suite_name":
        suite = await store.read_suite("aes_owner")
        await store.update_suite(
            suite_id=suite.suite_id, actor_user_id="user_owner", expected_row_version=suite.row_version, name="Renamed questions"
        )
    else:
        await store.create_run(
            subject=subject, suite_revision_id="aev_owner", trigger_kind="manual", idempotency_key="later-run", actor_user_id="user_owner"
        )
    with pytest.raises(AgentChangeProposalError, match="page_changed"):
        await adapter.get(**{**args, "offset": 1, "fingerprint": first["fingerprint"]})
    refreshed = await adapter.get(**args)
    assert refreshed["fingerprint"] != first["fingerprint"]
    if change == "suite_name":
        assert refreshed["items"][0]["suite_name"] == "Renamed questions"


async def test_empty_history_is_complete_and_still_requires_current_ownership(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path)
    _seed(path)
    adapter = reader(path)
    args = {
        "target": {"kind": "agent_eval_overview", "agent_id": "osa_policy"},
        "offset": 0,
        "limit": 10,
        "fingerprint": None,
        "include_failure_excerpts": False,
    }
    result = await adapter.get(**args)
    assert result["items"] == [] and result["next_offset"] is None
    assert result["coverage"] == {
        "scope": "agent_history",
        "total_runs": 0,
        "offset": 0,
        "returned_runs": 0,
        "complete": True,
        "includes_answer_quality": False,
    }
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_other' WHERE id='osa_policy'")
    with pytest.raises(AgentRevisionForbiddenError):
        await adapter.get(**args)


def reader(path):
    async def authorize(db, agent_id):
        await _require_owner_conn(db, team_id="team_p", agent_id=agent_id, actor_user_id="user_owner")

    async def reauthorize(agent_id):
        async with open_shared_ctx(str(path)) as db:
            db.row_factory = aiosqlite.Row
            await authorize(db, agent_id)

    return AgentCoachEvidence(str(path), "team_p", "user_owner", authorize, reauthorize)


async def current_run_fixture(path):
    store, subject, revisions = await history_fixture(path)
    saved = await store.read_suite_revision(revisions[-1])
    run = await store.create_run(
        subject=subject, suite_revision_id=saved.suite_revision_id, trigger_kind="manual", idempotency_key="current", actor_user_id="user_owner"
    )
    await store.mark_run_running(run.run_id)
    for attempt in (1, 2):
        await store.record_case_result(
            run_id=run.run_id, case_revision_id=saved.cases[0].case_revision_id, attempt=attempt, status="passed", deterministic={"passed": True}
        )
    await store.mark_run_terminal(run.run_id, status="passed", summary={})
    await store.project_evidence(
        team_id="team_p",
        agent_source_kind="team",
        agent_id="osa_policy",
        suite_id="aes_owner",
        subject_fingerprint=run.subject_fingerprint,
        state="valid",
        current_run_id=run.run_id,
    )
    return store, run


@pytest.mark.parametrize("same_content", [True, False], ids=["identical-content", "changed-content"])
async def test_run_compares_saved_content_without_rewriting_recorded_evidence(tmp_path, same_content):
    path = tmp_path / "core.db"
    store, run = await current_run_fixture(path)
    adapter = reader(path)
    args = {
        "target": {"kind": "agent_eval_run", "agent_id": "osa_policy", "run_id": run.run_id},
        "offset": 0,
        "limit": 1,
        "fingerprint": None,
        "include_failure_excerpts": False,
    }
    first = await adapter.get(**args)
    assert first["applicability"] == "recorded_current"
    assert first["current_suite"]["exact_revision_match"] is True
    assert first["current_suite"]["case_set_match"] is True
    if same_content:
        await store.create_suite_revision(
            suite_id="aes_owner",
            definition={},
            scorer_bundle_ref="evalkit.core@1",
            judge_policy={"enabled": False},
            cases=(
                EvalCaseDraft(
                    case_key="question",
                    input={"task": "Intervening question"},
                    fixture_ref={},
                    expected_contract={},
                    scorer_config={},
                    effect_policy={"mode": "no_effects"},
                ),
            ),
            actor_user_id="user_owner",
        )
    revision = await store.create_suite_revision(
        suite_id="aes_owner",
        definition={},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key="question",
                input={"task": "Question 1" if same_content else "Changed question"},
                fixture_ref={},
                expected_contract={},
                scorer_config={},
                effect_policy={"mode": "no_effects"},
            ),
        ),
        actor_user_id="user_owner",
    )
    original_evidence = await store.read_evidence(team_id="team_p", agent_source_kind="team", agent_id="osa_policy", suite_id="aes_owner")
    fresh = await adapter.get(**args)
    assert fresh["recorded_evidence"] == first["recorded_evidence"]
    assert fresh["recorded_evidence"]["state"] == "valid"
    assert fresh["identity"] == first["identity"]
    assert fresh["current_suite"]["revision_id"] == revision.suite_revision_id
    assert fresh["current_suite"]["exact_revision_match"] is False
    assert fresh["current_suite"]["case_set_match"] is same_content
    assert fresh["applicability"] == ("recorded_current" if same_content else "historical_or_incomplete")
    assert fresh["fingerprint"] != first["fingerprint"]
    with pytest.raises(AgentChangeProposalError, match="page_changed"):
        await adapter.get(**{**args, "offset": 1, "fingerprint": first["fingerprint"]})
    assert await store.read_evidence(team_id="team_p", agent_source_kind="team", agent_id="osa_policy", suite_id="aes_owner") == original_evidence
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_run").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM turn_agent_execution").fetchone()[0] == 0
    assert not fresh["release_authorized"] and not fresh["executed_by_this_call"]


async def test_run_applicability_page_restarts_when_the_evidence_pointer_state_changes(tmp_path):
    path = tmp_path / "core.db"
    store, run = await current_run_fixture(path)
    args = {
        "target": {"kind": "agent_eval_run", "agent_id": "osa_policy", "run_id": run.run_id},
        "offset": 0,
        "limit": 1,
        "fingerprint": None,
        "include_failure_excerpts": False,
    }
    adapter = reader(path)
    first = await adapter.get(**args)
    evidence = await store.read_evidence(team_id="team_p", agent_source_kind="team", agent_id="osa_policy", suite_id="aes_owner")
    await store.project_evidence(
        team_id="team_p",
        agent_source_kind="team",
        agent_id="osa_policy",
        suite_id="aes_owner",
        subject_fingerprint=run.subject_fingerprint,
        state="stale",
        current_run_id=run.run_id,
        stale_reason="test_cases_changed",
        expected_row_version=evidence.row_version,
    )
    with pytest.raises(AgentChangeProposalError, match="page_changed"):
        await adapter.get(**{**args, "offset": 1, "fingerprint": first["fingerprint"]})
    fresh = await adapter.get(**args)
    assert fresh["applicability"] == "historical_or_incomplete"
    assert fresh["current_suite"]["case_set_match"] is True


async def test_run_current_content_comparison_retains_the_current_owner_boundary(tmp_path):
    path = tmp_path / "core.db"
    _, run = await current_run_fixture(path)
    adapter = reader(path)
    args = {
        "target": {"kind": "agent_eval_run", "agent_id": "osa_policy", "run_id": run.run_id},
        "offset": 0,
        "limit": 1,
        "fingerprint": None,
        "include_failure_excerpts": False,
    }
    assert (await adapter.get(**args))["current_suite"]["case_set_match"] is True
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_other' WHERE id='osa_policy'")
    with pytest.raises(AgentRevisionForbiddenError):
        await adapter.get(**args)


async def test_archived_suite_is_historical_even_with_identical_cases_and_a_valid_pointer(tmp_path):
    path = tmp_path / "core.db"
    store, run = await current_run_fixture(path)
    adapter = reader(path)
    args = {
        "target": {"kind": "agent_eval_run", "agent_id": "osa_policy", "run_id": run.run_id},
        "offset": 0,
        "limit": 1,
        "fingerprint": None,
        "include_failure_excerpts": False,
    }
    first = await adapter.get(**args)
    suite = await store.read_suite("aes_owner")
    await store.update_suite(suite_id=suite.suite_id, actor_user_id="user_owner", expected_row_version=suite.row_version, state="archived")
    fresh = await adapter.get(**args)
    assert fresh["recorded_evidence"] == first["recorded_evidence"]
    assert fresh["current_suite"]["case_set_match"] is True
    assert fresh["current_suite"]["state"] == "archived"
    assert fresh["applicability"] == "historical_or_incomplete"
    with pytest.raises(AgentChangeProposalError, match="page_changed"):
        await adapter.get(**{**args, "offset": 1, "fingerprint": first["fingerprint"]})


async def test_snapshot_read_reports_original_result_separately_from_current_applicability(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path)
    _seed(path)
    plan, control = await _comparison_context(path)
    finalized = await _finish_passing_comparison(path, plan=plan, control=control, key="coach-evidence")
    adapter = reader(path)
    target = {"kind": "agent_eval_snapshot", "agent_id": "osa_policy", "snapshot_id": finalized.snapshot.id}
    args = {"target": target, "offset": 0, "limit": 1, "fingerprint": None, "include_failure_excerpts": False}
    first = await adapter.get(**args)
    assert first["recorded_readiness"] == "ready" and first["applicability"] == "recorded_current"
    assert not first["release_authorized"] and not first["executed_by_this_call"]
    assert first["identity"]["plan_id"] == plan.id
    assert first["cost"]["total_cost_microusd"] is None and first["aggregate_metrics"]["cost_microusd"] is None
    assert first["items"][0]["cost_microusd"] is None
    assert first["items"][0]["case_revision_id"].startswith("aec_")
    if first["next_offset"] is not None:
        with pytest.raises(AgentChangeProposalError, match="page_changed"):
            await adapter.get(**{**args, "offset": first["next_offset"]})
        page = await adapter.get(**{**args, "offset": first["next_offset"], "fingerprint": first["fingerprint"]})
        assert page["fingerprint"] == first["fingerprint"]
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM workspace_members WHERE workspace_id='ws_eval' AND user_id='user_owner'")
    stale = await adapter.get(**args)
    assert stale["recorded_readiness"] == "ready" and stale["applicability"] == "historical_or_stale"
    assert "evaluation_space_changed" in stale["current_restrictions"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_other' WHERE id='osa_policy'")
    with pytest.raises(AgentRevisionForbiddenError):
        await adapter.get(**args)


async def test_every_evidence_read_carries_the_release_tab_outcome(tmp_path):
    """Coach reports the release outcome the Owner sees on the Release tab, never one re-derived from a single run."""
    path = tmp_path / "core.db"
    _upgrade(path)
    _seed(path)
    plan, control = await _comparison_context(path)
    finalized = await _finish_passing_comparison(path, plan=plan, control=control, key="coach-outcome")
    adapter = reader(path)
    common = {"offset": 0, "limit": 5, "fingerprint": None, "include_failure_excerpts": False}
    async with open_shared_ctx(str(path)) as db:
        db.row_factory = aiosqlite.Row
        workbench = await read_agent_version_workbench_conn(db, team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")

    snapshot = await adapter.get(target={"kind": "agent_eval_snapshot", "agent_id": "osa_policy", "snapshot_id": finalized.snapshot.id}, **common)
    overview = await adapter.get(target={"kind": "agent_eval_overview", "agent_id": "osa_policy"}, **common)
    outcome = snapshot["release_check"]
    assert outcome == overview["release_check"]
    assert outcome["owner_surface"] == "release_tab" and outcome["owner_path"] == release_tab_path("osa_policy")
    assert outcome["readiness"] == workbench["readiness"] == "ready"
    assert outcome["reason_codes"] == workbench["reason_codes"]
    assert outcome["next_action"] == workbench["primary_action"]["kind"]
    assert outcome["snapshot_id"] == finalized.snapshot.id
    assert outcome["failure_summary"]["required_failed"] == 0

    # A single run is one side of the comparison and says which; the outcome comes with it.
    run_id = overview["items"][0]["id"]
    run = await adapter.get(target={"kind": "agent_eval_run", "agent_id": "osa_policy", "run_id": run_id}, **common)
    assert run["comparison"]["subject_side"] in {"baseline", "candidate"}
    assert run["release_check"] == outcome


def test_an_outdated_candidate_reads_stale_wherever_the_outcome_is_read():
    workbench = {
        "candidate_identity": {"candidate_id": "arc_1", "base_release_id": "arl_old"},
        "live_identity": {"release_id": "arl_new"},
        "active_snapshot_identity": {"id": "aevs_1"},
        "readiness": "ready",
        "stale_subject": None,
        "reason_codes": ["base_release_changed"],
        "primary_action": {"kind": "prepare_candidate"},
    }
    outcome = release_check_outcome(workbench, failure_counts=None)
    assert outcome["readiness"] == "stale" and outcome["stale_subject"] == "candidate"
    assert outcome["next_action"] == "prepare_candidate" and outcome["failure_summary"] is None
