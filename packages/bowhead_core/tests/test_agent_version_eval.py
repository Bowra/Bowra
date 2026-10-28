from dataclasses import asdict, replace

import pytest
from bowhead_core.agent_eval_insights import classify_change, comparison_overview
from bowhead_core.agent_eval_policy_store import DEFAULT_VERDICT_RULES
from bowhead_core.agent_version_eval import AttemptEvidence, ComparisonCase, project_readiness


def _attempt(status="passed", *, provider_hash="sha256:model", reproducibility="exact"):
    return AttemptEvidence(
        status=status,
        subject_provider_ref="model-config",
        subject_provider_hash=provider_hash,
        judge_provider_ref=None,
        judge_provider_hash=None,
        reproducibility=reproducibility,
        duration_ms=100,
        cost_microusd=200,
    )


def _decision(case, *, compatibility="compatible"):
    return project_readiness(
        (case,),
        pass_thresholds={"critical": 1.0, "standard": 0.8},
        minimum_delta=0.05,
        flaky_limits={"critical": 0.0, "standard": 0.2},
        verdict_rules=DEFAULT_VERDICT_RULES,
        safety_status="pass",
        compatibility_verdict=compatibility,
        max_comparison_cost_microusd=10_000,
        execution_identity={"execution_id": "aex_test"},
    )


def test_readiness_is_ready_for_complete_passing_exact_comparison() -> None:
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=3,
        execution_scope="paired",
        baseline=(_attempt(), _attempt(), _attempt()),
        candidate=(_attempt(), _attempt(), _attempt()),
    )

    decision = _decision(case)

    assert decision.state == "ready"
    assert decision.case_comparisons[0].classification == "unchanged_pass"
    assert decision.snapshot_hash.startswith("sha256:")


def test_readiness_separates_quality_risk_from_inconclusive_evidence() -> None:
    regression = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=3,
        execution_scope="paired",
        baseline=(_attempt(), _attempt(), _attempt()),
        candidate=(_attempt("failed"), _attempt("failed"), _attempt("failed")),
    )
    unknown = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=3,
        execution_scope="paired",
        baseline=(_attempt(), _attempt(), _attempt()),
        candidate=(_attempt(), _attempt("infra_failed"), _attempt()),
    )

    assert _decision(regression).state == "ready_with_risks"
    assert _decision(regression).reason_codes == ("non_critical_regression",)
    assert _decision(unknown).state == "unknown"
    assert "attempt_infrastructure_incomplete" in _decision(unknown).reason_codes


def test_readiness_requires_platform_safety_but_not_custom_cases() -> None:
    critical = ComparisonCase(
        suite_revision_id="aev_platform",
        case_revision_id="aec_platform",
        tier="critical",
        required=True,
        attempts=1,
        execution_scope="candidate_only",
        baseline=(),
        candidate=(_attempt("failed"),),
    )

    assert _decision(critical).state == "blocked"
    passing = ComparisonCase(
        suite_revision_id="aev_platform",
        case_revision_id="aec_platform",
        tier="critical",
        required=True,
        attempts=1,
        execution_scope="candidate_only",
        baseline=(),
        candidate=(_attempt(),),
    )
    decision = _decision(passing)
    assert decision.state == "ready"
    assert decision.reason_codes == ()
    assert _decision(passing, compatibility="unknown").reason_codes == ("compatibility_unknown",)


def _timed_out(failure_code="agent_eval_attempt_timeout"):
    return AttemptEvidence(
        status="infra_failed",
        subject_provider_ref="model-config",
        subject_provider_hash="sha256:model",
        judge_provider_ref=None,
        judge_provider_hash=None,
        reproducibility="exact",
        duration_ms=120_400,
        failure_code=failure_code,
    )


def test_a_timed_out_attempt_drops_only_itself_and_the_finished_attempts_decide() -> None:
    # 116 walkthrough: one 120 s cut on the candidate threw away five passing attempts.
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=3,
        execution_scope="paired",
        baseline=(_attempt(), _attempt(), _attempt()),
        candidate=(_attempt(), _attempt(), _timed_out()),
    )

    decision = _decision(case)

    assert decision.state == "ready"
    assert decision.case_comparisons[0].classification == "unchanged_pass"
    assert decision.case_comparisons[0].reason_codes == ()


