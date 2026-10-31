/**
 * useTurnRunner — drive one turn's state evolution.
 *
 * Two modes:
 *  - mode="mock" — play the scenarios.ts timeline with setTimeout
 *  - mode="sse"  — subscribe to real SSE from Gateway `/api/v1/chat/stream/{turn_id}`
 *
 * Switch with NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse|mock; default is mock.
 */

"use client";
import { requireMaintenanceReceipt, type ContextMaintenanceReceipt } from "./context-maintenance";
import { canonicalizeComposerAttachmentRef } from "./composer-attachment-refs";
import { projectDisplayUsage } from "@/lib/document-usage-display";


import { parseDocumentRevisionTarget, sameDocumentRevisionTarget } from "./composer-document-revision";
import { appPath } from "@/lib/base-path";
import { reportSectionsSavedFromDone } from "./report-sections-saved";
import { citationFromPayload, mergeCitation } from "@/lib/citation-payload";
import {
  createTransportSample,
  noteEvent as noteTransportEvent,
  noteKeepalive as noteTransportKeepalive,
  type TransportSample,
} from "@/lib/transport-health";
import { listClarificationsForTurn } from "@/lib/api";
import { clarificationResumeFailure } from "./hydrate-clarifications";
import { useCallback, useEffect, useRef, useState } from "react";

import { attachCanvasSSE, type CanvasSeqGate } from "./canvas-sse";
import {
  getChatSendErrorMessage,
  resolveChatSendErrorReason,
  resolveModelUnconfiguredPresentation,
} from "./chat-send-errors";
import { clarificationDisplayAnswer, getAnsweredClarification, isClarificationAnswered, markClarificationAnswered } from "./clarification-tombstone";
import { clearPyCells, clearSectionsForChatTurn, getCanvasSnapshot, hasBufferedCanvasContentForTurn, pruneStreamingSections, reconcileCanvasWithServer, resetCanvas, revealBufferedSectionsForTurn } from "./canvas-store";
import { SCENARIOS } from "./scenarios";
import { toast } from "./use-toast";
import { applyEntitlementDenial, applyEntitlementHeaders } from "./entitlement-store";
import { track, trackComposerTransportFailure } from "./metrics";
import { unpackPreferredModel } from "./provider-registry";
import { thinkingHint, toolActionLine, toolLabel, toolResultLine } from "./tool-labels";
import { getCurrentLocale } from "./locale";
import { apiFetch, readApiError } from "./api-client";
import { refreshAccessToken } from "./auth-api";
import { getPlanningModePref } from "./planning-mode-pref";
import { useComposerCapabilities } from "./use-composer-capabilities";
import { getTurnSnapshot, resetTurn } from "./turn-store";
import {
  formatTurnDuration,
  resolveLiveTurnDurationMs,
} from "./turn-duration";
import { resolveUserMessageTimeMs } from "./turn-user-time";
import { requestAutoOpenDraft } from "./config-copilot-draft";
import {
  clearPendingSubmission,
  captureDocumentRevisionSubmission,
  preparePendingSubmission,
  preparePendingReferenceSubmission,
  captureReferenceSubmissionDraft,
  type ComposerSubmissionWireSnapshot,
  type RecoveredReferenceSubmission,
  type RecoveredSubmission,
  type SubmissionRecoveryScope,
} from "./composer-submission-recovery";
import {
  composerChatActionUserMessageId,
  isComposerChatActionResponse,
  isDefinitiveComposerChatActionRejection,
  singleAttemptChatActionFetch,
  type ComposerChatAction,
} from "./composer-chat-action";
import type { InputDocument } from "./composer-input-document";
import { completeComposerScope, composerDraftScopeSignature } from "./composer-persistence-scope";
import { TERMINAL_CHAT_CAPABILITY_HEADERS } from "./terminal-chat-capability";
import { viewerTimeZoneHeaders } from "./time-zone";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import { isLegacyFailureAnswerReason, parseRecoveryAction } from "./turn-feedback";
import { parseThetaQuestions } from "./clarification-questions";
import {
  normalizeCanvasFailureReasonCode,
  normalizeReasonCode,
} from "./error-messages";
import { terminalErrorDiagnostics } from "./chat-failure-diagnostics";
import type { ErrorDiagnostics } from "./error-presentation";
import {
  backfillSpawnSubagentId,
  matchPlanStepForTool,
  planFinish,
  planStepResolved,
  planStepRunning,
} from "./trace-l-reducer";
import type {
  AttachmentRef,
  ChatMessage,
  IntentType,
  Scenario,
  TimelineEvent,
  ToolChipState,
  TraceStep,
  TurnRunnerState,
  TurnSummary,
} from "./types";
import { randomUuid } from "./random-id";

const INITIAL_STATE: TurnRunnerState = {
  user: null,
  intent: null,
  complexity: null,
  chips: [],
  answer: "",
  artifact: null,  // Mock timeline (m30 scenarios) still writes; the SSE artifact_ready path is dead
  source: null,
  summary: null,
  traceSteps: [],
  turnId: null,
  executionGeneration: null,
  terminalProtocolVersion: null,
  sessionId: null,
  sessionTitle: null,
  running: false,
  terminalStatus: null,
  effectDisclosure: null,
  effectDisclosureDiscoveryPending: false,
  started: false,
  reportOnly: false,
  messages: [],
  userAttachments: [],
  thinking: "",
  thinkingLength: 0,
  lastThinkingHintAttachedAt: "",
  clarifications: [],
  createToolOpen: null,
  subagentCreatorOpen: null,
  solidifyOpen: null,
  plan: null,
  inlinePreview: null,
  // Sprint θ Phase 8d defaults
  clarificationPending: null,
  aborted: false,
  errorReason: null,
  sendErrorReason: null,
  dispatchRole: null,
  peersDispatched: [],
  archivedTurns: [],
  // chat-redesign Phase 1.7-A
  userMsgId: null,
  startedAt: null,
  // prod-bug-bash-038 #5
  // prod-bug-bash-039 Phase 5 (0.39.3)
  citations: [],
};

const DEFINITIVE_LOCAL_UNLOCK_CODES = new Set([
  "empty_message",
  "message_too_long",
  "submission_contract_inactive",
  "submission_kind_unavailable",
  "replay_source_not_found",
  "turn_became_active",
  "steer_parent_not_active",
  "user_credit_exhausted",
  "team_pool_exhausted",
  "session_deleted",
]);

/**
 * A 409 or scoped settings refusal marked `admission: "not_started"` rejected the
 * submission before any Turn, dispatch or receipt existed. The same codes also follow a failed
 * Turn persist, where the Turn may have committed, so the code alone never releases the journal.
 */
function isRejectedBeforeAdmission(err: unknown): boolean {
  const { status, body } = (err ?? {}) as { status?: number; body?: unknown };
  const error = (body as { detail?: { error?: { admission?: unknown; code?: unknown } } } | null)?.detail?.error;
  const settingsDenial = (status === 403 || status === 503)
    && typeof error?.code === "string" && error.code.startsWith("configuration_target_");
  return (status === 409 || settingsDenial) && error?.admission === "not_started";
}

/** Accept only registered i18n codes; legacy prose fields are never a fallback. */
function terminalReasonCode(value: unknown): string {
  return normalizeReasonCode(value);
}

/**
 * Chat turn feedback D3: once the failure terminal arrives, drop an answer that was only a
 * legacy system failure fallback. The same events replay through this reducer, so live and
 * refresh agree; new failed turns never emit such an answer.
 */
function clearLegacyFailureAnswer(ns: TurnRunnerState, s: TurnRunnerState): void {
  if (ns.answerIsLegacyFailureFallback ?? s.answerIsLegacyFailureFallback) {
    ns.answer = "";
    ns.answerIsLegacyFailureFallback = false;
  }
}


function diagnosticsMatch(
  left: ErrorDiagnostics,
  right: ErrorDiagnostics,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function keepFirstTerminalDiagnostics(
  prior: ErrorDiagnostics | undefined,
  incoming: ErrorDiagnostics | undefined,
  turnId: string,
  reasonCode: string,
): ErrorDiagnostics | undefined {
  if (!prior) return incoming;
  if (
    incoming &&
    !diagnosticsMatch(prior, incoming)
  ) {
    console.warn("chat.turn_failure_diagnostics_mismatch", {
      turnId,
      reasonCode,
    });
  }
  return prior;
}

/**
 * Phase 1.7-A — generate a stable `bub-msg-${id}` for the user bubble at
 * submit time. ULID is the v8 spec but the project hasn't pulled the
 * dep yet; use `randomUuid()` (lib/random-id) — it must go through the helper
 * because plain HTTP deployments lack a secure context, leaving the native implementation undefined.
 */
function _newUserMsgId(): string {
  const rand = randomUuid().replace(/-/g, "").slice(0, 16);
  return `bub-msg-${rand}`;
}

/** Sprint canvas-pipeline-fix B4: snapshot current turn's full v3 state.
 * Called at both archive sites (new-turn-send and done-event-archive). */
export function snapshotArchivedTurn(s: TurnRunnerState): import("./types").ArchivedTurn | null {
  // Skip if no meaningful content (e.g. cleared mid-turn or fresh state)
  if (
    !s.user
    && !s.answer
    && s.userAttachments.length === 0
    && (s.traceSteps?.length ?? 0) === 0
    && s.operationKind !== "context_maintenance"
  ) return null;
  // chat-redesign Phase 1.7-A — snapshot turn-store beats[] at archive time
  // so ArchivedTurnView (1.7-B) can iterate the same composition as live.
  // If turn-store has no entry yet (e.g. early archive before any event
  // dispatched), default to empty array.
  const turnStoreSnap = s.turnId ? getTurnSnapshot(s.turnId) : undefined;
  const beats = turnStoreSnap?.beats ?? [];
  return {
    turnId: s.turnId,
    operationKind: s.operationKind,
    terminalStatus: s.terminalStatus ?? null,
    executionGeneration: s.executionGeneration ?? null,
    effectDisclosure: s.effectDisclosure ?? null,
    effectDisclosureDiscoveryPending:
      s.effectDisclosureDiscoveryPending ?? false,
    user: s.user ?? "",
    userAttachments: s.userAttachments ?? [],
    answer: s.answer ?? "",
    traceSteps: s.traceSteps ?? [],
    peersDispatched: s.peersDispatched ?? [],
    errorReason: s.errorReason ?? null,
    responseKind: s.responseKind,
    toolFindings: (s as TurnRunnerState & {
      toolFindings?: Array<{ callId: string; finding: string; parentAgentId?: string | null; seq?: number }>;
    }).toolFindings,
    headline: s.headline,
    // Sprint phase-plan-removal: v4TurnPlan + v4PhaseUpdates snapshot fields deleted.
    subagentSummaries: s.subagentSummaries,
    finalPayload: s.finalPayload,
    // Sprint phase-plan-removal Phase A0 r2 — carry shape-gate signal into
    // archive so hydrating prior turns mounts V4 path immediately.
    hasDispatched: s.hasDispatched,
    // chat-redesign Phase 1.7-A — stable msg-id + beats snapshot.
    userMsgId: s.userMsgId ?? null,
    beats,
    // prod-bug-bash-039 Phase 5 (0.39.3) — citations registry snapshot.
    citations: s.citations ?? [],
    // Likewise, retain the notice that explicit planning mode did not produce a plan after archiving.
    planningFallback: s.planningFallback,
    archivedAt: Date.now(),
    // User-bubble time: first turn_started (never overwritten by same-turn continuation), or send time if absent.
    // ⚠ Do not prefer `s.startedAt`: after clarification resumes, it is the new generation's start.
    userSentAt: resolveUserMessageTimeMs({
      firstTurnStartedAt: turnStoreSnap?.startedAt ?? null,
      submittedAtMs: s.startedAt,
    }),
  };
}

export function mergePolledEffectDisclosure(
  state: TurnRunnerState,
  turnId: string,
  executionGeneration: number,
  overlay: import("./api").EffectDisclosureOverlayDTO | null,
): TurnRunnerState {
  const sameProof = (
    current: import("./api").EffectDisclosureOverlayDTO | null | undefined,
  ) => Boolean(
    overlay != null
    && current
    && current.proof_digest === overlay.proof_digest
    && current.receipt_id === overlay.receipt_id
    && current.finalized === overlay.finalized
  );
  let changed = false;
  let next = state;
  if (
    state.turnId === turnId
    && state.executionGeneration === executionGeneration
    && (
      state.effectDisclosureDiscoveryPending
      || (overlay != null && !sameProof(state.effectDisclosure))
    )
  ) {
    next = {
      ...next,
      ...(overlay != null ? { effectDisclosure: overlay } : {}),
      effectDisclosureDiscoveryPending: false,
    };
    changed = true;
  }
  const archivedTurns = (next.archivedTurns ?? []).map((archived) => {
    if (
      archived.turnId !== turnId
      || archived.executionGeneration !== executionGeneration
      || (
        !archived.effectDisclosureDiscoveryPending
        && (overlay == null || sameProof(archived.effectDisclosure))
      )
    ) {
      return archived;
    }
    changed = true;
    return {
      ...archived,
      ...(overlay != null ? { effectDisclosure: overlay } : {}),
      effectDisclosureDiscoveryPending: false,
    };
  });
  return changed ? { ...next, archivedTurns } : state;
}

/** Completed turns have one archive projection in mock and real SSE modes.
 * Mutates only the reducer's fresh `next` object; `source` remains the
 * pre-event snapshot used for messages and ArchivedTurn provenance. */
function archiveCompletedTurnInPlace(
  source: TurnRunnerState,
  next: TurnRunnerState,
  terminalStatus: string = "completed",
): void {
  if (!source.user && !source.answer && source.userAttachments.length === 0) return;

  const messages: ChatMessage[] = [...source.messages];
  if (source.user || source.userAttachments.length > 0) {
    messages.push({
      role: "user",
      content: source.user ?? "",
      turnId: source.turnId,
      ...(source.userAttachments.length
        ? { attachments: source.userAttachments }
        : {}),
    });
  }
  if (source.answer) {
    messages.push({
      role: "assistant",
      content: source.answer,
      turnId: source.turnId,
    });
  }

  const priorSnapshot = snapshotArchivedTurn({
    ...source,
    terminalStatus,
    errorReason: source.errorReason?.source === "clarification_resume" ? null : source.errorReason,
  });
  next.archivedTurns = priorSnapshot
    ? [...(source.archivedTurns ?? []), priorSnapshot]
    : (source.archivedTurns ?? []);
  next.messages = messages;
  next.user = null;
  next.answer = "";
  next.userAttachments = [];
  next.traceSteps = [];
  next.peersDispatched = [];
  next.chips = [];
  next.thinking = "";
  next.thinkingLength = 0;
  next.headline = undefined;
  next.userMsgId = null;
  next.startedAt = null;
  next.effectDisclosure = null;
  next.effectDisclosureDiscoveryPending = false;
  // This runs inside a React state updater, which may run while the host component renders; a synchronous
  // canvas-store write would re-render its subscribers (ChatWorkbench) during that render. Defer the clear
  // past the render, keeping the archive condition above (nothing to archive → pyCells stay). Idempotent, so
  // a StrictMode double invocation is harmless.
  queueMicrotask(() => {
    try { clearPyCells(); } catch { /* ignore */ }
  });
}

/**
 * Canvas-store effects of a terminal SSE event. They must run outside `setState` updaters: an updater can
 * run while the hook's host component renders, and a store write there makes React warn "Cannot update a
 * component (`ChatWorkbench`) while rendering a different component" (seen when a hosted Skill opens).
 */
function terminalCanvasEffects(name: string, payload: Record<string, unknown>, fallbackTurnId: string): void {
  const turnId = String(payload.turn_id ?? fallbackTurnId ?? "");
  if (name === "done") {
    // plan_proposed ends the proposal turn without revealing or archiving (the plan card stays in place).
    if (String(payload.error_kind ?? "") === "plan_proposed") return;
    // Reveal-only and idempotent (see the done reducer for why done never clears). pyCells are cleared by
    // the archive step itself, under its own "anything to archive" condition (`archiveCompletedTurnInPlace`).
    if (turnId) revealBufferedSectionsForTurn(turnId);
    return;
  }
  if (name === "turn_failed" || name === "turn_aborted") {
    // Sprint κ Phase C: a cancelled turn drops streaming-state sections; done-state sections stay.
    if (name === "turn_aborted") pruneStreamingSections();
    // chat-truth Phase Q (P1-Q1): reveal partial canvas content when the turn has some, else clear.
    if (!turnId) return;
    if (hasBufferedCanvasContentForTurn(getCanvasSnapshot(), turnId)) revealBufferedSectionsForTurn(turnId);
    else clearSectionsForChatTurn(turnId);
  }
}

/** chat-solidify O2/UX-3 fix (2026-07-04, code-review): archive the previous turn + return all next-turn
 *  blank-state fields. **Both new-turn entries — a new user message (send) and attaching to steer continuation
 *  (turn.follow_up) — share this function**, preventing two manually copied reset-field lists from drifting
 *  (a new anti-stale field updated on only one path leaves old data on the other).
 *
 *  archive uses snapshotArchivedTurn's null guard: after done(completed) archives and clears
 *  live (user=null/answer=""), it returns null, avoiding duplicate archival; after done(failed/aborted),
 *  live retains content for terminal-card trace, so archive it here. Otherwise continuation overwrites
 *  the failed turn's bubble+partial+trace until F5 (the live≠F5 discrepancy this sprint addresses). */
function archiveAndFreshTurn(
  s: TurnRunnerState,
  overrides: {
    user: string | null;
    turnId: string | null;
    userAttachments?: AttachmentRef[];
    executionGeneration?: number;
    terminalProtocolVersion?: number;
  },
): TurnRunnerState {
  const archived: ChatMessage[] = [...s.messages];
  if (s.user || s.userAttachments.length > 0) {
    archived.push({
      role: "user", content: s.user ?? "", turnId: s.turnId,
      ...(s.userAttachments.length ? { attachments: s.userAttachments } : {}),
    });
  }
  if (s.answer) archived.push({ role: "assistant", content: s.answer, turnId: s.turnId });
  const priorSnapshot = snapshotArchivedTurn(s);
  const newArchived = priorSnapshot
    ? [...(s.archivedTurns ?? []), priorSnapshot]
    : (s.archivedTurns ?? []);
  return {
    ...s,
    messages: archived,
    archivedTurns: newArchived,
    user: overrides.user,
    userAttachments: overrides.userAttachments ?? [],
    turnId: overrides.turnId,
    executionGeneration: overrides.executionGeneration ?? null,
    terminalProtocolVersion: overrides.terminalProtocolVersion ?? null,
    // Fully clear every previous-turn state that must not leak into a new turn (anti-stale-carryover).
    // When adding such fields, **change only here**; send + steer stay synchronized automatically.
    errorReason: null,
    terminalStatus: null,
    reportSectionsSaved: null,
    effectDisclosure: null,
    effectDisclosureDiscoveryPending: false,
    sendErrorReason: null,
    intent: null,
    complexity: null,
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    chips: [],
    thinking: "",
    thinkingLength: 0,
    lastThinkingHintAttachedAt: "",
    clarifications: [],
    plan: null,
    // Conversational capability authoring overlays are one-shot UI intents —
    // must not survive into the next user turn (origin planning integrated; otherwise stale
    // cca.tool_create_open reopens the dialog after an unrelated follow-up).
    createToolOpen: null,
    subagentCreatorOpen: null,
    configurationCanvasOpen: null,
    settingsProposalIds: [],
    solidifyOpen: null,
    clarificationPending: null,
    cancelled: false,
    aborted: false,
    headline: undefined,
    hasDispatched: false,
    subagentSummaries: {},
    finalPayload: undefined,
    citations: [],
    userMsgId: _newUserMsgId(),
    startedAt: Date.now(),
    running: true,
    started: true,
  };
}

type Reducer = (prev: TurnRunnerState) => TurnRunnerState;

function applyEvent(ev: TimelineEvent): Reducer {
  return (s) => {
    const ns = { ...s };
    switch (ev.kind) {
      case "turn_started":
        break;
      case "intent":
        ns.intent = ev.intent;
        ns.complexity = ev.complexity;
        break;
      case "trace_step":
        ns.traceSteps = [...s.traceSteps, ev.step];
        break;
      case "tool_call": {
        // Sprint phase-plan-removal Phase A0: parity with SSE path — flip shape gate.
        ns.hasDispatched = true;
        const chip: ToolChipState = { id: ev.id, name: ev.name, state: "running", dur: null };
        ns.chips = [...s.chips, chip];
        // V6 Phase B: include toolName on mock traceStep so step_card can humanize through the V5 dictionary
        // (already set on the real SSE path at line 463).
        ns.traceSteps = [
          ...s.traceSteps,
          {
            id: `tool_${ev.id}`,
            title: `Tool · ${ev.name}`,
            kind: "running",
            input: ev.input,
            toolName: ev.name,
          },
        ];
        break;
      }
      case "tool_result": {
        ns.chips = s.chips.map((c) => (c.id === ev.id ? { ...c, state: "success", dur: ev.dur } : c));
        ns.traceSteps = s.traceSteps.map((st) =>
          st.id === `tool_${ev.id}`
            ? { ...st, kind: "success", dur: `${ev.dur}ms`, output: ev.output, sql: ev.sql }
            : st,
        );
        break;
      }
      case "partial_answer":
        ns.answer = s.answer + ev.text;
        break;
      case "artifact":
        ns.artifact = ev.card;
        break;
      case "source":
        ns.source = { text: ev.text, url: ev.url };
        break;
      case "done":
        ns.summary = ev.summary;
        ns.running = false;
        ns.terminalStatus = "completed";
        archiveCompletedTurnInPlace(s, ns, "completed");
        break;
      case "clarification_required":
        // D8 e2e: allow mock scenarios to inject ClarificationCard, matching the real SSE path
        if (!s.clarifications.some((c) => c.request_id === ev.payload.request_id)) {
          ns.clarifications = [...s.clarifications, ev.payload];
        }
        break;
      case "peer_dispatched": {
        // Sprint ρ Phase C/D — mock scenarios populate peersDispatched so rho
        // synthesiseEvents emits a peer_dispatched NormalisedEvent → PeerBlock.
        // λ F5 (R2-a): same upsert dedup as real SSE path (subagentId or peerName).
        // Sprint phase-plan-removal Phase A0: flip shape gate (mock parity).
        ns.hasDispatched = true;
        const existing = s.peersDispatched ?? [];
        const key = ev.peer.subagentId ?? ev.peer.peerName;
        const matchIdx = existing.findIndex((p) =>
          (p.subagentId ?? p.peerName) === key,
        );
        if (matchIdx >= 0) {
          ns.peersDispatched = [
            ...existing.slice(0, matchIdx),
            { ...existing[matchIdx], ...ev.peer },
            ...existing.slice(matchIdx + 1),
          ];
        } else {
          ns.peersDispatched = [...existing, ev.peer];
        }
        break;
      }
      case "turn_citation_registered": {
        // prod-bug-bash-039 Phase 5 (0.39.3) — mock parity with real SSE
        // reducer (Risk B). Scenarios can emit synthetic citations to test
        // ArtifactChip resolution paths in mock mode.
        // CTO Perf-Review #4 P0-2: return `s` on no-op dedup to skip render.
        const seq = Number(ev.seq);
        if (!Number.isInteger(seq) || seq < 1) return s;
        const existing = s.citations ?? [];
        if (existing.some((c) => c.seq === seq)) return s;
        const aid = String(ev.artifactId ?? "");
        if (!aid) return s;
        const kind: import("./types").Citation["kind"] = ev.citationKind ?? "data_ref";
        ns.citations = [
          ...existing,
          { seq, artifactId: aid, label: ev.label ?? null, kind },
        ];
        break;
      }
      case "error_set":
        // Sprint ρ Phase C/D — mock scenarios drive errorReason → FinalFailBanner.
        ns.errorReason = ev.reason;
        ns.running = false;
        break;
      case "clarification_pending":
        // Sprint ρ Phase C/D — θ-style turn-terminating clarification: surfaces
        // rho InlineClarification inside the lead agent body.
        ns.clarificationPending = ev.pending;
        break;
    }
    return ns;
  };
}

type MockActiveStep = {
  stepId: string;
  startedAt: number;
};

function dispatchMockTimelineEventToTurnStore(
  turnId: string,
  ev: TimelineEvent,
  activeSteps: Map<string, MockActiveStep>,
  getAnswerText: () => string,
): void {
  const nowIso = () => new Date().toISOString();
  const finishActiveSteps = (
    state: "success" | "failure",
    outputPreview: string | null,
  ) => {
    for (const [key, step] of Array.from(activeSteps.entries())) {
      dispatchToTurnStore("step_done", {
        card_id: turnId,
        step_id: step.stepId,
        step_type: key.startsWith("peer:") ? "subagent" : "tool_run",
        state,
        duration_ms: Math.max(1, Date.now() - step.startedAt),
        output_preview: outputPreview,
        ...(state === "failure"
          ? {
              error: {
                type: "timeout",
                // message_zh is the wire-contract field name; value is shown via
                // FailureAckCard → localize the fallback (outputPreview is backend text).
                message_zh:
                  outputPreview ??
                  (getCurrentLocale() === "zh-CN" ? "执行失败" : "Execution failed"),
                is_transient: true,
              },
            }
          : {}),
      });
      activeSteps.delete(key);
    }
  };

  switch (ev.kind) {
    case "tool_call": {
      const stepId = `tool_${ev.id}`;
      activeSteps.set(ev.id, { stepId, startedAt: Date.now() });
      dispatchToTurnStore("step_started", {
        card_id: turnId,
        step_id: stepId,
        step_type: "tool_run",
        header_zh: "运行工具",
        started_at: nowIso(),
        input_preview: ev.input,
        tool_name: ev.name,
        call_id: ev.id,
        ...(ev.subagentId ? { subagent_id: ev.subagentId } : {}),
      });
      break;
    }
    case "tool_result": {
      const step = activeSteps.get(ev.id);
      if (!step) break;
      dispatchToTurnStore("step_done", {
        card_id: turnId,
        step_id: step.stepId,
        step_type: "tool_run",
        state: "success",
        duration_ms: ev.dur,
        output_preview: ev.output,
        result_inline: ev.sql ? { sql: ev.sql } : undefined,
      });
      activeSteps.delete(ev.id);
      break;
    }
    case "peer_dispatched": {
      const peerName = ev.peer.peerName;
      const stepId = `peer_${peerName}_${activeSteps.size}`;
      const peerId = ev.peer.subagentId ?? stepId;
      activeSteps.set(`peer:${peerId}`, {
        stepId,
        startedAt: Date.now(),
      });
      // Match the production non-plan path: the Lead-owned spawn row lands
      // first, peer_dispatched attaches the peer id to that row, and
      // agent_spawned provides the authoritative Agent identity. Tool events
      // in the scenario carry the same explicit subagentId.
      dispatchToTurnStore("step_started", {
        card_id: turnId,
        step_id: stepId,
        step_type: "subagent",
        header_zh: "调用 peer",
        started_at: nowIso(),
        input_preview: ev.peer.parentCtxSummary ?? peerName,
        tool_name: "subagent_spawn",
      });
      dispatchToTurnStore("peer_dispatched", {
        card_id: turnId,
        peer_name: peerName,
        subagent_id: peerId,
        spawn_id: peerId,
      });
      dispatchToTurnStore("agent_spawned", {
        card_id: turnId,
        subagent_id: peerId,
        profile: peerName,
        display_name: ev.peer.displayName,
        task: ev.peer.parentCtxSummary ?? peerName,
      });
      break;
    }
    case "partial_answer": {
      dispatchToTurnStore("narrative.delta", {
        card_id: turnId,
        delta: ev.text,
      });
      break;
    }
    case "clarification_pending": {
      dispatchToTurnStore("clarification_required", {
        card_id: turnId,
        turn_id: turnId,
        clarification_id: ev.pending.clarificationId,
        candidates: ev.pending.candidates,
        fallback_text: ev.pending.fallbackText ?? null,
        questions: ev.pending.questions,
      });
      break;
    }
    case "error_set": {
      finishActiveSteps("failure", ev.reason.reasonCode);
      dispatchToTurnStore("turn_failed", {
        card_id: turnId,
        turn_id: turnId,
        reason: ev.reason.reasonCode,
      });
      dispatchToTurnStore("step_list_card_finalized", {
        card_id: turnId,
        state: "failed",
      });
      break;
    }
    case "done": {
      finishActiveSteps("success", ev.summary?.total ?? "done");
      dispatchToTurnStore("final_answer", {
        card_id: turnId,
        turn_id: turnId,
        text: getAnswerText(),
      });
      dispatchToTurnStore("step_list_card_finalized", {
        card_id: turnId,
        state: "completed",
      });
      dispatchToTurnStore("done", {
        card_id: turnId,
        turn_id: turnId,
        status: "completed",
      });
      break;
    }
  }
}

function finiteMsOrNull(ms: number | null | undefined): number | null {
  return typeof ms === "number" && Number.isFinite(ms) ? ms : null;
}

// ─── Mock runner (timeline playback) ────────────────────
function useMockRunner(
  scenarioId: string | null,
  replayKey: number,
  hydrate: UseTurnRunnerArgs["hydrate"] | null,
  hydrateTick: number,
  archivedTurnsPatch: ArchivedTurnsHydrationPatch | null,
  currentTurnHydrationPatch: CurrentTurnHydrationPatch | null,
): TurnRunnerState {
  const [state, setState] = useState<TurnRunnerState>(INITIAL_STATE);
  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  const answerRef = useRef("");

  // Canvas batch 4b remaining e2e cases (mounted only with NEXT_PUBLIC_BOWHEAD_E2E=1): mock mode has no SSE transport; use this to set mock runner
  // transport state (streamingState: reconnecting / given up), running flag and this turn's terminal state to SSE-path values, checking report-toolbar pause/interruption.
  // Change only these fields (transport state, running flag, terminal state and persisted section count), matching SSE runner setState's shape.
  useEffect(() => {
    if (typeof window === "undefined" || process.env.NEXT_PUBLIC_BOWHEAD_E2E !== "1") return;
    const w = window as unknown as Record<string, unknown>;
    w.__TURN_TEST_STATE__ = (
      patch: Partial<Pick<TurnRunnerState, "streamingState" | "running" | "terminalStatus" | "reportSectionsSaved">>,
    ) =>
      setState((s) => ({ ...s, ...patch }));
    return () => {
      delete w.__TURN_TEST_STATE__;
    };
  }, []);

  const cancel = useCallback(() => {
    timersRef.current.forEach(clearTimeout);
    timersRef.current = [];
  }, []);

  useEffect(() => {
    cancel();
    if (!scenarioId) {
      setState(INITIAL_STATE);
      answerRef.current = "";
      return;
    }
    const sc: Scenario | undefined = SCENARIOS[scenarioId];
    if (!sc) return;

    resetTurn(sc.turnId);
    answerRef.current = "";
    const activeSteps = new Map<string, MockActiveStep>();
    dispatchToTurnStore("step_list_card_created", { card_id: sc.turnId });

    setState({
      ...INITIAL_STATE,
      user: sc.user,
      turnId: sc.turnId,
      sessionTitle: sc.sessionTitle,
      running: true,
      started: true,
    });

    sc.timeline.forEach((ev) => {
      const t = setTimeout(() => {
        if (ev.kind === "partial_answer") answerRef.current += ev.text;
        dispatchMockTimelineEventToTurnStore(
          sc.turnId,
          ev,
          activeSteps,
          () => answerRef.current,
        );
        setState(applyEvent(ev));
      }, ev.at);
      timersRef.current.push(t);
    });

    return cancel;
  }, [scenarioId, replayKey, cancel]);

  useEffect(() => {
    if (!hydrate) return;
    cancel();
    answerRef.current = "";
    const hasRoutedLastTurn = Boolean(
      hydrate.lastTurnRouted
      || hydrate.lastOperationKind === "context_maintenance"
      || hydrate.lastFinalPayload !== undefined
      || hydrate.lastErrorReason,
    );
    const clarificationPending = resolveHydratedClarificationPending(
      hydrate.lastClarifications,
    );
    setState({
      ...INITIAL_STATE,
      sessionId: hydrate.sessionId,
      sessionTitle: hydrate.sessionTitle,
      messages: hydrate.messages,
      archivedTurns: hydrate.archivedTurns ?? [],
      intent: hydrate.lastIntent ?? null,
      turnId: hydrate.lastTurnId ?? null,
      operationKind: hydrate.lastOperationKind ?? null,
      startedAt: finiteMsOrNull(hydrate.lastStartedAt),
      user: hasRoutedLastTurn ? (hydrate.lastUserText ?? "") : "",
      userAttachments: hasRoutedLastTurn ? (hydrate.lastUserAttachments ?? []) : [],
      answer: hasRoutedLastTurn ? (hydrate.lastAnswer ?? "") : "",
      summary: hydrate.lastSummary ?? null,
      subagentSummaries: hydrate.lastSubagentSummaries,
      finalPayload: hydrate.lastFinalPayload,
      responseKind: hydrate.lastResponseKind ?? undefined,
      toolInvocationCount: hydrate.lastToolInvocationCount,
      reportSectionsSaved: hydrate.lastReportSectionsSaved ?? null,
      terminalStatus: hydrate.lastTerminalStatus ?? null,
      executionGeneration: hydrate.lastExecutionGeneration ?? null,
      terminalProtocolVersion: hydrate.lastTerminalProtocolVersion ?? null,
      effectDisclosure: hydrate.lastEffectDisclosure ?? null,
      effectDisclosureDiscoveryPending:
        hydrate.lastEffectDisclosureDiscoveryPending ?? false,
      errorReason: hydrate.lastErrorReason ?? null,
      clarificationPending,
      headline: hydrate.lastHeadline,
      citations: hydrate.lastCitations ?? [],
      running: false,
      started: hydrate.messages.length > 0 || hasRoutedLastTurn || Boolean(hydrate.reportOnly),
      reportOnly: Boolean(hydrate.reportOnly),
    });
  }, [hydrateTick, hydrate, cancel]);

  useEffect(() => {
    if (!archivedTurnsPatch) return;
    setState((current) => current.sessionId === archivedTurnsPatch.sessionId
      ? {
          ...current,
          archivedTurns: mergeArchivedTurnsHydration(
            current.archivedTurns ?? [],
            archivedTurnsPatch.turns,
          ),
        }
      : current);
  }, [archivedTurnsPatch]);

  useEffect(() => {
    if (!currentTurnHydrationPatch) return;
    setState((current) => mergeCurrentTurnHydration(
      current,
      currentTurnHydrationPatch,
    ));
  }, [currentTurnHydrationPatch]);

  return state;
}

// ─── Real SSE runner ────────────────────────────────────
// Match Gateway chat.py's SSE event contract (authoritative list in
// docs/design/chat-pipeline.md; wire() allowlist is the sole entry point)
// #109 Profile passthrough: read bowhead.user_profile from localStorage and include it in the request
function _loadUserProfile(): Record<string, string> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem("bowhead.user_profile");
    if (!raw) return null;
    const p = JSON.parse(raw) as Record<string, string>;
    // Include only nonempty, nondefault values
    const out: Record<string, string> = {};
    if (p.display_name) out.display_name = p.display_name;
    if (p.language && p.language !== "auto") out.language = p.language;
    if (p.answer_style && p.answer_style !== "detailed") out.answer_style = p.answer_style;
    return Object.keys(out).length ? out : null;
  } catch { return null; }
}

