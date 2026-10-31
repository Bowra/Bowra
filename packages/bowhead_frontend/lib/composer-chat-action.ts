import { appFetch } from "./base-path";
import { apiFetch, readApiError } from "./api-client";
import { randomUuid } from "./random-id";

const COMPOSER_CHAT_ACTION_TIMEOUT_MS = 15_000;

export async function boundedComposerControlFetch(
  fetcher: (input: RequestInfo | URL, init: RequestInit) => Promise<Response>,
  input: RequestInfo | URL,
  init: RequestInit,
  // null: no deadline, like an ordinary send (see singleAttemptChatActionFetch).
  timeoutMs: number | null = COMPOSER_CHAT_ACTION_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const upstreamSignal = init.signal;
  let rejectBoundary!: (reason: unknown) => void;
  const boundary = new Promise<never>((_resolve, reject) => {
    rejectBoundary = reject;
  });
  const abortFromUpstream = () => {
    const reason = upstreamSignal?.reason
      ?? new DOMException("The operation was aborted.", "AbortError");
    controller.abort(reason);
    rejectBoundary(reason);
  };
  if (upstreamSignal?.aborted) abortFromUpstream();
  else upstreamSignal?.addEventListener("abort", abortFromUpstream, { once: true });
  const timeout = timeoutMs === null ? undefined : setTimeout(() => {
    const reason = new DOMException(
      "composer chat action timed out",
      "TimeoutError",
    );
    controller.abort(reason);
    rejectBoundary(reason);
  }, timeoutMs);
  try {
    return await Promise.race([
      (async () => {
        const response = await fetcher(input, {
          ...init,
          signal: controller.signal,
        });
        // Keep the deadline active through body consumption. A proxy can
        // deliver headers and then stall forever; returning that Response
        // would otherwise clear the only action timeout before JSON parsing.
        const body = response.status === 204 || response.status === 205
          ? null
          : await response.arrayBuffer();
        return new Response(body, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      })(),
      boundary,
    ]);
  } finally {
    clearTimeout(timeout);
    upstreamSignal?.removeEventListener("abort", abortFromUpstream);
  }
}

export type ComposerAgentDraftTarget = { kind: "agent_draft"; id: string; version: number };

export function isComposerAgentDraftTarget(value: unknown): value is ComposerAgentDraftTarget {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const target = value as Record<string, unknown>;
  return Object.keys(target).sort().join(",") === "id,kind,version"
    && target.kind === "agent_draft" && typeof target.id === "string"
    && /^sad_[A-Za-z0-9_]{1,76}$/.test(target.id)
    && Number.isSafeInteger(target.version) && (target.version as number) >= 1 && (target.version as number) <= 999_999_999;
}

function draftTargetMatches(value: unknown, expected?: ComposerAgentDraftTarget): boolean {
  if (!expected) return value === undefined || value === null;
  return isComposerAgentDraftTarget(value) && value.id === expected.id && value.version === expected.version;
}

export type ComposerChatAction =
  | {
      actionId: string;
      actionKind: "start";
      agentDraftTarget?: ComposerAgentDraftTarget;
      attempted: true;
      sourceRevision: string | null;
      sessionId: string | null;
    }
  | {
      actionId: string;
      actionKind: "steer";
      attempted: true;
      sourceRevision: string | null;
      sessionId: string;
      expectedTurnId: string;
      expectedExecutionGeneration: number;
    };

export type PersistedComposerChatAction = ComposerChatAction & {
  sourceRevision: string;
};

export interface ComposerChatActionReceipt {
  action_id: string;
  action_kind: "start" | "steer";
  outcome: "admitted" | "accepted";
  session_id: string;
  turn_id: string;
  execution_generation: number;
  terminal_protocol_version: 1;
  stream_url: string;
  agent_draft_target?: ComposerAgentDraftTarget;
}

type CreateComposerChatActionInput =
  | {
      actionKind: "start";
      agentDraftTarget?: ComposerAgentDraftTarget;
      sourceRevision: string | null;
      sessionId?: string | null;
    }
  | {
      actionKind: "steer";
      sourceRevision: string | null;
      sessionId: string;
      expectedTurnId: string;
      expectedExecutionGeneration: number;
    };

const ACTION_ID_PATTERN = /^act_(start|steer)_([0-9a-f]{32})$/;

export function createComposerChatAction(
  input: CreateComposerChatActionInput,
): ComposerChatAction {
  const suffix = randomUuid().replaceAll("-", "").toLowerCase();
  if (input.actionKind === "start") {
    if (input.agentDraftTarget !== undefined && !isComposerAgentDraftTarget(input.agentDraftTarget))
      throw new Error("invalid Agent draft target");
    return {
      ...(input.agentDraftTarget ? { agentDraftTarget: { ...input.agentDraftTarget } } : {}),
      actionId: `act_start_${suffix}`,
      actionKind: "start",
      attempted: true,
      sourceRevision: input.sourceRevision,
      sessionId: input.sessionId ?? null,
    };
  }
  return {
    actionId: `act_steer_${suffix}`,
    actionKind: "steer",
    attempted: true,
    sourceRevision: input.sourceRevision,
    sessionId: input.sessionId,
    expectedTurnId: input.expectedTurnId,
    expectedExecutionGeneration: input.expectedExecutionGeneration,
  };
}

export function composerChatActionSuffix(action: ComposerChatAction): string {
  const match = ACTION_ID_PATTERN.exec(action.actionId);
  if (!match || match[1] !== action.actionKind) {
    throw new Error("composer chat action identity is invalid");
  }
  return match[2];
}

export function canonicalComposerStartSessionId(
  action: ComposerChatAction,
): string | null {
  if (action.actionKind !== "start") return null;
  return action.sessionId ?? `s_${composerChatActionSuffix(action)}`;
}

export function composerChatActionUserMessageId(
  action: ComposerChatAction,
): string {
  return `${action.actionKind === "start" ? "u_s" : "u_r"}_${composerChatActionSuffix(action)}`;
}

/** One request attempt only. In particular, a 401 is returned without replaying the POST. */
export function singleAttemptChatActionFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  timeoutMs?: number | null,
): Promise<Response> {
  return boundedComposerControlFetch(appFetch, input, {
    credentials: "include",
    ...(init ?? {}),
    // A browser may automatically replay the POST across a 307/308.  Terminal
    // actions permit exactly one mutation attempt, so redirects fail closed.
    redirect: "error",
  }, timeoutMs);
}

