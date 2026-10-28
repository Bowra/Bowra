"use client";

import { exportHiddenSeriesFor } from "@/lib/chart/export-hidden-series";
import { useCallback, useId, useRef, useSyncExternalStore, useState, useEffect, useLayoutEffect, useMemo } from "react";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";

import { useStickToBottom } from "@/lib/use-stick-to-bottom";
import { useResultsNewIndicator } from "@/lib/results-new-indicator";
import {
  backslashKeyOnLayout,
  hasOpenOverlay,
  isResultsMaximizeShortcut,
  resultsMaximizeAriaKeyshortcuts,
  resultsMaximizeShortcutLabel,
} from "@/lib/workbench-shortcuts";
import type { DocumentRevisionTarget } from "@/lib/composer-document-revision";
import { composerDraftScopeSignature } from "@/lib/composer-persistence-scope";
import { focusComposerFallback } from "@/lib/composer-menu";
import type { ComposerSubmissionWireSnapshot } from "@/lib/composer-submission-recovery";
import type { AttachmentRef } from "@/lib/types";
import { inputDocumentDisplayText, type InputDocument } from "@/lib/composer-input-document";
import type {
  ComposerDraftScope,
  ComposerOrdinaryDraft,
} from "@/lib/composer-ordinary-draft";
import { chartFollowupReference, onChartFollowup } from "@/lib/chart/chart-followup";
import type {
  ComposerDraftControl,
  ComposerDraftSubmissionContext,
  ComposerDraftWriteProtocol,
} from "./chat/v2-composer";

import type { DocumentCanvasScope } from "@/lib/document-canvas-navigation";
import { DataTabFiles, useArtifactItems, type ArtifactItem } from "./canvas/data-tab-files";
import {
  ResultsNavigator,
  resultsSelectionTitle,
  type ResultsSelection,
} from "./canvas/results-navigator";
import { ResultsQuickSwitcher } from "./canvas/results-quick-switcher";
import resultsNavigatorStyles from "./canvas/results-navigator.module.css";
import { ReportView, type ReportViewHandle } from "./canvas/report-view";
import { SteerMessage } from "./chat/step-list/SteerMessage";
import { TurnStream } from "./chat/turn-stream";
import { V2Composer } from "./chat/v2-composer";
import { ComposerRecoveryNotice } from "./chat/composer-recovery-notice";
import { PinnedAttachmentsStrip } from "./pinned-attachments";
import { SplitPane } from "./shell/SplitPane";
import { FileDown, Maximize, Minimize, MoreH, Trash, X, Caret, Chat, Doc } from "./icons";
import { useSteerAcks } from "@/lib/use-steer-acks";
import { useTurnAwaitingPlanApproval } from "@/lib/use-proposed-plan-gate";
import { useMediaQuery } from "@/lib/use-media-query";
import { useRequestSidebarAutoCompact } from "@/lib/sidebar-auto-compact";
import type { TurnRunnerState } from "@/lib/types";
import type { StreamConnection } from "@/components/canvas/report-toolbar-status";
import { useSessionPythonSteps, type PythonExecutionStep } from "@/lib/use-running-python";
import {
  getCanvasSnapshot,
  selectReportSurfaceSections,
  selectVisibleSections,
  subscribeCanvas,
  type CanvasState,
} from "@/lib/canvas-store";
import { useReportSurfaceSections, useVisibleSections } from "@/lib/use-canvas-derived-sections";
import { useCanvasExport } from "@/lib/use-canvas-export";
import { ExportMenu, ExportMenuItems, ExportToast, ExportsDrawer } from "@/components/canvas/export-ui";
import { ActiveReportStatusMenuItems } from "@/components/canvas/report-status";
import {
  MenuFloating,
  MenuItemReason,
  MenuSubmenu,
  useMenu,
  useMenuItem,
  withConfirmEllipsis,
} from "@/components/ui/menu";
import { useMergeRefs } from "@floating-ui/react";
import { Markdown } from "@/lib/md";
import { copyPermissionedReportLink } from "@/lib/report-share";
import { copyTextWithFallback } from "@/lib/clipboard";
import { ShareDialog } from "@/components/share/share-dialog";
import { ShIcon } from "@/components/share/share-ui";
import { useReportAutoExport } from "@/lib/use-report-auto-export";
import { useDocumentTitle } from "@/lib/use-document-title";
import { clearCanvas, deleteCanvas } from "@/lib/api";
import { resetCanvas, onCanvasDone } from "@/lib/canvas-store";
import clsx from "clsx";
import {
  CANVAS_FOCUS_DATA_EVENT,
  CANVAS_OPEN_RESULT_EVENT,
  requestCanvasDataFocus,
  type CanvasFocusDataDetail,
  type CanvasOpenResultDetail,
} from "@/lib/canvas-workspace-focus";
import { toast } from "@/lib/use-toast";
import { useCopyDiagnosticsToastAction } from "@/components/error-notice";
import { reportHeaderActionFailedKey } from "@/lib/report-header-errors";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { Tab as UiTab, TabList, Tabs } from "@/components/ui/tabs";
import {
  useResultsCapabilities,
  retryResultsCapabilities,
  type ResultsCapabilities,
} from "@/lib/use-results-capabilities";
import { useResultsQuery } from "@/lib/use-results-query";
import { useResultsQuickQuery } from "@/lib/use-results-quick-query";
import type { ResultItemDTO } from "@/lib/results-api";
import {
  clearResultsRoute,
  isStableResultToken,
  normalizeCanonicalResultsUrl,
  parseResultsRoute,
  writeResultsExplorerRoute,
  writeResultsRoute,
  type ResultsRoute,
  type ResultsRouteTarget,
  type ResultsViewerMode,
} from "@/lib/results-workspace-route";
import {
  createResultsExplorerState,
  createResultsNavigationState,
  DEFAULT_RESULTS_FILTERS,
  readResultsExplorerState,
  readResultsNavigationState,
  withResultsHistoryState,
  withoutResultsHistoryState,
  type ResultsOpeningContext,
  type ResultsHistoryStateV1,
} from "@/lib/results-navigation-state";
import {
  clearRecentResults,
  readRecentResults,
  recordRecentResult,
  removeRecentResult,
  resultsRecentScopeKey,
} from "@/lib/results-recent";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import type { ResultsWorkspaceErrorClass } from "@/lib/results-workspace-errors";
import { trackResultsWorkspace } from "@/lib/results-workspace-metrics";
import { track } from "@/lib/metrics";
import type { ExportFmt } from "@/lib/use-canvas-export";
import { terminalAutoOpenArtifact } from "@/lib/results-terminal-reconciliation";
import {
  projectResultsWorkspace,
  projectResultsDTO,
  resolveSelectedResult,
  type ArtifactResultRole,
  type ResultItem,
} from "@/lib/results-workspace-model";
import { useResultsExportVersions } from "@/lib/use-results-export-versions";
import { formatDateTime, getCurrentLocale } from "@/lib/locale";
import { projectCodeRuns, type CodeRunItem } from "@/lib/code-result-model";
import { withoutNextRouterHistoryMarkers } from "@/lib/url-session-nav";
import { viewerTimeZone } from "@/lib/time-zone";
import {
  ArrowDown as LucideArrowDown,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIcon } from "@/components/char-icon";

/** Three-state split gate (P2 batch 4: from 1024). */
const DESKTOP_SPLIT_QUERY = "(min-width: 1024px)";
/** Shared key of the work area in both desktop layouts, so React keeps the chat pane across a layout switch. */
const WORK_AREA_KEY = "work-area";

type RightTab = "report" | "data" | "sql";
const RESULTS_NAV_PIN_MIN_WIDTH = 860;
const ResultsExplorer = dynamic(
  () => import("./canvas/results-explorer").then((module) => module.ResultsExplorer),
  { ssr: false },
);

type ReportReturnContext = {
  artifactId: string;
  label: string;
};

type PendingResultsSourceRestore = {
  context: Exclude<ResultsOpeningContext, { type: "external" | "explorer" }>;
  sourceElement: HTMLElement | null;
};

function stableTokenOrNull(value: string | null | undefined): string | null {
  return value && isStableResultToken(value) ? value : null;
}

function closestStableToken(
  element: HTMLElement | null,
  attribute: "data-section-id" | "data-results-code-run-key" | "data-results-message-token",
): string | null {
  const value = element?.closest<HTMLElement>(`[${attribute}]`)?.getAttribute(attribute);
  return stableTokenOrNull(value);
}

function elementByStableAttribute(
  attribute: "data-section-id" | "data-results-code-run-key" | "data-results-message-token" | "data-results-focus-token",
  value: string | null,
  root: ParentNode = document,
): HTMLElement | null {
  if (!value) return null;
  return Array.from(root.querySelectorAll<HTMLElement>(`[${attribute}]`)).find(
    (element) => element.getAttribute(attribute) === value,
  ) ?? null;
}

function openingContextMatchesPoppedSource(
  context: ResultsOpeningContext,
  route: ResultsRoute,
  navigationEntryId: string | null,
): context is Exclude<ResultsOpeningContext, { type: "external" | "explorer" }> {
  if (context.type === "report-inspection") {
    return route.type === "report" && navigationEntryId === context.reportEntryId;
  }
  if (context.type === "code") return route.type === "code";
  // Chat-only/split is a shell state, not a Results URL. The target entry was
  // created by a trusted in-page event, so its immediate predecessor is the
  // source even when both entries intentionally share the same URL.
  return context.type === "chat";
}

export function canPinResultsNavigator(
  containerWidth: number,
  mobile: boolean,
): boolean {
  return !mobile && containerWidth >= RESULTS_NAV_PIN_MIN_WIDTH;
}

export function canProjectResultsExplorerRoute({
  resolved,
  scaleNav,
}: Pick<ResultsCapabilities, "resolved" | "scaleNav">): boolean {
  return scaleNav || !resolved;
}

export function explorerOwnsArtifactSelection({
  artifactId,
  scaleNav,
  selectionKey,
  surface,
}: {
  artifactId: string | null;
  scaleNav: boolean;
  selectionKey: string | null | undefined;
  surface: "viewer" | "explorer";
}): boolean {
  return Boolean(
    artifactId
    && scaleNav
    && surface === "explorer"
    && selectionKey === `artifact:${artifactId}`
  );
}

export interface ResultsWorkbenchRouteState {
  tab: RightTab;
  surface: "viewer" | "explorer";
  artifactId: string | null;
  viewerMode: ResultsViewerMode;
  unavailable: boolean;
  opened: boolean;
  revealPane: boolean;
}

type RestoredDraftNotice = "stopped" | "replay" | "replay_references_lost";

/** A restored question that may have carried references (their display text), as a hint only. */
const MAY_HAVE_REFERENCES = /(^|\s)[@/]\S/;

const RESTORED_DRAFT_NOTICE_KEYS = {
  stopped: "stopped_draft_restored",
  replay: "replay_draft_restored",
  replay_references_lost: "replay_draft_restored_references_lost",
} as const;

function composerPrefillKey(attachment: AttachmentRef): string {
  return JSON.stringify([
    attachment.fileId,
    ...(attachment.bundleArtifactIds ?? []),
  ]);
}

export function projectResultsRoute(
  route: ResultsRoute,
  reportOnly: boolean,
  scaleNav = false,
): ResultsWorkbenchRouteState {
  const surface = scaleNav && route.surface === "explorer"
    ? "explorer"
    : "viewer";
  if (route.type === "invalid" || (reportOnly && route.type === "code")) {
    return {
      tab: "report",
      surface,
      artifactId: null,
      viewerMode: "preview",
      unavailable: true,
      opened: true,
      revealPane: true,
    };
  }
  if (route.type === "explorer") {
    return {
      tab: "report",
      surface,
      artifactId: null,
      viewerMode: "preview",
      unavailable: false,
      opened: true,
      revealPane: true,
    };
  }
  if (route.type === "artifact") {
    return {
      tab: "data",
      surface,
      artifactId: route.artifactId,
      viewerMode: route.mode,
      unavailable: false,
      opened: true,
      revealPane: true,
    };
  }
  if (route.type === "code") {
    return {
      tab: "sql",
      surface,
      artifactId: null,
      viewerMode: "preview",
      unavailable: false,
      opened: true,
      revealPane: true,
    };
  }
  if (route.type === "legacy-data") {
    return {
      tab: "data",
      surface: "viewer",
      artifactId: null,
      viewerMode: "preview",
      unavailable: false,
      opened: true,
      revealPane: true,
    };
  }
  return {
    tab: "report",
    surface,
    artifactId: null,
    viewerMode: "preview",
    unavailable: false,
    opened: route.explicit,
    revealPane: route.explicit,
  };
}

export function shouldResetResultsSession(
  previousSessionId: string | null,
  nextSessionId: string | null,
): boolean {
  return previousSessionId !== null && previousSessionId !== nextSessionId;
}

export function reportResultsTitle(
  sections: CanvasState["sections"],
  canvasTitle: string | null | undefined,
  fallback: string,
): string {
  for (const section of sections) {
    if (section.kind !== "h1" || section.superseded) continue;
    // ★ P0 (dev 1.0.77 device dogfooding): `section.text` is **required by its type**
    // (`ReportSection`'s h1 branch declares `text: string`), yet may be missing at runtime.
    // Without `?? ""`, the entire chat route crashes to a blank screen with `route_render_failed`.
    //
    // The missing value comes from `canvas-store.ts` late-join upsert: when FE never saw a section's
    // `section_added` (missed frames on refresh/reconnect), a patch with `kind` is
    // **forced** into a section via `as unknown as ReportSection`; if that patch contains only
    // `kind:"h1"` without `text`, it creates an h1 without text.
    // Repro: propose a plan → **refresh the page** → approve execution. Refresh misses section_added,
    // then canvas-reconcile catches up (observed `drift {localCount:4, serverCount:7}`)
    // → reading undefined.trim() here → blank screen.
    //
    // Only a consumer-side fallback here (skip an h1 without text and continue searching). The unsound
    // cast belongs to canvas-store, another workstream, tracked separately.
    const title = (section.text ?? "").trim();
    if (title) return title;
  }
  return canvasTitle?.trim() || fallback;
}

const DEFAULT_V2_EXPORT_PROFILE = {
  pdf: "full_report",
  pptx: "full_report",
  xlsx: "data_workbook",
  // docx has the same semantics as pdf (one complete report). **docx uses only the legacy job path**:
  // backend `_EXPORT_PARITY_CONTRACTS` excludes docx, so the V2 endpoint rejects it with invalid_format_profile
  // and a clean 400 (V2 itself defaults entirely off). This value merely supplies a valid prepare-stage profile.
  docx: "full_report",
} as const;

interface Props {
  /** Admitted editing objects occupy the existing right work area while Chat stays intact. */
  creationPanel?: React.ReactNode;
  canvasObjectOpener?: React.ReactNode;
  /** Canvas object tab strip with Results selected, shown above Results while objects stay open (C1). */
  canvasObjectTabs?: React.ReactNode;
  /** Authorized document discovery in Results when no report is available. */
  canvasObjectList?: React.ReactNode;
  /** Session-scoped explicit Results selection, including restored document navigation. */
  resultsRevealRequest?: number;
  documentResultsRefresh?: { scope: DocumentCanvasScope; revision: string } | null;
  /** Tab title for the open session (its display title); the canvas title when absent. */
  documentTitle?: string | null;
  creationPanelOpen?: boolean;
  creationPanelTitle?: string;
  /** Number of open Canvas objects; the mobile pane switch reads “Results · N” (C1 narrow navigation). */
  creationPanelCount?: number;
  onShowCreationPanel?: () => void;
  homePane?: React.ReactNode;
  /** Config copilot batch 1-b: ConfigDrawer renderer (provided by host page.tsx). **This component chooses its layout**;
   *  only it knows hasCanvas (R2 review P1-A: computing layout at page level makes column layout unreachable,
   *  while the overlay's fixed scrim covers the entire conversation area). */
  configDrawer?: (layout: "overlay" | "column" | "sheet") => React.ReactNode;
  configDrawerOpen?: boolean;
  runner: TurnRunnerState;
  onSend: (
    text: string,
    attachments?: import("@/lib/types").AttachmentRef[],
    draftRevision?: string | null,
    submissionContext?: ComposerDraftSubmissionContext | null,
  ) => void | Promise<boolean>;
  /** Continue button under a partial answer: resume these saved helper runs. Omitted in read-only views. */
  onContinue?: (checkpointIds: string[]) => void | Promise<boolean>;
  onResolveDraftSubmissionProtocol?: (
    actionKind: "start" | "steer",
    attachments: AttachmentRef[],
    hasReferenceInput?: boolean,
  ) => Promise<ComposerDraftWriteProtocol | null>;
  onCaptureSubmissionWireSnapshot?: (actionKind: "start" | "steer", hasReferenceInput: boolean, documentRevisionTarget?: DocumentRevisionTarget) => ComposerSubmissionWireSnapshot | null;
  /** Stop: parent persists a stable cancel request and awaits the terminal SSE frame. */
  onCancel?: () => void | boolean | Promise<void | boolean>;
  stopRequested?: boolean;
  /** Phase 1.4 Steer mode — when runner.running, composer routes user text
   *  through this instead of starting a fresh turn. Backend buffers into
   *  the active turn's steer queue + emits 🤝 emoji_ack. */
  onSteer?: (
    text: string,
    attachments?: AttachmentRef[],
    draftRevision?: string | null,
    submissionContext?: ComposerDraftSubmissionContext | null,
  ) => void | boolean | Promise<void | boolean>;
  /** The server cannot replay (no submission protocol): Replay puts the last question back in the box instead. */
  replayRestoresDraft?: boolean;
  /** The structured input of this page's last send, so a restored question keeps its real @ references. */
  lastReferenceDocument?: InputDocument | null;
  onReplay?: (
    sourceTurnId: string,
    previewText: string,
    previewAttachments: AttachmentRef[],
  ) => Promise<boolean>;
  /** Licensing §13.2 — entitlement read-only (over_grace / suspended); threads to the composer. */
  readOnly?: boolean;
  /** Shared-report fallback: viewer can read the report canvas, not the owner session workspace. */
  reportOnly?: boolean;
  /** report-open (open your own report from My Reports): initially maximize the report, but **retain** the left `对话` reopen strip
   *  + maximize/close buttons, unlike reportOnly (shared read-only, no strip or conversation to return to). User report: opening a report
   *  should mean `chat 收起成细边` rather than `chat 消失/真全屏` (dogfood-0708). */
  startMaximized?: boolean;
  /** workflow-solidify surface B: open the 固化 card for a completed analysis turn (chip in TurnContainer). */
  onSolidify?: (turnId: string) => void;
  /** scheduled-recipe-runs S2: solidify the entire conversation (forwarded to TurnContainer's prop of the same name). */
  onSolidifySession?: (sessionId: string) => void;
  /** Prefilled attachments from `在新会话中使用` (done-state chip), passed in after the page consumes the one-shot channel. */
  prefillAttachments?: AttachmentRef[] | null;
  /** Pending text recovered from durable submission recovery after F5. */
  prefillText?: string | null;
  /** Whether the composer took the prefill or kept the Owner's own draft. */
  onPrefillConsumed?: (disposition?: "applied" | "discarded") => void;
  /** Each new value sends the composer's current input through its ordinary Send path. */
  composerSubmitRequest?: number;
  /** A structured message (e.g. "@Coach …") staged into an empty composer; a draft the user is writing is kept. */
  prefillInputDocument?: InputDocument | null;
  onPrefillInputDocumentConsumed?: (disposition: "applied" | "discarded") => void;
  submissionRecoveryPending?: boolean;
  submissionRecoveryVariant?: "submission" | "terminalAction";
  submissionRecoveryAllowAbandon?: boolean;
  submissionRecoveryLocked?: boolean;
  draftPersistenceScope?: ComposerDraftScope | null;
  draftControlRef?: React.RefObject<ComposerDraftControl | null>;
  composerRevealRequest?: { id: string; scopeKey: string } | null;
  onComposerRevealConsumed?: (id: string) => void;
  onPendingActionHydrated?: (draft: ComposerOrdinaryDraft) => void;
  draftReconcileEpoch?: number;
  draftPersistenceFailureMode?: "block" | "warn";
  onRetryRecoveredSubmission?: () => Promise<boolean>;
  onAbandonRecoveredSubmission?: () => Promise<boolean>;
}

