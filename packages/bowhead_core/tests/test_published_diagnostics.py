"""Named published-diagnostic policy and documentation contracts."""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from pathlib import Path

import pytest
from bowhead_core.llm_failure import LLMDeliveryFailedError, project_llm_failure
from bowhead_router.errors import (
    InvocationTargetsExhaustedError,
    LLMTargetFailure,
    ProviderFailureKind,
    StreamingInterruptedError,
    StreamingTargetsExhaustedError,
)
from bowhead_router.output_budget import ContextCapacityError, RenderedRequestBudget

from bowhead_core import metrics
from bowhead_core import published_diagnostics as diagnostics


@pytest.mark.parametrize(
    ("reason_code", "failure_class", "retryable"),
    [
        ("wallclock_exceeded", "timeout", True),
        ("peer_dispatch_network_error", "network", True),
        ("token_cap_exhausted", "limit", False),
        ("access_context_missing", "access", True),
        ("plan_runtime_unavailable", "configuration", False),
        ("reference_continuation_unavailable", "configuration", False),
        ("reference_policy_changed", "access", False),
        ("lead_internal_error", "internal", True),
        ("document_information_incomplete", "content", False),
        ("document_planning_failed", "runtime", False),
        ("document_authoring_script_failed", "runtime", False),
        ("document_candidate_invalid", "content", False),
        ("document_history_unavailable", "configuration", False),
        ("document_request_rejected", "content", False),
        ("document_routing_invalid", "runtime", True),
        ("document_routing_uncertain", "content", False),
        ("document_routing_source_unavailable", "access", False),
    ],
)
def test_chat_turn_failure_publisher_uses_only_finite_facts(
    reason_code: str,
    failure_class: str,
    retryable: bool,
) -> None:
    published = diagnostics.publish_chat_turn_failure(reason_code)

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "chat_turn_failed",
        "safe_detail": f"phase=chat; failure_class={failure_class}",
        "retryable": retryable,
    }


def test_chat_turn_failure_builder_creates_one_reusable_correlated_object() -> None:
    failure = diagnostics.build_chat_turn_failure(
        reason_code="wallclock_exceeded",
        turn_id="t_safe_123",
    )

    assert failure.reason_code == "wallclock_exceeded"
    assert len(failure.occurrence_id) == 32
    assert failure.diagnostics == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "chat_turn_failed",
        "occurrence_id": failure.occurrence_id,
        "turn_id": "t_safe_123",
        "safe_detail": "phase=chat; failure_class=timeout",
        "retryable": True,
    }
    turn_failed_payload = {
        "reason": failure.reason_code,
        "diagnostics": failure.diagnostics,
    }
    done_payload = {
        "reason_code": failure.reason_code,
        "diagnostics": failure.diagnostics,
    }
    assert turn_failed_payload["diagnostics"] is done_payload["diagnostics"]


def _llm_target_failure(
    *,
    kind: ProviderFailureKind,
    status_code: int | None,
    visible: bool = False,
) -> LLMTargetFailure:
    return LLMTargetFailure(
        provider="private-configured-name",
        model="private-model-name",
        provider_family="openai_compatible",
        mode="stream",
        target_index=1,
        attempt=2,
        kind=kind,
        retryable=True,
        status_code=status_code,
        upstream_error_id="upstream-error-123",
        upstream_message="Internal server error Authorization: Bearer secret",
        first_visible_chunk_emitted=visible,
    )


def test_chat_llm_failure_publishes_finite_detail_and_screens_provider_prose() -> None:
    projection = project_llm_failure(
        StreamingTargetsExhaustedError([_llm_target_failure(kind=ProviderFailureKind.UPSTREAM, status_code=500)]),
        fallback_state="failed",
    )
    assert projection is not None

    failure = diagnostics.build_chat_llm_failure(
        projection=projection,
        turn_id="t_llm_safe",
    )

    assert failure.reason_code == "llm_provider_unavailable"
    assert failure.diagnostics["diagnostic_code"] == "chat_llm_failed"
    assert failure.diagnostics["code"] == failure.reason_code
    assert failure.diagnostics["safe_detail"] == (
        "phase=stream; failure_class=upstream; provider_family=openai_compatible; "
        "http_status=500; stream_state=pre_visible; fallback=failed; attempts=1"
    )
    wire = str(failure.diagnostics)
    assert "private-configured-name" not in wire
    assert "private-model-name" not in wire
    assert "Internal server error" not in wire
    assert "secret" not in wire