function isReceipt(
  value: unknown,
  action: ComposerChatAction,
): value is ComposerChatActionReceipt {
  if (!value || typeof value !== "object") return false;
  const receipt = value as Partial<ComposerChatActionReceipt>;
  if (
    receipt.action_id !== action.actionId
    || receipt.action_kind !== action.actionKind
    || receipt.terminal_protocol_version !== 1
    || typeof receipt.session_id !== "string"
    || typeof receipt.turn_id !== "string"
    || receipt.stream_url !== `/api/v1/chat/stream/${receipt.turn_id}`
    || !Number.isSafeInteger(receipt.execution_generation)
    || (receipt.execution_generation ?? 0) < 1
  ) return false;
  if (action.actionKind === "start") {
    const suffix = composerChatActionSuffix(action);
    return draftTargetMatches(receipt.agent_draft_target, action.agentDraftTarget) && receipt.outcome === "admitted"
      && receipt.turn_id === `t_${suffix}`
      && receipt.session_id === (action.sessionId ?? `s_${suffix}`)
      && receipt.execution_generation === 1;
  }
  return draftTargetMatches(receipt.agent_draft_target) && receipt.outcome === "accepted"
    && receipt.session_id === action.sessionId
    && receipt.turn_id === action.expectedTurnId
    && receipt.execution_generation === action.expectedExecutionGeneration;
}

