"use client";

/**
 * TurnContainer — chat panel renderer per chat-redesign Phase 1.7-B.
 *
 * Composition (per v8 `variant-a.jsx` BotTurn + Phase 1.7-B tech-design §4):
 *
 *   .chat-content-frame  (max-width min(920px, 100%), centered, replaces .chat-panel)
 *     ├── ArchivedTurnView × N  (live==archived DOM parity)
 *     └── current turn
 *         ├── UserMessage (no-bubble; right-aligned mono byline + plain prose)
 *         ├── BotByline   (Bowhead identity + time — mounted ONCE)
 *         ├── PlanSection (lifted from StepListCard chrome; vertical layout)
 *         ├── beats[] dispatch
 *         │     ├── narrative → LeadNarrative
 *         │     ├── step      → marker only (StepListCard mounts ONCE at the
 *         │     │               first step-beat's position, internally handling
 *         │     │               concurrency grouping)
 *         │     ├── picker    → ClarificationCard / ClarificationAnsweredSummary
 *         │     └── answer    → ChatBubbleNew
 *         ├── StepSummary    (normal completed disclosure before final answer)
 *         ├── FinalFailBanner
 *
 * Deleted (vs. pre-1.7-B):
 *   · `<div className="chat-panel">` wrapper
 *   · per-turn `<div className="tline">` wrapper
 *   · `<div className="ab">` inner wrapper
 *   · UserBubble `<div className="ub"><div className="bub">` chrome
 *   · Canvas handoff pill mounts (current + archived) + canvas-tool gating
 *     helpers (deleted in 1.7-C, see phase-1.7-tech-design §5).
 *   · V3TurnStream text-only fallback (runner.messages.map)
 *   · SteerMessage render — relocated to shell (workspace.tsx) above composer
 *
 * Tokens come from `step-list/tokens.ts`; legacy `globals.css` `.tline/.ub/.bub/.ab`
 * rules become dead and are swept in 1.7-E.
 */

import { Fragment, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  canonicalizeAnswers,
  projectAnswerValues,
} from "@/lib/clarification-answers";
import { buildOptionValueByLabel, relabelQuestions } from "@/lib/clarification-option-labels";
import { useTranslations } from "next-intl";

import {
  fetchEffectDisclosure,
  listClarificationsForTurn,
  resolveClarification,
  resumeTurnAfterClarification,
  type EffectDisclosureOverlayDTO,
  type ResolveClarificationPayload,
} from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { getChatSendErrorMessage } from "@/lib/chat-send-errors";
import type { ErrorDiagnostics, ErrorPresentation } from "@/lib/error-presentation";
import { ruleClarificationErrorPresentation } from "@/lib/rule-clarification-error";
import { clarificationResumeFailure } from "@/lib/hydrate-clarifications";
import {
  isClarificationAnswered,
  markClarificationAnswered,
  unmarkClarificationAnswered,
} from "@/lib/clarification-tombstone";
import type { ReasonCode } from "@/lib/error-messages";
import { extractInlineArtifacts } from "@/lib/inline-artifacts";
import {
  acknowledgePlanEdit,
  invalidatePlanRevision,
  applyPlanSnapshot,
  dismissPlan,
  getPlanSnapshot,
  setPlanLocalStatus,
  usePlan,
} from "@/lib/plan-store";
import { amendPlan, approvePlanViaSend, fetchPlanStateAuthed, patchPlanSteps } from "@/lib/plan-api";
import { planActionErrorKey } from "./plan-card/plan-action-errors";
import { isDefinitiveComposerChatActionRejection } from "@/lib/composer-chat-action";
import { fetchMe } from "@/lib/auth-api";
import { Markdown } from "@/lib/md";
import { useMemoryCandidates, type MemoryCandidateMode } from "@/lib/use-memory-candidates";
import { useFeedbackReceipts } from "@/lib/use-feedback-receipts";
import { MemoryCandidateCard } from "./memory-candidate-card";
import { FeedbackReceiptCard } from "./feedback-receipt-card";
import { CoachChangeReceipts } from "./coach-change-receipt";
import { CoachAlignmentCards } from "./coach-alignment-card";
import { MemoryDisclose } from "@/components/chat/memory-disclose";
// prod-bug-bash-039 #13: cursor active getter for per-segId narrative streaming
import { isNarrativeSegActive } from "@/lib/turn-store-dispatch";
import {
  getTurnSnapshot,
  hasBlockingApprovalState,
  hasPeerDisclosureFacts,
  selectAnswerPayload,
  selectNarrativeBuffer,
  useTurn,
  type PeerTerminal,
  type TurnBeat,
  type TurnState,
} from "@/lib/turn-store";
import { toast } from "@/lib/use-toast";
import type {
  ArchivedTurn,
  AttachmentRef,
  ClarificationAnswer,
  ThetaClarificationQuestion,
  TurnRunnerState,
} from "@/lib/types";

import { AttachmentChip, type AttachmentItem } from "./attachment-chip";
import { useCopyDiagnosticsToastAction } from "@/components/error-notice";
import { FinalFailBanner } from "./rho/final-fail-banner";
import { DocumentRevisionReceipt, documentRevisionReceiptState } from "./document-revision-receipt";
import { documentCanvasOpen, documentCanvasVersion, subscribeDocumentCanvas } from "@/lib/document-canvas-events";
import { requestDocumentCanvas } from "@/lib/document-canvas-requests";
import type { DocumentRevisionReceipt as DocumentRevisionReceiptFacts, ModelRetryStatus } from "@/lib/turn-store";
import { handoffQuestionToNewChat } from "@/lib/composer-handoff";
import { resolveRecoveryAction } from "@/lib/turn-feedback";
import { BotByline } from "./step-list/BotByline";
import { ChatBubbleNew } from "./step-list/ChatBubbleNew";
import { SolidifyChip } from "@/components/workflow-solidify/turn-chip";
import {
  ClarificationAnsweredSummary,
  ClarificationCard,
  type ClarificationCardProps,
  type ClarificationMultiSubmit,
} from "./step-list/ClarificationCard";
import {
  ChatSourcesProvider,
  chatSourcesFromPayload,
  CitationsProvider,
  LeadNarrative,
  stripInternalMarkers,
} from "./step-list/LeadNarrative";
import { MessageActions } from "./message-actions";
import { ContinueAction, continueTargets } from "./continue-action";
import { TurnRunningProvider } from "./step-list/turn-running";
import { NetworkBanner } from "./step-list/NetworkBanner";
import { PlanSection } from "./step-list/PlanSection";
import { PlanCardConnected } from "./plan-card";
import { DraftChip } from "@/components/config-copilot/draft-chip";
import { requestOpenDraft } from "@/lib/config-copilot-draft";
import { LiteConfirmConnected } from "@/components/config-copilot/lite-confirm-connected";
import { stepsToStepViews } from "./step-list/step-view";
import { AgentTrace } from "./step-list/AgentTrace";
import { CollaborationTraceProvider } from "./step-list/collaboration-trace-state";
import { ContextActivityProvider, useContextActivities } from "./step-list/ContextActivity";
import { StepSummary } from "./step-list/StepSummary";
import { formatModelRetry, RunningBand, useElapsedText } from "./step-list/RunningBand";
import type { StepView } from "./step-list/types";
import { elapsedAnchorKeyOf, turnElapsedMs } from "@/lib/turn-elapsed";
import { getActiveSpaceId } from "@/lib/space-storage";
import { appPath } from "@/lib/base-path";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { useResultsWorkspaceCapability } from "@/lib/use-results-workspace-capability";
import { resolveUserMessageTimeMs } from "@/lib/turn-user-time";
import { CharIcon } from "@/components/char-icon";

type ExplicitDisclosurePlacement = "inline-open" | "before-answer-open";

interface Props {
  runner: TurnRunnerState;
  /** HITL continuation — picker option selection. */
  onSend?: (text: string) => void;
  /** Continue button: send the visible "continue" message that resumes these saved helper runs. */
  onContinue?: (checkpointIds: string[]) => void | Promise<boolean>;
  /** workflow-solidify surface B: open the 固化 card for this completed analysis turn. */
  onSolidify?: (turnId: string) => void;
  /** scheduled-recipe-runs S2: solidify the **entire conversation** (after multiple trial/correction turns, retain only the final agreed path). */
  onSolidifySession?: (sessionId: string) => void;
  /** Put text back into the composer without sending it (expired clarification "Ask again"). */
  onComposerPrefill?: (originalText: string | null) => void;
  /** Chat turn feedback D5: failure-card Retry for the latest turn. Same path as the composer's
   *  replay affordance (server replay, or the question restored with its real references). */
  onRetryTurn?: () => void;
  /** Replay only restores the question into the composer here; Retry then degrades to Edit and send. */
  retryRestoresDraft?: boolean;
}

// A (editable) review P1-1: serialize per-plan PATCH /steps so a stale (older-snapshot)
// persist can't commit last and clobber a newer edit (replace_steps is full DELETE+INSERT).
// Keyed by planId; each persist chains after the previous and only adopts the backend
// result if it's still the chain tail (else a newer edit is pending → don't overwrite it).
const _planPersistChain = new Map<string, Promise<void>>();

/** Settled clarification row data (persistent answered state after the card is gone). */
interface AnsweredClarification {
  requestId: string;
  values: string[];
  skipped?: boolean;
  /** The server no longer accepts the question (expired or already handled). */
  expired?: boolean;
  /** Resolved, but the resumed turn could not be attached. */
  resumeFailed?: boolean;
}

/** A document task saves its pause before its question can be answered (409 clarification_pause_pending). */
const PAUSE_POLL_INTERVAL_MS = 2000;
const PAUSE_POLL_MAX_ATTEMPTS = 30;

export function clarificationMultiFallbackText(
  { answers }: ClarificationMultiSubmit,
  skipFallback: string,
): string {
  const questionCount = new Set(answers.map((answer) => answer.question)).size;
  return answers
    .map((answer) => {
      if (answer.source === "skip") {
        return `${answer.question}: ${skipFallback}`;
      }
      return questionCount > 1 ? `${answer.question}: ${answer.answer}` : answer.answer;
    })
    .filter(Boolean)
    .join("、") || skipFallback;
}

/**
 * Plan-step ID → Team-**configured** subagent display name.
 *
 * chat-ux-2026-08 plan integration batch uses the same provenance join key:
 * `agent_runner_adapter.py` derives `subagent_id = "sub_" + stepId` for `ps_` plan-step IDs;
 * FE consumes `agent_spawned`, storing `display_name` in `peerDisplayNames[subagentId]`.
 * Thus card and external disclosure show **the same name**, including Team customizations.
 *
 * If unavailable (old plan / unconfigured name / unmatched step), return undefined;
 * `planAgentLabel` falls back to agent-meta's canonical name without inventing one.
 */
function planStepDisplayNameResolver(
  peerDisplayNames: Record<string, string> | undefined,
): ((planStepId: string) => string | undefined) | undefined {
  if (!peerDisplayNames) return undefined;
  return (planStepId: string) => peerDisplayNames[`sub_${planStepId}`];
}

/**
 * Render a plan card **once** per conversation, in the turn where it **first appears**.
 *
 * chat-ux-2026-08 · 116 prod 1.0.79 device dogfood (`dogfood-116-prod-1.0.79-2026-08-09.md` §3):
 * proposing a plan → **refreshing** → approving execution displayed the same card **twice**
 * (proposal turn 1323px + execution turn 1419px, identical text). **F5 left only one**,
 * meaning **live ≠ replay**, violating CLAUDE.md's chat invariant.
 *
 * Why first-appearance deduplication rather than guessing the second card's origin:
 *  ① **Replay already renders in the first appearance's turn** (observed after F5: one proposal card,
 *     zero execution cards). first-wins **aligns live with replay**, rather than introducing new semantics.
 *  ② Execution **cannot** legitimately own this plan beat; two independent observations:
 *     live EventSource probe installed after refresh but before approval saw only
 *     `plan_running`/`plan_completed`, no `plan_proposed`; production API retrieval of execution turn's
 *     **persisted** 59 events likewise found none. `plan_proposed` is the only trigger for
 *     `pushPlanBeat` repository-wide. The second card is therefore invalid; deduplication suppresses no legitimate rendering.
 *  ③ Cards subscribe to plan-store **by planId**, so duplicate renders always receive
 *     identical live state; the second adds no information.
 *  ④ `turn-store.ts`'s plan-beat type comment **declares** this intent:
 *     the card keeps updating from the execution turn even though the beat
 *     **lives on the proposal turn**. first-wins restores the existing contract, rather than redefining it.
 *
 * ⚠ The exact creation mechanism **remains unidentified** (production permits only black-box probes; no local backend repro).
 * This fixes the **invariant itself**, rather than a guessed path: regardless of the second card's origin,
 * one plan gets one card. Mechanism investigation remains pending.
 */
/**
 * plan_blocked failure card: offer resuming the plan this Turn left blocked (the plan card's own
 * "Resume plan" approval). Available only while that plan is still blocked, its revision is known and it
 * belongs to the current Space; otherwise the card keeps Edit and send.
 */
function useBlockedPlanResume(
  planId: string | undefined,
  resume: ((planId: string) => Promise<boolean>) | undefined,
): { available: boolean; revision: string; onResume: () => Promise<boolean> } | undefined {
  const plan = usePlan(planId ?? "");
  if (!planId || !resume) return undefined;
  const available = plan?.status === "blocked"
    && plan.currentRev !== undefined
    && Boolean(plan.workspaceId)
    && plan.workspaceId === getActiveSpaceId();
  return { available, revision: `${planId}:${plan?.currentRev ?? ""}:${plan?.status ?? ""}`, onResume: () => resume(planId) };
}

/**
 * Revision receipt under a Turn's user message (Chat Cards Spec §4). Title and draft number come from the
 * persisted `turn_started.document_revision`; the new draft opens through the Turn's own `document.draft_open`
 * pointer, so live and replayed receipts open the same draft.
 */
function TurnRevisionReceipt({
  receipt,
  turnId,
  sessionId,
  running,
  failed,
  terminalStatus,
  settled,
  startedAt,
  elapsedKey,
  time,
}: {
  receipt: DocumentRevisionReceiptFacts | undefined;
  turnId: string | null | undefined;
  sessionId: string | null | undefined;
  running: boolean;
  failed: boolean;
  terminalStatus?: string | null;
  settled: boolean;
  /** Generating: the Turn's start, ticked into "42 秒" like the running band. */
  startedAt?: number | null;
  elapsedKey?: string | null;
  /** Ready / sent: the Turn's time ("14:36"). */
  time?: string;
}) {
  useSyncExternalStore(subscribeDocumentCanvas, documentCanvasVersion, documentCanvasVersion);
  const tBand = useTranslations("step_list");
  const pointer = documentCanvasOpen(turnId);
  const state = documentRevisionReceiptState({ running, failed, terminalStatus, hasSuccessorDraft: Boolean(pointer) });
  const elapsed = useElapsedText(state === "generating" ? startedAt ?? null : null, running, tBand, elapsedKey ?? null);
  if (!receipt) return null;
  const onOpen = pointer && sessionId && pointer.session_id === sessionId
    ? () => { requestDocumentCanvas({ sessionId, jobId: pointer.job_id }); }
    : undefined;
  return (
    <DocumentRevisionReceipt
      receipt={receipt}
      state={state}
      settled={settled}
      onOpen={onOpen}
      meta={state === "generating" ? elapsed || undefined : time || undefined}
    />
  );
}

function buildPlanFirstTurnIndex(
  archived: readonly ArchivedTurn[],
  liveBeats: readonly { kind: string; planId?: string | null }[] | undefined,
  /** ★ Read beats from the **same source as rendering**. `ArchivedTurnView` uses
   *  `storeState?.beats ?? archived.beats ?? []` (turn-store first);
   *  indexing only `archived.beats` misses plans when turn-store is populated **after** the archived snapshot
   *  (reconcileOnMount / late replay). Rendering calls renderPlan without that planId in the index,
   *  so `planAllowed` returns false, **erasing the entire plan card**.
   *  (A real self-inflicted bug caught during final sprint review.) */
  beatsOf: (t: ArchivedTurn) => readonly { kind: string; planId?: string | null }[] | undefined,
): Map<string, number> {
  const first = new Map<string, number>();
  const note = (beats: readonly { kind: string; planId?: string | null }[] | undefined, idx: number) => {
    for (const b of beats ?? []) {
      if (b.kind !== "plan") continue;
      const pid = b.planId ?? "";
      if (pid && !first.has(pid)) first.set(pid, idx);
    }
  };
  archived.forEach((a, i) => note(beatsOf(a) as never, i));
  note(liveBeats as never, archived.length); // Live turn follows archived turns
  return first;
}

