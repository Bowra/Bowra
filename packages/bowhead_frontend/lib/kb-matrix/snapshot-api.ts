import { appFetch as fetch } from "@/lib/base-path";
import { coalesceRequest } from "@/lib/coalesce-request";

/**
 * kb-matrix M2 — snapshot data source (GET /api/v1/kb/matrices/{id}/snapshot).
 *
 * Gateway DTO is snake_case (repo convention); thin typed snake→camel mapping here
 * (technical-design decision 9); components continue consuming only `MatrixSnapshot`, coexisting with M1
 * template-view.ts assembly (two list groups, two data sources).
 */

import { spaceHeader } from "@/lib/space-storage";

import { matrixJsonOrThrow as jsonOrThrow } from "./api-error";
import type {
  MatrixCellEventKind,
  MatrixCellLastEvent,
  MatrixCellVM,
  MatrixColumnVM,
  MatrixRowVM,
  MatrixRunVM,
  MatrixSnapshot,
  MatrixVisibility,
} from "./types";
import { cellKey } from "./types";

// ─── wire shapes（gateway §7.5）────────────────────────────

interface WireVariant {
  raw?: unknown;
  normalized?: Record<string, unknown> | null;
  is_primary?: boolean;
  extracted_by?: string;
  confidence?: number | null;
  source_excerpt?: string | null;
  chunk_id?: string | null;
  page?: number | null;
  /** Step 1 S7: position of the quoted chunk; absent on cells written before it. */
  chunk_index?: number | null;
  /** M3: cross-verification agreement rate (wrap passthrough, only on cross_verify columns). */
  self_consistency_agree?: number | null;
  [k: string]: unknown;
}

interface WireCell {
  status: string;
  variants: WireVariant[];
  fail_reason?: string | null;
  edited: boolean;
  verified: boolean;
  stale: boolean;
  cached: boolean;
  kb_generation_id?: string | null;
  kb_source_check_state?: string | null;
  current_source?: { state: string } | null;
  prompt_version: number;
  seq: number;
  /** Step 1 (S4): the latest change; absent from older backends and unreadable rows. */
  last_event?: WireCellLastEvent | null;
}

export interface WireCellLastEvent {
  kind: string;
  actor_name: string | null;
  at: string;
}

const CELL_EVENT_KINDS: ReadonlySet<string> = new Set([
  "edited",
  "kept_as_text",
  "edit_reverted",
  "verified",
  "verify_reverted",
  "ai_value",
  "edit_superseded",
  "auto_retried",
]);

/** Wire event → VM; an unknown kind (a newer backend) is dropped rather than shown wrongly. */
export function buildLastEventFromWire(w: WireCellLastEvent | null | undefined): MatrixCellLastEvent | undefined {
  if (!w || !CELL_EVENT_KINDS.has(w.kind)) return undefined;
  return { kind: w.kind as MatrixCellEventKind, actorName: w.actor_name ?? null, at: w.at };
}

interface WireRun {
  run_id: string;
  status: string;
  scope?: { kind?: string | null; column_id?: string | null } | null;
  cells_total: number;
  cells_done: number;
  cells_failed: number;
  skipped_edited: number;
  error_code?: string | null;
  eta_s: number;
  initiated_by: string;
  can_cancel?: boolean;
  /** Step 1 (S3): a run limited to a few rows, and how many. */
  trial?: boolean | null;
  trial_files?: number | null;
}

