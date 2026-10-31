import type { FieldContract } from "./configuration-field-guidance";
import { randomUuid } from "@/lib/random-id";
import { appFetch as fetch, appPath } from "@/lib/base-path";
import type { KbParseStatus } from "./kb-api";

/**
 * Thin Bowhead Gateway REST API wrapper.
 *
 * All requests use Next.js rewrite `/api/v1/**` → Gateway :8080.
 * In development with NEXT_PUBLIC_BOWHEAD_SSE_MODE=mock, callers fall back to mock data themselves.
 */

import type {
  AttachmentRef,
  ClarificationAnswer,
  ReportSection,
} from "./types";
import type {
  ResourceUsageResponse,
  ResourceUsageSummary,
} from "./resource-usage";
import {
  applyEntitlementDenial,
  applyEntitlementHeaders,
} from "./entitlement-store";
import {
  ApiError,
  apiFetch,
  apiJson,
  apiVoid,
  HttpStatusError,
  readApiError,
} from "./api-client";
import { genericUserFacingErrorMessage } from "./user-facing-error";
import { responseRequestId } from "./error-presentation";
import {
  parseEffectiveThemeResponse,
  type ChartColorVision,
  type ChartContrast,
  type EffectiveThemeResponse,
} from "./theme-config";
import type { Locale } from "./locale";
import { staticTranslator } from "./locale-messages";
import { getActiveSpaceId, spaceHeader } from "@/lib/space-storage";
import { TIME_ZONE_HEADER, viewerTimeZone, viewerTimeZoneHeaders } from "./time-zone";
import {
  composerChatActionUserMessageId,
  boundedComposerControlFetch,
  isComposerChatActionResponse,
  singleAttemptChatActionFetch,
  type ComposerChatAction,
} from "./composer-chat-action";
import { TERMINAL_CHAT_CAPABILITY_HEADERS } from "./terminal-chat-capability";
import { CHART_STYLE_V2_HEADERS } from "./team-chart-styles-api";
import type { InputDocument } from "./composer-input-document";

/** **Authoritative** union of canvas export formats.
 *
 * Previously duplicated at 10 frontend sites; adding a format required 10 edits, with omissions causing TS errors
 * or silent lack of support. Add formats here only ([[feedback_anthropic_pattern_unify_not_patch]]:
 * the 4th repeated wiring signals centralization; do not add a 5th copy).
 */
export type CanvasExportFmt = "pdf" | "pptx" | "xlsx" | "docx";

export interface SessionGroupDTO {
  id: string;
  label: string;
  items: {
    id: string;
    title: string;
    intents?: string[];
    time?: string;
    /** Last activity, ISO 8601 instant (Gateway 1.5.66+); `time` is the legacy pre-rendered English string. */
    updated_at?: string | null;
    state: "active" | "done" | "error" | "pending";
    unread?: boolean;
    pinned?: boolean;
    archived?: boolean;
    skill_applied?: string | null;
  }[];
}

export interface MemoryEntryDTO {
  id: string;
  kind: string;
  key: string;
  value: unknown;
  locked: boolean;
  lock_reason: string | null;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface SessionListResponseDTO {
  groups: SessionGroupDTO[];
  archived_count?: number;
  /**
   * Opaque keyset cursor for the next page, bound to view/workspace/query;
   * null (or absent from older gateways) on the last page. Cursor pages reuse
   * the group ids: merge items by group id and dedupe by session id.
   */
  next_cursor?: string | null;
  has_more?: boolean;
}

export interface FetchSessionsOptions {
  view?: "active" | "archived";
  query?: string;
  limit?: number;
  /** `next_cursor` from the previous page of the same view/workspace/query. */
  cursor?: string | null;
  workspaceId?: string;
  signal?: AbortSignal;
}

export async function fetchSessions(
  options: FetchSessionsOptions = {},
): Promise<SessionListResponseDTO> {
  const params = new URLSearchParams();
  if (options.view) params.set("view", options.view);
  if (options.query?.trim()) params.set("q", options.query.trim());
  if (options.limit) params.set("limit", String(options.limit));
  if (options.cursor) params.set("cursor", options.cursor);
  const workspaceId =
    options.workspaceId === undefined
      ? getActiveSpaceId()
      : options.workspaceId;
  if (workspaceId) params.set("workspace_id", workspaceId);
  const query = params.toString();
  const r = await fetch(`/api/v1/sessions${query ? `?${query}` : ""}`, {
    // Groups (Today / Yesterday / ...) are computed server-side on the viewer's calendar days.
    headers: { ...spaceHeader(), [TIME_ZONE_HEADER]: viewerTimeZone() },
    cache: "no-store",
    signal: options.signal,
  });
  if (!r.ok) throw new HttpStatusError("sessions", r);
  return r.json();
}

// ─── M156-M3b · Clarification HITL resolve ────────────────────
// V2.7 P1/P2: prefer chosen_values multi-select; chosen_value supports token-kind single-select.
// M158 #10 P1 (2026-04-29): task_plan may include edited_subs (user-edited plan) +
// user_directive (≤4000 characters of free-form instructions), injected into next-turn force_spawn by turn_runner.
// CTO review fixup: one length cap; changes here must align with backend MAX_DIRECTIVE_CHARS.
export const MAX_DIRECTIVE_CHARS = 4000;
export const MAX_EDITED_SUB_TITLE_CHARS = 200;
export const MAX_EDITED_SUB_DESC_CHARS = 1000;
export const MAX_EDITED_SUBS_COUNT = 10;

export interface PendingClarificationDTO {
  request_id: string;
  turn_id: string;
  status: "pending";
}

export interface ClarificationListDTO {
  items: PendingClarificationDTO[];
  resume_failure?: {
    request_id: string;
    turn_id: string;
    execution_generation: number;
    code: "clarification_resume_unavailable";
    recovery_action: "copy_diagnostics";
    diagnostics?: unknown;
  } | null;
}

export async function listClarificationsForTurn(
  turnId: string,
  options: { signal?: AbortSignal } = {},
): Promise<ClarificationListDTO> {
  return apiJson<ClarificationListDTO>(
    `/api/v1/clarifications/turn/${encodeURIComponent(turnId)}`,
    { signal: options.signal },
  );
}

export interface ResolveClarificationPayload {
  chosen_value?: string;
  chosen_values?: string[];
  answers?: ClarificationAnswer[];
  skip?: boolean;
  edited_subs?: { title: string; description: string }[];
  user_directive?: string;
  // Batch 2 (PRD §2.1 step3): tool_context_select "记住选择" has two states:
  // absent/true=explicit permanent preference; false=session soft preference (current session only).
  remember?: boolean;
}

export async function resolveClarification(
  requestId: string,
  payload: ResolveClarificationPayload,
): Promise<{
  ok: boolean;
  request_id: string;
  chosen_value: string;
  chosen_values: string[];
  answers?: ClarificationAnswer[];
  skipped?: boolean;
  workspace_id: string;
  // Sprint chat-v4-streaming Phase 4C — same-turn-resume hint.
  // action="turn_resume" + stream_url means resolve already published the
  // durable resume lease. Older servers may omit it, in which case FE uses the
  // idempotent resume endpoint. action="noop" (default) → FE starts a fresh
  // turn via chat-send (used for clarification rows without resume_context).
  // planning-mode-audit WP-2: action="plan_reapprove" + reapprove_plan_id →
  // Plan preflight picker; FE automatically reapproves this plan directly (no resume / new turn).
  // graph-mode batch 2: "plan_midrun_ack" only acknowledges mid-execution answers (no resume/reapproval,
  // driver picks up at round boundary); after convergence, backend dynamically routes answers to plan_reapprove.
  action?: "turn_resume" | "turn_settled" | "noop" | "plan_reapprove" | "plan_midrun_ack";
  resume_turn_id?: string | null;
  stream_url?: string | null;
  execution_generation?: number | null;
  reapprove_plan_id?: string | null;
}> {
  const r = await apiFetch(
    `/api/v1/clarifications/${encodeURIComponent(requestId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!r.ok) {
    const error = await readApiError(r);
    applyEntitlementDenial(error.code);
    throw error;
  }
  return r.json();
}

// M158 #10 P1 (CTO review #5): user changes mind after resolve; revoke directive + edited_subs.
// Allowed only before injection (injected_at IS NULL). After revocation, next-turn force_spawn omits injection.
export async function cancelClarificationDirective(
  requestId: string,
): Promise<{ ok: boolean; request_id: string; cancelled: boolean }> {
  const r = await apiFetch(
    `/api/v1/clarifications/${encodeURIComponent(requestId)}/cancel-directive`,
    { method: "POST" },
  );
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

// Sprint chat-v4-streaming Phase 4C step 4 — same-turn-resume.
// Called by FE after resolve() returns action="turn_resume". Backend
// reconstructs TurnRequest from resume_context (same turn_id!) + spawns
// orchestrator task. FE then opens EventSource on stream_url (existing
// attachStream logic — bus replays past events + dedupes + subscribes).
export async function resumeTurnAfterClarification(requestId: string): Promise<{
  ok: boolean;
  request_id: string;
  turn_id: string; // == original turn_id (Option A invariant)
  stream_url: string; // /api/v1/chat/stream/{turn_id}
  execution_generation: number;
}> {
  const r = await apiFetch(
    `/api/v1/clarifications/${encodeURIComponent(requestId)}/resume_turn`,
    { method: "POST" },
  );
  if (!r.ok) {
    const error = await readApiError(r);
    applyEntitlementDenial(error.code);
    throw error;
  }
  // §3.7 WARN transport — the grace/degraded health rides the resume 200 header too.
  applyEntitlementHeaders(r);
  return r.json();
}

/**
 * Phase 1.4 — Steer mode send. POSTs the same chat/send endpoint with the
 * existing session_id; backend's steer-mode intercept (chat.py:113-203)
 * routes the message into session_store.steer_buffer + emits `emoji_ack`
 * 🤝 on the active turn's bus instead of starting a fresh turn. The FE
 * does NOT close the active EventSource — the steer ack lands on the
 * same stream.
 *
 * Response status discriminates:
 *   - "buffered"  — steer accepted, ack will arrive on the SSE bus
 *   - "cancelled" — historical submission receipt replay only
 *   - "completed" — the expected turn is no longer active; caller keeps the
 *                   draft instead of promoting it into a fresh task.
 */
export async function postSteerMessage(
  workspaceId: string,
  sessionId: string,
  text: string,
  expectedTurnId: string,
  expectedExecutionGeneration: number,
  attachments: AttachmentRef[] = [],
  userMsgId?: string,
  clientSubmissionId?: string | null,
  chatAction?: ComposerChatAction | null,
  referenceDocument?: InputDocument | null,
  canSendReference?: (() => boolean) | null,
): Promise<{
  session_id: string;
  turn_id: string;
  stream_url: string;
  status: "buffered" | "cancelled" | "completed";
  execution_generation?: number;
  terminal_protocol_version?: number;
}> {
  if (referenceDocument && (!clientSubmissionId || chatAction)) {
    throw new Error("reference steer identity unavailable");
  }
  if (chatAction && clientSubmissionId) {
    throw new Error(
      "composer chat action cannot use legacy submission identity",
    );
  }
  if (chatAction) {
    if (
      chatAction.actionKind !== "steer" ||
      chatAction.sessionId !== sessionId ||
      chatAction.expectedTurnId !== expectedTurnId ||
      chatAction.expectedExecutionGeneration !== expectedExecutionGeneration
    ) {
      throw new Error("composer steer action target mismatch");
    }
  }
  if (attachments.length > 0 && (!clientSubmissionId || chatAction)) {
    throw new Error(
      "composer steer attachments require durable submission support",
    );
  }
  const runtimeAttachmentIds = Array.from(
    new Set(
      attachments.flatMap((attachment) =>
        attachment.bundleArtifactIds?.length
          ? attachment.bundleArtifactIds
          : [attachment.fileId],
      ),
    ),
  );
  const post = () => {
    if (referenceDocument && !canSendReference?.()) {
      throw Object.assign(new Error("reference steer scope changed"), {
        code: "client_refresh_required",
      });
    }
    // An @ steer, like an @ start, is one attempt with no client deadline (an ordinary steer has none either).
    const fetchSteer = referenceDocument
      ? (input: RequestInfo | URL, init?: RequestInit) => singleAttemptChatActionFetch(input, init, null)
      : chatAction
        ? singleAttemptChatActionFetch
        : apiFetch;
    return fetchSteer(
      "/api/v1/chat/send",
      {
        method: "POST",
        headers: {
          "Content-Type": referenceDocument
            ? "application/vnd.bowhead.chat-references.v1+json"
            : "application/json",
          ...(chatAction ? TERMINAL_CHAT_CAPABILITY_HEADERS : {}),
          ...viewerTimeZoneHeaders(),
        },
        body: JSON.stringify({
          workspace_id: workspaceId,
          session_id: sessionId,
          message: text,
          ...(attachments.length
            ? {
                attachments: runtimeAttachmentIds,
                ...(clientSubmissionId ? { attachment_refs: attachments } : {}),
              }
            : {}),
          client_info: {
            composer_surface: "workbench",
            ...(chatAction
              ? {
                  request_id: chatAction.actionId,
                  action_kind: chatAction.actionKind,
                  user_msg_id: composerChatActionUserMessageId(chatAction),
                  expected_turn_id: expectedTurnId,
                  expected_execution_generation: String(
                    expectedExecutionGeneration,
                  ),
                }
              : {
                  expected_turn_id: expectedTurnId,
                  ...(userMsgId ? { user_msg_id: userMsgId } : {}),
                }),
          },
          ...(clientSubmissionId
            ? {
                client_submission_id: clientSubmissionId,
                submission_kind: "steer",
                submission_contract_version: referenceDocument
                  ? "chat-references.v1"
                  : "chat-submit.v1",
                model_control_batch: referenceDocument ? "references" : "a",
                ...(referenceDocument ? { input_document: referenceDocument } : {}),
                expected_parent_turn_id: expectedTurnId,
                expected_execution_generation: expectedExecutionGeneration,
              }
            : {}),
        }),
      },
    );
  };
  let postAttempts = 0;
  const submit = () => {
    postAttempts += 1;
    return post();
  };
  type SteerResponse = {
    session_id: string;
    turn_id: string;
    stream_url: string;
    status: "buffered" | "cancelled" | "completed";
    execution_generation?: number;
    terminal_protocol_version?: number;
  };
  const isSteerResponse = (value: unknown): value is SteerResponse => {
    if (!value || typeof value !== "object") return false;
    const candidate = value as Partial<SteerResponse>;
    return (
      typeof candidate.session_id === "string" &&
      typeof candidate.turn_id === "string" &&
      typeof candidate.stream_url === "string" &&
      ["buffered", "cancelled", "completed"].includes(candidate.status ?? "")
    );
  };
  const checkReferenceSteerResponse = (
    value: unknown,
    expectedOutcome?: string,
  ): SteerResponse => {
    if (!referenceDocument || chatAction) {
      if (!isSteerResponse(value)) throw new Error("steer response is invalid");
      return value;
    }
    if (!isSteerResponse(value)) {
      throw new Error("reference steer response is invalid");
    }
    const identity = (item: string): boolean => item.length > 0
      && item.length <= 128
      && /^[A-Za-z0-9_.:-]+$/.test(item);
    if (
      !identity(value.session_id)
      || value.session_id !== sessionId
      || !identity(value.turn_id)
      || value.terminal_protocol_version != null
      || value.stream_url.length > 2048
      || (value.execution_generation !== undefined && (
        !Number.isSafeInteger(value.execution_generation)
        || value.execution_generation <= 0
      ))
      || (expectedOutcome !== undefined && value.status !== expectedOutcome)
    ) {
      throw new Error("reference steer response target mismatch");
    }
    if (value.status !== "buffered") return value;
    if (
      value.turn_id !== expectedTurnId
      || value.execution_generation !== expectedExecutionGeneration
    ) {
      throw new Error("reference steer response target mismatch");
    }
    let stream: URL;
    try {
      stream = new URL(value.stream_url, window.location.href);
    } catch {
      throw new Error("reference steer response stream invalid");
    }
    if (!stream.pathname.endsWith(`/api/v1/chat/stream/${value.turn_id}`)) {
      throw new Error("reference steer response stream mismatch");
    }
    return value;
  };
  const readReceipt = async (): Promise<SteerResponse | null> => {
    if (!clientSubmissionId) return null;
    const query = new URLSearchParams({
      workspace_id: workspaceId,
      requested_session_id: sessionId,
    });
    const receipt = await apiFetch(
      `/api/v1/chat/submissions/${encodeURIComponent(clientSubmissionId)}?${query.toString()}`,
      { cache: "no-store" },
    );
    if (receipt.ok) {
      const body = (await receipt.json()) as {
        client_submission_id?: unknown;
        outcome: string;
        response?: unknown;
      };
      if (body.client_submission_id !== clientSubmissionId) {
        throw new Error("reference steer receipt identity mismatch");
      }
      if (body.outcome === "rejected") {
        const rejection =
          body.response && typeof body.response === "object"
            ? (body.response as { http_status?: unknown; detail?: unknown })
            : {};
        const rejectionStatus =
          typeof rejection.http_status === "number"
            ? rejection.http_status
            : 409;
        throw await readApiError(
          new Response(
            JSON.stringify({
              detail: rejection.detail ?? {
                error: { code: "submission_rejected" },
              },
            }),
            {
              status: rejectionStatus,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      if (["buffered", "cancelled", "completed"].includes(body.outcome)) {
        if (referenceDocument) {
          return checkReferenceSteerResponse(body.response, body.outcome);
        }
        if (isSteerResponse(body.response)) return body.response;
      }
    } else if (receipt.status !== 404) {
      throw await readApiError(receipt);
    }
    return null;
  };
  let r: Response;
  try {
    r = await submit();
  } catch (firstError) {
    if ((firstError as { code?: unknown })?.code === "client_refresh_required") {
      throw firstError;
    }
    if (!clientSubmissionId) throw firstError;
    const receipt = await readReceipt();
    if (receipt) return receipt;
    // An absent receipt is not proof that the first request never arrived.
    // One same-ID retry is safe because the Gateway commits buffer + receipt
    // in one transaction.
    r = await submit();
  }
  if (clientSubmissionId && !r.ok) {
    const retryableServerFailure = r.status >= 500;
    const pending =
      r.status === 409 &&
      (await readApiError(r.clone())).code === "submission_pending";
    if (retryableServerFailure || pending) {
      const receipt = await readReceipt();
      if (receipt) return receipt;
      if (postAttempts < 2) r = await submit();
    }
  }
  if (!r.ok) {
    throw await readApiError(r);
  }
  try {
    const response = await r.json() as unknown;
    if (chatAction && !isComposerChatActionResponse(response, chatAction)) {
      throw new Error("composer chat action response is invalid");
    }
    return checkReferenceSteerResponse(response);
  } catch (decodeError) {
    if (!clientSubmissionId) throw decodeError;
    const receipt = await readReceipt();
    if (receipt) return receipt;
    if (postAttempts >= 2) throw decodeError;
    const retried = await submit();
    if (!retried.ok) throw await readApiError(retried);
    return checkReferenceSteerResponse(await retried.json() as unknown);
  }
}

const QUEUED_REFERENCE_KINDS = new Set([
  "agent",
  "skill",
  "capability",
  "mcp_source",
  "mcp_tool",
  "mcp_prompt",
]);
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const AWARE_ISO_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

export interface QueuedReferenceSubmissionDTO {
  client_submission_id: string;
  display_text: string;
  resources: {
    kind: "agent" | "skill" | "capability" | "mcp_source" | "mcp_tool" | "mcp_prompt";
    label: string;
  }[];
  attachment_count: number;
  accepted_at: string;
}

function parseQueuedReferenceSubmissions(raw: unknown): QueuedReferenceSubmissionDTO[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("reference queue response is invalid");
  }
  const record = raw as Record<string, unknown>;
  if (Object.keys(record).length !== 1 || !Array.isArray(record.items)) {
    throw new Error("reference queue response is invalid");
  }
  return record.items.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("reference queue item is invalid");
    }
    const value = item as Record<string, unknown>;
    if (
      Object.keys(value).length !== 5
      || typeof value.client_submission_id !== "string"
      || !UUID_V4.test(value.client_submission_id)
      || typeof value.display_text !== "string"
      || [...value.display_text].length > 262_144
      || !Array.isArray(value.resources)
      || value.resources.length > 8
      || !Number.isInteger(value.attachment_count)
      || Number(value.attachment_count) < 0
      || Number(value.attachment_count) > 100
      || typeof value.accepted_at !== "string"
      || !AWARE_ISO_DATETIME.test(value.accepted_at)
      || Number.isNaN(Date.parse(value.accepted_at))
    ) {
      throw new Error("reference queue item is invalid");
    }
    const resources = value.resources.map((resource) => {
      if (!resource || typeof resource !== "object" || Array.isArray(resource)) {
        throw new Error("reference queue resource is invalid");
      }
      const projected = resource as Record<string, unknown>;
      if (
        Object.keys(projected).length !== 2
        || typeof projected.kind !== "string"
        || !QUEUED_REFERENCE_KINDS.has(projected.kind)
        || typeof projected.label !== "string"
        || !projected.label
        || [...projected.label].length > 256
      ) {
        throw new Error("reference queue resource is invalid");
      }
      return projected as QueuedReferenceSubmissionDTO["resources"][number];
    });
    return {
      client_submission_id: value.client_submission_id,
      display_text: value.display_text,
      resources,
      attachment_count: Number(value.attachment_count),
      accepted_at: value.accepted_at,
    };
  });
}

export async function fetchQueuedReferenceSubmissions(
  workspaceId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<QueuedReferenceSubmissionDTO[]> {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  const raw = await apiJson<unknown>(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/reference-queue?${query.toString()}`,
    { cache: "no-store", signal },
  );
  return parseQueuedReferenceSubmissions(raw);
}

export async function cancelQueuedReferenceSubmission(
  workspaceId: string,
  sessionId: string,
  clientSubmissionId: string,
): Promise<void> {
  const raw = await apiJson<unknown>(
    `/api/v1/chat/submissions/${encodeURIComponent(clientSubmissionId)}/cancel`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workspace_id: workspaceId,
        requested_session_id: sessionId,
      }),
    },
  );
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("reference queue cancellation response is invalid");
  }
  const value = raw as Record<string, unknown>;
  if (
    Object.keys(value).length !== 2
    || value.client_submission_id !== clientSubmissionId
    || value.outcome !== "cancelled"
  ) {
    throw new Error("reference queue cancellation response is invalid");
  }
}