def test_finished_attempts_after_a_time_limit_still_fail_a_case_they_fail() -> None:
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=3,
        execution_scope="paired",
        baseline=(_attempt("failed"), _timed_out(), _attempt("failed")),
        candidate=(_attempt("failed"), _attempt("failed"), _timed_out()),
    )

    decision = _decision(case)

    assert decision.state == "blocked"
    assert decision.reason_codes == ("required_quality_failed",)


def test_a_side_with_every_attempt_cut_has_no_result_and_says_time_limit() -> None:
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=2,
        execution_scope="paired",
        baseline=(_attempt(), _attempt()),
        candidate=(_timed_out(), _timed_out()),
    )

    decision = _decision(case)

    assert decision.state == "unknown"
    assert decision.reason_codes == ("attempt_time_limit_exceeded",)
    assert decision.case_comparisons[0].reason_codes == ("attempt_time_limit_exceeded",)


def test_run_budget_cut_is_reported_as_the_comparison_time_limit() -> None:
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=1,
        execution_scope="candidate_only",
        baseline=(),
        candidate=(_timed_out("agent_eval_duration_budget_exceeded"),),
    )

    assert _decision(case).reason_codes == ("comparison_duration_budget_exceeded",)


def test_required_tool_not_run_leaves_the_case_without_a_result() -> None:
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=1,
        execution_scope="candidate_only",
        baseline=(),
        candidate=(_timed_out("agent_eval_required_tool_not_run"),),
    )

    assert _decision(case).reason_codes == ("required_tool_not_run_in_eval",)


def test_a_case_neither_version_could_test_is_not_counted_as_worse_or_failing() -> None:
    # Both versions' answers depended on a data query the evaluation can't run yet.
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="critical",
        required=True,
        attempts=2,
        execution_scope="paired",
        baseline=(_timed_out("agent_eval_tool_not_run_in_eval"), _timed_out("agent_eval_tool_not_run_in_eval")),
        candidate=(_timed_out("agent_eval_tool_not_run_in_eval"), _timed_out("agent_eval_tool_not_run_in_eval")),
    )

    decision = _decision(case)

    assert decision.case_comparisons[0].classification == "inconclusive"
    assert decision.state == "unknown"
    assert decision.reason_codes == ("tool_not_run_in_eval",)


def test_flaky_counts_only_attempts_with_a_verdict() -> None:
    split = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=3,
        execution_scope="candidate_only",
        baseline=(),
        candidate=(_attempt(), _attempt("failed"), _attempt("infra_failed")),
    )

    reasons = _decision(split).case_comparisons[0].reason_codes
    assert reasons == ("attempt_infrastructure_incomplete", "flaky_limit_exceeded")


def test_long_concurrent_attempts_do_not_exceed_the_comparison_time_by_their_sum() -> None:
    # Six 40 s attempts sum past a short comparison limit only because they run in parallel; each
    # run enforces the wall-clock limit itself while it runs.
    slow = AttemptEvidence(
        status="passed",
        subject_provider_ref="model-config",
        subject_provider_hash="sha256:model",
        judge_provider_ref=None,
        judge_provider_hash=None,
        reproducibility="exact",
        duration_ms=40_000,
        cost_microusd=1,
    )
    case = ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier="standard",
        required=True,
        attempts=3,
        execution_scope="paired",
        baseline=(slow, slow, slow),
        candidate=(slow, slow, slow),
    )

    assert _decision(case).state == "ready"


def _sides(baseline: str, candidate: str, *, tier: str = "standard", scope: str = "paired") -> ComparisonCase:
    """One case from attempt strings: p passed, f failed, t cut by its time limit, i infrastructure failure."""
    kinds = {"p": lambda: _attempt(), "f": lambda: _attempt("failed"), "t": _timed_out, "i": lambda: _attempt("infra_failed")}
    return ComparisonCase(
        suite_revision_id="aev_owner",
        case_revision_id="aec_owner",
        tier=tier,  # type: ignore[arg-type]
        required=True,
        attempts=len(candidate),
        execution_scope=scope,  # type: ignore[arg-type]
        baseline=tuple(kinds[kind]() for kind in baseline),
        candidate=tuple(kinds[kind]() for kind in candidate),
    )


