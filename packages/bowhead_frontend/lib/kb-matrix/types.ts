/**
 * kb-matrix M1 — MatrixSnapshot view-model.
 *
 * This shape is the seam between M1 (FE-assembled from doc_types + records +
 * files, tech-design-m1 §3) and M2 (server snapshot endpoint returns the same
 * shape with readOnly=false and a monotonically increasing seq). Grid/panel
 * components consume ONLY these types — swapping the data source in M2 must
 * not touch them.
 */

import type { Variant } from "@/lib/kb-knowledge-tables-api";

/**
 * M2 full set. NOTE: no "empty" literal — empty = cells[columnId] === undefined
 * (M1 matches backend §4.2 no-row-means-empty contract; adding union value creates two empty representations).
 */
export type MatrixCellStatus = "queued" | "running" | "done" | "failed" | "needs_review";

export interface MatrixCellVM {
  status: MatrixCellStatus;
  /** Display value (primary variant, V1-style normalized). */
  value: string;
  /** Full variant array for the multi-source cards (passed through as-is). */
  variants: Variant[];
  sourceExcerpt?: string;
  chunkId?: string;
  page?: number;
  confidence?: number;
  extractedAt?: string;
  /** record.edited_by_user includes this field name → AI never overwrites. */
  edited: boolean;
  /** M1: row-level verify_status="approved" mapped onto every done cell (decision 2). */
  verified: boolean;
  // ── M2 additions (snapshot endpoint supplies, M1 template path omits) ──
  /** Server-derived (decision 3): compare both column prompt_version and file version. */
  stale?: boolean;
  /** Value from cache hit (no credits charged). */
  cached?: boolean;
  generationId?: string;
  sourceCheckState?: string;
  currentSourceState?: string;
  /** Sanitized error category for failed state (fail_reason enum, no raw LLM text). */
  failReason?: string;
  /** Cell change sequence (delta protocol). */
  seq?: number;
  /** 480ms pulse marker for running/queued→done (store applyDelta stamps, FR-24). */
  flash?: boolean;
  // ── M3 additive ──
  /** Primary-variant cross-verification agreement (absent = disabled, side panel omits row). */
  selfConsistencyAgree?: number;
}

/** M2 run state (snapshot run field; null = never run). */
export interface MatrixRunVM {
  runId: string;
  status: "running" | "paused" | "succeeded" | "failed" | "cancelling" | "cancelled";
  cellsTotal: number;
  cellsDone: number;
  cellsFailed: number;
  skippedEdited: number;
  errorCode?: string | null;
  etaS: number;
  initiatedBy: string;
  // ── M3 additions (multiple-run banner + server-authoritative cancellation permission) ──
  /** Run scope kind (all_pending/column/force_all/force_column/stale/stale_column/single_cell). */
  scopeKind?: string;
  /** Target column for column-family scopes (banner scope label resolves column name from this). */
  scopeColumnId?: string;
  /** Server authoritative (FR-34: members cancel own runs only; FE no shadow-role decisions). */
  canCancel?: boolean;
}

export interface MatrixRowVM {
  fileId: string;
  fileName: string;
  /** File absent from the caller-visible file listing (deleted OR no grant). */
  missing: boolean;
  /** verify_status="extract_failed" → fields={} → whole row renders "—". */
  extractFailed: boolean;
  /** M1 only: backing record id for PATCH field / verify_status. */
  recordId: string;
  /** M1 only: row-level verify status backing the verified mapping. */
  approved: boolean;
  /** Keyed by column_id; undefined → empty "—" cell (missing field / failed row). */
  cells: Record<string, MatrixCellVM | undefined>;
}

export interface MatrixColumnVM {
  columnId: string;
  name: string;
  /** Storage token: string/number/currency/date (+question from M2). */
  type: string;
  question?: string;
  /** M2 addition: extraction hint (echoed in column-editor panel). */
  extractionHint?: string;
  /** M2 addition: column prompt version (stale derivation input, not displayed directly). */
  promptVersion?: number;
  // ── M3 additive ──
  /** select/multi_select options (wave-1 passthrough only; control/chip rendering awaits mockup, §8). */
  options?: string[] | null;
  /** Cross-verification flag (FR-39; question columns server 422). */
  crossVerify?: boolean;
}

export interface MatrixSnapshot {
  matrixId: string; // M1: `tpl_${doc_type_id}` synthetic id; M2: real mx_*
  title: string;
  readOnly: boolean; // M1 template views: no run/add-column (edits still allowed)
  columns: MatrixColumnVM[];
  rows: MatrixRowVM[];
  seq: number; // M1: always 0; M2: monotonic snapshot sequence
  // ── M2 additive ──
  /** Server-authoritative can_edit (rank≥member; e2e-plan P2-1 implemented, FE shadow decision retired). */
  canEdit?: boolean;
  /** Structure version: server returns full snapshot when since_seq < structureSeq. */
  structureSeq?: number;
  /** Poll interval (server supplies BOWHEAD_KB_MATRIX_POLL_MS, FR-22). */
  pollMs?: number;
  /**
   * @deprecated Since M3 read runs (this field mirrors server runs[0] for image rollback window).
   */
  run?: MatrixRunVM | null;
  /** M3: active + recent terminal runs (server five-entry window; store flush also overlays locally sticky terminal states). */
  runs?: MatrixRunVM[];
}

/** List row for “模板记录” grouping on matrix list page. */
export interface TemplateViewSummary {
  viewId: string; // tpl_<doc_type_id>
  docTypeId: string;
  name: string;
  columnCount: number;
  recordCount: number;
  lastExtractedAt: string | null;
}

export function cellKey(fileId: string, columnId: string): string {
  return `${fileId}:${columnId}`;
}

/** Defensive read (M3 §4.5): image rollback window with old backend returning run only → single-element array. */
export function snapshotRuns(snap: MatrixSnapshot): MatrixRunVM[] {
  return snap.runs ?? (snap.run ? [snap.run] : []);
}

/** kb-matrix M3 wave-2: multi_select cell option array. Primary variant's
 * normalized.choices authoritative; absent variant (compact patch/delta shape) splits display value on
 * “、” join delimiter (inverse of variantDisplayValue, pure function for chip rendering only). */
export function cellChoiceList(cell: MatrixCellVM): string[] {
  const primary = cell.variants.find((v) => v.is_primary) ?? cell.variants[0];
  const n = primary?.normalized;
  if (n && typeof n === "object") {
    const choices = (n as { choices?: unknown }).choices;
    if (Array.isArray(choices)) return choices.map((x) => String(x));
  }
  return cell.value ? cell.value.split("、") : [];
}

/** kb-matrix M3 wave-2 “查看失败” filtered view (§4.4): rows containing ≥1 failed cell only,
 * columns untrimmed, original matrix row order preserved (pure function shared by filtered view/empty-state detection). */
export function rowsWithFailedCell(rows: MatrixRowVM[]): MatrixRowVM[] {
  return rows.filter((r) => Object.values(r.cells).some((c) => c?.status === "failed"));
}

/** Filter-chip count: all failed cells in matrix (“{n} 格”). */
export function failedCellCount(rows: MatrixRowVM[]): number {
  return rows.reduce(
    (acc, r) => acc + Object.values(r.cells).filter((c) => c?.status === "failed").length,
    0,
  );
}