export interface WorkspaceDTO {
  id: string;
  name: string;
  desc: string;
  role?: "admin" | "member" | "viewer";
}

// workspace-datarc-decouple R6: the canonical /me/workspaces item shape (Bowhead ws_*).
interface MeWorkspaceItem {
  workspace_id: string;
  name: string;
  description?: string | null;
  role?: WorkspaceDTO["role"];
}

export async function fetchWorkspaces(): Promise<{
  items: WorkspaceDTO[];
  active_id: string | null;
  error?: string;
  /** Failure status / request ID belong only in diagnostics ("复制诊断"), never directly displayed. */
  error_detail?: { status: number; requestId?: string };
}> {
  // workspace-datarc-decouple R6 (post-1c): the canonical Bowhead workspaces
  // (/auth/me/workspaces, membership-based) are the ONLY source — the transitional fallback
  // to the legacy datarc-projects list is REMOVED (it listed every datarc project the global
  // key could see, and the 1c migration has populated the canonical table). Empty list =
  // caller has no workspace memberships (team-less dev-fallback identity, or a fresh user
  // pre-enrollment) → picker shows its empty state; never throw here.
  try {
    const r = await apiFetch("/api/v1/auth/me/workspaces");
    if (!r.ok) {
      return {
        items: [],
        active_id: null,
        error: `workspaces: ${r.status}`,
        error_detail: { status: r.status, requestId: responseRequestId(r) },
      };
    }
    const data = (await r.json()) as {
      items?: MeWorkspaceItem[];
      active_id?: string | null;
    };
    const items: WorkspaceDTO[] = (data.items ?? []).map((w) => ({
      id: w.workspace_id,
      name: w.name,
      desc: w.description ?? "",
      role: w.role,
    }));
    return { items, active_id: data.active_id ?? null };
  } catch (e) {
    return { items: [], active_id: null, error: (e as Error).message };
  }
}

export async function createWorkspace(
  name: string,
  description?: string,
): Promise<{ workspace_id: string }> {
  return apiJson<{ workspace_id: string }>("/api/v1/auth/me/workspaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description: description || undefined }),
  });
}

export async function updateWorkspace(
  workspaceId: string,
  patch: {
    name?: string;
    description?: string;
    status?: "active" | "archived";
  },
): Promise<void> {
  await apiVoid(
    `/api/v1/auth/me/workspaces/${encodeURIComponent(workspaceId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    },
  );
}

// B1: persist the active workspace server-side (preferences.last_workspace_id), which is
// what GET /me/workspaces returns as `active_id`. Was never called by the FE before — the
// picker only wrote localStorage, leaving the server active_id null (→ Overview
// "(none selected)", Workspaces list lacks active marker, turn-runner "workspace_id missing").
// Backend enforces an active-member IDOR guard; callers should treat failure as non-fatal
// (localStorage still provides instant within-device persistence).
export async function setLastWorkspace(workspaceId: string): Promise<void> {
  await apiVoid("/api/v1/auth/me/last-space", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId }),
  });
}

// ─── T1 · Session pinned attachments ────────────────────────────
export interface PinnedAttachmentDTO {
  id: string;
  filename: string;
  mime: string;
  size: number;
}

export async function fetchPinnedAttachments(
  sessionId: string,
): Promise<PinnedAttachmentDTO[]> {
  const r = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/attachments`,
  );
  if (!r.ok) {
    if (r.status === 404 || r.status === 403) return [];
    throw new HttpStatusError("pinned", r);
  }
  const body = (await r.json()) as { items: PinnedAttachmentDTO[] };
  return body.items ?? [];
}