def test_local_capacity_failure_preserves_zero_attempt_diagnostics_and_same_session_recovery() -> None:
    projection = project_llm_failure(
        ContextCapacityError(
            "Private model capacity detail",
            model="private-model-name",
            budget=RenderedRequestBudget(20000, "text_estimated", 8192, "configured", 1024),
        )
    )
    assert projection is not None
    failure = diagnostics.build_chat_llm_failure(projection=projection, turn_id="t_local_capacity")

    assert failure.reason_code == "llm_context_overflow"
    assert failure.recovery_action == "edit_and_resend"
    assert failure.diagnostics["diagnostic_code"] == "chat_llm_failed"
    assert "stream_state=not_started" in failure.diagnostics["safe_detail"]
    assert "attempts=0" in failure.diagnostics["safe_detail"]
    assert "private-model-name" not in str(failure.diagnostics)
    assert "Private model capacity detail" not in str(failure.diagnostics)


@pytest.mark.parametrize(
    "invalid",
    [
        {"phase": "private"},
        {"failure_class": "traceback"},
        {"provider_family": "private-provider"},
        {"http_status": 99},
        {"stream_state": "unknown"},
        {"fallback_state": "maybe"},
        {"attempts": 100},
        {"attempts": 0},
    ],
)
def test_chat_llm_failure_publisher_rejects_non_finite_projection(
    invalid: dict[str, object],
) -> None:
    projection = project_llm_failure(
        StreamingTargetsExhaustedError(
            [
                _llm_target_failure(
                    kind=ProviderFailureKind.UPSTREAM,
                    status_code=500,
                )
            ]
        )
    )
    assert projection is not None

    assert diagnostics.publish_chat_llm_failure(replace(projection, **invalid)) is None


def test_chat_llm_interrupted_failure_is_non_fallback_and_correlated() -> None:
    projection = project_llm_failure(
        StreamingInterruptedError(
            [
                _llm_target_failure(
                    kind=ProviderFailureKind.TIMEOUT,
                    status_code=None,
                    visible=True,
                )
            ]
        )
    )
    assert projection is not None
    failure = diagnostics.build_chat_llm_failure(
        projection=projection,
        turn_id="t_llm_interrupted",
    )

    assert failure.reason_code == "llm_stream_interrupted"
    assert failure.diagnostics["retryable"] is True
    assert "stream_state=interrupted" in failure.diagnostics["safe_detail"]
    assert "fallback=not_attempted" in failure.diagnostics["safe_detail"]


def test_chat_llm_invalid_2xx_response_keeps_safe_http_status() -> None:
    projection = project_llm_failure(
        StreamingTargetsExhaustedError(
            [
                _llm_target_failure(
                    kind=ProviderFailureKind.RESPONSE_INVALID,
                    status_code=200,
                )
            ]
        )
    )
    assert projection is not None

    failure = diagnostics.build_chat_llm_failure(
        projection=projection,
        turn_id="t_llm_invalid_2xx",
    )

    assert failure.reason_code == "llm_response_invalid"
    assert "http_status=200" in failure.diagnostics["safe_detail"]


def test_chat_llm_fallback_failure_uses_invoke_terminal_reason() -> None:
    stream_error = StreamingTargetsExhaustedError([_llm_target_failure(kind=ProviderFailureKind.UPSTREAM, status_code=500)])
    invoke_error = InvocationTargetsExhaustedError([_llm_target_failure(kind=ProviderFailureKind.AUTH, status_code=401)])
    projection = project_llm_failure(
        LLMDeliveryFailedError(
            stream_error=stream_error,
            invoke_error=invoke_error,
        )
    )
    assert projection is not None

    assert projection.reason_code == "llm_provider_auth_failed"
    assert projection.phase == "invoke"
    assert projection.fallback_state == "failed"
    assert projection.attempts == 2


@pytest.mark.parametrize(
    ("raw_reason", "reason_code", "failure_class", "retryable"),
    [
        (
            "peer_wallclock_exhausted: 901s > 900s",
            "peer_execution_timeout",
            "timeout",
            True,
        ),
        (
            "peer_no_commit_force_bail",
            "peer_iteration_limit_exceeded",
            "limit",
            False,
        ),
        (
            # The runner's own step-limit exhaustion used to fall through to the generic "retry later" failure.
            "subagent exhausted max_iterations=8 without producing a final answer. Last response content: 'x'. tool_invocations=9.",
            "peer_iteration_limit_exceeded",
            "limit",
            False,
        ),
        (
            "peer_total_failures=8; Authorization: Bearer raw-secret",
            "peer_tool_failure_limit_exceeded",
            "limit",
            True,
        ),
        (
            "subagent_empty_answer: provider body raw-secret",
            "peer_empty_result",
            "content",
            True,
        ),
        (
            "tool_exception: provider body raw-secret",
            "peer_execution_failed",
            "runtime",
            True,
        ),
        ("subagent_execution_uncertain", "peer_execution_uncertain", "unknown", False),
    ],
)
def test_chat_peer_failure_builder_projects_only_finite_facts(
    raw_reason: str,
    reason_code: str,
    failure_class: str,
    retryable: bool,
) -> None:
    failure = diagnostics.build_chat_peer_failure(
        reason=raw_reason,
        turn_id="t_peer_safe",
    )

    assert failure.reason_code == reason_code
    assert raw_reason not in str(failure.diagnostics)
    assert failure.diagnostics == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "chat_peer_failed",
        "occurrence_id": failure.occurrence_id,
        "turn_id": "t_peer_safe",
        "safe_detail": f"phase=peer; failure_class={failure_class}",
        "retryable": retryable,
    }