export function TurnContainer({ runner, onSend, onContinue, onSolidify, onSolidifySession, onComposerPrefill, onRetryTurn, retryRestoresDraft = false }: Props) {
  // NOTE: `t` (imported as `tokens`) is the design-token object used throughout
  // this file; the translator is bound to `tr` to avoid shadowing it.
  const tr = useTranslations("chat");
  const tpc = useTranslations("plan_card");
  const tClar = useTranslations("clarification");
  const tErrors = useTranslations("errors");
  const tReasons = useTranslations("error_messages.reasons");
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  const prepareResend = (originalText: string) => {
    if (!onComposerPrefill || !originalText.trim()) return;
    onComposerPrefill(originalText);
  };
  const archivedTurns = runner.archivedTurns ?? [];

  // Subscribe turn-store for the active turn — beats[] is the single emit
  // log driving composition; emojiAcks still feed steer (consumed in shell
  // via use-steer-acks now, but turn-store-level subscription kept here
  // so the current-turn render stays reactive to dispatch).
  const turnStoreState = useTurn(runner.turnId ?? "");

  // hasCurrentTurn = there is genuinely something to render for the in-progress
  // turn. After `done(completed) + clarification_required` (cascading clarif on
  // the same logical turn) the runner state archives user/answer/traceSteps but
  // keeps runner.turnId set. Without the additional guards the result is an
  // empty current-turn container holding only the follow-up picker — visually
  // a duplicate, since the archived turn already rendered as its own block
  // immediately above. Gate on actual content (user / running stream / answer
  // / traceSteps), not turnId alone, so the picker (when nothing else is
  // current) renders alongside the archived turn without a phantom row.
  // A failed attachment-only turn has no text but still needs its failure card: Retry lives only there (D4).
  const currentHasContent = Boolean(
    runner.user ||
      runner.running ||
      runner.answer ||
      (runner.operationKind === "context_maintenance" && !runner.archivedTurns?.some((turn) => turn.turnId === runner.turnId)) ||
      (runner.traceSteps && runner.traceSteps.length > 0) ||
      (runner.errorReason && (runner.userAttachments?.length ?? 0) > 0),
  );
  const baseHasCurrentTurn = currentHasContent && Boolean(runner.turnId);
  const runningEffectDisclosureVisible = Boolean(
    runner.running
    && hasVisibleUnsettledEffect(runner.effectDisclosure),
  );

  // chat-ux-2026-08 batch C: old auto-scroll effect **removed entirely**.
  //
  // Two defects: ① dependency array checks only `traceSteps.length`, never reacting to existing steps' **internal height growth**;
  // ② `dist > 200` exits permanently after one crossing. Observed on 116:
  // distance from bottom increased monotonically 0→230→581→1418px.
  //
  // `lib/use-stick-to-bottom` now handles following centrally, attached to the **actual scroll container**
  // (`.v2-messages`, see chat-workbench.tsx), using ResizeObserver to detect content growth.
  // Coexisting following systems fight each other, so retain no remnants here.
  // ⚠ Final-review correction: the former keep-it-for-other-consumers claim was **unverified and wrong**.
  // Repository search found `tailRef` only defined and mounted, never read (removing batch C's auto-scroll effect
  // eliminated its consumer; use-stick-to-bottom observes the content root rather than this anchor).
  // Retained because the 1px div still supplies bottom spacing; ref is harmless but **currently unused**.
  const tailRef = useRef<HTMLDivElement | null>(null);

  // Phase 1.6 G4 — pluck `inline_artifacts` from the lead.final.payload stage
  // on the runner. Backend `_collect_inline_artifacts` shapes each entry to
  // match `ChatInlineArtifact`, so we just need to narrow the unknown blob.
  const inlineArtifacts = useMemo(
    () => extractInlineArtifacts(runner.finalPayload),
    [runner.finalPayload],
  );

  // D10: id of a clarification the user has just answered. We hide the card
  // optimistically on answer instead of waiting for the resolve + resume round-trip
  // to clear runner.clarificationPending (which previously left the card mounted,
  // in a busy state, until the whole turn finished).
  // dingsiyan #11: answered state has two sources: tombstone (lib/clarification-tombstone, authoritative replay-resurrection
  // prevention surviving attachStream) + answeredSummary (component summary-row data, with full values context
  // only at click time). Replaces D10's component-level answeredClarificationId,
  // cleared by an effect when attachStream clears pending, letting replayed
  // clarification_required revive the unanswered card (frontend half of root-cause report D3).
  const [answeredSummary, setAnsweredSummary] = useState<AnsweredClarification | null>(null);
  // A failed submission brings the card back with the same choice selected (Chat Cards Spec §1 "error").
  const [restoredAnswers, setRestoredAnswers] = useState<{
    requestId: string;
    answers: ClarificationAnswer[];
  } | null>(null);
  // A document task is still saving its pause: the card stays, locked, with the reason, until the pause settles.
  const [pausePending, setPausePending] = useState(false);
  const pausePollRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (pausePollRef.current !== null) window.clearTimeout(pausePollRef.current);
  }, []);
  const [clarificationError, setClarificationError] =
    useState<ErrorPresentation | null>(null);
  const [blockedClarification, setBlockedClarification] = useState<{
    turnId: string;
    generation: number | null;
    requestId: string;
    diagnostics?: ErrorDiagnostics;
    answerConfirmed: boolean;
  } | null>(null);
  const clarificationRunnerRef = useRef(runner);
  clarificationRunnerRef.current = runner;
  const clarificationMounted = useRef(true);
  useEffect(() => {
    clarificationMounted.current = true;
    return () => { clarificationMounted.current = false; };
  }, []);
  const currentBlockedClarification = blockedClarification
    && blockedClarification.turnId === runner.turnId
    && blockedClarification.generation === (runner.executionGeneration ?? null)
    && !runner.running
    && (runner.terminalStatus == null || runner.terminalStatus === "clarification_pending")
    && (!runner.clarificationPending || runner.clarificationPending.clarificationId === blockedClarification.requestId)
      ? blockedClarification : null;
  const hasCurrentTurn = baseHasCurrentTurn || Boolean(currentBlockedClarification);
  const displayedFailure = runner.errorReason ?? (currentBlockedClarification ? {
    reasonCode: "clarification_resume_unavailable",
    diagnostics: currentBlockedClarification.diagnostics,
    recoveryAction: resolveRecoveryAction(undefined, currentBlockedClarification.diagnostics),
  } : null);

  const projectClarificationError = (
    error: unknown,
    generic: string,
    transportPhase: "connect" | "send" | "receive" = "send",
  ) =>
    ruleClarificationErrorPresentation(error, {
      surface: "chat",
      transportPhase,
      copy: {
        generic,
        entitlementDenied: tReasons("entitlement_denied"),
        transport: {
          requestTimedOut: tErrors("transport.requestTimedOut"),
          serviceUnreachable: tErrors("transport.serviceUnreachable"),
          retry: tErrors("transport.retry"),
        },
      },
    });

  // HITL clarification — feed runner.clarificationPending into ClarificationCard.
  // B5 `questions` render as grouped single/multi-select; legacy `candidates`
  // (no questions) synthesize a single single_select question.
  const clarification = useMemo(() => {
    const cp = runner.clarificationPending;
    if (!cp) return null;
    // Tombstone hit = answered (local optimism or clarification.resolved/turn.resumed event)
    // → omit pending card (answeredSummary renders the summary instead). Tombstone is not
    // a reactive source; every write accompanies runner/component state changes (answeredSummary appears in
    // deps), recomputing memo accordingly. (merge: main dingsiyan#11 tombstone mechanism)
    if (isClarificationAnswered(cp.clarificationId)) return null;
    // Batch2 (PRD §3.3b): tool_context_select (Datarc workspace selection) shows options'
    // label and returns value (Datarc workspace ID); remember checkbox + switching hint.
    const isToolContextSelect = cp.kind === "tool_context_select" && (cp.options?.length ?? 0) > 0;
    const questions: ThetaClarificationQuestion[] | null = isToolContextSelect
      ? [{
          question: cp.fallbackText || tr("clarification_fallback_question"),
          options: (cp.options ?? []).map((o) => o.label),
          type: "single_select",
        }]
      : cp.questions?.length
        ? relabelQuestions(cp.questions)
        : cp.candidates?.length
          ? [{
              question: cp.fallbackText || tr("clarification_fallback_question"),
              options: cp.candidates,
              // 2026-09-07: synthesized questions **inherit backend multi_select**. This is where information
              // was lost: card creation with candidates but no questions (CH2 merged N≥2 has this shape)
              // previously hardcoded single_select, turning backend multi-select cards into
              // click-to-submit controls, permitting only one selection.
              // Change only this branch: `cp.questions` provides per-question type and length>1 already avoids
              // single-choice mode; backend tool_context_select always sets multi_select=False. Neither branch is affected.
              type: cp.multiSelect ? "multi_select" : "single_select",
            }]
          : null;
    if (!questions || questions.length === 0) return null;
    const valueByLabel: Record<string, string> | null = isToolContextSelect
      ? Object.fromEntries((cp.options ?? []).map((o) => [o.label, o.value]))
      : buildOptionValueByLabel(cp.questions);
    return {
      questions,
      intro: cp.fallbackText ?? null,
      clarificationId: cp.clarificationId,
      valueByLabel,
      showRemember: isToolContextSelect && Boolean(cp.rememberOption),
      rememberDefault: cp.rememberDefault,
      allowCustomAnswers: cp.kind !== "tool_context_select",
      footHint: isToolContextSelect ? tr("clarification_switch_hint") : null,
      documentDraft: cp.purpose === "document_draft",
    };
  }, [runner.clarificationPending, answeredSummary, tr]);

  // Submit-in-flight flag for the multi-select card spinner. Reset whenever the
  // pending clarification changes (new card) or clears (resume unmounts it).
  const [resolving, setResolving] = useState(false);
  useEffect(() => {
    setResolving(false);
    setClarificationError(null);
    setPausePending(false);
    if (pausePollRef.current !== null) window.clearTimeout(pausePollRef.current);
  }, [runner.clarificationPending?.clarificationId]);
  // A **different new** clarification card discards the old summary (same-ID replay retains it against resurrection).
  useEffect(() => {
    const cid = runner.clarificationPending?.clarificationId;
    if (cid && answeredSummary && cid !== answeredSummary.requestId) {
      setAnsweredSummary(null);
    }
  }, [runner.clarificationPending?.clarificationId, answeredSummary]);
  // New turn starts → summary exits with the old turn.
  useEffect(() => {
    setAnsweredSummary(null);
    setClarificationError(null);
    setBlockedClarification(null);
  }, [runner.turnId]);
  // dingsiyan #11 (review P1-A): clear summary when resume finishes, or `已确认：X` remains floating into
  // the next message (a new form of the reporter's "面板不收起" complaint). Summary lifecycle: click →
  // resume completion. Resume always passes through running=true (set by attachStream), then turn end drops to
  // running=false, a falling edge. Ref recognizes only false **after seeing running=true**, avoiding
  // click-time running=false (picker arrives after turn reaches clarification_pending terminal state).
  const summarySawResumeRunningRef = useRef(false);
  useEffect(() => {
    if (!answeredSummary) {
      summarySawResumeRunningRef.current = false;
      return;
    }
    if (runner.running) {
      summarySawResumeRunningRef.current = true;
    } else if (summarySawResumeRunningRef.current) {
      setAnsweredSummary(null);
    }
  }, [answeredSummary, runner.running]);

  // Network banner status — derived from runner.streamingState. Only
  // surface true disconnects; `paused` is the backend W11-8 stall hint
  // (LLM/tool still working, just no token for a few seconds), and a
  // "正在重连…" warning for that is misleading. Real disconnects still
  // show as "disconnected" → banner. CTO P2 close.
  // #9 (dingsiyan 2026-07-07): give-up (running=false) and active reconnect
  // (running=true) both set streamingState="error". Map the terminal give-up to a
  // distinct "failed" banner ("连接失败，刷新") instead of a perpetual "重连中…".
  const networkStatus = runner.streamingState === "error"
    ? (runner.running ? "disconnected" : "failed")
    : null;

  // Standalone picker = clarification exists but the current turn block is
  // otherwise empty (typical of "follow-up clarification on a completed turn"
  // backend pattern — done(completed) + clarification_required emitted
  // together). Render the picker right below the archived turns block so it
  // visually attaches to the prior turn instead of appearing in a phantom
  // current-turn row.
  const standalonePicker = !hasCurrentTurn && Boolean(clarification);

  // Turn-level error — backend done(status=error) or interrupted/aborted.

  // A normally completed turn may collapse its execution disclosure after a
  // short grace. Until then it stays at the original step beat so an active
  // reader is never surprised by a mid-read relocation.
  const turnComplete = Boolean(
    hasCurrentTurn &&
      !runner.running &&
      !runner.errorReason &&
      !runner.cancelled &&
      !runner.aborted &&
      (runner.terminalStatus != null
        ? runner.terminalStatus === "completed"
        : turnStoreState?.cardFinalized &&
          turnStoreState.cardFinalState === "completed"),
  );
  // Appearance refresh: persistently show the latest answer's action bar. Current if completed, otherwise latest normally completed archived turn
  // (t1 completed, t2 failed, t3 running ⇒ t1). Archive completion uses the same criterion as ArchivedTurnView.
  const latestArchivedIdx = latestAnsweredArchivedIndex(archivedTurns, turnComplete);
  const [stepsFolded, setStepsFolded] = useState(false);
  // chat-ux-2026-08 · 1e band: user **explicitly requested details** this turn (band click / disclosure-region
  // pointerdown / scroll / focus). Once requested, the band never takes over again this turn:
  // withdrawing details would take control away from the user.
  //
  // ⚠ Reset only on `runner.turnId`, **deliberately not on `running`**. HITL clarification resume
  // reuses turn_id; user decision: those reading trace during clarification must not have their content
  // replaced by the band on resume (inventory §4 ③). Resetting on running would violate this.
  /** **Expansion origin**, recorded only on the first absent→present transition and never rewritten afterward.
   *  · `"band"`: click `展开运行过程` → expand in the band slot without moving the viewport.
   *  · `"inline"`: explicit inspection of inline collapsed strip / step list → expand at its original position.
   *
   *  ⚠ **Distinguish origin**: users read trace **inline** during HITL clarification; moving it to the
   *  bottom after resume relocates content under their eyes (prohibited in inventory §2.9).
   *  `TurnContainer.test.tsx` guards this resume case and failed once during implementation.
   *  ⚠ **Record only the first**: rewriting on every inspection lets a tap on **already in-place expanded**
   *  trace change origin to inline, instantly moving content from the slot back to the beat position,
   *  relocating it mid-read, the same problem this fix addresses. */
  const [traceRequestOrigin, setTraceRequestOrigin] =
    useState<null | "band" | "inline">(null);
  const traceRequested = traceRequestOrigin !== null;
  const [disclosureBeforeAnswer, setDisclosureBeforeAnswer] = useState(false);
  const lastInteractionAtRef = useRef<number>(0);
  const explicitlyInspectedDisclosureRef = useRef(false);
  // done(completed) remounts the current turn as ArchivedTurnView in the same
  // update. Keep explicit reader intent by stable turn_id across that renderer
  // boundary; a full page/session remount intentionally resets it.
  const explicitDisclosurePlacementByTurnRef = useRef<
    Map<string, ExplicitDisclosurePlacement>
  >(new Map());

  // Reset disclosure intent when a different turn starts.
  useEffect(() => {
    setStepsFolded(false);
    setTraceRequestOrigin(null);
    setDisclosureBeforeAnswer(false);
    lastInteractionAtRef.current = 0;
    explicitlyInspectedDisclosureRef.current = false;
  }, [runner.turnId]);
  // HITL resumes the same turn_id, so the turn-id reset above does not fire.
  // turn.resumed reopens the canonical card; once execution is live again,
  // reveal the in-place trace instead of retaining the prior terminal fold.
  useEffect(() => {
    if (runner.running && turnStoreState && !turnStoreState.cardFinalized) {
      setStepsFolded(false);
      setDisclosureBeforeAnswer(false);
      lastInteractionAtRef.current = 0;
      explicitlyInspectedDisclosureRef.current = false;
      if (runner.turnId) {
        explicitDisclosurePlacementByTurnRef.current.delete(runner.turnId);
      }
    }
  }, [runner.running, runner.turnId, turnStoreState?.cardFinalized]);

  // p0-followup F3 (audit P1-4 / R13) — interaction guard for the
  // auto-collapse. Passive attention (hover / pointer movement) defers the
  // fold until five quiet seconds; explicit inspection (pointer down, focus
  // or an actual disclosure scroll) keeps this completed turn expanded at its
  // original beat. A wheel over non-scrollable trace content is only passive:
  // it commonly means the user is scrolling the page.
  //
  // Refs avoid state writes on every pointer movement. The collapse timer is
  // the only reader and runs after the relevant interaction events.
  const markInteraction = (): void => {
    lastInteractionAtRef.current = Date.now();
  };
  const markExplicitInspection = (): void => {
    markInteraction();
    explicitlyInspectedDisclosureRef.current = true;
    if (runner.turnId) {
      explicitDisclosurePlacementByTurnRef.current.set(
        runner.turnId,
        disclosureBeforeAnswer
          ? "before-answer-open"
          : "inline-open",
      );
    }
  };
  const eventTargetsDisclosure = (
    event: React.SyntheticEvent<HTMLElement>,
  ): boolean =>
    event.target instanceof Element &&
    Boolean(
      event.target.closest(
        // chat-ux-2026-08 · `.bh-band` is also disclosure, the **only** running-state disclosure surface;
        // excluding it invalidates region-level passive/explicit interaction guards throughout running state
        // (hover no longer postpones auto-collapse, hiding content users are about to open 600ms after turn completion).
        // ⚠ Predicate uses `closest()` selectors, so `.bh-band` must actually be the band root's class;
        //   data-testid alone does not suffice.
        '.bh-step-list, [data-testid="step-summary"], .bh-band',
      ),
    );
  const markPassiveDisclosureInteraction = (
    event: React.SyntheticEvent<HTMLElement>,
  ): void => {
    if (eventTargetsDisclosure(event)) markInteraction();
  };
  const markExplicitDisclosureInspection = (
    event: React.SyntheticEvent<HTMLElement>,
  ): void => {
    if (!eventTargetsDisclosure(event)) return;
    markExplicitInspection();
    // Yielding the band for user-requested details recognizes **only explicit inspection of real trace**, not the band's own
    // pointerdown / focus (final review):
    //   · Tab to band's disclosure button → focus → immediate unmount, keyboard focus lost to <body>;
    //   · Start swiping on mobile band (touch pointerdown) → entire running surface replaced.
    // Band yielding has its own entry: disclosure strip onClick.
    // ⚠ Keep `.bh-band` in `eventTargetsDisclosure`: that guard postpones auto-collapse;
    //   the band is the only running disclosure surface, so excluding it stops all running-state interaction from counting as reading.
    if (
      event.target instanceof Element
      && event.target.closest('.bh-step-list, [data-testid="step-summary"]')
    ) {
      setTraceRequestOrigin((o) => o ?? "inline");
    }
  };
  // 5000ms quiet window between collapse retries — anything below that
  // and the user is probably mid-read, so defer.
  const COLLAPSE_QUIET_MS = 5000;
  // 600ms initial grace once the turn completes.
  const COLLAPSE_INITIAL_GRACE_MS = 600;
  // 1000ms re-check cadence after the initial grace, while interaction is
  // hot. Short enough that the fold lands within ~5s once user stops
  // interacting; doesn't burn React renders since the ref is stable.
  const COLLAPSE_RECHECK_MS = 1000;
  // Chat-only turns have nothing to fold. A peer can, however, complete with
  // no child tool steps (direct LLM result / early terminal), so canonical
  // peer facts count as real disclosure even when there is no step beat.
  const hasAnyLiveStepBeat = (turnStoreState?.beats ?? []).some(
    (b) => b.kind === "step",
  );
  const hasAnyLiveDisclosureFact =
    hasAnyLiveStepBeat || hasPeerDisclosureFacts(turnStoreState);
  const liveApprovalBlocksFold = hasBlockingApprovalState(turnStoreState);
  // Only the replayable completed finalization is a normal completion.
  // Network give-up, cancellation, failure and HITL pause keep the trace
  // expanded at its execution beat.
  const normalLiveCompletion = Boolean(
    hasCurrentTurn &&
      turnStoreState?.cardFinalized &&
      turnStoreState.cardFinalState === "completed" &&
      turnComplete &&
      !runner.clarificationPending &&
      !liveApprovalBlocksFold &&
      hasAnyLiveDisclosureFact,
  );
  const placeLiveDisclosureBeforeAnswer = Boolean(
    normalLiveCompletion && disclosureBeforeAnswer,
  );
  useEffect(() => {
    if (!normalLiveCompletion) return;
    let timer: number | null = null;
    const trySchedule = (delay: number): void => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (explicitlyInspectedDisclosureRef.current) {
          timer = null;
          return;
        }
        const sinceInteraction = Date.now() - lastInteractionAtRef.current;
        if (sinceInteraction > COLLAPSE_QUIET_MS) {
          setStepsFolded(true);
          setDisclosureBeforeAnswer(true);
          timer = null;
        } else {
          // Interaction still hot — re-check soon. The re-check delay is
          // the remaining quiet window plus a small cushion so we don't
          // immediately retry only to fail again.
          const remain = Math.max(
            COLLAPSE_RECHECK_MS,
            COLLAPSE_QUIET_MS - sinceInteraction + 100,
          );
          trySchedule(remain);
        }
      }, delay);
    };
    trySchedule(COLLAPSE_INITIAL_GRACE_MS);
    return () => {
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [normalLiveCompletion]);

  // The failure card announces itself only when the failure arrived on this page's live stream: a failure
  // restored by hydration or replay (F5, reopening the session) is already settled and must stay quiet.
  const [liveRunTurnId, setLiveRunTurnId] = useState<string | null>(null);
  useEffect(() => {
    if (runner.running && runner.turnId) setLiveRunTurnId(runner.turnId);
  }, [runner.running, runner.turnId]);
  const failureArrivedLive = Boolean(runner.turnId && liveRunTurnId === runner.turnId);

  // A turn that ended without an answer folds its trace into one "Not finished · 3 / 5 steps" line (spec §8):
  // the failure card above says what went wrong; the expanded trace stays one click away.
  const liveFailureFold = Boolean(
    hasCurrentTurn &&
      runner.errorReason &&
      !runner.running &&
      !runner.clarificationPending &&
      !liveApprovalBlocksFold &&
      hasAnyLiveDisclosureFact,
  );
  useEffect(() => {
    if (!liveFailureFold || explicitlyInspectedDisclosureRef.current) return;
    setStepsFolded(true);
  }, [liveFailureFold]);

  // An approval failure may arrive after the turn initially completed. Never
  // leave that now-abnormal action hidden behind a stale normal-completion fold.
  useEffect(() => {
    if (!liveApprovalBlocksFold) return;
    setStepsFolded(false);
    setDisclosureBeforeAnswer(false);
  }, [liveApprovalBlocksFold]);

  // ── chat-ux-2026-08 · 1e band takes over running-state disclosure ────────────────────────────
  //
  // **Positive predicate**, rather than copying another component's suppression conditions, the main defect in second review:
  // copying `showWorking`'s `!fallbackNarrative`, which on the main path concatenates this turn's
  // **all narrative beats**, preventing the band once Lead speaks and disabling the whole feature
  // in real turns (all new tests stayed green because none supplied narrative beats).
  // Band and prose should coexist: design places prose above the band.
  //
  // Only five classes of interrupted normal progress / expressed user preference prevent takeover:
  //   · No longer running: band describes ongoing work only
  //   · Disclosure already frontloaded / collapsed: retain existing terminal layout
  //   · User explicitly requested details: inventory §4 ③
  //   · Awaiting user clarification: picker + full trace readable together; band would compete
  //   · Turn already errored: FinalFailBanner takes over (running already false)
  //
  // ⚠ Two layers (`bandSlotActive` / `bandMode`), rather than one flag:
  // after clicking `展开运行过程`, **the slot still belongs to running-state disclosure**,
  // merely switching band to full trace. Previously expansion rendered trace at the beat's original position,
  // while the band sat at the turn block's **bottom**; on 116, ~280px apart, with expansion above
  // the viewport. User complaint: `点击展开跳到顶部了`. In-place expansion structurally removes this jump:
  // content appears where the user tapped.
  const bandSlotActive = Boolean(
    runner.running
      && !placeLiveDisclosureBeforeAnswer
      && !stepsFolded
      && !clarification
      && !runner.errorReason,
  );
  /** Slot shows the collapsed band. */
  const bandMode = bandSlotActive && !traceRequested;
  /** Slot shows full trace expanded in place (user clicked `展开运行过程`). */
  const bandExpandedInPlace = bandSlotActive && traceRequestOrigin === "band";
  /** Whether the slot owns all running disclosure (collapsed band or in-place expanded trace).
   *  Inline expansion keeps the original path; no slot takeover, with BeatStream rendering at the beat position. */
  const bandSlotOwnsDisclosure = bandMode || bandExpandedInPlace;
  // The band replaces the **execution process**, so its position in conversation flow must match that process in replay:
  // `TurnBeatFlow` renders full trace at the **first step beat**, with subsequent prose/answers below.
  // Only one JSX band location is allowed (after BeatStream, see below); instead move prose/answers **after**
  // the first step below the band, or live order is answer → band while replay is execution process →
  // answer, yielding two display orders for the same event set.
  // Pre-dispatch prose remains above the band (the segment meant by design 1e's prose-above-band rule).
  const liveBeatSplit = splitBeatsAroundBand(
    turnStoreState?.beats ?? [],
    bandSlotOwnsDisclosure,
  );

  // Time stamp for BotByline (HH:mm in zh-CN). Derived from runner.startedAt
  // (set in use-turn-runner at submit time). Memoize so we don't re-format
  // every render — the stamp is stable for the turn's lifetime.
  const turnTimeText = useMemo(() => formatTurnTime(runner.startedAt), [runner.startedAt]);
  // User bubble submission time: clarification resume changes `runner.startedAt` to the new generation's start,
  // so prefer turn-store's first turn_started (same source as archive/refresh).
  const turnStoreStartedAt = turnStoreState?.startedAt ?? null;
  const userTimeText = useMemo(
    () => formatTurnTime(resolveUserMessageTimeMs({
      firstTurnStartedAt: turnStoreStartedAt,
      submittedAtMs: runner.startedAt,
    })),
    [turnStoreStartedAt, runner.startedAt],
  );
  /**
   * Timing-anchor key. **Include `executionGeneration`**: same-turn clarification resume resets
   * `startedAt` on the same turnId (`use-turn-runner.ts:1229`: exclude paused generation duration);
   * turnId alone would count the minutes spent answering clarification in the new generation.
   */
  const elapsedAnchorKey = elapsedAnchorKeyOf(runner.turnId, runner.executionGeneration);

  // userMsgId for the live user message — Phase 1.7-A stable id.
  const userMsgId = runner.userMsgId ?? null;

  // memory-revive S1: inline chat memory-candidate cards (REST-driven, no SSE/beat dependency).
  // Only show for the latest completed turn: live (observe running → done during this mount, then short-poll
  // distillation output), or archived (one fetch restores on F5 mount). Older pending candidates go to
  // bell notifications / memory page. Hook deduplicates per turnId, without interrupting pending clarification.
  const lastArchivedTurnId =
    archivedTurns.length > 0 ? (archivedTurns[archivedTurns.length - 1].turnId ?? null) : null;
  const latestDoneTurnId = !runner.running ? (runner.turnId ?? lastArchivedTurnId) : null;
  // scheduled-recipe-runs S2: count completed turns before this one (condition for `固化整段对话` chip).
  // Archived snapshots lack reliable steps (traceSteps retired; turn-store is authoritative, query by turnId),
  // so approximate with completed historical turns: overcounting one pure-chat turn only shows the chip slightly early (backend digest
  // accepts only tool-call turns, excluding pure chat); undercounting genuinely loses the entry point.
  const priorAnalysisTurnCount = archivedTurns.filter(
    (a) => a.turnId && (a.terminalStatus ?? "completed") === "completed",
  ).length;
  /**
   * chat-ux-2026-08 batch F: **total completed turns** in this conversation.
   *
   * `priorAnalysisTurnCount` counts only **archived turns**, excluding the completed current turn
   * not yet archived. `固化整段对话` requires more than one turn as stated above;
   * using archive-only count as the threshold misclassifies:
   *   - 1 archived + 1 current completed = two turns, counted as 1 (existing test shape)
   *   - 0 archived + 1 current completed = one turn, but `priorAnalysisTurnCount > 0` is false…
   *     Yet old code used it, showing the chip for a single-turn conversation on 116 **after**
   *     archiving that turn, beside its own `固化此流程`, precisely the shape to fix.
   * Include current turn and require ≥2.
   */
  const completedTurnCount = priorAnalysisTurnCount + (turnComplete ? 1 : 0);
  const sawRunningRef = useRef(false);
  useEffect(() => {
    if (runner.running) sawRunningRef.current = true;
  }, [runner.running]);
  const memCandMode: MemoryCandidateMode =
    !latestDoneTurnId || runner.clarificationPending
      ? "off"
      : sawRunningRef.current
        ? "live"
        : "archived";
  const memCands = useMemoryCandidates(latestDoneTurnId, memCandMode);
  // Suggestion-result receipts (batch 5). Unlike memory candidates, **not bound to this turn**: decisions happened days ago on another
  // timeline; chat merely delivers results on a surface users actually read. `latestDoneTurnId` is only
  // a refresh signal, not their origin.
  const receipts = useFeedbackReceipts(!runner.running, latestDoneTurnId);

  /** Poll until a document task's pause has settled (the list stops answering 409), then unlock the card. */
  const waitForPauseToSettle = (turnId: string, attempt = 0) => {
    if (pausePollRef.current !== null) window.clearTimeout(pausePollRef.current);
    pausePollRef.current = window.setTimeout(() => {
      void listClarificationsForTurn(turnId)
        .then(() => setPausePending(false))
        .catch((err: unknown) => {
          if (
            err instanceof ApiError
            && err.status === 409
            && err.code === "clarification_pause_pending"
            && attempt < PAUSE_POLL_MAX_ATTEMPTS
          ) {
            waitForPauseToSettle(turnId, attempt + 1);
            return;
          }
          setPausePending(false);
        });
    }, PAUSE_POLL_INTERVAL_MS);
  };

  const resolveWith = (
    payload: ResolveClarificationPayload,
    fallbackText: string,
    attempted?: ClarificationAnswer[],
  ) => {
    const clarificationId = runner.clarificationPending?.clarificationId ?? null;
    if (!clarificationId) {
      onSend?.(fallbackText);
      return;
    }
    setRestoredAnswers(attempted ? { requestId: clarificationId, answers: attempted } : null);
    const optimisticValues = payload.chosen_value
      ? [payload.chosen_value]
      : (payload.chosen_values ?? []);
    const connectorChoice = runner.clarificationPending?.kind === "tool_context_select" && !payload.skip;
    const displayValues = connectorChoice ? [fallbackText] : optimisticValues;
    setResolving(true);
    setClarificationError(null);
    // dingsiyan #11: optimistic answered state combines tombstone (survives attachStream, prevents replayed
    // clarification_required from reviving cards) + summary row (immediate click feedback).
    markClarificationAnswered(clarificationId, {
      values: displayValues,
      ...(connectorChoice ? { canonicalValues: optimisticValues } : {}),
      skipped: Boolean(payload.skip),
    });
    setAnsweredSummary({
      requestId: clarificationId,
      values: displayValues,
      skipped: Boolean(payload.skip),
    });
    void (async () => {
      // review FE-P2-2: separate resolve / resume requests. Resolve failure rolls back optimism, restoring the card
      // for retries. Resume failure **does not restore it** (server already resolved; another click gets 409,
      // misclassified as "已过期", root-cause report D4 blind spot). Retain answered tombstone; suggest refresh to continue.
      let resolved: Awaited<ReturnType<typeof resolveClarification>>;
      try {
        resolved = await resolveClarification(clarificationId, payload);
      } catch (err) {
        setResolving(false);
        if (err instanceof ApiError && err.code === "clarification_resume_unavailable"
          && err.diagnostics?.retryable === false && runner.turnId) {
          // Resolve can store the answer before continuation fails. Keep its
          // tombstone and check read-only authority instead of resubmitting it.
          const turnId = runner.turnId;
          const generation = runner.executionGeneration ?? null;
          const isCurrent = () => {
            const current = clarificationRunnerRef.current;
            return clarificationMounted.current && current.turnId === turnId
              && (current.executionGeneration ?? null) === generation
              && !current.running
              && (current.terminalStatus == null || current.terminalStatus === "clarification_pending")
              && (!current.clarificationPending || current.clarificationPending.clarificationId === clarificationId);
          };
          if (!isCurrent()) return;
          setAnsweredSummary(null);
          setBlockedClarification({
            turnId, generation, requestId: clarificationId,
            diagnostics: projectClarificationError(err, tClar("submit_failed")).diagnostics,
            answerConfirmed: false,
          });
          try {
            const authority = await listClarificationsForTurn(turnId);
            const failure = clarificationResumeFailure(authority.resume_failure, turnId, generation);
            if (isCurrent() && authority.resume_failure?.request_id === clarificationId
              && !authority.items.some((item) => item.request_id === clarificationId) && failure) {
              setBlockedClarification((current) => current?.requestId === clarificationId
                && current.turnId === turnId && current.generation === generation
                  ? { ...current, answerConfirmed: true } : current);
            }
          } catch {
            // An unavailable read establishes neither a saved nor an unsaved answer.
          }
          return;
        }
        if (
          err instanceof ApiError
          && err.status === 409
          && err.code === "clarification_not_resolvable"
        ) {
          // The server-side invalidation won the pending CAS.  Keep a
          // dismissed tombstone, remove the stale card, and avoid presenting
          // technical diagnostics or a retry that can never succeed.
          markClarificationAnswered(clarificationId, {
            values: [],
            dismissed: true,
          });
          runner.clearClarificationPending?.(clarificationId);
          // Settled in place as an "expired" row with "Ask again" (no toast; Chat Cards Spec §1).
          setAnsweredSummary({ requestId: clarificationId, values: [], expired: true });
          setClarificationError(null);
          return;
        }
        if (
          err instanceof ApiError
          && err.status === 409
          && err.code === "clarification_pause_pending"
          && runner.turnId
        ) {
          // The document task is still saving its pause: keep the card and the choice, lock it with the
          // reason, and unlock it by itself once the pause settles (no refresh needed).
          unmarkClarificationAnswered(clarificationId);
          setAnsweredSummary(null);
          setPausePending(true);
          waitForPauseToSettle(runner.turnId);
          return;
        }
        if (
          err instanceof ApiError
          && err.status === 404
          && err.code === "clarification_not_found"
        ) {
          // Backend has no such row: persistence failed and emitted the card with a local ID (2026-09-04 HITL
          // audit §5; creation fixed, but existing cards and old versions remain). Retrying always returns 404, so
          // dismiss without reviving and ask users to type the answer directly.
          markClarificationAnswered(clarificationId, {
            values: [],
            dismissed: true,
          });
          runner.clearClarificationPending?.(clarificationId);
          setAnsweredSummary(null);
          setClarificationError(null);
          toast({ kind: "info", text: tClar("unanswerable") });
          return;
        }
        unmarkClarificationAnswered(clarificationId);
        setAnsweredSummary(null);
        // The error is written inside the restored card, which keeps the user's choice (no toast).
        setClarificationError(projectClarificationError(
          err,
          tClar("submit_failed"),
        ));
        return;
      }
      // Resolve succeeded
      if (resolved.action === "plan_reapprove" && resolved.reapprove_plan_id) {
        // planning-mode-audit WP-2: plan-preflight picker automatically reapproves the plan after Datarc workspace selection
        // (same path as plan-card `重试`); neither resume nor new turn (noop would call onSend(fallbackText)
        // and invent an ordinary conversation turn).
        setResolving(false);
        void onApprovePlan(resolved.reapprove_plan_id);
        return;
      }
      if (resolved.action === "plan_midrun_ack") {
        // graph-mode batch2 (review P1-6): answering during execution only acknowledges; driver picks it up at the round boundary,
        // without subsequent requests. Without this branch, noop calls onSend(fallbackText)
        // and starts a spurious ordinary turn using picker hint text as user input.
        setResolving(false);
        return;
      }
      if (resolved.action === "turn_settled") {
        setResolving(false);
        runner.clearClarificationPending?.(clarificationId);
        if (resolved.stream_url) {
          runner.attachStream?.(resolved.stream_url, resolved.execution_generation ?? undefined);
        }
        return;
      }
      if (resolved.action !== "turn_resume") {
        if (onSend) onSend(fallbackText);  // Noop path: start a new turn
        return;
      }
      try {
        if (resolved.stream_url) {
          if (resolved.execution_generation == null) {
            runner.attachStream?.(resolved.stream_url);
          } else {
            runner.attachStream?.(
              resolved.stream_url,
              resolved.execution_generation,
            );
          }
          return;
        }
        // Backward compatibility for servers predating atomic resolve+resume.
        const resume = await resumeTurnAfterClarification(clarificationId);
        if (resume.execution_generation == null) {
          runner.attachStream?.(resume.stream_url);
        } else {
          runner.attachStream?.(resume.stream_url, resume.execution_generation);
        }
        // leave resolving=true; resume clears clarificationPending → effect resets
      } catch (err) {
        // Resume failed after resolve; do not revive the card (409). The answered row turns into
        // "Your choice was saved, but the turn couldn't continue" with Refresh.
        setResolving(false);
        setAnsweredSummary((prev) => (prev ? { ...prev, resumeFailed: true } : prev));
      }
    })();
  };

  // One question: "Continue" submits the selected (or typed) answer (D2).
  // Batch2: tool_context_select maps label→value (Datarc workspace ID), forwarding both remember states.
  const handleResolveClarification: ClarificationCardProps["onSubmitSingle"] = (answer, opts) => {
    const answers = canonicalizeAnswers([answer], clarification?.valueByLabel);
    const pending = runner.clarificationPending;
    // Connector and legacy token selections still consume a scalar identifier.
    // Ordinary question cards retain the same typed contract as grouped answers.
    const isQuestion = pending?.kind !== "tool_context_select"
      && (pending?.kind === "ambiguous_turn" || Boolean(pending?.questions?.length));
    resolveWith(
      {
        chosen_value: projectAnswerValues(answers, { multiQuestion: false })[0],
        ...(isQuestion ? { answers } : {}),
        ...(opts?.remember !== undefined ? { remember: opts.remember } : {}),
      },
      projectAnswerValues([answer], { multiQuestion: false })[0],
      [answer],
    );
  };
  // multi_select / multi-question: submit canonical answers plus legacy projection.
  // Cards submit **the rendered string**; renamed `option_labels` are display names and must map back to
  // canonical values (Datarc accepts only canonical names). Single-select does the same in `handleResolveClarification`;
  // both share `valueByLabel`. Fallback hint text still uses display names, intended for users
  // (noop sends it as a user message).
  const handleResolveClarificationMulti = ({ values, answers }: ClarificationMultiSubmit) => {
    const fallbackText = clarificationMultiFallbackText(
      { values, answers },
      tr("clarification_skip_fallback"),
    );
    const canonicalAnswers = canonicalizeAnswers(
      answers,
      clarification?.valueByLabel,
    );
    const canonicalValues =
      canonicalAnswers === answers
        ? values
        : projectAnswerValues(canonicalAnswers, {
            multiQuestion: (clarification?.questions.length ?? 0) > 1,
          });
    resolveWith(
      { chosen_values: canonicalValues, answers: canonicalAnswers },
      fallbackText,
      answers,
    );
  };
  const handleSkipClarification = () =>
    resolveWith({ skip: true }, tr("clarification_skip_fallback"));

  const clarificationSlot = clarification ? (
    <ClarificationCard
      key={clarification.clarificationId}
      questions={clarification.questions}
      intro={clarification.intro}
      busy={resolving}
      showRemember={clarification.showRemember}
      rememberDefault={clarification.rememberDefault}
      allowCustomAnswers={clarification.allowCustomAnswers}
      footHint={clarification.footHint}
      documentDraft={clarification.documentDraft}
      restoredAnswers={
        restoredAnswers?.requestId === clarification.clarificationId ? restoredAnswers.answers : null
      }
      error={clarificationError?.message ?? null}
      errorDiagnostics={clarificationError?.diagnostics}
      unavailableReason={pausePending ? tClar("pause_pending") : null}
      onSubmitSingle={handleResolveClarification}
      onSubmitMulti={handleResolveClarificationMulti}
      onSkip={handleSkipClarification}
    />
  ) : answeredSummary ? (
    <ClarificationAnsweredSummary
      values={answeredSummary.values}
      skipped={answeredSummary.skipped}
      resuming={runner.running}
      expired={answeredSummary.expired}
      onAskAgain={
        onComposerPrefill && runner.user ? () => onComposerPrefill(runner.user ?? null) : undefined
      }
      resumeFailed={answeredSummary.resumeFailed}
      onRefresh={() => window.location.reload()}
    />
  ) : null;

  // Planning Mode — approve a proposed plan through the one protocol-selected
  // chat writer, then attach its stream. A failed mutation is never retried
  // through the separate dev writer.
  // Resolves true only when the approval was admitted and its stream attached.
  const onApprovePlan = async (planId: string): Promise<boolean> => {
    // A pending save still displays optimistic steps with the preceding revision.
    // Require a fresh click after its outcome; never approve those older steps.
    if (_planPersistChain.has(planId)) {
      toast({ kind: "error", text: tpc("plan_save_pending") });
      return false;
    }
    const approvedRevision = getPlanSnapshot(planId)?.currentRev;
    // 2026-07: prefer plan-carried Space identity (hook1 persisted truth, included by plan_proposed);
    // localStorage is only a legacy-event fallback. Divergence previously caused approve/cancel 404s.
    const _planWsId = getPlanSnapshot(planId)?.workspaceId;
    const wsId = _planWsId !== undefined
      ? _planWsId
      : getActiveSpaceId();
    if (_planWsId === undefined && !wsId) {
      // Legacy event lacks plan Space and localStorage is empty → cannot locate tenant; do not send blindly.
      toast({ kind: "error", text: tpc("foot_approve_failed") });
      return false;
    }
    // Batch3 (review R2-F2): failure must restore **original status**; forcing "proposed" previously turned
    // BLOCKED cards into editable proposed variants (409 worker-tail window is a designed main path).
    const priorStatus = getPlanSnapshot(planId)?.status ?? "proposed";
    setPlanLocalStatus(planId, "running"); // immediate feedback → running variant
    // Production path (track D): chat/send with plan_execution_id → server derives
    // identity + authz + credit + spawns the execution turn + returns stream_url →
    // attach for LIVE step_*/plan_*/done (real running animation, no polling).
    try {
      const res = await approvePlanViaSend(planId, {
        workspaceId: wsId,
        sessionId: runner.sessionId,
        expectedRev: approvedRevision,
      });
      if (res.stream_url) {
        runner.attachStream?.(res.stream_url);
        return true;
      }
    } catch (err) {
      const approvalError = err as Error & { code?: string; status?: number };
      const planStateChanged = approvalError.status === 409
        && (
          approvalError.code === "plan_already_running"
          || approvalError.code === "plan_not_approvable"
        );
      const definitivePreAdmission = isDefinitiveComposerChatActionRejection(
        approvalError,
      );
      if (approvalError.code === "plan_not_found") {
        dismissPlan(planId);
      } else {
        try {
          const state = await fetchPlanStateAuthed(planId, {
            workspaceId: wsId,
          });
          // This GET began after the approval error, so an SSE terminal update
          // may already be newer. Only reconcile the optimistic state we own.
          if (getPlanSnapshot(planId)?.status === "running") {
            const remoteStatus = String(state.plan?.status ?? "");
            if (remoteStatus === "cancelled") {
              dismissPlan(planId);
            } else if (
              remoteStatus === "completed"
              || remoteStatus === "blocked"
              || remoteStatus === "failed"
              || remoteStatus === "awaiting_amend"
              || (approvalError.code === "plan_not_approvable" && remoteStatus === "proposed" && state.plan.retryable === true)
              || (definitivePreAdmission && !planStateChanged)
            ) {
              applyPlanSnapshot({
                plan_id: planId,
                plan: state.plan,
                steps: state.steps,
              });
            }
          }
        } catch {
          // Never let a late failed read overwrite a terminal SSE snapshot. Plan
          // state 409s and ambiguous mutations stay non-editable so the bounded
          // card reconciliation can observe their authoritative outcome.
          if (getPlanSnapshot(planId)?.status === "running") {
            const reconcile = planStateChanged
              || !definitivePreAdmission;
            setPlanLocalStatus(planId, reconcile ? "running" : priorStatus);
          }
        }
      }
      if (
        approvalError.status === 409
        && (
          approvalError.code === "plan_already_running"
          || approvalError.code === "plan_not_approvable"
        )
      ) {
        // Worker tail window (report still assembling) or concurrent approval cannot be solved by dev fallback.
        toast({ kind: "error", text: approvalError.code === "plan_not_approvable"
          ? getChatSendErrorMessage("plan_not_approvable")
          : tpc("plan_busy_retry") });
        return false;
      }
      toast({ kind: "error", text: tpc("foot_approve_failed") });
      return false;
    }
    return false;
  };

  // Plan cards only need completion, not the outcome.
  const approvePlanFromCard = async (planId: string): Promise<void> => {
    await onApprovePlan(planId);
  };

  // plan_blocked failure card — resume the blocked plan through the plan-card approval flow, but only after
  // the server confirms the plan is still blocked at the revision this page shows and still readable from the
  // current Space. `false` tells the card to fall back to Edit and send.
  const resumeBlockedPlan = async (planId: string): Promise<boolean> => {
    const local = getPlanSnapshot(planId);
    const workspaceId = local?.workspaceId;
    if (
      !local
      || local.status !== "blocked"
      || local.currentRev === undefined
      || !workspaceId
      || workspaceId !== getActiveSpaceId()
    ) return false;
    let remote: Awaited<ReturnType<typeof fetchPlanStateAuthed>>;
    try {
      remote = await fetchPlanStateAuthed(planId, { workspaceId });
    } catch {
      return false;
    }
    const current = getPlanSnapshot(planId);
    if (
      String(remote.plan?.status ?? "") !== "blocked"
      || remote.plan?.current_rev !== local.currentRev
      || current?.status !== "blocked"
      || current.currentRev !== local.currentRev
      || current.workspaceId !== workspaceId
      || workspaceId !== getActiveSpaceId()
    ) return false;
    // A 409 (plan_not_approvable / plan_already_running) or any other refused approval keeps the card's fallback.
    return onApprovePlan(planId);
  };
  const liveBlockedPlanResume = useBlockedPlanResume(
    runner.errorReason?.reasonCode === "plan_blocked" ? turnStoreState?.blockedPlanId : undefined,
    resumeBlockedPlan,
  );

  // Planning Mode — "view report": load the session's canvas (the plan's report
  // landed there on completion) and reveal the canvas pane.
  const onViewReportPlan = () => {
    const sid = runner.sessionId;
    if (!sid) return;
    void (async () => {
      try {
        const [{ fetchCanvasBySession }, { hydrateCanvas }] = await Promise.all([
          import("@/lib/api"),
          import("@/lib/canvas-store"),
        ]);
        const dto = await fetchCanvasBySession(sid);
        hydrateCanvas({
          canvas: dto.canvas
            ? { id: dto.canvas.id, sessionId: dto.canvas.sessionId, title: dto.canvas.title ?? null }
            : null,
          sections: dto.sections ?? [],
          sourceRefs: dto.sourceRefs,
          turnCitations: dto.turnCitations,
        });
      } catch (err) {
        toast({ kind: "error", text: tpc("action_errors.view_report_failed"), action: copyDiagnosticsAction(err, "chat") });
      }
    })();
  };

  // A (editable): dev REST needs IDs; prefer plan-carried Space, user from /auth/me.
  const resolvePlanIds = async (
    planId?: string,
  ): Promise<{ workspaceId: string; userId: string } | null> => {
    const _planWsId = planId ? getPlanSnapshot(planId)?.workspaceId : undefined;
    const wsId = _planWsId !== undefined
      ? _planWsId
      : getActiveSpaceId();
    if (_planWsId === undefined && !wsId) return null;
    try {
      return { workspaceId: wsId, userId: (await fetchMe()).id };
    } catch {
      return null;
    }
  };

  // A: persist current locally optimistic steps → PATCH /steps → apply backend-normalized results.
  // Serialize (P1-1): chain after previous write, reading latest snapshot only at send time; applyPlanSnapshot only if this remains
  // chain tail (no newer edits queued), otherwise skip to avoid clobbering newer local edits with old backend results.
  const onPersistEditsPlan = (planId: string): Promise<void> => {
    const prev = _planPersistChain.get(planId) ?? Promise.resolve();
    const next: Promise<void> = prev.catch(() => {}).then(async () => {
      const ids = await resolvePlanIds(planId);
      const snap = getPlanSnapshot(planId);
      if (!snap) return;
      if (!ids) {
        invalidatePlanRevision(planId, snap.currentRev);
        return;
      }
      const isTail = () => _planPersistChain.get(planId) === next;
      try {
        const res = await patchPlanSteps(planId, snap.steps, { ...ids, expectedRev: snap.currentRev });
        if (snap.currentRev !== undefined) acknowledgePlanEdit(planId, snap.currentRev, res.current_rev);
        if (isTail()) {
          applyPlanSnapshot({ plan_id: planId, plan: { id: planId, status: res.status, current_rev: res.current_rev }, steps: res.steps });
        }
      } catch (err) {
        invalidatePlanRevision(planId, snap.currentRev);
        toast({ kind: "error", text: tpc(`action_errors.${planActionErrorKey(err, "edit")}`), action: copyDiagnosticsAction(err, "chat") });
        if (isTail()) {
          try {
            const st = await fetchPlanStateAuthed(planId, { workspaceId: ids.workspaceId }); // Rollback: refetch backend truth
            applyPlanSnapshot({ plan_id: planId, plan: st.plan, steps: st.steps });
          } catch {
            /* ignore */
          }
        }
      }
    });
    _planPersistChain.set(planId, next);
    void next.finally(() => {
      if (_planPersistChain.get(planId) === next) _planPersistChain.delete(planId);
    });
    return next;
  };

  // A: NL plan change → amend preview (not persisted) → apply to store for confirmation. Return whether preview was produced.
  const onAmendPlanCard = async (planId: string, instruction: string): Promise<boolean> => {
    const ids = await resolvePlanIds(planId);
    if (!ids) return false;
    const snap = getPlanSnapshot(planId);
    try {
      const res = await amendPlan(planId, instruction, ids);
      if (getPlanSnapshot(planId) !== snap) {
        toast({ kind: "error", text: getChatSendErrorMessage("plan_not_approvable") });
        return false;
      }
      applyPlanSnapshot({
        plan_id: planId,
        plan: { id: planId, status: "proposed", title: snap?.title, current_rev: res.expected_rev },
        steps: res.steps,
      });
      return true;
    } catch (err) {
      toast({ kind: "error", text: tpc(`action_errors.${planActionErrorKey(err, "amend")}`), action: copyDiagnosticsAction(err, "chat") });
      return false;
    }
  };

  // A: discard amend preview → refetch actual backend plan.
  const onReloadPlanCard = async (planId: string) => {
    const ids = await resolvePlanIds(planId);
    if (!ids) return;
    try {
      const st = await fetchPlanStateAuthed(planId, { workspaceId: ids.workspaceId });
      applyPlanSnapshot({ plan_id: planId, plan: st.plan, steps: st.steps });
    } catch {
      /* ignore */
    }
  };

  // O5 (2026-07-03): remove handleRetryStep / handleSkipStep; their
  // /retry_step /skip_step were 202-ack stubs (backend never reruns, FE ignores ack events),
  // so buttons never responded (user evidence). Omit until real re-dispatch is wired
  // (chat-pipeline.md). Actual intervention channel = composer steer.


  // See buildPlanFirstTurnIndex: render a plan only in its first-appearance turn.
  const planFirstTurn = useMemo(
    // Exactly matches ArchivedTurnView:2123 (turn-store first, archived snapshot fallback).
    () => buildPlanFirstTurnIndex(
      archivedTurns,
      turnStoreState?.beats,
      (t) => getTurnSnapshot(t.turnId ?? "")?.beats ?? t.beats,
    ),
    [archivedTurns, turnStoreState?.beats],
  );
  const liveTurnIndex = archivedTurns.length;

  return (
    <div
      className="chat-content-frame"
      style={{
        // chat-polish-r2 G6 (2026-05-26): 740→920 wide-screen use, min(...)
        // keeps it within chat-col grid track when canvas opens (workspace is
        // CSS grid not flex — grid-template-columns: minmax(420px, 1fr)).
        // No hard min-width — let grid track minmax 420 handle floor.
        maxWidth: "min(920px, 100%)",
        margin: "0 auto",
        // D11: no horizontal padding here. The 840px cap on .v2-messages-inner
        // already matches the composer (.v2-composer max-width:840); an extra
        // `padding: 0 24px` netted chat content to ~792px, ~48px narrower than
        // the input box. The outer .v2-messages (padding 28px 22px) supplies the
        // side gutter, so dropping this aligns content width with the composer.
        width: "100%",
      }}
    >
      {networkStatus ? <NetworkBanner status={networkStatus} /> : null}

      {archivedTurns.map((a, i) => (
        <Fragment key={`arch-${a.turnId ?? i}`}>
          {/* chat-ux-2026-08 batch E: turn separators. Previously turn-01 answer directly joined turn-02
              question with **no separator**, visually merging both turns.
              Render only between turns, never before the first.
              ⚠ Batch F correction: batch E incorrectly claimed ArchivedTurn lacked a timestamp
              (`archivedAt` is required). Still implement only the design's thin line here,
              without its centered timestamp: **each turn's own byline** now carries time (batch F fixed
              its hardcoded empty string), avoiding duplicate timestamps on separators. */}
          {i > 0 ? <div className="bh-turn-sep" role="separator" /> : null}
        <ArchivedTurnView
          archived={a}
          onEffectDisclosure={runner.updateEffectDisclosure}
          planAllowed={(planId: string) => planFirstTurn.get(planId) === i}
          onSolidify={onSolidify}
          onApprovePlan={approvePlanFromCard}
          onResumeBlockedPlan={resumeBlockedPlan}
          sessionId={runner.sessionId}
          onViewReportPlan={onViewReportPlan}
          onPersistEditsPlan={onPersistEditsPlan}
          onAmendPlanCard={onAmendPlanCard}
          onReloadPlanCard={onReloadPlanCard}
          onRetry={onComposerPrefill ? prepareResend : undefined}
          latestAnswer={i === latestArchivedIdx}
          initialDisclosurePlacement={
            a.turnId
              ? explicitDisclosurePlacementByTurnRef.current.get(a.turnId)
              : undefined
          }
          awaitingUser={Boolean(
            runner.clarificationPending &&
              a.turnId &&
              a.turnId === runner.turnId,
          )}
        />
        </Fragment>
      ))}

      {/* Separate archived turns from the **current turn** too. */}
      {archivedTurns.length > 0 && hasCurrentTurn ? (
        <div className="bh-turn-sep" role="separator" />
      ) : null}

      {(standalonePicker && clarification) || (!hasCurrentTurn && answeredSummary) ? (
        // Picker attached to the last archived turn (a follow-up clarification on a turn that already
        // completed), or its settled row after the card is gone.
        clarificationSlot
      ) : null}

      {/* chat-ux-2026-08 batch B extraction: wrap only the **live turn block**.
          Archived turns are siblings (archivedTurns.map above), receiving default context
          false, correctly, since archived turns never run.
          `bandOwnsLiveStatus`: band exclusively reports active owner / step ordinal; peer ledger suppresses
          task heading / running glyph / full current-task body (retaining terminals and prose).
          Reuse `bandMode` directly, without another predicate.
          ⚠ **After ③ this is defense in depth, unreachable in production**: true `bandMode` prevents
          rendering the entire running ledger (see `traceEnabled` below), leaving no `AgentBlock` under true context.
          Retained for two reasons: ① its capability (ledger does not duplicate band live status) now comes from the
          **stronger** no-ledger structural rule, with assertions moved to ③'s paired guards; ② `AgentBlock.test.tsx`
          supplies context directly to test the component, independently of this wiring, remaining meaningful.
          Before removal, confirm ③'s guards remain. */}
      {hasCurrentTurn ? (
        <TurnRunningProvider running={runner.running} bandOwnsLiveStatus={bandMode}>
        <CollaborationTraceProvider turnId={runner.turnId ?? ""}>
        <ContextActivityProvider turnId={runner.turnId ?? ""}>
        <div
          className={`bh-bot-block rho-agent-block${runner.turnId === "t_rho_historical_1" ? " rho-historical-turn" : ""}`}
          data-archived="false"
          data-status={runner.running ? "running" : "done"}
          data-results-message-token={runner.turnId ?? undefined}
          tabIndex={-1}
        >
          {runner.user ? (
            <UserMessage
              text={runner.user}
              attachments={runner.userAttachments}
              id={userMsgId}
              time={userTimeText}
            />
          ) : null}
          <TurnRevisionReceipt
            receipt={turnStoreState?.documentRevision}
            turnId={runner.turnId}
            sessionId={runner.sessionId}
            running={runner.running}
            failed={Boolean(runner.errorReason && runner.errorReason.source !== "canvas_op_failed")}
            terminalStatus={runner.terminalStatus}
            settled={false}
            startedAt={runner.startedAt ?? null}
            elapsedKey={elapsedAnchorKey}
            time={turnTimeText}
          />

          <BotByline time={turnTimeText} />

          {runner.turnId === "t_rho_historical_1" ? (
            <button
              type="button"
              style={{
                margin: "4px 0 10px",
                padding: "6px 10px",
                border: `1px solid var(--c-line-2)`,
                borderRadius: 4,
                background: "transparent",
                color: "var(--c-ink-2)",
                fontFamily: "var(--f-sans)",
                cursor: "pointer",
              }}
            >
              {tr("expand")}
            </button>
          ) : null}

          {/* Plan section lifted out of StepListCard chrome — v8 mounts it
              above the step list in the BotTurn flow. Backend doesn't emit
              plan_announced in Phase 1, so runner.plan is typically null. */}
          {runner.plan && runner.plan.steps && runner.plan.steps.length > 0 ? (
            <PlanSection items={runner.plan.steps.map((s) => ({
              id: s.id,
              status: s.status === "running" ? "doing" : s.status === "skipped" ? "skip" : s.status === "done" ? "done" : "todo",
              text: s.hint || s.verb,
            }))} />
          ) : null}

          {/* p0-followup F3 (R13) — observe events from disclosure descendants.
              The collapse useEffect re-checks this ref before folding so a
              user who is actively reading the trace isn't yanked into
              StepSummary mid-look. Answer/clarification interaction is ignored. */}
          <div
            className="bh-step-interaction-region"
            data-testid="bh-step-interaction-region"
            onMouseOver={markPassiveDisclosureInteraction}
            onPointerMove={markPassiveDisclosureInteraction}
            onPointerDown={markExplicitDisclosureInspection}
            onWheel={markPassiveDisclosureInteraction}
            onScrollCapture={markExplicitDisclosureInspection}
            onFocusCapture={markExplicitDisclosureInspection}
          >
            <CitationsProvider citations={runner.citations}>
            {/* Bring projected sources into chat scope,
                allowing citation markers to join aliases to sources (identity-derived aliases match across both carriers). */}
            <ChatSourcesProvider sources={chatSourcesFromPayload(runner.finalPayload)}>
              <BeatStream
                beats={liveBeatSplit.aboveBand}
                answerBeatBelowBand={liveBeatSplit.belowBand.some((b) => b.kind === "answer")}
                narrativeSuperseded={turnStoreState?.narrativeSuperseded === true}
                planAllowed={(planId: string) => planFirstTurn.get(planId) === liveTurnIndex}
                runner={runner}
                stepsFolded={stepsFolded}
                bandMode={bandMode}
                bandSlotActive={bandSlotOwnsDisclosure}
                onExpandSteps={() => setStepsFolded(false)}
                clarification={clarification}
                clarificationSlot={clarificationSlot}
                inlineArtifacts={inlineArtifacts}
                turnStoreState={turnStoreState}
                onApprovePlan={approvePlanFromCard}
                onViewReportPlan={onViewReportPlan}
                onPersistEditsPlan={onPersistEditsPlan}
                onAmendPlanCard={onAmendPlanCard}
                onReloadPlanCard={onReloadPlanCard}
                placeStepDisclosureBeforeAnswer={
                  placeLiveDisclosureBeforeAnswer
                }
              />
            </ChatSourcesProvider>
            </CitationsProvider>
            {/* ★ Band uses **only this** JSX location, beside rather than nested inside BeatStream.
                Place **after** BeatStream: design prose is **above** the band (1e's `.disc` is the column's
                last element). The prior version put it first, placing band above narrative-first prose,
                contrary to design (final review; original test checked presence without order).
                Two locations would make React unmount/rebuild on the first beat, while avoiding rebuilds
                is precisely its purpose (116's `整条塌成 Bowhead ✓ 三秒` was caused by rebuilding). */}
            {bandSlotOwnsDisclosure ? (
              <>
                {/* ★ **One JSX location** (required here and in RunningBand's header):
                    collapsed and in-place expanded states share `RunningBand`, toggling only `expanded`.
                    ⚠ Separate branches each with `<RunningBand>` make React
                    **unmount/rebuild** on every toggle, replacing the disclosure button and dropping keyboard focus
                    to `<body>`, preventing a second keyboard press to collapse after expansion
                    (independent review, the same trap warned about at :537),
                    also losing throttle state and replaying three-row entry animation each time. */}
                <RunningBand
                  steps={collectStepViews(runner, turnStoreState)}
                  running={runner.running}
                  startedAt={runner.startedAt ?? null}
                  answerStreaming={Boolean(runner.answer)}
                  paused={runner.streamingState === "paused"}
                  modelRetry={turnStoreState?.modelRetry ?? null}
                  peerNames={turnStoreState?.peerNames}
                  peerDisplayNames={turnStoreState?.peerDisplayNames}
                  peerTerminalById={turnStoreState?.peerTerminalById}
                  peerProse={turnStoreState?.peerProse}
                  elapsedKey={elapsedAnchorKey}
                  transport={runner.transportRef?.current ?? null}
                  agentProgress={runner.agentProgress ?? null}
                  expanded={bandExpandedInPlace}
                  onExpand={() =>
                    setTraceRequestOrigin((o) => (o === "band" ? null : "band"))}
                />
                {/* In-place expansion: same slot, same `AgentTrace` (inventory §2.3 requires
                   returning to the same component, never a second list). Render here rather than at the beat,
                   keeping opened content under the user's finger instead of jumping above the viewport.
                   Correspondingly, `BeatStream` suppresses all trace while `bandSlotActive`
                   (otherwise peer ledger renders twice). */}
                {bandExpandedInPlace ? (
                  <div data-testid="execution-disclosure" data-placement="band-slot">
                    <TurnTraceRegion
                      steps={collectStepViews(runner, turnStoreState)}
                      peerNames={turnStoreState?.peerNames}
                      peerDisplayNames={turnStoreState?.peerDisplayNames}
                      peerProse={turnStoreState?.peerProse}
                      peerTasks={turnStoreState?.peerTasks}
                      peerTerminalById={turnStoreState?.peerTerminalById}
                      peerFailureRecovered={turnStoreState?.cardFinalState === "completed"}
                      ariaLabel={tr("step_list_aria")}
                    />
                  </div>
                ) : null}
              </>
            ) : null}
            {liveBeatSplit.belowBand.length > 0 ? (
              <CitationsProvider citations={runner.citations}>
                <ChatSourcesProvider sources={chatSourcesFromPayload(runner.finalPayload)}>
                  <TurnBeatFlow
                    beats={liveBeatSplit.belowBand}
                    stepViews={[]}
                    traceEnabled={false}
                    suppressedSegIds={EMPTY_SEG_IDS}
                    narrativeStreaming={(segId) =>
                      runner.running && isNarrativeSegActive(runner.turnId ?? "", segId)
                    }
                    answerText={(b) => answerTextFromBeat(b, runner)}
                    answerStreaming={runner.running}
                    inlineArtifacts={inlineArtifacts}
                    keyPrefix="below-band-"
                    ariaLabel={tr("step_list_aria")}
                  />
                </ChatSourcesProvider>
              </CitationsProvider>
            ) : null}
          </div>

          {/* UX-2 (2026-07-03): midstream silent-liveness indicator; backend emits streaming.state{paused}
              after 30s without chunks (connection healthy, keepalive feeds watchdog),
              whereas FE formerly rendered only errors, making long LLM gaps appear stuck.
              ⚠ code-review 2026-07-04: show only **after content exists**, during midstream silence;
              BeatStream's AssistantWorkingIndicator handles opening gaps (no beats/steps),
              or both render almost identical `仍在处理` rows. */}
          {/* chat-ux-2026-08 (inventory §2.7): this must also yield to band takeover.
              The two almost identical `仍在处理` rows above gain a third participant with the band,
              whose disclosure already reports `第 N 步 · X 分 Y 秒`; another role=status
              adds a second on-screen announcement source, read twice by screen readers. */}
          {/* A pending model retry uses the same row outside band mode, so the retry is visible on both layouts. */}
          {!bandMode && runner.running
            && (runner.streamingState === "paused" || turnStoreState?.modelRetry)
            && ((turnStoreState?.beats?.length ?? 0) > 0
                || (turnStoreState?.steps?.length ?? 0) > 0) ? (
            <MidTurnLivenessNote
              startedAt={runner.startedAt}
              elapsedKey={elapsedAnchorKey}
              modelRetry={turnStoreState?.modelRetry ?? null}
            />
          ) : null}

          {/* bi-absent-turn R5.1: retire TurnErrorCard; it shares FinalFailBanner's source
              (turnErrorReason is runner.errorReason.reasonCode), always co-rendering stacked errors
              (customer screenshot). Its `自动重试 2 次都没成` is canned copy;
              there is no automatic retry (no production phase="auto" calls; O5 removed
              retry handler). Retain banner (error code + turn ID + diagnostics disclosure);
              move `编辑后发送` to onPrimary. `反馈问题` was already a dead button (onFeedback
              always undefined), removed without migration. */}
          {displayedFailure ? (
            (() => {
              // review P2: omit resend for empty user text (chat/send rejects empty messages with 400).
              const retryText = runner.user;
              const retryAttachments = runner.userAttachments ?? [];
              // Chat turn feedback D5: exactly one primary action, chosen by the backend's
              // recovery_action (old turns: by `retryable`).
              return (
                <FinalFailBanner
                  live={failureArrivedLive ? "assertive" : "off"}
                  kind={displayedFailure.reasonCode as ReasonCode}
                  turnId={runner.turnId ?? undefined}
                  diagnostics={displayedFailure.diagnostics}
                  clarificationAnswerState={runner.errorReason?.source === "clarification_resume" || currentBlockedClarification?.answerConfirmed
                    ? "saved" : currentBlockedClarification ? "unconfirmed" : undefined}
                  recoveryAction={resolveRecoveryAction(
                    displayedFailure.recoveryAction,
                    displayedFailure.diagnostics,
                  )}
                  onRetry={onRetryTurn}
                  retryRestoresDraft={retryRestoresDraft}
                  resumePlan={liveBlockedPlanResume}
                  onEditAndResend={onComposerPrefill && retryText ? () => prepareResend(retryText) : undefined}
                  onNewSession={
                    retryText || retryAttachments.length > 0
                      ? () => handoffQuestionToNewChat(retryText ?? "", retryAttachments)
                      : undefined
                  }
                />
              );
            })()
          ) : null}

          <CoachChangeReceipts turnId={runner.turnId} sessionId={runner.sessionId} current />
          <CoachAlignmentCards turnId={runner.turnId} sessionId={runner.sessionId} />
          {runner.turnId
          && (
            !runner.running
            || runner.terminalProtocolVersion === 1
            || runningEffectDisclosureVisible
          ) ? (
            <EffectDisclosureNotice
              turnId={runner.turnId}
              executionGeneration={runner.executionGeneration ?? null}
              initial={runner.effectDisclosure ?? null}
              discoverWhileRunning={
                runner.running && runner.terminalProtocolVersion === 1
              }
              discoverUntilRead={runner.effectDisclosureDiscoveryPending}
              onUpdate={runner.updateEffectDisclosure}
            />
          ) : null}

          {/* workflow-solidify surface B: `⊕ 固化此流程` chip. Gate = a COMPLETED turn
              (turnComplete: done, no error) that ran actual analysis STEPS. We key on
              steps, NOT runner.answer — BI/report turns deliver their answer via the
              canvas (canvas.section_*), emitting no chat final_answer, so runner.answer
              is empty on exactly the analysis turns most worth solidifying (live-dogfood finding).
              Steps-present also correctly hides the chip on trivial pure-chat turns (no
              process to solidify). onSolidify is provided only when the solidify flag is on. */}
          {/* chat-ux-2026-08 batch F: message-level actions (copy / resend).
              Show only **after this turn completes**, since running-state copy yields an incomplete answer.
              With empty answerText (BI/report answers on canvas), the component omits `复制` itself. */}
          {turnComplete ? (
            <MessageActions
              answerText={runner.answer}
              resendText={runner.user}
              onResend={onComposerPrefill ? prepareResend : undefined}
              latest
              sessionId={runner.sessionId}
              turnId={runner.turnId}
            />
          ) : null}

          {turnComplete && onContinue && runner.operationKind !== "context_maintenance" ? (
            <ContinueAction
              targets={continueTargets(turnStoreState?.peerTerminalById, turnStoreState?.peerNames, turnStoreState?.peerDisplayNames)}
              onContinue={onContinue}
            />
          ) : null}

          {onSolidify && runner.turnId && turnComplete && (turnStoreState?.steps?.length ?? 0) > 0 ? (
            <div style={{ display: "flex", marginTop: 12 }}>
              <SolidifyChip label={tr("solidify_turn_chip")} onClick={() => onSolidify(runner.turnId as string)} />
            </div>
          ) : null}

        </div>
        </ContextActivityProvider>
        </CollaborationTraceProvider>
        </TurnRunningProvider>
      ) : null}

      {memCandMode !== "off" ? (
        // review-fix: render gated by mode; new running turn (mode=off) hides old turn's
        // leftover cards (hook also clears items at cycle start, a second safeguard).
        //
        // ★★ Disclosure strip and candidate cards **check their own data independently**. Previously both depended on
        // `memCands.items.length > 0`, but zero-click direct writes **produce no candidates**,
        // so automatic memory writes never received disclosure; rendering occurred only when inappropriate.
        // `MemoryDisclose` returns null when empty, without an outer check.
        // Both read one receipt (`GET /memory/turn-receipt`): candidate-origin entries settle on the card,
        // every other entry and a not-remembered explicit request show on the disclosure strip.
        <>
          <MemoryDisclose
            entries={memCands.entries.filter((e) => e.origin !== "candidate")}
            explicitRequest={memCands.explicitRequest}
            undo={memCands.undoEntry}
            hasLandedMemory={memCands.items.length > 0 || memCands.entries.length > 0}
          />
          {/* Mount the card whenever candidates are on: approving the last candidate empties
              `items`, yet the card must keep its local "Remembered" receipt and Undo. It returns
              null itself when there is neither a candidate nor a settled receipt. */}
          <MemoryCandidateCard
            turnId={latestDoneTurnId}
            items={memCands.items}
            actingId={memCands.actingId}
            decide={memCands.decide}
            redraft={memCands.redraft}
            entries={memCands.entries}
            undoEntry={memCands.undoEntry}
          />
        </>
      ) : null}

      {/* Suggestion-result receipts (batch 5): not gated by memCandMode, since unrelated to this turn.
          ⚠ Hook `enabled=!runner.running` **stops fetching without clearing fetched cards**, retaining
          the card when a new turn starts. Deliberate: its result concerns a suggestion from days ago,
          with no reason to disappear on this turn; clearing would cause a flash each turn. */}
      <FeedbackReceiptCard
        items={receipts.items}
        actingId={receipts.actingId}
        acknowledge={receipts.acknowledge}
      />

      {/* scheduled-recipe-runs S2: `固化整段对话` belongs at the **entire conversation's end**, not an individual turn's
          action bar. It is conversation-level; F5 archives all turns, so placing it inside live turn
          would hide it on refresh, while returning to solidify commonly happens when reopening a conversation.
          Require more than one completed turn; with one, it duplicates that turn's `固化此流程` as noise. */}
      {/* chat-ux-2026-08 batch F: threshold `> 0` → `> 1` implements the declared intent above.
          More than one completed turn contrasts with `> 0` meaning at least one:
          in one-turn conversations, conversation-level chip coexists with the turn's `固化此流程`, nearly equivalent
          (observed on 116: differently styled solidify buttons on left and right).
          Note: design originally combined entry points with secondary selection, **rejected during implementation**:
          chip placement differs for concrete reasons (conversation-level must be at the end; placing it in live turn
          hides it on F5, see above). Merging breaks this; the actual bug was only an off-by-one threshold. */}
      {onSolidifySession && runner.sessionId && !runner.running && completedTurnCount > 1 ? (
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 4, marginBottom: 4 }}>
          <SolidifyChip
            testId="solidify-session-chip"
            icon="layers"
            label={tr("solidify_session_chip")}
            onClick={() => onSolidifySession(runner.sessionId as string)}
          />
        </div>
      ) : null}

      <div ref={tailRef} aria-hidden style={{ height: 1 }} />
    </div>
  );
}