export async function unpinAttachmentFromSession(
  sessionId: string,
  artifactId: string,
): Promise<void> {
  const r = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(artifactId)}`,
    { method: "DELETE" },
  );
  if (!r.ok && r.status !== 404) throw new HttpStatusError("unpin", r);
}

// ─── Skill Management (Phase 1) ─────────────────────────────────
export interface SkillSummaryDTO {
  /** Persisted user-Skill instance ID; empty for built-in Skills without an instance. */
  id?: string | null;
  name: string;
  description: string;
  version: string | null;
  source_type: "builtin" | "user_upload" | "user_generated";
  enabled: boolean;
  size_bytes: number;
  created_at: string | null;
  /** Skills IA redesign: readable title (frontmatter title:, empty defaults to FE name fallback). */
  title?: string;
  /** Explicit Chinese display title for shipped built-ins only. */
  title_zh?: string;
  /** Reviewed user-facing descriptions for unmodified shipped built-ins; `description` is the model-facing text. */
  description_en?: string;
  description_zh?: string;
  description_de?: string;
  description_fr?: string;
  /** Category slug (analysis/documents/design/platform; empty defaults to FE prefix inference). */
  category?: string;
  usage_summary?: ResourceUsageSummary | null;
  /** skill-import batch 1: pending-import state, null | pending | cleared | overridden.
   *  effective = enabled ∧ import_status !== "pending"; pending skills excluded from chat catalog / Agent selection. */
  import_status?: SkillImportStatus | null;
}

export type SkillImportStatus = "pending" | "cleared" | "overridden";

/** One inventory finding (server Finding.to_dict()). Two severity tiers: red blocks activation, yellow does not. */
export interface SkillImportFinding {
  id: string;
  code: string;
  severity: "red" | "yellow";
  path: string;
  line?: number | null;
  subject: string;
  resolved_against: string;
  message_zh: string;
  message_en: string;
}

/** Bundle inventory (server BundleInventory.to_dict()). truncated means shortened findings; say only At Least N Findings. */
export interface SkillImportReport {
  version?: number;
  grade?: "red" | "yellow" | "green";
  red_count?: number;
  findings_digest?: string;
  truncated?: boolean;
  findings?: SkillImportFinding[];
  components?: { kind: string; path: string; meta?: Record<string, unknown> }[];
}

export interface SkillUsedByDTO {
  id: string;
  display_name: string;
}

export interface SkillDetailDTO extends SkillSummaryDTO {
  authoring_enabled?: boolean;
  script_execution?: "none" | "builtin" | "sandbox" | "disabled";
  eval_files?: { name: string; size: number }[];
  /** Display body (**without** frontmatter); promote content CAS compares this too. */
  body_md: string;
  /** Editing: full on-disk SKILL.md (with frontmatter). Save the complete manifest; never use body_md as draft. */
  body_raw?: string;
  allowed_tools: string[];
  scripts: { name: string; size: number }[];
  source_turn_id: string | null;
  trigger_examples: string[];
  references: { name: string; size: number }[];
  evals_count: number;
  has_evals: boolean;
  /** Custom subagents referencing this skill (always empty with subagent_creator flag off). */
  used_by_subagents?: SkillUsedByDTO[];
  /** skill-team-governance: personal-bundle fingerprint (compare team approved_sha256 for synchronized/behind states). */
  content_sha256?: string | null;
  /** skill-import batch 1: bundle inventory; non-null only for personal skills already inventoried. */
  import_report?: SkillImportReport | null;
}

export interface SkillFileContentDTO {
  /** Whole-package revision read with these bytes. */
  content_sha256?: string | null;
  path: string;
  content: string;
  size: number;
  mime: string;
}

function personalSkillsQuery(path: string): string {
  return `${path}${path.includes("?") ? "&" : "?"}scope=personal`;
}

function workspaceHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  return spaceHeader();
}

export async function fetchSkillFile(
  name: string,
  kind: "references" | "scripts" | "evals",
  filename: string,
): Promise<SkillFileContentDTO> {
  return apiJson<SkillFileContentDTO>(
    personalSkillsQuery(
      `/api/v1/skills/${encodeURIComponent(name)}/files/${kind}/${encodeURIComponent(filename)}`,
    ),
    {},
  );
}

/**
 * Direct editing in settings: overwrite one personal-skill bundle file / SKILL.md body.
 *
 * Backend stages, validates the whole bundle, then publishes only this file; failure preserves original content.
 */
export async function putSkillFile(
  name: string,
  kind: "references" | "scripts" | "evals",
  filename: string,
  content: string,
  expectedContentSha256?: string,
  expectedInstanceId?: string,
): Promise<SkillFileContentDTO> {
  const result = await apiJson<SkillFileContentDTO>(
    personalSkillsQuery(
      `/api/v1/skills/${encodeURIComponent(name)}/files/${kind}/${encodeURIComponent(filename)}`,
    ),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, expected_content_sha256: expectedContentSha256, expected_instance_id: expectedInstanceId }),
    },
  );
  _dispatchSkillsChanged();
  return result;
}

export interface SkillFileChange {
  expected_instance_id?: string;
  expected_content_sha256?: string;
  operation: "create" | "rename" | "delete";
  kind: "references" | "scripts" | "evals";
  filename: string;
  target?: string;
  content?: string;
}

export async function changeSkillFile(
  name: string,
  change: SkillFileChange,
): Promise<SkillFileContentDTO> {
  const result = await apiJson<SkillFileContentDTO>(
    personalSkillsQuery(`/api/v1/skills/${encodeURIComponent(name)}/files`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(change),
    },
  );
  _dispatchSkillsChanged();
  return result;
}

export async function generateSkill(bodyMd: string): Promise<SkillDetailDTO> {
  const result = await apiJson<SkillDetailDTO>(
    personalSkillsQuery("/api/v1/skills/generate"),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body_md: bodyMd }),
    },
  );
  _dispatchSkillsChanged();
  return result;
}

export async function putSkillBody(
  name: string,
  content: string,
  expectedContentSha256?: string,
  expectedInstanceId?: string,
): Promise<SkillFileContentDTO> {
  const result = await apiJson<SkillFileContentDTO>(
    personalSkillsQuery(`/api/v1/skills/${encodeURIComponent(name)}/body`),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, expected_content_sha256: expectedContentSha256, expected_instance_id: expectedInstanceId }),
    },
  );
  _dispatchSkillsChanged();
  return result;
}

export interface SkillListDTO {
  authoring_enabled?: boolean;
  impact_guard_enabled?: boolean;
  builtin: SkillSummaryDTO[];
  user_upload: SkillSummaryDTO[];
  user_generated: SkillSummaryDTO[];
}

export async function fetchSkills(): Promise<SkillListDTO> {
  return apiJson<SkillListDTO>(personalSkillsQuery("/api/v1/skills"));
}

export async function fetchSkillDetail(name: string): Promise<SkillDetailDTO> {
  return apiJson<SkillDetailDTO>(
    personalSkillsQuery(`/api/v1/skills/${encodeURIComponent(name)}`),
  );
}

/** skill-import batch 1: knowingly enable despite blockers (pending → overridden); server 409 if not pending. */
export async function overrideSkillImport(
  name: string,
): Promise<{ name: string; import_status: SkillImportStatus }> {
  return apiJson<{ name: string; import_status: SkillImportStatus }>(
    personalSkillsQuery(
      `/api/v1/skills/${encodeURIComponent(name)}/import-override`,
    ),
    { method: "POST" },
  );
}

export async function fetchSkillUsage(
  name: string,
): Promise<ResourceUsageResponse> {
  return apiJson<ResourceUsageResponse>(
    personalSkillsQuery(`/api/v1/skills/${encodeURIComponent(name)}/usage`),
  );
}

export interface SkillResourceChangeImpact {
  kind: "resource_change_impact";
  schema_version: 1;
  action: "patch" | "delete";
  blocking: boolean;
  summary: ResourceUsageSummary;
  new_admission: "blocked_for_affected_agents";
  running_execution: "future_dispatch_blocked";
  already_sent_effect: "not_reversible";
  required_follow_up: Array<{ kind: "schedule_reconfirm"; job_id: string }>;
  impact_token: string | null;
  expires_in_seconds: number;
  command: { action: string; proposed_change: Record<string, unknown> };
  reason_codes: string[];
}

export interface SkillResourceChangeGuard {
  impactToken: string;
  idempotencyKey: string;
}

export function previewSkillImpact(
  name: string,
  body: {
    action: "patch" | "delete";
    proposed_change: Record<string, unknown>;
    team_skill_id?: string;
  },
): Promise<SkillResourceChangeImpact> {
  return apiJson<SkillResourceChangeImpact>(
    personalSkillsQuery(
      `/api/v1/skills/${encodeURIComponent(name)}/impact-preview`,
    ),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

/** B4: every write makes all useSkills() consumers refetch immediately
 *  (cross-page consistency without 5-minute stale TTL).
 *  Detail-page patchSkill/deleteSkill/uploadSkill automatically dispatches; business pages
 *  need not remember events, infrastructure handles them.
 */
function _dispatchSkillsChanged(): void {
  if (typeof window !== "undefined") {
    try {
      window.dispatchEvent(new CustomEvent("bowhead:skills-changed"));
    } catch {
      /* ignore - SSR or restricted ctx */
    }
  }
}

export async function patchSkill(
  name: string,
  body: { enabled?: boolean; description?: string; team_skill_id?: string },
  guard?: SkillResourceChangeGuard,
): Promise<SkillDetailDTO> {
  const out = await apiJson<SkillDetailDTO>(
    personalSkillsQuery("/api/v1/skills/update"),
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        ...(guard
          ? {
              "X-Bowhead-Impact-Token": guard.impactToken,
              "Idempotency-Key": guard.idempotencyKey,
            }
          : {}),
      },
      body: JSON.stringify({ name, ...body }),
    },
  );
  _dispatchSkillsChanged();
  return out;
}

export interface SkillBulkPatchResult {
  items: SkillDetailDTO[];
  errors: { name: string; status: number; detail: unknown }[];
}

export async function patchSkillsBulk(
  items: { name: string; enabled?: boolean; description?: string }[],
): Promise<SkillBulkPatchResult> {
  const out = await apiJson<SkillBulkPatchResult>(
    personalSkillsQuery("/api/v1/skills/bulk"),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    },
  );
  _dispatchSkillsChanged();
  return out;
}

export async function deleteSkill(
  skillId: string,
  guard?: SkillResourceChangeGuard,
): Promise<void> {
  await apiVoid(
    personalSkillsQuery(`/api/v1/skills/${encodeURIComponent(skillId)}`),
    {
      method: "DELETE",
      headers: guard
        ? {
            "X-Bowhead-Impact-Token": guard.impactToken,
            "Idempotency-Key": guard.idempotencyKey,
          }
        : undefined,
    },
  );
  _dispatchSkillsChanged();
}

export async function uploadSkill(
  file: File,
  options: { overwrite?: boolean } = {},
): Promise<SkillDetailDTO> {
  const fd = new FormData();
  fd.append("file", file);
  const q = options.overwrite ? "?overwrite=1" : "";
  const body = await apiJson<SkillDetailDTO>(
    personalSkillsQuery(`/api/v1/skills/upload${q}`),
    {
      method: "POST",
      body: fd,
    },
  );
  _dispatchSkillsChanged();
  return body;
}

export async function remember(
  kind: "preference" | "constraint" | "datarc_binding",
  key: string,
  value: unknown,
): Promise<MemoryEntryDTO> {
  const r = await fetch("/api/v1/memory/user/remember", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, key, value }),
  });
  // Status, error code (memory_write_gate_refused / memory_scope_conflict) and request ID reside on ApiError;
  // /remember uses them for localized explanations, never displaying raw "remember: 500".
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

export async function authorizeDatarcCardUrl(cardUrl: string): Promise<string> {
  const r = await fetch("/api/v1/datarc/authorize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ card_url: cardUrl }),
  });
  if (!r.ok) return cardUrl; // Fallback: use original URL directly.
  const body = (await r.json()) as { authorized_url: string };
  return body.authorized_url;
}

export interface UploadResponse {
  file_id: string;
  filename: string;
  size: number;
  mime: string;
  url: string;
}

// V2.1.5: User Settings (persist PPTX template artifact ID, etc.).
export interface UserSettings {
  pptx_template_artifact_id?: string;
  [k: string]: string | undefined;
}

export async function getUserSettingsView(): Promise<{ settings: UserSettings; field_contract?: FieldContract }> {
  return apiJson(
    "/api/v1/user-settings",
  );
}

export async function getUserSettings(): Promise<UserSettings> {
  return (await getUserSettingsView()).settings ?? {};
}

export async function setUserSetting(
  key: string,
  value: string,
): Promise<void> {
  await apiVoid(`/api/v1/user-settings/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
}

export async function getEffectiveTheme(
  signal?: AbortSignal,
): Promise<EffectiveThemeResponse> {
  const response = await apiJson<unknown>("/api/v1/theme/effective", {
    signal,
    headers: CHART_STYLE_V2_HEADERS,
  });
  return parseEffectiveThemeResponse(response);
}