@pytest.mark.parametrize(
    ("raw_reason", "reason_code", "failure_class"),
    [
        ("lock_timeout", "canvas_compose_timeout", "timeout"),
        ("composer_exception", "canvas_compose_failed", "runtime"),
        ("canvas_section_not_found", "canvas_section_not_found", "not_found"),
        (
            "Traceback: Authorization: Bearer raw-secret",
            "canvas_internal_error",
            "internal",
        ),
    ],
)
def test_chat_canvas_failure_builder_projects_only_finite_facts(
    raw_reason: str,
    reason_code: str,
    failure_class: str,
) -> None:
    failure = diagnostics.build_chat_canvas_failure(
        reason=raw_reason,
        turn_id="t_canvas_safe",
    )

    assert failure.reason_code == reason_code
    assert raw_reason not in str(failure.diagnostics)
    assert failure.diagnostics["diagnostic_code"] == "chat_canvas_failed"
    assert failure.diagnostics["turn_id"] == "t_canvas_safe"
    assert failure.diagnostics["safe_detail"] == (f"phase=canvas; failure_class={failure_class}")


def test_chat_turn_failure_carrier_reuses_first_nested_projection() -> None:
    class _Carrier:
        pass

    carrier = _Carrier()
    first = diagnostics.get_or_build_chat_turn_failure(
        carrier=carrier,
        reason_code="lead_internal_error",
        turn_id="t_nested",
    )
    nested_fallback = diagnostics.get_or_build_chat_turn_failure(
        carrier=carrier,
        reason_code="wallclock_exceeded",
        turn_id="t_nested",
    )

    assert nested_fallback is first
    assert nested_fallback.reason_code == "lead_internal_error"
    assert nested_fallback.occurrence_id == first.occurrence_id


def test_chat_turn_failure_unknown_reason_fails_closed_to_internal() -> None:
    secret = "Authorization: Bearer raw-secret"

    failure = diagnostics.build_chat_turn_failure(
        reason_code=secret,
        turn_id="t_safe_123",
    )

    assert failure.reason_code == "lead_internal_error"
    assert secret not in repr(failure)


def test_chat_turn_failure_publisher_rejects_unknown_parent() -> None:
    assert diagnostics.publish_chat_turn_failure("provider said password=secret") is None


@pytest.mark.parametrize(
    ("reason_code", "phase", "failure_class", "retryable"),
    [
        ("E_SKILL_MANIFEST_PARSE", "validate_input", "validation", False),
        ("skill_store_unavailable", "configuration", "configuration", False),
        ("team_skill_not_found", "lookup", "not_found", False),
        ("team_skill_submit_state_conflict", "state_change", "state", False),
        ("workflow_distill_timeout", "distill", "timeout", True),
        ("workflow_dry_run_execution_failed", "execute", "runtime", True),
    ],
)
def test_skill_workflow_publisher_uses_only_finite_authenticated_facts(
    reason_code: str,
    phase: str,
    failure_class: str,
    retryable: bool,
) -> None:
    published = diagnostics.publish_skill_workflow_failure(reason_code)

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "skill_workflow_failure",
        "safe_detail": f"phase={phase}; failure_class={failure_class}",
        "retryable": retryable,
    }


def test_skill_workflow_publisher_rejects_parser_or_bundle_prose() -> None:
    assert (
        diagnostics.publish_skill_workflow_failure(
            "SyntaxError: password=raw-secret",
        )
        is None
    )


@pytest.mark.parametrize(
    ("reason_code", "phase", "failure_class", "retryable"),
    [
        (
            "clarification_store_unavailable",
            "configuration",
            "configuration",
            False,
        ),
        ("clarification_answer_required", "validate_input", "validation", False),
        ("clarification_not_found", "lookup", "not_found", False),
        ("clarification_resume_unavailable", "state_change", "state", False),
        ("clarification_pause_pending", "state_change", "state", True),
        ("clarification_operation_failed", "execute", "runtime", True),
        ("ws_access_denied", "authorize", "access", False),
    ],
)
def test_rule_clarification_publisher_uses_only_finite_authenticated_facts(
    reason_code: str,
    phase: str,
    failure_class: str,
    retryable: bool,
) -> None:
    published = diagnostics.publish_rule_clarification_failure(reason_code)

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "rule_clarification_failure",
        "safe_detail": f"phase={phase}; failure_class={failure_class}",
        "retryable": retryable,
    }


def test_rule_clarification_publisher_rejects_store_or_resume_prose() -> None:
    assert (
        diagnostics.publish_rule_clarification_failure(
            "resume_context.session_id=/srv/private; password=raw-secret",
        )
        is None
    )


