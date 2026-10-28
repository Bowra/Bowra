/**
 * CanvasContext — expose global canvas state to NodeViews.
 *
 * Supports:
 *   - insight_card.evidence[].chart_section_id referencing chart specs across sections
 *   - NodeView awareness of isEditMode (P3.2.5 inline editing toggles contentEditable)
 *   - NodeView awareness of isStreaming (P3.5 whole-turn lock)
 *   - dispatchPatch calling the PATCH endpoint after edits (P3.2.6 wiring)
 *
 * Usage:
 *   Wrap ReportView at the top level with <CanvasContextProvider sections={...} canvasId={...}>
 *   Inside NodeView: const { chartLookup, isEditMode } = useCanvasContext()
 */

"use client";

import { appFetch as fetch, appPath } from "@/lib/base-path";
import React, { createContext, useContext, useEffect, useMemo } from "react";
import type { ReportSection } from "@/lib/types";
import type { ChartSpec } from "@/lib/chart/types";
import type { FlushResult } from "@/lib/use-debounced-sync";
import { copyKbFields, isViewerSourceAlias, type SourceRef } from "./citation-types";

/** kind="image" section data (embedded by insight_card visual references). */
export type ImageSectionData = Extract<ReportSection, { kind: "image" }>;

export interface CanvasContextValue {
  /** All sections (for cross-section lookups). */
  sections: ReportSection[];
  /** chart_section_id → ChartSpec mapping for insight_card.evidence references. */
  chartLookup: Map<string, ChartSpec>;
  /** feedback-0713 #3: image section id → section data. chart_section_id is a visual
   * reference (same field for chart/image); compute peer matplotlib PNGs are persisted by
   * add_image as kind="image", embedded here when insight_card.evidence references them. */
  imageLookup: Map<string, ImageSectionData>;
  /** P3.3: source_id → SourceRef metadata mapping (Citation Plugin hover). */
  sourceLookup: Map<string, SourceRef>;
  /** Sprint agent-redesign-1 Phase F: ordered sources from canvas sources section.
   * `[N]` chip click → numberedSources[N-1] → render popover content. */
  numberedSources: Array<Record<string, unknown>>;
  /** Edit-mode switch (desktop always-on / mobile toggle / false while streaming). */
  isEditMode: boolean;
  /** Whole-turn lock while streaming; NodeView ignores interaction. */
  isStreaming: boolean;
  /** Read-only report surface: section interactions must not mutate or start new turns. */
  readOnly: boolean;
  /** Current canvas id (for PATCH endpoint). */
  canvasId: string | null;
  /**
   * content-permissions: session_id of a persisted report (status==="done"). When nonempty, section
   * artifact fetching uses the permission intermediary endpoint (owner∨grant authorization), exposing images/poster backgrounds to share recipients;
   * null (live streaming / non-report context) → fallback owner-gated /api/v1/files/{id} (current behavior unchanged).
   */
  sessionId: string | null;
  /** Sprint ι (PRD §10.1): current turn_id for popover cross-turn no-op comparisons. */
  currentTurnId: string | null;
  /**
   * Trigger PATCH (implemented after P3.2.6 wiring).
   * Placeholder in P3.2.2, default no-op.
   */
  dispatchPatch: (sectionId: string, patch: Partial<ReportSection>) => void;
  /** Appearance refresh batch 4b: whether this section currently allows "在此处分割" (cursor within this section, no selected text).
   * Section menu disables the item and explains why; default is allowed (onSplitAtCursor notifies after click). */
  canSplitAtCursor?: (sectionId: string) => boolean;
  /** Flush pending local PM edits before actions that read from server state. */
  flushPendingEdits?: () => Promise<FlushResult>;
  /** Register a view-only chart presentation override so export preparation can restore it. */
  registerPresentationOverride?: (sectionId: string, restore: () => void) => () => void;
  /** Latest known section rev, used by server-side actions that must reject stale state. */
  getSectionRev?: (sectionId: string) => number | undefined;
  /** Accept a full section returned by a server-side action that bypassed debounced PATCH. */
  acceptServerSection?: (section: ReportSection) => void;
  /** W11-5 / #42: section actions (delete / merge); null hides chrome menu. */
  onDeleteSection?: (sectionId: string) => Promise<void> | void;
  onMergeWithPrev?: (sectionId: string) => Promise<void> | void;
  onMergeWithNext?: (sectionId: string) => Promise<void> | void;
  /** W11-15 / #66: split section at PM cursor (narrative/deck/callout only). */
  onSplitAtCursor?: (sectionId: string) => Promise<void> | void;
  /** mobile r1 §2.1: touch reorder swaps a top-level node with its neighbor (PM doc order, existing reorder pipeline). */
  onMoveSection?: (sectionId: string, dir: "up" | "down") => void;
  /** WP3 (design D2): narrative action cluster "✦ Edit with AI" opens the block-level AI bar.
   * ReportView implements it (select whole block + float rc-ai-bar). undefined hides the button. */
  onEditBlockWithAI?: (sectionId: string) => void;
}

