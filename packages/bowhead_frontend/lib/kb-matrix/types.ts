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
  /** Position of the quoted chunk; shown as the paragraph when there is no page (step 1 S7). */
  chunkIndex?: number;
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
  // ── Document matrix step 1 (S4) ──
  /** The cell's latest change (server `last_event`); absent for cells without history and unreadable rows. */
  lastEvent?: MatrixCellLastEvent;
}

/** Kinds of cell history (`kb_matrix_cell_events.kind`). The last three are AI writes and name no person. */
export type MatrixCellEventKind =
  | "edited"
  | "kept_as_text"
  | "edit_reverted"
  | "verified"
  | "verify_reverted"
  | "ai_value"
  | "edit_superseded"
  | "auto_retried";

export const AI_CELL_EVENT_KINDS: ReadonlySet<MatrixCellEventKind> = new Set(["ai_value", "edit_superseded", "auto_retried"]);

export interface MatrixCellLastEvent {
  kind: MatrixCellEventKind;
  /** Null for AI writes and for people whose name cannot be resolved (for example after account deletion). */
  actorName: string | null;
  /** ISO time. */
  at: string;
}

/** One entry of a cell's history (GET …/cells/{file}/{col}/events, newest first). */
export interface MatrixCellEvent extends MatrixCellLastEvent {
  eventId: string;
  runId: string | null;
  /** Shared by the cells of one bulk verify. */
  batchId: string | null;
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
  // ── Document matrix step 1 (S3) ──
  /** A run limited to a few rows (trial); the banner says so. */
  trial?: boolean;
  /** Rows the trial covers. */
  trialFiles?: number;
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
  navigationRole?: "none" | "description" | "topics";
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

/** Who can see a matrix (step 1 S8): every member of its Space, or only its creator. */
export type MatrixVisibility = "space" | "private";

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
  /** Server-authoritative: creator or Space/Team admin (deletes the matrix, removes rows with edited or verified cells). */
  canManage?: boolean;
  /** Step 1 S8: who can see the matrix ("private": its creator only; may hold the creator's personal files). */
  visibility?: MatrixVisibility;
  /** Server-authoritative: the caller created the matrix and only-me matrices are enabled. */
  canChangeVisibility?: boolean;
  /** Structure version: server returns full snapshot when since_seq < structureSeq. */
  structureSeq?: number;
  /** Step 1 S6: digest of everything in this projection a delta cannot carry; polls send it back with seq. */
  projectionDigest?: string;
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

/** What removing these rows deletes (S2 confirmation): cells with a row, and how many are edited or verified. */
export function rowRemovalCounts(rows: MatrixRowVM[]): { cells: number; edited: number; verified: number } {
  let cells = 0;
  let edited = 0;
  let verified = 0;
  for (const row of rows) {
    for (const cell of Object.values(row.cells)) {
      if (!cell) continue;
      cells++;
      if (cell.edited) edited++;
      if (cell.verified) verified++;
    }
  }
  return { cells, edited, verified };
}

/** Rows a trial runs first (server `TRIAL_FILE_COUNT`). */
export const TRIAL_FILE_COUNT = 3;

/** Run menu counts (display only; the estimate is authoritative). */
export interface RunMenuCounts {
  /** all_pending: empty cells and failed cells without an edit. */
  pending: number;
  /** Stale cells without an edit. */
  stale: number;
  /** Failed cells without an edit. */
  failed: number;
  /** Rows a trial would run: the first TRIAL_FILE_COUNT rows with pending cells. */
  trialFiles: number;
  /** Rerun all keeps these: verified cells that hold a value, and edited cells. */
  keptVerified: number;
  keptEdited: number;
}

/** Mirrors the server's scope selection (engine `select_targets`) over the snapshot. */
export function runMenuCounts(rows: MatrixRowVM[], columns: MatrixColumnVM[]): RunMenuCounts {
  const counts: RunMenuCounts = { pending: 0, stale: 0, failed: 0, trialFiles: 0, keptVerified: 0, keptEdited: 0 };
  let pendingRows = 0;
  for (const row of rows) {
    let rowPending = false;
    for (const col of columns) {
      const cell = row.cells[col.columnId];
      if (!cell) {
        counts.pending++;
        rowPending = true;
        continue;
      }
      if (cell.edited) {
        counts.keptEdited++;
        continue;
      }
      if (cell.status === "failed") {
        counts.pending++;
        counts.failed++;
        rowPending = true;
      } else if (cell.verified && cell.status !== "queued" && cell.status !== "running") {
        counts.keptVerified++;
      }
      if (cell.stale) counts.stale++;
    }
    if (rowPending) pendingRows++;
  }
  counts.trialFiles = Math.min(TRIAL_FILE_COUNT, pendingRows);
  return counts;
}

/** Filter-chip count: all failed cells in matrix (“{n} 格”). */
export function failedCellCount(rows: MatrixRowVM[]): number {
  return rows.reduce(
    (acc, r) => acc + Object.values(r.cells).filter((c) => c?.status === "failed").length,
    0,
  );
}