export function isComposerChatActionResponse(
  value: unknown,
  action: ComposerChatAction,
): value is {
  session_id: string;
  turn_id: string;
  stream_url: string;
  status: "started" | "buffered";
  execution_generation: number;
  terminal_protocol_version: 1;
} {
  if (!value || typeof value !== "object") return false;
  const response = value as Record<string, unknown>;
  if (
    typeof response.session_id !== "string"
    || typeof response.turn_id !== "string"
    || response.stream_url !== `/api/v1/chat/stream/${response.turn_id}`
    || !Number.isSafeInteger(response.execution_generation)
    || (response.execution_generation as number) < 1
    || response.terminal_protocol_version !== 1
  ) return false;
  if (action.actionKind === "start") {
    const suffix = composerChatActionSuffix(action);
    return draftTargetMatches(response.agent_draft_target, action.agentDraftTarget) && response.status === "started"
      && response.turn_id === `t_${suffix}`
      && response.session_id === (action.sessionId ?? `s_${suffix}`)
      && response.execution_generation === 1;
  }
  return draftTargetMatches(response.agent_draft_target) && response.status === "buffered"
    && response.session_id === action.sessionId
    && response.turn_id === action.expectedTurnId
    && response.execution_generation === action.expectedExecutionGeneration;
}

export async function lookupComposerChatAction(
  action: ComposerChatAction,
): Promise<ComposerChatActionReceipt> {
  const query = new URLSearchParams();
  if (action.actionKind === "steer") {
    query.set("session_id", action.sessionId);
    query.set("expected_turn_id", action.expectedTurnId);
    query.set(
      "expected_execution_generation",
      String(action.expectedExecutionGeneration),
    );
  }
  const response = await boundedComposerControlFetch(apiFetch,
    `/api/v1/chat/actions/${encodeURIComponent(action.actionId)}${query.size ? `?${query.toString()}` : ""}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw await readApiError(response);
  const body = await response.json() as unknown;
  if (!isReceipt(body, action)) {
    throw new Error("composer chat action receipt is invalid");
  }
  return body;
}

// These codes are emitted before the Terminal admission/steer mutation is
// invoked.  Terminal protocol/admission/generation codes are intentionally not
// listed: several of them can be projected after the action was committed.
const DEFINITIVE_PRE_ADMISSION_REJECTION_CODES = new Set([
  "context_maintenance_unsupported",
  "context_maintenance_inputs_unsupported",
  "context_conversation_head_changed",
  "context_maintenance_session_busy",
  "context_maintenance_pending_operation",
  "access_context_denied",
  "attachment_bundle_mismatch",
  "attachment_metadata_stale",
  "attachment_unavailable",
  "attachment_validation_unavailable",
  "auth_required",
  "authz_unavailable",
  "client_refresh_required",
  "datarc_identity_required",
  "datarc_member_not_found",
  "datarc_member_resolve_failed",
  "datarc_unavailable",
  "empty_message",
  "email_not_verified",
  "entitlement_unavailable",
  "invalid_token",
  "member_not_in_workspace",
  "message_too_long",
  "needs_license",
  "orchestrator_not_ready",
  "over_grace",
  "password_changed_relogin_required",
  "plan_already_running",
  "plan_not_approvable",
  "plan_not_found",
  "planning_unavailable",
  "public_entry_team_mismatch",
  "request_validation_failed",
  "result_reference_unavailable",
  "chart_selection_unavailable",
  "subagent_draft_unsaved_changes",
  "agent_draft_target_unavailable",
  "session_deleted",
  "suspended",
  "tamper",
  "team_model_unconfigured",
  "team_membership_required",
  "team_pool_exhausted",
  "too_many_attachments",
  "user_inactive",
  "user_credit_exhausted",
  "clock_tamper",
  "workspace_archived",
  "workspace_unavailable",
  "ws_access_denied",
]);

/** A direct client/server rejection proves the action was not adopted. */
export function isDefinitiveComposerChatActionRejection(
  error: unknown,
  action?: ComposerChatAction,
): boolean {
  const code = (error as { code?: unknown })?.code;
  // Exact steer admission is one transaction. These outcomes are returned
  // only after that transaction rejected/rolled back. Start keeps them
  // unknown because its runtime attach can reuse the same public codes after
  // the Turn admission itself committed.
  if (
    action?.actionKind === "steer"
    && (
      code === "session_turn_active"
      || code === "session_transitioning"
    )
  ) return true;
  return typeof code === "string"
    && DEFINITIVE_PRE_ADMISSION_REJECTION_CODES.has(code);
}