export async function patchChartAccessibility(patch: {
  contrast?: ChartContrast | null;
  colorVision?: ChartColorVision | null;
}): Promise<void> {
  await apiVoid("/api/v1/theme/chart-style/accessibility", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

/**
 * Cancel a running turn. The accepted response records a durable intent;
 * callers keep SSE attached until the worker publishes turn_cancelled/done.
 *
 * Design: docs/M157-cancel-turn-design.md
 */
export type CancelTurnOutcome = {
  status: "cancelled" | "noop";
  ack: "accepted" | "rejected";
  target_state: "present" | "erased";
  turn_id: string;
  durable: boolean;
  plan_cancelled?: boolean;
  reason?: string;
};

export async function cancelTurn(
  turnId: string,
  expectedExecutionGeneration: number,
  cancelRequestId: string,
): Promise<CancelTurnOutcome> {
  return apiJson(`/api/v1/turns/${encodeURIComponent(turnId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cancel_request_id: cancelRequestId,
      source: "explicit_stop",
      expected_execution_generation: expectedExecutionGeneration,
    }),
  });
}

/** Read the durable Stop receipt after reload or an unknown POST response. */
export async function getCancelTurnOutcome(
  turnId: string,
  expectedExecutionGeneration: number,
  cancelRequestId: string,
): Promise<CancelTurnOutcome> {
  const params = new URLSearchParams({
    expected_execution_generation: String(expectedExecutionGeneration),
  });
  return apiJson(
    `/api/v1/turns/${encodeURIComponent(turnId)}/cancel/${encodeURIComponent(cancelRequestId)}?${params.toString()}`,
  );
}

export async function deleteUserSetting(key: string): Promise<void> {
  await apiVoid(`/api/v1/user-settings/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
}

// ─── i18n v1 · workspace settings ───────────────────────────
// Key fields:
//   i18n_v1               "true" | "false"
//   default_ui_language   "zh-CN" | "en-US" | "de-DE" | "fr-FR"
//   default_data_language "zh-CN" | "en-US"
//   week_start_day        "monday" | "sunday"
export interface WorkspaceSettings {
  [k: string]: string | undefined;
}

export async function getWorkspaceSettingsView(
  workspaceId: string,
): Promise<{ settings: WorkspaceSettings; field_contract?: FieldContract }> {
  return apiJson(
    `/api/v1/workspace-settings/${encodeURIComponent(workspaceId)}`,
  );
}

export async function getWorkspaceSettings(workspaceId: string): Promise<WorkspaceSettings> {
  return (await getWorkspaceSettingsView(workspaceId)).settings ?? {};
}

export async function setWorkspaceSetting(
  workspaceId: string,
  key: string,
  value: string,
): Promise<void> {
  await apiVoid(
    `/api/v1/workspace-settings/${encodeURIComponent(workspaceId)}/${encodeURIComponent(key)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    },
  );
}

export interface WorkspaceMetadata {
  field_contract?: FieldContract;
  name: string;
  description: string | null;
  revision: number;
}

export async function getWorkspaceMetadata(
  workspaceId: string,
): Promise<WorkspaceMetadata> {
  return apiJson(
    `/api/v1/workspace-settings/${encodeURIComponent(workspaceId)}/metadata`,
  );
}

export async function updateWorkspaceMetadata(
  workspaceId: string,
  patch: {
    name?: string;
    description?: string | null;
    expected_revision: number;
  },
): Promise<WorkspaceMetadata> {
  return apiJson(
    `/api/v1/workspace-settings/${encodeURIComponent(workspaceId)}/metadata`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    },
  );
}

// Space instructions — MIRRORS packages/bowhead_gateway/.../routes/space_instructions.py
export interface SpaceInstructions {
  field_contract?: FieldContract;
  workspace_id: string;
  /** 0 when the Space has never had instructions. */
  version: number;
  content: string;
  updated_by_name: string | null;
  updated_at: string | null;
  max_chars: number;
  can_edit: boolean;
  /** Archived Spaces keep their instructions readable but reject changes until restored. */
  archived: boolean;
  /** Display names of the Space administrators, so readers know whom to ask. */
  administrators: string[];
}

export interface SpaceInstructionsVersion {
  version: number;
  content: string;
  created_by: string;
  created_by_name: string;
  created_at: string;
  restored_from: number | null;
  /** The adopted suggestion inbox candidate whose wording this version added. */
  source_candidate_id: string | null;
}

export async function getSpaceInstructions(workspaceId: string): Promise<SpaceInstructions> {
  return apiJson(`/api/v1/space-instructions/${encodeURIComponent(workspaceId)}`);
}

export async function saveSpaceInstructions(
  workspaceId: string,
  body: { content: string; expected_version: number; restored_from?: number; source_candidate_id?: string },
): Promise<SpaceInstructions> {
  return apiJson(`/api/v1/space-instructions/${encodeURIComponent(workspaceId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function listSpaceInstructionVersions(workspaceId: string): Promise<SpaceInstructionsVersion[]> {
  const result = await apiJson<{ versions: SpaceInstructionsVersion[] }>(
    `/api/v1/space-instructions/${encodeURIComponent(workspaceId)}/versions`,
  );
  return result.versions;
}

// ────────────────────────────────────────────────────────────────
// Sprint file-upload-presign Phase 2 — presign types + size dispatch.
//
// MIRRORS: packages/bowhead_gateway/.../routes/files_presign.py
//   FilesPresignRequest / FilesPresignResponse / FilesConfirmRequest /
//   FilesConfirmResponse. Round-trip fixture test at
//   __tests__/upload-presign.test.ts pins the wire shape.
// ────────────────────────────────────────────────────────────────

export interface PresignUploadEnvelope {
  url: string;
  fields: Record<string, string>;
  method: "POST";
}

export interface FilesPresignResponse {
  file_id: string;
  artifact_id: string;
  upload: PresignUploadEnvelope;
  expires_at: string; // ISO8601
  confirm_url: string;
}

export interface FilesConfirmResponse extends UploadResponse {
  status: "ready";
  idempotent_replay: boolean;
}

export interface UploadOptions {
  sessionId?: string;
  /** Capture the upload scope before asynchronous hashing or transfer. */
  spaceHeaders?: Record<string, string>;
  onProgress?: (pct: number) => void;
  signal?: AbortSignal;
}

export async function uploadFile(
  file: File,
  opts: UploadOptions = {},
): Promise<UploadResponse> {
  opts = { ...opts, spaceHeaders: { ...(opts.spaceHeaders ?? spaceHeader()) } };
  if (file.size < PRESIGN_THRESHOLD) {
    return uploadSmallFile(file, opts);
  }
  return uploadLargeFile(file, opts);
}

/** Authenticated multipart transport, also used when presign is unsupported. */
async function uploadSmallFile(
  file: File,
  opts: UploadOptions = {},
): Promise<UploadResponse> {
  const fd = new FormData();
  fd.append("file", file);
  const query = opts.sessionId
    ? `?${new URLSearchParams({ session_id: opts.sessionId })}`
    : "";
  const r = await fetch(`/api/v1/files/upload${query}`, {
    method: "POST",
    headers: opts.spaceHeaders ?? spaceHeader(),
    body: fd,
    signal: opts.signal,
  });
  if (!r.ok) throw await readApiError(r);
  opts.onProgress?.(100);
  return r.json();
}

function officeMultipartRequired(error: unknown, file: File): boolean {
  const ext = file.name.toLowerCase().match(/\.[a-z0-9]+$/)?.[0] ?? "";
  return (
    error instanceof ApiError &&
    error.code === "file_office_multipart_required" &&
    OFFICE_UPLOAD_MIMES.has(ext) &&
    file.size <= 32 * 1024 * 1024
  );
}

/** Only a presign refusal before pending admission can negotiate multipart. */
export function isMultipartUploadRequired(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.code === "file_multipart_required";
}

/**
 * Presigned-POST large-file path (tech-design §5.1a + §5.3a).
 *
 * 1. POST /files/presign → envelope + confirm_url
 * 2. POST envelope.url with envelope.fields + file (via XHR for progress)
 * 3. POST confirm_url → FilesConfirmResponse
 *
 * Retry policy per tech-design §5.5:
 *   - POST 403 → re-presign + retry once (PresignExpiredError caught)
 *   - POST 5xx / network → up to 2 retries in uploadViaEnvelopeWithRetry
 *   - confirm 409 'confirming' → wait 1s + retry once
 *   - confirm 404 → caller re-presigns (we surface)
 *   - confirm 415 → terminal (mime swap; no retry)
 */
export async function uploadLargeFile(
  file: File,
  opts: UploadOptions = {},
): Promise<UploadResponse> {
  opts = { ...opts, spaceHeaders: { ...(opts.spaceHeaders ?? spaceHeader()) } };
  const scopeHeaders = opts.spaceHeaders!;
  const {
    sha256ChunkedHex,
    uploadViaEnvelopeWithRetry,
    PresignExpiredError,
    ConfirmExpiredError,
  } = await import("./upload-xhr");
  const checksum = await sha256ChunkedHex(file);

  // Phase 2 wraps presign + upload + confirm in a single retry-on-expired loop.
  // Phase 2 review P1-1: confirm 410 also triggers re-presign (same outer loop)
  // so the user-driven gap closes for confirm-time expiry (clock skew or slow
  // head() probe).
  let attempt = 0;
  for (;;) {
    opts.signal?.throwIfAborted();
    let presign: FilesPresignResponse;
    try {
      presign = await issueFilesPresign(file, checksum, opts.sessionId, scopeHeaders, opts.signal);
    } catch (error) {
      if (isMultipartUploadRequired(error) || officeMultipartRequired(error, file))
        return uploadSmallFile(file, { ...opts, spaceHeaders: scopeHeaders });
      throw error;
    }
    try {
      await uploadViaEnvelopeWithRetry(presign.upload, file, {
        onProgress: opts.onProgress,
        signal: opts.signal,
      });
    } catch (e) {
      if (e instanceof PresignExpiredError && attempt < 1) {
        attempt++;
        continue; // re-presign + retry
      }
      throw e;
    }
    try {
      return await confirmFilesPresign(presign.confirm_url, {
        session_id: opts.sessionId,
        uploaded_size: file.size,
        checksum_sha256: checksum,
      }, scopeHeaders, opts.signal);
    } catch (e) {
      if (officeMultipartRequired(e, file)) return uploadSmallFile(file, { ...opts, spaceHeaders: scopeHeaders });
      if (e instanceof ConfirmExpiredError && attempt < 1) {
        attempt++;
        continue; // re-presign + retry
      }
      throw e;
    }
  }
}

async function issueFilesPresign(
  file: File,
  checksum: string,
  sessionId: string | undefined,
  scopeHeaders: Record<string, string>,
  signal?: AbortSignal,
): Promise<FilesPresignResponse> {
  const r = await fetch("/api/v1/files/presign", {
    method: "POST",
    signal,
    headers: { ...scopeHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: file.name,
      mime: file.type || "application/octet-stream",
      size_bytes: file.size,
      session_id: sessionId ?? null,
      checksum_sha256: checksum || null,
    }),
  });
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

async function confirmFilesPresign(
  confirmUrl: string,
  body: {
    session_id?: string;
    uploaded_size: number;
    checksum_sha256: string;
  },
  scopeHeaders: Record<string, string>,
  signal?: AbortSignal,
): Promise<FilesConfirmResponse> {
  let attempt = 0;
  for (;;) {
    signal?.throwIfAborted();
    const r = await fetch(confirmUrl, {
      method: "POST",
      signal,
      headers: { ...scopeHeaders, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (r.ok) return r.json();
    const error = await readApiError(r);
    // Only an in-flight confirmation is retryable; conflicts and Office
    // negotiation must reach their caller without a duplicate confirmation.
    if (r.status === 409 && error.code === "file_confirm_in_progress" && attempt < 1) {
      const { sleep } = await import("./upload-xhr");
      attempt++;
      await sleep(1000);
      continue;
    }
    // Phase 2 review P1-1: 410 (presign expired and not uploaded) → throw
    // typed error so outer loop can re-presign + retry once.
    if (r.status === 410) {
      const { ConfirmExpiredError } = await import("./upload-xhr");
      throw new ConfirmExpiredError();
    }
    throw error;
  }
}

// ─── File management ────────────────────────────────────────────
/** KB record projection in /files list; same field names / semantics as KbFileDTO. */
export interface FileKbStatus {
  /** KB file ID (for /knowledge?file_id= deep links), not artifact ID. */
  file_id: string;
  scope: "personal" | "space" | "team";
  /** Space records only: owning space (you are a member), selected when following deep link. */
  workspace_id?: string | null;
  parse_status: KbParseStatus;
  parse_progress_pct: number | null;
  parse_stuck: boolean;
  parse_error_code: string | null;
}

export interface FileDTO {
  file_id: string;
  filename: string;
  mime: string;
  mime_family:
    | "image"
    | "pdf"
    | "presentation"
    | "spreadsheet"
    | "document"
    | "text"
    | "other";
  size: number;
  source: string; // raw enum from backend (upload | canvas_export | python_sandbox | sandbox_output | bi_fetch_rows | …)
  // Sprint ι (PRD §4.B.3): user-facing origin enum mapped from source.
  // Frontend uses next-intl `useTranslations("dataTab.origin")` to localize.
  // file-management-v2 P0-5: backend also emits web (web_clip) / deck
  // (deliverable) / other (unknown-source fallback).
  origin: "query" | "compute" | "upload" | "export" | "web" | "deck" | "other";
  created_at: string;
  last_used_at: string | null;
  deleted_at: string | null;
  category: string | null;
  category_source: string | null; // "llm" | "user" | "rule"
  usage_count: number;
  // file-management-v2 P1: asset-model kind determines three library tabs / category badges.
  // (lib/files-taxonomy.ts). Keep origin for the Canvas Data tab to consume.
  kind: string;
  trust: string;
  // Whether this file has an active knowledge-base overlay. true → show the "知识库" badge, and the delete entry
  // becomes a navigation link (D2: knowledge-base files cannot be deleted on the files page, or retrieval of originals returns 404).
  in_kb: boolean;
  /** Backend parser and asset kind jointly determine whether manual addition to the knowledge base is allowed. */
  kb_promotable: boolean;
  /**
   * Knowledge-base UX cleanup FR7: status of the knowledge-base record visible to you (map five states with lib/kb-file-status.ts).
   * `in_kb` true with null here: the file is in the knowledge base but outside your visibility scope; omit its status.
   */
  kb?: FileKbStatus | null;
  /** "来源会话" — which task produced this file (a column in the runtime-data tab). */
  session_id: string | null;
  session_title: string | null;
  /** Attached to a canvas/report — warn additionally on deletion (downloads there will stop working). */
  has_canvas_ref: boolean;
  /** D7 retention rule: only long_term files are automatically indexed (this drives the badge's ✨). */
  retention_hint: string | null;
  url: string;
}

export interface ListFilesParams {
  q?: string;
  mime_family?: string[];
  source?: string[];
  /** file-management-v2 P1: asset-library tab filter (AND with source; usually only one is supplied). */
  kind?: readonly string[];
  since?: string;
  until?: string;
  sort?: "created_desc" | "size_desc" | "last_used_desc";
  limit?: number;
  offset?: number;
  include_deleted?: boolean;
  /** 1 = only files added to the knowledge base; 0 = exclude them (server filters before pagination). */
  in_kb?: 0 | 1;
  /** Business classification (server filters before pagination). */
  category?: string;
}

export async function listFiles(p: ListFilesParams = {}): Promise<{
  total: number;
  size_total_bytes: number;
  items: FileDTO[];
}> {
  const qs = new URLSearchParams();
  if (p.q) qs.set("q", p.q);
  (p.mime_family || []).forEach((f) => qs.append("mime_family", f));
  (p.source || []).forEach((s) => qs.append("source", s));
  (p.kind || []).forEach((k) => qs.append("kind", k));
  if (p.since) qs.set("since", p.since);
  if (p.until) qs.set("until", p.until);
  if (p.sort) qs.set("sort", p.sort);
  qs.set("limit", String(p.limit ?? 50));
  qs.set("offset", String(p.offset ?? 0));
  if (p.include_deleted) qs.set("include_deleted", "1");
  if (p.in_kb !== undefined) qs.set("in_kb", String(p.in_kb));
  if (p.category) qs.set("category", p.category);
  const r = await fetch(`/api/v1/files/?${qs.toString()}`);
  if (!r.ok) throw await readApiError(r);
  const body = (await r.json()) as Partial<{
    total: number;
    size_total_bytes: number;
    items: FileDTO[];
  }>;
  const items = Array.isArray(body.items) ? body.items : [];
  const total =
    typeof body.total === "number" && Number.isFinite(body.total)
      ? body.total
      : items.length;
  const sizeTotal =
    typeof body.size_total_bytes === "number" &&
    Number.isFinite(body.size_total_bytes)
      ? body.size_total_bytes
      : items.reduce(
          (sum, item) =>
            sum +
            (typeof item.size === "number" && Number.isFinite(item.size)
              ? item.size
              : 0),
          0,
        );
  return { total, size_total_bytes: sizeTotal, items };
}

/** Backend D2 guard refuses deletion (this artifact is still referenced by the knowledge base). */
export class FileInKbError extends Error {
  constructor(public readonly fileId: string) {
    super(`deleteFile: ${fileId} is referenced by the knowledge base`);
    this.name = "FileInKbError";
  }
}

export async function deleteFile(fileId: string): Promise<void> {
  const r = await fetch(`/api/v1/files/${encodeURIComponent(fileId)}`, {
    method: "DELETE",
  });
  if (!r.ok && r.status !== 204) {
    const error = await readApiError(r);
    // Knowledge-base reference guard (D2). Branch only on the stable code; do not mistake other 409s for this state.
    if (error.code === "file_in_knowledge_base") {
      throw new FileInKbError(fileId);
    }
    throw error;
  }
}

/**
 * file-management-v2 P2.5: add an existing file to the knowledge base (zero-copy, reuse the same blob).
 *
 * The endpoint is under KB routes rather than /files — the complete knowledge-base write authorization chain (scope grant / Space
 * context / entitlement) lives there. The server resolves `scope_id` from scope; the client omits it.
 */
export async function promoteFileToKb(
  fileId: string,
  scope: "personal" | "space" | "team" = "personal",
): Promise<{ file_id: string; workspace_id?: string | null }> {
  const r = await fetch(`/api/v1/kb/files/from-artifact`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...workspaceHeaders() },
    body: JSON.stringify({ artifact_id: fileId, scope, knowledge_intent: "add_to_knowledge", action_id: randomUuid() }),
  });
  if (!r.ok) {
    // Stable UI error codes: do not expose raw parser reasons or backend 500 text to users.
    if (r.status === 503 && (await readApiError(r)).code === "kb_embedding_not_configured") {
      throw new Error("kb_embedding_not_configured");
    }
    if (r.status === 400) throw new Error("space_required");
    if (r.status === 403) throw new Error("forbidden");
    if (r.status === 422) throw new Error("unsupported");
    throw new Error("promotion_failed");
  }
  return r.json();
}

/** Remove from the knowledge base — AI stops referencing it automatically; the file itself remains. */
export async function removeFileFromKb(fileId: string): Promise<void> {
  const r = await fetch(
    `/api/v1/kb/files/by-artifact/${encodeURIComponent(fileId)}`,
    { method: "DELETE", headers: workspaceHeaders() },
  );
  if (!r.ok && r.status !== 204) {
    if (r.status === 400) throw new Error("space_required");
    if (r.status === 403) throw new Error("forbidden");
    throw new Error("removal_failed");
  }
}

/** P4 "永久删除": purge specified files from trash without waiting for 30-day GC.
 *
 * Returns `{purged, kb_blocked}` — files referenced by the knowledge base **are not** deleted (D2 uses the same lock:
 * this path lacks even the 30-day recovery window, so a missed check irreversibly damages the knowledge base). Callers must
 * explain `kb_blocked` to users, never silently discard it. */
export async function purgeFiles(
  fileIds: string[],
): Promise<{ purged: number; kb_blocked: number }> {
  const r = await fetch(`/api/v1/files/purge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ artifact_ids: fileIds }),
  });
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

/** P4 usage: bytes occupied by assets / runtime data separately.
 *
 * Return two numbers separately — one total cannot distinguish My PPT Uses 2GB from The Agent's Intermediate
 * CSV Uses 2GB; their cleanup methods differ entirely (users clear the former; the latter waits for TTL). */
export async function fetchFilesUsage(): Promise<{
  assets: number;
  run_data: number;
}> {
  const r = await fetch(`/api/v1/files/usage`);
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

export async function restoreFile(fileId: string): Promise<FileDTO> {
  const r = await fetch(`/api/v1/files/${encodeURIComponent(fileId)}/restore`, {
    method: "POST",
  });
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

export interface FileUsage {
  session_id: string;
  session_title: string | null;
  turn_id: string;
  turn_seq: number;
  user_text: string;
  used_at: string;
}

// file-management-v2 P0-8: backend FileUsagesDTO has only the usages field (previously sent items incorrectly,
// returning undefined from this function and crashing the drawer's "被使用在" section — route tests now lock the contract).
export async function fetchFileUsages(fileId: string): Promise<{
  usages: FileUsage[];
}> {
  const r = await fetch(`/api/v1/files/${encodeURIComponent(fileId)}/usages`);
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

export interface FilePreview {
  family: string;
  mode?: "text" | "rows" | "image" | string;
  text: string | null;
  truncated?: boolean;
  error?: string | null;
  error_code?: string | null;
  note?: string;
  columns?: string[] | null;
  rows?: Record<string, unknown>[] | null;
  total_rows?: number | null;
  shown_rows?: number | null;
}
export async function fetchFilePreview(
  fileId: string,
  opts: { mode?: "auto" | "text" | "rows" | "image"; maxRows?: number } = {},
): Promise<FilePreview> {
  const params = new URLSearchParams();
  params.set("mode", opts.mode ?? "auto");
  if (opts.maxRows) params.set("max_rows", String(opts.maxRows));
  const r = await fetch(
    `/api/v1/files/${encodeURIComponent(fileId)}/preview?${params}`,
  );
  if (!r.ok) throw await readApiError(r);
  const preview = (await r.json()) as FilePreview;
  if (preview.error_code || preview.error) {
    preview.error = genericUserFacingErrorMessage();
  }
  return preview;
}

// File categories are language-neutral codes: the backend stores and accepts exactly these
// values, and only the rendered LABEL is localized (messages `api_client.file_categories`,
// keyed by the same code).
export const FILE_CATEGORIES = [
  "report",
  "dataset",
  "reference",
  "meeting_notes",
  "metric_definition",
  "screenshot",
  "other",
] as const;
export type FileCategory = (typeof FILE_CATEGORIES)[number];

export function isFileCategory(value: string | null | undefined): value is FileCategory {
  return (FILE_CATEGORIES as readonly string[]).includes(value ?? "");
}

/** Locale-aware display label for a file category code. Unknown codes are shown verbatim. */
export function fileCategoryLabel(category: string): string {
  return isFileCategory(category) ? staticTranslator("api_client.file_categories")(category) : category;
}

export async function setFileCategory(
  fileId: string,
  category: FileCategory,
): Promise<FileDTO> {
  const r = await fetch(
    `/api/v1/files/${encodeURIComponent(fileId)}/category`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category }),
    },
  );
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

// Align the allowlist with backend ALLOWED_MIME (see bowhead_gateway/routes/files.py).
// Server admission remains authoritative; new template containers require
// the Office document flow. Selecting a file never assigns its request role.
const OFFICE_UPLOAD_MIMES = new Map([
  [
    ".docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ],
  [
    ".dotx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
  ],
  [
    ".pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ],
  [
    ".potx",
    "application/vnd.openxmlformats-officedocument.presentationml.template",
  ],
]);

/** Office containers may have ZIP MIME, but must not enter KB bundle ingestion. */
export function isOfficeUpload(file: File): boolean {
  return OFFICE_UPLOAD_MIMES.has(
    file.name.toLowerCase().match(/\.[a-z0-9]+$/)?.[0] ?? "",
  );
}

export const UPLOAD_ALLOWED_MIME = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "application/pdf",
  "text/plain",
  "text/csv",
  "text/tab-separated-values",
  "text/markdown",
  "text/html",
  "application/xhtml+xml",
  "application/json",
  "application/x-ndjson",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
  "application/vnd.openxmlformats-officedocument.presentationml.template",
  "application/zip",
  "application/x-zip-compressed",
]);
// Frontend input[accept] values; align with MIME types (browsers sometimes provide only extensions).
export const UPLOAD_ACCEPT =
  ".pdf,.pptx,.potx,.xlsx,.xls,.csv,.tsv,.json,.jsonl,.ndjson,.docx,.dotx,.txt,.md,.html,.htm,.xhtml,.zip,.png,.jpg,.jpeg,.webp,image/*";
// Sprint file-upload-presign Phase 2: 100MB → 2GB. Large files bypass the
// gateway via presign-direct-upload (tech-design §1).
export const UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024;
const UPLOAD_MIME_REQUIRED_EXTENSIONS = new Map<string, Set<string>>([
  ["text/plain", new Set([".txt"])],
  ["text/markdown", new Set([".md"])],
  ["text/csv", new Set([".csv"])],
  ["text/tab-separated-values", new Set([".tsv"])],
  ["application/json", new Set([".json"])],
  ["application/x-ndjson", new Set([".jsonl", ".ndjson"])],
  ["text/html", new Set([".html", ".htm"])],
  ["application/xhtml+xml", new Set([".xhtml"])],
]);
const UPLOAD_EXT_FALLBACK_MIME = new Map<string, string>([
  [".md", "text/markdown"],
  [".txt", "text/plain"],
  [".csv", "text/csv"],
  [".tsv", "text/tab-separated-values"],
  [".json", "application/json"],
  [".jsonl", "application/x-ndjson"],
  [".ndjson", "application/x-ndjson"],
  [".html", "text/html"],
  [".htm", "text/html"],
  [".xhtml", "application/xhtml+xml"],
  // Align with Gateway _EXT_FALLBACK_MIME: clients without MIME mappings report OOXML as octet-stream.
  [
    ".xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ],
  [".zip", "application/zip"],
]);
const UPLOAD_BROWSER_AMBIGUOUS_MIME = new Set([
  "application/octet-stream",
  "text/plain",
]);
export const UPLOAD_MAX_PER_MESSAGE = 5;
// Files >= this threshold use the new presign path; smaller files keep the
// existing multipart /files/upload endpoint (battle-tested, no churn).
export const PRESIGN_THRESHOLD = 10 * 1024 * 1024;

function formatMB(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) {
    return `${(mb / 1024).toFixed(1)}GB`;
  }
  return `${mb.toFixed(1)}MB`;
}

function resolveUploadMime(type: string, ext: string): string {
  const officeMime = OFFICE_UPLOAD_MIMES.get(ext);
  if (officeMime) {
    return new Set([
      "",
      "application/octet-stream",
      "application/zip",
      "application/x-zip-compressed",
      officeMime,
    ]).has(type)
      ? officeMime
      : "";
  }
  if ([...OFFICE_UPLOAD_MIMES.values()].includes(type)) return "";
  const htmlMimes = new Set(["text/html", "application/xhtml+xml"]);
  const htmlExtensions = new Set([".html", ".htm", ".xhtml"]);
  if (htmlMimes.has(type)) {
    return UPLOAD_MIME_REQUIRED_EXTENSIONS.get(type)?.has(ext) ? type : "";
  }
  if (htmlExtensions.has(ext) && !UPLOAD_BROWSER_AMBIGUOUS_MIME.has(type)) {
    return "";
  }
  const fallback = UPLOAD_EXT_FALLBACK_MIME.get(ext);
  const browserJsonlFallback =
    type === "application/json" && (ext === ".jsonl" || ext === ".ndjson");
  if (
    fallback &&
    (!UPLOAD_ALLOWED_MIME.has(type) ||
      UPLOAD_BROWSER_AMBIGUOUS_MIME.has(type) ||
      browserJsonlFallback)
  ) {
    return fallback;
  }
  return type;
}

/** Local prevalidation failure reason: kind selects the UI hint; message is a sentence for users. */
export interface UploadValidationIssue {
  kind: "size" | "unsupported";
  message: string;
}

/** Local prevalidation: size + MIME; return a failure reason or null. */
export function validateUpload(file: File): UploadValidationIssue | null {
  const t = staticTranslator("api_client.upload");
  const unsupported = (type: string): UploadValidationIssue => ({
    kind: "unsupported",
    message: t("unsupported_type", { type: type || t("unknown_type") }),
  });
  if (file.size > UPLOAD_MAX_BYTES) {
    return {
      kind: "size",
      message: t("too_large", { size: formatMB(file.size), max: formatMB(UPLOAD_MAX_BYTES) }),
    };
  }
  const ext = file.name.toLowerCase().match(/\.[a-z0-9]+$/)?.[0] ?? "";
  const allowedExt = new Set([
    ".md",
    ".txt",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".html",
    ".htm",
    ".xhtml",
    ".zip",
    ".pdf",
    ".xls",
    ".xlsx",
    ".pptx",
    ".potx",
    ".docx",
    ".dotx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
  ]);
  if (file.type) {
    const effectiveType = resolveUploadMime(file.type, ext);
    const typeAllowed = UPLOAD_ALLOWED_MIME.has(effectiveType);
    const browserTextFallback =
      effectiveType.startsWith("text/") && allowedExt.has(ext);
    if (!typeAllowed && !browserTextFallback) {
      return unsupported(file.type || ext);
    }
    const requiredExt = UPLOAD_MIME_REQUIRED_EXTENSIONS.get(effectiveType);
    if (requiredExt && !requiredExt.has(ext)) {
      return unsupported(file.type || ext);
    }
    return null;
  }
  if (!allowedExt.has(ext)) {
    return unsupported(ext);
  }
  return null;
}

// ─────────────────────────────────────────────────────────
// Canvas API
// ─────────────────────────────────────────────────────────

export interface CanvasDTO {
  canvas: {
    id: string;
    sessionId: string;
    workspaceId: string;
    title: string | null;
    status: string;
    createdAt: string;
    updatedAt: string;
  } | null;
  sections: ReportSection[];
  /** Viewer-safe source presentations. IDs are report-scoped opaque aliases;
   *  canonical tool/resource identities never cross this API boundary. */
  sourceRefs?: import("./canvas-pm-adapter/citation-types").SourceRef[];
  /** Canvas export reliability: identity of the persisted current-content set. */
  projectionToken?: string | null;
  /** Page/export shared placement plan. */
  surfaceProjection?: CanvasSurfaceProjectionDTO | null;
  /** Identity of surfaceProjection, duplicated for convenient conflict checks. */
  surfaceToken?: string | null;
  /** Runtime-ready V2 processors after server flags and dependency checks. */
  exportV2Capabilities?: CanvasExportV2CapabilityDTO[];
  /** A3 (report-edit-audit): true = persisted seq is already the authoritative display order → render by seq (preserve the user's
   *  drag order); false/absent = legacy data or flag off → fall back to preprocessSections' forced rank ordering. */
  seqAuthoritative?: boolean;
  /** source-accessibility c1/c2: turn_id → viewer-safe citation rows (resolution data for body [cite:N] markers). */
  turnCitations?: import("./canvas-pm-adapter/citation-types").TurnCitationsMap;
}

export interface CanvasExportV2CapabilityDTO {
  format: CanvasExportFmt;
  profile: "full_report" | "executive_summary" | "data_workbook";
}

export interface CanvasSurfaceEntryDTO {
  sectionId: string;
  sectionRev: number;
  order: number;
  placement:
    | "report_body"
    | "report_embedded"
    | "data_tab"
    | "code_tab"
    | "sources"
    | "excluded";
  parentSectionId: string | null;
  effectiveKind: string;
  parityRequirement:
    | "semantic_content"
    | "exact_visual"
    | "typed_data"
    | "interaction_fallback"
    | "summary_policy";
  /** W3: canonical visual input shared by the page and export materializer. */
  visualInputSha256?: string | null;
}

export interface CanvasSurfaceExclusionDTO {
  sectionId: string;
  reason:
    | "hidden_for_chat_mode"
    | "superseded_history"
    | "quarantined_rejected";
}

export interface CanvasSurfaceProjectionDTO {
  contractVersion: number;
  contentProjectionToken: string;
  surfaceToken: string;
  entries: CanvasSurfaceEntryDTO[];
  exclusions: CanvasSurfaceExclusionDTO[];
}

/** content-permissions wave 2: a shared report the viewer can't access 403s with
 *  {code: report_unavailable}. Sentinel error so the landing path can render the
 *  ReportUnavailable placeholder (+ "申请访问") instead of a generic failure. */
export class ReportUnavailableError extends Error {
  readonly sessionId: string;
  constructor(sessionId: string) {
    super("report_unavailable");
    this.name = "ReportUnavailableError";
    this.sessionId = sessionId;
  }
}

export async function fetchCanvasBySession(
  sessionId: string,
  opts?: { expectReport?: boolean },
): Promise<CanvasDTO> {
  // citation-audit M3-3 (§4.5 i18n inline-numbers v1 retired): previously, `raw=true` was required
  // to receive raw DB text (otherwise backend render_inline_numbers rendered `[num:..]` as
  // "302,376,608.44", so citation-plugin's regex could not match → numbers lost provenance). That
  // rendering half is fully retired — **backend now always returns raw DB text**, so the parameter is removed.
  const params = new URLSearchParams();
  if (opts?.expectReport) params.set("expect_report", "true");
  const r = await fetch(
    `/api/v1/canvas/by-session/${encodeURIComponent(sessionId)}?${params.toString()}`,
  );
  if (!r.ok) {
    if (r.status === 403) {
      const body = await r.json().catch(() => null);
      const code = body?.detail?.code ?? body?.code;
      if (code === "report_unavailable")
        throw new ReportUnavailableError(sessionId);
    }
    throw new HttpStatusError("canvas", r);
  }
  return r.json() as Promise<CanvasDTO>;
}

export async function fetchCanvasById(
  canvasId: string,
  opts?: { locale?: Locale },
): Promise<CanvasDTO> {
  const params = new URLSearchParams({ raw: "true" });
  const response = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}?${params.toString()}`,
    opts?.locale ? { headers: { "Accept-Language": opts.locale } } : undefined,
  );
  if (!response.ok) throw new HttpStatusError("canvas", response);
  return response.json() as Promise<CanvasDTO>;
}

/** message is for logs only; status code and request ID go in "复制诊断"; the UI localizes descriptions by status code. */
export class SessionDetailError extends Error {
  readonly status: number;
  readonly requestId?: string;

  constructor(status: number, requestId?: string) {
    super(`session detail: ${status}`);
    this.name = "SessionDetailError";
    this.status = status;
    this.requestId = requestId;
  }
}

/** W12-3 (#74): mark the session seen to clear the sidebar unread badge. Fire-and-forget. */
export async function markSessionSeen(sessionId: string): Promise<void> {
  try {
    await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/seen`, {
      method: "POST",
    });
  } catch {
    // Silent — an unread badge failing to clear is poor UX; do not block interaction.
  }
}

/** W12-3b (#87): pin / unpin session — callers handle thrown errors (UI shows a toast). */
export async function pinSession(sessionId: string): Promise<void> {
  const r = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/pin`,
    { method: "POST" },
  );
  if (!r.ok) throw new HttpStatusError("pin", r);
}

export async function unpinSession(sessionId: string): Promise<void> {
  const r = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/pin`,
    { method: "DELETE" },
  );
  if (!r.ok) throw new HttpStatusError("unpin", r);
}

export class SessionArchiveError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string) {
    super(`session archive: ${status} ${code}`);
    this.name = "SessionArchiveError";
    this.status = status;
    this.code = code;
  }
}

export async function archiveSession(sessionId: string): Promise<void> {
  const response = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/archive`,
    { method: "POST", headers: spaceHeader() },
  );
  if (!response.ok) throw await readSessionArchiveError(response);
}

export async function restoreSession(sessionId: string): Promise<void> {
  const response = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/archive`,
    { method: "DELETE", headers: spaceHeader() },
  );
  if (!response.ok) throw await readSessionArchiveError(response);
}

/**
 * Soft-delete a session (feedback-triage-2026-08-24: customer request「对话要可以删掉」).
 *
 * Soft deletion: remove the session from all list surfaces and reject new turns at backend turn admission, while keeping turn/trace/billing records
 * auditable. Undo with `undeleteSession`. Backend returns 409 when a turn is active:
 * `session_delete_active_turn` — reuse the SessionArchiveError error shape.
 */
export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE", headers: spaceHeader() },
  );
  if (!response.ok) throw await readSessionArchiveError(response);
}

/**
 * Rename one owned session. The gateway marks the title user-set, so automatic title
 * generation never replaces it. Errors reuse the SessionArchiveError shape; codes:
 * `session_title_empty` / `session_title_too_long` / `session_title_invalid` (422),
 * `session_not_found` (404, existence concealed).
 */
export async function renameSession(
  sessionId: string,
  title: string,
): Promise<{ id: string; title: string | null }> {
  const response = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "PATCH",
      headers: { ...spaceHeader(), "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    },
  );
  if (!response.ok) throw await readSessionArchiveError(response);
  return response.json() as Promise<{ id: string; title: string | null }>;
}

export async function undeleteSession(sessionId: string): Promise<void> {
  const response = await fetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/undelete`,
    { method: "POST", headers: spaceHeader() },
  );
  if (!response.ok) throw await readSessionArchiveError(response);
}

async function readSessionArchiveError(
  response: Response,
): Promise<SessionArchiveError> {
  let code = "session_archive_failed";
  try {
    const body = (await response.json()) as {
      detail?: { error?: { code?: unknown } };
    };
    if (typeof body.detail?.error?.code === "string") {
      code = body.detail.error.code;
    }
  } catch {
    // A stable fallback keeps the UI actionable when a proxy strips JSON.
  }
  return new SessionArchiveError(response.status, code);
}

export interface SessionDetailDTO {
  context_generation?: string | null;
  chat_execution: import("./auth-api").ChatExecutionBootstrap;
  session: {
    id: string;
    workspace_id: string;
    title: string | null;
    started_at: string;
    last_active_at: string;
    status: string;
    cost_usd: number;
    tokens_total: number;
    intents: string[] | null;
  };
  turns: Array<{
    operation_kind?: "context_maintenance" | null;
    id: string;
    seq: number;
    user_text: string;
    intent: string | null;
    status: string;
    execution_generation?: number;
    terminal_protocol_version?: number | null;
    /** 0708 ⑩: failure cause ("interrupted" = gateway-restart-killed) for a targeted retry. */
    error_kind?: string | null;
    started_at: string;
    ended_at: string | null;
    final_answer: string | null;
    /** B-4: agent owning this turn (Lead/Analyst/Modeler/...). Default None → frontend omits the badge. */
    agent_type?: string | null;
    attachments?: Array<{
      file_id: string;
      filename: string;
      mime: string;
      size: number;
      reference_kind?: "result" | null;
      selected_context?: import("./chart/selected-context").ChartSelectedContext | null;
      bundle_artifact_ids?: string[] | null;
    }>;
    /** chat-hydrate-overhaul: persisted trace steps. Gracefully use [] when absent on legacy turns. */
    trace_steps?: Array<{
      id: string;
      kind: string;
      title?: string | null;
      tool_name?: string | null;
      duration_ms?: number | null;
      summary?: string | null;
      /** Round 5: associate nested children with their spawn parent (FE renders them under the spawn node). */
      subagent_id?: string | null;
    }>;
    /** chat-hydrate-overhaul: persisted turn summary. null when absent on legacy turns. */
    summary?: {
      total_ms: number;
      cost_text?: string | null;
      tools_count: number;
      llm_count: number;
      tokens_input?: number | null;
      tokens_output?: number | null;
      usage_partial?: boolean;
    } | null;
  }>;
}

export async function fetchSessionDetail(
  sessionId: string,
): Promise<SessionDetailDTO> {
  const r = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
    headers: TERMINAL_CHAT_CAPABILITY_HEADERS,
  });
  if (!r.ok) throw new SessionDetailError(r.status, responseRequestId(r));
  return r.json() as Promise<SessionDetailDTO>;
}

export async function fetchSessionChatExecutionPreflight(
  sessionId: string,
): Promise<SessionDetailDTO> {
  const r = await boundedComposerControlFetch(
    fetch,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    {
      headers: TERMINAL_CHAT_CAPABILITY_HEADERS,
      cache: "no-store",
    },
  );
  if (!r.ok) throw new SessionDetailError(r.status, responseRequestId(r));
  return r.json() as Promise<SessionDetailDTO>;
}

/** P4.1: clear canvas sections (keep the shell). */
export async function clearCanvas(canvasId: string): Promise<void> {
  const r = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/clear`,
    { method: "POST" },
  );
  if (!r.ok) throw new HttpStatusError("clear canvas", r);
}

/** P4.1: delete the entire canvas + sections + revisions.
 * feedback-0713 R1: opts.deleteSession=true also soft-deletes the source session (off by default). */
export async function deleteCanvas(
  canvasId: string,
  opts?: { deleteSession?: boolean },
): Promise<void> {
  const qs = opts?.deleteSession ? "?delete_session=1" : "";
  const r = await fetch(`/api/v1/canvas/${encodeURIComponent(canvasId)}${qs}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new HttpStatusError("delete canvas", r);
}

/** P4.2: list user's canvases across sessions.
 * chat-page-polish B1: backend also returns first_section_title — FE falls back to it when title is empty.
 */
/** R2 report lifecycle states. active=active (fresh) · verified=verified · stale=stale · archived=archived. */
export type ReportStatus = "active" | "verified" | "stale" | "archived";

export interface ReportListItem {
  id: string;
  session_id: string;
  workspace_id: string;
  workspace_name?: string | null;
  title: string | null;
  status: string; // R2 persisted state
  /** R2 display state: archived takes priority; any quarantined section → stale (folded in). Badges use this. */
  effective_status?: string;
  quarantined_count?: number;
  created_at: string;
  updated_at: string;
  sections_count: number;
  /** B1: earliest section's heading/title; backend assembles it (h1→h2→h3→title→heading→text). */
  first_section_title?: string | null;
  /** content-permissions S7: audience of reports I created (private/custom/team_wide) + specified recipient count. */
  audience?: "private" | "custom" | "team_wide" | null;
  share_count?: number;
  /** content-permissions S5: owner of a report shared with me (scope=shared). */
  shared_by?: string | null;
}

export interface ReportListQuery {
  workspace_id?: string;
  q?: string;
  sort?: string;
  limit?: number;
  scope?: "mine" | "shared";
  include_empty?: boolean;
}

/** One page of the report library. On the first page `total` counts every
 * report matching the same filters and visibility (not the page size); cursor
 * pages return null. `next_cursor` is an opaque cursor bound to the same sort
 * and filters, or null on the last page. */
export interface ReportListPage {
  items: ReportListItem[];
  /** Null on cursor pages: only the first page counts. */
  total: number | null;
  /** First page with include_empty=false: reports hidden for having no sections; otherwise null/absent. */
  empty_total?: number | null;
  next_cursor: string | null;
  has_more: boolean;
}

export async function fetchReportsPage(
  opts: ReportListQuery & { cursor?: string | null } = {},
): Promise<ReportListPage> {
  const params = new URLSearchParams();
  if (opts.workspace_id) params.set("workspace_id", opts.workspace_id);
  if (opts.q) params.set("q", opts.q);
  if (opts.sort) params.set("sort", opts.sort);
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.scope) params.set("scope", opts.scope);
  if (opts.include_empty !== undefined)
    params.set("include_empty", String(opts.include_empty));
  if (opts.cursor) params.set("cursor", opts.cursor);
  const qs = params.toString() ? `?${params}` : "";
  const r = await fetch(`/api/v1/canvas${qs}`); // P4.2: list uses the canvas router.
  if (!r.ok) throw new HttpStatusError("reports", r);
  const j = (await r.json()) as {
    items?: ReportListItem[];
    total?: number | null;
    next_cursor?: string | null;
  };
  const items = Array.isArray(j.items) ? j.items : [];
  const nextCursor =
    typeof j.next_cursor === "string" && j.next_cursor ? j.next_cursor : null;
  return {
    items,
    // Cursor pages send total=null (first page only counts); older gateways
    // echoed the page length on every page.
    total:
      typeof j.total === "number"
        ? j.total
        : opts.cursor
          ? null
          : items.length,
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  };
}

export async function fetchReports(
  opts: ReportListQuery = {},
): Promise<ReportListItem[]> {
  return (await fetchReportsPage(opts)).items;
}

// Feishu 07.06 #12: edit saves (reorder/patch/delete/split/merge/title) previously used raw fetch,
// bypassing apiFetch (=fetchWithRefresh)'s 401→automatic renewal→retry; after the 1h access token expired,
// typing an edit immediately produced "报告保存失败: reorder: 401". All now use apiFetch; 401 after renewal
// means an actual logout; throw readable text rather than internal "op: 401".
// Status code and request ID live on HttpStatusError for "复制诊断"; message is only for logs and checks such as `: 422$`, never displayed to users.
function _saveErr(op: string, response: Response): HttpStatusError {
  return new HttpStatusError(op, response);
}

export async function patchCanvasSection(
  canvasId: string,
  sectionId: string,
  patch: Record<string, unknown>,
): Promise<{ section: ReportSection; rev: number }> {
  const r = await apiFetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/sections/${encodeURIComponent(sectionId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch }),
    },
  );
  if (!r.ok) throw _saveErr("patch section", r);
  return r.json();
}

export async function flattenCanvasDesign(
  canvasId: string,
  sectionId: string,
  expectedRev?: number,
): Promise<{
  artifact_id: string;
  url: string;
  section: ReportSection;
  rev: number;
  warnings?: string[];
}> {
  const r = await apiFetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/sections/${encodeURIComponent(sectionId)}/flatten`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_rev: expectedRev }),
    },
  );
  if (!r.ok) throw await readApiError(r);
  return r.json();
}

export async function deleteCanvasSection(
  canvasId: string,
  sectionId: string,
  opts?: { cascade?: boolean },
): Promise<{ deletedSectionIds: string[] }> {
  // A2 (report-edit-audit): cascade=1 makes the server also delete inline charts/images exclusively owned by the host
  // (server parses and applies three filters to the subset; FE omits child IDs). Return actual deletions so hints use the real count.
  const query = opts?.cascade ? "?cascade=1" : "";
  const r = await apiFetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/sections/${encodeURIComponent(sectionId)}${query}`,
    { method: "DELETE" },
  );
  if (!r.ok) throw _saveErr("delete section", r);
  const body = (await r.json().catch(() => null)) as {
    deletedSectionIds?: string[];
  } | null;
  return { deletedSectionIds: body?.deletedSectionIds ?? [sectionId] };
}

