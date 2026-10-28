"""Agent Eval run insights: usage capture, usage estimates, progress, per-case changes and answers.

The contract reports usage only (tokens, model requests, duration). Every response read here
is also checked for the absence of cost, credit and currency fields.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_comparison import AgentEvalComparisonStore
from bowhead_core.agent_eval_insights import (
    MIN_ESTIMATE_SAMPLES,
    AgentEvalInsightsError,
    PlannedSuite,
    PlannedWork,
    _bounded_answer,
    _Sample,
    _unsourced_figures,
    case_label,
    change_sort_key,
    classify_change,
    coach_estimate_conn,
    estimate_from_samples,
    execution_progress_conn,
    read_case_answers,
    read_case_changes,
    read_comparison_estimate,
    read_run_case_answers,
    read_unmet_points,
)
from bowhead_core.agent_eval_job_store import AgentEvalJobStore
from bowhead_core.agent_eval_store import AgentEvalStore
from bowhead_core.agent_eval_usage import EvalAttemptUsage
from bowhead_core.agent_revision_store import AgentRevisionForbiddenError
from test_agent_eval_policy_plan_store import _Clock, _comparison_context, _model_hashes, _seed, _upgrade

from bowhead_core import agent_eval_insights

pytestmark = pytest.mark.integration

_MONEY_TOKENS = ("cost", "credit", "currency", "usd", "price", "cny", "money")
_OWNER = {"team_id": "team_p", "agent_id": "osa_policy", "candidate_id": "arc_policy"}


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _assert_usage_only(value: Any) -> None:
    """No cost, credit or currency figure may appear anywhere in the contract (Q1)."""
    leaked = sorted({key for key in _keys(value) if any(token in key.lower() for token in _MONEY_TOKENS)})
    assert leaked == []


# ── pure: estimate ──────────────────────────────────────────────────────


def _samples(count: int, *, case: str = "aec_a", suite: str = "aes_a", tokens: int = 100, calls: int | None = 2) -> list[_Sample]:
    return [
        _Sample(
            case_revision_id=case,
            suite_id=suite,
            tokens=tokens + index * 10,
            tokens_cached=5 if calls is not None else None,
            model_calls=calls,
            duration_ms=1000.0 + index,
        )
        for index in range(count)
    ]


def _work(*, attempts: int = 10) -> PlannedWork:
    return PlannedWork(cases_planned=2, attempts_planned=attempts, case_revision_ids=frozenset({"aec_a"}), suite_ids=frozenset({"aes_a"}))


def test_planned_attempts_count_both_sides_only_for_paired_suites() -> None:
    work = PlannedWork.from_suites(
        [
            PlannedSuite(suite_revision_id="aev_owner", attempts=3, paired=True, case_revision_ids=("aec_1", "aec_2")),
            PlannedSuite(suite_revision_id="aev_platform", attempts=1, paired=False, case_revision_ids=("aec_3",)),
        ],
        suite_ids=["aes_owner", "aes_platform"],
    )
    assert (work.cases_planned, work.attempts_planned) == (3, 2 * 3 * 2 + 1)


def test_estimate_prefers_same_cases_then_suites_then_agent_with_five_samples() -> None:
    same_case = _samples(MIN_ESTIMATE_SAMPLES, case="aec_a", suite="aes_other", tokens=100)
    same_suite = _samples(MIN_ESTIMATE_SAMPLES, case="aec_b", suite="aes_a", tokens=1000)
    agent_only = _samples(MIN_ESTIMATE_SAMPLES, case="aec_c", suite="aes_c", tokens=5000)
    now = "2026-10-05T00:00:00+00:00"

    assert estimate_from_samples(same_case + same_suite + agent_only, _work(), computed_at=now)["basis"]["scope"] == "same_cases"
    assert estimate_from_samples(same_case[:4] + same_suite + agent_only, _work(), computed_at=now)["basis"]["scope"] == "same_suites"
    fallback = estimate_from_samples(same_case[:4] + same_suite[:-1] + agent_only, _work(), computed_at=now)
    # The agent scope keeps every sample of this Agent, including the narrower ones.
    assert fallback["basis"] == {"window_days": 30, "scope": "agent", "sample_attempts": 4 + 4 + 5}

    insufficient = estimate_from_samples(same_case[:4], _work(), computed_at=now)
    assert insufficient["status"] == "unavailable"
    assert insufficient["unavailable_reason"] == "insufficient_history"
    assert [insufficient[key] for key in ("tokens", "tokens_cached", "model_calls", "duration_ms")] == [None] * 4
    _assert_usage_only(insufficient)


def test_estimate_ranges_are_planned_attempts_times_p25_and_p75() -> None:
    estimate = estimate_from_samples(_samples(5, tokens=100), _work(attempts=10), computed_at="now")
    # Per-attempt tokens 100..140: P25=110, P75=130.
    assert estimate["tokens"] == {"low": 1100, "high": 1300}
    # Identical per-attempt calls still produce a non-degenerate range.
    assert estimate["model_calls"] == {"low": 20, "high": 21}
    assert estimate["tokens_cached"] == {"low": 50, "high": 51}
    assert estimate["duration_ms"] == {"low": 10010, "high": 10030}
    assert estimate["status"] == "available"
    _assert_usage_only(agent_eval_insights.public_estimate(estimate))


def test_metrics_without_enough_own_samples_are_null_not_zero() -> None:
    legacy = _samples(5, calls=None)
    estimate = estimate_from_samples(legacy, _work(), computed_at="now")
    assert estimate["status"] == "available"
    assert estimate["tokens"] is not None
    assert estimate["model_calls"] is None and estimate["tokens_cached"] is None


@pytest.mark.parametrize(("calls_high", "cap", "exhaust"), [(21, 20, True), (21, 21, False)])
async def test_coach_estimate_reports_call_cap_and_estimated_token_ceiling(monkeypatch, calls_high, cap, exhaust) -> None:
    async def planned(_db, _row):
        return _work()

    async def estimate(_db, **_kwargs):
        return {
            "status": "available",
            "tokens": {"low": 1000, "high": 1300},
            "tokens_cached": None,
            "model_calls": {"low": 20, "high": calls_high},
            "duration_ms": {"low": 1, "high": 2},
            "basis": {"window_days": 30, "scope": "agent", "sample_attempts": 9},
            "_tokens_per_call_p90": 62.5,
        }

    monkeypatch.setattr(agent_eval_insights, "_coach_planned_work_conn", planned)
    monkeypatch.setattr(agent_eval_insights, "estimate_usage_conn", estimate)
    row = {"id": "acig_1", "state": "requested", "max_provider_calls": cap, "max_comparisons": 2, "team_id": "t", "agent_id": "a"}
    result = await coach_estimate_conn(None, row, has_execution=False)  # type: ignore[arg-type]
    assert result is not None
    assert result["model_call_cap"] == cap and result["max_comparisons"] == 2
    assert result["tokens_ceiling_at_cap"] == -(-cap * 625 // 10)
    assert result["may_exhaust_call_cap"] is exhaust
    assert "_tokens_per_call_p90" not in json.dumps(result)
    _assert_usage_only(result)
    assert await coach_estimate_conn(None, {**row, "state": "authorized"}, has_execution=True) is None  # type: ignore[arg-type]
    assert await coach_estimate_conn(None, {**row, "state": "exhausted"}, has_execution=False) is None  # type: ignore[arg-type]


# ── pure: change mapping, labels, outcomes ──────────────────────────────


@pytest.mark.parametrize(
    ("classification", "reasons", "change"),
    [
        ("regressed", (), "worse"),
        ("fixed", (), "better"),
        ("inconclusive", ("flaky_limit_exceeded",), "unstable"),
        ("inconclusive", ("minimum_delta_not_met",), "unstable"),
        ("inconclusive", ("flaky_limit_exceeded", "minimum_delta_not_met"), "unstable"),
        ("inconclusive", ("attempt_infrastructure_incomplete",), "not_comparable"),
        ("inconclusive", ("flaky_limit_exceeded", "attempt_infrastructure_incomplete"), "not_comparable"),
        ("inconclusive", ("required_attempts_missing",), "not_comparable"),
        ("inconclusive", ("provider_identity_unknown",), "not_comparable"),
        ("inconclusive", ("candidate_result_missing",), "not_comparable"),
        ("inconclusive", (), "not_comparable"),
        ("incomparable", ("provider_identity_incomparable",), "not_comparable"),
        ("unchanged_pass", (), "unchanged"),
        ("unchanged_fail", (), "unchanged"),
    ],
)
def test_change_mapping_matches_the_design_table(classification, reasons, change) -> None:
    assert classify_change(classification, reasons) == change


def test_changes_sort_worse_unstable_better_then_label_then_stable_tie_breakers(monkeypatch) -> None:
    items = [
        {"change": "better", "case_label": "A", "case_key": "quality.1", "case_revision_id": "aec_1"},
        {"change": "unstable", "case_label": "B", "case_key": "quality.2", "case_revision_id": "aec_2"},
        {"change": "worse", "case_label": "b", "case_key": "quality.3", "case_revision_id": "aec_3"},
        {"change": "worse", "case_label": "B", "case_key": "quality.5", "case_revision_id": "aec_5"},
        {"change": "worse", "case_label": "B", "case_key": "quality.4", "case_revision_id": "aec_4"},
    ]
    expected = ["aec_4", "aec_5", "aec_3", "aec_2", "aec_1"]
    assert [item["case_revision_id"] for item in sorted(items, key=change_sort_key)] == expected
    # The key is total, so refreshes keep the same order whatever the input order.
    assert len({change_sort_key(item) for item in items}) == len(items)
    # Mutation check: swapping the worse/better order must break the expectation above.
    monkeypatch.setattr(agent_eval_insights, "_CHANGE_ORDER", {"worse": 2, "unstable": 1, "better": 0})
    assert [item["case_revision_id"] for item in sorted(items, key=change_sort_key)] != expected


def test_case_label_is_the_trimmed_first_task_line() -> None:
    assert case_label({"task": "  渠道毛利率排名前五\nThen explain the drivers"}) == "渠道毛利率排名前五"
    assert case_label({"task": "x" * 200}) == "x" * 80
    assert case_label({"task": "   "}) is None
    assert case_label({}) is None


def test_answer_is_bounded_at_32_kib_without_splitting_a_character() -> None:
    assert _bounded_answer(None) == {"answer": None, "answer_truncated": False}
    assert _bounded_answer("ok") == {"answer": "ok", "answer_truncated": False}
    cut = _bounded_answer("毛" * 20_000)
    assert cut["answer_truncated"] is True
    assert len(cut["answer"].encode()) <= 32 * 1024 and set(cut["answer"]) == {"毛"}


# ── store: comparisons on real SQLite ───────────────────────────────────


async def _start(db_path: Path, *, key: str = "comparison-insights"):
    plan, control = await _comparison_context(db_path)
    comparison = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await comparison.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key=key,
    )
    return plan, comparison, started


def _runs(started) -> dict[tuple[str, str], str]:
    return {(link.suite_revision_id, link.subject_side): link.run_id for link in started.runs}


async def _settle(
    db_path: Path,
    run_id: str,
    *,
    status: str,
    usage: EvalAttemptUsage | None,
    answer: str | None = None,
    failure_code: str | None = None,
    deterministic: dict[str, Any] | None = None,
) -> None:
    jobs = AgentEvalJobStore(db_path)
    claim = await jobs.claim_next_job(run_id=run_id, worker_id="insights.test")
    assert claim is not None
    turn_id = f"t_eval_{claim.job.job_id[4:28]}"
    await jobs.start_scratch_turn(
        job_id=claim.job.job_id,
        lease_token=claim.lease_token,
        session_id=f"s_eval_{claim.job.job_id[4:28]}",
        turn_id=turn_id,
        user_text="task",
    )
    if answer is not None:
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE turns SET final_answer=? WHERE id=?", (answer, turn_id))
    await jobs.settle_job(
        job_id=claim.job.job_id,
        lease_token=claim.lease_token,
        status=status,  # type: ignore[arg-type]
        deterministic=deterministic or {"passed": status == "passed"},
        trace_evidence_ref=f"turn:{turn_id}",
        duration_ms=10,
        failure_code=failure_code or ("agent_eval_attempt_failed" if status == "infra_failed" else None),
        subject_provider_ref="model-config",
        subject_provider_hash=f"sha256:{'6' * 64}",
        reproducibility="exact",
        usage=usage,
    )


async def _progress(db_path: Path, execution_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        return await execution_progress_conn(db, execution_id, now=now)


async def test_progress_counts_cases_attempts_settled_usage_and_eta(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)

    queued = await _progress(db_path, started.id)
    # Jobs are materialized at start, so N is known while the comparison is still queued.
    assert queued["cases"] == {"completed": 0, "total": 2}
    # Owner and platform suites: 3 attempts each on Live and the candidate.
    assert queued["attempts"] == {"completed": 0, "total": 12, "in_flight": 0}
    assert queued["elapsed_ms"] is None and queued["eta_ms"] is None
    assert queued["usage"]["tokens"] == 0 and queued["usage"]["partial"] is False
    assert queued["model_calls"] == {"used": 0, "basis": "successful_calls", "limit": None, "estimated_total": None}

    await comparison.mark_running(started.id)
    eval_store = AgentEvalStore(db_path)
    runs = _runs(started)
    for run_id in runs.values():
        await eval_store.mark_run_running(run_id)
    usage = EvalAttemptUsage(tokens_input=100, tokens_output=20, tokens_cached=5, model_call_count=2)
    for _ in range(3):
        await _settle(db_path, runs[("aev_owner", "candidate")], status="passed", usage=usage)
    with sqlite3.connect(db_path) as db:
        started_at = datetime.fromisoformat(db.execute("SELECT started_at FROM agent_eval_comparison_execution").fetchone()[0])
    running = await _progress(db_path, started.id, now=started_at + timedelta(seconds=60))
    # The owner case still waits for its baseline side, so it is not complete.
    assert running["cases"] == {"completed": 0, "total": 2}
    assert running["attempts"]["completed"] == 3
    assert running["usage"] | {"estimated_total_tokens": None} == {
        "tokens": 360,
        "tokens_input": 300,
        "tokens_output": 60,
        "tokens_cached": 15,
        "estimated_total_tokens": None,
        "settled_only": True,
        "partial": False,
    }
    assert running["model_calls"]["used"] == 6
    assert running["eta_ms"] is not None and running["eta_ms"]["low"] < running["eta_ms"]["high"]
    assert running["elapsed_ms"] == 60_000
    # 60 s for 3 of 12 attempts: 180 s remaining, reported as ±25%.
    assert running["eta_ms"] == {"low": 135_000, "high": 225_000}

    for _ in range(3):
        await _settle(db_path, runs[("aev_owner", "baseline")], status="passed", usage=usage)
    # The estimate is computed once per execution: the earlier polls cached "unavailable".
    cached = await _progress(db_path, started.id)
    assert cached["usage"]["estimated_total_tokens"] is None
    agent_eval_insights._PROGRESS_ESTIMATES.pop(started.id)
    estimated = await _progress(db_path, started.id)
    assert estimated["cases"] == {"completed": 1, "total": 2}
    # Six settled samples of these cases now give a D6 midpoint for the whole execution (12 attempts).
    assert estimated["usage"]["estimated_total_tokens"] == 12 * 120
    assert estimated["model_calls"]["estimated_total"] == 12 * 2

    async def must_not_recompute(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("progress polls must reuse the execution's estimate")

    monkeypatch.setattr(agent_eval_insights, "estimate_usage_conn", must_not_recompute)
    again = await _progress(db_path, started.id)
    assert again["usage"]["estimated_total_tokens"] == 12 * 120

    for _ in range(3):
        await _settle(db_path, runs[("aev_platform", "baseline")], status="passed", usage=None)
        await _settle(db_path, runs[("aev_platform", "candidate")], status="passed", usage=None)
    legacy = await _progress(db_path, started.id)
    assert legacy["cases"] == {"completed": 2, "total": 2}
    # A result row without usage makes the total partial instead of a misleading sum.
    assert legacy["usage"]["tokens"] is None and legacy["usage"]["partial"] is True
    _assert_usage_only(legacy)


async def test_eta_is_hidden_below_three_settled_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    await comparison.mark_running(started.id)
    runs = _runs(started)
    for run_id in runs.values():
        await AgentEvalStore(db_path).mark_run_running(run_id)
    usage = EvalAttemptUsage(tokens_input=1, tokens_output=1, tokens_cached=0, model_call_count=1)
    for _ in range(2):
        await _settle(db_path, runs[("aev_owner", "baseline")], status="passed", usage=usage)
    assert (await _progress(db_path, started.id))["eta_ms"] is None


async def test_cancelled_comparison_keeps_spent_usage_without_turning_partial(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    running = await comparison.mark_running(started.id)
    runs = _runs(started)
    for run_id in runs.values():
        await AgentEvalStore(db_path).mark_run_running(run_id)
    usage = EvalAttemptUsage(tokens_input=40, tokens_output=2, tokens_cached=0, model_call_count=1)
    await _settle(db_path, runs[("aev_owner", "candidate")], status="passed", usage=usage)
    await comparison.cancel(
        **_OWNER,
        execution_id=started.id,
        actor_user_id="user_owner",
        expected_execution_row_version=running.row_version,
    )
    final = await _progress(db_path, started.id)
    assert final["attempts"] == {"completed": 12, "total": 12, "in_flight": 0}
    # Attempts fenced by the cancellation carry no usage; the spent usage stays visible.
    assert final["usage"]["tokens"] == 42 and final["usage"]["partial"] is False
    assert final["eta_ms"] is None and final["usage"]["estimated_total_tokens"] is None


def test_history_read_uses_the_full_run_subject_index(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    with sqlite3.connect(db_path) as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN " + agent_eval_insights.HISTORY_SAMPLES_SQL,
            ("team_p", "osa_policy", "2026-01-01", 10),
        ).fetchall()
    details = " | ".join(str(row[-1]) for row in plan)
    assert "idx_agent_eval_run_subject (team_id=? AND agent_source_kind=? AND agent_id=?)" in details


def _insert_history(db_path: Path, run_id: str, case_id: str, attempts: int, *, tokens: int) -> None:
    with sqlite3.connect(db_path) as db:
        for attempt in range(1, attempts + 1):
            db.execute(
                """INSERT INTO agent_eval_case_result(run_id,case_revision_id,attempt,status,deterministic_json,created_at,
                   tokens_input,tokens_output,tokens_cached,model_call_count) VALUES(?,?,?,'passed','{}',?,?,?,?,?)""",
                (run_id, case_id, attempt, datetime.now(UTC).isoformat(), tokens + attempt, 10, 3, 2),
            )


async def test_comparison_estimate_is_owner_only_exact_plan_and_needs_five_samples(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, _comparison, started = await _start(db_path)
    args = {**_OWNER, "actor_user_id": "user_owner", "plan_id": plan.id, "plan_hash": plan.plan_hash}

    empty = await read_comparison_estimate(str(db_path), **args)
    assert (empty["status"], empty["unavailable_reason"]) == ("unavailable", "insufficient_history")
    assert empty["attempts_planned"] == 12 and empty["cases_planned"] == 2

    runs = _runs(started)
    for run_id in runs.values():
        await AgentEvalStore(db_path).mark_run_running(run_id)
    _insert_history(db_path, runs[("aev_owner", "baseline")], "aec_aes_owner", 3, tokens=100)
    _insert_history(db_path, runs[("aev_owner", "candidate")], "aec_aes_owner", 3, tokens=200)
    estimate = await read_comparison_estimate(str(db_path), **args)
    assert estimate["status"] == "available"
    assert estimate["basis"] == {"window_days": 30, "scope": "same_cases", "sample_attempts": 6}
    # Scaled from the sampled attempts to the 12 planned ones (platform now runs on both sides).
    assert estimate["model_calls"] == {"low": 24, "high": 25}
    assert estimate["tokens"]["low"] < estimate["tokens"]["high"]
    _assert_usage_only(estimate)

    with pytest.raises(AgentEvalInsightsError, match="agent_eval_plan_version_conflict"):
        await read_comparison_estimate(str(db_path), **(args | {"plan_hash": "sha256:" + "0" * 64}))
    unknown = await read_comparison_estimate(str(db_path), **(args | {"plan_id": "aep_missing"}))
    assert unknown["unavailable_reason"] == "plan_unavailable"
    with pytest.raises(AgentRevisionForbiddenError):
        await read_comparison_estimate(str(db_path), **(args | {"actor_user_id": "user_other"}))
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_eval_plan").fetchone() == (1,)


async def _complete(db_path: Path, comparison: AgentEvalComparisonStore, started, *, candidate_status: str) -> None:
    await comparison.mark_running(started.id)
    eval_store = AgentEvalStore(db_path)
    runs = _runs(started)
    for run_id in runs.values():
        await eval_store.mark_run_running(run_id)
    usage = EvalAttemptUsage(tokens_input=10, tokens_output=5, tokens_cached=0, model_call_count=1)
    for _ in range(3):
        await _settle(db_path, runs[("aev_owner", "baseline")], status="passed", usage=usage, answer="Baseline answer")
        await _settle(db_path, runs[("aev_owner", "candidate")], status=candidate_status, usage=usage, answer="Candidate answer")
    for _ in range(3):
        await _settle(db_path, runs[("aev_platform", "baseline")], status="passed", usage=usage)
        await _settle(db_path, runs[("aev_platform", "candidate")], status="passed", usage=usage)
    for key, run_id in runs.items():
        terminal = candidate_status if key == ("aev_owner", "candidate") else "passed"
        await eval_store.mark_run_terminal(run_id, status=terminal, summary={"schema_version": 1})  # type: ignore[arg-type]
    finalized = await comparison.finalize(started.id)
    assert finalized.snapshot is not None


async def test_case_changes_list_only_changed_cases_with_exact_pass_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    args = {**_OWNER, "execution_id": started.id, "actor_user_id": "user_owner"}
    with pytest.raises(AgentEvalInsightsError, match="agent_eval_case_changes_unavailable"):
        await read_case_changes(str(db_path), **args)

    await _complete(db_path, comparison, started, candidate_status="failed")
    changes = await read_case_changes(str(db_path), **args)
    assert changes["summary"] == {
        "case_count": 2,
        "worse": 1,
        "unstable": 0,
        "better": 0,
        "not_comparable": 0,
        "unchanged_required_failures": 0,
    }
    [item] = changes["items"]
    assert item["change"] == "worse" and item["classification"] == "regressed"
    assert item["baseline"] == {"passed": 3, "failed": 0, "runs": 3}
    assert item["candidate"] == {"passed": 0, "failed": 3, "runs": 3}
    assert item["key_case"] is False and item["suite_name"] == "owner_quality"
    assert item["case_key"] == "case-aes_owner"
    assert changes["baseline"] == {"release_id": "arl_policy_base", "release_sequence": 1}
    assert changes["candidate"] == {"candidate_id": "arc_policy", "release_sequence": None}
    assert changes["next_offset"] is None
    _assert_usage_only(changes)

    with pytest.raises(AgentRevisionForbiddenError):
        await read_case_changes(str(db_path), **(args | {"actor_user_id": "user_other"}))
    with pytest.raises(AgentEvalInsightsError, match="agent_eval_comparison_not_found"):
        await read_case_changes(str(db_path), **(args | {"candidate_id": "arc_other"}))

    answers = await read_case_answers(str(db_path), **args, case_revision_id="aec_aes_owner")
    assert [attempt["unsourced_figures"] for attempt in answers["candidate"]["attempts"]] == [[], [], []]
    assert [attempt["answer"] for attempt in answers["baseline"]["attempts"]] == ["Baseline answer"] * 3
    assert [attempt["status"] for attempt in answers["candidate"]["attempts"]] == ["failed"] * 3
    assert answers["candidate"]["attempts"][0]["answer"] == "Candidate answer"
    platform = await read_case_answers(str(db_path), **args, case_revision_id="aec_aes_platform")
    # The platform contract also ran on Live; it records no answers on either side.
    assert platform["baseline"]["attempts"][0]["answer"] is None and platform["candidate"]["attempts"][0]["answer"] is None
    for forbidden in ("expected_contract", "scorer_config", "fixture_ref", "deterministic", "judge"):
        assert forbidden not in json.dumps(answers)
    _assert_usage_only(answers)
    with pytest.raises(AgentEvalInsightsError, match="agent_eval_case_not_found"):
        await read_case_answers(str(db_path), **args, case_revision_id="aec_unrelated")
    with pytest.raises(AgentRevisionForbiddenError):
        await read_case_answers(str(db_path), **(args | {"actor_user_id": "user_other"}), case_revision_id="aec_aes_owner")


@pytest.mark.asyncio
async def test_cases_left_without_a_result_are_listed_with_their_cause_and_reference(tmp_path: Path) -> None:
    # 1.5.91: both versions' attempts stopped with agent_eval_attempt_failed; case-changes counted the cases as
    # not comparable but listed none, so the page had nothing to name. Every such case is listed now, with the
    # cause, the role or Tool, and the attempt reference the server logged.
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    args = {**_OWNER, "execution_id": started.id, "actor_user_id": "user_owner"}
    await comparison.mark_running(started.id)
    eval_store = AgentEvalStore(db_path)
    runs = _runs(started)
    for run_id in runs.values():
        await eval_store.mark_run_running(run_id)
    no_model = {
        "passed": False,
        "critical_failure": False,
        "scorers": [],
        "failure": {"category": "model_unavailable", "cause": "role_unassigned", "role": "analyst", "reference": "aej_ref_model"},
    }
    for _ in range(3):
        await _settle(db_path, runs[("aev_owner", "baseline")], status="infra_failed", usage=None, failure_code="agent_eval_attempt_failed")
        await _settle(
            db_path,
            runs[("aev_owner", "candidate")],
            status="infra_failed",
            usage=None,
            failure_code="agent_eval_model_unavailable",
            deterministic=no_model,
        )
        await _settle(db_path, runs[("aev_platform", "baseline")], status="passed", usage=None)
        await _settle(db_path, runs[("aev_platform", "candidate")], status="passed", usage=None)
    for key, run_id in runs.items():
        await eval_store.mark_run_terminal(run_id, status="incomplete" if key[0] == "aev_owner" else "passed", summary={"schema_version": 1})
    assert (await comparison.finalize(started.id)).snapshot is not None

    changes = await read_case_changes(str(db_path), **args)

    assert changes["summary"]["not_comparable"] == 1
    [case] = changes["unfinished"]
    assert case["case_revision_id"] == "aec_aes_owner"
    assert {"attempt_model_unavailable", "attempt_infrastructure_incomplete"} <= set(case["reason_codes"])
    by_reason = {failure["reason_code"]: failure for failure in case["failures"]}
    assert by_reason["attempt_model_unavailable"]["role"] == "analyst"
    assert by_reason["attempt_model_unavailable"]["references"] == ["aej_ref_model"]
    # An older attempt without stored facts is an unclassified platform error with its own job reference.
    platform = by_reason["attempt_infrastructure_incomplete"]
    assert platform["category"] == "platform" and platform["references"] and all(ref.startswith("aej_") for ref in platform["references"])

    answers = await read_case_answers(str(db_path), **args, case_revision_id="aec_aes_owner")
    assert {attempt["failure"]["category"] for attempt in answers["candidate"]["attempts"]} == {"model_unavailable"}
    assert {attempt["failure"]["category"] for attempt in answers["baseline"]["attempts"]} == {"platform"}

    unmet = await read_unmet_points(str(db_path), **args)
    assert {(item["side"], item["category"]) for item in unmet["unfinished_attempts"]} == {
        ("baseline", "platform"),
        ("candidate", "model_unavailable"),
    }


@pytest.mark.parametrize(
    ("failure_code", "evidence", "reason_code"),
    [
        ("agent_eval_required_tool_not_run", {"required_tools_not_run": ["python_sandbox"]}, "required_tool_not_run_in_eval"),
        # The answer depended on a Tool the evaluation can't run, though the case names no required Tool.
        ("agent_eval_tool_not_run_in_eval", {"tools_not_run": ["python_sandbox"]}, "tool_not_run_in_eval"),
    ],
)
async def test_case_changes_name_the_cases_and_tools_a_comparison_could_not_run(
    tmp_path: Path, failure_code: str, evidence: dict[str, Any], reason_code: str
) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    args = {**_OWNER, "execution_id": started.id, "actor_user_id": "user_owner"}
    await comparison.mark_running(started.id)
    eval_store = AgentEvalStore(db_path)
    runs = _runs(started)
    for run_id in runs.values():
        await eval_store.mark_run_running(run_id)
    not_run = {"passed": False, "critical_failure": False, "scorers": [], **evidence}
    structured = json.dumps({"delivery_surface": "chat", "exec_summary": "## Weekly report\n\n| Region | Sales |"})
    for _ in range(3):
        await _settle(db_path, runs[("aev_owner", "baseline")], status="passed", usage=None, answer=structured)
        await _settle(
            db_path,
            runs[("aev_owner", "candidate")],
            status="infra_failed",
            usage=None,
            failure_code=failure_code,
            deterministic=not_run,
        )
        await _settle(db_path, runs[("aev_platform", "baseline")], status="passed", usage=None)
        await _settle(db_path, runs[("aev_platform", "candidate")], status="passed", usage=None)
    for key, run_id in runs.items():
        await eval_store.mark_run_terminal(
            run_id, status="incomplete" if key == ("aev_owner", "candidate") else "passed", summary={"schema_version": 1}
        )
    assert (await comparison.finalize(started.id)).snapshot is not None

    changes = await read_case_changes(str(db_path), **args)

    assert changes["summary"]["not_comparable"] == 1 and changes["items"] == []
    assert changes["unfinished"] == [
        {
            "suite_revision_id": "aev_owner",
            "suite_name": "owner_quality",
            "case_revision_id": "aec_aes_owner",
            "case_key": "case-aes_owner",
            "case_label": changes["unfinished"][0]["case_label"],
            "reason_codes": [reason_code],
            "tools": ["python_sandbox"],
            # A required Tool that can't run has its own reason, not an attempt-failure cause.
            "failures": [],
            # No attempt on the change reached a verdict.
            "baseline": {"passed": 3, "failed": 0, "runs": 3},
            "candidate": {"passed": 0, "failed": 0, "runs": 3},
        }
    ]
    answers = await read_case_answers(str(db_path), **args, case_revision_id="aec_aes_owner")
    # A structured answer reads as its Chat text, not as JSON.
    assert [attempt["answer"] for attempt in answers["baseline"]["attempts"]] == ["## Weekly report\n\n| Region | Sales |"] * 3


def test_an_attempt_lists_each_required_action_with_why_it_was_not_met() -> None:
    action = {
        "id": "p_0001",
        "kind": "required_action",
        "text": "Uses Run code",
        "source": "owner",
        "state": "confirmed",
        "action": {"tool": "python_sandbox"},
    }
    either = {**action, "id": "p_0002", "action": {"any_of": ["bi_query", "powerbi_query"]}}
    suggested = {**action, "id": "p_0003", "state": "suggested"}
    expected = {"points": [action, either, suggested]}

    def stored(missing: list[str], **extra: Any) -> str:
        row = {"name": "tool_contract", "passed": not missing, "applicable": True, "data": {"missing": missing, "forbidden_called": []}}
        return json.dumps({"scorers": [row], **extra})

    rows = agent_eval_insights._action_point_rows(expected, stored(["python_sandbox"]), failure_code=None)
    assert [(row["id"], row["met"], row["reason"]) for row in rows] == [("p_0001", False, "tool_not_called"), ("p_0002", True, None)]
    not_run = json.dumps({"scorers": [], "required_tools_not_run": ["python_sandbox"]})
    assert [
        (row["id"], row["met"], row["reason"])
        for row in agent_eval_insights._action_point_rows(expected, not_run, failure_code="agent_eval_required_tool_not_run")
    ] == [("p_0001", None, "tool_not_run_in_eval")]
    # An answer that depended on a Tool the evaluation can't run names single Tools.
    denied = json.dumps({"scorers": [], "tools_not_run": ["bi_query"]})
    assert [
        (row["id"], row["met"], row["reason"])
        for row in agent_eval_insights._action_point_rows(expected, denied, failure_code="agent_eval_tool_not_run_in_eval")
    ] == [("p_0002", None, "tool_not_run_in_eval")]
    # An attempt that stopped before scoring has no verdict to show.
    assert agent_eval_insights._action_point_rows(expected, json.dumps({"scorers": []}), failure_code="agent_eval_attempt_timeout") == []

    # A required action is listed as a point, so the tool contract is not named again for it.
    assert agent_eval_insights._failed_checks(expected, stored(["python_sandbox"])) == []
    assert agent_eval_insights._failed_checks(expected, stored(["web_search"])) == ["tool_contract"]
    terminal = json.dumps(
        {"scorers": [{"name": "terminal_contract", "passed": False}, {"name": "no_fabrication", "passed": False, "advisory": True}]}
    )
    assert agent_eval_insights._failed_checks(expected, terminal) == ["terminal_contract"]


def test_answers_view_withholds_the_failure_breaker_instruction() -> None:
    stopped = (
        "insufficient_data: peer_total_failures=8 ≥ 8. Too many cross-tool failures (datarc unstable / query unsupported); "
        "return insufficient_data instead of retrying further. 已停止迭代以节省用户时间."
    )
    assert agent_eval_insights._reader_case_answer(stopped) == {
        "answer": None,
        "answer_truncated": False,
        "answer_withheld": "stopped_without_answer",
    }
    assert agent_eval_insights._reader_case_answer("Sales were 500.") == {
        "answer": "Sales were 500.",
        "answer_truncated": False,
        "answer_withheld": None,
    }
    assert agent_eval_insights._attempt_tools_not_run(
        "agent_eval_tool_not_run_in_eval", json.dumps({"tools_not_run": ["bi_query", "bi_schema"]})
    ) == [
        "bi_query",
        "bi_schema",
    ]
    assert agent_eval_insights._attempt_tools_not_run(
        "agent_eval_required_tool_not_run", json.dumps({"required_tools_not_run": ["bi_query|powerbi_query"]})
    ) == ["bi_query", "powerbi_query"]
    assert agent_eval_insights._attempt_tools_not_run("agent_eval_attempt_failed", json.dumps({"tools_not_run": ["bi_query"]})) == []


def test_unsourced_figures_come_only_from_an_applicable_unmatched_number_check() -> None:
    def stored(row: dict[str, Any]) -> str:
        return json.dumps({"scorers": [{"name": "citation", "passed": False, "data": {"violations": [1.0]}}, row]})

    unmatched = {"name": "no_fabrication", "applicable": True, "passed": False, "data": {"violations": [1200.0, 13.75, 1200.0]}}
    assert _unsourced_figures(stored(unmatched)) == ["1200", "13.75"]
    assert _unsourced_figures(stored({**unmatched, "applicable": False})) == []
    assert _unsourced_figures(stored({**unmatched, "passed": True})) == []
    assert _unsourced_figures(None) == [] and _unsourced_figures("not json") == []


async def test_unchanged_required_failures_are_listed_for_release_recovery(tmp_path: Path) -> None:
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
    changes = await read_case_changes(str(db_path), **_OWNER, execution_id=started.id, actor_user_id="user_owner")
    [item] = changes["items"]
    assert item["change"] == "required_failure"
    assert item["suite_name"] == "owner_quality"
    assert item["baseline"] == {"passed": 0, "failed": 3, "runs": 3}
    assert item["candidate"] == {"passed": 0, "failed": 3, "runs": 3}
    assert changes["summary"]["unchanged_required_failures"] == 1


# ── Coach: consent estimate and dispatch-admission progress ────────────


async def test_coach_projection_adds_estimate_and_dispatch_progress(tmp_path: Path, monkeypatch) -> None:
    from bowhead_core.coach_ci_delegation import CoachCIDelegations
    from bowhead_router.types import TokenUsage
    from test_coach_ci_delegation import OWNER, SPEC, fixture, guard_for, prepare, start

    path, store, revisions, _args, pending = await fixture(tmp_path, monkeypatch, calls=2)
    assert "estimate" in await store.get(**OWNER, delegation_id=pending["delegation_id"])

    requested = await CoachCIDelegations(str(path)).get(**OWNER, delegation_id=pending["delegation_id"])
    estimate = requested["estimate"]
    # This fixture has no platform contract suite, so the default Policy cannot compile: never 0.
    assert estimate["status"] == "unavailable" and estimate["unavailable_reason"] == "plan_unavailable"
    assert estimate["per_comparison"] is None and estimate["tokens_ceiling_at_cap"] is None
    assert (estimate["model_call_cap"], estimate["max_comparisons"]) == (2, 1)
    _assert_usage_only(estimate)

    prepared = await prepare(store, revisions, pending)
    _comparisons, _start_args, execution = await start(path, pending, prepared)
    guard = await guard_for(path, execution)
    ticket = await guard.begin(SPEC)
    await ticket.complete(usage=TokenUsage(input_tokens=7, output_tokens=3, cached_tokens=1), response_digest="b" * 64)
    running = await store.get(**OWNER, delegation_id=pending["delegation_id"])
    assert running["estimate"] is None
    [item] = running["executions"]
    assert item["progress"]["model_calls"] == {
        "used": 1,
        "basis": "dispatch_admissions",
        "limit": 2,
        "delegation_used": 1,
        "estimated_total": None,
    }
    assert item["progress"]["usage"]["tokens"] == 10 and item["progress"]["usage"]["tokens_cached"] == 1
    # The existing projection still carries its legacy marker; the new fields carry no money.
    _assert_usage_only({key: value for key, value in running.items() if key != "provider_currency_cost"})


async def _finish_owner_case(db_path: Path, candidate: tuple[tuple[str, str | None], ...]):
    """Settle the Owner case on the change with these (status, failure code) attempts; everything else passes."""
    _plan, comparison, started = await _start(db_path)
    await comparison.mark_running(started.id)
    eval_store = AgentEvalStore(db_path)
    runs = _runs(started)
    for run_id in runs.values():
        await eval_store.mark_run_running(run_id)
    for status, failure_code in candidate:
        await _settle(db_path, runs[("aev_owner", "baseline")], status="passed", usage=None)
        await _settle(db_path, runs[("aev_owner", "candidate")], status=status, usage=None, failure_code=failure_code)
        await _settle(db_path, runs[("aev_platform", "baseline")], status="passed", usage=None)
        await _settle(db_path, runs[("aev_platform", "candidate")], status="passed", usage=None)
    for key, run_id in runs.items():
        await eval_store.mark_run_terminal(run_id, status="failed" if key == ("aev_owner", "candidate") else "passed", summary={"schema_version": 1})
    finalized = await comparison.finalize(started.id)
    assert finalized.snapshot is not None
    changes = await read_case_changes(str(db_path), **_OWNER, execution_id=started.id, actor_user_id="user_owner")
    return finalized.snapshot, changes


async def test_a_case_its_majority_decided_is_not_unstable_and_its_split_stays_listed(tmp_path: Path) -> None:
    """2 of 3 attempts passing passes a standard case; the disagreement is still shown with both sides' attempts."""
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    snapshot, changes = await _finish_owner_case(db_path, (("passed", None), ("passed", None), ("failed", None)))

    assert snapshot.readiness == "ready"
    assert changes["summary"]["unstable"] == 0 and changes["summary"]["not_comparable"] == 0 and changes["items"] == []
    [item] = changes["split"]
    assert item["classification"] == "unchanged_pass" and item["case_revision_id"] == "aec_aes_owner"
    assert item["candidate"] == {"passed": 2, "failed": 1, "runs": 3}
    assert item["baseline"] == {"passed": 3, "failed": 0, "runs": 3}