interface WireFullSnapshot {
  /** Step 1 S6 (absent from older backends). */
  kind?: "full";
  /** Step 1 S6: what this projection holds that a delta cannot carry; absent from older backends. */
  projection_digest?: string | null;
  matrix: {
    matrix_id: string;
    name: string;
    can_edit: boolean;
    /** Creator or Space/Team admin (absent from older backends: treated as false). */
    can_manage?: boolean;
    seq: number;
    structure_seq: number;
    poll_ms: number;
    template_doc_type_id?: string | null;
    /** Step 1 S8 (absent from older backends: a Space matrix). */
    visibility?: MatrixVisibility;
    /** The caller created the matrix and only-me matrices are enabled (absent: false). */
    can_change_visibility?: boolean;
  };
  columns: WireColumn[];
  rows: Array<{ file_id: string; file_name: string; missing: boolean; file_version: number }>;
  cells: Record<string, WireCell>;
  /** M3: active + latest 5 terminal runs (excluding single_cell). */
  runs?: WireRun[] | null;
  /** @deprecated runs[0] compatibility field (old backend in rollback window returns only this). */
  run: WireRun | null;
}

export interface WireColumn {
  column_id: string;
  name: string;
  type: string;
  question?: string;
  extraction_hint?: string;
  prompt_version?: number;
  options?: string[] | null;
  cross_verify?: boolean;
  navigation_role?: "none" | "description" | "topics";
}

/** Step 1 S6: cells written after since_seq in rows the viewer can read; absent rows/columns are unchanged. */
interface WireDelta {
  kind?: "delta";
  seq: number;
  structure_seq?: number;
  /** Equals the digest the poll sent (the server answers in full otherwise). */
  projection_digest?: string | null;
  cells: Record<string, WireCell>;
  runs?: WireRun[] | null;
  run: WireRun | null;
}

export interface MatrixListItem {
  matrixId: string;
  name: string;
  templateDocTypeId: string | null;
  filesCount: number;
  columnsCount: number;
  statusSummary: { done: number; failed: number; needs_review: number; running: number; queued: number };
  lastRunAt: string | null;
  createdBy: string;
  /** Creator's display name in this Team; null when it cannot be resolved. */
  createdByName: string | null;
  createdAt: string;
  /** Step 1 S8: "private" matrices are listed for their creator only (absent: a Space matrix). */
  visibility?: MatrixVisibility;
}

export type MatrixDelta = {
  seq: number;
  cells: Record<string, MatrixCellVM>;
  /** M3: every delta poll sends full runs (poll liveness reads payload directly, not store after 100ms buffer). */
  runs: MatrixRunVM[];
  /** Step 1 S6: the digest the delta was computed against (undefined from older backends). */
  projectionDigest?: string;
};

// ─── helpers ────────────────────────────────────────────────
// Standardize errors as MatrixApiError (api-error.ts; M3 §4.8: plain Error without status caused dead polling).

function headers(): Record<string, string> {
  if (typeof window === "undefined") return {};
  return spaceHeader();
}

/** Primary-variant display: normalized amount/value/choice/choices/flag/raw_string →
 * raw (keep_as_text makes normalized=null, original text in raw) → "". Matches M1 buildCell order
 * plus raw fallback. M3 shapes: choice(select)/choices(multi_select, joined with “、”)/
 * flag(boolean → semantic "true"|"false" tokens, renderer maps bool_true/bool_false i18n
 * because this pure function lacks i18n context, G-P3-10). */
export function variantDisplayValue(v: WireVariant | undefined): string {
  if (!v) return "";
  const n = v.normalized;
  if (n && typeof n === "object") {
    if ("amount" in n && n.amount != null) return String(n.amount);
    if ("value" in n && n.value != null) return String(n.value);
    if ("choice" in n && n.choice != null) return String(n.choice);
    if ("choices" in n && Array.isArray(n.choices)) return (n.choices as unknown[]).map(String).join("、");
    if ("flag" in n && typeof n.flag === "boolean") return n.flag ? "true" : "false";
    if ("raw_string" in n && n.raw_string != null) return String(n.raw_string);
  }
  if (v.raw != null) return String(v.raw);
  return "";
}

