/**
 * ReportView — Tiptap editor-driven renderer (P3.2.3 switches to the adapter path).
 *
 * Data flow:
 *   sections → preprocessSections (chart deduplication) → canvasToPMDoc → Tiptap editor
 *   Editing triggers (P3.2.5) → onUpdate → debouncedSync → PATCH (P3.2.6)
 *
 * P3.2.3 initial version: editable=false by default (view mode), visually consistent with legacy ReportView.
 * Follow-ups:
 *   - P3.2.4: isEditMode toggle (desktop always-on / Mobile toggle)
 *   - P3.2.5: inline editing for 11 field types
 *   - P3.2.6: PATCH wire
 *   - P3.2.7: Revision drawer
 *
 * ⚠ Visual differences (vs legacy):
 *   Consecutive datarc_iframe grid layout (.v2-iframe-grid, 2 columns) is not migrated yet; P3.2 wrap-up or P3.2.x follow-up.
 *   Single-column iframe rendering is fully functional; the visual difference is acceptable.
 */

"use client";

import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import Link from "@tiptap/extension-link";
import { Table } from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import { TextStyle } from "@tiptap/extension-text-style";
import FontFamily from "@tiptap/extension-font-family";
import { FontSize } from "@/lib/canvas-pm-adapter/extensions/font-size";
import { FontWeight } from "@/lib/canvas-pm-adapter/extensions/font-weight";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import type { ReportSection } from "@/lib/types";
import {
  CanvasDocument,
  CanvasContextProvider,
  canvasToPMDoc,
  getAllCanvasExtensions,
  normalizeDocForEditor,
  shouldRefuseSetContent,
  type SourceRef,
} from "@/lib/canvas-pm-adapter";
import { redactViewerSourceIdentitiesInValue, type TurnCitationsMap } from "@/lib/canvas-pm-adapter/citation-types";
import { NodeSelection, TextSelection } from "@tiptap/pm/state";
import { extractTopLevelSectionOrder, ordersEqual } from "@/lib/section-order";
import { applySectionMove } from "@/lib/canvas-pm-adapter/section-move";
import { useTouchAffordance } from "@/lib/use-touch-affordance";
import { CitationExtension, buildCiteResolver, deniedSourceIndexes } from "@/lib/canvas-pm-adapter/extensions/citation-plugin";
import { useCitationDecorationRefresh } from "@/lib/canvas-pm-adapter/use-citation-decoration-refresh";
import { FigureCaptionExtension } from "@/lib/canvas-pm-adapter/extensions/figure-caption";
import { VerifiedEditGuardExtension } from "@/lib/canvas-pm-adapter/extensions/verified-edit-guard";
import { AiRewriteDecorationsExtension } from "@/lib/canvas-pm-adapter/extensions/ai-rewrite-decorations";
import { SectionDeleteGuardExtension } from "@/lib/canvas-pm-adapter/extensions/section-delete-guard";
import { CitationHoverManager } from "@/lib/canvas-pm-adapter/extensions/citation-popover";
import { ErrorDiagnosticsInline, useCopyDiagnosticsToastAction } from "@/components/error-notice";
import { SourcePanel, SourcePanelProvider } from "@/components/canvas/source-panel";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { AIRewriteDiffBar } from "./ai-rewrite-diff-bar";
import { BlockAIBar } from "./block-ai-bar";
import { FormattingToolbar } from "./formatting-toolbar";
import { StreamingBanner } from "./streaming-banner";
import {
  ReportToolbarStatusGroup,
  writingInterruption,
  type StreamConnection,
} from "./report-toolbar-status";
import { StreamingPreview } from "./streaming-preview";
import { WaitBand } from "./wait-band";
import { useAIRewrite } from "@/lib/use-ai-rewrite";
import { useVirtualViewport } from "@/lib/use-virtual-viewport";
import {
  flushFailureBlocksReadOnlyAction,
  useDebouncedSync,
} from "@/lib/use-debounced-sync";
import { useReportOverlayManager } from "@/lib/use-report-overlay-manager";
import {
  deleteCanvasSection as apiDeleteCanvasSection,
  mergeCanvasSections as apiMergeCanvasSections,
  splitCanvasSection as apiSplitCanvasSection,
  fetchCanvasById,
  fetchCanvasBySession,
} from "@/lib/api";
import {
  getCanvasSnapshot,
  hydrateCanvas,
  onSectionPatched,
  redactSourceIdentityInView,
  refreshCanvas,
  selectStreamingSectionIdKey,
  subscribeCanvas,
} from "@/lib/canvas-store";
import { registerCanvasNavigationGuard } from "@/lib/canvas-navigation-guard";
import { getBrandSnapshot } from "@/lib/brand-runtime";
import {
  pendingInlineEditCount,
  subscribePendingInlineEdits,
} from "@/lib/canvas-pm-adapter/inline-edit-registry";
import { trackReportSetContent } from "@/lib/canvas-report-metrics";
import { EditModeToggle } from "./edit-mode-toggle";
import { ConfirmDialog, useConfirmDialog } from "@/components/ui/confirm-dialog";
import { toast } from "@/lib/use-toast";
import { SectionRevisionDrawer } from "./section-revision-drawer";
import { SourceLinkageStrip } from "./trust-strip";
import { ReportStatusChip } from "./report-status";
import { ReplacedFigures, ReportHistory } from "./report-history";
import { splitSupersededSections } from "./report-history-split";
import { History } from "@/components/icons";
import { detectCjkContentLang } from "@/lib/content-lang";
import { useLocale, useTranslations } from "next-intl";
import {
  validateAndBuildRenderedSurfaceReceipt,
  type PrepareExportResult,
  type SurfaceRenderState,
} from "@/lib/canvas-export-prepare";
import type { CanvasExportFmt } from "@/lib/api";
import type { Locale } from "@/lib/locale";
import {
  File as LucideFile,
  Lightbulb as LucideLightbulb,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

export interface ReportViewHandle {
  prepareForExport: (request: {
    format: CanvasExportFmt;
    profile: "full_report" | "executive_summary" | "data_workbook";
    forceLegacy: boolean;
  }) => Promise<
    | PrepareExportResult
    | {
        ok: true;
        mode: "legacy";
        locale: Locale;
        /** Batch 0 0-A: "forced"=template cloning deliberately uses legacy;
         *  "v2_capability_unavailable"=server capability failure forces fallback (must notify the user). */
        reason: "forced" | "v2_capability_unavailable";
      }
  >;
  /**
   * Visual refresh batch 4b remainder: mobile reading toolbar puts "编辑" under "更多"; workbench enters edit mode (same path as "编辑此报告").
   * Do nothing when editing is unavailable (no saved context, writing in progress, or read-only).
   */
  enterEditMode: () => void;
  openHistory: () => void;
}

export interface ReportViewProps {
  sections: ReportSection[];
  /** Local reading layout; never persisted into the report or export. */
  readingWidth?: "standard" | "wide";
  /** Visual/context contract only; both modes share the same editor and persistence state. */
  presentation?: "canvas" | "results-reading";
  /** Complete visible Canvas state used to validate Data/Code entries before export. */
  exportSections?: ReportSection[];
  /** Force isEditMode (e.g. tests). Still gated by canEdit and streaming locks. */
  isEditMode?: boolean;
  /** True only when parent has a real save context. Defaults to Boolean(canvasId). */
  canEdit?: boolean;
  /** Render the edit/status bar. Workspace report tab sets this; Code tab does not. */
  showEditControls?: boolean;
  /** Optional Results toolbar host. Undefined keeps the legacy inline placement. */
  editControlsHost?: HTMLElement | null;
  /** The existing toolbar owns History in compact reading mode. */
  hideHistoryControl?: boolean;
  /**
   * Visual refresh batch 4b: output toolbar status group. With this prop supplied (including not-yet-mounted null), streaming-banner and report-header
   * status badges move here (writing progress while writing, status badge otherwise). Without the prop, keep the banner/header positions.
   */
  statusHost?: HTMLElement | null;
  /** Visual refresh batch 4b: this turn's own terminal state (turn runner terminalStatus), used by the status group to announce the writing outcome. */
  turnTerminalStatus?: string | null;
  /**
   * Visual refresh batch 4b remainder: the turn owning `turnTerminalStatus` (turn runner turnId). If this turn wrote content in the report and ended
   * stopped or failed, the status group persistently shows "已停止 / 写作中断，已写的部分已保存". F5 reconstructs it from hydrated terminal state and paragraph turn IDs, matching live.
   */
  turnTerminalTurnId?: string | null;
  /** Visual refresh batch 4b remainder: report_sections_saved from this turn's done terminal state (same source live/F5), displayed in "已保存 N 节". */
  turnReportSectionsSaved?: number | null;
  /** Visual refresh batch 4b remainder: connection state during writing (turn runner transport: reconnecting / abandoned); see StreamConnection. */
  streamConnection?: StreamConnection | null;
  /** Visual refresh batch 4b remainder: notify the caller when edit mode changes (workbench switches the mobile reading toolbar accordingly). */
  onEditModeChange?: (editing: boolean) => void;
  /** Feishu 07.06 #22: Code panel reuses ReportView, but its empty state should not show the report placeholder
   *  '你的报告会在这里生成' — override with a custom empty state (or null). */
  emptyState?: ReactNode;
  /** True when interactions inside report sections must not mutate or start follow-up turns. */
  readOnly?: boolean;
  /** Disabled reason shown when edit controls are visible but editing is unavailable. */
  readOnlyReason?: string;
  /** Added in P3.5: whole-turn lock during streaming (takes precedence over isEditMode) */
  isStreaming?: boolean;
  /** P3.5: turn id (shown in the banner) */
  currentTurnId?: string | null;
  /** P3.5: cancellation callback (caller handles SSE cancelTurn) */
  onCancelTurn?: () => void;
  /** Added in P3.2.6: PATCH call context */
  canvasId?: string | null;
  /** content-permissions: persisted report session_id — when nonempty, section artifacts use the permission mediator (visible to sharing recipients). */
  sessionId?: string | null;
  /** P3.3: SourceRef metadata (injected from trace API or mock). Citation popover shows fallback when omitted */
  sourceRefs?: SourceRef[];
  /** canvas-elements source-merging batch: nested archived views resolve `[N]` through this (canvas-level source table is outside archived groups). */
  citationSources?: Array<Record<string, unknown>>;
  /** A3 (report-edit-audit): server seq is already the authoritative display order → skip preprocessSections'
   *  forced ranking and render in stored order (preserves user drag order). Default false covers old data / flag off. */
  seqAuthoritative?: boolean;
  /** source-accessibility c2: canvas detail turnCitations sidecar — resolve body [cite:N]
   *  into superscripts here using its paragraph's turn_id. Omitted = all stay hidden as before (current behavior). */
  turnCitations?: TurnCitationsMap;
}

/**
 * Preprocess sections: like old ReportView, filter charts referenced by insight_card to avoid duplicate rendering.
 *
 * Old ReportView (M137 #144 follow-up) logic:
 *   - With insight_card, charts referenced by evidence[].chart_section_id → embedded in InsightCard
 *     (chartLookup path), without repeating the top-level chart section
 *   - Old turn path (no insight_card) → render all charts normally
 */
export function preprocessSections(
  sections: ReportSection[],
  /** A3 (report-edit-audit): true = server seq is already authoritative display order → skip forced ranking and
   *  render in stored order (preserving user drag order, P1-4 interpretation B). Default false covers old data. */
  seqAuthoritative = false,
): ReportSection[] {
  const referencedChartIds = new Set<string>();
  for (const s of sections) {
    if (s.kind === "insight_card") {
      const ev = s.evidence ?? [];
      for (const e of ev) {
        if (e?.chart_section_id) referencedChartIds.add(e.chart_section_id);
      }
    }
  }
  const kept = sections.filter((s) => {
    // feedback-0713 #3: chart_section_id is a visual reference (same field for chart/image) — images
    // referenced by insight_card are also embedded, without top-level duplicates. Fold explicit references only:
    // unreferenced images (AI posters, etc.) remain top-level, unaffected by the orphan-chart rule.
    //
    // plan-report-assembly batch 2-d (decision #4, 2026-08-24): remove the previous
    // `if (hasInsightCard) return false;` — it also folded **unreferenced** charts.
    // The criterion now asks only whether evidence references this chart, matching images.
    // Reason: calling `add_chart` explicitly requests placing this chart; no other
    // expression of hiding is read (hidden_for_chat_mode all 0, surface_rationale all None).
    // Production t_a06cfbe66ec8: 8 add_chart calls persisted all 8 charts; only 4 were referenced,
    // and the other 4 (退款侵蚀敞口 / 三年营业额与利润预测 / 三年月坪效 / 近30天同比环比)
    // **were silently hidden from users**, while 20 raw Python sections rendered normally — the priorities were reversed.
    // The old rule assumed payload-path orphan charts were mostly duplicates: true for single-leg turns; in multi-leg turns,
    // orphan charts usually belong to **another leg** or the **direct-writing leg** (which never produces insight_card).
    if (s.kind === "image" && referencedChartIds.has(s.id)) return false;
    if (s.kind !== "chart") return true;
    return !referencedChartIds.has(s.id);                // Referenced → embedded in InsightCard; otherwise shown at top level
  });
  // Design draft P1: canonical document order. The writer-loop emits header sections
  // (eyebrow/h1/meta) at COMPOSE time → high seq, AFTER peer-loop artifacts
  // (python_sandbox code_block, raw charts) which carry a LOWER seq. Without a
  // reorder a raw python block leads the report. Stable-sort to: header leads →
  // body (kpi/narrative/charts, original seq order) → code/sql appendix at the
  // end (design treats raw code as provenance, not report body). Stable: ties
  // break by original index, so the narrative→[figure:] weave (resolved by id in
  // canvasToPMDoc, position-independent) and multi-writer chapter order are kept.
  // A3: with authoritative seq, this forced ranking becomes a **fallback** — server compose persists using the same rank
  // (bowhead_core/canvas_rank.py, identical on both ends); forcing rank again would undo the user's drag order.
  if (seqAuthoritative) return kept;
  const rank = (s: ReportSection): number => {
    const k: string = s.kind;
    if (k === "eyebrow") return 0;
    if (k === "h1") return 1;
    if (k === "meta") return 2;
    if (k === "kpi_row") return 3;   // KPI band leads the body (design: header → KPI → prose)
    if (k === "sources") return 90; // Sources come last (identical to BE canvas_rank._SOURCES_RANK)
    if (k === "code_block" || k === "sql_block") return 100;
    return 50;
  };
  // ★116 dogfood 1.0.93 P2: each leg of a multi-leg plan turn produces a full report (each with h1) — global
  // forced ranking groups all h1 / kpi_row together, breaking up and interleaving both reports. Segmentation (identical to
  // BE bowhead_core/canvas_rank.py `_segment_bounds`): the second and subsequent
  // h1 start new segments, taking the immediately preceding eyebrow; single-h1 canvas=one segment, byte-identical to old sorting.
  const bounds: number[] = [0];
  let seenH1 = false;
  for (let i = 0; i < kept.length; i++) {
    if (kept[i].kind !== "h1") continue;
    if (seenH1) {
      let start = i;
      if (i > 0 && kept[i - 1].kind === "eyebrow" && start > bounds[bounds.length - 1]) {
        start = i - 1; // This leg's eyebrow joins h1 in the new segment
      }
      if (start > bounds[bounds.length - 1]) bounds.push(start);
    }
    seenH1 = true;
  }
  const out: ReportSection[] = [];
  for (let bi = 0; bi < bounds.length; bi++) {
    const start = bounds[bi];
    const end = bi + 1 < bounds.length ? bounds[bi + 1] : kept.length;
    const seg = kept
      .slice(start, end)
      .map((s, i) => [s, i] as const)
      .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1])
      .map(([s]) => s);
    out.push(...seg);
  }
  return out;
}

