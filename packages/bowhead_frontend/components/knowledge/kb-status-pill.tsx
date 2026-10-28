"use client";

/**
 * KbStatusPill — kb-structured-tables Sprint 2 Phase C surface 4 (v9 design).
 *
 * 6 states with v9 cream/terracotta palette. Renders inside kb-file-list row
 * and (in compact form) as a record's verify-status indicator.
 *
 * State machine for parse and structured extraction signals:
 *   parsing                  - file.parse_status != "done"  (gray + spinner)
 *   chunks_only              - parse done, no record, no auto col (available for search)
 *   classified_skipped       - auto_classified_doc_type_id set, no record    (gray dot)
 *   extracted_pending_review - record exists, verify_status=needs_review    (terracotta dot, clickable)
 *   extracted_ready          - record exists, verify_status=ready/approved   (green dot, clickable)
 *   parse_failed             - file parsing failed before searchable chunks  (red triangle)
 */

import { useLocale, useTranslations } from "next-intl";

import type { KbFileDTO } from "@/lib/kb-api";
import { formatShortDateTime } from "@/lib/locale";

import { IconChevR, IconRetry } from "./kb-icons";

export type ExtractionState =
  | "uploading"            // Knowledge-base experience cleanup §4: large-file direct upload incomplete.
  | "queued"               // Knowledge-base experience cleanup §4: queued (display separately from Processing).
  | "parsing"
  | "chunks_only"
  | "classified_skipped"
  | "extracted_pending_review"
  | "extracted_ready"
  | "extracting_rows"      // Sprint 25 NEW: cardinality=many per-row extract running
  | "extracting_failed"    // Sprint 25 NEW: per-row extract terminal failure
  | "parse_failed";

// label + tooltip resolved per-render via t() (knowledge_base.pill.* /
// knowledge_base.pill_tooltip.*). Colors live in styles/kb-management.css as
// `kb-pill <variant>` (design kb.css class names, knowledge-base experience cleanup §4 five states).
type PillVariant = "uploading" | "queued" | "proc" | "avail" | "failed" | "muted" | "review" | "extracting";

const STATE_CONFIG: Record<ExtractionState, {
  variant: PillVariant;
  kind: "spinner" | "dot" | "triangle";
  clickable: boolean;
  hasTooltip?: boolean;
  /** Five state names for shortest narrow-screen form (knowledge_base.file_state.*); extraction substates also count as Ready. */
  fileState: "uploading" | "queued" | "processing" | "ready" | "failed";
}> = {
  uploading: { variant: "uploading", kind: "spinner", clickable: false, fileState: "uploading" },
  queued: { variant: "queued", kind: "dot", clickable: false, fileState: "queued" },
  parsing: { variant: "proc", kind: "spinner", clickable: false, fileState: "processing" },
  chunks_only: { variant: "avail", kind: "dot", clickable: false, fileState: "ready" },
  classified_skipped: { variant: "muted", kind: "dot", clickable: false, fileState: "ready" },
  extracted_pending_review: { variant: "review", kind: "dot", clickable: true, fileState: "ready" },
  extracted_ready: { variant: "avail", kind: "dot", clickable: true, fileState: "ready" },
  // Sprint 25 NEW states (cardinality=many per-row extract)
  extracting_rows: { variant: "extracting", kind: "spinner", clickable: false, hasTooltip: true, fileState: "ready" },
  // Failed states: the pill only names the state; retry is a separate icon button after it (onRetry).
  extracting_failed: { variant: "failed", kind: "triangle", clickable: false, hasTooltip: true, fileState: "ready" },
  parse_failed: { variant: "failed", kind: "triangle", clickable: false, hasTooltip: true, fileState: "failed" },
};

const PARSING_STATUSES = new Set(["pending", "parsing", "chunking", "embedding"]);
export interface ExtractionStateInputs {
  file: KbFileDTO;
  hasRecord: boolean;
  recordVerifyStatus: "ready" | "needs_review" | "approved" | "extract_failed" | null;
  // Sprint 25 NEW: per-row extraction status (cardinality=many path).
  // From file.extracted_rows_status SQL column ('na'/'queued'/'running'/'succeeded'/'failed'/'cancelled').
  extractedRowsStatus?: string | null;
}

/** Derive the visible extraction state from observable file + record signals.
 *
 * Once parsing is done, base search is available. Classification and structured
 * extraction can update the pill later, but without a backend state we must not
 * invent a 30-second "analyzing" period that contradicts the upload result.
 */
export function getExtractionState(inp: ExtractionStateInputs): ExtractionState {
  const { file, hasRecord, recordVerifyStatus, extractedRowsStatus } = inp;
  if (PARSING_STATUSES.has(file.parse_status)) return "parsing";
  if (file.parse_status === "failed") return "parse_failed";
  // Sprint 25 P0-UX-3: cardinality=many extraction status takes priority over
  // hasRecord (which only counts kb_extracted_records, NOT row-level rows).
  const erStatus = extractedRowsStatus
    ?? (file as { extracted_rows_status?: string | null }).extracted_rows_status;
  if (erStatus === "failed") return "extracting_failed";
  if (erStatus === "running" || erStatus === "queued") return "extracting_rows";
  // succeeded → treat as extracted_ready (row records exist, chat SQL aggregate available)
  if (erStatus === "succeeded") return "extracted_ready";
  if (hasRecord) {
    return recordVerifyStatus === "needs_review"
      ? "extracted_pending_review"
      : "extracted_ready";
  }
  // Both Sprint 1 explicit and Sprint 2 auto col count as "classified, no record"
  const classified = Boolean(
    (file as { doc_type_id?: string | null }).doc_type_id
    || (file as { auto_classified_doc_type_id?: string | null }).auto_classified_doc_type_id,
  );
  if (classified) return "classified_skipped";
  return "chunks_only";
}