export function buildCellFromWire(w: WireCell): MatrixCellVM {
  const primary = w.variants.find((v) => v.is_primary) ?? w.variants[0];
  return {
    status: (["queued", "running", "done", "failed", "needs_review"].includes(w.status)
      ? w.status
      : "done") as MatrixCellVM["status"],
    value: variantDisplayValue(primary),
    // Variant mirrors wire shape (same records variant family); pass through to multi-source card.
    variants: w.variants as unknown as MatrixCellVM["variants"],
    sourceExcerpt: primary?.source_excerpt ?? undefined,
    chunkId: primary?.chunk_id ?? undefined,
    page: primary?.page ?? undefined,
    chunkIndex: primary?.chunk_index ?? undefined,
    confidence: primary?.confidence ?? undefined,
    edited: !!w.edited,
    verified: !!w.verified,
    stale: !!w.stale,
    cached: !!w.cached,
    generationId: w.kb_generation_id ?? undefined,
    sourceCheckState: w.kb_source_check_state ?? undefined,
    currentSourceState: w.current_source?.state,
    failReason: w.fail_reason ?? undefined,
    seq: w.seq,
    selfConsistencyAgree: typeof primary?.self_consistency_agree === "number" ? primary.self_consistency_agree : undefined,
    lastEvent: buildLastEventFromWire(w.last_event),
  };
}

function buildRun(w: WireRun | null): MatrixRunVM | null {
  if (!w) return null;
  return {
    runId: w.run_id,
    status: w.status as MatrixRunVM["status"],
    cellsTotal: w.cells_total,
    cellsDone: w.cells_done,
    cellsFailed: w.cells_failed,
    skippedEdited: w.skipped_edited,
    errorCode: w.error_code ?? null,
    etaS: w.eta_s,
    initiatedBy: w.initiated_by,
    scopeKind: w.scope?.kind ?? undefined,
    scopeColumnId: w.scope?.column_id ?? undefined,
    canCancel: w.can_cancel ?? undefined,
    trial: w.trial ?? undefined,
    trialFiles: w.trial_files ?? undefined,
  };
}

/** Defensive mapping (M3 §4.5): old rollback-window backend returns only `run` → singleton array. */
function buildRuns(runs: WireRun[] | null | undefined, run: WireRun | null | undefined): MatrixRunVM[] {
  const wire = runs ?? (run ? [run] : []);
  return wire.map((w) => buildRun(w)).filter((r): r is MatrixRunVM => r !== null);
}

/** Wire column → VM (snapshot and PATCH column responses share shape; reused by run-api.updateColumn). */
export function buildColumnFromWire(c: WireColumn): MatrixColumnVM {
  return {
    columnId: c.column_id,
    name: c.name,
    type: c.type,
    question: c.question || undefined,
    extractionHint: c.extraction_hint || undefined,
    promptVersion: c.prompt_version,
    options: c.options ?? null,
    crossVerify: !!c.cross_verify,
    navigationRole: c.navigation_role ?? "none",
  };
}

function buildSnapshot(w: WireFullSnapshot): MatrixSnapshot {
  const columns: MatrixColumnVM[] = w.columns.map(buildColumnFromWire);
  const cellsByFile = new Map<string, Record<string, MatrixCellVM | undefined>>();
  for (const [key, cw] of Object.entries(w.cells)) {
    const sep = key.indexOf(":");
    if (sep < 0) continue;
    const fileId = key.slice(0, sep);
    const columnId = key.slice(sep + 1);
    const bag = cellsByFile.get(fileId) ?? {};
    bag[columnId] = buildCellFromWire(cw);
    cellsByFile.set(fileId, bag);
  }
  const rows: MatrixRowVM[] = w.rows.map((r) => ({
    fileId: r.file_id,
    fileName: r.file_name,
    missing: r.missing,
    extractFailed: false,
    recordId: "", // M2 real matrix has no record anchor (`fileId:recordId` grid key remains unique).
    approved: false,
    cells: cellsByFile.get(r.file_id) ?? {},
  }));
  return {
    matrixId: w.matrix.matrix_id,
    title: w.matrix.name,
    readOnly: false,
    columns,
    rows,
    seq: w.matrix.seq,
    canEdit: w.matrix.can_edit,
    canManage: !!w.matrix.can_manage,
    visibility: w.matrix.visibility === "private" ? "private" : "space",
    canChangeVisibility: !!w.matrix.can_change_visibility,
    structureSeq: w.matrix.structure_seq,
    projectionDigest: w.projection_digest ?? undefined,
    pollMs: w.matrix.poll_ms,
    run: buildRun(w.run),
    runs: buildRuns(w.runs, w.run),
  };
}