const DEFAULT_VALUE: CanvasContextValue = {
  sections: [],
  chartLookup: new Map(),
  imageLookup: new Map(),
  sourceLookup: new Map(),
  numberedSources: [],
  isEditMode: false,
  isStreaming: false,
  readOnly: false,
  canvasId: null,
  sessionId: null,
  currentTurnId: null,
  dispatchPatch: () => {
    // Implemented in P3.2.6; previously a no-op (warn to help detect missing wiring)
    // eslint-disable-next-line no-console
    console.warn("[CanvasContext] dispatchPatch called without provider; P3.2.6 待 wire");
  },
};

const CanvasContext = createContext<CanvasContextValue>(DEFAULT_VALUE);

/**
 * Derive chartLookup from sections.
 * Same logic as report-view.tsx (avoid duplicate maintenance): chart kind → map[id] = spec.
 */
function buildChartLookup(sections: ReportSection[]): Map<string, ChartSpec> {
  const map = new Map<string, ChartSpec>();
  for (const s of sections) {
    if (s.kind === "chart") {
      map.set(s.id, s.spec);
    }
  }
  return map;
}

/** feedback-0713 #3: image section mapping (image half of chart_section_id visual references). */
export function buildImageLookup(sections: ReportSection[]): Map<string, ImageSectionData> {
  const map = new Map<string, ImageSectionData>();
  for (const s of sections) {
    if (s.kind === "image") {
      map.set(s.id, s);
    }
  }
  return map;
}

function sourceRefFromSourcesEntry(
  entry: Record<string, unknown>,
  sourceIdx: number,
): { ref: SourceRef; aliases: string[] } | null {
  const explicitId = typeof entry.source_alias === "string"
    ? entry.source_alias
    : typeof entry.id === "string" ? entry.id : "";
  if (!isViewerSourceAlias(explicitId)) return null;
  // review F8: pass through six states (unknown still collapses to unavailable=unclickable, forward-safe).
  // linked_labeled is OWNED: label may be exposed (no byte exit; safe_query/preview remain omitted).
  const knownStates = ["linked_accessible", "linked_labeled", "linked_restricted", "linked_missing", "unsupported"] as const;
  const accessState: SourceRef["access_state"] = (knownStates as readonly string[]).includes(String(entry.access_state))
    ? (entry.access_state as SourceRef["access_state"])
    : "linked_unavailable";
  const ref: SourceRef = {
    id: explicitId,
    source_alias: explicitId,
    access_state: accessState,
    generic_type: typeof entry.generic_type === "string"
      ? entry.generic_type as SourceRef["generic_type"]
      : "other_source",
    ordinal: typeof entry.ordinal === "number" ? entry.ordinal : sourceIdx + 1,
  };
  if (accessState === "linked_labeled" && typeof entry.safe_label === "string") {
    ref.safe_label = entry.safe_label;  // OWNED: name present, no exit
  }
  if (accessState === "linked_accessible") {
    if (typeof entry.safe_label === "string") ref.safe_label = entry.safe_label;
    if (typeof entry.safe_query === "string") ref.safe_query = entry.safe_query;
    if (typeof entry.safe_preview === "string") ref.safe_preview = entry.safe_preview;
    if (Array.isArray(entry.safe_preview_columns)) ref.safe_preview_columns = entry.safe_preview_columns as string[];
    if (Array.isArray(entry.safe_preview_rows)) ref.safe_preview_rows = entry.safe_preview_rows as Record<string, string>[];
    if (typeof entry.safe_total_rows === "number") ref.safe_total_rows = entry.safe_total_rows;
    if (typeof entry.safe_url === "string") ref.safe_url = entry.safe_url;
    if (typeof entry.safe_preview_url === "string") ref.safe_preview_url = entry.safe_preview_url;
    copyKbFields(entry, ref);
  }
  return {
    ref,
    aliases: [explicitId, `idx_${sourceIdx}`],
  };
}

