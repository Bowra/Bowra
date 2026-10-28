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
  const code = wireCode ?? (SKILL_DENIAL_ERROR_TYPES.has(e.type) ? e.type : undefined);
  const parsedDiagnostics = parseErrorDiagnosticsWire(e.diagnostics);
  const diagnostics =
    parsedDiagnostics &&
    code &&
    parsedDiagnostics.disclosure === "authenticated" &&
    parsedDiagnostics.diagnosticCode === "powerbi_query_failure"
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
    message: e.message_zh,
    retryable: e.is_transient,
    ...(code ? { code } : {}),
    ...(diagnostics ? { diagnostics } : {}),
    ...(skill ? { skill } : {}),
  };
}