// ─── beat stream renderer ────────────────────────────────────────────

interface BeatStreamProps {
  /** A superseded narrative is replaced by an evidence-backed final_answer.
   *  Passed by parent from subscribed turn-store state, rather than direct getTurnSnapshot reads inside component; those do not
   *  trigger rerender, leaving the display unchanged when the flag persists. */
  narrativeSuperseded?: boolean;
  /** Render each plan card only in its first-appearance turn (see `buildPlanFirstTurnIndex`). */
  planAllowed?: (planId: string) => boolean;
  beats: TurnBeat[];
  /** With band present, answer beats after the first step render below it (`splitBeatsAroundBand`);
   *  omit this component's `runner.answer` fallback bubble, avoiding duplicate answers above/below the band. */
  answerBeatBelowBand?: boolean;
  runner: TurnRunnerState;
  stepsFolded: boolean;
  /** 1e band owns running step rows (rendered in TurnContainer, outside this component). */
  bandMode?: boolean;
  /** Whether the running disclosure slot is owned (band or in-place expanded trace).
   *  If true, BeatStream never renders trace at the beat's original position, avoiding duplicate peer ledgers. */
  bandSlotActive?: boolean;
  onExpandSteps: () => void;
  clarification: {
    questions: ThetaClarificationQuestion[];
    intro: string | null;
    clarificationId: string;
    // Batch2 tool_context_select: label→Datarc workspace ID mapping + remember checkbox + footer hint.
    valueByLabel?: Record<string, string> | null;
    showRemember?: boolean;
    rememberDefault?: boolean;
    allowCustomAnswers?: boolean;
    footHint?: string | null;
    documentDraft?: boolean;
  } | null;
  /** The pending card or its settled row (built once by TurnContainer). */
  clarificationSlot: React.ReactNode;
  inlineArtifacts: import("./step-list/ChatBubbleNew").ChatInlineArtifact[];
  turnStoreState: TurnState | undefined;
  /** p0-followup F1 — failed step retry / skip / ack handlers. */
  /** Planning Mode — approve / view-report for an inline plan card. */
  onApprovePlan?: (planId: string) => Promise<void> | void;
  onViewReportPlan?: (planId: string) => void;
  /** Planning Mode A (editable) — persist edits / NL amend / reload (discard). */
  onPersistEditsPlan?: (planId: string) => Promise<void> | void;
  onAmendPlanCard?: (planId: string, instruction: string) => Promise<boolean>;
  onReloadPlanCard?: (planId: string) => Promise<void> | void;
  /** Normal completion has settled this disclosure immediately before answer. */
  placeStepDisclosureBeforeAnswer: boolean;
}

