"""Pure version-comparison classification and release-readiness projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Literal, cast

from bowhead_core.agent_eval_attempt_failure import incomplete_attempt_reason

AttemptStatus = Literal["passed", "failed", "judge_unknown", "infra_failed", "cancelled"]
CaseClassification = Literal[
    "unchanged_pass",
    "unchanged_fail",
    "fixed",
    "regressed",
    "inconclusive",
    "incomparable",
]
SnapshotReadiness = Literal["ready", "ready_with_risks", "blocked", "unknown"]
# How a tier turns one side's attempts into a verdict. `threshold`: the pass rate against the tier's
# pass threshold, and any split over the tier's flaky limit leaves the case without a verdict.
# `majority`: more than half of the planned attempts passing passes, more than half failing fails, and
# only a side with no majority (an even split, or too few attempts with a verdict) is unstable.
VerdictRule = Literal["threshold", "majority"]
# Attempts cut by a time limit: no verdict, but the cause is the limit, not the environment.
ATTEMPT_TIME_LIMIT_FAILURES = frozenset({"agent_eval_attempt_timeout"})
RUN_TIME_LIMIT_FAILURES = frozenset({"agent_eval_duration_budget_exceeded"})
# The case requires a Tool the evaluation has no way to run, so the attempt has no verdict.
REQUIRED_TOOL_NOT_RUN_FAILURES = frozenset({"agent_eval_required_tool_not_run"})
# The answer depended on a Tool the evaluation can't run yet (a Datarc query, say): the case can't be
# tested in evaluation, so the attempt has no verdict and never counts as better, same or worse.
TOOL_NOT_RUN_FAILURES = frozenset({"agent_eval_tool_not_run_in_eval"})
# Only these carry a quality verdict; the rest say nothing about how often the case passes.
_DECIDED_STATUSES = frozenset({"passed", "failed"})


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    status: AttemptStatus
    subject_provider_ref: str | None
    subject_provider_hash: str | None
    judge_provider_ref: str | None
    judge_provider_hash: str | None
    reproducibility: Literal["exact", "attributed_only", "unknown"]
    duration_ms: int | None = None
    cost_microusd: int | None = None
    # The three verdicts behind a `failed` attempt, kept apart (technical design §5.3, E11): rules,
    # a v1 rubric (None when the run has none) and expected points. Only rules passing, the rubric
    # passing or absent, and points failing make the attempt fail on points alone.
    rule_passed: bool | None = None
    rubric_passed: bool | None = None
    points_passed: bool | None = None
    unmet_point_ids: tuple[str, ...] = ()
    failure_code: str | None = None
    # FEATURE_AGENT_EVAL_DATARC_READ_ONLY: (semantics, result) keyed digests of the attempt's live Datarc row
    # queries; empty with the switch off. Only the §6.3 "data changed during the test" rule reads them.
    datarc_digests: tuple[tuple[str, str], ...] = ()
    # The stored cause of an attempt without a verdict (deterministic.failure.cause); a Datarc environment
    # failure's release reason names it.
    failure_cause: str | None = None


@dataclass(frozen=True, slots=True)
class ComparisonCase:
    suite_revision_id: str
    case_revision_id: str
    tier: Literal["critical", "standard", "diagnostic"]
    required: bool
    attempts: int
    execution_scope: Literal["paired", "candidate_only"]
    baseline: tuple[AttemptEvidence, ...]
    candidate: tuple[AttemptEvidence, ...]
    allow_subject_provider_delta: bool = False


@dataclass(frozen=True, slots=True)
class CaseComparison:
    suite_revision_id: str
    case_revision_id: str
    tier: str
    required: bool
    classification: CaseClassification
    reason_codes: tuple[str, ...]
    baseline_pass_rate: float | None
    candidate_pass_rate: float | None
    baseline_samples: int
    candidate_samples: int
    cost_microusd: int
    duration_ms: int
    # True when the case fails only because model-graded points were unmet; rules alone pass.
    point_only: bool = False
    unmet_point_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    state: SnapshotReadiness
    reason_codes: tuple[str, ...]
    case_comparisons: tuple[CaseComparison, ...]
    coverage_gaps: tuple[str, ...]
    aggregate_metrics: dict[str, int]
    reviewer_projection: dict[str, object]
    identity_document: dict[str, object]

    @property
    def snapshot_hash(self) -> str:
        return _hash_document(
            {
                "aggregate_metrics": self.aggregate_metrics,
                "case_comparisons": [asdict(item) for item in self.case_comparisons],
                "coverage_gaps": list(self.coverage_gaps),
                "identity_document": self.identity_document,
                "readiness": self.state,
                "reason_codes": list(self.reason_codes),
                "reviewer_projection": self.reviewer_projection,
                "schema_version": 1,
            },
        )


def compare_case(
    case: ComparisonCase,
    *,
    pass_threshold: float,
    minimum_delta: float,
    flaky_limit: float,
    verdict_rule: VerdictRule,
) -> CaseComparison:
    """Classify one exact case without reading mutable state."""

    reasons: set[str] = set()
    if len(case.candidate) != case.attempts or (case.execution_scope == "paired" and len(case.baseline) != case.attempts):
        reasons.add("required_attempts_missing")
    all_attempts = (*case.baseline, *case.candidate)
    # An attempt a time limit cut is that one attempt without a verdict: the other attempts still
    # decide the case. Only a side left with no attempt that finished has no result.
    baseline = tuple(item for item in case.baseline if not _time_limited(item))
    candidate = tuple(item for item in case.candidate if not _time_limited(item))
    sides = (case.baseline, case.candidate) if case.execution_scope == "paired" else (case.candidate,)
    for side in sides:
        if side and all(_time_limited(item) for item in side):
            reasons.update(
                "comparison_duration_budget_exceeded" if item.failure_code in RUN_TIME_LIMIT_FAILURES else "attempt_time_limit_exceeded"
                for item in side
            )
    finished = (*baseline, *candidate)
    for item in finished:
        if item.status == "infra_failed" and item.failure_code in REQUIRED_TOOL_NOT_RUN_FAILURES:
            reasons.add("required_tool_not_run_in_eval")
        elif item.status == "infra_failed" and item.failure_code in TOOL_NOT_RUN_FAILURES:
            reasons.add("tool_not_run_in_eval")
        else:
            # Keep the cause (evaluation model, the Agent's model, a Tool, a cancellation or an
            # unclassified platform error): each has a different fix, so one generic code can't say it.
            incomplete = incomplete_attempt_reason(item.status, item.failure_code, item.failure_cause)
            if incomplete is not None:
                reasons.add(incomplete)
    if any(item.reproducibility == "unknown" for item in finished):
        reasons.add("provider_identity_unknown")
    if case.execution_scope == "paired" and _data_changed(case):
        # The same row query read different data on the two sides: the data changed during the test,
        # so the case says nothing about the change. Not comparable; testing again can read stable data.
        reasons.add("data_changed_during_test")
    if case.execution_scope == "paired" and _identity_mismatch(
        baseline=baseline,
        candidate=candidate,
        allow_subject_provider_delta=case.allow_subject_provider_delta,
    ):
        reasons.add("provider_identity_incomparable")

    paired = case.execution_scope == "paired"
    baseline_rate = _pass_rate(baseline) if paired else None
    candidate_rate = _pass_rate(candidate)
    if verdict_rule == "majority":
        if _short_of_majority(case.candidate, attempts=case.attempts) or (paired and _short_of_majority(case.baseline, attempts=case.attempts)):
            reasons.add("flaky_limit_exceeded")
    elif _flaky_rate(candidate) > flaky_limit or (paired and _flaky_rate(baseline) > flaky_limit):
        reasons.add("flaky_limit_exceeded")

    if "provider_identity_incomparable" in reasons or "data_changed_during_test" in reasons:
        classification: CaseClassification = "incomparable"
    elif reasons:
        classification = "inconclusive"
    elif verdict_rule == "majority":
        classification = _majority_classification(case, reasons)
    elif candidate_rate is None:
        classification = "inconclusive"
        reasons.add("candidate_result_missing")
    elif case.execution_scope == "candidate_only":
        classification = "unchanged_pass" if candidate_rate >= pass_threshold else "unchanged_fail"
    elif baseline_rate is None:
        classification = "inconclusive"
        reasons.add("baseline_result_missing")
    elif baseline_rate >= pass_threshold and candidate_rate >= pass_threshold:
        classification = "unchanged_pass"
    elif baseline_rate < pass_threshold and candidate_rate < pass_threshold:
        classification = "unchanged_fail"
    elif candidate_rate - baseline_rate >= minimum_delta:
        classification = "fixed"
    elif baseline_rate - candidate_rate >= minimum_delta:
        classification = "regressed"
    else:
        classification = "inconclusive"
        reasons.add("minimum_delta_not_met")

    return CaseComparison(
        suite_revision_id=case.suite_revision_id,
        case_revision_id=case.case_revision_id,
        tier=case.tier,
        required=case.required,
        classification=classification,
        reason_codes=tuple(sorted(reasons)),
        baseline_pass_rate=baseline_rate,
        candidate_pass_rate=candidate_rate,
        baseline_samples=len(case.baseline),
        candidate_samples=len(case.candidate),
        cost_microusd=sum(item.cost_microusd or 0 for item in all_attempts),
        duration_ms=sum(item.duration_ms or 0 for item in all_attempts),
    )


def project_readiness(
    cases: tuple[ComparisonCase, ...],
    *,
    pass_thresholds: dict[str, float],
    minimum_delta: float,
    flaky_limits: dict[str, float],
    verdict_rules: Mapping[str, str],
    safety_status: str,
    compatibility_verdict: str,
    max_comparison_cost_microusd: int,
    execution_identity: dict[str, object],
    run_stop_reasons: tuple[str, ...] = (),
) -> ReadinessDecision:
    comparisons = tuple(
        _compare_with_rule_view(
            case,
            pass_threshold=pass_thresholds.get(case.tier, pass_thresholds.get("standard", 0.8)),
            minimum_delta=minimum_delta,
            flaky_limit=flaky_limits.get(case.tier, flaky_limits.get("standard", 0.2)),
            verdict_rule=_verdict_rule(verdict_rules, case.tier),
        )
        for case in cases
    )
    hard: set[str] = set()
    unknown: set[str] = set()
    risks: set[str] = set()
    coverage: set[str] = set()

    if safety_status in {"blocked", "unknown"}:
        hard.add("agent_safety_blocked")
    for item in comparisons:
        if item.point_only:
            # A model verdict alone never hard-blocks or leaves the gate unknown: the Owner can
            # release past it (PRD E11, R23). Non-required cases keep their existing weight.
            if item.classification == "regressed":
                risks.add("point_regression")
            elif item.required:
                risks.add("point_unmet")
        elif item.classification in {"incomparable", "inconclusive"} and item.required:
            unknown.update(item.reason_codes or ("required_result_inconclusive",))
            coverage.add(f"{item.suite_revision_id}:{item.case_revision_id}")
        elif item.classification == "unchanged_fail" and item.required:
            hard.add("required_quality_failed")
        elif item.classification == "regressed":
            if item.tier == "critical":
                hard.add("critical_regression")
            else:
                risks.add("non_critical_regression")
    if compatibility_verdict == "unknown":
        risks.add("compatibility_unknown")
    if any(attempt.reproducibility == "attributed_only" for case in cases for attempt in (*case.baseline, *case.candidate)):
        risks.add("provider_identity_attributed_only")

    total_cost = sum(item.cost_microusd for item in comparisons)
    total_duration = sum(item.duration_ms for item in comparisons)
    if total_cost > max_comparison_cost_microusd:
        unknown.add("comparison_cost_budget_exceeded")
    # A run stopped before its attempts (for example no usable model): the cause, not only "attempts missing".
    unknown.update(run_stop_reasons)
    # Duration is not summed here: attempts run concurrently, so their summed time is not the
    # comparison's wall-clock time. Each run enforces its share of max_comparison_duration_ms while
    # it runs, and an attempt it cut reports comparison_duration_budget_exceeded above.

    if hard:
        state: SnapshotReadiness = "blocked"
        reasons = hard
    elif unknown:
        state = "unknown"
        reasons = unknown
    elif risks:
        state = "ready_with_risks"
        reasons = risks
    else:
        state = "ready"
        reasons = set()
    identity_document = {
        **execution_identity,
        "attempt_identities": [
            {
                "case_revision_id": case.case_revision_id,
                "suite_revision_id": case.suite_revision_id,
                "baseline": [_identity(item) for item in case.baseline],
                "candidate": [_identity(item) for item in case.candidate],
            }
            for case in cases
        ],
        "schema_version": 1,
    }
    aggregate = {
        "case_count": len(comparisons),
        "cost_microusd": total_cost,
        "duration_ms": total_duration,
    }
    reviewer_projection: dict[str, object] = {
        "aggregate_metrics": aggregate,
        "case_summaries": [
            {
                "case_revision_id": item.case_revision_id,
                "classification": item.classification,
                "reason_codes": list(item.reason_codes),
                "suite_revision_id": item.suite_revision_id,
                "tier": item.tier,
                **({"point_only": True} if item.point_only else {}),
                **({"unmet_point_ids": list(item.unmet_point_ids)} if item.unmet_point_ids else {}),
            }
            for item in comparisons
        ],
        "coverage_gaps": sorted(coverage),
        "readiness": state,
        "reason_codes": sorted(reasons),
        "schema_version": 1,
    }
    return ReadinessDecision(
        state=state,
        reason_codes=tuple(sorted(reasons)),
        case_comparisons=comparisons,
        coverage_gaps=tuple(sorted(coverage)),
        aggregate_metrics=aggregate,
        reviewer_projection=reviewer_projection,
        identity_document=identity_document,
    )


# A full verdict that may come from points alone: failing, or unstable because a point failed on
# some attempts. The rules alone must then pass cleanly for the case to count as point-only.
_POINT_SENSITIVE = frozenset({"regressed", "unchanged_fail", "inconclusive"})
_RULES_CLEAN = frozenset({"unchanged_pass", "fixed"})


def _compare_with_rule_view(
    case: ComparisonCase,
    *,
    pass_threshold: float,
    minimum_delta: float,
    flaky_limit: float,
    verdict_rule: VerdictRule,
) -> CaseComparison:
    """Classify on the full verdict, then on the rules alone to tell a point-only failure apart."""

    full = compare_case(case, pass_threshold=pass_threshold, minimum_delta=minimum_delta, flaky_limit=flaky_limit, verdict_rule=verdict_rule)
    unmet = tuple(sorted({point for attempt in case.candidate for point in attempt.unmet_point_ids}))
    if full.classification not in _POINT_SENSITIVE:
        return full
    rule_view = replace(
        case,
        baseline=tuple(_rule_attempt(item) for item in case.baseline),
        candidate=tuple(_rule_attempt(item) for item in case.candidate),
    )
    if rule_view == case:
        return replace(full, unmet_point_ids=unmet)
    rules = compare_case(rule_view, pass_threshold=pass_threshold, minimum_delta=minimum_delta, flaky_limit=flaky_limit, verdict_rule=verdict_rule)
    return replace(full, point_only=rules.classification in _RULES_CLEAN, unmet_point_ids=unmet)


def _rule_attempt(item: AttemptEvidence) -> AttemptEvidence:
    if item.status == "failed" and item.rule_passed is True and item.rubric_passed is not False and item.points_passed is False:
        return replace(item, status="passed")
    return item


def _identity_mismatch(
    *,
    baseline: tuple[AttemptEvidence, ...],
    candidate: tuple[AttemptEvidence, ...],
    allow_subject_provider_delta: bool,
) -> bool:
    attempts = (*baseline, *candidate)
    judge = {(item.judge_provider_ref, item.judge_provider_hash) for item in attempts}
    if len(judge) > 1:
        return True
    if allow_subject_provider_delta:
        return (
            len({(item.subject_provider_ref, item.subject_provider_hash) for item in baseline}) > 1
            or len(
                {(item.subject_provider_ref, item.subject_provider_hash) for item in candidate},
            )
            > 1
        )
    return len({(item.subject_provider_ref, item.subject_provider_hash) for item in attempts}) > 1


def _data_changed(case: ComparisonCase) -> bool:
    from bowhead_core.agent_eval_datarc import data_changed_during_test

    return data_changed_during_test(
        (pair for item in case.baseline for pair in item.datarc_digests),
        (pair for item in case.candidate for pair in item.datarc_digests),
    )


def _time_limited(item: AttemptEvidence) -> bool:
    return item.status == "infra_failed" and item.failure_code in ATTEMPT_TIME_LIMIT_FAILURES | RUN_TIME_LIMIT_FAILURES


def _pass_rate(items: tuple[AttemptEvidence, ...]) -> float | None:
    if not items:
        return None
    return sum(item.status == "passed" for item in items) / len(items)


def _verdict_rule(verdict_rules: Mapping[str, str], tier: str) -> VerdictRule:
    """A tier's rule; a tier the policy doesn't name follows `standard`, like its limits. A policy
    written before verdict rules has none, and every tier keeps the threshold rule it was decided by."""

    rule = verdict_rules.get(tier, verdict_rules.get("standard", "threshold"))
    if rule not in {"threshold", "majority"}:
        raise ValueError("Agent Eval verdict rule is invalid")
    return cast(VerdictRule, rule)


def _majority(items: tuple[AttemptEvidence, ...], *, attempts: int) -> bool | None:
    """True when more than half of the planned attempts passed, False when more than half failed."""

    if 2 * sum(item.status == "passed" for item in items) > attempts:
        return True
    if 2 * sum(item.status == "failed" for item in items) > attempts:
        return False
    return None


def _short_of_majority(items: tuple[AttemptEvidence, ...], *, attempts: int) -> bool:
    """One side's verdicts form no majority: an even split, or too few attempts with a verdict.

    An even split (1-1, 2-2) is unstable whatever else happened, as a split was under the flaky rate.
    Too few verdicts without a split is unstable only when time limits cut the other attempts: missing
    attempts, or attempts that ended without a verdict for another reason, already make the case not
    comparable with that cause. A side with no verdict at all has nothing to call unstable.
    """

    passed = sum(item.status == "passed" for item in items)
    failed = sum(item.status == "failed" for item in items)
    if not passed + failed or _majority(items, attempts=attempts) is not None:
        return False
    if passed == failed:
        return True
    return len(items) == attempts and all(item.status in _DECIDED_STATUSES or _time_limited(item) for item in items)


def _majority_classification(case: ComparisonCase, reasons: set[str]) -> CaseClassification:
    """Classify from each side's majority verdict; a flipped majority is always a real change."""

    candidate = _majority(case.candidate, attempts=case.attempts)
    if candidate is None:
        reasons.add("candidate_result_missing")
        return "inconclusive"
    if case.execution_scope == "candidate_only":
        return "unchanged_pass" if candidate else "unchanged_fail"
    baseline = _majority(case.baseline, attempts=case.attempts)
    if baseline is None:
        reasons.add("baseline_result_missing")
        return "inconclusive"
    if baseline == candidate:
        return "unchanged_pass" if candidate else "unchanged_fail"
    return "fixed" if candidate else "regressed"


def _flaky_rate(items: tuple[AttemptEvidence, ...]) -> float:
    """How split the attempts that reached a verdict are; attempts without one never make a case flaky."""

    rate = _pass_rate(tuple(item for item in items if item.status in _DECIDED_STATUSES))
    return min(rate, 1.0 - rate) if rate is not None else 0.0


def _identity(item: AttemptEvidence) -> dict[str, object]:
    return {
        "judge_provider_hash": item.judge_provider_hash,
        "judge_provider_ref": item.judge_provider_ref,
        "reproducibility": item.reproducibility,
        "subject_provider_hash": item.subject_provider_hash,
        "subject_provider_ref": item.subject_provider_ref,
    }


def _hash_document(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


__all__ = [
    "AttemptEvidence",
    "CaseComparison",
    "ComparisonCase",
    "ReadinessDecision",
    "VerdictRule",
    "compare_case",
    "project_readiness",
]
