"""Owner-facing Agent Eval run insights: usage estimates, progress and per-case changes.

Every number here is usage (tokens, model requests, duration). No cost, credit or
currency figure is part of this contract: token prices differ by model and
provider. Reads derive from committed, immutable rows and write nothing.

See `docs/design/agent-ci-evaluation-feedback-tech-design.md` (D6, D7, D8, D13).
"""

from __future__ import annotations

import json
import logging
import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_change_proposal_store import _require_owner_conn
from bowhead_core.agent_eval_attempt_failure import INCOMPLETE_ATTEMPT_REASONS, attempt_failure_detail

logger = logging.getLogger(__name__)

ESTIMATE_WINDOW_DAYS = 30
#: Fewer settled samples than this gives `unavailable`, never a misleading `0`.
MIN_ESTIMATE_SAMPLES = 5
#: Upper bound on history rows read per estimate (one bounded aggregate read).
_MAX_HISTORY_ROWS = 5000
#: ETA appears only after this many attempts have settled.
_MIN_ETA_ATTEMPTS = 3
_ANSWER_MAX_BYTES = 32 * 1024
_CASE_LABEL_MAX_CHARS = 80

_TERMINAL_JOBS = frozenset({"completed", "failed", "cancelled"})
_IN_FLIGHT_JOBS = frozenset({"leased", "running"})
_TERMINAL_EXECUTIONS = frozenset({"completed", "failed", "cancelled"})
_SAMPLE_STATUSES = ("passed", "failed", "judge_unknown")
_UNSTABLE_REASONS = frozenset({"flaky_limit_exceeded", "minimum_delta_not_met"})
_CHANGE_ORDER = {"required_failure": 0, "worse": 1, "unstable": 2, "better": 3}

Change = Literal["required_failure", "worse", "unstable", "better", "not_comparable", "unchanged"]
EstimateScope = Literal["same_cases", "same_suites", "agent"]


class AgentEvalInsightsError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ── D6: usage estimate ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PlannedSuite:
    suite_revision_id: str
    attempts: int
    paired: bool
    case_revision_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlannedWork:
    """Attempts one comparison will run; history is matched by case, then suite, then Agent."""

    cases_planned: int
    attempts_planned: int
    case_revision_ids: frozenset[str]
    suite_ids: frozenset[str]

    @classmethod
    def from_suites(cls, suites: Sequence[PlannedSuite], *, suite_ids: Iterable[str]) -> PlannedWork:
        cases: set[str] = set()
        attempts = 0
        for suite in suites:
            cases.update(suite.case_revision_ids)
            attempts += len(suite.case_revision_ids) * suite.attempts * (2 if suite.paired else 1)
        return cls(
            cases_planned=len(cases),
            attempts_planned=attempts,
            case_revision_ids=frozenset(cases),
            suite_ids=frozenset(suite_ids),
        )


@dataclass(frozen=True, slots=True)
class _Sample:
    case_revision_id: str
    suite_id: str
    tokens: int | None
    tokens_cached: int | None
    model_calls: int | None
    duration_ms: float | None


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (`q` in [0, 1]) of a non-empty sequence."""
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def scaled_range(values: Sequence[float], planned: int) -> dict[str, int] | None:
    """`planned × P25` .. `planned × P75`, floored/ceiled with `high >= low + 1`."""
    if len(values) < MIN_ESTIMATE_SAMPLES or planned <= 0:
        return None
    low = math.floor(planned * percentile(values, 0.25))
    high = math.ceil(planned * percentile(values, 0.75))
    return {"low": low, "high": max(high, low + 1)}


def range_midpoint(value: Mapping[str, int] | None) -> int | None:
    if value is None:
        return None
    return (int(value["low"]) + int(value["high"])) // 2


def _unavailable(reason: str, *, work: PlannedWork | None = None) -> dict[str, Any]:
    return {
        "kind": "agent_eval_usage_estimate",
        "schema_version": 1,
        "status": "unavailable",
        "unavailable_reason": reason,
        "cases_planned": work.cases_planned if work is not None else None,
        "attempts_planned": work.attempts_planned if work is not None else None,
        "tokens": None,
        "tokens_cached": None,
        "model_calls": None,
        "duration_ms": None,
        "basis": None,
        "computed_at": _iso(_utc_now()),
    }


#: Settled, non-truncated attempts of one team Agent in the window, newest first, bounded.
#: `agent_source_kind='team'` completes the `idx_agent_eval_run_subject` prefix
#: (team_id, agent_source_kind, agent_id); without it the read scans the Team's runs.
HISTORY_SAMPLES_SQL = """
        SELECT result.run_id,result.case_revision_id,suite_revision.suite_id,
               result.tokens_input,result.tokens_output,result.tokens_cached,result.model_call_count,
               turn.tokens_input AS turn_tokens_input,turn.tokens_output AS turn_tokens_output,
               link.execution_id,execution.started_at AS execution_started_at,
               execution.finished_at AS execution_finished_at
        FROM agent_eval_case_result result
        JOIN agent_eval_run run ON run.id=result.run_id
        JOIN agent_eval_suite_revision suite_revision ON suite_revision.id=run.suite_revision_id
        LEFT JOIN turns turn
          ON result.tokens_input IS NULL AND run.judge_role_ref IS NULL
         AND result.trace_evidence_ref LIKE 'turn:%' AND turn.id=substr(result.trace_evidence_ref,6)
        LEFT JOIN agent_eval_comparison_run link ON link.run_id=result.run_id
        LEFT JOIN agent_eval_comparison_execution execution
          ON execution.id=link.execution_id AND execution.status='completed'
        WHERE run.team_id=? AND run.agent_source_kind='team' AND run.agent_id=? AND result.created_at>=?
          AND result.status IN ('passed','failed','judge_unknown')
        ORDER BY result.created_at DESC
        LIMIT ?
