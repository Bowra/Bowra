/**
 * Sprint θ Phase 8d: SSE event canonical TypeScript types.
 *
 * Per design §1 SSE event schema migration (additive).
 * Backend Phase 8b-5 emits these shapes; frontend handlers (canvas-sse.ts +
 * use-turn-runner.ts) parse + dispatch.
 *
 * Backward compat: 0.9.0 emits both old + new fields for 1 release.
 * 0.9.1 removes legacy `meta_intent` field on `done` event.
 */

import { TOOL_FAILURE_CODES } from "./tool-failure";

import {
  parseErrorDiagnosticsWire,
  type ErrorDiagnostics,
} from "./error-presentation";

// ─────────────────────────────────────────────────────────────
// Sprint chat-redesign-2026-05-21 Phase 1.1 — Step List Card events
// (backend events.py StepStartedPayload / StepProgressPayload /
//  StepDonePayload / StepListCardCreatedPayload /
//  StepListCardFinalizedPayload / EmojiAckPayload / turn.follow_up)
// ─────────────────────────────────────────────────────────────

export type StepType = "parse" | "tool_run" | "lead_thinking_writing" | "subagent" | "plan_announce";

/** Backend wire state — normalize to FE step-list state with normalizeStepState(). */
export type WireStepState = "success" | "failure" | "partial";

/** Backend StepError wire shape (events.py:182). */
export interface WireStepError {
  /** Categorical kind — corrupted | unsupported_mime | too_big | http_4xx | http_5xx | auth_required | timeout | vision_failed | sql_error | tool_exception | ... */
  type: string;
  message_zh: string;
  is_transient: boolean;
  code?: string | null;
  diagnostics?: unknown;
  /** Runtime name of the Skill a `skill_unavailable_<reason>` denial is about. */
  skill?: string | null;
}

/** Backend SubStepResult wire shape (events.py:193). */
export interface WireSubStep {
  label: string;
  state: "success" | "failure";
  output_preview?: string | null;
  error_kind?: string | null;
}

/** Backend PlanItem wire shape (events.py:208). Phase 3+ only. */
export interface WirePlanItem {
  item_id: string;
  label: string;
  tag: string;
  priority: string;
  status: "pending" | "in_progress" | "done" | "skipped";
  related_step_id?: string | null;
}

/** step_started payload (events.py:220). */
export interface StepStartedEvent {
  card_id: string;
  step_id: string;
  step_type: StepType;
  header_zh: string;
  started_at: string;
  parent_step_id?: string | null;
  concurrency_group_id?: string | null;
  input_preview?: string | null;
  // backward-compat (legacy tool_call mirror, Phase 1.1→1.5)
  tool_name?: string | null;
  call_id?: string | null;
  subagent_id?: string | null;
  arguments_preview?: string | null;
  /** Bounded structured source for durable Code projection. */
  code_input?: {
    language: "python";
    code: string;
    truncated: boolean;
    input_artifact_ids?: string[];
  } | null;
  kind?: string | null;
  display_category?: string | null;
  // tool-domain (D1): true=open world (web/external), false=closed in-workspace (dataset/kb).
  // Backend computes it by name at emit (subagent.py); FE can surface a source/world badge.
  open_world?: boolean | null;
}

/** step_progress payload (events.py:247). */
export interface StepProgressEvent {
  card_id: string;
  step_id: string;
  content_delta?: string | null;
  completed?: number | null;
  total?: number | null;
  percent?: number | null;
}

/** step_done payload (events.py:263). */
export interface StepDoneEvent {
  card_id: string;
  step_id: string;
  step_type: StepType;
  state: WireStepState;
  duration_ms: number;
  output_preview?: string | null;
  sub_steps?: WireSubStep[] | null;
  error?: WireStepError | null;
  // backward-compat (legacy tool_result mirror)
  tool_name?: string | null;
  call_id?: string | null;
  ok?: boolean | null;
  result_inline?: unknown;
  result_preview?: string | null;
  result_meta?: Record<string, unknown> | null;
  result_token_count?: number | null;
}

/** step_list_card_created payload (events.py:294). */
export interface StepListCardCreatedEvent {
  card_id: string;
  plan?: WirePlanItem[] | null;
}

/** step_list_card_finalized payload (events.py:302). */
export interface StepListCardFinalizedEvent {
  card_id: string;
  state: "completed" | "failed" | "cancelled";
  step_count: number;
}

/** emoji_ack / emoji_remove payload (events.py:311). */
export interface EmojiAckEvent {
  target_msg_id: string;
  emoji: string;
}

/** turn.follow_up — emitted on original turn bus before close when a steer drain spawned a continuation turn. */
export interface TurnFollowUpEvent {
  follow_up_turn_id: string;
  follow_up_stream_url: string;
  reason: "steer_drain" | string;
  buffered_count: number;
}

/** narrative.delta — Lead's voice prose chunk (post-scanner safe_text). Phase 1.6 G3. */
export interface NarrativeDeltaEvent {
  card_id: string;
  delta: string;
}

