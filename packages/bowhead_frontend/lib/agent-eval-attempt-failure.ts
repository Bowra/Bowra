/**
 * Why evaluation attempts ended without a verdict, and what the Owner can do about each cause.
 *
 * The server keeps the cause per attempt (agent_eval_attempt_failure.py): the evaluation model, the
 * Agent's model (rate limit, credentials, timeout...), a Tool, a cancellation, or an unclassified
 * platform error with a diagnostic reference the server logs. Release, Evaluate and the copied
 * diagnostics read the same rule from here.
 */

const AGENT_MODEL_CAUSES = ["rate_limited", "auth_failed", "timeout", "unavailable", "request_rejected", "context_overflow"] as const;

/**
 * Live Datarc reads in evaluation (FEATURE_AGENT_EVAL_DATARC_READ_ONLY): the environment causes that end an attempt
 * (agent-eval-datarc-read-only.md §7.1). Each is its own release reason, `attempt_datarc_unavailable_<cause>`.
 */
export const DATARC_FAILURE_CODE = "agent_eval_datarc_unavailable";
const DATARC_REASON_PREFIX = "attempt_datarc_unavailable_";
/** Causes that testing again can fix by itself: Datarc was unreachable, busy, or a row query ran out of time. */
const DATARC_RETRYABLE_CAUSES = ["datarc_unreachable", "datarc_busy", "datarc_timeout"] as const;
/** Causes fixed somewhere else first (the connection, the Owner's account or workspace, Datarc permissions). */
const DATARC_PERSISTENT_CAUSES = [
  "no_connection",
  "connection_unusable",
  "connection_rejected",
  "starter_not_owner",
  "owner_identity_unverified",
  "no_workspace",
  "owner_not_member",
  "owner_permission_denied",
] as const;
const DATARC_CAUSES: readonly string[] = [...DATARC_RETRYABLE_CAUSES, ...DATARC_PERSISTENT_CAUSES];
/** Causes a team administrator fixes under Team settings › Data Sources (the Team's Datarc connection). */
export const DATA_SOURCE_SETTINGS_REASONS: ReadonlySet<string> = new Set(
  ["no_connection", "connection_unusable", "connection_rejected"].map((cause) => `${DATARC_REASON_PREFIX}${cause}`),
);
/** A case whose data changed during the test is not compared; testing again can read stable data. */
export const DATA_CHANGED_REASON = "data_changed_during_test";

/** Release-check reasons for attempts that ended without a verdict (besides time limits and Tools that can't run). */
export const ATTEMPT_FAILURE_REASONS: ReadonlySet<string> = new Set([
  "attempt_model_unavailable",
  "run_model_unavailable",
  "run_model_config_changed",
  "attempt_judge_unavailable",
  "attempt_judge_not_configured",
  "attempt_cancelled",
  "attempt_tool_failed",
  "attempt_infrastructure_incomplete",
  ...AGENT_MODEL_CAUSES.map((cause) => `attempt_agent_model_${cause}`),
  ...DATARC_CAUSES.map((cause) => `${DATARC_REASON_PREFIX}${cause}`),
]);

/** Causes that stay until something else changes: testing again alone gives the same result. */
export const PERSISTENT_ATTEMPT_REASONS: ReadonlySet<string> = new Set([
  "attempt_model_unavailable",
  "run_model_unavailable",
  "attempt_judge_not_configured",
  "attempt_agent_model_auth_failed",
  "attempt_agent_model_request_rejected",
  "attempt_agent_model_context_overflow",
  "required_tool_not_run_in_eval",
  "tool_not_run_in_eval",
  ...DATARC_PERSISTENT_CAUSES.map((cause) => `${DATARC_REASON_PREFIX}${cause}`),
]);

/**
 * Attempts the evaluation couldn't test: a Tool the case requires, or one the answer depended on (a data query,
 * say), can't run in evaluation yet. Neither a pass nor the Agent's failure; testing again gives the same result.
 */
export const REQUIRED_TOOL_NOT_RUN_CODE = "agent_eval_required_tool_not_run";
export const TOOL_NOT_RUN_CODE = "agent_eval_tool_not_run_in_eval";
export const TOOL_NOT_RUN_FAILURE_CODES: ReadonlySet<string> = new Set([REQUIRED_TOOL_NOT_RUN_CODE, TOOL_NOT_RUN_CODE]);

/** Causes a team administrator fixes under Team settings › Models. */
export const MODELS_SETTINGS_REASONS: ReadonlySet<string> = new Set([
  "attempt_model_unavailable",
  "run_model_unavailable",
  "run_model_config_changed",
  "attempt_judge_unavailable",
  "attempt_judge_not_configured",
  "attempt_agent_model_auth_failed",
  "attempt_agent_model_request_rejected",
  "attempt_agent_model_unavailable",
  "attempt_agent_model_timeout",
]);

/**
 * Whether testing again can give this result a verdict. It can unless every cause the reasons name is one
 * that stays (a missing evaluation model, rejected credentials, a Tool evaluation can't run).
 */
export function retryCanHelp(reasons: readonly string[]): boolean {
  const causes = reasons.filter((code) => ATTEMPT_FAILURE_REASONS.has(code) || PERSISTENT_ATTEMPT_REASONS.has(code));
  return causes.length === 0 || causes.some((code) => !PERSISTENT_ATTEMPT_REASONS.has(code));
}

const CANCEL_CAUSE_ORDER = ["restart", "authority_lost", "stopped"] as const;

type Translate = (key: string, values?: Record<string, string>) => string;