"""


async def _history_samples(db: aiosqlite.Connection, *, team_id: str, agent_id: str, since: str) -> list[_Sample]:
    async with db.execute(HISTORY_SAMPLES_SQL, (team_id, agent_id, since, _MAX_HISTORY_ROWS)) as cursor:
        rows = await cursor.fetchall()
    execution_ids = sorted({str(row["execution_id"]) for row in rows if row["execution_id"] is not None and row["execution_started_at"]})
    per_attempt_ms: dict[str, float] = {}
    if execution_ids:
        async with db.execute(
            """
            SELECT link.execution_id,count(*) AS attempts
            FROM agent_eval_comparison_run link
            JOIN agent_eval_case_result result ON result.run_id=link.run_id
            WHERE link.execution_id IN (SELECT value FROM json_each(?))
            GROUP BY link.execution_id
            """,
            (json.dumps(execution_ids),),
        ) as cursor:
            counts = {str(row["execution_id"]): int(row["attempts"]) for row in await cursor.fetchall()}
        for row in rows:
            execution_id = row["execution_id"]
            if execution_id is None or str(execution_id) in per_attempt_ms or not row["execution_started_at"]:
                continue
            elapsed = _elapsed_ms(row["execution_started_at"], row["execution_finished_at"])
            attempts = counts.get(str(execution_id), 0)
            if elapsed is not None and attempts > 0:
                # Wall-clock per attempt of the whole comparison captures its real concurrency.
                per_attempt_ms[str(execution_id)] = elapsed / attempts
    samples: list[_Sample] = []
    for row in rows:
        if row["tokens_input"] is not None:
            tokens: int | None = int(row["tokens_input"]) + int(row["tokens_output"])
            cached: int | None = int(row["tokens_cached"])
            calls: int | None = int(row["model_call_count"])
        elif row["turn_tokens_input"] is not None:
            # Legacy row from a run without a Judge: the scratch Turn holds the subject's tokens.
            tokens = int(row["turn_tokens_input"] or 0) + int(row["turn_tokens_output"] or 0)
            cached, calls = None, None
        else:
            tokens, cached, calls = None, None, None
        execution_id = row["execution_id"]
        samples.append(
            _Sample(
                case_revision_id=str(row["case_revision_id"]),
                suite_id=str(row["suite_id"]),
                tokens=tokens,
                tokens_cached=cached,
                model_calls=calls,
                duration_ms=per_attempt_ms.get(str(execution_id)) if execution_id is not None else None,
            )
        )
    return samples


def estimate_from_samples(
    samples: Sequence[_Sample],
    work: PlannedWork,
    *,
    computed_at: str,
) -> dict[str, Any]:
    """Pure range computation over already-read samples (unit-tested without a database)."""
    if work.attempts_planned <= 0:
        return {**_unavailable("plan_unavailable", work=work), "computed_at": computed_at}
    scopes: tuple[tuple[EstimateScope, Any], ...] = (
        ("same_cases", lambda sample: sample.case_revision_id in work.case_revision_ids),
        ("same_suites", lambda sample: sample.suite_id in work.suite_ids),
        ("agent", lambda sample: True),
    )
    for scope, matches in scopes:
        chosen = [sample for sample in samples if sample.tokens is not None and matches(sample)]
        if len(chosen) < MIN_ESTIMATE_SAMPLES:
            continue
        tokens = [float(sample.tokens or 0) for sample in chosen]
        cached = [float(sample.tokens_cached) for sample in chosen if sample.tokens_cached is not None]
        calls = [float(sample.model_calls) for sample in chosen if sample.model_calls is not None]
        durations = [sample.duration_ms for sample in chosen if sample.duration_ms is not None]
        per_call = [float(sample.tokens or 0) / sample.model_calls for sample in chosen if sample.model_calls is not None and sample.model_calls > 0]
        return {
            "kind": "agent_eval_usage_estimate",
            "schema_version": 1,
            "status": "available",
            "unavailable_reason": None,
            "cases_planned": work.cases_planned,
            "attempts_planned": work.attempts_planned,
            "tokens": scaled_range(tokens, work.attempts_planned),
            # Each metric below needs its own minimum sample count; otherwise it is null, never 0.
            "tokens_cached": scaled_range(cached, work.attempts_planned),
            "model_calls": scaled_range(calls, work.attempts_planned),
            "duration_ms": scaled_range(durations, work.attempts_planned),
            "basis": {"window_days": ESTIMATE_WINDOW_DAYS, "scope": scope, "sample_attempts": len(chosen)},
            "computed_at": computed_at,
            # Internal only: Coach consent derives `tokens_ceiling_at_cap` from it.
            "_tokens_per_call_p90": percentile(per_call, 0.9) if len(per_call) >= MIN_ESTIMATE_SAMPLES else None,
        }
    return {**_unavailable("insufficient_history", work=work), "computed_at": computed_at}


async def estimate_usage_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    work: PlannedWork,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _utc_now()
    since = _iso(current - timedelta(days=ESTIMATE_WINDOW_DAYS))
    samples = await _history_samples(db, team_id=team_id, agent_id=agent_id, since=since)
    return estimate_from_samples(samples, work, computed_at=_iso(current))


def public_estimate(estimate: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in estimate.items() if not key.startswith("_")}


async def _suite_ids_conn(db: aiosqlite.Connection, suite_revision_ids: Iterable[str]) -> list[str]:
    async with db.execute(
        "SELECT DISTINCT suite_id FROM agent_eval_suite_revision WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(set(suite_revision_ids))),),
    ) as cursor:
        return [str(row[0]) for row in await cursor.fetchall()]


async def _case_ids_conn(db: aiosqlite.Connection, suite_revision_id: str) -> tuple[str, ...]:
    async with db.execute(
        "SELECT id FROM agent_eval_case_revision WHERE suite_revision_id=? ORDER BY case_key,id",
        (suite_revision_id,),
    ) as cursor:
        return tuple(str(row[0]) for row in await cursor.fetchall())


async def planned_work_from_plan_conn(db: aiosqlite.Connection, *, plan_id: str) -> PlannedWork | None:
    async with db.execute(
        "SELECT suite_revision_id,attempts,execution_scope FROM agent_eval_plan_suite WHERE plan_id=? ORDER BY order_index",
        (plan_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return None
    suites = [
        PlannedSuite(
            suite_revision_id=str(row["suite_revision_id"]),
            attempts=int(row["attempts"]),
            paired=str(row["execution_scope"]) == "paired",
            case_revision_ids=await _case_ids_conn(db, str(row["suite_revision_id"])),
        )
        for row in rows
    ]
    return PlannedWork.from_suites(suites, suite_ids=await _suite_ids_conn(db, [suite.suite_revision_id for suite in suites]))


async def planned_work_from_policy_conn(db: aiosqlite.Connection, policy: Mapping[str, Any]) -> PlannedWork | None:
    items = policy.get("suite_items")
    if not isinstance(items, list) or not items:
        return None
    suites: list[PlannedSuite] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("suite_revision_id"), str):
            return None
        suites.append(
            PlannedSuite(
                suite_revision_id=item["suite_revision_id"],
                attempts=int(item.get("attempts", 1)),
                paired=item.get("execution_scope") == "paired",
                case_revision_ids=await _case_ids_conn(db, item["suite_revision_id"]),
            )
        )
    return PlannedWork.from_suites(suites, suite_ids=await _suite_ids_conn(db, [suite.suite_revision_id for suite in suites]))


async def read_comparison_estimate(
    db_path: str,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    actor_user_id: str,
    plan_id: str,
    plan_hash: str,
) -> dict[str, Any]:
    """D6 for one exact Plan. Reads only; creates no Plan or Policy."""
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            async with db.execute(
                """
                SELECT plan.plan_hash,control.active_plan_id
                FROM agent_eval_plan plan
                LEFT JOIN agent_eval_candidate_control control ON control.candidate_id=plan.candidate_id
                WHERE plan.id=? AND plan.team_id=? AND plan.agent_id=? AND plan.candidate_id=?
                """,
                (plan_id, team_id, agent_id, candidate_id),
            ) as cursor:
                plan = await cursor.fetchone()
            if plan is None:
                return _unavailable("plan_unavailable")
            if str(plan["plan_hash"]) != plan_hash:
                raise AgentEvalInsightsError("agent_eval_plan_version_conflict")
            if plan["active_plan_id"] != plan_id:
                return _unavailable("plan_identity_changed")
            work = await planned_work_from_plan_conn(db, plan_id=plan_id)
            if work is None:
                return _unavailable("plan_unavailable")
            try:
                return public_estimate(await estimate_usage_conn(db, team_id=team_id, agent_id=agent_id, work=work))
            except Exception:  # noqa: BLE001 — An estimate never blocks a comparison start.
                logger.warning("agent_eval.estimate_failed plan_id=%s", plan_id, exc_info=True)
                return _unavailable("insufficient_history", work=work)
        finally:
            await db.rollback()


async def coach_estimate_conn(db: aiosqlite.Connection, row: Mapping[str, Any], *, has_execution: bool) -> dict[str, Any] | None:
    """D6 consent estimate for a Coach request; `None` outside the pending states."""
    state = row["state"]
    if not (state == "requested" or (state == "authorized" and not has_execution)):
        return None
    max_calls = int(row["max_provider_calls"])
    base: dict[str, Any] = {
        "schema_version": 1,
        "max_comparisons": int(row["max_comparisons"]),
        "model_call_cap": max_calls,
    }
    try:
        work = await _coach_planned_work_conn(db, row)
        if work is None:
            estimate = _unavailable("plan_unavailable")
        else:
            estimate = await estimate_usage_conn(db, team_id=row["team_id"], agent_id=row["agent_id"], work=work)
    except Exception:  # noqa: BLE001 — A missing Policy or suite must never block consent.
        logger.warning("agent_eval.coach_estimate_failed delegation_id=%s", row["id"], exc_info=True)
        estimate = _unavailable("plan_unavailable")
    if estimate["status"] != "available":
        return {
            **base,
            "status": "unavailable",
            "unavailable_reason": estimate["unavailable_reason"],
            "per_comparison": None,
            "tokens_ceiling_at_cap": None,
            "may_exhaust_call_cap": None,
            "basis": None,
        }
    per_call_p90 = estimate.get("_tokens_per_call_p90")
    model_calls = estimate["model_calls"]
    return {
        **base,
        "status": "available",
        "unavailable_reason": None,
        "per_comparison": {key: estimate[key] for key in ("tokens", "tokens_cached", "model_calls", "duration_ms")},
        # An estimated ceiling, not a guarantee: a single call's tokens are not capped.
        "tokens_ceiling_at_cap": math.ceil(max_calls * per_call_p90) if per_call_p90 is not None else None,
        "may_exhaust_call_cap": (int(model_calls["high"]) > max_calls) if model_calls is not None else None,
        "basis": estimate["basis"],
    }


async def _coach_planned_work_conn(db: aiosqlite.Connection, row: Mapping[str, Any]) -> PlannedWork | None:
    if row["plan_id"]:
        return await planned_work_from_plan_conn(db, plan_id=str(row["plan_id"]))
    context = json.loads(str(row["context_json"]))
    frozen = context.get("policy") if isinstance(context, dict) else None
    if isinstance(frozen, dict) and isinstance(frozen.get("id"), str):
        async with db.execute(
            "SELECT policy_json FROM agent_eval_policy_revision WHERE id=? AND team_id=? AND agent_id=?",
            (frozen["id"], row["team_id"], row["agent_id"]),
        ) as cursor:
            policy_row = await cursor.fetchone()
        if policy_row is None:
            return None
        return await planned_work_from_policy_conn(db, json.loads(str(policy_row[0])))
    from bowhead_core.agent_eval_policy_store import AgentEvalPolicyStore

    # The same read-only default compiler the automation context uses; it writes nothing.
    preview = await AgentEvalPolicyStore("").default_preview_conn(
        db,
        team_id=row["team_id"],
        agent_id=row["agent_id"],
        actor_user_id=row["owner_user_id"],
    )
    return await planned_work_from_policy_conn(db, preview)


# ── D7: progress ────────────────────────────────────────────────────────


async def execution_progress_conn(
    db: aiosqlite.Connection,
    execution_id: str,
    *,
    now: datetime | None = None,
    include_estimate: bool = True,
) -> dict[str, Any]:
    """Progress for one comparison from committed rows in the caller's snapshot."""
    current = now or _utc_now()
    async with db.execute(
        """
        SELECT id,team_id,agent_id,status,started_at,finished_at,cancelled_at,coach_delegation_id
        FROM agent_eval_comparison_execution WHERE id=?
        """,
        (execution_id,),
    ) as cursor:
        execution = await cursor.fetchone()
    if execution is None:
        raise AgentEvalInsightsError("agent_eval_comparison_not_found")
    async with db.execute(
        """
        SELECT link.suite_revision_id,link.subject_side,job.case_revision_id,job.status
        FROM agent_eval_comparison_run link
        JOIN agent_eval_case_job job ON job.run_id=link.run_id
        WHERE link.execution_id=?
        """,
        (execution_id,),
    ) as cursor:
        jobs = await cursor.fetchall()
    by_case: dict[tuple[str, str], bool] = {}
    completed_attempts = 0
    in_flight = 0
    for job in jobs:
        key = (str(job["suite_revision_id"]), str(job["case_revision_id"]))
        terminal = str(job["status"]) in _TERMINAL_JOBS
        by_case[key] = by_case.get(key, True) and terminal
        completed_attempts += int(terminal)
        in_flight += int(str(job["status"]) in _IN_FLIGHT_JOBS)
    total_attempts = len(jobs)
    async with db.execute(
        """
        SELECT count(*) AS settled,
               -- Rows fenced by comparison cancellation never ran here (or their in-flight usage is
               -- unknown); only pre-0156 rows make the total partial.
               sum(CASE WHEN result.tokens_input IS NULL AND NOT (
                     result.status='cancelled' AND result.failure_code='agent_eval_comparison_cancelled'
                   ) THEN 1 ELSE 0 END) AS legacy,
               coalesce(sum(result.tokens_input),0) AS tokens_input,
               coalesce(sum(result.tokens_output),0) AS tokens_output,
               coalesce(sum(result.tokens_cached),0) AS tokens_cached,
               coalesce(sum(result.model_call_count),0) AS model_calls
        FROM agent_eval_comparison_run link
        JOIN agent_eval_case_result result ON result.run_id=link.run_id
        WHERE link.execution_id=?
        """,
        (execution_id,),
    ) as cursor:
        settled = await cursor.fetchone()
    terminal_execution = str(execution["status"]) in _TERMINAL_EXECUTIONS
    estimate: dict[str, Any] | None = None
    cached = _PROGRESS_ESTIMATES.get(execution_id) if include_estimate and not terminal_execution else None
    if cached is not None:
        _PROGRESS_ESTIMATES.move_to_end(execution_id)
        estimate = cached[0]
    elif include_estimate and not terminal_execution and total_attempts > 0:
        try:
            work = PlannedWork(
                cases_planned=len(by_case),
                attempts_planned=total_attempts,
                case_revision_ids=frozenset(case_id for _suite, case_id in by_case),
                suite_ids=frozenset(await _suite_ids_conn(db, {suite for suite, _case in by_case})),
            )
            estimate = await estimate_usage_conn(db, team_id=execution["team_id"], agent_id=execution["agent_id"], work=work, now=current)
            if estimate["status"] != "available":
                estimate = None
            _remember_progress_estimate(execution_id, estimate)
        except Exception:  # noqa: BLE001 — The estimate is optional context for progress.
            logger.warning("agent_eval.progress_estimate_failed execution_id=%s", execution_id, exc_info=True)
            estimate = None
    legacy = int(settled["legacy"] or 0)
    partial = legacy > 0
    usage: dict[str, Any] = {
        "tokens": None if partial else int(settled["tokens_input"]) + int(settled["tokens_output"]),
        "tokens_input": None if partial else int(settled["tokens_input"]),
        "tokens_output": None if partial else int(settled["tokens_output"]),
        "tokens_cached": None if partial else int(settled["tokens_cached"]),
        "estimated_total_tokens": range_midpoint(estimate["tokens"]) if estimate is not None else None,
        "settled_only": True,
        "partial": partial,
    }
    model_calls: dict[str, Any] = {
        "used": int(settled["model_calls"]),
        "basis": "successful_calls",
        "limit": None,
        "estimated_total": range_midpoint(estimate["model_calls"]) if estimate is not None else None,
    }
    if execution["coach_delegation_id"]:
        coach_calls, coach_usage = await _coach_progress_conn(db, execution_id, str(execution["coach_delegation_id"]))
        model_calls = coach_calls
        # Settled dispatch receipts are fresher than settled attempt rows for Coach runs.
        usage = {**coach_usage, "estimated_total_tokens": usage["estimated_total_tokens"], "settled_only": True, "partial": False}
    finished = execution["finished_at"] or execution["cancelled_at"]
    elapsed = _elapsed_ms(execution["started_at"], finished if terminal_execution else _iso(current))
    eta: dict[str, int] | None = None
    if not terminal_execution and elapsed is not None and completed_attempts >= _MIN_ETA_ATTEMPTS:
        remaining = max(0, total_attempts - completed_attempts)
        expected = elapsed / completed_attempts * remaining
        eta = {"low": math.floor(expected * 0.75), "high": math.ceil(expected * 1.25)}
    return {
        "schema_version": 1,
        "cases": {"completed": sum(1 for done in by_case.values() if done), "total": len(by_case)},
        "attempts": {"completed": completed_attempts, "total": total_attempts, "in_flight": in_flight},
        "model_calls": model_calls,
        "usage": usage,
        "elapsed_ms": int(elapsed) if elapsed is not None else None,
        "eta_ms": eta,
        "as_of": _iso(current),
    }