/**
 * llm.stream_restarted — the Router restarted a model attempt after part of its
 * output was already streamed; emitted after `llm.retry_scheduled` and before the
 * retried attempt's output. Character counts are Unicode code points (Python `len`).
 * Like `llm.retry_scheduled`, live frames may omit `card_id`; replay backfills it.
 */
export interface LlmStreamRestartedEvent {
  card_id?: string;
  subagent_id: string;
  profile: string | null;
  /** Present for peers/helpers: the same id their `partial_answer.parent_agent_id` carries. */
  parent_agent_id?: string | null;
  /** Lead only: trailing `narrative.delta` characters to withdraw. */
  discarded_narrative_chars: number;
  /** Peers only: trailing `partial_answer` characters of that peer to withdraw. */
  discarded_partial_answer_chars: number;
  /** Writing steps started in the withdrawn attempt; the backend also closes them with step_done. */
  discarded_step_ids: string[];
}

// ─────────────────────────────────────────────────────────────
// Inline tool call confirmation (FEATURE_INLINE_TOOL_CONFIRMATION).
// Both events are persisted Turn events attached to the waiting tool step and
// replayed through the normal replay path. They never carry arguments, email
// bodies or diffs: the full preview stays in the owner-only queue.
// ─────────────────────────────────────────────────────────────

export const TOOL_CONFIRMATION_KINDS = ["personal_mcp", "email_send", "shopify_content", "team_mcp"] as const;
export type ToolConfirmationKind = (typeof TOOL_CONFIRMATION_KINDS)[number];

export const TOOL_CONFIRMATION_CHOICES = ["reject", "approve", "approve_session"] as const;
export type ToolConfirmationChoice = (typeof TOOL_CONFIRMATION_CHOICES)[number];

export const TOOL_CONFIRMATION_CATEGORIES = ["external_write", "destructive", "unknown"] as const;
export type ToolConfirmationCategory = (typeof TOOL_CONFIRMATION_CATEGORIES)[number];

export const TOOL_CONFIRMATION_OUTCOMES = ["approved", "approved_session", "rejected", "expired", "unavailable"] as const;
export type ToolConfirmationOutcome = (typeof TOOL_CONFIRMATION_OUTCOMES)[number];

/** confirmation_requested payload. */
export interface ConfirmationRequestedEvent {
  card_id: string;
  /** "step_<call_id>": the waiting tool step. */
  step_id: string;
  call_id: string;
  /** pmi_* | esc_* | scc_* | ttc_* */
  confirmation_id: string;
  kind: ToolConfirmationKind;
  /** Only this user may decide. */
  requester_user_id: string;
  title: string;
  source_label: string | null;
  category: ToolConfirmationCategory | null;
  choices: ToolConfirmationChoice[];
  /** ISO-8601, server clock. Never compared with the local clock. */
  expires_at: string;
  /** Remaining seconds when the event was emitted. */
  timeout_seconds: number;
  seq?: number;
  parent_agent_id?: string | null;
}

/** confirmation_resolved payload. */
export interface ConfirmationResolvedEvent {
  card_id: string;
  step_id: string;
  confirmation_id: string;
  outcome: ToolConfirmationOutcome;
  decided_at: string;
  seq?: number;
}

function _oneOf<T extends string>(values: readonly T[], value: unknown): T | null {
  return typeof value === "string" && (values as readonly string[]).includes(value) ? (value as T) : null;
}

function _nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/** Parsed `confirmation_requested`; null when a required field is missing or malformed. */
export interface ParsedConfirmationRequested {
  stepId: string;
  callId: string | null;
  confirmationId: string;
  kind: ToolConfirmationKind;
  requesterUserId: string;
  title: string;
  sourceLabel: string | null;
  category: ToolConfirmationCategory | null;
  choices: ToolConfirmationChoice[];
  expiresAt: string;
  timeoutSeconds: number;
  parentAgentId: string | null;
}

export function parseConfirmationRequested(payload: Record<string, unknown>): ParsedConfirmationRequested | null {
  const stepId = _nonEmptyString(payload.step_id);
  const confirmationId = _nonEmptyString(payload.confirmation_id);
  const kind = _oneOf(TOOL_CONFIRMATION_KINDS, payload.kind);
  const requesterUserId = _nonEmptyString(payload.requester_user_id);
  if (!stepId || !confirmationId || !kind || !requesterUserId) return null;
  const choices = Array.isArray(payload.choices)
    ? Array.from(
        new Set(
          payload.choices.flatMap((choice) => {
            const known = _oneOf(TOOL_CONFIRMATION_CHOICES, choice);
            return known ? [known] : [];
          }),
        ),
      )
    : [];
  const timeout = payload.timeout_seconds;
  return {
    stepId,
    callId: _nonEmptyString(payload.call_id),
    confirmationId,
    kind,
    requesterUserId,
    title: typeof payload.title === "string" ? payload.title.slice(0, 120) : "",
    sourceLabel: _nonEmptyString(payload.source_label),
    category: _oneOf(TOOL_CONFIRMATION_CATEGORIES, payload.category),
    choices,
    expiresAt: typeof payload.expires_at === "string" ? payload.expires_at : "",
    timeoutSeconds: typeof timeout === "number" && Number.isFinite(timeout) && timeout > 0 ? timeout : 0,
    parentAgentId: _nonEmptyString(payload.parent_agent_id),
  };
}