function _loadPreferredModel(): { providerId: string; model: string } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem("bowhead.preferred_model");
    return raw ? unpackPreferredModel(raw) : null;
  } catch {
    return null;
  }
}

export function captureComposerSubmissionWireSnapshot(
  sessionId: string | null,
  planningCapability: boolean,
  draftChatBinding: UseTurnRunnerArgs["draftChatBinding"] = null,
): ComposerSubmissionWireSnapshot {
  const preferred = _loadPreferredModel();
  return {
    composerSurface: sessionId ? "workbench" : "home",
    userProfile: _loadUserProfile(),
    preferredProvider: preferred?.providerId ?? null,
    preferredModel: preferred?.model ?? null,
    // Of the three states, only planning / off need wire fields; auto is the backend default and can be omitted,
    // keeping ordinary-turn request bodies byte-identical to before off was wired. Never send when capability is false.
    planningMode: planningCapability
      ? (({ planning: "planning", off: "off", auto: null }) as const)[
        getPlanningModePref()
      ]
      : null,
    ...(draftChatBinding ? { draftChatBinding: { ...draftChatBinding } } : {}),
  };
}

export async function postSendMessage(
  workspaceId: string,
  memberUid: string,
  message: string,
  sessionId: string | null,
  attachments?: AttachmentRef[],
  clientSubmissionId: string | null = null,
  replaySourceTurnId: string | null = null,
  frozenWireSnapshot: ComposerSubmissionWireSnapshot | null = null,
  chatAction: ComposerChatAction | null = null,
  referenceDocument: InputDocument | null = null,
  canSendReference: (() => boolean) | null = null,
): Promise<{
  turn_id: string;
  stream_url: string;
  session_id?: string;
  execution_generation?: number;
  terminal_protocol_version?: number;
}> {
  const revisionTarget = frozenWireSnapshot?.documentRevisionTarget;
  if (revisionTarget && (!clientSubmissionId || chatAction || replaySourceTurnId || referenceDocument || attachments?.length || revisionTarget.session_id !== sessionId))
    throw Object.assign(new Error("Document revision send identity unavailable"), { code: "document_revision_inputs_unsupported" });
  if (referenceDocument && (!clientSubmissionId || chatAction || replaySourceTurnId || !frozenWireSnapshot)) {
    throw Object.assign(new Error("reference send identity unavailable"), { code: "reference_input_invalid" });
  }
  if (chatAction && (clientSubmissionId || replaySourceTurnId)) {
    throw new Error("composer chat action cannot use legacy submission identity");
  }
  if (chatAction) {
    if (chatAction.actionKind !== "start" || chatAction.sessionId !== sessionId) {
      throw new Error("composer start action target mismatch");
    }
  }
  const profile = (referenceDocument || revisionTarget || frozenWireSnapshot?.contextMaintenance) && frozenWireSnapshot
    ? frozenWireSnapshot.userProfile
    : frozenWireSnapshot?.userProfile ?? _loadUserProfile();
  const preferred = frozenWireSnapshot
    ? (
        frozenWireSnapshot.preferredProvider && frozenWireSnapshot.preferredModel
          ? {
              providerId: frozenWireSnapshot.preferredProvider,
              model: frozenWireSnapshot.preferredModel,
            }
          : null
      )
    : _loadPreferredModel();
  const composerSurface = frozenWireSnapshot?.composerSurface
    ?? (sessionId ? "workbench" : "home");
  const planningMode = frozenWireSnapshot?.planningMode ?? null;
  const runtimeAttachmentIds = Array.from(new Set((attachments ?? []).flatMap((attachment) =>
    attachment.bundleArtifactIds?.length
      ? attachment.bundleArtifactIds
      : [attachment.fileId]
  )));
  const requestBody = {
    workspace_id: workspaceId,
    ...(frozenWireSnapshot?.contextMaintenance ? { context_maintenance: frozenWireSnapshot.contextMaintenance } : {}),
    ...(revisionTarget ? { document_revision_target: parseDocumentRevisionTarget(revisionTarget) } : {}),
    // Empty memberUid → Gateway resolves dynamically by identity × Space;
    // send only explicit frontend values (nonempty/nondefault), avoiding a permanently incorrect memberUid
    ...(memberUid ? { member_uid: memberUid } : {}),
    message: replaySourceTurnId ? "" : message,
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(!replaySourceTurnId && attachments?.length ? {
      attachments: runtimeAttachmentIds,
      attachment_refs: attachments,
    } : {}),
    client_info: {
      ...(chatAction?.actionKind === "start" && chatAction.agentDraftTarget ? {
        agent_draft_id: chatAction.agentDraftTarget.id,
        agent_draft_version: String(chatAction.agentDraftTarget.version),
      } : {}),
      composer_surface: composerSurface,
      ...(frozenWireSnapshot?.coachCanvasBinding ? { coach_canvas_target: JSON.stringify(frozenWireSnapshot.coachCanvasBinding) } : {}),
      ...(!replaySourceTurnId && frozenWireSnapshot?.continueCheckpointIds?.length
        ? { continue_checkpoints: JSON.stringify(frozenWireSnapshot.continueCheckpointIds.slice(0, 4)) }
        : {}),
      ...(frozenWireSnapshot?.workspaceSettingsBinding ? {
        configuration_settings_target: frozenWireSnapshot.workspaceSettingsBinding.clientInfo,
      } : {}),
      ...(!replaySourceTurnId && !chatAction && frozenWireSnapshot?.draftChatBinding ? {
        agent_draft_id: frozenWireSnapshot.draftChatBinding.draftId,
        agent_draft_version: String(frozenWireSnapshot.draftChatBinding.version),
        ...(frozenWireSnapshot.draftChatBinding.dirty ? { agent_draft_dirty: "1" } : {}),
      } : {}),
      ...(chatAction ? {
        request_id: chatAction.actionId,
        action_kind: chatAction.actionKind,
        user_msg_id: composerChatActionUserMessageId(chatAction),
      } : {}),
    },
    ...(profile ? { user_profile: profile } : {}),
    ...(preferred ? {
      preferred_provider: preferred.providerId,
      preferred_model: preferred.model,
    } : {}),
    // Planning Mode — the frozen wire snapshot only carries this value when
    // the server capability was true and the user picked planning mode. Auto or a
    // disabled capability omits it from the request.
    ...(planningMode
      ? { planning_mode: planningMode }
      : {}),
    ...(clientSubmissionId ? {
      client_submission_id: clientSubmissionId,
      submission_kind: replaySourceTurnId ? "replay" : "start",
      submission_contract_version: referenceDocument ? "chat-references.v1" : "chat-submit.v1",
      model_control_batch: referenceDocument ? "references" : "a",
      ...(referenceDocument ? { input_document: referenceDocument } : {}),
      ...(replaySourceTurnId
        ? { replay_source_turn_id: replaySourceTurnId }
        : {}),
    } : {}),
  };
  const post = () => {
    if ((referenceDocument || revisionTarget) && !canSendReference?.())
      throw Object.assign(new Error("reference send scope changed"), { code: "client_refresh_required" });
    // An @ send keeps the single-attempt rule but, like an ordinary send, has no client deadline: the server prepares
    // it (exact versions, materials, possibly a model call to pick the receiver) before accepting, and 116 measured
    // 16s against the 15s bound it used to inherit.
    const fetchMutation = referenceDocument || revisionTarget || frozenWireSnapshot?.contextMaintenance
      ? (input: RequestInfo | URL, init?: RequestInit) => singleAttemptChatActionFetch(input, init, null)
      : chatAction
        ? singleAttemptChatActionFetch
        : apiFetch;
    return fetchMutation("/api/v1/chat/send", {
      method: "POST",
      headers: {
        "Content-Type": referenceDocument
          ? "application/vnd.bowhead.chat-references.v1+json"
          : "application/json",
        ...(referenceDocument || revisionTarget ? {} : TERMINAL_CHAT_CAPABILITY_HEADERS),
        ...viewerTimeZoneHeaders(),
      },
      body: JSON.stringify(requestBody),
    });
  };
  type StartedResponse = {
    turn_id: string;
    stream_url: string;
    session_id?: string;
    execution_generation?: number;
    terminal_protocol_version?: number;
  };
  const checkReferenceStarted = (value: StartedResponse): StartedResponse => {
    if (frozenWireSnapshot?.contextMaintenance) {
      if ((value as { status?: unknown } | null)?.status !== "started")
        throw new Error("Context maintenance response is not admitted");
      requireMaintenanceReceipt(value, frozenWireSnapshot.contextMaintenance, sessionId!);
      return value;
    }
    if (!referenceDocument && !revisionTarget) return value;
    if (revisionTarget && !sameDocumentRevisionTarget(revisionTarget, (value as { document_revision_target?: unknown })?.document_revision_target))
      throw new Error("Document revision receipt target mismatch");
    const result = value as StartedResponse & { status?: unknown };
    const identity = (item: unknown): item is string => typeof item === "string"
      && item.length > 0 && item.length <= 128 && /^[A-Za-z0-9_.:-]+$/.test(item);
    if (!result || result.status !== "started" || !identity(result.turn_id)
      || !identity(result.session_id) || (sessionId !== null && result.session_id !== sessionId)
      || result.execution_generation !== 1 || result.terminal_protocol_version != null
      || typeof result.stream_url !== "string" || result.stream_url.length > 2048)
      throw new Error("reference submission response invalid");
    const stream = new URL(result.stream_url, window.location.href);
    if (!stream.pathname.endsWith(`/api/v1/chat/stream/${result.turn_id}`))
      throw new Error("reference submission stream mismatch");
    return result;
  };
  const readStartedReceipt = async (): Promise<StartedResponse | null> => {
    if (!clientSubmissionId) return null;
    const query = new URLSearchParams({ workspace_id: workspaceId });
    if (sessionId) query.set("requested_session_id", sessionId);
    const receiptResponse = await apiFetch(
      `/api/v1/chat/submissions/${encodeURIComponent(clientSubmissionId)}?${query.toString()}`,
      { cache: "no-store" },
    );
    if (receiptResponse.ok) {
      const receipt = await receiptResponse.json() as {
        outcome: string;
        response?: StartedResponse;
      };
      if (receipt.outcome === "started" && receipt.response) return checkReferenceStarted(receipt.response);
      return null;
    }
    if (receiptResponse.status === 404) return null;
    throw await readApiError(receiptResponse);
  };
  let resp: Response;
  try {
    resp = await post();
  } catch (firstError) {
    if (!clientSubmissionId) throw firstError;
    try {
      const receipt = await readStartedReceipt();
      if (receipt) return receipt;
    } catch (recoveryError) {
      throw recoveryError instanceof Error ? recoveryError : firstError;
    }
    // An absent receipt is not proof that admission did not commit.  Keep the
    // journal for explicit recovery instead of issuing another POST inside the
    // same user action; this prevents an uncertain v1 projection from creating
    // a second Session.
    throw firstError;
  }
  if (clientSubmissionId && !resp.ok) {
    const retryableServerFailure = resp.status >= 500;
    const pending = resp.status === 409
      && (await readApiError(resp.clone())).code === "submission_pending";
    if (retryableServerFailure || pending) {
      const receipt = await readStartedReceipt();
      if (receipt) return receipt;
    }
  }
  if (!resp.ok) {
    const error = await readApiError(resp);
    // licensing §13.2 — surface only known entitlement codes to the global
    // read-only banner. Credit and ordinary authz codes are no-ops there.
    applyEntitlementDenial(error.code);
    throw error;
  }
  // §3.7 WARN transport — the grace/degraded health rides the POST 200 header.
  applyEntitlementHeaders(resp);
  try {
    const started = await resp.json() as StartedResponse;
    if (chatAction && !isComposerChatActionResponse(started, chatAction)) {
      throw new Error("composer chat action response is invalid");
    }
    return checkReferenceStarted(started);
  } catch (decodeError) {
    if (!clientSubmissionId) throw decodeError;
    const receipt = await readStartedReceipt();
    if (receipt) return receipt;
    throw decodeError;
  }
}

