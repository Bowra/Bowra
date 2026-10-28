"""Exact evidence remains historical after its release context changes."""

import sqlite3

import aiosqlite
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, _require_owner_conn
from bowhead_core.agent_coach_evidence import AgentCoachEvidence
from bowhead_core.agent_revision_store import AgentRevisionForbiddenError
from bowhead_core.agent_version_service import read_agent_version_workbench_conn, release_check_outcome, release_tab_path
from test_agent_eval_policy_plan_store import _comparison_context, _finish_passing_comparison, _seed, _upgrade

pytestmark = pytest.mark.unit


def reader(path):
    async def authorize(db, agent_id):
        await _require_owner_conn(db, team_id="team_p", agent_id=agent_id, actor_user_id="user_owner")

    async def reauthorize(agent_id):
        async with open_shared_ctx(str(path)) as db:
            db.row_factory = aiosqlite.Row
            await authorize(db, agent_id)

    return AgentCoachEvidence(str(path), "team_p", "user_owner", authorize, reauthorize)


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