export interface KbStatusPillProps {
  state: ExtractionState;
  sourceHealth?: KbFileDTO["source_health"];
  /** href to navigate when clickable; ignored when state is non-clickable */
  href?: string;
  /** size variant — `sm` shrinks padding for dense rows */
  size?: "sm" | "md";
  /** Sprint 25: per-row job progress for extracting_rows state.
   * completed_batches / total_batches → percentage label.
   * rows_extracted optional row count for "1240/5000 行" UX.
   */
  jobProgress?: {
    completed_batches: number;
    total_batches: number;
    rows_extracted?: number;
  } | null;
  /** Failed states (parse_failed / extracting_failed): renders a retry icon button after the pill.
   * If undefined, no retry is offered. */
  onRetry?: () => void;
  /** File name for the retry button's accessible label ("Retry {name}"). */
  retryName?: string;
  /** Override default hover hint (e.g. specific parse-failure reason). */
  title?: string;
  /** Processing percentage (show when uploading/processing has progress). */
  pct?: number | null;
  /** Shortest narrow form: icon + percentage or five-state label only (design 06). */
  short?: boolean;
}

export function KbStatusPill({ state, href, size = "md", jobProgress, onRetry, retryName, title, pct, short = false, sourceHealth }: KbStatusPillProps) {
  const t = useTranslations("knowledge_base");
  const locale = useLocale();
  if (sourceHealth && sourceHealth.state !== "eligible" && !["uploading", "queued", "parsing", "parse_failed"].includes(state)) {
    return <span className={`kb-pill ${sourceHealth.state === "paused" ? "review" : "muted"}`} data-testid="kb-source-status" title={sourceHealth.checked_at ? t("health.checked_at", { time: formatShortDateTime(sourceHealth.checked_at, locale) }) : t("health.source_unchecked")}><span className="dot" aria-hidden />{t(`source_status.${sourceHealth.state}`)}</span>;
  }
  const c = STATE_CONFIG[state];
  const tooltip = title ?? (c.hasTooltip ? t(`pill_tooltip.${state}`) : undefined);
  const isFailed = state === "extracting_failed" || state === "parse_failed";
  const legacyStatus =
    state === "extracting_failed"
      ? "failed"
      : state === "parse_failed"
        ? "failed"
        : state === "uploading" || state === "queued" || state === "parsing" || state === "extracting_rows"
          ? "parsing"
          : "done";
  const isClickable = !short && c.clickable && Boolean(href);
  const showPct = (state === "uploading" || state === "parsing") && pct != null;
  let label = short ? t(`file_state.${c.fileState}`) : t(`pill.${state}`);
  // Sprint 25 dynamic label for extracting_rows (progress %)
  if (!short && state === "extracting_rows" && jobProgress && jobProgress.total_batches > 0) {
    const jobPct = Math.floor((jobProgress.completed_batches / jobProgress.total_batches) * 100);
    label = jobProgress.rows_extracted
      ? t("pill.extracting_progress_rows", { pct: jobPct, rows: jobProgress.rows_extracted })
      : t("pill.extracting_progress", { pct: jobPct });
  }
  const indicator =
    c.kind === "spinner" ? <span className="spinner" aria-hidden /> : c.kind === "triangle" ? <span className="tri" aria-hidden /> : <span className="dot" aria-hidden />;
  const inner = (
    <>
      {indicator}
      {short && showPct ? <span className="pct">{pct}%</span> : <span className="lbl">{label}</span>}
      {!short && showPct && <span className="pct">{pct}%</span>}
      {isClickable && <IconChevR size={10} />}
    </>
  );
  const className = `kb-pill ${c.variant}${size === "sm" ? " sm" : ""}${isClickable ? " clickable" : ""}`;
  // Failed states: the pill keeps only the state name (long "click to retry" copy overflowed the status
  // column in de-DE); retry is an adjacent icon button whose accessible name carries the file name.
  if (isFailed && !short && onRetry) {
    const retryLabel = t("pill.retry_file", { name: retryName ?? label });
    return (
      <span className="kb-pill-group">
        <span className={className} title={tooltip} data-state={state} data-status={legacyStatus}>{inner}</span>
        <button
          type="button"
          className="kb-iconbtn kb-pill-retry"
          onClick={onRetry}
          aria-label={retryLabel}
          title={retryLabel}
          data-testid="kb-pill-retry"
          data-state={state}
        >
          <IconRetry />
        </button>
      </span>
    );
  }
  if (isClickable && href) {
    return (
      <a href={href} className={className} title={tooltip ?? t("pill_tooltip.go_records")} data-state={state} data-status={legacyStatus}>
        {inner}
      </a>
    );
  }
  return <span className={className} title={tooltip} data-state={state} data-status={legacyStatus}>{inner}</span>;
}
