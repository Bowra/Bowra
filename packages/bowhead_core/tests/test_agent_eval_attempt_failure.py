"""Attempts without a verdict keep their cause: each category the Owner acts on differently."""

from __future__ import annotations

import pytest
from bowhead_core.agent_eval_attempt_failure import (
    attempt_failure_detail,
    classify_attempt_exception,
    incomplete_attempt_reason,
)
from bowhead_core.agent_eval_subagent_executor import AgentEvalPlatformFailure
from bowhead_core.agent_version_eval import AttemptEvidence, ComparisonCase, compare_case, project_readiness
from bowhead_router.adapter import TeamModelUnconfiguredError
from bowhead_router.router import NoBindingError


@pytest.mark.parametrize(
    ("exc", "failed_tool", "expected"),
    [
        (
            AgentEvalPlatformFailure("subagent_internal_error", llm_reason_code="llm_provider_rate_limited"),
            None,
            ("agent_eval_agent_model_rate_limited", "agent_model", "rate_limited", None),
        ),
        (
            AgentEvalPlatformFailure("subagent_internal_error", llm_reason_code="llm_provider_auth_failed"),
            None,
            ("agent_eval_agent_model_auth_failed", "agent_model", "auth_failed", None),
        ),
        (AgentEvalPlatformFailure("peer_llm_deadline_exceeded"), None, ("agent_eval_agent_model_timeout", "agent_model", "timeout", None)),
        (
            AgentEvalPlatformFailure("subagent_internal_error", llm_reason_code="llm_context_overflow"),
            None,
            ("agent_eval_agent_model_context_overflow", "agent_model", "context_overflow", None),
        ),
        # No usable model at all is its own category, never an unclassified failure.
        (
            AgentEvalPlatformFailure("subagent_internal_error", llm_reason_code="team_model_role_unbound", model_role="analyst"),
            None,
            ("agent_eval_model_unavailable", "model_unavailable", "role_unassigned", None),
        ),
        (NoBindingError("analyst"), None, ("agent_eval_model_unavailable", "model_unavailable", "role_unassigned", None)),
        (TeamModelUnconfiguredError("assembly router"), None, ("agent_eval_model_unavailable", "model_unavailable", "unconfigured", None)),
        (RuntimeError("socket closed"), "python_exec", ("agent_eval_tool_failed", "tool", None, "python_exec")),
        (RuntimeError("anything else"), None, ("agent_eval_attempt_failed", "platform", None, None)),
    ],
)
def test_exceptions_are_classified_from_typed_facts(exc, failed_tool, expected) -> None:
    failure = classify_attempt_exception(exc, failed_tool=failed_tool)
    assert (failure.failure_code, failure.category, failure.cause, failure.tool) == expected


def test_the_role_without_a_model_is_named() -> None:
    wrapped = AgentEvalPlatformFailure("subagent_internal_error", llm_reason_code="team_model_role_unbound", model_role="analyst")
    assert classify_attempt_exception(wrapped, failed_tool=None).role == "analyst"
    assert classify_attempt_exception(NoBindingError("judge"), failed_tool=None).role == "judge"


def test_provider_text_never_enters_the_stored_evidence() -> None:
    secret = "sk-live-0123456789 rejected for org acme"
    failure = classify_attempt_exception(RuntimeError(secret), failed_tool=None)
    assert secret not in str(failure.evidence(reference="aej_1"))
    assert failure.evidence(reference="aej_1") == {"failure": {"category": "platform", "reference": "aej_1"}}


@pytest.mark.parametrize(
    ("status", "failure_code", "reason"),
    [
        ("judge_unknown", "agent_eval_judge_unavailable", "attempt_judge_unavailable"),
        ("judge_unknown", "agent_eval_judge_not_configured", "attempt_judge_not_configured"),
        ("cancelled", "agent_eval_cancelled", "attempt_cancelled"),
        ("infra_failed", "agent_eval_agent_model_rate_limited", "attempt_agent_model_rate_limited"),
        ("infra_failed", "agent_eval_model_unavailable", "attempt_model_unavailable"),
        ("infra_failed", "agent_eval_tool_failed", "attempt_tool_failed"),
        ("infra_failed", "agent_eval_attempt_failed", "attempt_infrastructure_incomplete"),
        # Time limits and required Tools that can't run have their own reasons.
        ("infra_failed", "agent_eval_attempt_timeout", None),
        ("infra_failed", "agent_eval_required_tool_not_run", None),
        ("infra_failed", "agent_eval_tool_not_run_in_eval", None),
        ("passed", None, None),
    ],
)
def test_each_cause_has_its_own_release_reason(status, failure_code, reason) -> None:
    assert incomplete_attempt_reason(status, failure_code) == reason