/**
 * feedback-0713 F1: admission gate for the setContent effect. exported for tests.
 *
 * - "defer": selection-anchored overlay (format toolbar/block AI bar) is open — markdown round-trip structure
 *   is not idempotent; rebuilding the whole doc with setContent misaligns absolute positions captured by the overlay,
 *   so subsequent formatting targets the wrong range (device reproduction: after auto-save round-trip, font size changes
 *   only 1 character / serif silently no-ops). **Do not mark initialDoc seen**; after the overlay closes,
 *   the effect reruns and applies normally — deliberately distinct from skip-mark-seen; do not merge.
 * - "skip-mark-seen": unsaved local edits exist; mark seen, then skip (existing #3 semantics).
 * - forced (conflict reload/rollback) already discards local edits; neither gate blocks it.
 */
export function gateReportSetContent(args: {
  overlayKind: string | null;
  forced: boolean;
  dirtyCount: number;
}): "defer" | "skip-mark-seen" | "apply" {
  if (args.forced) return "apply";
  if (args.overlayKind !== null) return "defer";
  if (args.dirtyCount > 0) return "skip-mark-seen";
  return "apply";
}

export const ReportView = forwardRef<ReportViewHandle, ReportViewProps>(function ReportViewImpl({
  sections: canonicalSections,
  readingWidth = "standard",
  presentation = "canvas",
  exportSections: canonicalExportSections,
  isEditMode: forcedEditMode,
  isStreaming = false,
  currentTurnId = null,
  onCancelTurn,
  canvasId = null,
  sessionId = null,
  sourceRefs: canonicalSourceRefs,
  citationSources,
  seqAuthoritative = false,
  turnCitations,
  canEdit: canEditProp,
  showEditControls = false,
  editControlsHost,
  hideHistoryControl = false,
  statusHost,
  turnTerminalStatus = null,
  turnTerminalTurnId = null,
  turnReportSectionsSaved = null,
  streamConnection = null,
  onEditModeChange,
  readOnly = false,
  readOnlyReason,
  emptyState,
}: ReportViewProps, ref) {
  // P0-B/P1-A: report defaults to read mode. Editing is explicit and only
  // enabled when the caller has a real canvas save context.
  const canEdit = (canEditProp ?? Boolean(canvasId)) && Boolean(canvasId);
  const tConflict = useTranslations("report_conflict");
  const tCanvas = useTranslations("canvas");
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  // Export visuals follow the UI locale (all four UI locales supported; rendering script and export-page contract agree).
  const exportLocale = useLocale() as Locale;
  const authSession = useOptionalAuthSession();
  const sourceAccessScopeKey = [
    authSession?.status ?? "no-auth-provider",
    authSession?.me?.id ?? "no-viewer",
    authSession?.me?.team_id ?? "no-team",
    sessionId ?? "live",
    canvasId ?? "none",
  ].join(":");
  const [redactedSourceAliases, setRedactedSourceAliases] = useState<Set<string>>(new Set());
  useEffect(() => setRedactedSourceAliases(new Set()), [sourceAccessScopeKey]);
  const sections = useMemo(
    () => redactViewerSourceIdentitiesInValue(canonicalSections, redactedSourceAliases),
    [canonicalSections, redactedSourceAliases],
  );
  const exportSections = useMemo(
    () => canonicalExportSections
      ? redactViewerSourceIdentitiesInValue(canonicalExportSections, redactedSourceAliases)
      : undefined,
    [canonicalExportSections, redactedSourceAliases],
  );
  const sourceRefs = useMemo(
    () => canonicalSourceRefs
      ? redactViewerSourceIdentitiesInValue(canonicalSourceRefs, redactedSourceAliases)
      : undefined,
    [canonicalSourceRefs, redactedSourceAliases],
  );
  const redactSourceAlias = useCallback((sourceAlias: string) => {
    setRedactedSourceAliases((current) => {
      if (current.has(sourceAlias)) return current;
      return new Set(current).add(sourceAlias);
    });
    redactSourceIdentityInView(sourceAlias);
  }, []);

  const [localEditMode, setLocalEditMode] = useState(false);
  const reportRootRef = useRef<HTMLDivElement | null>(null);
  const exportSectionsRef = useRef(exportSections ?? sections);
  exportSectionsRef.current = exportSections ?? sections;
  const presentationOverridesRef = useRef<Map<string, Set<() => void>>>(new Map());
  const registerPresentationOverride = useCallback((sectionId: string, restore: () => void) => {
    const callbacks = presentationOverridesRef.current.get(sectionId) ?? new Set<() => void>();
    callbacks.add(restore);
    presentationOverridesRef.current.set(sectionId, callbacks);
    return () => {
      const current = presentationOverridesRef.current.get(sectionId);
      current?.delete(restore);
      if (current?.size === 0) presentationOverridesRef.current.delete(sectionId);
    };
  }, []);

  useEffect(() => {
    if (!canEdit) setLocalEditMode(false);
  }, [canEdit]);

  const requestedEditMode =
    forcedEditMode !== undefined ? forcedEditMode : localEditMode;
  const isEditMode = Boolean(canEdit && requestedEditMode && !isStreaming);
  // Visual refresh batch 4b remainder: tell the caller when edit mode changes (only callers supplying a callback, such as the workbench, receive it)
  const onEditModeChangeRef = useRef(onEditModeChange);
  onEditModeChangeRef.current = onEditModeChange;
  useEffect(() => {
    onEditModeChangeRef.current?.(isEditMode);
  }, [isEditMode]);

  // α v1 (collapse-in-place): superseded prior-report sections are pulled OUT of the
  // live PM doc (so the editor shows ONLY the latest report) and rendered separately
  // in the ReportHistory accordion above. Marking (not removal) keeps them in `sections`
  // for hydration consistency + recoverability.
  // canvas-update-turn B1-2: superseded splits into two paths — full old reports enter History; old charts replaced
  // by a revision turn (`_meta.folded_by="revise"`) enter the current report's trailing "已替换的图表" bar.
  const { prior: supersededSections, replaced: replacedFigures } = useMemo(
    () => splitSupersededSections(sections.filter((s) => s.superseded)),
    [sections],
  );
  // report-streaming B: **keep actively streaming sections outside the editor document**.
  //
  // Measurement (discovery §8.3): high-frequency chunks fed into TipTap create a single blocking
  // 1833ms task in a 96-section document (page freezes nearly two seconds), occupying 58–86% of the main thread — reproducing
  // the editor-freeze regression history described in canvas-store.ts comments. During streaming, use
  // a read-only preview block (<StreamingPreview>); only after `section_patched` arrives (content fixed and
  // sanitation complete), return the section to `filtered` for one normal setContent.
  const streamingIds = useSyncExternalStore(
    subscribeCanvas,
    () => selectStreamingSectionIdKey(getCanvasSnapshot()),
    () => "",
  );
  const streamingIdSet = useMemo(
    () => new Set(streamingIds ? streamingIds.split(",") : []),
    [streamingIds],
  );
  const currentSections = useMemo(
    () =>
      sections.filter(
        (s) =>
          !s.superseded
          && !streamingIdSet.has((s as unknown as { id?: string }).id ?? ""),
      ),
    [sections, streamingIdSet],
  );
  const streamingSections = useMemo(
    () =>
      sections.filter((s) =>
        streamingIdSet.has((s as unknown as { id?: string }).id ?? ""),
      ),
    [sections, streamingIdSet],
  );
  const filtered = useMemo(
    () => preprocessSections(currentSections, seqAuthoritative),
    [currentSections, seqAuthoritative],
  );
  const initialDoc = useMemo(() => canvasToPMDoc(filtered), [filtered]);
  // Feishu 07.06 #12: reorder comparison baseline = top-level section id order (instead of stored order; otherwise
  // preprocessSections moves titles first/code last and deduplicates charts, making the orders always differ → every edit sends a false reorder).
  // dogfood-0708 remainder fix: baseline uses the **editor-normalized** top-level order (captured by setContent effect
  // below), rather than raw canvasToPMDoc(initialDoc) order — they differ slightly in some reports (narrative embedded
  // in figures, etc.) → first edit sends 1~2 one-off false reorders. Seed raw order; correct to editor order after mount.
  const [reorderBaseline, setReorderBaseline] = useState<string[]>(
    () => extractTopLevelSectionOrder(initialDoc),
  );
  const canvasCitationSources = useMemo(() => {
    const sec = currentSections.find(
      (s): s is Extract<ReportSection, { kind: "sources" }> => s.kind === "sources",
    );
    return (sec?.entries as Array<Record<string, unknown>> | undefined) ?? undefined;
  }, [currentSections]);
  const numberedSourcesLength = useMemo(() => {
    // α v1 (code-review fix): find the CURRENT report's sources — the editor renders
    // `currentSections` (superseded folded out), so the citation [N] decorator must
    // count against the current report's sources, not the first (possibly superseded
    // earlier-turn) "sources" section in the full array.
    const srcSec = currentSections.find(
      (s): s is Extract<ReportSection, { kind: "sources" }> => s.kind === "sources",
    );
    // canvas-elements: nested archived views cannot access canvas-level sources; the caller supplies them via prop.
    // ★Fallback, not precedence — if an existing archived group has its own sources, use that copy (same as canvas-context).
    return srcSec?.entries?.length ?? citationSources?.length ?? 0;
  }, [currentSections, citationSources]);
  const numberedSourcesLengthRef = useRef(numberedSourcesLength);
  numberedSourcesLengthRef.current = numberedSourcesLength;
  // P2 batch 6: a CJK report carries its language so the content measure (keyed off :lang()) fits the
  // report rather than the UI language; Latin reports follow the UI language.
  const contentLang = useMemo(() => {
    const text = currentSections
      .map((section) => {
        const fields = section as unknown as { text?: unknown; markdown?: unknown };
        return typeof fields.markdown === "string" ? fields.markdown : typeof fields.text === "string" ? fields.text : "";
      })
      .join("\n");
    return detectCjkContentLang(text) ?? undefined;
  }, [currentSections]);
  // Writing progress and status badges live in the output toolbar (when the toolbar status group exists).
  const toolbarStatus = statusHost !== undefined;
  // Persistent status-group explanation when this turn stopped / failed and wrote in this report (see turnTerminalTurnId)
  // This turn wrote paragraphs in this report (by paragraph turn_id)
  const reportTouchedByTurn = useMemo(
    () => Boolean(turnTerminalTurnId)
      && sections.some((section) => (section as unknown as { turn_id?: string }).turn_id === turnTerminalTurnId),
    [turnTerminalTurnId, sections],
  );
  const writingInterruptionKind = useMemo(() => {
    if (!toolbarStatus || !reportTouchedByTurn) return null;
    return writingInterruption(turnTerminalStatus);
  }, [toolbarStatus, reportTouchedByTurn, turnTerminalStatus]);
  // "连接中断" follows the interruption explanation: shown only while the canvas is writing or this turn wrote this report (unrelated turn disconnects do not attach here)
  const reportStreamConnection =
    streamConnection === "lost" && !isStreaming && !reportTouchedByTurn ? null : streamConnection;
  // Canvas batch 3: [N] for restricted / invalid / unsupported sources carries data-source-denied (same source table as above and
  // same retrieval rule as numberedSourcesLength; outlined + struck through).
  const numberedSourcesDenied = useMemo(() => {
    const srcSec = currentSections.find(
      (s): s is Extract<ReportSection, { kind: "sources" }> => s.kind === "sources",
    );
    return deniedSourceIndexes(
      (srcSec?.entries as Array<Record<string, unknown>> | undefined) ?? citationSources,
    );
  }, [currentSections, citationSources]);
  const numberedSourcesDeniedRef = useRef(numberedSourcesDenied);
  numberedSourcesDeniedRef.current = numberedSourcesDenied;

  // source-accessibility c2: [cite:N] resolver (sections + sidecar → memo → ref forwarding,
  // wired like numberedSourcesLengthRef; identity changes invalidate the plugin cache and rescan).
  const citeResolver = useMemo(
    () => buildCiteResolver(currentSections as Array<{ id?: string; turn_id?: string }>, turnCitations),
    [currentSections, turnCitations],
  );
  const citeResolverRef = useRef(citeResolver);
  citeResolverRef.current = citeResolver;

  // WP3 (design D8): detaching a verified number uses in-app confirmation — ref supplies current-locale copy to the plugin.
  const tGuard = useTranslations("verified_guard");
  const { confirm: confirmVerifiedEdit, dialog: verifiedEditConfirmDialog } = useConfirmDialog();
  const verifiedGuardConfirmRef = useRef<() => Promise<boolean>>(() => Promise.resolve(false));
  verifiedGuardConfirmRef.current = () => confirmVerifiedEdit({
    title: tGuard("confirm_title"),
    description: tGuard("confirm"),
    confirmLabel: tGuard("confirm_action"),
    cancelLabel: tGuard("confirm_keep"),
  });
  // Another change (SSE refresh, etc.) replaced the number during confirmation, so the edit was not applied: explain instead of silently discarding.
  const verifiedGuardDroppedRef = useRef<() => void>(() => undefined);
  verifiedGuardDroppedRef.current = () => {
    toast({ kind: "warning", text: tGuard("edit_dropped") });
  };

  const editor = useEditor({
    extensions: [
      CanvasDocument,
      // design r2 (report-edit-affordance): drag insertion line = 2px terracotta (follows brand color)
      StarterKit.configure({
        document: false,
        dropcursor: { color: "var(--c-accent)", width: 2 },
      }),
      TextStyle,
      FontFamily,
      FontSize,
      FontWeight,
      Link.configure({ openOnClick: false }),
      Table.configure({ resizable: false }),
      TableRow,
      TableCell,
      TableHeader,
      // ar1 P1-3 fix: pass `numberedSources` length so plugin only decorates `[N]`
      // when there's a backing source entry. Computed from sections' sources kind.
      CitationExtension.configure({
        getNumberedSourcesLength: () => numberedSourcesLengthRef.current,
        getCiteResolver: () => citeResolverRef.current,
        getNumberedSourcesDenied: () => numberedSourcesDeniedRef.current,
      }),
      // WP3 (design D8): editing a verified number → detach anchor, downgrade to unverified + confirm.
      VerifiedEditGuardExtension.configure({
        getNumberedSourcesLength: () => numberedSourcesLengthRef.current,
        confirmEdit: () => verifiedGuardConfirmRef.current(),
        onEditDropped: () => verifiedGuardDroppedRef.current(),
      }),
      // Feishu 07.06 #15b: AI rewrite revision visuals (old text gray/struck through, new text highlighted), view-only.
      AiRewriteDecorationsExtension,
      // Batch 1 2.1 (chart-table-ux): in read-only mode, snap the "图N：" paragraph immediately next to a [figure:] anchor into
      // caption layout and hide the prefix; no decorations in edit mode (source text editable, persisted unchanged on save).
      FigureCaptionExtension,
      // A0-2 (report-edit-audit P0-2 containment): keyboard/selection deletion of top-level sections does not persist and falsely reports
      // saved → intercept in editor + notify. Menu "删除" uses API+refresh (whole-document replacement), unaffected.
      SectionDeleteGuardExtension,
      ...getAllCanvasExtensions(),
    ],
    content: initialDoc,
    editable: isEditMode,
    immediatelyRender: false,
    shouldRerenderOnTransaction: true,
  });

  // Sync editable changes manually (Tiptap content prop only applies at init; editable prop likewise)
  useEffect(() => {
    if (editor) {
      editor.setEditable(isEditMode);
      editor.setOptions({ editorProps: { attributes: {
        role: isEditMode ? "textbox" : "document",
        "aria-label": tCanvas(isEditMode ? "editor_label" : "document_label"),
        ...(isEditMode ? { "aria-multiline": "true" } : {}),
      } } });
    }
    // Batch B (P1-3): clicking an atomic node in read-only mode leaves NodeSelection; after entering edit mode,
    // the first keystroke within <500ms **replaces the entire selected node** (reliably reproduced in walkthrough: typed text
    // consumes eyebrow). Collapse NodeSelection to a cursor there when entering edit mode — closes the incident window.
    if (editor && isEditMode) {
      const sel = editor.state.selection;
      if (sel instanceof NodeSelection) {
        const tr = editor.state.tr.setSelection(
          TextSelection.near(editor.state.doc.resolve(sel.from), 1),
        );
        editor.view.dispatch(tr);
      }
    }
  }, [editor, isEditMode, tCanvas]);

  // Canvas batch 3: when the restricted set changes (flag enabled after hydration, sources reevaluated) without body changes, decorations do not recompute;
  // dispatch an empty meta-only transaction to rerun the citation plugin (see hook comment; tests/wiring contract in the matching test file).
  useCitationDecorationRefresh(editor, numberedSourcesDenied);

  // P3.7: when the Mobile virtual keyboard opens, scroll cursor into view
  useVirtualViewport();

  // dev-only editor expose for debug / stress test (excluded from actual prod-bundle logic)
  useEffect(() => {
    if (typeof window !== "undefined" && process.env.NODE_ENV !== "production" && editor) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).__bowheadEditor = editor;
    }
  }, [editor]);

  // P3.4 AI rewrite state — canvasId automatically uses the real backend (#57 wired @ P4.5b).
  // (Declared before sync: sync suspended needs isAIRewriting; review #1)
  const aiRewrite = useAIRewrite({ editor, canvasId, enabled: isEditMode && !!canvasId });
  const isAIRewriting = aiRewrite.state.active || aiRewrite.state.reviewing;
  const editorOverlays = useReportOverlayManager({
    enabled: isEditMode,
    isStreaming,
    isAIRewriting,
  });

  // P3.2.6: connect PATCH wire — no PATCH when canvasId is null (spike safety)
  const sync = useDebouncedSync({
    editor,
    baseline: sections,
    reorderBaseline,
    canvasId,
    enabled: isEditMode && !!canvasId,
    // Keep saves suspended until the rewrite is accepted or reverted.
    suspended: isAIRewriting,
    debounceMs: 800,
    // #3: after successful save, write persisted content back to the store so initialDoc reflects local saves —
    // otherwise the store retains old server content; the next SSE-triggered setContent overwrites the just-saved text (including the just-
    // accepted rewrite) with the old version.
    onSaved: (sectionId, rev, patch) => {
      onSectionPatched({ section_id: sectionId, patch: patch as Record<string, unknown>, rev });
    },
    // A2: PATCH hits a deleted section (undo revived a local node / deleted elsewhere) → refetch from server,
    // converging local doc with server instead of trapping the user on "保存失败".
    onSectionMissing: () => {
      forceSetContentRef.current = true;
      void refreshFromServer();
    },
  });

  // P3.2.7: revision drawer toggle
  const [revisionOpen, setRevisionOpen] = useState(false);
  // Visual refresh batch 4b: revision history uses section names instead of block numbers (drawer uses only with flag enabled)
  const revisionSectionLabel = useCallback(
    (sectionId: string) => sectionDisplayLabel(sections.find((s) => s.id === sectionId)),
    [sections],
  );
  const saveErrorMessage = sync.lastError ? sync.lastError.message : null;

  // design r2 (report-edit-affordance) frame H: one-time guidance bar on first entering edit mode +
  // change marks/editable atoms flash together for 800ms. once semantics: mark seen on display (strictly once);
  // "知道了" only dismisses the current bar.
  const [showEditGuide, setShowEditGuide] = useState(false);
  const [affordanceFlash, setAffordanceFlash] = useState(false);
  // mobile r1 §2.4: guidance copy varies by form factor (touch points to persistent ⋯); seen key shared with desktop —
  // same guidance, so no need to see it again on mobile after viewing on desktop.
  const isTouch = useTouchAffordance();
  useEffect(() => {
    if (!isEditMode) {
      setShowEditGuide(false);
      setAffordanceFlash(false);
      return;
    }
    let seen = true;
    try {
      seen = localStorage.getItem("bowhead.report_edit.guide_seen") === "1";
    } catch {
      /* SSR / private mode: treat as already seen */
    }
    if (seen) return;
    try {
      localStorage.setItem("bowhead.report_edit.guide_seen", "1");
    } catch {
      /* noop */
    }
    setShowEditGuide(true);
    setAffordanceFlash(true);
    const timer = setTimeout(() => setAffordanceFlash(false), 800);
    return () => clearTimeout(timer);
  }, [isEditMode]);

  useImperativeHandle(ref, () => ({
    openHistory: () => { if (canvasId && !isEditMode) setRevisionOpen(true); },
    enterEditMode: () => {
      if (!canEdit || isStreaming || isEditMode) return;
      setLocalEditMode(true);
    },
    prepareForExport: async (request) => {
      if (!canvasId) {
        return {
          ok: false,
          reason: "surface_render_incomplete",
          message: "Canvas is not available for export",
        };
      }
      if (isStreaming) {
        return {
          ok: false,
          reason: "active_canvas_generation",
          message: "Canvas generation is still active",
        };
      }
      if (isAIRewriting) {
        return {
          ok: false,
          reason: "rewrite_in_progress",
          message: "Accept or revert the rewrite before exporting",
        };
      }
      if (sync.conflict) {
        return {
          ok: false,
          reason: "revision_conflict",
          sectionId: sync.conflict.sectionId,
          message: "Resolve the report revision conflict before exporting",
        };
      }

      const restorers = Array.from(presentationOverridesRef.current.values())
        .flatMap((callbacks) => Array.from(callbacks));
      presentationOverridesRef.current.clear();
      try {
        restorers.forEach((restore) => restore());
      } catch (error) {
        return {
          ok: false,
          reason: "unsaved_presentation_override",
          message: error instanceof Error ? error.message : String(error),
        };
      }
      await waitForStablePaint();

      const flushResult = await sync.flushNow();
      // Export is **read-only**; see `flushFailureBlocksReadOnlyAction`: failure to write back
      // ≠ failure to read. flush already provides persistent lastError/toast explaining which sections
      // failed to save; allowing export does not hide this from the user.
      if (!flushResult.ok && flushFailureBlocksReadOnlyAction(flushResult.reason)) {
        return {
          ok: false,
          reason: flushResult.reason,
          ...(flushResult.section_id ? { sectionId: flushResult.section_id } : {}),
          message: flushResult.message,
        };
      }

      let detail;
      try {
        detail = await fetchCanvasById(canvasId, {
          locale: exportLocale,
        });
      } catch (error) {
        return {
          ok: false,
          reason: "surface_projection_conflict",
          message: error instanceof Error ? error.message : String(error),
        };
      }
      const supportsRequestedV2 =
        !request.forceLegacy
        && (detail.exportV2Capabilities ?? []).some(
          (capability) =>
            capability.format === request.format
            && capability.profile === request.profile,
        );
      if (!supportsRequestedV2) {
        // Batch 0 0-A (chart-table-ux tech-design §1.1): distinguish deliberately using legacy
        // (template cloning) from forced fallback due to missing V2 capability. The latter previously silently returned
        // ok:true ⇒ caller showed no notice ⇒ actual prod used legacy export for a long time
        // (all images became floating-point tables) without anyone noticing. reason enables caller notification and telemetry.
        return {
          ok: true,
          mode: "legacy",
          locale: exportLocale,
          reason: request.forceLegacy ? "forced" : "v2_capability_unavailable",
        };
      }
      await waitForStablePaint();
      const root = reportRootRef.current;
      if (!root) {
        return {
          ok: false,
          reason: "surface_render_incomplete",
          message: "Report surface is not mounted",
        };
      }
      try {
        const effectiveChartStyle = getBrandSnapshot().chartStyle;
        return await validateAndBuildRenderedSurfaceReceipt({
          detail,
          localSections: exportSectionsRef.current,
          locale: exportLocale,
          chartStyle:
            effectiveChartStyle?.schemaVersion === 1
              ? effectiveChartStyle
              : undefined,
          chartStyleV2:
            effectiveChartStyle?.schemaVersion === 2
              ? effectiveChartStyle
              : undefined,
          revisionOf: sync.getSectionRev,
          renderStateOf: (entry) => inspectSurfaceRenderState(root, entry),
          exposeVisualIdentity: (entry, visualInputSha256, disposition) => {
            exposeSurfaceVisualIdentity(
              root,
              entry,
              visualInputSha256,
              disposition,
            );
          },
        });
      } catch (error) {
        return {
          ok: false,
          reason: "surface_render_incomplete",
          message: error instanceof Error ? error.message : String(error),
        };
      }
    },
  }), [
    canEdit,
    canvasId,
    exportLocale,
    isAIRewriting,
    isEditMode,
    isStreaming,
    sync.conflict,
    sync.flushNow,
    sync.getSectionRev,
  ]);

  // Batch B (P1-2): uncommitted blur-only inline edits — combine with sync.dirty for the actual
  // unsaved-change state. Previously dirty only: editing KPI without blur falsely showed "已保存",
  // beforeunload did not block, leaving edit mode did not flush (walkthrough §7 confirms silent edit loss).
  const pendingInline = useSyncExternalStore(
    subscribePendingInlineEdits,
    pendingInlineEditCount,
    () => 0,
  );
  const hasUnsavedEdits = sync.dirty.length > 0 || pendingInline > 0;
  const protectLeave = isEditMode && (hasUnsavedEdits || sync.saving || isAIRewriting);
  useEffect(() => {
    if (!protectLeave) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [protectLeave]);

  const navigationLifecycle = useRef({ protectLeave, flush: sync.flushNow });
  navigationLifecycle.current = { protectLeave, flush: sync.flushNow };
  useEffect(() => {
    if (!showEditControls) return;
    return registerCanvasNavigationGuard({
      hasUnsaved: () => navigationLifecycle.current.protectLeave,
      flush: async () => {
        const result = await navigationLifecycle.current.flush();
        // Let the original report editor publish its acknowledged lifecycle before the aggregate recheck.
        await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
        return result;
      },
    });
  }, [showEditControls, canvasId]);

  const handleEditToggle = () => {
    // A1-2 + batch B (P1-2): "完成" = successful flush (including inline edits not yet blurred) → reset doc
    // from server → exit editing. On flush failure **stay editing** (A1 exited anyway, leaving users in read-only
    // mode with unsaved content and no notice) + toast; without changes flushNow is a cheap no-op, so stop prechecking
    // dirty (prechecking misses pending inline edits).
    if (!isEditMode) {
      setLocalEditMode(true);
      return;
    }
    void (async () => {
      const res = await sync.flushNow();
      if (res && !res.ok) {
        toast({ kind: "error", text: res.reason === "rewrite_in_progress" ? res.message : tCanvas("edit_finish_save_failed") });
        return;
      }
      forceSetContentRef.current = true;
      await refreshFromServer();
      setLocalEditMode(false);
    })();
  };
  // Batch C (P3 hygiene): use in-product ConfirmDialog to confirm discard;
  // batch B: check hasUnsavedEdits(dirty ∪ pending inline); dirty alone misses
  // unblurred KPI/title edits → silent discard.
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);
  const discardAndExitEdit = () => {
    aiRewrite.cancel();
    setCancelConfirmOpen(false);
    forceSetContentRef.current = true;
    sync.discardLocalChanges();
    editorOverlays.closeAll();
    setLocalEditMode(false);
    void refreshFromServer();
  };
  const handleCancelEdit = () => {
    if (hasUnsavedEdits || isAIRewriting) {
      setCancelConfirmOpen(true);
      return;
    }
    discardAndExitEdit();
  };

  // #3: conflict reload / explicitly discard local and fetch server: force next setContent to bypass the unsaved-local-
  // edit guard (discarding local is intended). One-shot; clears automatically after setContent effect consumes it.
  const forceSetContentRef = useRef(false);

  // W11-5 / #42: section operations (delete / merge) enabled only with canvasId.
  // Refetch/hydrate after every operation to keep store and server synchronized.
  // W12-5: use store action instead of dispatching window event (architectural debt).
  const refreshFromServer = async () => {
    if (!canvasId) return;
    await refreshCanvas();
  };

  // WP3 (design D2Conflict): conflict-bar "重新载入".
  // Fix latent bug: resolveConflictByReload previously cleared conflict state without fetching the latest
  // server version (use-debounced-sync comment delegated fetching to caller, but ReportView never wired it) →
  // clicking reload left the local version unchanged. refreshCanvas here actually discards local and fetches server version.
  const handleConflictReload = async () => {
    forceSetContentRef.current = true; // #3: reload discards local for server; force next setContent
    sync.resolveConflictByReload();
    await refreshFromServer();
  };
  // A2 (report-edit-audit, contract=cascading deletion+confirmation): menu deletion is the sole deletion entry (keyboard path
  // always intercepted by SectionDeleteGuard). For hosts with inline charts, show cascade count in confirmation; actual deletion count
  // comes from the server response (FE local count only previews the confirmation).
  const [pendingDelete, setPendingDelete] = useState<{
    sectionId: string;
    inlineCount: number;
  } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const countInlineVisuals = useCallback(
    (sectionId: string): number => {
      const target = currentSections.find((s) => s.id === sectionId);
      if (!target) return 0;
      const ids = new Set<string>();
      const md = "markdown" in target ? String((target as { markdown?: string }).markdown ?? "") : "";
      for (const m of md.matchAll(/\[figure:([a-zA-Z0-9_]+)\]/g)) ids.add(m[1]);
      if (target.kind === "insight_card") {
        for (const ev of target.evidence ?? []) {
          if (ev?.chart_section_id) ids.add(ev.chart_section_id);
        }
      }
      // Count only references that actually exist in this report and are visual blocks (same kind allowlist as server).
      return [...ids].filter((id) =>
        currentSections.some((s) => s.id === id && (s.kind === "chart" || s.kind === "image")),
      ).length;
    },
    [currentSections],
  );
  const handleDeleteSection = async (sectionId: string) => {
    if (!canvasId) return;
    // canvas-elements source-merging batch: sources section carries provenance shared across the whole document; both server entries reject deletion
    // (409). Intercept here first with a **reason** — otherwise users click a control guaranteed to fail with only
    // "删除失败，请重试" (the advertise-vs-execute class of mismatch).
    if (sections.find((s) => s.id === sectionId)?.kind === "sources") {
      toast({ kind: "error", text: tCanvas("section_delete_protected") });
      return;
    }
    setPendingDelete({ sectionId, inlineCount: countInlineVisuals(sectionId) });
  };
  const confirmDeleteSection = async () => {
    if (!canvasId || !pendingDelete) return;
    const { sectionId, inlineCount } = pendingDelete;
    setDeleteBusy(true);
    try {
      const res = await apiDeleteCanvasSection(canvasId, sectionId, {
        cascade: inlineCount > 0,
      });
      await refreshFromServer();
      const extra = res.deletedSectionIds.length - 1;
      toast({
        kind: "info",
        text: extra > 0
          ? tCanvas("section_deleted_with_figures", { count: extra })
          : tCanvas("section_deleted"),
      });
    } catch (e) {
      toast({ kind: "error", text: tCanvas("section_delete_failed"), action: copyDiagnosticsAction(e, "task") });
      // eslint-disable-next-line no-console
      console.warn("[chrome menu] delete failed", e);
    } finally {
      setDeleteBusy(false);
      setPendingDelete(null);
    }
  };
  const handleSplitAtCursor = async (sectionId: string) => {
    if (!canvasId || !editor) return;
    // r3 §5.2: early returns are no longer silent — since launch, menu clicks stole focus so split always had offset=0,
    // discovered only in dogfood. SectionChrome fixed focus theft with mousedown preventDefault;
    // provide user feedback for the remaining early-return paths here.
    const nudge = (): void => {
      toast({ kind: "info", text: tCanvas("split_place_cursor") });
    };
    // Find enclosing section node + compute cursor markdown-character offset relative to section start
    const sel = editor.state.selection;
    if (!sel.empty) return nudge(); // Cursor mode only, no selection (selecting a range does not identify a split point)
    const { $from } = sel;
    let sectionStartPos: number | null = null;
    let sectionKind: string | null = null;
    for (let depth = $from.depth; depth >= 0; depth--) {
      const n = $from.node(depth);
      const name = n.type.name;
      if (name === "section_narrative" || name === "section_deck" || name === "section_callout") {
        sectionStartPos = $from.start(depth);
        sectionKind = name;
        // Verify sectionId matches (guards cursor in another section)
        const sid = (n.attrs as { sectionId?: string }).sectionId;
        if (sid !== sectionId) return nudge(); // Cursor is outside the target section
        break;
      }
    }
    if (sectionStartPos == null || !sectionKind) return nudge();
    // textBetween separates blocks with "\n", approximating markdown character count (formatting tokens excluded — small discrepancy)
    const charOffset = editor.state.doc.textBetween(sectionStartPos, sel.from, "\n").length;
    try {
      // r3 §5.2: flush first — otherwise offset uses editor text while server splits old markdown,
      // misaligning the split point when unsaved edits exist.
      // ⚠ Must **handle the result**: flush failure breaks the invariant above; proceeding splits old markdown
      // at the wrong position, then forced refresh overwrites local draft ⇒ user edits silently disappear.
      const flushed = await sync.flushNow();
      if (!flushed.ok) {
        toast({ kind: "error", text: tCanvas("split_needs_saved_edits") });
        return;
      }
      await apiSplitCanvasSection(canvasId, sectionId, charOffset);
      // r3 §5.2 reconciliation trio (feedback-0713 #6 precedent): server is authoritative after split;
      // bypass the unsaved guard — otherwise editor section ids remain stale and later operations target dead ids.
      // Local edits already flushed; forced loses no content.
      forceSetContentRef.current = true;
      await refreshFromServer();
    } catch (e) {
      // 422 = degenerate boundary split (offset 0/len); server refuses empty shells — notify instead of error
      const degenerate = /: 422$/.test(e instanceof Error ? e.message : String(e));
      toast({
        kind: degenerate ? "info" : "error",
        text: degenerate ? tCanvas("split_boundary_noop") : tCanvas("split_failed"),
        action: degenerate ? undefined : copyDiagnosticsAction(e, "task"),
      });
      // eslint-disable-next-line no-console
      console.warn("[chrome menu] split failed", e);
    }
  };
  // Visual refresh batch 4b: on opening section menu, evaluate whether "在此处分割" is currently available (same early-return conditions as handleSplitAtCursor:
  // cursor has no text selection and is inside this section's body); if unavailable, disable item with reason instead of notifying after click.
  const canSplitAtCursor = useCallback((sectionId: string): boolean => {
    if (!editor) return false;
    const sel = editor.state.selection;
    if (!sel.empty) return false;
    const { $from } = sel;
    for (let depth = $from.depth; depth >= 0; depth--) {
      const n = $from.node(depth);
      const name = n.type.name;
      if (name === "section_narrative" || name === "section_deck" || name === "section_callout") {
        return (n.attrs as { sectionId?: string }).sectionId === sectionId;
      }
    }
    return false;
  }, [editor]);
  // mobile r1 §2.1: touch reordering = change PM doc top-level order; existing debounced sync detects
  // order changes and sends full reorder (same pipeline as desktop drag). Notify at boundaries instead of silent no-op.
  const handleMoveSection = (sectionId: string, dir: "up" | "down") => {
    if (!editor) return;
    const moved = editor
      .chain()
      .focus()
      .command(({ tr, state, dispatch }) => {
        if (!dispatch) return applySectionMove(tr, state.doc, sectionId, dir);
        return applySectionMove(tr, state.doc, sectionId, dir);
      })
      .run();
    if (!moved) {
      toast({
        kind: "info",
        text: dir === "up" ? tCanvas("move_at_top") : tCanvas("move_at_bottom"),
      });
    }
  };
  const handleMergeWithPrev = async (sectionId: string) => {
    if (!canvasId) return;
    // Find previous text-based (narrative/deck/callout) section
    const idx = sections.findIndex((s) => s.id === sectionId);
    if (idx <= 0) return;
    let prevIdx = idx - 1;
    while (prevIdx >= 0) {
      const k = sections[prevIdx].kind;
      if (k === "narrative" || k === "deck" || k === "callout") break;
      prevIdx -= 1;
    }
    if (prevIdx < 0) return;
    const survivor = sections[prevIdx];
    const absorbed = sections[idx];
    if (
      (survivor.kind !== "narrative" && survivor.kind !== "deck" && survivor.kind !== "callout") ||
      (absorbed.kind !== "narrative" && absorbed.kind !== "deck" && absorbed.kind !== "callout")
    ) {
      return;
    }
    const survivorMd = (survivor as { markdown?: string }).markdown || "";
    const absorbedMd = (absorbed as { markdown?: string }).markdown || "";
    const merged = survivorMd ? `${survivorMd}\n\n${absorbedMd}` : absorbedMd;
    const refsA = (survivor as { source_refs?: string[] }).source_refs || [];
    const refsB = (absorbed as { source_refs?: string[] }).source_refs || [];
    const mergedRefs = Array.from(new Set([...refsA, ...refsB]));
    try {
      // r3 §5.2: like split — flush first to keep the split surface consistent, then force reconciliation on success,
      // preventing stale editor section ids (absorbed section is dead but remains in doc).
      // ⚠ Must handle the result: `merged` is assembled from store (server state), **excluding unsaved local edits**;
      // proceeding after flush failure overwrites merge result with old content, then forced refresh wipes local draft.
      const flushed = await sync.flushNow();
      if (!flushed.ok) {
        toast({ kind: "error", text: tCanvas("merge_needs_saved_edits") });
        return;
      }
      await apiMergeCanvasSections(canvasId, survivor.id, absorbed.id, merged, mergedRefs);
      forceSetContentRef.current = true;
      await refreshFromServer();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("[chrome menu] merge failed", e);
    }
  };

  // WP3 (design D2): block AI bar — opened by narrative block action-cluster "Edit with AI".
  // Submit free-form instruction → obtain whole-block range → custom rewrite SSE → AIRewriteDiffBar takes over.
  const handleEditBlockWithAI = (sectionId: string) => editorOverlays.openBlockAI(sectionId);

  // When sections change (new SSE patch / append / streaming chunk), rerun setContent.
  // B9 (W13 fix): previous useMemo was an antipattern — setContent is a side effect (dispatches PM transaction
  // triggering NodeView setState); synchronous calls during render cause 'Cannot update a component while
  // rendering' + flushSync warnings. useEffect runs setContent in commit phase.
  // #306 fix: no setContent while AI rewrite runs (state.active) — otherwise streaming text
  // is wiped + oldRange invalidated; user acceptance deletes wrong range → block disappears. Streaming protected likewise.
  // CTO review P0-R2.1 fix: ref tracks previous initialDoc; call setContent only when initialDoc truly
  // changes. Otherwise flipping isAIRewriting (user accepts rewrite) → useEffect fires →
  // editor.getJSON() contains new text ≠ initialDoc (old) → erroneous setContent wipes user rewrite.
  // Sprint chat-stream-fix B-1 (2026-05-16): previously also skipped when
  // `isStreaming` true — but that froze the editor against new chart sections
  // arriving via canvas.section_added during streaming. Result: user saw "AI
  // 正在生成报告" banner indefinitely + had to manually F5 to see charts. The
  // `prevInitialDocRef === initialDoc` early-exit below already prevents
  // wipe-on-stale-doc, so this guard was redundant.
  // Only `isAIRewriting` keeps the guard (we don't want to clobber user's
  // mid-rewrite local edits). Streaming-side render is now incremental.
  const prevInitialDocRef = useRef<typeof initialDoc | null>(null);
  useEffect(() => {
    if (!editor || !initialDoc) return;
    if (isAIRewriting) return;
    const gate = gateReportSetContent({
      overlayKind: editorOverlays.activeKind,
      forced: forceSetContentRef.current,
      dirtyCount: sync.dirty.length,
    });
    // defer (feedback-0713 F1): do not mark initialDoc seen — after overlay closes
    // (activeKind in deps), effect reruns and applies normally; updates are not lost.
    if (gate === "defer") return;
    // initialDoc unchanged (object identity) → no setContent (avoids wiping local edits)
    if (prevInitialDocRef.current === initialDoc) return;
    // #3: with unsaved local edits (including just-accepted rewrite), do not overwrite with server initialDoc.
    // Mark that initialDoc seen before returning — save (onSaved→onSectionPatched writes back
    // to store) creates a new initialDoc (including local content), applied normally; SSE changes are not permanently lost.
    // Conflict reload bypasses via forceSetContentRef (discarding local is intended).
    const forced = forceSetContentRef.current;
    prevInitialDocRef.current = initialDoc;
    forceSetContentRef.current = false;
    if (gate === "skip-mark-seen") return;
    const current = editor.getJSON();
    const currentJson = JSON.stringify(current);
    // ★ Compare with **normalized** initialDoc rather than raw output.
    // Left `editor.getJSON()` is serialized by editor schema; raw output on the right would use
    // a different representation — any output attr absent from schema (observed list `tight`)
    // makes this comparison **always true**, needlessly rebuilding the whole document on every `initialDoc` object-identity change
    // (116 measurement: ~13 times per full-page load, affecting 64% of reports).
    // `normalizeDocForEditor` shares implementation with `setContent`; compare the actual representation
    // the editor holds after application — corrects the gate rather than bypassing it. See file header and
    // docs/canvas-report-optimization/setcontent-gate-spike-2026-08-08.md。
    const nextJson = JSON.stringify(normalizeDocForEditor(editor.schema, initialDoc));
    if (currentJson !== nextJson) {
      // feedback-0713 #7 draft-preservation invariant: re-hydrate returns empty doc while editor is nonempty
      // → refuse application, preserving original text. Whatever upstream cause (fetch race / server exception / folding bug),
      // overwriting user drafts with empty content is unacceptable; temporary display desync costs far less than losing drafts.
      // review P1: explicit discard-local paths (cancel editing/conflict reload/rollback, forced=true) bypass this
      // gate — user explicitly requests discard; blocking would resurrect stale draft via PATCH in next edit.
      if (shouldRefuseSetContent(initialDoc, current, forced)) {
        console.warn(
          "[report-view] refused empty-doc setContent over non-empty editor (anti-swallow, feedback-0713 #7)",
        );
        return;
      }
      const start = typeof performance !== "undefined" ? performance.now() : Date.now();
      editor.commands.setContent(initialDoc, { emitUpdate: false });
      const end = typeof performance !== "undefined" ? performance.now() : Date.now();
      trackReportSetContent({
        sectionCount: filtered.length,
        docBytes: nextJson.length,
        durationMs: Math.max(0, end - start),
      });
    }
    // dogfood-0708 #12 remainder fix: after content (re)load, use editor-**normalized** top-level order for reorder
    // baseline — raw canvasToPMDoc(initialDoc) order differs from actual editor top-level order in some reports
    // (narrative embedded in figures / merged nodes, etc.) → first edit sends one-off false reorder. With editor order,
    // currentOrder==baseline without user reordering, eliminating false sends. ordersEqual guard avoids unnecessary re-render.
    const editorOrder = extractTopLevelSectionOrder(editor.getJSON());
    setReorderBaseline((prev) => (ordersEqual(prev, editorOrder) ? prev : editorOrder));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialDoc, isAIRewriting, filtered.length, sync.dirty.length, editorOverlays.activeKind]);

  return (
    <CanvasContextProvider
      sections={sections}
      canvasId={canvasId}
      sessionId={sessionId}
      currentTurnId={currentTurnId}
      isEditMode={isEditMode}
      isStreaming={isStreaming}
      readOnly={readOnly}
      sourceRefs={sourceRefs}
      citationSources={citationSources}
      flushPendingEdits={sync.flushNow}
      registerPresentationOverride={registerPresentationOverride}
      getSectionRev={sync.getSectionRev}
      acceptServerSection={sync.acceptServerSection}
      onDeleteSection={isEditMode && canvasId ? handleDeleteSection : undefined}
      onMergeWithPrev={isEditMode && canvasId ? handleMergeWithPrev : undefined}
      onSplitAtCursor={isEditMode && canvasId ? handleSplitAtCursor : undefined}
      canSplitAtCursor={isEditMode && canvasId ? canSplitAtCursor : undefined}
      onMoveSection={isEditMode && canvasId ? handleMoveSection : undefined}
      onEditBlockWithAI={isEditMode && canvasId ? handleEditBlockWithAI : undefined}
    >
    {/* Sprint γ: SourcePanelProvider wraps so citation click can dispatch open. */}
    <SourceAccessRegistryProvider scopeKey={sourceAccessScopeKey} onRedactAlias={redactSourceAlias}>
    <SourcePanelProvider>
      <SourcePanel />
      {/* report-streaming D3: previously passed sectionCount={filtered.length}, making the banner report
          "已生成 N 个部分" — N counted all sections (including charts) across turns.
          Banner now classifies/counts from canvas-store by turnId itself (selectTurnSurfaceCounts). */}
      {isStreaming && !toolbarStatus && (
        <StreamingBanner
          turnId={currentTurnId}
          onCancel={onCancelTurn}
        />
      )}
      {/* Visual refresh batch 4b: toolbar status group — writing progress while writing (replaces banner above), otherwise status badge
          (replaces the report-header badge below); edit group explains editing via "正在编辑 · 保存状态", omitted here. */}
      {toolbarStatus && statusHost
        ? createPortal(
            <ReportToolbarStatusGroup
              isStreaming={isStreaming}
              turnId={currentTurnId}
              onCancel={onCancelTurn}
              turnTerminalStatus={turnTerminalStatus}
              connection={reportStreamConnection}
              interruption={writingInterruptionKind}
              sectionsSaved={turnReportSectionsSaved}
              onRefresh={canvasId ? () => void refreshFromServer() : undefined}
              idle={canvasId && !isEditMode ? <ReportStatusChip canvasId={canvasId} readOnly={!canEdit} hideDefault /> : null}
            />,
            statusHost,
          )
        : null}
      {/* report-streaming D2: phase before the output begins taking shape (prod p50 62s / p90 173s,
          77% of turns have their longest stationary phase here) — band determines its own visibility; appearance and exit
          rules are documented at the component top. */}
      {isStreaming && (
        <WaitBand turnId={currentTurnId} />
      )}
      {showEditControls && (() => {
        const controls = <EditModeToggle
          isEditMode={isEditMode}
          onToggle={handleEditToggle}
          onCancelEdit={handleCancelEdit}
          hasUnsavedChanges={hasUnsavedEdits || isAIRewriting}
          saving={sync.saving}
          error={saveErrorMessage}
          disabled={!canEdit || isStreaming}
          disabledReason={
            isStreaming
              ? tCanvas("readonly_streaming")
              : readOnlyReason ?? tCanvas("readonly_no_context")
          }
          onRetry={() => void sync.flushNow()}
        />;
        if (editControlsHost === undefined) return controls;
        return editControlsHost ? createPortal(controls, editControlsHost) : null;
      })()}
      <div
        ref={reportRootRef}
        className={`canvas-report-shell${presentation === "results-reading" ? " results-reading" : ""}`}
        data-presentation={presentation}
        data-reading-width={readingWidth}
        data-edit-mode={isEditMode ? "on" : "off"}
        data-can-edit={canEdit ? "true" : "false"}
        data-affordance-flash={affordanceFlash ? "on" : "off"}
      >
      {isEditMode && showEditGuide && (
        <div className="rc-guide" role="status">
          <LucideGlyph icon={LucideLightbulb} size={14} aria-hidden="true" />
          {/* mobile r1 §2.4: no hover on touch; guidance must point to persistent ⋯ instead of "悬停任意区块" */}
          <span className="rc-guide-text">
            {tCanvas(isTouch ? "edit_guide_text_touch" : "edit_guide_text")}
          </span>
          <button
            type="button"
            className="rc-guide-dismiss"
            onClick={() => setShowEditGuide(false)}
          >
            {tCanvas("edit_guide_got_it")}
          </button>
        </div>
      )}
      {sync.lastError && (
        <>
          {/* Explain what happened and what to do; block ID, status code and request ID go into "复制诊断" (outside announcement container). */}
          <div role="alert" className="canvas-report-alert canvas-report-alert--error">
            {sync.lastError.message}
          </div>
          <ErrorDiagnosticsInline error={sync.lastError.error} surface="task" />
        </>
      )}
      {sync.conflict && (
        <div role="alert" className="rc-conflict-bar">
          <History size={14} className="legacy-rc-conflict-icon" aria-hidden="true" />
          <span className="rc-conflict-msg">{tConflict("message")}</span>
          <button
            type="button"
            className="rc-pill-btn accept"
            onClick={() => void handleConflictReload()}
          >
            {tConflict("reload")}
          </button>
          <button
            type="button"
            className="rc-pill-btn ghost"
            onClick={() => void sync.resolveConflictByOverride()}
          >
            {tConflict("keep_mine")}
          </button>
        </div>
      )}
      {/* feedback-0713 #6: history entry adds isEditMode gate — complements onDeleteSection/
          onMergeWithPrev (enabled only when editing; history must hide while editing): opening history/rollback in edit mode
          causes out-of-band writes + stuck edit mode; previously checking only canvasId was missed wiring,
          not a design choice. */}
      {canvasId && !isEditMode && !hideHistoryControl && (
        <div className="rc-report-header-meta">
          {/* R2: report lifecycle status chip (verified/stale/archived) + transition menu */}
          {toolbarStatus ? null : <ReportStatusChip canvasId={canvasId} hideDefault />}
          <button
            type="button"
            onClick={() => setRevisionOpen(true)}
            className="canvas-report-history-btn"
            title={tCanvas("history_tooltip")}
            aria-label={tCanvas("history_tooltip")}
          >
            <History size={14} aria-hidden="true" />
            <span>{tCanvas("history")}</span>
          </button>
        </div>
      )}
      {/* α v1 (collapse-in-place): prior superseded reports, folded above the current
          one as an expandable two-level history accordion (nothing vanishes).
          feedback-0713 #6: same edit-mode gate hides this (accordion embeds read-only ReportView; opening
          while editing would interfere with editor state). */}
      {supersededSections.length > 0 && !isEditMode && (
        <ReportHistory
          superseded={supersededSections}
          allSections={sections}
          renderArchived={(secs) => (
            <ReportView
              // strip `superseded` so the nested read-only render treats these as a
              // normal (current) report — else it would recursively fold them into
              // its OWN history accordion + show an empty "current" report.
              //
              // canvas-elements source-merging batch: `[N]` is now **canvas-scoped**; entire canvas has only one
              // sources section, belonging to no turn's archive group — without it, [N] in folded history
              // all becomes plain text because numberedLen=0. Numbers are global and append-only; therefore using
              // the current table to resolve archived turns' [N] is correct (more accurate than a table per turn).
              sections={secs.map((s) => ({ ...s, superseded: false }))}
              // canvas-elements source-merging batch: `[N]` is **canvas-scoped**; entire canvas has only one
              // sources section, retained in current report. Without this prop, archived numberedLen=0 and all historical [N]
              // becomes plain text; pass the **table**, not a section inserted into the group — insertion renders an extra source block.
              citationSources={canvasCitationSources}
              canvasId={null}
              canEdit={false}
              showEditControls={false}
              sourceRefs={sourceRefs}
              // review F6: sidecar deliberately includes superseded-turn rows (backend comment
              // says superseded renders in folded history) — failing to forward discards fetched data at the final
              // prop, so [cite:N] in folded history can never light up.
              turnCitations={turnCitations}
            />
          )}
        />
      )}
      {/* B1a: source linkage summary — source citations / numbers pending verification / source entries shown independently.
          α v1: pass `currentSections` (superseded history folded out) so the linkage
          facts reflect the CURRENT report, not the folded prior reports. Still
          includes insight_card-absorbed charts of the current turn (they're not
          superseded), so the original "count absorbed-chart markers" intent holds. */}
      <SourceLinkageStrip sections={currentSections} isStreaming={isStreaming} />
      {filtered.length === 0 && !isStreaming ? (
        // Feishu 07.06 #22: caller (e.g. Code panel) can override report empty state
        emptyState !== undefined ? <>{emptyState}</> : <EmptyReportState />
      ) : (
        <ReportArticle
          editor={editor}
          isEditMode={isEditMode}
          isStreaming={isStreaming}
          isAIRewriting={isAIRewriting}
          streamingSections={streamingSections}
          contentLang={contentLang}
        />
      )}
      {/* canvas-update-turn B1-2: old charts replaced by revision turn (+ accompanying code blocks) — one folded row at report end,
          same shell as History; hidden in edit mode like History. */}
      {replacedFigures.length > 0 && !isEditMode && (
        <ReplacedFigures
          figures={replacedFigures}
          renderArchived={(secs) => (
            <ReportView
              sections={secs.map((s) => ({ ...s, superseded: false }))}
              citationSources={canvasCitationSources}
              canvasId={null}
              canEdit={false}
              showEditControls={false}
              sourceRefs={sourceRefs}
              turnCitations={turnCitations}
            />
          )}
        />
      )}
      </div>
      <AIRewriteDiffBar rewrite={aiRewrite} editor={editor} />
      {/* WP3 (design D2): block AI rewrite input bar (narrative blocks) */}
      <BlockAIBar
        editor={editor}
        sectionId={editorOverlays.blockAISectionId}
        onClose={() => editorOverlays.close("block-ai")}
        floatingRef={editorOverlays.registerFloating("block-ai")}
        onSubmit={(instruction, range) => aiRewrite.start("custom", instruction, range)}
        draftScope={canvasId ?? sessionId}
      />
      {/* WP3 (design D2TextEdit): single selection toolbar — formatting + AI shortcuts (rewrite/shorten/formal) */}
      <FormattingToolbar
        editor={editor}
        open={editorOverlays.isSelectionToolbarOpen}
        isEditMode={isEditMode}
        isStreaming={isStreaming}
        isAIRewriting={isAIRewriting}
        onOpen={editorOverlays.openSelectionToolbar}
        onClose={() => editorOverlays.close("selection-toolbar")}
        floatingRef={editorOverlays.registerFloating("selection-toolbar")}
        onAIAction={(preset) => aiRewrite.start(preset)}
        onAICustom={(instruction, range) => aiRewrite.start("custom", instruction, range)}
        draftScope={canvasId ?? sessionId}
      />
      {canvasId && (
        <SectionRevisionDrawer
          canvasId={canvasId}
          open={revisionOpen}
          onClose={() => setRevisionOpen(false)}
          sectionLabel={revisionSectionLabel}
          canEdit={canEdit}
          onRolledBack={async () => {
            // feedback-0713 #6 rollback trio: ①exit edit mode (entry already gates editing,
            // retained defensively) ②force next setContent past unsaved guard + discard local dirty bookkeeping
            // (out-of-band write advances server rev, making local dirty meaningless) ③refresh
            // fetches actual post-rollback state — sync rebuilds reconciliation baseline from new sections (rev).
            setLocalEditMode(false);
            editorOverlays.closeAll();
            sync.discardLocalChanges();
            forceSetContentRef.current = true;
            await refreshFromServer();
          }}
        />
      )}
      {verifiedEditConfirmDialog}
      {cancelConfirmOpen && (
        <ConfirmDialog
          title={tCanvas("edit_cancel_title")}
          description={tCanvas("edit_cancel_confirm")}
          confirmLabel={tCanvas("edit_cancel_discard_btn")}
          cancelLabel={tCanvas("edit_cancel_keep_btn")}
          tone="danger"
          onCancel={() => setCancelConfirmOpen(false)}
          onConfirm={discardAndExitEdit}
        />
      )}
      {pendingDelete && (
        <ConfirmDialog
          title={tCanvas("section_delete_title")}
          description={
            pendingDelete.inlineCount > 0
              ? tCanvas("section_delete_confirm_with_figures", { count: pendingDelete.inlineCount })
              : tCanvas("section_delete_confirm")
          }
          confirmLabel={tCanvas("section_delete_confirm_btn")}
          cancelLabel={tCanvas("section_delete_cancel_btn")}
          tone="danger"
          busy={deleteBusy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void confirmDeleteSection()}
        />
      )}
    </SourcePanelProvider>
    </SourceAccessRegistryProvider>
    </CanvasContextProvider>
  );
});

