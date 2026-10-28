import { appFetch as fetch } from "@/lib/base-path";
import { fetchSessionChatExecutionPreflight } from "@/lib/api";
import { ApiError, readApiError } from "@/lib/api-client";
import { getChatSendErrorMessage } from "@/lib/chat-send-errors";
import type { ChatExecutionBootstrap } from "@/lib/auth-api";
import {
  composerChatActionUserMessageId,
  createComposerChatAction,
  isComposerChatActionResponse,
  isDefinitiveComposerChatActionRejection,
  lookupComposerChatAction,
  singleAttemptChatActionFetch,
} from "@/lib/composer-chat-action";
import type { ComposerChatAction } from "@/lib/composer-chat-action";
import { TERMINAL_CHAT_CAPABILITY_HEADERS } from "@/lib/terminal-chat-capability";
import { viewerTimeZoneHeaders } from "@/lib/time-zone";

/**
 * Planning Mode REST. Production approval uses the protocol-selected chat
 * writer; the remaining plan routes edit, inspect, and control Plan state.
 *
 * Production approval goes through the protocol-selected chat writer. The
 * dev-only route is not a fallback: a failed chat mutation must never trigger
 * a second Plan execution writer.
 */

import type { PlanStepView } from "@/components/chat/plan-card/types";

const pendingPlanApprovalActions = new Map<string, ComposerChatAction>();
const inFlightPlanApprovals = new Map<
  string,
  Promise<{ turn_id: string; stream_url: string; status?: string }>
>();

export interface PlanIdentity {
  workspaceId: string;
  userId: string;
  memberUid?: string;
}

/** PlanStepView → backend EditStep wire shape (PATCH /steps · amend confirm). */
function toEditStep(s: PlanStepView): Record<string, unknown> {
  return {
    id: s.id,
    agent: s.agent,
    objective: s.objective,
    deliverable: s.deliverable ?? "",
    depends_on: s.dependsOn,
    needs_corpus: s.needsCorpus,
    needs_web: s.needsWeb,
    included: s.included,
    origin: s.origin ?? "initial",
    // graph-mode batch-1 review B2/F-1: omitting criteria lets backend replace_steps full rewrite
    // clear user-approved acceptance to [] (backend also backfills by id; explicit return adds second safeguard).
    success_criteria: s.successCriteria ?? [],
    agent_choice: s.agentChoice ?? null,
  };
}

export interface PlanStateResponse {
  plan_id: string;
  plan: {
    id: string;
    status: string;
    title: string;
    deliverable_kind?: string;
    canvas_id?: string | null;
    retryable?: boolean;
    current_rev?: number;
  };
  steps: Array<Record<string, unknown>>;
}

/** Production approve: preflight the Plan Session, then use exactly the writer
 *  selected by the Gateway. Terminal POST ambiguity is resolved by action
 *  lookup and never by a second mutation or the dev route. */
export function approvePlanViaSend(
  planId: string,
  id: { workspaceId: string; sessionId?: string | null; expectedRev: number | undefined },
): Promise<{ turn_id: string; stream_url: string; status?: string }> {
  const requestKey = `${id.workspaceId}\u0000${id.sessionId ?? ""}\u0000${planId}`;
  const existing = inFlightPlanApprovals.get(requestKey);
  if (existing) return existing;
  let request!: Promise<{ turn_id: string; stream_url: string; status?: string }>;
  request = approvePlanViaSendOnce(planId, id).finally(() => {
    if (inFlightPlanApprovals.get(requestKey) === request) {
      inFlightPlanApprovals.delete(requestKey);
    }
  });
  inFlightPlanApprovals.set(requestKey, request);
  return request;
}

