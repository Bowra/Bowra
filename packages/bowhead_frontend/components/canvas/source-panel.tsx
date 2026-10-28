"use client";

/**
 * SourcePanel — sentence-level citation source viewer (sprint γ V1).
 *
 * Triggered by clicking a citation pill (data-citation-kind="num"). Replaces
 * the artifact-level "open Data tab" jump with an inline panel showing the
 * source artifact text + sentence-level highlight.
 *
 * V1 layout (responsive, CSS-only — no vaul / radix-dialog dep):
 *  - Desktop (≥ 768px): right-slide panel, 480px wide
 *  - Mobile (< 768px): bottom-sheet, 75vh, slide-up
 *
 * Per PRD §11:
 *  - F22 fallback: artifact lookup fails → show "暂无来源数据" + caveat
 *  - Panel ↔ popover mutually exclusive: opening panel suppresses popover
 *  - HighlightedContent algorithm: exact → normalized → fallback box
 *  - ESC + click-outside + X button all close
 */

import { useTranslations } from "next-intl";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useCanvasContext } from "@/lib/canvas-pm-adapter/canvas-context";
import type { SourceRef } from "@/lib/canvas-pm-adapter/citation-types";
import { appPath } from "@/lib/base-path";
import { useSourceAccess, useSourceAccessActions } from "@/lib/source-access-registry";
import type { FilePreview } from "@/lib/api";
import { buildSourceTable, codeRunForSource, sourceTextBlocks, type SourceBlock } from "@/lib/source-content";
import type { ChartSpec } from "@/lib/chart/types";
import type { ReportSection } from "@/lib/types";
import { SourceContent, SourceRunCode, SourceRunMeta } from "@/components/canvas/source-content";
import {
  filesFocusHref,
  requestCanvasDataFocus,
  requestCanvasResultOpen,
} from "@/lib/canvas-workspace-focus";
import { useResultsWorkspaceCapabilitySnapshot } from "@/lib/use-results-workspace-capability";
import { Dialog } from "@/components/ui/dialog";
import { CharIconText } from "@/components/char-icon";

// ─── Types ─────────────────────────────────────────────────


export type SourceQuality = "linked_accessible" | "artifact_only" | "unavailable" | "never_linked";

export interface SourcePanelCitation {
  sourceId?: string;
  sourceRef?: SourceRef;
  sourceQuality?: SourceQuality;
  sourceIndex?: number;
  value: string;
  unit: string;
  display: string;
  highlight: string;     // empty string when artifact-level only
}

interface SourcePanelState {
  citation: SourcePanelCitation | null;
  returnFocus: HTMLElement | null;
}

interface SourcePanelContextValue {
  state: SourcePanelState;
  open: (c: SourcePanelCitation, trigger?: HTMLElement | null) => void;
  close: () => void;
}

const SourcePanelContext = createContext<SourcePanelContextValue>({
  state: { citation: null, returnFocus: null },
  open: () => {},
  close: () => {},
});


// ─── Provider ──────────────────────────────────────────────


export function SourcePanelProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<SourcePanelState>({ citation: null, returnFocus: null });
  // Source details use the shared Dialog drawer; DialogShell handles Esc, backdrop clicks and focus return.

  const open = useCallback((c: SourcePanelCitation, trigger?: HTMLElement | null) => {
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setState({ citation: c, returnFocus: trigger ?? active });
  }, []);
  const close = useCallback(() => {
    setState((current) => {
      const trigger = current.returnFocus;
      if (trigger?.isConnected) requestAnimationFrame(() => trigger.focus());
      return { citation: null, returnFocus: null };
    });
  }, []);

  const value = useMemo(() => ({ state, open, close }), [state, open, close]);
  return (
    <SourcePanelContext.Provider value={value}>
      {children}
    </SourcePanelContext.Provider>
  );
}

export function useSourcePanel(): SourcePanelContextValue {
  return useContext(SourcePanelContext);
}