def test_request_validation_publisher_uses_only_public_neutral_parent_facts() -> None:
    published = diagnostics.publish_request_validation_failure()

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "public-neutral",
        "diagnostic_code": "request_validation_failure",
        "safe_detail": "phase=validate_input; failure_class=validation",
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("reason_code", "phase", "failure_class", "retryable"),
    [
        ("datarc_connection_auth_rejected", "validate", "access", False),
        ("datarc_connection_unreachable", "validate", "network", True),
        (
            "datarc_connection_secret_storage_unavailable",
            "persist",
            "configuration",
            False,
        ),
        ("datarc_connection_load_failed", "load", "storage", True),
    ],
)
def test_datarc_connection_publisher_uses_only_finite_admin_facts(
    reason_code: str,
    phase: str,
    failure_class: str,
    retryable: bool,
) -> None:
    published = diagnostics.publish_datarc_connection_failure(reason_code)

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "admin",
        "diagnostic_code": "datarc_connection_failure",
        "safe_detail": (f"phase={phase}; failure_class={failure_class}"),
        "retryable": retryable,
    }


def test_datarc_connection_publisher_rejects_provider_prose() -> None:
    assert (
        diagnostics.publish_datarc_connection_failure(
            "Authorization: Bearer raw-secret",
        )
        is None
    )


@pytest.mark.parametrize(
    ("reason_code", "phase", "failure_class", "retryable"),
    [
        ("scheduled_invalid_cron", "validate_input", "validation", False),
        ("scheduled_job_load_failed", "load", "storage", True),
        ("scheduled_run_interrupted", "execute", "interrupted", True),
        ("scheduled_turn_requires_input", "execute", "input", False),
        ("user_credit_exhausted", "authorize", "credit", False),
        ("workspace_archived", "authorize", "state", False),
    ],
)
def test_scheduled_publisher_uses_only_finite_authenticated_facts(
    reason_code: str,
    phase: str,
    failure_class: str,
    retryable: bool,
) -> None:
    published = diagnostics.publish_scheduled_failure(reason_code)

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "scheduled_failure",
        "safe_detail": f"phase={phase}; failure_class={failure_class}",
        "retryable": retryable,
    }


def test_scheduled_publisher_and_normalizer_reject_run_output() -> None:
    secret = "RuntimeError: password=raw-secret"

    assert diagnostics.publish_scheduled_failure(secret) is None
    assert diagnostics.normalize_scheduled_failure_code(secret) == "scheduled_turn_failed"
    assert diagnostics.normalize_scheduled_failure_code(None) is None


# ─── scheduled-recipe-runs S1: five new stable codes ────────────────────────────────────
# normalize fail-closes any unregistered value into `scheduled_turn_failed`, and a code folded that
# way loses its skipped-vs-failed meaning (a skipped code read as failed bumps failure_count and
# trips the 3-strike auto-pause). Idempotence under normalize is therefore the registration proof.
_SCHEDULED_RECIPE_CODES = (
    ("scheduled_input_timeout", "failed"),
    ("scheduled_input_superseded", "skipped"),
    ("scheduled_recipe_lost", "skipped"),
    ("scheduled_recipe_changed", "skipped"),
    ("scheduled_recipe_rejected", "skipped"),
)


@pytest.mark.parametrize(("reason_code", "run_status"), _SCHEDULED_RECIPE_CODES)
def test_scheduled_recipe_code_is_registered_and_publishable(
    reason_code: str,
    run_status: str,
) -> None:
    assert diagnostics.normalize_scheduled_failure_code(reason_code) == reason_code
    published = diagnostics.publish_scheduled_failure(reason_code)
    assert published is not None
    wire = published.to_wire()
    assert wire["diagnostic_code"] == "scheduled_failure"
    assert wire["disclosure"] == "authenticated"
    assert reason_code not in str(wire["safe_detail"])


@pytest.mark.parametrize(("reason_code", "run_status"), _SCHEDULED_RECIPE_CODES)
def test_scheduled_recipe_code_classifies_into_the_intended_run_status(
    reason_code: str,
    run_status: str,
) -> None:
    from bowhead_core.scheduler_loop import classify_run_status

    # A recipe/awaiting failure never carries a turn_id: the run either never started a turn
    # (binding refused) or its turn is being abandoned.
    assert classify_run_status(None, reason_code) == run_status
    assert (reason_code in diagnostics.SCHEDULED_SKIPPED_CODES) is (run_status == "skipped")