async function approvePlanViaSendOnce(
  planId: string,
  id: { workspaceId: string; sessionId?: string | null; expectedRev: number | undefined },
): Promise<{ turn_id: string; stream_url: string; status?: string }> {
  if (!id.sessionId) {
    throw new Error("plan.approve.send requires the Plan Session");
  }

  if (!Number.isSafeInteger(id.expectedRev) || (id.expectedRev ?? 0) < 1) {
    throw new ApiError(409, getChatSendErrorMessage("plan_not_approvable"), { code: "plan_not_approvable" });
  }

  const pendingKey = `${id.workspaceId}\u0000${id.sessionId}\u0000${planId}`;
  const pendingAction = pendingPlanApprovalActions.get(pendingKey);
  if (pendingAction) {
    try {
      const receipt = await lookupComposerChatAction(pendingAction);
      pendingPlanApprovalActions.delete(pendingKey);
      return {
        turn_id: receipt.turn_id,
        stream_url: receipt.stream_url,
        status: "started",
      };
    } catch (error) {
      if (
        error instanceof ApiError
        && error.code === "chat_action_not_found"
      ) {
        const state = await fetchPlanStateAuthed(planId, {
          workspaceId: id.workspaceId,
        });
        if (state.plan.retryable !== true) {
          throw new ApiError(
            503,
            "Plan execution admission is still being reconciled",
            { code: "terminal_dependency_recovering" },
          );
        }
        // 404 + authoritative retryable=true makes a new action safe to race:
        // Plan CAS still precedes Terminal admission, so at most one can win.
        pendingPlanApprovalActions.delete(pendingKey);
        if (state.plan.current_rev !== id.expectedRev) {
          throw new ApiError(409, getChatSendErrorMessage("plan_not_approvable"), { code: "plan_not_approvable" });
        }
      } else {
        throw error;
      }
    }
  }

  const bootstrap: ChatExecutionBootstrap = (
    await fetchSessionChatExecutionPreflight(id.sessionId)
  ).chat_execution;

  const body = {
    workspace_id: id.workspaceId,
    session_id: id.sessionId,
    plan_execution_id: planId,
    expected_plan_revision: id.expectedRev,
    message: "",
  };
  if (bootstrap.write_protocol === "legacy_v0") {
    const response = await fetch("/api/v1/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...viewerTimeZoneHeaders() },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw await readApiError(response);
    }
    return response.json();
  }
  if (bootstrap.write_protocol !== "terminal_v1") {
    const status = bootstrap.write_protocol === "refresh_required" ? 409 : 503;
    throw new ApiError(status, "Plan execution writer is not ready", {
      code: bootstrap.write_protocol === "refresh_required"
        ? "client_refresh_required"
        : "terminal_protocol_writer_not_ready",
    });
  }

  const action = createComposerChatAction({
    actionKind: "start",
    sourceRevision: null,
    sessionId: id.sessionId,
  });
  pendingPlanApprovalActions.set(pendingKey, action);
  let sendError: unknown = new Error("plan.approve.send outcome is unknown");
  try {
    const response = await singleAttemptChatActionFetch(
      "/api/v1/chat/send",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...TERMINAL_CHAT_CAPABILITY_HEADERS,
          ...viewerTimeZoneHeaders(),
        },
        body: JSON.stringify({
          ...body,
          client_info: {
            action_kind: "start",
            request_id: action.actionId,
            user_msg_id: composerChatActionUserMessageId(action),
          },
        }),
      },
    );
    if (!response.ok) {
      const error = await readApiError(response);
      if (isDefinitiveComposerChatActionRejection(error, action)) {
        pendingPlanApprovalActions.delete(pendingKey);
        throw error;
      }
      sendError = error;
    } else {
      const result = (await response.json()) as unknown;
      if (isComposerChatActionResponse(result, action)) {
        pendingPlanApprovalActions.delete(pendingKey);
        return result;
      }
      sendError = new Error("plan.approve.send returned an invalid action receipt");
    }
  } catch (error) {
    if (isDefinitiveComposerChatActionRejection(error, action)) {
      pendingPlanApprovalActions.delete(pendingKey);
      throw error;
    }
    sendError = error;
  }

  try {
    const receipt = await lookupComposerChatAction(action);
    pendingPlanApprovalActions.delete(pendingKey);
    return {
      turn_id: receipt.turn_id,
      stream_url: receipt.stream_url,
      status: "started",
    };
  } catch {
    // A failed lookup is inconclusive: preserve the pending action so a later
    // explicit click must reconcile it and authoritative Plan state before it
    // can choose a new identity.
    throw sendError;
  }
}

/** A (editable): structured editing persists entire edited steps (PROPOSED-only).
 *  Returns backend-normalized steps (feed applyPlanSnapshot). */