// ─── Panel ─────────────────────────────────────────────────


export function SourcePanel() {
  const t = useTranslations("source_panel");
  const tSources = useTranslations("sources");
  const { state, close } = useSourcePanel();
  const { sourceLookup } = useCanvasContext();
  const c = state.citation;
  const ref = c?.sourceRef ?? (c?.sourceId ? sourceLookup.get(c.sourceId) : undefined);
  const access = useSourceAccess(ref);
  const { check } = useSourceAccessActions();
  const accessStatus = access?.generation === 0 ? "checking" : access?.status;

  useEffect(() => {
    if (!c || !ref) return;
    void check(ref);
  }, [c, ref, check]);

  if (!c) return null;

  const accessibleRecord = accessStatus === "linked_accessible" && access ? access : null;
  const sourceTitle = accessibleRecord
    ? srcLabel(accessibleRecord.source)
    : t("generic_title", { index: c.sourceIndex ?? 1 });

  const body = accessStatus === "checking" ? (
    <div className="legacy-source-panel-note">{t("checking_access")}</div>
  ) : accessibleRecord ? (
    <>
      <SourceArtifactBody
        ref_={accessibleRecord.source}
        highlight={c.highlight}
        sourceQuality={c.sourceQuality ?? "linked_accessible"}
        preview={accessibleRecord.preview}
      />
      <SourcePanelActions ref_={accessibleRecord.source} />
    </>
  ) : (
    <SourceLookupFailed sourceQuality={c.sourceQuality ?? "unavailable"} />
  );

  // Report Canvas Chrome Spec §6: source details use the shared Dialog drawer — mounted on body, layered by tokens,
  // width min(420, 100vw); narrow screens (< 640) use the bottom sheet. Esc/backdrop/close button close; focus returns
  // to the opening citation marker.
  return (
    <Dialog
      variant="drawer"
      width={420}
      title={sourceTitle}
      closeLabel={t("close")}
      onClose={close}
      returnFocusRef={{ current: state.returnFocus }}
    >
      <div className="source-panel-drawer" data-testid="source-panel-drawer">
        <div className="legacy-source-panel-eyebrow">
          {tSources("head")} · {c.display}
        </div>
        {body}
      </div>
    </Dialog>
  );
}

function SourcePanelActions({ ref_ }: { ref_: _Ref }) {
  const t = useTranslations("source_panel");
  const resultsWorkspaceEnabled = useResultsWorkspaceCapabilitySnapshot();
  const artifactId = ref_.artifact_id ?? "";
  const fileId = ref_.kb_file_id ?? "";
  const codeAvailable = Boolean(ref_.tool_call_id);
  if (
    !ref_.safe_url
    && !artifactId
    && !fileId
    && !(resultsWorkspaceEnabled && codeAvailable)
  ) {
    return null;
  }
  return (
    <div className="legacy-source-panel-actions">
      {artifactId ? (
        <button
          type="button"
          onClick={() => requestCanvasDataFocus(artifactId, {
            entryPoint: "source_panel",
          })}
          className="legacy-source-panel-action"
        >
          {resultsWorkspaceEnabled ? t("open_in_results") : t("open_in_data")}
        </button>
      ) : null}
      {!artifactId && resultsWorkspaceEnabled && codeAvailable ? (
        <button
          type="button"
          onClick={() => requestCanvasResultOpen(
            { type: "code" },
            { entryPoint: "source_panel" },
          )}
          className="legacy-source-panel-action"
        >
          {t("open_code")}
        </button>
      ) : null}
      {fileId ? (
        <a href={filesFocusHref(fileId)} target="_blank" rel="noreferrer" className="legacy-source-panel-action">
          {t("open_in_files")}
        </a>
      ) : null}
      {ref_.safe_url ? (
        <a
          href={appPath(ref_.safe_url)}
          target="_blank"
          rel="noreferrer"
          className="legacy-source-panel-action"
        >
          {t("download")}
        </a>
      ) : null}
    </div>
  );
}