def _attempt(status: str, failure_code: str | None = None) -> AttemptEvidence:
    return AttemptEvidence(
        status=status,  # type: ignore[arg-type]
        subject_provider_ref="p",
        subject_provider_hash="sha256:" + "0" * 64,
        judge_provider_ref=None,
        judge_provider_hash=None,
        reproducibility="exact",
        failure_code=failure_code,
    )


def _case(baseline: tuple[AttemptEvidence, ...], candidate: tuple[AttemptEvidence, ...]) -> ComparisonCase:
    return ComparisonCase(
        suite_revision_id="aev_1",
        case_revision_id="aec_1",
        tier="standard",
        required=True,
        attempts=len(candidate),
        execution_scope="paired",
        baseline=baseline,
        candidate=candidate,
    )


def test_a_case_reports_every_cause_of_its_attempts_not_one_generic_code() -> None:
    result = compare_case(
        _case(
            (_attempt("infra_failed", "agent_eval_model_unavailable"),),
            (_attempt("judge_unknown", "agent_eval_judge_unavailable"),),
        ),
        pass_threshold=0.8,
        minimum_delta=0.05,
        flaky_limit=0.2,
        verdict_rule="majority",
    )
    assert result.classification == "inconclusive"
    assert set(result.reason_codes) >= {"attempt_model_unavailable", "attempt_judge_unavailable"}
    assert "attempt_infrastructure_incomplete" not in result.reason_codes


def test_a_run_stopped_for_its_model_names_that_cause_in_readiness() -> None:
    decision = project_readiness(
        (_case((), ()),),
        pass_thresholds={"standard": 0.8},
        minimum_delta=0.05,
        flaky_limits={"standard": 0.2},
        verdict_rules={"standard": "majority"},
        safety_status="passed",
        compatibility_verdict="compatible",
        max_comparison_cost_microusd=1_000_000,
        execution_identity={},
        run_stop_reasons=("run_model_unavailable",),
    )
    assert decision.state == "unknown"
    assert "run_model_unavailable" in decision.reason_codes


def test_old_attempts_without_stored_facts_read_as_platform_errors_with_their_reference() -> None:
    detail = attempt_failure_detail(status="infra_failed", failure_code="agent_eval_attempt_failed", deterministic={}, reference="aej_old")
    assert detail == {"category": "platform", "reference": "aej_old", "reason_code": "attempt_infrastructure_incomplete"}


@pytest.mark.parametrize(
    ("stored", "failure_code", "cause"),
    [
        ({"category": "cancelled", "cause": "restart", "reference": "aej_1"}, "agent_eval_cancelled", "restart"),
        ({"category": "cancelled", "cause": "authority_lost", "reference": "aej_1"}, "agent_eval_cancelled", "authority_lost"),
        ({}, "agent_eval_comparison_cancelled", "stopped"),
    ],
)
def test_a_cancelled_attempt_says_what_stopped_it(stored, failure_code, cause) -> None:
    detail = attempt_failure_detail(status="cancelled", failure_code=failure_code, deterministic={"failure": stored}, reference="aej_1")
    assert detail is not None and detail["category"] == "cancelled" and detail["cause"] == cause


def test_a_tool_failure_keeps_the_tool_and_a_missing_model_keeps_the_role() -> None:
    tool = attempt_failure_detail(
        status="infra_failed",
        failure_code="agent_eval_tool_failed",
        deterministic={"failure": {"category": "tool", "tool": "python_exec", "reference": "aej_2"}},
        reference="fallback",
    )
    assert tool is not None and (tool["tool"], tool["reference"]) == ("python_exec", "aej_2")
    model = attempt_failure_detail(
        status="infra_failed",
        failure_code="agent_eval_model_unavailable",
        deterministic={"failure": {"category": "model_unavailable", "cause": "role_unassigned", "role": "analyst", "reference": "aej_3"}},
        reference="fallback",
    )
    assert model is not None and (model["category"], model["role"], model["cause"]) == ("model_unavailable", "analyst", "role_unassigned")
