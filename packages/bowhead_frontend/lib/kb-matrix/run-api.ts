import { appFetch as fetch } from "@/lib/base-path";

/**
 * kb-matrix M2 — matrix CRUD / run / cell mutation API client
 * (`/api/v1/kb/matrices/*`，gateway routes/kb_matrix.py).
 */

import { spaceHeader } from "@/lib/space-storage";

import { MatrixApiError, matrixJsonOrThrow as orThrow } from "./api-error";
import { type WireColumn, buildColumnFromWire } from "./snapshot-api";
import type { MatrixColumnVM } from "./types";

// Reexport (M3: types moved to shared api-error.ts; existing import sites stay unchanged).
export { MatrixApiError };

// ─── DTOs ───────────────────────────────────────────────────

export interface RunScope {
  kind: "all_pending" | "column" | "force_all" | "force_column" | "stale" | "stale_column";
  column_id?: string;
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

export async function createMatrix(
  workspaceId: string,
  name: string,
  fileIds: string[],
  columns: NewColumnIn[],
): Promise<{ matrix_id: string }> {
  const r = await fetch(BASE, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ workspace_id: workspaceId, name, file_ids: fileIds, columns }),
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

export async function renameMatrix(matrixId: string, name: string): Promise<void> {
  const r = await fetch(mx(matrixId), { method: "PATCH", headers: headers(true), body: JSON.stringify({ name }) });
  await orThrow(r, "renameMatrix");
}

export async function deleteMatrix(matrixId: string): Promise<void> {
  const r = await fetch(mx(matrixId), { method: "DELETE", headers: headers() });
  await orThrow(r, "deleteMatrix");
}

export async function replaceMatrixFiles(matrixId: string, fileIds: string[]): Promise<void> {
  const r = await fetch(`${mx(matrixId)}/files`, {
    method: "PUT",
    headers: headers(true),
    body: JSON.stringify({ file_ids: fileIds }),
  });
  await orThrow(r, "replaceMatrixFiles");
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
  patch: { name?: string; question?: string; extraction_hint?: string; options?: string[] | null; cross_verify?: boolean },
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