def test_paused_team_model_code_is_no_longer_folded_into_the_generic_failure() -> None:
    # PRD §10#3: pausing a routine for a missing team model used to return the ad-hoc literal
    # `team_model_unconfigured:paused`, which was in no word list — normalize folded it into
    # `scheduled_turn_failed`, so the run row could never say why it stopped. The producer now
    # returns a proper stable code; this asserts both the registration AND that the old
    # colon-suffixed literal is gone (a stray producer would re-open the fold).
    code = "scheduled_team_model_unconfigured"

    assert diagnostics.normalize_scheduled_failure_code("team_model_unconfigured:paused") == "scheduled_turn_failed"

    assert diagnostics.normalize_scheduled_failure_code(code) == code
    published = diagnostics.publish_scheduled_failure(code)
    assert published is not None
    assert published.to_wire()["safe_detail"] == "phase=prepare; failure_class=configuration"
    # Still a hard failure, not a skip — registration must not silently flip the run status.
    assert code not in diagnostics.SCHEDULED_SKIPPED_CODES


@pytest.mark.parametrize(
    ("reason_code", "phase", "failure_class", "retryable"),
    [
        ("pbi_connection_discovery_unreachable", "discover", "network", True),
        ("pbi_connection_secret_storage_unavailable", "persist", "configuration", False),
        ("pbi_connection_create_failed", "persist", "storage", True),
        (
            "pbi_connection_credential_update_failed",
            "persist_credential",
            "storage",
            True,
        ),
        ("pbi_connection_auth_rejected", "discover", "access", False),
    ],
)
def test_powerbi_admin_publisher_uses_only_finite_facts(
    reason_code: str,
    phase: str,
    failure_class: str,
    retryable: bool,
) -> None:
    published = diagnostics.publish_powerbi_admin_failure(reason_code)

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "admin",
        "diagnostic_code": "powerbi_admin_failure",
        "safe_detail": f"phase={phase}; failure_class={failure_class}",
        "retryable": retryable,
    }


@pytest.mark.parametrize(
    ("reason_code", "phase", "failure_class", "retryable"),
    [
        ("pbi_connection_unavailable", "prepare", "state", False),
        ("pbi_query_unreachable", "execute", "network", True),
        ("pbi_query_result_incomplete", "parse", "limit", False),
        ("pbi_token_refresh_rejected", "refresh_token", "access", False),
    ],
)
def test_powerbi_query_publisher_uses_only_finite_facts(
    reason_code: str,
    phase: str,
    failure_class: str,
    retryable: bool,
) -> None:
    published = diagnostics.publish_powerbi_query_failure(reason_code)

    assert published is not None
    assert published.to_wire() == {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "powerbi_query_failure",
        "safe_detail": f"phase={phase}; failure_class={failure_class}",
        "retryable": retryable,
    }


def test_powerbi_publishers_reject_provider_prose() -> None:
    secret = "Authorization: Bearer raw-provider-token"

    assert diagnostics.publish_powerbi_admin_failure(secret) is None
    assert diagnostics.publish_powerbi_query_failure(secret) is None


@pytest.mark.parametrize(
    "unsafe_detail",
    [
        "Authorization: Bearer secret",
        "password=hunter2",
        "https://user:pass@internal.example/path?token=secret",
        "/srv/bowhead/private/config.yaml",
        "SELECT * FROM users",
        "Traceback (most recent call last):",
        "<!doctype html><html>upstream</html>",
        "line one\nline two",
        "control\x00character",
        "x" * 241,
    ],
)
def test_private_renderer_drops_the_whole_diagnostic_for_unsafe_detail(
    unsafe_detail: str,
) -> None:
    metrics.get_registry().reset()

    published = diagnostics._render_registered_diagnostic(  # noqa: SLF001
        diagnostic_code="datarc_connection_failure",
        parent_code="datarc_connection_unreachable",
        safe_detail=unsafe_detail,
        retryable=False,
    )

    assert published is None
    rendered_metrics = metrics.get_registry().render_prometheus()
    assert "bowhead_published_diagnostic_dropped_total" in rendered_metrics
    assert unsafe_detail not in rendered_metrics


def test_private_renderer_rejects_a_parent_code_outside_policy() -> None:
    published = diagnostics._render_registered_diagnostic(  # noqa: SLF001
        diagnostic_code="datarc_connection_failure",
        parent_code="account_unavailable",
        safe_detail=None,
        retryable=False,
    )

    assert published is None