/**
 * Visual refresh batch 4b: human-readable block names — heading text, first body line (without markdown heading markers), or chart title;
 * truncate beyond 24 characters. Return null when unrecognized (caller only identifies the author). exported for tests.
 */
export function sectionDisplayLabel(section: ReportSection | undefined): string | null {
  if (!section) return null;
  const s = section as unknown as {
    markdown?: string;
    text?: string;
    title?: string;
    spec?: { title?: string };
  };
  const raw = s.title ?? s.spec?.title ?? s.text ?? s.markdown ?? "";
  const firstLine = raw.split("\n").map((line) => line.trim()).find(Boolean) ?? "";
  const flat = firstLine.replace(/^#{1,6}\s+/, "").replace(/\s+/g, " ").trim();
  if (!flat) return null;
  return flat.length > 24 ? `${flat.slice(0, 24)}…` : flat;
}

function inspectSurfaceRenderState(
  root: HTMLElement,
  entry: import("@/lib/api").CanvasSurfaceEntryDTO,
): SurfaceRenderState {
  if (entry.placement === "data_tab" || entry.placement === "code_tab") return "registered";
  const markers = surfaceMarkers(root, entry.sectionId);
  if (markers.length === 0) return "missing";
  if (entry.parityRequirement === "exact_visual" && markers.some((element) => (
    element.matches("[data-export-render-state='loading']")
    || element.querySelector("[data-export-render-state='loading']")
  ))) {
    return "missing";
  }
  if (entry.parityRequirement === "exact_visual" && markers.some((element) => (
    element.matches("[data-export-render-state='error']")
    || element.querySelector(
      "[data-export-render-state='error'], .v2-chart-fallback-wrap, .rc-spec-fallback",
    )
  ))) {
    return "runtime_fallback";
  }
  return "rendered";
}

function exposeSurfaceVisualIdentity(
  root: HTMLElement,
  entry: import("@/lib/api").CanvasSurfaceEntryDTO,
  visualInputSha256: string,
  disposition: "rendered",
): void {
  for (const marker of surfaceMarkers(root, entry.sectionId)) {
    marker.dataset.exportVisualInputSha256 = visualInputSha256;
    marker.dataset.exportRenderDisposition = disposition;
  }
}

function surfaceMarkers(
  root: HTMLElement,
  sectionId: string,
): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(
    "[data-section-id], [data-export-section-id]",
  )).filter((element) => (
    element.dataset.sectionId === sectionId
    || element.dataset.exportSectionId === sectionId
  ));
}