export async function patchPlanSteps(
  planId: string,
  steps: PlanStepView[],
  id: Pick<PlanIdentity, "workspaceId" | "userId"> & { expectedRev: number | undefined },
): Promise<{ plan_id: string; status: string; current_rev: number; steps: Array<Record<string, unknown>> }> {
  if (!Number.isSafeInteger(id.expectedRev) || (id.expectedRev ?? 0) < 1) {
    throw new ApiError(409, getChatSendErrorMessage("plan_not_approvable"), { code: "plan_not_approvable" });
  }
  const r = await fetch(`/api/v1/plans/${encodeURIComponent(planId)}/steps`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: id.workspaceId,
      user_id: id.userId,
      expected_rev: id.expectedRev,
      steps: steps.map(toEditStep),
    }),
  });
  if (!r.ok) {
    throw await readApiError(r);
  }
  return r.json();
}

/** A (editable): natural-language amendment returns **preview** steps (not persisted; FE confirmation calls patchPlanSteps). */
export async function amendPlan(
  planId: string,
  instruction: string,
  id: Pick<PlanIdentity, "workspaceId" | "userId">,
): Promise<{ plan_id: string; preview: boolean; expected_rev: number; steps: Array<Record<string, unknown>> }> {
  const r = await fetch(`/api/v1/plans/${encodeURIComponent(planId)}/amend`, {
    method: "POST",
    // The amend preview's model call dates relative periods on the viewer's calendar.
    headers: { "Content-Type": "application/json", ...viewerTimeZoneHeaders() },
    body: JSON.stringify({
      workspace_id: id.workspaceId,
      user_id: id.userId,
      instruction,
    }),
  });
  if (!r.ok) {
    // ApiError carries status/error codes/request ID for diagnostics; never show message (plan card localizes by status).
    throw await readApiError(r);
  }
  return r.json();
}

/** Batch 3 R3.2: revise/retry failed/blocked step in two phases: preview (LLM revised steps), then confirm (persist
 *  preview exactly, no further server LLM call). 409 {plan_busy_or_conflict} means
 *  worker tail window/concurrent conflict; caller asks retry later, **never downgrade card to proposed**. */
export interface RevisedStepPayload {
  agent_name: string;
  objective: string;
  deliverable: string;
  needs_corpus: boolean;
  needs_web: boolean;
  success_criteria: string[];
  agent_choice?: Record<string, unknown> | null;
}

export async function reviseStepPreview(
  planId: string,
  stepId: string,
  body: { workspaceId: string; instruction?: string },
): Promise<{ revised: RevisedStepPayload; expected_rev: number; warning?: string; criteria_missing?: boolean }> {
  const r = await fetch(
    `/api/v1/plans/${encodeURIComponent(planId)}/revise_step/${encodeURIComponent(stepId)}`,
    {
      method: "POST",
      // The preview's model call dates relative periods on the viewer's calendar.
      headers: { "Content-Type": "application/json", ...viewerTimeZoneHeaders() },
      body: JSON.stringify({ workspace_id: body.workspaceId, instruction: body.instruction ?? "", confirm: false }),
    },
  );
  if (!r.ok) {
    // ApiError carries status/error codes/request ID for diagnostics; never show message (plan card localizes by status).
    throw await readApiError(r);
  }
  return r.json();
}