/** The model settings name of a role that had no model: the evaluation model, or the main model the Agent runs on. */
export function modelRoleLabel(role: string | null | undefined, t: Translate): string {
  return t(role === "judge" ? "version_model_role_judge" : "version_model_role_main");
}

/**
 * The sentence under one reason: the affected cases and the action. `cases` is the already formatted case list;
 * `tools`, `causes` and `references` come from the attempts with that reason.
 */
export function attemptReasonDetail(
  code: string,
  facts: {
    cases: string;
    tools: readonly string[];
    causes: readonly (string | null)[];
    references: readonly string[];
    toolNames?: string;
    role?: string;
    /** Datarc causes: the Agent's Owner (starter_not_owner) and the Datarc workspace (owner_not_member). */
    owner?: string | null;
    workspace?: string | null;
  },
  t: Translate,
): string | null {
  if (!ATTEMPT_FAILURE_REASONS.has(code)) return null;
  // Stopped before any attempt: the whole test, not particular cases.
  if (code === "run_model_unavailable") return t("version_reason_detail_run_model_unavailable", { role: facts.role ?? t("version_model_role_main") });
  if (code === "run_model_config_changed") return t("version_reason_detail_run_model_config_changed");
  if (!facts.cases) return null;
  if (code === "attempt_model_unavailable") {
    return t("version_reason_detail_attempt_model_unavailable", { cases: facts.cases, role: facts.role ?? t("version_model_role_main") });
  }
  if (code === "attempt_tool_failed") {
    return facts.tools.length && facts.toolNames
      ? t("version_reason_detail_attempt_tool_failed", { tools: facts.toolNames, cases: facts.cases })
      : t("version_reason_detail_attempt_tool_failed_unnamed", { cases: facts.cases });
  }
  if (code === "attempt_cancelled") {
    const cause = CANCEL_CAUSE_ORDER.find((item) => facts.causes.includes(item)) ?? "stopped";
    return t(`version_reason_detail_attempt_cancelled_${cause}`, { cases: facts.cases });
  }
  if (code.startsWith(DATARC_REASON_PREFIX)) {
    const reference = facts.references.join(", ");
    if (code === `${DATARC_REASON_PREFIX}starter_not_owner`) {
      return facts.owner
        ? t(`version_reason_detail_${code}`, { cases: facts.cases, owner: facts.owner })
        : t(`version_reason_detail_${code}_unnamed`, { cases: facts.cases });
    }
    if (code === `${DATARC_REASON_PREFIX}owner_not_member`) {
      return facts.workspace
        ? t(`version_reason_detail_${code}`, { cases: facts.cases, workspace: facts.workspace })
        : t(`version_reason_detail_${code}_unnamed`, { cases: facts.cases });
    }
    if ((code === `${DATARC_REASON_PREFIX}datarc_unreachable` || code === `${DATARC_REASON_PREFIX}owner_permission_denied`) && !reference) {
      return t(`version_reason_detail_${code}_unreferenced`, { cases: facts.cases });
    }
    return t(`version_reason_detail_${code}`, { cases: facts.cases, reference });
  }
  if (code === "attempt_infrastructure_incomplete") {
    // Older attempts have no stored reference; Copy diagnostics carries the run instead.
    return facts.references.length
      ? t("version_reason_detail_attempt_platform", { cases: facts.cases, reference: facts.references.join(", ") })
      : t("version_reason_detail_attempt_platform_unreferenced", { cases: facts.cases });
  }
  return t(`version_reason_detail_${code}`, { cases: facts.cases });
}

/** The same reason the server gives one attempt (`incomplete_attempt_reason`), for raw run results on Evaluate. */
export function attemptFailureReason(status: string, failureCode: string | null | undefined, cause?: string | null): string | null {
  if (status === "judge_unknown") return failureCode === "agent_eval_judge_not_configured" ? "attempt_judge_not_configured" : "attempt_judge_unavailable";
  if (status === "cancelled") return "attempt_cancelled";
  if (status !== "infra_failed") return null;
  const code = failureCode ?? "";
  if (code === "agent_eval_model_unavailable") return "attempt_model_unavailable";
  if (code.startsWith("agent_eval_agent_model_")) {
    const cause = code.slice("agent_eval_agent_model_".length);
    if ((AGENT_MODEL_CAUSES as readonly string[]).includes(cause)) return `attempt_agent_model_${cause}`;
  }
  if (code === "agent_eval_tool_failed") return "attempt_tool_failed";
  // A live Datarc read's environment cause; a row without a known cause stays an unclassified platform error.
  if (code === DATARC_FAILURE_CODE) return cause && DATARC_CAUSES.includes(cause) ? `${DATARC_REASON_PREFIX}${cause}` : "attempt_infrastructure_incomplete";
  // Time limits and Tools that can't run in evaluation have their own lines.
  if (code === "agent_eval_attempt_timeout" || code === "agent_eval_duration_budget_exceeded" || TOOL_NOT_RUN_FAILURE_CODES.has(code)) return null;
  return "attempt_infrastructure_incomplete";
}

/** The stored safe facts of one attempt's failure (`deterministic.failure`). */
export function storedAttemptFailure(deterministic: unknown): {
  cause: string | null;
  tool: string | null;
  reference: string | null;
  role: string | null;
  workspace: string | null;
  owner: string | null;
} {
  const failure = (deterministic as { failure?: unknown } | null | undefined)?.failure;
  const value = failure && typeof failure === "object" ? failure as Record<string, unknown> : {};
  const text = (item: unknown) => (typeof item === "string" && item ? item : null);
  return {
    cause: text(value.cause),
    tool: text(value.tool),
    reference: text(value.reference),
    role: text(value.role),
    workspace: text(value.workspace),
    owner: text(value.owner_name),
  };
}