/**
 * Planning fall-through notice: user **explicitly** chose planning mode, but this turn produced no plan card.
 *
 * Without it, backend silently falls through to ordinary Lead, which often writes Markdown `执行计划` prose
 * following the user's request, without a card, approval, or execution; users assume it is scheduled. Silence is correct
 * when planning is unnecessary (Auto does this every turn), but deceptive after a failed attempt.
 * Backend provides only reason codes; all copy lives in bilingual i18n.
 */
function PlanningFallbackNotice({ reason }: { reason: string }) {
  const tpc = useTranslations("plan_card");
  const bodyKey = `fallback_body_${reason}`;
  // Unknown/future reason code → most generic copy; never expose raw codes or omit the entire notice.
  const known =
    reason === "authoring_empty" ||
    reason === "reify_failed" ||
    reason === "unknown_agents" ||
    reason === "plan_infeasible";
  return (
    <div className="pfallback" role="status">
      <strong className="pfallback-t">{tpc("fallback_title")}</strong>
      <span className="pfallback-b">{tpc(known ? bodyKey : "fallback_body_authoring_empty")}</span>
      <span className="pfallback-h">{tpc("fallback_hint")}</span>
    </div>
  );
}

/**
 * Iterate beats[] and dispatch per kind. Step beats fold into a single
 * StepListCard mount placed at the FIRST step beat's position in the
 * sequence — concurrency grouping happens inside StepListCard.
 *
 * Per tech-design §4.1: split beats[] into preStep / postStep around the
 * first step beat so narrative-before-step / answer-after-step ordering
 * is preserved while StepListCard remains a single mount.
 */
