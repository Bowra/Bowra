import { appFetch as fetch } from "@/lib/base-path";

/**
 * kb-matrix M2 — matrix CRUD / run / cell mutation API client
 * (`/api/v1/kb/matrices/*`，gateway routes/kb_matrix.py).
 */

import { spaceHeader } from "@/lib/space-storage";

import { MatrixApiError, matrixJsonOrThrow as orThrow } from "./api-error";
import { type WireColumn, buildColumnFromWire, buildLastEventFromWire } from "./snapshot-api";
import type { MatrixCellEvent, MatrixColumnVM, MatrixVisibility } from "./types";

// Reexport (M3: types moved to shared api-error.ts; existing import sites stay unchanged).
export { MatrixApiError };

// ─── DTOs ───────────────────────────────────────────────────

export interface RunScope {
  kind: "all_pending" | "failed" | "column" | "force_all" | "force_column" | "stale" | "stale_column" | "single_cell";
  column_id?: string;
  /** single_cell only. */
  file_id?: string;
  /** Trial or selection: at most `MAX_RUN_FILE_IDS` rows of this matrix. */
  file_ids?: string[];
  /** Force scopes keep verified cells unless this is false (the server default is true). */
  keep_verified?: boolean;
}

/** Rows a trial or selection may name (server `MAX_SCOPE_FILE_IDS`). */
export const MAX_RUN_FILE_IDS = 50;

/** One column's share of an estimate; a cross-checked column also carries its extra reading of each document. */
export interface RunEstimateColumn {
  column_id: string;
  cells: number;
  credits: number;
  cross_verify: boolean;
}

export interface RunEstimate {
  cells_pending: number;
  calls: number;
  credits_estimated: number;
  eta_s: number;
  skipped_edited: number;
  available_credits: number;
  team_pool_tighter: boolean;
  warn: boolean;
  blocked: boolean;
  // ── Document matrix step 1 (S3) breakdown; absent from older backends ──
  /** Files with cells to run. */
  files?: number;
  /** Shared cost of reading the documents; reading_credits + Σ columns[].credits = credits_estimated. */
  reading_credits?: number;
  columns?: RunEstimateColumn[];
  /** Verified cells a force scope keeps. */
  skipped_verified?: number;
  /** The longest file with cells to run and its share of the estimate. */
  longest_file?: { file_id: string; chars: number; credits: number } | null;
  /** First rows with cells to run in this scope: send them back as `file_ids` for a trial. */
  trial_file_ids?: string[];
}

export interface RunStarted {
  run_id: string;
  cells_total: number;
  skipped_edited: number;
  credits_estimated: number;
}

export interface ColumnDraft {
  name: string;
  type: string;
  extraction_hint: string;
  /** M3: AI-suggested options (wave-1 receives but does not consume; options editor awaits mockup, §8). */
  options: string[] | null;
  fallback: boolean;
}

export interface NewColumnIn {
  name: string;
  type: string;
  question: string;
  extraction_hint?: string;
  options?: string[] | null;
  cross_verify?: boolean;
}

// ─── helpers ────────────────────────────────────────────────
// Standardize errors as MatrixApiError (api-error.ts; shared with snapshot-api).

function headers(json = false): Record<string, string> {
  const h: Record<string, string> = typeof window === "undefined" ? {} : spaceHeader();
  if (json) h["content-type"] = "application/json";
  return h;
}

const BASE = "/api/v1/kb/matrices";

function mx(matrixId: string): string {
  return `${BASE}/${encodeURIComponent(matrixId)}`;
}

// ─── matrices ───────────────────────────────────────────────

/**
 * `visibility` (step 1 S8) is sent only when given; the server ignores it while only-me matrices are off and
 * then answers `visibility: "space"` with `ignored_fields: ["visibility"]`.
 */
export async function createMatrix(
  workspaceId: string,
  name: string,
  fileIds: string[],
  columns: NewColumnIn[],
  visibility?: MatrixVisibility,
): Promise<{ matrix_id: string; visibility?: MatrixVisibility; ignored_fields?: string[] }> {
  const r = await fetch(BASE, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ workspace_id: workspaceId, name, file_ids: fileIds, columns, ...(visibility ? { visibility } : {}) }),
  });
  return orThrow(r, "createMatrix");
}

export async function createMatrixFromTemplate(
  workspaceId: string,
  docTypeId: string,
): Promise<{ matrix_id: string }> {
  const r = await fetch(`${BASE}/from_template`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ workspace_id: workspaceId, doc_type_id: docTypeId }),
  });
  return orThrow(r, "createMatrixFromTemplate");
}