/** Parsed `confirmation_resolved`; null when a required field is missing or malformed. */
export interface ParsedConfirmationResolved {
  stepId: string | null;
  confirmationId: string;
  outcome: ToolConfirmationOutcome;
  decidedAt: string | null;
}

export function parseConfirmationResolved(payload: Record<string, unknown>): ParsedConfirmationResolved | null {
  const confirmationId = _nonEmptyString(payload.confirmation_id);
  const outcome = _oneOf(TOOL_CONFIRMATION_OUTCOMES, payload.outcome);
  if (!confirmationId || !outcome) return null;
  return {
    stepId: _nonEmptyString(payload.step_id),
    confirmationId,
    outcome,
    decidedAt: _nonEmptyString(payload.decided_at),
  };
}

/** Parsed `llm.stream_restarted` withdrawal; malformed counts read as nothing to withdraw. */
export interface StreamRestartWithdrawal {
  parentAgentId: string | null;
  narrativeChars: number;
  partialAnswerChars: number;
}

function _nonNegativeCount(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : 0;
}

export function parseStreamRestartWithdrawal(
  payload: Record<string, unknown>,
): StreamRestartWithdrawal {
  const parent = payload.parent_agent_id;
  return {
    parentAgentId: typeof parent === "string" && parent ? parent : null,
    narrativeChars: _nonNegativeCount(payload.discarded_narrative_chars),
    partialAnswerChars: _nonNegativeCount(payload.discarded_partial_answer_chars),
  };
}

/**
 * Normalize a backend wire state into the FE step-list state vocab. The
 * two sets diverge: wire uses "success"; FE uses "done". Mapping is total
 * and lossless.
 */
export function normalizeStepState(s: WireStepState): "done" | "failed" | "partial" {
  if (s === "success") return "done";
  if (s === "failure") return "failed";
  return "partial";
}

/**
 * Normalize a backend StepError into the FE step-list StepError shape.
 * Maps `is_transient` → `kind` (the FE renderer uses kind to drive the
 * variant in FailureAckCard). `type` is preserved in `kind` only
 * when it's a recognized FE variant; otherwise the FE marks it as
 * "transient" or "permanent" based on is_transient.
 */
/** Tool-result `error_kind` values for a Skill call the Agent could not make. */
export const SKILL_DENIAL_ERROR_TYPES: ReadonlySet<string> = new Set([
  "skill_unavailable",
  // Older servers; their text is generic, so the step shows the localized Skill copy instead.
  "assigned_skill_unavailable",
  "skill_scope_denied",
]);

/** Prefix of the per-reason code a current server writes for a denied Skill call. */
export const SKILL_UNAVAILABLE_CODE_PREFIX = "skill_unavailable_";
const SKILL_RUNTIME_NAME = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

export function normalizeStepError(e: WireStepError | null | undefined): {
  kind: "transient" | "permanent" | "needs_user_input" | "unknown";
  message: string;
  retryable?: boolean;
  code?: string;
  diagnostics?: ErrorDiagnostics;
  skill?: string;
} | null {
  if (!e) return null;
  const isPermanent = !e.is_transient;
  const wireCode =
    typeof e.code === "string" &&
    /^[a-z][a-z0-9_]{0,127}$/.test(e.code)
      ? e.code
      : undefined;
  // A denied Skill call arrives as a tool result whose `error_kind` becomes the error type; keep it as
  // the code so the step names the Skill problem instead of a generic failure.
  const code = e.type === "tool_arguments_invalid" && (!wireCode || wireCode === "tool_execution_failed")
    ? "tool_arguments_invalid"
    : wireCode ?? (SKILL_DENIAL_ERROR_TYPES.has(e.type) ? e.type : undefined);
  const parsedDiagnostics = parseErrorDiagnosticsWire(e.diagnostics);
  const diagnostics =
    parsedDiagnostics &&
    code &&
    parsedDiagnostics.disclosure === "authenticated" &&
    (parsedDiagnostics.diagnosticCode === "powerbi_query_failure"
      || parsedDiagnostics.diagnosticCode === "tool_failure" && TOOL_FAILURE_CODES.has(code))
    ? {
        ...parsedDiagnostics,
        code,
      }
    : undefined;
  // Only a Skill denial names a Skill, and only a plain runtime name is carried into copy.
  const skill =
    code?.startsWith(SKILL_UNAVAILABLE_CODE_PREFIX) && typeof e.skill === "string" && SKILL_RUNTIME_NAME.test(e.skill)
      ? e.skill
      : undefined;
  return {
    kind: isPermanent ? "permanent" : "transient",
    message: e.type === "non_query_text" && code === "tool_execution_failed" ? "non_query_text" : e.message_zh,
    retryable: e.is_transient,
    ...(code ? { code } : {}),
    ...(diagnostics ? { diagnostics } : {}),
    ...(skill ? { skill } : {}),
  };
}