/**
 * TurnBeatFlow — **sole** iterative renderer for beats[] (chat-solidify O2, 2026-07-03).
 *
 * Previously BeatStream (live) and ArchivedTurnView (archived) each handwrote flat iteration;
 * every beat change needed two updates, with omissions causing live-correct/F5-wrong behavior (W2,
 * origin of chat-pipeline.md). Both now share this component, exposing all **intentional**
 * differences as props:
 *   · narrativeStreaming — live: running && isNarrativeSegActive; archived: always false
 *   · answerText         — beat.text first (O2), each surface's legacy fallback
 *   · stepViews          — caller performs fold/fast-path gating (empty array = no trace)
 *   · renderPlan         — live supplies plan card; archived omits it (existing behavior: skip plan beats)
 *   · fallbackAnswerText — fallback bubble without answer beat (pre-0.40.1 compatibility)
 * Shared structural invariants: fold ALL step beats into one <AgentTrace> at the first step beat
 * (P4b deinterleaves parallel peers); skip picker beats (caller renders clarification).
 */
interface TurnBeatFlowProps {
  beats: TurnBeat[];
  stepViews: StepView[];
  peerNames?: Record<string, string>;
  peerDisplayNames?: Record<string, string>;
  peerProse?: Record<string, string>;
  peerTasks?: Record<string, string>;
  peerTerminalById?: Record<string, PeerTerminal>;
  peerFailureRecovered?: boolean;
  /** Explicit render gate. Empty stepViews no longer implies no trace because
   *  peer-only turns can have terminal/prose facts without child steps. */
  traceEnabled?: boolean;
  suppressedSegIds: Set<string>;
  narrativeStreaming: (segId: string) => boolean;
  answerText: (b: Extract<TurnBeat, { kind: "answer" }>) => string;
  answerStreaming: boolean;
  inlineArtifacts: import("./step-list/ChatBubbleNew").ChatInlineArtifact[];
  renderPlan?: (planId: string) => React.ReactNode;
  /** Config copilot batch 0.5: lightweight confirmation beat (both live/archived, like plans). */
  renderLite?: (proposalId: string) => React.ReactNode;
  /** Config copilot batch 1-b: config-draft chip beat (both surfaces as above). */
  renderDraft?: (draftId: string) => React.ReactNode;
  fallbackAnswerText?: string | null;
  /** Settled execution disclosure inserted once, immediately before answer. */
  beforeAnswer?: React.ReactNode;
  /** Preserve historical React-key prefixes ("" = live, "a" = archived), avoiding needless remounts. */
  keyPrefix?: string;
  ariaLabel: string;
}