export interface CanvasContextProviderProps {
  sections: ReportSection[];
  canvasId?: string | null;
  /** content-permissions: persisted report session_id (artifact intermediary); live/non-report passes null. */
  sessionId?: string | null;
  /** Sprint ι: current turn_id (cross-turn popover safety, PRD §10.1). */
  currentTurnId?: string | null;
  isEditMode?: boolean;
  isStreaming?: boolean;
  readOnly?: boolean;
  dispatchPatch?: CanvasContextValue["dispatchPatch"];
  flushPendingEdits?: CanvasContextValue["flushPendingEdits"];
  registerPresentationOverride?: CanvasContextValue["registerPresentationOverride"];
  getSectionRev?: CanvasContextValue["getSectionRev"];
  acceptServerSection?: CanvasContextValue["acceptServerSection"];
  onDeleteSection?: CanvasContextValue["onDeleteSection"];
  onMergeWithPrev?: CanvasContextValue["onMergeWithPrev"];
  onMergeWithNext?: CanvasContextValue["onMergeWithNext"];
  onSplitAtCursor?: CanvasContextValue["onSplitAtCursor"];
  canSplitAtCursor?: CanvasContextValue["canSplitAtCursor"];
  onMoveSection?: CanvasContextValue["onMoveSection"];
  onEditBlockWithAI?: CanvasContextValue["onEditBlockWithAI"];
  /** P3.3: inject SourceRef metadata (from backend trace API / mock fixture). */
  sourceRefs?: SourceRef[];
  /** canvas-elements source-consolidation batch: folded archives use the **canvas-level** source table to resolve `[N]` (that section is outside archive groups). */
  citationSources?: Array<Record<string, unknown>>;
  children: React.ReactNode;
}