export async function reorderCanvas(
  canvasId: string,
  order: string[],
): Promise<void> {
  const r = await apiFetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/reorder`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order }),
    },
  );
  if (!r.ok) throw _saveErr("reorder", r);
}

/**
 * W1-3 split section at cursor.
 * Backend pending: expected endpoint POST /api/v1/canvas/{cid}/sections/{sid}/split
 *   body: { offset: number }   // Split original markdown into 2 sections at character offset
 *   resp: { newSectionId: string }
 * Frontend wiring is complete; no further frontend changes are needed when the backend endpoint launches.
 */
export async function splitCanvasSection(
  canvasId: string,
  sectionId: string,
  offset: number,
): Promise<{ newSectionId: string }> {
  const r = await apiFetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/sections/${encodeURIComponent(sectionId)}/split`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ offset }),
    },
  );
  if (!r.ok) throw _saveErr("split section", r);
  return r.json() as Promise<{ newSectionId: string }>;
}

/**
 * W12-6 merge: use the new atomic backend endpoint /sections/{sid}/merge.
 * One transaction: UPDATE survivor.payload (concatenate markdown + deduplicated source_refs union)
 *                  + DELETE absorbed + 2 revisions. No longer use separate patch+delete calls.
 *
 * Only absorbedId (= current sid) is required; backend computes survivor / merged content (takes prev).
 * Legacy callers' mergedMarkdown / mergedSourceRefs are now unused (backend is the source of truth).
 *
 * Callers must still validate in the UI: both sections are adjacent + have the same text-based kind.
 */
