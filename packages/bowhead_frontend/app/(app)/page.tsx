"use client";

/**
 * Canvas Mode home page (v1.4) — UnifiedSidebar · Main workspace.
 *
 * User flow (PRD §21 / canvas-mode-design §7):
 *   1. Click "+" to create a session (clear runner, empty state).
 *   2. First question → meta_intent=canvas_init → new canvas + streaming sections.
 *   3. UnifiedSidebar immediately shows and highlights a "streaming…" entry (optimistic); bump a full refresh after turn done.
 *   4. Follow-up questions append; targeting a section patches it.
 */

import { citationFromPayload, mergeCitation } from "@/lib/citation-payload";
import { inputDocumentDisplayText, parseInputDocument, type InputDocument } from "@/lib/composer-input-document";
import { composerAgentHandoffAdmitted, takeComposerPrefill, useComposerAgentUseHandoff } from "@/lib/composer-handoff";
import { resolveAgentReferenceDocument } from "@/lib/agent-reference-document";
import { randomUuid } from "@/lib/random-id";
import { useAgentDraftUrl } from "@/lib/use-agent-draft-url";
import { useSessionSettleRefresh } from "@/lib/use-session-settle-refresh";
import { useSettingsCanvas } from "@/lib/use-settings-canvas";
import { WorkspaceSettingsCanvasProvider, useWorkspaceSettingsCanvasBridge } from "@/components/canvas/workspace-settings-canvas-context";
import { WorkspaceSettingsStageError } from "@/lib/workspace-settings-agent";
import { subscribeConfigurationCanvas, configurationCanvasVersion, configurationCanvasOpen } from "@/lib/configuration-canvas-events";
import { settingsCanvasKey, readSettingsCanvasTarget, readSettingsCanvasSelection } from "@/lib/settings-canvas-target";
import { resolveCanvasSelection, type CanvasSelection } from "@/lib/canvas-selection";
import { useDocumentCanvasObjects } from "@/lib/use-document-canvas-objects";
import { useCanvasDocumentJobs, documentJobActive, documentPublicationKey } from "@/lib/use-canvas-document-jobs";
import { documentOpenItems } from "@/components/canvas/document-open-items";
import { documentCanvasScopeKey, type DocumentCanvasScope } from "@/lib/document-canvas-navigation";
import { documentCanvasOpen, documentCanvasVersion, subscribeDocumentCanvas } from "@/lib/document-canvas-events";
import { subscribeDocumentCanvasRequests } from "@/lib/document-canvas-requests";
import { subscribeCoachOperationRequests } from "@/lib/coach-operation-requests";
import { subscribeCoachAlignmentContinue } from "@/lib/coach-alignment-cards";
import { COACH_AGENT_ID, composerAcceptsContinue, shouldSendStagedContinue } from "@/lib/coach-alignment-continue";
import { DocumentDraftCanvasObject, type DocumentDraftCanvasPublication } from "@/components/canvas/document-draft-canvas-object";
import { DocumentCanvasList } from "@/components/canvas/document-canvas-list";
import { ConfigurationCanvasHost, ConfigurationCanvasStrip, type ConfigurationCanvasObject } from "@/components/canvas/configuration-canvas-host";
import { CanvasOpenMenu, objectStatusMarker, type CanvasObjectMarker, type CanvasOpenDocuments, type CanvasOpenMenuModel, type CanvasSaveReport } from "@/components/canvas/object";
import { documentObjectStatus } from "@/components/canvas/document-draft-canvas-object";
import { SettingsPageCanvas } from "@/components/canvas/settings-page-canvas";
import { Notice } from "@/components/ui/notice";
import { manualSubagentPayload, reviewSubagentPayload } from "@/lib/subagent-create-flow";
import { ComposerReferencePreview } from "@/components/chat/composer-reference-preview";
import { ReferenceFollowUpQueue } from "@/components/chat/reference-follow-up-queue";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";

import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { BrandName } from "@/components/brand-identity";
import {
  fetchMe,
  revalidateMe,
  type ChatExecutionBootstrap,
} from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { AppShell } from "@/components/shell/AppShell";
import { PageStateHeading } from "@/components/shell/PageStateHeading";
import { Topbar } from "@/components/shell/Topbar";
import { HomeWorkSurface, homePageTitleKey } from "@/components/home/HomeWorkSurface";
import {
  V2Composer,
  type ComposerDraftControl,
  type ComposerDraftSubmissionContext,
  type ComposerDraftWriteProtocol,
} from "@/components/chat/v2-composer";
import { ComposerRecoveryNotice } from "@/components/chat/composer-recovery-notice";
import { CreateToolCard } from "@/components/canvas/sections/create-tool-card";
import { useConnectorPresets } from "@/lib/use-connector-presets";
import { CreateSubagentCard } from "@/components/canvas/sections/create-subagent-card";
import { SubagentManualDraftBootstrap } from "@/components/canvas/sections/subagent-manual-draft-bootstrap";
import { SubagentDraftEntry } from "@/components/canvas/sections/subagent-draft-entry";
import { type ManualSubagentDraftDetail } from "@/lib/subagent-drafts-api";
import { CoachResourceEditor } from "@/components/canvas/coach-resource-editor";
import { captureCoachCanvasBinding, CoachCanvasNotReadyError } from "@/lib/coach-canvas-target";
import { agentEditorsToReload, useCoachResourceEditors } from "@/lib/use-coach-resource-editors";
import { coachResourceOpens, coachResourcesVersion, subscribeCoachResources } from "@/lib/coach-resource-editors";
import { CoachOperationView, type CoachOperationTarget } from "@/components/canvas/coach-operation-view";
import { useAppRouter } from "@/lib/use-app-router";
import { agentCaseProposalPath } from "@/lib/agent-case-proposal-path";
import { CoachReceiptScopeContext } from "@/lib/coach-receipt-scope";
import { maySelectCoachOperation, type CoachOperationIntent } from "@/lib/coach-operation-selection";
import { useCoachOperationViews } from "@/lib/use-coach-operation-views";
import { coachOperationOpens, coachOperationsVersion, subscribeCoachOperations, type CoachOperationOpen } from "@/lib/coach-operations";
import { useAgentCanvasObjects } from "@/lib/use-agent-canvas-objects";
import { AgentDraftCanvasObject } from "@/components/canvas/sections/agent-draft-canvas-object";
import { SubagentIntentCard } from "@/components/canvas/sections/subagent-intent-card";
import { CreateWorkflowCard } from "@/components/canvas/sections/create-workflow-card";
import { ConfigDrawer } from "@/components/config-copilot/config-drawer";
import { subscribeOpenDraft } from "@/lib/config-copilot-draft";
import { ChatWorkbench } from "@/components/chat-workbench";
import { EntitlementBanner } from "@/components/chat/entitlement-banner";
import { SendInitErrorBanner } from "@/components/chat/send-init-error-banner";
import { SessionLoadingSkeleton } from "@/components/chat/session-loading-skeleton";
import { useEntitlement } from "@/lib/entitlement-store";
import {
  resolveLiveClarifications,
  readHydratedClarificationAuthority,
} from "@/lib/hydrate-clarifications";
import {
  reconcileHydratedErrorAfterDone,
  resolveHydratedTurnError,
} from "@/lib/turn-hydration-error";
import {
  reconcileRunnerSessionSync,
  shouldIgnoreRunnerSessionOnReset,
} from "@/lib/runner-session-sync";
import {
  cleanUrlNavigationOutcome,
  isExternalCleanUrlNavigation,
  resolveUrlSessionNavAgainstLocation,
  syncActiveSessionSearchParams,
  urlHasPendingForeignSession,
  urlSessionTargetKey,
  withoutNextRouterHistoryMarkers,
} from "@/lib/url-session-nav";
import { withoutResultsHistoryState } from "@/lib/results-navigation-state";
import {
  archiveSession,
  cancelTurn,
  deleteSession,
  undeleteSession,
  fetchCanvasBySession,
  fetchSessionChatExecutionPreflight,
  fetchSessionDetail,
  fetchTurnEvents,
  fetchTurnEventsEnvelope,
  getCancelTurnOutcome,
  markSessionSeen,
  pinSession,
  ReportUnavailableError,
  restoreSession,
  SessionDetailError,
  type SessionDetailDTO,
  type TurnEventDTO,
  unpinSession,
} from "@/lib/api";
import { terminalErrorDiagnostics } from "@/lib/chat-failure-diagnostics";
import { reportSectionsSavedFromDone } from "@/lib/report-sections-saved";
import {
  shouldApplyComposerCancellation,
  shouldClearComposerCancellation,
} from "@/lib/composer-cancel-outcome";
import { ReportUnavailable } from "@/components/share/report-unavailable";
import { dismissPlan, runningPlanIds } from "@/lib/plan-store";
import { getCanvasSnapshot, hydrateCanvas } from "@/lib/canvas-store";
import {
  normalizeCanvasFailureReasonCode,
  normalizeReasonCode,
} from "@/lib/error-messages";
import { parseRecoveryAction } from "@/lib/turn-feedback";
import {
  normalizeErrorDiagnostics,
  parseErrorDiagnosticsWire,
} from "@/lib/error-presentation";
import {
  replayTurnEventRows,
  type ReplayTurnEventsResult,
} from "@/lib/turn-store-replay";
import { confirmCanvasNavigation } from "@/lib/canvas-navigation-guard";
import { getSteerTarget } from "@/lib/steer-guard";
// prod-bug-bash-038 #9: hydrate archivedTurns from /turns/{id}/events replay
import { getTurnSnapshot, failActiveSteps } from "@/lib/turn-store";
import { resolveUserMessageTimeMs } from "@/lib/turn-user-time";
import type { ArchivedTurn } from "@/lib/types";

// prod-bug-bash-039 P1-2: inline concurrency limiter (no new dep). Caps parallel
// per-turn event hydration to a browser-friendly limit (~6).
// Long sessions (30+ turns) won't pile up behind browser's HTTP/1.1 6-per-origin
// ceiling; sequential-fallback keeps hydrate latency bounded.
function _makeLimit(n: number) {
  const queue: (() => void)[] = [];
  let active = 0;
  const next = () => { active--; queue.shift()?.(); };
  return <T,>(fn: () => Promise<T>): Promise<T> => new Promise((resolve, reject) => {
    const run = () => { active++; fn().then(resolve, reject).finally(next); };
    if (active < n) run(); else queue.push(run);
  });
}
const _hydrateLimit = _makeLimit(6);
import { sessionTurnOrdinals, type TurnCaptionInput } from "@/lib/turn-counter";
import { SessionTurnOrdinalsContext } from "@/lib/session-turn-ordinals";
import {
  formatTurnDuration,
  resolveHydratedTurnDurationMs,
} from "@/lib/turn-duration";
import { SessionTopbarMenu } from "@/components/chat/session-topbar-menu";
import { SessionRenameDialog } from "@/components/sidebar/session-rename-dialog";
import {
  renameInitialTitle,
  sessionRenameErrorKey,
  submitSessionRename,
  useSessionTitleOverrides,
} from "@/lib/session-rename";
import { resolveSessionTopbarTitle, sessionTopbarMenuTarget } from "@/lib/session-topbar-title";
import { pickScenarioFromText, pickScenarioFromUrl } from "@/lib/scenarios";
import { notifySessionListChanged } from "@/lib/session-list-events";
import { useSessionList } from "@/lib/use-sessions";
import { olderSessionsPaging } from "@/components/sidebar/older-sessions-row";
import {
  captureComposerSubmissionWireSnapshot,
  useTurnRunner,
  type ArchivedTurnsHydrationPatch,
  type CurrentTurnHydrationPatch,
  type UseTurnRunnerArgs,
} from "@/lib/use-turn-runner";
import { useContextMaintenance } from "@/lib/use-context-maintenance";
import { trackComposerTransportFailure, trackInteraction } from "@/lib/metrics";
import {
  clearCancelRequest,
  compareAndSetCancelRequestState,
  getOrCreateCancelRequest,
  listPendingCancelRequests,
  type PendingCancelRequest,
} from "@/lib/composer-cancel-recovery";
import { toast } from "@/lib/use-toast";
import { useCopyDiagnosticsToastAction } from "@/components/error-notice";
import { clearSessionLoadFailure, sessionLoadFailedKey, showSessionLoadFailure } from "@/lib/session-load-error";
import {
  abandonPendingSubmission,
  captureReferenceSubmissionDraft,
  clearPendingSubmission,
  ComposerSubmissionFenceUnresolvedError,
  preparePendingReferenceSubmission,
  preparePendingSubmission,
  readPendingSubmission,
  readPendingReferenceSubmission,
  type RecoveredReferenceSubmission,
  reconcileStaleSubmissionFence,
  type RecoveredSubmission,
  type ComposerSubmissionWireSnapshot,
  type SubmissionRecoveryScope,
} from "@/lib/composer-submission-recovery";
import { ComposerRecoveryUnavailableError } from "@/lib/composer-local-persistence-db";
import {
  canonicalComposerStartSessionId,
  createComposerChatAction,
  isDefinitiveComposerChatActionRejection,
  lookupComposerChatAction,
  type ComposerChatAction,
} from "@/lib/composer-chat-action";
import {
  canonicalizeComposerAttachmentRefs,
  clearAcceptedComposerReferenceDraft,
  clearAcceptedComposerDocumentRevisionDraft,
  clearComposerOrdinaryDraft,
  clearComposerInputDraft,
  readComposerInputDraft,
  ComposerOrdinaryDraftInvalidError,
  prepareComposerPendingAction,
  readComposerOrdinaryDraft,
  replaceComposerPendingAction,
  settleComposerPendingAction,
  writeComposerOrdinaryDraft,
  type ComposerDraftScope,
  type ComposerOrdinaryDraft,
} from "@/lib/composer-ordinary-draft";
import { sameDocumentRevisionTarget, type DocumentRevisionTarget } from "@/lib/composer-document-revision";
import { composerDraftScopeSignature } from "@/lib/composer-persistence-scope";
import {
  clearAllComposerTransientDrafts,
  clearComposerTransientDraft,
} from "@/lib/composer-transient-draft";
import {
  chatReplayAdmission,
  legacyChatActionAttachmentsSupported,
  maySkipChatExecutionPreflight,
  shouldRouteSecondaryChatEntryThroughComposer,
  terminalChatActionAttachmentsSupported,
} from "@/lib/terminal-chat-capability";
import { restoreActiveSpace, useSpace } from "@/lib/use-space";
import { requestCanvasResultOpen } from "@/lib/canvas-workspace-focus";
import type {
  AttachmentRef,
  ChatMessage,
  CreateSubagentOpenPayload,
  IntentType,
  SessionGroup,
  SessionItem,
} from "@/lib/types";

const SSE_MODE = (process.env.NEXT_PUBLIC_BOWHEAD_SSE_MODE ?? "mock") as "sse" | "mock";
const FALLBACK_WORKSPACE = process.env.NEXT_PUBLIC_BOWHEAD_WORKSPACE_ID ?? "1";
type CanvasBySessionDTO = Awaited<ReturnType<typeof fetchCanvasBySession>>;
type PendingChatActionController = {
  action: ComposerChatAction;
  message: string;
  draftMessage?: string;
  attachments: AttachmentRef[];
  workspaceId: string;
  persistenceScope: ComposerDraftScope | null;
  fallbackDraftScope?: ComposerDraftScope;
  recoveryState?:
    | "lookup_pending"
    | "resend_allowed"
    | "accepted_unsettled"
    | "rejected_unsettled";
  scopeKey: string;
};
type ChatActionSettlementResult = "settled" | "deferred" | "superseded";

function composerDraftSnapshotsMatch(
  left: Pick<ComposerOrdinaryDraft, "message" | "attachments">,
  right: Pick<ComposerOrdinaryDraft, "message" | "attachments">,
): boolean {
  return JSON.stringify({
    message: left.message,
    attachments: left.attachments,
  }) === JSON.stringify({
    message: right.message,
    attachments: right.attachments,
  });
}

// chat-hydrate-overhaul: revive backend turn DTO → FE runner state.
// chat-polish-r3 Phase 1.5: _toFETrace + _BETraceStep retired. F5 hydrate
// now replays /api/v1/turns/{id}/events through lib/turn-store-replay,
// populating the same turn-store the live SSE path writes into. The
// legacy `lastTraceSteps` payload in setHydrate is gone (see TurnContainer
// reading from turn-store via useTurn / getTurnSnapshot).
type _BETurnSummary = NonNullable<import("@/lib/api").SessionDetailDTO["turns"][number]["summary"]>;
type _BETurn = import("@/lib/api").SessionDetailDTO["turns"][number];
/** chat-v4 hydrate: pull turn events and extract the 4 v4 slot payloads.
 *  Completed historical turns don't reconnect SSE (use-turn-runner.ts:370+),
 *  so backend-emitted v4 events otherwise never reach runner state on session
 *  refresh. This helper backfills from `/api/v1/turns/{id}/events`. */
type HydratedTurnProjection = {
  subagentSummaries: Record<string, unknown>;
  finalPayload: unknown;
  responseKind: "chat" | "canvas" | null;
  toolInvocationCount: number;
  /** Last done's report_sections_saved (lib/report-sections-saved.ts, same condition as live). */
  reportSectionsSaved: number | null;
  /** F1 clarification: hydrate path also pulls clarification_required so HITL
   *  option buttons render on session refresh. */
  clarifications: unknown[];
  /** prod-bug-bash-039 P1-1: scrape terminal-failure events so ArchivedTurnView
   *  renders FinalFailBanner for failed prior turns (was: failed turn rendered
   *  as success on session reload because errorReason came from runner only). */
  errorReason: import("@/lib/types").SseErrorReason | null;
  /** prod-bug-bash-039 P1-1: scrape headline event so archived turn shows the
   *  Chinese introduction above the agent timeline. */
  headline: string | undefined;
  /** prod-bug-bash-039 Phase 5 (0.39.3): scrape turn.citation_registered events
   *  so ArchivedTurnView's CitationsProvider can resolve `[cite:N]` chips. */
  citations: import("@/lib/types").Citation[];
  /** Last generation start and last legacy done are needed to reconstruct a
   * terminal-protocol synthetic Stop on F5. */
  lastExecutionStartedAt: string | null;
  lastPersistedDoneStatus: string | null;
};

function _blankHydratedTurnProjection(): HydratedTurnProjection {
  return {
    subagentSummaries: {},
    finalPayload: undefined,
    responseKind: null,
    toolInvocationCount: 0,
    reportSectionsSaved: null,
    clarifications: [],
    errorReason: null,
    headline: undefined,
    citations: [],
    lastExecutionStartedAt: null,
    lastPersistedDoneStatus: null,
  };
}

async function _fetchLastFromTurn(
  turnId: string,
  prefetchedEvents?: TurnEventDTO[],
  generation?: number | null,
): Promise<HydratedTurnProjection> {
  // Sprint phase-plan-removal: v4TurnPlan + v4PhaseUpdates extracted slots removed.
  const blank = _blankHydratedTurnProjection();
  try {
    const events = prefetchedEvents ?? await fetchTurnEvents(turnId);
    const summaries: Record<string, unknown> = {};
    let final: unknown = undefined;
    let responseKind: "chat" | "canvas" | null = null;
    let toolInvocationCount = 0;
    let reportSectionsSaved: number | null = null;
    const clarifications: unknown[] = [];
    // A2 (clarification-reload-hardening): track which clarifications were
    // resolved and the turn's terminal status, so a reload doesn't re-seed the
    // HITL card for a clarification the user already answered/abandoned. Mirrors
    // the live SSE reducer (use-turn-runner.ts:1556) + backend
    // _replay_rows_for_stream — a clarification re-renders ONLY while the turn is
    // still genuinely paused on it.
    const resolvedClarIds = new Set<string>();
    let lastDoneStatus: string | null = null;
    let lastExecutionStartedAt: string | null = null;
    let errorReason: import("@/lib/types").SseErrorReason | null = null;
    let headline: string | undefined;
    const citationsSeen = new Map<number, import("@/lib/types").Citation>();
    for (const e of events) {
      const p = e.payload ?? {};
      switch (e.event_type) {
        case "turn_started": {
          const startedAt = (p as Record<string, unknown>).started_at;
          if (typeof startedAt === "string" && Number.isFinite(Date.parse(startedAt))) {
            lastExecutionStartedAt = startedAt;
          }
          break;
        }
        case "lead.subagent_summary": {
          const k = String((p as Record<string, unknown>).spawn_id
            ?? (p as Record<string, unknown>).subagent_id ?? "");
          if (k) summaries[k] = p;
          break;
        }
        case "lead.final.payload":
          final = p; break;
        case "clarification_required":
        case "clarification.required":
          // F1: backfill clarification_required (ar2 + clarification_ask). On
          // live SSE this populates runner.clarifications via the existing
          // handler at use-turn-runner.ts:826 — hydrate path mirrors that.
          clarifications.push(p);
          break;
        case "clarification.resolved":
        case "clarification_resolved":
        // dogfood-0708 F1: dismiss ("换个问法") also counts as handled — it must not revive after refresh either.
        // Backend dismiss now emits clarification.dismissed (symmetric with resolved); include it in the same discard set.
        case "clarification.dismissed": {
          // A2: a resolved/dismissed clarification must not re-seed the card on
          // reload, even if the resumed pass later failed/aborted (no final payload).
          const rid = String((p as Record<string, unknown>).request_id ?? "");
          if (rid) resolvedClarIds.add(rid);
          break;
        }
        case "done": {
          // ν2 v2: response_kind is stamped on the done payload by gateway/Lead
          // (use-turn-runner.ts:748). Backfill here so TurnContainer's chat-shape gate
          // works for historical session reloads (hydrate path skips SSE).
          const rk = (p as Record<string, unknown>).response_kind;
          if (rk === "chat" || rk === "canvas") responseKind = rk;
          // tool_invocation_count distinguishes true prose (no tool) from
          // lookup-self-answer (1 BI call, Lead self-answers without peer →
          // response_kind=chat but still has data work). StepListCard should suppress
          // only when both flags say "no actual work".
          const tic = (p as Record<string, unknown>).tool_invocation_count;
          if (typeof tic === "number") toolInvocationCount = tic;
          // Report sections persisted in this turn: take the last done (same field and condition as live).
          reportSectionsSaved = reportSectionsSavedFromDone(p);
          // A2: remember the turn's last done status. clarification_pending = the
          // turn paused here; any terminal status = it moved on (resumed/failed/
          // dismissed) → no live clarification to re-render.
          const ds = (p as Record<string, unknown>).status;
          if (typeof ds === "string") {
            lastDoneStatus = ds;
            errorReason = reconcileHydratedErrorAfterDone(errorReason, ds);
          }
          // Chat turn feedback D5: the failed done carries the same recovery_action as turn_failed.
          if (errorReason?.source === "turn_failed" && !errorReason.recoveryAction) {
            const action = parseRecoveryAction((p as Record<string, unknown>).recovery_action);
            if (action) errorReason = { ...errorReason, recoveryAction: action };
          }
          break;
        }
        // prod-bug-bash-039 P1-1: terminal-failure events — mirror use-turn-runner
        // reducers at lines 1117-1170 so archived turn renders FinalFailBanner.
        case "turn_failed": {
          const pl = p as Record<string, unknown>;
          const reasonCode = normalizeReasonCode(
            pl.reason_code ?? pl.reason,
          );
          errorReason = {
            source: "turn_failed",
            reasonCode,
            diagnostics: terminalErrorDiagnostics(pl, reasonCode, turnId),
            recoveryAction: parseRecoveryAction(pl.recovery_action),
          };
          break;
        }
        case "canvas_op_failed": {
          const pl = p as Record<string, unknown>;
          const reasonCode = normalizeCanvasFailureReasonCode(
            pl.reason_code,
          );
          const parsed = parseErrorDiagnosticsWire(pl.diagnostics);
          errorReason = {
            source: "canvas_op_failed",
            reasonCode,
            diagnostics:
              parsed?.disclosure === "authenticated" &&
              parsed.diagnosticCode === "chat_canvas_failed" &&
              parsed.turnId === turnId
                ? normalizeErrorDiagnostics({
                    ...parsed,
                    code: reasonCode,
                  })
                : undefined,
            sectionId: pl.section_id != null ? String(pl.section_id) : undefined,
          };
          break;
        }
        case "headline": {
          const t = (p as Record<string, unknown>).text;
          if (typeof t === "string" && t.trim()) headline = t;
          break;
        }
        case "turn.citation_registered": {
          // prod-bug-bash-039 Phase 5 (0.39.3) — replay citation registry from
          // event log so archived turn's CitationsProvider can resolve [cite:N]
          // chips on reload. Mirror use-turn-runner.ts:1273-1296 reducer.
          //
          // IMPORTANT: read `citation_seq` (NOT `seq`) — `seq` is the bus
          // monotonic counter clobbered by OrchestratorEvent.to_sse (CTO Sec
          // #3 P0-1).
          // Review F4: share citationFromPayload with use-turn-runner's live mapping —
          // the two handwritten versions drifted (live rewrote web_clip as data_ref; hydrate preserved it).
          const parsed = citationFromPayload(p as Record<string, unknown>);
          if (!parsed) break;
          citationsSeen.set(parsed.seq, mergeCitation(citationsSeen.get(parsed.seq), parsed));
          break;
        }
      }
    }
    // sort by seq so consumer iteration matches registration order
    const citations = Array.from(citationsSeen.values()).sort(
      (a, b) => a.seq - b.seq,
    );
    // Which clarification cards revive on hydrate: only when the turn is still paused on that card. See the contract and full
    // history of pitfalls (including dogfood-0708 F1: paused done also stamps response_kind → legacy final/responseKind
    // proxy misclassified turnEnded → refresh lost the deck; lastDoneStatus is now authoritative) in lib/hydrate-clarifications.
    // Resumed execution must clear a stale clarification panel before rendering the final answer.
    const eventCandidates = resolveLiveClarifications({
      clarifications, resolvedClarIds, final, responseKind, lastDoneStatus,
    });
    let liveClarifications = eventCandidates;
    {
      try {
        const authority = await readHydratedClarificationAuthority(turnId, eventCandidates, lastDoneStatus, generation);
        liveClarifications = authority.clarifications;
        errorReason ??= authority.resumeFailure;
      } catch (error) {
        // Fail open only for the authority probe.  The Turn's already-folded
        // answer/headline/citations must survive a transient clarification
        // endpoint failure; live inverse events and stale-click healing remain.
        const status = (error as { status?: unknown })?.status;
        const code = (error as { code?: unknown })?.code;
        console.warn("clarification hydrate authority probe deferred", {
          turnId,
          status: typeof status === "number" ? status : undefined,
          code: typeof code === "string" ? code : undefined,
        });
      }
    }
    return {
      subagentSummaries: summaries,
      finalPayload: final,
      responseKind,
      toolInvocationCount,
      reportSectionsSaved,
      clarifications: liveClarifications,
      errorReason,
      headline,
      citations,
      lastExecutionStartedAt,
      lastPersistedDoneStatus: lastDoneStatus,
    };
  } catch {
    return blank;
  }
}

async function _hydrateTurnEventsOnce(
  turnId: string,
  { replayRows = true }: { replayRows?: boolean } = {},
): Promise<{
  replay: ReplayTurnEventsResult;
  projection: HydratedTurnProjection;
  effectDisclosure: import("@/lib/api").EffectDisclosureOverlayDTO | null;
  terminalProtocolVersion: number | null;
  currentGeneration: number | null;
}> {
  const envelope = await fetchTurnEventsEnvelope(turnId);
  const events = envelope.events;
  return {
    replay: replayRows
      ? replayTurnEventRows(turnId, events)
      : { turnId, eventCount: 0, claimed: 0 },
    projection: replayRows
      ? await _fetchLastFromTurn(turnId, events, envelope.current_generation)
      : _blankHydratedTurnProjection(),
    effectDisclosure: envelope.current_effect_disclosure ?? null,
    terminalProtocolVersion: envelope.terminal_protocol_version ?? null,
    currentGeneration: envelope.current_generation ?? null,
  };
}

function _toFESummary(
  s: _BETurnSummary | undefined | null,
  terminal?: {
    status?: string | null;
    endedAt?: string | null;
    projection?: HydratedTurnProjection | null;
  },
): import("@/lib/types").TurnSummary | null {
  const totalMs = resolveHydratedTurnDurationMs({
    summaryTotalMs: s?.total_ms,
    terminalStatus: terminal?.status,
    lastPersistedDoneStatus: terminal?.projection?.lastPersistedDoneStatus,
    lastExecutionStartedAt: terminal?.projection?.lastExecutionStartedAt,
    endedAt: terminal?.endedAt,
  });
  if (!s && totalMs === null) return null;
  return {
    total: totalMs === null ? "—" : formatTurnDuration(totalMs),
    cost: s?.cost_text ?? "",
    tools: s?.tools_count ?? 0,
    llm: s?.llm_count ?? 0,
    tokensInput: s?.tokens_input ?? undefined,
    tokensOutput: s?.tokens_output ?? undefined,
    usagePartial: s?.usage_partial ?? false,
  };
}

function _toArchivedTurn(
  turn: _BETurn,
  hydrated: Awaited<ReturnType<typeof _hydrateTurnEventsOnce>> | null,
  { disclosureReadPending = false }: { disclosureReadPending?: boolean } = {},
): ArchivedTurn {
  const snapshot = getTurnSnapshot(turn.id);
  const projection = hydrated?.projection ?? null;
  return {
    turnId: turn.id,
    operationKind: turn.operation_kind,
    terminalStatus: turn.status,
    executionGeneration:
      hydrated?.currentGeneration ?? turn.execution_generation ?? 1,
    terminalProtocolVersion:
      hydrated?.terminalProtocolVersion
      ?? turn.terminal_protocol_version
      ?? null,
    effectDisclosure: hydrated?.effectDisclosure ?? null,
    effectDisclosureDiscoveryPending: Boolean(
      disclosureReadPending && turn.terminal_protocol_version === 1,
    ),
    user: turn.user_text ?? "",
    interrupted: turn.status === "failed" && turn.error_kind === "interrupted",
    userAttachments: (turn.attachments ?? []).map((attachment) => ({
      fileId: attachment.file_id,
      filename: attachment.filename,
      size: attachment.size,
      mime: attachment.mime,
      ...(attachment.selected_context ? { selectedContext: attachment.selected_context } : {}),
      ...(attachment.reference_kind === "result"
        ? { referenceKind: "result" as const }
        : {}),
      ...(attachment.bundle_artifact_ids?.length
        ? { bundleArtifactIds: attachment.bundle_artifact_ids }
        : {}),
    })),
    answer: turn.final_answer ?? "",
    traceSteps: [],
    peersDispatched: [],
    errorReason: resolveHydratedTurnError(
      projection?.errorReason ?? null,
      turn.status,
      turn.error_kind,
    ),
    responseKind: projection?.responseKind ?? undefined,
    toolFindings: undefined,
    headline: projection?.headline,
    subagentSummaries: projection?.subagentSummaries ?? undefined,
    finalPayload: projection?.finalPayload,
    hasDispatched: (snapshot?.steps?.length ?? 0) > 0,
    userMsgId: null,
    beats: snapshot?.beats ?? [],
    citations: projection?.citations ?? [],
    archivedAt: Date.parse(turn.ended_at ?? turn.started_at),
    // User bubbles display submission time rather than ended_at (observed on 116:「07:46 发送 → 显示 07:53」).
    userSentAt: resolveUserMessageTimeMs({
      firstTurnStartedAt: snapshot?.startedAt ?? null,
      submittedAtMs: Date.parse(turn.started_at),
    }),
  };
}