/** Returns the stored name (the server trims surrounding whitespace). */
export async function renameMatrix(matrixId: string, name: string): Promise<string> {
  const r = await fetch(mx(matrixId), { method: "PATCH", headers: headers(true), body: JSON.stringify({ name }) });
  const data = await orThrow<{ matrix_id: string; name: string }>(r, "renameMatrix");
  return data.name;
}

/** What changing who can see a matrix would do (step 1 S8; the creator only, 403 `matrix_creator_required` otherwise). */
export interface MatrixVisibilityPreview {
  visibility: MatrixVisibility;
  /** Only-me matrices are enabled and the caller may edit. */
  can_change: boolean;
  /** Names of the other people who changed a cell or started a run here (unresolved names are only counted). */
  contributors: string[];
  contributors_count: number;
  /** The caller's personal files in the matrix (they keep it from becoming visible to the Space). */
  personal_files: Array<{ file_id: string; name: string }>;
  personal_files_count: number;
  /** Other people's personal files in the matrix (counted, never named). */
  hidden_personal_files: number;
  /** Other members' active runs (they keep it from becoming private). */
  active_runs_by_others: number;
}

export async function fetchMatrixVisibility(matrixId: string): Promise<MatrixVisibilityPreview> {
  const r = await fetch(`${mx(matrixId)}/visibility`, { headers: headers() });
  return orThrow(r, "fetchMatrixVisibility");
}

/**
 * Change who can see the matrix (the creator, while only-me matrices are on). Refusals carry their reason:
 * 422 `matrix_personal_files_block_space` (with the files), 409 `matrix_visibility_runs_active`, 422
 * `matrix_navigation_columns_block_private` (with the columns), 422 `matrix_visibility_unavailable`.
 */
export async function setMatrixVisibility(matrixId: string, visibility: MatrixVisibility): Promise<MatrixVisibility> {
  const r = await fetch(mx(matrixId), { method: "PATCH", headers: headers(true), body: JSON.stringify({ visibility }) });
  const data = await orThrow<{ matrix_id: string; visibility: MatrixVisibility }>(r, "setMatrixVisibility");
  return data.visibility;
}

export async function deleteMatrix(matrixId: string): Promise<void> {
  const r = await fetch(mx(matrixId), { method: "DELETE", headers: headers() });
  await orThrow(r, "deleteMatrix");
}

/**
 * @deprecated Kept for API compatibility only: a full replacement drops rows the caller cannot see.
 * The UI adds and removes named rows with `addMatrixFiles` / `removeMatrixFiles`.
 */
export async function replaceMatrixFiles(matrixId: string, fileIds: string[]): Promise<void> {
  const r = await fetch(`${mx(matrixId)}/files`, {
    method: "PUT",
    headers: headers(true),
    body: JSON.stringify({ file_ids: fileIds }),
  });
  await orThrow(r, "replaceMatrixFiles");
}

/** Rows added or removed per request (server `_MAX_FILES_PER_CHANGE`). */
export const MATRIX_FILES_PER_CHANGE = 100;

export interface MatrixFilesAdded {
  added: string[];
  skipped: number;
  files_count: number;
}

export interface MatrixFilesRemoved {
  removed: string[];
  files_count: number;
  edited: number;
  verified: number;
}

/** Append rows at the end; files already in the matrix are skipped. */
export async function addMatrixFiles(matrixId: string, fileIds: string[]): Promise<MatrixFilesAdded> {
  const r = await fetch(`${mx(matrixId)}/files`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ file_ids: fileIds }),
  });
  return orThrow(r, "addMatrixFiles");
}

/** Remove only the named rows (403 `matrix_remove_requires_owner` when edited or verified cells need the creator or an admin). */
export async function removeMatrixFiles(matrixId: string, fileIds: string[]): Promise<MatrixFilesRemoved> {
  const r = await fetch(`${mx(matrixId)}/files/remove`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ file_ids: fileIds }),
  });
  return orThrow(r, "removeMatrixFiles");
}

// ─── columns ────────────────────────────────────────────────

export async function addColumn(matrixId: string, column: NewColumnIn): Promise<{ column_id: string }> {
  const r = await fetch(`${mx(matrixId)}/columns`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify(column),
  });
  return orThrow(r, "addColumn");
}

export async function draftColumn(matrixId: string, question: string): Promise<ColumnDraft> {
  const r = await fetch(`${mx(matrixId)}/columns/draft`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ question }),
  });
  return orThrow(r, "draftColumn");
}

/** M3: return updated column (server returns {matrix_id, column}); post-edit confirmation compares
 * prompt_version increments (§4.2, review G-P3-12: returning void leaves FE unable to compare). */