export function CanvasContextProvider({
  sections,
  canvasId = null,
  sessionId = null,
  currentTurnId = null,
  isEditMode = false,
  isStreaming = false,
  readOnly = false,
  dispatchPatch,
  flushPendingEdits,
  registerPresentationOverride,
  getSectionRev,
  acceptServerSection,
  onDeleteSection,
  onMergeWithPrev,
  onMergeWithNext,
  onSplitAtCursor,
  canSplitAtCursor,
  onMoveSection,
  onEditBlockWithAI,
  sourceRefs,
  citationSources,
  children,
}: CanvasContextProviderProps) {
  const chartLookup = useMemo(() => buildChartLookup(sections), [sections]);
  const imageLookup = useMemo(() => buildImageLookup(sections), [sections]);
  // Sprint agent-redesign-1 Phase F: numbered source entries.
  // Derives from `sections.find(kind='sources').entries[]` so `[N]` chip click
  // can lookup canvas-store sources by N-1 index. Falls back to empty list when
  // no sources section emitted (legacy turns).
  const numberedSources = useMemo(() => {
    for (const sec of sections) {
      if ((sec as { kind?: string }).kind !== "sources") continue;
      // Debt 1 (citation-audit M0-1): skip superseded (folded prior-report) sources —
      // popover/panel must resolve [N] against the CURRENT report's sources, the
      // same rule as report-view's `numberedSourcesLength` decoration gate.
      // Archived reports render in a nested ReportView with their own provider
      // (superseded stripped), so history [N]s still resolve within themselves.
      if ((sec as { superseded?: boolean }).superseded) continue;
      const entries = (sec as { entries?: unknown }).entries;
      if (Array.isArray(entries)) return entries as Array<Record<string, unknown>>;
    }
    // canvas-elements source-consolidation batch: the entire canvas scope has only one sources section, kept
    // in the **current** report. Nested folded archive views cannot see it; callers supply it via a prop.
    // ★ Must be a **fallback**, not preferred: existing pre-consolidation archives carry their own sources sections;
    // their [N] already resolves correctly within the group, so overriding with the current table turns correct references into wrong targets.
    return citationSources ?? [];
  }, [sections, citationSources]);
  const sourceLookup = useMemo(() => {
    const m = new Map<string, SourceRef>();
    for (const r of sourceRefs ?? []) {
      const alias = r.source_alias || r.id;
      if (!isViewerSourceAlias(alias) || !r.access_state) continue;
      m.set(alias, { ...r, id: alias, source_alias: alias });
    }
    numberedSources.forEach((entry, idx) => {
      const built = sourceRefFromSourcesEntry(entry, idx);
      if (!built) return;
      for (const alias of built.aliases) {
        if (!m.has(alias)) m.set(alias, built.ref);
      }
    });
    return m;
  }, [sourceRefs, numberedSources]);

  // Sprint ν1 Phase 3a — sourceLookup contract assertion (Eng r1 P0-3 close).
  // Detect narrative with [num:V:U:src:S] / [ref:S] markers but empty sourceLookup:
  // upstream ReportView etc. omitted sourceRefs prop, silently making every citation "来源已失效".
  // dev: console.warn; prod: POST /api/v1/metrics/event (existing metrics_ingest)
  // WP3 review (Agent4 P2-3) perf: previously every sections change (each streaming chunk)
  // JSON.stringify scanned all sections for markers. Now debounce 500ms: while streaming,
  // timer keeps resetting; scan once after sections stabilize (turn ends), avoiding per-chunk O(doc bytes).
  useEffect(() => {
    if (sourceLookup.size > 0) return;
    const timer = setTimeout(() => {
      let hasMarker = false;
      for (const sec of sections) {
        const text = JSON.stringify(sec);  // section is union; stringify whole obj
        if (/\[num:[^:]+:[^:]*:src:|\[ref:/.test(text)) {
          hasMarker = true;
          break;
        }
      }
      if (!hasMarker) return;
      const msg =
        "[ν1] sourceLookup empty but citation markers present — likely missing sourceRefs prop";
      if (typeof process !== "undefined" && process.env.NODE_ENV !== "production") {
        console.warn(msg);
      } else {
        // best-effort prod telemetry; metric must be in metrics_ingest._ALLOWED
        try {
          void fetch("/api/v1/metrics/event", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: "frontend_source_lookup_empty",
              labels: {},  // no turn label (cardinality), debug via audit log
            }),
          });
        } catch {
          // swallow — best-effort
        }
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [sourceLookup, sections, currentTurnId]);

  const value = useMemo<CanvasContextValue>(
    () => ({
      sections,
      chartLookup,
      imageLookup,
      sourceLookup,
      numberedSources,
      isEditMode,
      isStreaming,
      readOnly,
      canvasId,
      sessionId,
      currentTurnId,
      dispatchPatch: dispatchPatch ?? DEFAULT_VALUE.dispatchPatch,
      flushPendingEdits,
      registerPresentationOverride,
      getSectionRev,
      acceptServerSection,
      onDeleteSection,
      onMergeWithPrev,
      onMergeWithNext,
      onSplitAtCursor,
      canSplitAtCursor,
      onMoveSection,
      onEditBlockWithAI,
    }),
    [
      sections, chartLookup, imageLookup, sourceLookup, numberedSources, isEditMode, isStreaming, readOnly, canvasId, sessionId, currentTurnId, dispatchPatch,
      flushPendingEdits, registerPresentationOverride, getSectionRev, acceptServerSection, onDeleteSection, onMergeWithPrev, onMergeWithNext,
      onSplitAtCursor, canSplitAtCursor, onMoveSection, onEditBlockWithAI,
    ],
  );

  return <CanvasContext.Provider value={value}>{children}</CanvasContext.Provider>;
}

/**
 * Hook: access canvas state inside a NodeView.
 * Outside Provider, return defaults (no-op) without throwing, supporting spike scenarios.
 */
export function useCanvasContext(): CanvasContextValue {
  return useContext(CanvasContext);
}

/**
 * content-permissions: construct artifact-fetch URLs within sections.
 * Report context (nonempty CanvasContext.sessionId, a persisted status==="done" report) → permission intermediary
 * (owner∨grant authorization + closure allowlist), enabling share recipients to fetch images/poster backgrounds/raw HTML;
 * otherwise (live streaming / no provider) → owner-gated /api/v1/files/{id} fallback (behavior byte-for-byte unchanged).
 * Default (no provider / null sessionId) exactly matches historical strings, requiring no existing direct-render test changes.
 */
export function useArtifactHref(artifactId: string, opts?: { inline?: boolean }): string {
  const { sessionId } = useCanvasContext();
  const qs = opts?.inline ? "?disposition=inline" : "";
  if (sessionId) {
    return appPath(`/api/v1/canvas/by-session/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactId)}${qs}`);
  }
  return appPath(`/api/v1/files/${encodeURIComponent(artifactId)}${qs}`);
}