// ─── Artifact body (with highlight) ────────────────────────


type _Ref = SourceRef;


/** Pick a human-readable label from SourceRef (V1 simple). */
function srcLabel(ref: _Ref): string {
  return ref.safe_label || ref.safe_query || `Source ${ref.ordinal ?? ""}`.trim();
}


function SourceArtifactBody({
  ref_,
  highlight,
  sourceQuality,
  preview,
}: {
  ref_: _Ref;
  highlight: string;
  sourceQuality: SourceQuality;
  preview: import("@/lib/api").FilePreview | null;
}) {
  const t = useTranslations("source_panel");
  const { sections } = useCanvasContext();
  const inlinePreviewText = ref_.safe_preview ?? "";
  const rowsPreviewText = inlinePreviewText ? "" : previewTextFromRows(ref_);
  const query = ref_.safe_query ?? "";
  const lazyPreviewText = previewTextFromFilePreview(preview);
  const previewText = inlinePreviewText || rowsPreviewText || lazyPreviewText;
  // Render the source in its most structured form (rows → table, Markdown → safe Markdown,
  // anything else → preformatted text that keeps its line breaks). Sandbox stdout used to go
  // through Markdown, which folds single line breaks and flattened printed DataFrames.
  const blocks = useMemo(() => sourceBlocksFor(ref_, preview), [ref_, preview]);
  const charts = useMemo(() => chartsBuiltFromSource(sections, ref_.id), [sections, ref_.id]);
  // Run Code view: when the source is a run's output (linked by viewer alias), show the run's
  // label and time above its output and the code below it. Old canvases have no link: output only.
  const run = useMemo(() => codeRunForSource(sections, ref_.source_alias ?? ref_.id), [sections, ref_.source_alias, ref_.id]);
  const imageHref = preview?.family === "image" && ref_.safe_url
    ? `${appPath(ref_.safe_url)}${ref_.safe_url.includes("?") ? "&" : "?"}disposition=inline`
    : null;

  return (
    <div>
      {query && (
        <div className="legacy-source-panel-query">
          {query && (
            <div className="legacy-source-panel-query-line">
              {t("query_label")}：<em>{query.length > 120 ? query.slice(0, 120) + "…" : query}</em>
            </div>
          )}
        </div>
      )}

      {highlight ? (
        <HighlightedBlock content={previewText} highlight={highlight} />
      ) : (
        <ArtifactLevelHint sourceQuality={sourceQuality} />
      )}

      {/* The image family note is an API hint ("use <img> to render"), not reader copy. */}
      {preview?.note && preview.family !== "image" ? (
        <div className="legacy-source-panel-note">{preview.note}</div>
      ) : null}

      {imageHref && !highlight ? (
        <div className="source-panel-block" data-testid="source-panel-image">
          {/* eslint-disable-next-line @next/next/no-img-element -- authenticated same-origin artifact stream */}
          <img className="source-panel-image" src={imageHref} alt={ref_.safe_label ?? ""} loading="lazy" />
        </div>
      ) : null}
      {run && !highlight ? <SourceRunMeta run={run} /> : null}
      {!highlight ? (
        <SourceContent blocks={blocks} truncated={Boolean(preview?.truncated)} charts={charts} />
      ) : null}
      {run ? <SourceRunCode run={run} /> : null}
    </div>
  );
}

/** Structured rows win over text; text is classified once per payload. */
export function sourceBlocksFor(ref: _Ref, preview: FilePreview | null): SourceBlock[] {
  const carriedRows = ref.safe_preview_rows;
  if (carriedRows && carriedRows.length > 0) {
    const columns = ref.safe_preview_columns?.length ? ref.safe_preview_columns : Object.keys(carriedRows[0] ?? {});
    return [{ kind: "table", table: buildSourceTable(columns, carriedRows, ref.safe_total_rows) }];
  }
  if (ref.safe_preview) return sourceTextBlocks(ref.safe_preview);
  if (!preview || preview.error) return [];
  if (preview.rows && preview.rows.length > 0) {
    const columns = preview.columns?.length ? preview.columns : Object.keys(preview.rows[0] ?? {});
    return [{ kind: "table", table: buildSourceTable(columns, preview.rows, preview.total_rows) }];
  }
  return preview.text ? sourceTextBlocks(preview.text) : [];
}

