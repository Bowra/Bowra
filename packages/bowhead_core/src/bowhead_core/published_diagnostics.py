"""Typed publication boundary for user-visible diagnostic facts.

Business modules may call only the named public publishers below. The generic
renderer is deliberately module-private and rejects an entire diagnostic when
its policy, parent code, or safe-detail validation fails.
"""

from __future__ import annotations

import contextlib
import logging
import re
import types
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Literal, get_args

from bowhead_core import metrics
from bowhead_core.llm_failure import LLM_REASON_CODES, LLMFailureProjection
from bowhead_core.scrub import scrub as scrub_credentials

logger = logging.getLogger(__name__)

_CONTROL_OR_MARKUP_RE = re.compile(r"[\x00-\x1f\x7f<>]")
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://")
_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[ =])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*")
_INTERNAL_TEXT_RE = re.compile(
    r"(?i)\b(?:traceback|select\s+.+\s+from|insert\s+into|update\s+.+\s+set|delete\s+from)\b",
)
_OTHER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LLM_FAILURE_PHASES = frozenset({"stream", "invoke"})
_LLM_FAILURE_CLASSES = frozenset(
    {
        "access",
        "rate_limit",
        "timeout",
        "request",
        "response",
        "network",
        "upstream",
        "interrupted",
    }
)
_LLM_PROVIDER_FAMILIES = frozenset(
    {
        "anthropic_compatible",
        "custom",
        "mock",
        "openai_compatible",
    }
)
_LLM_STREAM_STATES = frozenset({"not_started", "pre_visible", "interrupted"})
_LLM_FALLBACK_STATES = frozenset({"not_attempted", "succeeded", "failed"})

CHAT_TURN_FAILURE_CODES = frozenset(
    {
        "access_context_missing",
        "canvas_payload_invalid",
        "canvas_section_not_found",
        "cost_cap_exceeded",
        "clarification_resume_unavailable",
        "document_information_incomplete",
        "document_history_unavailable",
        "document_request_rejected",
        "document_routing_invalid",
        "document_routing_uncertain",
        "document_routing_source_unavailable",
        "document_planning_failed",
        "entitlement_denied",
        "hop_cap_exceeded",
        "lead_internal_error",
        "lead_empty_answer",
        "lead_output_exhausted",
        "reference_policy_changed",
        "reference_continuation_unavailable",
        "reference_primary_incomplete",
        "no_safe_fields_changed",
        "peer_depth_exceeded",
        "peer_dispatch_network_error",
        "peer_dispatch_timeout",
        "plan_blocked",
        "plan_execution_failed",
        "plan_runtime_unavailable",
        # Turn running during a service update/restart (dispatch owner process vanished), not a system logic error.
        "service_interrupted",
        "session_deleted",
        "single_write_leaf_violation",
        # Turn blocked by quota admission (steer continuation child blocked after parent settlement, recorded as failed).
        "team_pool_exhausted",
        "token_cap_exhausted",
        "user_credit_exhausted",
        "wallclock_exceeded",
    }
)

CHAT_PEER_FAILURE_CODES = frozenset(
    {
        "peer_configuration_error",
        "peer_empty_result",
        "peer_execution_failed",
        "peer_execution_uncertain",
        "peer_execution_timeout",
        "peer_internal_error",
        "peer_iteration_limit_exceeded",
        "peer_tool_failure_limit_exceeded",
        "peer_tool_call_limit_exceeded",
        "peer_credit_exhausted",
    }
)

_CHAT_PEER_FAILURE_FACTS_BY_PARENT = types.MappingProxyType(
    {
        "peer_configuration_error": ("configuration", False),
        "peer_empty_result": ("content", True),
        "peer_execution_failed": ("runtime", True),
        "peer_execution_uncertain": ("unknown", False),
        "peer_execution_timeout": ("timeout", True),
        "peer_internal_error": ("internal", True),
        "peer_iteration_limit_exceeded": ("limit", False),
        "peer_tool_failure_limit_exceeded": ("limit", True),
        "peer_tool_call_limit_exceeded": ("limit", False),
        "peer_credit_exhausted": ("limit", False),
    }
)

CHAT_CANVAS_FAILURE_CODES = frozenset(
    {
        "canvas_compose_failed",
        "canvas_compose_timeout",
        "canvas_internal_error",
        "canvas_payload_invalid",
        "canvas_section_not_found",
        "no_safe_fields_changed",
    }
)

_CHAT_CANVAS_FAILURE_FACTS_BY_PARENT = types.MappingProxyType(
    {
        "canvas_compose_failed": ("runtime", True),
        "canvas_compose_timeout": ("timeout", True),
        "canvas_internal_error": ("internal", True),
        "canvas_payload_invalid": ("content", False),
        "canvas_section_not_found": ("not_found", True),
        "no_safe_fields_changed": ("content", False),
    }
)

REQUEST_VALIDATION_FAILURE_CODES = frozenset({"request_validation_failed"})

_SKILL_VALIDATION_CODES = frozenset(
    {
        "E_SKILL_BINARY_NOT_ALLOWED",
        "E_SKILL_BODY_EMPTY",
        "E_SKILL_DESCRIPTION_TOO_LONG",
        "E_SKILL_FILE_TYPE_UNSUPPORTED",
        "E_SKILL_INVALID_ZIP",
        "E_SKILL_INVALID_ZIP_PATH",
        "E_SKILL_LIMIT_REACHED",
        "E_SKILL_MANIFEST_NOT_CLOSED",
        "E_SKILL_MANIFEST_NOT_FOUND_IN_ZIP",
        "E_SKILL_MANIFEST_NOT_OBJECT",
        "E_SKILL_MANIFEST_PARSE",
        "E_SKILL_MANIFEST_TOO_LARGE",
        "E_SKILL_MISSING_DESCRIPTION",
        "E_SKILL_MISSING_MANIFEST",
        "E_SKILL_NAME_EXISTS",
        "E_SKILL_NAME_INVALID",
        "E_SKILL_NAME_MISMATCH",
        "E_SKILL_READ_ONLY_ENTRY_INVALID",
        "E_SKILL_RESERVED_NAME",
        "E_SKILL_SCRIPT_EXTENSION_INVALID",
        "E_SKILL_SCRIPT_INVALID",
        "E_SKILL_SCRIPT_NAME_INVALID",
        "E_SKILL_SCRIPT_NOT_PY",
        "E_SKILL_SCRIPT_SYNTAX_INVALID",
        "E_SKILL_SCRIPT_TOO_LARGE",
        "E_SKILL_SCRIPTS_NOT_LIST",
        "E_SKILL_SHEBANG_MISMATCH",
        "E_SKILL_STORAGE_LIMIT_REACHED",
        "E_SKILL_SYMLINK_NOT_ALLOWED",
        "E_SKILL_TOO_LARGE",
        "E_SKILL_ZIP_TOO_MANY_ENTRIES",
        "request_validation_failed",
        "skill_body_name_immutable",
        "skill_bulk_limit_exceeded",
        "skill_file_kind_invalid",
        "skill_filename_invalid",
        "skill_file_exists",
        "skill_file_authoring_disabled",
        "skill_upload_empty",
    }
)

_SKILL_ACCESS_CODES = frozenset(
    {
        "resource_grant_denied",
        "team_required",
        "team_skill_admin_required",
        "team_skill_audience_admin_required",
        "team_skill_copy_requires_approval",
        "team_skill_delete_forbidden",
        "team_skill_modify_forbidden",
        "team_skill_withdraw_forbidden",
        "skill_not_editable",
        "ws_access_denied",
        # Workflow dry runs resolve the caller's Datarc member like chat does.
        "datarc_identity_required",
        "datarc_member_not_found",
    }
)

