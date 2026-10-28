import { describe, expect, it } from "vitest";

import {
  normalizePeerFailureReasonCode,
  normalizeReasonCode,
} from "./error-messages";
import enMessages from "../messages/en-US.json";
import zhMessages from "../messages/zh-CN.json";

const TURN_REASON_CODES = [
  "access_context_missing",
  "canvas_compose_failed",
  "canvas_compose_timeout",
  "canvas_internal_error",
  "canvas_payload_invalid",
  "canvas_section_not_found",
  "clarification_abort",
  "clarification_resume_unavailable",
  "cost_cap_exceeded",
  "document_information_incomplete",
  "document_planning_failed",
  "document_history_unavailable",
  "document_request_rejected",
  "document_routing_invalid",
  "document_routing_uncertain",
  "document_routing_source_unavailable",
  "entitlement_denied",
  "hop_cap_exceeded",
  "lead_empty_answer",
  "lead_internal_error",
  "lead_output_exhausted",
  "reference_continuation_unavailable",
  "reference_primary_incomplete",
  "reference_policy_changed",
  "llm_provider_auth_failed",
  "llm_provider_rate_limited",
  "llm_provider_timeout",
  "llm_provider_unavailable",
  "llm_request_rejected",
  "llm_context_overflow",
  "llm_response_invalid",
  "llm_stream_deadline_exceeded",
  "llm_stream_interrupted",
  "no_safe_fields_changed",
  "peer_depth_exceeded",
  "peer_dispatch_network_error",
  "peer_dispatch_timeout",
  "plan_blocked",
  "plan_execution_failed",
  "plan_runtime_unavailable",
  "service_interrupted",
  "session_deleted",
  "single_write_leaf_violation",
  "team_pool_exhausted",
  "terminal_projection_not_committed",
  "token_cap_exhausted",
  "user_cancel",
  "user_credit_exhausted",
  "wallclock_exceeded",
] as const;

const PEER_REASON_TO_MESSAGE_KEY = {
  peer_configuration_error: "configuration_error",
  peer_empty_result: "empty_result",
  peer_execution_failed: "execution_failed",
  peer_execution_uncertain: "execution_uncertain",
  peer_execution_timeout: "execution_timeout",
  peer_internal_error: "internal_error",
  peer_iteration_limit_exceeded: "iteration_limit_exceeded",
  peer_tool_failure_limit_exceeded: "tool_failure_limit_exceeded",
  peer_tool_call_limit_exceeded: "tool_call_limit_exceeded",
  peer_credit_exhausted: "credit_exhausted",
} as const;

describe("Chat error i18n registry", () => {
  it("covers every finite Turn/Canvas reason in both locales", () => {
    expect(Object.keys(zhMessages.error_messages.reasons).sort()).toEqual(
      [...TURN_REASON_CODES].sort(),
    );
    expect(Object.keys(enMessages.error_messages.reasons).sort()).toEqual(
      [...TURN_REASON_CODES].sort(),
    );
    for (const code of TURN_REASON_CODES) {
      expect(normalizeReasonCode(code)).toBe(code);
      expect(zhMessages.error_messages.reasons[code].trim()).not.toBe("");
      expect(enMessages.error_messages.reasons[code].trim()).not.toBe("");
    }
  });

  it("covers every finite Peer reason in both locales", () => {
    const messageKeys = Object.values(PEER_REASON_TO_MESSAGE_KEY).sort();
    expect(Object.keys(zhMessages.step_list.peer_errors).sort()).toEqual(
      messageKeys,
    );
    expect(Object.keys(enMessages.step_list.peer_errors).sort()).toEqual(
      messageKeys,
    );
    for (const [code, messageKey] of Object.entries(
      PEER_REASON_TO_MESSAGE_KEY,
    )) {
      expect(normalizePeerFailureReasonCode(code)).toBe(code);
      expect(
        zhMessages.step_list.peer_errors[
          messageKey as keyof typeof zhMessages.step_list.peer_errors
        ].trim(),
      ).not.toBe("");
      expect(
        enMessages.step_list.peer_errors[
          messageKey as keyof typeof enMessages.step_list.peer_errors
        ].trim(),
      ).not.toBe("");
    }
  });

  it("maps a subagent step-limit exhaustion to the step-limit copy, not the generic failure", () => {
    expect(
      normalizePeerFailureReasonCode(
        "subagent exhausted max_iterations=8 without producing a final answer. Last response content: 'x'.",
      ),
    ).toBe("peer_iteration_limit_exceeded");
  });

  it("fails closed for unknown or raw-looking reasons", () => {
    expect(
      normalizeReasonCode("Authorization: Bearer raw-secret"),
    ).toBe("lead_internal_error");
    expect(
      normalizePeerFailureReasonCode(
        "tool_exception: Authorization: Bearer raw-secret",
      ),
    ).toBe("peer_execution_failed");
  });
});