def test_runtime_policy_and_documented_policy_have_identical_code_sets() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    catalog_doc = (repo_root / "docs" / "user-facing-error-catalog.md").read_text(
        encoding="utf-8",
    )
    table = catalog_doc.split(
        "<!-- ERROR_DIAGNOSTIC_POLICY_TABLE_START -->",
        maxsplit=1,
    )[1].split(
        "<!-- ERROR_DIAGNOSTIC_POLICY_TABLE_END -->",
        maxsplit=1,
    )[0]
    documented: dict[str, tuple[str, str, frozenset[str], tuple[str, ...], int]] = {}
    for line in table.splitlines():
        if not line.startswith("| `"):
            continue
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        code = columns[0].strip("`")
        documented[code] = (
            columns[1],
            columns[2].strip("`"),
            frozenset(item.strip().strip("`") for item in columns[3].split(",") if item.strip()),
            tuple(item.strip().strip("`") for item in columns[4].split(",") if item.strip()),
            int(columns[5]),
        )

    runtime = {
        code: (
            policy.disclosure.value,
            policy.publisher_name,
            policy.allowed_parent_codes,
            policy.facts,
            policy.max_length,
        )
        for code, policy in diagnostics.DIAGNOSTIC_POLICIES.items()
    }
    assert documented == runtime
    publisher_names = [policy.publisher_name for policy in diagnostics.DIAGNOSTIC_POLICIES.values()]
    assert len(publisher_names) == len(set(publisher_names))
    for code, policy in diagnostics.DIAGNOSTIC_POLICIES.items():
        assert policy.diagnostic_code == code
        publisher = getattr(diagnostics, policy.publisher_name)
        assert callable(publisher)
        parameters = tuple(inspect.signature(publisher).parameters)
        if publisher in {diagnostics.publish_chat_turn_failure, diagnostics.publish_rule_clarification_failure}:
            assert parameters == ("reason_code", "clarification_stage")
            stage = inspect.signature(publisher).parameters["clarification_stage"]
            assert stage.kind is inspect.Parameter.KEYWORD_ONLY and stage.default is None
        elif publisher in {
            diagnostics.publish_chat_canvas_failure,
            diagnostics.publish_chat_peer_failure,
            diagnostics.publish_datarc_connection_failure,
            diagnostics.publish_powerbi_admin_failure,
            diagnostics.publish_powerbi_query_failure,
            diagnostics.publish_scheduled_failure,
            diagnostics.publish_skill_workflow_failure,
        }:
            assert parameters == ("reason_code",)
        elif publisher is diagnostics.publish_chat_llm_failure:
            assert parameters == ("projection",)
        else:
            assert not parameters


def test_business_modules_cannot_import_or_call_the_private_renderer() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "packages" / "bowhead_core" / "src" / "bowhead_core" / "published_diagnostics.py"
    violations: list[str] = []
    for path in (repo_root / "packages").glob("*/src/**/*.py"):
        if path == module_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bowhead_core.published_diagnostics":
                if any(alias.name.startswith("_") for alias in node.names):
                    violations.append(f"{path}:{node.lineno}: private import")
            if isinstance(node, ast.Call):
                called = node.func
                if isinstance(called, ast.Name) and called.id == "_render_registered_diagnostic":
                    violations.append(f"{path}:{node.lineno}: private call")
                if isinstance(called, ast.Attribute) and called.attr == "_render_registered_diagnostic":
                    violations.append(f"{path}:{node.lineno}: private call")

    assert violations == []


def _rejected_by_provider(
    *,
    error_type: str | None = "invalid_request_error",
    code: str | None = "unsupported_value",
    param: str | None = "temperature",
) -> InvocationTargetsExhaustedError:
    """Create a structured rejection for a synthetic non-streaming provider."""

    return InvocationTargetsExhaustedError(
        [
            LLMTargetFailure(
                provider="private-configured-name",
                model="private-model-name",
                provider_family="openai_compatible",
                mode="invoke",
                target_index=0,
                attempt=1,
                kind=ProviderFailureKind.REQUEST_REJECTED,
                retryable=False,
                status_code=400,
                upstream_error_type=error_type,
                upstream_error_code=code,
                upstream_error_param=param,
                upstream_message="Unsupported value: 'temperature' does not support 0.7 with this model.",
            )
        ]
    )


def test_llm_failure_detail_names_the_rejected_parameter() -> None:
    """Provider rejection details identify the unsupported request parameter."""
    projection = project_llm_failure(_rejected_by_provider())
    assert projection is not None

    published = diagnostics.publish_chat_llm_failure(projection)

    assert published is not None, "带 provider 指认的诊断不得被边界丢掉"
    assert published.safe_detail == (
        "phase=invoke; failure_class=request; provider_family=openai_compatible; "
        "http_status=400; stream_state=not_started; fallback=not_attempted; attempts=1; "
        "upstream_type=invalid_request_error; upstream_code=unsupported_value; "
        "upstream_param=temperature"
    )
    assert published.to_wire()["provider_message"] == "Unsupported value: 'temperature' does not support 0.7 with this model."


def test_chat_llm_failure_omits_unsafe_provider_message_without_losing_diagnostics() -> None:
    projection = project_llm_failure(StreamingTargetsExhaustedError([_llm_target_failure(kind=ProviderFailureKind.UPSTREAM, status_code=500)]))
    assert projection is not None

    published = diagnostics.publish_chat_llm_failure(replace(projection, upstream_message="See https://example.test/?api_key=secret"))

    assert published is not None
    assert "provider_message" not in published.to_wire()
    assert "http_status=500" in published.to_wire()["safe_detail"]