async def test_an_even_split_is_unstable_and_listed_with_how_its_attempts_split(tmp_path: Path) -> None:
    """No majority (one pass, one fail, one attempt cut by its time limit) is enough for "no conclusion"."""
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    snapshot, changes = await _finish_owner_case(db_path, (("passed", None), ("failed", None), ("infra_failed", "agent_eval_attempt_timeout")))

    assert snapshot.readiness == "unknown"
    assert changes["summary"]["unstable"] == 1 and changes["summary"]["not_comparable"] == 0 and changes["split"] == []
    [item] = changes["items"]
    assert item["change"] == "unstable" and item["reason_codes"] == ["flaky_limit_exceeded"]
    assert item["candidate"] == {"passed": 1, "failed": 1, "runs": 3}
    assert item["baseline"] == {"passed": 3, "failed": 0, "runs": 3}


async def test_run_answers_preserve_passed_answers_and_scope_to_the_frozen_run(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path)
    _seed(path)
    _plan, _comparison, started = await _start(path)
    run_id = _runs(started)[("aev_owner", "candidate")]
    await AgentEvalStore(path).mark_run_running(run_id)
    await _settle(path, run_id, status="passed", usage=None, answer='{"exec_summary":"Total: 999"}')
    args = {"team_id": "team_p", "agent_id": "osa_policy", "run_id": run_id, "case_revision_id": "aec_aes_owner", "actor_user_id": "user_owner"}
    value = await read_run_case_answers(str(path), **args)
    assert value["attempts"][0]["answer"] == "Total: 999"
    assert value["attempts"][0]["status"] == "passed"
    assert value["revision_id"] == "arv_policy_candidate"
    with pytest.raises(AgentEvalInsightsError, match="case_not_found"):
        await read_run_case_answers(str(path), **(args | {"case_revision_id": "aec_aes_platform"}))
    with pytest.raises(AgentEvalInsightsError, match="case_not_found"):
        await read_run_case_answers(str(path), **(args | {"run_id": "aer_missing"}))
    with pytest.raises(AgentRevisionForbiddenError):
        await read_run_case_answers(str(path), **(args | {"actor_user_id": "user_other"}))
