/**
 * kb-matrix M4 — graduation + template-management API client
 * (typed `POST /api/v1/kb/matrices/{id}/graduate` + doc_types PUT/DELETE,
 * gateway routes/kb_matrix.py / kb_doc_types.py).
 *
 * doc_types PUT/DELETE in kb-knowledge-tables-api throw plain Error (detail
 * stringified); template management must branch by code (doc_type_name_taken 409 /
 * graduated_fields_frozen 422), so matrixJsonOrThrow throws MatrixApiError here.
 */

import { appFetch as fetch } from "@/lib/base-path";
import { type ExtractField, type KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";
import { spaceHeader } from "@/lib/space-storage";

import { MatrixApiError, matrixJsonOrThrow as orThrow } from "./api-error";
import type { NewColumnIn } from "./run-api";

export { MatrixApiError };

// ─── graduate ───────────────────────────────────────────────

export type GraduateMode = "snapshot" | "template";

export interface GraduateResult {
  doc_type_id: string;
  mode: GraduateMode;
  records: number;
  columns: number;
  name: string;
  /** Idempotent hit (same matrix+last_seq+mode reuses existing doc_type). */
  reused: boolean;
}

/** Match server MAX_FIELDS_PER_SCHEMA (doc_type_store.py); frontend precheck, with 422 as backstop. */
export const GRADUATE_MAX_COLUMNS = 20;

function headers(json = false): Record<string, string> {
  const h: Record<string, string> = typeof window === "undefined" ? {} : spaceHeader();
  if (json) h["content-type"] = "application/json";
  return h;
}

export async function graduateMatrix(
  matrixId: string,
  body: { name: string; mode: GraduateMode },
): Promise<GraduateResult> {
  const r = await fetch(`/api/v1/kb/matrices/${encodeURIComponent(matrixId)}/graduate`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify(body),
  });
  return orThrow(r, "graduateMatrix");
}

// ─── doc_type ops（typed errors）────────────────────────────

export async function updateDocTypeMeta(
  docTypeId: string,
  patch: { name?: string; description?: string; fields?: ExtractField[] },
): Promise<KbDocTypeDTO> {
  const r = await fetch(`/api/v1/kb/doc_types/${encodeURIComponent(docTypeId)}`, {
    method: "PUT",
    headers: headers(true),
    body: JSON.stringify(patch),
  });
  return orThrow(r, "updateDocTypeMeta");
}

export async function deleteDocTypeTyped(docTypeId: string): Promise<void> {
  const r = await fetch(`/api/v1/kb/doc_types/${encodeURIComponent(docTypeId)}`, {
    method: "DELETE",
    headers: headers(),
  });
  if (!r.ok) await orThrow(r, "deleteDocType");
}

// ─── Pure functions (dialog readiness / application mapping / grouping, enforced by UT) ─────────

export type ReadinessKind = "agg" | "cat" | "list";

/** Readiness buckets (mockup readiness()): currency/number→aggregatable (with undeclared-unit hint);
 * select/multi_select/boolean→categorical summaries; string/date/question→listing and filtering only.
 * Server can query date MIN/MAX, but user-facing wording keeps three simple buckets (brief A-3). */
export function readinessOf(type: string): { k: ReadinessKind; unit: boolean } {
  if (type === "currency" || type === "number") return { k: "agg", unit: true };
  if (type === "select" || type === "multi_select" || type === "boolean") return { k: "cat", unit: false };
  return { k: "list", unit: false };
}

/** Column-group template classification (management includes all kinds; application routes accordingly, decision 14). */
export function isColumnGroupKind(kind: string | undefined): boolean {
  return kind === "seed" || kind === "template" || kind === "graduated";
}

/** doc_type fields → new-matrix column prefill (decision 14 application routing).
 * Reverse build_graduation_fields: field.description is the original question; question columns were
 * downgraded on graduation/seed to string + question-preserving hint (roundtrip as string, documented intentional loss). */
export function fieldsToColumns(fields: ExtractField[]): NewColumnIn[] {
  // Defensive truncation (contract review P2): return path respects _ColumnIn question≤2000/hint≤500 caps;
  // Graduation already truncates; cover oversized legacy doc_types in existing storage (otherwise application always hits 422).
  return fields.map((f) => ({
    name: f.name,
    type: f.type,
    question: (f.description || f.name).slice(0, 2000),
    extraction_hint: f.extraction_hint ? f.extraction_hint.slice(0, 500) : undefined,
    options: f.options && f.options.length ? f.options : undefined,
    cross_verify: f.cross_verify || undefined,
  }));
}