def test_llm_failure_detail_omits_upstream_fields_when_provider_did_not_say() -> None:
    """负对照：provider 没指认时不得出现空字段。

    打出 `upstream_param=none` 会让读者以为「provider 说了 none」——
    「没这个字段」与「没指认」必须是同一件事。
    """
    projection = project_llm_failure(
        _rejected_by_provider(error_type=None, code=None, param=None),
    )
    assert projection is not None

    published = diagnostics.publish_chat_llm_failure(projection)

    assert published is not None
    assert "upstream_" not in published.safe_detail


@pytest.mark.parametrize(
    "hostile",
    [
        {"error_type": "see https://evil.example.com/leak"},
        {"param": "<script>alert(1)</script>"},
        {"code": "/etc/passwd"},
        {"param": "x" * 400},
    ],
)
def test_hostile_upstream_values_lose_the_field_not_the_whole_diagnostic(
    hostile: dict[str, str],
) -> None:
    """★ 上游正文是不可信输入：坏值只让**那个字段**消失。

    如果坏值能一路走到 `_safe_detail_is_allowed`，整条诊断会被判 `unsafe_detail`
    丢掉 —— 连 phase / http_status 这些本来就有、本来就安全的事实一起赔进去，
    页面反而比不接这个功能之前更瞎。这条钉住「降级而非归零」。
    """
    projection = project_llm_failure(_rejected_by_provider(**hostile))
    assert projection is not None

    published = diagnostics.publish_chat_llm_failure(projection)

    assert published is not None, "坏的 upstream 值不得连累整条诊断"
    assert published.safe_detail is not None
    assert published.safe_detail.startswith("phase=invoke; failure_class=request; provider_family=openai_compatible; http_status=400")
    for fragment in ("evil.example.com", "<script>", "/etc/passwd"):
        assert fragment not in published.safe_detail
    assert len(published.safe_detail) <= diagnostics.DIAGNOSTIC_POLICIES["chat_llm_failed"].max_length


def test_every_public_turn_failure_reason_has_exactly_one_recovery_action() -> None:
    """Chat turn feedback D5 guard: a reason without an action would render a card with no way out.

    Mirrors the ``LLM_REASON_CODES`` allowlist lesson: adding a reason code must also add its action.
    """
    table = diagnostics._CHAT_RECOVERY_ACTION_BY_PARENT

    assert set(table) == diagnostics.PUBLIC_TURN_FAILURE_CODES
    assert set(table.values()) <= diagnostics.RECOVERY_ACTIONS
    assert {"retry", "edit_and_resend", "copy_diagnostics", "new_session"} == diagnostics.RECOVERY_ACTIONS
    for code in diagnostics.PUBLIC_TURN_FAILURE_CODES:
        assert diagnostics.chat_recovery_action(code) == table[code]


@pytest.mark.parametrize(
    ("reason_code", "action"),
    [
        # Decided 2026-10-05 (design §8 Q2): these differ from what ``retryable`` alone would suggest.
        ("wallclock_exceeded", "edit_and_resend"),
        ("access_context_missing", "copy_diagnostics"),
        ("plan_blocked", "edit_and_resend"),
        ("llm_context_overflow", "edit_and_resend"),
        ("lead_internal_error", "retry"),
    ],
)
def test_decided_recovery_actions(reason_code: str, action: str) -> None:
    assert diagnostics.chat_recovery_action(reason_code) == action
    if reason_code in diagnostics.CHAT_TURN_FAILURE_CODES:
        assert diagnostics.build_chat_turn_failure(reason_code=reason_code, turn_id="t_1").recovery_action == action


def test_non_failure_reasons_and_free_form_errors_get_no_recovery_action() -> None:
    for value in ("user_cancel", "clarification_abort", "internal_error", "terminal_projection_not_committed", None, 3):
        assert diagnostics.chat_recovery_action(value) is None
    assert diagnostics.failure_event_reason_code({"error": "wallclock_exceeded"}) is None
    assert diagnostics.failure_event_reason_code({"reason": "wallclock_exceeded"}) == "wallclock_exceeded"


def test_persisted_error_kind_follows_the_public_reason() -> None:
    """D3: a document or internal failure is no longer persisted as an LLM failure.

    The reason itself is kept so the durable error_kind still names the failure-card reason when
    the event tail is unavailable.
    """
    assert diagnostics.chat_failure_error_kind("document_request_rejected") == "document_request_rejected"
    assert diagnostics.chat_failure_error_kind("lead_internal_error") == "lead_internal_error"
    assert diagnostics.chat_failure_error_kind("llm_provider_timeout") == "llm_failed"
    assert diagnostics.chat_failure_error_kind("not_a_reason") == "internal"


