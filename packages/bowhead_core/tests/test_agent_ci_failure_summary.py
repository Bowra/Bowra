"""CI failure detail from a finished comparison: the snapshot's execution and the D3 failure counts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_insights import (
    _unmet_points_conn,
    comparison_overview,
    read_case_changes,
    read_snapshot_failure_summary_conn,
    required_failure_summary,
)
from bowhead_core.agent_eval_store import AgentEvalStore
from bowhead_core.agent_version_service import read_agent_version_workbench_conn
from test_agent_eval_policy_plan_store import _seed, _upgrade
from test_agent_eval_run_insights import _OWNER, _complete, _runs, _settle, _start

pytestmark = pytest.mark.integration


async def _workbench(db_path: Path) -> dict:
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        return await read_agent_version_workbench_conn(db, team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")


async def _summary(db_path: Path, snapshot_id: str) -> dict[str, int] | None:
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        return await read_snapshot_failure_summary_conn(
            db, team_id="team_p", agent_id="osa_policy", candidate_id="arc_policy", snapshot_id=snapshot_id
        )


async def test_finished_comparison_keeps_its_execution_and_counts_an_introduced_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    assert (await _workbench(db_path))["active_snapshot_identity"] is None

    await _complete(db_path, comparison, started, candidate_status="failed")

    workbench = await _workbench(db_path)
    # The bug this field fixes: the run summary is gone once the comparison finishes.
    assert workbench["active_execution_summary"] is None
    snapshot = workbench["active_snapshot_identity"]
    assert snapshot["execution_id"] == started.id
    # The same execution id opens the failing-case table.
    changes = await read_case_changes(str(db_path), **_OWNER, execution_id=snapshot["execution_id"], actor_user_id="user_owner")
    assert changes["snapshot"]["id"] == snapshot["id"]
    assert [item["change"] for item in changes["items"]] == ["worse"]

    assert await _summary(db_path, snapshot["id"]) == {
        "required_failed": 1,
        "introduced": 1,
        "also_failing_on_live": 0,
        "not_run_on_live": 0,
        "unmet_points": [],
    }
    assert await _summary(db_path, "aevs_unknown") is None
    # The same snapshot gives the content-free batch overview (R15) on the workbench.
    # One owner case passed on Live and fails now; the platform case passes on both sides.
    assert workbench["comparison_overview"] == {
        "cases": 2,
        "candidate_pass_rate": 0.5,
        "live_pass_rate": 1.0,
        "newly_failing": 1,
        "newly_passing": 0,
    }


async def test_required_case_failing_on_both_sides_counts_as_also_failing_on_live(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    await comparison.mark_running(started.id)
    eval_store = AgentEvalStore(db_path)
    runs = _runs(started)
    for run_id in runs.values():
        await eval_store.mark_run_running(run_id)
    for side in ("baseline", "candidate"):
        for _ in range(3):
            await _settle(db_path, runs[("aev_owner", side)], status="failed", usage=None)
            await _settle(db_path, runs[("aev_platform", side)], status="passed", usage=None)
    for run_id in runs.values():
        await eval_store.mark_run_terminal(run_id, status="failed", summary={"schema_version": 1})
    await comparison.finalize(started.id)

    snapshot = (await _workbench(db_path))["active_snapshot_identity"]
    changes = await read_case_changes(str(db_path), **_OWNER, execution_id=snapshot["execution_id"], actor_user_id="user_owner")
    assert changes["summary"]["unchanged_required_failures"] == 1
    assert await _summary(db_path, snapshot["id"]) == {
        "required_failed": 1,
        "introduced": 0,
        "also_failing_on_live": 1,
        "not_run_on_live": 0,
        "unmet_points": [],
    }


def _comparison(suite: str, case: str, classification: str, *, required: bool = True, reasons: tuple[str, ...] = ()) -> dict:
    return {
        "suite_revision_id": suite,
        "case_revision_id": case,
        "classification": classification,
        "required": required,
        "reason_codes": list(reasons),
        "tier": "standard",
    }


def test_failure_labels_follow_the_d3_table() -> None:
    """Candidate-only suites have no Live result, so their failures are `not_run_on_live`."""
    identity = {
        "runs": [
            {"run_id": "r1", "suite_revision_id": "paired", "subject_side": "baseline"},
            {"run_id": "r2", "suite_revision_id": "paired", "subject_side": "candidate"},
            {"run_id": "r3", "suite_revision_id": "new_suite", "subject_side": "candidate"},
        ]
    }
    summary = {
        "case_comparisons": [
            _comparison("paired", "c1", "regressed"),
            _comparison("paired", "c2", "unchanged_fail"),
            _comparison("new_suite", "c3", "unchanged_fail"),
            # Not counted: optional failures, passes, fixes and inconclusive results.
            _comparison("paired", "c4", "regressed", required=False),
            _comparison("paired", "c5", "unchanged_fail", required=False),
            _comparison("paired", "c6", "unchanged_pass"),
            _comparison("paired", "c7", "fixed"),
            _comparison("paired", "c8", "inconclusive", reasons=("flaky_limit_exceeded",)),
        ]
    }
    assert required_failure_summary(summary, identity) == {
        "required_failed": 3,
        "introduced": 1,
        "also_failing_on_live": 1,
        "not_run_on_live": 1,
    }


def test_overview_counts_only_cases_that_reached_a_verdict() -> None:
    """Inconclusive cases have no pass rate; a candidate-only case has no Live side at all."""
    summary = {
        "case_comparisons": [
            {**_comparison("paired", "c1", "regressed"), "baseline_pass_rate": 1.0},
            {**_comparison("paired", "c2", "fixed"), "baseline_pass_rate": 0.0},
            {**_comparison("paired", "c3", "unchanged_pass"), "baseline_pass_rate": 1.0},
            {**_comparison("new_suite", "c4", "unchanged_fail"), "baseline_pass_rate": None},
            {**_comparison("paired", "c5", "inconclusive"), "baseline_pass_rate": None},
        ]
    }
    assert comparison_overview(summary) == {
        "cases": 5,
        "candidate_pass_rate": 0.5,
        "live_pass_rate": round(2 / 3, 4),
        "newly_failing": 1,
        "newly_passing": 1,
    }
    # Before any case reached a verdict there is no rate to show, not a 0% one.
    assert comparison_overview({"case_comparisons": [_comparison("paired", "c5", "inconclusive")]})["live_pass_rate"] is None


async def test_unmet_points_name_failing_cases_by_key_and_stop_at_twenty(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    with sqlite3.connect(db_path) as db:
        case_id, case_key = db.execute("SELECT id,case_key FROM agent_eval_case_revision ORDER BY id LIMIT 1").fetchone()
    summary = {
        "case_comparisons": [
            {**_comparison("paired", case_id, "regressed"), "unmet_point_ids": [f"p_{index:04x}" for index in range(25)]},
            # A passing case's attempt-level misses don't explain a gate result.
            {**_comparison("paired", case_id, "unchanged_pass"), "unmet_point_ids": ["p_ffff"]},
        ]
    }
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        points = await _unmet_points_conn(db, summary)
    assert len(points) == 20
    assert points[0] == {"case_key": case_key, "point_id": "p_0000"}
    assert {"case_key": case_key, "point_id": "p_ffff"} not in points
    # Content-free: identifiers only.
    assert all(set(item) == {"case_key", "point_id"} for item in points)