export async function mergeCanvasSections(
  canvasId: string,
  _survivorId: string, // unused (kept for backward call signature)
  absorbedId: string,
  _mergedMarkdown?: string, // unused
  _mergedSourceRefs?: string[], // unused
): Promise<void> {
  const r = await apiFetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/sections/${encodeURIComponent(absorbedId)}/merge`,
    { method: "POST" },
  );
  if (!r.ok) {
    throw _saveErr("merge", r);
  }
}

/** R2: change report lifecycle state (archive/restore/mark-verified/mark-stale). Owner-only (backend guard). */
export async function setCanvasStatus(
  canvasId: string,
  status: ReportStatus,
): Promise<void> {
  const r = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/status`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    },
  );
  if (!r.ok) throw new HttpStatusError("status", r);
}

export interface CanvasStatusMeta {
  status: string;
  effectiveStatus: string;
  quarantinedCount: number;
}

/** R2: lightweight state read for the report-header chip (omit sections). */
export async function fetchCanvasStatus(
  canvasId: string,
): Promise<CanvasStatusMeta> {
  const r = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/status`,
  );
  if (!r.ok) throw new HttpStatusError("status", r);
  return (await r.json()) as CanvasStatusMeta;
}

// ─────────────────────────────────────────────────────────
// Approvals API (M2.3a)
// ─────────────────────────────────────────────────────────
export interface ApprovalDTO {
  id: string;
  turnId: string;
  sessionId: string | null;
  workspaceId: string;
  memberUid: string;
  userId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  reason: string | null;
  riskLevel: string;
  status:
    | "pending"
    | "approved"
    | "rejected"
    | "executed"
    | "execution_failed"
    | "expired";
  decidedBy: string | null;
  decidedAt: string | null;
  executionOk: boolean | null;
  executionResult: unknown;
  executionError: string | null;
  executedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export async function fetchApprovals(
  scope: "pending" | "all" = "pending",
): Promise<ApprovalDTO[]> {
  const r = await apiFetch(`/api/v1/approvals?scope=${scope}`);
  if (!r.ok) throw new HttpStatusError("approvals", r);
  const body = (await r.json()) as { items: ApprovalDTO[] };
  return body.items;
}

export async function approveApproval(
  id: string,
  reason?: string,
): Promise<ApprovalDTO> {
  const r = await apiFetch(
    `/api/v1/approvals/${encodeURIComponent(id)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
  if (!r.ok) throw new HttpStatusError("approve", r);
  return r.json();
}

export async function rejectApproval(
  id: string,
  reason?: string,
): Promise<ApprovalDTO> {
  const r = await apiFetch(
    `/api/v1/approvals/${encodeURIComponent(id)}/reject`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
  if (!r.ok) throw new HttpStatusError("reject", r);
  return r.json();
}

// ─────────────────────────────────────────────────────────
// Notification Center
// ─────────────────────────────────────────────────────────
export type NotificationTab = "all" | "action" | "updates";

export interface NotificationActionDTO {
  id: string;
  label: string;
  kind: string;
  url: string | null;
}

export interface NotificationItemDTO {
  id: string;
  teamId: string;
  category: string;
  type: string;
  priority: string;
  title: string;
  body: string | null;
  subjectType: string | null;
  subjectId: string | null;
  actorUserId: string | null;
  actionUrl: string | null;
  actions: NotificationActionDTO[];
  metadata: Record<string, unknown>;
  state: string;
  recipientRole: string | null;
  deliveredAt: string;
  readAt: string | null;
  archivedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface NotificationCountDTO {
  actionRequired: number;
  updatesUnread: number;
  totalBadge: number;
}

export async function fetchNotificationCount(): Promise<NotificationCountDTO> {
  const r = await fetch("/api/v1/notifications/unread-count");
  if (!r.ok) throw new HttpStatusError("notification count", r);
  return r.json();
}

export async function fetchNotifications(
  tab: NotificationTab = "all",
  cursor?: string | null,
): Promise<{ items: NotificationItemDTO[]; nextCursor: string | null }> {
  const params = new URLSearchParams({ tab, limit: "30" });
  if (cursor) params.set("cursor", cursor);
  const r = await fetch(`/api/v1/notifications?${params}`);
  if (!r.ok) throw new HttpStatusError("notifications", r);
  return r.json();
}

export async function markNotificationRead(id: string): Promise<void> {
  const r = await fetch(
    `/api/v1/notifications/${encodeURIComponent(id)}/read`,
    {
      method: "POST",
    },
  );
  if (!r.ok) throw new HttpStatusError("notification read", r);
}

export async function archiveNotification(id: string): Promise<void> {
  const r = await fetch(
    `/api/v1/notifications/${encodeURIComponent(id)}/archive`,
    {
      method: "POST",
    },
  );
  if (!r.ok) throw new HttpStatusError("notification archive", r);
}

export async function markAllNotificationsRead(
  tab: NotificationTab = "all",
): Promise<void> {
  const r = await fetch("/api/v1/notifications/read-all", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab }),
  });
  if (!r.ok) throw new HttpStatusError("notification read-all", r);
}