@pytest.mark.parametrize("stage", ["clarification_capture", "clarification_resume_preflight"])
def test_clarification_failure_stage_is_finite_and_preserves_parent_policy(stage: str) -> None:
    publisher = diagnostics.publish_chat_turn_failure if stage == "clarification_capture" else diagnostics.publish_rule_clarification_failure
    published = publisher("clarification_resume_unavailable", clarification_stage=stage)
    assert published is not None
    failure_class = "configuration" if stage == "clarification_capture" else "state"
    assert published.to_wire()["safe_detail"] == f"phase={stage}; failure_class={failure_class}"
    assert published.to_wire()["retryable"] is False
    assert publisher("lead_internal_error", clarification_stage=stage) is None
    assert publisher("clarification_resume_unavailable", clarification_stage="Traceback: password=secret") is None


def test_native_capture_stage_survives_nested_projection_without_inference() -> None:
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable

    carrier = OrdinaryContinuationUnavailable(stage="clarification_capture")
    first = diagnostics.get_or_build_chat_turn_failure(carrier=carrier, reason_code=str(carrier), turn_id="t_capture")
    assert first.diagnostics["safe_detail"] == "phase=clarification_capture; failure_class=configuration"
    bus = type("Carrier", (), {})()
    carried = diagnostics.get_or_build_chat_turn_failure(carrier=bus, cause=carrier, reason_code=str(carrier), turn_id="t_bus")
    assert carried.diagnostics["safe_detail"] == first.diagnostics["safe_detail"]
    assert diagnostics.get_or_build_chat_turn_failure(carrier=bus, reason_code="lead_internal_error", turn_id="t_bus") is carried
    assert diagnostics.get_or_build_chat_turn_failure(carrier=carrier, reason_code="lead_internal_error", turn_id="t_capture") is first
    legacy = diagnostics.get_or_build_chat_turn_failure(carrier=OrdinaryContinuationUnavailable(), reason_code=str(carrier), turn_id="t_legacy")
    assert legacy.diagnostics["safe_detail"] == "phase=chat; failure_class=configuration"
    guessed = ValueError("clarification_capture")
    guessed.stage = "clarification_capture"
    fallback = diagnostics.get_or_build_chat_turn_failure(carrier=guessed, reason_code=str(carrier), turn_id="t_guessed")
    assert fallback.diagnostics["safe_detail"] == "phase=chat; failure_class=configuration"


@pytest.mark.parametrize(
    "invalid",
    [
        {"safe_detail": "phase=clarification_capture; failure_class=configuration; password=raw"},
        {"disclosure": "admin"},
        {"diagnostic_code": "rule_clarification_failure"},
        {"schema_version": 2},
    ],
)
def test_clarification_stage_copy_ignores_unpublished_or_extra_facts(invalid: dict[str, object]) -> None:
    native = {
        "schema_version": 1,
        "disclosure": "authenticated",
        "diagnostic_code": "chat_turn_failed",
        "safe_detail": "phase=clarification_capture; failure_class=configuration",
    }
    assert diagnostics.clarification_failure_copy_code("clarification_resume_unavailable", native) == "clarification_capture_unavailable"
    assert diagnostics.clarification_failure_copy_code("lead_internal_error", native) == "lead_internal_error"
    assert (
        diagnostics.clarification_failure_copy_code("clarification_resume_unavailable", {**native, **invalid}) == "clarification_resume_unavailable"
    )


def test_terminal_llm_diagnostics_rescreen_provider_message_without_losing_http_facts() -> None:
    projection = project_llm_failure(
        InvocationTargetsExhaustedError(
            [
                _llm_target_failure(kind=ProviderFailureKind.REQUEST_REJECTED, status_code=400),
            ]
        )
    )
    assert projection is not None
    failure = diagnostics.build_chat_llm_failure(projection=replace(projection, api_surface="responses", outcome_unknown=True), turn_id="t_safe")
    wire = {**failure.diagnostics, "provider_message": "Authorization: Bearer private-key", "private_payload": "never publish"}
    republished = diagnostics.republish_chat_llm_diagnostics(wire, reason_code=failure.reason_code)
    assert republished is not None
    assert "http_status=400" in republished["safe_detail"]
    assert "api_surface=responses" in republished["safe_detail"]
    assert "provider_message" not in republished
    assert "private_payload" not in republished
    assert republished["occurrence_id"] == failure.occurrence_id
    assert diagnostics.republish_chat_llm_diagnostics(wire, reason_code="lead_internal_error") is None


@pytest.mark.parametrize("reason", ["agent_step_limit", "agent_tool_call_limit", "agent_stalled"])
def test_runtime_stop_preserves_bounded_usage_in_the_terminal_projection(reason):
    failure = diagnostics.build_chat_turn_failure(reason_code=reason, turn_id="t_runtime", step_usage={"used": 14, "limit": 14})
    assert failure.reason_code == reason
    assert failure.diagnostics["step_usage"] == {"used": 14, "limit": 14}
    assert failure.diagnostics["retryable"] is False
    assert diagnostics.chat_recovery_action(reason) == "edit_and_resend"
    invalid = diagnostics.build_chat_turn_failure(reason_code=reason, turn_id="t_runtime", step_usage={"used": True, "limit": 14})
    assert "step_usage" not in invalid.diagnostics