#: The D6 estimate behind progress is computed once per execution (including "unavailable"),
#: not on every 2.5 s poll. In-process and bounded; a restart recomputes it once.
_PROGRESS_ESTIMATE_LIMIT = 256
_PROGRESS_ESTIMATES: OrderedDict[str, tuple[dict[str, Any] | None]] = OrderedDict()


def _remember_progress_estimate(execution_id: str, estimate: dict[str, Any] | None) -> None:
    _PROGRESS_ESTIMATES[execution_id] = (estimate,)
    _PROGRESS_ESTIMATES.move_to_end(execution_id)
    while len(_PROGRESS_ESTIMATES) > _PROGRESS_ESTIMATE_LIMIT:
        _PROGRESS_ESTIMATES.popitem(last=False)


async def _coach_progress_conn(db: aiosqlite.Connection, execution_id: str, delegation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    async with db.execute(
        """
        SELECT count(*) AS used,
               coalesce(sum(invocation.tokens_input),0) AS tokens_input,
               coalesce(sum(invocation.tokens_output),0) AS tokens_output,
               coalesce(sum(invocation.tokens_cached),0) AS tokens_cached
        FROM coach_ci_invocation invocation
        JOIN agent_eval_comparison_run link ON link.run_id=invocation.run_id
        WHERE invocation.delegation_id=? AND link.execution_id=?
        """,
        (delegation_id, execution_id),
    ) as cursor:
        execution_row = await cursor.fetchone()
    async with db.execute(
        """
        SELECT delegation.max_provider_calls,
               (SELECT count(*) FROM coach_ci_invocation WHERE delegation_id=delegation.id) AS used
        FROM coach_ci_delegation delegation WHERE delegation.id=?
        """,
        (delegation_id,),
    ) as cursor:
        delegation = await cursor.fetchone()
    tokens_input = int(execution_row["tokens_input"])
    tokens_output = int(execution_row["tokens_output"])
    return (
        {
            # Every admitted dispatch consumes the call budget, including in-flight and failed ones.
            "used": int(execution_row["used"]),
            "basis": "dispatch_admissions",
            "limit": int(delegation["max_provider_calls"]) if delegation is not None else None,
            "delegation_used": int(delegation["used"]) if delegation is not None else None,
            "estimated_total": None,
        },
        {
            "tokens": tokens_input + tokens_output,
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "tokens_cached": int(execution_row["tokens_cached"]),
        },
    )


async def read_execution_progress(db_path: str, execution_id: str) -> dict[str, Any] | None:
    """Progress in its own read snapshot; failures omit progress instead of failing the caller."""
    try:
        async with open_shared_ctx(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            try:
                return await execution_progress_conn(db, execution_id)
            finally:
                await db.rollback()
    except Exception:  # noqa: BLE001 — The UI falls back to status plus elapsed time.
        logger.warning("agent_eval.progress_failed execution_id=%s", execution_id, exc_info=True)
        return None


# ── D8: per-case changes and answers ────────────────────────────────────


def classify_change(classification: str, reason_codes: Iterable[str]) -> Change:
    """Map a frozen case classification to the Owner change table.

    Any infrastructure, attempt or provider-identity reason makes an inconclusive case
    `not_comparable`, even when it is also flaky: its attempts cannot support a verdict.
    """
    reasons = set(reason_codes)
    if classification == "regressed":
        return "worse"
    if classification == "fixed":
        return "better"
    if classification == "inconclusive":
        if reasons and reasons <= _UNSTABLE_REASONS:
            return "unstable"
        return "not_comparable"
    if classification == "incomparable":
        return "not_comparable"
    return "unchanged"


def change_sort_key(item: Mapping[str, Any]) -> tuple[int, str, str, str]:
    """Required failures first, then worse, unstable, better, and case identity."""
    return (
        _CHANGE_ORDER[str(item["change"])],
        str(item.get("case_label") or ""),
        str(item.get("case_key") or ""),
        str(item["case_revision_id"]),
    )


def case_label(input_value: object) -> str | None:
    task = input_value.get("task") if isinstance(input_value, dict) else None
    if not isinstance(task, str) or not task.strip():
        return None
    first_line = task.strip().splitlines()[0].strip()
    return first_line[:_CASE_LABEL_MAX_CHARS]


async def _owned_execution_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    execution_id: str,
    actor_user_id: str,
) -> aiosqlite.Row:
    await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
    async with db.execute(
        """
        SELECT execution.id,execution.status,execution.run_kind,execution.plan_id,plan.baseline_release_id
        FROM agent_eval_comparison_execution execution
        JOIN agent_eval_plan plan ON plan.id=execution.plan_id
        WHERE execution.id=? AND execution.team_id=? AND execution.agent_id=? AND execution.candidate_id=?
        """,
        (execution_id, team_id, agent_id, candidate_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise AgentEvalInsightsError("agent_eval_comparison_not_found")
    return row


async def read_case_changes(
    db_path: str,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    execution_id: str,
    actor_user_id: str,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    if not 0 <= offset <= 10_000 or not 1 <= limit <= 100:
        raise ValueError("Case-change page is invalid")
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            execution = await _owned_execution_conn(
                db, team_id=team_id, agent_id=agent_id, candidate_id=candidate_id, execution_id=execution_id, actor_user_id=actor_user_id
            )
            async with db.execute(
                "SELECT id,snapshot_hash,created_at,identity_json,summary_json FROM agent_eval_snapshot WHERE execution_id=?",
                (execution_id,),
            ) as cursor:
                snapshot = await cursor.fetchone()
            if str(execution["status"]) != "completed" or str(execution["run_kind"]) != "release" or snapshot is None:
                raise AgentEvalInsightsError("agent_eval_case_changes_unavailable")
            identity = json.loads(str(snapshot["identity_json"]))
            summary = json.loads(str(snapshot["summary_json"]))
            run_sides = _run_sides(identity)
            counts: dict[tuple[str, str, str], list[int]] = {}
            # Tools each case required but the evaluation couldn't run, from the attempts that said so.
            not_run_tools: dict[str, set[str]] = {}
            # Why each case's attempts ended without a verdict: cause, Tool and diagnostic reference.
            attempt_failures: dict[str, list[dict[str, Any]]] = {}
            # Whether any attempt read the Team's Datarc live (FEATURE_AGENT_EVAL_DATARC_READ_ONLY).
            datarc_read = False
            if run_sides:
                async with db.execute(
                    """
                    SELECT result.run_id,result.case_revision_id,result.attempt,result.status,result.failure_code,
                           COALESCE(json_extract(result.deterministic_json,'$.required_tools_not_run'),
                                    json_extract(result.deterministic_json,'$.tools_not_run')) AS tools_not_run,
                           json_extract(result.deterministic_json,'$.failure') AS failure_json,
                           json_extract(result.deterministic_json,'$.datarc_calls') AS datarc_calls,
                           job.id AS job_id
                    FROM agent_eval_case_result result
                    LEFT JOIN agent_eval_case_job job
                      ON job.run_id=result.run_id AND job.case_revision_id=result.case_revision_id AND job.attempt=result.attempt
                    WHERE result.run_id IN (SELECT value FROM json_each(?))
                    ORDER BY result.run_id,result.case_revision_id,result.attempt
                    """,
                    (json.dumps(sorted(run_sides)),),
                ) as cursor:
                    for row in await cursor.fetchall():
                        suite_id, side = run_sides[str(row["run_id"])]
                        # Exact k of n from immutable rows; infra attempts stay in the denominator.
                        bucket = counts.setdefault((suite_id, side, str(row["case_revision_id"])), [0, 0, 0])
                        bucket[0] += int(str(row["status"]) == "passed")
                        bucket[1] += 1
                        # Attempts that failed; the rest of the runs ended without a verdict.
                        bucket[2] += int(str(row["status"]) == "failed")
                        datarc_read = datarc_read or bool(isinstance(row["datarc_calls"], int) and row["datarc_calls"] > 0)
                        if row["failure_code"] in _TOOL_NOT_RUN_FAILURES:
                            not_run_tools.setdefault(str(row["case_revision_id"]), set()).update(_tool_names(row["tools_not_run"]))
                        detail = attempt_failure_detail(
                            status=str(row["status"]),
                            failure_code=row["failure_code"],
                            deterministic={"failure": _json_object(row["failure_json"])},
                            reference=str(row["job_id"] or f"{row['run_id']}:{row['case_revision_id']}:{row['attempt']}"),
                        )
                        if detail is not None:
                            attempt_failures.setdefault(str(row["case_revision_id"]), []).append(detail)
            paired_suites = _paired_suites(run_sides)
            totals = {"case_count": 0, "worse": 0, "unstable": 0, "better": 0, "not_comparable": 0, "unchanged_required_failures": 0}
            changed: list[dict[str, Any]] = []
            unfinished: list[dict[str, Any]] = []
            split: list[dict[str, Any]] = []
            for comparison in summary.get("case_comparisons", []):
                totals["case_count"] += 1
                change = classify_change(str(comparison["classification"]), comparison.get("reason_codes", ()))
                if change == "unchanged":
                    if comparison["classification"] == "unchanged_fail" and comparison.get("required"):
                        totals["unchanged_required_failures"] += 1
                        change = "required_failure"
                    else:
                        # A case its majority decided although its attempts disagreed: not a change,
                        # but the split is still shown, so it never silently reads as all passed.
                        suite_id = str(comparison["suite_revision_id"])
                        case_id = str(comparison["case_revision_id"])
                        sides = {
                            "baseline": _pass_count(counts, suite_id, "baseline", case_id) if suite_id in paired_suites else None,
                            "candidate": _pass_count(counts, suite_id, "candidate", case_id),
                        }
                        if len(split) < MAX_SPLIT_CASES and any(side is not None and side["passed"] and side["failed"] for side in sides.values()):
                            split.append(
                                {
                                    "suite_revision_id": suite_id,
                                    "case_revision_id": case_id,
                                    "classification": str(comparison["classification"]),
                                    **sides,
                                }
                            )
                        continue
                if change != "required_failure":
                    totals[change] += 1
                if change == "not_comparable":
                    # List every case left without a result with all its reasons, so the card can name
                    # each one: which case to split, which point to change, or what stopped its attempts
                    # (with the Tool and the diagnostic reference). A counted case is never left unlisted.
                    causes = sorted(set(comparison.get("reason_codes", ())))
                    if len(unfinished) < MAX_UNFINISHED_CASES:
                        case_id = str(comparison["case_revision_id"])
                        suite_id = str(comparison["suite_revision_id"])
                        unfinished.append(
                            {
                                "suite_revision_id": suite_id,
                                "case_revision_id": case_id,
                                "reason_codes": causes,
                                "tools": sorted(not_run_tools.get(case_id, ())),
                                "failures": _grouped_failures(attempt_failures.get(case_id, ())),
                                # Each side's attempts, so an unstable case without a result can say how they split.
                                "baseline": _pass_count(counts, suite_id, "baseline", case_id) if suite_id in paired_suites else None,
                                "candidate": _pass_count(counts, suite_id, "candidate", case_id),
                            }
                        )
                    continue
                suite_id = str(comparison["suite_revision_id"])
                case_id = str(comparison["case_revision_id"])
                changed.append(
                    {
                        "suite_revision_id": suite_id,
                        "case_revision_id": case_id,
                        "change": change,
                        "classification": str(comparison["classification"]),
                        "reason_codes": list(comparison.get("reason_codes", ())),
                        "key_case": comparison.get("tier") == "critical",
                        "baseline": _pass_count(counts, suite_id, "baseline", case_id) if suite_id in paired_suites else None,
                        "candidate": _pass_count(counts, suite_id, "candidate", case_id),
                    }
                )
            labels = await _case_labels_conn(db, [item["case_revision_id"] for item in [*changed, *unfinished, *split]])
            suite_names = await _suite_names_conn(db, {item["suite_revision_id"] for item in [*changed, *unfinished, *split]})
            for item in [*changed, *unfinished, *split]:
                key, label = labels.get(item["case_revision_id"], (None, None))
                item["case_key"] = key
                item["case_label"] = label
                item["suite_name"] = suite_names.get(item["suite_revision_id"])
            changed.sort(key=change_sort_key)
            page = changed[offset : offset + limit]
            baseline_id = execution["baseline_release_id"]
            return {
                "kind": "agent_version_case_changes",
                "schema_version": 1,
                "execution_id": execution_id,
                "snapshot": {"id": str(snapshot["id"]), "hash": str(snapshot["snapshot_hash"]), "created_at": str(snapshot["created_at"])},
                "baseline": (
                    {"release_id": str(baseline_id), "release_sequence": await _release_sequence_conn(db, "id", str(baseline_id))}
                    if baseline_id is not None
                    else None
                ),
                "candidate": {
                    "candidate_id": candidate_id,
                    "release_sequence": await _release_sequence_conn(db, "source_candidate_id", candidate_id),
                },
                "summary": totals,
                "items": [
                    {
                        key: item[key]
                        for key in (
                            "suite_revision_id",
                            "suite_name",
                            "case_revision_id",
                            "case_key",
                            "case_label",
                            "change",
                            "classification",
                            "reason_codes",
                            "key_case",
                            "baseline",
                            "candidate",
                        )
                    }
                    for item in page
                ],
                "next_offset": offset + limit if offset + limit < len(changed) else None,
                # Design §3.4: data cases were tested with the Owner's Datarc access; the card says so once.
                "datarc_owner_access": await _datarc_owner_access_conn(db, team_id=team_id, agent_id=agent_id)
                if datarc_read and _datarc_live()
                else None,
                "unfinished": [
                    {
                        key: item[key]
                        for key in (
                            "suite_revision_id",
                            "suite_name",
                            "case_revision_id",
                            "case_key",
                            "case_label",
                            "reason_codes",
                            "tools",
                            "failures",
                            "baseline",
                            "candidate",
                        )
                    }
                    for item in unfinished
                ],
                # Unchanged cases whose attempts disagreed on a side, each with both sides' attempts.
                "split": [
                    {
                        key: item[key]
                        for key in (
                            "suite_revision_id",
                            "suite_name",
                            "case_revision_id",
                            "case_key",
                            "case_label",
                            "classification",
                            "baseline",
                            "candidate",
                        )
                    }
                    for item in sorted(
                        split, key=lambda item: (str(item.get("case_label") or ""), str(item.get("case_key") or ""), item["case_revision_id"])
                    )
                ],
            }
        finally:
            await db.rollback()


def _datarc_live() -> bool:
    from bowhead_core.agent_eval_datarc import datarc_live_enabled

    return datarc_live_enabled()


async def _datarc_owner_access_conn(db: aiosqlite.Connection, *, team_id: str, agent_id: str) -> dict[str, str | None]:
    """Whose Datarc access the data cases were tested with: the Agent's current Owner, by display name."""

    async with db.execute(
        """
        SELECT owner.display_name FROM team_subagent agent
        JOIN users owner ON owner.id=COALESCE(agent.owner_user_id,agent.created_by)
        WHERE agent.team_id=? AND agent.id=?
        """,
        (team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    name = str(row[0] or "").strip() if row is not None else ""
    return {"owner_name": name or None}


def _run_sides(identity: object) -> dict[str, tuple[str, str]]:
    runs = identity.get("runs", []) if isinstance(identity, dict) else []
    return {str(run["run_id"]): (str(run["suite_revision_id"]), str(run["subject_side"])) for run in runs}


def _paired_suites(run_sides: Mapping[str, tuple[str, str]]) -> set[str]:
    """Suites that also ran on Live; their cases have a baseline result."""
    return {suite for suite, side in run_sides.values() if side == "baseline"}


def required_failure_summary(summary: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, int]:
    """Count failing required cases by the D3 label, from the same rows as the case-change table.

    A required case fails when it is a `required_failure` (failed on both sides, or on a candidate-only
    suite) or a `worse` change (passed on Live, fails now). Its label follows the table: no baseline
    -> `not_run_on_live`; a `required_failure` with a baseline -> `also_failing_on_live`; otherwise
    `introduced`. Counts only: no case identity or content leaves this function.
    """
    paired_suites = _paired_suites(_run_sides(identity))
    counts = {"required_failed": 0, "introduced": 0, "also_failing_on_live": 0, "not_run_on_live": 0}
    for comparison in summary.get("case_comparisons", []):
        if not comparison.get("required"):
            continue
        change = classify_change(str(comparison["classification"]), comparison.get("reason_codes", ()))
        if change == "unchanged" and comparison["classification"] == "unchanged_fail":
            change = "required_failure"
        if change not in {"required_failure", "worse"}:
            continue
        counts["required_failed"] += 1
        if str(comparison["suite_revision_id"]) not in paired_suites:
            counts["not_run_on_live"] += 1
        elif change == "required_failure":
            counts["also_failing_on_live"] += 1
        else:
            counts["introduced"] += 1
    return counts


_CANDIDATE_JUDGED = frozenset({"unchanged_pass", "unchanged_fail", "fixed", "regressed"})
_CANDIDATE_PASSING = frozenset({"unchanged_pass", "fixed"})
_LIVE_PASSING = frozenset({"unchanged_pass", "regressed"})
# Failing or unstable cases: the only ones whose unmet points explain a gate result.
_UNMET_POINT_CLASSES = frozenset({"regressed", "unchanged_fail", "inconclusive"})
MAX_UNMET_POINTS = 20
# Causes of a case without a result that the Owner acts on per case: split it, change its point, or
# fix what stopped its attempts (the evaluation model, the Agent's model, a Tool, a cancellation, or an
# unclassified error with its reference).
_UNFINISHED_REASONS = frozenset(
    {
        "attempt_time_limit_exceeded",
        "comparison_duration_budget_exceeded",
        "required_tool_not_run_in_eval",
        "tool_not_run_in_eval",
        *INCOMPLETE_ATTEMPT_REASONS,
    }
)
# Attempts the evaluation couldn't test because a Tool they called (or the case requires) can't run there.
_TOOL_NOT_RUN_FAILURES = frozenset({"agent_eval_required_tool_not_run", "agent_eval_tool_not_run_in_eval"})
MAX_UNFINISHED_CASES = 20
MAX_SPLIT_CASES = 20
# References kept per cause of one case; enough to find the attempts in the logs.
_MAX_FAILURE_REFERENCES = 3


def _grouped_failures(details: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per distinct cause of a case, with the references of its attempts."""

    grouped: OrderedDict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = OrderedDict()
    for detail in details:
        key = (str(detail["category"]), detail.get("cause"), detail.get("tool"), detail.get("role"))
        row = grouped.setdefault(
            key,
            {
                "category": key[0],
                "cause": key[1],
                "tool": key[2],
                "role": key[3],
                "reason_code": detail["reason_code"],
                "references": [],
                # A Datarc cause names the workspace or the Owner it is about.
                **{name: detail[name] for name in ("workspace", "owner_name") if detail.get(name)},
            },
        )
        if len(row["references"]) < _MAX_FAILURE_REFERENCES and detail["reference"] not in row["references"]:
            row["references"].append(str(detail["reference"]))
    return list(grouped.values())


def comparison_overview(summary: Mapping[str, Any]) -> dict[str, Any]:
    """PRD R15 batch overview from the snapshot's per-case classification; counts and rates only.

    A rate is None when no case on that side reached a verdict (inconclusive or incomparable cases
    have none), so an absent Live run never reads as a 0% pass rate.
    """
    comparisons = [item for item in summary.get("case_comparisons", []) if isinstance(item, Mapping)]
    candidate_judged = [item for item in comparisons if item.get("classification") in _CANDIDATE_JUDGED]
    live_judged = [item for item in candidate_judged if item.get("baseline_pass_rate") is not None]

    def rate(passing: int, judged: int) -> float | None:
        return round(passing / judged, 4) if judged else None

    return {
        "cases": len(comparisons),
        "candidate_pass_rate": rate(sum(item["classification"] in _CANDIDATE_PASSING for item in candidate_judged), len(candidate_judged)),
        "live_pass_rate": rate(sum(item["classification"] in _LIVE_PASSING for item in live_judged), len(live_judged)),
        "newly_failing": sum(item.get("classification") == "regressed" for item in comparisons),
        "newly_passing": sum(item.get("classification") == "fixed" for item in comparisons),
    }


async def _unmet_points_conn(db: aiosqlite.Connection, summary: Mapping[str, Any]) -> list[dict[str, str]]:
    """PRD R20: which points a failing case missed, by case key and point id; no text or quotes."""
    pairs = [
        (str(item["case_revision_id"]), str(point))
        for item in summary.get("case_comparisons", [])
        if isinstance(item, Mapping) and item.get("classification") in _UNMET_POINT_CLASSES
        for point in item.get("unmet_point_ids") or ()
        if isinstance(point, str)
    ]
    if not pairs:
        return []
    labels = await _case_labels_conn(db, [case_id for case_id, _ in pairs])
    rows = sorted({(labels[case_id][0], point) for case_id, point in pairs if case_id in labels})
    return [{"case_key": case_key, "point_id": point} for case_key, point in rows[:MAX_UNMET_POINTS]]


async def read_snapshot_failure_summary_conn(
    db: aiosqlite.Connection, *, team_id: str, agent_id: str, candidate_id: str, snapshot_id: str
) -> dict[str, Any] | None:
    """`required_failure_summary` plus unmet points for one candidate's snapshot; None when it does not exist."""
    async with db.execute(
        """SELECT identity_json,summary_json FROM agent_eval_snapshot
        WHERE id=? AND team_id=? AND agent_id=? AND candidate_id=?""",
        (snapshot_id, team_id, agent_id, candidate_id),
    ) as cursor:
        snapshot = await cursor.fetchone()
    if snapshot is None:
        return None
    summary = json.loads(str(snapshot["summary_json"]))
    return {
        **required_failure_summary(summary, json.loads(str(snapshot["identity_json"]))),
        "unmet_points": await _unmet_points_conn(db, summary),
    }


def _pass_count(counts: Mapping[tuple[str, str, str], list[int]], suite_id: str, side: str, case_id: str) -> dict[str, int]:
    passed, runs, failed = counts.get((suite_id, side, case_id), [0, 0, 0])
    return {"passed": passed, "failed": failed, "runs": runs}


async def _case_labels_conn(db: aiosqlite.Connection, case_ids: Sequence[str]) -> dict[str, tuple[str, str | None]]:
    if not case_ids:
        return {}
    async with db.execute(
        "SELECT id,case_key,input_json FROM agent_eval_case_revision WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(set(case_ids))),),
    ) as cursor:
        return {str(row["id"]): (str(row["case_key"]), case_label(json.loads(str(row["input_json"])))) for row in await cursor.fetchall()}


async def _suite_names_conn(db: aiosqlite.Connection, suite_revision_ids: Iterable[str]) -> dict[str, str]:
    ids = sorted(set(suite_revision_ids))
    if not ids:
        return {}
    async with db.execute(
        """
        SELECT revision.id,suite.name FROM agent_eval_suite_revision revision
        JOIN agent_eval_suite suite ON suite.id=revision.suite_id
        WHERE revision.id IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(ids),),
    ) as cursor:
        return {str(row["id"]): str(row["name"]) for row in await cursor.fetchall()}


_RELEASE_SEQUENCE_QUERIES = {
    "id": "SELECT release_sequence FROM team_subagent_release WHERE id=?",
    # A candidate has a release sequence only once it has been activated.
    "source_candidate_id": "SELECT release_sequence FROM team_subagent_release WHERE source_candidate_id=? ORDER BY release_sequence DESC LIMIT 1",
}


async def _release_sequence_conn(db: aiosqlite.Connection, column: Literal["id", "source_candidate_id"], value: str) -> int | None:
    async with db.execute(_RELEASE_SEQUENCE_QUERIES[column], (value,)) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else None


async def read_run_case_answers(
    db_path: str,
    *,
    team_id: str,
    agent_id: str,
    run_id: str,
    case_revision_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Read a frozen standalone case and its answers within the canonical Owner boundary."""
    from bowhead_core.agent_eval_quality import answer_quality

    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            await _require_owner_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
            async with db.execute(
                """SELECT c.*,s.suite_id,r.revision_ref FROM agent_eval_run r
                   JOIN agent_eval_suite_revision s ON s.id=r.suite_revision_id
                   JOIN agent_eval_case_revision c ON c.suite_revision_id=s.id
                   WHERE r.id=? AND r.team_id=? AND r.agent_id=? AND r.agent_source_kind='team' AND c.id=?""",
                (run_id, team_id, agent_id, case_revision_id),
            ) as cursor:
                case = await cursor.fetchone()
            if case is None:
                raise AgentEvalInsightsError("agent_eval_case_not_found")
            async with db.execute(
                """SELECT result.attempt,result.status,result.failure_code,result.duration_ms,
                          result.judge_json,result.deterministic_json,turn.final_answer,job.id AS job_id
                   FROM agent_eval_case_result result
                   LEFT JOIN agent_eval_case_job job ON job.run_id=result.run_id
                     AND job.case_revision_id=result.case_revision_id AND job.attempt=result.attempt
                   LEFT JOIN agent_eval_scratch_turn scratch
                     ON result.trace_evidence_ref LIKE 'turn:%' AND scratch.turn_id=substr(result.trace_evidence_ref,6)
                    AND scratch.run_id=result.run_id AND scratch.case_revision_id=result.case_revision_id
                    AND scratch.attempt=result.attempt
                   LEFT JOIN turns turn ON turn.id=scratch.turn_id
                   WHERE result.run_id=? AND result.case_revision_id=? ORDER BY result.attempt""",
                (run_id, case_revision_id),
            ) as cursor:
                rows = await cursor.fetchall()
            expected = _json_object(case["expected_contract_json"])
            attempts = []
            for row in rows:
                quality = answer_quality(expected, row["judge_json"])
                points = _attempt_points(row["judge_json"])
                recorded = {point["id"] for point in points}
                points.extend(
                    {"id": point["id"], "met": None, "quote": None, "applicable": True, "blocking": False, "reason": point["reason"]}
                    for point in quality["points"]
                    if point["id"] not in recorded
                )
                attempts.append(
                    {
                        "attempt": row["attempt"],
                        "status": row["status"],
                        "failure_code": row["failure_code"],
                        "duration_ms": row["duration_ms"],
                        "run_id": run_id,
                        **_reader_case_answer(row["final_answer"]),
                        "points": [*points, *_action_point_rows(expected, row["deterministic_json"], failure_code=row["failure_code"])],
                        "quality": quality,
                        "failed_checks": _failed_checks(expected, row["deterministic_json"]),
                        "unsourced_figures": _unsourced_figures(row["deterministic_json"]),
                        "failure": _attempt_failure(row, run_id=run_id, case_revision_id=case_revision_id),
                    }
                )
            return {
                "kind": "agent_eval_run_case_answers",
                "schema_version": 1,
                "run_id": run_id,
                "revision_id": case["revision_ref"],
                "suite_revision_id": case["suite_revision_id"],
                "case_revision_id": case_revision_id,
                "case_key": case["case_key"],
                "task": _json_object(case["input_json"]).get("task"),
                "points": await _case_point_definitions(db, suite_id=case["suite_id"], expected_json=case["expected_contract_json"]),
                "attempts": attempts,
            }
        finally:
            await db.rollback()


async def read_case_answers(
    db_path: str,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    execution_id: str,
    case_revision_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Both versions' answers for one case; scorers, fixtures and rule expectations stay private.

    The case's expected points are the Owner's own words, so they are listed with each attempt's
    verdict and quote (PRD R26), and the point's calibration failure with Coach's rewording.
    """
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            await _owned_execution_conn(
                db, team_id=team_id, agent_id=agent_id, candidate_id=candidate_id, execution_id=execution_id, actor_user_id=actor_user_id
            )
            async with db.execute(
                """
                SELECT case_revision.case_key,case_revision.input_json,case_revision.expected_contract_json,
                       link.suite_revision_id,link.subject_side,link.run_id,suite_revision.suite_id
                FROM agent_eval_case_revision case_revision
                JOIN agent_eval_suite_revision suite_revision ON suite_revision.id=case_revision.suite_revision_id
                JOIN agent_eval_comparison_run link
                  ON link.suite_revision_id=case_revision.suite_revision_id AND link.execution_id=?
                WHERE case_revision.id=?
                ORDER BY link.subject_side
                """,
                (execution_id, case_revision_id),
            ) as cursor:
                links = await cursor.fetchall()
            if not links:
                raise AgentEvalInsightsError("agent_eval_case_not_found")
            sides: dict[str, dict[str, Any] | None] = {"baseline": None, "candidate": None}
            for link in links:
                async with db.execute(
                    """
                    SELECT result.attempt,result.status,result.failure_code,result.duration_ms,turn.final_answer,result.judge_json,
                           result.deterministic_json,job.id AS job_id
                    FROM agent_eval_case_result result
                    LEFT JOIN agent_eval_case_job job
                      ON job.run_id=result.run_id AND job.case_revision_id=result.case_revision_id AND job.attempt=result.attempt
                    LEFT JOIN agent_eval_scratch_turn scratch
                      ON result.trace_evidence_ref LIKE 'turn:%' AND scratch.turn_id=substr(result.trace_evidence_ref,6)
                     AND scratch.run_id=result.run_id AND scratch.case_revision_id=result.case_revision_id
                     AND scratch.attempt=result.attempt
                    LEFT JOIN turns turn ON turn.id=scratch.turn_id
                    WHERE result.run_id=? AND result.case_revision_id=?
                    ORDER BY result.attempt
                    """,
                    (link["run_id"], case_revision_id),
                ) as cursor:
                    attempts = await cursor.fetchall()
                expected = _json_object(link["expected_contract_json"])
                sides[str(link["subject_side"])] = {
                    "attempts": [
                        {
                            "attempt": int(row["attempt"]),
                            "status": str(row["status"]),
                            "failure_code": row["failure_code"],
                            "duration_ms": int(row["duration_ms"]) if row["duration_ms"] is not None else None,
                            **_reader_case_answer(row["final_answer"]),
                            # The Tools the evaluation couldn't run, when that is why the attempt has no verdict.
                            "tools_not_run": _attempt_tools_not_run(row["failure_code"], row["deterministic_json"]),
                            "run_id": str(link["run_id"]),
                            # Required actions are checked by rules, not the judge; listed beside the graded
                            # points so an unmet one is shown with why, not left out of "N points met".
                            "points": [
                                *_attempt_points(row["judge_json"]),
                                *_action_point_rows(expected, row["deterministic_json"], failure_code=row["failure_code"]),
                            ],
                            "failed_checks": _failed_checks(expected, row["deterministic_json"]),
                            "unsourced_figures": _unsourced_figures(row["deterministic_json"]),
                            "time_limit": _time_limit(row["deterministic_json"]),
                            # Owner-only: the attempt's live Datarc call and row counts (never rows).
                            "datarc": _attempt_datarc_counts(row["deterministic_json"]),
                            # An attempt without a verdict says why: category, cause, Tool and reference.
                            "failure": _attempt_failure(row, run_id=str(link["run_id"]), case_revision_id=case_revision_id),
                        }
                        for row in attempts
                    ]
                }
            first = links[0]
            input_value = json.loads(str(first["input_json"]))
            task = input_value.get("task") if isinstance(input_value, dict) else None
            points = await _case_point_definitions(db, suite_id=str(first["suite_id"]), expected_json=first["expected_contract_json"])
            return {
                "kind": "agent_version_case_answers",
                "schema_version": 1,
                "execution_id": execution_id,
                "suite_revision_id": str(first["suite_revision_id"]),
                "case_revision_id": case_revision_id,
                "case_key": str(first["case_key"]),
                "case_label": case_label(input_value),
                "task": task if isinstance(task, str) else None,
                "points": points,
                "baseline": sides["baseline"],
                "candidate": sides["candidate"],
            }
        finally:
            await db.rollback()


async def read_unmet_points(
    db_path: str,
    *,
    team_id: str,
    agent_id: str,
    candidate_id: str,
    execution_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Points the candidate failed in one comparison, each with its quote, for the risk acknowledgement (R28).

    Only blocking points are listed: they are what `point_regression` and `point_unmet` stand for.
    """

    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            await _owned_execution_conn(
                db, team_id=team_id, agent_id=agent_id, candidate_id=candidate_id, execution_id=execution_id, actor_user_id=actor_user_id
            )
            async with db.execute(
                """
                SELECT result.run_id,result.case_revision_id,result.attempt,result.judge_json,
                       case_revision.case_key,case_revision.input_json,case_revision.expected_contract_json
                FROM agent_eval_comparison_run link
                JOIN agent_eval_case_result result ON result.run_id=link.run_id
                JOIN agent_eval_case_revision case_revision ON case_revision.id=result.case_revision_id
                WHERE link.execution_id=? AND link.subject_side='candidate' AND result.judge_json IS NOT NULL
                ORDER BY case_revision.case_key,result.case_revision_id,result.attempt
                """,
                (execution_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            # Attempts with no verdict have no points to list; they are reported with their cause
            # instead of being dropped, so an empty point list never hides why.
            async with db.execute(
                """
                SELECT result.run_id,result.case_revision_id,result.attempt,result.status,result.failure_code,
                       result.deterministic_json,job.id AS job_id,link.subject_side,
                       case_revision.case_key,case_revision.input_json
                FROM agent_eval_comparison_run link
                JOIN agent_eval_case_result result ON result.run_id=link.run_id
                JOIN agent_eval_case_revision case_revision ON case_revision.id=result.case_revision_id
                LEFT JOIN agent_eval_case_job job
                  ON job.run_id=result.run_id AND job.case_revision_id=result.case_revision_id AND job.attempt=result.attempt
                WHERE link.execution_id=? AND result.status IN ('infra_failed','judge_unknown','cancelled')
                ORDER BY case_revision.case_key,result.case_revision_id,link.subject_side,result.attempt
                """,
                (execution_id,),
            ) as cursor:
                unfinished_rows = await cursor.fetchall()
        finally:
            await db.rollback()
    from bowhead_core.agent_eval_points import case_points

    items: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        expected = json.loads(str(row["expected_contract_json"]))
        texts = {str(point["id"]): point for point in case_points(expected if isinstance(expected, dict) else None)}
        for point in _attempt_points(row["judge_json"]):
            if not point["blocking"] or not point["applicable"] or point["met"] is not False:
                continue
            key = (str(row["case_revision_id"]), point["id"])
            if key in items:
                items[key]["attempts"] += 1
                continue
            input_value = json.loads(str(row["input_json"]))
            definition = texts.get(point["id"], {})
            items[key] = {
                "case_revision_id": key[0],
                "case_key": str(row["case_key"]),
                "case_label": case_label(input_value),
                "point_id": point["id"],
                "kind": str(definition.get("kind") or ""),
                "text": str(definition.get("text") or ""),
                "run_id": str(row["run_id"]),
                "attempt": int(row["attempt"]),
                "quote": point["quote"],
                "attempts": 1,
            }
    unfinished = []
    for row in unfinished_rows[:_MAX_UNFINISHED_ATTEMPTS]:
        failure = _attempt_failure(row, run_id=str(row["run_id"]), case_revision_id=str(row["case_revision_id"]))
        if failure is None:
            # Time limits and required Tools that can't run: still listed, with their own category.
            failure = {"category": "time_limit" if _time_limit(row["deterministic_json"]) else "tool_not_run", "cause": None, "tool": None}
        unfinished.append(
            {
                "case_revision_id": str(row["case_revision_id"]),
                "case_key": str(row["case_key"]),
                "case_label": case_label(_json_object(row["input_json"])),
                "side": str(row["subject_side"]),
                "attempt": int(row["attempt"]),
                "status": str(row["status"]),
                **failure,
            }
        )
    return {
        "kind": "agent_version_unmet_points",
        "schema_version": 1,
        "execution_id": execution_id,
        "items": list(items.values()),
        "unfinished_attempts": unfinished,
    }


_MAX_UNFINISHED_ATTEMPTS = 50


def _attempt_failure(row: Mapping[str, Any], *, run_id: str, case_revision_id: str) -> dict[str, Any] | None:
    """Category, cause, Tool and diagnostic reference of an attempt without a verdict (None otherwise)."""

    detail = attempt_failure_detail(
        status=str(row["status"]),
        failure_code=row["failure_code"],
        deterministic=_json_object(row["deterministic_json"]),
        reference=str(row["job_id"] or f"{run_id}:{case_revision_id}:{row['attempt']}"),
    )
    if detail is None:
        return None
    document = {key: detail.get(key) for key in ("category", "cause", "tool", "role", "reference", "reason_code")}
    # A Datarc cause names the workspace or the Owner it is about (agent_eval_datarc.failure_document).
    document.update({key: detail[key] for key in ("workspace", "owner_name") if detail.get(key)})
    return document


_UNSOURCED_FIGURES_MAX = 20


def _attempt_datarc_counts(deterministic_json: object) -> dict[str, int] | None:
    """Datarc calls, row queries and rows read by one attempt (FEATURE_AGENT_EVAL_DATARC_READ_ONLY); None without."""

    from bowhead_core.agent_eval_datarc import datarc_live_enabled

    if not datarc_live_enabled():
        # With the switch off, recorded evidence stays stored and is ignored (rollback).
        return None
    try:
        deterministic = json.loads(str(deterministic_json)) if deterministic_json is not None else None
    except (TypeError, ValueError):
        return None
    if not isinstance(deterministic, dict):
        return None
    calls = deterministic.get("datarc_calls")
    entries = deterministic.get("datarc")
    if isinstance(calls, bool) or not isinstance(calls, int) or not isinstance(entries, list):
        return None
    queries = [entry for entry in entries if isinstance(entry, dict) and entry.get("op") == "query" and entry.get("outcome") in {"ok", "empty"}]
    rows = sum(
        int(entry["row_count"]) for entry in queries if isinstance(entry.get("row_count"), int) and not isinstance(entry.get("row_count"), bool)
    )
    shown = [entry["rows_shown"] for entry in queries if isinstance(entry.get("rows_shown"), int) and not isinstance(entry.get("rows_shown"), bool)]
    counts = {"calls": calls, "queries": len(queries), "rows": rows}
    if shown:
        counts["rows_shown"] = sum(shown)
    return counts


def _time_limit(deterministic_json: object) -> dict[str, Any] | None:
    """The limit an attempt hit and how long it ran, when a time limit ended it; else None."""

    try:
        deterministic = json.loads(str(deterministic_json)) if deterministic_json is not None else None
    except (TypeError, ValueError):
        return None
    value = deterministic.get("time_limit") if isinstance(deterministic, dict) else None
    if not isinstance(value, dict) or value.get("scope") not in {"attempt", "run"}:
        return None
    limit_ms, observed_ms = value.get("limit_ms"), value.get("observed_ms")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in (limit_ms, observed_ms)):
        return None
    return {"limit_ms": limit_ms, "observed_ms": observed_ms, "scope": value["scope"]}


def _unsourced_figures(deterministic_json: object) -> list[str]:
    """Figures in the answer with no direct source: an advisory hint that never decided the attempt.

    Derived figures (sums, shares, rounding) land here too, so the list is shown as a hint only.
    """

    try:
        deterministic = json.loads(str(deterministic_json)) if deterministic_json is not None else None
    except (TypeError, ValueError):
        return []
    rows = deterministic.get("scorers") if isinstance(deterministic, dict) else None
    row = next((item for item in rows or [] if isinstance(item, dict) and item.get("name") == "no_fabrication"), None)
    if row is None or row.get("applicable") is False or row.get("passed") is not False:
        return []
    data = row.get("data")
    violations = data.get("violations") if isinstance(data, dict) else None
    figures: list[str] = []
    for value in violations if isinstance(violations, list) else []:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        text = str(int(value)) if float(value).is_integer() else str(value)
        if text not in figures:
            figures.append(text)
    return figures[:_UNSOURCED_FIGURES_MAX]


def _attempt_points(judge_json: object) -> list[dict[str, Any]]:
    """Per-point verdicts of one attempt; a run without a pinned judge stored none (not graded)."""

    try:
        judge = json.loads(str(judge_json)) if judge_json is not None else None
    except (TypeError, ValueError):
        return []
    rows = judge.get("points") if isinstance(judge, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        {
            "id": str(row["id"]),
            "met": row.get("met") if isinstance(row.get("met"), bool) else None,
            "quote": row.get("quote") if isinstance(row.get("quote"), str) else None,
            "applicable": row.get("applicable", True) is not False,
            "blocking": row.get("blocking") is True,
            "reason": row.get("reason") if isinstance(row.get("reason"), str) else None,
        }
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]


async def _case_point_definitions(db: aiosqlite.Connection, *, suite_id: str, expected_json: object) -> list[dict[str, Any]]:
    from bowhead_core.agent_eval_point_calibration import point_text_hash
    from bowhead_core.agent_eval_points import case_points

    try:
        expected = json.loads(str(expected_json))
    except (TypeError, ValueError):
        return []
    definitions: list[dict[str, Any]] = []
    for point in case_points(expected if isinstance(expected, dict) else None):
        async with db.execute(
            "SELECT text_hash,state,failure_reason,rewording FROM agent_eval_point_calibration WHERE suite_id=? AND point_id=?",
            (suite_id, point["id"]),
        ) as cursor:
            calibration = await cursor.fetchone()
        text = str(point.get("text") or "")
        failed = (
            calibration is not None
            and calibration["state"] == "failed"
            and calibration["text_hash"] == point_text_hash(text)
            and calibration["failure_reason"] not in {"judge_unavailable", "coach_unavailable"}
        )
        definitions.append(
            {
                "id": str(point["id"]),
                "kind": str(point.get("kind")),
                "text": text,
                "state": str(point.get("state")),
                "calibration_failure": ({"reason": str(calibration["failure_reason"]), "rewording": calibration["rewording"]} if failed else None),
                **({"tools": sorted(_action_tools(point))} if point.get("kind") == "required_action" else {}),
            }
        )
    return definitions


def _json_object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _tool_names(value: object) -> set[str]:
    """Tool names from a stored "a|b" group list (the any-of form of a required action)."""

    try:
        decoded = json.loads(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return set()
    return {name for group in decoded if isinstance(group, str) for name in group.split("|") if name} if isinstance(decoded, list) else set()


def _action_tools(point: Mapping[str, Any]) -> frozenset[str]:
    action = point.get("action")
    if not isinstance(action, Mapping):
        return frozenset()
    tool, alternatives = action.get("tool"), action.get("any_of")
    if isinstance(tool, str) and tool:
        return frozenset([tool])
    return frozenset(name for name in alternatives if isinstance(name, str) and name) if isinstance(alternatives, list) else frozenset()


def _scorer_row(deterministic: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    rows = deterministic.get("scorers")
    return next((row for row in rows if isinstance(row, Mapping) and row.get("name") == name), None) if isinstance(rows, list) else None


def _action_point_rows(expected: Mapping[str, Any], deterministic_json: object, *, failure_code: object) -> list[dict[str, Any]]:
    """Each confirmed required action's verdict on one attempt, from the tool contract scorer.

    `tool_not_called`: the Agent never called any of its Tools. `tool_not_run_in_eval`: it called one
    the evaluation can't run, so there is no verdict. An attempt that stopped before scoring has none.
    """

    from bowhead_core.agent_eval_points import case_points

    deterministic = _json_object(deterministic_json)
    not_run = {group for group in deterministic.get("required_tools_not_run") or () if isinstance(group, str)}
    # An attempt whose answer depended on a Tool the evaluation can't run names single Tools.
    denied = {name for name in deterministic.get("tools_not_run") or () if isinstance(name, str)}
    contract = _scorer_row(deterministic, "tool_contract")
    data = contract.get("data") if contract is not None else None
    missing = {group for group in (data.get("missing") or ()) if isinstance(group, str)} if isinstance(data, Mapping) else None
    rows: list[dict[str, Any]] = []
    for point in case_points(expected):
        if point.get("kind") != "required_action" or point.get("state") != "confirmed":
            continue
        group = "|".join(sorted(_action_tools(point)))
        if not group:
            continue
        row = {"id": str(point["id"]), "quote": None, "applicable": True, "blocking": True}
        if failure_code in _TOOL_NOT_RUN_FAILURES and (group in not_run or set(group.split("|")) & denied):
            rows.append({**row, "met": None, "reason": "tool_not_run_in_eval"})
        elif missing is not None:
            rows.append({**row, "met": group not in missing, "reason": "tool_not_called" if group in missing else None})
    return rows


# The rule checks an attempt can fail on, in the order the answers view names them.
_RULE_CHECKS = ("terminal_contract", "output_schema", "tool_contract", "permission_denial", "effect_policy", "citation", "budget_timeout")


def _failed_checks(expected: Mapping[str, Any], deterministic_json: object) -> list[str]:
    """Rule checks that failed the attempt, by name, besides required actions (listed as points).

    The tool contract is named only when a Tool it requires is not a confirmed required action's.
    """

    deterministic = _json_object(deterministic_json)
    failed: list[str] = []
    for name in _RULE_CHECKS:
        row = _scorer_row(deterministic, name)
        if row is None or row.get("passed") is not False or row.get("applicable") is False or row.get("advisory"):
            continue
        if name == "tool_contract":
            from bowhead_core.agent_eval_points import case_points

            data = row.get("data")
            missing = {group for group in (data.get("missing") or ()) if isinstance(group, str)} if isinstance(data, Mapping) else set()
            forbidden = data.get("forbidden_called") if isinstance(data, Mapping) else None
            actions = {"|".join(sorted(_action_tools(point))) for point in case_points(expected) if point.get("kind") == "required_action"}
            if not (missing - actions) and not forbidden:
                continue
        failed.append(name)
    return failed


def _attempt_tools_not_run(failure_code: object, deterministic_json: object) -> list[str]:
    if failure_code not in _TOOL_NOT_RUN_FAILURES:
        return []
    deterministic = _json_object(deterministic_json)
    names = _tool_names(json.dumps(deterministic.get("required_tools_not_run") or deterministic.get("tools_not_run") or []))
    return sorted(names)


def _reader_case_answer(value: object) -> dict[str, Any]:
    """An attempt's answer for the Owner's answers view.

    An answer that is the fixed ``insufficient_data:`` stop is an instruction written for a model (the
    failure breaker's reason, such as ``peer_total_failures=8``), not prose for the Owner: it is withheld
    and the view says in plain words that the Agent stopped. The stored turn keeps it for diagnostics.
    """

    from bowhead_core.turn_failure_text import INSUFFICIENT_DATA_PREFIX

    bounded = _bounded_answer(value)
    answer = bounded["answer"]
    if isinstance(answer, str) and answer.lstrip().startswith(INSUFFICIENT_DATA_PREFIX.strip()):
        return {"answer": None, "answer_truncated": False, "answer_withheld": "stopped_without_answer"}
    return {**bounded, "answer_withheld": None}


def _bounded_answer(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"answer": None, "answer_truncated": False}
    # An Agent's structured answer is stored as JSON; a reader sees its Chat text, as in Chat.
    from bowhead_core.agent_eval_alignment import reader_answer

    value = reader_answer(value)[0]
    encoded = value.encode("utf-8")
    if len(encoded) <= _ANSWER_MAX_BYTES:
        return {"answer": value, "answer_truncated": False}
    return {"answer": encoded[:_ANSWER_MAX_BYTES].decode("utf-8", errors="ignore"), "answer_truncated": True}


# ── helpers ─────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _elapsed_ms(started: object, finished: object) -> float | None:
    start = _parse(started)
    end = _parse(finished)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() * 1000)