_SKILL_CONFIGURATION_CODES = frozenset(
    {
        "resource_usage_not_enabled",
        "access_control_store_unavailable",
        "authz_lookup_failed",
        "datarc_unavailable",
        "skill_catalog_unavailable",
        "skill_store_unavailable",
        "user_store_unavailable",
        "workflow_dry_run_judge_unavailable",
        "workflow_dry_run_orchestrator_unavailable",
        "workflow_store_unavailable",
    }
)

_SKILL_NOT_FOUND_CODES = frozenset(
    {
        "skill_file_not_found",
        "skill_not_found",
        "team_skill_not_found",
        "workflow_turn_not_found",
    }
)

_SKILL_STATE_CODES = frozenset(
    {
        "resource_impact_required",
        "resource_delete_has_live_dependencies",
        "E_SKILL_APPROVAL_REQUIRED",
        "E_SKILL_CANDIDATE_STALE",
        "E_SKILL_NAME_TAKEN_BY_OTHER",
        "managed_skill_delete_forbidden",
        "skill_name_conflict",
        "skill_write_conflict",
        "team_skill_bundle_incomplete",
        "team_skill_bundle_path_invalid",
        "team_skill_candidate_incomplete",
        "team_skill_candidate_invalid",
        "team_skill_candidate_state_conflict",
        "team_skill_create_conflict",
        "team_skill_description_governed",
        "team_skill_content_admin_required",
        "team_skill_metadata_admin_required",
        "team_skill_nothing_to_approve",
        "team_skill_nothing_to_reject",
        "team_skill_rejected_candidate",
        "team_skill_resubmit_state_conflict",
        "skill_promote_content_stale",
        # skill-import batch 1: pending imports with inventory errors cannot be shared/activated; lifecycle state, not authorization.
        "skill_import_pending",
        "skill_import_not_pending",
        "team_skill_submit_state_conflict",
        "team_skill_withdraw_state_conflict",
        "team_skill_preference_delete_forbidden",
        "workflow_creator_disabled",
    }
)

_SKILL_RUNTIME_CODES = frozenset(
    {
        "resource_usage_unavailable",
        "resource_impact_projection_incomplete",
        "datarc_member_resolve_failed",
        "skill_bundle_invalid",
        "skill_copy_failed",
        "workflow_distill_failed",
        "workflow_distill_response_invalid",
        "workflow_dry_run_execution_failed",
        "workflow_dry_run_judge_failed",
    }
)

_SKILL_LIMIT_CODES = frozenset(
    {
        "skill_file_too_large",
        "workflow_distill_rate_limited",
        "workflow_dry_run_rate_limited",
        "workflow_session_too_large",
    }
)

_SKILL_INPUT_CODES = frozenset(
    {
        "resource_command_invalid",
        "missing_workspace_context",
        "team_skill_status_invalid",
        "team_skill_visibility_invalid",
    }
)

_SKILL_CONTENT_CODES = frozenset(
    {
        "skill_file_not_text",
    }
)

_SKILL_TIMEOUT_CODES = frozenset(
    {
        "workflow_distill_timeout",
    }
)

_SKILL_WORKFLOW_FAILURE_FACTS_BY_PARENT = types.MappingProxyType(
    {
        **dict.fromkeys(_SKILL_VALIDATION_CODES | _SKILL_INPUT_CODES, ("validate_input", "validation", False)),
        **dict.fromkeys(_SKILL_ACCESS_CODES, ("authorize", "access", False)),
        **dict.fromkeys(_SKILL_CONFIGURATION_CODES, ("configuration", "configuration", False)),
        **dict.fromkeys(_SKILL_NOT_FOUND_CODES, ("lookup", "not_found", False)),
        **dict.fromkeys(_SKILL_STATE_CODES, ("state_change", "state", False)),
        **dict.fromkeys(_SKILL_RUNTIME_CODES, ("execute", "runtime", True)),
        **dict.fromkeys(_SKILL_LIMIT_CODES, ("validate_input", "limit", False)),
        **dict.fromkeys(_SKILL_CONTENT_CODES, ("preview", "content", False)),
        **dict.fromkeys(_SKILL_TIMEOUT_CODES, ("distill", "timeout", True)),
        "workflow_distill_router_unavailable": (
            "distill",
            "configuration",
            True,
        ),
        # Workflow distillation/dry runs use the same credit admission as chat/send (credit_admission).
        "team_pool_exhausted": ("authorize", "credit", False),
        "user_credit_exhausted": ("authorize", "credit", False),
    }
)

SKILL_WORKFLOW_FAILURE_CODES = frozenset(
    _SKILL_WORKFLOW_FAILURE_FACTS_BY_PARENT,
)

_RULE_CLARIFICATION_CONFIGURATION_CODES = frozenset(
    {
        "authz_lookup_failed",
        "authz_unavailable",
        "clarification_orchestrator_unavailable",
        "clarification_store_unavailable",
        "team_model_runtime_unavailable",
        "user_store_unavailable",
    }
)

_RULE_CLARIFICATION_ACCESS_CODES = frozenset(
    {
        "clarification_access_denied",
        "not_workspace_member",
        "user_not_active",
        "workspace_archived",
        "workspace_required",
        "workspace_role_unknown",
        "workspace_unavailable",
        "ws_access_denied",
    }
)

_RULE_CLARIFICATION_INPUT_CODES = frozenset(
    {
        "clarification_answer_required",
        "clarification_cancel_directive_invalid",
        "clarification_input_rejected",
    }
)

_RULE_CLARIFICATION_NOT_FOUND_CODES = frozenset(
    {
        "clarification_not_found",
    }
)

_RULE_CLARIFICATION_STATE_CODES = frozenset(
    {
        "clarification_not_resolvable",
        "clarification_plan_reapprove_required",
        "clarification_resolution_pending",
        "clarification_resume_not_ready",
        "clarification_resume_unavailable",
        "session_deleted",
        "turn_already_terminal",
    }
)

_RULE_CLARIFICATION_FAILURE_FACTS_BY_PARENT = types.MappingProxyType(
    {
        **dict.fromkeys(
            _RULE_CLARIFICATION_CONFIGURATION_CODES,
            ("configuration", "configuration", False),
        ),
        **dict.fromkeys(
            _RULE_CLARIFICATION_ACCESS_CODES,
            ("authorize", "access", False),
        ),
        **dict.fromkeys(
            _RULE_CLARIFICATION_INPUT_CODES,
            ("validate_input", "validation", False),
        ),
        **dict.fromkeys(
            _RULE_CLARIFICATION_NOT_FOUND_CODES,
            ("lookup", "not_found", False),
        ),
        **dict.fromkeys(
            _RULE_CLARIFICATION_STATE_CODES,
            ("state_change", "state", False),
        ),
        "clarification_operation_failed": ("execute", "runtime", True),
        "clarification_pause_pending": ("state_change", "state", True),
        "clarification_receipt_not_durable": ("persist", "storage", True),
        # Clarification continuations use the same credit admission as chat/send (credit_admission).
        "team_pool_exhausted": ("authorize", "credit", False),
        "user_credit_exhausted": ("authorize", "credit", False),
        "terminal_protocol_malformed": (
            "session_lifecycle",
            "integrity",
            False,
        ),
        "decision_source_malformed": (
            "session_lifecycle",
            "integrity",
            False,
        ),
        "terminal_protocol_writer_not_ready": (
            "session_lifecycle",
            "configuration",
            True,
        ),
    }
)

RULE_CLARIFICATION_FAILURE_CODES = frozenset(
    _RULE_CLARIFICATION_FAILURE_FACTS_BY_PARENT,
)