export async function updateColumn(
  matrixId: string,
  columnId: string,
  patch: { name?: string; question?: string; extraction_hint?: string; options?: string[] | null; cross_verify?: boolean; navigation_role?: "none" | "description" | "topics" },
): Promise<MatrixColumnVM> {
  const r = await fetch(`${mx(matrixId)}/columns/${encodeURIComponent(columnId)}`, {
    method: "PATCH",
    headers: headers(true),
    body: JSON.stringify(patch),
  });
  const data = await orThrow<{ matrix_id: string; column: WireColumn }>(r, "updateColumn");
  return buildColumnFromWire(data.column);
}

export async function deleteColumn(matrixId: string, columnId: string): Promise<void> {
  const r = await fetch(`${mx(matrixId)}/columns/${encodeURIComponent(columnId)}`, {
    method: "DELETE",
    headers: headers(),
  });
  await orThrow(r, "deleteColumn");
}

// ─── runs ───────────────────────────────────────────────────

export async function estimateRun(matrixId: string, scope: RunScope): Promise<RunEstimate> {
  const r = await fetch(`${mx(matrixId)}/runs/estimate`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify(scope),
  });
  return orThrow(r, "estimateRun");
}

/** Price one cell's rerun (the per-cell buttons show it; the rerun itself starts without a dialog). */
export async function estimateCell(matrixId: string, fileId: string, columnId: string): Promise<RunEstimate> {
  return estimateRun(matrixId, { kind: "single_cell", file_id: fileId, column_id: columnId });
}

export async function startRun(matrixId: string, scope: RunScope): Promise<RunStarted> {
  const r = await fetch(`${mx(matrixId)}/runs`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify(scope),
  });
  return orThrow(r, "startRun");
}

export async function cancelRun(matrixId: string, runId: string): Promise<void> {
  const r = await fetch(`${mx(matrixId)}/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    headers: headers(),
  });
  await orThrow(r, "cancelRun");
}

export async function rerunCell(matrixId: string, fileId: string, columnId: string): Promise<{ run_id: string; cells_total: number }> {
  const r = await fetch(
    `${mx(matrixId)}/cells/${encodeURIComponent(fileId)}/${encodeURIComponent(columnId)}/rerun`,
    { method: "POST", headers: headers() },
  );
  return orThrow(r, "rerunCell");
}

// ─── cells ──────────────────────────────────────────────────

type CellPatchOp =
  | { op: "edit_value"; value: unknown }
  | { op: "keep_as_text" }
  // M3: set_verified permits false (undo verification, FR-12 r5); revert_edited clears edit marker (FR-9 r5).
  | { op: "set_verified"; verified: boolean }
  | { op: "revert_edited" };

/** Bulk verify result: cells written, and the others by reason (step 1 §4.2). */
export interface CellsVerified {
  changed: number;
  skipped: { not_done: number; stale: number; unavailable: number; already: number };
  batch_id: string;
}

/**
 * Verify (or undo verifying) a column, named rows, or both. Verifying takes done, current cells (edited ones too);
 * undoing takes every verified cell. Rows the caller cannot read are counted as `unavailable` and left untouched.
 */
export async function verifyCells(
  matrixId: string,
  body: { column_id?: string; file_ids?: string[]; verified: boolean },
): Promise<CellsVerified> {
  const r = await fetch(`${mx(matrixId)}/cells/verify`, { method: "POST", headers: headers(true), body: JSON.stringify(body) });
  return orThrow(r, "verifyCells");
}

interface WireCellEvent {
  event_id: string;
  kind: string;
  actor_name: string | null;
  at: string;
  run_id: string | null;
  batch_id: string | null;
}

/** A cell's history, newest first; `more` says older events exist beyond the server limit (50). */
export async function fetchCellEvents(matrixId: string, fileId: string, columnId: string): Promise<{ items: MatrixCellEvent[]; more: boolean }> {
  const r = await fetch(`${mx(matrixId)}/cells/${encodeURIComponent(fileId)}/${encodeURIComponent(columnId)}/events`, { headers: headers() });
  const data = await orThrow<{ items: WireCellEvent[]; more?: boolean }>(r, "fetchCellEvents");
  const items: MatrixCellEvent[] = [];
  for (const w of data.items) {
    const base = buildLastEventFromWire(w);
    if (base) items.push({ ...base, eventId: w.event_id, runId: w.run_id ?? null, batchId: w.batch_id ?? null });
  }
  return { items, more: !!data.more };
}

/** Return wire cell (caller converts to VM via snapshot-api.buildCellFromWire). */
export async function patchCell(
  matrixId: string,
  fileId: string,
  columnId: string,
  body: CellPatchOp,
): Promise<{ cell: unknown }> {
  const r = await fetch(`${mx(matrixId)}/cells/${encodeURIComponent(fileId)}/${encodeURIComponent(columnId)}`, {
    method: "PATCH",
    headers: headers(true),
    body: JSON.stringify(body),
  });
  return orThrow(r, "patchCell");
}