async function waitForStablePaint(): Promise<void> {
  if (typeof requestAnimationFrame !== "function") {
    await Promise.resolve();
    return;
  }
  await new Promise<void>((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    const timeout = window.setTimeout(finish, 100);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        window.clearTimeout(timeout);
        finish();
      });
    });
  });
}

/** P1 (design D6Empty): empty-report invitation state — serif heading + quiet explanation */
function EmptyReportState() {
  const t = useTranslations("canvas");
  return (
    <div className="rc-empty" data-testid="report-empty-state">
      <div className="rc-empty-inner">
        <div className="rc-empty-icon" aria-hidden>
          <LucideGlyph icon={LucideFile} size={22} />
        </div>
        <div className="rc-empty-title">{t("empty_title")}</div>
        <div className="rc-empty-sub">{t("empty_sub")}</div>
      </div>
    </div>
  );
}

/** Wrap article + EditorContent + CitationHoverManager (ref listens for hover) */
function ReportArticle({
  editor,
  isEditMode,
  isStreaming = false,
  isAIRewriting = false,
  streamingSections = [],
  contentLang,
}: {
  /** P2 batch 6: `lang` for a CJK report, see detectCjkContentLang. */
  contentLang?: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  editor: any;
  isEditMode: boolean;
  isStreaming?: boolean;
  /** W2-6: lock the whole article during AI rewrite (avoids conflicts from user editing other sections).
   *  For section-scoped lock, see W1b follow-up. */
  isAIRewriting?: boolean;
  /** report-streaming B: actively streaming sections render **outside the editor** (see StreamingPreview
   *  header comment: measured high-frequency chunks in PM doc create a 1833ms blocking task).
   *  Placed after EditorContent, inside article, immediately following finalized content in document flow;
   *  when patch arrives, replacement stays in place instead of jumping position. */
  streamingSections?: ReportSection[];
}) {
  const articleRef = useRef<HTMLElement | null>(null);
  return (
    <>
      <article
        ref={articleRef}
        className="v2-body canvas-report-article"
        lang={contentLang}
        data-edit-mode={isEditMode ? "on" : "off"}
        data-streaming={isStreaming ? "on" : "off"}
        data-ai-rewriting={isAIRewriting ? "on" : "off"}
        // Report Canvas Chrome Spec section 9: completed sections do not dim collectively during writing;
        // only the actively written section has outline and explanation (StreamingPreview); aria-busy and toolbar status group inform screen readers of generation.
        // ui-refresh-canvas.css handles dimming / disabling pointer input using the two data attributes above (AI rewrite still dims).
        aria-busy={isStreaming ? true : undefined}
      >
        <EditorContent editor={editor} />
        <StreamingPreview sections={streamingSections} />
      </article>
      <CitationHoverManager rootRef={articleRef} />
    </>
  );
}