/** Report charts drawn from this exact source (same viewer alias on the chart's source row). */
function chartsBuiltFromSource(sections: ReportSection[], alias: string): Array<{ id: string; spec: ChartSpec }> {
  if (!alias) return [];
  const out: Array<{ id: string; spec: ChartSpec }> = [];
  for (const section of sections) {
    if (section.kind !== "chart") continue;
    const meta = section.spec?.source_meta;
    const metaAlias = meta?.source_alias ?? meta?.id;
    if (metaAlias === alias) out.push({ id: section.id, spec: section.spec });
  }
  return out;
}

/** Tab-joined table text from the SourceRef's inline preview rows (mirrors
 *  previewTextFromFilePreview so both sources render identically). */
// Batch C (P2-4): 23 columns × full-precision floats are unreadable in 480px panel (confirmed inspection). Limit columns + format numbers;
// both preview builders share the same conventions.
const MAX_PREVIEW_COLUMNS = 8;

function fmtPreviewCell(value: unknown): string {
  const s = String(value ?? "");
  const trimmed = s.trim();
  if (trimmed !== "" && /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(trimmed)) {
    const n = Number(trimmed);
    if (Number.isFinite(n)) {
      if (Number.isInteger(n)) {
        // review F-C2: four-digit integers often represent years/IDs (2026 → "2,026" is incorrect); only ≥5 digits clearly
        // represent magnitudes requiring thousands separators.
        return Math.abs(n) >= 10000 ? n.toLocaleString() : trimmed;
      }
      // |v|≥1 → 2 decimal places (amount/headcount); |v|<1 → 6 significant decimal places (ratios/growth).
      return n.toLocaleString(undefined, {
        maximumFractionDigits: Math.abs(n) >= 1 ? 2 : 6,
      });
    }
  }
  return s;
}

function buildPreviewTable(columns: string[], rows: Array<Record<string, unknown>>): string {
  const shown = columns.slice(0, MAX_PREVIEW_COLUMNS);
  const extra = columns.length - shown.length;
  const head = shown.join("\t") + (extra > 0 ? `\t…(+${extra})` : "");
  const body = rows
    .slice(0, 20)
    .map((row) => shown.map((col) => fmtPreviewCell(row[col])).join("\t"))
    .join("\n");
  return [head, body].filter(Boolean).join("\n");
}

function previewTextFromRows(ref: _Ref): string {
  const rows = ref.safe_preview_rows;
  if (!rows || rows.length === 0) return "";
  const columns =
    ref.safe_preview_columns && ref.safe_preview_columns.length > 0
      ? ref.safe_preview_columns
      : Object.keys(rows[0] ?? {});
  return buildPreviewTable(columns, rows as Array<Record<string, unknown>>);
}

function previewTextFromFilePreview(preview: FilePreview | null): string {
  if (!preview || preview.error) return "";
  if (preview.text) return preview.text;
  if (preview.rows && preview.rows.length > 0) {
    const columns = preview.columns ?? Object.keys(preview.rows[0] ?? {});
    return buildPreviewTable(columns, preview.rows as Array<Record<string, unknown>>);
  }
  return "";
}

function ArtifactLevelHint({ sourceQuality }: { sourceQuality: SourceQuality }) {
  const t = useTranslations("source_panel");
  const copy =
    sourceQuality === "artifact_only"
      ? t("artifact_level_summary")
      : t("artifact_level_no_highlight");
  return (
    <div className="legacy-source-panel-hint">
      {copy}
    </div>
  );
}


