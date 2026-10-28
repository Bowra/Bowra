import { isNetworkFailure } from "@/lib/transport-error";
/**
 * Agent draft (intent card, draft review) operation failures → user-facing sentence keys under `settings.subagents.*`.
 * Select by error/status codes; do not show raw API text or “Failed to fetch”. Status codes and request IDs go into Copy Diagnostics.
 */
export type SubagentDraftFailureKey =
  | "intent_model_unconfigured"
  | "draft_conflict"
  | "draft_fix_validation"
  | "draft_failed_forbidden"
  | "draft_failed_gone"
  | "draft_failed_too_frequent"
  | "draft_failed_network"
  | "draft_failed_plain"
  | "draft_store_unavailable";

export function subagentDraftFailureKey(error: unknown): SubagentDraftFailureKey {
  const record = error && typeof error === "object" ? (error as { status?: unknown; code?: unknown }) : {};
  const status = typeof record.status === "number" ? record.status : undefined;
  if (record.code === "team_model_unconfigured") return "intent_model_unconfigured";
  if (record.code === "subagent_draft_store_unavailable") return "draft_store_unavailable";
  if (record.code === "subagent_draft_version_conflict" || status === 409 || status === 412) return "draft_conflict";
  if (status === 400 || status === 422) return "draft_fix_validation";
  if (status === 403) return "draft_failed_forbidden";
  if (status === 404) return "draft_failed_gone";
  if (status === 429) return "draft_failed_too_frequent";
  if (status === undefined && isNetworkFailure(error)) return "draft_failed_network";
  return "draft_failed_plain";
}

export type DraftGenerationFailureKey =
  | "intent_timeout"
  | "intent_model_unconfigured"
  | "intent_credits_exhausted"
  | "intent_invalid_result"
  | "intent_sensitive_result"
  | "intent_unavailable";

/**
 * Why AI drafting failed, for the intent card and the canvas "Describe … let AI draft it" panel alike. A timeout
 * says what to do about a slow model; it is never presented as a read failure that a refresh would fix.
 */
export function draftGenerationFailureKey(error: unknown): DraftGenerationFailureKey {
  const code = error && typeof error === "object" ? (error as { code?: unknown }).code : undefined;
  if (code === "subagent_draft_model_timeout" || code === "subagent_draft_generation_timeout") return "intent_timeout";
  if (code === "team_model_unconfigured") return "intent_model_unconfigured";
  if (code === "user_credit_exhausted" || code === "team_pool_exhausted") return "intent_credits_exhausted";
  if (code === "subagent_draft_schema_invalid" || code === "subagent_draft_response_invalid") return "intent_invalid_result";
  if (code === "subagent_draft_secret_pattern_rejected") return "intent_sensitive_result";
  return "intent_unavailable";
}