// ─────────────────────────────────────────────────────────
// Turn replay (M2.3c)
// ─────────────────────────────────────────────────────────
export interface TurnEventDTO {
  seq: number;
  event_type: string;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface EffectDisclosureOverlayDTO {
  protocol: 2;
  receipt_id: string | null;
  attempt_count: number;
  unknown_count: number;
  counts: Record<string, Record<string, number>>;
  proof_digest: string;
  oldest_unknown_attempt_started_at: string | null;
  oldest_unknown_age_upper_bound_seconds: number | null;
  warning_message_keys: string[];
  finalized: boolean;
}

export interface TurnEventsEnvelopeDTO {
  turn_id: string;
  events: TurnEventDTO[];
  terminal_protocol_version?: number | null;
  current_generation?: number | null;
  persistent_event_highwater?: number | null;
  current_effect_disclosure?: EffectDisclosureOverlayDTO | null;
}

export async function fetchTurnIds(limit = 100): Promise<string[]> {
  const r = await fetch(`/api/v1/turns?limit=${limit}`);
  if (!r.ok) throw new HttpStatusError("turns", r);
  return (await r.json()).turn_ids as string[];
}

export async function fetchTurnEvents(turnId: string): Promise<TurnEventDTO[]> {
  return (await fetchTurnEventsEnvelope(turnId)).events;
}

export async function fetchTurnEventsEnvelope(
  turnId: string,
): Promise<TurnEventsEnvelopeDTO> {
  const r = await fetch(`/api/v1/turns/${encodeURIComponent(turnId)}/events`);
  if (!r.ok) throw new HttpStatusError("events", r);
  return (await r.json()) as TurnEventsEnvelopeDTO;
}

export async function fetchEffectDisclosure(turnId: string): Promise<{
  currentGeneration: number;
  overlay: EffectDisclosureOverlayDTO | null;
}> {
  const r = await fetch(
    `/api/v1/turns/${encodeURIComponent(turnId)}/effect-disclosure`,
  );
  if (!r.ok) throw new HttpStatusError("effect disclosure", r);
  const payload = (await r.json()) as {
    current_generation?: number;
    current_effect_disclosure?: EffectDisclosureOverlayDTO | null;
  };
  if (
    !Number.isSafeInteger(payload.current_generation) ||
    payload.current_generation! < 1
  ) {
    throw new Error("effect disclosure: malformed generation");
  }
  return {
    currentGeneration: payload.current_generation!,
    overlay: payload.current_effect_disclosure ?? null,
  };
}

// V4 PRD-D-4-4: admin replay — fetch the memory pack snapshot that was injected
// at this turn (team-scoped; cross-team / NULL-team turns return 404).
export interface MemoryPackSnapshotDTO {
  turn_id: string;
  user_id: string;
  team_id: string | null;
  workspace_id: string | null;
  char_count: number;
  sections_breakdown: Record<string, number>;
  content: string;
  pack_version: string;
  created_at: string;
}

export async function fetchAdminTurnSnapshot(
  turnId: string,
): Promise<MemoryPackSnapshotDTO | null> {
  const r = await fetch(
    `/api/v1/admin/turns/${encodeURIComponent(turnId)}/memory-pack-snapshot`,
    { credentials: "include" },
  );
  if (r.status === 204) return null;
  // A 404 means the turn is missing or outside this administrator's scope.
  if (!r.ok) throw new HttpStatusError("admin replay snapshot", r);
  return r.json() as Promise<MemoryPackSnapshotDTO>;
}

// ─────────────────────────────────────────────────────────
// Canvas export job polling (M2.2c)
// ─────────────────────────────────────────────────────────
export interface ExportJobDTO {
  jobId: string;
  canvasId: string;
  contractVersion: number;
  fmt: string;
  status: "queued" | "running" | "done" | "failed" | "canceled";
  url: string | null;
  artifactId: string | null;
  filename: string | null;
  size: number | null;
  error: string | null;
  createdAt: string;
  updatedAt: string;
}

const EXPORT_JOB_STATUSES = new Set<ExportJobDTO["status"]>([
  "queued",
  "running",
  "done",
  "failed",
  "canceled",
]);

type RawExportJobDTO = Partial<Omit<ExportJobDTO, "status">> &
  Record<string, unknown> & {
    status?: unknown;
  };

function normalizeExportStatus(rawStatus: unknown): {
  status: ExportJobDTO["status"];
  error: string | null;
} {
  if (rawStatus == null) return { status: "queued", error: null };
  if (
    typeof rawStatus === "string" &&
    EXPORT_JOB_STATUSES.has(rawStatus as ExportJobDTO["status"])
  ) {
    return { status: rawStatus as ExportJobDTO["status"], error: null };
  }
  // Treat unrecognized states as failures; do not pass internal explanations such as Unknown export status: … to the UI,
  // which displays a generic localized sentence for failures without an explanation.
  return { status: "failed", error: null };
}

export function normalizeExportJobDTO(raw: RawExportJobDTO): ExportJobDTO {
  const normalizedStatus = normalizeExportStatus(raw.status);
  const rawContractVersion = raw.contractVersion ?? raw.contract_version ?? 1;
  const contractVersion =
    typeof rawContractVersion === "number" &&
    Number.isInteger(rawContractVersion) &&
    (rawContractVersion === 1 ||
      rawContractVersion === 2 ||
      rawContractVersion === 3)
      ? rawContractVersion
      : 1;
  return {
    jobId: String(raw.jobId ?? raw.job_id ?? ""),
    canvasId: String(raw.canvasId ?? raw.canvas_id ?? ""),
    contractVersion,
    fmt: String(raw.fmt ?? raw.format ?? ""),
    status: normalizedStatus.status,
    url:
      (raw.url as string | null | undefined) ??
      (raw.download_url as string | null | undefined) ??
      null,
    artifactId:
      (raw.artifactId as string | null | undefined) ??
      (raw.artifact_id as string | null | undefined) ??
      null,
    filename: (raw.filename as string | null | undefined) ?? null,
    size: (raw.size as number | null | undefined) ?? null,
    error: (raw.error as string | null | undefined) ?? normalizedStatus.error,
    createdAt: String(raw.createdAt ?? raw.created_at ?? ""),
    updatedAt: String(raw.updatedAt ?? raw.updated_at ?? ""),
  };
}

export function exportJobDownloadUrl(job: ExportJobDTO): string | null {
  if (job.url) return appPath(job.url);
  if (job.artifactId)
    return appPath(`/api/v1/files/${encodeURIComponent(job.artifactId)}`);
  return null;
}

export async function fetchExportJob(
  canvasId: string,
  jobId: string,
): Promise<ExportJobDTO> {
  const r = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/export/${encodeURIComponent(jobId)}`,
  );
  if (!r.ok) throw new HttpStatusError("export job", r);
  return normalizeExportJobDTO(await r.json());
}

export async function startExportJob(
  canvasId: string,
  fmt: CanvasExportFmt,
  opts?: {
    /** V2.9 A6+ #60: export modes. auto = default (automatically clone when a .pptx template exists);
     *  clone = force full preservation of the user's master template;
     *  default = force canvas-pptx's own 7-slide design system. */
    mode?: "auto" | "clone" | "default";
    template_artifact_id?: string;
  },
): Promise<ExportJobDTO> {
  const r = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/export`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fmt, ...(opts ?? {}) }),
    },
  );
  if (!r.ok) throw new HttpStatusError("start export", r);
  return normalizeExportJobDTO(await r.json());
}

