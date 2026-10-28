import { blockedItemLines, parseBlockedItems } from "@/lib/agent-blocked-items";

const SENSITIVE_DATA_REASONS = new Set([
  "credential_or_secret",
  "pii_cn_mobile",
  "pii_cn_national_id",
  "pii_email",
  "pii_unknown",
]);

const UNSAFE_INSTRUCTION_REASONS = new Set([
  "prompt_injection_risk",
  "prompt_malformed",
  "prompt_safety_check_failed",
]);

export function agentReleaseReasonKey(code: string): string {
  if (code === "semantic_scanner_unavailable") return "release_reason_semantic_scanner_unavailable";
  if (SENSITIVE_DATA_REASONS.has(code)) return "release_reason_sensitive_data";
  if (UNSAFE_INSTRUCTION_REASONS.has(code)) return "release_reason_unsafe_instructions";
  if (code === "external_write") return "release_reason_external_write";
  if (code === "destructive") return "release_reason_destructive";
  if (code === "internal_artifact_write" || code === "sandboxed_skill_artifact_write") return "release_reason_artifact_write";
  if (code === "skill_static_risk_signal" || code === "skill_vendored_dependency") return "release_reason_skill_code_risk";
  if (code === "external_data_source") return "release_reason_external_data";
  if (code === "automated_execution") return "release_reason_automated_execution";
  if (code === "primary_capability_unproven") return "release_reason_primary_capability";
  if (code === "behavior_compile_blocked") return "release_reason_behavior_compile";
  if (code === "agent_runtime_extension_source_unavailable") return "release_reason_optional_skill";
  if (code === "precise_skill_scope_required") return "release_reason_precise_skill_scope";
  if (code.startsWith("tool_")) return "release_reason_tool_dependency";
  if (code.startsWith("skill_") || code === "precise_skill_scope_required" || code === "legacy_skill_scope_not_publishable") {
    return "release_reason_skill_dependency";
  }
  if (code.includes("dependency") || code.includes("definition")) return "release_reason_dependency_definition";
  return "release_reason_requires_attention";
}

// Publication error codes carry the one thing the generic "action failed" copy
// throws away: what the person in front of the screen is supposed to do next.
// The Agent Eval gate is the sharp case -- it blocks the *reviewer*, but only the
// Agent's owner can run an eval, so the copy has to name whose move it is.
const RELEASE_ERROR_KEYS: Record<string, string> = {
  agent_skill_dependency_missing: "release_error_skill_dependency_missing",
  agent_dependency_blocked: "release_error_dependency_blocked",
  agent_runtime_extension_preservation_required: "release_error_extension_preservation",
  agent_runtime_extension_private_source: "release_error_extension_private",
  agent_runtime_extensions_invalid: "release_error_extension_invalid",
  agent_runtime_extension_migration_required: "release_error_extension_unavailable",
  agent_runtime_extension_unproven: "release_error_extension_unavailable",
  agent_eval_required: "release_error_eval_required",
  agent_eval_critical_failure: "release_error_eval_critical_failure",
  agent_eval_platform_suite_missing: "release_error_eval_suite_missing",
  qualified_reviewer_unavailable: "release_error_no_reviewer",
};

export function agentReleaseErrorKey(code: string | undefined): string | null {
  return code ? RELEASE_ERROR_KEYS[code] ?? null : null;
}

/**
 * A publication block lists each blocked Skill or Tool with its reasons, one line per item, read from
 * the error's `details` (Agents) or its body (`detail.blocked_items`, team Subagents). Null when the
 * server sent no items, so callers keep their code-based copy.
 */
export function agentReleaseBlockedText(
  cause: { details?: unknown; body?: unknown } | null | undefined,
  t: (key: string, values?: Record<string, string>) => string,
): string | null {
  if (!cause) return null;
  const items = [cause.details, cause.body].map(parseBlockedItems).find((list) => list.length > 0);
  return items ? blockedItemLines(items, t).join("\n") : null;
}