function useSSERunner(
  message: string | null,
  workspaceId: string,
  memberUid: string,
  sendTick: number,
  activeSessionId: string | null,
  resetSignal: number,
  hydrate: UseTurnRunnerArgs["hydrate"],
  hydrateTick: number,
  archivedTurnsPatch: ArchivedTurnsHydrationPatch | null,
  currentTurnHydrationPatch: CurrentTurnHydrationPatch | null,
  attachments: AttachmentRef[] | undefined,
  planningCapability: boolean,
  submissionProtocolEnabled: boolean,
  submissionRecoveryScope: SubmissionRecoveryScope | null,
  submissionWireSnapshot: ComposerSubmissionWireSnapshot | null,
  referenceInputDocument: InputDocument | null,
  referenceSourceDraftRevision: string | null,
  referenceSubmissionEnabled: boolean,
  replaySourceTurnId: string | null,
  chatAction: ComposerChatAction | null,
  draftChatBinding: UseTurnRunnerArgs["draftChatBinding"],
  chatActionWorkspaceId: string | null,
  chatActionScopeKey: string | null,
  currentChatScopeKey: string,
  sendAttemptKey: string | null,
  onChatActionUnknown: ((
    action: ComposerChatAction,
  ) => void) | undefined,
  onChatActionSettled: ((
    actionId: string,
    disposition: "adopted" | "rejected",
  ) => (
    "settled" | "deferred" | "superseded"
  ) | Promise<"settled" | "deferred" | "superseded">) | undefined,
  onSubmissionPending: ((
    attemptKey: string,
    scopeKey: string,
    pending: RecoveredSubmission,
  ) => void) | undefined,
  onReferenceSubmissionAccepted: UseTurnRunnerArgs["onReferenceSubmissionAccepted"],
  onDocumentRevisionSubmissionAccepted: UseTurnRunnerArgs["onDocumentRevisionSubmissionAccepted"],
  onSubmissionCleared: ((
    attemptKey: string,
    scopeKey: string,
    clientSubmissionId: string,
  ) => void) | undefined,
  onSendInitSettled: UseTurnRunnerArgs["onSendInitSettled"],
  onWorkspaceSettingsTurnAccepted: UseTurnRunnerArgs["onWorkspaceSettingsTurnAccepted"],
  onWorkspaceSettingsAccessDenied: UseTurnRunnerArgs["onWorkspaceSettingsAccessDenied"],
): TurnRunnerState {
  const [state, setState] = useState<TurnRunnerState>(INITIAL_STATE);
  const pendingSubmissionRef = useRef<{
    semanticKey: string;
    clientSubmissionId: string;
  } | null>(null);
  const activeSendAttemptRef = useRef<{
    attemptKey: string;
    scopeKey: string;
  } | null>(null);
  const currentChatScopeKeyRef = useRef(currentChatScopeKey);
  currentChatScopeKeyRef.current = currentChatScopeKey;
  const referenceSubmissionEnabledRef = useRef(referenceSubmissionEnabled);
  referenceSubmissionEnabledRef.current = referenceSubmissionEnabled;
  const referenceScopeSignature = composerDraftScopeSignature(submissionRecoveryScope);
  const referenceScopeSignatureRef = useRef(referenceScopeSignature);
  referenceScopeSignatureRef.current = referenceScopeSignature;
  const runnerMountedRef = useRef(false);
  const esRef = useRef<EventSource | null>(null);
  // stream-liveness ①: this turn's transport measurement sample, rebuilt whenever attachStream attaches;
  // consumers (troubleshooting diagnostics / current-row copy) read as needed. Use a ref, not state: it changes on every event,
  // and state would rerender the entire turn subtree every frame, while reading surfaces already have second-level tickers.
  const transportRef = useRef<TransportSample | null>(null);
  // R3: retain watchdog ref; consistently clear it on useEffect cleanup / cancel / hydrate paths.
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearInterval(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);
  useEffect(() => {
    runnerMountedRef.current = true;
    return () => {
      runnerMountedRef.current = false;
      esRef.current?.close();
      esRef.current = null;
      clearWatchdog();
    };
  }, [clearWatchdog]);

  // 0.26.3: exposed stable reattach callback. HITLPickerCard calls this
  // via runner.attachStream(stream_url) after /resume_turn so the FE reconnects
  // to the SAME turn_id's SSE stream and consumes the resumed Lead pass
  // (turn_started seq 12 / final_answer / done). Without this, prod dogfood
  // showed turns stuck at "已 confirm · 续 turn 中" forever — backend ran the
  // resume + emitted seq 12-19 + persisted final_answer, but FE had no event
  // pipe to display them. Refs (esRef/watchdogRef) captured by closure; stable
  // identity via empty deps so HITLPickerCard's useEffect doesn't churn.
  const attachStreamCallback = useCallback((
    streamUrl: string,
    executionGeneration?: number,
    terminalProtocolVersion?: number,
  ) => {
    esRef.current?.close();
    esRef.current = null;
    clearWatchdog();
    // Sprint phase-plan-removal post-dogfood r2 (2026-05-19) — clear stale
    // clarificationPending on resume. Without this, synthesise-v3-events keeps
    // re-emitting clarification_required to the reducer, TurnContainer early-returns
    // the ClarificationCard, and the resumed Lead pass + final.payload never
    // render unless that stale pending state is cleared.
    // ★ chat-ux-2026-08 root fix: **advance runner turnId to the stream's turn when attaching**.
    //
    // Previously this path **never updated** turnId: turn_started did not set it (only
    // sessionTitle/user/startedAt), and streamTurnId computed from the stream URL only fed
    // canvas reconciliation. After approving a plan and attaching to its execution stream, runner still pointed at **the prior turn**.
    //
    // Observed on 116 prod 1.0.79 (read-only sampling every 300ms):
    //   303ms   1 card  turn blocks A0,A1
    //   11703ms 2 cards turn blocks A0,A1,**L1**   ← only __open__ had occurred
    //   12039ms turn_started arrived (336ms later)
    // Thus the second plan card rendered **before any execution-turn event arrived**; events did not cause it:
    // setState({...s, running:true}) here creates a live turn block, which reads beats with
    // useTurn(runner.turnId); turnId still refers to **the proposal turn**, rendering its
    // plan beat again. Refresh is required: without refresh the proposal is already live, avoiding
    // separate archived-proposal and live-block-pointing-at-it copies.
    //
    // The harm exceeds one extra card: **the entire live block attaches to the previous turn during execution** (step ledger and
    // beats all misaligned); the plan card is simply most visible.
    //
    // Both callers are safe: clarification continuation passes the same turnId (idempotent write);
    // plan approval passes the execution turn, exactly the intended advancement. URL shape:
    // `/api/v1/chat/stream/<turnId>` (possibly with query).
    const attachTurnId = streamUrl.split("?")[0].split("/").filter(Boolean).pop() ?? "";
    setState((s) => {
      // ★ Self-inflicted issue caught on 116 dogfood: advancing turnId **must first archive the displaced turn**.
      // Initial implementation manually wrote `{ turnId, user: "", answer: "", traceSteps: [], chips: [] }`
      // to clear directly; when the previous turn was live (as with a plan proposal after F5),
      // its entire content **vanished**, recoverable only by another F5 fetching server data. Observed:
      //   Before approval: L1 (live proposal with card) → after: A0 (only execution turn remains, proposal gone)
      //   → After F5 it returns to A1,A0. **Another live≠replay discrepancy**.
      // The repository already has the canonical archiveAndFreshTurn implementation, whose comment describes this exact pitfall:
      // continuation overwrites content from scrollback until F5. Use it rather than writing another reset.
      const base = attachTurnId && attachTurnId !== s.turnId
        ? archiveAndFreshTurn(s, {
            user: null,
            turnId: attachTurnId,
            executionGeneration,
            terminalProtocolVersion,
          })
        : s;
      const generationChanged = executionGeneration != null
        && executionGeneration !== base.executionGeneration;
      return {
      ...base,
      running: true,
      cancelled: false,
      terminalStatus: null,
      // Same-turn clarification resume starts a new execution generation.
      // The paused generation's summary/duration must not leak into it.
      summary: null,
      startedAt: Date.now(),
      clarificationPending: null,
      errorReason: base.errorReason?.source === "clarification_resume" ? null : base.errorReason,
      executionGeneration: executionGeneration ?? base.executionGeneration,
      terminalProtocolVersion:
        terminalProtocolVersion ?? base.terminalProtocolVersion,
      effectDisclosure: generationChanged ? null : base.effectDisclosure,
      effectDisclosureDiscoveryPending: generationChanged
        ? terminalProtocolVersion === 1
        : base.effectDisclosureDiscoveryPending,
      };
    });
    attachStream(
      streamUrl,
      esRef,
      watchdogRef,
      setState,
      transportRef,
      true,   // attachStreamCallback = HITL resume / external stream reattachment ⇒ mid-turn attachment
      terminalProtocolVersion,
    );
  }, [clearWatchdog]);

  const maintenanceStateRef = useRef(state);
  maintenanceStateRef.current = state;
  const attachContextMaintenance = useCallback((receipt: ContextMaintenanceReceipt, existingOnly = false): boolean => {
    const current = maintenanceStateRef.current;
    if (current.sessionId === receipt.session_id && current.turnId === receipt.turn_id
      && current.operationKind === "context_maintenance" && current.executionGeneration === receipt.execution_generation)
      return true;
    if (existingOnly) return false;
    if ((current.sessionId && current.sessionId !== receipt.session_id) || current.clarificationPending
      || (current.running && current.turnId !== receipt.turn_id)) return false;
    esRef.current?.close();
    esRef.current = null;
    clearWatchdog();
    setState((previous) => ({
      ...(previous.turnId === receipt.turn_id ? previous : archiveAndFreshTurn(previous, { user: null, turnId: receipt.turn_id, userAttachments: [] })),
      sessionId: receipt.session_id, operationKind: "context_maintenance", running: true,
      executionGeneration: receipt.execution_generation, terminalProtocolVersion: receipt.terminal_protocol_version ?? null,
    }));
    attachStream(receipt.stream_url, esRef, watchdogRef, setState, transportRef, true, receipt.terminal_protocol_version ?? undefined);
    return true;
  }, [clearWatchdog]);

  // dogfood-0708 F3: dismissing「换个问法」clears clarificationPending, collapsing the card.
  // resolve clears pending through attachStreamCallback; dismiss has no resume/attachStream,
  // previously only tombstoning + no-op setState (setAnsweredSummary(null)/setResolving(false) are already
  // null/false while pending) → clarification useMemo for card rendering (deps exclude the nonreactive
  // tombstone) never recomputes → card stays open (confirmed on dogfood: /dismiss 200 but card still clickable).
  // Clearing pending changes memo dependencies and collapses, symmetrical with resolve. reqId guard clears only the matching card,
  // protecting a next card that arrives immediately afterward.
  const clearClarificationPending = useCallback((reqId?: string | null) => {
    setState((s) => (
      s.clarificationPending
      && (!reqId || s.clarificationPending.clarificationId === reqId)
        ? { ...s, clarificationPending: null }
        : s
    ));
  }, []);

  // A durable inverse event can be published on a different Gateway than the
  // browser's current EventSource. While a card or settled question pause is
  // visible, sparsely re-read its authority so that connection affinity
  // cannot leave an already-invalidated card actionable for the rest of the tab.
  const visibleClarificationId = state.clarificationPending?.clarificationId;
  const visibleClarificationTurnId = state.turnId;
  const awaitingClarificationResume = !visibleClarificationId && !state.running && state.terminalStatus === "clarification_pending";
  useEffect(() => {
    if ((!visibleClarificationId && !awaitingClarificationResume) || !visibleClarificationTurnId) return;
    let disposed = false;
    let inFlight: AbortController | null = null;

    const revalidate = async (): Promise<void> => {
      if (
        disposed
        || inFlight !== null
        || document.visibilityState !== "visible"
        || navigator.onLine === false
      ) return;
      const controller = new AbortController();
      inFlight = controller;
      try {
        const pending = await listClarificationsForTurn(
          visibleClarificationTurnId,
          { signal: controller.signal },
        );
        if (
          disposed
          || pending.items.some(
            (item) => item.request_id === visibleClarificationId,
          )
        ) return;
        if (visibleClarificationId) {
          markClarificationAnswered(visibleClarificationId, { values: [], dismissed: true });
        }
        setState((current) => {
          if (current.turnId !== visibleClarificationTurnId) return current;
          const pendingCard = current.clarificationPending;
          const resumeFailure = current.terminalStatus == null || current.terminalStatus === "clarification_pending"
            ? clarificationResumeFailure(pending.resume_failure, visibleClarificationTurnId, current.executionGeneration)
            : null;
          const clarifications = current.clarifications.filter(
            (item) => item.request_id !== visibleClarificationId,
          );
          if (
            pendingCard?.clarificationId !== visibleClarificationId
            && clarifications.length === current.clarifications.length
            && (!resumeFailure || current.errorReason?.source === "clarification_resume")
          ) return current;
          return {
            ...current,
            errorReason: current.errorReason ?? resumeFailure,
            clarificationPending:
              pendingCard?.clarificationId === visibleClarificationId
                ? null
                : pendingCard,
            clarifications,
          };
        });
      } catch (error) {
        if (!controller.signal.aborted) {
          const status = (error as { status?: unknown })?.status;
          const code = (error as { code?: unknown })?.code;
          console.warn("clarification live authority probe deferred", {
            turnId: visibleClarificationTurnId,
            status: typeof status === "number" ? status : undefined,
            code: typeof code === "string" ? code : undefined,
          });
        }
      } finally {
        if (inFlight === controller) inFlight = null;
      }
    };

    void revalidate();
    const interval = window.setInterval(() => void revalidate(), 30_000);
    const onVisible = (): void => {
      if (document.visibilityState === "visible") void revalidate();
    };
    const onFocus = (): void => void revalidate();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onFocus);
    return () => {
      disposed = true;
      inFlight?.abort();
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onFocus);
    };
  }, [visibleClarificationId, visibleClarificationTurnId, awaitingClarificationResume]);

  const previousResetSignalRef = useRef(resetSignal);
  // Clear canvas only when resetSignal changes (user clicks "新建"); initial effect/StrictMode
  // replay is not a reset and must not revoke the same attempt's single-flight owner.
  useEffect(() => {
    if (previousResetSignalRef.current === resetSignal) return;
    previousResetSignalRef.current = resetSignal;
    esRef.current?.close();
    esRef.current = null;
    clearWatchdog();
    activeSendAttemptRef.current = null;
    resetCanvas();
    setState(INITIAL_STATE);
  }, [clearWatchdog, resetSignal]);

  // Clicking a historical session loads the full conversation into messages; runner no longer displays only one turn
  useEffect(() => {
    if (!hydrate) return;
    esRef.current?.close();
    esRef.current = null;
    clearWatchdog();
    const resume = hydrate.resumeTurn;
    if (resume) {
      // Last turn still running: user-bubble placeholder + open SSE stream; server first
      // replays persisted events, then attaches live events, fully returning to the ongoing work.
      setState({
        ...INITIAL_STATE,
        sessionId: hydrate.sessionId,
        sessionTitle: hydrate.sessionTitle,
        messages: hydrate.messages,
        // prod-bug-bash-038 #9: populate archivedTurns from session hydrate so
        // prior completed turns render via ArchivedTurnView (the canonical
        // path). Without this, runner.archivedTurns is empty on F5/session
        // reload and prior Q&A disappears (messages[] is workspace-context
        // fallback only, NOT visible chat history per TurnContainer:354).
        archivedTurns: hydrate.archivedTurns ?? [],
        intent: hydrate.lastIntent ?? null,
        user: resume.userText,
        userAttachments: resume.userAttachments ?? [],
        turnId: resume.turnId,
        operationKind: hydrate.lastOperationKind ?? null,
        executionGeneration: resume.executionGeneration ?? 1,
        terminalProtocolVersion:
          hydrate.lastTerminalProtocolVersion ?? null,
        effectDisclosure: hydrate.lastEffectDisclosure ?? null,
        effectDisclosureDiscoveryPending:
          hydrate.lastEffectDisclosureDiscoveryPending ?? false,
        running: true,
        terminalStatus: null,
        started: true,
      });
      clearPyCells();
      attachStream(
        `/api/v1/chat/stream/${resume.turnId}`,
        esRef,
        watchdogRef,
        setState,
        transportRef,
        true,   // Hydrating into an already-running turn ⇒ mid-turn attachment
        hydrate.lastTerminalProtocolVersion ?? undefined,
      );
    } else {
      // chat-hydrate-overhaul: restore last turn's trace + summary + turnId so IntentStrip
      // shows done state and step_card's collapsed area can expand trace, avoiding a stuck planning… status.
      // chat-v4 hydrate: when finalPayload was backfilled, TurnContainer owns the
      // last-turn render and `messages` excludes it — seed runner.user from
      // hydrate.lastUserText so the user bubble still renders.
      const hasRoutedLastTurn = Boolean(
        hydrate.lastTurnRouted
        || hydrate.lastOperationKind === "context_maintenance"
        || hydrate.lastFinalPayload !== undefined
        || hydrate.lastErrorReason,
      );
      const clarificationPending = resolveHydratedClarificationPending(
        hydrate.lastClarifications,
      );
      setState({
        ...INITIAL_STATE,
        sessionId: hydrate.sessionId,
        sessionTitle: hydrate.sessionTitle,
        messages: hydrate.messages,
        // prod-bug-bash-038 #9: populate archivedTurns from session hydrate so
        // prior completed turns render via ArchivedTurnView (the canonical
        // path). Without this, runner.archivedTurns is empty on F5/session
        // reload and prior Q&A disappears (messages[] is workspace-context
        // fallback only, NOT visible chat history per TurnContainer:354).
        archivedTurns: hydrate.archivedTurns ?? [],
        intent: hydrate.lastIntent ?? null,
        // chat-polish-r3 Phase 1.5: lastTraceSteps retired. Hydrate
        // populates lib/turn-store via lib/turn-store-replay so step
        // rendering reads getTurnSnapshot(turnId).steps directly.
        traceSteps: [],
        summary: hydrate.lastSummary ?? null,
        turnId: hydrate.lastTurnId ?? null,
        operationKind: hydrate.lastOperationKind ?? null,
        startedAt: finiteMsOrNull(hydrate.lastStartedAt),
        user: hasRoutedLastTurn ? (hydrate.lastUserText ?? "") : "",
        userAttachments: hasRoutedLastTurn ? (hydrate.lastUserAttachments ?? []) : [],
        // chat-redesign Phase 1.2 CTO 2nd review P0 #1 — seed runner.answer
        // from the persisted final_answer so ChatBubbleNew renders the prose
        // on session reload. Without this, the V4-owned last turn previously
        // had its answer rendered by ChatBubbleNew (now deleted) and the new
        // surface would show user bubble + step list with no bot reply.
        answer: hasRoutedLastTurn ? (hydrate.lastAnswer ?? "") : "",
        // Sprint phase-plan-removal: hydrate of v4TurnPlan/v4PhaseUpdates deleted.
        subagentSummaries: hydrate.lastSubagentSummaries,
        finalPayload: hydrate.lastFinalPayload,
        responseKind: hydrate.lastResponseKind ?? undefined,
        toolInvocationCount: hydrate.lastToolInvocationCount,
        reportSectionsSaved: hydrate.lastReportSectionsSaved ?? null,
        terminalStatus: hydrate.lastTerminalStatus ?? null,
        executionGeneration: hydrate.lastExecutionGeneration ?? null,
        terminalProtocolVersion: hydrate.lastTerminalProtocolVersion ?? null,
        effectDisclosure: hydrate.lastEffectDisclosure ?? null,
        effectDisclosureDiscoveryPending:
          hydrate.lastEffectDisclosureDiscoveryPending ?? false,
        errorReason: hydrate.lastErrorReason ?? null,
        clarificationPending,
        headline: hydrate.lastHeadline,
        citations: hydrate.lastCitations ?? [],
        running: false,
        started: hydrate.messages.length > 0 || hasRoutedLastTurn || Boolean(hydrate.reportOnly),
        reportOnly: Boolean(hydrate.reportOnly),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrateTick]);

  // Historical replay is deliberately independent from session hydrate. A
  // late patch must not close/re-attach the current SSE or reset a follow-up
  // Turn the user started while old event logs were still loading.
  useEffect(() => {
    if (!archivedTurnsPatch) return;
    setState((current) => current.sessionId === archivedTurnsPatch.sessionId
      ? {
          ...current,
          archivedTurns: mergeArchivedTurnsHydration(
            current.archivedTurns ?? [],
            archivedTurnsPatch.turns,
          ),
        }
      : current);
  }, [archivedTurnsPatch]);

  // The persisted user/answer baseline is intentionally committed before the
  // last Turn's event log. Enrichment may arrive later, but it must never reset
  // a resumed stream, a follow-up the user already started, or another session.
  useEffect(() => {
    if (!currentTurnHydrationPatch) return;
    setState((current) => mergeCurrentTurnHydration(
      current,
      currentTurnHydrationPatch,
    ));
  }, [currentTurnHydrationPatch]);

  useEffect(() => {
    if (!message && !(attachments && attachments.length > 0)) return;
    const currentAttemptKey = sendAttemptKey
      ?? chatAction?.actionId
      ?? `legacy_${sendTick}`;
    if (activeSendAttemptRef.current?.attemptKey === currentAttemptKey) return;
    const attemptScopeKey = chatActionScopeKey ?? currentChatScopeKey;
    activeSendAttemptRef.current = {
      attemptKey: currentAttemptKey,
      scopeKey: attemptScopeKey,
    };
    const ownsPresentation = () => (
      runnerMountedRef.current
      && activeSendAttemptRef.current?.attemptKey === currentAttemptKey
      && currentChatScopeKeyRef.current === attemptScopeKey
    );
    esRef.current?.close();
    // fix/sse-reconnect-resilience: null esRef (matching all four teardown sites: cancel/hydrate/unmount/resume).
    // Otherwise, during postSendMessage's await window, the previous turn's reopenTimer fires while
    // isCurrent() (esRef.current===currentEs) still holds, reviving the old connection;
    // its full replay pollutes the new turn's reducer state. See supervisor isCurrent() comment.
    esRef.current = null;
    clearWatchdog();

    // attachments already include complete metadata collected by composer; store directly in userAttachments.
    let attachmentRefs: AttachmentRef[] = attachments ?? [];

    (async () => {
      let activeSubmissionId: string | null = null;
      let activeRecoveryScope = submissionRecoveryScope;
      let referenceDocument: InputDocument | null = null;
      try {
        // Sprint file-upload-presign Phase 3: a bundle AttachmentRef expands
        // into its children's artifact_ids; the opaque zip artifact has no
        // useful content for the orchestrator. When a chip has neither (legacy
        // single-file shape), use its fileId.
        const fileIds: string[] = [];
        for (const a of attachments ?? []) {
          if (a.bundleArtifactIds && a.bundleArtifactIds.length > 0) {
            fileIds.push(...a.bundleArtifactIds);
          } else if (a.fileId) {
            fileIds.push(a.fileId);
          }
        }
        const actionWorkspaceId = chatAction
          ? (chatActionWorkspaceId ?? workspaceId)
          : workspaceId;
        const actionSessionId = chatAction
          ? chatAction.sessionId
          : activeSessionId;
        let wireSnapshot = submissionWireSnapshot
          ?? captureComposerSubmissionWireSnapshot(
            actionSessionId,
            planningCapability,
            draftChatBinding,
          );
        if (wireSnapshot.coachCanvasBinding && (chatAction || replaySourceTurnId || referenceInputDocument
          || wireSnapshot.draftChatBinding || wireSnapshot.workspaceSettingsBinding || wireSnapshot.planningMode === "planning"
          || wireSnapshot.coachCanvasBinding.dirty || wireSnapshot.coachCanvasBinding.pending_write))
          throw Object.assign(new Error("Original resource target is not ready"), { code: "coach_canvas_target_unavailable" });
        if (wireSnapshot.workspaceSettingsBinding && (chatAction || replaySourceTurnId
          || referenceInputDocument || wireSnapshot.draftChatBinding || wireSnapshot.planningMode === "planning"))
          throw Object.assign(new Error("settings target entry unavailable"), { code: "configuration_target_unavailable" });
        if (referenceInputDocument !== null) {
          if (!referenceSubmissionEnabled || !submissionProtocolEnabled || chatAction || replaySourceTurnId)
            throw Object.assign(new Error("reference entry unavailable"), { code: "unsupported_entry" });
          activeRecoveryScope = submissionRecoveryScope ? { ...submissionRecoveryScope } : null;
          if (!completeComposerScope(activeRecoveryScope)
            || activeRecoveryScope.workspaceId !== actionWorkspaceId
            || activeRecoveryScope.requestedSessionId !== actionSessionId
            || !attemptScopeKey || !sendAttemptKey || !ownsPresentation())
            throw Object.assign(new Error("reference scope unavailable"), { code: "client_refresh_required" });
          const captured = captureReferenceSubmissionDraft(activeRecoveryScope, {
            schemaVersion: 2,
            input_document: referenceInputDocument,
            message: message ?? "",
            attachments: attachmentRefs,
            sourceDraftRevision: referenceSourceDraftRevision,
            wireSnapshot,
          });
          referenceDocument = captured.input_document;
          attachmentRefs = captured.attachments;
          wireSnapshot = captured.wireSnapshot;
        }
        const revisionTarget = wireSnapshot.documentRevisionTarget;
        if (revisionTarget) {
          activeRecoveryScope = submissionRecoveryScope ? { ...submissionRecoveryScope } : null;
          if (chatAction || replaySourceTurnId || referenceDocument || !completeComposerScope(activeRecoveryScope) ||
              activeRecoveryScope.workspaceId !== actionWorkspaceId || activeRecoveryScope.requestedSessionId !== actionSessionId ||
              !attemptScopeKey || !sendAttemptKey || !ownsPresentation())
            throw Object.assign(new Error("Document revision scope unavailable"), { code: "document_revision_inputs_unsupported" });
          wireSnapshot = captureDocumentRevisionSubmission(activeRecoveryScope, {
            message: message ?? "", attachments: attachmentRefs, wireSnapshot,
          }).wireSnapshot;
        }
        const semanticKey = JSON.stringify({
          workspaceId: actionWorkspaceId,
          activeSessionId: actionSessionId,
          message: message ?? "",
          attachmentRefs,
          draftChatBinding: wireSnapshot.draftChatBinding ?? null,
          workspaceSettingsBinding: wireSnapshot.workspaceSettingsBinding ?? null,
          coachCanvasBinding: wireSnapshot.coachCanvasBinding ?? null,
          ...(revisionTarget ? { documentRevisionTarget: revisionTarget, sourceDraftRevision: wireSnapshot.sourceDraftRevision } : {}),
          ...(referenceDocument ? { input_document: referenceDocument } : {}),
        });
        let clientSubmissionId: string | null = null;
        if ((submissionProtocolEnabled || revisionTarget) && !chatAction) {
          if (!activeRecoveryScope) {
            throw Object.assign(new Error("submission recovery scope unavailable"), {
              code: "submission_recovery_unavailable",
            });
          }
          const prepared = referenceDocument
            ? await preparePendingReferenceSubmission(activeRecoveryScope, {
                schemaVersion: 2,
                input_document: referenceDocument,
                message: message ?? "",
                attachments: attachmentRefs,
                sourceDraftRevision: referenceSourceDraftRevision,
                wireSnapshot,
              })
            : await preparePendingSubmission(activeRecoveryScope, {
                message: message ?? "",
                attachments: attachments ?? [],
                submissionKind: replaySourceTurnId ? "replay" : "start",
                replaySourceTurnId,
                wireSnapshot,
              });
          clientSubmissionId = prepared.clientSubmissionId;
          activeSubmissionId = clientSubmissionId;
          pendingSubmissionRef.current = { semanticKey, clientSubmissionId };
          const pending: RecoveredSubmission | RecoveredReferenceSubmission = {
            clientSubmissionId,
            contentChecksum: prepared.contentChecksum,
            createdAt: new Date().toISOString(),
            message: message ?? "",
            attachments: attachmentRefs,
            submissionKind: replaySourceTurnId ? "replay" : "start",
            replaySourceTurnId,
            wireSnapshot,
            ...(revisionTarget ? { schemaVersion: 3 as const, sourceDraftRevision: wireSnapshot.sourceDraftRevision } : {}),
            ...(referenceDocument ? {
              schemaVersion: 2 as const,
              input_document: referenceDocument,
              sourceDraftRevision: referenceSourceDraftRevision,
            } : {}),
          };
          onSubmissionPending?.(
            currentAttemptKey,
            attemptScopeKey,
            referenceDocument || revisionTarget ? structuredClone(pending) : pending,
          );
        }
        const {
          turn_id,
          stream_url,
          session_id,
          execution_generation,
          terminal_protocol_version,
        } = await postSendMessage(
          actionWorkspaceId,
          memberUid,
          message ?? "",
          actionSessionId,
          attachmentRefs,
          clientSubmissionId,
          replaySourceTurnId,
          wireSnapshot,
          chatAction,
          referenceDocument,
          () => ownsPresentation() && (Boolean(revisionTarget) || referenceSubmissionEnabledRef.current)
            && referenceScopeSignatureRef.current === composerDraftScopeSignature(activeRecoveryScope),
        );
        if (wireSnapshot.workspaceSettingsBinding)
          onWorkspaceSettingsTurnAccepted?.(turn_id, wireSnapshot.workspaceSettingsBinding);
        if (
          clientSubmissionId
          && pendingSubmissionRef.current?.clientSubmissionId === clientSubmissionId
        ) {
          pendingSubmissionRef.current = null;
        }
        if (clientSubmissionId && activeRecoveryScope) {
          let cleared = true;
          try {
            if (referenceDocument && onReferenceSubmissionAccepted) {
              cleared = await onReferenceSubmissionAccepted(currentAttemptKey, attemptScopeKey);
            }
            if (revisionTarget) {
              cleared = await onDocumentRevisionSubmissionAccepted?.(currentAttemptKey, attemptScopeKey) ?? false;
            }
            if (cleared) await clearPendingSubmission(activeRecoveryScope, clientSubmissionId);
          } catch (error) {
            cleared = false;
            console.warn("composer submission recovery cleanup deferred", error);
          }
          if ((!referenceDocument && !revisionTarget) || cleared) onSubmissionCleared?.(
            currentAttemptKey,
            attemptScopeKey,
            clientSubmissionId,
          );
          if ((referenceDocument || revisionTarget) && !cleared) {
            onSendInitSettled?.(currentAttemptKey, false);
            return;
          }
        }
        if (chatAction) {
          const settlement = await onChatActionSettled?.(
            chatAction.actionId,
            "adopted",
          );
          if (settlement === "superseded") {
            onSendInitSettled?.(currentAttemptKey, false);
            return;
          }
        }
        if (ownsPresentation()) {
          onSendInitSettled?.(currentAttemptKey, true, { turnId: turn_id, sessionId: session_id ?? actionSessionId ?? null });
        } else {
          onSendInitSettled?.(currentAttemptKey, true);
        }
        if (!ownsPresentation()) return;
        // Only move the runner into a new Turn after a definitive accepted
        // receipt. Keeping the prior surface mounted during POST preserves the
        // controller-owned draft and uploads when initialization fails.
        clearPyCells();
        setState((s) => ({
          ...archiveAndFreshTurn(s, {
            user: message,
            turnId: turn_id,
            userAttachments: attachmentRefs,
          }),
          sessionId: session_id ?? s.sessionId,
          operationKind: null,
          executionGeneration: execution_generation ?? 1,
          terminalProtocolVersion: terminal_protocol_version ?? null,
        }));
        attachStream(
          stream_url,
          esRef,
          watchdogRef,
          setState,
          transportRef,
          false,  // Stream attachment after send ⇒ fresh turn measured from the start, the only path that can issue a verdict
          terminal_protocol_version,
        );
      } catch (err) {
        if (!chatAction) {
          onSendInitSettled?.(currentAttemptKey, false);
        }
        trackComposerTransportFailure(err, {
          surface: submissionWireSnapshot?.composerSurface
            ?? (activeSessionId ? "workbench" : "home"),
          modelControlBatch: "a",
          hasAttachments: attachmentRefs.length > 0,
        });
        // A send-init failure has no turnId, so FinalFailBanner cannot render it.
        // Keep the immediate toast and a durable sendErrorReason strip. This is
        // deliberately NOT errorReason: that drives the composer replay button,
        // which would only re-send a request rejected by a preflight gate.
        const status = (err as { status?: number })?.status;
        const code = (err as { code?: string })?.code;
        if ((status === 401 || status === 403) && submissionWireSnapshot?.workspaceSettingsBinding)
          onWorkspaceSettingsAccessDenied?.(submissionWireSnapshot.workspaceSettingsBinding);
        if (chatAction) {
          if (isDefinitiveComposerChatActionRejection(err, chatAction)) {
            const settlement = await onChatActionSettled?.(
              chatAction.actionId,
              "rejected",
            );
            if (status === 401) {
              // Authentication recovery is independent from local IndexedDB
              // settlement. Never replay this POST, but do refresh credentials
              // even when the rejected marker must be cleaned up later.
              await refreshAccessToken().catch(() => undefined);
            }
            if (settlement === "superseded" || settlement === "deferred") {
              onSendInitSettled?.(currentAttemptKey, false);
              return;
            }
          } else {
            onChatActionUnknown?.(chatAction);
          }
          onSendInitSettled?.(currentAttemptKey, false);
        }
        if (code && (DEFINITIVE_LOCAL_UNLOCK_CODES.has(code) || isRejectedBeforeAdmission(err))) {
          pendingSubmissionRef.current = null;
          if (activeSubmissionId && activeRecoveryScope) {
            let cleared = true;
            await clearPendingSubmission(
              activeRecoveryScope,
              activeSubmissionId,
            ).catch((cleanupError) => {
              cleared = false;
              console.warn("composer rejected-submission cleanup deferred", cleanupError);
            });
            if ((!referenceDocument && !submissionWireSnapshot?.documentRevisionTarget) || cleared) onSubmissionCleared?.(
              currentAttemptKey,
              attemptScopeKey,
              activeSubmissionId,
            );
          }
        }
        if (!ownsPresentation()) return;
        console.error("SSE runner init failed", { status, code });
        const reasonCode = resolveChatSendErrorReason(code, status);
        if (reasonCode === "team_model_unconfigured") {
          // credential-readiness D5: branch by can_act (direct admins to configure themselves; provide members
          // a contact). Fetch role asynchronously; failure falls back to role-neutral baseline (resolver never throws).
          void resolveModelUnconfiguredPresentation().then((p) => {
            toast({ kind: "error", text: p.message });
          });
        } else {
          toast({ kind: "error", text: getChatSendErrorMessage(reasonCode) });
        }
        setState((s) => ({ ...s, running: false, sendErrorReason: reasonCode }));
      }
    })();

    return undefined;
    // Deliberately omit activeSessionId dependency: sendTick changes resend, reading current activeSessionId from ref
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [message, workspaceId, memberUid, sendTick]);

  // 0.26.3: merge stable attachStream callback into returned state so
  // HITLPickerCard / workspace can call runner.attachStream(streamUrl)
  // after /resume_turn to reconnect the SSE pipe to the same turn.
  const updateEffectDisclosure = useCallback(
    (
      turnId: string,
      executionGeneration: number,
      overlay: import("./api").EffectDisclosureOverlayDTO | null,
    ) => {
      setState((current) => mergePolledEffectDisclosure(
        current,
        turnId,
        executionGeneration,
        overlay,
      ));
    },
    [],
  );
  return {
    ...state,
    attachStream: attachStreamCallback,
    attachContextMaintenance,
    clearClarificationPending,
    updateEffectDisclosure,
    // stream-liveness ①: expose the measurement sample unchanged; **draw no conclusions here**.
    // Consumers call transport-health msSinceLastEvent / verdictOf as needed.
    transportRef,
  };
}

// stream-liveness ① (review #3): **sole removal point** for the visibility listener.
// Removing only in markTerminal is insufficient: cancel / hydrate session switch / unmount / follow-up streaming
// merely null esRef; stopped stays false on those paths, permanently retaining the listener and the entire attachStream
// closure (seenSeq / seenEventIds / every Map). Remove the previous listener **before every attachment** here,
// guaranteeing at most one globally, regardless of how paths are split.
let detachPreviousVisibilityProbe: (() => void) | null = null;

// ─── attachStream helper: two callers, after send + hydrate resume ─────
function attachStream(
  streamUrl: string,
  esRef: React.MutableRefObject<EventSource | null>,
  watchdogRef: React.MutableRefObject<ReturnType<typeof setInterval> | null>,
  setState: React.Dispatch<React.SetStateAction<TurnRunnerState>>,
  // stream-liveness ①: this turn's transport measurement. Write only; all judgment lives in lib/transport-health.ts,
  // read by consumers (current-row copy / troubleshooting diagnostics). **Make no UI decisions here**.
  transportRef: React.MutableRefObject<TransportSample | null>,
  // review #2: with **mid-turn attachment** (F5 / hydrating into a running turn / follow-up streaming),
  // sampling begins only at replay while done.duration_ms covers the **whole turn**, truncating the span
  // and falsely classifying buffered. Real median turn duration is 42s, 55% exceed 40s; refresh is routine, so this
  // is no edge case. Include this flag so the criterion abstains directly.
  attachedMidTurn: boolean,
  expectedTerminalProtocolVersion?: number,
): void {
  // EventSource bypasses fetch/history patches. Keep its URL on the current
  // browser entry, including runtime prefixes and native reconnects.
  const absUrl = streamUrl.startsWith("/") ? appPath(streamUrl) : streamUrl;
  // ── SSE resilience supervisor (fix/sse-reconnect-resilience) ──────────────────
  // Tencent CLB cuts long BI turns (5min+) every ~60–90s. Previously native
  // EventSource onerror closed immediately + marked interrupted; watchdog returned early on readyState
  // ===OPEN (half-open socket), leaving turns at「运行中」, losing intermediate output and requiring manual report refresh.
  // Now disconnection retains running, exponentially backs off and reconnects to the same turn's /chat/stream/{id} until
  // terminal state. Server already supported reconnection (replay all persisted events + synthesize done from
  // session_store status for terminal turns, chat.py:962–991); the gap was frontend never reconnecting.
  // Server fully replays on each reconnect; deduplicate with seenSeq using monotonic bus seq (events.py:177).
  const RECONNECT_BASE_MS = 1_000;
  const RECONNECT_MAX_MS = 8_000;
  // Gateway guarantees keepalive every 15s (chat.py:1052); 40s silence means dead transport, forcing a cycle even if
  // readyState still reports OPEN. (Old watchdog's early OPEN return caused the stall.)
  const STALL_RECONNECT_MS = 40_000;
  // After this long without a new event, a dropped stream asks the backend whether the turn is
  // still alive instead of guessing from the clock (agent run limits, phase 3): long agent runs
  // can be quiet for many minutes while the worker is healthy.
  const GIVE_UP_SILENCE_MS = 10 * 60_000;
  // Reconnects slow down to this once the stream has been quiet that long.
  const QUIET_RECONNECT_MAX_MS = 30_000;
  // A running turn whose worker stopped renewing its lease for this long is reported as failed.
  const OWNER_LOST_GIVE_UP_MS = 2 * 60_000;
  const turnIdMatch = /\/chat\/stream\/([^/?#]+)/.exec(streamUrl);
  const statusTurnId = turnIdMatch ? decodeURIComponent(turnIdMatch[1]) : null;
  let livenessCheck: Promise<void> | null = null;
  let ownerLostSince: number | null = null;
  // Only done/error close the transport. turn_failed/turn_aborted are semantic
  // failure facts and may be followed by the continuation barrier + done; a
  // network cut between them must still reconnect to discover the child.
  const STREAM_TERMINAL_EVENTS = new Set(["done", "error"]);
  const TURN_TERMINAL_FACT_EVENTS = new Set(["done", "error", "turn_failed", "turn_aborted"]);

  let currentEs: EventSource | null = null;
  let detachCanvas: () => void = () => {};
  let doneSeen = false;
  let stopped = false;            // Terminal / given up / replaced by new attachStream ⇒ stop reconnecting
  let protocolFailed = false;
  let backoffMs = RECONNECT_BASE_MS;
  let reopenTimer: ReturnType<typeof setTimeout> | null = null;
  // chat-polish-r3 Phase 3 (B10): any inbound event resets watchdog liveness, not only
  // explicit keepalive (Gateway token storms prevent its keepalive branch running).
  let lastKeepalive = Date.now();
  let lastNewEventAt = Date.now();   // Latest nonduplicate event drives the give-up limit
  // stream-liveness ①: this turn's transport measurement. **Record only, never judge**; criteria live in lib/transport-health.ts,
  // where consumers call verdictOf(sample, doneDurationMs) as needed.
  const transportSample = createTransportSample();
  transportSample.attachedMidTurn = attachedMidTurn;
  transportRef.current = transportSample;
  // ⚠ Background/frozen pages accumulate handlers, then fire together, **identical** to transport buffering;
  // indistinguishable afterward, so record **when it happens**, preventing backgrounding from being reported as buffering.
  const onVisibilityChange = (): void => {
    if (document.visibilityState === "hidden") transportSample.hiddenAtSomePoint = true;
  };
  detachPreviousVisibilityProbe?.();
  detachPreviousVisibilityProbe = null;
  if (typeof document !== "undefined") {
    if (document.visibilityState === "hidden") transportSample.hiddenAtSomePoint = true;
    document.addEventListener("visibilitychange", onVisibilityChange);
    detachPreviousVisibilityProbe = () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }
  const seenSeq = new Set<number>();
  const seenEventIds = new Set<string>();
  const eventIdBySeq = new Map<number, string>();
  const eventSeqById = new Map<string, number>();
  const eventWireById = new Map<string, string>();
  let highestContiguousEventSeq = 0;
  let terminalProtocolVersion: number | null = expectedTerminalProtocolVersion ?? null;
  let protocolCheckpointSeen = false;
  let protocolCheckpointHighwater = -1;
  let currentProtocolGeneration: number | null = null;
  let currentProtocolReceiptId: string | null = null;
  // Critical parent→child discovery has its own identity gate. During durable
  // owner-loss recovery an old event may have reached this browser live but be
  // fenced before persistence; a remote Gateway cannot know that hidden seq
  // floor. Never let an unrelated same-seq event suppress the follow-up link,
  // while still making duplicate replay pointers idempotent.
  const seenFollowUps = new Set<string>();
  // canvas-comprehensive Batch 1 (TD-2): canvas-event dedup cursor, same lifecycle as seenSeq
  // (per attachStream, retained across reconnects); each wire() reconnect passes it to attachCanvasSSE, whose gate drops
  // duplicate replayed canvas frames, fixing reconnect double accumulation of section_chunk (F2). Canvas events use
  // separate canvas-sse listeners, bypassing handle() seenSeq above, so need their own cursor.
  const canvasGate: CanvasSeqGate = { maxEvtSeq: 0 };

  const clearReopen = (): void => {
    if (reopenTimer) { clearTimeout(reopenTimer); reopenTimer = null; }
  };
  const localClearWatchdog = (): void => {
    if (watchdogRef.current) { clearInterval(watchdogRef.current); watchdogRef.current = null; }
  };
  // Does this supervisor still belong to this attachStream? External teardown (cancel/hydrate/unmount/
  // resume-callback) sets esRef.current=null; a new attachStream writes a new es. Either makes
  // esRef.current !== currentEs, invalidating this supervisor and preventing reconnects/cycles.
  const isCurrent = (): boolean => !stopped && esRef.current === currentEs;
  const markTerminal = (): void => {
    doneSeen = true; stopped = true; clearReopen();
    detachPreviousVisibilityProbe?.();
    detachPreviousVisibilityProbe = null;
  };
  const failProtocol = (code: string): void => {
    protocolFailed = true;
    markTerminal();
    try { currentEs?.close(); } catch { /* noop */ }
    localClearWatchdog();
    setState((s) => ({
      ...s,
      running: false,
      streamingState: "error",
      terminalStatus: "failed",
      terminalProtocolVersion: terminalProtocolVersion ?? 1,
      errorReason: {
        source: "turn_failed",
        reasonCode: terminalReasonCode(code),
      },
    }));
  };

  // canvas-comprehensive WP-2 (TD-5 / FR-1.1): terminal reconciliation; capture session/turn from stream.
  const streamTurnId = streamUrl.split("?")[0].split("/").filter(Boolean).pop() ?? "";
  let streamSessionId: string | null = null;
  // After terminal state, fetch by-session and converge canvas to server authority (built-in F5 healing, INV-2). Turn guard:
  // before applying response, verify no new turn's attachStream replaced this stream (esRef.current === currentEs); **do not
  // depend on store.currentTurnId**, which has gaps (RK-1). Silently retry failures once. Dynamically import fetchCanvasBySession
  // (like canvas-store.refreshCanvas, avoiding a use-turn-runner↔api cycle).
  const scheduleReconcile = (turnId: string, attempt = 0): void => {
    const sid = streamSessionId ?? getCanvasSnapshot().sessionId;
    if (!sid) return; // No session (pure chat without canvas) → no reconciliation needed
    void (async () => {
      try {
        const { fetchCanvasBySession } = await import("./api");
        const dto = await fetchCanvasBySession(sid);
        if (esRef.current !== currentEs) return; // New turn replaced this one → discard stale response (AC-1.1b)
        reconcileCanvasWithServer(
          { canvas: dto.canvas, sections: dto.sections ?? [], sourceRefs: dto.sourceRefs, turnCitations: dto.turnCitations },
          { turnId },
        );
        // Banner healing (FR-1.6, three chat-pipeline paths converge): reconcile already clears canvas-store's lastCanvasOpFailed
        // pill; clear runner-level errorReason here. Server sections mean
        // the report actually exists; the earlier canvas_op_failed was a false alarm/already handled by reconciliation.
        if ((dto.sections?.length ?? 0) > 0) {
          setState((s) => (
            s.errorReason?.source === "canvas_op_failed" ? { ...s, errorReason: null } : s
          ));
        }
      } catch {
        if (attempt < 1) {
          setTimeout(() => scheduleReconcile(turnId, attempt + 1), 1_500);
        } else {
          // eslint-disable-next-line no-console
          console.warn("[canvas-reconcile] fetch failed, giving up", { turnId });
        }
      }
    })();
  };

  const handle = (name: string, data: string) => {
    // chat-polish-r3 Phase 3 (B10) — reset liveness on ANY event from the
    // gateway. See lastKeepalive declaration above for rationale.
    lastKeepalive = Date.now();
    let payload: Record<string, unknown> = {};
    try {
      payload = JSON.parse(data) as Record<string, unknown>;
    } catch {
      // ignore
    }
    // stream-liveness ①: record **server-stamped** duration in the sample for self-contained transport diagnostics.
    // ⚠ Accept only duration_ms actually sent by backend (duration, not timestamp; no clock synchronization needed).
    //   Absent backend value stays null, making criteria abstain. **Never substitute a client-computed duration**:
    //   that silently replaces server-reported elapsed time with the browser's estimate,
    //   while the criterion's entire evidential value depends on the former.
    if (name === "done" && typeof payload.duration_ms === "number") {
      transportSample.doneDurationMs = payload.duration_ms;
    }
    const payloadProtocolVersion = typeof payload.terminal_protocol_version === "number"
      ? payload.terminal_protocol_version
      : null;
    if (payloadProtocolVersion != null && payloadProtocolVersion !== 1) {
      failProtocol("terminal_protocol_future_version");
      return;
    }
    if (payloadProtocolVersion === 1) {
      if (!protocolCheckpointSeen) {
        failProtocol("terminal_protocol_malformed");
        return;
      }
      terminalProtocolVersion = 1;
    }
    const isProtocolV1 = terminalProtocolVersion === 1;
    if (isProtocolV1 && !protocolCheckpointSeen) {
      failProtocol("terminal_protocol_malformed");
      return;
    }
    if (name === "turn.follow_up") {
      const followUpTurnId = typeof payload.follow_up_turn_id === "string"
        ? payload.follow_up_turn_id
        : "";
      const followUpStreamUrl = typeof payload.follow_up_stream_url === "string"
        ? payload.follow_up_stream_url
        : "";
      if (followUpTurnId && followUpStreamUrl) {
        const followUpIdentity = `${followUpTurnId}\u0000${followUpStreamUrl}`;
        if (seenFollowUps.has(followUpIdentity)) return;
        seenFollowUps.add(followUpIdentity);
      }
    }
    // A steer continuation is announced before the parent terminal frame.
    // attachStream(follow-up) replaces esRef immediately; any later parent
    // done/error is stale and must not stop the child stream.
    if (TURN_TERMINAL_FACT_EVENTS.has(name) && esRef.current !== currentEs) return;
    // fix/sse-reconnect-resilience: server fully replays on each reconnect; deduplicate monotonic bus seq,
    // or append reducers (chips/traceSteps/narrative) count twice. seq comes from bus; emit already
    // writes it into payload (events.py) + to_sse includes it, covering live+replay. Bus seq starts at 1;
    // seq<1 = synthetic replayed done (chat.py:987, no seq) or bypass-bus event, never deduplicated:
    // always process. Retain seenSeq across reconnects of one attachStream; reset for a new attachStream (new turn/resume).
    const evtSeq = isProtocolV1
      ? (
          typeof payload.event_seq === "number"
            ? payload.event_seq
            : typeof payload.evt_seq === "number"
              ? payload.evt_seq
              : -1
        )
      : typeof payload.seq === "number"
        ? payload.seq
        : -1;
    if (isProtocolV1) {
      const eventId = typeof payload.event_id === "string"
        ? payload.event_id
        : "";
      const generation = typeof payload.generation === "number"
        ? payload.generation
        : -1;
      if (
        !eventId
        || !Number.isSafeInteger(evtSeq)
        || evtSeq < 1
        || !Number.isSafeInteger(generation)
        || generation < 1
      ) {
        failProtocol("terminal_protocol_malformed");
        return;
      }
      const priorId = eventIdBySeq.get(evtSeq);
      const priorSeq = eventSeqById.get(eventId);
      if (
        (priorId != null && priorId !== eventId)
        || (priorSeq != null && priorSeq !== evtSeq)
      ) {
        failProtocol("terminal_protocol_malformed");
        return;
      }
      if (seenEventIds.has(eventId)) {
        if (eventWireById.get(eventId) !== data) {
          failProtocol("terminal_protocol_malformed");
        }
        return;
      }
      if (evtSeq !== highestContiguousEventSeq + 1) {
        failProtocol("terminal_protocol_malformed");
        return;
      }
      seenEventIds.add(eventId);
      eventIdBySeq.set(evtSeq, eventId);
      eventSeqById.set(eventId, evtSeq);
      eventWireById.set(eventId, data);
      seenSeq.add(evtSeq);
      highestContiguousEventSeq = evtSeq;
      if (
        STREAM_TERMINAL_EVENTS.has(name)
        && typeof payload.terminal_current !== "boolean"
      ) {
        failProtocol("terminal_protocol_malformed");
        return;
      }
      if (
        STREAM_TERMINAL_EVENTS.has(name)
        && payload.terminal_current === true
        && (
          currentProtocolGeneration == null
          || generation !== currentProtocolGeneration
        )
      ) {
        failProtocol("terminal_protocol_malformed");
        return;
      }
      if (
        STREAM_TERMINAL_EVENTS.has(name)
        && payload.terminal_current === true
        && currentProtocolReceiptId != null
        && payload.terminal_receipt_id !== currentProtocolReceiptId
      ) {
        failProtocol("terminal_protocol_malformed");
        return;
      }
    } else if (evtSeq >= 1 && name !== "turn.follow_up") {
      if (seenSeq.has(evtSeq)) return;   // Duplicate replayed event → discard
      seenSeq.add(evtSeq);
    }
    lastNewEventAt = Date.now();
    backoffMs = RECONNECT_BASE_MS;        // Real progress → reset backoff
    if (
      isProtocolV1
      && STREAM_TERMINAL_EVENTS.has(name)
      && payload.terminal_current === false
    ) {
      // Prior-generation terminal truth belongs to history only. Applying it
      // to the current reducer would stop a resumed generation during F5.
      return;
    }
    // WP-2: capture session_id for terminal reconciliation (turn_started and most events include it).
    if (typeof payload.session_id === "string") streamSessionId = payload.session_id;
    if (STREAM_TERMINAL_EVENTS.has(name)) markTerminal();
    // WP-2 (FR-1.1): reconcile after done / turn_failed / turn_aborted (INV-2), excluding plain error
    // (transport/fatal error, canvas irrelevant). Prefer payload turn_id, falling back to stream URL's final segment.
    if (name === "done" || name === "turn_failed" || name === "turn_aborted") {
      scheduleReconcile(String(payload.turn_id ?? streamTurnId));
    }
    // Sprint chat-redesign-2026-05-21 Phase 1.2 — pre-dispatch step_* /
    // card_* / emoji_* / turn.follow_up frames into lib/turn-store so the
    // new Step List Card surface sees them. Runs alongside the legacy
    // setState reducer below — legacy state still flows for the dual-emit
    // window. The dispatcher returns true if it claimed the event; we
    // still fall through to setState so legacy v3/v4 surfaces keep
    // working until they're deleted in the follow-up batch.
    // P4a (chat-redesign): agent_spawned carries the peer's identity
    // (subagent_id + profile) but omits card_id — inject the turn id from the
    // stream URL so dispatchToTurnStore can route it (peerNames is keyed by
    // card_id == turn_id). agent_spawned is the universal peer-identity event
    // (every peer's runner-start, plan or non-plan); the plan path emits ONLY it
    // (no peer_dispatched). Verified live on 116.
    if (
      (name === "agent_spawned" ||
        name === "lead.subagent_summary" ||
        name === "peer_dispatch_failed" ||
        name === "approval.required" ||
        name === "approval.executed" ||
        name === "approval.rejected") &&
      typeof payload.card_id !== "string"
    ) {
      const tid = streamUrl.split("/").filter(Boolean).pop();
      if (tid) payload.card_id = tid;
    }
    if ((name === "emoji_ack" || name === "emoji_remove") && typeof payload.__turnId !== "string") {
      const tid =
        typeof payload.turn_id === "string"
          ? payload.turn_id
          : streamUrl.split("/").filter(Boolean).pop();
      if (tid) payload.__turnId = tid;
    }
    // Some turn_started frames omit turn_id. Use the subscribed stream's identity
    // before dispatching so the shared store is updated outside React's state updater.
    if (name === "turn_started" && typeof payload.turn_id !== "string" && streamTurnId) {
      payload.turn_id = streamTurnId;
    }
    dispatchToTurnStore(name, payload, "live", streamTurnId);
    terminalCanvasEffects(name, payload, streamTurnId);
    // UX-3 G2 (chat-solidify 2026-07-03): attach to steer continuation. turn.follow_up arriving
    // means backend spawned a continuation turn from steer buffer (old turn already done+archived). Previously
    // dispatcher comments claimed runner would chain subscriptions, but that implementation never existed, leaving steer
    // execution entirely silent in UI until F5 (main cause of the complaint 「steer 做得不好」). Switch runner to
    // follow-up turn and attach its stream; initially use this turn's 🤝 steer text as user-copy fallback,
    // then new stream turn_started.user_text (backend-merged text) supplies authority if s.user is empty.
    if (name === "turn.follow_up") {
      const fuId = String(payload.follow_up_turn_id ?? "");
      const fuUrl = String(payload.follow_up_stream_url ?? "");
      if (fuId && fuUrl) {
        // Continuation = new turn: archive previous (including unarchived done(failed/aborted), otherwise
        // the failed turn vanishes from scrollback until F5) + fully reset, using send's same helper.
        // Clear pyCells outside setState as send does (never carry prior-turn In[N] into continuation).
        clearPyCells();
        const followUpAttachments = Array.isArray(payload.attachments)
          ? payload.attachments.flatMap((item) => {
              const ref = canonicalizeComposerAttachmentRef(item);
              return ref ? [ref] : [];
            })
          : [];
        const followUpGeneration = typeof payload.execution_generation === "number"
          ? Math.max(1, Math.trunc(payload.execution_generation))
          : 1;
        setState((s) => {
          // Continuation user = user's interjection (🤝 steer text); helper already archived
          // previous s.user, so never reuse it as continuation user (duplicates rendering). Empty stays null, filled by continuation
          // stream turn_started.user_text (backend-merged text).
          const steerText = (s.turnId ? (getTurnSnapshot(s.turnId)?.emojiAcks ?? []) : [])
            .filter((a) => a.emoji === "🤝" && a.text)
            .map((a) => String(a.text))
            .join("；");
          return archiveAndFreshTurn(s, {
            user: steerText || null,
            turnId: fuId,
            userAttachments: followUpAttachments,
            executionGeneration: followUpGeneration,
          });
        });
        attachStream(
          fuUrl,
          esRef,
          watchdogRef,
          setState,
          transportRef,
          true,   // Follow-up streaming ⇒ new sample, but done.duration_ms belongs to another turn
          terminalProtocolVersion ?? undefined,
        );
      }
      return;
    }
    setState((s) => {
      const ns = { ...s };
      // fix/sse-reconnect-resilience: events arriving after successful reconnect clear the「重连中」banner.
      // Only frontend sets streamingState error (Gateway emits only active/paused); real stalls
      // make Gateway emit paused again, so this line never incorrectly clears them.
      if (s.streamingState === "error") ns.streamingState = "active";
      switch (name) {
        case "turn_started": {
          // 0.7.10 fix: initial sessionTitle from long input was untruncated, wrapping excessively in Topbar/sidebar
          // (after refresh, backend title is truncated user_text[:40]). Match backend behavior.
          // First line + 40-character cap, matching session_store.py.
          //
          // chat-ux-2026-08 batch E: **write only when title is empty**, matching backend policy.
          // Previously **every turn_started overwrote unconditionally**, while backend
          // session_store.py::touch_session uses title = row["title"] or title_hint
          // (first write fixes it forever) → live shows **last turn's** question, F5 shows **first turn's**,
          // giving one session two names and inconsistent Topbar/sidebar titles.
          // This violates CLAUDE.md's chat invariant: live, persisted and replayed data must agree,
          // making it a contract problem, not merely copy.
          const rawUser = (payload.user_text as string | undefined) ?? s.user ?? "";
          const firstLine = rawUser.split(/\r?\n/)[0] ?? "";
          if (!ns.sessionTitle) {
            ns.sessionTitle = firstLine.length > 40 ? firstLine.slice(0, 40) + "…" : firstLine;
          }
          ns.sessionId = (payload.session_id as string | undefined) ?? null;
          // UX-3 G2: steer continuation/mid-turn attachment can leave s.user empty; fill from authoritative backend user_text
          // (normal send already sets user on submission and is never overwritten).
          if (!s.user && typeof payload.user_text === "string" && payload.user_text) {
            ns.user = payload.user_text;
          }
          // Runner duration follows the latest execution generation. Replayed
          // same-turn resumes therefore intentionally replace the earlier
          // turn_started value; turn-store separately preserves the first
          // value for whole-turn canvas wait-band timing.
          if (typeof payload.started_at === "string") {
            const authoritativeStartedAt = Date.parse(payload.started_at);
            if (Number.isFinite(authoritativeStartedAt)) {
              ns.startedAt = authoritativeStartedAt;
            }
          }
          // V5 PRD-E2: reset inline preview state for new turn (avoid stale carry-over)
          ns.inlinePreview = null;
          break;
        }
        // chat-solidify 2026-07-03: removed case "intent_classified", with zero backend emit sites
        // (M1 fake-pipeline remnant; intent chip actually uses done.dispatch_role / hydrated lastIntent).
        case "tool_call": {
          // Sprint phase-plan-removal Phase A0: any tool_call means turn is
          // actively dispatched — flip shape gate (supersedes v4TurnPlan).
          ns.hasDispatched = true;
          const id = String(payload.call_id ?? "tc?");
          const tname = String(payload.tool_name ?? "unknown");
          const argsPreview = String(payload.arguments_preview ?? "");
          // subagent.py child tool_call payload includes subagent_id (internal UUID, not spawn call_id)
          const subagentId = payload.subagent_id ? String(payload.subagent_id) : undefined;
          // chat-style-fix Phase B3 (Phase A backend A4): tool_call now carries
          // `kind` (8-enum) + `display_category` (3-enum: read/full/mutate).
          const toolKindRaw = typeof payload.kind === "string" ? payload.kind : undefined;
          const dispCatRaw = payload.display_category;
          const toolDispCat =
            dispCatRaw === "read" || dispCatRaw === "full" || dispCatRaw === "mutate"
              ? dispCatRaw
              : undefined;
          const lbl = toolLabel(tname);
          ns.chips = [...s.chips, { id, name: lbl.name, state: "running", dur: null }];
          const curThink = s.thinking || "";
          const alreadyAttached = s.lastThinkingHintAttachedAt === curThink;
          const hint = !alreadyAttached ? thinkingHint(curThink) : "";
          if (hint) ns.lastThinkingHintAttachedAt = curThink;
          // L2 (CTO review fix): subagent_id comes from child tool_call backend payload, not
          // spawn call_id. Pure helper backfills the nearest unbound spawn step.
          const working = backfillSpawnSubagentId(s.traceSteps, tname, subagentId);
          ns.traceSteps = [
            ...working,
            {
              id: `tool_${id}`,
              title: toolActionLine(tname, argsPreview),
              kind: "running",
              input: argsPreview,
              toolName: tname,
              startedAt: Date.now(),
              ...(hint ? { thinkingHint: hint } : {}),
              // Attach only child tool_calls whose backend payload actually has subagent_id (old
              // activeSubagentId=spawn.call_id fallback was a bug and has been removed).
              ...(subagentId && tname !== "subagent.spawn" ? { subagentId } : {}),
              // chat-style-fix Phase B3: persist Phase A backend `kind` +
              // `display_category` so synthesise-v3-events can forward them
              // into the V3 reducer ToolCallV3Node (FE 3-category logic).
              ...(toolKindRaw ? { toolKind: toolKindRaw } : {}),
              ...(toolDispCat ? { toolDisplayCategory: toolDispCat } : {}),
            },
          ];
          // L1: associate plan step → mark running (helper checks status / matching)
          if (s.plan) {
            ns.plan = planStepRunning(s.plan, tname);
            // A2 telemetry: unmatched plan heuristic signals the need for calibration
            if (s.plan.status === "running" && matchPlanStepForTool(s.plan, tname) < 0) {
              track("bowhead_plan_step_mismatch_total", {
                intent: s.intent ?? "unknown", tool_name: tname,
              });
            }
          }
          break;
        }
        case "tool_result": {
          const id = String(payload.call_id ?? "tc?");
          const dur = (payload.duration_ms as number | undefined) ?? 0;
          const ok = payload.ok === true;
          const rPreview = String(payload.result_preview ?? "");
          const rMeta = (payload.result_meta as Record<string, unknown> | undefined) ?? null;
          // φ Phase C: full raw result if backend emitted result_inline (≤4KB);
          // fall back to truncated preview otherwise. Root cause of the Inspector "(no result)" issue.
          const rInline = (payload as { result_inline?: unknown }).result_inline;
          const rRaw = rInline != null
            ? (typeof rInline === "string" ? rInline : JSON.stringify(rInline, null, 2))
            : rPreview;
          const errMsg = payload.error ? String(payload.error) : null;
          const tname = String(payload.tool_name ?? "");
          // V5 PRD-E2: pull preview_payload (optional, may be null)
          const previewPayload = (payload.preview_payload as
            | import("./types").ToolResultPreviewDTO
            | null
            | undefined) ?? null;
          ns.chips = s.chips.map((c) => (c.id === id ? { ...c, state: ok ? "success" : "error", dur } : c));
          ns.traceSteps = s.traceSteps.map((st) => {
            if (st.id !== `tool_${id}`) return st;
            const summary = toolResultLine(tname, rPreview, ok, errMsg, rMeta);
            return {
              ...st,
              kind: ok ? "success" : "error",
              dur: `${dur}ms`,
              output: summary,
              // V5 PRD-E3: store raw result for ToolDetailDrawer.
              // φ Phase C: prefer full result_inline (≤4KB raw) over truncated preview.
              rawResult: rRaw,
              // chat-trace-fix (2026-05-15): preserve per-tool preview_payload on
              // the trace step. synthesise-v3-events.ts reads this to populate
              // ToolCallV3Node.preview. Was previously only stored at turn-level
              // (ns.inlinePreview, first tool only) → V3 timeline lost it for
              // every tool, falling back to tile "—".
              previewPayload: previewPayload,
            };
          });
          // V5 PRD-E2: capture first ok tool's preview_payload for inline preview
          if (ok && previewPayload && !s.inlinePreview) {
            ns.inlinePreview = previewPayload;
          }
          // L2 (CTO review fix): trace step's own subagentId replaces activeSubagentId
          // (backfilled during tool_call), so no activeSubagentId clearing is needed here.
          // L1: plan step running → done/failed (helper decides internally)
          if (s.plan) ns.plan = planStepResolved(s.plan, ok, dur, errMsg);
          break;
        }
        case "partial_answer": {
          // P4.B: backend emits {text=cumulative, delta=incremental}. Append delta; without delta (old format), overwrite.
          // dogfood s_3e854 ④: PEER prose (parent_agent_id set) is now accumulated into
          // the canonical turn-store by dispatchToTurnStore (called at the top of handle),
          // so live/same-session-archived/F5 all read one store. Here we handle ONLY the
          // Lead-scope answer (parent_agent_id null).
          const parentAgentId = (
            (payload as { parent_agent_id?: unknown }).parent_agent_id ?? null
          );
          if (typeof parentAgentId === "string" && parentAgentId) break;  // peer → turn-store
          if (payload.delta !== undefined) {
            ns.answer = s.answer + String(payload.delta);
          } else {
            ns.answer = String(payload.text ?? s.answer);
          }
          break;
        }
        case "final_answer":
          // ν2 v2 P0 fix: lead_orchestrator emits final_answer with the full
          // assistant text. Previously canvas narrative section was the only
          // surface; after ν2 v2 clears canvas sections for chat turns, we
          // need to ensure the chat bubble has the answer. Overwrite (not
          // append) since final_answer carries the complete text.
          ns.answer = String(payload.text ?? s.answer);
          ns.answerIsLegacyFailureFallback = isLegacyFailureAnswerReason(payload.reason_code);
          break;
        // chat-solidify 2026-07-03: removed case "artifact_ready", with zero backend emit sites
        // (inline artifacts use lead.final.payload / canvas.section_added).
        case "thinking":
          // P4.D: backend emits {text=cumulative, delta=incremental}, matching partial_answer path
          // Support old format emitting {preview, length}
          if (payload.delta !== undefined) {
            ns.thinking = (s.thinking || "") + String(payload.delta);
            ns.thinkingLength = ns.thinking.length;
          } else if (payload.text !== undefined) {
            ns.thinking = String(payload.text);
            ns.thinkingLength = ns.thinking.length;
          } else {
            ns.thinking = String(payload.preview ?? "");
            ns.thinkingLength = Number(payload.length ?? 0);
          }
          break;
        case "plan_preview": {
          // L1: heuristic plan list (pushed immediately after intent_classified)
          const stepsRaw = Array.isArray(payload.steps) ? payload.steps : [];
          const steps = (stepsRaw as Array<Record<string, unknown>>).map((p) => ({
            id: String(p.id ?? ""),
            verb: String(p.verb ?? ""),
            hint: String(p.hint ?? ""),
            etaMs: Number(p.eta_ms ?? 0),
            skillTag: String(p.skill_tag ?? ""),
            status: "pending" as const,
          }));
          if (steps.length > 0) {
            ns.plan = {
              steps,
              estimatedDurationMs: Number(payload.estimated_duration_ms ?? 0),
              startedAt: Date.now(),
              status: "running",
            };
          }
          break;
        }
        case "error": {
          // A wire-level error is the durable terminal for this Turn. Project
          // only the finite public reason enum; never render the backend's
          // legacy message/prose field. Without this state the UI classifies an
          // empty failed replay as a verified success merely because running
          // became false.
          const reasonCode = terminalReasonCode(
            payload.reason_code ?? payload.reason,
          );
          const failedTurnId = String(payload.turn_id ?? s.turnId ?? "");
          const prior = ns.errorReason ?? s.errorReason;
          ns.errorReason = {
            source: "turn_failed",
            reasonCode,
            diagnostics: keepFirstTerminalDiagnostics(
              prior?.source === "turn_failed"
                ? prior.diagnostics
                : undefined,
              terminalErrorDiagnostics(
                payload,
                reasonCode,
                failedTurnId,
              ),
              failedTurnId,
              reasonCode,
            ),
            recoveryAction:
              parseRecoveryAction(payload.recovery_action)
              ?? (prior?.source === "turn_failed" ? prior.recoveryAction : undefined),
          };
          clearLegacyFailureAnswer(ns, s);
          ns.running = false;
          if (s.plan && s.plan.status === "running") {
            ns.plan = planFinish(s.plan, Date.now(), "failed");
          }
          break;
        }
        case "done": {
          const durationMs = resolveLiveTurnDurationMs({
            wireDurationMs: payload.duration_ms,
            executionStartedAtMs: s.startedAt,
          });
          ns.summary = {
            total: durationMs === null ? "—" : formatTurnDuration(durationMs),
            cost: `$${(payload.cost_usd ?? 0).toString()}`,
            llm: 0,
            tools: (payload.tool_invocation_count as number | undefined) ?? 0,
            ...projectDisplayUsage(payload),
          } as TurnSummary;
          ns.running = false;
          // A running read may have returned ZERO immediately before the
          // durable writer committed its terminal effect facts. Force one
          // exact-generation read after every v1 terminal transition so that
          // the timer teardown cannot freeze that stale ZERO projection.
          const needsTerminalEffectRead = s.terminalProtocolVersion === 1;
          if (needsTerminalEffectRead) {
            ns.effectDisclosureDiscoveryPending = true;
          }
          // A failed done may be the only terminal a replay sees. Only its stable
          // reason_code/reason is accepted; legacy `error` prose is never a
          // fallback. Cancelled/aborted terminals stay outside this branch.
          {
            const priorError = ns.errorReason ?? s.errorReason;
            const prior = priorError?.source === "clarification_resume" ? null : priorError;
            const terminalStatus = String(payload.status ?? "");
            if (terminalStatus !== "clarification_pending" && priorError?.source === "clarification_resume") {
              ns.errorReason = null;
            }
            const isFailure =
              terminalStatus === "error" ||
              terminalStatus === "failed";
            if (isFailure && !prior) {
              const reasonCode = terminalReasonCode(
                payload.reason_code ?? payload.reason,
              );
              const failedTurnId = String(
                payload.turn_id ?? s.turnId ?? "",
              );
              ns.errorReason = {
                source: "turn_failed",
                reasonCode,
                diagnostics: terminalErrorDiagnostics(
                  payload,
                  reasonCode,
                  failedTurnId,
                ),
                recoveryAction: parseRecoveryAction(payload.recovery_action),
              };
            } else if (isFailure && prior) {
              const reasonCode = prior.reasonCode;
              const failedTurnId = String(
                payload.turn_id ?? s.turnId ?? "",
              );
              ns.errorReason = {
                ...prior,
                diagnostics: keepFirstTerminalDiagnostics(
                  prior.diagnostics,
                  terminalErrorDiagnostics(
                    payload,
                    reasonCode,
                    failedTurnId,
                  ),
                  failedTurnId,
                  reasonCode,
                ),
                recoveryAction:
                  prior.recoveryAction ?? parseRecoveryAction(payload.recovery_action),
              };
            }
            if (isFailure) clearLegacyFailureAnswer(ns, s);
          }
          // Planning Mode (BOWHEAD_PLANNING_MODE): the plan-proposed terminal
          // reuses status="clarification_pending" but is NOT a clarification —
          // the plan card was already delivered via the plan_proposed event
          // (→ plan beat). There is no clarification row, so surfacing the
          // clarification UI / calling resolveClarification would 404. End the
          // turn, keep clarificationPending null, leave the card in place.
          if (String(payload.error_kind ?? "") === "plan_proposed") {
            ns.clarificationPending = null;
            break;
          }
          // ν2 v2: response_kind decided post-hoc from Lead's tool pattern. If
          // chat, clear any canvas sections produced by Lead's StreamingDecomposer
          // for this turn — the chat bubble (final_answer) is the right surface.
          // Default "canvas" for old turns / replay where backend omitted the field.
          const rk = payload.response_kind;
          if (rk === "chat" || rk === "canvas") {
            ns.responseKind = rk;
          } else {
            ns.responseKind = "canvas";
          }
          // chat-v4: tool_invocation_count is used by TurnContainer to gate StepListCard
          // (no tool work = pure prose, hide plan card). Mirror on live SSE
          // path so behavior matches hydrate path.
          const tic = payload.tool_invocation_count;
          if (typeof tic === "number") ns.toolInvocationCount = tic;
          // Report sections persisted in this turn (read only this done; after F5, page.tsx reads the same field from latest persisted done)
          ns.reportSectionsSaved = reportSectionsSavedFromDone(payload);
          // ν2 v2 P1 (CTO impl-review Eng-P1): use payload.turn_id (not s.turnId)
          // to avoid race when turn N+1 starts before turn N's done event lands —
          // s.turnId may already be turn N+1's id by then.
          const doneTurnId = String(payload.turn_id ?? s.turnId ?? "");
          // canvas-comprehensive WP-3 + review-fix (P0): done path is **reveal-only**.
          // response_kind **leaves** visibility decisions (previously drove clear+reveal). Sole keep-or-hide
          // authority = backend D6 canvas.sections_hidden (chat-mode sections removed from the array before done,
          // making reveal a no-op without flashing) + terminal-reconciliation fallback (if sections_hidden is lost, fetch
          // by-session, already excluding hidden sections, and converge by removing them).
          // ⚠ Never decide clear/reveal by remaining buffer content (three-perspective review P0): canvas.done precedes
          // done; revealing there empties the buffer, falsely indicating no content here and clearing just-revealed report sections.
          // Reveal-only is idempotent and safe when React updaters run twice (no toggling branch remains).
          // The reveal itself runs outside this updater (`terminalCanvasEffects`).
          // Sprint θ Phase 8d: capture dispatch_role for chip rendering, fall back to
          // legacy meta_intent for replay backward compat (per intent-mapping.ts).
          if (payload.dispatch_role !== undefined) {
            ns.dispatchRole = String(payload.dispatch_role);
          } else if (payload.meta_intent !== undefined) {
            ns.dispatchRole = String(payload.meta_intent);
          }
          // Honest planning fall-through: user explicitly chose planning mode but no plan card resulted. Backend
          // supplies only reason codes (authoring_empty / reify_failed / unknown_agents); copy uses
          // bilingual i18n. Key absent by default; unaffected turns remain byte-identical.
          if (typeof payload.planning_fallback === "string" && payload.planning_fallback) {
            ns.planningFallback = payload.planning_fallback;
          }
          if (s.plan && s.plan.status === "running") {
            ns.plan = planFinish(s.plan, Date.now(), "done");
          }
          // Sprint θ Phase 8d: branch on status. Only "completed" archives + clears trace.
          // For clarification_pending / aborted / error, preserve trace + don't archive
          // (terminal cards in turn-stream need current state to render).
          const doneStatus = String(payload.status ?? "completed");
          ns.terminalStatus = doneStatus;
          if (doneStatus === "completed") {
            // B7 (W13 #10 fix): collapse trace live on turn completion; append current turn's user/answer
            // to messages history + clear traceSteps + clearPyCells, matching refreshed UI behavior.
            archiveCompletedTurnInPlace(
              needsTerminalEffectRead
                ? { ...s, effectDisclosureDiscoveryPending: true }
                : s,
              ns,
              doneStatus,
            );
          }
          // P2 #19: retain pending clarifications after turn end for later answers; ClarificationCard itself
          // hides locally after user action (P0 #1). Do not force clearing here; let users decide.
          // 116-hitl-resume fix (fallback): completed turn makes turn-terminating
          // clarification (clarificationPending, distinct from the legacy list above) meaningless:
          // resume pass finished. Mirror hydration's turnCompleted-drop
          // (app/page.tsx:228–234), preventing permanently mounted cards after failed/lost clarification.resolved emission.
          // Clear only this turn's card (retain mismatched turn_id, matching L1052 stale-guard).
          if (
            doneStatus === "completed"
            && s.clarificationPending
            && (!doneTurnId || !s.turnId || doneTurnId === s.turnId)
          ) {
            ns.clarificationPending = null;
          }
          break;
        }
        case "clarification_required":
        case "clarification.required": {
          // Sprint θ Phase 8d (P0 #3 discriminator): branch by clarification_id field presence.
          // - Has clarification_id → Sprint θ turn-terminating clarification
          //   (set clarificationPending state; turn ends, no partial_answer/final_answer follows)
          // - No clarification_id → V2.7 HITL legacy flow (existing handler unchanged below)
          if (payload.clarification_id !== undefined) {
            // Sprint ρ Phase D — drop stale event from prior turn.
            // (Late SSE delivery on the same EventSource can mis-render an old
            // clarification on top of a fresh turn. Compare turn_id when both
            // sides have it; if mismatch → ignore.)
            const incomingTurnId = payload.turn_id != null ? String(payload.turn_id) : null;
            if (incomingTurnId && s.turnId && incomingTurnId !== s.turnId) {
              break;
            }
            // Clarification lifecycle: tombstones prevent revival when full attachStream replay after resume
            // redelivers old clarification_required (per-attach seenSeq cannot block it);
            // answered cards must never return unanswered (TurnContainer renders an answered summary row).
            if (isClarificationAnswered(String(payload.clarification_id))) {
              const requestId = String(payload.clarification_id);
              const answered = getAnsweredClarification(requestId);
              if (answered && !answered.skipped && !answered.dismissed && payload.kind === "tool_context_select" && Array.isArray(payload.options)) {
                const options = payload.options.flatMap((option: unknown) => {
                  if (!option || typeof option !== "object") return [];
                  const pair = option as { value?: unknown; label?: unknown };
                  return pair.value != null && pair.label != null ? [{ value: String(pair.value), label: String(pair.label) }] : [];
                });
                markClarificationAnswered(requestId, clarificationDisplayAnswer(
                  requestId, answered.canonicalValues ?? answered.values,
                  { clarificationId: requestId, kind: "tool_context_select", options }, false,
                ));
              }
              break;
            }
            const candidates = Array.isArray(payload.candidates)
              ? (payload.candidates as unknown[]).map((v) => String(v))
              : [];
            // Batch 2 (PRD §3.3b): pass through tool_context_select structured options / remember toggle.
            const pairOptions = Array.isArray(payload.options)
              ? (payload.options as unknown[]).flatMap((o) => {
                  const rec = o as { value?: unknown; label?: unknown } | null;
                  return rec && rec.value != null
                    ? [{ value: String(rec.value), label: String(rec.label ?? rec.value) }]
                    : [];
                })
              : undefined;
            ns.clarificationPending = {
              candidates,
              fallbackText: payload.fallback_text != null ? String(payload.fallback_text) : null,
              clarificationId: String(payload.clarification_id),
              questions: parseThetaQuestions(payload.questions),
              multiSelect: Boolean(payload.multi_select),
              kind: payload.kind != null ? String(payload.kind) : undefined,
              options: pairOptions && pairOptions.length ? pairOptions : undefined,
              rememberOption: Boolean(payload.remember_option),
              rememberDefault: typeof payload.remember_default === "boolean" ? payload.remember_default : undefined,
              tool: payload.tool != null ? String(payload.tool) : undefined,
              ...(payload.purpose === "document_draft" ? { purpose: "document_draft" as const } : {}),
            };
            break;
          }
          // M156-M3b: backend detects verdict=clarify and pushes a candidate request; frontend shows ClarificationCard
          const reqId = String(payload.request_id ?? "");
          if (!reqId) break;
          // Deduplicate: admit each request_id only once
          if (s.clarifications.some((c) => c.request_id === reqId)) break;
          const rawKind = String(payload.kind ?? "token");
          const kind:
            | "token"
            | "options"
            | "task_plan"
            | "business_rule_review"
            | "ambiguous_turn"
            | "formula_confirm"
            | "concept_resolution"
            | "metric_formula" =
            rawKind === "options" || rawKind === "task_plan" ||
            rawKind === "business_rule_review" || rawKind === "ambiguous_turn" ||
            rawKind === "formula_confirm" || rawKind === "concept_resolution" ||
            rawKind === "metric_formula"
              ? rawKind : "token";
          ns.clarifications = [
            ...s.clarifications,
            {
              request_id: reqId,
              turn_id: String(payload.turn_id ?? s.turnId ?? ""),
              workspace_id: String(payload.workspace_id ?? payload.space_id ?? ""),  // Batch 3/c5 forward dual reading (old live/persist keys frozen)
              token: String(payload.token ?? ""),
              rationale: String(payload.rationale ?? ""),
              kind,
              multi_select: Boolean(payload.multi_select),
              candidates: Array.isArray(payload.candidates)
                ? (payload.candidates as Array<Record<string, unknown>>).map((c) => ({
                    value: String(c.value ?? ""),
                    label: String(c.label ?? c.value ?? ""),
                    hint: c.hint ? String(c.hint) : undefined,
                    confidence: c.confidence ? String(c.confidence) : undefined,
                  }))
                : [],
            },
          ];
          break;
        }
        // ─── Sprint θ Phase 8d → chat-redesign Phase 1.5: tool_called
        //     handler retired. The event was already dead (0 backend
        //     emit sites; deleted from events.py enum). step_started
        //     covers the surface end-to-end. ─────────────────────────
        case "agent_spawned": {
          // Sprint chat-stream-fix A-1 (2026-05-16): agent_spawned event was
          // never listened-to → Lead identity lost frontend-side, all events
          // fell through to v3-reducer fallback stub w/ role="compute" →
          // "Lead 消失" production regression. Now upsert peersDispatched with real
          // backend-issued subagent_id (UUID) for BOTH lead and compute
          // profiles. peer_dispatched event (which lacks subagent_id, see
          // subagent.py:2689) becomes informational; agent_spawned is the
          // authoritative source for identity.
          // Sprint phase-plan-removal Phase A0: earliest "turn is V4" signal
          // (Lead spawn fires at seq 2, before any tool_call) — flip shape gate.
          ns.hasDispatched = true;
          const subagentId = String(payload.subagent_id ?? "");
          const profile = String(payload.profile ?? "");
          const task = String(payload.task ?? "");
          if (!subagentId) break;
          const existing = s.peersDispatched ?? [];
          const matchIdx = existing.findIndex((p) => p.subagentId === subagentId);
          // agent_spawned can fire 3x per agent (spawn / finish / max-iter).
          // First emit carries task; later emits may omit. Preserve task.
          const entry = {
            peerName: profile || subagentId,
            dispatchedAt: Date.now(),
            parentCtxSummary: task,
            subagentId,
          };
          if (matchIdx >= 0) {
            const old = existing[matchIdx];
            ns.peersDispatched = [
              ...existing.slice(0, matchIdx),
              { ...old, peerName: profile || old.peerName,
                parentCtxSummary: task || old.parentCtxSummary,
                subagentId },
              ...existing.slice(matchIdx + 1),
            ];
          } else {
            ns.peersDispatched = [...existing, entry];
          }
          break;
        }
        case "peer_dispatched": {
          // Sprint λ F5 (R2-a): upsert by subagent_id to dedup 3× backend emit
          // (subagent.py:694/1390/1505). Falls back to peerName when no id.
          // Sprint chat-stream-fix (2026-05-16): peer_dispatched payload from
          // backend (subagent.py:2689) has NO subagent_id — only peer_name +
          // parent_ctx_summary. Authoritative identity now comes from
          // agent_spawned handler above. This case retained for legacy
          // mock-scenarios + as a parent_ctx_summary enrichment path.
          // Sprint phase-plan-removal Phase A0: redundant V4 shape-gate flip.
          ns.hasDispatched = true;
          const peerName = String(payload.peer_name ?? "");
          const subagentId = payload.subagent_id != null
            ? String(payload.subagent_id) : undefined;
          const summary = String(payload.parent_ctx_summary ?? "");
          if (!peerName && !subagentId) break;
          const existing = s.peersDispatched ?? [];
          const matchIdx = existing.findIndex((p) =>
            subagentId && p.subagentId
              ? p.subagentId === subagentId
              : p.peerName === peerName,
          );
          const entry = {
            peerName: peerName || subagentId!,
            dispatchedAt: Date.now(),
            parentCtxSummary: summary,
            subagentId,
          };
          if (matchIdx >= 0) {
            // Upsert: P1-Eng-1 (post-review) — keep old summary if new is empty
            // to prevent clobber from 2nd/3rd emit that omits summary.
            const old = existing[matchIdx];
            ns.peersDispatched = [
              ...existing.slice(0, matchIdx),
              {
                ...old,
                parentCtxSummary: summary || old.parentCtxSummary,
                subagentId: subagentId ?? old.subagentId,
              },
              ...existing.slice(matchIdx + 1),
            ];
          } else {
            ns.peersDispatched = [...existing, entry];
          }
          break;
        }
        case "turn_failed": {
          // Match error / done extraction: public code is reason_code. Restart-recovery turn_failed uses an internal
          // reason string (turn_dispatch_owner_lost); reading only it displays service_interrupted live as
          // lead_internal_error instead of the correct service_interrupted message.
          const reasonCode = terminalReasonCode(payload.reason_code ?? payload.reason);
          const failedTurnId = String(
            payload.turn_id ?? s.turnId ?? "",
          );
          const prior = ns.errorReason ?? s.errorReason;
          ns.errorReason = {
            source: "turn_failed",
            reasonCode,
            diagnostics: keepFirstTerminalDiagnostics(
              prior?.source === "turn_failed"
                ? prior.diagnostics
                : undefined,
              terminalErrorDiagnostics(
                payload,
                reasonCode,
                failedTurnId,
              ),
              failedTurnId,
              reasonCode,
            ),
            retryUrl: payload.retry_url != null ? String(payload.retry_url) : undefined,
            recoveryAction:
              parseRecoveryAction(payload.recovery_action)
              ?? (prior?.source === "turn_failed" ? prior.recoveryAction : undefined),
          };
          clearLegacyFailureAnswer(ns, s);
          ns.running = false;
          if (s.plan && s.plan.status === "running") {
            ns.plan = planFinish(s.plan, Date.now(), "failed");
          }
          // chat-truth Phase Q (P1-Q1): on terminal failure, branch on
          // canvas-content-existence, NOT on responseKind (which is only set
          // on `done` path — unreliable here). If turn has buffered Lead
          // canvas content, reveal partial canvas (better UX than empty);
          // else clear (no orphan reveal).
          // O5: sticky lastFailedTurnId was removed with the fake button (retry handler no longer exists).
          // The reveal/clear runs outside this updater (`terminalCanvasEffects`).
          break;
        }
        case "canvas_op_failed": {
          const reasonCode = normalizeCanvasFailureReasonCode(
            payload.reason_code,
          );
          const failedTurnId = String(
            payload.turn_id ?? s.turnId ?? "",
          );
          // S8.c — turn keeps running (canvas op was non-blocking); inline error pill.
          // Per integration plan §3.6: lives next to turn (not in canvas-store).
          ns.errorReason = {
            source: "canvas_op_failed",
            reasonCode,
            diagnostics: terminalErrorDiagnostics(
              payload,
              reasonCode,
              failedTurnId,
              "chat_canvas_failed",
            ),
            sectionId: payload.section_id != null ? String(payload.section_id) : undefined,
          };
          break;
        }
        case "peer_dispatch_failed": {
          // A peer failure is not a turn failure. The canonical turn-store
          // already captured this peer's terminal fact before the legacy
          // reducer runs; Lead may recover and complete with the other facts.
          break;
        }
        case "turn_aborted": {
          ns.aborted = true;
          ns.running = false;
          ns.terminalStatus = "aborted";
          if (s.plan && s.plan.status === "running") {
            ns.plan = planFinish(s.plan, Date.now(), "failed");
          }
          // Sprint κ Phase C (PRD r2 §4.4.4 / U2): prune any streaming-state
          // sections so canvas shows clean cancel state. tool-result-driven
          // sections (already in done state) are preserved.
          // pruneStreamingSections and the same content-existence reveal/clear as
          // turn_failed run outside this updater (`terminalCanvasEffects`).
          break;
        }
        // chat-solidify 2026-07-03: removed case "memory.proposal.suggested", with zero backend emit
        // sites (deterministic /remember+recall replaced V3.5 NL proposals); memory-proposal-card.tsx also removed.
        case "cca.tool_create_open": {
          // cca P2 (C3): the Lead opened the create-tool card (team-admin self-serve). The payload
          // carries ONLY a scrubbed spec-URL hint — never a secret. The card calls the P1 endpoints
          // directly with the user's session; this just surfaces it. Persists until dismissed.
          ns.createToolOpen = {
            spec_url_hint:
              typeof payload.spec_url_hint === "string" ? payload.spec_url_hint : null,
            // narrow to the two known modes — an unexpected value must not select a tab
            kind_hint: payload.kind_hint === "mcp" || payload.kind_hint === "openapi" ? payload.kind_hint : null,
          };
          break;
        }
        case "document.draft_open":
        case "coach.resource_open":
        case "coach.alignment_card":
        case "coach.operation_open": break; // Shared dispatcher owns live and replay projection.
        case "configuration.canvas_open": {
          const pointer = payload.target as Record<string, unknown> | undefined;
          if (pointer?.page === "workspace_i18n" && typeof pointer.user_id === "string" && typeof pointer.team_id === "string" && typeof pointer.workspace_id === "string") {
            ns.configurationCanvasOpen = { turnId: String(payload.turn_id ?? ""), target: { page: "workspace_i18n", userId: pointer.user_id, teamId: pointer.team_id, workspaceId: pointer.workspace_id } };
          }
          break;
        }
        case "config.lite_propose": {
          if (typeof payload.proposal_id === "string") ns.settingsProposalIds = [...new Set([...(s.settingsProposalIds ?? []), payload.proposal_id])];
          break;
        }
        case "cca.subagent_creator_open": {
          // Conversational authoring publishes only a durable owner-scoped draft pointer.
          // Generated config never rides the event stream; the review card GETs it using
          // the current authenticated team/user. Legacy events remain manual-prefill only.
          const draftId = typeof payload.draft_id === "string" ? payload.draft_id : null;
          const previous = ns.subagentCreatorOpen;
          const sameDraft = draftId !== null && draftId === previous?.draft_id;
          ns.subagentCreatorOpen = {
            purpose_hint:
              typeof payload.purpose_hint === "string" ? payload.purpose_hint : null,
            draft_id: draftId,
            draft_status:
              typeof payload.draft_status === "string" ? payload.draft_status : null,
            draft_title:
              typeof payload.draft_title === "string" ? payload.draft_title : null,
            draft_version:
              typeof payload.draft_version === "number" ? payload.draft_version : null,
            // User-stated identity hints survive clarification of this exact draft.
            // A different draft cannot inherit hints from the previous one.
            name_hint:
              typeof payload.name_hint === "string" ? payload.name_hint : sameDraft ? previous?.name_hint ?? null : null,
            display_name_hint:
              typeof payload.display_name_hint === "string" ? payload.display_name_hint : sameDraft ? previous?.display_name_hint ?? null : null,
          };
          break;
        }
        case "cca.workflow_solidify_open": {
          // workflow-solidify S2: the Lead opened the solidify card. Payload carries {turn_id (distill
          // target; null on conversational entry → FE resolves the most recent completed turn),
          // hint}. The card calls /api/v1/workflow-solidify/distill + dry-run + team-skills with the
          // user's session; this just surfaces it. Persists until dismissed.
          ns.solidifyOpen = {
            turn_id: typeof payload.turn_id === "string" ? payload.turn_id : null,
            hint: typeof payload.hint === "string" ? payload.hint : null,
          };
          break;
        }
        case "clarification.resolved": {
          // User selected → backend resolves the choice and pushes its event → frontend removes it from the list
          const reqId = String(payload.request_id ?? "");
          if (!reqId) break;
          ns.clarifications = s.clarifications.filter((c) => c.request_id !== reqId);
          // 116-hitl-resume fix: reattachment triggers full Gateway replay (chat.py stream
          // reconnection semantics), redelivering old clarification_required and restoring
          // clarificationPending just cleared by attachStream; HITL card revives, suppressing resumed-pass
          // rendering. In the replay sequence,
          // resolved immediately follows required; reverse required here to make the reducer
          // replay-idempotent (same principle as turn.citation_registered).
          if (s.clarificationPending?.clarificationId === reqId) {
            ns.clarificationPending = null;
          }
          // Resolved arrival (live or replay after sequence repair) permanently
          // tombstones the card, preventing revival by any later clarification_required replay.
          // review FE-P2-3: module-level mark side effect inside setState updater matches existing patterns
          // here (track/canvas-store operations also live in updaters). mark is idempotent for the same key/value,
          // harmless under StrictMode double invocation / bail-out recomputation.
          markClarificationAnswered(reqId, clarificationDisplayAnswer(reqId,
            Array.isArray(payload.chosen_values)
              ? (payload.chosen_values as unknown[]).map((v) => String(v))
              : payload.chosen_value != null ? [String(payload.chosen_value)] : [],
            s.clarificationPending, Boolean(payload.skipped),
          ));
          break;
        }
        case "clarification.dismissed": {
          const reqId = String(payload.request_id ?? "");
          if (!reqId) break;
          ns.clarifications = s.clarifications.filter(
            (clarification) => clarification.request_id !== reqId,
          );
          if (s.clarificationPending?.clarificationId === reqId) {
            ns.clarificationPending = null;
          }
          // An invalidation is an inverse of the actionable card, not an
          // answered summary.  The dismissed tombstone only prevents a later
          // required replay from resurrecting it.
          markClarificationAnswered(reqId, { values: [], dismissed: true });
          break;
        }
        case "turn.resumed": {
          // Clarification lifecycle: resume-start placeholder (spawn_resume_turn emits before worker).
          // Three roles: ① visible choice-received signal in the silence between selection and LLM's first token
          // (TurnContainer summary + liveness rows); ② authoritative server answer acknowledgement → tombstone;
          // ③ once persisted, stale done(clarification_pending) is no longer the last row, enabling backend
          // replay filtering.
          // review FE-P2-4: turn_id stale-guard matches clarification_required (late/cross-turn
          // turn.resumed neither changes this turn's state nor causes needless rerenders).
          const incomingTurnId = payload.turn_id != null ? String(payload.turn_id) : null;
          if (incomingTurnId && s.turnId && incomingTurnId !== s.turnId) {
            return s;
          }
          const reqId = String(payload.request_id ?? "");
          if (!reqId) return s;
          if (ns.errorReason?.source === "clarification_resume") ns.errorReason = null;
          markClarificationAnswered(reqId, clarificationDisplayAnswer(reqId,
            Array.isArray(payload.chosen_values)
              ? (payload.chosen_values as unknown[]).map((v) => String(v))
              : [],
            s.clarificationPending, Boolean(payload.skipped),
          ));
          ns.clarifications = s.clarifications.filter((c) => c.request_id !== reqId);
          if (s.clarificationPending?.clarificationId === reqId) {
            ns.clarificationPending = null;
          }
          break;
        }
        case "headline": {
          // chat-style-fix Phase B1 (Phase A backend A2): Lead emits a single
          // Turn guide (≤30 chars) right after response_kind. Hard-clamp + trim
          // defensively even though scanner already enforces this on the backend.
          const text = String(payload.text ?? "").slice(0, 30).trim();
          if (text) ns.headline = text;
          break;
        }
        // ─── chat-v4 W3/W4 — stage v4 events on runner so synthesise-v3-events
        // emits V3InputEvent passthrough → reducer applies → TurnContainer renders.
        // SSE `data` IS the payload dict directly (gateway sends
        // `json.dumps(row.payload)`, no wrapping — see chat.py:342).
        // Sprint phase-plan-removal: turn.plan + phase.update SSE cases deleted.
        case "lead.subagent_summary": {
          const key = String(payload.spawn_id ?? payload.subagent_id ?? "");
          if (!key) break;
          const prev = (ns.subagentSummaries as Record<string, unknown> | undefined) ?? {};
          ns.subagentSummaries = { ...prev, [key]: payload };
          break;
        }
        case "lead.final.payload": {
          ns.finalPayload = payload;
          break;
        }
        case "turn.citation_registered": {
          // prod-bug-bash-039 Phase 5 (0.39.3) — backend `_resolve_citations`
          // + ξ auto-append populate the registry; backend emits one event
          // per Citation at narrative finalize time.
          //
          // Field name `citation_seq` (NOT `seq`) — bus envelope clobbers
          // `payload.seq` with its monotonic counter. CTO Sec-Review #3 P0-1.
          //
          // Perf: return `s` (NOT `break`/`ns`) on no-op dedup so React's
          // strict-equality bail-out skips the render. attachStream replay can
          // re-fire 50+ identical events. CTO Perf-Review #4 P0-2.
          const parsed = citationFromPayload(payload as Record<string, unknown>);
          const seq = Number(payload.citation_seq ?? payload.seq);
          if (!Number.isInteger(seq) || seq < 1) return s;
          const existing = s.citations ?? [];
          const collision = existing.find((c) => c.seq === seq);
          const aid = String(payload.artifact_id ?? payload.artifactId ?? "");
          if (collision) {
            if (parsed) {
              const merged = mergeCitation(collision, parsed);
              if (merged !== collision) {
                ns.citations = existing.map(c => c.seq === seq ? merged : c);
                break;
              }
            }
            // CTO FE-Review P1-3: surface seq-collision-with-different-artifact
            // (silent registry corruption). Dedup still wins by first-write.
            if (aid && aid !== collision.artifactId) {
              console.warn(
                "[bowhead] citation.dedup.collision",
                { seq, existing: collision.artifactId, incoming: aid },
              );
            }
            return s;
          }
          if (!parsed) return s;
          // Same parsing as refreshed replay (citation-payload.ts): all kinds, six states, anchors and KB locations.
          ns.citations = [...existing, parsed];
          break;
        }
        // Sprint phase-plan-removal: turn.plan_cancelled SSE case deleted
        // (no more turn.plan event = no need to cancel it).
      }
      return ns;
    });
  };

  // ── Per-connection listeners (reattach to new es on every reconnect) ─────────────────────────────
  const wire = (es: EventSource): void => {
    const detach = attachCanvasSSE(es, canvasGate);
    detachCanvas = detach;
    (["turn_started", "tool_call", "tool_result",
      "partial_answer", "final_answer",  // ν2 v2: final_answer wires chat bubble
      "thinking", "error", "done",
      "plan_preview",
      "clarification_required", "clarification.required",  // omicron-12: backend now emits underscore; keep dot listener for replay back-compat
      "clarification.resolved", "clarification.dismissed",
      "turn.resumed",  // Clarification lifecycle: resume-start placeholder (tombstone + summary row + silence-window signal)
      // Sprint θ Phase 8d additive events
      // tool_called dropped chat-redesign Phase 1.5 (0 backend emit sites).
      "peer_dispatched",
      // Sprint chat-stream-fix Phase A (2026-05-16): agent_spawned was missing
      // from FE listener set → silent drop. chat-solidify 2026-07-03: trace.step /
      // Removed tool_finding: allowlisted but absent from reducer/dispatcher cases (pure no-op);
      // both now classified as backend internal telemetry (chat-pipeline.md), named in guard tests' ignore list.
      "agent_spawned",
      "agent.collaboration",
      "agent.context",
      "turn_failed", "canvas_op_failed", "peer_dispatch_failed", "turn_aborted",
      "cca.tool_create_open",  // cca P2 (C3): opens the inline create-tool card
      "document.draft_open",
      "coach.operation_open",
      "coach.alignment_card",
      "coach.resource_open",
      "configuration.canvas_open",
      "cca.subagent_creator_open",  // cca P3 (S5): opens the inline create-subagent card
      "cca.workflow_solidify_open",  // workflow-solidify S2: opens the inline solidify card
      // chat-style-fix Phase B1 (Phase A backend A2): Lead turn-guide event.
      "headline",
      // chat-v4 W3/W4 — V4 chat-bubble events (turn.plan / phase.update /
      // lead.subagent_summary / lead.final.payload). Without these in the
      // whitelist, backend emits arrive at SSE but EventSource never wakes
      // handle() → runner.v4* fields stay undefined → synthesise-v3-events
      // skips V3 passthrough → TurnContainer never renders. See chat-v4 D dogfood
      // 2026-05-18 finding (sprint summary, project_chat_v4_impl_done.md).
      // Sprint phase-plan-removal: "turn.plan" + "phase.update" + "turn.plan_cancelled" deleted.
      "lead.subagent_summary", "lead.final.payload",
      // Existing approval lifecycle facts. Chat consumes them only for the
      // execution-disclosure fold guard; the approval/result surface remains
      // owned by the approval UI and notification center. approval.rejected
      // (R-2) is the terminal fact for the reject path — without it a rejected
      // approval stays "pending" in approvalStatusById forever.
      "approval.required", "approval.executed", "approval.rejected",
      // Sprint chat-redesign-2026-05-21 Phase 1.1 — Step List Card events.
      // Without these in the listener whitelist EventSource silently drops
      // them and the turn trace surface (TurnContainer.TurnTraceRegion)
      // stays blank even when backend is emitting. dispatch routes to lib/turn-store; legacy
      // tool_call/tool_result still flows in parallel during the dual-emit
      // window so v3/v4 surfaces don't regress.
      "step_started", "step_progress", "step_done",
      "step_list_card_created", "step_list_card_finalized",
      // Planning Mode — plan-card events. SAME EventSource silent-drop concern:
      // without these the dispatcher (turn-store-dispatch.ts) never sees them, so
      // plan_proposed never seeds the card in a real browser session (curl SSE +
      // the static mockup both bypass this listener, which is why it slipped).
      // step_blocked/step_failed are plan-level (plan_id-scoped), distinct from
      // the step-list step_started/step_done above (card_id-scoped) — dispatch
      // disambiguates by plan_id.
      "plan_proposed", "plan_running", "plan_completed", "plan_blocked",
      "plan_failed", "step_blocked", "step_failed",
      "emoji_ack",  // emoji_remove removed with dead event names (backend never emitted it)
      "turn.follow_up",
      // Phase 1.6 G3 — Lead narrative streaming. EventSource silently drops
      // unlisted events, so the dispatcher in turn-store-dispatch.ts never sees
      // them without this entry.
      "narrative.delta",
      // prod-bug-bash-039 Phase 5 (0.39.3) — citation registry events.
      // Same EventSource silent-drop concern: without explicit listener the
      // turn.citation_registered emits never wake `handle()` → runner.citations
      // stays empty → ArtifactChip can't resolve [cite:N] → click no-ops.
      "turn.citation_registered",
      // Configuration copilot batch 0.5: lite-confirmation beat. Same silent-drop pattern: omitting this line prevents dispatcher
      // from ever seeing config.lite_propose, so confirmation never appears in a real browser.
      "config.lite_propose",
      // Batch 1-b: configuration-draft drawer (likewise, omitting this line means it never appears in real browsers)
      "config.draft_updated",
      // Durable control facts advance the exact contiguous event sequence but
      // do not project a marker into chat content.
      "turn.llm_invocation", "turn.effect_disclosure_settled",
      // Router retry after a model call failed before replying. Same silent-drop concern:
      // without this entry the running band never shows "retrying (n/m)".
      "llm.retry_scheduled",
    ] as const).forEach((name) => {
      es.addEventListener(name, (e: MessageEvent) => {
        // error collides with EventSource's native transport error: native errors lack .data; only
        // server event: error with a payload is a genuine terminal turn failure. Leave native transport errors to
        // es.onerror reconnection; never invoke error reducer and incorrectly mark the turn failed.
        if (name === "error" && e.data == null) return;
        handle(name, e.data);
        // Batch 1-b: automatically opening drawer is **live-only transient UI** (wire runs only for live SSE; F5 uses
        // replayTurnEvents, bypassing here); dispatcher maintains turn-store terminal state symmetrically on both paths.
        // Never reopen user-closed drafts (full SSE reconnect replay, tech-design-batch1b §3-6).
        if (name === "config.draft_updated") {
          try {
            const draftId = JSON.parse(e.data || "{}")?.draft_id;
            if (typeof draftId === "string" && draftId) requestAutoOpenDraft(draftId);
          } catch {
            /* Invalid payload: dispatcher already discarded it; stay silent here too. */
          }
        }
      });
    });

    // W11-8: streaming.state, backend stall/error detection and frontend W3-3 visual indicator
    es.addEventListener("streaming.state", (e: MessageEvent) => {
      try {
        const payload = JSON.parse(e.data || "{}");
        const next = payload.state as "active" | "paused" | "error" | undefined;
        if (next === "active" || next === "paused" || next === "error") {
          setState((s) => ({ ...s, streamingState: next }));
        }
      } catch {
        // ignore malformed
      }
    });
    // turn.progress: transient snapshot of the agents running this turn (agent run limits,
    // phase 3). Like keepalive it bypasses bus/handle and is never replayed.
    es.addEventListener("turn.progress", (e: MessageEvent) => {
      try {
        const payload = JSON.parse(e.data || "{}") as { agents?: unknown };
        if (!Array.isArray(payload.agents)) return;
        const agents = payload.agents.flatMap((item) => {
          const a = item as Record<string, unknown>;
          if (typeof a.subagent_id !== "string" || typeof a.step !== "number") return [];
          return [{
            subagentId: a.subagent_id,
            profile: typeof a.profile === "string" ? a.profile : "",
            displayName: typeof a.display_name === "string" ? a.display_name : "",
            step: a.step,
          }];
        });
        setState((s) => (s.running ? { ...s, agentProgress: { agents } } : s));
      } catch {
        // ignore malformed
      }
    });
    // keepalive: Gateway emits every 15s (bypasses bus/handle); reset watchdog liveness.
    es.addEventListener("keepalive", () => {
      lastKeepalive = Date.now();
      // ⚠ Heartbeats **bypass handle**, requiring a separate measurement here or we miss the entire
      // heartbeat-only interval with no business events, precisely the interval most needing visibility.
      noteTransportKeepalive(transportSample, lastKeepalive);
    });

    es.addEventListener("stream.checkpoint", (e: MessageEvent) => {
      lastKeepalive = Date.now();
      try {
        const payload = JSON.parse(e.data || "{}") as Record<string, unknown>;
        if (payload.terminal_protocol_version !== 1) {
          failProtocol("terminal_protocol_future_version");
          return;
        }
        const generation = Number(payload.current_generation);
        const highwater = Number(payload.persistent_event_highwater);
        const receipt = payload.current_receipt_id;
        if (
          !Number.isSafeInteger(generation)
          || generation < 1
          || !Number.isSafeInteger(highwater)
          || highwater < 0
          || (
            receipt !== null
            && receipt !== undefined
            && (typeof receipt !== "string" || receipt.length === 0)
          )
        ) {
          failProtocol("terminal_protocol_malformed");
          return;
        }
        if (
          highwater < highestContiguousEventSeq
          || highwater < protocolCheckpointHighwater
          || (
            protocolCheckpointSeen
            && currentProtocolGeneration !== generation
          )
          || (
            currentProtocolReceiptId != null
            && receipt !== currentProtocolReceiptId
          )
        ) {
          failProtocol("terminal_protocol_malformed");
          return;
        }
        terminalProtocolVersion = 1;
        currentProtocolGeneration = generation;
        currentProtocolReceiptId = typeof receipt === "string"
          ? receipt
          : null;
        protocolCheckpointHighwater = highwater;
        protocolCheckpointSeen = true;
        setState((s) => {
          const generationChanged = s.executionGeneration != null
            && s.executionGeneration !== generation;
          return {
            ...s,
            terminalProtocolVersion: 1,
            executionGeneration: generation,
            effectDisclosure: generationChanged ? null : s.effectDisclosure,
            effectDisclosureDiscoveryPending:
              generationChanged || s.effectDisclosure == null,
          };
        });
      } catch {
        failProtocol("terminal_protocol_malformed");
      }
    });

    es.addEventListener("terminal.protocol_error", (e: MessageEvent) => {
      let code = "terminal_dependency_recovering";
      try {
        const payload = JSON.parse(e.data || "{}") as Record<string, unknown>;
        if (typeof payload.code === "string") code = payload.code;
      } catch {
        code = "terminal_protocol_malformed";
      }
      failProtocol(code);
    });

    es.addEventListener("done", (e: MessageEvent) => {
      if (esRef.current !== es) return;
      if (protocolFailed) return;
      if (terminalProtocolVersion === 1) {
        try {
          const payload = JSON.parse(e.data || "{}") as Record<string, unknown>;
          if (payload.terminal_current !== true) return;
          if (
            currentProtocolGeneration != null
            && payload.generation !== currentProtocolGeneration
          ) return;
        } catch {
          failProtocol("terminal_protocol_malformed");
          return;
        }
      }
      markTerminal();
      // UX-3 G2 race fix (chat-solidify 2026-07-04, code-review): do not use shared
      // localClearWatchdog for delayed cleanup: steer turn.follow_up attaches its continuation stream
      // within milliseconds after done, installing the continuation watchdog into the same watchdogRef;
      // the old setTimeout fires 1s later and wrongly clears the continuation stall watchdog, leaving its turn
      // permanently at「运行中」on half-open sockets (Tencent CLB cuts every 60–90s) until F5, recreating the supervisor's target
      // problem accidentally. Clear only **this stream's** interval ID, and null the ref only if it still
      // points there (leave it alone after a subsequent attachStream takes ownership).
      const ownWatchdog = watchdogRef.current;
      setTimeout(() => {
        if (ownWatchdog) clearInterval(ownWatchdog);
        if (watchdogRef.current === ownWatchdog) watchdogRef.current = null;
      }, 1_000);
      // Normal completion: running=false, streamingState idle. (interrupted field has no readers.)
      setState((s) => ({ ...s, running: false, streamingState: "idle" }));
      // image-slim sprint UI fix (#284): /reports listens for this event and silently refetches.
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("bowhead.canvas.changed"));
      }
    });

    es.onerror = (): void => {
      // This es replaced / externally torn down ⇒ stop silently without reconnecting.
      if (stopped || esRef.current !== es) { try { es.close(); } catch { /* noop */ } detach(); return; }
      // image-slim sprint UI fix: server closes SSE after emitting done; browser treats this as abnormal
      // and fires onerror. doneSeen separates normal completion from actual disconnection (never falsely mark failure).
      if (doneSeen) { try { es.close(); } catch { /* noop */ } detach(); return; }
      // Actual transport interruption ⇒ close this connection and reconnect with backoff (retain running, avoiding false completion).
      try { es.close(); } catch { /* noop */ }
      detach();
      if (Date.now() - lastNewEventAt > GIVE_UP_SILENCE_MS) {
        // Long quiet stretch: ask the backend before declaring anything (see confirmLivenessOrGiveUp).
        setState((s) => (s.running ? { ...s, streamingState: "error" } : s));
        confirmLivenessOrGiveUp();
        return;
      }
      // streamingState=error ⇒ NetworkBanner「连接断了，重连中…」; running unchanged.
      setState((s) => (s.running ? { ...s, streamingState: "error" } : s));
      scheduleReopen();
    };
  };

  function giveUp(): void {
    // The backend says the turn's worker is gone: running=false + streamingState=error shows「连接断了」.
    stopped = true; clearReopen(); localClearWatchdog();
    setState((s) => (s.running ? { ...s, running: false, streamingState: "error" } : s));
  }

  // A dropped stream after a long quiet stretch: the turn may simply be working (a long tool or
  // model call). Reconnect while the backend reports it alive or finished (a finished turn replays
  // its terminal events on reconnect); give up only when its worker has stopped renewing the
  // lease for OWNER_LOST_GIVE_UP_MS. Network errors on the status check keep reconnecting.
  function confirmLivenessOrGiveUp(): void {
    if (livenessCheck || stopped) return;
    if (!statusTurnId) { giveUp(); return; }
    livenessCheck = (async () => {
      try {
        const response = await apiFetch(`/api/v1/turns/${encodeURIComponent(statusTurnId)}/status`, { cache: "no-store" });
        if (!isCurrent() || stopped) return;
        if (response.ok) {
          const body = (await response.json()) as { terminal?: unknown; alive?: unknown };
          if (body.terminal === true || body.alive === true) {
            ownerLostSince = null;
          } else if (body.alive === false) {
            ownerLostSince ??= Date.now();
            if (Date.now() - ownerLostSince > OWNER_LOST_GIVE_UP_MS) { giveUp(); return; }
          }
        }
      } catch {
        // Network trouble on the check itself is not evidence that the turn died.
      } finally {
        livenessCheck = null;
      }
      if (isCurrent() && !stopped) scheduleReopen();
    })();
  }

  function scheduleReopen(): void {
    clearReopen();
    const delay = backoffMs;
    const quiet = Date.now() - lastNewEventAt > GIVE_UP_SILENCE_MS;
    backoffMs = Math.min(backoffMs * 2, quiet ? QUIET_RECONNECT_MAX_MS : RECONNECT_MAX_MS);
    reopenTimer = setTimeout(() => {
      reopenTimer = null;
      if (!isCurrent()) return;
      openConnection();
    }, delay);
  }

  function openConnection(): void {
    if (stopped) return;
    // review #1: openConnection has **three** callers: initial connection, scheduleReopen and
    // watchdog stall cycle. Initially I counted in scheduleReopen, missing watchdog's
    // **main** path (half-open socket every 40s), so connection cuts + replay bursts were
    // falsely classified buffered, exactly what reconnected guards against. Counting here covers everything.
    if (currentEs !== null) transportSample.reconnects += 1;
    const es = new EventSource(absUrl);
    // review #4: ~17 canvas listeners (canvas-sse.ts:254), stream.checkpoint and
    // streaming.state share **the same es**, **bypassing handle**. Patching individual callers misses some,
    // so wrap here: thereafter record every message received by any listener.
    // ⇒ Three minutes of canvas streaming during a report turn no longer appear as three minutes of silence to msSinceLastEvent.
    const origAddEventListener = es.addEventListener.bind(es);
    (es as EventSource).addEventListener = ((name: string, fn: unknown, opts?: unknown) => {
      if (typeof fn !== "function") return origAddEventListener(name, fn as EventListener, opts as never);
      return origAddEventListener(
        name,
        ((ev: MessageEvent) => {
          noteTransportEvent(transportSample, Date.now());
          return (fn as (e: MessageEvent) => unknown)(ev);
        }) as EventListener,
        opts as never,
      );
    }) as typeof es.addEventListener;
    currentEs = es;
    esRef.current = es;
    // Give new connections all of STALL_RECONNECT_MS to prove liveness; otherwise next 5s watchdog tick uses
    // stale lastKeepalive to cycle again, causing half-open-socket reconnect storms every 5s rather than every 40s.
    lastKeepalive = Date.now();
    wire(es);
  }

  // Watchdog: Gateway keepalive every 15s; no activity for STALL_RECONNECT_MS (40s) means dead transport,
  // even with readyState OPEN (half-open socket; old implementation's early return caused stuck「运行中」),
  // forcing a cycle. R3: watchdogRef allows useEffect cleanup / cancel / hydrate to clear it.
  localClearWatchdog();
  watchdogRef.current = setInterval(() => {
    if (stopped || doneSeen || reopenTimer || esRef.current !== currentEs) return;
    if (Date.now() - lastKeepalive > STALL_RECONNECT_MS) {
      try { currentEs?.close(); } catch { /* noop */ }
      detachCanvas();
      setState((s) => (s.running ? { ...s, streamingState: "error" } : s));
      if (Date.now() - lastNewEventAt > GIVE_UP_SILENCE_MS) {
        confirmLivenessOrGiveUp();
        return;
      }
      backoffMs = RECONNECT_BASE_MS;
      openConnection();
    }
  }, 5_000);

  openConnection();
}

// ─── Public API ─────────────────────────────────────────
export interface UseTurnRunnerArgs {
  mode: "mock" | "sse";
  // mock mode
  scenarioId?: string | null;
  replayKey?: number;
  // sse mode
  message?: string | null;
  workspaceId?: string;
  memberUid?: string;
  /** Same-session follow-up: pass current sessionId so Gateway attaches the turn to that session. */
  activeSessionId?: string | null;
  draftChatBinding?: { draftId: string; version: number; dirty: boolean } | null;
  /** Monotonically increasing; bump means user clicked "新建", requiring canvas and all turn state to clear. */
  resetSignal?: number;
  sendTick?: number;
  /** Supplied on historical-session click: restore full conversation (chronological messages alternating user/assistant). */
  hydrate?: {
    sessionId: string;
    sessionTitle: string | null;
    messages: ChatMessage[];
    lastIntent?: IntentType | null;
    /** chat-polish-r3 Phase 1.5: lastTraceSteps retired — hydrate
     *  populates lib/turn-store via lib/turn-store-replay; step rendering
     *  reads turn-store directly. */
    lastSummary?: import("./types").TurnSummary | null;
    /** chat-hydrate-overhaul: last turn's turnId makes trace chip openable. */
    lastTurnId?: string | null;
    /** Last turn's server start time (epoch ms). Live startedAt exists on submission; after refresh,
     *  fill from here so user-bubble and Bowhead-signature times do not disappear after F5. */
    lastStartedAt?: number | null;
    /** chat-v4 hydrate: v4 slot payloads backfilled from persisted events
     *  of the last turn — completed turns don't reconnect SSE, so V4 emits
     *  otherwise never reach the reducer. See app/page.tsx `_fetchV4FromTurn`.
     *  Sprint phase-plan-removal: lastTurnPlan + lastPhaseUpdates slots deleted. */
    lastSubagentSummaries?: Record<string, unknown>;
    lastFinalPayload?: unknown;
    /** ν2 response_kind backfilled from the persisted done event so TurnContainer
     *  can suppress StepListCard / progress stack for chat-shape (prose) turns
     *  on session refresh. Mirrors live SSE path use-turn-runner.ts:748. */
    lastResponseKind?: "chat" | "canvas" | null;
    /** Durable status of the last hydrated Turn. */
    lastTerminalStatus?: string | null;
    lastExecutionGeneration?: number | null;
    lastTerminalProtocolVersion?: number | null;
    /** done.tool_invocation_count — TurnContainer gates StepListCard on this. See
     *  app/page.tsx `_fetchV4FromTurn` for source. */
    lastToolInvocationCount?: number;
    /** Latest persisted done's report_sections_saved (lib/report-sections-saved.ts); missing field = null. */
    lastReportSectionsSaved?: number | null;
    /** F1 ar2 clarification: persisted clarification_required payloads (each
     *  has candidates + fallback_text + clarification_id). Hydrate sets
     *  state.clarificationPending from the last one so ClarificationCard
     *  re-renders on session refresh. */
    lastClarifications?: unknown[];
    /** chat-v4 hydrate: when V4 owns the last turn, runner.user/userAttachments
     *  carry the user_text (since it's been removed from `messages`). */
    lastUserText?: string | null;
    lastUserAttachments?: AttachmentRef[];
    /** Persisted terminal failure for the routed last Turn (Replay after F5). */
    lastErrorReason?: import("./types").SseErrorReason | null;
    /** chat-redesign Phase 1.2 CTO 2nd review P0 #1 — the assistant prose
     *  of the last completed turn, threaded into runner.answer when V4 owns
     *  it (the V4 surface used to render this; now ChatBubbleNew does). */
    lastAnswer?: string | null;
    /** Explicit ownership of the terminal last Turn. Unlike finalPayload, this
     * is available before its event log has been replayed. */
    lastTurnRouted?: boolean;
    lastOperationKind?: "context_maintenance" | null;
    /** Event-derived fields that are empty during the persisted baseline and
     * filled by the guarded current-turn patch. */
    lastHeadline?: string;
    lastCitations?: import("./types").Citation[];
    lastEffectDisclosure?: import("./api").EffectDisclosureOverlayDTO | null;
    lastEffectDisclosureDiscoveryPending?: boolean;
    /** Canvas-only hydrate for shared reports where session detail is not readable. */
    reportOnly?: boolean;
    /** If the session's last turn remains running (status=running), returning to this session opens frontend
     * EventSource /stream/{turnId}; backend replays persisted events and continues streaming. */
    resumeTurn?: ResumeTurnHint | null;
    /** prod-bug-bash-038 #9 — full ArchivedTurn[] for prior completed turns;
     *  built by page.tsx via parallel /turns/{id}/events replay so prior Q&A
     *  renders on session reload (was disappearing before — runner.messages
     *  is workspace-context fallback, NOT visible chat history). */
    archivedTurns?: import("./types").ArchivedTurn[];
  } | null;
  hydrateTick?: number;
  /** Progressive replay patch for historical Turns. It updates only archived
   * projections and never resets the active runner or its SSE connection. */
  archivedTurnsPatch?: ArchivedTurnsHydrationPatch | null;
  /** Progressive enrichment for the terminal last Turn. */
  currentTurnHydrationPatch?: CurrentTurnHydrationPatch | null;
  /** Paired with sendTick: this message's artifact metadata (fileId + filename + size + mime).
   * Extract fileId for Gateway on send; runner keeps metadata for user-bubble chips. */
  attachments?: AttachmentRef[];
  /** Batch A: emit UUIDv4 + chat-submit.v1 and use receipt-based same-ID recovery. */
  submissionProtocolEnabled?: boolean;
  submissionRecoveryScope?: SubmissionRecoveryScope | null;
  /** Exact request preferences captured before the first durable attempt. */
  submissionWireSnapshot?: ComposerSubmissionWireSnapshot | null;
  /** A real start receipt associates the captured original page with its Turn. */
  onWorkspaceSettingsTurnAccepted?: (
    turnId: string, binding: NonNullable<ComposerSubmissionWireSnapshot["workspaceSettingsBinding"]>,
  ) => void;
  onWorkspaceSettingsAccessDenied?: (binding: NonNullable<ComposerSubmissionWireSnapshot["workspaceSettingsBinding"]>) => void;
  /** Explicit full reference source. Never infer tokens from the display message. */
  referenceInputDocument?: InputDocument | null;
  /** Exact local draft revision captured before a reference POST. */
  referenceSourceDraftRevision?: string | null;
  /** Internal adapter opt-in; production Composer remains governed by readiness. */
  referenceSubmissionEnabled?: boolean;
  /** Canonical failed Turn to replay; server ignores client preview as source. */
  replaySourceTurnId?: string | null;
  onSubmissionPending?: (
    attemptKey: string,
    scopeKey: string,
    pending: RecoveredSubmission,
  ) => void;
  /** Recovery launched outside Composer must settle its captured draft before the journal. */
  onReferenceSubmissionAccepted?: (attemptKey: string, scopeKey: string) => Promise<boolean>;
  onDocumentRevisionSubmissionAccepted?: (attemptKey: string, scopeKey: string) => Promise<boolean>;
  /** Mirrors definitive IndexedDB cleanup into the current-tab recovery guard. */
  onSubmissionCleared?: (
    attemptKey: string,
    scopeKey: string,
    clientSubmissionId: string,
  ) => void;
  /** Resolves the Composer's preparing state only after POST receipt/rejection. */
  onSendInitSettled?: (attemptKey: string, accepted: boolean, turn?: { turnId: string; sessionId: string | null }) => void;
  /** Terminal v1 identity already persisted (or retained in-memory if storage failed). */
  chatAction?: ComposerChatAction | null;
  /** Workspace captured with the marker; never borrow a later selection. */
  chatActionWorkspaceId?: string | null;
  chatActionScopeKey?: string | null;
  currentChatScopeKey?: string;
  sendAttemptKey?: string | null;
  onChatActionUnknown?: (
    action: ComposerChatAction,
  ) => void;
  onChatActionSettled?: (
    actionId: string,
    disposition: "adopted" | "rejected",
  ) => (
    "settled" | "deferred" | "superseded"
  ) | Promise<"settled" | "deferred" | "superseded">;
}

/** Reconnection information when session's last turn is unfinished during hydration. */
export interface ResumeTurnHint {
  turnId: string;
  executionGeneration?: number;
  userText: string;
  userAttachments?: AttachmentRef[];
}

export interface ArchivedTurnsHydrationPatch {
  sessionId: string;
  turns: import("./types").ArchivedTurn[];
}

export interface CurrentTurnHydrationPatch {
  sessionId: string;
  turnId: string;
  lastSummary?: import("./types").TurnSummary | null;
  lastSubagentSummaries?: Record<string, unknown>;
  lastFinalPayload?: unknown;
  lastResponseKind?: "chat" | "canvas" | null;
  lastToolInvocationCount?: number;
  lastReportSectionsSaved?: number | null;
  lastClarifications?: unknown[];
  lastErrorReason?: import("./types").SseErrorReason | null;
  lastHeadline?: string;
  lastCitations?: import("./types").Citation[];
  lastEffectDisclosure?: import("./api").EffectDisclosureOverlayDTO | null;
  terminalProtocolVersion?: number | null;
  executionGeneration?: number | null;
}

export function resolveHydratedClarificationPending(
  clarifications: unknown[] | undefined,
): TurnRunnerState["clarificationPending"] {
  if (!Array.isArray(clarifications) || clarifications.length === 0) {
    return undefined;
  }
  const last = clarifications[clarifications.length - 1] as Record<string, unknown>;
  if (
    !last
    || typeof last !== "object"
    || !last.clarification_id
    || isClarificationAnswered(String(last.clarification_id))
  ) {
    return undefined;
  }
  const candidates = Array.isArray(last.candidates)
    ? (last.candidates as unknown[]).map((value) => String(value))
    : [];
  const pairOptions = Array.isArray(last.options)
    ? (last.options as unknown[]).flatMap((option) => {
        const record = option as { value?: unknown; label?: unknown } | null;
        return record && record.value != null
          ? [{
              value: String(record.value),
              label: String(record.label ?? record.value),
            }]
          : [];
      })
    : undefined;
  return {
    candidates,
    fallbackText:
      last.fallback_text != null ? String(last.fallback_text) : null,
    clarificationId: String(last.clarification_id),
    questions: parseThetaQuestions(last.questions),
    multiSelect: Boolean(last.multi_select),
    kind: last.kind != null ? String(last.kind) : undefined,
    options: pairOptions?.length ? pairOptions : undefined,
    rememberOption: Boolean(last.remember_option),
    rememberDefault: typeof last.remember_default === "boolean" ? last.remember_default : undefined,
    tool: last.tool != null ? String(last.tool) : undefined,
    ...(last.purpose === "document_draft" ? { purpose: "document_draft" as const } : {}),
  };
}

export function mergeCurrentTurnHydration(
  current: TurnRunnerState,
  patch: CurrentTurnHydrationPatch,
): TurnRunnerState {
  if (
    current.sessionId !== patch.sessionId
    || current.turnId !== patch.turnId
  ) {
    return current;
  }
  if (
    current.executionGeneration != null
    && patch.executionGeneration != null
    && patch.executionGeneration < current.executionGeneration
  ) {
    return current;
  }
  const patchAdvancesGeneration = current.executionGeneration != null
    && patch.executionGeneration != null
    && patch.executionGeneration > current.executionGeneration;
  const mergedEffectDisclosure = patchAdvancesGeneration
    ? patch.lastEffectDisclosure ?? null
    : current.effectDisclosure ?? patch.lastEffectDisclosure ?? null;
  if (current.running) {
    return {
      ...current,
      effectDisclosure: mergedEffectDisclosure,
      effectDisclosureDiscoveryPending:
        current.effectDisclosureDiscoveryPending,
      terminalProtocolVersion:
        patch.terminalProtocolVersion ?? current.terminalProtocolVersion ?? null,
      executionGeneration:
        patch.executionGeneration ?? current.executionGeneration ?? null,
    };
  }
  return {
    ...current,
    summary: patch.lastSummary ?? current.summary,
    subagentSummaries: patch.lastSubagentSummaries,
    finalPayload: patch.lastFinalPayload,
    responseKind: patch.lastResponseKind ?? undefined,
    toolInvocationCount: patch.lastToolInvocationCount,
    reportSectionsSaved: patch.lastReportSectionsSaved ?? null,
    errorReason: patch.lastErrorReason ?? null,
    clarificationPending: resolveHydratedClarificationPending(
      patch.lastClarifications,
    ),
    headline: patch.lastHeadline,
    citations: patch.lastCitations ?? [],
    effectDisclosure: mergedEffectDisclosure,
    effectDisclosureDiscoveryPending:
      current.effectDisclosureDiscoveryPending,
    terminalProtocolVersion:
      patch.terminalProtocolVersion ?? current.terminalProtocolVersion ?? null,
    executionGeneration:
      patch.executionGeneration ?? current.executionGeneration ?? null,
  };
}

export function mergeArchivedTurnsHydration(
  current: import("./types").ArchivedTurn[],
  hydrated: import("./types").ArchivedTurn[],
): import("./types").ArchivedTurn[] {
  const hydratedIds = new Set(
    hydrated.flatMap((turn) => turn.turnId ? [turn.turnId] : []),
  );
  return [
    ...hydrated,
    ...current.filter((turn) => !turn.turnId || !hydratedIds.has(turn.turnId)),
  ];
}

export function useTurnRunner(args: UseTurnRunnerArgs): TurnRunnerState {
  const composerCapabilities = useComposerCapabilities();
  const mock = useMockRunner(
    args.mode === "mock" ? args.scenarioId ?? null : null,
    args.replayKey ?? 0,
    args.hydrate ?? null,
    args.hydrateTick ?? 0,
    args.archivedTurnsPatch ?? null,
    args.currentTurnHydrationPatch ?? null,
  );
  // bug fix: workspace_id no longer silently falls back to "1" (user selects 353 but the actual query targets ws 1).
  // Missing workspace_id → empty string, rejected by Gateway with 400; UI errors naturally rather than silently routing incorrectly.
  const wsId = args.workspaceId ?? "";
  // This error is meaningful only **when actually sending**, and belongs in an effect: previously in the hook body,
  // it logged on every render, twice under StrictMode. wsId is necessarily empty before Space hydration,
  // especially on report deep links: page.tsx waits for canvas DTO before setActiveId(canvas.workspaceId),
  // flooding every new browser/private window with red errors despite working normally (116 prod 2026-07-30
  // measurement: reports render normally, attachments 200, zero HTTP errors; noisy logs caused testers to falsely report faults).
  const pendingSend = args.mode === "sse"
    && (Boolean(args.message) || Boolean(args.attachments?.length));
  useEffect(() => {
    if (pendingSend && !wsId) {
      console.error("[useTurnRunner] workspace_id missing — chat send will fail. Caller must pass valid workspaceId.");
    }
  }, [pendingSend, wsId, args.sendTick]);
  const sse = useSSERunner(
    args.mode === "sse" ? args.message ?? null : null,
    wsId,
    args.memberUid ?? "",
    args.sendTick ?? 0,
    args.activeSessionId ?? null,
    args.resetSignal ?? 0,
    args.hydrate ?? null,
    args.hydrateTick ?? 0,
    args.archivedTurnsPatch ?? null,
    args.currentTurnHydrationPatch ?? null,
    args.attachments,
    composerCapabilities.planning,
    args.submissionProtocolEnabled === true,
    args.submissionRecoveryScope ?? null,
    args.submissionWireSnapshot ?? null,
    args.referenceInputDocument ?? null,
    args.referenceSourceDraftRevision ?? null,
    args.referenceSubmissionEnabled === true,
    args.replaySourceTurnId ?? null,
    args.chatAction ?? null,
    args.draftChatBinding ?? null,
    args.chatActionWorkspaceId ?? null,
    args.chatActionScopeKey ?? null,
    args.currentChatScopeKey ?? "",
    args.sendAttemptKey ?? null,
    args.onChatActionUnknown,
    args.onChatActionSettled,
    args.onSubmissionPending,
    args.onReferenceSubmissionAccepted,
    args.onDocumentRevisionSubmissionAccepted,
    args.onSubmissionCleared,
    args.onSendInitSettled,
    args.onWorkspaceSettingsTurnAccepted,
    args.onWorkspaceSettingsAccessDenied,
  );
  return args.mode === "mock" ? mock : sse;
}