export interface StartExportJobV2Input {
  format: CanvasExportFmt;
  profile: "full_report" | "executive_summary" | "data_workbook";
  locale: Locale;
  timezone: string;
  expected_projection_token: string;
  expected_surface_token: string;
  client_surface_receipt_sha256: string;
  client_visual_receipts: Array<
    | {
        section_id: string;
        section_rev: number;
        visual_input_sha256: string;
        render_disposition: "rendered";
      }
    | {
        section_id: string;
        section_rev: number;
        surface_token: string;
        screen_visual_input_sha256: string;
        render_disposition: "rendered";
      }
  >;
  theme:
    | import("./chart/chart-style-export").ChartStyleExportThemeV1
    | import("./chart/chart-style-export").ChartStyleExportFreshnessV2;
  include_appendix?: boolean;
  /** Series the exporting user hid in each report chart's legend, by section id (omitted when none). The export
   *  draws those charts the way this user sees them; other users' exports are unaffected. */
  chart_hidden_series?: Record<string, string[]>;
}

export async function startExportJobV2(
  canvasId: string,
  input: StartExportJobV2Input,
  idempotencyKey: string,
): Promise<ExportJobDTO> {
  const response = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/exports`,
    {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        format: input.format,
        profile: input.profile,
        locale: input.locale,
        timezone: input.timezone,
        theme: input.theme,
        consistency: "require_projection",
        expected_projection_token: input.expected_projection_token,
        expected_surface_token: input.expected_surface_token,
        client_surface_receipt_sha256: input.client_surface_receipt_sha256,
        client_visual_receipts: input.client_visual_receipts,
        options: {
          include_appendix: input.include_appendix ?? true,
          ...(input.chart_hidden_series && Object.keys(input.chart_hidden_series).length
            ? { chart_hidden_series: input.chart_hidden_series }
            : {}),
        },
      }),
    },
  );
  if (!response.ok) throw await readApiError(response);
  return normalizeExportJobDTO(await response.json());
}

export async function fetchExportJobV2(
  canvasId: string,
  jobId: string,
): Promise<ExportJobDTO> {
  const response = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/exports/${encodeURIComponent(jobId)}`,
    { credentials: "include" },
  );
  if (!response.ok) throw await readApiError(response);
  return normalizeExportJobDTO(await response.json());
}

// Admin · Routing override (/api/v1/admin/routing) was removed with WS0 — team-level model selection
// now uses agent bindings in team LLM config ("模型" settings page). fetchRouting /
// putRoutingOverride / clearRoutingOverride and RoutingStateDTO were deleted.

/** WP1 export completion loop (design D5): this report's historical export jobs — recover them from the drawer after timeout/restart. */
export async function listExportJobs(
  canvasId: string,
  limit = 20,
): Promise<ExportJobDTO[]> {
  const boundedLimit = Math.max(1, Math.min(limit, 100));
  const res = await fetch(
    `/api/v1/canvas/${encodeURIComponent(canvasId)}/exports?limit=${boundedLimit}`,
    { credentials: "include" },
  );
  if (!res.ok) throw new HttpStatusError("listExportJobs failed", res);
  const data = (await res.json()) as { items?: RawExportJobDTO[] };
  return (data.items ?? []).map(normalizeExportJobDTO);
}

// ─────────────────────────────────────────────────────────────────────────────
// Scheduled Jobs (Routines) — named NL queries + cron schedules + notification delivery;
// the system automatically runs them in the cloud on schedule to produce an analysis. Backend /api/v1/scheduled/jobs (+/runs,/run-now).
// ─────────────────────────────────────────────────────────────────────────────

export type ScheduledJobStatus = "active" | "paused" | "expired";
export type ScheduledSessionStrategy = "fresh" | "anchor";
export type ScheduledRunStatus =
  "running" | "success" | "failed" | "skipped" | "expired" | "awaiting_input";

/** Backend stores notify_channels as a dict (NOT a bare array): {email:[...], deliver_on_success:bool}.
 *  Phase B delivery reads `notify_channels.get("email")` / `deliver_on_success` — sending an array breaks it. */
export interface ScheduledNotifyChannels {
  email?: string[];
  deliver_on_success?: boolean;
}

export interface ScheduledJobDTO {
  id: string;
  user_id: string;
  workspace_id: string;
  name: string;
  cron: string;
  tz: string;
  query: string;
  session_strategy: ScheduledSessionStrategy;
  anchor_session_id?: string | null;
  notify_channels: ScheduledNotifyChannels;
  status: ScheduledJobStatus;
  failure_count: number;
  next_run_at?: string | null;
  last_run_at?: string | null;
  last_turn_id?: string | null;
  created_at: string;
  updated_at: string;
  /** Why the system paused it (stable code). User-initiated pauses omit this field, letting list rows distinguish them. */
  pause_reason?: string | null;
  /** Bound solidified workflow, JSON string: {form, id_or_name, bound_sha?}. Unbound = null/"". */
  recipe_ref?: string | null;
  /** Human-readable bound workflow name (server enriches from the bindable set). Unresolvable (deleted/no access) = null. */
  bound_display_name?: string | null;
  /** Bound workflow author's display name, **only for another author** (US-6 delegated-access visibility). Own workflow = null. */
  bound_author?: string | null;
  /** M3-7 normalized exact Agent pin; definitions/fingerprints stay server-side. */
  agent_binding_state?: "ready" | "needs_reconfirm" | "paused" | null;
  agent_id?: string | null;
  agent_revision_id?: string | null;
  agent_release_id?: string | null;
  agent_binding_confirmed_at?: string | null;
}

export interface ScheduledJobRunDTO {
  id: string;
  job_id: string;
  turn_id?: string | null;
  session_id?: string | null; // Resolved from turn → "查看结果" deep link (/?session=).
  started_at: string;
  finished_at?: string | null;
  status: ScheduledRunStatus;
  error?: string | null;
  error_diagnostics?: unknown;
}

export interface CreateScheduledJobPayload {
  name: string;
  cron: string;
  query: string;
  workspace_id: string;
  tz?: string;
  session_strategy?: ScheduledSessionStrategy;
  anchor_session_id?: string | null;
  notify_channels?: ScheduledNotifyChannels;
  /** Bind a solidified workflow: {form, id_or_name}. Omitted = no binding. */
  recipe_ref?: Record<string, unknown> | null;
}

export interface PatchScheduledJobPayload {
  name?: string;
  cron?: string;
  status?: ScheduledJobStatus;
  query?: string;
  tz?: string;
  session_strategy?: ScheduledSessionStrategy;
  anchor_session_id?: string | null;
  notify_channels?: ScheduledNotifyChannels;
  /** Omitted = leave binding unchanged; {form, id_or_name} = rebind/reconfirm; {unbind:true} = unbind. */
  recipe_ref?: Record<string, unknown> | null;
}

/** Bindable solidified workflows (aggregate three forms; server prefilters binding eligibility). */
export type BindableWorkflowForm =
  "personal_skill" | "team_skill" | "team_subagent";

export interface BindableWorkflowDTO {
  form: BindableWorkflowForm;
  /** personal_skill uses name as its key, team_* uses id — backend resolves separate branches by form; do not interchange them. */
  id_or_name: string;
  display_name: string;
  description: string;
  author: string | null;
  /** Human-readable author name; null if backend cannot resolve it. UI falls back to author (prefer unattractive text to losing attribution). */
  author_name?: string | null;
  is_own: boolean;
}

/** GET /scheduled/jobs — all scheduled jobs for the current user (backend filters by user). */
export async function fetchScheduledJobs(): Promise<{
  items: ScheduledJobDTO[];
}> {
  return apiJson<{ items: ScheduledJobDTO[] }>("/api/v1/scheduled/jobs", {
    headers: workspaceHeaders(),
  });
}

/** POST /scheduled/jobs — create a scheduled job. */
export async function createScheduledJob(
  payload: CreateScheduledJobPayload,
): Promise<ScheduledJobDTO> {
  return apiJson<ScheduledJobDTO>("/api/v1/scheduled/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...workspaceHeaders() },
    body: JSON.stringify(payload),
  });
}

/** PATCH /scheduled/jobs/{id} — rename / change cron / enable·pause. */
export async function patchScheduledJob(
  id: string,
  payload: PatchScheduledJobPayload,
): Promise<ScheduledJobDTO> {
  return apiJson<ScheduledJobDTO>(
    `/api/v1/scheduled/jobs/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...workspaceHeaders() },
      body: JSON.stringify(payload),
    },
  );
}

/** DELETE /scheduled/jobs/{id} — delete the job and its run history. */
export async function deleteScheduledJob(id: string): Promise<void> {
  return apiVoid(`/api/v1/scheduled/jobs/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: workspaceHeaders(),
  });
}

/** GET /scheduled/jobs/{id}/runs — recent run history. */
export async function fetchScheduledJobRuns(
  id: string,
  limit = 10,
): Promise<{ items: ScheduledJobRunDTO[] }> {
  return apiJson<{ items: ScheduledJobRunDTO[] }>(
    `/api/v1/scheduled/jobs/${encodeURIComponent(id)}/runs?limit=${encodeURIComponent(String(limit))}`,
    { headers: workspaceHeaders() },
  );
}

/** GET /scheduled/bindable-workflows — solidified workflows bindable by the current user (personal + team-visible set).
 *
 * Server aggregates three stores and prefilters binding eligibility (enabled / visibility / whether it is a solidified artifact); frontend only renders.
 * Flag off returns an empty list; a missing endpoint (legacy backend) returns 404 — both omit the entire picker.
 */
export async function fetchBindableWorkflows(): Promise<{
  items: BindableWorkflowDTO[];
}> {
  return apiJson<{ items: BindableWorkflowDTO[] }>(
    "/api/v1/scheduled/bindable-workflows",
    { headers: workspaceHeaders() },
  );
}

/** POST /scheduled/jobs/{id}/run-now — trigger one immediate run (asynchronous). 409 = a run is already in progress. */
export async function runScheduledJobNow(
  id: string,
): Promise<{ run_id: string; status: ScheduledRunStatus }> {
  return apiJson<{ run_id: string; status: ScheduledRunStatus }>(
    `/api/v1/scheduled/jobs/${encodeURIComponent(id)}/run-now`,
    { method: "POST", headers: workspaceHeaders() },
  );
}

// ─── Team public entries ─────────────────────────────────────
export type PublicEntryStatus = "pending_verification" | "active" | "disabled";

export interface PublicEntryDTO {
  id: string;
  team_id: string;
  base_url: string;
  base_path: string;
  is_default: boolean;
  is_current: boolean;
  match_source: string | null;
  status: PublicEntryStatus;
  verification_method: string;
  verification_expires_at: string | null;
  verified_at: string | null;
  created_at: string;
  updated_at: string;
}

export type IntegrationProviderId = "feishu" | "slack" | "teams";

export interface IntegrationOverviewItemDTO {
  provider: IntegrationProviderId;
  available: boolean;
  configured: boolean;
  enabled: boolean;
  healthy: boolean;
}

export interface IntegrationOverviewDTO {
  items: IntegrationOverviewItemDTO[];
}

export interface PublicEntryChallengeDTO {
  verification_url: string;
  expires_at: string | null;
  entry: PublicEntryDTO;
}

export async function fetchPublicEntries(): Promise<PublicEntryDTO[]> {
  return apiJson<PublicEntryDTO[]>("/api/v1/team-settings/public-entries");
}

export async function fetchIntegrationOverview(): Promise<IntegrationOverviewDTO> {
  return apiJson<IntegrationOverviewDTO>(
    "/api/v1/team-settings/integrations/overview",
  );
}

export async function createPublicEntry(
  baseUrl: string,
): Promise<PublicEntryChallengeDTO> {
  return apiJson<PublicEntryChallengeDTO>(
    "/api/v1/team-settings/public-entries",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: baseUrl }),
    },
  );
}

export async function reissuePublicEntryChallenge(
  entryId: string,
): Promise<PublicEntryChallengeDTO> {
  return apiJson<PublicEntryChallengeDTO>(
    `/api/v1/team-settings/public-entries/${encodeURIComponent(entryId)}/challenge`,
    { method: "POST" },
  );
}

export async function setDefaultPublicEntry(
  entryId: string,
): Promise<PublicEntryDTO> {
  return apiJson<PublicEntryDTO>(
    `/api/v1/team-settings/public-entries/${encodeURIComponent(entryId)}/default`,
    { method: "PUT" },
  );
}

export async function disablePublicEntry(
  entryId: string,
  options: { new_default_entry_id?: string; disable_last?: boolean },
): Promise<PublicEntryDTO> {
  return apiJson<PublicEntryDTO>(
    `/api/v1/team-settings/public-entries/${encodeURIComponent(entryId)}/disable`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    },
  );
}

export async function deletePublicEntry(entryId: string): Promise<void> {
  return apiVoid(
    `/api/v1/team-settings/public-entries/${encodeURIComponent(entryId)}`,
    { method: "DELETE" },
  );
}