@pytest.mark.parametrize(
    ("baseline", "candidate", "classification", "state"),
    [
        # The customer case: one disagreeing attempt no longer leaves the test without a verdict.
        ("ppp", "ppf", "unchanged_pass", "ready"),
        ("ppf", "fpp", "unchanged_pass", "ready"),
        ("ppp", "pff", "regressed", "ready_with_risks"),
        ("pff", "ppf", "fixed", "ready"),
        ("pff", "ffp", "unchanged_fail", "blocked"),
        ("ppppf", "pppff", "unchanged_pass", "ready"),
    ],
)
def test_a_standard_case_is_decided_by_the_majority_of_its_attempts(baseline: str, candidate: str, classification: str, state: str) -> None:
    decision = _decision(_sides(baseline, candidate))

    assert decision.case_comparisons[0].classification == classification
    assert "flaky_limit_exceeded" not in decision.case_comparisons[0].reason_codes
    assert decision.state == state


@pytest.mark.parametrize(
    ("baseline", "candidate"),
    [
        ("pppp", "ppff"),  # an even split
        ("ppff", "pppp"),  # on either side of a paired case
        ("ppp", "pft"),  # one attempt cut: one pass and one fail
        ("ppp", "ptt"),  # too few attempts with a verdict to form a majority
    ],
)
def test_a_standard_case_without_a_majority_is_unstable(baseline: str, candidate: str) -> None:
    decision = _decision(_sides(baseline, candidate))

    assert decision.case_comparisons[0].classification == "inconclusive"
    assert decision.case_comparisons[0].reason_codes == ("flaky_limit_exceeded",)
    assert decision.state == "unknown" and decision.reason_codes == ("flaky_limit_exceeded",)


def test_a_majority_needs_more_than_half_of_the_planned_attempts() -> None:
    # Two of three passed after one was cut: still a majority of the three planned attempts.
    assert _decision(_sides("ppp", "ppt")).case_comparisons[0].classification == "unchanged_pass"
    # An infrastructure failure is its own cause; the attempts left never read as unstable.
    reasons = _decision(_sides("ppp", "pii")).case_comparisons[0].reason_codes
    assert "flaky_limit_exceeded" not in reasons and "attempt_infrastructure_incomplete" in reasons
    # A candidate-only standard case is passed or failed by its own majority.
    assert _decision(_sides("", "fpp", scope="candidate_only")).case_comparisons[0].classification == "unchanged_pass"
    assert _decision(_sides("", "ffp", scope="candidate_only")).state == "blocked"


def test_a_critical_case_still_needs_every_attempt_to_agree() -> None:
    decision = _decision(_sides("ppp", "ppf", tier="critical"))

    assert decision.case_comparisons[0].classification == "inconclusive"
    assert decision.state == "unknown" and decision.reason_codes == ("flaky_limit_exceeded",)


def test_a_diagnostic_case_follows_the_standard_majority() -> None:
    case = replace(_sides("ppp", "ppf", tier="diagnostic"), required=False)

    assert _decision(case).case_comparisons[0].classification == "unchanged_pass"


def test_a_policy_without_verdict_rules_keeps_the_threshold_rule_its_plans_were_made_with() -> None:
    legacy = project_readiness(
        (_sides("ppp", "ppf"),),
        pass_thresholds={"critical": 1.0, "standard": 0.8},
        minimum_delta=0.05,
        flaky_limits={"critical": 0.0, "standard": 0.2},
        verdict_rules={},
        safety_status="pass",
        compatibility_verdict="compatible",
        max_comparison_cost_microusd=10_000,
        execution_identity={"execution_id": "aex_test"},
    )

    assert legacy.state == "unknown" and legacy.reason_codes == ("flaky_limit_exceeded",)


def test_majority_results_feed_the_pass_rates_and_newly_failing_counts() -> None:
    decision = _decision(_sides("ppp", "ppf"))
    regressed = _decision(_sides("ppp", "pff"))
    summary = {"case_comparisons": [asdict(item) for item in (*decision.case_comparisons, *regressed.case_comparisons)]}

    overview = comparison_overview(summary)

    assert overview["candidate_pass_rate"] == 0.5 and overview["live_pass_rate"] == 1.0
    assert overview["newly_failing"] == 1 and overview["newly_passing"] == 0
    assert classify_change("unchanged_pass", ()) == "unchanged"
    assert [classify_change(item.classification, item.reason_codes) for item in regressed.case_comparisons] == ["worse"]