_CHAT_FAILURE_CLASS_BY_PARENT = types.MappingProxyType(
    {
        "access_context_missing": "access",
        "canvas_payload_invalid": "content",
        "canvas_section_not_found": "content",
        "cost_cap_exceeded": "limit",
        "clarification_resume_unavailable": "configuration",
        "document_information_incomplete": "content",
        "document_planning_failed": "runtime",
        "document_history_unavailable": "configuration",
        "document_request_rejected": "content",
        "document_routing_invalid": "runtime",
        "document_routing_uncertain": "content",
        "document_routing_source_unavailable": "access",
        "entitlement_denied": "access",
        "hop_cap_exceeded": "limit",
        "lead_internal_error": "internal",
        "lead_empty_answer": "content",
        "lead_output_exhausted": "limit",
        "reference_policy_changed": "access",
        "reference_continuation_unavailable": "configuration",
        "reference_primary_incomplete": "runtime",
        "no_safe_fields_changed": "content",
        "peer_depth_exceeded": "limit",
        "peer_dispatch_network_error": "network",
        "peer_dispatch_timeout": "timeout",
        "plan_blocked": "plan",
        "plan_execution_failed": "plan",
        "plan_runtime_unavailable": "configuration",
        "service_interrupted": "interrupted",
        "session_deleted": "access",
        "single_write_leaf_violation": "access",
        "team_pool_exhausted": "credit",
        "token_cap_exhausted": "limit",
        "user_credit_exhausted": "credit",
        "wallclock_exceeded": "timeout",
    }
)

_CHAT_RETRYABLE_BY_PARENT = types.MappingProxyType(
    {
        "access_context_missing": True,
        "canvas_payload_invalid": False,
        "canvas_section_not_found": True,
        "cost_cap_exceeded": False,
        "clarification_resume_unavailable": False,
        "document_information_incomplete": False,
        "document_planning_failed": False,
        "document_history_unavailable": False,
        "document_request_rejected": False,
        "document_routing_invalid": True,
        "document_routing_uncertain": False,
        "document_routing_source_unavailable": False,
        "entitlement_denied": False,
        "hop_cap_exceeded": False,
        "lead_internal_error": True,
        "lead_empty_answer": True,
        "lead_output_exhausted": True,
        "reference_policy_changed": False,
        "reference_continuation_unavailable": False,
        "reference_primary_incomplete": False,
        "no_safe_fields_changed": False,
        "peer_depth_exceeded": False,
        "peer_dispatch_network_error": True,
        "peer_dispatch_timeout": True,
        "plan_blocked": True,
        "plan_execution_failed": True,
        "plan_runtime_unavailable": False,
        "service_interrupted": True,
        "session_deleted": False,
        "single_write_leaf_violation": False,
        "team_pool_exhausted": False,
        "token_cap_exhausted": False,
        "user_credit_exhausted": False,
        "wallclock_exceeded": True,
    }
)

#: Public turn-failure reasons that render the failure card: chat reasons plus LLM reasons.
PUBLIC_TURN_FAILURE_CODES = CHAT_TURN_FAILURE_CODES | LLM_REASON_CODES

RecoveryAction = Literal["retry", "edit_and_resend", "copy_diagnostics", "new_session"]
RECOVERY_ACTIONS: frozenset[str] = frozenset(get_args(RecoveryAction))

#: The one primary recovery action the failure card offers per public reason.
#:
#: This is product behaviour, not a disclosure-gated support fact, so it ships next to
#: ``reason_code`` rather than inside ``diagnostics``. It deliberately differs from
#: ``retryable`` ("an identical retry may succeed") where an identical retry is unlikely to help:
#: ``wallclock_exceeded`` and ``llm_stream_deadline_exceeded`` are retryable, but the same request
#: tends to hit the same limit, so the user should narrow it first; ``access_context_missing`` is
#: retryable, but an identical retry cannot restore missing access context; ``plan_blocked`` needs
#: the user to change the request or the plan. ``llm_context_overflow`` needs a new session, which a
#: boolean cannot express. Every code in ``PUBLIC_TURN_FAILURE_CODES`` must have exactly one action.
_CHAT_RECOVERY_ACTION_BY_PARENT: Mapping[str, RecoveryAction] = types.MappingProxyType(
    {
        # retry
        "lead_internal_error": "retry",
        "lead_empty_answer": "retry",
        "lead_output_exhausted": "retry",
        "service_interrupted": "retry",
        "peer_dispatch_network_error": "retry",
        "peer_dispatch_timeout": "retry",
        "canvas_section_not_found": "retry",
        "plan_execution_failed": "retry",
        "llm_provider_rate_limited": "retry",
        "llm_provider_timeout": "retry",
        "llm_provider_unavailable": "retry",
        "llm_response_invalid": "retry",
        "document_routing_invalid": "retry",
        "llm_stream_interrupted": "retry",
        # edit_and_resend
        "wallclock_exceeded": "edit_and_resend",
        "hop_cap_exceeded": "edit_and_resend",
        "peer_depth_exceeded": "edit_and_resend",
        "cost_cap_exceeded": "edit_and_resend",
        "token_cap_exhausted": "edit_and_resend",
        "canvas_payload_invalid": "edit_and_resend",
        "no_safe_fields_changed": "edit_and_resend",
        "document_information_incomplete": "edit_and_resend",
        "document_request_rejected": "edit_and_resend",
        "document_routing_uncertain": "edit_and_resend",
        "document_routing_source_unavailable": "edit_and_resend",
        "reference_policy_changed": "edit_and_resend",
        "reference_continuation_unavailable": "edit_and_resend",
        "reference_primary_incomplete": "edit_and_resend",
        "clarification_resume_unavailable": "edit_and_resend",
        "llm_request_rejected": "edit_and_resend",
        "llm_stream_deadline_exceeded": "edit_and_resend",
        "plan_blocked": "edit_and_resend",
        # copy_diagnostics
        "entitlement_denied": "copy_diagnostics",
        "single_write_leaf_violation": "copy_diagnostics",
        "plan_runtime_unavailable": "copy_diagnostics",
        "document_planning_failed": "copy_diagnostics",
        "user_credit_exhausted": "copy_diagnostics",
        "team_pool_exhausted": "copy_diagnostics",
        "llm_provider_auth_failed": "copy_diagnostics",
        "access_context_missing": "copy_diagnostics",
        # new_session
        "session_deleted": "new_session",
        "document_history_unavailable": "new_session",
        "llm_context_overflow": "new_session",
    }
)


def chat_recovery_action(reason_code: object) -> RecoveryAction | None:
    """Return the finite recovery action for a public turn-failure reason, else ``None``."""

    if not isinstance(reason_code, str):
        return None
    return _CHAT_RECOVERY_ACTION_BY_PARENT.get(reason_code)