// ─── API ────────────────────────────────────────────────────

export async function listMatrices(workspaceId: string): Promise<MatrixListItem[]> {
  const url = `/api/v1/kb/matrices?workspace_id=${encodeURIComponent(workspaceId)}`;
  // Header counts/matrix page request same space matrix list: coalesce concurrent requests (FR8.2).
  return coalesceRequest(url, () => loadMatrices(url));
}

async function loadMatrices(url: string): Promise<MatrixListItem[]> {
  const r = await fetch(url, {
    headers: headers(),
  });
  const data = await jsonOrThrow<{
    items: Array<{
      matrix_id: string;
      name: string;
      template_doc_type_id: string | null;
      files_count: number;
      columns_count: number;
      status_summary: MatrixListItem["statusSummary"];
      last_run_at: string | null;
      created_by: string;
      created_by_name?: string | null;
      created_at: string;
      visibility?: MatrixVisibility;
    }>;
  }>(r, "listMatrices");
  return data.items.map((i) => ({
    matrixId: i.matrix_id,
    name: i.name,
    templateDocTypeId: i.template_doc_type_id,
    filesCount: i.files_count,
    columnsCount: i.columns_count,
    statusSummary: i.status_summary,
    lastRunAt: i.last_run_at,
    createdBy: i.created_by,
    createdByName: i.created_by_name ?? null,
    createdAt: i.created_at,
    visibility: i.visibility === "private" ? "private" : "space",
  }));
}

/** A full snapshot. `opened` marks the first load of a matrix page, which the server records once as the matrix
 * being opened (step 1 S9); polls and later reloads never send it. */
export async function fetchMatrixSnapshot(matrixId: string, options?: { opened?: boolean }): Promise<MatrixSnapshot> {
  const query = options?.opened ? "?opened=true" : "";
  const r = await fetch(`/api/v1/kb/matrices/${encodeURIComponent(matrixId)}/snapshot${query}`, { headers: headers() });
  const wire = await jsonOrThrow<WireFullSnapshot>(r, "fetchMatrixSnapshot");
  return buildSnapshot(wire);
}

/** Polling (step 1 S6): with the digest of the projection held, the server answers with only the cells written
 * after since_seq while nothing else changed, and in full otherwise; without a digest it always answers in full. */
export async function fetchMatrixDelta(
  matrixId: string,
  sinceSeq: number,
  digest?: string,
): Promise<{ kind: "delta"; delta: MatrixDelta } | { kind: "full"; snapshot: MatrixSnapshot }> {
  const query = new URLSearchParams({ since_seq: String(sinceSeq) });
  if (digest) query.set("digest", digest);
  const r = await fetch(`/api/v1/kb/matrices/${encodeURIComponent(matrixId)}/snapshot?${query.toString()}`, {
    headers: headers(),
  });
  const wire = await jsonOrThrow<WireFullSnapshot | WireDelta>(r, "fetchMatrixDelta");
  if ("matrix" in wire) return { kind: "full", snapshot: buildSnapshot(wire) };
  const cells: Record<string, MatrixCellVM> = {};
  for (const [key, cw] of Object.entries(wire.cells)) cells[key] = buildCellFromWire(cw);
  return {
    kind: "delta",
    delta: {
      seq: wire.seq,
      cells,
      runs: buildRuns(wire.runs, wire.run),
      projectionDigest: wire.projection_digest ?? undefined,
    },
  };
}

export { cellKey };