// ─── HighlightedBlock — find + bracket the highlight in content ────


export function HighlightedBlock({
  content,
  highlight,
}: {
  content: string;
  highlight: string;
}) {
  const t = useTranslations("source_panel");
  if (!content) {
    return (
      <div className="legacy-source-panel-quote">
        <strong>{t("quoted_sentence")}</strong>: <em>{highlight}</em>
        <div className="legacy-source-panel-quote-note">
          {t("artifact_body_not_loaded")}
        </div>
      </div>
    );
  }

  const found = findHighlightSpan(content, highlight);
  if (!found) {
    return (
      <>
        <div className="legacy-source-panel-unmatched">
          <CharIconText ch="⚠">{t("highlight_not_matched")}</CharIconText>
          <br />
          <em>{highlight}</em>
        </div>
        <pre className="legacy-source-panel-code">{content.slice(0, 2000)}</pre>
      </>
    );
  }

  const [start, end] = found;
  return (
    <pre className="legacy-source-panel-code">
      {content.slice(0, start)}
      <mark
        className="legacy-source-panel-mark"
        ref={(el) => {
          // Auto-scroll highlight into center viewport on mount
          if (el && typeof el.scrollIntoView === "function") {
            requestAnimationFrame(() => {
              el.scrollIntoView({ behavior: "smooth", block: "center" });
            });
          }
        }}
      >
        {content.slice(start, end)}
      </mark>
      {content.slice(end)}
    </pre>
  );
}


/**
 * Find highlight span in content (HighlightedContent algorithm per IntelliVault):
 *  1. Try exact substring match
 *  2. Try whitespace-normalized match
 *  3. None → return null (caller shows fallback)
 */
export function findHighlightSpan(
  content: string,
  highlight: string,
): [number, number] | null {
  if (!content || !highlight) return null;

  // 1. Exact match
  const exact = content.indexOf(highlight);
  if (exact !== -1) return [exact, exact + highlight.length];

  // 2. Normalize whitespace (collapse runs)
  const normWs = (s: string) => s.replace(/\s+/g, " ").trim();
  const normContent = normWs(content);
  const normHighlight = normWs(highlight);
  const normIdx = normContent.indexOf(normHighlight);
  if (normIdx !== -1) {
    // Map back to original content offset (approx)
    // For V1 simplicity: scan original to find first non-whitespace
    // matching prefix of highlight. Acceptable since whitespace-normalized
    // matches are rare.
    return findApproxSpan(content, highlight);
  }

  return null;
}


function findApproxSpan(content: string, highlight: string): [number, number] | null {
  // Strip whitespace from highlight, scan content for matching char run
  const stripped = highlight.replace(/\s+/g, "");
  if (!stripped) return null;
  let i = 0;
  let j = 0;
  let start = -1;
  while (i < content.length && j < stripped.length) {
    const c = content[i];
    if (/\s/.test(c)) {
      i++;
      continue;
    }
    if (c === stripped[j]) {
      if (start === -1) start = i;
      j++;
      i++;
    } else {
      // Reset
      if (start !== -1) {
        i = start + 1;
        start = -1;
      } else {
        i++;
      }
      j = 0;
    }
  }
  if (j === stripped.length && start !== -1) {
    return [start, i];
  }
  return null;
}


// ─── F22 fallback (artifact lookup failed) ─────────────────


function SourceLookupFailed({ sourceQuality }: { sourceQuality: SourceQuality }) {
  const t = useTranslations("source_panel");
  const tProv = useTranslations("provenance");
  const isLegacy = sourceQuality === "never_linked";
  return (
    <div className="legacy-source-panel-failed">
      <div className="legacy-source-panel-failed-title">
        {isLegacy ? tProv("no_source") : t("no_source_data")}
      </div>
      <div className="legacy-source-panel-failed-hint">
        {isLegacy ? t("legacy_marker_hint") : t("unavailable_hint")}
      </div>
    </div>
  );
}