def failure_event_reason_code(payload: Mapping[str, Any]) -> str | None:
    """The public failure reason carried by a ``turn_failed`` / failed ``done`` payload.

    Producers spell it ``reason_code`` (done, gateway) or ``reason`` (Lead ``turn_failed``).
    Only members of ``PUBLIC_TURN_FAILURE_CODES`` count; free-form ``error`` strings never do.
    """

    for key in ("reason_code", "reason"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate in PUBLIC_TURN_FAILURE_CODES:
            return candidate
    return None


def chat_failure_error_kind(reason_code: str) -> str:
    """Persisted ``turns.error_kind`` for a public failure reason.

    A chat reason persists the public reason itself, so a document, plan or internal failure is no
    longer recorded as an LLM failure. The reason (not its coarser failure class) is kept because the
    browser already uses the durable ``error_kind`` as the failure-card reason when a failed Turn's
    event tail is unavailable (``resolveHydratedTurnError``); a class value would degrade that card to
    ``lead_internal_error``. LLM reasons keep ``llm_failed``, which dispatch recovery and the history
    marker already recognise.
    """

    if reason_code in LLM_REASON_CODES:
        return "llm_failed"
    return reason_code if reason_code in CHAT_TURN_FAILURE_CODES else "internal"


DATARC_CONNECTION_FAILURE_CODES = frozenset(
    {
        "datarc_connection_auth_rejected",
        "datarc_connection_configuration_invalid",
        "datarc_connection_create_failed",
        "datarc_connection_load_failed",
        "datarc_connection_not_found",
        "datarc_connection_secret_storage_unavailable",
        "datarc_connection_store_unavailable",
        "datarc_connection_unreachable",
        "datarc_connection_update_failed",
        "datarc_connection_url_rejected",
        "datarc_connection_validation_failed",
    }
)

_DATARC_CONNECTION_FACTS_BY_PARENT = types.MappingProxyType(
    {
        "datarc_connection_auth_rejected": ("validate", "access", False),
        "datarc_connection_configuration_invalid": (
            "validate",
            "configuration",
            False,
        ),
        "datarc_connection_create_failed": ("persist", "storage", True),
        "datarc_connection_load_failed": ("load", "storage", True),
        "datarc_connection_not_found": ("lookup", "not_found", False),
        "datarc_connection_secret_storage_unavailable": (
            "persist",
            "configuration",
            False,
        ),
        "datarc_connection_store_unavailable": (
            "configuration",
            "configuration",
            False,
        ),
        "datarc_connection_unreachable": ("validate", "network", True),
        "datarc_connection_update_failed": ("persist", "storage", True),
        "datarc_connection_url_rejected": (
            "validate_input",
            "policy",
            False,
        ),
        "datarc_connection_validation_failed": (
            "validate",
            "upstream",
            True,
        ),
    }
)

SCHEDULED_FAILURE_CODES = frozenset(
    {
        "authz_unavailable",
        "authz_lookup_failed",
        "clock_tamper",
        "feature_unlicensed",
        "needs_license",
        "orchestrator_not_ready",
        "over_grace",
        "scheduled_anchor_session_deleted",
        "scheduled_datarc_context_failed",
        "scheduled_input_superseded",
        "scheduled_input_timeout",
        "scheduled_invalid_cron",
        "scheduled_job_create_failed",
        "scheduled_job_delete_failed",
        "scheduled_job_limit_reached",
        "scheduled_job_load_failed",
        "scheduled_job_not_found",
        "scheduled_job_run_active",
        "scheduled_job_update_failed",
        "scheduled_owner_lost_datarc_access",
        "scheduled_public_entry_mismatch",
        "scheduled_recipe_changed",
        "scheduled_recipe_lost",
        "scheduled_recipe_rejected",
        "scheduled_run_conflict",
        "scheduled_run_dispatch_failed",
        "scheduled_run_interrupted",
        "scheduled_runs_load_failed",
        "scheduled_store_unavailable",
        "scheduled_turn_cancelled",
        "scheduled_turn_failed",
        "scheduled_turn_requires_approval",
        "scheduled_turn_requires_input",
        "seat_exceeded",
        "suspended",
        "tamper",
        # `_scheduled_runner` returns this literal (suffix included) when a routine is paused for a
        # missing team model. Registering the exact producer string is what stops
        # `normalize_scheduled_failure_code` from fail-closing it into the generic
        # `scheduled_turn_failed` (PRD §10#3). Keep in sync with the producer if it ever drops the suffix.
        "scheduled_team_model_unconfigured",
        "team_pool_exhausted",
        "user_credit_exhausted",
        "user_not_active",
        "workspace_archived",
        "workspace_required",
        "workspace_role_unknown",
        "workspace_unavailable",
        "ws_access_denied",
    }
)

SCHEDULED_SKIPPED_CODES = frozenset(
    {
        "clock_tamper",
        "feature_unlicensed",
        "needs_license",
        "over_grace",
        # Recipe-binding outcomes are `skipped`, not `failed`: the routine is paused (or the run was
        # replaced by an owner-initiated one), so bumping failure_count would double-punish and trip
        # the 3-strike auto-pause on top of an already-paused job.
        "scheduled_input_superseded",
        "scheduled_recipe_changed",
        "scheduled_recipe_lost",
        "scheduled_recipe_rejected",
        "seat_exceeded",
        "suspended",
        "tamper",
        "team_pool_exhausted",
        "user_credit_exhausted",
    }
)

POWERBI_ADMIN_FAILURE_CODES = frozenset(
    {
        "pbi_connection_archive_failed",
        "pbi_connection_auth_rejected",
        "pbi_connection_access_denied",
        "pbi_connection_create_failed",
        "pbi_connection_credential_unavailable",
        "pbi_connection_credential_update_failed",
        "pbi_connection_discovery_failed",
        "pbi_connection_discovery_unreachable",
        "pbi_connection_load_failed",
        "pbi_connection_not_found",
        "pbi_connection_secret_storage_unavailable",
        "pbi_connection_store_unavailable",
    }
)

_POWERBI_ADMIN_FACTS_BY_PARENT = types.MappingProxyType(
    {
        "pbi_connection_archive_failed": ("archive", "storage", True),
        "pbi_connection_auth_rejected": ("discover", "access", False),
        "pbi_connection_access_denied": ("discover", "access", False),
        "pbi_connection_create_failed": ("persist", "storage", True),
        "pbi_connection_credential_unavailable": (
            "load_credential",
            "configuration",
            False,
        ),
        "pbi_connection_credential_update_failed": (
            "persist_credential",
            "storage",
            True,
        ),
        "pbi_connection_discovery_failed": ("discover", "upstream", True),
        "pbi_connection_discovery_unreachable": (
            "discover",
            "network",
            True,
        ),
        "pbi_connection_load_failed": ("load", "storage", True),
        "pbi_connection_not_found": ("lookup", "not_found", False),
        "pbi_connection_secret_storage_unavailable": (
            "persist",
            "configuration",
            False,
        ),
        "pbi_connection_store_unavailable": (
            "configuration",
            "configuration",
            False,
        ),
    }
)

POWERBI_QUERY_FAILURE_CODES = frozenset(
    {
        "pbi_catalog_unavailable",
        "pbi_connection_credential_unavailable",
        "pbi_connection_credential_update_failed",
        "pbi_connection_load_failed",
        "pbi_connection_unavailable",
        "pbi_query_access_denied",
        "pbi_query_auth_rejected",
        "pbi_query_invalid_request",
        "pbi_query_failed",
        "pbi_query_input_required",
        "pbi_query_not_found",
        "pbi_query_rate_limited",
        "pbi_query_response_invalid",
        "pbi_query_result_incomplete",
        "pbi_query_unreachable",
        "pbi_token_refresh_failed",
        "pbi_token_refresh_rejected",
    }
)

_POWERBI_QUERY_FACTS_BY_PARENT = types.MappingProxyType(
    {
        "pbi_catalog_unavailable": ("load_catalog", "configuration", False),
        "pbi_connection_credential_unavailable": (
            "prepare",
            "configuration",
            False,
        ),
        "pbi_connection_credential_update_failed": (
            "persist_credential",
            "storage",
            True,
        ),
        "pbi_connection_load_failed": ("prepare", "storage", True),
        "pbi_connection_unavailable": ("prepare", "state", False),
        "pbi_query_access_denied": ("execute", "access", False),
        "pbi_query_auth_rejected": ("execute", "access", False),
        "pbi_query_failed": ("execute", "upstream", True),
        "pbi_query_input_required": ("validate_input", "validation", False),
        "pbi_query_rate_limited": ("execute", "limit", True),
        "pbi_query_response_invalid": ("parse", "upstream", True),
        "pbi_query_result_incomplete": ("parse", "limit", False),
        "pbi_query_unreachable": ("execute", "network", True),
        "pbi_token_refresh_failed": ("refresh_token", "network", True),
        "pbi_token_refresh_rejected": ("refresh_token", "access", False),
    }
)

_SCHEDULED_FACTS_BY_PARENT = types.MappingProxyType(
    {
        "authz_unavailable": ("authorize", "configuration", False),
        "authz_lookup_failed": ("authorize", "access", True),
        "clock_tamper": ("authorize", "license", False),
        "feature_unlicensed": ("authorize", "license", False),
        "needs_license": ("authorize", "license", False),
        "orchestrator_not_ready": ("dispatch", "configuration", True),
        "over_grace": ("authorize", "license", False),
        "scheduled_anchor_session_deleted": ("prepare", "state", False),
        "scheduled_datarc_context_failed": ("prepare", "upstream", True),
        "scheduled_input_superseded": ("execute", "cancelled", True),
        "scheduled_input_timeout": ("execute", "timeout", True),
        "scheduled_invalid_cron": ("validate_input", "validation", False),
        "scheduled_job_create_failed": ("create", "storage", True),
        "scheduled_job_delete_failed": ("delete", "storage", True),
        "scheduled_job_limit_reached": ("create", "limit", False),
        "scheduled_job_load_failed": ("load", "storage", True),
        "scheduled_job_not_found": ("lookup", "not_found", False),
        "scheduled_job_run_active": ("delete", "state", True),
        "scheduled_job_update_failed": ("update", "storage", True),
        "scheduled_owner_lost_datarc_access": ("authorize", "access", False),
        "scheduled_public_entry_mismatch": ("create", "policy", False),
        "scheduled_recipe_changed": ("prepare", "state", False),
        "scheduled_recipe_lost": ("prepare", "not_found", False),
        "scheduled_recipe_rejected": ("prepare", "policy", False),
        "scheduled_run_conflict": ("dispatch", "state", True),
        "scheduled_run_dispatch_failed": ("dispatch", "runtime", True),
        "scheduled_run_interrupted": ("execute", "interrupted", True),
        "scheduled_runs_load_failed": ("load_history", "storage", True),
        "scheduled_store_unavailable": ("configuration", "configuration", False),
        "scheduled_turn_cancelled": ("execute", "cancelled", True),
        "scheduled_turn_failed": ("execute", "runtime", True),
        "scheduled_turn_requires_approval": ("execute", "approval", False),
        "scheduled_turn_requires_input": ("execute", "input", False),
        "seat_exceeded": ("authorize", "license", False),
        "suspended": ("authorize", "license", False),
        "tamper": ("authorize", "license", False),
        "scheduled_team_model_unconfigured": ("prepare", "configuration", False),
        "team_pool_exhausted": ("authorize", "credit", False),
        "user_credit_exhausted": ("authorize", "credit", False),
        "user_not_active": ("authorize", "access", False),
        "workspace_archived": ("authorize", "state", False),
        "workspace_required": ("validate_input", "validation", False),
        "workspace_role_unknown": ("authorize", "access", False),
        "workspace_unavailable": ("authorize", "state", False),
        "ws_access_denied": ("authorize", "access", False),
    }
)


class ErrorDisclosure(StrEnum):
    PUBLIC_NEUTRAL = "public-neutral"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"
    PROTOCOL = "protocol"


@dataclass(frozen=True, slots=True)
class DiagnosticPolicy:
    diagnostic_code: str
    disclosure: ErrorDisclosure
    publisher_name: str
    allowed_parent_codes: frozenset[str]
    facts: tuple[str, ...]
    max_length: int = 240


@dataclass(frozen=True, slots=True)
class PublishedDiagnostic:
    diagnostic_code: str
    disclosure: ErrorDisclosure
    safe_detail: str | None = None
    retryable: bool | None = None
    provider_message: str | None = None

    def to_wire(
        self,
        *,
        occurrence_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": 1,
            "disclosure": self.disclosure.value,
            "diagnostic_code": self.diagnostic_code,
        }
        if occurrence_id is not None:
            wire["occurrence_id"] = occurrence_id
        if turn_id is not None:
            wire["turn_id"] = turn_id
        if self.safe_detail is not None:
            wire["safe_detail"] = self.safe_detail
        if self.retryable is not None:
            wire["retryable"] = self.retryable
        if self.provider_message is not None:
            wire["provider_message"] = self.provider_message
        return wire


@dataclass(frozen=True, slots=True)
class ChatTurnFailure:
    """One immutable projection reused by every event for a terminal failure."""

    reason_code: str
    occurrence_id: str
    diagnostics: dict[str, Any]

    @property
    def recovery_action(self) -> str | None:
        """The failure card's primary action; see ``_CHAT_RECOVERY_ACTION_BY_PARENT``."""

        return chat_recovery_action(self.reason_code)


DIAGNOSTIC_POLICIES: Mapping[str, DiagnosticPolicy] = types.MappingProxyType(
    {
        "chat_llm_failed": DiagnosticPolicy(
            diagnostic_code="chat_llm_failed",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_chat_llm_failure",
            allowed_parent_codes=LLM_REASON_CODES,
            facts=(
                "occurrence_id",
                "turn_id",
                "phase=<finite>",
                "failure_class=<finite>",
                "provider_family=<finite>",
                "http_status=<finite>",
                "stream_state=<finite>",
                "fallback=<finite>",
                "attempts=<integer>",
                "retryable=<finite>",
                # Optional provider-reported failure location: accept only **identifiers** in a finite grammar
                # (type / error code / parameter name). Provider prose uses the separately screened field below.
                "upstream_type=<identifier?>",
                "upstream_code=<identifier?>",
                "upstream_param=<identifier?>",
                "provider_message=<redacted text?>",
                "api_surface=<finite?>",
                "outcome=unknown?",
            ),
            # The three fields above lengthen this detail; body safety is enforced by content checks (URL / markup /
            # credentials), not length, so only relax the length here.
            max_length=420,
        ),
        "chat_turn_failed": DiagnosticPolicy(
            diagnostic_code="chat_turn_failed",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_chat_turn_failure",
            allowed_parent_codes=CHAT_TURN_FAILURE_CODES,
            facts=(
                "occurrence_id",
                "turn_id",
                "phase=<finite>",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "chat_peer_failed": DiagnosticPolicy(
            diagnostic_code="chat_peer_failed",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_chat_peer_failure",
            allowed_parent_codes=CHAT_PEER_FAILURE_CODES,
            facts=(
                "occurrence_id",
                "turn_id",
                "phase=peer",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "chat_canvas_failed": DiagnosticPolicy(
            diagnostic_code="chat_canvas_failed",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_chat_canvas_failure",
            allowed_parent_codes=CHAT_CANVAS_FAILURE_CODES,
            facts=(
                "occurrence_id",
                "turn_id",
                "phase=canvas",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "request_validation_failure": DiagnosticPolicy(
            diagnostic_code="request_validation_failure",
            disclosure=ErrorDisclosure.PUBLIC_NEUTRAL,
            publisher_name="publish_request_validation_failure",
            allowed_parent_codes=REQUEST_VALIDATION_FAILURE_CODES,
            facts=(
                "phase=validate_input",
                "failure_class=validation",
                "retryable=false",
            ),
        ),
        "skill_workflow_failure": DiagnosticPolicy(
            diagnostic_code="skill_workflow_failure",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_skill_workflow_failure",
            allowed_parent_codes=SKILL_WORKFLOW_FAILURE_CODES,
            facts=(
                "phase=<finite>",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "rule_clarification_failure": DiagnosticPolicy(
            diagnostic_code="rule_clarification_failure",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_rule_clarification_failure",
            allowed_parent_codes=RULE_CLARIFICATION_FAILURE_CODES,
            facts=(
                "phase=<finite>",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "datarc_connection_failure": DiagnosticPolicy(
            diagnostic_code="datarc_connection_failure",
            disclosure=ErrorDisclosure.ADMIN,
            publisher_name="publish_datarc_connection_failure",
            allowed_parent_codes=DATARC_CONNECTION_FAILURE_CODES,
            facts=(
                "phase=<finite>",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "scheduled_failure": DiagnosticPolicy(
            diagnostic_code="scheduled_failure",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_scheduled_failure",
            allowed_parent_codes=SCHEDULED_FAILURE_CODES,
            facts=(
                "phase=<finite>",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "powerbi_admin_failure": DiagnosticPolicy(
            diagnostic_code="powerbi_admin_failure",
            disclosure=ErrorDisclosure.ADMIN,
            publisher_name="publish_powerbi_admin_failure",
            allowed_parent_codes=POWERBI_ADMIN_FAILURE_CODES,
            facts=(
                "phase=<finite>",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
        "powerbi_query_failure": DiagnosticPolicy(
            diagnostic_code="powerbi_query_failure",
            disclosure=ErrorDisclosure.AUTHENTICATED,
            publisher_name="publish_powerbi_query_failure",
            allowed_parent_codes=POWERBI_QUERY_FAILURE_CODES,
            facts=(
                "phase=<finite>",
                "failure_class=<finite>",
                "retryable=<finite>",
            ),
        ),
    }
)


def _drop(diagnostic_code: str, reason: str) -> None:
    safe_code = diagnostic_code if isinstance(diagnostic_code, str) and diagnostic_code in DIAGNOSTIC_POLICIES else "unregistered"
    logger.warning(
        "published_diagnostic.dropped diagnostic_code=%s reason=%s",
        safe_code,
        reason,
    )
    metrics.inc(
        "bowhead_published_diagnostic_dropped_total",
        labels={"diagnostic_code": safe_code, "reason": reason},
        help="Published diagnostics dropped by the typed safety boundary.",
    )


def _safe_detail_is_allowed(value: object, *, max_length: int) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or candidate != value or len(candidate) > max_length:
        return False
    if _CONTROL_OR_MARKUP_RE.search(candidate):
        return False
    if _URL_RE.search(candidate) or _ABSOLUTE_PATH_RE.search(candidate):
        return False
    if _INTERNAL_TEXT_RE.search(candidate):
        return False
    return scrub_credentials(candidate) == candidate


def _render_registered_diagnostic(
    *,
    diagnostic_code: str,
    parent_code: str,
    safe_detail: str | None,
    retryable: bool | None,
) -> PublishedDiagnostic | None:
    """Private generic renderer; named publishers are the only business API."""

    policy = DIAGNOSTIC_POLICIES.get(diagnostic_code)
    if policy is None:
        _drop(diagnostic_code, "unknown_policy")
        return None
    if parent_code not in policy.allowed_parent_codes:
        _drop(diagnostic_code, "parent_code")
        return None
    if retryable is not None and not isinstance(retryable, bool):
        _drop(diagnostic_code, "retryable_type")
        return None
    if safe_detail is not None and not _safe_detail_is_allowed(
        safe_detail,
        max_length=policy.max_length,
    ):
        _drop(diagnostic_code, "unsafe_detail")
        return None
    published = PublishedDiagnostic(
        diagnostic_code=diagnostic_code,
        disclosure=policy.disclosure,
        safe_detail=safe_detail,
        retryable=retryable,
    )
    metrics.inc(
        "bowhead_published_diagnostic_total",
        labels={
            "diagnostic_code": diagnostic_code,
            "disclosure": policy.disclosure.value,
        },
        help="Diagnostics published through a named typed policy.",
    )
    return published


def publish_chat_turn_failure(
    reason_code: str,
    *,
    clarification_stage: Literal["clarification_capture"] | None = None,
) -> PublishedDiagnostic | None:
    """Publish only finite Chat failure facts; exception text is not accepted."""

    if reason_code not in CHAT_TURN_FAILURE_CODES:
        _drop("chat_turn_failed", "parent_code")
        return None
    if clarification_stage is not None and (reason_code != "clarification_resume_unavailable" or clarification_stage != "clarification_capture"):
        _drop("chat_turn_failed", "clarification_stage")
        return None
    failure_class = _CHAT_FAILURE_CLASS_BY_PARENT[reason_code]
    phase = clarification_stage or "chat"
    return _render_registered_diagnostic(
        diagnostic_code="chat_turn_failed",
        parent_code=reason_code,
        safe_detail=f"phase={phase}; failure_class={failure_class}",
        retryable=_CHAT_RETRYABLE_BY_PARENT[reason_code],
    )


def clarification_failure_copy_code(reason_code: str, diagnostics: object) -> str:
    """Select stage copy only from the named finite diagnostic projection.

    These are presentation keys, never a replacement for the parent code or
    a claim that an answer was stored. Legacy facts keep the original copy.
    """
    if reason_code != "clarification_resume_unavailable" or not isinstance(diagnostics, Mapping):
        return reason_code
    if diagnostics.get("schema_version") != 1 or diagnostics.get("disclosure") != "authenticated":
        return reason_code
    facts = (diagnostics.get("diagnostic_code"), diagnostics.get("safe_detail"))
    if facts == ("chat_turn_failed", "phase=clarification_capture; failure_class=configuration"):
        return "clarification_capture_unavailable"
    if facts == ("rule_clarification_failure", "phase=clarification_resume_preflight; failure_class=state"):
        return "clarification_resume_preflight_unavailable"
    return reason_code


def publish_chat_llm_failure(
    projection: LLMFailureProjection,
) -> PublishedDiagnostic | None:
    """Publish finite LLM facts and only a screened provider message."""

    if projection.reason_code not in LLM_REASON_CODES:
        _drop("chat_llm_failed", "parent_code")
        return None
    status_valid = projection.http_status is None or (type(projection.http_status) is int and 100 <= projection.http_status <= 599)
    attempts_valid = type(projection.attempts) is int and 1 <= projection.attempts <= 99
    if (
        projection.phase not in _LLM_FAILURE_PHASES
        or projection.failure_class not in _LLM_FAILURE_CLASSES
        or projection.provider_family not in _LLM_PROVIDER_FAMILIES
        or projection.stream_state not in _LLM_STREAM_STATES
        or projection.fallback_state not in _LLM_FALLBACK_STATES
        or not status_valid
        or not attempts_valid
        or not isinstance(projection.retryable, bool)
    ):
        _drop("chat_llm_failed", "projection")
        return None
    published = _render_registered_diagnostic(
        diagnostic_code="chat_llm_failed",
        parent_code=projection.reason_code,
        safe_detail=projection.safe_detail,
        retryable=projection.retryable,
    )
    if published is None:
        return None
    message = projection.upstream_message
    if message is None or not _safe_detail_is_allowed(message, max_length=500):
        return published
    return replace(published, provider_message=message)


def republish_chat_llm_diagnostics(
    value: object,
    *,
    reason_code: str,
) -> dict[str, Any] | None:
    """Retain only screened LLM diagnostic fields at the Terminal event boundary."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    if value.get("diagnostic_code") != "chat_llm_failed" or value.get("disclosure") != "authenticated":
        return None
    if value.get("code", reason_code) != reason_code:
        return None
    published = _render_registered_diagnostic(
        diagnostic_code="chat_llm_failed",
        parent_code=reason_code,
        safe_detail=value.get("safe_detail"),
        retryable=value.get("retryable"),
    )
    if published is None:
        return None
    message = value.get("provider_message")
    if _safe_detail_is_allowed(message, max_length=500):
        published = replace(published, provider_message=message)
    occurrence_id = value.get("occurrence_id")
    turn_id = value.get("turn_id")
    wire = published.to_wire(
        occurrence_id=occurrence_id if isinstance(occurrence_id, str) and re.fullmatch(r"[0-9a-f]{32}", occurrence_id) else None,
        turn_id=turn_id if isinstance(turn_id, str) and _OTHER_ID_RE.fullmatch(turn_id) else None,
    )
    wire["code"] = reason_code
    return wire


def normalize_chat_peer_failure_code(reason: object) -> str:
    """Project arbitrary peer errors onto the finite user-visible registry."""

    if not isinstance(reason, str):
        return "peer_execution_failed"
    candidate = reason.strip()
    if candidate in CHAT_PEER_FAILURE_CODES:
        return candidate
    prefix = candidate.partition(":")[0].partition("=")[0].strip()
    if prefix in ("peer_wallclock_exhausted", "peer_llm_deadline_exceeded"):
        return "peer_execution_timeout"
    if prefix in ("peer_no_commit_force_bail", "peer_no_progress", "subagent exhausted max_iterations"):
        return "peer_iteration_limit_exceeded"
    # Agent run limits: a run stopped at a limit (phase 2 partial result).
    if prefix in ("run_limit_step_limit", "run_limit_no_progress"):
        return "peer_iteration_limit_exceeded"
    if prefix == "run_limit_time_limit":
        return "peer_execution_timeout"
    if prefix == "run_limit_tool_call_limit":
        return "peer_tool_call_limit_exceeded"
    if prefix == "run_limit_budget":
        return "peer_credit_exhausted"
    if prefix == "peer_total_failures":
        return "peer_tool_failure_limit_exceeded"
    if prefix == "subagent_empty_answer":
        return "peer_empty_result"
    if prefix == "subagent_execution_uncertain":
        return "peer_execution_uncertain"
    if prefix == "subagent_internal_error":
        return "peer_internal_error"
    if prefix == "unknown profile" or (prefix.startswith("tool '") and "not registered" in candidate):
        return "peer_configuration_error"
    return "peer_execution_failed"


def publish_chat_peer_failure(
    reason_code: str,
) -> PublishedDiagnostic | None:
    """Publish finite peer failure facts without accepting result prose."""

    if reason_code not in CHAT_PEER_FAILURE_CODES:
        _drop("chat_peer_failed", "parent_code")
        return None
    failure_class, retryable = _CHAT_PEER_FAILURE_FACTS_BY_PARENT[reason_code]
    return _render_registered_diagnostic(
        diagnostic_code="chat_peer_failed",
        parent_code=reason_code,
        safe_detail=f"phase=peer; failure_class={failure_class}",
        retryable=retryable,
    )


def normalize_chat_canvas_failure_code(reason: object) -> str:
    """Project historical Canvas reason aliases onto the finite registry."""

    if not isinstance(reason, str):
        return "canvas_internal_error"
    candidate = reason.strip()
    if candidate in CHAT_CANVAS_FAILURE_CODES:
        return candidate
    return {
        "composer_exception": "canvas_compose_failed",
        "lock_timeout": "canvas_compose_timeout",
        "patch_failed": "canvas_internal_error",
    }.get(candidate, "canvas_internal_error")


def publish_chat_canvas_failure(
    reason_code: str,
) -> PublishedDiagnostic | None:
    """Publish finite Canvas failure facts without accepting exception text."""

    if reason_code not in CHAT_CANVAS_FAILURE_CODES:
        _drop("chat_canvas_failed", "parent_code")
        return None
    failure_class, retryable = _CHAT_CANVAS_FAILURE_FACTS_BY_PARENT[reason_code]
    return _render_registered_diagnostic(
        diagnostic_code="chat_canvas_failed",
        parent_code=reason_code,
        safe_detail=f"phase=canvas; failure_class={failure_class}",
        retryable=retryable,
    )


def publish_request_validation_failure() -> PublishedDiagnostic | None:
    """Publish only the finite parent facts for framework validation."""

    return _render_registered_diagnostic(
        diagnostic_code="request_validation_failure",
        parent_code="request_validation_failed",
        safe_detail="phase=validate_input; failure_class=validation",
        retryable=False,
    )


def publish_skill_workflow_failure(
    reason_code: str,
) -> PublishedDiagnostic | None:
    """Publish finite Skills/Workflow facts without parser or bundle prose."""

    if reason_code not in SKILL_WORKFLOW_FAILURE_CODES:
        _drop("skill_workflow_failure", "parent_code")
        return None
    phase, failure_class, retryable = _SKILL_WORKFLOW_FAILURE_FACTS_BY_PARENT[reason_code]
    return _render_registered_diagnostic(
        diagnostic_code="skill_workflow_failure",
        parent_code=reason_code,
        safe_detail=f"phase={phase}; failure_class={failure_class}",
        retryable=retryable,
    )


def publish_rule_clarification_failure(
    reason_code: str,
    *,
    clarification_stage: Literal["clarification_resume_preflight"] | None = None,
) -> PublishedDiagnostic | None:
    """Publish finite Business Rules/Clarification facts without store prose."""

    if reason_code not in RULE_CLARIFICATION_FAILURE_CODES:
        _drop("rule_clarification_failure", "parent_code")
        return None
    if clarification_stage is not None and (
        reason_code != "clarification_resume_unavailable" or clarification_stage != "clarification_resume_preflight"
    ):
        _drop("rule_clarification_failure", "clarification_stage")
        return None
    phase, failure_class, retryable = _RULE_CLARIFICATION_FAILURE_FACTS_BY_PARENT[reason_code]
    phase = clarification_stage or phase
    return _render_registered_diagnostic(
        diagnostic_code="rule_clarification_failure",
        parent_code=reason_code,
        safe_detail=f"phase={phase}; failure_class={failure_class}",
        retryable=retryable,
    )


def publish_datarc_connection_failure(
    reason_code: str,
) -> PublishedDiagnostic | None:
    """Publish finite connection-test facts without accepting provider prose."""

    if reason_code not in DATARC_CONNECTION_FAILURE_CODES:
        _drop("datarc_connection_failure", "parent_code")
        return None
    phase, failure_class, retryable = _DATARC_CONNECTION_FACTS_BY_PARENT[reason_code]
    return _render_registered_diagnostic(
        diagnostic_code="datarc_connection_failure",
        parent_code=reason_code,
        safe_detail=f"phase={phase}; failure_class={failure_class}",
        retryable=retryable,
    )


def normalize_scheduled_failure_code(
    reason_code: object,
) -> str | None:
    """Keep only finite scheduled failure codes; legacy/raw values fail closed."""

    if reason_code is None:
        return None
    if isinstance(reason_code, str) and reason_code in SCHEDULED_FAILURE_CODES:
        return reason_code
    return "scheduled_turn_failed"


def publish_scheduled_failure(
    reason_code: str,
) -> PublishedDiagnostic | None:
    """Publish finite routine lifecycle facts without accepting run output."""

    if reason_code not in SCHEDULED_FAILURE_CODES:
        _drop("scheduled_failure", "parent_code")
        return None
    phase, failure_class, retryable = _SCHEDULED_FACTS_BY_PARENT[reason_code]
    return _render_registered_diagnostic(
        diagnostic_code="scheduled_failure",
        parent_code=reason_code,
        safe_detail=f"phase={phase}; failure_class={failure_class}",
        retryable=retryable,
    )


def publish_powerbi_admin_failure(
    reason_code: str,
) -> PublishedDiagnostic | None:
    """Publish finite Power BI onboarding facts without provider prose."""

    if reason_code not in POWERBI_ADMIN_FAILURE_CODES:
        _drop("powerbi_admin_failure", "parent_code")
        return None
    phase, failure_class, retryable = _POWERBI_ADMIN_FACTS_BY_PARENT[reason_code]
    return _render_registered_diagnostic(
        diagnostic_code="powerbi_admin_failure",
        parent_code=reason_code,
        safe_detail=f"phase={phase}; failure_class={failure_class}",
        retryable=retryable,
    )


def publish_powerbi_query_failure(
    reason_code: str,
) -> PublishedDiagnostic | None:
    """Publish finite Power BI query facts without DAX or response bodies."""

    if reason_code not in POWERBI_QUERY_FAILURE_CODES:
        _drop("powerbi_query_failure", "parent_code")
        return None
    phase, failure_class, retryable = _POWERBI_QUERY_FACTS_BY_PARENT[reason_code]
    return _render_registered_diagnostic(
        diagnostic_code="powerbi_query_failure",
        parent_code=reason_code,
        safe_detail=f"phase={phase}; failure_class={failure_class}",
        retryable=retryable,
    )


def build_chat_turn_failure(
    *,
    reason_code: str,
    turn_id: str,
    clarification_stage: Literal["clarification_capture"] | None = None,
) -> ChatTurnFailure:
    """Create the one correlation object shared by turn_failed and failed done."""

    safe_reason = reason_code if reason_code in CHAT_TURN_FAILURE_CODES else "lead_internal_error"
    occurrence_id = uuid.uuid4().hex
    published = publish_chat_turn_failure(safe_reason, clarification_stage=clarification_stage)
    safe_turn_id = turn_id if _OTHER_ID_RE.fullmatch(turn_id) else None
    diagnostics = (
        published.to_wire(
            occurrence_id=occurrence_id,
            turn_id=safe_turn_id,
        )
        if published is not None
        else {}
    )
    return ChatTurnFailure(
        reason_code=safe_reason,
        occurrence_id=occurrence_id,
        diagnostics=diagnostics,
    )


def build_chat_llm_failure(
    *,
    projection: LLMFailureProjection,
    turn_id: str,
) -> ChatTurnFailure:
    """Create one correlated, safe LLM terminal failure projection."""

    occurrence_id = uuid.uuid4().hex
    published = publish_chat_llm_failure(projection)
    safe_turn_id = turn_id if _OTHER_ID_RE.fullmatch(turn_id) else None
    diagnostics = (
        published.to_wire(
            occurrence_id=occurrence_id,
            turn_id=safe_turn_id,
        )
        if published is not None
        else {}
    )
    if diagnostics:
        diagnostics["code"] = projection.reason_code
    return ChatTurnFailure(
        reason_code=projection.reason_code,
        occurrence_id=occurrence_id,
        diagnostics=diagnostics,
    )


def _build_chat_scoped_failure(
    *,
    reason_code: str,
    turn_id: str,
    publisher: Callable[[str], PublishedDiagnostic | None],
) -> ChatTurnFailure:
    occurrence_id = uuid.uuid4().hex
    published = publisher(reason_code)
    safe_turn_id = turn_id if _OTHER_ID_RE.fullmatch(turn_id) else None
    diagnostics = (
        published.to_wire(
            occurrence_id=occurrence_id,
            turn_id=safe_turn_id,
        )
        if published is not None
        else {}
    )
    return ChatTurnFailure(
        reason_code=reason_code,
        occurrence_id=occurrence_id,
        diagnostics=diagnostics,
    )


def build_chat_peer_failure(
    *,
    reason: object,
    turn_id: str,
) -> ChatTurnFailure:
    """Build the immutable peer projection reused by both peer events."""

    reason_code = normalize_chat_peer_failure_code(reason)
    return _build_chat_scoped_failure(
        reason_code=reason_code,
        turn_id=turn_id,
        publisher=publish_chat_peer_failure,
    )


def build_chat_canvas_failure(
    *,
    reason: object,
    turn_id: str,
) -> ChatTurnFailure:
    """Build a safe Canvas failure projection for one replayable event."""

    reason_code = normalize_chat_canvas_failure_code(reason)
    return _build_chat_scoped_failure(
        reason_code=reason_code,
        turn_id=turn_id,
        publisher=publish_chat_canvas_failure,
    )


def get_or_build_chat_turn_failure(
    *,
    carrier: object,
    reason_code: str,
    turn_id: str,
    cause: BaseException | None = None,
) -> ChatTurnFailure:
    """Reuse the first terminal projection across nested safety-net layers."""

    existing = getattr(carrier, "_chat_turn_failure", None)
    if isinstance(existing, ChatTurnFailure) and existing.diagnostics.get("turn_id") == turn_id:
        return existing
    from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable

    native_cause = cause if cause is not None else carrier
    failure = build_chat_turn_failure(
        reason_code=reason_code,
        turn_id=turn_id,
        clarification_stage=native_cause.stage if isinstance(native_cause, OrdinaryContinuationUnavailable) else None,
    )
    with contextlib.suppress(Exception):
        carrier._chat_turn_failure = failure
    return failure


def get_or_build_chat_llm_failure(
    *,
    carrier: object,
    projection: LLMFailureProjection,
    turn_id: str,
) -> ChatTurnFailure:
    """Reuse the first LLM terminal projection across nested turn layers."""

    existing = getattr(carrier, "_chat_turn_failure", None)
    if isinstance(existing, ChatTurnFailure) and existing.diagnostics.get("turn_id") == turn_id:
        return existing
    failure = build_chat_llm_failure(
        projection=projection,
        turn_id=turn_id,
    )
    with contextlib.suppress(Exception):
        carrier._chat_turn_failure = failure
    return failure


__all__ = [
    "CHAT_CANVAS_FAILURE_CODES",
    "LLM_REASON_CODES",
    "CHAT_PEER_FAILURE_CODES",
    "CHAT_TURN_FAILURE_CODES",
    "PUBLIC_TURN_FAILURE_CODES",
    "RECOVERY_ACTIONS",
    "DATARC_CONNECTION_FAILURE_CODES",
    "DIAGNOSTIC_POLICIES",
    "POWERBI_ADMIN_FAILURE_CODES",
    "POWERBI_QUERY_FAILURE_CODES",
    "REQUEST_VALIDATION_FAILURE_CODES",
    "RULE_CLARIFICATION_FAILURE_CODES",
    "SCHEDULED_FAILURE_CODES",
    "SCHEDULED_SKIPPED_CODES",
    "SKILL_WORKFLOW_FAILURE_CODES",
    "ChatTurnFailure",
    "DiagnosticPolicy",
    "ErrorDisclosure",
    "PublishedDiagnostic",
    "build_chat_canvas_failure",
    "build_chat_llm_failure",
    "build_chat_peer_failure",
    "build_chat_turn_failure",
    "chat_failure_error_kind",
    "chat_recovery_action",
    "clarification_failure_copy_code",
    "failure_event_reason_code",
    "get_or_build_chat_turn_failure",
    "get_or_build_chat_llm_failure",
    "normalize_chat_canvas_failure_code",
    "normalize_chat_peer_failure_code",
    "normalize_scheduled_failure_code",
    "publish_chat_canvas_failure",
    "publish_chat_llm_failure",
    "publish_chat_peer_failure",
    "publish_chat_turn_failure",
    "publish_datarc_connection_failure",
    "publish_powerbi_admin_failure",
    "publish_powerbi_query_failure",
    "publish_request_validation_failure",
    "publish_rule_clarification_failure",
    "publish_scheduled_failure",
    "publish_skill_workflow_failure",
]