interface TurnTraceRegionProps {
  steps: StepView[];
  peerNames?: Record<string, string>;
  peerDisplayNames?: Record<string, string>;
  peerProse?: Record<string, string>;
  peerTasks?: Record<string, string>;
  peerTerminalById?: Record<string, PeerTerminal>;
  peerFailureRecovered?: boolean;
  ariaLabel: string;
}

function TurnTraceRegion({
  steps,
  peerNames,
  peerDisplayNames,
  peerProse,
  peerTasks,
  peerTerminalById,
  peerFailureRecovered,
  ariaLabel,
}: TurnTraceRegionProps) {
  return (
    <div
      className="bh-step-list"
      role="region"
      aria-label={ariaLabel}
      style={{ display: "flex", flexDirection: "column" }}
    >
      <AgentTrace
        steps={steps}
        peerNames={peerNames ?? {}}
        peerDisplayNames={peerDisplayNames ?? {}}
        peerProse={peerProse ?? {}}
        peerTasks={peerTasks ?? {}}
        peerTerminalById={peerTerminalById ?? {}}
        peerFailureRecovered={peerFailureRecovered}
      />
    </div>
  );
}

function TurnBeatFlow({
  beats,
  stepViews,
  peerNames,
  peerDisplayNames,
  peerProse,
  peerTasks,
  peerTerminalById,
  peerFailureRecovered = false,
  traceEnabled = true,
  suppressedSegIds,
  narrativeStreaming,
  answerText,
  answerStreaming,
  inlineArtifacts,
  renderPlan,
  renderLite,
  renderDraft,
  fallbackAnswerText,
  beforeAnswer,
  keyPrefix = "",
  ariaLabel,
}: TurnBeatFlowProps) {
  const kp = keyPrefix;
  const contextActivities = useContextActivities();
  const out: React.ReactNode[] = [];
  let traceRendered = false;
  let renderedAnswerBeat = false;
  let beforeAnswerRendered = false;
  const renderBeforeAnswerOnce = () => {
    if (beforeAnswerRendered || beforeAnswer == null) return;
    beforeAnswerRendered = true;
    out.push(
      <Fragment key={`${kp}execution-disclosure`}>
        {beforeAnswer}
      </Fragment>,
    );
  };
  const hasTraceFacts =
    contextActivities.length > 0 ||
    stepViews.length > 0 ||
    Object.keys(peerNames ?? {}).length > 0 ||
    Object.keys(peerDisplayNames ?? {}).length > 0 ||
    Object.keys(peerProse ?? {}).length > 0 ||
    Object.keys(peerTasks ?? {}).length > 0 ||
    Object.keys(peerTerminalById ?? {}).length > 0;
  const renderTraceOnce = () => {
    if (traceRendered || !traceEnabled || !hasTraceFacts) return;
    traceRendered = true;
    out.push(
      <TurnTraceRegion
        key={`${kp}sg-${out.length}`}
        steps={stepViews}
        peerNames={peerNames}
        peerDisplayNames={peerDisplayNames}
        peerProse={peerProse}
        peerTasks={peerTasks}
        peerTerminalById={peerTerminalById}
        peerFailureRecovered={peerFailureRecovered}
        ariaLabel={ariaLabel}
      />,
    );
  };
  beats.forEach((b, idx) => {
    if (b.kind === "step") {
      renderTraceOnce();
      return;
    }
    if (b.kind === "narrative") {
      if (suppressedSegIds.has(b.segId)) return;
      out.push(
        <LeadNarrative
          key={`${kp}n-${b.segId}`}
          text={b.text}
          isStreaming={narrativeStreaming(b.segId)}
        />,
      );
    } else if (b.kind === "answer") {
      renderedAnswerBeat = true;
      // Old/peer-only turns may have trace facts but no step beat. The
      // execution ledger still belongs immediately before the final answer.
      renderTraceOnce();
      renderBeforeAnswerOnce();
      out.push(
        <ChatBubbleNew
          key={`${kp}a-${idx}`}
          text={answerText(b)}
          isStreaming={answerStreaming}
          artifacts={inlineArtifacts}
        />,
      );
    } else if (b.kind === "plan" && renderPlan) {
      out.push(
        <Fragment key={`${kp}plan-${b.planId}`}>{renderPlan(b.planId)}</Fragment>,
      );
    } else if (b.kind === "lite" && renderLite) {
      out.push(
        <Fragment key={`${kp}lite-${b.proposalId}`}>{renderLite(b.proposalId)}</Fragment>,
      );
    } else if (b.kind === "draft" && renderDraft) {
      out.push(
        <Fragment key={`${kp}draft-${b.draftId}`}>{renderDraft(b.draftId)}</Fragment>,
      );
    }
    // Picker beat: caller renders through the clarification block.
  });
  renderTraceOnce();
  renderBeforeAnswerOnce();
  if (!renderedAnswerBeat && fallbackAnswerText) {
    // 0.40.1 #2: pre-0.40.1 final_answer lacks card_id → no answer beat;
    // fallback bubble retains both original implementations' semantics.
    out.push(
      <ChatBubbleNew
        key={`${kp}a-fallback`}
        text={fallbackAnswerText}
        isStreaming={answerStreaming}
        artifacts={inlineArtifacts}
      />,
    );
  }
  return <>{out}</>;
}

function BeatStream({
  beats,
  answerBeatBelowBand = false,
  narrativeSuperseded,
  planAllowed,
  runner,
  stepsFolded,
  bandMode = false,
  bandSlotActive = false,
  onExpandSteps,
  clarification,
  clarificationSlot,
  inlineArtifacts,
  turnStoreState,
  onApprovePlan,
  onViewReportPlan,
  onPersistEditsPlan,
  onAmendPlanCard,
  onReloadPlanCard,
  placeStepDisclosureBeforeAnswer,
}: BeatStreamProps) {
  const tr = useTranslations("chat");
  const failedSummary = runner.errorReason && !runner.running
    ? { plannedTotal: runner.plan?.steps?.length ?? null }
    : null;
  // Phase 1.6 G3 / 1.7-A — narrative buffer derived from beats[] for the
  // legacy LeadNarrative call site below (used only when there are no
  // narrative beats yet but the runner has streamed narrative tokens via
  // a different path). In practice with the beats[] dispatch this is rare;
  // kept defensively so streaming-without-beats still renders narrative.
  const fallbackNarrative = selectNarrativeBuffer(turnStoreState);

  // p0-followup F2 — fast-path gate. Backend currently emits
  // `step_list_card_created` unconditionally on Lead start (turn_runner.py:889),
  // so a Lead<3s "direct answer" turn lands with cardCreated=true + steps=[].
  // We render BotByline + ChatBubble (caller TurnContainer) but skip the
  // step-list card entirely so the UI doesn't show a hollow scaffolding row.
  // The condition is: turn-store cardCreated but zero steps; or hydrated
  // path with no traceSteps. Streaming turns (steps still arriving) keep the
  // card path so a placeholder/active rail shows up.
  const fastPathHasNoSteps =
    (turnStoreState?.cardCreated &&
      turnStoreState.steps.length === 0 &&
      !hasPeerDisclosureFacts(turnStoreState) &&
      !runner.running) ||
    (!turnStoreState?.cardCreated &&
      !hasPeerDisclosureFacts(turnStoreState) &&
      (runner.traceSteps?.length ?? 0) === 0);
  const stepViews = collectStepViews(runner, turnStoreState);
  // Band owns step rows during takeover.
  // ⚠ **Correction (③, 2026-08-14)**: formerly never-change-`traceEnabled` (inventory §2.5),
  //   **overturned**: `traceEnabled` below now equals `!bandSlotActive`.
  //   §2.5 protected peer **terminals and prose** against disappearing for the whole turn on hard failure,
  //   but ①② moved both into the band, so preservation no longer requires the whole ledger;
  //   retaining it creates the two live progress surfaces this iteration removes.
  //   ⚠ Conversely, **verify ①② before changing this predicate** (peer terminal rows + prose tails on band).
  const traceStepViews = bandSlotActive ? [] : stepViews;
  // ── Keep **one** live progress surface while running (chat-ux-2026-08 ③) ────────────────────
  //
  // User report: `之前的卡片也有、现在的卡片也有，我以为是一起的`. 116 @1.0.97 plan turn
  // same-frame DOM observation: band at y=9877 reported `正在想下一步 · 第 3 步 · 22 秒`, peer ledger at
  // y=9597 repeated **the same task text**, each with `⟳`, 280px apart.
  //
  // Root cause: **this predicate itself** previously rendered the ledger if band lacked some ledger content,
  // while one of its three tables, `peerTasks`, is **written at dispatch time**
  // (`turn-store-dispatch.ts:449`'s `applyPeerName`), so **every multi-agent turn**
  // retains ledger beside band from first dispatch throughout. Single-agent false makes it appear intermittent.
  //
  // Thus simplify: **band (or trace expanded in its slot) present → silence the beat-position surface**.
  // ⚠ Requires ①② implemented: the band carries both peer terminals and prose now.
  //   Reversing that order genuinely lost both in an intermediate version, a regression rather than refactoring.
  //   Before removing this predicate, recheck ①②.
  // chat-ux-2026-08 (inventory §2.6): empty-shell guard for the no-beat branch.
  //
  // Previously rendered `CurrentTurnStepList` → `StepListCardConnected`, a **second independent
  // data path**, reading store steps via its own `useTurn(turnId)`, entirely ignoring `stepViews`
  // prop. Two trace-render predicates inevitably diverge (third 1e band iteration failed here:
  // clearing stepViews had no effect, requiring another predicate above it).
  //
  // Both branches now share `TurnTraceRegion` + `stepViews`. Guard exactly copies the former
  // `StepListCardConnected` predicate, preserving semantics:
  //   · Backend **unconditionally** emits step_list_card_created on Lead start, giving even 3-second direct-answer turns
  //     cardCreated=true + steps=[]; this must not render an empty scaffold;
  //   · Peers may have only task/prose/terminal without child steps, requiring their ledger to remain.
  // ⚠ Same source as `traceEnabled` below: in-place expansion gives the slot full trace ownership;
  //   rerendering here duplicates peer ledgers (nearly missed here during final review).
  const noBeatHasTraceFacts = Boolean(
    turnStoreState?.cardCreated
      && (traceStepViews.length > 0
        || (!bandSlotActive && hasPeerDisclosureFacts(turnStoreState))),
  );
  const beforeAnswerDisclosure = placeStepDisclosureBeforeAnswer ? (
    <div
      data-testid="execution-disclosure"
      data-placement="before-answer"
    >
      {stepsFolded ? (
        <StepSummary
          steps={stepViews}
          peerNames={turnStoreState?.peerNames}
          totalDurationMs={turnStoreState?.totalDurationMs}
          stepOutcome={turnStoreState?.stepOutcome}
          failed={failedSummary}
          onExpand={onExpandSteps}
        />
      ) : (
        <TurnTraceRegion
          steps={stepViews}
          peerNames={turnStoreState?.peerNames}
          peerDisplayNames={turnStoreState?.peerDisplayNames}
          peerProse={turnStoreState?.peerProse}
          peerTasks={turnStoreState?.peerTasks}
          peerTerminalById={turnStoreState?.peerTerminalById}
          peerFailureRecovered={turnStoreState?.cardFinalState === "completed"}
          ariaLabel={tr("step_list_aria")}
        />
      )}
    </div>
  ) : null;

  // No beats yet — render any narrative buffer (defensive) + the step list
  // (live turn-store path) + answer fallback (runner.answer for hydrated
  // sessions that didn't capture beats[]).
  if (beats.length === 0) {
    const showWorking = Boolean(
      runner.running &&
        // chat-ux-2026-08 (inventory §2.7): the band replaces this; design §2-A combines
        // `处理中／思考中／开始处理` into one `正在准备` + skeleton.
        // Band activation must suppress this, avoiding two simultaneous role=status announcements read consecutively.
        !bandMode &&
        !fallbackNarrative &&
        !runner.answer &&
        !clarification &&
        stepViews.length === 0 &&
        !hasPeerDisclosureFacts(turnStoreState) &&
        !runner.errorReason,
    );
    return (
      <>
        {fallbackNarrative ? (
          <LeadNarrative text={fallbackNarrative} isStreaming={runner.running} />
        ) : null}
        {showWorking ? <AssistantWorkingIndicator
            startedAt={runner.startedAt}
            elapsedKey={elapsedAnchorKeyOf(runner.turnId, runner.executionGeneration)}
          /> : null}
        {placeStepDisclosureBeforeAnswer || fastPathHasNoSteps || showWorking ? null : stepsFolded ? (
          <StepSummary
            steps={stepViews}
            peerNames={turnStoreState?.peerNames}
            totalDurationMs={turnStoreState?.totalDurationMs}
            stepOutcome={turnStoreState?.stepOutcome}
            failed={failedSummary}
            onExpand={onExpandSteps}
          />
        ) : noBeatHasTraceFacts ? (
          <TurnTraceRegion
            steps={traceStepViews}
            peerNames={turnStoreState?.peerNames}
            peerDisplayNames={turnStoreState?.peerDisplayNames}
            peerProse={turnStoreState?.peerProse}
            peerTasks={turnStoreState?.peerTasks}
            peerTerminalById={turnStoreState?.peerTerminalById}
            peerFailureRecovered={turnStoreState?.cardFinalState === "completed"}
            ariaLabel={tr("step_list_aria")}
          />
        ) : null}
        {clarificationSlot}
        {beforeAnswerDisclosure}
        {runner.answer ? (
          <ChatBubbleNew
            text={runner.answer}
            isStreaming={runner.running}
            artifacts={inlineArtifacts}
          />
        ) : null}
      </>
    );
  }

  // Flat beats iteration: narrative/answer/plan beats render at their actual
  // positions; ALL step beats collapse into ONE grouped <AgentTrace> rendered at
  // the first step beat (see renderTraceOnce below). P4b: the turn's parallel
  // peers interleave into a jumble in raw beat order, so we de-interleave by
  // grouping per turn rather than rendering steps inline per beat.
  const turnIdForCursor = runner.turnId ?? "";
  const hasAnyStepBeat = beats.some((b) => b.kind === "step");
  // 0.39.8 #5 / 0.40.1 #1: post-stream, suppress narrative beats whose prose
  // is a prefix of the answer text (the answer text gets re-rendered as a
  // ChatBubble either as an answer beat OR as the line-740 fallback when
  // backend's final_answer event lacked card_id and no beat got created).
  // 0.40.1 #2: dropped `beatAnswerExists` requirement — fallback bubble also
  // shows runner.answer, so we should dedup against it regardless of whether
  // the beat exists. Pre-0.40.1 turns (before backend started emitting
  // card_id on final_answer) have no answer beat → narrative wasn't being
  // deduped → user saw the prose twice. Plus dropped the 50-char floor
  // (blocked 33-char Chinese narratives).
  const answerTextForDedup = !runner.running
    ? (runner.answer ?? "")
    : "";
  const suppressedNarrativeSegIds = computeNarrativeDedupSegIds(
    beats,
    answerTextForDedup,
    // Read the predicate from turn-store (same source for live / archived / F5), rather than runner state;
    // runner is empty after F5; on 116, `出结果时压住、一刷新又冒出来` was observed.
    // A corrected question can resume before its final answer arrives. Running
    // again must not restore prose already rejected by the backend.
    narrativeSuperseded === true,
  );

  return (
    <>
      {/* Explicit planning mode failed to produce a plan → explain first, then answer (**before** the answer:
          users expected a plan; correct that expectation before reading the answer). */}
      {runner.planningFallback ? (
        <PlanningFallbackNotice reason={runner.planningFallback} />
      ) : null}
      {/* Folded path: ONE StepSummary collapses ALL steps regardless of beats
          position. Narrative + answer beats still iterate inline below so
          the prose around the summary stays readable. */}
      {!placeStepDisclosureBeforeAnswer && stepsFolded && hasAnyStepBeat && !fastPathHasNoSteps ? (
        <StepSummary
          steps={collectStepViews(runner, turnStoreState)}
          peerNames={turnStoreState?.peerNames}
          totalDurationMs={turnStoreState?.totalDurationMs}
          stepOutcome={turnStoreState?.stepOutcome}
          failed={failedSummary}
          onExpand={onExpandSteps}
        />
      ) : null}

      {/* O2 (2026-07-03): flat iteration consolidated into TurnBeatFlow, shared with ArchivedTurnView;
          change beat rendering once. Explicit live differences via props:
          narrative-streaming predicate (prod-bug-bash-039 #13 per-segId cursor AND
          runner.running; 0.39.4 F1's module-state cursor failed to rerender),
          fold/fast-path gating (empty stepViews), plan-card rendering.
          ⚠ code-review 2026-07-04: live **omits** fallbackAnswerText; render runner.answer
          fallback **after** ClarificationCard below (pre-sprint order), see there. */}
      <TurnBeatFlow
        beats={beats}
        stepViews={
          placeStepDisclosureBeforeAnswer || stepsFolded || fastPathHasNoSteps
            ? []
            : traceStepViews
        }
        peerNames={turnStoreState?.peerNames}
        peerDisplayNames={turnStoreState?.peerDisplayNames}
        peerProse={turnStoreState?.peerProse}
        peerTasks={turnStoreState?.peerTasks}
        peerTerminalById={turnStoreState?.peerTerminalById}
        peerFailureRecovered={turnStoreState?.cardFinalState === "completed"}
        traceEnabled={
          // Slot present ⇒ owns all running disclosure (collapsed band or in-place expanded trace),
          // silencing the beat-position surface. **Same source** as `noBeatHasTraceFacts` above.
          !bandSlotActive
          && !placeStepDisclosureBeforeAnswer && !stepsFolded && !fastPathHasNoSteps
        }
        suppressedSegIds={suppressedNarrativeSegIds}
        narrativeStreaming={(segId) =>
          runner.running && isNarrativeSegActive(turnIdForCursor, segId)
        }
        answerText={(b) => answerTextFromBeat(b, runner)}
        answerStreaming={runner.running}
        inlineArtifacts={inlineArtifacts}
        renderPlan={(planId) => (
          // See buildPlanFirstTurnIndex: render a plan only on first appearance, aligning live with replay.
          planAllowed && !planAllowed(planId) ? null : (
          <PlanCardConnected
            planId={planId}
            displayNameFor={planStepDisplayNameResolver(turnStoreState?.peerDisplayNames)}
            onApprove={onApprovePlan}
            onViewReport={onViewReportPlan}
            onPersistEdits={onPersistEditsPlan}
            onAmendPlan={onAmendPlanCard}
            onReloadPlan={onReloadPlanCard}
            reconcileOnMount
          />
          )
        )}
        renderLite={(proposalId) => <LiteConfirmConnected proposalId={proposalId} />}
        renderDraft={(draftId) => <DraftChip draftId={draftId} onOpen={requestOpenDraft} />}
        beforeAnswer={beforeAnswerDisclosure}
        ariaLabel={tr("step_list_aria")}
      />

      {clarificationSlot}

      {/* runner.answer fallback bubble (hydrated / mock / answer present without answer beat);
          after clarification, retaining pre-sprint BeatStream order. Skip if a beat rendered the answer. */}
      {runner.answer && !answerBeatBelowBand && !beats.some((b) => b.kind === "answer") ? (
        <ChatBubbleNew
          text={runner.answer}
          isStreaming={runner.running}
          artifacts={inlineArtifacts}
        />
      ) : null}
    </>
  );
}