// Batch3/c3 (discovery C15): rename FE shell Workspace→ChatWorkbench to avoid a name collision;
// this chat/canvas split shell is unrelated to tenant Space, the fourth of five meanings of the overloaded term.
export function ChatWorkbench({ runner, onSend, onContinue, onResolveDraftSubmissionProtocol, onCaptureSubmissionWireSnapshot, onCancel, stopRequested = false, onSteer, onReplay, replayRestoresDraft = false, lastReferenceDocument = null, readOnly, reportOnly = false, startMaximized = false, onSolidify, onSolidifySession, prefillAttachments = null, prefillText = null, onPrefillConsumed, composerSubmitRequest, prefillInputDocument = null, onPrefillInputDocumentConsumed, submissionRecoveryPending = false, submissionRecoveryVariant = "submission", submissionRecoveryAllowAbandon = true, submissionRecoveryLocked = false, draftControlRef, composerRevealRequest = null, onComposerRevealConsumed, draftPersistenceScope = null, onPendingActionHydrated, draftReconcileEpoch = 0, draftPersistenceFailureMode = "block", onRetryRecoveredSubmission, onAbandonRecoveredSubmission, configDrawer, configDrawerOpen = false, creationPanel, creationPanelOpen = false, creationPanelTitle, creationPanelCount, onShowCreationPanel, homePane, canvasObjectOpener, canvasObjectTabs, canvasObjectList, resultsRevealRequest = 0, documentResultsRefresh = null, documentTitle = null }: Props) {
  const t = useTranslations("canvas");
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  const tCreateAgent = useTranslations("create_subagent");
  const tChat = useTranslations("chat");
  const tComposer = useTranslations("composer");
  // chat-ux-2026-08 batch C: viewport following. enabled is true only while this turn **is still running**:
  // stop forcing bottom alignment when the turn ends; users may be reading, so do not fight them.
  const stick = useStickToBottom(Boolean(runner.running));
  // New progress counts steps and answer start, **not character growth**, or the float's count would keep jumping.
  const progressTickRef = useRef<{ steps: number; answered: boolean }>({
    steps: 0,
    answered: false,
  });
  useEffect(() => {
    const steps = runner.traceSteps?.length ?? 0;
    const answered = Boolean(runner.answer);
    const prev = progressTickRef.current;
    let delta = Math.max(0, steps - prev.steps);
    if (answered && !prev.answered) delta += 1;
    progressTickRef.current = { steps, answered };
    for (let i = 0; i < delta; i++) stick.notePendingProgress();
  }, [runner.traceSteps?.length, runner.answer, stick]);
  const resultsT = useTranslations("results_workspace");
  const authSession = useOptionalAuthSession();
  // chat-redesign Phase 1.7-B (tech-design §4.6): SteerMessage relocates
  // OUT of TurnContainer's BotTurn flow and pins above the composer per
  // v8 `variant-a.jsx:720-736`. Subscribe via the dedicated hook so this
  // shell doesn't reach into turn-store directly.
  const steerAcks = useSteerAcks(runner.turnId);
  // ④a (bug 0708): while THIS turn's plan is awaiting approval, the plan card
  // shows its own amend input — hide the bottom main composer so two input
  // boxes don't stack. Turn-scoped so a proposed plan in another session can't
  // blank the composer here. Approve/cancel flips it out of "proposed" → back.
  const planAwaitingApproval = useTurnAwaitingPlanApproval(runner.turnId);
  const sessionTurnIds = useMemo(() => Array.from(new Set([
    ...(runner.archivedTurns ?? []).map((turn) => turn.turnId).filter((id): id is string => Boolean(id)),
    ...(runner.turnId ? [runner.turnId] : []),
  ])), [runner.archivedTurns, runner.turnId]);
  // Persisted step events are the shared live/F5 source for Results > Code.
  const pythonExecutionSteps = useSessionPythonSteps(sessionTurnIds);
  const runningPySteps = pythonExecutionSteps.filter((step) => step.state === "active");
  const canvas = useSyncExternalStore<CanvasState>(
    subscribeCanvas,
    getCanvasSnapshot,
    getCanvasSnapshot,
  );
  // ★ Must use memo: computing directly during render invalidates the entire downstream memo chain, eventually triggering
  // report-view's setState-in-effect and infinite rerenders (production React #185;
  // see the root-cause chain in lib/use-canvas-derived-sections.ts).
  const reportSections = useReportSurfaceSections(canvas);
  // Same pattern: previously, inline `selectVisibleSections(canvas)` in JSX created a new array on every render.
  const exportVisibleSections = useVisibleSections(canvas);
  const hasCanvas = reportSections.length > 0;
  const reportSessionId = runner.sessionId ?? canvas.sessionId ?? null;
  const [resultsNavigatorQuery, setResultsNavigatorQuery] = useState("");
  const [resultsQuickQuery, setResultsQuickQuery] = useState("");
  const [resultsQuickOpeningContext, setResultsQuickOpeningContext] =
    useState<ResultsOpeningContext>({ type: "external" });
  const [resultsQuickHistory, setResultsQuickHistory] =
    useState<"push" | "replace">("push");
  const [resultsSurface, setResultsSurface] =
    useState<"viewer" | "explorer">("viewer");
  const [resultsExplorerState, setResultsExplorerState] =
    useState<ResultsHistoryStateV1 | null>(null);
  const resultsCapabilities = useResultsCapabilities();
  const resultsWorkspaceEnabled = resultsCapabilities.workspace;
  const resultsExplorerRouteSupported = canProjectResultsExplorerRoute(
    resultsCapabilities,
  );
  const resultsCapabilityUnresolved = !resultsCapabilities.resolved;
  const resultsWorkspaceShellEnabled = resultsWorkspaceEnabled
    || resultsCapabilityUnresolved;
  const resultsArtifactScope = reportOnly ? "report" : "session";
  const resultsArtifactSessionId = resultsWorkspaceEnabled
    ? (reportOnly ? reportSessionId : runner.sessionId)
    : null;
  const resultsRecentScope = resultsArtifactSessionId
    ? resultsRecentScopeKey({
        principalId: authSession?.me?.id ?? "anonymous",
        sessionId: resultsArtifactSessionId,
        scope: resultsArtifactScope,
      })
    : "";
  const [resultsRecentKeys, setResultsRecentKeys] = useState<string[]>([]);
  useEffect(() => {
    setResultsRecentKeys(
      resultsRecentScope ? readRecentResults(resultsRecentScope) : [],
    );
  }, [resultsRecentScope]);
  const completeResults = useResultsQuery({
    enabled: resultsCapabilities.completeList,
    entryId: resultsSurface === "explorer" ? resultsExplorerState?.entry_id : null,
    selectionKey: resultsSurface === "explorer"
      ? resultsExplorerState?.selected_key ?? resultsExplorerState?.anchor?.key ?? null
      : null,
    filters: resultsCapabilities.scaleNav && resultsSurface === "explorer"
      ? resultsExplorerState?.filters ?? DEFAULT_RESULTS_FILTERS
      : DEFAULT_RESULTS_FILTERS,
    query: resultsCapabilities.scaleNav && resultsSurface === "explorer"
      ? resultsExplorerState?.query ?? ""
      : resultsNavigatorQuery,
    scope: resultsArtifactScope,
    sessionId: resultsArtifactSessionId,
  });
  const useLegacyResults = resultsWorkspaceEnabled && (
    !resultsCapabilities.completeList
    || !resultsArtifactSessionId
    || completeResults.fallbackAllowed
  );
  const {
    items: resultArtifacts,
    error: resultArtifactsError,
    retry: retryResultArtifacts,
    refresh: refreshResultArtifacts,
  } = useArtifactItems(
    resultsArtifactSessionId,
    resultsArtifactScope,
    "",
    useLegacyResults,
  );
  const {
    groups: exportVersionGroups,
    refresh: refreshExportVersions,
  } = useResultsExportVersions(
    canvas.canvasId,
    useLegacyResults && !reportOnly,
  );
  const [tab, setTab] = useState<RightTab>("report");
  const selectedTab: RightTab = reportOnly && tab === "sql" ? "report" : tab;
  const [dataFocusArtifactId, setDataFocusArtifactId] = useState<string | null>(null);
  const [artifactViewerMode, setArtifactViewerMode] =
    useState<ResultsViewerMode>("preview");
  const [resultsOpeningContext, setResultsOpeningContext] =
    useState<ResultsOpeningContext>({ type: "external" });
  const resultsOpeningContextRef = useRef<ResultsOpeningContext>(resultsOpeningContext);
  resultsOpeningContextRef.current = resultsOpeningContext;
  const [resultsRouteUnavailable, setResultsRouteUnavailable] = useState(false);
  const [resultsNavigatorOpen, setResultsNavigatorOpen] = useState(false);
  const [resultsMobileListOpen, setResultsMobileListOpen] = useState(false);
  const [resultsPinPreferred, setResultsPinPreferred] = useState(false);
  const [resultsOpenedByUser, setResultsOpenedByUser] = useState(false);
  const [resultsPaneWidth, setResultsPaneWidth] = useState(0);
  const [reportReturnContext, setReportReturnContext] =
    useState<ReportReturnContext | null>(null);
  const [resultReferencePrefills, setResultReferencePrefills] = useState<AttachmentRef[]>([]);
  const [consumedComposerPrefillKeys, setConsumedComposerPrefillKeys] = useState<string[]>([]);
  const resultsTriggerRef = useRef<HTMLButtonElement | null>(null);
  const resultsCurrentTitleRef = useRef<HTMLElement | null>(null);
  const resultsMobileBackRef = useRef<HTMLButtonElement | null>(null);
  const resultsWorkspaceBodyRef = useRef<HTMLDivElement | null>(null);
  const reportReturnFocusRef = useRef<HTMLElement | null>(null);
  const resultsSourceFocusRef = useRef<HTMLElement | null>(null);
  const resultsClickSourceRef = useRef<HTMLElement | null>(null);
  const pendingResultsSourceRestoreRef = useRef<PendingResultsSourceRestore | null>(null);
  const codeScrollTopRef = useRef(0);
  const [resultsSourceRestoreEpoch, setResultsSourceRestoreEpoch] = useState(0);
  const pendingReportReturnFocusRef = useRef<HTMLElement | null>(null);
  const pendingReportReturnRef = useRef(false);
  const pendingReportHistoryEntryRef = useRef<string | null>(null);
  const previousResultsNavigatorPinnedRef = useRef(false);
  const previousResultsSessionRef = useRef(reportSessionId);
  const previousRunnerRunningRef = useRef(false);
  const turnBaselineKeysRef = useRef<Set<string>>(new Set());
  const baselineTurnIdRef = useRef<string | null>(null);
  const terminalPaneClosedAtStartRef = useRef(false);
  const resultsClosedDuringTurnRef = useRef(false);
  const resultsInteractionEpochRef = useRef(0);
  const handledTerminalTurnIdRef = useRef<string | null>(null);
  const activeResultsSessionRef = useRef(resultsArtifactSessionId);
  const refreshedExportJobRef = useRef<string | null>(null);
  activeResultsSessionRef.current = resultsArtifactSessionId;
  // ⑧ (bug 0708): the Report/Data/Code tab body is a ternary that UNMOUNTS
  // ReportView on tab switch, and .v2-ws-right-body's scrollTop collapses to 0
  // under the shorter Data content — so "打开数据" → back-to-Report loses the
  // reader's scroll position. Save scrollTop while on Report, restore on return.
  const reportBodyRef = useRef<HTMLDivElement | null>(null);
  const reportScrollTopRef = useRef(0);
  // WP1 export loop: shared hook (start/poll/timeout/retry) + export-record drawer
  const exportCtl = useCanvasExport();
  const reportViewRef = useRef<ReportViewHandle | null>(null);
  const [reportEditControlsHost, setReportEditControlsHost] =
    useState<HTMLDivElement | null>(null);
  // Appearance refresh batch 4b: toolbar status group mount point (writing progress / status badge) and share-dialog toggle.
  const [reportStatusHost, setReportStatusHost] = useState<HTMLDivElement | null>(null);
  const [reportShareOpen, setReportShareOpen] = useState(false);
  const reportShareButtonRef = useRef<HTMLButtonElement | null>(null);
  // ReportView reports editing state; mobile reading toolbar applies only outside editing.
  const [reportEditing, setReportEditing] = useState(false);
  // Focus target after exiting edit (clicking `完成`): mobile reading no longer renders the edit group or that button, dropping focus to the page;
  // move focus to a surviving toolbar button (`更多操作`, which contains edit; on wide screens, `编辑此报告`).
  const reportEditingPrevRef = useRef(false);
  useEffect(() => {
    const was = reportEditingPrevRef.current;
    reportEditingPrevRef.current = reportEditing;
    if (!was || reportEditing) return;
    const frame = requestAnimationFrame(() => {
      // Do not steal focus from an actionable control (the user moved it); recover only from page/container focus (body in Chromium, main in WebKit).
      const active = document.activeElement;
      if (active?.isConnected && active.matches("button, a[href], input, textarea, select, [contenteditable='true']")) return;
      const bar = document.querySelector<HTMLElement>(".results-report-object-toolbar");
      const target = bar?.querySelector<HTMLElement>(".results-report-edit-controls .v2-edit-toggle-btn:not(:disabled)")
        ?? (moreMenu.refs.domReference.current as HTMLElement | null);
      target?.focus();
    });
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportEditing]);
  const [exportPreparing, setExportPreparing] = useState(false);
  const [exportsDrawerOpen, setExportsDrawerOpen] = useState(false);
  // layout-redesign v10 canvas header: two header dropdowns (export · more).
  // Pure chrome state — the underlying export/clear/delete/history handlers are unchanged.
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  // useMenu handles keyboard, Esc, outside clicks, and focus return. Selecting a menu item returns focus to the trigger before running its action,
  // so dialogs opened by a menu item naturally return to `更多操作` on close, without a separate return-focus target.
  const exportMenu = useMenu({
    open: exportMenuOpen,
    onOpenChange: (open) => {
      if (open) setMoreMenuOpen(false);
      setExportMenuOpen(open);
    },
    placement: "bottom-end",
  });
  const moreMenu = useMenu({
    open: moreMenuOpen,
    onOpenChange: (open) => {
      if (open) setExportMenuOpen(false);
      setMoreMenuOpen(open);
    },
    placement: "bottom-end",
  });
  // Export records also open from the export-progress notice (without the export menu); closing returns focus to `导出`.
  const exportButtonRef = useRef<HTMLButtonElement>(null);
  const exportTriggerRef = useMergeRefs([exportButtonRef, exportMenu.refs.setReference]);
  useEffect(() => {
    if (selectedTab === "report") return;
    setExportMenuOpen(false);
    setMoreMenuOpen(false);
  }, [selectedTab]);
  // layout-redesign P3: three-state split panelMode replaces the old canvasMaximized boolean.
  // split = draggable chat|canvas split · chat = conversation only (report becomes a reopenable right strip) ·
  // canvas = maximized report (46px left strip reopens conversation). Derive canvasMaximized so all existing consumers
  // (canvas-maxed class / mobile-tab active / display:none / maximize icon) require no changes.
  // Feishu 07.06 #26: shared read-only reports have no conversation → initially maximize the report (canvas),
  // otherwise an empty conversation strip remains at left and the report is not full-width.
  const [panelMode, setPanelMode] = useState<"split" | "chat" | "canvas">(
    reportOnly || startMaximized ? "canvas" : "split",
  );
  const canvasMaximized = panelMode === "canvas";
  // Batch 1-b (tech-design-batch1b §4.3 decision): collapsed canvas (panelMode="chat") has a canvas column with
  // display:none and no box, leaving overlay drawers nowhere to mount; opening forces split to restore canvas and drawer together
  // (closing returns to the report consistently; closing the drawer does not restore the original panelMode).
  useEffect(() => {
    if (!configDrawerOpen) return;
    setPanelMode((mode) => (mode === "chat" ? "split" : mode));
  }, [configDrawerOpen]);
  useEffect(() => {
    if (creationPanelOpen) {
      setPanelMode(
        window.matchMedia(DESKTOP_SPLIT_QUERY).matches ? "split" : "canvas",
      );
    }
  }, [creationPanelOpen]);
  useEffect(() => {
    const captureClickSource = (event: MouseEvent) => {
      const source = event.target instanceof Element
        ? event.target.closest<HTMLElement>(
            "button, a[href], input, textarea, select, [tabindex], [data-section-id]",
          )
        : null;
      resultsClickSourceRef.current = source;
      queueMicrotask(() => {
        if (resultsClickSourceRef.current === source) {
          resultsClickSourceRef.current = null;
        }
      });
    };
    // Safari does not consistently move document.activeElement to a clicked
    // button. Capture-phase click still gives the exact trusted control before
    // its React onClick dispatches the Results event. Keyboard activation also
    // produces the same click path.
    document.addEventListener("click", captureClickSource, true);
    return () => document.removeEventListener("click", captureClickSource, true);
  }, []);
  const ensureCurrentResultsNavigation = useCallback(() => {
    if (!resultsWorkspaceEnabled || !reportSessionId) return null;
    const expected = {
      sessionId: reportSessionId,
      scope: resultsArtifactScope,
    } as const;
    const current = readResultsNavigationState(window.history.state, expected);
    if (current) return current;
    const navigation = createResultsNavigationState(expected);
    const explorer = readResultsExplorerState(window.history.state, expected);
    window.history.replaceState(
      withoutNextRouterHistoryMarkers(withResultsHistoryState(window.history.state, navigation, explorer)),
      "",
      window.location.href,
    );
    return navigation;
  }, [reportSessionId, resultsArtifactScope, resultsWorkspaceEnabled]);
  const captureResultsOpeningContext = useCallback((
    entryPoint: CanvasFocusDataDetail["entryPoint"],
    reportContext?: CanvasFocusDataDetail["reportContext"],
  ): ResultsOpeningContext | undefined => {
    const activeElement = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const clickedElement = resultsClickSourceRef.current;
    resultsClickSourceRef.current = null;
    const sourceMarker = entryPoint === "report"
      ? "data-section-id" as const
      : entryPoint === "code"
        ? "data-results-code-run-key" as const
        : entryPoint === "chat"
          ? "data-results-message-token" as const
          : null;
    const sourceElement = !sourceMarker
      ? null
      : closestStableToken(clickedElement, sourceMarker)
        ? clickedElement
        : closestStableToken(activeElement, sourceMarker)
          ? activeElement
          : null;
    if (entryPoint === "report") {
      reportReturnFocusRef.current = sourceElement;
      resultsSourceFocusRef.current = null;
      const reportEntry = ensureCurrentResultsNavigation();
      if (!reportEntry) return undefined;
      const sectionToken = stableTokenOrNull(reportContext?.sectionId)
        ?? closestStableToken(sourceElement, "data-section-id");
      return {
        type: "report-inspection",
        reportEntryId: reportEntry.entry_id,
        anchor: sectionToken,
        focusToken: sectionToken,
      };
    }
    if (entryPoint === "code") {
      const runKey = closestStableToken(sourceElement, "data-results-code-run-key");
      if (!runKey) return undefined;
      reportReturnFocusRef.current = null;
      resultsSourceFocusRef.current = sourceElement;
      codeScrollTopRef.current = reportBodyRef.current?.scrollTop ?? 0;
      return {
        type: "code",
        runKey,
        focusToken: stableTokenOrNull(
          sourceElement?.closest<HTMLElement>("[data-results-focus-token]")
            ?.dataset.resultsFocusToken,
        ),
      };
    }
    if (entryPoint === "chat") {
      reportReturnFocusRef.current = null;
      resultsSourceFocusRef.current = sourceElement;
      return {
        type: "chat",
        messageToken: closestStableToken(sourceElement, "data-results-message-token"),
        panelMode: panelMode === "split" ? "split" : "chat",
      };
    }
    reportReturnFocusRef.current = null;
    resultsSourceFocusRef.current = null;
    return undefined;
  }, [ensureCurrentResultsNavigation, panelMode]);
  const applyResultsRoute = useCallback((
    route: ResultsRoute,
    navigation: ReturnType<typeof readResultsNavigationState> = null,
  ) => {
    const next = projectResultsRoute(
      route,
      reportOnly,
      resultsExplorerRouteSupported,
    );
    setResultsNavigatorOpen(false);
    setResultsMobileListOpen(false);
    setResultsOpeningContext(
      navigation?.opening_context ?? { type: "external" },
    );
    setResultsSurface(next.surface);
    setDataFocusArtifactId(next.artifactId);
    setArtifactViewerMode(next.viewerMode);
    setResultsRouteUnavailable(next.unavailable);
    setResultsOpenedByUser(next.opened);
    setTab(next.tab);
    if (next.revealPane) {
      // Explicit object navigation is user intent. Without a report surface,
      // open Results directly rather than leaving an empty split column.
      setPanelMode((mode) => (
        hasCanvas && window.matchMedia?.(DESKTOP_SPLIT_QUERY).matches ? (mode === "chat" ? "split" : mode) : "canvas"
      ));
    }
  }, [hasCanvas, reportOnly, resultsExplorerRouteSupported]);
  const navigateToResult = useCallback((
    target: ResultsRouteTarget,
    history: "push" | "replace" = "push",
    openingContext?: ResultsOpeningContext,
    preserveSourceEntry = false,
  ) => {
    resultsInteractionEpochRef.current += 1;
    if (!resultsWorkspaceEnabled) {
      applyResultsRoute(
        target.type === "artifact"
          ? {
              type: "artifact",
              artifactId: target.artifactId,
              mode: target.mode ?? "preview",
              explicit: true,
              source: "legacy",
              surface: "viewer",
            }
          : target.type === "code"
            ? {
                type: "code",
                explicit: true,
                source: "legacy",
                surface: "viewer",
              }
            : {
                type: "report",
                explicit: true,
                source: "legacy",
                surface: "viewer",
              },
      );
      return;
    }
    const currentNavigation = ensureCurrentResultsNavigation();
    const currentRoute = parseResultsRoute(
      new URL(window.location.href).searchParams,
    );
    const explorerActive = Boolean(
      resultsCapabilities.scaleNav
      && currentRoute.surface === "explorer",
    );
    const expected = reportSessionId
      ? { sessionId: reportSessionId, scope: resultsArtifactScope } as const
      : null;
    let explorer = explorerActive && expected
      ? readResultsExplorerState(window.history.state, expected)
      : null;
    if (explorerActive && expected && !explorer) {
      explorer = createResultsExplorerState(expected);
    }
    let nextUrl: URL;
    try {
      nextUrl = explorerActive
        ? writeResultsExplorerRoute(
            new URL(window.location.href),
            target,
            reportSessionId,
          )
        : writeResultsRoute(
            new URL(window.location.href),
            target,
            reportSessionId,
          );
    } catch {
      applyResultsRoute({
        type: "invalid",
        explicit: true,
        source: "canonical",
        surface: explorerActive ? "explorer" : "viewer",
      });
      return;
    }
    if (explorer) {
      explorer = {
        ...explorer,
        selected_key: target.type === "artifact"
          ? `artifact:${target.artifactId}`
          : target.type,
      };
    }
    const nextUrlString = nextUrl.toString();
    const effectiveHistory = nextUrlString === window.location.href && !preserveSourceEntry
      ? "replace"
      : history;
    const resolvedOpeningContext = openingContext
      ?? (explorer
        ? {
            type: "explorer" as const,
            explorerEntryId: explorer.entry_id,
            focusKey: explorer.focus_key,
          }
        : effectiveHistory === "replace" && currentNavigation
          ? currentNavigation.opening_context
          : { type: "external" as const });
    const navigation = expected
      ? createResultsNavigationState({
          ...expected,
          entryId: effectiveHistory === "replace"
            ? currentNavigation?.entry_id
            : undefined,
          openingContext: resolvedOpeningContext,
        })
      : null;
    const nextState = withResultsHistoryState(window.history.state, navigation, explorer);
    if (effectiveHistory === "push") {
      window.history.pushState(withoutNextRouterHistoryMarkers(nextState), "", nextUrlString);
    } else {
      window.history.replaceState(withoutNextRouterHistoryMarkers(nextState), "", nextUrlString);
    }
    setResultsExplorerState(explorer);
    applyResultsRoute(
      parseResultsRoute(nextUrl.searchParams),
      navigation,
    );
  }, [
    applyResultsRoute,
    ensureCurrentResultsNavigation,
    reportSessionId,
    resultsArtifactScope,
    resultsCapabilities.scaleNav,
    resultsWorkspaceEnabled,
  ]);
  const clearResultsLocation = useCallback((
    history: "push" | "replace",
  ) => {
    const currentUrl = new URL(window.location.href);
    const nextUrl = clearResultsRoute(currentUrl, reportSessionId);
    const method = nextUrl.toString() === currentUrl.toString()
      ? "replaceState"
      : history === "push"
        ? "pushState"
        : "replaceState";
    const nextState = withoutResultsHistoryState(window.history.state);
    if (method === "pushState") {
      window.history.pushState(withoutNextRouterHistoryMarkers(nextState), "", nextUrl.toString());
    } else {
      window.history.replaceState(withoutNextRouterHistoryMarkers(nextState), "", nextUrl.toString());
    }
    return nextUrl;
  }, [reportSessionId]);
  // Desktop gate: the three-state split starts at 1024 (P2 batch 4); smaller / no report → grid + mobile-tab path. Below 1280 the
  // sidebar collapses to its 56 rail while the results pane is open, which leaves room for the 360 + 420 minimums.
  const isDesktop = useMediaQuery(DESKTOP_SPLIT_QUERY);
  useEffect(() => {
    // Preserve an explicitly addressed result when split view becomes a single pane.
    // A deliberate Conversation selection remains authoritative.
    if (isDesktop || window.matchMedia?.(DESKTOP_SPLIT_QUERY).matches) return;
    if (!parseResultsRoute(new URL(window.location.href).searchParams).explicit) return;
    setPanelMode((mode) => mode === "split" ? "canvas" : mode);
  }, [isDesktop]);
  const isResultsMobile = useMediaQuery("(max-width: 640px)");
  useLayoutEffect(() => {
    if (!resultsWorkspaceEnabled) return;
    const element = resultsWorkspaceBodyRef.current;
    if (!element) return;
    const update = () => setResultsPaneWidth(element.getBoundingClientRect().width);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [resultsWorkspaceEnabled]);
  const resultsCanPin = Boolean(
    resultsWorkspaceEnabled
    && !resultsCapabilities.scaleNav
    && canPinResultsNavigator(resultsPaneWidth, isResultsMobile),
  );
  const resultsNavigatorPinned = !resultsCapabilities.scaleNav
    && resultsPinPreferred
    && resultsCanPin;
  useEffect(() => {
    const wasPinned = previousResultsNavigatorPinnedRef.current;
    previousResultsNavigatorPinnedRef.current = resultsNavigatorPinned;
    if (
      wasPinned
      && !resultsNavigatorPinned
      && resultsPinPreferred
      && !isResultsMobile
      && !resultsCapabilities.scaleNav
    ) {
      setResultsNavigatorOpen(true);
    }
  }, [
    isResultsMobile,
    resultsCapabilities.scaleNav,
    resultsNavigatorPinned,
    resultsPinPreferred,
  ]);
  useEffect(() => {
    try {
      setResultsPinPreferred(
        window.localStorage.getItem("bowhead:results-navigator-pinned") === "1",
      );
    } catch {
      setResultsPinPreferred(false);
    }
  }, []);
  const setResultsNavigatorPin = useCallback((pinned: boolean) => {
    setResultsPinPreferred(pinned);
    setResultsNavigatorOpen(false);
    try {
      window.localStorage.setItem(
        "bowhead:results-navigator-pinned",
        pinned ? "1" : "0",
      );
    } catch {
      // Storage is an optional device preference; layout remains functional.
    }
    trackResultsWorkspace({
      action: pinned ? "navigator_pinned" : "navigator_unpinned",
      entryPoint: "header",
    });
  }, []);
  useEffect(() => {
    if (resultsNavigatorPinned) setResultsNavigatorOpen(false);
  }, [resultsNavigatorPinned]);
  // P6 (design D7) previously made mobile reports entirely read-only at width ≤640 for two reasons: touch has no hover
  // affordance, and narrow-screen editing was fragmented (selection toolbar / block menu would not fit).
  // mobile-tech-design r1 resolved both: touch gets persistent ⋯ and editable-state hints
  // (useTouchAffordance tests hover capability rather than width, also covering 641-1023 tablets);
  // touch hides the selection toolbar and uses the block action sheet for AI rewrites, so remove this editing gate entirely.
  const hadCreationPanel = useRef(Boolean(creationPanel));
  const documentOpenerRef = useRef<HTMLSpanElement>(null);
  const workbenchFocusRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const original = workbenchFocusRef.current;
    if (creationPanel || !canvasObjectOpener || !original || original.isConnected || document.activeElement !== document.body) return;
    const opener = documentOpenerRef.current?.querySelector<HTMLButtonElement>("button");
    const workspace = documentOpenerRef.current?.closest(".v2-workspace");
    const target = opener?.getClientRects().length ? opener
      : workspace?.querySelector<HTMLButtonElement>(".v2-ws-edge--canvas")
        ?? workspace?.querySelector<HTMLButtonElement>(".v2-ws-mobile-tab:last-child");
    target?.focus({ preventScroll: true });
  }, [Boolean(creationPanel), Boolean(canvasObjectOpener)]);
  // P3: when the canvas underneath changes (session switch, or a turn that opens a NEW report),
  // snap back to split so a freshly hydrated/generated report is never left hidden behind a
  // collapsed edge (review P1). canvas.canvasId is stable across appends to the same report.
  useEffect(() => {
    // An admitted editor owns opening its pane; report hydration must not hide it on compact screens.
    const closingEditor = hadCreationPanel.current && !creationPanel;
    hadCreationPanel.current = Boolean(creationPanel);
    if (creationPanel || (closingEditor && documentOpenerRef.current)) return;
    // Feishu 07.06 #26: reportOnly (shared read-only) always maximizes the report.
    // startMaximized (open your own report from My Reports) also starts maximized, but soloCanvas=reportOnly
    // remains false → retain the 46px left `对话` reopen strip + maximize/close buttons, allowing return to conversation anytime.
    const explicitResult = parseResultsRoute(new URL(window.location.href).searchParams).explicit;
    const compactResult = explicitResult && !window.matchMedia?.(DESKTOP_SPLIT_QUERY).matches;
    setPanelMode(reportOnly || startMaximized || compactResult ? "canvas" : "split");
  }, [canvas.canvasId, reportOnly, startMaximized, Boolean(creationPanel)]);
  useEffect(() => {
    const applyLocation = (expectedReportEntryId: string | null = null) => {
      const currentUrl = new URL(window.location.href);
      const normalizedUrl = resultsWorkspaceEnabled
        ? normalizeCanonicalResultsUrl(currentUrl)
        : currentUrl;
      const expected = reportSessionId
        ? { sessionId: reportSessionId, scope: resultsArtifactScope } as const
        : null;
      let navigation = expected
        ? readResultsNavigationState(window.history.state, expected)
        : null;
      const route = parseResultsRoute(normalizedUrl.searchParams);
      let explorer = expected && resultsExplorerRouteSupported && route.surface === "explorer"
        ? readResultsExplorerState(window.history.state, expected)
        : null;
      if (resultsWorkspaceEnabled && expected && !navigation) {
        navigation = createResultsNavigationState(expected);
      }
      if (
        resultsWorkspaceEnabled
        && expected
        && resultsExplorerRouteSupported
        && route.surface === "explorer"
        && !explorer
      ) {
        explorer = createResultsExplorerState(expected);
      }
      if (
        explorer && expected
        && (route.type === "artifact" || route.type === "report" || route.type === "code")
      ) {
        explorer = {
          ...explorer,
          selected_key: route.type === "artifact"
            ? `artifact:${route.artifactId}`
            : route.type === "code" ? `code:${expected.sessionId}`
            : canvas.canvasId ? `report:${canvas.canvasId}` : null,
        };
      }
      if (
        expectedReportEntryId
        && (
          route.type !== "report"
          || navigation?.entry_id !== expectedReportEntryId
        )
      ) {
        const fallbackUrl = writeResultsRoute(
          normalizedUrl,
          { type: "report" },
          reportSessionId,
        );
        const fallbackNavigation = expected
          ? createResultsNavigationState({
              ...expected,
              entryId: navigation?.entry_id,
            })
          : null;
        window.history.replaceState(
          withoutNextRouterHistoryMarkers(withResultsHistoryState(
            window.history.state,
            fallbackNavigation,
            null,
          )),
          "",
          fallbackUrl.toString(),
        );
        setResultsExplorerState(null);
        applyResultsRoute(
          parseResultsRoute(fallbackUrl.searchParams),
          fallbackNavigation,
        );
        setReportReturnContext(null);
        return;
      }
      if (
        normalizedUrl.toString() !== currentUrl.toString()
        || (resultsWorkspaceEnabled && expected)
      ) {
        window.history.replaceState(
          withoutNextRouterHistoryMarkers(resultsWorkspaceEnabled && expected
            ? withResultsHistoryState(
                window.history.state,
                navigation,
                explorer,
              )
            : window.history.state),
          "",
          normalizedUrl.toString(),
        );
      }
      setResultsExplorerState(
        route.surface === "explorer" ? explorer : null,
      );
      applyResultsRoute(route, navigation);
      if (route.type === "report") setReportReturnContext(null);
    };
    applyLocation();
    const onPopState = () => {
      resultsInteractionEpochRef.current += 1;
      const departingContext = resultsOpeningContextRef.current;
      const expectedReportEntryId = pendingReportHistoryEntryRef.current;
      pendingReportHistoryEntryRef.current = null;
      const poppedUrl = resultsWorkspaceEnabled
        ? normalizeCanonicalResultsUrl(new URL(window.location.href))
        : new URL(window.location.href);
      const expected = reportSessionId
        ? { sessionId: reportSessionId, scope: resultsArtifactScope } as const
        : null;
      const poppedNavigation = expected
        ? readResultsNavigationState(window.history.state, expected)
        : null;
      const poppedRoute = parseResultsRoute(poppedUrl.searchParams);
      applyLocation(expectedReportEntryId);
      if (openingContextMatchesPoppedSource(
        departingContext,
        poppedRoute,
        poppedNavigation?.entry_id ?? null,
      )) {
        pendingResultsSourceRestoreRef.current = {
          context: departingContext,
          sourceElement: departingContext.type === "report-inspection"
            ? reportReturnFocusRef.current
            : resultsSourceFocusRef.current,
        };
        if (departingContext.type === "chat") {
          setPanelMode(departingContext.panelMode);
        }
        setResultsSourceRestoreEpoch((epoch) => epoch + 1);
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [
    applyResultsRoute,
    canvas.canvasId,
    reportSessionId,
    resultsArtifactScope,
    resultsExplorerRouteSupported,
    resultsWorkspaceEnabled,
  ]);
  useLayoutEffect(() => {
    const pending = pendingResultsSourceRestoreRef.current;
    if (!pending) return;
    if (pending.context.type === "report-inspection" && selectedTab !== "report") return;
    if (pending.context.type === "code" && selectedTab !== "sql") return;
    if (pending.context.type === "chat" && panelMode === "canvas") return;
    pendingResultsSourceRestoreRef.current = null;
    requestAnimationFrame(() => {
      let target = pending.sourceElement?.isConnected
        ? pending.sourceElement
        : null;
      if (!target && pending.context.type === "report-inspection") {
        const section = elementByStableAttribute(
          "data-section-id",
          pending.context.focusToken ?? pending.context.anchor,
        );
        target = section?.querySelector<HTMLElement>(
          "button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])",
        ) ?? section;
      } else if (!target && pending.context.type === "code") {
        const run = elementByStableAttribute(
          "data-results-code-run-key",
          pending.context.runKey,
        );
        target = elementByStableAttribute(
          "data-results-focus-token",
          pending.context.focusToken,
          run ?? document,
        ) ?? run;
      } else if (!target && pending.context.type === "chat") {
        target = elementByStableAttribute(
          "data-results-message-token",
          pending.context.messageToken,
        ) ?? document.querySelector<HTMLElement>(".v2-messages");
      }
      if (pending.context.type === "code" && reportBodyRef.current) {
        reportBodyRef.current.scrollTop = codeScrollTopRef.current;
      }
      if (!target) target = resultsCurrentTitleRef.current;
      if (!target) return;
      if (!target.matches("button, a[href], input, textarea, select, [tabindex]")) {
        target.tabIndex = -1;
      }
      if (target !== pending.sourceElement && typeof target.scrollIntoView === "function") {
        target.scrollIntoView({ block: "center" });
      }
      target.focus();
    });
  }, [panelMode, resultsSourceRestoreEpoch, selectedTab]);
  useEffect(() => {
    const onFocusData = (event: Event) => {
      const detail = (event as CustomEvent<CanvasFocusDataDetail>).detail;
      const artifactId = detail?.artifactId;
      if (artifactId) {
        const openingContext = captureResultsOpeningContext(
          detail.entryPoint,
          detail.reportContext,
        );
        if (detail.entryPoint === "report") {
          setReportReturnContext({
            artifactId,
            label: detail.reportContext?.label.trim() || resultsT("report"),
          });
        } else {
          setReportReturnContext(null);
        }
        trackResultsWorkspace({
          action: "cross_link_opened",
          resultType: "artifact",
          entryPoint: detail.entryPoint ?? "unknown",
          mode: detail.mode ?? "preview",
        });
        navigateToResult(
          {
            type: "artifact",
            artifactId,
            mode: detail.mode ?? "preview",
          },
          detail.history ?? "push",
          openingContext,
          openingContext?.type === "report-inspection"
            || openingContext?.type === "code"
            || openingContext?.type === "chat",
        );
      }
    };
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, onFocusData);
    return () => window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, onFocusData);
  }, [captureResultsOpeningContext, navigateToResult, resultsT]);
  useEffect(() => {
    const onOpenResult = (event: Event) => {
      const detail = (event as CustomEvent<CanvasOpenResultDetail>).detail;
      if (detail?.target) {
        setReportReturnContext(null);
        const openingContext = captureResultsOpeningContext(detail.entryPoint);
        trackResultsWorkspace({
          action: "cross_link_opened",
          resultType: detail.target.type,
          entryPoint: detail.entryPoint ?? "unknown",
          mode: detail.target.type === "artifact"
            ? detail.target.mode ?? "preview"
            : "none",
        });
        navigateToResult(
          detail.target,
          detail.history ?? "push",
          openingContext,
          openingContext?.type === "report-inspection"
            || openingContext?.type === "code"
            || openingContext?.type === "chat",
        );
      }
    };
    window.addEventListener(CANVAS_OPEN_RESULT_EVENT, onOpenResult);
    return () => window.removeEventListener(CANVAS_OPEN_RESULT_EVENT, onOpenResult);
  }, [captureResultsOpeningContext, navigateToResult]);
  // Inspection pre-fills the existing Composer; only an explicit Send submits analysis.
  // Resolve exact metadata and fence late replies by the original account and draft scope.
  const chartFollowupDraftScope = composerDraftScopeSignature(draftPersistenceScope);
  useEffect(() => {
    if (readOnly || reportOnly) return;
    let active = true;
    let pending: AbortController | null = null;
    const stop = onChartFollowup((detail) => {
      pending?.abort();
      const scope = detail.selectedContext;
      const scopeText = scope ? (scope.tuples ? JSON.stringify(scope.tuples) : `${scope.window!.lower} – ${scope.window!.upper}`) : null;
      const question = tChat("chart_followup_prefill", { title: detail.title }) + (scopeText ? "\n" + tChat("chart_followup_scope", { scope: scopeText }) : "");
      // Install selected prose with its reference, so an in-flight lookup cannot pair it with an old scope.
      if (!scope) setComposerPrefill(question);
      if (!detail.dataArtifactId) return;
      if (!runner.sessionId) { toast({ kind: "error", text: tChat("chart_followup_unavailable") }); return; }
      const controller = new AbortController();
      pending = controller;
      void chartFollowupReference(runner.sessionId, detail.dataArtifactId, controller.signal).then((reference) => {
        if (!active || controller.signal.aborted) return;
        if (scope) { reference = { ...reference, selectedContext: scope }; setComposerPrefill(question); }
        setResultReferencePrefills((current) => current.some((ref) => ref.fileId === reference.fileId) ? current.map((ref) => ref.fileId === reference.fileId ? reference : ref) : [...current, reference]);
      }).catch(() => {
        if (active && !controller.signal.aborted) toast({ kind: "error", text: tChat("chart_followup_unavailable") });
      });
    });
    return () => { active = false; pending?.abort(); stop(); };
  }, [tChat, readOnly, reportOnly, runner.sessionId, authSession?.me?.id, authSession?.me?.team_id, authSession?.me?.auth_session_generation, chartFollowupDraftScope]);
  const returnToReport = useCallback(() => {
    if (
      !reportReturnContext
      && resultsOpeningContext.type !== "report-inspection"
    ) return;
    resultsInteractionEpochRef.current += 1;
    if (resultsOpeningContext.type === "report-inspection") {
      pendingReportHistoryEntryRef.current = resultsOpeningContext.reportEntryId;
      window.history.back();
      return;
    }
    pendingReportReturnFocusRef.current = reportReturnFocusRef.current;
    pendingReportReturnRef.current = true;
    pendingReportHistoryEntryRef.current = null;
    navigateToResult(
      { type: "report" },
      "replace",
      { type: "external" },
    );
  }, [navigateToResult, reportReturnContext, resultsOpeningContext]);
  const returnToOpeningSource = useCallback(() => {
    if (resultsOpeningContext.type === "report-inspection" || reportReturnContext) {
      returnToReport();
      return;
    }
    if (
      resultsOpeningContext.type !== "code"
      && resultsOpeningContext.type !== "chat"
    ) return;
    resultsInteractionEpochRef.current += 1;
    window.history.back();
  }, [reportReturnContext, resultsOpeningContext, returnToReport]);
  useLayoutEffect(() => {
    if (selectedTab !== "report" || !pendingReportReturnRef.current) return;
    const target = pendingReportReturnFocusRef.current;
    pendingReportReturnRef.current = false;
    pendingReportReturnFocusRef.current = null;
    requestAnimationFrame(() => {
      if (target?.isConnected) target.focus();
      else resultsCurrentTitleRef.current?.focus();
    });
  }, [selectedTab]);
  // ⑧ (bug 0708): restore Report scroll position when returning from Data/Code.
  // ReportView remounts fresh at scrollTop 0. A one-shot set clamps to 0 for
  // chart/image/iframe reports (canvas charts render async → container still short
  // when the effect runs). Re-apply the target across a few frames until the
  // content has grown tall enough for it to stick (or a frame cap), so the reporter's
  // chart-report case actually restores.
  useLayoutEffect(() => {
    if (selectedTab !== "report") return;
    const target = reportScrollTopRef.current;
    if (target <= 0 || !reportBodyRef.current) return;
    if (typeof requestAnimationFrame !== "function") {
      reportBodyRef.current.scrollTop = target;
      return;
    }
    let raf = 0;
    let frames = 0;
    const apply = () => {
      const el = reportBodyRef.current;
      if (!el) return;
      el.scrollTop = target;
      frames += 1;
      // Keep going while the container is still too short to reach target (async
      // content growing); stop once it sticks or after ~15 frames (~250ms).
      if (el.scrollTop < target && frames < 15) raf = requestAnimationFrame(apply);
    };
    apply();
    return () => cancelAnimationFrame(raf);
  }, [selectedTab]);
  // ⑧: a new report/session resets the saved offset, so switching sessions and then
  // toggling Data→Report doesn't restore the previous report's scroll position.
  useEffect(() => {
    reportScrollTopRef.current = 0;
  }, [canvas.canvasId, reportSessionId]);
  useEffect(() => {
    const previousSessionId = previousResultsSessionRef.current;
    previousResultsSessionRef.current = reportSessionId;
    if (shouldResetResultsSession(previousSessionId, reportSessionId)) {
      setResultReferencePrefills([]);
      setConsumedComposerPrefillKeys([]);
      setResultsMobileListOpen(false);
      setResultsNavigatorQuery("");
      setResultsQuickQuery("");
      setResultsQuickOpeningContext({ type: "external" });
      setResultsOpeningContext({ type: "external" });
      setResultsExplorerState(null);
      setReportReturnContext(null);
      reportReturnFocusRef.current = null;
      resultsSourceFocusRef.current = null;
      resultsClickSourceRef.current = null;
      pendingResultsSourceRestoreRef.current = null;
      codeScrollTopRef.current = 0;
      pendingReportReturnRef.current = false;
      pendingReportReturnFocusRef.current = null;
      pendingReportHistoryEntryRef.current = null;
      const nextUrl = clearResultsLocation("replace");
      applyResultsRoute(parseResultsRoute(nextUrl.searchParams));
    }
  }, [applyResultsRoute, clearResultsLocation, reportSessionId]);
  useEffect(() => {
    if (resultsWorkspaceShellEnabled) return;
    const url = new URL(window.location.href);
    // Replace only a session already in the URL; do not add one to a clean URL (`?session=` belongs to page synchronization; see results-workspace-route).
    if (reportSessionId && url.searchParams.has("session")) url.searchParams.set("session", reportSessionId);
    if (selectedTab === "report") {
      url.searchParams.delete("tab");
      url.searchParams.delete("focus");
    } else {
      url.searchParams.set("tab", selectedTab);
      if (selectedTab === "data" && dataFocusArtifactId) {
        url.searchParams.set("focus", dataFocusArtifactId);
      } else {
        url.searchParams.delete("focus");
      }
    }
    window.history.replaceState(
      withoutNextRouterHistoryMarkers(withoutResultsHistoryState(window.history.state)),
      "",
      url.toString(),
    );
  }, [
    dataFocusArtifactId,
    reportSessionId,
    resultsWorkspaceShellEnabled,
    selectedTab,
  ]);
  // Sprint chat-v4-streaming Wave 4 — composer prefill on HITLPickerCard
  // dismiss ("换个问法"). Setting non-empty triggers V2Composer's useEffect to
  // overwrite its input field. Parent clears after one cycle to avoid sticky
  // re-prefill on unrelated workspace re-renders.
  const [composerPrefill, setComposerPrefill] = useState<string | null>(null);
  // Which restore put the question back in the box (stop, or a replay the server cannot take), for its notice.
  const [restoredDraftNotice, setRestoredDraftNotice] = useState<RestoredDraftNotice | null>(null);
  const restoredPrefillPendingRef = useRef<RestoredDraftNotice | null>(null);
  // A replay restore: the last send's structured input (real @ references) and its attachments.
  const [replayPrefillDocument, setReplayPrefillDocument] = useState<InputDocument | null>(null);
  const [replayAttachmentPrefills, setReplayAttachmentPrefills] = useState<AttachmentRef[]>([]);
  const composerAttach = useMemo(
    () => [
      ...(prefillAttachments ?? []).filter(
        (attachment) => !consumedComposerPrefillKeys.includes(
          composerPrefillKey(attachment),
        ),
      ),
      ...resultReferencePrefills,
      ...replayAttachmentPrefills,
    ],
    [consumedComposerPrefillKeys, prefillAttachments, replayAttachmentPrefills, resultReferencePrefills],
  );
  useEffect(() => {
    if (prefillText?.trim()) setComposerPrefill(prefillText);
  }, [prefillText]);
  useEffect(() => {
    restoredPrefillPendingRef.current = null;
    setRestoredDraftNotice(null);
    setReplayPrefillDocument(null);
    setReplayAttachmentPrefills([]);
  }, [runner.sessionId]);
  // skill-team-governance P3: cross-page prefill for modifying a skill via conversation.
  //
  // ⚠ feedback-triage-2026-08-24: this was originally **this component's** mount effect, with the same pitfall
  // as attachment prefill below: this component mounts only when `runner.started || showingReportOnly`,
  // while navigation from the skill drawer lands on the empty home page without mounting it. Beyond missing prefill,
  // a later real conversation mount picks up the leftover key and inserts `帮我修改技能 XXX` into a conversation
  // where the user did not intend to edit a skill (the customer reported exactly this behavior).
  // Consumption moved up to `app/(app)/page.tsx` (unconditionally mounted), via externalComposerPrefill
  // → prefillText; this component no longer reads that key itself.
  // file-management-v2 P2 `在新会话中使用` attachments are read by the **page** and passed down.
  //
  // ⚠ Originally a mount effect here, reading a one-shot sessionStorage key. Production dogfooding in 0.87.0
  // proved it wrong: this component mounts only when `runner.started || showingReportOnly`,
  // but navigation from files lands on the **empty home page** (HOME_V9's HomeWorkSurface),
  // leaving this component unmounted → key never consumed. Beyond missing prefill, a later
  // real conversation mount picks up the leftover key and attaches the file to an unintended conversation.
  // Consumption therefore moved up to `app/(app)/page.tsx` (unconditionally mounted).
  // WP1 (design D5): all start/poll/template flows move into useCanvasExport + ExportMenu.
  // V2.9 #61's clone-vs-default dialog is replaced by inline template single-choice selection in the menu.

  // chat-truth P0-B fix (2026-05-14): right pane only when canvas has real
  // content sections. Previously also flipped on `status !== "idle"` which
  // surfaces "Untitled Report" empty placeholder for chat-mode turns where
  // backend fired canvas.opened but never produced sections (post-hoc
  // response_kind=chat clears sec_lead_* leaving sections.length === 0).
  // Canvas-mode turns: outline_ready fills skeletons first → length > 0 → hasCanvas true.
  // #3 / DF-04 flash fix: gate the right pane on REPORT-SURFACE sections, not raw
  // canvas.sections. Lead's buffered sec_lead_* sections and auxiliary SQL/Python/
  // raw-data sections must not flip the Report pane open only to disappear when a
  // chat-mode turn settles. Real canvas turns still open through report skeletons.
  const codeAvailable = projectCodeRuns({
    sections: canvas.sections,
    executionSteps: pythonExecutionSteps,
  }).length > 0;
  const currentReportTitle = reportResultsTitle(
    reportSections,
    canvas.title,
    resultsT("report"),
  );
  const resultModelLabels = useMemo(() => ({
    reportEditable: resultsT("report_meta"),
    reportReadonly: resultsT("report_readonly_meta"),
    codeRunning: resultsT("running"),
    codeTitle: resultsT("code"),
    codeLastRun: resultsT("last_run"),
    sourceUpload: resultsT("source_upload"),
    sourceGenerated: resultsT("source_generated"),
    sourceExport: resultsT("source_export"),
    sourceWeb: resultsT("source_web"),
    sourceSandbox: resultsT("source_sandbox"),
    sourceData: resultsT("source_data"),
    exportVersions: (count: number) => resultsT("export_versions", { count }),
    formatDate: (value: string) => {
      const date = new Date(value);
      return Number.isNaN(date.getTime())
        ? value
        : formatDateTime(date, getCurrentLocale());
    },
  }), [resultsT]);
  const legacyResultItems = useMemo(() => projectResultsWorkspace({
    artifacts: resultArtifacts ?? [],
    exportGroups: exportVersionGroups,
    report: hasCanvas && canvas.canvasId
      ? {
          canvasId: canvas.canvasId,
          title: currentReportTitle,
          editable: Boolean(canvas.canvasId) && !readOnly && !reportOnly,
        }
      : null,
    code: runner.sessionId
      ? {
          sessionId: runner.sessionId,
          available: codeAvailable,
          running: runningPySteps.length > 0,
        }
      : null,
    labels: resultModelLabels,
    readOnly: Boolean(readOnly),
    reportOnly,
  }), [
    canvas.canvasId,
    codeAvailable,
    currentReportTitle,
    exportVersionGroups,
    hasCanvas,
    readOnly,
    reportOnly,
    resultArtifacts,
    resultModelLabels,
    runner.sessionId,
    runningPySteps.length,
  ]);
  const completeResultsActive = resultsCapabilities.completeList
    && Boolean(resultsArtifactSessionId)
    && !completeResults.fallbackAllowed;
  const completeCatalogItems = useMemo(
    () => projectResultsDTO(completeResults.catalogItems, resultModelLabels),
    [completeResults.catalogItems, resultModelLabels],
  );
  const completeNavigationItems = useMemo(
    () => projectResultsDTO(completeResults.items, resultModelLabels),
    [completeResults.items, resultModelLabels],
  );
  const resultItems = completeResultsActive
    ? completeCatalogItems
    : legacyResultItems;
  const resultsNavigatorItems = completeResultsActive
    ? completeNavigationItems
    : legacyResultItems;
  const resultArtifactsForViewer = useMemo(
    () => completeResultsActive
      ? completeCatalogItems.flatMap((item) => (
          item.artifactItem ? [item.artifactItem] : []
        ))
      : resultArtifacts,
    [completeCatalogItems, completeResultsActive, resultArtifacts],
  );
  const [failedResultLocateKey, setFailedResultLocateKey] = useState<string | null>(null);
  const selectedArtifactKnown = !dataFocusArtifactId
    || Boolean(resultArtifactsForViewer?.some((item) => item.id === dataFocusArtifactId));
  const explorerOwnsSelectedArtifact = explorerOwnsArtifactSelection({
    artifactId: dataFocusArtifactId,
    scaleNav: resultsCapabilities.scaleNav,
    selectionKey: resultsExplorerState?.selected_key ?? resultsExplorerState?.anchor?.key,
    surface: resultsSurface,
  });
  const explorerSelectedArtifactUnavailable = Boolean(
    explorerOwnsSelectedArtifact
    && dataFocusArtifactId
    && !completeResults.loading
    && completeResults.errorAnchorKey === `artifact:${dataFocusArtifactId}`
  );
  useEffect(() => {
    if (
      !completeResultsActive
      || !dataFocusArtifactId
      || selectedArtifactKnown
      || explorerOwnsSelectedArtifact
    ) {
      setFailedResultLocateKey(null);
      return;
    }
    const artifactId = dataFocusArtifactId;
    let active = true;
    setFailedResultLocateKey(null);
    void completeResults.locate(`artifact:${artifactId}`).then((located) => {
      if (active && !located) setFailedResultLocateKey(artifactId);
    });
    return () => {
      active = false;
    };
  }, [
    completeResults.locate,
    completeResultsActive,
    dataFocusArtifactId,
    explorerOwnsSelectedArtifact,
    selectedArtifactKnown,
  ]);
  const locatingSelectedArtifact = completeResultsActive
    && Boolean(dataFocusArtifactId)
    && !selectedArtifactKnown
    && !explorerSelectedArtifactUnavailable
    && failedResultLocateKey !== dataFocusArtifactId;
  const viewerArtifactItems = locatingSelectedArtifact
    ? null
    : resultArtifactsForViewer;
  const retryViewerArtifactItems = useCallback(() => {
    if (!completeResultsActive || !dataFocusArtifactId) {
      retryResultArtifacts();
      return;
    }
    const artifactId = dataFocusArtifactId;
    setFailedResultLocateKey(null);
    void completeResults.locate(`artifact:${artifactId}`).then((located) => {
      if (!located) setFailedResultLocateKey(artifactId);
    });
  }, [
    completeResults.locate,
    completeResultsActive,
    dataFocusArtifactId,
    retryResultArtifacts,
  ]);
  const resultsNavigatorListState = completeResultsActive
    ? {
        searchMode: "server" as const,
        completeness: "complete" as const,
        loadedCount: completeResults.loadedCount,
        authorizedTotal: completeResults.authorizedTotal,
        matchedTotal: completeResults.matchedTotal,
        loading: completeResults.loading,
        loadingMore: completeResults.loadingMore,
        hasMore: completeResults.hasMore,
        error: completeResults.error !== null,
      }
    : {
        searchMode: "local" as const,
        completeness: (resultArtifacts?.length ?? 0) >= 100
          ? "partial" as const
          : "complete" as const,
        loadedCount: legacyResultItems.length,
        authorizedTotal: (resultArtifacts?.length ?? 0) < 100
          ? legacyResultItems.length
          : null,
        matchedTotal: null,
        loading: resultArtifacts === null,
        loadingMore: false,
        hasMore: false,
        error: resultArtifactsError !== null,
      };
  const refreshWorkspaceArtifacts = useCallback(async (
    reason: Parameters<typeof refreshResultArtifacts>[0],
  ): Promise<ArtifactItem[]> => {
    if (!completeResultsActive) return refreshResultArtifacts(reason);
    if (
      resultsCapabilities.scaleNav
      && resultsSurface === "explorer"
      && panelMode !== "chat"
    ) {
      await completeResults.checkChanges();
      return resultArtifactsForViewer ?? [];
    }
    const items = await completeResults.refreshBase();
    return projectResultsDTO(items, resultModelLabels).flatMap((item) => (
      item.artifactItem ? [item.artifactItem] : []
    ));
  }, [
    completeResults.checkChanges,
    completeResults.refreshBase,
    completeResultsActive,
    panelMode,
    refreshResultArtifacts,
    resultArtifactsForViewer,
    resultModelLabels,
    resultsCapabilities.scaleNav,
    resultsSurface,
  ]);
  const refreshedDocumentReceipt = useRef<string | null>(null);
  useEffect(() => {
    if (!documentResultsRefresh || !resultsWorkspaceEnabled || reportOnly) return;
    const { scope, revision } = documentResultsRefresh;
    if (scope.sessionId !== resultsArtifactSessionId || scope.userId !== authSession?.me?.id
      || scope.teamId !== authSession.me.team_id || scope.authSessionGeneration !== authSession.me.auth_session_generation) return;
    if (draftPersistenceScope && (scope.workspaceId !== draftPersistenceScope.workspaceId
      || scope.authSessionGeneration !== draftPersistenceScope.authSessionGeneration)) return;
    const identity = JSON.stringify([scope, revision]);
    if (refreshedDocumentReceipt.current === identity) return;
    refreshedDocumentReceipt.current = identity;
    // Canonical published files retain their original Turn/source association. Refresh never selects them.
    void refreshWorkspaceArtifacts("document-published");
  }, [documentResultsRefresh, resultsWorkspaceEnabled, reportOnly, resultsArtifactSessionId,
    authSession?.me?.id, authSession?.me?.team_id, authSession?.me?.auth_session_generation, draftPersistenceScope, refreshWorkspaceArtifacts]);
  const hasBrowsableResources = resultItems.length > 0;
  // Discovery offers an entry; only real content or an explicit user action opens Results.
  const hasResultsPane = hasCanvas || resultsOpenedByUser || resultsRevealRequest > 0;
  const hasRightPane = hasResultsPane || creationPanelOpen;
  // Config copilot placement: float inside the visible right results column (scrim covers only results, leaving conversation visible);
  // otherwise enter document flow as the second column. Desktop keeps one split host even without results.
  // Manually opened results host the floating drawer, avoiding a third column.
  const splitBranchActive = isDesktop;
  const resultsPaneVisible = hasResultsPane && panelMode !== "chat";
  // P2 batch 4: an open results canvas asks the sidebar for its icon rail; the sidebar honours it below 1280
  // (lib/sidebar-state.ts).
  useRequestSidebarAutoCompact(splitBranchActive && resultsPaneVisible);
  const configDrawerOverlaysResults = configDrawerOpen
    && isDesktop
    && (splitBranchActive ? resultsPaneVisible : hasCanvas);
  const closeResultsPane = useCallback(() => {
    resultsInteractionEpochRef.current += 1;
    if (runner.running) resultsClosedDuringTurnRef.current = true;
    setReportReturnContext(null);
    reportReturnFocusRef.current = null;
    pendingReportReturnRef.current = false;
    pendingReportReturnFocusRef.current = null;
    setPanelMode("chat");
  }, [runner.running]);
  // Workbench Spec §10: new results while results are hidden show a dot on the mobile `成果` switch and desktop restore strip;
  // clear after viewing (opening results / switching to `成果`).
  const resultsNewIds = useMemo(
    () => [
      ...reportSections.map((section) => `section:${section.id}`),
      ...resultItems.map((item) => `result:${item.key}`),
    ],
    [reportSections, resultItems],
  );
  const resultsHasNew = useResultsNewIndicator({
    enabled: !reportOnly && !creationPanelOpen,
    // Same conversation identity as the report: use canvas identity when a new runner's session ID arrives after the canvas.
    sessionId: reportSessionId,
    ids: resultsNewIds,
    visible: isDesktop ? resultsPaneVisible : panelMode === "canvas",
    running: runner.running,
  });
  // Workbench Spec §9: ⌘⇧\ / Ctrl+Shift+\ maximizes results; pressing again restores. Only desktop split
  // (≥1024, results column present) supports it; shared read-only has no conversation to return to. Yield to open dialogs, nonmodal overlays, or menus.
  // Esc does not restore (reserved for overlays).
  // The entire conversation column receives file drops (Workbench Spec §4); results column and report canvas do not.
  const chatColumnRef = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    if (!composerRevealRequest) return;
    const consume = () => onComposerRevealConsumed?.(composerRevealRequest.id);
    if (reportOnly || composerRevealRequest.scopeKey !== composerDraftScopeSignature(draftPersistenceScope)) { consume(); return; }
    const previousFocus = document.activeElement;
    // Commit the compact Chat layout before the guarded frame focuses its input.
    setPanelMode(window.matchMedia(DESKTOP_SPLIT_QUERY).matches ? "split" : "chat");
    const frame = requestAnimationFrame(() => {
      consume();
      if (document.activeElement !== previousFocus && document.activeElement !== document.body) return;
      focusComposerFallback(chatColumnRef.current?.querySelector<HTMLElement>(".composer-v2") ?? null);
    });
    return () => { cancelAnimationFrame(frame); consume(); };
    // Only an explicit original-instance handoff requests focus, never a scope change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerRevealRequest]);
  const maximizeShortcutEnabled = isDesktop && hasResultsPane && !reportOnly && !creationPanelOpen;
  useEffect(() => {
    if (!maximizeShortcutEnabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || !isResultsMaximizeShortcut(event)) return;
      if (hasOpenOverlay()) return;
      event.preventDefault();
      setPanelMode((mode) => (mode === "canvas" ? "split" : "canvas"));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [maximizeShortcutEnabled]);
  // Button hints show the current keyboard-layout character at the backslash position; without browser layout information, identify the US-keyboard \ position.
  const [backslashKey, setBackslashKey] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void backslashKeyOnLayout().then((key) => {
      if (!cancelled) setBackslashKey(key);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  const shortcutPlatform = typeof navigator === "undefined" ? undefined : navigator.platform;
  const maximizeShortcut = backslashKey
    ? resultsMaximizeShortcutLabel(shortcutPlatform, backslashKey)
    : `${resultsMaximizeShortcutLabel(shortcutPlatform)}${t("max_shortcut_us_key_note")}`;
  const resultsChooserAvailable = !hasCanvas && !reportOnly && Boolean(resultsWorkspaceEnabled || canvasObjectList);
  const resultsChooserSelected = resultsChooserAvailable && selectedTab === "report" && !resultsRouteUnavailable && resultsSurface === "viewer";
  const resultsSelection: ResultsSelection =
    resultsRouteUnavailable || (resultsChooserAvailable && selectedTab === "report")
      ? { type: "none" }
      : selectedTab === "data"
      ? dataFocusArtifactId
        ? { type: "artifact", artifactId: dataFocusArtifactId }
        : { type: "none" }
      : selectedTab === "sql"
        ? { type: "code" }
        : { type: "report" };
  const resultsExplorerDetailOpen = Boolean(
    resultsCapabilities.scaleNav
    && resultsSurface === "explorer"
    && resultsExplorerState?.selected_key,
  );
  const resultsCurrentTitle = resultsSurface === "explorer"
    && !resultsExplorerDetailOpen
    ? resultsT("explorer_title")
    : resultsChooserAvailable && selectedTab === "report" && !resultsRouteUnavailable
      ? resultsT("title")
    : resultsSelectionTitle(
        resultsSelection,
        resultItems,
        {
          report: currentReportTitle,
          code: resultsT("code"),
          missing:
            resultsRouteUnavailable
              ? resultsT("missing_object")
              : selectedTab === "data"
              ? resultsT("evidence")
              : resultsT("missing_object"),
        },
      );
  const selectedResult = resultsSurface === "explorer" && !resultsExplorerDetailOpen
    ? undefined
    : resolveSelectedResult(resultItems, resultsSelection);
  const selectedResultKey = selectedResult?.key ?? null;
  const purgeResultsRecent = useCallback(() => {
    if (resultsRecentScope) clearRecentResults(resultsRecentScope);
    setResultsRecentKeys([]);
  }, [resultsRecentScope]);
  const quickResults = useResultsQuickQuery({
    currentKey: selectedResultKey,
    enabled: resultsCapabilities.scaleNav,
    onScopePurged: purgeResultsRecent,
    open: resultsNavigatorOpen,
    query: resultsQuickQuery,
    recentKeys: resultsRecentKeys,
    scope: resultsArtifactScope,
    sessionId: resultsArtifactSessionId,
  });
  useEffect(() => {
    if (
      !resultsCapabilities.scaleNav
      || !resultsOpenedByUser
      || !resultsRecentScope
      || !selectedResultKey
    ) return;
    setResultsRecentKeys(recordRecentResult(resultsRecentScope, selectedResultKey));
  }, [
    resultsCapabilities.scaleNav,
    resultsOpenedByUser,
    resultsRecentScope,
    selectedResultKey,
  ]);
  const selectedArtifactRole: ArtifactResultRole | undefined =
    selectedResult?.objectType !== "artifact"
      ? undefined
      : selectedResult.viewerProfile === "unsupported"
        ? "unsupported"
        : selectedResult.primaryGroup === "deliverables"
          ? "deliverable"
          : selectedResult.secondaryGroup === "input"
            ? "input"
            : "data";
  const selectedArtifactIsHistorical = Boolean(
    selectedResult?.version
    && selectedResult.route.type === "artifact"
    && dataFocusArtifactId
    && selectedResult.route.artifactId !== dataFocusArtifactId,
  );
  const referenceArtifactInChat = useCallback((artifact: ArtifactItem) => {
    if (readOnly || reportOnly || !runner.sessionId) return;
    setResultReferencePrefills((current) => [
      ...current,
      {
        fileId: artifact.id,
        filename: artifact.filename,
        mime: artifact.mime,
        size: artifact.size,
        referenceKind: "result",
      },
    ]);
    setPanelMode("split");
    trackResultsWorkspace({
      action: "referenced",
      resultType: "artifact",
      group: resolveSelectedResult(resultItems, {
        type: "artifact",
        artifactId: artifact.id,
      })?.primaryGroup ?? "none",
      entryPoint: "navigator",
      mode: artifactViewerMode,
    });
    toast({ kind: "info", text: resultsT("referenced_in_chat") });
  }, [
    artifactViewerMode,
    readOnly,
    reportOnly,
    resultItems,
    resultsT,
    runner.sessionId,
  ]);
  const exportBusy = exportCtl.busy || exportPreparing;

  useEffect(() => {
    if (!resultsWorkspaceEnabled) return;
    const refreshOnFocus = () => {
      void refreshWorkspaceArtifacts("window-focus");
      void refreshExportVersions();
    };
    const refreshOnVisible = () => {
      if (document.visibilityState === "visible") refreshOnFocus();
    };
    window.addEventListener("focus", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnVisible);
    return () => {
      window.removeEventListener("focus", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnVisible);
    };
  }, [
    refreshExportVersions,
    refreshWorkspaceArtifacts,
    resultsWorkspaceEnabled,
  ]);

  useEffect(() => {
    if (
      resultsSurface !== "explorer"
      || panelMode === "chat"
      || !resultsCapabilities.scaleNav
      || !completeResultsActive
    ) return;
    const check = () => {
      if (document.visibilityState === "visible") {
        void completeResults.checkChanges();
      }
    };
    const timer = window.setInterval(check, 15_000);
    return () => window.clearInterval(timer);
  }, [
    completeResults.checkChanges,
    completeResultsActive,
    panelMode,
    resultsCapabilities.scaleNav,
    resultsSurface,
  ]);

  useEffect(() => {
    const completedJob = exportCtl.state.job;
    if (
      !resultsWorkspaceEnabled
      || completedJob?.status !== "done"
      || refreshedExportJobRef.current === completedJob.jobId
    ) return;
    refreshedExportJobRef.current = completedJob.jobId;
    void refreshWorkspaceArtifacts("export-completed");
    void refreshExportVersions();
  }, [
    exportCtl.state.job,
    refreshExportVersions,
    refreshWorkspaceArtifacts,
    resultsWorkspaceEnabled,
  ]);

  useEffect(() => {
    const wasRunning = previousRunnerRunningRef.current;
    previousRunnerRunningRef.current = runner.running;
    if (!resultsWorkspaceEnabled) return;

    const startsNewTurn = runner.running && (
      !wasRunning || baselineTurnIdRef.current !== runner.turnId
    );
    if (startsNewTurn) {
      baselineTurnIdRef.current = runner.turnId;
      turnBaselineKeysRef.current = new Set(
        (resultArtifactsForViewer ?? []).map((artifact) => artifact.id),
      );
      terminalPaneClosedAtStartRef.current =
        panelMode === "chat" || !hasResultsPane;
      resultsClosedDuringTurnRef.current = false;
      if (handledTerminalTurnIdRef.current !== runner.turnId) {
        handledTerminalTurnIdRef.current = null;
      }
      return;
    }

    if (runner.running || !wasRunning || !runner.turnId) return;
    const terminalTurnId = runner.turnId;
    if (handledTerminalTurnIdRef.current === terminalTurnId) return;
    handledTerminalTurnIdRef.current = terminalTurnId;
    const terminalSessionId = resultsArtifactSessionId;
    const interactionEpoch = resultsInteractionEpochRef.current;
    const canSurfaceAfterRefresh =
      terminalPaneClosedAtStartRef.current
      && !resultsClosedDuringTurnRef.current
      && (panelMode === "chat" || !hasResultsPane);

    void refreshWorkspaceArtifacts("turn-terminal").then((snapshot) => {
      if (
        activeResultsSessionRef.current !== terminalSessionId
        || !canSurfaceAfterRefresh
        || resultsClosedDuringTurnRef.current
        || resultsInteractionEpochRef.current !== interactionEpoch
      ) return;
      const candidate = terminalAutoOpenArtifact(
        turnBaselineKeysRef.current,
        snapshot,
        terminalTurnId,
      );
      if (!candidate) return;
      navigateToResult({
        type: "artifact",
        artifactId: candidate.id,
        mode: "preview",
      }, "replace");
    });
  }, [
    hasResultsPane,
    navigateToResult,
    panelMode,
    refreshWorkspaceArtifacts,
    resultArtifactsForViewer,
    resultsArtifactSessionId,
    resultsWorkspaceEnabled,
    runner.running,
    runner.turnId,
  ]);

  const triggerExport = useCallback((fmt: ExportFmt, opts?: {
    mode?: "auto" | "clone" | "default";
    template_artifact_id?: string;
    /** Automatic export (`?export=`) only: prepare may be queried before the newly mounted report finishes rendering.
     *  **Do not** fall back to legacy then; return false only for retryable transient states, and true
     *  for an existing export or explicit rejection, telling the caller to stop retrying. */
    retryTransient?: boolean;
  }): Promise<boolean> => {
    if (!canvas.canvasId || exportBusy) return Promise.resolve(false);
    const exportCanvasId = canvas.canvasId;
    setExportPreparing(true);
    return (async () => {
      try {
        if (selectedTab !== "report") setTab("report");
        const reportView = await waitForReportView(reportViewRef);
        if (!reportView) {
          if (opts?.retryTransient) return false;
          toast({ kind: "error", text: t("export_prepare_not_ready") });
          return false;
        }
        const v2Profile = DEFAULT_V2_EXPORT_PROFILE[fmt];
        const prepared = await reportView.prepareForExport({
          format: fmt,
          profile: v2Profile,
          forceLegacy: opts?.mode === "clone",
        });
        if (!prepared.ok) {
          const hardReject =
            prepared.reason === "active_canvas_generation"
            || prepared.reason === "rewrite_in_progress"
            || prepared.reason === "revision_conflict";
          if (hardReject) {
            toast({ kind: "error", text: t(`export_prepare_${prepared.reason}`) });
            return true;
          }
          // Automatic export: these two reasons are **transient** on cold load (surface unfinished / section still streaming);
          // do not rush into legacy fallback, the behavior this change removes.
          if (
            opts?.retryTransient
            && (prepared.reason === "surface_render_incomplete"
              || prepared.reason === "surface_projection_conflict")
          ) {
            return false;
          }
          toast({ kind: "info", text: t("export_prepare_degraded_legacy") });
          // R8 (116 device): this branch previously **only toasted, without metrics**, despite being the production path:
          // `surface_projection_conflict` kept all fleet V2 exports degraded long-term,
          // without any metric evidence (zero metrics requests in the observed export). tech-design §1.1
          // requires recording **all fallback except forceLegacy**, rather than only capability fallback.
          track("bowhead_export_legacy_fallback_total", {
            reason: prepared.reason,
            format: fmt,
          });
          await exportCtl.start(exportCanvasId, fmt, opts, undefined);
          // A legacy job was already started once. Report stop-retrying here, or the `?export=`
          // caller treats false as transient surface unavailability and creates up to 6 duplicate jobs.
          return true;
        }
        const useV2 = prepared.mode === "v2";
        if (!useV2 && prepared.reason === "v2_capability_unavailable") {
          // Batch0 0-A (chart-table-ux tech-design §1.1): capability collapse forced
          // legacy fallback silently, leaving production PDFs with charts converted to floating-point tables
          // unnoticed long-term. Notify users of fallback and record metrics.
          toast({ kind: "info", text: t("export_prepare_degraded_legacy") });
          track("bowhead_export_legacy_fallback_total", {
            reason: "v2_capability_unavailable",
            format: fmt,
          });
        }
        await exportCtl.start(
          exportCanvasId,
          fmt,
          opts,
          useV2
            ? {
                format: fmt,
                profile: v2Profile,
                locale: prepared.locale,
                timezone: viewerTimeZone(),
                expected_projection_token: prepared.projectionToken,
                expected_surface_token: prepared.surfaceToken,
                client_surface_receipt_sha256: prepared.renderedSurfaceReceiptSha256,
                client_visual_receipts: prepared.visualReceipts,
                theme: prepared.theme,
                include_appendix: true,
                chart_hidden_series: exportHiddenSeriesFor(prepared.visualReceipts.map((r) => r.section_id)),
              }
            : undefined,
        );
        return true;
      } finally {
        setExportPreparing(false);
      }
    })();
  }, [canvas.canvasId, exportBusy, exportCtl, selectedTab, t]);

  // Report-library `下载` sends users here with `?export=<fmt>`; the report surface already renders,
  // and `triggerExport` switches tabs + `waitForReportView` itself, enabling the full V2 receipt chain.
  // Jobs started on the library page can only use legacy (no rendered surface to produce V2's three credentials).
  useReportAutoExport({
    enabled: resultsCapabilities.resolved,
    canvasId: canvas.canvasId,
    sessionId: canvas.sessionId,
    emptyMessage: t("export_source_empty"),
    triggerExport,
  });

  const shareCurrentReport = useCallback(() => {
    if (reportSessionId) void copyPermissionedReportLink(reportSessionId);
  }, [reportSessionId]);
  const { confirm: confirmReportAction, dialog: reportConfirmDialog } = useConfirmDialog();
  const reportSectionCount = reportSections.length;
  const clearCurrentReport = useCallback(async () => {
    const canvasId = canvas.canvasId;
    if (!canvasId) return;
    const ok = await confirmReportAction({
      title: t("hdr_clear_confirm_title"),
      description: t("hdr_clear_confirm", { count: reportSectionCount }),
      confirmLabel: t("hdr_clear_report"),
    });
    if (!ok) return;
    // The report may change while confirmation is open (back navigation switches conversations); the referenced report is no longer visible,
    // so do not clear it; let users repeat the action on the current report.
    if (getCanvasSnapshot().canvasId !== canvasId) {
      toast({ kind: "info", text: t("hdr_clear_report_changed") });
      return;
    }
    try {
      await clearCanvas(canvasId);
      if (getCanvasSnapshot().canvasId === canvasId) resetCanvas();
    } catch (error) {
      toast({ kind: "error", text: t(reportHeaderActionFailedKey("clear", error)), action: copyDiagnosticsAction(error, "route") });
    }
  }, [canvas.canvasId, confirmReportAction, copyDiagnosticsAction, reportSectionCount, resetCanvas, t]);
  const deleteCurrentReport = useCallback(async () => {
    const canvasId = canvas.canvasId;
    if (!canvasId) return;
    const ok = await confirmReportAction({
      title: t("hdr_delete_confirm_title"),
      description: t("hdr_delete_confirm", { count: reportSectionCount }),
      confirmLabel: t("hdr_delete_report"),
    });
    if (!ok) return;
    // Likewise, do not delete if the report changes during confirmation, avoiding deletion of an unseen report.
    if (getCanvasSnapshot().canvasId !== canvasId) {
      toast({ kind: "info", text: t("hdr_delete_report_changed") });
      return;
    }
    try {
      await deleteCanvas(canvasId);
      if (getCanvasSnapshot().canvasId === canvasId) resetCanvas();
    } catch (error) {
      toast({ kind: "error", text: t(reportHeaderActionFailedKey("delete", error)), action: copyDiagnosticsAction(error, "route") });
    }
  }, [canvas.canvasId, confirmReportAction, copyDiagnosticsAction, reportSectionCount, resetCanvas, t]);

  // chat-truth Phase L (2026-05-14 P0-3 backstop): when Lead's turn finishes
  // (runner.running false) but canvas-store still stuck at "streaming"/"outline"
  // (backend canvas.done SSE never arrived for compute-heavy canvas paths),
  // force-emit a synthetic canvas done so the badge transitions. Gated on
  // sections.length > 0 so chat-mode turns (no canvas content) don't flip
  // status from "idle" → "done" and surface an empty right pane. Idempotent —
  // onCanvasDone is a setState that re-sets the same fields.
  useEffect(() => {
    if (!runner.running
      && (canvas.status === "streaming" || canvas.status === "outline")
      && canvas.sections.length > 0
      && runner.turnId) {
      onCanvasDone({ errors: [] });
    }
  }, [runner.running, runner.turnId, canvas.status, canvas.sections.length]);

  // The metadata controller composes this raw page title with the active brand. The page passes the session's display
  // title (the one the title bar shows, rename overrides included) so the tab follows a rename at once (116 dogfood
  // 1.5.25: the tab kept the old name until refresh); the canvas title is the fallback.
  useDocumentTitle(documentTitle?.trim() || canvas.title, "chat-workbench-canvas");

  // layout-redesign v10 canvas header — Escape closes whichever header dropdown
  // is open (WAI-ARIA menu pattern; the scrim already handles click-away).
  const closeResultsNavigator = useCallback(() => {
    setResultsNavigatorOpen(false);
    setResultsQuickQuery("");
    requestAnimationFrame(() => resultsTriggerRef.current?.focus());
  }, []);
  const openResultsQuick = useCallback(() => {
    let openingContext: ResultsOpeningContext = resultsOpeningContext;
    let history: "push" | "replace" = "replace";
    const route = parseResultsRoute(
      new URL(window.location.href).searchParams,
    );
    const expected = reportSessionId
      ? { sessionId: reportSessionId, scope: resultsArtifactScope } as const
      : null;
    if (route.surface === "explorer" && expected) {
      const explorer = readResultsExplorerState(window.history.state, expected)
        ?? createResultsExplorerState(expected);
      const navigation = readResultsNavigationState(window.history.state, expected)
        ?? createResultsNavigationState(expected);
      openingContext = {
        type: "explorer",
        explorerEntryId: explorer.entry_id,
        focusKey: explorer.focus_key,
      };
      window.history.replaceState(
        withoutNextRouterHistoryMarkers(withResultsHistoryState(window.history.state, navigation, explorer)),
        "",
        window.location.href,
      );
    } else if (
      selectedResult?.objectType === "report"
      && !resultsRouteUnavailable
    ) {
      const reportEntry = ensureCurrentResultsNavigation();
      if (reportEntry) {
        openingContext = {
          type: "report-inspection",
          reportEntryId: reportEntry.entry_id,
          anchor: null,
          focusToken: null,
        };
        history = "push";
      }
    }
    setResultsQuickOpeningContext(openingContext);
    setResultsQuickHistory(history);
    setResultsQuickQuery("");
    setResultsNavigatorOpen(true);
    trackResultsWorkspace({
      action: "navigator_opened",
      entryPoint: "header",
    });
  }, [
    ensureCurrentResultsNavigation,
    reportSessionId,
    resultsArtifactScope,
    resultsOpeningContext,
    resultsRouteUnavailable,
    selectedResult?.objectType,
  ]);
  const browseAllResults = useCallback((query: string) => {
    if (!reportSessionId) {
      closeResultsNavigator();
      return;
    }
    const normalizedQuery = query.trim().slice(0, 120);
    const expected = {
      sessionId: reportSessionId,
      scope: resultsArtifactScope,
    } as const;
    const currentUrl = new URL(window.location.href);
    const nextUrl = writeResultsExplorerRoute(
      currentUrl,
      null,
      reportSessionId,
    );
    const existingNavigation = readResultsNavigationState(
      window.history.state,
      expected,
    );
    const existingExplorer = readResultsExplorerState(
      window.history.state,
      expected,
    );
    const effectiveHistory = nextUrl.toString() === currentUrl.toString()
      ? "replace"
      : resultsQuickHistory;
    const baseExplorer = createResultsExplorerState({
      ...expected,
      entryId: effectiveHistory === "replace"
        ? existingExplorer?.entry_id
        : undefined,
    });
    const explorer = {
      ...baseExplorer,
      query: normalizedQuery,
      focus_key: selectedResultKey,
    };
    const navigation = createResultsNavigationState({
      ...expected,
      entryId: effectiveHistory === "replace"
        ? existingNavigation?.entry_id
        : undefined,
      openingContext: resultsQuickOpeningContext,
    });
    const nextState = withResultsHistoryState(window.history.state, navigation, explorer);
    if (effectiveHistory === "push") {
      window.history.pushState(withoutNextRouterHistoryMarkers(nextState), "", nextUrl.toString());
    } else {
      window.history.replaceState(withoutNextRouterHistoryMarkers(nextState), "", nextUrl.toString());
    }
    setResultsExplorerState(explorer);
    setResultsQuickQuery("");
    setReportReturnContext(null);
    reportReturnFocusRef.current = null;
    applyResultsRoute(parseResultsRoute(nextUrl.searchParams), navigation);
  }, [
    applyResultsRoute,
    closeResultsNavigator,
    reportSessionId,
    resultsArtifactScope,
    resultsQuickHistory,
    resultsQuickOpeningContext,
    selectedResultKey,
  ]);
  const replaceResultsExplorerState = useCallback((
    update: (current: ResultsHistoryStateV1) => ResultsHistoryStateV1,
  ) => {
    if (!reportSessionId || !resultsCapabilities.scaleNav) return;
    const expected = {
      sessionId: reportSessionId,
      scope: resultsArtifactScope,
    } as const;
    const current = readResultsExplorerState(window.history.state, expected)
      ?? resultsExplorerState
      ?? createResultsExplorerState(expected);
    const next = update(current);
    const navigation = readResultsNavigationState(window.history.state, expected)
      ?? createResultsNavigationState(expected);
    window.history.replaceState(
      withoutNextRouterHistoryMarkers(withResultsHistoryState(window.history.state, navigation, next)),
      "",
      window.location.href,
    );
    setResultsExplorerState(next);
  }, [
    reportSessionId,
    resultsArtifactScope,
    resultsCapabilities.scaleNav,
    resultsExplorerState,
  ]);
  const selectExplorerResult = useCallback((dto: ResultItemDTO) => {
    const item = projectResultsDTO([dto], resultModelLabels)[0];
    if (!item) return;
    const route = parseResultsRoute(new URL(window.location.href).searchParams);
    replaceResultsExplorerState((current) => ({
      ...current,
      focus_key: dto.key,
    }));
    navigateToResult(
      item.route,
      route.type === "explorer" ? "push" : "replace",
    );
  }, [navigateToResult, replaceResultsExplorerState, resultModelLabels]);
  const returnToExplorer = useCallback(() => {
    if (!reportSessionId) return;
    if (
      resultsOpeningContext.type === "explorer"
      && resultsExplorerState
      && resultsOpeningContext.explorerEntryId === resultsExplorerState.entry_id
    ) {
      window.history.back();
      return;
    }
    const expected = {
      sessionId: reportSessionId,
      scope: resultsArtifactScope,
    } as const;
    const explorer = {
      ...(resultsExplorerState ?? createResultsExplorerState(expected)),
      selected_key: null,
    };
    const currentNavigation = readResultsNavigationState(
      window.history.state,
      expected,
    );
    const navigation = createResultsNavigationState({
      ...expected,
      entryId: currentNavigation?.entry_id,
      openingContext: currentNavigation?.opening_context ?? { type: "external" },
    });
    const nextUrl = writeResultsExplorerRoute(
      new URL(window.location.href),
      null,
      reportSessionId,
    );
    window.history.replaceState(
      withoutNextRouterHistoryMarkers(withResultsHistoryState(window.history.state, navigation, explorer)),
      "",
      nextUrl.toString(),
    );
    setResultsExplorerState(explorer);
    applyResultsRoute(parseResultsRoute(nextUrl.searchParams), navigation);
  }, [
    applyResultsRoute,
    reportSessionId,
    resultsArtifactScope,
    resultsExplorerState,
    resultsOpeningContext,
  ]);
  const acceptExplorerChanges = useCallback(async () => {
    await completeResults.acceptChanges();
    // The effect below locates the current selection after commit, never the
    // selection captured before the refresh (which the user may have changed).
  }, [completeResults.acceptChanges]);
  useEffect(() => {
    if (
      resultsSurface !== "explorer"
      || !resultsCapabilities.scaleNav
      || !completeResultsActive
      || completeResults.loading
      || completeResults.error !== null
    ) return;
    const targetKey = resultsExplorerState?.selected_key
      ?? resultsExplorerState?.anchor?.key
      ?? null;
    if (!targetKey || completeResults.items.some((item) => item.key === targetKey)) return;
    void completeResults.locatePage(targetKey);
  }, [
    completeResults.error,
    completeResults.items,
    completeResults.loading,
    completeResults.locatePage,
    completeResultsActive,
    resultsCapabilities.scaleNav,
    resultsExplorerState?.anchor?.key,
    resultsExplorerState?.selected_key,
    resultsSurface,
  ]);
  useEffect(() => {
    if (
      resultsSurface !== "explorer"
      || !resultsCapabilities.scaleNav
      || !completeResultsActive
      || completeResults.error !== "results_filter_unavailable"
      || !resultsExplorerState
      || (
        resultsExplorerState.filters.lens === "all"
        && !resultsExplorerState.filters.technical_only
      )
    ) return;
    replaceResultsExplorerState((current) => ({
      ...current,
      filters: {
        ...current.filters,
        lens: "all",
        technical_only: false,
      },
    }));
    toast({ kind: "info", text: resultsT("explorer_filter_unavailable") });
  }, [
    completeResults.error,
    completeResultsActive,
    replaceResultsExplorerState,
    resultsCapabilities.scaleNav,
    resultsExplorerState,
    resultsSurface,
    resultsT,
  ]);
  useEffect(() => {
    if (
      resultsSurface !== "explorer"
      || !completeResults.unavailable
      || !reportSessionId
    ) return;
    const expected = {
      sessionId: reportSessionId,
      scope: resultsArtifactScope,
    } as const;
    const currentNavigation = readResultsNavigationState(
      window.history.state,
      expected,
    );
    const navigation = createResultsNavigationState({
      ...expected,
      entryId: currentNavigation?.entry_id,
    });
    const nextUrl = hasCanvas
      ? writeResultsRoute(new URL(window.location.href), { type: "report" }, reportSessionId)
      : clearResultsRoute(new URL(window.location.href), reportSessionId);
    window.history.replaceState(
      withoutNextRouterHistoryMarkers(withResultsHistoryState(window.history.state, navigation, null)),
      "",
      nextUrl.toString(),
    );
    setResultsExplorerState(null);
    applyResultsRoute(parseResultsRoute(nextUrl.searchParams), navigation);
    if (!hasCanvas && !reportOnly) setPanelMode("chat");
    toast({ kind: "info", text: resultsT("explorer_scope_unavailable") });
  }, [
    applyResultsRoute,
    completeResults.unavailable,
    hasCanvas,
    reportOnly,
    reportSessionId,
    resultsArtifactScope,
    resultsSurface,
    resultsT,
  ]);
  const selectQuickResult = useCallback((dto: ResultItemDTO) => {
    const item = projectResultsDTO([dto], resultModelLabels)[0];
    if (!item) return;
    if (item.key === selectedResultKey) {
      closeResultsNavigator();
      return;
    }
    setResultsQuickQuery("");
    setReportReturnContext(null);
    reportReturnFocusRef.current = null;
    pendingReportReturnRef.current = false;
    pendingReportReturnFocusRef.current = null;
    trackResultsWorkspace({
      action: "result_selected",
      resultType: item.objectType,
      group: item.primaryGroup,
      entryPoint: "navigator",
      mode: item.objectType === "artifact" ? "preview" : "none",
    });
    navigateToResult(
      item.route,
      resultsQuickHistory,
      resultsQuickOpeningContext,
    );
    requestAnimationFrame(() => resultsCurrentTitleRef.current?.focus());
  }, [
    navigateToResult,
    closeResultsNavigator,
    resultModelLabels,
    resultsQuickHistory,
    resultsQuickOpeningContext,
    selectedResultKey,
  ]);
  const selectResultsObject = useCallback((item: ResultItem) => {
    setResultsMobileListOpen(false);
    setReportReturnContext(null);
    reportReturnFocusRef.current = null;
    pendingReportReturnRef.current = false;
    pendingReportReturnFocusRef.current = null;
    trackResultsWorkspace({
      action: "result_selected",
      resultType: item.objectType,
      group: item.primaryGroup,
      entryPoint: "navigator",
      mode: item.objectType === "artifact" ? "preview" : "none",
    });
    navigateToResult(item.route);
    if (isResultsMobile) {
      requestAnimationFrame(() => resultsMobileBackRef.current?.focus());
    }
  }, [isResultsMobile, navigateToResult]);
  const recordResultOpenFailure = useCallback((errorClass: ResultsWorkspaceErrorClass) => {
    const failedResultKey = selectedResultKey
      ?? (dataFocusArtifactId ? `artifact:${dataFocusArtifactId}` : null);
    if (
      resultsCapabilities.scaleNav
      && resultsRecentScope
      && failedResultKey
    ) {
      setResultsRecentKeys(removeRecentResult(resultsRecentScope, failedResultKey));
    }
    trackResultsWorkspace({
      action: "open_failed",
      resultType: "artifact",
      entryPoint: "navigator",
      mode: artifactViewerMode,
      errorClass,
    });
  }, [
    artifactViewerMode,
    dataFocusArtifactId,
    resultsCapabilities.scaleNav,
    resultsRecentScope,
    selectedResultKey,
  ]);
  const recordResultDownload = useCallback((artifact: ArtifactItem) => {
    trackResultsWorkspace({
      action: "downloaded",
      resultType: "artifact",
      group: resolveSelectedResult(resultItems, {
        type: "artifact",
        artifactId: artifact.id,
      })?.primaryGroup ?? "none",
      entryPoint: "navigator",
      mode: artifactViewerMode,
    });
  }, [artifactViewerMode, resultItems]);

  useEffect(() => {
    if (!resultsRevealRequest) return;
    resultsInteractionEpochRef.current += 1;
    setResultsOpenedByUser(true);
    setPanelMode(window.matchMedia(DESKTOP_SPLIT_QUERY).matches ? "split" : "canvas");
  }, [resultsRevealRequest]);

  // W1-7 mobile-only tab: Chat | Canvas (CSS-shown ≤1023, hidden on desktop). Drives panelMode.
  // Workbench Spec (user decision 2026-09-28): `对话 / 成果` rather than `对话 / 报告`, since the right column also contains tables and files;
  // `报告` is wrong for conversations without a report; the collapsed restore strip's accessible name likewise becomes `重开成果`.
  const rightPaneLabel = creationPanelOpen
    ? creationPanelCount ? t("tab_results_count", { count: creationPanelCount }) : creationPanelTitle ?? tCreateAgent("header_title_create")
    : t("tab_results");
  // The pane switch counts objects (“成果 · 2”); the pane itself is named after the selected object.
  const canvasPaneLabel = creationPanelOpen ? creationPanelTitle ?? rightPaneLabel : rightPaneLabel;
  const mobileTabs = (
    hasResultsPane
    || Boolean(canvasObjectOpener)
    || Boolean(creationPanel)
    || (resultsWorkspaceEnabled && hasBrowsableResources)
  ) ? (
    <div className="v2-ws-mobile-tabs">
      {/* layout-redesign P4: v10 mobile.jsx Segmented — surface-2 pill, icons
          (not emoji), active = lifted surface + shadow. */}
      <Tabs
        value={hasRightPane && canvasMaximized ? "canvas" : "chat"}
        onValueChange={(pane) => {
          if (pane === "chat") {
            setPanelMode("chat");
            return;
          }
          if (!creationPanelOpen) {
            resultsInteractionEpochRef.current += 1;
            setResultsOpenedByUser(true);
          }
          setPanelMode("canvas");
          if (
            !creationPanelOpen
            &&
            resultsWorkspaceEnabled
            && !resultsCapabilities.scaleNav
            && isResultsMobile
            && !(resultsChooserAvailable && selectedTab === "report")
          ) {
            trackResultsWorkspace({
              action: "pane_opened",
              entryPoint: "header",
            });
            trackResultsWorkspace({
              action: "navigator_opened",
              entryPoint: "header",
            });
            setResultsMobileListOpen(true);
          }
        }}
      >
        <TabList appearance="segmented" className="v2-ws-mobile-tabs-pill" aria-label={`${t("tab_chat")} / ${rightPaneLabel}`}>
          <UiTab value="chat" className={clsx("v2-ws-mobile-tab", { active: !hasRightPane || !canvasMaximized })}>
            <Chat aria-hidden /> {t("tab_chat")}
          </UiTab>
          <UiTab
            value="canvas"
            className={clsx("v2-ws-mobile-tab", { active: hasRightPane && canvasMaximized })}
            // Unseen new results add a 7px accent dot and accessible name `成果，有新内容`.
            {...(resultsHasNew
              ? { "data-has-new": "1", "aria-label": `${rightPaneLabel}${t("results_has_new_suffix")}` }
              : {})}
          >
            <Doc aria-hidden /> {rightPaneLabel}
            {resultsHasNew ? <span className="v2-ws-new-dot" aria-hidden /> : null}
          </UiTab>
        </TabList>
      </Tabs>
    </div>
  ) : null;

  // LEFT — chat + pycells + trace strip + composer (hidden in the grid path when canvas-maxed)
  // Phase 1.7-D replay of the latest turn. Shared by the composer's replay affordance and the
  // failure card's Retry action (chat turn feedback D5), so both take the same path: server
  // replay when available, otherwise the question restored with its real references.
  const replayLastTurn = (
    onReplay && runner.turnId
      ? () => {
          const question = runner.user
            || (runner.messages.filter((m) => m.role === "user").slice(-1)[0]?.content ?? "");
          if (replayRestoresDraft) {
            // Without the submission protocol the server cannot replay a Turn: a plain resend would drop an
            // @ reference without saying so, so put the question back for the user to check and send —
            // with its real references when this page sent it, and its attachments.
            const attachments = runner.userAttachments ?? [];
            if (!question && attachments.length === 0) return false;
            const document = lastReferenceDocument && inputDocumentDisplayText(lastReferenceDocument) === question
              ? lastReferenceDocument
              : null;
            // Agents display as "@label", Skills, capabilities and MCP resources as "/label".
            const notice: RestoredDraftNotice = document || !MAY_HAVE_REFERENCES.test(question) ? "replay" : "replay_references_lost";
            if (attachments.length > 0) setReplayAttachmentPrefills(attachments);
            if (document) {
              restoredPrefillPendingRef.current = notice;
              setReplayPrefillDocument(document);
            } else if (question) {
              restoredPrefillPendingRef.current = notice;
              setComposerPrefill(question);
            } else {
              setRestoredDraftNotice(notice);
            }
            return true;
          }
          return onReplay(runner.turnId as string, question, runner.userAttachments ?? []);
        }
      : undefined
  );
  // The failure card's Retry goes through the composer's Replay (send guards, single flight), so the
  // workbench needs the composer control even when the page did not pass one.
  const localComposerControlRef = useRef<ComposerDraftControl | null>(null);
  const composerControlRef = draftControlRef ?? localComposerControlRef;
  const chatPane = homePane ? <div className="v2-ws-left home-work-page-content">{homePane}</div> : (
      <div ref={chatColumnRef} className="v2-ws-left" style={hasRightPane && canvasMaximized ? { display: "none" } : undefined}>
        <div className="v2-ws-left-body">
          {/* chat-ux-2026-08 batch C: viewport following attaches to the **scroll container**.
              scrollRef → .v2-messages (the actual scrolling layer)
              contentRef → .v2-messages-inner (ResizeObserver watches its height,
                            letting growth **within** steps trigger following; the old implementation watched
                            only step counts, never responding to internal height growth). */}
          <div className="v2-messages" ref={stick.scrollRef} tabIndex={-1}>
            <div className="v2-messages-inner" ref={stick.contentRef}>
              {/* Sprint τ: TurnStream owns the whole chat layout. Phase 1.7-B
                  rebuilt the composition tree per v8 (.chat-content-frame >
                  .bh-bot-block > BotByline / step-list / artifacts); legacy
                  .chat-panel / .tline / .ub / .bub / .ab are gone. HITL still
                  renders inline; SteerMessage now mounts in this shell above
                  the composer, OUTSIDE the BotTurn scroll body. */}
              <TurnStream
                runner={runner}
                onSend={onSend}
                onContinue={readOnly || reportOnly ? undefined : onContinue}
                onSolidify={onSolidify}
                onSolidifySession={onSolidifySession}
                onComposerPrefill={setComposerPrefill}
                onRetryTurn={replayLastTurn ? () => { composerControlRef.current?.requestReplay(); } : undefined}
                retryRestoresDraft={replayRestoresDraft}
                /* 0.26.3: wire runner.attachStream so HITLPickerCard's
                   /resume_turn flow reconnects to the same turn's SSE stream
                   without page reload. Prod 0.26.2 dogfood proved backend
                   resumed correctly (DB seq 12-19 with final_answer) but FE
                   stuck at "已 confirm · 续 turn 中" forever because EventSource
                   closed on done(clarification_pending) and was never reopened.
                   Sprint phase-plan-removal post-dogfood r2 (2026-05-19) — fix
                   arity mismatch: HITLPickerCard fires onResumed(turnId,
                   streamUrl) with TWO args, but runner.attachStream only takes
                   streamUrl. The naked wire silently treated turnId AS the
                   streamUrl → EventSource opened on /t_xxxx → Next.js 404 →
                   resume turn rendered nothing live. */
                onAttachStream={(_turnId, streamUrl) => runner.attachStream?.(streamUrl)}
              />
            </div>
          </div>
          {/* chat-ux-2026-08 batch C: `回到最新` float. Currently **entirely absent**:
              on 116, running viewport distance from bottom increased monotonically 0→230→581→1418px, with
              no return control. Appears only when following is disengaged; click to return to bottom and resume following.
              Inside .v2-ws-left-body, **after** the scroll container, it floats above content and
              composer without occupying layout space or obscuring the composer. */}
          {!stick.following ? (
            <button
              type="button"
              className="bh-jump-latest"
              data-testid="jump-to-latest"
              onClick={stick.jumpToLatest}
            >
              <LucideGlyph icon={LucideArrowDown} size={13} aria-hidden />
              {tChat("jump_to_latest")}
              {stick.pendingCount > 0 ? (
                <span className="bh-jump-latest__n">{stick.pendingCount}</span>
              ) : null}
            </button>
          ) : null}
        </div>
        {/* V6 Phase B: retire InlineTraceStrip; the step_card list already renders full trace in chat flow.
            Phase C adds IntentStrip to replace the old plan progress strip's role. */}
        <PinnedAttachmentsStrip
          sessionId={runner.sessionId}
          refreshKey={runner.running ? "running" : (runner.turnId ?? "init")}
        />
        {steerAcks.length > 0 ? (
          <div
            className="bh-steer-mount"
            style={{ padding: "0 24px", maxWidth: 740, margin: "0 auto", width: "100%" }}
          >
            {steerAcks.map((ack) => (
              <SteerMessage
                key={ack.msgId}
                who={t("steer_you")}
                time={
                  ack.ts
                    ? new Date(ack.ts).toLocaleTimeString("zh-CN", {
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : ""
                }
                text={ack.text ?? ""}
                emoji={ack.emoji}
                ackText={t("steer_ack")}
              />
            ))}
          </div>
        ) : null}
        {submissionRecoveryPending && onRetryRecoveredSubmission && onAbandonRecoveredSubmission ? (
          <ComposerRecoveryNotice
            variant={submissionRecoveryVariant}
            allowAbandon={submissionRecoveryAllowAbandon}
            onRetry={onRetryRecoveredSubmission}
            onAbandon={onAbandonRecoveredSubmission}
          />
        ) : null}
        <V2Composer
          surface="workbench"
          draftControlRef={composerControlRef}
          hidden={planAwaitingApproval}
          fileDropTargetRef={chatColumnRef}
          onSend={(text, attachments, draftRevision, submissionContext) => {
            setRestoredDraftNotice(null);
            return onSend(text, attachments, draftRevision, submissionContext);
          }}
          onSteer={onSteer}
          onResolveDraftSubmissionProtocol={onResolveDraftSubmissionProtocol}
          onCaptureSubmissionWireSnapshot={onCaptureSubmissionWireSnapshot}
          disabled={runner.running}
          running={runner.running}
          stopRequested={stopRequested}
          readOnly={readOnly}
          recoveryLocked={submissionRecoveryLocked}
          draftPersistenceScope={draftPersistenceScope}
          onPendingActionHydrated={onPendingActionHydrated}
          draftReconcileEpoch={draftReconcileEpoch}
          draftPersistenceFailureMode={draftPersistenceFailureMode}
          statusMessage={
            restoredDraftNotice
              ? tComposer(RESTORED_DRAFT_NOTICE_KEYS[restoredDraftNotice])
              : null
          }
          // Feishu 07.06 #4: after stopping, prefill the stopped question in composer for editing and resubmission.
          // Reuse composerPrefill→prefillText (previously used only by HITL `换个问法`).
          onCancel={onCancel ? async () => {
            const stopped = runner.user
              || (runner.messages.filter((m) => m.role === "user").slice(-1)[0]?.content ?? "");
            const accepted = await onCancel();
            if (accepted !== false && stopped) {
              restoredPrefillPendingRef.current = "stopped";
              setComposerPrefill(stopped);
            }
          } : undefined}
          sessionTexts={runner.messages
            .filter((m) => m.role === "user")
            .map((m) => m.content ?? "")
            .filter((t) => t.length > 0)
            .slice(-5)  // The last 5 messages suffice as preferred Tab-completion candidates.
          }
          prefillText={composerPrefill}
          submitRequest={composerSubmitRequest}
          onPrefillConsumed={(disposition) => {
            setComposerPrefill(null);
            onPrefillConsumed?.(disposition);
            const restored = restoredPrefillPendingRef.current;
            if (!restored) return;
            restoredPrefillPendingRef.current = null;
            setRestoredDraftNotice(disposition === "applied" ? restored : null);
          }}
          prefillInputDocument={replayPrefillDocument ?? prefillInputDocument}
          onPrefillInputDocumentConsumed={(disposition) => {
            const document = replayPrefillDocument;
            if (!document) {
              onPrefillInputDocumentConsumed?.(disposition);
              return;
            }
            setReplayPrefillDocument(null);
            const restored = restoredPrefillPendingRef.current;
            if (!restored) return;
            if (disposition !== "applied" && document) {
              // References cannot be restored here: fall back to the text, and say the @ must be picked again.
              restoredPrefillPendingRef.current = "replay_references_lost";
              setComposerPrefill(inputDocumentDisplayText(document));
              return;
            }
            restoredPrefillPendingRef.current = null;
            setRestoredDraftNotice(disposition === "applied" ? restored : null);
          }}
          prefillAttachments={composerAttach}
          onPrefillAttachmentsConsumed={(consumed) => {
            setConsumedComposerPrefillKeys((current) => Array.from(new Set([
              ...current,
              ...consumed.map(composerPrefillKey),
            ])));
            const consumedKeys = new Set(consumed.map(composerPrefillKey));
            setReplayAttachmentPrefills((current) => current.filter((attachment) => !consumedKeys.has(composerPrefillKey(attachment))));
            const consumedResultIds = new Set(
              consumed
                .filter((attachment) => attachment.referenceKind === "result")
                .map((attachment) => attachment.fileId),
            );
            if (consumedResultIds.size === 0) return;
            setResultReferencePrefills((current) => current.filter(
              (attachment) => !consumedResultIds.has(attachment.fileId),
            ));
          }}
          // Replay source for the failure card's Retry (D4). lastUserText falls back to the
          // last user message in runner.messages so it survives runner.user
          // being cleared at turn-done.
          lastUserText={
            runner.user
            || (runner.messages.filter((m) => m.role === "user").slice(-1)[0]?.content ?? null)
          }
          lastUserAttachments={runner.userAttachments ?? []}
          onReplay={replayLastTurn}
        />
      </div>
  );

  const reportWriting = canvas.status === "streaming" || canvas.status === "outline";
  // Report Canvas Chrome Spec §1: export disabled while writing, with a persistent reason below the toolbar
  // (previously clickable and rejected only during prepare).
  const exportBlockedByWriting = reportWriting;
  // Below 560, export moves into `更多` (`导出` submenu); mobile reading also moves `编辑` there, removing the edit group from the toolbar.
  const reportToolbarNarrow = resultsPaneWidth > 0 && resultsPaneWidth < 560;
  const reportPhoneReading = isResultsMobile && !reportEditing && !reportWriting;
  // Mobile reading combines results header and report toolbar into `返回 · 标题 · 状态 · 分享 · ⋯` (Report Canvas Chrome Spec §10).
  // Reuse the compact reading bar for both navigator variants; each retains its own open action.
  const reportPhoneBar = reportPhoneReading
    && hasCanvas
    && !canvasObjectOpener
    && resultsWorkspaceShellEnabled
    && selectedTab === "report"
    && !resultsCapabilityUnresolved
    && resultsSurface === "viewer"
    && !resultsMobileListOpen;
  const openResultsMobileList = () => {
    trackResultsWorkspace({
      action: "navigator_opened",
      entryPoint: "header",
    });
    setResultsMobileListOpen(true);
  };
  const reportCanEditFromMenu = reportPhoneReading && Boolean(canvas.canvasId) && !readOnly && !reportOnly;
  const reportWritingHintId = useId();
  const reportExportAction = (
    <div className="ws-menu-wrap">
      <button
        ref={exportTriggerRef}
        type="button"
        className="ws-export"
        disabled={exportBusy || !canvas.canvasId || exportBlockedByWriting}
        title={t("hdr_export")}
        {...(exportBlockedByWriting ? { "aria-describedby": reportWritingHintId } : null)}
        {...exportMenu.getReferenceProps()}
      >
        <FileDown size={14} />
        {t("hdr_export")}
        <Caret className="legacy-ws-export-caret" />
      </button>
      <ExportMenu
        menu={exportMenu}
        busy={exportBusy}
        onPick={triggerExport}
        onOpenHistory={() => setExportsDrawerOpen(true)}
      />
    </div>
  );
  // Report Canvas Chrome Spec §§1,2: standalone toolbar share icon opens the existing share dialog;
  // `复制权限链接` stays in `更多`. Visibility matches that action (report conversation present, not shared read-only);
  // the sharing API determines audience-edit permission from resource ownership and Team governance.
  const reportShareAction = canvas.canvasId && reportSessionId && !reportOnly && !readOnly ? (
    <button
      ref={reportShareButtonRef}
      type="button"
      className="v2-icon-btn results-report-share"
      title={t("hdr_share")}
      aria-label={t("hdr_share_aria")}
      aria-haspopup="dialog"
      onClick={() => setReportShareOpen(true)}
    >
      <ShIcon name="share" size={15} />
    </button>
  ) : null;
  const reportMoreAction = canvas.canvasId && !reportOnly ? (
    <div className="ws-menu-wrap">
      <button
        ref={moreMenu.refs.setReference}
        type="button"
        className="v2-icon-btn"
        title={t("hdr_more")}
        aria-label={t("hdr_more_aria")}
        {...moreMenu.getReferenceProps()}
      >
        <MoreH size={16} />
      </button>
      <MenuFloating menu={moreMenu}>
        <div ref={moreMenu.refs.setFloating} {...moreMenu.getFloatingProps({ className: "ws-menu" })}>
          {/* Appearance refresh batch 4b remainder: `导出` submenu on narrow toolbars and `编辑` during mobile reading. */}
          {reportToolbarNarrow ? (
            exportBlockedByWriting || exportBusy || !canvas.canvasId ? (
              <DisabledMenuItem
                label={t("hdr_export")}
                reason={exportBlockedByWriting ? t("export_writing_reason") : null}
              />
            ) : (
              <MenuSubmenu label={t("hdr_export")} icon={<FileDown size={15} className="ws-menu-icon" />}>
                <ExportMenuItems
                  busy={exportBusy}
                  onPick={triggerExport}
                  onOpenHistory={() => setExportsDrawerOpen(true)}
                />
              </MenuSubmenu>
            )
          ) : null}
          {reportPhoneReading ? (
            <WorkbenchMenuItem onSelect={() => reportViewRef.current?.openHistory()}>
              {t("history")}
            </WorkbenchMenuItem>
          ) : null}
          {reportCanEditFromMenu ? (
            <WorkbenchMenuItem onSelect={() => reportViewRef.current?.enterEditMode()}>
              {t("edit_this_report")}
            </WorkbenchMenuItem>
          ) : null}
          {reportSessionId && !reportOnly ? (
            <WorkbenchMenuItem onSelect={shareCurrentReport}>
              {t("hdr_copy_perm_link")}
            </WorkbenchMenuItem>
          ) : null}
          {/* The default (active) status shows no toolbar chip, so Mark as Reviewed / Archive live here, as on the
              reports list row "⋯". A non-default status keeps its chip and menu. */}
          {canvas.canvasId && !readOnly && !reportOnly && !reportEditing ? (
            <ActiveReportStatusMenuItems
              canvasId={canvas.canvasId}
              renderItem={(item) => (
                <WorkbenchMenuItem key={item.key} onSelect={item.onSelect}>
                  {item.label}
                </WorkbenchMenuItem>
              )}
            />
          ) : null}
          {/* Batch 4b: writing progress lives in the toolbar, so `复制轮次 ID` (troubleshooting) is here;
              show only while writing with a turn ID available. */}
          {reportWriting && canvas.currentTurnId ? (
            <CopyTurnIdMenuItem turnId={canvas.currentTurnId} />
          ) : null}
          {!readOnly && !reportOnly ? (
            <>
              {/* Behavior batch A: clearing deletes all report content and requires confirmation; mark as a red dangerous action with `…`. */}
              <WorkbenchMenuItem
                className="danger"
                onSelect={() => void clearCurrentReport()}
              >
                <Trash size={15} className="ws-menu-icon" />
                {withConfirmEllipsis(t("hdr_clear_report"))}
              </WorkbenchMenuItem>
              <WorkbenchMenuItem className="danger" onSelect={() => void deleteCurrentReport()}>
                {withConfirmEllipsis(t("hdr_delete_report"))}
              </WorkbenchMenuItem>
            </>
          ) : null}
        </div>
      </MenuFloating>
    </div>
  ) : null;
  const reportSurface = !hasCanvas && !reportOnly && (resultsWorkspaceEnabled || canvasObjectList) ? (
    <section className={resultsNavigatorStyles.landing} aria-label={resultsT("choose_result_title")}>
      <h2>{resultsT("choose_result_title")}</h2>
      <p className={resultsNavigatorStyles.hint}>{resultsT("choose_result_hint")}</p>
      {canvasObjectList}
      {resultsWorkspaceEnabled && !resultsNavigatorPinned && !resultsMobileListOpen ? <ResultsNavigator
        items={resultsNavigatorItems}
        open
        query={resultsNavigatorQuery}
        selection={{ type: "none" }}
        presentation="inline"
        listState={resultsNavigatorListState}
        onClose={() => {}}
        onQueryChange={setResultsNavigatorQuery}
        onSelect={selectResultsObject}
        onLoadMore={completeResultsActive ? () => { void completeResults.loadMore(); } : undefined}
        onRetry={completeResultsActive ? () => { void completeResults.retry(); } : retryResultArtifacts}
      /> : null}
    </section>
  ) : !hasCanvas ? (
    <div className="v2-empty">
      <p>{t("empty_main")}</p>
    </div>
  ) : (
    <>
      {resultsWorkspaceEnabled ? (
        <div
          className="results-report-object-toolbar"
          aria-label={resultsT("report_actions")}
          {...(reportPhoneReading ? { "data-phone-reading": "true" } : null)}
        >
          <span>{resultsT("report")}</span>
          {reportPhoneBar ? (
            <>
              {/* Merged mobile reading toolbar: move back and title here from results header (same refs, focus return and accessible targets unchanged). */}
              <button
                ref={resultsMobileBackRef}
                type="button"
                className="results-mobile-back results-report-phone-back"
                aria-label={resultsT("back_to_results")}
                title={resultsT("back_to_results")}
                onClick={resultsCapabilities.scaleNav ? openResultsQuick : openResultsMobileList}
              >
                <span aria-hidden><CharIcon ch="←" aria-hidden /></span>
              </button>
              <strong
                ref={resultsCurrentTitleRef}
                className="results-current-title results-report-phone-title"
                title={resultsCurrentTitle}
                tabIndex={-1}
              >
                {resultsCurrentTitle}
              </strong>
            </>
          ) : null}
          {/* Status group (writing progress or status badge, portaled by ReportView). */}
          <div ref={setReportStatusHost} className="results-report-status-group" />
          <div>
            {/* Mobile reading removes the edit group from toolbar (`编辑` lives in `更多`). */}
            {reportPhoneReading ? null : (
              <div
                ref={setReportEditControlsHost}
                className="results-report-edit-controls"
              />
            )}
            {reportToolbarNarrow ? null : reportExportAction}
            {reportShareAction}
            {reportMoreAction}
          </div>
          {/* Persistent writing-time reasons for unavailable editing/export (export button reads via aria-describedby). */}
          {exportBlockedByWriting ? (
            <p id={reportWritingHintId} className="results-report-writing-hint">
              {t("export_writing_hint")}
            </p>
          ) : null}
        </div>
      ) : null}
      <ReportView
        ref={reportViewRef}
        presentation={resultsWorkspaceEnabled ? "results-reading" : "canvas"}
        sections={reportSections}
        exportSections={exportVisibleSections}
        sourceRefs={canvas.sourceRefs}
        seqAuthoritative={canvas.seqAuthoritative}
        turnCitations={canvas.turnCitations}
        canvasId={canvas.canvasId}
        sessionId={canvas.status === "done" ? reportSessionId : null}
        canEdit={Boolean(canvas.canvasId) && !readOnly && !reportOnly}
        showEditControls={
          !readOnly
          && !reportOnly
          && canvas.status !== "streaming"
          && canvas.status !== "outline"
        }
        hideHistoryControl={resultsWorkspaceEnabled && reportPhoneReading && !reportOnly}
        editControlsHost={resultsWorkspaceEnabled ? reportEditControlsHost : undefined}
        statusHost={resultsWorkspaceEnabled ? reportStatusHost : undefined}
        turnTerminalStatus={runner.terminalStatus ?? null}
        turnTerminalTurnId={runner.turnId ?? null}
        turnReportSectionsSaved={runner.reportSectionsSaved ?? null}
        streamConnection={runnerStreamConnection(runner)}
        onEditModeChange={setReportEditing}
        readOnly={readOnly}
        readOnlyReason={readOnly ? t("readonly_account") : undefined}
        isStreaming={canvas.status === "streaming" || canvas.status === "outline"}
        currentTurnId={canvas.currentTurnId}
      />
    </>
  );
  const activeReportReturnContext = reportReturnContext?.artifactId === dataFocusArtifactId
    ? reportReturnContext
    : resultsOpeningContext.type === "report-inspection" && dataFocusArtifactId
      ? { artifactId: dataFocusArtifactId, label: resultsT("report") }
      : null;
  const activeSourceReturn = activeReportReturnContext
    ? {
        label: resultsT("back_to_report"),
        detail: activeReportReturnContext.label,
        ariaLabel: resultsT("back_to_report_named", {
          section: activeReportReturnContext.label,
        }),
      }
    : resultsOpeningContext.type === "code"
      ? {
          label: resultsT("back_to_code"),
          detail: null,
          ariaLabel: resultsT("back_to_code"),
        }
      : resultsOpeningContext.type === "chat"
        ? {
            label: resultsT("back_to_chat"),
            detail: null,
            ariaLabel: resultsT("back_to_chat"),
          }
        : null;
  const dataSurface = (
    <DataTabFiles
      sessionId={reportOnly ? reportSessionId : runner.sessionId}
      scope={reportOnly ? "report" : "session"}
      focusArtifactId={dataFocusArtifactId}
      viewerMode={resultsWorkspaceEnabled ? artifactViewerMode : undefined}
      onViewerModeChange={
        resultsWorkspaceEnabled && dataFocusArtifactId
          ? (mode, history = "push") => {
              trackResultsWorkspace({
                action: "viewer_mode_changed",
                resultType: "artifact",
                entryPoint: "navigator",
                mode,
              });
              navigateToResult({
                type: "artifact",
                artifactId: dataFocusArtifactId,
                mode,
              }, (
                activeSourceReturn
                || resultsOpeningContext.type === "explorer"
              ) ? "replace" : history);
            }
          : undefined
      }
      onFocusedArtifactChange={resultsWorkspaceEnabled ? undefined : (artifactId) => {
        resultsInteractionEpochRef.current += 1;
        if (artifactId) {
          if (resultsWorkspaceEnabled) {
            navigateToResult({
              type: "artifact",
              artifactId,
              mode: "preview",
            });
          } else {
            setDataFocusArtifactId(artifactId);
          }
        } else if (resultsWorkspaceEnabled) {
          if (hasCanvas) navigateToResult({ type: "report" });
          else {
            clearResultsLocation("push");
            setDataFocusArtifactId(null);
            setArtifactViewerMode("preview");
            setResultsRouteUnavailable(false);
            setResultsOpenedByUser(true);
            setResultsNavigatorOpen(true);
          }
        } else {
          setDataFocusArtifactId(null);
        }
      }}
      viewerOnly={resultsWorkspaceEnabled && Boolean(dataFocusArtifactId)}
      artifactItems={resultsWorkspaceEnabled ? viewerArtifactItems : undefined}
      artifactItemsError={resultsWorkspaceEnabled
        ? completeResultsActive
          ? completeResults.unavailable ? "unavailable" : null
          : resultArtifactsError
        : undefined}
      onArtifactItemsRetry={retryViewerArtifactItems}
      onRegenerateExport={readOnly || reportOnly ? undefined : triggerExport}
      onReferenceArtifact={
        !readOnly && !reportOnly && runner.sessionId
          ? referenceArtifactInChat
          : undefined
      }
      onDownloadArtifact={recordResultDownload}
      resultCapabilities={selectedResult?.objectType === "artifact"
        ? selectedResult.capabilities
        : undefined}
      resultRole={selectedArtifactRole}
      downloadLabel={selectedResult?.version
        ? resultsT(selectedArtifactIsHistorical
            ? "download_this_version"
            : "download_current_version")
        : undefined}
      onOpenArtifactHistory={selectedResult?.version
        ? () => setExportsDrawerOpen(true)
        : undefined}
      onOpenFailed={recordResultOpenFailure}
    />
  );
  const codeSurface = (
    <CodeTab
      sections={canvas.sections}
      executionSteps={pythonExecutionSteps}
      legacyPyCells={resultsWorkspaceEnabled ? undefined : canvas.pyCells}
    />
  );
  const resultsWorkspaceViewer = (
    <>
      {resultsRouteUnavailable ? (
        <div className="v2-empty" role="alert">
          <p>{resultsT("missing_object")}</p>
        </div>
      ) : null}
      <div
        className="results-object-surface"
        hidden={resultsRouteUnavailable || selectedTab !== "report"}
        aria-hidden={resultsRouteUnavailable || selectedTab !== "report"}
      >
        {reportSurface}
      </div>
      {!resultsRouteUnavailable && selectedTab === "data" ? (
        <div className="results-object-surface results-object-surface--artifact">
          {activeSourceReturn ? (
            <div className="results-return-context">
              <button
                type="button"
                onClick={returnToOpeningSource}
                aria-label={activeSourceReturn.ariaLabel}
              >
                <span aria-hidden><CharIcon ch="←" aria-hidden /></span>
                <span>{activeSourceReturn.label}</span>
                {activeSourceReturn.detail ? (
                  <>
                    <span aria-hidden>·</span>
                    <strong>{activeSourceReturn.detail}</strong>
                  </>
                ) : null}
              </button>
            </div>
          ) : null}
          {dataSurface}
        </div>
      ) : null}
      {!reportOnly ? (
        <div
          className="results-object-surface"
          hidden={resultsRouteUnavailable || selectedTab !== "sql"}
          aria-hidden={resultsRouteUnavailable || selectedTab !== "sql"}
        >
          {codeSurface}
        </div>
      ) : null}
    </>
  );
  const resultsViewerBodyClass = clsx("v2-ws-right-body", {
    "results-body--report": resultsWorkspaceShellEnabled && selectedTab === "report",
    "results-body--artifact": resultsWorkspaceShellEnabled && selectedTab === "data",
    "results-body--code": resultsWorkspaceShellEnabled && selectedTab === "sql",
    "results-mobile-detail-hidden": (
      resultsWorkspaceShellEnabled
      && !resultsCapabilities.scaleNav
      && isResultsMobile
      && resultsMobileListOpen
    ),
  });
  const onResultsViewerScroll = (element: HTMLDivElement) => {
    if (selectedTab === "report") reportScrollTopRef.current = element.scrollTop;
    else if (selectedTab === "sql") codeScrollTopRef.current = element.scrollTop;
  };

  // RIGHT — canvas tabs + ReportView (collapsed when no sections in the grid path)
  const canvasPane = (
      <div
        className="v2-ws-right"
        style={!hasResultsPane && !canvasMaximized ? { display: "none" } : undefined}
        {...(reportPhoneBar ? { "data-phone-reading-bar": "true" } : null)}
      >
        {/* layout-redesign v10 — single clean header bar (canvas-panel.jsx HeaderBar):
            segmented tabs · export dropdown (folds PDF/PPT/XLS) · fullscreen toggle ·
            collapse-to-chat (desktop) · "更多" menu (history / clear / delete).
            Chrome only — every export/clear/delete/history handler below is verbatim. */}
        <div className="v2-ws-right-head">
          {canvasObjectOpener ? <span ref={documentOpenerRef}>{canvasObjectOpener}</span> : null}
          {resultsWorkspaceShellEnabled ? (
            <div className="results-head-context">
              <button
                ref={resultsTriggerRef}
                type="button"
                className="results-trigger"
                disabled={resultsCapabilityUnresolved}
                aria-haspopup={
                  resultsCapabilities.scaleNav
                    ? "dialog"
                    : isResultsMobile || resultsNavigatorPinned
                      ? undefined
                      : "dialog"
                }
                aria-expanded={
                  resultsCapabilities.scaleNav
                    ? resultsNavigatorOpen
                    : isResultsMobile
                    ? resultsMobileListOpen
                    : resultsNavigatorPinned || resultsNavigatorOpen
                }
                onClick={() => {
                  if (resultsCapabilityUnresolved) return;
                  resultsInteractionEpochRef.current += 1;
                  setExportMenuOpen(false);
                  setMoreMenuOpen(false);
                  if (resultsCapabilities.scaleNav) {
                    if (resultsNavigatorOpen) closeResultsNavigator();
                    else openResultsQuick();
                    return;
                  }
                  if (isResultsMobile) {
                    if (!resultsMobileListOpen) {
                      trackResultsWorkspace({
                        action: "navigator_opened",
                        entryPoint: "header",
                      });
                    }
                    setResultsMobileListOpen(true);
                  } else if (resultsNavigatorPinned) {
                    setResultsNavigatorPin(false);
                  } else {
                    if (!resultsNavigatorOpen) {
                      trackResultsWorkspace({
                        action: "navigator_opened",
                        entryPoint: "header",
                      });
                    }
                    setResultsNavigatorOpen((open) => !open);
                  }
                }}
              >
                <span>{resultsT("title")}</span>
                <Caret aria-hidden />
              </button>
              {!resultsCapabilityUnresolved
              && !resultsCapabilities.scaleNav
              && isResultsMobile
              && !resultsMobileListOpen
              && !reportPhoneBar
              && !resultsChooserSelected ? (
                <button
                  ref={resultsMobileBackRef}
                  type="button"
                  className="results-mobile-back"
                  onClick={() => {
                    trackResultsWorkspace({
                      action: "navigator_opened",
                      entryPoint: "header",
                    });
                    setResultsMobileListOpen(true);
                  }}
                >
                  <span aria-hidden><CharIcon ch="←" aria-hidden /></span>
                  {resultsT("back_to_results")}
                </button>
              ) : null}
              {reportPhoneBar || resultsChooserSelected ? null : (
                <strong
                  ref={resultsCurrentTitleRef}
                  className="results-current-title"
                  title={resultsCurrentTitle}
                  tabIndex={-1}
                >
                  {resultsCurrentTitle}
                </strong>
              )}
              {resultsCapabilities.scaleNav ? (
                <ResultsQuickSwitcher
                  activeKey={selectedResultKey}
                  authorizedTotal={quickResults.authorizedTotal}
                  compact={
                    isResultsMobile
                    || (resultsPaneWidth > 0 && resultsPaneWidth < 640)
                  }
                  currentKey={selectedResultKey}
                  error={quickResults.error}
                  hasMore={quickResults.hasMore}
                  items={quickResults.items}
                  loading={quickResults.loading}
                  matchedTotal={quickResults.matchedTotal}
                  open={resultsNavigatorOpen}
                  query={resultsQuickQuery}
                  recentKeys={resultsRecentKeys}
                  triggerRef={resultsTriggerRef}
                  onBrowseAll={browseAllResults}
                  onClose={closeResultsNavigator}
                  onQueryChange={setResultsQuickQuery}
                  onRetry={() => { void quickResults.retry(); }}
                  onSelect={selectQuickResult}
                />
              ) : !resultsCapabilityUnresolved
                && !isResultsMobile
                && !resultsNavigatorPinned ? (
                <ResultsNavigator
                  items={resultsNavigatorItems}
                  open={resultsNavigatorOpen}
                  query={resultsNavigatorQuery}
                  selection={resultsSelection}
                  canPin={resultsCanPin}
                  listState={resultsNavigatorListState}
                  onPinnedChange={setResultsNavigatorPin}
                  onClose={closeResultsNavigator}
                  onQueryChange={setResultsNavigatorQuery}
                  onSelect={selectResultsObject}
                  onLoadMore={completeResultsActive
                    ? () => { void completeResults.loadMore(); }
                    : undefined}
                  onRetry={completeResultsActive
                    ? () => { void completeResults.retry(); }
                    : retryResultArtifacts}
                />
              ) : null}
            </div>
           ) : (
             <Tabs value={selectedTab} onValueChange={setTab}>
               <TabList appearance="segmented" className="tabs" aria-label={t("tabs_aria")}>
                 <UiTab value="report" className={clsx("tab", { active: selectedTab === "report" })}>{t("tab_report")}</UiTab>
                 <UiTab value="data" className={clsx("tab", { active: selectedTab === "data" })}>{t("tab_data")}</UiTab>
                 {!reportOnly ? (
                   <UiTab value="sql" className={clsx("tab", { active: selectedTab === "sql" })}>{t("tab_code")}</UiTab>
                 ) : null}
               </TabList>
             </Tabs>
           )}
          <div className="r-actions">
            {/* Export — one button folds PDF / PPT / XLS (triggerExport unchanged). */}
            {/* followup-context-deck #7: always render export, disabled/grayed without a canvas;
                commit 79a54412 (layout v10) wrapped it in {canvas.canvasId ? ...:null}, making
                the button disappear with an empty canvasId (originally disabled/grayed). Restore disabled-not-hidden. */}
            {!resultsWorkspaceShellEnabled ? reportExportAction : null}
            {/* Fullscreen — maximize / restore. Feishu 07.06 #26 review[8]: shared read-only reports
                have no conversation to return to, so omit fullscreen-exit / return-to-conversation controls
                (otherwise clicking exits fullscreen canvas into a broken split/chat layout). */}
            {!reportOnly ? (
              <button
                type="button"
                className="v2-icon-btn"
                onClick={() => setPanelMode((m) => (m === "canvas" ? "split" : "canvas"))}
                title={maximizeShortcut && isDesktop
                  ? `${canvasMaximized ? t("max_exit") : t("max_enter")} (${maximizeShortcut})`
                  : canvasMaximized ? t("max_exit") : t("max_enter")}
                aria-label={canvasMaximized ? t("max_exit") : t("max_enter")}
                {...(maximizeShortcut && isDesktop
                  ? { "aria-keyshortcuts": resultsMaximizeAriaKeyshortcuts(shortcutPlatform, backslashKey ?? undefined) }
                  : {})}
              >
                {canvasMaximized ? <Minimize size={16} /> : <Maximize size={16} />}
              </button>
            ) : null}
            {/* Close · return to chat-only (desktop three states; v10 'x'). Dragging the divider beyond min also triggers setPanelMode('chat'). */}
            {isDesktop && !reportOnly ? (
              <button
                type="button"
                className="v2-icon-btn"
                onClick={closeResultsPane}
                title={resultsWorkspaceShellEnabled
                  ? resultsT("close_to_chat")
                  : t("hdr_close_to_chat")}
                aria-label={resultsWorkspaceShellEnabled
                  ? resultsT("close_to_chat")
                  : t("hdr_close_to_chat_aria")}
              >
                <X size={15} />
              </button>
            ) : null}
            {/* More — history (RevisionRail) / clear / delete; handlers verbatim. */}
            {!resultsWorkspaceShellEnabled ? reportMoreAction : null}
          </div>
          {/* click-away: closes whichever header menu is open */}
        </div>
        {reportConfirmDialog}
        {reportShareOpen && reportSessionId ? (
          <ShareDialog
            kind="report"
            resourceId={reportSessionId}
            label={canvas.title || resultsT("report")}
            returnFocusRef={reportShareButtonRef}
            onClose={() => setReportShareOpen(false)}
          />
        ) : null}
        <ExportToast
          state={exportCtl.state}
          onDismiss={exportCtl.dismiss}
          onRetry={exportCtl.retry}
          onRetryDefaultTheme={exportCtl.retryWithDefaultTheme}
          onOpenHistory={() => setExportsDrawerOpen(true)}
        />
        <ExportsDrawer
          canvasId={canvas.canvasId}
          open={exportsDrawerOpen}
          onClose={() => setExportsDrawerOpen(false)}
          onRegenerate={readOnly || reportOnly ? undefined : triggerExport}
          returnFocusRef={exportButtonRef}
        />
        <div ref={resultsWorkspaceBodyRef} className="results-workspace-body">
        {resultsWorkspaceEnabled
        && !resultsCapabilities.scaleNav
        && resultsNavigatorPinned ? (
          <ResultsNavigator
            items={resultsNavigatorItems}
            open
            query={resultsNavigatorQuery}
            selection={resultsSelection}
            presentation="pinned"
            canPin
            listState={resultsNavigatorListState}
            onPinnedChange={setResultsNavigatorPin}
            onClose={() => setResultsNavigatorPin(false)}
            onQueryChange={setResultsNavigatorQuery}
            onSelect={selectResultsObject}
            onLoadMore={completeResultsActive
              ? () => { void completeResults.loadMore(); }
              : undefined}
            onRetry={completeResultsActive
              ? () => { void completeResults.retry(); }
              : retryResultArtifacts}
          />
        ) : null}
        {resultsWorkspaceEnabled
        && !resultsCapabilities.scaleNav
        && isResultsMobile
        && resultsMobileListOpen ? (
          <ResultsNavigator
            items={resultsNavigatorItems}
            open
            query={resultsNavigatorQuery}
            selection={resultsSelection}
            presentation="mobile"
            listState={resultsNavigatorListState}
            onClose={() => setResultsMobileListOpen(false)}
            onQueryChange={setResultsNavigatorQuery}
            onSelect={selectResultsObject}
            onLoadMore={completeResultsActive
              ? () => { void completeResults.loadMore(); }
              : undefined}
            onRetry={completeResultsActive
              ? () => { void completeResults.retry(); }
              : retryResultArtifacts}
          />
        ) : null}
        {resultsCapabilityUnresolved ? (
          <div className="v2-ws-right-body results-capability-state" aria-busy={!resultsCapabilities.error}>
            <div className="results-explorer-state" role={resultsCapabilities.error ? "alert" : "status"}>
              <strong>{resultsT(resultsCapabilities.error
                ? resultsCapabilities.error === "auth"
                  ? "capability_auth_error"
                  : resultsCapabilities.error === "timeout"
                    ? "capability_timeout"
                    : "capability_error"
                : "list_loading")}</strong>
              {resultsCapabilities.error ? (
                <button type="button" onClick={() => { void retryResultsCapabilities(); }}>
                  {resultsT("list_retry")}
                </button>
              ) : null}
            </div>
          </div>
        ) : resultsWorkspaceEnabled
        && resultsCapabilities.scaleNav
        && resultsSurface === "explorer"
        && resultsExplorerState ? (
          <div className={clsx("results-explorer-shell", {
            "detail-open": resultsExplorerDetailOpen,
          })}>
            <ResultsExplorer
              anchor={resultsExplorerState.anchor}
              authorizedTotal={completeResults.authorizedTotal}
              checkingChanges={completeResults.checkingChanges}
              completedView={completeResults.completedView}
              compactFilters={
                isResultsMobile
                || resultsExplorerDetailOpen
                || (resultsPaneWidth > 0 && resultsPaneWidth < 640)
              }
              entryId={resultsExplorerState.entry_id}
              error={completeResults.error}
              errorDirection={completeResults.errorDirection}
              errorAnchorKey={completeResults.errorAnchorKey}
              expandedGroups={resultsExplorerState.expanded_groups}
              filters={resultsExplorerState.filters}
              focusKey={resultsExplorerState.focus_key}
              groupCounts={completeResults.groupCounts}
              hasMore={completeResults.hasMore}
              hasPrevious={completeResults.hasPrevious}
              items={completeResults.items}
              loadedCount={completeResults.loadedCount}
              loading={completeResults.loading}
              loadingMore={completeResults.loadingMore}
              loadingPrevious={completeResults.loadingPrevious}
              matchedTotal={completeResults.matchedTotal}
              newResultsCount={completeResults.newResultsCount}
              projectionChanged={completeResults.projectionChanged}
              projectionStatus={completeResults.projectionStatus}
              query={resultsExplorerState.query}
              selectedKey={resultsExplorerState.selected_key}
              onAcceptChanges={() => { void acceptExplorerChanges(); }}
              onAnchorChange={(anchor) => replaceResultsExplorerState((current) => (
                current.anchor?.key === anchor?.key
                && current.anchor?.offset_px === anchor?.offset_px
                  ? current
                  : { ...current, anchor }
              ))}
              onExpandedGroupsChange={(expandedGroups) => (
                replaceResultsExplorerState((current) => ({
                  ...current,
                  expanded_groups: expandedGroups,
                }))
              )}
              onFiltersChange={(filters) => replaceResultsExplorerState((current) => ({
                ...current,
                filters: {
                  ...filters,
                  primary: filters.secondary === "all"
                    ? filters.primary
                    : "evidence",
                  secondary: filters.primary === "deliverables"
                    ? "all"
                    : filters.secondary,
                },
                anchor: null,
              }))}
              onFocusKeyChange={(focusKey) => replaceResultsExplorerState((current) => (
                current.focus_key === focusKey
                  ? current
                  : { ...current, focus_key: focusKey }
              ))}
              onLoadNext={() => { void completeResults.loadNext(); }}
              onLoadPrevious={() => { void completeResults.loadPrevious(); }}
              onQueryChange={(query) => replaceResultsExplorerState((current) => ({
                ...current,
                query,
                anchor: null,
              }))}
              onRetry={() => { void completeResults.retry(); }}
              onSelect={selectExplorerResult}
            />
            <div className="results-explorer-detail" aria-hidden={!resultsExplorerDetailOpen}>
              <button type="button" className="results-explorer-back" onClick={returnToExplorer}>
                <CharIcon ch="←" /> {resultsT("explorer_back")}
              </button>
              <div
                className={resultsViewerBodyClass}
                ref={reportBodyRef}
                onScroll={(event) => onResultsViewerScroll(event.currentTarget)}
              >
                {resultsWorkspaceViewer}
              </div>
            </div>
          </div>
        ) : (
          <div
            className={resultsViewerBodyClass}
            aria-hidden={
              resultsWorkspaceEnabled
              && !resultsCapabilities.scaleNav
              && isResultsMobile
              && resultsMobileListOpen
                ? true
                : undefined
            }
            ref={reportBodyRef}
            onScroll={(event) => onResultsViewerScroll(event.currentTarget)}
          >
            {resultsWorkspaceEnabled
              ? resultsWorkspaceViewer
              : resultsRouteUnavailable
                ? (
                    <div className="v2-empty" role="alert">
                      <p>{resultsT("missing_object")}</p>
                    </div>
                  )
                : selectedTab === "report"
                  ? reportSurface
                  : selectedTab === "data"
                    ? dataSurface
                    : codeSurface}
          </div>
        )}
        </div>
        {/* Config copilot batch 1-b: overlay drawer mounts inside the canvas column (.v2-ws-right has position:relative),
            scrim covers only canvas, never the conversation area (design screen 11). */}
        {configDrawerOverlaysResults ? configDrawer?.("overlay") : null}
      </div>
  );

  const retainedCanvasPane = creationPanel ? <aside className="v2-ws-right" aria-label={canvasPaneLabel} style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
    <div hidden={!creationPanelOpen} className="configuration-canvas-pane" style={{ display: creationPanelOpen ? "flex" : "none", flex: 1, minHeight: 0 }}><div className="configuration-canvas-pane-body">{creationPanel}</div></div>
    <div hidden={creationPanelOpen} style={{ display: creationPanelOpen ? "none" : "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {canvasObjectTabs ?? (onShowCreationPanel ? <button type="button" className="settings-btn settings-btn--secondary" onClick={onShowCreationPanel}>{tCreateAgent("canvas_return_editors")}</button> : null)}
      {canvasPane}
    </div>
  </aside> : canvasPane;

  // Responsive changes keep one host and the same editor/composer instances.
  const columnDrawerOpen = isDesktop && configDrawerOpen && !configDrawerOverlaysResults;
  return (
    <div
      className={clsx("v2-workspace v2-split-flex", {
        "agent-creation-workspace": creationPanelOpen,
        "canvas-maxed": hasRightPane && panelMode === "canvas",
        "no-canvas": !hasRightPane,
        "with-config-drawer": columnDrawerOpen,
      })}
      onFocusCapture={(event) => { workbenchFocusRef.current = event.target; }}
    >
      <div hidden={isDesktop} style={{ display: isDesktop ? "none" : undefined }}>{mobileTabs}</div>
      <SplitPane
        key={WORK_AREA_KEY}
        layout={isDesktop ? "desktop" : "compact"}
        mode={!hasRightPane ? "chat" : isDesktop ? panelMode : canvasMaximized ? "canvas" : "chat"}
        chat={chatPane}
        canvas={retainedCanvasPane}
        onSnapCanvas={() => setPanelMode("canvas")}
        onSnapChat={closeResultsPane}
        onReopen={() => {
          resultsInteractionEpochRef.current += 1;
          setResultsOpenedByUser(true);
          setPanelMode("split");
          if (resultsWorkspaceEnabled) trackResultsWorkspace({ action: "pane_opened", entryPoint: "header" });
        }}
        soloCanvas={reportOnly}
        canvasLabel={canvasPaneLabel}
        showCanvasReopen={Boolean(canvasObjectOpener) || Boolean(creationPanel) || (hasRightPane && (!resultsWorkspaceEnabled || hasBrowsableResources))}
        canvasHasNew={resultsHasNew}
      />
      {columnDrawerOpen ? configDrawer?.("column") : !isDesktop && configDrawerOpen ? configDrawer?.("sheet") : null}
    </div>
  );
}

async function waitForReportView(
  ref: { current: ReportViewHandle | null },
): Promise<ReportViewHandle | null> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (ref.current) return ref.current;
    await new Promise<void>((resolve) => window.setTimeout(resolve, 10));
  }
  return ref.current;
}

/** Report-header `更多操作` menu item (`.ws-menu button` appearance unchanged). */
/**
 * Appearance refresh batch 4b: `复制轮次 ID` in `更多` uses shared `copyTextWithFallback` (manual-copy
 * dialog when clipboard is unavailable); keep menu open and show `已复制` or `复制失败` in place for 1.5 seconds (polite announcement).
 */
function CopyTurnIdMenuItem({ turnId }: { turnId: string }) {
  const t = useTranslations("canvas");
  const [copied, setCopied] = useState<"copied" | "failed" | null>(null);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(null), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const itemProps = useMenuItem({
    closeOnSelect: false,
    label: t("hdr_copy_turn_id"),
    onSelect: () => {
      void copyTextWithFallback(turnId, t("hdr_copy_turn_id")).then((ok) => setCopied(ok ? "copied" : "failed"));
    },
  });
  return (
    <button type="button" className="results-copy-turn-id" data-state={copied || undefined} {...itemProps}>
      <span aria-live="polite">
        {copied === "copied" ? t("hdr_copy_turn_copied") : copied === "failed" ? t("hdr_copy_turn_failed") : t("hdr_copy_turn_id")}
      </span>
    </button>
  );
}

/**
 * Appearance refresh batch 4b remainder: turn-runner transport → report-toolbar connection state. Reconnecting is
 * `reconnecting`; abandoned reconnection (no longer running)
 * **without this turn's terminal state** is `lost`. Any terminal state (including failed recorded for unreadable
 * terminal frames) prevents connection-lost status; its terminal description
 * (`写作中断`, etc.) expresses the outcome, matching F5's persisted-terminal description. exported for tests.
 */
export function runnerStreamConnection(
  runner: Pick<TurnRunnerState, "streamingState" | "running" | "terminalStatus">,
): StreamConnection | null {
  if (runner.streamingState !== "error") return null;
  if (runner.running) return "reconnecting";
  return runner.terminalStatus ? null : "lost";
}

/** Unavailable menu item: persistently shows the reason as smaller second-line text (decision 9), read through aria-describedby. */
function DisabledMenuItem({ label, reason }: { label: string; reason: string | null }) {
  const reasonId = useId();
  const itemProps = useMenuItem({ disabled: true, label });
  return (
    <>
      <button type="button" aria-describedby={reason ? reasonId : undefined} {...itemProps}>
        <FileDown size={15} className="ws-menu-icon" />
        {label}
      </button>
      {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </>
  );
}

function WorkbenchMenuItem({
  className,
  onSelect,
  children,
}: {
  className?: string;
  onSelect: () => void;
  children: React.ReactNode;
}) {
  const itemProps = useMenuItem({ onSelect });
  return (
    <button type="button" className={className} {...itemProps}>
      {children}
    </button>
  );
}

type WorkbenchSection = CanvasState["sections"][number];
type WorkbenchCodeSection = Extract<WorkbenchSection, { kind: "code_block" }>;

function isWorkbenchCodeSection(section: WorkbenchSection): section is WorkbenchCodeSection {
  return section.kind === "code_block";
}

function CodeTab({
  sections, executionSteps, legacyPyCells,
}: {
  sections: CanvasState["sections"];
  executionSteps?: PythonExecutionStep[];
  legacyPyCells?: CanvasState["pyCells"];
}) {
  const t = useTranslations("canvas");
  const runs = projectCodeRuns({ sections, executionSteps, legacyPyCells });
  if (runs.length === 0) {
    return <div className="v2-empty"><p>{t("sql_empty")}</p></div>;
  }
  return (
    <div className="code-result-viewer">
      <div className="code-result-intro">
        <h3>{t("code_runs_title", { count: runs.length })}</h3>
        <p>{t("code_runs_hint")}</p>
      </div>
      {runs.map((run, index) => (
        <CodeRunCard key={run.key} run={run} defaultOpen={index === 0} />
      ))}
    </div>
  );
}

export function CodeRunCard({
  run,
  defaultOpen,
}: {
  run: CodeRunItem;
  defaultOpen: boolean;
}) {
  const t = useTranslations("canvas");
  const statusLabel = run.status === "running"
    ? t("running")
    : run.status === "success"
      ? t("last_run")
      : t("run_failed");
  const label = run.label || t(`code_run_${run.language}`);
  const copyCode = () => {
    if (!run.code) return;
    if (!navigator.clipboard) {
      toast({ kind: "error", text: t("code_copy_failed") });
      return;
    }
    void navigator.clipboard.writeText(run.code).then(
      () => toast({ kind: "info", text: t("code_copied") }),
      () => toast({ kind: "error", text: t("code_copy_failed") }),
    );
  };

  return (
    <article
      className="code-run-card"
      data-status={run.status}
      data-results-code-run-key={run.key}
      tabIndex={-1}
    >
      <div className="code-run-card-head">
        <div>
          <strong>{label}</strong>
          <span>{run.language.toUpperCase()} · {statusLabel}</span>
        </div>
        <div className="code-run-actions">
          {run.code ? (
            <button type="button" onClick={copyCode}>{t("code_copy")}</button>
          ) : null}
          {run.outputs[0] ? (
            <button
              type="button"
              className="primary"
              data-results-focus-token={run.outputs[0]!.artifactId}
              onClick={() => requestCanvasDataFocus(run.outputs[0]!.artifactId, {
                entryPoint: "code",
              })}
            >
              {t("code_open_output")}
            </button>
          ) : null}
        </div>
      </div>
      {run.code ? (
        <pre className="code-run-source"><code>{run.code}</code></pre>
      ) : (
        <div className="code-run-pending">{t("code_running_no_source")}</div>
      )}
      {run.codeTruncated ? (
        <div className="code-run-pending">{t("code_source_truncated")}</div>
      ) : null}
      {run.outputs.length > 0 ? (
        <div className="code-run-outputs" aria-label={t("code_outputs")}>
          {run.outputs.map((output, index) => (
            <button
              key={output.artifactId}
              type="button"
              data-results-focus-token={output.artifactId}
              onClick={() => requestCanvasDataFocus(output.artifactId, {
                entryPoint: "code",
              })}
              title={output.filename ?? t("code_output_n", { index: index + 1 })}
            >
              {output.filename ?? t("code_output_n", { index: index + 1 })}
            </button>
          ))}
        </div>
      ) : null}
      <details className="code-run-details" open={defaultOpen && run.status === "error"}>
        <summary>{t("code_run_details")}</summary>
        <dl>
          <div><dt>{t("code_detail_status")}</dt><dd>{statusLabel}</dd></div>
          <div><dt>{t("code_detail_language")}</dt><dd>{run.language.toUpperCase()}</dd></div>
          {run.actor ? (
            <div><dt>{t("code_detail_actor")}</dt><dd>{t(`code_actor_${run.actor}`)}</dd></div>
          ) : null}
          {run.durationMs !== null ? (
            <div><dt>{t("code_detail_duration")}</dt><dd>{t("code_duration_ms", { duration: run.durationMs })}</dd></div>
          ) : null}
        </dl>
        {run.stdout ? (
          <div className="code-run-stdout">
            <strong>{t("python_sandbox_output")}</strong>
            <pre>{run.stdout.slice(0, 2000)}</pre>
          </div>
        ) : null}
      </details>
    </article>
  );
}