export async function reviseStepConfirm(
  planId: string,
  stepId: string,
  body: { workspaceId: string; expectedRev: number; revised: RevisedStepPayload },
): Promise<{ revised_step_id: string; current_rev: number; steps: Array<Record<string, unknown>> }> {
  const r = await fetch(
    `/api/v1/plans/${encodeURIComponent(planId)}/revise_step/${encodeURIComponent(stepId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workspace_id: body.workspaceId,
        confirm: true,
        expected_rev: body.expectedRev,
        revised: body.revised,
      }),
    },
  );
  if (!r.ok) {
    // ApiError carries status/error codes/request ID for diagnostics; never show message (plan card localizes by status).
    throw await readApiError(r);
  }
  return r.json();
}

/** Batch 3 R3.3: full replanning keeps done steps as fixed inputs, regenerating only unfinished subgraph. */
export async function replanPreview(
  planId: string,
  body: { workspaceId: string; instruction?: string },
): Promise<{
  new_steps: Array<Record<string, unknown>>;
  kept_done: string[];
  superseded: string[];
  expected_rev: number;
  warning?: string;
  criteria_missing?: string[];
}> {
  const r = await fetch(`/api/v1/plans/${encodeURIComponent(planId)}/replan`, {
    method: "POST",
    // The preview's model call dates relative periods on the viewer's calendar.
    headers: { "Content-Type": "application/json", ...viewerTimeZoneHeaders() },
    body: JSON.stringify({ workspace_id: body.workspaceId, instruction: body.instruction ?? "", confirm: false }),
  });
  if (!r.ok) {
    // ApiError carries status/error codes/request ID for diagnostics; never show message (plan card localizes by status).
    throw await readApiError(r);
  }
  return r.json();
}

export async function replanConfirm(
  planId: string,
  body: { workspaceId: string; expectedRev: number; newSteps: Array<Record<string, unknown>> },
): Promise<{ current_rev: number; steps: Array<Record<string, unknown>> }> {
  const r = await fetch(`/api/v1/plans/${encodeURIComponent(planId)}/replan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: body.workspaceId,
      confirm: true,
      expected_rev: body.expectedRev,
      new_steps: body.newSteps,
    }),
  });
  if (!r.ok) {
    // ApiError carries status/error codes/request ID for diagnostics; never show message (plan card localizes by status).
    throw await readApiError(r);
  }
  return r.json();
}


/** B (blocked recovery): skip a blocked/failed step (authed — server derives identity via
 *  _current_user; only workspace_id in the body). After skip, the caller re-approves
 *  (approve accepts BLOCKED) → resume runs downstream without the skipped step. */
export async function skipPlanStep(
  planId: string,
  stepId: string,
  id: { workspaceId: string; expectedRev: number | undefined },
): Promise<{ plan_id: string; step_id: string; status: string; current_rev: number; steps: Array<Record<string, unknown>> }> {
  if (!Number.isSafeInteger(id.expectedRev) || (id.expectedRev ?? 0) < 1) {
    throw new ApiError(409, getChatSendErrorMessage("plan_not_approvable"), { code: "plan_not_approvable" });
  }
  const r = await fetch(
    `/api/v1/plans/${encodeURIComponent(planId)}/skip_step/${encodeURIComponent(stepId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: id.workspaceId, expected_rev: id.expectedRev }),
    },
  );
  if (r.status === 409) {
    throw new ApiError(409, getChatSendErrorMessage("plan_not_approvable"), { code: "plan_not_approvable" });
  }
  if (!r.ok) {
    // ApiError carries status/error codes/request ID for diagnostics; never show message (plan card localizes by status).
    throw await readApiError(r);
  }
  return r.json();
}

/** planning-ux-reliability (2026-07): authed read-only plan state — server derives
 *  identity (_current_user); only workspace_id as query. Used by the mount-time
 *  reconcile after F5/archived replay (event replay is unordered → state drift). */
export async function fetchPlanStateAuthed(
  planId: string,
  id: { workspaceId: string; signal?: AbortSignal },
): Promise<{ plan_id: string } & Omit<PlanStateResponse, "plan_id">> {
  const qs = new URLSearchParams({ workspace_id: id.workspaceId });
  const r = await fetch(
    `/api/v1/plans/${encodeURIComponent(planId)}/state?${qs.toString()}`,
    { signal: id.signal },
  );
  if (!r.ok) throw new Error(`plan.state_authed ${r.status}`);
  const body = (await r.json()) as Omit<PlanStateResponse, "plan_id">;
  return { plan_id: planId, ...body };
}

/** C(durable cancel), FE wired 2026-07: cancel server-side (CANCELLED sticky).
 *  Previously Cancel only removed frontend store, leaving backend PROPOSED forever; F5 replay revived card. */
export async function cancelPlan(
  planId: string,
  id: { workspaceId: string },
): Promise<{ plan_id: string; status: string }> {
  const r = await fetch(`/api/v1/plans/${encodeURIComponent(planId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: id.workspaceId }),
  });
  if (!r.ok) {
    // ApiError carries status/error codes/request ID for diagnostics; never show message (plan card localizes by status).
    throw await readApiError(r);
  }
  return r.json();
}