/** UX-2: midstream silent-liveness row, a visible signal while turn runs without model output.
 *  Reuses .bh-working-indicator appearance; one-second ticks update elapsed seconds. */
function MidTurnLivenessNote(
  { startedAt, elapsedKey, modelRetry = null }: {
    startedAt?: number | null;
    elapsedKey?: string | null;
    modelRetry?: ModelRetryStatus | null;
  },
) {
  const t = useTranslations("chat_working");
  const tBand = useTranslations("step_list");
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  // Same algorithm as band: raw subtraction stays 0 during server/browser clock-offset windows (lib/turn-elapsed.ts).
  // ⚠ Key must be turnId: `startedAt` changes clocks within a turn; using it reanchors (116 observed 0→1→0).
  const elapsedSec = Math.floor(turnElapsedMs(startedAt, undefined, elapsedKey ?? null) / 1000);
  return (
    <div
      className="bh-working-indicator"
      data-testid="midturn-liveness-note"
      role="status"
      aria-live="polite"
    >
      <span className="bh-working-dot" aria-hidden />
      <span className="bh-working-text">
        {modelRetry ? formatModelRetry(modelRetry, tBand) : t("still_running_paused", { sec: elapsedSec })}
        <span className="bh-working-dots" aria-hidden>
          <i />
          <i />
          <i />
        </span>
      </span>
    </div>
  );
}

function AssistantWorkingIndicator(
  { startedAt, elapsedKey }: { startedAt?: number | null; elapsedKey?: string | null },
) {
  const t = useTranslations("chat_working");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 1600);
    return () => window.clearInterval(timer);
  }, []);

  // Likewise use shared algorithm, never raw subtraction; key must also be turnId.
  const elapsedSec = Math.floor(turnElapsedMs(startedAt, undefined, elapsedKey ?? null) / 1000);
  const phrases =
    elapsedSec >= 10
      ? [t("still_working"), t("checking_context"), t("preparing_update")]
      : elapsedSec >= 4
        ? [t("thinking"), t("checking_context"), t("working")]
        : [t("working"), t("thinking"), t("getting_started")];
  const phrase = phrases[tick % phrases.length];

  return (
    <div
      className="bh-working-indicator"
      data-testid="assistant-working-indicator"
      role="status"
      aria-live="polite"
    >
      <span className="bh-working-dot" aria-hidden />
      <span className="bh-working-text">
        {phrase}
        <span className="bh-working-dots" aria-hidden>
          <i />
          <i />
          <i />
        </span>
      </span>
    </div>
  );
}

/**
 * Pull StepView[] for the live trace + StepSummary fold count. Reads turn-store
 * only; legacy traceSteps fallback retired in chat-polish-r3 Phase 1.5 since
 * lib/turn-store-replay.ts hydrates the same canonical surface on F5.
 *
 * chat-ux-2026-08 (inventory §2.11): **delegate** to `stepsToStepViews` (merge `准备中` rows, then per-step `stepToStepView`),
 * never another handwritten projection. Former copies had identical fields except `uiKind`
 * (this function forwarded `s.error`; the other added `inferFailureUiKind`).
 * Divergence appears only for `needs_user_input` / `unknown` kinds, while the wire normalizer
 * (`sse-events.ts:370`) narrows kind to transient/permanent, so **no actual**
 * production divergence was observed. Consolidation is structural: handwritten copies have no mechanism for joint changes;
 * widening normalization immediately forks behavior. Full contract in `step-list/step-view.ts` header.
 */
const EMPTY_SEG_IDS: Set<string> = new Set();

/**
 * With running band present, split beats around the first step beat into above/below-band segments.
 * Move only prose and answers; retain step beats above (already suppressed during takeover), preserving plans/confirmation
 * and all other beats. No split without band (pre-execution, completed, clarification, error); share replay's iteration path.
 */
function splitBeatsAroundBand(
  beats: TurnBeat[],
  bandSlotOwnsDisclosure: boolean,
): { aboveBand: TurnBeat[]; belowBand: TurnBeat[] } {
  const firstStep = bandSlotOwnsDisclosure ? beats.findIndex((b) => b.kind === "step") : -1;
  if (firstStep < 0) return { aboveBand: beats, belowBand: [] };
  const movable = (b: TurnBeat, i: number) =>
    i > firstStep && (b.kind === "narrative" || b.kind === "answer");
  return {
    aboveBand: beats.filter((b, i) => !movable(b, i)),
    belowBand: beats.filter((b, i) => movable(b, i)),
  };
}

function collectStepViews(
  runner: TurnRunnerState,
  turnStoreState: TurnState | undefined,
): StepView[] {
  void runner;
  if (!turnStoreState || turnStoreState.steps.length === 0) return [];
  return stepsToStepViews(turnStoreState.steps, turnStoreState.peerTerminalById);
}

function answerTextFromBeat(b: Extract<TurnBeat, { kind: "answer" }>, runner: TurnRunnerState): string {
  // chat-solidify O2 (2026-07-03): answer beats now carry text (applyFinalAnswer
  // persists it; live/replay use the same dispatcher); beat.text is the first source,
  // with runner.answer fallback during streaming and for pre-O2 data.
  return b.text ?? runner.answer ?? "";
}

/**
 * 0.40.1 #1 — shared narrative/answer dedup. Returns the set of narrative
 * segIds whose normalized prose overlaps the answer text (in EITHER
 * containment direction) and should therefore be suppressed (the answer beat
 * carries the same prose, often with citation reformatting). Used by both
 * BeatStream (live, when not streaming) and ArchivedTurnView (always — turn is
 * complete by definition).
 *
 * Replaces the prior 50-char floor that blocked Chinese narratives
 * (~30-char "报告已生成，..." duplicated against identical final_answer).
 *
 * chat-solidify follow-up (2026-07): dedup is now BIDIRECTIONAL. The original
 * check was forward-only (narrative ⊆ answer). Lead-self / no-data / chit-chat
 * turns stream the whole Lead prose as narrative ("intro + answer") while
 * final_answer carries only the answer tail — so the answer's prefix lives
 * INSIDE the narrative (narrative ⊇ answer), which the forward check missed →
 * user saw the answer twice. See chat-pipeline.md.
 */
function computeNarrativeDedupSegIds(
  beats: TurnBeat[],
  answerText: string,
  /** The backend may supersede a narrative whose numbers disagree with tool results.
   *  Similarity checks cannot detect conflicting values with different wording, so this
   *  explicit marker suppresses the narrative independently of textual overlap. */
  narrativeSuperseded?: boolean,
): Set<string> {
  const suppressed = new Set<string>();
  if (narrativeSuperseded) {
    for (const b of beats) {
      if (b.kind === "narrative") suppressed.add(b.segId);
    }
    return suppressed;
  }
  if (!answerText) return suppressed;
  // Compare the prose the user sees: internal <response_kind>/<headline> markers are hidden at render
  // time, and leaving them in let a marker split the overlap probe so a duplicate narrative stayed visible.
  const norm = (s: string) => stripInternalMarkers(s)
    .replace(/\*\*/g, "")
    .replace(/\[(?:data|cite|num|ref|figure):[^\]]*\]/g, "")
    // Final citation cleanup removes bare Artifact labels; compare their streamed form equally.
    // Keep knowledge membership and operation IDs, which carry the actual receipt identity.
    .replace(/\s*[(（\[【]?\s*(?:artifact(?:_id)?\s*[=:：]\s*|artifact\s+)?art_[a-f0-9]{8,}\s*[)）\]】]?/gi, "")
    .replace(/\s+/g, "")
    .trim();
  const a = norm(answerText);
  if (a.length === 0) return suppressed;
  const probeLen = 80;
  const aProbe = a.slice(0, Math.min(probeLen, a.length));
  for (const b of beats) {
    if (b.kind !== "narrative") continue;
    const t = norm(b.text);
    if (t.length === 0) continue;
    const slice = t.slice(0, Math.min(probeLen, t.length));
    // Bidirectional: a.includes(slice) → narrative ⊆ answer (0.40.1 case);
    // t.includes(aProbe) → narrative ⊇ answer (Lead-self "intro + answer").
    if (a.includes(slice) || t.includes(aProbe)) suppressed.add(b.segId);
  }
  return suppressed;
}