function _toAttachmentRef(
  attachment: NonNullable<_BETurn["attachments"]>[number],
): AttachmentRef {
  return {
    fileId: attachment.file_id,
    filename: attachment.filename,
    size: attachment.size,
    mime: attachment.mime,
    ...(attachment.selected_context ? { selectedContext: attachment.selected_context } : {}),
    ...(attachment.reference_kind === "result"
      ? { referenceKind: "result" as const }
      : {}),
    ...(attachment.bundle_artifact_ids?.length
      ? { bundleArtifactIds: attachment.bundle_artifact_ids }
      : {}),
  };
}

function _buildSessionHydrationBase(
  detail: import("@/lib/api").SessionDetailDTO,
  sessionId: string,
): {
  payload: NonNullable<UseTurnRunnerArgs["hydrate"]> & {
    lastIntent: IntentType | null;
  };
  lastTurn: _BETurn | null;
  historyTurns: _BETurn[];
} {
  const lastTurn = detail.turns.length
    ? detail.turns[detail.turns.length - 1]
    : null;
  const hasResume = Boolean(lastTurn && lastTurn.status === "running");
  const lastTurnRouted = Boolean(lastTurn && !hasResume);
  const historyTurns = (hasResume || lastTurnRouted)
    ? detail.turns.slice(0, -1)
    : detail.turns;
  const messages: ChatMessage[] = [];
  for (const turn of historyTurns) {
    if (turn.operation_kind === "context_maintenance") continue;
    const attachments = turn.attachments ?? [];
    if (turn.user_text || attachments.length > 0) {
      messages.push({
        role: "user",
        content: turn.user_text,
        turnId: turn.id,
        createdAt: turn.started_at,
        ...(attachments.length
          ? { attachments: attachments.map(_toAttachmentRef) }
          : {}),
      });
    }
    if (turn.final_answer) {
      messages.push({
        role: "assistant",
        content: turn.final_answer,
        turnId: turn.id,
        createdAt: turn.ended_at ?? turn.started_at,
        agentType: turn.agent_type ?? null,
      });
    }
  }
  const projection = _blankHydratedTurnProjection();
  return {
    lastTurn,
    historyTurns,
    payload: {
      sessionId,
      sessionTitle: detail.session.title,
      messages,
      archivedTurns: historyTurns.map((turn) => _toArchivedTurn(turn, null)),
      lastIntent: (lastTurn?.intent as IntentType | null) ?? null,
      lastSummary: _toFESummary(lastTurn?.summary, {
        status: lastTurn?.status,
        endedAt: lastTurn?.ended_at,
        projection,
      }),
      lastTurnId: lastTurn?.id ?? null,
      lastOperationKind: lastTurn?.operation_kind ?? null,
      lastStartedAt: lastTurn ? Date.parse(lastTurn.started_at) : null,
      lastTerminalStatus: lastTurn?.status ?? null,
      lastExecutionGeneration: lastTurn?.execution_generation ?? null,
      lastTerminalProtocolVersion:
        lastTurn?.terminal_protocol_version ?? null,
      lastEffectDisclosureDiscoveryPending: Boolean(
        lastTurn && lastTurn.terminal_protocol_version === 1,
      ),
      lastTurnRouted,
      lastResponseKind: projection.responseKind,
      lastToolInvocationCount: projection.toolInvocationCount,
      lastReportSectionsSaved: projection.reportSectionsSaved,
      lastClarifications: projection.clarifications,
      lastErrorReason: lastTurnRouted
        ? resolveHydratedTurnError(
            projection.errorReason,
            lastTurn?.status,
            lastTurn?.error_kind,
          )
        : null,
      lastUserText: lastTurnRouted ? lastTurn!.user_text : null,
      lastUserAttachments: lastTurnRouted
        ? (lastTurn!.attachments ?? []).map(_toAttachmentRef)
        : [],
      lastAnswer: lastTurnRouted ? (lastTurn!.final_answer ?? "") : null,
      resumeTurn: hasResume && lastTurn
        ? {
            turnId: lastTurn.id,
            executionGeneration: lastTurn.execution_generation ?? 1,
            userText: lastTurn.user_text,
            userAttachments: (lastTurn.attachments ?? []).map(_toAttachmentRef),
          }
        : null,
    },
  };
}
// memberUid is no longer hardcoded on the frontend: Gateway resolves it dynamically by (identity × workspace_id). Keep env
// as an override channel (explicitly set in tests or to pin a particular member).
const MEMBER_OVERRIDE = process.env.NEXT_PUBLIC_BOWHEAD_MEMBER_UID ?? "";

