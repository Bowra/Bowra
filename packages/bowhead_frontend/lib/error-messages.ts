/**
 * Sprint θ Phase 8d: Static error message templates.
 *
 * Per tech-design §0.6 U3 fix + design §3 (S8):
 * - Backend SSE payloads carry ONLY enum reason_code (no LLM-generated strings)
 * - Frontend renders static locale-aware templates per code (no LLM English leakage)
 *
 * i18n: `error_messages.reasons` in messages/*.json is the single reason-code -> copy
 * table. Components read it through `useTranslations("error_messages.reasons")`: the turn
 * failure banner, and the entitlement denial on clarification and formula actions.
 *
 * Source: design Sprint-Theta-Frontend-Flow.html §3 S8 errors.
 */

/** Canonical error reason codes emitted by backend; each has copy in `error_messages.reasons`. */
export const REASON_CODE_LIST = [
  "agent_step_limit",
  "agent_tool_call_limit",
  "agent_stalled",
  "wallclock_exceeded",
  "hop_cap_exceeded",
  "peer_depth_exceeded",
  "cost_cap_exceeded",
  "token_cap_exhausted",
  "canvas_section_not_found",
  "canvas_payload_invalid",
  "canvas_compose_failed",
  "canvas_compose_timeout",
  "canvas_internal_error",
  "no_safe_fields_changed",
  "peer_dispatch_network_error",
  "peer_dispatch_timeout",
  "access_context_missing",
  "plan_runtime_unavailable",
  "plan_blocked",
  "plan_execution_failed",
  "lead_internal_error",
  "lead_empty_answer",
  "lead_output_exhausted",
  "document_information_incomplete",
  "document_planning_failed",
  "document_authoring_script_failed",
  "document_candidate_invalid",
  "document_history_unavailable",
  "document_request_rejected",
  "document_routing_invalid",
  "document_routing_uncertain",
  "document_routing_source_unavailable",
  "service_interrupted",
  "reference_policy_changed",
  "reference_continuation_unavailable",
  "reference_primary_incomplete",
  "llm_provider_auth_failed",
  "llm_provider_rate_limited",
  "llm_provider_timeout",
  "llm_provider_unavailable",
  "llm_request_rejected",
  "llm_context_overflow",
  "llm_response_invalid",
  "llm_stream_deadline_exceeded",
  "llm_stream_interrupted",
  "terminal_projection_not_committed",
  "user_cancel",
  "clarification_abort",
  "clarification_resume_unavailable",
  "single_write_leaf_violation",
  // licensing FE slice A (§13.2): make 402/403 entitlement denials VISIBLE instead of a frozen spinner
  "entitlement_denied",
  // feedback-0713 F2: sending a turn to a soft-deleted session (linked report deletion) is rejected by the backend with 410
  "session_deleted",
  // Turns blocked by credit admission (e.g. a steering-resume child turn blocked after its parent settles)
  "user_credit_exhausted",
  "team_pool_exhausted",
] as const;

export type ReasonCode = (typeof REASON_CODE_LIST)[number];

const REASON_CODES = new Set<string>(REASON_CODE_LIST);
const LEGACY_REASON_CODE_ALIASES: Readonly<Record<string, ReasonCode>> = {
  lead_loop_hard_timeout: "wallclock_exceeded",
  peer_no_commit_force_bail: "peer_dispatch_network_error",
  peer_total_failures: "peer_dispatch_network_error",
  peer_wallclock_exhausted: "peer_dispatch_timeout",
  subagent_internal_error: "lead_internal_error",
  token_cap_exhausted: "token_cap_exhausted",
  tool_calls_exceeded: "hop_cap_exceeded",
  composer_exception: "canvas_compose_failed",
  lock_timeout: "canvas_compose_timeout",
  patch_failed: "canvas_internal_error",
};

const CANVAS_FAILURE_CODES = new Set<ReasonCode>([
  "canvas_compose_failed",
  "canvas_compose_timeout",
  "canvas_internal_error",
  "canvas_payload_invalid",
  "canvas_section_not_found",
  "no_safe_fields_changed",
]);

/** Project event input onto the finite, localized public reason registry. */
export function normalizeReasonCode(reason: unknown): ReasonCode {
  if (typeof reason !== "string") return "lead_internal_error";
  const candidate = reason.trim();
  if (REASON_CODES.has(candidate)) return candidate as ReasonCode;
  const prefix = candidate.split(/[:=]/, 1)[0]?.trim() ?? "";
  return LEGACY_REASON_CODE_ALIASES[prefix] ?? "lead_internal_error";
}

export function normalizeCanvasFailureReasonCode(
  reason: unknown,
): ReasonCode {
  const normalized = normalizeReasonCode(reason);
  return CANVAS_FAILURE_CODES.has(normalized)
    ? normalized
    : "canvas_internal_error";
}

export type PeerFailureReasonCode =
  | "peer_configuration_error"
  | "peer_empty_result"
  | "peer_execution_failed"
  | "peer_execution_uncertain"
  | "peer_execution_timeout"
  | "peer_internal_error"
  | "peer_iteration_limit_exceeded"
  | "peer_tool_failure_limit_exceeded"
  | "peer_tool_call_limit_exceeded"
  | "peer_credit_exhausted";

const PEER_FAILURE_CODES = new Set<PeerFailureReasonCode>([
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
]);

const LEGACY_PEER_FAILURE_ALIASES: Readonly<
  Record<string, PeerFailureReasonCode>
> = {
  peer_wallclock_exhausted: "peer_execution_timeout",
  peer_no_commit_force_bail: "peer_iteration_limit_exceeded",
  "subagent exhausted max_iterations": "peer_iteration_limit_exceeded",
  peer_total_failures: "peer_tool_failure_limit_exceeded",
  subagent_empty_answer: "peer_empty_result",
  subagent_execution_uncertain: "peer_execution_uncertain",
  subagent_internal_error: "peer_internal_error",
};

export function normalizePeerFailureReasonCode(
  reason: unknown,
): PeerFailureReasonCode {
  if (typeof reason !== "string") return "peer_execution_failed";
  const candidate = reason.trim();
  if (PEER_FAILURE_CODES.has(candidate as PeerFailureReasonCode)) {
    return candidate as PeerFailureReasonCode;
  }
  const prefix = candidate.split(/[:=]/, 1)[0]?.trim() ?? "";
  return LEGACY_PEER_FAILURE_ALIASES[prefix] ?? "peer_execution_failed";
}