function formatTurnTime(startedAt: number | null | undefined): string {
  if (!startedAt) return "";
  try {
    return new Date(startedAt).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

// ─── inner: archived turn renderer ───────────────────────────────────

/** Whether an archived turn completed normally (same criterion as action bar/completion summary); parent uses it to find the latest answer. */
export function archivedTurnComplete(archived: ArchivedTurn, storeState: TurnState | undefined): boolean {
  return Boolean(
    !archived.errorReason &&
      (archived.terminalStatus != null
        ? archived.terminalStatus === "completed"
        : storeState?.cardFinalized &&
          storeState.cardFinalState === "completed"),
  );
}

/**
 * Archived index of the latest answer: completed current turn is latest (return -1);
 * otherwise last normally completed archived turn (failed/stopped turns have no action bar). Same criterion for live and F5.
 */
export function latestAnsweredArchivedIndex(
  archivedTurns: ArchivedTurn[],
  currentTurnComplete: boolean,
  snapshotOf: (turnId: string) => TurnState | undefined = getTurnSnapshot,
): number {
  if (currentTurnComplete) return -1;
  for (let i = archivedTurns.length - 1; i >= 0; i -= 1) {
    const a = archivedTurns[i];
    if (archivedTurnComplete(a, snapshotOf(a.turnId ?? ""))) return i;
  }
  return -1;
}

interface ArchivedTurnViewProps {
  archived: ArchivedTurn;
  /** Render plan card only in its first-appearance turn (TurnContainer's
   *  `buildPlanFirstTurnIndex`). Omission = no gate (standalone mounts/tests). */
  planAllowed?: (planId: string) => boolean;
  /** Preserve explicit live-reader placement across done(completed) remount. */
  initialDisclosurePlacement?: ExplicitDisclosurePlacement;
  /** A turn-terminating clarification can arrive after completed archiving. */
  awaitingUser?: boolean;
  /** chat-polish-r3 Phase 1 (B5) — failed step retry against archived turn.
   *  Parent (TurnContainer) wires `archived.turnId` into the closure so the
   *  POST goes to the right resume endpoint even after the live runner has
   *  moved on (runner.turnId = null after done event). */
  /** workflow-solidify surface B: solidify a past analysis turn (flag-gated; undefined = hidden). */
  onSolidify?: (turnId: string) => void;
  /** planning-ux-reliability (2026-07): archived/F5 also renders plan beats. Formerly only
   *  live BeatStream handled plans; refresh/history lost cards and proposed-state approval access
   *  (suspected cause of abandoned plan_6477cebec5dc on 116). */
  onApprovePlan?: (planId: string) => Promise<void> | void;
  /** plan_blocked failure card: re-check and resume the blocked plan (`false` = fall back to Edit and send). */
  onResumeBlockedPlan?: (planId: string) => Promise<boolean>;
  /** Session of this conversation (revision receipts open drafts of this session only). */
  sessionId?: string | null;
  onViewReportPlan?: (planId: string) => void;
  onPersistEditsPlan?: (planId: string) => Promise<void> | void;
  onAmendPlanCard?: (planId: string, instruction: string) => Promise<boolean>;
  onReloadPlanCard?: (planId: string) => Promise<void> | void;
  /** Put an interrupted turn's question back in the composer for review. */
  onRetry?: (text: string) => void;
  /** This is the conversation's latest answer (no later completed current turn); its action bar is always shown. */
  latestAnswer?: boolean;
  onEffectDisclosure?: Props["runner"]["updateEffectDisclosure"];
}

function ArchivedTurnView({
  archived,
  planAllowed,
  initialDisclosurePlacement,
  awaitingUser = false,
  onSolidify,
  onApprovePlan,
  onResumeBlockedPlan,
  sessionId,
  onViewReportPlan,
  onPersistEditsPlan,
  onAmendPlanCard,
  onReloadPlanCard,
  onRetry,
  latestAnswer = false,
  onEffectDisclosure,
}: ArchivedTurnViewProps) {
  const tr = useTranslations("chat");
  // chat-polish-r3 Phase 1 — read steps from turn-store keyed by archived
  // turnId. Replaces the lossy `archived.traceSteps` snapshot path which
  // captured the empty LEGACY runner.traceSteps (B1 root cause). Live
  // step_started/step_done events already populate turn-store; F5 hydrate
  // re-populates via `replayTurnEvents` (lib/turn-store-replay.ts).
  const storeState = useTurn(archived.turnId ?? "");
  const archivedBlockedPlanResume = useBlockedPlanResume(
    archived.errorReason?.reasonCode === "plan_blocked" ? storeState?.blockedPlanId : undefined,
    onResumeBlockedPlan,
  );
  // ⚠ Dependencies **must include `peerTerminalById`**: projection now uses it to correct spawn uiKind
  //   (`step-view.ts`'s `correctSpawnFailureKind`). Depending only on `steps` makes
  //   terminal-only updates retaining the steps reference leave archive rendering **uncorrected** values,
  //   collapsed strip `已重试 1 次` and amber temporary failure in ledger, although the peer actually failed.
  //   `applyPeerTerminal` really performs this second write (`lead.subagent_summary` overrides
  //   status written by `agent_spawned`), so this is not hypothetical.
  const steps = useMemo<StepView[]>(
    () => (storeState ? stepsToStepViews(storeState.steps, storeState.peerTerminalById) : []),
    [storeState?.steps, storeState?.peerTerminalById],
  );

  // Phase 1.6 G4 — same artifact surface for replayed/historical turns.
  const inlineArtifacts = useMemo(
    () => extractInlineArtifacts(archived.finalPayload),
    [archived.finalPayload],
  );

  // Phase 1.7-B — beats snapshot captured at archive time (`unknown[]` in
  // types.ts; cast here to TurnBeat[] since the dispatcher writes only that
  // type). chat-polish-r3 Phase 1: also prefer turn-store beats live —
  // archived.beats stays as the snapshot fallback for pre-r3 archived turns
  // that were captured before the replay wire landed.
  const beats = (storeState?.beats ?? archived.beats ?? []) as TurnBeat[];

  // Historical turns default to folded. The one exception is an explicit
  // inspection made while this same turn was still live: done(completed)
  // remounts it as archived, but must not move content under the reader.
  const [stepsFolded, setStepsFolded] = useState(
    initialDisclosurePlacement === undefined,
  );

  // chat-ux-2026-08 batch F: formerly hardcoded `""`, with a comment claiming historical turns lacked timestamps.
  // Obsolete: `ArchivedTurn.archivedAt` is now required. Live byline showed
  // 14:32, then F5 **erased** that turn's time, violating CLAUDE.md's
  // `live、持久化和重放的数据必须一致`.
  // Both write paths have consistent semantics, so displaying it is safe:
  //   live    → `archiveCompletedTurnInPlace` uses `Date.now()` **on completion**
  //   hydrate → `Date.parse(turn.ended_at ?? turn.started_at)`
  // If both timestamps are absent in legacy archive, `Date.parse` yields NaN; `formatTurnTime`'s falsy guard
  // turns it into empty (NaN is falsy), degrading to original behavior.
  const turnTimeText = formatTurnTime(archived.archivedAt);
  // ⚠ Above applies only to BotByline: `archivedAt` is **end** time. User bubbles formerly also used it;
  // observed on 116: `07:46 发送 → 完成后显示 你 · 07:53`. User bubbles now use submission time, same source as live.
  const userTimeText = formatTurnTime(resolveUserMessageTimeMs({
    firstTurnStartedAt: storeState?.startedAt ?? null,
    submittedAtMs: archived.userSentAt,
    fallbackMs: archived.archivedAt,
  }));
  const archivedComplete = archivedTurnComplete(archived, storeState);

  const userId = archived.userMsgId ?? null;

  // 0.40.1 #1 — dedup narrative beats whose prose is a prefix of the
  // archived answer text (parity with live BeatStream).
  const suppressedArchivedSegIds = computeNarrativeDedupSegIds(
    beats,
    archived.answer ?? "",
    getTurnSnapshot(archived.turnId ?? "")?.narrativeSuperseded === true,
  );

  // chat-polish-r3 Phase 1.5 — legacy traceSteps fallback retired.
  // prod-bug-bash-039 #13: ArchivedTurnView now mirrors live BeatStream flat
  // iteration so historical turns show narrative interleaved between tool
  // steps (was: all narrative trailing after step list — same bug as live
  // BeatStream pre-r2.1).

  // Fallback path for pre-1.7 archived turns with no beats[].
  const legacyAnswerFallback =
    beats.length === 0 && archived.answer ? (
      <ChatBubbleNew
        text={archived.answer}
        isStreaming={false}
        artifacts={inlineArtifacts}
      />
    ) : null;

  // Pre-r3 archived turns whose DB events lack step_list_card_created render
  // no step block (consistent with their actual state).
  const hasConnectedState = Boolean(
    archived.turnId && storeState?.cardCreated,
  );
  const hasArchivedDisclosure = Boolean(
    hasConnectedState &&
      (steps.length > 0 || hasPeerDisclosureFacts(storeState)),
  );
  // Only a persisted completed finalization gets the compact historical
  // placement. Failure, cancellation, HITL and unfinished turns retain the
  // expanded trace at the original execution beat.
  const normalArchivedCompletion = Boolean(
    hasArchivedDisclosure &&
      archivedComplete &&
      storeState?.cardFinalized &&
      storeState.cardFinalState === "completed" &&
      !awaitingUser &&
      !hasBlockingApprovalState(storeState),
  );
  const placeArchivedDisclosureBeforeAnswer = Boolean(
    normalArchivedCompletion &&
      initialDisclosurePlacement !== "inline-open",
  );
  // A failed turn keeps the trace at its beat only once the reader expands it; folded it is one line above the
  // failure card (spec §8), like the live turn after it failed.
  const foldArchivedFailure = Boolean(
    archived.errorReason && hasArchivedDisclosure && stepsFolded && !awaitingUser,
  );
  const hideArchivedTrace = placeArchivedDisclosureBeforeAnswer || foldArchivedFailure;
  const archivedBeforeAnswerDisclosure = placeArchivedDisclosureBeforeAnswer ? (
    <div
      data-testid="execution-disclosure"
      data-placement="before-answer"
    >
      {stepsFolded ? (
        <StepSummary
          steps={steps}
          peerNames={storeState?.peerNames}
          totalDurationMs={storeState?.totalDurationMs}
          stepOutcome={storeState?.stepOutcome}
          onExpand={() => setStepsFolded(false)}
        />
      ) : (
        <TurnTraceRegion
          steps={steps}
          peerNames={storeState?.peerNames}
          peerDisplayNames={storeState?.peerDisplayNames}
          peerProse={storeState?.peerProse}
          peerTasks={storeState?.peerTasks}
          peerTerminalById={storeState?.peerTerminalById}
          peerFailureRecovered={
            storeState?.cardFinalState === "completed" && !archived.errorReason
          }
          ariaLabel={tr("step_list_aria")}
        />
      )}
    </div>
  ) : null;

  return (
    <CitationsProvider citations={archived.citations}>
    <ChatSourcesProvider sources={chatSourcesFromPayload(archived.finalPayload)}>
    <CollaborationTraceProvider turnId={archived.turnId ?? ""}>
    <ContextActivityProvider turnId={archived.turnId ?? ""}>
    <div
      className="bh-bot-block"
      data-archived="true"
      data-results-message-token={archived.turnId ?? undefined}
      tabIndex={-1}
    >
      {archived.user ? (
        <UserMessage
          text={archived.user}
          attachments={archived.userAttachments}
          id={userId}
          time={userTimeText}
        />
      ) : null}
      <TurnRevisionReceipt
        receipt={storeState?.documentRevision}
        turnId={archived.turnId}
        sessionId={sessionId}
        running={false}
        failed={Boolean(archived.errorReason && archived.errorReason.source !== "canvas_op_failed")}
        terminalStatus={archived.terminalStatus ?? "completed"}
        settled={!latestAnswer}
        time={turnTimeText}
      />

      <BotByline time={turnTimeText} />

      {/* Like live BeatStream: explicit planning mode failed to produce a plan; still acknowledge it after archiving
          (live and archived must match, CLAUDE.md's chat invariant). */}
      {archived.planningFallback ? (
        <PlanningFallbackNotice reason={archived.planningFallback} />
      ) : null}

      {/* Same place as the live turn's folded summary: the top of the reply, before its beats. */}
      {foldArchivedFailure ? (
        <StepSummary
          steps={steps}
          peerNames={storeState?.peerNames}
          totalDurationMs={storeState?.totalDurationMs}
          stepOutcome={storeState?.stepOutcome}
          failed={{ plannedTotal: null }}
          onExpand={() => setStepsFolded(false)}
        />
      ) : null}

      {/* O2 flat iteration consolidated into TurnBeatFlow, sharing live BeatStream's iterator.
          Archived differences via props: never streaming, answer beat.text first + archived.answer snapshot
          fallback. Normally completed disclosure collapses before answer beat; abnormal completion stays expanded
          at its original step beat. renderPlan =
          planning-ux-reliability (2026-07, merged from origin): F5/history formerly lost cards;
          shared renderer's renderPlan restores them (reconcileOnMount). */}
      {beats.length > 0 ? (
        <TurnBeatFlow
          beats={beats}
          stepViews={hideArchivedTrace ? [] : steps}
          peerNames={storeState?.peerNames}
          peerDisplayNames={storeState?.peerDisplayNames}
          peerProse={storeState?.peerProse}
          peerTasks={storeState?.peerTasks}
          peerTerminalById={storeState?.peerTerminalById}
          peerFailureRecovered={
            storeState?.cardFinalState === "completed" && !archived.errorReason
          }
          traceEnabled={!hideArchivedTrace}
          suppressedSegIds={suppressedArchivedSegIds}
          narrativeStreaming={() => false}
          answerText={(b) => b.text ?? archived.answer ?? ""}
          answerStreaming={false}
          inlineArtifacts={inlineArtifacts}
          renderPlan={(planId) => (
            // See TurnContainer's buildPlanFirstTurnIndex: render a plan only in its first-appearance turn.
            planAllowed && !planAllowed(planId) ? null : (
            <PlanCardConnected
              planId={planId}
              displayNameFor={planStepDisplayNameResolver(storeState?.peerDisplayNames)}
              onApprove={onApprovePlan}
              onViewReport={onViewReportPlan}
              onPersistEdits={onPersistEditsPlan}
              onAmendPlan={onAmendPlanCard}
              onReloadPlan={onReloadPlanCard}
              reconcileOnMount
            />
            )
          )}
          renderLite={(proposalId) => <LiteConfirmConnected proposalId={proposalId} />}
          renderDraft={(draftId) => <DraftChip draftId={draftId} onOpen={requestOpenDraft} />}
          fallbackAnswerText={archived.answer}
          beforeAnswer={archivedBeforeAnswerDisclosure}
          keyPrefix="a"
          ariaLabel={tr("step_list_aria")}
        />
      ) : (
        <>
          {hasArchivedDisclosure && !hideArchivedTrace ? (
            <TurnTraceRegion
              steps={steps}
              peerNames={storeState?.peerNames}
              peerDisplayNames={storeState?.peerDisplayNames}
              peerProse={storeState?.peerProse}
              peerTasks={storeState?.peerTasks}
              peerTerminalById={storeState?.peerTerminalById}
              peerFailureRecovered={
                storeState?.cardFinalState === "completed" && !archived.errorReason
              }
              ariaLabel={tr("step_list_aria")}
            />
          ) : null}
          {archivedBeforeAnswerDisclosure}
          {legacyAnswerFallback}
        </>
      )}

      {archived.errorReason ? (
        // Chat turn feedback D5: an older turn cannot be replayed from here, so a Retry
        // action degrades to editing the question (chooseFailureCardAction).
        <FinalFailBanner
          live="off"
          kind={archived.errorReason.reasonCode as ReasonCode}
          turnId={archived.turnId ?? undefined}
          diagnostics={archived.errorReason.diagnostics}
          clarificationAnswerState={archived.errorReason.source === "clarification_resume" ? "saved" : undefined}
          recoveryAction={resolveRecoveryAction(
            archived.errorReason.recoveryAction,
            archived.errorReason.diagnostics,
          )}
          resumePlan={archivedBlockedPlanResume}
          onEditAndResend={onRetry && archived.user ? () => onRetry(archived.user) : undefined}
          onNewSession={
            archived.user || (archived.userAttachments ?? []).length > 0
              ? () => handoffQuestionToNewChat(archived.user ?? "", archived.userAttachments ?? [])
              : undefined
          }
        />
      ) : null}

      <CoachChangeReceipts turnId={archived.turnId} sessionId={sessionId} current={latestAnswer} />
      <CoachAlignmentCards turnId={archived.turnId} sessionId={sessionId} />
      {archived.turnId ? (
        <EffectDisclosureNotice
          turnId={archived.turnId}
          executionGeneration={archived.executionGeneration ?? null}
          initial={archived.effectDisclosure ?? null}
          discoverUntilRead={archived.effectDisclosureDiscoveryPending}
          onUpdate={onEffectDisclosure}
        />
      ) : null}

      {/* Interrupted turn: review the question and reselect any references or
          attachments before starting a new turn. Only when no failure card is shown: the card
          already offers the one primary recovery action (chat turn feedback D5). */}
      {archived.interrupted && !archived.errorReason && onRetry && archived.user ? (
        <button
          type="button"
          onClick={() => onRetry(archived.user)}
          style={{
            display: "inline-flex", alignItems: "center", gap: 6,
            margin: "8px 0 4px", padding: "5px 12px",
            fontSize: "0.8125rem", fontFamily: "inherit", cursor: "pointer",
            color: "var(--c-accent)", background: "var(--c-accent-soft, transparent)",
            border: "1px solid var(--c-accent)", borderRadius: 8,
          }}
        >
          <span aria-hidden="true"><CharIcon ch="⟳" aria-hidden /></span> {tr("interrupted_retry")}
        </button>
      ) : null}

      {/* workflow-solidify surface B: solidify chip on a past analysis turn (same gate as the
          live current-turn chip: completed + had steps). Lets users solidify an earlier
          turn after they've moved on / reloaded the session. */}
      {/* Archived turns use the same edit-before-send path as live turns. */}
      {archivedComplete ? (
        <MessageActions
          answerText={archived.answer}
          resendText={archived.user}
          onResend={onRetry}
          latest={latestAnswer}
          sessionId={sessionId}
          turnId={archived.turnId}
        />
      ) : null}

      {onSolidify && archived.turnId && archivedComplete && steps.length > 0 ? (
        <div style={{ display: "flex", marginTop: 12 }}>
          <SolidifyChip label={tr("solidify_turn_chip")} onClick={() => onSolidify(archived.turnId as string)} />
        </div>
      ) : null}

    </div>
    </ContextActivityProvider>
    </CollaborationTraceProvider>
    </ChatSourcesProvider>
    </CitationsProvider>
  );
}

export function EffectDisclosureNotice({
  turnId,
  executionGeneration,
  initial,
  discoverWhileRunning = false,
  discoverUntilRead = false,
  onUpdate,
}: {
  turnId: string;
  executionGeneration: number | null;
  initial: EffectDisclosureOverlayDTO | null;
  discoverWhileRunning?: boolean;
  discoverUntilRead?: boolean;
  onUpdate?: Props["runner"]["updateEffectDisclosure"];
}) {
  const tr = useTranslations("chat");
  const [overlay, setOverlay] = useState(initial);
  const overlayRef = useRef(initial);
  const initialRef = useRef(initial);
  const discoverUntilReadRef = useRef(discoverUntilRead);
  const overlayTurnRef = useRef(turnId);
  const overlayGenerationRef = useRef(executionGeneration);
  initialRef.current = initial;
  discoverUntilReadRef.current = discoverUntilRead;
  const sameRenderedExecution = overlayTurnRef.current === turnId
    && overlayGenerationRef.current === executionGeneration;
  const renderOverlay = sameRenderedExecution ? overlay : initial;
  const pollMode = discoverWhileRunning
    ? "running"
    : discoverUntilRead
        || hasVisibleUnsettledEffect(renderOverlay)
      ? "terminal"
      : "idle";

  // Progressive hydrate may supply a proof after this component mounted.
  // Poll results already update the local ref before notifying the parent, so
  // the identical parent echo is a no-op and must not restart the poll loop.
  useEffect(() => {
    if (
      overlayTurnRef.current === turnId
      && overlayGenerationRef.current === executionGeneration
      && initial != null
      && initial !== overlayRef.current
    ) {
      overlayRef.current = initial;
      setOverlay(initial);
    }
  }, [executionGeneration, initial, turnId]);

  useEffect(() => {
    const sameExecution = overlayTurnRef.current === turnId
      && overlayGenerationRef.current === executionGeneration;
    const seed = sameExecution
      ? overlayRef.current ?? initialRef.current
      : initialRef.current;
    overlayTurnRef.current = turnId;
    overlayGenerationRef.current = executionGeneration;
    overlayRef.current = seed;
    setOverlay(seed);
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    // An effect may start at any point in the server's running lifecycle.
    // Terminal handoff performs one mandatory read, then any visible UNKNOWN
    // result continues on the normal bounded terminal polling budget.
    let mandatoryReadPending = discoverUntilReadRef.current;
    let attemptsRemaining = pollMode === "running" || mandatoryReadPending
      ? Number.POSITIVE_INFINITY
      : 6;
    let consecutiveFailures = 0;

    const refresh = async (): Promise<void> => {
      if (stopped || attemptsRemaining <= 0) return;
      attemptsRemaining -= 1;
      let current: EffectDisclosureOverlayDTO | null = null;
      let succeeded = false;
      try {
        const read = await fetchEffectDisclosure(turnId);
        if (
          executionGeneration == null
          || read.currentGeneration !== executionGeneration
        ) {
          throw new Error("stale effect disclosure generation");
        }
        current = read.overlay;
        if (stopped) return;
        succeeded = true;
        consecutiveFailures = 0;
        if (mandatoryReadPending) {
          mandatoryReadPending = false;
          // The terminal handoff must survive transient read failures, but a
          // successful UNKNOWN result returns to the normal bounded budget.
          attemptsRemaining = Math.min(attemptsRemaining, 5);
        }
        overlayRef.current = current;
        setOverlay(current);
        onUpdate?.(turnId, executionGeneration, current);
      } catch {
        if (stopped) return;
        consecutiveFailures += 1;
      }
      if (
        attemptsRemaining > 0
        && (
          (!succeeded && (
            pollMode === "running"
            || mandatoryReadPending
            || consecutiveFailures < 6
          ))
          || (
            pollMode === "running"
            && current?.protocol === 2
          )
          || hasVisibleUnsettledEffect(current)
        )
      ) {
        const delayMs = succeeded
          ? 5_000
          : Math.min(
              30_000,
              5_000 * (2 ** Math.min(consecutiveFailures - 1, 3)),
            );
        timer = setTimeout(() => void refresh(), delayMs);
      }
    };

    if (pollMode !== "idle") {
      void refresh();
    }
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [
    executionGeneration,
    onUpdate,
    pollMode,
    turnId,
  ]);

  if (renderOverlay == null || renderOverlay.attempt_count === 0) return null;
  const userAction = renderOverlay.counts.user_action;
  const inference = renderOverlay.counts.inference_internal;
  const userActionCount = Object.values(userAction).reduce(
    (sum, count) => sum + count,
    0,
  );
  const inferenceCount = Object.values(inference).reduce(
    (sum, count) => sum + count,
    0,
  );
  if (userActionCount === 0 && inferenceCount === 0) return null;
  const pending = userAction.unsettled > 0 || inference.unsettled > 0;
  const color = pending ? "var(--c-warn)" : "var(--c-ok)";
  return (
    <div
      data-testid="effect-disclosure-notice"
      role="status"
      aria-live="polite"
      style={{
        display: "flex",
        gap: 8,
        marginTop: 12,
        padding: "8px 12px",
        borderLeft: `2px solid ${color}`,
        background: pending ? "var(--c-warn-soft)" : "var(--c-ok-soft)",
        color: "var(--c-ink-2)",
        fontSize: "0.8125rem",
        lineHeight: 1.5,
      }}
    >
      <span aria-hidden><CharIcon ch={pending ? "⚠" : "✓"} aria-hidden /></span>
      <span>
        {userActionCount > 0 ? (
          <span style={{ display: "block" }}>
            {tr(
              userAction.unsettled > 0
                ? "effect_user_action_pending"
                : "effect_user_action_settled",
              { count: userAction.unsettled || userActionCount },
            )}
          </span>
        ) : null}
        {inferenceCount > 0 ? (
          <span style={{ display: "block" }}>
            {tr(
              inference.unsettled > 0
                ? "effect_inference_pending"
                : "effect_inference_settled",
              { count: inference.unsettled || inferenceCount },
            )}
          </span>
        ) : null}
      </span>
    </div>
  );
}

function hasVisibleUnsettledEffect(
  overlay: EffectDisclosureOverlayDTO | null | undefined,
): boolean {
  return Boolean(
    overlay
    && (
      (overlay.counts.user_action?.unsettled ?? 0) > 0
      || (overlay.counts.inference_internal?.unsettled ?? 0) > 0
    ),
  );
}

// ─── inner: user message (no-bubble, plain text) ─────────────────────

interface UserMessageProps {
  text: string;
  attachments?: AttachmentRef[];
  id?: string | null;
  /** Display time stamp (HH:mm). Empty string elides. */
  time?: string;
  /** Display name — defaults to "你" per v8. */
  who?: string;
}

/**
 * UserMessage — right-aligned, no chrome, mono byline above + plain sans
 * 16px text below. Phase 1.7-B per v8 `variant-a.jsx:233-285`.
 *
 * Background transparent (NOT t.surface) so the message floats over the
 * cream page background like a Discord-style "no bubble" message. Max
 * width 78% leaves breathing room on long single-line questions.
 */
function UserMessage({ text, attachments, id, time, who }: UserMessageProps) {
  const tr = useTranslations("chat");
  const byline = who ?? tr("user_byline_self");
  const hasText = text && text.trim();
  return (
    <div
      data-msg-id={id ?? undefined}
      data-testid="user-msg"
      className="bh-user-msg"
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-end",
        margin: "4px 0 26px",
      }}
    >
      <div
        style={{
          // feedback-triage-2026-08-24: this dark-theme row formerly used --c-ink-4, measured on #131210
          // at only 2.21:1 (10.5px), almost unreadable. ink-3 gives 4.42:1.
          // ⚠ Root cause extends beyond this row: dark --c-ink-4(#4F4C46)=2.21:1, light --c-ink-4(#8a8170)
          // =3.66:1 (deliberately darkened earlier for WCAG-AA, see globals.css inline comment),
          // leaving the theme ink scales misaligned. 554 references repository-wide make token changes global visual changes,
          // needing separate visual acceptance. Fix only the reported element here; track token imbalance separately.
          fontFamily: "var(--f-mono)",
          fontSize: "0.65625rem",
          color: "var(--c-ink-3)",
          letterSpacing: "0.04em",
          marginBottom: 6,
        }}
      >
        {time ? `${byline} · ${time}` : byline}
      </div>
      {hasText ? (
        <div
          style={{
            maxWidth: "78%",
            overflowWrap: "anywhere",
            fontFamily: "var(--f-sans)",
            fontSize: "1rem",
            lineHeight: 1.5,
            color: "var(--c-ink)",
            fontWeight: 500,
          }}
        >
          {text}
        </div>
      ) : (
        <div
          style={{
            // Likewise this is a **body-text substitute** (attachment-only messages),
            // needing even clearer legibility than bylines; likewise upgrade ink-4 to ink-3.
            fontFamily: "var(--f-sans)",
            fontSize: "0.8125rem",
            color: "var(--c-ink-3)",
            fontStyle: "italic",
          }}
        >
          {tr("dropped_files_no_note")}
        </div>
      )}
      {attachments && attachments.length > 0 ? (
        <UserAttachmentStrip refs={attachments} />
      ) : null}
    </div>
  );
}

function UserAttachmentStrip({ refs }: { refs: AttachmentRef[] }) {
  const resultsWorkspaceEnabled = useResultsWorkspaceCapability();
  const resultsT = useTranslations("results_workspace");
  return (
    <div
      className="ub-attachments"
      style={{
        display: "flex",
        gap: 6,
        flexWrap: "wrap",
        marginTop: 10,
        justifyContent: "flex-end",
      }}
    >
      {refs.map((r) => {
        // chat-file-download #2: chip itself is now the download trigger
        // (ChatInlineFile-style downloading/done/error states) — replaces the
        // bare `<a target="_blank">` wrapper. downloadUrl pre-wrapped with
        // appPath() keeps native downloads on the current browser entry.
        const item: AttachmentItem = {
          localId: r.fileId,
          fileId: r.fileId,
          filename: r.filename || r.fileId,
          size: r.size || 0,
          mime: r.mime || "",
          ...(r.selectedContext ? { selectedContext: r.selectedContext } : {}),
          status: "done",
          downloadUrl: appPath(`/api/v1/files/${r.fileId}`),
        };
        return (
          <AttachmentChip
            key={r.fileId}
            item={item}
            onOpen={
              resultsWorkspaceEnabled
                ? () => requestCanvasDataFocus(r.fileId, {
                    entryPoint: "chat",
                  })
                : undefined
            }
            openLabel={
              resultsWorkspaceEnabled
                ? resultsT("open_object", { filename: item.filename })
                : undefined
            }
          />
        );
      })}
    </div>
  );
}

// `selectAnswerPayload` re-export for parents that pluck answer payload
// off beats[] — keeps the import shape stable across 1.7 sub-phases.
export { selectAnswerPayload };
// `Markdown` import kept for downstream callers reaching through here.
export { Markdown };