function CanvasHomeInner() {
  const ts = useTranslations("session");
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  const tSessionMenu = useTranslations("session");
  // Session rename from the top bar ⋯ menu; optimistic titles from any surface show here too.
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const sessionTitleOverrides = useSessionTitleOverrides();
  const locale = useLocale();
  const tSidebar = useTranslations("sidebar");
  const tHome = useTranslations("home");
  const tComposer = useTranslations("composer");
  const tCreateAgent = useTranslations("create_subagent");
  const tMarketplace = useTranslations("agents_marketplace");
  const tSettingsCanvas = useTranslations("settings_canvas");
  const tCoachResourceEditor = useTranslations("coach_resource_editor");
  const tCoachOperation = useTranslations("coach_operation");
  const tDocuments = useTranslations("documents");
  const tCanvasObject = useTranslations("canvas_object");
  const tChat = useTranslations("chat");
  const settingsBridge = useWorkspaceSettingsCanvasBridge();
  const tSubagents = useTranslations("settings.subagents");
  const agentUseUnavailableText = tCreateAgent("agent_use_unavailable");
  const agentUseDraftPreservedText = tCreateAgent("agent_use_draft_preserved");
  const authSession = useOptionalAuthSession();
  // #13 notification deep link: observe ?session= changes after soft navigation through this (outer layer already wraps Suspense).
  const searchParams = useSearchParams();
  const router = useAppRouter();
  // ② (bug 0708): picking a session from the rail (or restoring from ?session= on
  // refresh) runs a chain of network round-trips (session detail → canvas → turn
  // replay → hydrate) with no UI feedback — the main area sat blank / on stale
  // content until hydrate landed. Track the session being loaded → loading skeleton.
  // Cleared by a readiness effect (below) once the runner actually reflects the new
  // session, NOT in the fetch handler — clearing there dropped the skeleton one
  // render before runner.started flipped, flashing the home hero / previous session.
  const [loadingSid, setLoadingSid] = useState<string | null>(null);
  const sessionLoading = loadingSid !== null;

  // Space: fetch from /api/v1/auth/me/workspaces (canonical Bowhead ws_*) + persist active ID in localStorage.
  const workspace = useSpace();
  // Bug fix: stop silently falling back to "1" when activeId is null (race / new user / cleared localStorage).
  // Empty string → useTurnRunner warning + Gateway 400, requiring the UI to select a Space explicitly.
  // Still allow NEXT_PUBLIC_BOWHEAD_WORKSPACE_ID env as a development fallback (CI / scripts).
  const activeWorkspaceId =
    workspace.activeId
    ?? (typeof process !== "undefined" ? process.env.NEXT_PUBLIC_BOWHEAD_WORKSPACE_ID : null)
    ?? "";

  // W12-5: the legacy "bowhead:canvas-refresh-needed" global event listener moved to
  // the canvas-store.refreshCanvas() store action — see lib/canvas-store.ts.
  // Callers (report-view.tsx) import and call it directly, no longer using window events.

  // Session ID ref (let the effect above access the latest value; avoid stale closures).
  const activeSessionIdRef = useRef<string | null>(null);

  // Each +1 → useSessions refetches /api/v1/sessions.
  const [sessionsRefreshKey, setSessionsRefreshKey] = useState(0);
  const remoteSessions = useSessionList(
    sessionsRefreshKey,
    true,
    activeWorkspaceId,
  );
  const remoteGroups = remoteSessions.groups;

  const [banner, setBanner] = useState<string | null>(null);
  // Unsaved edits in a hosted Coach editor blocked a send: the message stays in the composer until the Owner
  // saves (then it is sent again through the ordinary Send path) or discards the edits.
  const [canvasSaveBlock, setCanvasSaveBlock] = useState<{ key: string; incarnation: string } | null>(null);
  const [canvasSaveBusy, setCanvasSaveBusy] = useState<"save" | "discard" | null>(null);
  const [canvasSaveFailed, setCanvasSaveFailed] = useState(false);
  const [composerSubmitRequest, setComposerSubmitRequest] = useState(0);
  // licensing §13.2 — entitlement health (X-Entitlement-Health / 402-403 deny code), drives
  // the <EntitlementBanner/> strip below the nav + the composer read-only treatment.
  const entitlement = useEntitlement();
  const sendReadOnlyRef = useRef(false);
  const lastTurnId = useRef<string | null>(null);
  useEffect(() => {
    if (!banner) return;
    const t = window.setTimeout(() => setBanner(null), 4000);
    return () => window.clearTimeout(t);
  }, [banner]);

  const [scenarioId, setScenarioId] = useState<string | null>(null);
  const [replayKey, setReplayKey] = useState(0);

  const [message, setMessage] = useState<string | null>(null);
  const [pendingAttachments, setPendingAttachments] = useState<AttachmentRef[]>([]);
  const [pendingSendWireSnapshot, setPendingSendWireSnapshot] = useState<ComposerSubmissionWireSnapshot | null>(null);
  const [pendingReplaySourceTurnId, setPendingReplaySourceTurnId] = useState<string | null>(null);
  const [sendTick, setSendTick] = useState(0);
  const [sendAttemptKey, setSendAttemptKey] = useState<string | null>(null);
  const sendAttemptSequenceRef = useRef(0);
  const sendInitResolversRef = useRef(new Map<string, (accepted: boolean) => void>());
  const sendChatInFlightRef = useRef(false);
  const sendSteerInFlightRef = useRef(false);
  const settleSendInit = (attemptKey: string, accepted: boolean): boolean => {
    const resolve = sendInitResolversRef.current.get(attemptKey);
    if (!resolve) return false;
    sendInitResolversRef.current.delete(attemptKey);
    resolve(accepted);
    return true;
  };
  // Current session ID; SSE fills it after the first message; follow-ups use it so backend appends to the same session.
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const composerSubmissionProtocolEnabled = Boolean(
    authSession?.me?.capabilities?.composer?.submission_protocol === true
    && authSession.me.id
    && authSession.me.team_id
    && authSession.me.auth_session_generation
    && activeWorkspaceId,
  );
  const referenceInputEnabled = authSession?.me?.capabilities?.composer?.reference_input === true;
  const [referenceQueueRefresh, setReferenceQueueRefresh] = useState(0);
  const [pendingReferenceDocument, setPendingReferenceDocument] = useState<InputDocument | null>(null);
  // The structured input of this page's last send, for Replay. Unlike `pendingReferenceDocument` it survives the
  // draft scope moving from "new chat" to the session that send created; only opening another session or starting
  // new work drops it.
  const [lastSentReferenceDocument, setLastSentReferenceDocument] = useState<InputDocument | null>(null);
  const [pendingReferenceSourceRevision, setPendingReferenceSourceRevision] = useState<string | null>(null);
  const referenceRecoveryCleanups = useRef(new Map<string, {
    scope: ComposerDraftScope;
    revision: string;
    inputDocument: InputDocument;
    attachments: AttachmentRef[];
  }>());
  const documentRevisionCleanups = useRef(new Map<string, { scope: ComposerDraftScope; pending: RecoveredSubmission | null }>());
  const cancelTeamId = authSession?.me?.team_id ?? null;
  const cancelUserId = authSession?.me?.id ?? null;
  const submissionRecoveryScope = useMemo<SubmissionRecoveryScope | null>(() => {
    const me = authSession?.me;
    if (
      !me
      || !me.auth_session_generation
      || !activeWorkspaceId
    ) {
      return null;
    }
    return {
      userId: me.id,
      teamId: me.team_id,
      workspaceId: activeWorkspaceId,
      requestedSessionId: activeSessionId,
      authSessionGeneration: me.auth_session_generation,
    };
  }, [
    activeSessionId,
    activeWorkspaceId,
    authSession?.me?.auth_session_generation,
    authSession?.me?.id,
    authSession?.me?.team_id,
  ]);
  const [recoveredSubmission, setRecoveredSubmission] =
    useState<RecoveredSubmission | null>(null);
  // The journal is written before POST so an interrupted send can be recovered.
  // It is not a recovery problem while that same POST is still in flight.
  const [submissionOutcomeUnknown, setSubmissionOutcomeUnknown] = useState(false);
  const [referenceSubmissionPreview, setReferenceSubmissionPreview] = useState<{
    scopeKey: string;
    draft: RecoveredReferenceSubmission;
  } | null>(null);
  const recoveredSubmissionRef = useRef<RecoveredSubmission | null>(null);
  recoveredSubmissionRef.current = recoveredSubmission;
  const [submissionRecoveryChecked, setSubmissionRecoveryChecked] = useState(false);
  const submissionProtocolEnabled = Boolean(
    composerSubmissionProtocolEnabled || recoveredSubmission !== null,
  );
  // Ordinary drafts are a local Composer concern, independent from the retired
  // submission journal protocol. Keeping this scope behind
  // `submissionProtocolEnabled` disabled F5 draft recovery for every fresh
  // browser because that capability intentionally defaults to false.
  const composerDraftPersistenceScope = submissionRecoveryScope;
  const composerDraftScopeKey = composerDraftScopeSignature(
    composerDraftPersistenceScope,
  );
  const composerDraftScopeKeyRef = useRef(composerDraftScopeKey);
  composerDraftScopeKeyRef.current = composerDraftScopeKey;
  useEffect(() => () => {
    clearAllComposerTransientDrafts();
  }, [authSession?.me?.auth_session_generation, authSession?.me?.id]);
  const [chatActionAttempt, setChatActionAttempt] =
    useState<PendingChatActionController | null>(null);
  const chatActionAttemptRef = useRef<PendingChatActionController | null>(null);
  chatActionAttemptRef.current = chatActionAttempt;
  const [recoveredChatAction, setRecoveredChatAction] =
    useState<PendingChatActionController | null>(null);
  const recoveredChatActionRef = useRef<PendingChatActionController | null>(null);
  recoveredChatActionRef.current = recoveredChatAction;
  const [draftReconcileEpoch, setDraftReconcileEpoch] = useState(0);
  const inMemoryChatActionsRef = useRef(
    new Map<string, PendingChatActionController>(),
  );
  // Presentation state is scope-local and may be cleared while navigating.
  // This registry survives that navigation only until the one network attempt
  // settles, preventing an old marker from being replaced mid-flight.
  const inFlightChatActionsRef = useRef(
    new Map<string, PendingChatActionController>(),
  );
  const finishedChatActionIdsRef = useRef(new Set<string>());
  const actionSettlementInFlightRef = useRef(
    new Map<string, Promise<ChatActionSettlementResult>>(),
  );
  const acceptedStartRefreshActionsRef = useRef(new Set<string>());
  const notifyAcceptedStart = (
    controller: PendingChatActionController,
  ): void => {
    if (controller.action.actionKind !== "start") return;
    const actionId = controller.action.actionId;
    if (acceptedStartRefreshActionsRef.current.has(actionId)) return;
    acceptedStartRefreshActionsRef.current.add(actionId);
    setSessionsRefreshKey((key) => key + 1);
  };
  const adoptCanonicalPendingDraft = (
    controller: PendingChatActionController,
  ): void => {
    if (finishedChatActionIdsRef.current.has(controller.action.actionId)) return;
    const previous = inMemoryChatActionsRef.current.get(controller.scopeKey);
    if (
      previous
      && previous.action.actionId !== controller.action.actionId
    ) {
      finishChatActionController(previous);
    }
    inMemoryChatActionsRef.current.set(controller.scopeKey, controller);
    if (controller.scopeKey === composerDraftScopeKeyRef.current) {
      recoveredChatActionRef.current = controller;
      setRecoveredChatAction(controller);
      setDraftReconcileEpoch((epoch) => epoch + 1);
    }
  };
  const settlePresentedChatAction = (
    actionId: string,
    reconcilePresentedDraft = true,
  ): void => {
    let currentScopeSettled = false;
    if (chatActionAttemptRef.current?.action.actionId === actionId) {
      currentScopeSettled = chatActionAttemptRef.current.scopeKey
        === composerDraftScopeKeyRef.current;
      chatActionAttemptRef.current = null;
      setChatActionAttempt(null);
    }
    if (recoveredChatActionRef.current?.action.actionId === actionId) {
      currentScopeSettled = currentScopeSettled
        || recoveredChatActionRef.current.scopeKey
          === composerDraftScopeKeyRef.current;
      recoveredChatActionRef.current = null;
      setRecoveredChatAction(null);
    }
    if (currentScopeSettled && reconcilePresentedDraft) {
      setDraftReconcileEpoch((epoch) => epoch + 1);
    }
  };
  const retainChatActionForRecovery = (
    controller: PendingChatActionController,
    recoveryState: NonNullable<PendingChatActionController["recoveryState"]>,
  ): PendingChatActionController => {
    if (finishedChatActionIdsRef.current.has(controller.action.actionId)) {
      return controller;
    }
    const scopeCanonical = inMemoryChatActionsRef.current.get(controller.scopeKey);
    if (
      scopeCanonical
      && scopeCanonical.action.actionId !== controller.action.actionId
    ) return scopeCanonical;
    const canonical = scopeCanonical
      ?? recoveredChatActionRef.current
      ?? chatActionAttemptRef.current;
    if (
      canonical?.action.actionId === controller.action.actionId
      && (
        canonical.recoveryState === "accepted_unsettled"
        || canonical.recoveryState === "rejected_unsettled"
      )
      && canonical.recoveryState !== recoveryState
    ) {
      return canonical;
    }
    const recovered = { ...controller, recoveryState };
    inMemoryChatActionsRef.current.set(recovered.scopeKey, recovered);
    if (recovered.scopeKey === composerDraftScopeKeyRef.current) {
      recoveredChatActionRef.current = recovered;
      setRecoveredChatAction(recovered);
    }
    if (
      chatActionAttemptRef.current?.action.actionId
      === recovered.action.actionId
    ) {
      chatActionAttemptRef.current = null;
      setChatActionAttempt(null);
    }
    return recovered;
  };
  const finishChatActionController = (
    controller: PendingChatActionController,
    reconcilePresentedDraft = true,
  ): void => {
    finishedChatActionIdsRef.current.add(controller.action.actionId);
    while (finishedChatActionIdsRef.current.size > 64) {
      const oldest = finishedChatActionIdsRef.current.values().next().value;
      if (typeof oldest !== "string") break;
      finishedChatActionIdsRef.current.delete(oldest);
    }
    inFlightChatActionsRef.current.delete(controller.action.actionId);
    for (const [scopeKey, pending] of inMemoryChatActionsRef.current) {
      if (pending.action.actionId === controller.action.actionId) {
        inMemoryChatActionsRef.current.delete(scopeKey);
      }
    }
    settlePresentedChatAction(
      controller.action.actionId,
      reconcilePresentedDraft,
    );
  };
  const adoptSupersedingChatAction = (
    superseded: PendingChatActionController,
    replacement: PendingChatActionController,
  ): void => {
    finishChatActionController(superseded);
    adoptCanonicalPendingDraft(replacement);
  };
  const clearFallbackAcceptedDraft = async (
    controller: PendingChatActionController,
  ): Promise<boolean> => {
    const scope = controller.fallbackDraftScope;
    const revision = controller.action.sourceRevision;
    if (!scope || !revision) return true;
    try {
      if (await clearComposerOrdinaryDraft(scope, revision)) {
        clearComposerTransientDraft(controller.scopeKey, {
          message: controller.draftMessage ?? controller.message,
          attachments: controller.attachments,
          expectedRevision: revision,
        });
        return true;
      }
      const latest = await readComposerOrdinaryDraft(scope);
      // Missing or newer content is already safe. Only the exact accepted
      // snapshot remains fenced against another send.
      const safe = !latest || latest.revision !== revision;
      if (safe) {
        clearComposerTransientDraft(controller.scopeKey, {
          message: controller.draftMessage ?? controller.message,
          attachments: controller.attachments,
          expectedRevision: revision,
        });
      }
      return safe;
    } catch (error) {
      console.warn("composer in-memory action cleanup deferred", error);
      return false;
    }
  };
  const actionLookupInFlightRef = useRef(new Map<string, Promise<boolean>>());
  const [sessionChatExecution, setSessionChatExecution] = useState<{
    sessionId: string;
    bootstrap: ChatExecutionBootstrap;
  } | null>(null);

  const preflightComposerIdentity = async () => {
    const expected = authSession?.me;
    const me = await revalidateMe();
    if (
      !expected
      || me.id !== expected.id
      || me.team_id !== expected.team_id
      || me.auth_session_generation !== expected.auth_session_generation
    ) {
      window.location.reload();
      throw new Error("composer action identity changed during preflight");
    }
    return me;
  };

  const preflightChatExecution = async (
    requestedSessionId: string | null,
  ): Promise<ChatExecutionBootstrap | null> => {
    const me = await preflightComposerIdentity();
    if (!requestedSessionId) return me.chat_execution ?? null;
    return (await fetchSessionChatExecutionPreflight(requestedSessionId))
      .chat_execution;
  };

  const preflightSessionChatExecution = async (
    requestedSessionId: string,
  ): Promise<SessionDetailDTO> => {
    await preflightComposerIdentity();
    return fetchSessionChatExecutionPreflight(requestedSessionId);
  };

  const resolveComposerDraftSubmissionProtocol = async (
    actionKind: "start" | "steer",
    actionAttachments: AttachmentRef[],
    hasReferenceInput = false,
  ): Promise<ComposerDraftWriteProtocol | null> => {
    if (selectedSettingsTarget && (actionKind !== "start" || hasReferenceInput)) {
      setBanner(tSettingsCanvas("protocol_unavailable"));
      return null;
    }
    const requestedSessionId = activeSessionId;
    const actionScopeKey = composerDraftScopeKeyRef.current;
    const hasExactSessionHint = Boolean(
      requestedSessionId
      && sessionChatExecution?.sessionId === requestedSessionId,
    );
    const bootstrapHint = requestedSessionId
      ? (hasExactSessionHint ? sessionChatExecution!.bootstrap : null)
      : authSession?.me?.chat_execution ?? null;
    let bootstrap = bootstrapHint;
    if (!maySkipChatExecutionPreflight(
      requestedSessionId,
      hasExactSessionHint,
      bootstrapHint,
    )) {
      try {
        bootstrap = await preflightChatExecution(requestedSessionId);
      } catch (error) {
        if (composerDraftScopeKeyRef.current === actionScopeKey) {
          console.warn("composer action auth preflight failed", error);
          setBanner(tComposer("action_preflight_failed"));
        }
        return null;
      }
    }
    if (composerDraftScopeKeyRef.current !== actionScopeKey) return null;
    if (!bootstrap || bootstrap.write_protocol === "legacy_v0") {
      if (!legacyChatActionAttachmentsSupported(
        actionKind,
        actionAttachments,
        submissionProtocolEnabled,
      )) {
        setBanner(tComposer("action_unavailable"));
        return null;
      }
      return "legacy_v0";
    }
    if (bootstrap.write_protocol !== "terminal_v1") {
      setBanner(tComposer(
        bootstrap.write_protocol === "refresh_required"
          ? "action_refresh_required"
          : "action_unavailable",
      ));
      return null;
    }
    if (selectedSettingsTarget) {
      setBanner(tSettingsCanvas("protocol_unavailable"));
      return null;
    }
    if (!terminalChatActionAttachmentsSupported(
      actionKind,
      actionAttachments,
      bootstrap,
    )) {
      setBanner(tComposer("action_unavailable"));
      return null;
    }
    return "terminal_v1";
  };

  const prepareTerminalChatAction = async ({
    actionKind,
    requestedSessionId,
    expectedTurnId,
    expectedExecutionGeneration,
    message: actionMessage,
    attachments: actionAttachments,
    draftRevision,
    submissionContext,
    draftChatBinding = null,
    workspaceSettingsBinding = null,
    coachCanvasBinding = null,
  }: {
    actionKind: "start" | "steer";
    requestedSessionId: string | null;
    expectedTurnId?: string;
    expectedExecutionGeneration?: number;
    message: string;
    attachments: AttachmentRef[];
    draftRevision: string | null;
    submissionContext?: ComposerDraftSubmissionContext | null;
    draftChatBinding?: ComposerSubmissionWireSnapshot["draftChatBinding"];
    workspaceSettingsBinding?: ComposerSubmissionWireSnapshot["workspaceSettingsBinding"];
    coachCanvasBinding?: ComposerSubmissionWireSnapshot["coachCanvasBinding"];
  }): Promise<{
    blocked: boolean;
    controller: PendingChatActionController | null;
  }> => {
    if (!activeWorkspaceId) {
      return { blocked: true, controller: null };
    }
    const actionWorkspaceId = activeWorkspaceId;
    const actionScope = submissionRecoveryScope;
    const actionScopeKey = composerDraftScopeKey;
    const hasExactSessionHint = Boolean(
      requestedSessionId
      && sessionChatExecution?.sessionId === requestedSessionId,
    );
    const bootstrapHint = requestedSessionId
      ? (hasExactSessionHint ? sessionChatExecution!.bootstrap : null)
      : authSession?.me?.chat_execution ?? null;
    // An existing session is sticky to its persisted protocol.  When its
    // exact hint is not hydrated yet, the Home rollout hint cannot authorize a
    // legacy send; fetch the session decision below instead.
    let bootstrap: ChatExecutionBootstrap | null;
    if (submissionContext) {
      bootstrap = {
        write_protocol: submissionContext.writeProtocol,
        config_generation: "composer-click",
        readiness_generation: "composer-click",
      };
    } else {
      if (maySkipChatExecutionPreflight(
        requestedSessionId,
        hasExactSessionHint,
        bootstrapHint,
      )) {
        return { blocked: false, controller: null };
      }
      try {
        bootstrap = await preflightChatExecution(requestedSessionId);
      } catch (error) {
        if (composerDraftScopeKeyRef.current === actionScopeKey) {
          console.warn("composer action auth preflight failed", error);
          setBanner(tComposer("action_preflight_failed"));
        }
        return { blocked: true, controller: null };
      }
    }
    if (composerDraftScopeKeyRef.current !== actionScopeKey) {
      return { blocked: true, controller: null };
    }
    if (!bootstrap || bootstrap.write_protocol === "legacy_v0") {
      return { blocked: false, controller: null };
    }
    if (bootstrap.write_protocol !== "terminal_v1") {
      setBanner(tComposer(
        bootstrap.write_protocol === "refresh_required"
          ? "action_refresh_required"
          : "action_unavailable",
      ));
      return { blocked: true, controller: null };
    }
    // Only starts carry an exact saved Agent draft; settings keep their
    // independent admission contract and cannot inherit this authority.
    if (coachCanvasBinding) {
      setBanner(tCoachResourceEditor("chat_protocol_unavailable"));
      return { blocked: true, controller: null };
    }
    if (workspaceSettingsBinding) {
      setBanner(tSettingsCanvas("protocol_unavailable"));
      return { blocked: true, controller: null };
    }
    if (draftChatBinding && (actionKind !== "start" || draftChatBinding.dirty)) {
      setDraftChatSubmissionError(draftChatBinding.draftId);
      return { blocked: true, controller: null };
    }
    if (
      !submissionContext
      && !terminalChatActionAttachmentsSupported(
        actionKind,
        actionAttachments,
        bootstrap,
      )
    ) {
      setBanner(tComposer("action_unavailable"));
      return { blocked: true, controller: null };
    }
    const action = actionKind === "start"
      ? createComposerChatAction({
          actionKind: "start",
          sourceRevision: draftRevision,
          sessionId: requestedSessionId,
          ...(draftChatBinding ? { agentDraftTarget: { kind: "agent_draft", id: draftChatBinding.draftId, version: draftChatBinding.version } as const } : {}),
        })
      : createComposerChatAction({
          actionKind: "steer",
          sourceRevision: draftRevision,
          sessionId: requestedSessionId!,
          expectedTurnId: expectedTurnId!,
          expectedExecutionGeneration: expectedExecutionGeneration!,
        });
    const persistenceScope = actionScope && (
      submissionContext?.persistenceAvailable
      || (!submissionContext && Boolean(draftRevision))
    )
      ? {
          ...actionScope,
          requestedSessionId,
        }
      : null;
    const controller = {
      action,
      message: actionMessage,
      draftMessage: submissionContext?.draftMessage ?? actionMessage,
      attachments: actionAttachments,
      workspaceId: actionWorkspaceId,
      persistenceScope,
      scopeKey: actionScopeKey,
    } satisfies PendingChatActionController;
    if (!persistenceScope) {
      return { blocked: false, controller };
    }
    try {
      const outcome = await prepareComposerPendingAction(
        persistenceScope,
        action,
        draftRevision,
        {
          message: submissionContext?.draftMessage ?? actionMessage,
          attachments: actionAttachments,
        },
      );
      if (composerDraftScopeKeyRef.current !== actionScopeKey) {
        return { blocked: true, controller: null };
      }
      if (outcome.status === "written") {
        return {
          blocked: false,
          controller: { ...controller, action: outcome.pendingAction },
        };
      }
      const canonicalDraft = outcome.draft;
      if (
        outcome.pendingAction
        && canonicalDraft
        && composerDraftSnapshotsMatch(canonicalDraft, {
          message: submissionContext?.draftMessage ?? actionMessage,
          attachments: actionAttachments,
        })
      ) {
        const existing = {
          action: outcome.pendingAction,
          message: canonicalDraft.message.trim(),
          draftMessage: canonicalDraft.message,
          attachments: canonicalDraft.attachments,
          workspaceId: actionWorkspaceId,
          persistenceScope,
          scopeKey: actionScopeKey,
        } satisfies PendingChatActionController;
        recoveredChatActionRef.current = existing;
        setRecoveredChatAction(existing);
        void lookupPendingChatAction(existing);
      }
      setBanner(tComposer(
        outcome.pendingAction && canonicalDraft
          && composerDraftSnapshotsMatch(canonicalDraft, {
            message: submissionContext?.draftMessage ?? actionMessage,
            attachments: actionAttachments,
          })
          ? "action_recovery_pending"
          : "draft_conflict",
      ));
      return { blocked: true, controller: null };
    } catch (error) {
      if (composerDraftScopeKeyRef.current !== actionScopeKey) {
        return { blocked: true, controller: null };
      }
      if (error instanceof ComposerRecoveryUnavailableError) {
        // The draft was readable at click time, but the marker transaction was
        // unavailable.  No durable marker can exist after an aborted IDB
        // transaction, so keep the stable identity process-local and make one
        // network attempt. Unknown/schema failures still fail closed below.
        return {
          blocked: false,
          controller: {
            ...controller,
            persistenceScope: null,
            fallbackDraftScope: persistenceScope,
          },
        };
      }
      // This branch had a readable persisted revision.  Losing the marker
      // write must not silently downgrade it to an in-memory action: a reload
      // could then resurrect the pre-action draft with no durable identity.
      console.warn("composer action marker rejected", error);
      setBanner(tComposer("action_unavailable"));
      return { blocked: true, controller: null };
    }
  };

  const settleKnownChatActionOutcomeImpl = async (
    controller: PendingChatActionController,
    disposition: "adopted" | "rejected",
  ): Promise<ChatActionSettlementResult> => {
    const outcomeController = controller;
    if (disposition === "adopted") {
      notifyAcceptedStart(outcomeController);
    }
    if (outcomeController.persistenceScope) {
      try {
        const settlement = await settleComposerPendingAction(
          outcomeController.persistenceScope,
          outcomeController.action.actionId,
          disposition,
        );
        if (
          settlement.status === "not_owner"
          && settlement.draft?.pendingAction
        ) {
          const superseding = {
            action: settlement.draft.pendingAction,
            message: settlement.draft.message.trim(),
            draftMessage: settlement.draft.message,
            attachments: settlement.draft.attachments,
            workspaceId: outcomeController.workspaceId,
            persistenceScope: outcomeController.persistenceScope,
            scopeKey: outcomeController.scopeKey,
            recoveryState: "lookup_pending",
          } satisfies PendingChatActionController;
          adoptSupersedingChatAction(outcomeController, superseding);
          if (outcomeController.scopeKey === composerDraftScopeKeyRef.current) {
            void lookupPendingChatAction(superseding);
          }
          return "superseded";
        }
        if (
          disposition === "adopted"
          && settlement.status === "not_owner"
          && settlement.draft?.revision === outcomeController.action.sourceRevision
        ) {
          const cleared = await clearComposerOrdinaryDraft(
            outcomeController.persistenceScope,
            outcomeController.action.sourceRevision,
          );
          if (!cleared) {
            const latest = await readComposerOrdinaryDraft(
              outcomeController.persistenceScope,
            );
            if (latest?.revision === outcomeController.action.sourceRevision) {
              retainChatActionForRecovery(outcomeController, "accepted_unsettled");
              return "deferred";
            }
          }
        }
      } catch (error) {
        console.warn("composer action settlement deferred", error);
        retainChatActionForRecovery(
          outcomeController,
          disposition === "adopted"
            ? "accepted_unsettled"
            : "rejected_unsettled",
        );
        return "deferred";
      }
    } else if (
      disposition === "adopted"
      && !(await clearFallbackAcceptedDraft(outcomeController))
    ) {
      retainChatActionForRecovery(outcomeController, "accepted_unsettled");
      return "deferred";
    }
    finishChatActionController(outcomeController);
    return "settled";
  };

  const settleKnownChatActionOutcome = (
    controller: PendingChatActionController,
    disposition: "adopted" | "rejected",
  ): Promise<ChatActionSettlementResult> => {
    const actionId = controller.action.actionId;
    const existing = actionSettlementInFlightRef.current.get(actionId);
    if (existing) return existing;
    let settlement!: Promise<ChatActionSettlementResult>;
    settlement = settleKnownChatActionOutcomeImpl(
      controller,
      disposition,
    )
      .finally(() => {
        if (actionSettlementInFlightRef.current.get(actionId) === settlement) {
          actionSettlementInFlightRef.current.delete(actionId);
        }
      });
    actionSettlementInFlightRef.current.set(actionId, settlement);
    return settlement;
  };

  const lookupPendingChatAction = (
    controller: PendingChatActionController,
  ): Promise<boolean> => {
    if (inFlightChatActionsRef.current.has(controller.action.actionId)) {
      return Promise.resolve(false);
    }
    const existingLookup = actionLookupInFlightRef.current.get(
      controller.action.actionId,
    );
    if (existingLookup) return existingLookup;
    if (controller.scopeKey !== composerDraftScopeKeyRef.current) {
      return Promise.resolve(false);
    }
    const lookup = (async () => {
      try {
        await lookupComposerChatAction(controller.action);
        const ownedPresentation = (
          recoveredChatActionRef.current?.action.actionId
          === controller.action.actionId
          && controller.scopeKey === composerDraftScopeKeyRef.current
        );
        if (
          await settleKnownChatActionOutcome(controller, "adopted")
          !== "settled"
        ) {
          return false;
        }
        if (!ownedPresentation || controller.scopeKey !== composerDraftScopeKeyRef.current) {
          return true;
        }
        const acceptedSessionId = canonicalComposerStartSessionId(controller.action);
        if (!acceptedSessionId) return true;
        const url = new URL(window.location.href);
        url.searchParams.set("session", acceptedSessionId);
        window.location.replace(url.toString());
        return true;
      } catch (error) {
        console.warn("composer action lookup deferred", error);
        if (
          (error as { status?: number })?.status === 404
          || (error as { code?: string })?.code === "chat_action_not_found"
        ) {
          retainChatActionForRecovery(controller, "resend_allowed");
        } else {
          retainChatActionForRecovery(controller, "lookup_pending");
        }
        return false;
      }
    })().finally(() => {
      if (
        actionLookupInFlightRef.current.get(controller.action.actionId)
        === lookup
      ) actionLookupInFlightRef.current.delete(controller.action.actionId);
    });
    actionLookupInFlightRef.current.set(controller.action.actionId, lookup);
    return lookup;
  };

  const reconcileRecoveredChatAction = (
    controller: PendingChatActionController,
  ): Promise<boolean> => {
    if (controller.recoveryState === "accepted_unsettled") {
      return settleKnownChatActionOutcome(controller, "adopted")
        .then((result) => {
          if (result !== "settled") return false;
          const acceptedSessionId = canonicalComposerStartSessionId(controller.action);
          if (
            acceptedSessionId
            && controller.scopeKey === composerDraftScopeKeyRef.current
          ) {
            const url = new URL(window.location.href);
            url.searchParams.set("session", acceptedSessionId);
            window.location.replace(url.toString());
          }
          return true;
        });
    }
    if (controller.recoveryState === "rejected_unsettled") {
      return settleKnownChatActionOutcome(controller, "rejected")
        .then((result) => result === "settled");
    }
    return lookupPendingChatAction(controller);
  };

  const handlePendingActionHydrated = (draft: ComposerOrdinaryDraft) => {
    if (!draft.pendingAction || !composerDraftPersistenceScope) return;
    if (
      chatActionAttemptRef.current?.action.actionId
      === draft.pendingAction.actionId
    ) return;
    if (
      recoveredChatActionRef.current?.action.actionId
      === draft.pendingAction.actionId
    ) return;
    const controller = {
      action: draft.pendingAction,
      message: draft.message.trim(),
      draftMessage: draft.message,
      attachments: draft.attachments,
      workspaceId: composerDraftPersistenceScope.workspaceId,
      persistenceScope: composerDraftPersistenceScope,
      recoveryState: "lookup_pending",
      scopeKey: composerDraftScopeKey,
    } satisfies PendingChatActionController;
    adoptCanonicalPendingDraft(controller);
    if (!inFlightChatActionsRef.current.has(controller.action.actionId)) {
      void reconcileRecoveredChatAction(controller);
    }
  };
  useEffect(() => {
    const pending = recoveredChatActionRef.current;
    if (!pending || pending.scopeKey === composerDraftScopeKey) return;
    const canonicalSessionId = canonicalComposerStartSessionId(pending.action);
    if (
      canonicalSessionId
      && activeSessionId === canonicalSessionId
      && (
        pending.persistenceScope?.requestedSessionId === null
        || pending.fallbackDraftScope?.requestedSessionId === null
      )
    ) {
      const handedOff = { ...pending, scopeKey: composerDraftScopeKey };
      inMemoryChatActionsRef.current.set(composerDraftScopeKey, handedOff);
      recoveredChatActionRef.current = handedOff;
      setRecoveredChatAction(handedOff);
      if (!inFlightChatActionsRef.current.has(pending.action.actionId)) {
        void reconcileRecoveredChatAction(handedOff);
      }
      return;
    }
    recoveredChatActionRef.current = null;
    setRecoveredChatAction(null);
  }, [activeSessionId, composerDraftScopeKey]);
  const previousComposerDraftScopeKeyRef = useRef(composerDraftScopeKey);
  useEffect(() => {
    if (previousComposerDraftScopeKeyRef.current === composerDraftScopeKey) {
      return;
    }
    previousComposerDraftScopeKeyRef.current = composerDraftScopeKey;
    setMessage(null);
    setPendingReferenceDocument(null);
    setPendingReferenceSourceRevision(null);
    setPendingAttachments([]);
    setSendAttemptKey(null);
    const pending = chatActionAttemptRef.current;
    if (pending && pending.scopeKey !== composerDraftScopeKey) {
      chatActionAttemptRef.current = null;
      setChatActionAttempt(null);
    }
    const inMemoryPending = inMemoryChatActionsRef.current.get(
      composerDraftScopeKey,
    );
    if (inMemoryPending) {
      recoveredChatActionRef.current = inMemoryPending;
      setRecoveredChatAction(inMemoryPending);
      void reconcileRecoveredChatAction(inMemoryPending);
    }
  }, [composerDraftScopeKey]);
  useEffect(() => {
    let current = true;
    if (!submissionRecoveryScope) {
      setRecoveredSubmission(null);
      setSubmissionOutcomeUnknown(false);
      setReferenceSubmissionPreview(null);
      setSubmissionRecoveryChecked(true);
      return () => {
        current = false;
      };
    }
    // A pending draft belongs to one exact (workspace, requested Session)
    // partition. Clear the prior partition synchronously while the new one is
    // reconciled so a slow IndexedDB/API read cannot prefill another Session's
    // composer with stale text.
    recoveredSubmissionRef.current = null;
    setRecoveredSubmission(null);
    setSubmissionOutcomeUnknown(false);
    setReferenceSubmissionPreview(null);
    setPendingReplaySourceTurnId(null);
    setSubmissionRecoveryChecked(false);
    const previewScopeKey = composerDraftScopeSignature(submissionRecoveryScope);
    void reconcileStaleSubmissionFence(submissionRecoveryScope)
      .then(async (reconciled) => {
        const committedSessionId = reconciled?.outcome !== "abandoned"
          ? reconciled?.response?.session_id
          : null;
        if (reconciled && committedSessionId) {
          // The old login generation lost its local response after the server
          // committed. Never discard that receipt: reopen the canonical
          // session under the current authenticated generation.
          const url = new URL(window.location.href);
          url.searchParams.set("session", committedSessionId);
          try {
            await clearPendingSubmission(
              submissionRecoveryScope,
              reconciled.client_submission_id,
            );
          } catch (error) {
            // The server has positively identified a committed Turn.  If the
            // local fence cannot be cleared, keep Chat locked instead of
            // falling through to a fresh legacy POST and duplicating it.
            throw new ComposerSubmissionFenceUnresolvedError(
              reconciled.client_submission_id,
              error,
            );
          }
          window.location.replace(url.toString());
          return null;
        }
        return readPendingSubmission(submissionRecoveryScope);
      })
      .then((pending) => {
        if (current) {
          setRecoveredSubmission(pending);
          setSubmissionOutcomeUnknown(pending !== null);
          recoveredSubmissionRef.current = pending;
          setPendingReplaySourceTurnId(
            pending?.submissionKind === "replay"
              ? pending.replaySourceTurnId ?? null
              : null,
          );
          setSubmissionRecoveryChecked(true);
        }
      })
      .catch(async (error) => {
        // IndexedDB is not a dependency for ordinary legacy Chat.  When no
        // durable fence could be identified, retired-protocol deployments
        // continue with one legacy POST.  A positively identified unresolved
        // fence remains locked because a new ID could duplicate a committed
        // Turn whose response was lost.
        console.warn("composer submission recovery bootstrap failed", error);
        if (current) {
          setSubmissionRecoveryChecked(
            !composerSubmissionProtocolEnabled
            && !(error instanceof ComposerSubmissionFenceUnresolvedError),
          );
          if (error instanceof ComposerSubmissionFenceUnresolvedError) {
            try {
              const draft = await readPendingReferenceSubmission(submissionRecoveryScope);
              if (current && draft && composerDraftScopeKeyRef.current === previewScopeKey) {
                setReferenceSubmissionPreview({ scopeKey: previewScopeKey, draft });
                if (referenceInputEnabled) {
                  setRecoveredSubmission(draft);
                  setSubmissionOutcomeUnknown(true);
                  recoveredSubmissionRef.current = draft;
                  setSubmissionRecoveryChecked(true);
                }
              }
            } catch { /* Unknown/corrupt envelopes retain the original fence. */ }
          }
        }
      });
    return () => {
      current = false;
    };
  }, [composerSubmissionProtocolEnabled, referenceInputEnabled, submissionRecoveryScope]);
  const handleSendInitSettled = (attemptKey: string, accepted: boolean, turn?: { turnId: string; sessionId: string | null }) => {
    const intent = operationSendIntents.current.get(attemptKey);
    operationSendIntents.current.delete(attemptKey);
    if (accepted && turn && intent) {
      operationTurnIntents.current.set(turn.turnId, { ...intent, sessionId: turn.sessionId });
      if (operationTurnIntents.current.size > 8) operationTurnIntents.current.delete(operationTurnIntents.current.keys().next().value!);
    }
    referenceRecoveryCleanups.current.delete(attemptKey);
    documentRevisionCleanups.current.delete(attemptKey);
    setPendingReferenceSourceRevision(null);
    // This callback means the POST-init transport is over. Recovery state may
    // still be unsettled locally, but it must no longer be fenced as an active
    // network attempt or its exact action lookup/retry can never run.
    inFlightChatActionsRef.current.delete(attemptKey);
    const recovered = recoveredChatActionRef.current?.action.actionId === attemptKey
      ? recoveredChatActionRef.current
      : Array.from(inMemoryChatActionsRef.current.values()).find(
        (candidate) => candidate.action.actionId === attemptKey,
      ) ?? null;
    if (recovered) {
      if (recovered.recoveryState === "accepted_unsettled") {
        if (recovered.scopeKey === composerDraftScopeKeyRef.current) {
          void settleKnownChatActionOutcome(recovered, "adopted");
        }
      } else if (recovered.recoveryState === "rejected_unsettled") {
        if (recovered.scopeKey === composerDraftScopeKeyRef.current) {
          void settleKnownChatActionOutcome(recovered, "rejected");
        }
      } else if (recovered.recoveryState === "lookup_pending") {
        if (recovered.scopeKey === composerDraftScopeKeyRef.current) {
          void lookupPendingChatAction(recovered);
        }
      }
    }
    if (recoveredSubmissionRef.current !== null) {
      setSubmissionOutcomeUnknown(true);
    }
    settleSendInit(attemptKey, accepted);
  };
  const handleSubmissionPending = (
    attemptKey: string,
    scopeKey: string,
    pending: RecoveredSubmission,
  ) => {
    const cleanup = documentRevisionCleanups.current.get(attemptKey);
    if (cleanup && composerDraftScopeSignature(cleanup.scope) === scopeKey) cleanup.pending = structuredClone(pending);
    if (scopeKey !== composerDraftScopeKeyRef.current) return;
    recoveredSubmissionRef.current = pending;
    setRecoveredSubmission(pending);
    setSubmissionOutcomeUnknown(false);
    if ("input_document" in pending) setReferenceSubmissionPreview({
      scopeKey, draft: pending as RecoveredReferenceSubmission,
    });
  };
  const handleSubmissionCleared = (
    _attemptKey: string,
    scopeKey: string,
    clientSubmissionId: string,
  ) => {
    if (scopeKey !== composerDraftScopeKeyRef.current) return;
    if (recoveredSubmissionRef.current?.clientSubmissionId !== clientSubmissionId) return;
    recoveredSubmissionRef.current = null;
    setRecoveredSubmission(null);
    setSubmissionOutcomeUnknown(false);
    setReferenceSubmissionPreview(null);
    setPendingReplaySourceTurnId(null);
  };
  const handleAbandonRecoveredSubmission = async () => {
    if (!submissionRecoveryScope || !recoveredSubmission) return false;
    const recoveryScopeKey = composerDraftScopeSignature(submissionRecoveryScope);
    const revisionTarget = recoveredSubmission.wireSnapshot?.documentRevisionTarget;
    try {
      const clientSubmissionId = recoveredSubmission.clientSubmissionId;
      const outcome = await abandonPendingSubmission(
        submissionRecoveryScope,
        clientSubmissionId,
      );
      if (outcome.outcome !== "abandoned" && outcome.outcome !== "rejected") {
        if (![
          "started",
          "buffered",
          "completed",
          "cancelled",
        ].includes(outcome.outcome)) return false;
        const committedSessionId = outcome.response?.session_id;
        if (!committedSessionId) return false;
        if (revisionTarget) {
          if (committedSessionId !== revisionTarget.session_id
            || !sameDocumentRevisionTarget(outcome.response?.document_revision_target, revisionTarget)
            || !recoveredSubmission.sourceDraftRevision) return false;
          const cleaned = await clearAcceptedComposerDocumentRevisionDraft(submissionRecoveryScope, {
            message: recoveredSubmission.message, target: revisionTarget,
            sourceDraftRevision: recoveredSubmission.sourceDraftRevision,
          });
          if (cleaned === "unbound") return false;
        }
        if (
          ["started", "buffered"].includes(outcome.outcome)
          && "input_document" in recoveredSubmission
        ) {
          await clearAcceptedComposerReferenceDraft(
            submissionRecoveryScope,
            recoveredSubmission as RecoveredReferenceSubmission,
          );
        }
        await clearPendingSubmission(submissionRecoveryScope, clientSubmissionId);
        if (revisionTarget && composerDraftScopeKeyRef.current !== recoveryScopeKey) return false;
        const url = new URL(window.location.href);
        url.searchParams.set("session", committedSessionId);
        window.location.replace(url.toString());
        return true;
      }
      await clearPendingSubmission(submissionRecoveryScope, clientSubmissionId);
      if (revisionTarget && composerDraftScopeKeyRef.current !== recoveryScopeKey) return false;
      recoveredSubmissionRef.current = null;
      setRecoveredSubmission(null);
      setSubmissionOutcomeUnknown(false);
      setReferenceSubmissionPreview(null);
      setPendingReplaySourceTurnId(null);
      if (outcome.outcome === "rejected") {
        setBanner(tComposer("submission_rejected"));
      }
      return true;
    } catch (error) {
      console.warn("composer submission abandon failed", error);
      return false;
    }
  };
  // Canonical report-link intent is independent from access mode: owners open an
  // editable report, while recipients may enter report-only. Both must retain
  // `report=1` so refresh/copy/history restore the same surface.
  const [reportLinkSessionId, setReportLinkSessionId] = useState<string | null>(null);
  const [reportOnlySessionId, setReportOnlySessionId] = useState<string | null>(null);
  // content-permissions wave 2: a shared-report link the viewer can't access →
  // render the ReportUnavailable placeholder (+ "申请访问") instead of a blank fail.
  const [reportUnavailableSid, setReportUnavailableSid] = useState<string | null>(null);
  // Synchronize ref for the W11-5 refresh handler (avoid stale closures).
  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);
  // User clicks "+ 新建" → bump resetSignal; runner clears canvas + all turn state.
  const [resetSignal, setResetSignal] = useState(0);
  const [composerResetKey, setComposerResetKey] = useState(0);
  const homeComposerDraftControlRef = useRef<ComposerDraftControl | null>(null);
  const workbenchComposerDraftControlRef = useRef<ComposerDraftControl | null>(null);
  const [composerRevealRequest, setComposerRevealRequest] = useState<{ id: string; scopeKey: string } | null>(null);
  // Stop uses a stable durable request id and keeps SSE authoritative until
  // the worker emits its terminal frame.
  const cancelPendingRef = useRef(false);
  const cancelRequestRef = useRef<{
    key: string;
    record: PendingCancelRequest;
  } | null>(null);
  const cancelTerminalKeysRef = useRef(new Set<string>());
  const [cancelRequestedKey, setCancelRequestedKey] = useState<string | null>(null);
  // Click a historical session → hydrate runner + canvas; runner.messages[] carries the full conversation history.
  // Optional resumeTurn: if the session's last turn has status=running, record turnId so runner
  // reconnects the disconnected SSE stream.
  const [hydrate, setHydrate] = useState<{
    sessionId: string;
    sessionTitle: string | null;
    messages: ChatMessage[];
    lastIntent: IntentType | null;
    /** chat-hydrate-overhaul: revive last turn summary + turnId. (Phase 1.5:
     *  lastTraceSteps retired — turn-store-replay populates steps via
     *  /api/v1/turns/{id}/events instead.) */
    lastSummary?: import("@/lib/types").TurnSummary | null;
    lastTurnId?: string | null;
    /** Durable status of the last hydrated Turn. */
    lastTerminalStatus?: string | null;
    lastExecutionGeneration?: number | null;
    lastTerminalProtocolVersion?: number | null;
    /** chat-v4 hydrate: 4 v4 slot payloads for the displayed last turn,
     *  backfilled from `/api/v1/turns/{id}/events`. Triggers TurnContainer render
     *  on session refresh. Undefined/empty when turn has no v4 emits
     *  (legacy V1 path). */
    lastSubagentSummaries?: Record<string, unknown>;
    lastFinalPayload?: unknown;
    /** ν2 response_kind backfilled from the persisted done event payload —
     *  TurnContainer's chat-shape gate (skip plan/rail/phase for prose answers)
     *  needs this on session refresh. */
    lastResponseKind?: "chat" | "canvas" | null;
    /** done.tool_invocation_count — TurnContainer gates StepListCard on this (0 = pure
     *  prose / greeting → suppress plan; ≥1 = data work → keep plan even if
     *  response_kind=chat because Lead self-answered after BI tool call). */
    lastToolInvocationCount?: number;
    /** Last persisted done's report_sections_saved; null when the field is absent. */
    lastReportSectionsSaved?: number | null;
    /** F1 ar2 clarification: persisted clarification_required events
     *  rehydrated so ClarificationCard re-renders on session refresh. */
    lastClarifications?: unknown[];
    /** Persisted failure reason for a terminal turn whose done payload was
     * never emitted; keeps the failed bubble and Replay affordance after F5. */
    lastErrorReason?: import("@/lib/types").SseErrorReason | null;
    /** chat-v4 hydrate: when lastFinalPayload is present we must seed
     *  runner.user (and userAttachments) so V3TurnStream renders the user
     *  bubble for this turn (last-turn user_text is excluded from `messages`
     *  to avoid duplicate ag-prose / V1 final answer rendering). */
    lastUserText?: string | null;
    lastUserAttachments?: AttachmentRef[];
    /** chat-redesign Phase 1.2 CTO 2nd review P0 #1 — last turn's prose
     *  threaded into runner.answer so ChatBubbleNew renders on reload. */
    lastAnswer?: string | null;
    /** Terminal last Turn is visible from persisted DTO fields before its
     * event-derived projection arrives. */
    lastTurnRouted?: boolean;
    lastHeadline?: string;
    lastCitations?: import("@/lib/types").Citation[];
    lastEffectDisclosure?: import("@/lib/api").EffectDisclosureOverlayDTO | null;
    lastEffectDisclosureDiscoveryPending?: boolean;
    resumeTurn?: {
      turnId: string;
      executionGeneration?: number;
      userText: string;
      userAttachments?: AttachmentRef[];
    } | null;
    /** Shared report deep-link: canvas is readable but owner session detail is not. */
    reportOnly?: boolean;
    /** report-open (owner opens their report from My Reports, ?report=1 with chat access):
     *  initially fullscreen report but retain the thin "对话" edge (≠ reportOnly read-only sharing). */
    openMaximized?: boolean;
    /** prod-bug-bash-038 #9 — full ArchivedTurn[] for all prior completed
     *  turns in this session. Replaces text-only `messages[]` rendering for
     *  prior Q&A. Populated by parallel `/turns/{id}/events` replay so each
     *  archived turn has beats[] / steps[] / canvas state matching what a
     *  live turn produces. Without this, prior turns silently disappear from
     *  the chat on session reload because `runner.messages` is consumed only
     *  by workspace context fallback, not by visible chat history. */
    archivedTurns?: ArchivedTurn[];
  } | null>(null);
  const [hydrateTick, setHydrateTick] = useState(0);
  const [archivedTurnsPatch, setArchivedTurnsPatch] =
    useState<ArchivedTurnsHydrationPatch | null>(null);
  const [currentTurnHydrationPatch, setCurrentTurnHydrationPatch] =
    useState<CurrentTurnHydrationPatch | null>(null);
  const sessionHydrationEpochRef = useRef(0);
  const requestedSessionIdRef = useRef<string | null>(null);

  const isCurrentHydration = (hydrationEpoch: number, sessionId: string) =>
    sessionHydrationEpochRef.current === hydrationEpoch
    && requestedSessionIdRef.current === sessionId;

  const hydrateHistoricalTurns = async ({
    sessionId,
    turns,
    hydrationEpoch,
    logPrefix,
  }: {
    sessionId: string;
    turns: _BETurn[];
    hydrationEpoch: number;
    logPrefix: string;
  }) => {
    const replays = await Promise.all(
      turns.map((turn) =>
        _hydrateLimit(() => _hydrateTurnEventsOnce(turn.id)).catch((error) => {
          console.warn(`${logPrefix} turn events failed`, turn.id, error);
          return null;
        }),
      ),
    );
    if (!isCurrentHydration(hydrationEpoch, sessionId)) return;
    for (const turn of turns) {
      if (turn.status === "failed" || turn.status === "aborted") {
        failActiveSteps(turn.id);
      }
    }
    setArchivedTurnsPatch({
      sessionId,
      turns: turns.map((turn, index) => _toArchivedTurn(
        turn,
        replays[index],
        { disclosureReadPending: replays[index] == null },
      )),
    });
  };

  const hydrateCurrentTerminalTurn = async ({
    sessionId,
    turn,
    hydrationEpoch,
    logPrefix,
  }: {
    sessionId: string;
    turn: _BETurn;
    hydrationEpoch: number;
    logPrefix: string;
  }) => {
    const hydrated = await _hydrateTurnEventsOnce(turn.id, {
      // The running Turn already attaches the SSE replay stream. Dispatching
      // this independently fetched HTTP prefix into the same store lets a late
      // old step_started overwrite a newer SSE step_done.
      replayRows: turn.status !== "running",
    }).catch((error) => {
      console.warn(`${logPrefix} last turn events failed`, turn.id, error);
      return null;
    });
    if (!hydrated || !isCurrentHydration(hydrationEpoch, sessionId)) return;
    const projection = hydrated.projection;
    setCurrentTurnHydrationPatch({
      sessionId,
      turnId: turn.id,
      lastSummary: _toFESummary(turn.summary, {
        status: turn.status,
        endedAt: turn.ended_at,
        projection,
      }),
      lastSubagentSummaries: projection.subagentSummaries,
      lastFinalPayload: projection.finalPayload,
      lastResponseKind: projection.responseKind,
      lastToolInvocationCount: projection.toolInvocationCount,
      lastReportSectionsSaved: projection.reportSectionsSaved,
      lastClarifications: projection.clarifications,
      lastErrorReason: resolveHydratedTurnError(
        projection.errorReason,
        turn.status,
        turn.error_kind,
      ),
      lastHeadline: projection.headline,
      lastCitations: projection.citations,
      lastEffectDisclosure: hydrated.effectDisclosure,
      terminalProtocolVersion: hydrated.terminalProtocolVersion,
      executionGeneration: hydrated.currentGeneration,
    });
  };

  // First mount: restore the session from URL ?session=xxx — retain context after refresh.
  const didHydrateFromUrl = useRef(false);
  // feedback-0713 #13: track the latest URL-initiated hydrate target (sid + report),
  // preventing initial-mount and searchParams effects below from hydrating the same target twice, and preventing a leftover ?session=
  // from sending users back to an old session when activeSessionId clears (new conversation).
  const urlSessionReqRef = useRef<string | null>(null);
  // URL synchronization has a single owner: the effect below. New work only
  // records cleanup intent so the effect runs after activeSessionId=null has
  // committed; imperative history updates here would race useSearchParams.
  const clearSessionUrlRef = useRef(false);
  // Only an accepted user switch owns this intent. Initial deep links and
  // Back/Forward must retain their explicit result target.
  const pendingSessionScopedResultsResetRef = useRef<string | null>(null);
  // While that committed reset propagates through Next's search params, ignore
  // the old session exactly until the canonical URL is observed. Otherwise the
  // URL hydrate effect can reopen the session that New work just closed.
  const resettingUrlSessionRef = useRef<string | null>(null);
  // Session last written to the URL by this page (entry URL on first mount). Distinguish whether the live URL's session was our own
  // write or an unhandled external navigation (see urlHasPendingForeignSession).
  const lastWrittenUrlSessionRef = useRef<string | null>(null);
  // Handling an external navigation to clean / (confirmation may be open); effect reruns must not handle it twice.
  const cleanUrlNavigationRef = useRef(false);
  useEffect(() => {
    if (didHydrateFromUrl.current) return;
    didHydrateFromUrl.current = true;
    const params = new URLSearchParams(window.location.search);
    const sid = params.get("session");
    lastWrittenUrlSessionRef.current = sid;
    if (sid) {
      const reportIntent = params.get("report") === "1";
      urlSessionReqRef.current = urlSessionTargetKey(sid, reportIntent);
      void handlePickSessionFromUrl(sid, reportIntent);
    }
    // Sprint ρ Phase D3 e2e: `?scenario=<id>` URL override fires a mock
    // scenario without composer input. Only effective in SSE_MODE === "mock".
    if (SSE_MODE === "mock") {
      const sc = pickScenarioFromUrl(window.location.search);
      if (sc) {
        setScenarioId(sc);
        setReplayKey((k) => k + 1);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // feedback-0713 #13: clicking "定时任务结果" in the notification center → router.push("/?session=…") is
  // soft navigation and does not remount this component → the initial-mount effect above does not rerun, so clicking the session did nothing. Observe
  // searchParams changes; post-mount ?session= changes also use handlePickSessionFromUrl.
  // Guard: only record when current sid + report intent echo the rail's own URL write;
  // skip a URL target already hydrating/handled.
  useEffect(() => {
    if (!didHydrateFromUrl.current) return; // The effect above handles first mount.
    const sid = searchParams.get("session");
    const reportIntent = searchParams.get("report") === "1";
    // A stale search-parameter snapshot can reopen a previous session after starting new work.
    //
    // This page maintains its URL with `window.history.replaceState` (see the synchronization effect below). Those writes
    // carried Next's history markers, so Next's `useSearchParams` **did not track them** — after switching A→B,
    // `searchParams` here could still show A (writes now omit the markers so Next follows, but synchronization uses
    // a transition, so this snapshot can still lag one tick). Clicking "新工作" clears activeSessionId to null;
    // this effect reruns and reads stale A, while the old guard checked `sid === resettingUrlSessionRef.current`
    // (only the session being closed = B). A ≠ B therefore **passes unblocked**, hydrating A; 2 seconds later,
    // hydration resets activeSessionId to A, and the synchronization effect writes `?session=A` back to the URL.
    //
    // With two sessions, stale hydration can request session A after the URL has already been cleared,
    // then write `?session=A` back. Compare the current browser URL before accepting the snapshot.
    // ⚠ Cannot reproduce with only **one** session open: stale sid then equals the session being closed, and the old guard blocks it.
    //
    // Always use `window.location` as authoritative; delegate Ignore Any URL-Derived Session During Reset to
    // `resolveUrlSessionNav` (regression-locked there).
    const liveSid = new URL(window.location.href).searchParams.get("session");
    if (!liveSid) {
      // Canonical clean URL is now in place → reset period ends. Check the live URL, never a potentially stale snapshot.
      resettingUrlSessionRef.current = null;
    }
    if (!sid) {
      if (
        !cleanUrlNavigationRef.current
        && isExternalCleanUrlNavigation({
          snapshotSid: sid,
          liveSid,
          handledTarget: urlSessionReqRef.current,
        })
      ) {
        void followCleanUrlNavigation();
      }
      return;
    }
    // F5-#13: extract the guard into a pure function (lib/url-session-nav.ts) for regression locking — final
    // review P1's Repeated Clicks On The Same Notification Do Nothing came from this incorrect guard.
    const action = resolveUrlSessionNavAgainstLocation({
      snapshot: searchParams,
      location: window.location.href,
      activeSessionId,
      activeReportIntent: Boolean(activeSessionId && reportLinkSessionId === activeSessionId),
      lastRequestedTarget: urlSessionReqRef.current,
      resetInFlight: resettingUrlSessionRef.current !== null,
    });
    if (action === "ignore") return;
    urlSessionReqRef.current = urlSessionTargetKey(sid, reportIntent);
    if (action === "hydrate") {
      void handlePickSessionFromUrl(sid, reportIntent);
    } else if (action === "sync-report") {
      const canvas = getCanvasSnapshot();
      const hasCurrentReport = canvas.sessionId === sid && Boolean(canvas.canvasId);
      // Entering report mode still has to prove that this session has a readable
      // report. Shared report-only sessions likewise cannot exit to a chat surface
      // the viewer is not authorised to read, so those two cases use the existing
      // authoritative hydrate path instead of only flipping presentation state.
      if ((reportIntent && !hasCurrentReport) || (!reportIntent && reportOnlySessionId === sid)) {
        void handlePickSessionFromUrl(sid, reportIntent);
        return;
      }
      setReportLinkSessionId(reportIntent ? sid : null);
      setHydrate((current) => current?.sessionId === sid
        ? { ...current, openMaximized: reportIntent }
        : current);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, activeSessionId, reportLinkSessionId, reportOnlySessionId]);

  // activeSessionId changes → write URL query (without refresh) for rehydration on the next refresh.
  useEffect(() => {
    // Native history updates are observed by useSearchParams before React has
    // necessarily committed the matching state update. Ignore an effect from
    // that stale render, otherwise an explicit New work reset can briefly
    // remove `session` and then write the old id straight back.
    if (activeSessionIdRef.current !== activeSessionId) return;
    const url = new URL(window.location.href);
    const userSwitch = Boolean(
      activeSessionId
        && pendingSessionScopedResultsResetRef.current === activeSessionId,
    );
    if (urlHasPendingForeignSession({
      liveSid: url.searchParams.get("session"),
      activeSessionId,
      lastWrittenSid: lastWrittenUrlSessionRef.current,
      userSwitch,
    })) {
      return;
    }
    const resetSessionScopedResults = Boolean(
      userSwitch
      || (!activeSessionId && clearSessionUrlRef.current),
    );
    if (activeSessionId) {
      url.search = syncActiveSessionSearchParams({
        current: url.searchParams,
        activeSessionId,
        reportLinkSessionId,
        clearSessionScopedResults: resetSessionScopedResults,
      }).toString();
    } else {
      if (
        url.searchParams.has("session") &&
        !clearSessionUrlRef.current
      ) {
        return;
      }
      url.search = syncActiveSessionSearchParams({
        current: url.searchParams,
        activeSessionId: null,
        reportLinkSessionId: null,
        clearSessionScopedResults: resetSessionScopedResults,
      }).toString();
    }
    const nextHistoryState = resetSessionScopedResults
      ? withoutResultsHistoryState(window.history.state)
      : window.history.state;
    const resetUrlLanded = !activeSessionId && !url.searchParams.has("session");
    window.history.replaceState(
      // Next routing must see the URL written here: replaceState with Next's own history marker (`__NA`)
      // does not synchronize useSearchParams, leaving its snapshot at an old `?session=`; later,
      // deep-linking to that old value leaves the snapshot unchanged, so the effect above does not rerun (enter `?session=s2` → sidebar
      // selects s3 → notification cannot reopen s2; after New Work, a deep link cannot reopen the previous session). Remove the marker so Next's
      // patch synchronizes routing and copies internal state back to this history entry.
      //
      // Echo: after Next synchronizes, the effect above reruns with a new snapshot. At write time, activeSessionId / report view was
      // this URL's target ⇒ mark-handled (only record target, no hydration). If the view changes afterward: the next write is in place
      // ⇒ snapshot ≠ live URL ⇒ ignore; not yet in place (the effect above runs first in the same commit) ⇒ snapshot is the just-recorded
      // target ⇒ ignore; echo not yet received ⇒ snapshot ≠ live URL ⇒ ignore. None rehydrate or undo
      // the switch just made.
      withoutNextRouterHistoryMarkers(nextHistoryState),
      "",
      url.toString(),
    );
    // No case fails if this line is omitted: it only acts when activeSessionId changes without user selection or URL hydration,
    // while the address bar remains at a session the page wrote earlier (still remembering the entry session would misclassify our own write
    // as external navigation and skip writing the URL). Existing entry paths cannot reach this state; keep it so the condition does not depend on coincidence.
    lastWrittenUrlSessionRef.current = url.searchParams.get("session");
    if (
      activeSessionId
      && pendingSessionScopedResultsResetRef.current === activeSessionId
    ) {
      pendingSessionScopedResultsResetRef.current = null;
    }
    // Reset ends here: this code wrote the canonical clean URL — the fix's main path, not a fallback.
    // The old implementation cleared this marker only when the ?session= effect above observed a clean URL; on the most common path
    // (start at `/`, select A then B in the sidebar → New Work; or click New Work twice), Next's snapshot is the same string before and after
    // this write, so that effect never reruns and the marker remains — all subsequent deep links (notifications, forward
    // and back to ?session=) are swallowed as stale reset-period snapshots (two After New Work Deep-Link cases in e2e `sidebar-session-archive.spec.ts`
    // lock this line). Once the clean URL is in place, comparison with the live URL blocks stale snapshots.
    if (resetUrlLanded) resettingUrlSessionRef.current = null;
    clearSessionUrlRef.current = false;
  }, [activeSessionId, reportLinkSessionId, resetSignal]);

  const hydrateReportOnlySession = (sid: string, canvasDto: CanvasBySessionDTO) => {
    hydrateCanvas({
      canvas: canvasDto.canvas
        ? {
            id: canvasDto.canvas.id,
            sessionId: canvasDto.canvas.sessionId,
            title: canvasDto.canvas.title ?? null,
          }
        : null,
      sections: canvasDto.sections ?? [],
      sourceRefs: canvasDto.sourceRefs,
      turnCitations: canvasDto.turnCitations,
    });
    setReportLinkSessionId(sid);
    setReportOnlySessionId(sid);
    setReportUnavailableSid(null);
    activeSessionIdRef.current = sid;
    setActiveSessionId(sid);
    if (canvasDto.canvas?.workspaceId) {
      restoreActiveSpace(canvasDto.canvas.workspaceId);
    }
    void markSessionSeen(sid);
    setPendingAttachments([]);
    setMessage(null);
    setPendingReferenceDocument(null);
    setLastSentReferenceDocument(null);
    setPendingReferenceSourceRevision(null);
    lastTurnId.current = null;
    setHydrate({
      sessionId: sid,
      sessionTitle: canvasDto.canvas?.title ?? null,
      messages: [],
      archivedTurns: [],
      lastIntent: null,
      lastResponseKind: "canvas",
      lastToolInvocationCount: 0,
      resumeTurn: null,
      reportOnly: true,
    });
    setHydrateTick((k) => k + 1);
  };

  const startSessionHydration = async ({
    sid,
    expectReport,
    origin,
    hydrationEpoch,
  }: {
    sid: string;
    expectReport: boolean;
    origin: "url" | "picker";
    hydrationEpoch: number;
  }) => {
    const logPrefix = origin === "url" ? "[hydrate]" : "[handlePickSession]";
    try {
      setReportLinkSessionId(null);
      setReportOnlySessionId(null);
      const detailResultPromise = fetchSessionDetail(sid)
        .then((detail) => ({ ok: true as const, detail }))
        .catch((err) => ({ ok: false as const, err }));
      const canvasDto = await fetchCanvasBySession(
        sid,
        expectReport ? { expectReport: true } : undefined,
      );
      if (!isCurrentHydration(hydrationEpoch, sid)) return;
      const detailResult = await detailResultPromise;
      if (!isCurrentHydration(hydrationEpoch, sid)) return;
      if (!detailResult.ok) {
        if (!(detailResult.err instanceof SessionDetailError) || detailResult.err.status !== 403) {
          throw detailResult.err;
        }
        if (!canvasDto.canvas) {
          if (expectReport) {
            setReportUnavailableSid(sid);
            return;
          }
          throw detailResult.err;
        }
        console.warn(`${logPrefix} session detail unavailable; opening report-only:`, detailResult.err);
        hydrateReportOnlySession(sid, canvasDto);
        clearSessionLoadFailure();
        return;
      }
      if (expectReport && !canvasDto.canvas) {
        setReportUnavailableSid(sid);
        return;
      }
      const detail = detailResult.detail;
      setSessionChatExecution(detail.chat_execution
        ? { sessionId: sid, bootstrap: detail.chat_execution }
        : null);
      const { payload, lastTurn, historyTurns } =
        _buildSessionHydrationBase(detail, sid);
      if (!isCurrentHydration(hydrationEpoch, sid)) return;
      hydrateCanvas({
        canvas: canvasDto.canvas
          ? {
              id: canvasDto.canvas.id,
              sessionId: canvasDto.canvas.sessionId,
              title: canvasDto.canvas.title ?? detail.session.title ?? null,
            }
          : null,
        sections: canvasDto.sections ?? [],
        sourceRefs: canvasDto.sourceRefs,
        turnCitations: canvasDto.turnCitations,
      });
      setReportLinkSessionId(expectReport ? sid : null);
      activeSessionIdRef.current = sid;
      setActiveSessionId(sid);
      setReportUnavailableSid(null);
      clearSessionLoadFailure();
      if (detail.session.workspace_id) {
        restoreActiveSpace(detail.session.workspace_id);
      }
      void markSessionSeen(sid);
      setMessage(null);
      setPendingReferenceDocument(null);
      setLastSentReferenceDocument(null);
      setPendingReferenceSourceRevision(null);
      setHydrate({
        ...payload,
        openMaximized: expectReport,
      });
      setHydrateTick((k) => k + 1);
      if (lastTurn) {
        void hydrateCurrentTerminalTurn({
          sessionId: sid,
          turn: lastTurn,
          hydrationEpoch,
          logPrefix,
        });
      }
      void hydrateHistoricalTurns({
        sessionId: sid,
        turns: historyTurns,
        hydrationEpoch,
        logPrefix,
      });
    } catch (err) {
      if (!isCurrentHydration(hydrationEpoch, sid)) return;
      if (err instanceof ReportUnavailableError) {
        setReportUnavailableSid(err.sessionId);  // readiness effect clears loadingSid
        if (
          origin === "picker"
          && pendingSessionScopedResultsResetRef.current === sid
        ) {
          pendingSessionScopedResultsResetRef.current = null;
        }
        return;
      }
      if (origin === "picker") {
        // Explain only what happened and what to do; status code and request ID go in the Toast's "复制诊断".
        showSessionLoadFailure({ kind: "error", text: ts(sessionLoadFailedKey(err)), action: copyDiagnosticsAction(err, "route") });
        if (pendingSessionScopedResultsResetRef.current === sid) {
          pendingSessionScopedResultsResetRef.current = null;
        }
      } else {
        console.warn("hydrate from url failed:", err);
        if (urlSessionReqRef.current === urlSessionTargetKey(sid, expectReport)) {
          urlSessionReqRef.current = null;
        }
      }
      setLoadingSid(null);                        // hard failure — no destination to wait for
    }
  };

  // Mount, notifications and Back/Forward supply a complete URL target. They
  // supersede (rather than inherit) a pending picker-only Results reset.
  const handlePickSessionFromUrl = async (sid: string, expectReport = false) => {
    ignoreSyncRef.current = true;
    pendingSessionScopedResultsResetRef.current = null;
    setLoadingSid(sid);
    const hydrationEpoch = ++sessionHydrationEpochRef.current;
    requestedSessionIdRef.current = sid;
    setArchivedTurnsPatch(null);
    setCurrentTurnHydrationPatch(null);
    await startSessionHydration({
      sid,
      expectReport,
      origin: "url",
      hydrationEpoch,
    });
  };

  const [subagentCardOpen, setSubagentCardOpen] = useState(false);
  const settingsCanvas = useSettingsCanvas({
    enabled: authSession?.me?.capabilities?.settings_pages_canvas === true,
    scope: authSession?.me?.id && authSession.me.team_id && activeWorkspaceId ? {
      userId: authSession.me.id,
      teamId: authSession.me.team_id,
      workspaceId: activeWorkspaceId,
    } : null,
    canManageSpace: workspace.items.some((item) => item.id === activeWorkspaceId && item.role === "admin"),
    searchParams: new URLSearchParams(searchParams.toString()),
    sessionId: activeSessionId,
  });
  useEffect(() => {
    settingsBridge?.retainScope(authSession?.me?.id && authSession.me.team_id && activeWorkspaceId ? {
      userId: authSession.me.id, teamId: authSession.me.team_id, workspaceId: activeWorkspaceId,
    } : null);
  }, [settingsBridge, authSession?.me?.id, authSession?.me?.team_id, activeWorkspaceId]);
  const [subagentCardPayload, setSubagentCardPayload] =
    useState<CreateSubagentOpenPayload | null>(null);
  const [subagentEntryIntent, setSubagentEntryIntent] = useState(false);
  const [subagentEntryChooser, setSubagentEntryChooser] = useState(false);
  const [draftChatSubmissionError, setDraftChatSubmissionError] = useState<string | null>(null);
  const [canvasResultsSelected, setCanvasResultsSelected] = useState(false);
  const [canvasResultsReveal, setCanvasResultsReveal] = useState<{ sessionId: string; sequence: number } | null>(null);
  const revealCanvasResults = () => {
    if (activeSessionId) setCanvasResultsReveal((previous) => ({ sessionId: activeSessionId, sequence: (previous?.sequence ?? 0) + 1 }));
  };
  const lastEditorKeyRef = useRef<string | null>(null);
  const canvasScopeKey = !workspace.loading && !workspace.error && authSession?.me?.id && authSession.me.team_id && activeWorkspaceId
    ? JSON.stringify([authSession.me.id, authSession.me.team_id, activeWorkspaceId]) : null;
  const canvasScopeRef = useRef(canvasScopeKey);
  canvasScopeRef.current = canvasScopeKey;
  const canvasOpenIntentRef = useRef(0);
  const operationSendIntents = useRef(new Map<string, CoachOperationIntent>());
  const operationTurnIntents = useRef(new Map<string, CoachOperationIntent>());
  const multipleCanvasEnabled = authSession?.me?.capabilities?.configuration_canvas === true;
  const agentCanvas = useAgentCanvasObjects(canvasScopeKey, multipleCanvasEnabled);
  const resourceCanvas = useCoachResourceEditors(canvasScopeKey && activeSessionId && multipleCanvasEnabled
    ? JSON.stringify([canvasScopeKey, activeSessionId]) : null);
  const operationCanvas = useCoachOperationViews(canvasScopeKey && activeSessionId && multipleCanvasEnabled
    ? JSON.stringify([canvasScopeKey, activeSessionId]) : null);
  const documentScope = useMemo<DocumentCanvasScope | null>(() => multipleCanvasEnabled && canvasScopeKey && activeSessionId
    && activeSessionId !== reportOnlySessionId && authSession?.me?.auth_session_generation ? {
      userId: authSession.me.id, teamId: authSession.me.team_id!, workspaceId: activeWorkspaceId!,
      authSessionGeneration: authSession.me.auth_session_generation, sessionId: activeSessionId,
    } : null, [multipleCanvasEnabled, canvasScopeKey, activeSessionId, reportOnlySessionId, authSession?.me?.auth_session_generation]);
  const documentScopeKey = documentCanvasScopeKey(documentScope);
  const documentCanvas = useDocumentCanvasObjects(documentScope, new URLSearchParams(searchParams.toString()), (mode) => {
    if (mode === "editor" || mode === "results") {
      canvasOpenIntentRef.current += 1;
      resourceCanvas.select(null); operationCanvas.select(null); agentCanvas.select(null);
      settingsCanvas.deselect(); setSubagentCardOpen(false);
      setCanvasResultsSelected(mode === "results");
      if (mode === "results") revealCanvasResults();
      showDraftInUrl(null);
    } else if (mode === null && activeCanvasSelectionRef.current?.kind === "document_draft") {
      const params = new URLSearchParams(window.location.search);
      const editor = params.has("agent_draft") || (readSettingsCanvasTarget(params) && readSettingsCanvasSelection(params));
      setCanvasResultsSelected(!editor);
    }
  });
  const [documentReceipt, setDocumentReceipt] = useState<DocumentDraftCanvasPublication | null>(null);
  const draftChatEditEnabled = authSession?.me?.capabilities?.subagent_authoring?.draft_chat_edit === true;
  const unifiedDraftEnabled = authSession?.me?.capabilities?.subagent_authoring?.unified_draft === true;
  const subagentCanvasEnabled = authSession?.me?.capabilities?.subagent_authoring?.authoring === true;
  const { showDraftInUrl, scopeMatches: draftScopeMatches } = useAgentDraftUrl({
    searchParams,
    authoringEnabled: canvasScopeKey !== null && authSession?.me?.capabilities?.subagent_authoring?.authoring === true,
    cardOpen: subagentCardOpen || agentCanvas.objects.length > 0,
    scope: { teamId: authSession?.me?.team_id || null, workspaceId: activeWorkspaceId || null, sessionId: activeSessionId },
    onScopeChange: () => {
      // Accepted creation of this Chat is not a later manual selection.
      for (const intent of operationTurnIntents.current.values()) {
        if (intent.scope === canvasScopeKey && intent.sessionId === activeSessionId && intent.generation === canvasOpenIntentRef.current)
          intent.generation += 1;
      }
      canvasOpenIntentRef.current += 1;
      lastEditorKeyRef.current = null;
      agentCanvas.clear();
      setSubagentCardOpen(false);
      setSubagentCardPayload(null);
      setSubagentEntryIntent(false);
      setSubagentEntryChooser(false);
      setDraftChatSubmissionError(null);
      setCanvasResultsSelected(false);
    },
    onRestoreDraft: (draftId) => {
      void openAgentCanvas({ purpose_hint: null, draft_id: draftId, draft_status: null, draft_title: null });
    },
    onStartCreation: (purpose) => {
      canvasOpenIntentRef.current += 1;
      documentCanvas.deselect();
      resourceCanvas.select(null);
      operationCanvas.select(null);
      setSubagentEntryIntent(true);
      setSubagentEntryChooser(true);
      setSubagentCardPayload({ purpose_hint: purpose, draft_id: null, draft_status: null, draft_title: null });
      setSubagentCardOpen(true);
      setCanvasResultsSelected(false);
      settingsCanvas.deselect();
    },
  });
  const settingsTarget = settingsCanvas.target;
  const settingsKey = settingsTarget ? settingsCanvasKey(settingsTarget) : null;
  const agentEntryKey = "agent-creation-entry";
  const agentEntryVisible = subagentCardOpen && subagentCanvasEnabled && draftScopeMatches;
  const canvasSelectionItems: CanvasSelection[] = [
    ...(agentEntryVisible ? [{ key: agentEntryKey, kind: "agent_entry" as const }] : []),
    ...(subagentCanvasEnabled && draftScopeMatches && activeWorkspaceId
      ? agentCanvas.objects.map((object) => ({ key: object.key, kind: "agent_draft" as const })) : []),
    ...settingsCanvas.targets.map((target) => ({ key: settingsCanvasKey(target), kind: "settings" as const })),
    ...operationCanvas.objects.map((object) => ({ key: object.key, kind: "operation" as const })),
    ...resourceCanvas.objects.map((object) => ({ key: object.key, kind: "resource" as const })),
    ...documentCanvas.objects.map((object) => ({ key: object.key, kind: "document_draft" as const })),
  ];
  const canvasSelection = resolveCanvasSelection(canvasSelectionItems, [
    documentCanvas.selectedKey, resourceCanvas.selectedKey, operationCanvas.selectedKey, settingsCanvas.selected ? settingsKey : null,
    agentEntryVisible ? agentEntryKey : null, agentCanvas.selectedKey, settingsKey,
  ]);
  const activeCanvasSelection = canvasResultsSelected ? null : canvasSelection;
  const activeCanvasSelectionRef = useRef(activeCanvasSelection);
  activeCanvasSelectionRef.current = activeCanvasSelection;
  const selectedSettingsTarget = activeCanvasSelection?.kind === "settings"
    ? settingsCanvas.targets.find((target) => settingsCanvasKey(target) === activeCanvasSelection.key) ?? null : null;
  const selectedAgentObject = activeCanvasSelection?.kind === "agent_draft"
    ? agentCanvas.objects.find((object) => object.key === activeCanvasSelection.key) : undefined;
  const activeDraftChatBinding = draftChatEditEnabled ? selectedAgentObject?.binding ?? null : null;
  const draftChatPresentationConnected = Boolean(activeDraftChatBinding)
    && draftChatSubmissionError !== selectedAgentObject?.payload.draft_id;
  const captureCanvasSubmissionWire = (actionKind: "start" | "steer", hasReferenceInput: boolean, documentRevisionTarget?: DocumentRevisionTarget, bindDraft = true): ComposerSubmissionWireSnapshot | null => {
    if (documentRevisionTarget) {
      if (actionKind !== "start" || hasReferenceInput || documentRevisionTarget.session_id !== activeSessionId) return null;
      return captureComposerSubmissionWireSnapshot(activeSessionId, authSession?.me?.capabilities?.planning === true, null);
    }
    const snapshot = captureComposerSubmissionWireSnapshot(activeSessionId,
      authSession?.me?.capabilities?.planning === true,
      bindDraft && actionKind === "start" && !hasReferenceInput ? activeDraftChatBinding : null);
    if (activeCanvasSelection?.kind === "resource") {
      if (!bindDraft || actionKind !== "start" || hasReferenceInput || snapshot.planningMode === "planning") {
        setBanner(tCoachResourceEditor("chat_protocol_unavailable")); return null;
      }
      const original = resourceCanvas.objects.find((object) => object.key === activeCanvasSelection.key);
      try {
        if (!original || original.pointer.userId !== authSession?.me?.id || original.pointer.teamId !== authSession?.me?.team_id
          || original.pointer.workspaceId !== activeWorkspaceId || original.pointer.sessionId !== activeSessionId)
          throw new CoachCanvasNotReadyError("page_not_ready");
        const binding = captureCoachCanvasBinding({ user_id: authSession?.me?.id ?? "", team_id: authSession?.me?.team_id ?? "",
          workspace_id: activeWorkspaceId ?? "", session_id: activeSessionId ?? "" }, original.pointer.reference, original.editorRef.current);
        return { ...snapshot, draftChatBinding: null, coachCanvasBinding: binding };
      } catch (error) {
        const unsaved = error instanceof CoachCanvasNotReadyError && error.code === "local_changes";
        // An editor that can save itself offers "Save and continue" instead of a passing notice (C6-e).
        if (unsaved && original?.editorRef.current?.save) {
          setCanvasSaveFailed(false);
          setCanvasSaveBlock({ key: original.key, incarnation: original.incarnation });
        }
        else setBanner(tCoachResourceEditor(unsaved ? "chat_save_first" : "chat_not_ready"));
        return null;
      }
    }
    if (!selectedSettingsTarget) return snapshot;
    if (actionKind !== "start" || hasReferenceInput || snapshot.planningMode === "planning") {
      setBanner(tSettingsCanvas("protocol_unavailable"));
      return null;
    }
    try {
      if (!settingsBridge) return null;
      return { ...snapshot, draftChatBinding: null, workspaceSettingsBinding: settingsBridge.capture(selectedSettingsTarget) };
    } catch (error) {
      setBanner(tSettingsCanvas(error instanceof WorkspaceSettingsStageError && error.code === "local_changes" ? "local_changes" : "page_not_ready"));
      return null;
    }
  };
  const runner = useTurnRunner({
    mode: SSE_MODE,
    scenarioId: SSE_MODE === "mock" ? scenarioId : null,
    replayKey,
    message: SSE_MODE === "sse" ? message : null,
    attachments: SSE_MODE === "sse" ? pendingAttachments : undefined,
    workspaceId: activeWorkspaceId,
    memberUid: MEMBER_OVERRIDE,
    sendTick,
    activeSessionId,
    draftChatBinding: activeDraftChatBinding,
    resetSignal,
    hydrate,
    hydrateTick,
    archivedTurnsPatch,
    currentTurnHydrationPatch,
    submissionProtocolEnabled: submissionProtocolEnabled || pendingReferenceDocument !== null,
    referenceInputDocument: pendingReferenceDocument,
    referenceSourceDraftRevision: pendingReferenceSourceRevision,
    referenceSubmissionEnabled: referenceInputEnabled,
    submissionRecoveryScope,
    submissionWireSnapshot: pendingSendWireSnapshot ?? recoveredSubmission?.wireSnapshot ?? null,
    onWorkspaceSettingsTurnAccepted: settingsBridge?.acceptTurn,
    onWorkspaceSettingsAccessDenied: settingsBridge?.denyBinding,
    replaySourceTurnId: pendingReplaySourceTurnId,
    chatAction: chatActionAttempt?.action ?? null,
    chatActionWorkspaceId: chatActionAttempt?.workspaceId ?? null,
    chatActionScopeKey: chatActionAttempt?.scopeKey ?? null,
    currentChatScopeKey: composerDraftScopeKey,
    sendAttemptKey,
    onChatActionUnknown: (action) => {
      const pending = (
        chatActionAttemptRef.current?.action.actionId === action.actionId
          ? chatActionAttemptRef.current
          : null
      )
        ?? inFlightChatActionsRef.current.get(action.actionId)
        ?? Array.from(inMemoryChatActionsRef.current.values()).find(
          (candidate) => candidate.action.actionId === action.actionId,
      )
        ?? null;
      if (!pending) return;
      retainChatActionForRecovery(pending, "lookup_pending");
    },
    onChatActionSettled: async (actionId, disposition) => {
      const controller = inFlightChatActionsRef.current.get(actionId)
        ?? chatActionAttemptRef.current
        ?? recoveredChatActionRef.current;
      if (!controller || controller.action.actionId !== actionId) {
        return "deferred" as const;
      }
      return settleKnownChatActionOutcome(controller, disposition);
    },
    onReferenceSubmissionAccepted: async (attemptKey) => {
      const cleanup = referenceRecoveryCleanups.current.get(attemptKey);
      if (!cleanup) return false;
      const outcome = await clearAcceptedComposerReferenceDraft(
        cleanup.scope,
        {
          input_document: cleanup.inputDocument,
          attachments: cleanup.attachments,
          sourceDraftRevision: cleanup.revision,
        },
      );
      if (outcome === "unbound") return false;
      referenceRecoveryCleanups.current.delete(attemptKey);
      if (composerDraftScopeSignature(cleanup.scope) === composerDraftScopeKeyRef.current) {
        setDraftReconcileEpoch((epoch) => epoch + 1);
      }
      return true;
    },
    onDocumentRevisionSubmissionAccepted: async (attemptKey, scopeKey) => {
      const cleanup = documentRevisionCleanups.current.get(attemptKey);
      const pending = cleanup?.pending;
      const target = pending?.wireSnapshot?.documentRevisionTarget;
      if (!cleanup || composerDraftScopeSignature(cleanup.scope) !== scopeKey || !pending?.sourceDraftRevision || !target) return false;
      const result = await clearAcceptedComposerDocumentRevisionDraft(cleanup.scope, {
        message: pending.message, target, sourceDraftRevision: pending.sourceDraftRevision,
      });
      if (result === "unbound") return false;
      documentRevisionCleanups.current.delete(attemptKey);
      if (scopeKey === composerDraftScopeKeyRef.current) setDraftReconcileEpoch((epoch) => epoch + 1);
      return true;
    },
    onSubmissionPending: handleSubmissionPending,
    onSubmissionCleared: handleSubmissionCleared,
    onSendInitSettled: handleSendInitSettled,
  });

  const contextMaintenance = useContextMaintenance({
    scope: submissionRecoveryScope,
    blocked: Boolean(runner.running || runner.clarificationPending || sendReadOnlyRef.current || chatActionAttempt || recoveredChatAction || recoveredSubmission || sendChatInFlightRef.current),
    attach: runner.attachContextMaintenance,
    onMessage: setBanner,
  });

  // ② clear the loading skeleton once the destination is actually on screen: the
  // runner has adopted the target session, OR a report-only / unavailable placeholder
  // took over. Clearing here (not in the fetch handler) closes the 1-frame gap where
  // the skeleton dropped before runner.started flipped and briefly flashed the home
  // hero / previous session. (Inlines showingReportOnly — it's defined after an early
  // return below, so it can't be referenced from this unconditional hook.)
  useEffect(() => {
    if (loadingSid === null) return;
    const reportOnlyReady = Boolean(reportOnlySessionId && activeSessionId === reportOnlySessionId);
    if (runner.sessionId === loadingSid || reportOnlyReady || reportUnavailableSid) {
      setLoadingSid(null);
    }
  }, [loadingSid, runner.sessionId, reportOnlySessionId, activeSessionId, reportUnavailableSid]);

  // cca P2 (C2 mount): the Lead opened the create-tool card → surface it as an overlay.
  // Local open state seeded by a fresh runner.createToolOpen identity (the runner state isn't
  // cleared; we gate rendering on the local flag so closing the card doesn't re-trigger).
  const [createCardOpen, setCreateCardOpen] = useState(false);
  const [createCardHint, setCreateCardHint] = useState<string | null>(null);
  const [createCardKind, setCreateCardKind] = useState<"openapi" | "mcp" | null>(null);
  // external-capability-onboarding §3.1 — curated preset catalog, fetched when the card opens.
  const connectorPresets = useConnectorPresets(createCardOpen);
  useEffect(() => {
    if (runner.createToolOpen) {
      setCreateCardHint(runner.createToolOpen.spec_url_hint);
      setCreateCardKind(runner.createToolOpen.kind_hint);
      setCreateCardOpen(true);
    }
  }, [runner.createToolOpen]);

  const selectAgentCanvas = useCallback((key: string) => {
    if (!agentCanvas.objects.some((object) => object.key === key)) return;
    canvasOpenIntentRef.current += 1;
    documentCanvas.deselect();
    resourceCanvas.select(null);
    operationCanvas.select(null);
    agentCanvas.select(key);
    const object = agentCanvas.objects.find((item) => item.key === key);
    if (object) showDraftInUrl(object.payload.draft_id);
    setSubagentCardOpen(false);
    setCanvasResultsSelected(false);
    settingsCanvas.deselect();
  }, [agentCanvas.select, agentCanvas.objects, showDraftInUrl, settingsCanvas.deselect, operationCanvas.select, resourceCanvas.select, documentCanvas.deselect]);
  const closeAgentCanvas = useCallback((key: string, incarnation: string) => {
    const selected = activeCanvasSelectionRef.current;
    const closed = agentCanvas.close(key, incarnation);
    if (closed?.selectionChanged && selected?.kind === "agent_draft" && selected.key === key) showDraftInUrl(closed.selectedDraftId);
  }, [agentCanvas.close, showDraftInUrl]);
  const openAgentCanvas = async (payload: CreateSubagentOpenPayload, initialDraft?: ManualSubagentDraftDetail) => {
    const intent = ++canvasOpenIntentRef.current;
    const originalScope = canvasScopeRef.current;
    if (!multipleCanvasEnabled && agentCanvas.objects.some((object) => object.payload.draft_id !== payload.draft_id)) {
      const allowed = await confirmCanvasNavigation();
      if (intent !== canvasOpenIntentRef.current || originalScope !== canvasScopeRef.current) return;
      if (!allowed) {
        showDraftInUrl(selectedAgentObject?.payload.draft_id ?? null);
        return;
      }
      agentCanvas.clear();
    }
    documentCanvas.deselect();
    resourceCanvas.select(null);
    operationCanvas.select(null);
    agentCanvas.open(payload, true, initialDraft);
    if (payload.draft_id) showDraftInUrl(payload.draft_id);
    setSubagentCardOpen(false);
    setCanvasResultsSelected(false);
    settingsCanvas.deselect();
  };
  const consumedAgentOpenRef = useRef<CreateSubagentOpenPayload | null>(null);
  useEffect(() => {
    if (!canvasScopeKey || !runner.subagentCreatorOpen || runner.sessionId !== activeSessionId
      || consumedAgentOpenRef.current === runner.subagentCreatorOpen) return;
    consumedAgentOpenRef.current = runner.subagentCreatorOpen;
    setConfigDraftId(null);
    if (runner.subagentCreatorOpen.draft_id) {
      // Background effects update the original retained editor without changing selection.
      agentCanvas.open(runner.subagentCreatorOpen, false);
    } else if (!agentCanvas.objects.length && !subagentCardOpen) {
      setSubagentCardPayload(runner.subagentCreatorOpen);
      setSubagentCardOpen(true);
    }
  }, [runner.subagentCreatorOpen, runner.sessionId, activeSessionId, canvasScopeKey, agentCanvas.open]);
  // workflow-solidify S2 (mount): the Lead opened the solidify card → surface it as an overlay.
  // turn_id resolution: the event carries the distill-target (null on conversational entry); the
  // chip entry passes the real completed turn_id. Same local-flag gating so closing doesn't re-trigger.
  const [solidifyCardOpen, setSolidifyCardOpen] = useState(false);
  // Configuration copilot batch 1-b: ConfigDrawer host (same layer as the three cca cards). Open requests come from chip clicks (explicit) and
  // wire-layer live events (once automatically, no reopening after dismissal); the drawer mounts in ChatWorkbench's slot.
  const [configDraftId, setConfigDraftId] = useState<string | null>(null);
  useEffect(() => subscribeOpenDraft((draftId) => {
    setSubagentCardOpen(false);
    setConfigDraftId(draftId);
  }), []);
  const [solidifyTurnId, setSolidifyTurnId] = useState<string | null>(null);
  /** S2: target session for solidifying the entire conversation (mutually exclusive with solidifyTurnId — exactly one is nonempty). */
  const [solidifySessionId, setSolidifySessionId] = useState<string | null>(null);
  const [solidifyHint, setSolidifyHint] = useState<string | null>(null);
  // Surface B gate — "固化此流程" chip (single-turn + entire-conversation entries share this variable).
  // Since 2026-09-01, read `capabilities.workflow_solidify` (= workflow_creator &&
  // BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY, **off by default**, per the user's request to hide the feature first).
  // ⚠ Do not revert to `workflow_creator`: that is the **family flag** for solidification + team skill governance,
  // Disabling it also removes the team group/approval area and /settings/workflows management page (where solidified artifacts live).
  // The backend implicitly disables the SSE card-opening path through the same flag pair (trigger tool unregistered); this only controls the manual chip.
  const [solidifyEnabled, setSolidifyEnabled] = useState(() =>
    Boolean(authSession?.me?.capabilities?.workflow_solidify),
  );
  // content-permissions wave 2 gate — the ReportUnavailable request panel only shows
  // when BOWHEAD_ACCESS_REQUESTS is on (server-side /me capability); flag off ⇒ the intake
  // route is unmounted, so don't offer a request that 404s.
  const [accessRequestsEnabled, setAccessRequestsEnabled] = useState(() =>
    Boolean(authSession?.me?.capabilities?.access_requests),
  );
  useEffect(() => {
    if (authSession?.me) {
      setSolidifyEnabled(
        Boolean(authSession.me.capabilities?.workflow_solidify),
      );
      setAccessRequestsEnabled(
        Boolean(authSession.me.capabilities?.access_requests),
      );
      return;
    }
    if (authSession) return;
    let cancelled = false;
    void fetchMe()
      .then((u) => {
        if (!cancelled) {
          setSolidifyEnabled(Boolean(u.capabilities?.workflow_solidify));
          setAccessRequestsEnabled(Boolean(u.capabilities?.access_requests));
        }
      })
      .catch(() => {
        /* anon / transient — leave chip hidden */
      });
    return () => {
      cancelled = true;
    };
  }, [authSession]);
  useEffect(() => {
    if (runner.solidifyOpen) {
      setSolidifyTurnId(runner.solidifyOpen.turn_id);
      setSolidifySessionId(null);  // Lead-initiated cards always target a single turn.
      setSolidifyHint(runner.solidifyOpen.hint);
      setSolidifyCardOpen(true);
    }
  }, [runner.solidifyOpen]);

  // Stop handler: persist the cancel intent, then keep SSE attached until the
  // exact worker terminal frame arrives.
  const handleCancel = async () => {
    if (
      !runner.turnId
      || !runner.executionGeneration
      || !cancelTeamId
      || !cancelUserId
      || cancelPendingRef.current
    ) return false;
    cancelPendingRef.current = true;
    const cancelKey = `${runner.turnId}:${runner.executionGeneration}`;
    if (cancelRequestRef.current?.key !== cancelKey) {
      cancelRequestRef.current = {
        key: cancelKey,
        record: getOrCreateCancelRequest(
          cancelTeamId,
          cancelUserId,
          runner.turnId,
          runner.executionGeneration,
        ),
      };
    }
    const requestRecord = cancelRequestRef.current.record;
    let cancellationApplied = false;
    try {
      const outcome = await cancelTurn(
        runner.turnId,
        runner.executionGeneration,
        requestRecord.cancelRequestId,
      );
      if (!outcome.durable) throw new Error("turn cancellation was not durable");
      cancellationApplied = shouldApplyComposerCancellation(outcome);
      if (cancellationApplied) {
        if (cancelTerminalKeysRef.current.has(cancelKey)) {
          clearCancelRequest(requestRecord);
          return false;
        }
        const accepted = compareAndSetCancelRequestState(
          requestRecord,
          "accepted_pending_terminal",
        );
        if (!accepted) return false;
        if (cancelRequestRef.current?.record.cancelRequestId !== requestRecord.cancelRequestId) {
          return false;
        }
        cancelRequestRef.current.record = accepted;
        setCancelRequestedKey(cancelKey);
      } else if (shouldClearComposerCancellation(outcome)) {
        clearCancelRequest(requestRecord);
        if (cancelRequestRef.current?.record.cancelRequestId === requestRecord.cancelRequestId) {
          cancelRequestRef.current = null;
        }
      }
    } catch (error) {
      console.warn("turn cancellation failed", error);
      toast({ kind: "error", text: tComposer("stop_failed") });
      return false;
    } finally {
      cancelPendingRef.current = false;
    }
    // The Turn may have completed durably before its delayed done frame reached
    // this tab. A durable noop means Stop lost that race: keep SSE open and let
    // the exact terminal status arrive instead of overwriting it as cancelled.
    if (!cancellationApplied) return false;
    // Feishu 07.06 #7: Stop means Cancel Execution. Backend cancel_turn cascades durable cancellation (stamps the plan
    // CANCELLED → run_plan stops itself + F5 does not revive it); frontend optimistically removes the running plan card for immediate feedback.
    // Review[4]: optimistically remove a card **only** with exactly 1 running plan — backend cancels only the plan bound to this turn;
    // removing all cards when multiple plans run would wrongly remove unrelated plan cards (still running in the background).
    // Leave multiple plans (rare; turns are serial) to backend cascade + reconcile/SSE correction.
    const _running = runningPlanIds();
    if (_running.length === 1) dismissPlan(_running[0]);
    // Keep the stream and stable request id until the worker's terminal frame.
    // HTTP status=cancelled means accepted_pending_terminal.
    return true;
  };

  useEffect(() => {
    if (!cancelTeamId || !cancelUserId) return;
    let disposed = false;
    for (const pending of listPendingCancelRequests(cancelTeamId, cancelUserId)) {
      void (async () => {
        try {
          let journal = compareAndSetCancelRequestState(pending, "reconciling");
          if (!journal) return;
          let outcome;
          try {
            outcome = await getCancelTurnOutcome(
              pending.turnId,
              pending.executionGeneration,
              pending.cancelRequestId,
            );
          } catch {
            // The page may have closed after journaling but before POST. Replay
            // the same deterministic request; every other failure stays
            // pending for the next mount rather than guessing an outcome.
            outcome = await cancelTurn(
              pending.turnId,
              pending.executionGeneration,
              pending.cancelRequestId,
            );
          }
          if (outcome.durable) {
            const pendingKey = `${pending.turnId}:${pending.executionGeneration}`;
            if (shouldClearComposerCancellation(outcome)) {
              clearCancelRequest(journal);
            } else if (
              !disposed
              && !cancelTerminalKeysRef.current.has(pendingKey)
              && shouldApplyComposerCancellation(outcome)
            ) {
              journal = compareAndSetCancelRequestState(
                journal,
                "accepted_pending_terminal",
              );
              if (!journal) return;
              cancelRequestRef.current = {
                key: pendingKey,
                record: journal,
              };
              setCancelRequestedKey(pendingKey);
            }
          }
        } catch {
          // Unknown transport/auth state is retryable. Keep the journal entry.
        }
      })();
    }
    return () => {
      disposed = true;
    };
  }, [cancelTeamId, cancelUserId]);

  useEffect(() => {
    if (!runner.turnId || !runner.executionGeneration) return;
    const runnerKey = `${runner.turnId}:${runner.executionGeneration}`;
    const terminal = !runner.running && Boolean(runner.terminalStatus);
    if (terminal) {
      cancelTerminalKeysRef.current.add(runnerKey);
      if (cancelTeamId && cancelUserId) {
        for (const pending of listPendingCancelRequests(cancelTeamId, cancelUserId)) {
          if (
            pending.turnId === runner.turnId
            && pending.executionGeneration === runner.executionGeneration
          ) clearCancelRequest(pending);
        }
      }
      if (cancelRequestRef.current?.key === runnerKey) {
        cancelRequestRef.current = null;
      }
      if (cancelRequestedKey === runnerKey) setCancelRequestedKey(null);
    } else if (cancelRequestedKey && cancelRequestedKey !== runnerKey) {
      setCancelRequestedKey(null);
    }
  }, [
    cancelRequestedKey,
    cancelTeamId,
    cancelUserId,
    runner.executionGeneration,
    runner.running,
    runner.terminalStatus,
    runner.turnId,
  ]);

  // Runner receives the real sessionId after the first message → persist in page state for the next follow-up.
  // Synchronize **only** while active is null (no declared session); otherwise, after "+ 新建" clears active to
  // null, runner.sessionId retains the old ID for one frame, and this effect restores active to
  // that old ID, sending the next new message to the wrong session. Track with a ref: I Just Cleared Active; The Next
  // runner.sessionId Change Is A Residual Old Value; Do Not Synchronize — bump on resetSignal or pickSession.
  const ignoreSyncRef = useRef(false);
  useEffect(() => {
    // Extracted condition in lib/runner-session-sync.ts (frame-sequence regression lock).
    const next = reconcileRunnerSessionSync(
      { ignoring: ignoreSyncRef.current },
      { runnerSessionId: runner.sessionId, activeSessionId },
    );
    ignoreSyncRef.current = next.ignoring;
    if (next.adopt) {
      activeSessionIdRef.current = next.adopt;
      setActiveSessionId(next.adopt);
    }
  }, [runner.sessionId, activeSessionId]);

  // Turn starts / ends → bump a full sessions refresh.
  useEffect(() => {
    if (runner.turnId && runner.turnId !== lastTurnId.current) {
      lastTurnId.current = runner.turnId;
      setSessionsRefreshKey((k) => k + 1);
    }
  }, [runner.turnId]);
  useEffect(() => {
    if (runner.summary) {
      setSessionsRefreshKey((k) => k + 1);
    }
  }, [runner.summary]);
  useSessionSettleRefresh({
    finished: Boolean(runner.summary && !runner.running && runner.sessionId),
    turnId: runner.turnId,
    rowState: remoteGroups.flatMap((group) => group.items).find((item) => item.id === runner.sessionId)?.state,
    refreshKey: sessionsRefreshKey,
    refresh: () => setSessionsRefreshKey((k) => k + 1),
  });

  // Optimistic injection: when a turn starts but the backend session has not appeared in /api/v1/sessions yet,
  // synthesize a local "streaming…" entry so users see it immediately.
  const groups = useMemo<SessionGroup[]>(() => {
    if (!runner.started || !runner.sessionId) return remoteGroups;

    const sid = runner.sessionId;
    const exists = remoteGroups.some((g) =>
      g.items.some((it) => it.id === sid),
    );
    if (exists) return remoteGroups;

    const optimistic: SessionItem = {
      id: sid,
      title: runner.sessionTitle || runner.user || ts("new_session"),
      intents: runner.intent ? [runner.intent] : [],
      time: runner.running ? ts("streaming") : ts("just_now"),
      state: runner.running ? "active" : "done",
    };
    const today = remoteGroups.find((g) => g.id === "today");
    if (today) {
      return remoteGroups.map((g) =>
        g.id === "today" ? { ...g, items: [optimistic, ...g.items] } : g,
      );
    }
    return [
      { id: "today", label: "TODAY", items: [optimistic] },
      ...remoteGroups,
    ];
  }, [remoteGroups, runner.started, runner.sessionId, runner.sessionTitle,
      runner.user, runner.intent, runner.running]);

  // file-management-v2 P2 "在新会话中使用": files page writes a one-time sessionStorage key before navigating here.
  //
  // Consumption must happen at the **page** layer: ChatWorkbench mounts only when `runner.started || showingReportOnly`,
  // but navigation from the files page lands on the empty home page, where it is not mounted — observed in 0.87.0 production dogfooding:
  // the key is never consumed; a later conversation mount picks up the leftover key (attaching the file to a session the user never intended
  // to attach it to). Read and delete here, covering both landing paths:
  //   · Existing session → workbench mounts directly → preattach a done-state chip in the composer.
  //   · Empty home page → handleSend includes it with the prompt box's first message (see below).
  const [handoffAttach, setHandoffAttach] = useState<AttachmentRef[] | null>(null);
  const [externalComposerPrefill, setExternalComposerPrefill] = useState<string | null>(null);
  // C1 tab markers reported by Canvas objects (save state, pending suggestions); keyed by object key.
  const [canvasMarkers, setCanvasMarkers] = useState<Record<string, { marker: CanvasObjectMarker; statusLabel: string | null }>>({});
  /** Operation result tab names reported after each read (outcome-dependent); keyed by object key. */
  const [operationLabels, setOperationLabels] = useState<Record<string, string>>({});
  const reportCanvasMarker = useCallback((key: string, value: { marker: CanvasObjectMarker; statusLabel: string | null } | null) => {
    setCanvasMarkers((previous) => {
      const current = previous[key] ?? null;
      if (current?.marker === value?.marker && current?.statusLabel === value?.statusLabel) return previous;
      const next = { ...previous };
      if (value) next[key] = value;
      else delete next[key];
      return next;
    });
  }, []);
  const canvasClosersRef = useRef(new Map<string, () => void>());
  const { pending: pendingAgentUse, settle: settleAgentUse } = useComposerAgentUseHandoff();
  const [externalAgentDocument, setExternalAgentDocument] = useState<InputDocument | null>(null);
  const [agentUseNotice, setAgentUseNotice] = useState<string | null>(null);
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("bowhead:composer-attach");
      if (!raw) return;
      sessionStorage.removeItem("bowhead:composer-attach");
      const parsed = canonicalizeComposerAttachmentRefs(JSON.parse(raw));
      if (parsed && parsed.length > 0) {
        setHandoffAttach(parsed);
      }
    } catch {
      /* Storage unavailable or corrupt payload → no preattachment; silent. */
    }
  }, []);

  // feedback-triage-2026-08-24 (customer reported「点『用对话修改』跳到新对话，切到已有对话时
  // 输入框出现『帮我修改技能 XXX』」): second instance of the same pitfall.
  //
  // The skill drawer writes one-time key `bowhead:composer-prefill` before navigating to `/`, but its consumer previously lived
  // in ChatWorkbench's mount effect — that component mounts only when `runner.started ||
  // showingReportOnly`; navigation lands on the empty home page, where it does not mount. No one consumed the key,
  // which remained in sessionStorage until users switched to any existing session; as the workbench mounted,
  // it picked up the stale prefill and inserted it into that session's input.
  //
  // The composer-attach comments above already explained this pitfall clearly (file-management-v2 P2
  // hit exactly the same issue); the fix was moving consumption up to the page layer. The skill key
  // was not moved up then. Fix that now with externalComposerPrefill, feeding both the workbench and
  // empty-home-page paths, and clear it via onPrefillConsumed.
  useEffect(() => {
    const pending = takeComposerPrefill();
    if (pending) setExternalComposerPrefill(pending);
  }, []);
  useEffect(() => {
    if (!pendingAgentUse || authSession?.status !== "authenticated" || !submissionRecoveryScope) return;
    // A request staged with the Agent survives an unavailable Agent as plain composer text.
    const keepStagedText = () => {
      if (pendingAgentUse.text) setExternalComposerPrefill(pendingAgentUse.text);
    };
    if (!composerAgentHandoffAdmitted(pendingAgentUse.agentId, authSession?.me?.capabilities?.composer)) {
      setAgentUseNotice(agentUseUnavailableText);
      keepStagedText();
      settleAgentUse();
      return;
    }
    const controller = new AbortController();
    const resolve = async () => {
      try {
        const document = await resolveAgentReferenceDocument(submissionRecoveryScope, pendingAgentUse, controller.signal);
        if (controller.signal.aborted) return;
        if (document) {
          setExternalAgentDocument(document);
          setAgentUseNotice(null);
          settleAgentUse();
          return;
        }
        setAgentUseNotice(agentUseUnavailableText);
        keepStagedText();
        settleAgentUse();
      } catch {
        if (!controller.signal.aborted) {
          setAgentUseNotice(agentUseUnavailableText);
          keepStagedText();
          settleAgentUse();
        }
      }
    };
    void resolve();
    return () => controller.abort();
  }, [agentUseUnavailableText, authSession?.me?.capabilities?.composer, authSession?.status, pendingAgentUse, settleAgentUse, submissionRecoveryScope]);

  type SendChatOptions = {
    /** Continue button: saved helper runs this visible "continue" message resumes. */
    continueCheckpointIds?: string[] | null;
    replaySourceTurnId?: string | null;
    preparedTerminalAction?: PendingChatActionController | null;
    submissionContext?: ComposerDraftSubmissionContext | null;
    referenceRecovery?: RecoveredReferenceSubmission | null;
  };
  const chatStartLaunchBlocked = (
    preparedActionId: string | null,
    supersededActionId: string | null = null,
  ): boolean => {
    if (workspace.loading || workspace.error || !activeWorkspaceId) return true;
    if (sendReadOnlyRef.current || contextMaintenance.busy || contextMaintenance.pending) return true;
    if (runner.running && runner.operationKind === "context_maintenance") {
      setBanner(tChat("compact_context_busy"));
      return true;
    }
    if (submissionRecoveryScope && !submissionRecoveryChecked) return true;
    const unresolvedAction = [
      chatActionAttemptRef.current,
      recoveredChatActionRef.current,
      inMemoryChatActionsRef.current.get(composerDraftScopeKeyRef.current),
    ].find((candidate) => (
      candidate?.scopeKey === composerDraftScopeKeyRef.current
    )) ?? null;
    if (
      unresolvedAction
      && unresolvedAction.action.actionId !== preparedActionId
      && unresolvedAction.action.actionId !== supersededActionId
    ) return true;
    return SSE_MODE === "sse" && sendChatInFlightRef.current;
  };
  const sendChat = async (
    text: string,
    attachments?: AttachmentRef[],
    draftRevision: string | null = null,
    {
      continueCheckpointIds = null,
      replaySourceTurnId = null,
      preparedTerminalAction = null,
      submissionContext = null,
      referenceRecovery = null,
    }: SendChatOptions = {},
  ) => {
    if (chatStartLaunchBlocked(
      preparedTerminalAction?.action.actionId ?? null,
    )) return false;
    if (referenceRecovery && (
      referenceRecovery.clientSubmissionId !== recoveredSubmissionRef.current?.clientSubmissionId
      || referenceRecovery.contentChecksum !== recoveredSubmissionRef.current?.contentChecksum
    )) return false;
    // Capture before asynchronous preparation. A retained object can be
    // selected or edited while preflight runs, without retargeting this send.
    // An empty binding is also a snapshot: do not attach a later selection.
    const operationIntent = { scope: canvasScopeKey, generation: canvasOpenIntentRef.current, sessionId: activeSessionId, submittedText: text };
    const capturedWireSnapshot = referenceRecovery?.wireSnapshot
      ?? recoveredSubmission?.wireSnapshot
      ?? submissionContext?.wireSnapshot
      ?? captureCanvasSubmissionWire("start", Boolean(submissionContext?.referenceInputDocument), undefined, !replaySourceTurnId && !preparedTerminalAction);
    if (!capturedWireSnapshot) return false;
    // Continue is an ordinary start: it never carries planning mode or an open draft, settings or resource editor.
    const sendWireSnapshot = continueCheckpointIds?.length
      ? {
          ...capturedWireSnapshot,
          planningMode: null,
          draftChatBinding: null,
          workspaceSettingsBinding: null,
          coachCanvasBinding: null,
          continueCheckpointIds,
        }
      : capturedWireSnapshot;
    const documentRevisionTarget = sendWireSnapshot.documentRevisionTarget;
    if (documentRevisionTarget && (
      SSE_MODE !== "sse" || !submissionRecoveryScope || documentRevisionTarget.session_id !== activeSessionId
      || replaySourceTurnId || preparedTerminalAction || referenceRecovery || submissionContext?.referenceInputDocument
      || attachments?.length || sendWireSnapshot.draftChatBinding || sendWireSnapshot.workspaceSettingsBinding
      || sendWireSnapshot.coachCanvasBinding || sendWireSnapshot.planningMode === "planning"
      || !sendWireSnapshot.sourceDraftRevision
      || (!recoveredSubmission && (!draftRevision || draftRevision !== sendWireSnapshot.sourceDraftRevision
        || submissionContext?.persistenceAvailable !== true || submissionContext.writeProtocol !== "legacy_v0"))
    )) return false;
    if (sendWireSnapshot.draftChatBinding?.dirty) {
      setBanner(tSubagents("draft_chat_save_first"));
      return false;
    }
    if (sendWireSnapshot.coachCanvasBinding && (replaySourceTurnId || preparedTerminalAction || referenceRecovery
      || submissionContext?.referenceInputDocument || sendWireSnapshot.planningMode === "planning")) {
      setBanner(tCoachResourceEditor("chat_protocol_unavailable")); return false;
    }
    if (sendWireSnapshot.workspaceSettingsBinding && (replaySourceTurnId || preparedTerminalAction || referenceRecovery
      || submissionContext?.referenceInputDocument || sendWireSnapshot.planningMode === "planning")) {
      setBanner(tSettingsCanvas("protocol_unavailable"));
      return false;
    }
    const sendScopeKey = composerDraftScopeKey;
    const referenceSource = referenceRecovery?.input_document ?? submissionContext?.referenceInputDocument;
    const referenceDocument = referenceSource ? parseInputDocument(referenceSource) : null;
    const referenceScopeKey = composerDraftScopeKeyRef.current;
    if (referenceDocument && (
      SSE_MODE !== "sse" || !referenceInputEnabled || !submissionRecoveryScope || replaySourceTurnId
      || preparedTerminalAction || (!referenceRecovery && (
        !draftRevision || submissionContext?.writeProtocol !== "legacy_v0"
        || submissionContext.persistenceAvailable !== true
        || submissionContext.referenceScopeKey !== referenceScopeKey
      ))
      || inputDocumentDisplayText(referenceDocument) !== text
    )) return false;
    // The home-page prompt box supplies no attachment parameter → include the files preattached by "在新会话中使用",
    // or the first message sent after arriving from the files page omits that file, making the feature ineffective.
    // Composer sends already include preattached chips in attachments, so no duplication.
    if (attachments === undefined && handoffAttach && handoffAttach.length > 0) {
      attachments = handoffAttach;
    }
    setHandoffAttach(null);
    setReportOnlySessionId(null);
    if (SSE_MODE === "mock") {
      setScenarioId(pickScenarioFromText(text));
      setReplayKey((k) => k + 1);
      return true;
    } else {
      sendChatInFlightRef.current = true;
      try {
      const effectiveReplaySourceTurnId = replaySourceTurnId
        ?? (
          recoveredSubmission?.submissionKind === "replay"
          && recoveredSubmission.message === text
            ? recoveredSubmission.replaySourceTurnId ?? null
            : null
        );
      let preparedAction = preparedTerminalAction;
      if (!preparedAction && !effectiveReplaySourceTurnId && !recoveredSubmission && !referenceDocument && !documentRevisionTarget) {
        const prepared = await prepareTerminalChatAction({
          actionKind: "start",
          requestedSessionId: activeSessionId,
          message: text,
          attachments: attachments ?? [],
          draftRevision,
          submissionContext,
          draftChatBinding: sendWireSnapshot.draftChatBinding,
          workspaceSettingsBinding: sendWireSnapshot.workspaceSettingsBinding,
          coachCanvasBinding: sendWireSnapshot.coachCanvasBinding,
        });
        if (prepared.blocked) return false;
        preparedAction = prepared.controller;
      }
      if (composerDraftScopeKeyRef.current !== sendScopeKey) return false;
      chatActionAttemptRef.current = preparedAction;
      setChatActionAttempt(preparedAction);
      if (preparedAction) {
        inFlightChatActionsRef.current.set(
          preparedAction.action.actionId,
          preparedAction,
        );
      }
      if (preparedAction && !preparedAction.persistenceScope) {
        inMemoryChatActionsRef.current.set(preparedAction.scopeKey, preparedAction);
      }
      const attemptKey = preparedAction?.action.actionId
        ?? `send_${++sendAttemptSequenceRef.current}`;
      operationSendIntents.current.set(attemptKey, operationIntent);
      if (operationSendIntents.current.size > 8) operationSendIntents.current.delete(operationSendIntents.current.keys().next().value!);
      if (documentRevisionTarget && submissionRecoveryScope) documentRevisionCleanups.current.set(attemptKey, {
        scope: { ...submissionRecoveryScope }, pending: null,
      });
      const referenceSourceRevision = referenceRecovery?.sourceDraftRevision
        ?? draftRevision;
      if (
        referenceDocument
        && referenceSourceRevision
        && submissionRecoveryScope
      ) {
        referenceRecoveryCleanups.current.set(attemptKey, {
          scope: { ...submissionRecoveryScope },
          revision: referenceSourceRevision,
          inputDocument: structuredClone(referenceDocument),
          attachments: structuredClone(attachments ?? []),
        });
      }
      if (referenceDocument && referenceScopeKey !== composerDraftScopeKeyRef.current) {
        referenceRecoveryCleanups.current.delete(attemptKey);
        return false;
      }
      setSendAttemptKey(attemptKey);
      setPendingReferenceDocument(referenceDocument);
      setLastSentReferenceDocument(referenceDocument);
      setPendingReferenceSourceRevision(referenceDocument
        ? referenceSourceRevision
        : null);
      setPendingReplaySourceTurnId(effectiveReplaySourceTurnId);
      setPendingSendWireSnapshot(sendWireSnapshot);
      if (sendWireSnapshot.draftChatBinding) setDraftChatSubmissionError(null);
      setMessage(text);
      setPendingAttachments(attachments ?? []);
      setSendTick((k) => k + 1);
      return await new Promise<boolean>((resolve) => {
        sendInitResolversRef.current.set(attemptKey, resolve);
      });
      } finally {
        sendChatInFlightRef.current = false;
      }
    }
  };

  const handleSend = (
    text: string,
    attachments?: AttachmentRef[],
    draftRevision: string | null = null,
    submissionContext: ComposerDraftSubmissionContext | null = null,
  ) => sendChat(text, attachments, draftRevision, { submissionContext });

  const handleContinue = (checkpointIds: string[]) =>
    sendChat(tChat("continue_message"), undefined, null, { continueCheckpointIds: checkpointIds });

  const handleReplay = async (
    sourceTurnId: string,
    previewText: string,
    previewAttachments: AttachmentRef[],
  ) => {
    if (activeSessionId) {
      try {
        const bootstrap = await preflightChatExecution(activeSessionId);
        const replayAdmission = chatReplayAdmission(bootstrap);
        if (replayAdmission !== "legacy_v0") {
          // Replay has a different server-owned identity contract. Until it is
          // action-wired, every non-legacy authority decision fails closed.
          setBanner(tComposer(
            replayAdmission === "refresh_required"
              ? "action_refresh_required"
              : "action_unavailable",
          ));
          return false;
        }
      } catch {
        setBanner(tComposer("action_preflight_failed"));
        return false;
      }
    }
    return sendChat(previewText, previewAttachments, null, {
      replaySourceTurnId: sourceTurnId,
    });
  };

  // Canvas shortcuts enter the same visible Composer.  This gives them an
  // ordinary-draft revision before any Terminal action is prepared. Legacy
  // sessions retain the pre-rollout direct-send behavior.
  useEffect(() => {
    const handler = (event: Event) => {
      const text = (event as CustomEvent<{ text: string }>).detail?.text;
      if (!text) return;
      const hasExactSessionHint = Boolean(
        activeSessionId
        && sessionChatExecution?.sessionId === activeSessionId,
      );
      if (activeSessionId && !hasExactSessionHint) {
        const targetSessionId = activeSessionId;
        const targetScopeKey = composerDraftScopeKeyRef.current;
        void preflightChatExecution(targetSessionId)
          .then((bootstrap) => {
            if (composerDraftScopeKeyRef.current !== targetScopeKey) return;
            if (bootstrap?.write_protocol === "legacy_v0") {
              void handleSend(text);
            } else {
              setExternalComposerPrefill(text);
            }
          })
          .catch(() => {
            if (composerDraftScopeKeyRef.current !== targetScopeKey) return;
            setExternalComposerPrefill(text);
            setBanner(tComposer("action_preflight_failed"));
          });
        return;
      }
      const bootstrapHint = activeSessionId
        ? (hasExactSessionHint ? sessionChatExecution!.bootstrap : null)
        : authSession?.me?.chat_execution ?? null;
      if (shouldRouteSecondaryChatEntryThroughComposer(
        activeSessionId,
        hasExactSessionHint,
        bootstrapHint,
      )) {
        setExternalComposerPrefill(text);
      } else {
        void handleSend(text);
      }
    };
    window.addEventListener("canvas:followup", handler);
    return () => window.removeEventListener("canvas:followup", handler);
  }, [activeSessionId, authSession?.me?.chat_execution, handleSend, preflightChatExecution, sessionChatExecution, tComposer]);

  // Phase 1.4 Steer — wire onSteer to backend steer path. Fire-and-forget;
  // the backend emits an emoji_ack on the SAME turn's SSE bus so the
  // existing EventSource sees it. Falls back to handleSend if there's no
  // active session or workspace (rare; the backend would 400 anyway).
  type SteerChatOptions = {
    recovered?: RecoveredSubmission;
    recoveredScope?: SubmissionRecoveryScope;
    recoveredScopeKey?: string;
    preparedTerminalAction?: PendingChatActionController | null;
    submissionContext?: ComposerDraftSubmissionContext | null;
  };
  const sendSteerAttempt = async (
    text: string,
    attachments: AttachmentRef[] = [],
    draftRevision: string | null = null,
    {
      recovered,
      recoveredScope,
      recoveredScopeKey,
      preparedTerminalAction = null,
      submissionContext = null,
    }: SteerChatOptions = {},
  ) => {
    const steerScopeKey = recoveredScopeKey ?? composerDraftScopeKeyRef.current;
    if (steerScopeKey !== composerDraftScopeKeyRef.current) return false;
    if (contextMaintenance.busy || contextMaintenance.pending || (runner.running && runner.operationKind === "context_maintenance")) {
      setBanner(tChat("compact_context_busy"));
      return false;
    }
    const recoveredReference = recovered && "input_document" in recovered
      ? recovered as RecoveredReferenceSubmission
      : null;
    const referenceSource = recoveredReference?.input_document
      ?? submissionContext?.referenceInputDocument;
    const referenceDocument = referenceSource
      ? parseInputDocument(referenceSource)
      : null;
    const unresolvedAction = [
      chatActionAttemptRef.current,
      recoveredChatActionRef.current,
      inMemoryChatActionsRef.current.get(steerScopeKey),
    ].find((candidate) => candidate?.scopeKey === steerScopeKey) ?? null;
    if (
      unresolvedAction
      && preparedTerminalAction?.action.actionId
        !== unresolvedAction.action.actionId
    ) return false;
    const preparedSteerAction = preparedTerminalAction?.action.actionKind === "steer"
      ? preparedTerminalAction.action
      : null;
    const effectiveRecoveryScope = recovered ? recoveredScope : submissionRecoveryScope;
    const recoveredSessionId = effectiveRecoveryScope?.requestedSessionId;
    const target = preparedSteerAction
      ? {
          ok: true as const,
          workspaceId: preparedTerminalAction?.workspaceId ?? activeWorkspaceId,
          sessionId: preparedSteerAction.sessionId,
          turnId: preparedSteerAction.expectedTurnId,
        }
      : recovered
      && recovered.submissionKind === "steer"
      && recovered.expectedParentTurnId
      && recovered.expectedExecutionGeneration
      && recoveredSessionId
      ? {
          ok: true as const,
          workspaceId: effectiveRecoveryScope!.workspaceId,
          sessionId: recoveredSessionId,
          turnId: recovered.expectedParentTurnId,
        }
      : getSteerTarget({
          sseMode: SSE_MODE,
          running: runner.running,
          turnId: runner.turnId,
          activeWorkspaceId,
          activeSessionId,
          runnerSessionId: runner.sessionId,
          allowRunnerSessionClaim: !ignoreSyncRef.current,
        });
    if (!target.ok) {
      if (target.reason === "session_mismatch") {
        setBanner(ts("steer_session_changed"));
      } else if (target.reason === "not_running") {
        setBanner(ts("steer_completed"));
      } else if (target.reason === "missing_context") {
        setBanner(ts("steer_failed"));
      }
      return false;
    }
    let steerSubmissionId: string | null = null;
    const expectedExecutionGeneration = preparedSteerAction
      ?.expectedExecutionGeneration
      ?? recovered?.expectedExecutionGeneration
      ?? runner.executionGeneration;
    if (!expectedExecutionGeneration) {
      setBanner(ts("steer_completed"));
      return false;
    }
    const steerRecoveryScope = (submissionProtocolEnabled || referenceDocument) && effectiveRecoveryScope
      ? {
          ...effectiveRecoveryScope,
          requestedSessionId: target.sessionId,
        }
      : null;
    if (referenceDocument && (
      !referenceInputEnabled
      || !steerRecoveryScope
      || preparedTerminalAction
      || inputDocumentDisplayText(referenceDocument) !== text
      || (!recoveredReference && (
        !draftRevision || submissionContext?.writeProtocol !== "legacy_v0"
        || submissionContext.persistenceAvailable !== true
        || submissionContext.referenceScopeKey !== steerScopeKey
      ))
    )) return false;
    let terminalAction = preparedTerminalAction;
    if (!terminalAction && !recovered && !referenceDocument) {
      const prepared = await prepareTerminalChatAction({
        actionKind: "steer",
        requestedSessionId: target.sessionId,
        expectedTurnId: target.turnId,
        expectedExecutionGeneration,
        message: text,
        attachments,
        draftRevision,
        submissionContext,
      });
      if (prepared.blocked) return false;
      terminalAction = prepared.controller;
    }
    if (steerScopeKey !== composerDraftScopeKeyRef.current) return false;
    chatActionAttemptRef.current = terminalAction;
    setChatActionAttempt(terminalAction);
    if (terminalAction) {
      inFlightChatActionsRef.current.set(
        terminalAction.action.actionId,
        terminalAction,
      );
    }
    if (terminalAction && !terminalAction.persistenceScope) {
      inMemoryChatActionsRef.current.set(terminalAction.scopeKey, terminalAction);
    }
    const ownsTerminalPresentation = () => (
      composerDraftScopeKeyRef.current === steerScopeKey
      && (
        !terminalAction
        || terminalAction.scopeKey === composerDraftScopeKeyRef.current
      )
    );
    const retainTerminalActionForRecovery = (
      recoveryState: NonNullable<PendingChatActionController["recoveryState"]>,
    ) => {
      if (!terminalAction) return;
      retainChatActionForRecovery(terminalAction, recoveryState);
    };
    try {
      const { postSteerMessage } = await import("@/lib/api");
      if ((submissionProtocolEnabled || referenceDocument) && !terminalAction) {
        if (!steerRecoveryScope) return false;
        const referenceRecoveryDraft = referenceDocument
          ? captureReferenceSubmissionDraft(steerRecoveryScope, {
              schemaVersion: 2,
              input_document: referenceDocument,
              message: text,
              attachments,
              submissionKind: "steer",
              expectedParentTurnId: target.turnId,
              expectedExecutionGeneration,
              sourceDraftRevision: recoveredReference?.sourceDraftRevision
                ?? draftRevision,
              wireSnapshot: recoveredReference?.wireSnapshot
                ?? captureComposerSubmissionWireSnapshot(target.sessionId, false),
            })
          : null;
        const prepared = referenceRecoveryDraft
          ? await preparePendingReferenceSubmission(
              steerRecoveryScope,
              referenceRecoveryDraft,
            )
          : await preparePendingSubmission(
              steerRecoveryScope,
              {
                message: text,
                attachments,
                submissionKind: "steer",
                replaySourceTurnId: null,
                expectedParentTurnId: target.turnId,
                expectedExecutionGeneration,
              },
            );
        if (steerScopeKey !== composerDraftScopeKeyRef.current) return false;
        steerSubmissionId = prepared.clientSubmissionId;
        const pending = {
          clientSubmissionId: prepared.clientSubmissionId,
          contentChecksum: prepared.contentChecksum,
          createdAt: new Date().toISOString(),
          message: text,
          attachments,
          submissionKind: "steer",
          replaySourceTurnId: null,
          expectedParentTurnId: target.turnId,
          expectedExecutionGeneration,
          ...(referenceRecoveryDraft ? {
            schemaVersion: 2 as const,
            input_document: referenceRecoveryDraft.input_document,
            sourceDraftRevision: referenceRecoveryDraft.sourceDraftRevision,
            wireSnapshot: referenceRecoveryDraft.wireSnapshot,
          } : {}),
        } satisfies RecoveredSubmission | RecoveredReferenceSubmission;
        recoveredSubmissionRef.current = pending;
        setRecoveredSubmission(pending);
        setSubmissionOutcomeUnknown(false);
      }
      if (steerScopeKey !== composerDraftScopeKeyRef.current) return false;
      const r = await postSteerMessage(
        target.workspaceId,
        target.sessionId,
        text,
        target.turnId,
        expectedExecutionGeneration,
        attachments,
        undefined,
        steerSubmissionId,
        terminalAction?.action ?? null,
        referenceDocument,
        () => composerDraftScopeKeyRef.current === steerScopeKey
          && referenceInputEnabled,
      );
      if (steerSubmissionId && steerRecoveryScope) {
        const acceptedSubmissionId = steerSubmissionId;
        const settleSubmissionJournal = async () => {
          try {
            await clearPendingSubmission(
              steerRecoveryScope,
              acceptedSubmissionId,
            );
          } catch (error) {
            if (recoveredSubmissionRef.current?.clientSubmissionId === acceptedSubmissionId) {
              setSubmissionOutcomeUnknown(true);
            }
            throw error;
          }
          if (recoveredSubmissionRef.current?.clientSubmissionId === acceptedSubmissionId) {
            recoveredSubmissionRef.current = null;
            setSubmissionOutcomeUnknown(false);
          }
          setRecoveredSubmission((current) =>
            current?.clientSubmissionId === acceptedSubmissionId ? null : current,
          );
          setReferenceSubmissionPreview((current) =>
            current?.draft.clientSubmissionId === acceptedSubmissionId ? null : current,
          );
        };
        if (referenceDocument && r.status === "buffered" && submissionContext) {
          submissionContext.finalizeReferenceSubmission = settleSubmissionJournal;
        } else {
          if (referenceDocument && r.status === "buffered" && recoveredReference) {
            const draftOutcome = await clearAcceptedComposerReferenceDraft(
              steerRecoveryScope,
              recoveredReference,
            );
            if (draftOutcome === "unbound") {
              throw new ComposerRecoveryUnavailableError(
                "accepted reference draft lacks its source revision",
              );
            }
          }
          await settleSubmissionJournal();
        }
      }
      if (terminalAction) {
        const settlement = await settleKnownChatActionOutcome(
          terminalAction,
          "adopted",
        );
        if (settlement === "deferred") return true;
        if (settlement === "superseded") return false;
      }
      if (!ownsTerminalPresentation()) return false;
      if (
        r.status === "completed"
        || (referenceDocument && r.status === "cancelled")
      ) {
        // Active turn was cleared between FE read + backend check (rare
        // race). Keep the draft in the composer instead of silently turning a
        // steer into a fresh task; that silent fallback is what made sessions
        // look like they were restarting or pointing at the wrong turn.
        setBanner(ts("steer_completed"));
        return false;
      }
      if (r.status === "buffered" && referenceDocument) {
        const resources = referenceDocument.nodes.flatMap((node) =>
          node.type === "reference" ? [node.label] : []
        ).join(locale === "zh-CN" ? "、" : ", ");
        setBanner(ts("steer_reference_queued", { resources }));
        setReferenceQueueRefresh((current) => current + 1);
      }
      // r.status === "buffered" → 🤝 ack will land on SSE; nothing else to do.
      // Historical receipt replay may still return "cancelled"; current
      // /chat/send never writes that outcome, and canonical SSE state owns its UI.
      return true;
    } catch (err) {
      trackComposerTransportFailure(err, {
        surface: "workbench",
        modelControlBatch: "a",
        hasAttachments: attachments.length > 0,
      });
      // eslint-disable-next-line no-console
      console.warn("steer.send failed:", err);
      // feedback-0713 F2 review: follow-ups to soft-deleted sessions (report-deletion cascade) always return 410 — do not suggest retry
      // (it always fails, an F2 anti-pattern); suggest starting a new session.
      const status = (err as { status?: number })?.status;
      const code = (err as { code?: string })?.code;
      if (terminalAction) {
        if (isDefinitiveComposerChatActionRejection(
          err,
          terminalAction.action,
        )) {
          const settlement = await settleKnownChatActionOutcome(
            terminalAction,
            "rejected",
          );
          if (status === 401) await revalidateMe().catch(() => undefined);
          if (settlement !== "settled") return false;
          if (ownsTerminalPresentation()) {
            setBanner(code === "client_refresh_required"
              ? tComposer("action_refresh_required")
              : ts(status === 410 ? "steer_session_deleted" : "steer_failed"));
          }
        } else {
          retainTerminalActionForRecovery("lookup_pending");
        }
      }
      if (
        steerRecoveryScope
        && steerSubmissionId
        && code
        && [
          "steer_parent_not_active",
          "submission_contract_inactive",
          "session_deleted",
        ].includes(code)
      ) {
        const rejectedSubmissionId = steerSubmissionId;
        const journalCleared = await clearPendingSubmission(
          steerRecoveryScope,
          rejectedSubmissionId,
        ).then(() => true, () => false);
        if (journalCleared) {
          if (recoveredSubmissionRef.current?.clientSubmissionId === rejectedSubmissionId) {
            recoveredSubmissionRef.current = null;
            setSubmissionOutcomeUnknown(false);
          }
          setRecoveredSubmission((current) =>
            current?.clientSubmissionId === rejectedSubmissionId ? null : current,
          );
          setReferenceSubmissionPreview((current) =>
            current?.draft.clientSubmissionId === rejectedSubmissionId ? null : current,
          );
        }
      }
      if (!terminalAction && ownsTerminalPresentation()) {
        setBanner(ts(status === 410 ? "steer_session_deleted" : "steer_failed"));
      }
      if (steerSubmissionId && recoveredSubmissionRef.current?.clientSubmissionId === steerSubmissionId) {
        setSubmissionOutcomeUnknown(true);
      }
      return false;
    } finally {
      if (terminalAction) {
        inFlightChatActionsRef.current.delete(terminalAction.action.actionId);
        const pending = recoveredChatActionRef.current;
        if (
          pending?.action.actionId === terminalAction.action.actionId
          && pending.recoveryState === "lookup_pending"
          && pending.scopeKey === composerDraftScopeKeyRef.current
        ) {
          void lookupPendingChatAction(pending);
        }
      }
    }
  };

  const sendSteer = async (
    text: string,
    attachments: AttachmentRef[] = [],
    draftRevision: string | null = null,
    options: SteerChatOptions = {},
  ): Promise<boolean> => {
    if (sendSteerInFlightRef.current) return false;
    sendSteerInFlightRef.current = true;
    try {
      return await sendSteerAttempt(
        text,
        attachments,
        draftRevision,
        options,
      );
    } finally {
      sendSteerInFlightRef.current = false;
    }
  };

  const handleSteer = (
    text: string,
    attachments: AttachmentRef[] = [],
    draftRevision: string | null = null,
    submissionContext: ComposerDraftSubmissionContext | null = null,
  ) => sendSteer(text, attachments, draftRevision, { submissionContext });

  const handleRetryRecoveredSubmission = async () => {
    const pending = recoveredSubmission;
    const recoveryScope = submissionRecoveryScope;
    const recoveryScopeKey = composerDraftScopeKeyRef.current;
    if (!pending || !recoveryScope) return false;
    if (pending.submissionKind === "steer") {
      return sendSteer(pending.message, pending.attachments, null, {
        recovered: pending,
        recoveredScope: recoveryScope,
        recoveredScopeKey: recoveryScopeKey,
      });
    }
    return sendChat(
      pending.message,
      pending.attachments,
      null,
      {
        replaySourceTurnId: pending.submissionKind === "replay"
          ? pending.replaySourceTurnId ?? null
          : null,
        referenceRecovery: "input_document" in pending ? pending as RecoveredReferenceSubmission : null,
      },
    );
  };

  const reconcileStaleResendController = (
    pending: PendingChatActionController,
    draft: ComposerOrdinaryDraft | null,
    scope: ComposerDraftScope,
  ): void => {
    if (draft?.pendingAction) {
      const superseding = {
        action: draft.pendingAction,
        message: draft.message.trim(),
        draftMessage: draft.message,
        attachments: draft.attachments,
        workspaceId: pending.workspaceId,
        persistenceScope: scope,
        scopeKey: pending.scopeKey,
        recoveryState: "lookup_pending",
      } satisfies PendingChatActionController;
      adoptSupersedingChatAction(pending, superseding);
      if (pending.scopeKey === composerDraftScopeKeyRef.current) {
        void lookupPendingChatAction(superseding);
      }
      return;
    }
    finishChatActionController(pending);
  };

  const chatActionTransportBusy = (
    actionKind: ComposerChatAction["actionKind"],
  ): boolean => actionKind === "steer"
    ? sendSteerInFlightRef.current
    : false;

  const handleResendPendingChatAction = async (): Promise<boolean> => {
    if (chatActionAttemptRef.current) return false;
    const pending = recoveredChatActionRef.current;
    if (!pending || pending.scopeKey !== composerDraftScopeKeyRef.current) {
      return false;
    }
    if (pending.recoveryState !== "resend_allowed") return false;
    const resendBlocked = (replacementActionId = pending.action.actionId) => (
      pending.scopeKey !== composerDraftScopeKeyRef.current
      || (pending.action.actionKind === "start"
        ? chatStartLaunchBlocked(
            replacementActionId,
            pending.action.actionId,
          )
        : Boolean(chatActionAttemptRef.current)
          || inFlightChatActionsRef.current.has(pending.action.actionId)
          || chatActionTransportBusy(pending.action.actionKind))
    );
    if (resendBlocked()) {
      return false;
    }
    const pendingDraftScope = pending.persistenceScope
      ?? pending.fallbackDraftScope
      ?? null;
    let draft: ComposerOrdinaryDraft | null = null;
    if (pendingDraftScope) {
      try {
        draft = await readComposerOrdinaryDraft(pendingDraftScope);
      } catch (error) {
        console.warn("composer action resend draft read failed", error);
        return false;
      }
      if (resendBlocked()) return false;
      if (
        pending.persistenceScope
        && draft?.pendingAction?.actionId !== pending.action.actionId
      ) {
        reconcileStaleResendController(pending, draft, pendingDraftScope);
        return false;
      }
      if (
        pending.fallbackDraftScope
        && (
          draft?.pendingAction
          || (draft?.revision ?? null) !== pending.action.sourceRevision
        )
      ) {
        reconcileStaleResendController(pending, draft, pendingDraftScope);
        return false;
      }
    }
    const draftMessageToPreserve = draft?.message
      ?? pending.draftMessage
      ?? pending.message;
    const messageToSend = draftMessageToPreserve.trim();
    const attachmentsToSend = draft?.attachments ?? pending.attachments;
    const draftRevision = draft?.revision ?? null;
    let bootstrap: ChatExecutionBootstrap | null;
    let steerPreflight: SessionDetailDTO | null = null;
    try {
      if (pending.action.actionKind === "steer") {
        steerPreflight = await preflightSessionChatExecution(
          pending.action.sessionId,
        );
        bootstrap = steerPreflight?.chat_execution ?? null;
      } else {
        bootstrap = await preflightChatExecution(
          pending.action.sessionId,
        );
      }
    } catch (error) {
      console.warn("composer action resend preflight failed", error);
      if (pending.scopeKey === composerDraftScopeKeyRef.current) {
        setBanner(tComposer("action_preflight_failed"));
      }
      return false;
    }
    if (resendBlocked()) return false;
    if (pending.action.actionKind === "steer") {
      const activeTurnId = steerPreflight?.chat_execution.active_turn_id;
      const activeGeneration = steerPreflight
        ?.chat_execution.active_execution_generation;
      if (activeTurnId === undefined || activeGeneration === undefined) {
        setBanner(tComposer("action_refresh_required"));
        return false;
      }
      if (
        activeTurnId !== pending.action.expectedTurnId
        || activeGeneration !== pending.action.expectedExecutionGeneration
      ) {
        const settlement = await settleKnownChatActionOutcome(
          pending,
          "rejected",
        );
        if (
          settlement === "settled"
          && pending.scopeKey === composerDraftScopeKeyRef.current
        ) {
          setBanner(ts("steer_completed"));
        }
        return settlement === "settled";
      }
    }
    if (bootstrap?.write_protocol === "legacy_v0") {
      let rollbackDraftPersisted = draftRevision !== null;
      if (pending.persistenceScope) {
        if (!pendingDraftScope || !draftRevision) return false;
        const release = await settleComposerPendingAction(
          pending.persistenceScope,
          pending.action.actionId,
          "rejected",
        );
        if (release.status !== "settled") {
          reconcileStaleResendController(
            pending,
            release.draft,
            pending.persistenceScope,
          );
          return false;
        }
      } else if (!pending.fallbackDraftScope) {
        return false;
      } else if (!draftRevision) {
        try {
          const restored = await writeComposerOrdinaryDraft(
            pending.fallbackDraftScope,
            {
              message: draftMessageToPreserve,
              attachments: attachmentsToSend,
            },
            null,
          );
          if (restored.status !== "written") {
            setBanner(tComposer("draft_conflict"));
            return false;
          }
          rollbackDraftPersisted = restored.revision !== null;
        } catch (error) {
          if (!(error instanceof ComposerRecoveryUnavailableError)) {
            console.warn("composer rollback draft restore failed", error);
            setBanner(tComposer("action_unavailable"));
            return false;
          }
          // Keep the mounted V2 transient snapshot. The obsolete process-local
          // fence can still be released without pretending a durable draft was
          // written.
        }
      }
      // Rollback escape hatch: confirmation only removes the obsolete
      // Terminal fence.  It deliberately does not chain a legacy mutation,
      // because legacy has no action identity that could make response loss
      // queryable. The restored ordinary draft is sent by the next explicit
      // Composer click under the server's current legacy decision.
      finishChatActionController(pending, rollbackDraftPersisted);
      if (pending.scopeKey === composerDraftScopeKeyRef.current) {
        setBanner(tComposer("submission_rejected"));
      }
      return true;
    }
    if (bootstrap?.write_protocol !== "terminal_v1") {
      setBanner(tComposer(
        bootstrap?.write_protocol === "refresh_required"
          ? "action_refresh_required"
          : "action_unavailable",
      ));
      return false;
    }
    if (!terminalChatActionAttachmentsSupported(
      pending.action.actionKind,
      attachmentsToSend,
      bootstrap,
    )) {
      setBanner(tComposer("action_unavailable"));
      return false;
    }
    const replacementAction = pending.action.actionKind === "start"
      ? createComposerChatAction({
          actionKind: "start",
          sourceRevision: draftRevision,
          sessionId: pending.action.sessionId,
          ...(pending.action.agentDraftTarget ? { agentDraftTarget: pending.action.agentDraftTarget } : {}),
        })
      : createComposerChatAction({
          actionKind: "steer",
          sourceRevision: draftRevision,
          sessionId: pending.action.sessionId,
          expectedTurnId: pending.action.expectedTurnId,
          expectedExecutionGeneration:
            pending.action.expectedExecutionGeneration,
        });
    let replacement = {
      action: replacementAction,
      message: messageToSend,
      draftMessage: draftMessageToPreserve,
      attachments: attachmentsToSend,
      workspaceId: pending.workspaceId,
      persistenceScope: pending.persistenceScope,
      ...(pending.fallbackDraftScope
        ? { fallbackDraftScope: pending.fallbackDraftScope }
        : {}),
      scopeKey: pending.scopeKey,
    } satisfies PendingChatActionController;
    let replacementMarkerCommitted = false;
    const parkCommittedReplacement = (): false => {
      const canonical = [
        recoveredChatActionRef.current,
        inMemoryChatActionsRef.current.get(replacement.scopeKey),
        chatActionAttemptRef.current,
      ].find((candidate) => (
        candidate?.action.actionId === replacement.action.actionId
        && candidate.recoveryState !== undefined
      ));
      // The delegate may already have classified the POST and only failed its
      // local marker settlement. That accepted/rejected/lookup recovery state
      // is strictly newer than this fallback and must never be downgraded.
      if (canonical) return false;
      adoptSupersedingChatAction(pending, replacement);
      const parked = retainChatActionForRecovery(
        replacement,
        "lookup_pending",
      );
      if (parked.scopeKey === composerDraftScopeKeyRef.current) {
        void lookupPendingChatAction(parked);
      }
      return false;
    };
    if (pending.fallbackDraftScope && draftRevision) {
      if (resendBlocked()) return false;
      try {
        const outcome = await prepareComposerPendingAction(
          pending.fallbackDraftScope,
          replacementAction,
          draftRevision,
          { message: draftMessageToPreserve, attachments: attachmentsToSend },
        );
        if (outcome.status !== "written") {
          reconcileStaleResendController(
            pending,
            outcome.draft,
            pending.fallbackDraftScope,
          );
          return false;
        }
        replacement = {
          ...replacement,
          action: outcome.pendingAction,
          persistenceScope: pending.fallbackDraftScope,
        };
        delete replacement.fallbackDraftScope;
        replacementMarkerCommitted = true;
        if (resendBlocked(replacement.action.actionId)) {
          return parkCommittedReplacement();
        }
      } catch (error) {
        if (!(error instanceof ComposerRecoveryUnavailableError)) {
          console.warn("composer fallback resend marker rejected", error);
          if (pending.scopeKey === composerDraftScopeKeyRef.current) {
            setBanner(tComposer("action_unavailable"));
          }
          return false;
        }
        // A second explicit marker outage still permits exactly this one
        // process-local attempt. Its stable action identity remains queryable.
      }
    }
    if (pending.persistenceScope && draftRevision) {
      if (resendBlocked(replacement.action.actionId)) return replacementMarkerCommitted
        ? parkCommittedReplacement()
        : false;
      const outcome = await replaceComposerPendingAction(
        pending.persistenceScope,
        replacementAction,
        draftRevision,
        pending.action.actionId,
      );
      if (outcome.status !== "written") {
        reconcileStaleResendController(
          pending,
          outcome.draft,
          pending.persistenceScope,
        );
        return false;
      }
      replacement = { ...replacement, action: outcome.pendingAction };
      replacementMarkerCommitted = true;
      if (resendBlocked(replacement.action.actionId)) {
        return parkCommittedReplacement();
      }
    }
    if (resendBlocked(replacement.action.actionId)) return replacementMarkerCommitted
      ? parkCommittedReplacement()
      : false;
    adoptSupersedingChatAction(pending, replacement);
    chatActionAttemptRef.current = replacement;
    setChatActionAttempt(replacement);
    recoveredChatActionRef.current = null;
    setRecoveredChatAction(null);
    let result: boolean;
    if (replacement.action.actionKind === "steer") {
      result = await sendSteer(
        messageToSend,
        attachmentsToSend,
        draftRevision,
        { preparedTerminalAction: replacement },
      );
    } else {
      result = await sendChat(
        messageToSend,
        attachmentsToSend,
        draftRevision,
        { preparedTerminalAction: replacement },
      );
    }
    if (
      !result
      && replacementMarkerCommitted
      && !finishedChatActionIdsRef.current.has(replacement.action.actionId)
    ) {
      return parkCommittedReplacement();
    }
    setDraftReconcileEpoch((epoch) => epoch + 1);
    return result;
  };

  const resetToNewWork = async ({
    confirmNavigation,
    discardHomeDraft = false,
    stillWanted,
  }: {
    confirmNavigation: boolean;
    discardHomeDraft?: boolean;
    /** After confirmation, recheck whether resetting is still desired (conditions may change during confirmation). */
    stillWanted?: () => boolean;
  }) => {
    if (confirmNavigation && !(await confirmCanvasNavigation())) {
      return "cancelled" as const;
    }
    if (stillWanted && !stillWanted()) return "cancelled" as const;
    const pendingAction = recoveredChatActionRef.current;
    if (pendingAction) {
      await reconcileRecoveredChatAction(pendingAction);
      return "cancelled" as const;
    }
    // The home-page input below restores a local draft on remount; "新工作" requires an empty input, so discard
    // this home-page draft first (Composer itself rejects protected drafts such as pending-confirmation sends, restoring them unchanged).
    if (discardHomeDraft) {
      await homeComposerDraftControlRef.current?.discardForNewWork();
    }
    // Block the runner.sessionId Old Value → active frame — only when runner actually holds an old ID. Enabling the block when the page is already
    // clean never releases it, swallowing the first message's new session (see runner-session-sync.ts).
    ignoreSyncRef.current = shouldIgnoreRunnerSessionOnReset(runner.sessionId);
    setReportLinkSessionId(null);
    setReportOnlySessionId(null);
    setReportUnavailableSid(null);
    setLoadingSid(null);
    setScenarioId(null);
    setSessionChatExecution(null);
    setMessage(null);
    setPendingReferenceDocument(null);
    setLastSentReferenceDocument(null);
    setPendingReferenceSourceRevision(null);
    setPendingAttachments([]);
    setHandoffAttach(null);
    sessionHydrationEpochRef.current += 1;
    requestedSessionIdRef.current = null;
    setArchivedTurnsPatch(null);
    setCurrentTurnHydrationPatch(null);
    pendingSessionScopedResultsResetRef.current = null;
    lastTurnId.current = null;
    // feedback-0713 #13 review P1: clear URL hydration ref — otherwise Click Notification To Enter Session X →
    // New Conversation → Click The Same Notification Again hits sid===ref and the guard swallows it, yielding a dead click + URL/view divergence.
    urlSessionReqRef.current = null;
    let resettingSessionId =
      activeSessionIdRef.current ?? runner.sessionId;
    if (!resettingSessionId) {
      try {
        resettingSessionId = new URL(
          window.location.href,
        ).searchParams.get("session");
      } catch {
        // State-backed ids remain authoritative when URL parsing is unavailable.
      }
    }
    resettingUrlSessionRef.current = resettingSessionId;
    clearSessionUrlRef.current = true;
    activeSessionIdRef.current = null;
    setActiveSessionId(null);              // Crucial: clear it so Gateway creates a new session on the next message.
    setResetSignal((k) => k + 1);          // Runner clears canvas + turn state.
    setComposerResetKey((k) => k + 1);     // remount → clear draft/files + focus
    setSessionsRefreshKey((k) => k + 1);
    setHydrate(null);
    return "created" as const;
  };

  const handleNewSession = () =>
    resetToNewWork({ confirmNavigation: true, discardHomeDraft: true });

  // Forward / back (or navigation elsewhere) changed the address bar to clean `/`, but the page still shows (or is opening) a session:
  // URL identity includes the session, so return to "新工作". Use the same reset as clicking "新工作"; confirm unsaved reports first;
  // the home-page input is currently out of view, so keep its old draft (restore normally on remount).
  // Cancel ⇒ replaceState changes **this current** history entry back to the session URL: stay in the original session, preserve history length,
  // and back still reaches the previous entry; pushState would leave an extra `/` behind and trigger another confirmation on forward.
  const followCleanUrlNavigation = async () => {
    cleanUrlNavigationRef.current = true;
    try {
      const viewSessionId = activeSessionIdRef.current;
      const liveSessionId = () => new URL(window.location.href).searchParams.get("session");
      const result = await resetToNewWork({
        confirmNavigation: Boolean(viewSessionId),
        // Users may press back again while confirmation is open: if the address bar now shows a session, do not overwrite that entry with `/`.
        stillWanted: () => !liveSessionId(),
      });
      const outcome = cleanUrlNavigationOutcome({ result, viewSessionId, liveSid: liveSessionId() });
      if (outcome === "release-target") {
        urlSessionReqRef.current = null;
        return;
      }
      if (outcome !== "restore-session-url" || !viewSessionId) return;
      const url = new URL(window.location.href);
      url.search = syncActiveSessionSearchParams({
        current: url.searchParams,
        activeSessionId: viewSessionId,
        reportLinkSessionId,
        clearSessionScopedResults: false,
      }).toString();
      window.history.replaceState(
        withoutNextRouterHistoryMarkers(window.history.state),
        "",
        url.toString(),
      );
      lastWrittenUrlSessionRef.current = viewSessionId;
    } finally {
      cleanUrlNavigationRef.current = false;
    }
  };

  const handlePickSession = async (sid: string) => {
    resettingUrlSessionRef.current = null;
    if (sid === activeSessionId || sid === runner.sessionId) return "picked" as const;
    if (!(await confirmCanvasNavigation())) return "cancelled" as const;

    ignoreSyncRef.current = true;
    pendingSessionScopedResultsResetRef.current = sid;
    setLoadingSid(sid);
    const hydrationEpoch = ++sessionHydrationEpochRef.current;
    requestedSessionIdRef.current = sid;
    setArchivedTurnsPatch(null);
    setCurrentTurnHydrationPatch(null);
    void startSessionHydration({
      sid,
      expectReport: false,
      origin: "picker",
      hydrationEpoch,
    });
    return "picked" as const;
  };

  const handleToggleSessionPin = async (
    sid: string,
    currentlyPinned: boolean,
  ) => {
    try {
      if (currentlyPinned) await unpinSession(sid);
      else await pinSession(sid);
      setSessionsRefreshKey((key) => key + 1);
    } catch (error) {
      console.warn("[pin] failed", error);
      throw error;
    }
  };

  const handleArchiveSession = async (sid: string) => {
    let urlSessionId: string | null = null;
    try {
      urlSessionId = new URL(window.location.href).searchParams.get(
        "session",
      );
    } catch {
      // Page state remains the primary source when URL parsing is unavailable.
    }
    const isCurrent =
      sid === activeSessionId ||
      sid === runner.sessionId ||
      sid === urlSessionId;
    if (isCurrent && !(await confirmCanvasNavigation())) {
      return "cancelled" as const;
    }
    await archiveSession(sid);
    if (isCurrent) {
      try {
        await resetToNewWork({ confirmNavigation: false });
      } catch (error) {
        try {
          window.location.assign(appPath("/"));
        } catch {
          // The archive is already durable. Do not restore it implicitly.
        }
      }
    }
    notifySessionListChanged();
    return "success" as const;
  };

  const handleRestoreSession = async (sid: string) => {
    await restoreSession(sid);
    notifySessionListChanged();
  };

  // Title-bar ⋯ › Delete Session: the same soft delete as the sidebar row (no confirmation, eight-second Undo in the
  // menu's toast). Deleting the open session confirms unsaved canvas changes first, then returns to New Work.
  // Shared by the sidebar row and the title bar, like handleArchiveSession.
  const handleDeleteSession = async (sid: string) => {
    let urlSessionId: string | null = null;
    try {
      urlSessionId = new URL(window.location.href).searchParams.get("session");
    } catch {
      // Page state remains the primary source when URL parsing is unavailable.
    }
    const isCurrent = sid === activeSessionId || sid === runner.sessionId || sid === urlSessionId;
    if (isCurrent && !(await confirmCanvasNavigation())) {
      return "cancelled" as const;
    }
    await deleteSession(sid);
    if (isCurrent) {
      // The deletion is durable, so the page must leave the deleted session. When the in-page reset declines
      // (for example a recovered send is being reconciled) or fails, load New Work instead of staying on it.
      let left = false;
      try {
        left = (await resetToNewWork({ confirmNavigation: false })) !== "cancelled";
      } catch {
        left = false;
      }
      if (!left) {
        try {
          window.location.assign(appPath("/"));
        } catch {
          // A navigation failure is not a deletion failure.
        }
      }
    }
    notifySessionListChanged();
    return "success" as const;
  };

  const handleUndeleteSession = async (sid: string) => {
    await undeleteSession(sid);
    notifySessionListChanged();
  };

  const showingReportOnly = Boolean(reportOnlySessionId && activeSessionId === reportOnlySessionId);
  // Single-column home page (Workbench Spec §1「标题 → 输入框 → 三张提议卡 → 分组」): the input occupies a HomeWorkSurface slot
  // after the heading (DOM, focus and screen-reader order match visual order).

  // content-permissions wave 2: a shared report the viewer can't open → placeholder
  // (+ "申请访问") instead of the workspace. Owner/grantee opens fine (200) → no overlay.
  if (reportUnavailableSid) {
    return (
      <div style={{ position: "fixed", inset: 0, zIndex: 50, background: "var(--c-bg)", display: "flex" }}>
        <ReportUnavailable
          sessionId={reportUnavailableSid}
          canRequest={accessRequestsEnabled}
          onBack={() => {
            setReportUnavailableSid(null);
            try {
              window.history.replaceState(
                withoutNextRouterHistoryMarkers(
                  withoutResultsHistoryState(window.history.state),
                ),
                "",
                appPath("/"),
              );
            } catch { /* ignore */ }
          }}
        />
      </div>
    );
  }

  const reportOnlyActive = Boolean(
    runner.reportOnly
      || (hydrate?.reportOnly
        && hydrate.sessionId
        && (hydrate.sessionId === runner.sessionId || hydrate.sessionId === activeSessionId)),
  );
  // dogfood-0708: owner opens their report from My Reports → fullscreen report retains the thin "对话" edge (≠reportOnly).
  // reportOnly (read-only sharing) takes priority: genuinely lacking chat access locks everything and omits the return-to-chat entry.
  const startMaximizedActive = Boolean(
    !reportOnlyActive
      && hydrate?.openMaximized
      && hydrate.sessionId
      && (hydrate.sessionId === runner.sessionId || hydrate.sessionId === activeSessionId),
  );
  const sendReadOnlyActive = entitlement.readOnly || reportOnlyActive;
  sendReadOnlyRef.current = sendReadOnlyActive;
  // A work always belongs to a Space. Once the Space list has resolved empty,
  // the home surface shows the actionable join/create guidance instead of a
  // disabled composer that cannot submit anything.
  const showHomeComposer = workspace.loading || Boolean(workspace.error) || Boolean(activeWorkspaceId);
  // The home surface's H1 doubles as the top-bar title, so the large-title pattern shows it once.
  const homeTitle = !runner.started && !showingReportOnly && !sessionLoading
    ? tHome(homePageTitleKey({
      workspaceId: activeWorkspaceId || null,
      workspaceLoading: workspace.loading,
      workspaceError: workspace.error,
    }))
    : null;
  const showSubmissionRecovery = recoveredSubmission !== null && submissionOutcomeUnknown;
  const subagentCanvas = agentEntryVisible ? (
    subagentEntryChooser && unifiedDraftEnabled ? (
      <SubagentDraftEntry unified={multipleCanvasEnabled}
        requestContext={{ teamId: authSession?.me?.team_id ?? "", workspaceId: activeWorkspaceId }}
        // A failed Space list is not "no Space": keep waiting instead of claiming the account has none.
        spaceLoading={workspace.loading || Boolean(workspace.error)}
        onResume={(draftId) => openAgentCanvas({ purpose_hint: null, draft_id: draftId, draft_status: null, draft_title: null })}
        onNewAi={() => {
          setSubagentEntryChooser(false);
          setSubagentEntryIntent(true);
        }}
        onNewManual={() => {
          setSubagentEntryChooser(false);
          setSubagentEntryIntent(false);
        }}
      />
    ) : subagentEntryIntent && unifiedDraftEnabled ? (
      <SubagentManualDraftBootstrap
        requestContext={{ teamId: authSession?.me?.team_id ?? "", workspaceId: activeWorkspaceId ?? "" }}
        sourceMode="intent"
        purposeHint={subagentCardPayload?.purpose_hint ?? null}
        onDraft={(draft) => {
          openAgentCanvas({ ...manualSubagentPayload(subagentCardPayload, draft.purpose), draft_id: draft.draft_id, draft_status: draft.draft_status }, draft);
        }}
      />
    ) : subagentEntryIntent ? (
      <SubagentIntentCard
        key={subagentCardPayload?.purpose_hint ?? ""}
        initialPurpose={subagentCardPayload?.purpose_hint ?? ""}
        embedded
        onDraft={(draft) => {
          openAgentCanvas(reviewSubagentPayload(subagentCardPayload, draft));
        }}
        onManual={(purposeHint) => {
          setSubagentEntryIntent(false);
          setSubagentCardPayload((current) => manualSubagentPayload(current, purposeHint));
        }}
        onClose={() => setSubagentCardOpen(false)}
      />
    ) : unifiedDraftEnabled ? (
      <SubagentManualDraftBootstrap
        requestContext={{ teamId: authSession?.me?.team_id ?? "", workspaceId: activeWorkspaceId ?? "" }}
        purposeHint={subagentCardPayload?.purpose_hint ?? null}
        onDraft={(draft) => {
          openAgentCanvas({ ...manualSubagentPayload(subagentCardPayload, draft.purpose), draft_id: draft.draft_id, draft_status: draft.draft_status }, draft);
        }}
      />
    ) : (
      <CreateSubagentCard
        purposeHint={subagentCardPayload?.purpose_hint ?? null}
        nameHint={subagentCardPayload?.name_hint ?? null}
        displayNameHint={subagentCardPayload?.display_name_hint ?? null}
        embedded
        unifiedDraftEnabled={unifiedDraftEnabled}
        onManualDraftSaved={showDraftInUrl}
        onClose={() => {
          showDraftInUrl(null);
          setSubagentCardOpen(false);
        }}
        onCreated={() => showDraftInUrl(null)}
      />
    )
  ) : null;

  const documentJobs = useCanvasDocumentJobs(documentScope, runner.running,
    documentCanvas.objects.some((object) => object.update?.phase === "ready" && object.update.state && documentJobActive({ state: object.update.state })));
  const documentProjectionRef = useRef<{ scope: string | null; fingerprints: Map<string, string> }>({ scope: null, fingerprints: new Map() });
  useEffect(() => {
    if (documentJobs.denied) { documentCanvas.clear(); return; }
    if (!documentJobs.revision || documentJobs.loading) return;
    if (documentProjectionRef.current.scope !== documentScopeKey)
      documentProjectionRef.current = { scope: documentScopeKey, fingerprints: new Map() };
    const changed = new Set<string>();
    for (const job of documentJobs.jobs) {
      const fingerprint = JSON.stringify([job.state, job.can_confirm, job.can_revise, job.draft_digest, job.error_code, job.question]);
      const previous = documentProjectionRef.current.fingerprints.get(job.id);
      if (previous !== undefined && previous !== fingerprint) changed.add(job.id);
      documentProjectionRef.current.fingerprints.set(job.id, fingerprint);
    }
    for (const object of documentCanvas.objects) {
      if (!documentJobs.jobs.some((job) => job.id === object.context.jobId)
        && object.update?.phase === "ready" && object.update.state && documentJobActive({ state: object.update.state })) changed.add(object.context.jobId);
    }
    if (changed.size) documentCanvas.invalidate(changed);
  }, [documentJobs.revision, documentJobs.denied, documentScopeKey]);
  const documentResultsRevision = JSON.stringify([
    ...documentJobs.jobs.map(documentPublicationKey).filter(Boolean),
    ...(documentReceipt && documentCanvasScopeKey(documentReceipt.context) === documentScopeKey
      ? [JSON.stringify([documentReceipt.context.jobId, documentReceipt.originTurnId, documentReceipt.draftDigest, documentReceipt.source, documentReceipt.pdf])] : []),
  ]);

  useSyncExternalStore(subscribeDocumentCanvas, documentCanvasVersion, documentCanvasVersion);
  const openedDocument = documentCanvasOpen(runner.turnId);
  const documentToolAdmission = useRef({ scopeKey: documentScopeKey, turnId: runner.turnId,
    sessionId: runner.sessionId, denied: documentJobs.denied });
  documentToolAdmission.current = { scopeKey: documentScopeKey, turnId: runner.turnId,
    sessionId: runner.sessionId, denied: documentJobs.denied };
  useEffect(() => {
    if (!openedDocument || !documentScopeKey || runner.sessionId !== activeSessionId || documentJobs.denied) return;
    const originalScope = documentScopeKey;
    const intent = canvasOpenIntentRef.current;
    const mayInitiallySelect = !activeCanvasSelectionRef.current && !canvasResultsSelected;
    void documentCanvas.openFromTool(openedDocument, {
      isCurrent: () => documentToolAdmission.current.scopeKey === originalScope
        && documentToolAdmission.current.turnId === openedDocument.turn_id
        && documentToolAdmission.current.sessionId === openedDocument.session_id && !documentToolAdmission.current.denied,
      maySelect: () => {
        const focused = document.activeElement;
        const editing = focused instanceof HTMLTextAreaElement || focused instanceof HTMLInputElement
          ? focused.value.trim().length > 0 : focused instanceof HTMLElement && focused.isContentEditable && Boolean(focused.textContent?.trim());
        return mayInitiallySelect && !editing && canvasOpenIntentRef.current === intent && !activeCanvasSelectionRef.current;
      },
      onOpened: (selected) => {
        if (selected) {
          resourceCanvas.select(null); operationCanvas.select(null); agentCanvas.select(null);
          settingsCanvas.deselect(); setSubagentCardOpen(false); setCanvasResultsSelected(false); showDraftInUrl(null);
        } else if (!activeCanvasSelectionRef.current) setCanvasResultsSelected(true);
      },
    });
  }, [openedDocument, documentScopeKey, runner.sessionId, activeSessionId, documentJobs.denied, documentCanvas.openFromTool]);

  useSyncExternalStore(subscribeConfigurationCanvas, configurationCanvasVersion, configurationCanvasVersion);
  const openedSettings = configurationCanvasOpen(runner.turnId);
  useEffect(() => {
    if (!canvasScopeKey || runner.sessionId !== activeSessionId || !openedSettings || authSession?.me?.capabilities?.settings_pages_canvas !== true
      || !workspace.items.some((item) => item.id === activeWorkspaceId && item.role === "admin")
      || openedSettings.target.userId !== authSession.me.id
      || openedSettings.target.teamId !== authSession.me.team_id
      || openedSettings.target.workspaceId !== activeWorkspaceId) return;
    settingsBridge?.acceptToolOpen(openedSettings.turnId, openedSettings.target, openedSettings.source);
    settingsCanvas.open(openedSettings.target, openedSettings.turnId,
      !subagentCardOpen && agentCanvas.objects.length === 0 && operationCanvas.objects.length === 0 && resourceCanvas.objects.length === 0 && documentCanvas.objects.length === 0 && !canvasResultsSelected);
  }, [openedSettings, settingsBridge, authSession?.me?.id, authSession?.me?.team_id,
    authSession?.me?.capabilities?.settings_pages_canvas, activeWorkspaceId, workspace.items,
    canvasScopeKey, runner.sessionId, activeSessionId]);

  useSyncExternalStore(subscribeCoachOperations, coachOperationsVersion, coachOperationsVersion);
  const openedOperations = coachOperationOpens(runner.turnId);
  useEffect(() => {
    if (!canvasScopeKey || !multipleCanvasEnabled || runner.sessionId !== activeSessionId) return;
    for (const pointer of openedOperations) {
      if (pointer.userId !== authSession?.me?.id || pointer.teamId !== authSession.me.team_id
        || pointer.workspaceId !== activeWorkspaceId || pointer.sessionId !== activeSessionId) continue;
      const focus = document.activeElement;
      const input = focus instanceof HTMLInputElement || focus instanceof HTMLTextAreaElement ? focus.value
        : focus instanceof HTMLElement && focus.isContentEditable ? focus.textContent ?? "" : "";
      // WebKit may still paint the just-accepted message before Composer clears it.
      const intent = operationTurnIntents.current.get(pointer.turnId);
      const editing = Boolean(input.trim()) && input.trim() !== intent?.submittedText?.trim();
      const select = maySelectCoachOperation(pointer, {
        intent, scope: canvasScopeKey, generation: canvasOpenIntentRef.current,
        selectedKey: operationCanvas.selectedKey,
        available: !editing && !subagentCardOpen && !settingsCanvas.selected && !agentCanvas.selectedKey
          && !resourceCanvas.selectedKey && !documentCanvas.selectedKey && !canvasResultsSelected,
      });
      if (operationCanvas.open(pointer, select)) revealCanvasResults();
    }
  }, [openedOperations, canvasScopeKey, multipleCanvasEnabled, runner.sessionId, activeSessionId,
    authSession?.me?.id, authSession?.me?.team_id, activeWorkspaceId, operationCanvas.open]);
  // Coach writes the Agent draft directly: an open hosted editor of that Agent reloads to show the change,
  // unless it holds unsaved input (Coach was not sent a message over that input in the first place).
  const reloadedForOperation = useRef(new Set<string>());
  useEffect(() => {
    for (const object of agentEditorsToReload(openedOperations, resourceCanvas.objects, reloadedForOperation.current)) {
      resourceCanvas.reload(object.key, object.incarnation);
    }
  }, [openedOperations, resourceCanvas.objects, resourceCanvas.reload]);
  const selectOperationCanvas = (key: string) => {
    if (!operationCanvas.objects.some((object) => object.key === key)) return;
    canvasOpenIntentRef.current += 1;
    documentCanvas.deselect();
    resourceCanvas.select(null);
    operationCanvas.select(key);
    setSubagentCardOpen(false);
    setCanvasResultsSelected(false);
    settingsCanvas.deselect();
    showDraftInUrl(null);
  };

  useSyncExternalStore(subscribeCoachResources, coachResourcesVersion, coachResourcesVersion);
  const openedResources = coachResourceOpens(runner.turnId);
  useEffect(() => {
    if (!canvasScopeKey || !multipleCanvasEnabled || runner.sessionId !== activeSessionId) return;
    for (const pointer of openedResources) {
      if (pointer.userId !== authSession?.me?.id || pointer.teamId !== authSession.me.team_id
        || pointer.workspaceId !== activeWorkspaceId || pointer.sessionId !== activeSessionId) continue;
      resourceCanvas.open(pointer, !subagentCardOpen && !settingsCanvas.selected && !agentCanvas.selectedKey
        && !operationCanvas.selectedKey && !resourceCanvas.selectedKey && !documentCanvas.selectedKey && !canvasResultsSelected);
    }
  }, [openedResources, canvasScopeKey, multipleCanvasEnabled, runner.sessionId, activeSessionId,
    authSession?.me?.id, authSession?.me?.team_id, activeWorkspaceId, resourceCanvas.open]);
  const canvasSaveBlockObject = canvasSaveBlock
    ? resourceCanvas.objects.find((object) => object.key === canvasSaveBlock.key && object.incarnation === canvasSaveBlock.incarnation) ?? null
    : null;
  const resolveCanvasSaveBlock = async (action: "save" | "discard") => {
    const port = canvasSaveBlockObject?.editorRef.current;
    if (!port || canvasSaveBusy) return;
    setCanvasSaveBusy(action);
    setCanvasSaveFailed(false);
    try {
      // Saved meanwhile in the editor itself: nothing more to save before sending again.
      const clean = action === "save" && port.capture?.()?.dirty === false && port.capture?.()?.pending_write === false;
      const done = clean || (action === "save" ? await port.save?.() : await port.discard?.());
      if (!done) {
        if (action === "save") setCanvasSaveFailed(true);
        return;
      }
      setCanvasSaveBlock(null);
      // Saved: send the same message again through the composer's own Send path and its guards.
      if (action === "save") setComposerSubmitRequest((value) => value + 1);
    } finally {
      setCanvasSaveBusy(null);
    }
  };
  const selectResourceCanvas = (key: string) => {
    if (!resourceCanvas.objects.some((object) => object.key === key)) return;
    canvasOpenIntentRef.current += 1;
    documentCanvas.deselect();
    resourceCanvas.select(key);
    operationCanvas.select(null);
    settingsCanvas.deselect();
    setSubagentCardOpen(false);
    setCanvasResultsSelected(false);
    showDraftInUrl(null);
  };

  /** An operation receipt's card action: open its Skill / Plugin / Agent editor or the original Agent draft. */
  const openOperationTarget = (pointer: CoachOperationOpen, target: CoachOperationTarget) => {
    if (pointer.userId !== authSession?.me?.id || pointer.teamId !== authSession.me.team_id
      || pointer.workspaceId !== activeWorkspaceId || pointer.sessionId !== activeSessionId) return;
    if (target.kind === "agent_cases") {
      router.push(agentCaseProposalPath(target.agentId, target.pending));
      return;
    }
    if (target.kind === "agent_draft") {
      void openAgentCanvas({ draft_id: target.draftId, draft_version: null, purpose_hint: null, draft_status: null, draft_title: null });
      return;
    }
    canvasOpenIntentRef.current += 1;
    documentCanvas.deselect();
    operationCanvas.select(null);
    settingsCanvas.deselect();
    setSubagentCardOpen(false);
    setCanvasResultsSelected(false);
    showDraftInUrl(null);
    // The same scope as the receipt; a new request id so reopening a closed editor works.
    resourceCanvas.open({ requestId: randomUuid(), turnId: pointer.turnId, userId: pointer.userId, teamId: pointer.teamId,
      workspaceId: pointer.workspaceId, sessionId: pointer.sessionId, reference: target.reference, parent: null }, true);
  };

  // A Chat receipt row for Coach's draft write opens that Agent's editor (the same scope check as the receipt card).
  const openOperationTargetRef = useRef(openOperationTarget);
  openOperationTargetRef.current = openOperationTarget;
  // An alignment card tells Coach to continue (labels saved, point reworded, model re-pinned). The message is
  // "@Coach <text>", resolved through the composer's own @ catalog, so the Turn reaches Coach in the same loop
  // instead of the Lead. It goes through the composer's own Send path and guards, and only into an empty
  // composer of the same conversation; one message at a time.
  const alignmentSendRef = useRef<{ text: string | null; onUnsent?: () => void } | null>(null);
  const alignmentSessionRef = useRef<string | null>(null);
  alignmentSessionRef.current = runner.sessionId === activeSessionId ? activeSessionId : null;
  const alignmentScopeRef = useRef<SubmissionRecoveryScope | null>(null);
  alignmentScopeRef.current = referenceInputEnabled ? submissionRecoveryScope : null;
  const coachLabelRef = useRef(tMarketplace("system_agent_coach_name"));
  coachLabelRef.current = tMarketplace("system_agent_coach_name");
  const [alignmentContinueDocument, setAlignmentContinueDocument] = useState<InputDocument | null>(null);
  // Leaving the conversation drops a message not yet sent; its card asks the Owner to message Coach.
  useEffect(() => () => {
    const pending = alignmentSendRef.current;
    alignmentSendRef.current = null;
    setAlignmentContinueDocument(null);
    pending?.onUnsent?.();
  }, [activeSessionId]);
  useEffect(() => subscribeCoachAlignmentContinue((request) => {
    const sessionId = alignmentSessionRef.current;
    const scope = alignmentScopeRef.current;
    if (!sessionId || request.sessionId !== sessionId || !scope || alignmentSendRef.current) return false;
    // A draft in the composer stays: the card then tells the Owner to message Coach themselves.
    if (!composerAcceptsContinue(workbenchComposerDraftControlRef.current)) return false;
    const pending: { text: string | null; onUnsent?: () => void } = { text: null, onUnsent: request.onUnsent };
    alignmentSendRef.current = pending;
    void resolveAgentReferenceDocument(scope, { agentId: COACH_AGENT_ID, label: coachLabelRef.current, text: request.text }, new AbortController().signal)
      .catch(() => null)
      .then((document) => {
        if (alignmentSendRef.current !== pending) return;
        // Coach unavailable, another conversation opened or the Owner started typing meanwhile: send nothing.
        if (!document || alignmentSessionRef.current !== sessionId || !composerAcceptsContinue(workbenchComposerDraftControlRef.current)) {
          alignmentSendRef.current = null;
          pending.onUnsent?.();
          return;
        }
        pending.text = inputDocumentDisplayText(document);
        setAlignmentContinueDocument(document);
      });
    return true;
  }), []);
  const onAlignmentContinueConsumed = (disposition: "applied" | "discarded") => {
    setAlignmentContinueDocument(null);
    const pending = alignmentSendRef.current;
    alignmentSendRef.current = null;
    if (!pending) return;
    if (shouldSendStagedContinue(pending.text, disposition, workbenchComposerDraftControlRef.current)) setComposerSubmitRequest((value) => value + 1);
    else pending.onUnsent?.();
  };
  useEffect(() => subscribeCoachOperationRequests((request) => {
    const pointer = coachOperationOpens(request.turnId).find((item) => item.requestId === request.requestId);
    if (!pointer || pointer.reference.kind !== "agent_change") return;
    openOperationTargetRef.current(pointer, { kind: "resource", reference: { kind: "agent", id: pointer.reference.agent_id, revision: "current" } });
  }), []);

  const selectSettingsCanvas = (key: string) => {
    if (!settingsCanvas.targets.some((target) => settingsCanvasKey(target) === key)) return;
    canvasOpenIntentRef.current += 1;
    documentCanvas.deselect();
    resourceCanvas.select(null);
    operationCanvas.select(null);
    setSubagentCardOpen(false);
    setCanvasResultsSelected(false);
    showDraftInUrl(null);
    settingsCanvas.select(key);
  };
  const showDocumentCanvas = (jobId: string) => {
    if (!documentCanvas.open(jobId)) return;
    canvasOpenIntentRef.current += 1;
    resourceCanvas.select(null); operationCanvas.select(null); agentCanvas.select(null);
    settingsCanvas.deselect(); setSubagentCardOpen(false); setCanvasResultsSelected(false); showDraftInUrl(null);
  };
  // Chat surfaces (revision receipt, Composer revision bar) ask to open a draft of this session.
  const showDocumentCanvasRef = useRef(showDocumentCanvas);
  showDocumentCanvasRef.current = showDocumentCanvas;
  const documentRequestScopeRef = useRef({ sessionId: runner.sessionId, activeSessionId, scopeKey: documentScopeKey });
  documentRequestScopeRef.current = { sessionId: runner.sessionId, activeSessionId, scopeKey: documentScopeKey };
  useEffect(() => subscribeDocumentCanvasRequests((request) => {
    const scope = documentRequestScopeRef.current;
    if (!scope.scopeKey || !request.sessionId || request.sessionId !== scope.sessionId || scope.sessionId !== scope.activeSessionId) return;
    showDocumentCanvasRef.current(request.jobId);
  }), []);
  const selectCanvasObject = (key: string) => {
    const object = canvasSelectionItems.find((item) => item.key === key);
    if (!object) return;
    if (object.kind === "document_draft") {
      const original = documentCanvas.objects.find((item) => item.key === key);
      if (original) showDocumentCanvas(original.context.jobId);
    } else if (object.kind === "resource") selectResourceCanvas(key);
    else if (object.kind === "operation") selectOperationCanvas(key);
    else if (object.kind === "settings") selectSettingsCanvas(key);
    else if (object.kind === "agent_draft") selectAgentCanvas(key);
    else {
      canvasOpenIntentRef.current += 1;
      documentCanvas.deselect();
      resourceCanvas.select(null);
      operationCanvas.select(null);
      settingsCanvas.deselect();
      setCanvasResultsSelected(false);
      setSubagentCardOpen(true);
      showDraftInUrl(null);
    }
  };
  const canvasObjects: ConfigurationCanvasObject[] = [];
  const saveStateMarker = (key: string) => (state: CanvasSaveReport) => reportCanvasMarker(key, state.failed
    ? { marker: "failed", statusLabel: tCanvasObject("save_state_failed") }
    : state.saving ? { marker: "running", statusLabel: tCanvasObject("save_state_saving") }
      : state.dirty ? { marker: "dirty", statusLabel: null } : null);
  const reportedMarker = (key: string, dirty = false) => {
    const reported = canvasMarkers[key];
    if (reported && reported.marker !== "dirty") return reported;
    return dirty || reported ? { marker: "dirty" as const, statusLabel: null } : null;
  };
  if (subagentCanvas) canvasObjects.push({ key: agentEntryKey, kind: "agent", title: tCreateAgent("header_title_create"), content: subagentCanvas,
    onClose: () => { showDraftInUrl(null); setSubagentCardOpen(false); } });
  if (subagentCanvasEnabled && draftScopeMatches && activeWorkspaceId) for (const object of agentCanvas.objects) canvasObjects.push({
    key: object.key,
    kind: "agent",
    title: object.payload.draft_title || object.initialDraft?.definition.display_name || tCreateAgent("header_title_create"),
    onClose: () => object.editorRef.current?.requestClose(),
    marker: reportedMarker(object.key, Boolean(object.binding?.dirty))?.marker ?? null,
    statusLabel: reportedMarker(object.key, Boolean(object.binding?.dirty))?.statusLabel ?? null,
    content: <>
      {draftChatSubmissionError === object.payload.draft_id ? <Notice tone="error">{tSubagents("draft_chat_protocol_unavailable")}</Notice> : null}
      <AgentDraftCanvasObject key={object.incarnation} object={object} workspaceId={activeWorkspaceId} teamId={authSession?.me?.team_id ?? undefined} unified={unifiedDraftEnabled} continuous={multipleCanvasEnabled}
        connected={draftChatPresentationConnected && selectedAgentObject?.key === object.key}
        onBinding={agentCanvas.bind} onTitle={agentCanvas.title} onClose={closeAgentCanvas} onActivate={selectAgentCanvas}
        onMarker={(value) => reportCanvasMarker(object.key, value)}
        // A clarification is answered in chat only while the chat is connected to this draft.
        onAnswerInChat={draftChatPresentationConnected && selectedAgentObject?.key === object.key ? () => {
          const scope = composerDraftPersistenceScope;
          if (!scope) return;
          canvasOpenIntentRef.current += 1;
          setComposerRevealRequest({ id: randomUuid(), scopeKey: composerDraftScopeSignature(scope) });
        } : undefined} />
    </>,
  });
  for (const target of settingsCanvas.targets) {
    const key = settingsCanvasKey(target);
    const settingsMarker = reportedMarker(key);
    canvasObjects.push({ key, kind: "settings", title: tSettingsCanvas(target.page === "space_general" ? "general_title" : "title"),
      marker: settingsMarker?.marker ?? null, statusLabel: settingsMarker?.statusLabel ?? null,
      onClose: () => { const close = canvasClosersRef.current.get(key); if (close) close(); else settingsCanvas.close(key); },
      content: <SettingsPageCanvas target={target} readOnly={entitlement.readOnly} onClose={() => settingsCanvas.close(key)}
        onActivate={() => selectSettingsCanvas(key)} onSaveStateChange={saveStateMarker(key)}
        onRegisterClose={(close) => { if (close) canvasClosersRef.current.set(key, close); else canvasClosersRef.current.delete(key); }} /> });
  }
  // Tabs name the operation as its outcome allows (“Skill saved”, “Skill couldn't be saved”, neutral “Skill save”
  // until read); repeated operations of one kind add “· No. 2”.
  operationCanvas.objects.forEach((object, index) => {
    const kind = object.pointer.reference.kind;
    const ordinal = operationCanvas.objects.slice(0, index).filter((other) => other.pointer.reference.kind === kind).length + 1;
    const label = operationLabels[object.key] ?? tCoachOperation(`operation_names.${kind}`);
    canvasObjects.push({
      key: object.key, kind: "operation", title: ordinal > 1 ? tCoachOperation("ordinal", { kind: label, number: ordinal }) : label,
      marker: canvasMarkers[object.key]?.marker ?? null, statusLabel: canvasMarkers[object.key]?.statusLabel ?? null,
      onClose: () => { canvasOpenIntentRef.current += 1; operationCanvas.close(object.key); },
      content: <CoachOperationView pointer={object.pointer} active={activeCanvasSelection?.key === object.key}
        onMarker={(value) => reportCanvasMarker(object.key, value)}
        onLabel={(value) => setOperationLabels((previous) => {
          if ((previous[object.key] ?? null) === value) return previous;
          const next = { ...previous };
          if (value) next[object.key] = value;
          else delete next[object.key];
          return next;
        })}
        onOpen={(target) => openOperationTarget(object.pointer, target)} />,
    });
  });
  const selectedCanvasKey = canvasSelection?.key;
  for (const object of resourceCanvas.objects) canvasObjects.push({
    key: object.key, kind: object.pointer.reference.kind === "capability" ? "plugin" : object.pointer.reference.kind === "agent" ? "agent" : "skill",
    title: object.title || tCoachResourceEditor("title"),
    marker: reportedMarker(object.key)?.marker ?? null, statusLabel: reportedMarker(object.key)?.statusLabel ?? null,
    onClose: () => object.editorRef.current ? object.editorRef.current.requestClose() : resourceCanvas.close(object.key, object.incarnation),
    content: <CoachResourceEditor key={object.incarnation} object={object} onSaveStateChange={saveStateMarker(object.key)}
      parentLabel={(() => {
        const parent = object.pointer.parent;
        if (parent?.kind === "agent_draft") return canvasObjects.find((candidate) =>
          agentCanvas.objects.some((agent) => agent.key === candidate.key && agent.payload.draft_id === parent.id))?.title ?? null;
        if (parent?.kind === "agent_working_copy") return resourceCanvas.objects.find((candidate) =>
          candidate.pointer.reference.kind === "agent" && candidate.pointer.reference.id === parent.agent_id)?.title ?? null;
        return null;
      })()}
      active={!canvasResultsSelected && selectedCanvasKey === object.key}
      onClose={() => resourceCanvas.close(object.key, object.incarnation)} onActivate={() => selectResourceCanvas(object.key)}
      onTitle={(value) => resourceCanvas.title(object.key, object.incarnation, value)}
      onReload={() => resourceCanvas.reload(object.key, object.incarnation)}
      parentRetained={object.pointer.parent?.kind === "agent_working_copy" && resourceCanvas.objects.some((candidate) =>
        candidate.pointer.reference.kind === "agent" && object.pointer.parent?.kind === "agent_working_copy" && candidate.pointer.reference.id === object.pointer.parent.agent_id)}
      onReturn={(parent) => {
        if (parent.kind === "agent_draft") void openAgentCanvas({ draft_id: parent.id, draft_version: parent.version, purpose_hint: null, draft_status: null, draft_title: null });
        else { const original = resourceCanvas.objects.find((candidate) => candidate.pointer.reference.kind === "agent" && candidate.pointer.reference.id === parent.agent_id);
          if (original) selectResourceCanvas(original.key); }
      }} />,
  });
  // Tabs reuse the Open menu's names so a same-name draft keeps one ordinal in both places.
  const documentItems = documentOpenItems(documentJobs.jobs, { documents: tDocuments, settingsCanvas: tSettingsCanvas });
  for (const object of documentJobs.denied ? [] : documentCanvas.objects) canvasObjects.push({
    key: object.key, kind: "document",
    title: documentItems.find((item) => item.id === object.context.jobId)?.title || object.update?.title || tDocuments("title"),
    marker: objectStatusMarker(object.update?.objectStatus),
    statusLabel: object.update?.objectStatus ? tCanvasObject(`status.${object.update.objectStatus}`) : null,
    onClose: () => {
      const selected = activeCanvasSelectionRef.current;
      // Publishing is accepted by the server: closing never cancels it (Canvas Objects Spec C14).
      if (object.update?.phase === "confirming") toast({ kind: "info", text: tCanvasObject("closed_while_running", { name: object.update.title || tDocuments("title") }) });
      canvasOpenIntentRef.current += 1;
      const closed = documentCanvas.close(object.context);
      if (!closed || selected?.key !== object.key) return;
      const next = resolveCanvasSelection(canvasSelectionItems.filter((item) => item.key !== object.key), [closed.selectedKey]);
      if (next) selectCanvasObject(next.key);
      else setCanvasResultsSelected(true);
    },
    content: <DocumentDraftCanvasObject context={object.context} isCurrent={documentCanvas.isCurrent} refreshKey={object.refreshKey}
      onReturnToChat={(context) => {
        const scope = composerDraftPersistenceScope;
        if (!scope || !documentCanvas.isCurrent(context) || activeCanvasSelectionRef.current?.key !== object.key
          || scope.userId !== context.userId || scope.teamId !== context.teamId
          || scope.workspaceId !== context.workspaceId || scope.authSessionGeneration !== context.authSessionGeneration
          || scope.requestedSessionId !== context.sessionId) return;
        canvasOpenIntentRef.current += 1;
        setComposerRevealRequest({ id: randomUuid(), scopeKey: composerDraftScopeSignature(scope) });
      }}
      onRegenerateInChat={(context, requestText) => {
        const scope = composerDraftPersistenceScope;
        if (!scope || !documentCanvas.isCurrent(context) || activeCanvasSelectionRef.current?.key !== object.key
          || scope.requestedSessionId !== context.sessionId) return;
        // The original request goes back into the composer; nothing is sent until the user confirms.
        setExternalComposerPrefill(requestText);
        canvasOpenIntentRef.current += 1;
        setComposerRevealRequest({ id: randomUuid(), scopeKey: composerDraftScopeSignature(scope) });
      }}
      onRequestRevision={async (request) => {
        const scope = composerDraftPersistenceScope;
        const control = workbenchComposerDraftControlRef.current;
        if (!scope || !control || !request.isCurrent() || !documentCanvas.isCurrent(request.context)
          || activeCanvasSelectionRef.current?.key !== object.key
          || scope.userId !== request.context.userId || scope.teamId !== request.context.teamId
          || scope.workspaceId !== request.context.workspaceId || scope.authSessionGeneration !== request.context.authSessionGeneration
          || scope.requestedSessionId !== request.context.sessionId) return { status: "stale" };
        const canReveal = request.mayReveal();
        const focus = document.activeElement;
        const intent = canvasOpenIntentRef.current;
        const scopeKey = composerDraftScopeSignature(scope);
        const result = await control.stageDocumentRevision({
          handoffId: request.handoffId, scope: { ...scope }, target: { ...request.target }, instruction: "",
          ...(request.display ? { display: { ...request.display } } : {}),
        });
        if (result.status === "applied" && canReveal && request.mayReveal() && request.isCurrent() && documentCanvas.isCurrent(request.context)
          && workbenchComposerDraftControlRef.current === control && composerDraftScopeKeyRef.current === scopeKey
          && activeCanvasSelectionRef.current?.key === object.key && canvasOpenIntentRef.current === intent
          && (document.activeElement === focus || document.activeElement === document.body))
          setComposerRevealRequest({ id: randomUuid(), scopeKey });
        return result;
      }}
      onStateChange={(value) => {
        if (!documentCanvas.isCurrent(value.context)) return;
        documentCanvas.update(value);
        if (value.phase === "unavailable") documentJobs.forget(value.context.jobId);
      }} onOpenPublished={(context, artifactId) => {
        if (!documentCanvas.isCurrent(context) || activeCanvasSelectionRef.current?.key !== object.key) return;
        canvasOpenIntentRef.current += 1;
        lastEditorKeyRef.current = object.key;
        documentCanvas.deselect("results");
        setCanvasResultsSelected(true);
        requestCanvasResultOpen({ type: "artifact", artifactId, mode: "preview" });
      }} onPublished={(receipt) => {
        if (!documentCanvas.isCurrent(receipt.context)) return;
        setDocumentReceipt(receipt); documentJobs.retry();
      }} />,
  });
  const documentMenu: CanvasOpenDocuments | undefined = documentScope && (documentJobs.jobs.length > 0 || documentJobs.error || documentJobs.hasPrevious || documentCanvas.objects.length > 0) ? {
    items: documentItems,
    loading: documentJobs.loading, error: documentJobs.error, hasPrevious: documentJobs.hasPrevious, hasNext: Boolean(documentJobs.next),
    onOpen: showDocumentCanvas, onRefresh: () => { documentJobs.retry(); documentCanvas.invalidate(); },
    onPrevious: documentJobs.previousPage, onNext: documentJobs.nextPage,
  } : undefined;
  const openNewAgent = multipleCanvasEnabled && subagentCanvasEnabled && (activeCanvasSelection?.kind === "agent_entry" || activeCanvasSelection?.kind === "agent_draft") ? () => {
    canvasOpenIntentRef.current += 1;
    documentCanvas.deselect();
    resourceCanvas.select(null);
    operationCanvas.select(null); setSubagentCardPayload(null); setSubagentEntryChooser(true); setSubagentCardOpen(true); setCanvasResultsSelected(false); settingsCanvas.deselect();
  } : undefined;
  const canvasOpenMenu: CanvasOpenMenuModel | null = openNewAgent || documentMenu ? { onNewAgent: openNewAgent, documents: documentMenu } : null;
  const documentOpener = documentMenu ? <CanvasOpenMenu model={{ documents: documentMenu }} /> : null;
  const showCanvasResults = runner.started ? () => {
    revealCanvasResults(); canvasOpenIntentRef.current += 1; lastEditorKeyRef.current = selectedCanvasKey ?? null;
    documentCanvas.deselect("results"); setCanvasResultsSelected(true); settingsCanvas.deselect(); showDraftInUrl(null);
  } : undefined;
  const editorCanvas = canvasObjects.length && selectedCanvasKey ? (
    <ConfigurationCanvasHost objects={canvasObjects} selectedKey={selectedCanvasKey} onSelect={selectCanvasObject}
      openMenu={canvasOpenMenu} onResults={showCanvasResults} />
  ) : null;
  const canvasObjectTabs = editorCanvas && showCanvasResults ? (
    <ConfigurationCanvasStrip objects={canvasObjects} onSelect={selectCanvasObject} onResults={showCanvasResults} openMenu={canvasOpenMenu} />
  ) : null;
  const editorCanvasTitle = canvasObjects.find((object) => object.key === selectedCanvasKey)?.title;

  const homeComposerDock = (
    <div
      className="home-work-composer-dock home-work-composer-dock--inline"
    >
      <div className="home-work-composer-inner">
        {agentUseNotice ? <p role="alert">{agentUseNotice}</p> : null}
        {recoveredChatAction || showSubmissionRecovery ? (
          <ComposerRecoveryNotice
            variant={recoveredChatAction ? "terminalAction" : "submission"}
            allowAbandon={
              !recoveredChatAction
              || recoveredChatAction.recoveryState === "resend_allowed"
            }
            onRetry={recoveredChatAction
              ? () => reconcileRecoveredChatAction(recoveredChatAction)
              : handleRetryRecoveredSubmission}
            onAbandon={recoveredChatAction
              ? handleResendPendingChatAction
              : handleAbandonRecoveredSubmission}
          />
        ) : null}
        <V2Composer
          onInputInteraction={() => { canvasOpenIntentRef.current += 1; }}
          surface="home"
          key={composerResetKey}
          draftControlRef={homeComposerDraftControlRef}
          onSend={handleSend}
          disabled={
            runner.running
            || workspace.loading
            || Boolean(workspace.error)
            || !activeWorkspaceId
          }
          // Explain why Send is unavailable below the input; Space loading is transient, so omit it.
          sendBlockedReason={
            runner.running
              ? tComposer("send_blocked_running")
              : workspace.error
                ? tComposer("send_blocked_space_error")
                : !workspace.loading && !activeWorkspaceId
                  ? tComposer("send_blocked_no_space")
                  : null
          }
          readOnly={sendReadOnlyActive}
          recoveryLocked={
            recoveredChatAction !== null
            || recoveredSubmission !== null
            || chatActionAttempt !== null
            || (submissionRecoveryScope !== null && !submissionRecoveryChecked)
          }
          draftPersistenceScope={composerDraftPersistenceScope}
          onPendingActionHydrated={handlePendingActionHydrated}
          draftReconcileEpoch={draftReconcileEpoch}
          onResolveDraftSubmissionProtocol={resolveComposerDraftSubmissionProtocol}
          onCaptureSubmissionWireSnapshot={captureCanvasSubmissionWire}
          draftPersistenceFailureMode={
            submissionProtocolEnabled ? "block" : "warn"
          }
          autoFocus={composerResetKey > 0}
          // Navigation from "在新会话中使用" **lands in this input** (empty home page). Without preattached
          // attachments, handleSend still includes the file as a fallback, but users see
          // no sign that the file followed them and assume failure. Passing it provides complete semantics.
          prefillAttachments={
            recoveredChatAction?.attachments
            ?? recoveredSubmission?.attachments
            ?? handoffAttach
          }
          prefillText={
            recoveredChatAction?.draftMessage
            ?? recoveredChatAction?.message
            ?? (recoveredSubmission && !("input_document" in recoveredSubmission) && !recoveredSubmission.wireSnapshot?.documentRevisionTarget ? recoveredSubmission.message : null)
            ?? externalComposerPrefill
          }
          prefillInputDocument={externalAgentDocument}
          onPrefillInputDocumentConsumed={(disposition) => {
            setExternalAgentDocument(null);
            if (disposition === "discarded") setAgentUseNotice(agentUseDraftPreservedText);
          }}
          onPrefillConsumed={() => setExternalComposerPrefill(null)}
        />
      </div>
    </div>
  );

  const topbarTurnInput: TurnCaptionInput = {
    archivedTurnsLength: runner.archivedTurns?.length ?? 0,
    archivedTurnIds: (runner.archivedTurns ?? []).map((turn) => turn.turnId),
    hasActiveTurnId: Boolean(runner.turnId),
    activeTurnId: runner.turnId,
    hasUnarchivedActiveTurn: Boolean(runner.user || runner.answer),
    running: runner.running,
    hasError: Boolean(runner.errorReason),
    terminalStatus: runner.terminalStatus,
  };
  // P2 batch 3: the report history numbers prior reports by these session ordinals so they match the topbar caption.
  const turnOrdinalsForReports = sessionTurnOrdinals(topbarTurnInput);
  // The listed entry of the open session (pin state for the session ⋯ menu); absent until the list knows it.
  // A session being opened (URL ?session= or a list pick) that the runner has not shown yet. `loadingSid` is
  // cleared on success, hard failure and report-only/unavailable outcomes, so the skeleton cannot outlive the load.
  const pendingSessionId = loadingSid;
  const pendingSessionTitle = pendingSessionId
    ? groups.flatMap((group) => group.items).find((item) => item.id === pendingSessionId)?.title ?? ""
    : "";
  const currentSessionItem = runner.sessionId
    ? groups.flatMap((group) => group.items).find((item) => item.id === runner.sessionId && !item.archived)
    : undefined;

  // The navigation state, rather than runner lifecycle, owns the title identity. A report-only
  // session or a just-opened session can have a valid URL/list entry before the runner is ready.
  const titleSessionId = pendingSessionId ?? activeSessionId ?? runner.sessionId;
  // Title order: an optimistic rename, then the remote or optimistic session list, then the
  // runner for the same session. This keeps the chrome populated through every session state.
  const displaySessionTitle = resolveSessionTopbarTitle({
    sessionId: titleSessionId,
    titleOverrides: sessionTitleOverrides,
    remoteItems: remoteGroups.flatMap((group) => group.items),
    localItems: groups.flatMap((group) => group.items),
    runnerSessionId: runner.sessionId,
    runnerSessionTitle: runner.sessionTitle,
  });
  const topbarTitle = displaySessionTitle
    || (pendingSessionId ? pendingSessionTitle : "")
    || (titleSessionId ? tSidebar("new_work") : homeTitle ?? tSidebar("new_work"));
  const submitTopbarRename = async (sessionId: string, title: string) => {
    setRenamingSessionId(null);
    if (title === displaySessionTitle) return;
    try {
      await submitSessionRename(sessionId, title);
      trackInteraction("bowhead_sidebar_session_action_total", { action: "rename", outcome: "success", surface: "topbar" });
    } catch (error) {
      trackInteraction("bowhead_sidebar_session_action_total", { action: "rename", outcome: "error", surface: "topbar" });
      toast({ kind: "error", text: tSessionMenu(sessionRenameErrorKey(error)) });
    }
  };

  // Chat Cards Spec D12: the session ⋯ menu, in the desktop Topbar and (below 1200) the mobile top bar.
  // The menu acts on the session the title names; it is withheld while a different session is pending.
  const menuSessionId = sessionTopbarMenuTarget({ titleSessionId, runnerSessionId: runner.sessionId });
  const sessionMenu = menuSessionId ? (
    <SessionTopbarMenu
      sessionId={menuSessionId}
      onCompactContext={sendReadOnlyRef.current ? undefined : contextMaintenance.run}
      compactDisabledReason={runner.running || runner.clarificationPending || contextMaintenance.busy ? tChat("compact_context_busy") : null}
      turnId={runner.turnId}
      transport={runner.transportRef?.current ?? null}
      pinned={currentSessionItem ? Boolean(currentSessionItem.pinned) : undefined}
      onTogglePin={
        currentSessionItem
          ? () => handleToggleSessionPin(currentSessionItem.id, Boolean(currentSessionItem.pinned))
          : undefined
      }
      title={displaySessionTitle || undefined}
      onArchive={() => handleArchiveSession(menuSessionId)}
      onRestore={() => handleRestoreSession(menuSessionId)}
      archiveDisabledReason={runner.running ? tSessionMenu("rail_archive_running_error") : null}
      onDelete={() => handleDeleteSession(menuSessionId)}
      onUndelete={() => handleUndeleteSession(menuSessionId)}
      deleteDisabledReason={runner.running ? tSessionMenu("rail_delete_running_error") : null}
      onRename={currentSessionItem ? () => setRenamingSessionId(currentSessionItem.id) : undefined}
    />
  ) : null;

  return (
    // layout-redesign P2 (R1): the 3 hand-rolled `.v2-app-root` compositions collapse
    // into the persistent <ProductShell> frame plus this page-owned <AppShell> slot.
    // ALL runner/hydration/sessions/canvas state stays in this page; AppShell only
    // registers the current session adapter with the persistent UnifiedSidebar. The
    // topbar's runner-derived title/meta are built HERE and passed down via slots
    // (runner never leaves the page). The right cluster (space/notifications/credits)
    // is owned by <Topbar> and shared across every route.
    <AppShell
      onNewSession={handleNewSession}
      mobileActions={sessionMenu}
      mobileTitle={topbarTitle}
      // 116 phone: the top bar said "New work" while a session was loading. Until the session (or its list
      // title) is known, show a title skeleton instead.
      mobileTitleLoading={Boolean(pendingSessionId) && !displaySessionTitle && !pendingSessionTitle}
      // The large-title treatment belongs only to Home. Every active or pending session keeps its
      // title visible, regardless of whether a runner has started or which Canvas surface is open.
      collapseTopbarTitle={!titleSessionId}
      unifiedSidebarSessions={{
        groups,
        loading: remoteSessions.loading,
        error: remoteSessions.error,
        stalledOn: remoteSessions.stalledOn,
        onRetry: remoteSessions.retry,
        olderSessions: olderSessionsPaging(remoteSessions),
        currentSessionId: runner.sessionId,
        workspaceId: activeWorkspaceId,
        archivedCount: remoteSessions.archivedCount,
        onPick: handlePickSession,
        onTogglePin: handleToggleSessionPin,
        onArchive: handleArchiveSession,
        onRestore: handleRestoreSession,
        onDelete: handleDeleteSession,
        onUndelete: handleUndeleteSession,
      }}
      topbar={
        <Topbar
          titleSlot={
            <>
              <span className="title">
                {/* P4.5b #14: omit placeholder "Canvas" when no session is active, letting main dominate visually.
                    The home title stays hidden while the home H1 is on screen and fades in after it scrolls away. */}
                {topbarTitle}
              </span>
            </>
          }
          // Chat Cards Spec D12: session actions, including "Copy Diagnostic Information" (the session and turn
          // ids needed when reporting an issue), live in the session ⋯ menu; turn number and duration are not
          // repeated in the title bar (duration is in the step summary).
          metaSlot={sessionMenu}
        />
      }
    >
      <SessionTurnOrdinalsContext.Provider value={turnOrdinalsForReports}>
      <CoachReceiptScopeContext.Provider value={authSession?.me?.id && authSession.me.team_id && activeWorkspaceId && activeSessionId
        ? { userId: authSession.me.id, teamId: authSession.me.team_id, workspaceId: activeWorkspaceId, sessionId: activeSessionId } : null}>
      {banner ? <div className="v2-banner">{banner}</div> : null}
      {canvasSaveBlockObject ? (
        <div className="v2-banner v2-banner--actions" role="alert">
          <span>{tCoachResourceEditor(canvasSaveFailed ? "chat_save_failed" : "chat_save_and_continue_body")}</span>
          <span className="v2-banner-actions">
            <button type="button" disabled={canvasSaveBusy !== null} onClick={() => void resolveCanvasSaveBlock("discard")}>
              {tCoachResourceEditor("chat_discard_edits")}
            </button>
            <button type="button" className="v2-banner-primary" disabled={canvasSaveBusy !== null} onClick={() => void resolveCanvasSaveBlock("save")}>
              {tCoachResourceEditor(canvasSaveBusy === "save" ? "chat_saving" : "chat_save_and_continue")}
            </button>
          </span>
        </div>
      ) : null}
      {/* licensing §13.2 — full-width entitlement strip pinned below the nav, pushes content down. */}
      <EntitlementBanner />
      <SendInitErrorBanner
        reasonCode={recoveredChatAction || recoveredSubmission
          ? null
          : runner.sendErrorReason}
      />
      <ReferenceFollowUpQueue
        workspaceId={activeWorkspaceId || null}
        sessionId={sessionLoading ? null : activeSessionId}
        actorScopeKey={
          authSession?.me?.id
          && authSession.me.team_id
          && authSession.me.auth_session_generation
            ? JSON.stringify([
                authSession.me.id,
                authSession.me.team_id,
                authSession.me.auth_session_generation,
              ])
            : null
        }
        canCancel={!sendReadOnlyActive}
        refreshKey={[
          runner.turnId ?? "",
          runner.executionGeneration ?? "",
          runner.running ? "running" : "settled",
          referenceQueueRefresh,
        ].join(":")}
      />
      {referenceSubmissionPreview?.scopeKey === composerDraftScopeKey ? (
        <ComposerReferencePreview
          document={referenceSubmissionPreview.draft.input_document}
          attachments={referenceSubmissionPreview.draft.attachments}
          collapsed
        />
      ) : null}
      {sessionLoading ? (
        // ② loading skeleton while a picked/restored session hydrates — covers the
        // whole fetch→hydrate window so the main area never sits blank or on stale
        // content.
        // chat-ux-2026-08 batch E: the old 740px single column differed from the real left/right split,
        // causing layout jumps as content arrived; it also lacked meaningful copy. See component comments.
        <SessionLoadingSkeleton />
      ) : runner.started || showingReportOnly || editorCanvas !== null ? (
        <ChatWorkbench
          runner={runner}
          onContinue={handleContinue}
          // Report-only and editor views keep the report (canvas) title in the tab.
          documentTitle={runner.sessionId && !showingReportOnly && editorCanvas === null ? displaySessionTitle ?? null : null}
          prefillText={
            recoveredChatAction?.draftMessage
            ?? recoveredChatAction?.message
            ?? (recoveredSubmission && !("input_document" in recoveredSubmission) && !recoveredSubmission.wireSnapshot?.documentRevisionTarget ? recoveredSubmission.message : null)
            ?? externalComposerPrefill
          }
          onPrefillConsumed={() => setExternalComposerPrefill(null)}
          composerSubmitRequest={composerSubmitRequest}
          prefillInputDocument={alignmentContinueDocument}
          onPrefillInputDocumentConsumed={onAlignmentContinueConsumed}
          prefillAttachments={
            recoveredChatAction?.attachments
            ?? recoveredSubmission?.attachments
            ?? handoffAttach
          }
          submissionRecoveryPending={
            recoveredChatAction !== null || showSubmissionRecovery
          }
          submissionRecoveryVariant={
            recoveredChatAction ? "terminalAction" : "submission"
          }
          submissionRecoveryAllowAbandon={
            !recoveredChatAction
            || recoveredChatAction.recoveryState === "resend_allowed"
          }
          submissionRecoveryLocked={
            recoveredChatAction !== null
            || recoveredSubmission !== null
            || chatActionAttempt !== null
            || (submissionRecoveryScope !== null && !submissionRecoveryChecked)
          }
          draftControlRef={workbenchComposerDraftControlRef}
          composerRevealRequest={composerRevealRequest}
          onComposerRevealConsumed={(id) => setComposerRevealRequest((request) => request?.id === id ? null : request)}
          draftPersistenceScope={composerDraftPersistenceScope}
          onPendingActionHydrated={handlePendingActionHydrated}
          draftReconcileEpoch={draftReconcileEpoch}
          onResolveDraftSubmissionProtocol={resolveComposerDraftSubmissionProtocol}
          onCaptureSubmissionWireSnapshot={captureCanvasSubmissionWire}
          draftPersistenceFailureMode={
            submissionProtocolEnabled ? "block" : "warn"
          }
          onRetryRecoveredSubmission={recoveredChatAction
            ? () => reconcileRecoveredChatAction(recoveredChatAction)
            : handleRetryRecoveredSubmission}
          onAbandonRecoveredSubmission={recoveredChatAction
            ? handleResendPendingChatAction
            : handleAbandonRecoveredSubmission}
          onSend={handleSend}
          onReplay={handleReplay}
          replayRestoresDraft={!submissionProtocolEnabled}
          lastReferenceDocument={lastSentReferenceDocument}
          onCancel={handleCancel}
          stopRequested={
            Boolean(
              runner.turnId
              && runner.executionGeneration
              && cancelRequestedKey
                === `${runner.turnId}:${runner.executionGeneration}`,
            )
          }
          onSteer={handleSteer}
          readOnly={sendReadOnlyActive}
          reportOnly={reportOnlyActive}
          startMaximized={startMaximizedActive}
          onSolidify={
            solidifyEnabled
              ? (tid) => {
                  setSolidifyTurnId(tid);
                  setSolidifySessionId(null);
                  setSolidifyHint(null);
                  setSolidifyCardOpen(true);
                }
              : undefined
          }
          // S2: solidify the entire conversation. Share the single-turn card, changing only the distillation target to the session.
          onSolidifySession={
            solidifyEnabled
              ? (sid) => {
                  setSolidifyTurnId(null);
                  setSolidifySessionId(sid);
                  setSolidifyHint(null);
                  setSolidifyCardOpen(true);
                }
              : undefined
          }
          configDrawerOpen={configDraftId !== null}
          configDrawer={(layout) =>
            configDraftId ? (
              <ConfigDrawer draftId={configDraftId} layout={layout} onClose={() => setConfigDraftId(null)} />
            ) : null
          }
          onCanvasInteraction={() => { canvasOpenIntentRef.current += 1; }}
          resultsRevealRequest={canvasResultsReveal?.sessionId === activeSessionId ? canvasResultsReveal.sequence : 0}
          creationPanelOpen={editorCanvas !== null && !canvasResultsSelected}
          canvasObjectOpener={canvasObjectTabs ? null : documentOpener}
          canvasObjectTabs={canvasObjectTabs}
          canvasObjectList={documentOpener ? <DocumentCanvasList jobs={documentJobs.jobs} loading={documentJobs.loading} error={documentJobs.error}
            hasPrevious={documentJobs.hasPrevious} hasNext={Boolean(documentJobs.next)} onOpen={showDocumentCanvas}
            onRefresh={() => { documentJobs.retry(); documentCanvas.invalidate(); }} onPrevious={documentJobs.previousPage} onNext={documentJobs.nextPage} /> : null}
          documentResultsRefresh={documentScope && documentResultsRevision !== "[]" ? { scope: documentScope, revision: documentResultsRevision } : null}
          creationPanel={editorCanvas}
          creationPanelTitle={editorCanvasTitle}
          creationPanelCount={canvasObjects.length}
          onShowCreationPanel={() => {
            const target = resolveCanvasSelection(canvasSelectionItems, [lastEditorKeyRef.current, selectedCanvasKey]);
            if (target) selectCanvasObject(target.key);
            else setCanvasResultsSelected(false);
          }}
          homePane={!runner.started && !showingReportOnly ? <HomeWorkSurface workspaceId={activeWorkspaceId || null} workspaceName={workspace.activeName}
            workspaceLoading={workspace.loading} workspaceError={workspace.error} readOnly={sendReadOnlyActive}
            onStartPrompt={(prompt) => { setExternalComposerPrefill(prompt); return true; }} onWorkspaceInvalidated={workspace.refresh}
            composerSlot={showHomeComposer ? homeComposerDock : undefined} /> : undefined}
        />
      ) : (
        <div className={`home-work-page${editorCanvas ? " home-work-page--agent-creation" : ""}`}>
          <div className="home-work-page-content">
            <HomeWorkSurface
              workspaceId={activeWorkspaceId || null}
              workspaceName={workspace.activeName}
              workspaceLoading={workspace.loading}
              workspaceError={workspace.error}
              readOnly={sendReadOnlyActive}
              onStartPrompt={(prompt) => {
                if (shouldRouteSecondaryChatEntryThroughComposer(
                  null,
                  false,
                  authSession?.me?.chat_execution ?? null,
                )) {
                  setExternalComposerPrefill(prompt);
                  return true;
                }
                return handleSend(prompt);
              }}
              onWorkspaceInvalidated={workspace.refresh}
              composerSlot={showHomeComposer ? homeComposerDock : undefined}
            />
          </div>
          {editorCanvas ? (
            <aside className="configuration-canvas-pane" aria-label={editorCanvasTitle}>
              <div className="configuration-canvas-pane-body">{editorCanvas}</div>
            </aside>
          ) : null}
        </div>
      )}
      {/* task_plan HITL now routes through clarification_ask SSE (backend
          chat-redesign Phase 1.1 P5) → standard HITLPickerCard surface inside
          TurnContainer. The floating WS modal (TaskPlanCardMount) is retired. */}
      {/* cca P2 (C2): the inline create-tool card, opened by the cca.tool_create_open event.
          (Merged onto the v10 AppShell — the overlay is fixed-position, lives inside the shell.) */}
      {createCardOpen ? (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 60,
            background: "color-mix(in srgb, var(--c-ink) 34%, transparent)",
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "center",
            padding: "48px 16px",
            overflowY: "auto",
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) setCreateCardOpen(false);
          }}
        >
          <div style={{ width: "100%", maxWidth: 560 }}>
            <CreateToolCard
              specUrlHint={createCardHint}
              kindHint={createCardKind}
              presets={connectorPresets}
              onClose={() => setCreateCardOpen(false)}
            />
          </div>
        </div>
      ) : null}

      {/* workflow-solidify S2: the inline solidify card overlay (same chrome as create-tool). */}
      {solidifyCardOpen ? (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 60,
            background: "color-mix(in srgb, var(--c-ink) 34%, transparent)",
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "center",
            padding: "48px 16px",
            overflowY: "auto",
          }}
          onClick={(e) => {
            if (e.target === e.currentTarget) setSolidifyCardOpen(false);
          }}
        >
          <div style={{ width: "100%", maxWidth: 560 }}>
            <CreateWorkflowCard
              turnId={solidifyTurnId}
              sessionId={solidifySessionId}
              hint={solidifyHint}
              onClose={() => setSolidifyCardOpen(false)}
            />
          </div>
        </div>
      ) : null}
      {renamingSessionId && renamingSessionId === runner.sessionId ? (
        <SessionRenameDialog
          initialTitle={renameInitialTitle(renamingSessionId, displaySessionTitle)}
          onCancel={() => setRenamingSessionId(null)}
          onConfirm={(title) => void submitTopbarRename(renamingSessionId, title)}
        />
      ) : null}
      </CoachReceiptScopeContext.Provider>
      </SessionTurnOrdinalsContext.Provider>
    </AppShell>
  );
}

/**
 * Auth gate for the root canvas page. The root rendered the full chat shell for ANYONE — on a
 * non-dev deployment its store calls now 401 (the passwordless-login backend fix), so an anonymous visitor
 * saw a dead shell stuck on the user-menu `loading_user` placeholder instead of the login page.
 * Mirror the protected-page pattern (knowledge / admin / profile): `useAuthRedirect` redirects to
 * `/login` on a 401 and only the authed branch mounts the (hook-heavy) inner page. The neutral
 * placeholder avoids flashing the chat chrome / a locale-unresolved key during the redirect.
 */
export default function CanvasHome() {
  const auth = useAuthRedirect();
  if (auth.status !== "authed") {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: "100vh",
          color: "var(--c-ink-3)",
          fontSize: "0.875rem",
        }}
      >
        <PageStateHeading><BrandName /></PageStateHeading>
        …
      </div>
    );
  }
  // Next 15 + output:export requires useSearchParams() inside <Suspense> for prerendering
  // (same handling as admin/credits/users).
  return (
    <Suspense fallback={null}>
      <WorkspaceSettingsCanvasProvider><CanvasHomeInner /></WorkspaceSettingsCanvasProvider>
    </Suspense>
  );
}
