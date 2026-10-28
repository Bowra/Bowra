import { appFetch as fetch } from "@/lib/base-path";
import { readApiError } from "@/lib/api-client";
import { coalesceRequest } from "@/lib/coalesce-request";

import { spaceHeader } from "@/lib/space-storage";/**
 * KB Knowledge Tables API client.
 *
 * kb-knowledge-tables sprint v1.0. Replaces kb-doc-types-api.ts.
 *
 * Endpoints (gateway):
 * - GET    /api/v1/kb/doc_types?workspace_id=...
 * - POST   /api/v1/kb/doc_types/seed (once only when list reports space unseeded)
 * - POST   /api/v1/kb/doc_types
 * - PUT    /api/v1/kb/doc_types/{id}
 * - DELETE /api/v1/kb/doc_types/{id}
 * - POST   /api/v1/kb/doc_types/draft  (LLM extract_schema gen)
 * - GET    /api/v1/kb/doc_types/{id}/records?workspace_id=...
 * - GET    /api/v1/kb/records/{id}?workspace_id=...
 * - PATCH  /api/v1/kb/records/{id}/field
 * - PATCH  /api/v1/kb/records/{id}/verify_status
 */

// kb-matrix M4: FIELD_TYPES single source +3 types (select/multi_select/boolean), aligned with
// bowhead_core.kb.doc_type_store.FIELD_TYPES.
export type FieldType = "string" | "number" | "currency" | "date" | "select" | "multi_select" | "boolean";
export type VerifyStatus = "ready" | "needs_review" | "approved" | "extract_failed";

/** M4 kind derivation (server to_dict, decision 12): template-management grouping/bind-picker filtering. */
export type DocTypeKind = "manual" | "seed" | "template" | "graduated";

export interface ExtractField {
  name: string;
  type: FieldType;
  description: string;
  required: boolean;
  cross_verify: boolean;
  extraction_hint: string;
  // M4 additive (server conditionally emits: missing values omit keys).
  options?: string[] | null;
  dimension?: Record<string, unknown> | null;
}

export interface KbDocTypeDTO {
  doc_type_id: string;
  workspace_id: string;
  name: string;
  description: string;
  fields: ExtractField[];
  record_count: number;
  extractor_model: string;
  cross_verify_enabled: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
  // Sprint 2 Phase A: cardinality + classifier hints (defaults preserve V1)
  cardinality?: "one" | "many";
  auto_extract_enabled?: boolean;
  sample_keywords?: string[];
  // kb-matrix M4 additive (always emitted by to_dict): graduation lineage + derived kind.
  kind?: DocTypeKind;
  /** null=nongraduated | 'template'=fields only | 'snapshot'=frozen snapshot (pending hidden from lists). */
  graduated_mode?: "template" | "snapshot" | "pending_snapshot" | null;
  graduated_from_matrix_id?: string | null;
  /** Cached cardinality=many row count (graduated table = snapshot row count). */
  row_count?: number;
  /** kb-matrix-sharing: copied-team-template lineage (null=not copied; to_dict always emits). */
  copied_from_team_template_id?: string | null;
}

// Sprint 2 Phase C: aggregate stats from kb_extracted_records, returned by
// listDocTypesWithStats. Keyed by doc_type_id in the response.
export interface DocTypeStats {
  total: number;
  needs_review_count: number;
  last_extracted_at: string | null;
}

// kb-v2 sprint Phase A Step 3: backend now emits v2 variant array shape
// (Record<string, Variant[]>). The legacy V1-style {value, source_snippet, ...}
// is exposed via the primary variant. ``getPrimaryField`` adapter normalizes.
// FE renderer adaptation to read full variant array is a follow-up.
export interface Variant {
  raw: string | null;
  normalized: Record<string, unknown> | null;
  effective_at: string | null;
  is_primary: boolean;
  extracted_by: string;
  confidence: number | null;
  source_excerpt: string | null;
  chunk_id: string | null;
  page: number | null;
  edited_by_user?: boolean;
  /** kb-matrix M3 additive: cross-verification agreement rate (wrap passthrough, cross_verify columns only). */
  self_consistency_agree?: number | null;
}

// Legacy V1-style read shape — extracted from primary variant via getPrimaryField.
export interface FieldExtraction {
  value: unknown;
  source_snippet?: string;
  source_page?: number;
  // provenance path B: origin chunk of source_snippet (backfilled at extract time);
  // present when the snippet matched an indexed chunk, undefined otherwise.
  chunk_id?: string;
  confidence?: number;
  citation_verified?: boolean;
  edited_by_user?: boolean;
  needs_review?: boolean;
  self_consistency_agree?: number;
}

export interface KbRecordDTO {
  record_id: string;
  doc_type_id: string;
  file_id: string;
  workspace_id: string;
  // v2 shape: each field is a variant array. Use ``getPrimaryField(r, fname)``
  // to access V1-style {value, source_snippet, ...} via the primary variant.
  fields: Record<string, Variant[]>;
  verify_status: VerifyStatus;
  edited_by_user: string[];
  extractor_model: string;
  extraction_duration_ms: number | null;
  extracted_at: string;
  last_modified_at: string;
  last_modified_by: string;
  schema_version?: number;
}

/**
 * Extract V1-style {value, source_snippet, confidence, edited_by_user} from
 * the primary variant of a v2 record field. Returns undefined when the field
 * is absent or has no variants.
 */
export function getPrimaryField(
  rec: KbRecordDTO,
  fname: string,
): FieldExtraction | undefined {
  const variants = rec.fields?.[fname];
  if (!variants || variants.length === 0) return undefined;
  const primary = variants.find((v) => v.is_primary) ?? variants[0];
  const norm = primary.normalized || {};
  // Re-extract V1-style ``value`` from ``normalized.amount`` (currency/number)
  // or ``normalized.value`` (date/string) or ``normalized.raw_string`` (fallback).
  let value: unknown =
    "amount" in norm ? norm.amount :
    "value" in norm ? norm.value :
    "raw_string" in norm ? norm.raw_string :
    undefined;
  // Coerce Decimal-as-string back to number when purely digits (legacy V1
  // assertions expected ints).
  if (typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value)) {
    value = Number(value);
  }
  return {
    value,
    source_snippet: primary.source_excerpt ?? undefined,
    source_page: primary.page ?? undefined,
    chunk_id: primary.chunk_id ?? undefined,
    confidence: primary.confidence ?? undefined,
    edited_by_user: primary.edited_by_user,
  };
}

function getWorkspaceHeader(): Record<string, string> {
  if (typeof window === "undefined") return {};
  return spaceHeader();  // Batch 3/c2 consolidation.
}

async function jsonOrThrow<T>(r: Response, _fn: string): Promise<T> {
  if (!r.ok) {
    throw await readApiError(r);
  }
  return (await r.json()) as T;
}

// ─── doc_types CRUD ──────────────────────

const templateSeedRequests = new Map<string, Promise<void>>();

/** Explicit built-in template seeding (once per space in this page lifecycle). */
async function ensureBuiltInTemplates(workspace_id: string): Promise<void> {
  const existing = templateSeedRequests.get(workspace_id);
  if (existing) return existing;
  const pending = fetch("/api/v1/kb/doc_types/seed", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...getWorkspaceHeader(),
    },
    body: JSON.stringify({ workspace_id }),
  })
    .then(() => undefined)
    .catch(() => undefined);
  templateSeedRequests.set(workspace_id, pending);
  return pending;
}

type DocTypeListResponse = { items: KbDocTypeDTO[]; templates_seeded?: boolean };

/**
 * Read template list. Read-only endpoint reports built-in seed status; only unseeded spaces trigger one
 * explicit seed and refetch, once per space, normally zero page-open writes (FR8.1). Rejected seeding
 * (e.g. archived space/read-only instance) returns original list as usual, without retry.
 */
async function readDocTypeList<T extends DocTypeListResponse>(
  workspace_id: string,
  query: string,
  fn: string,
): Promise<T> {
  const url = `/api/v1/kb/doc_types?workspace_id=${encodeURIComponent(workspace_id)}${query}`;
  const load = () =>
    fetch(url, { headers: { ...getWorkspaceHeader() } }).then((r) => jsonOrThrow<T>(r, fn));
  // Coalesce concurrent same-list requests (FR8.2: matrix-page components request same space list).
  const first = await coalesceRequest(url, load);
  if (first.templates_seeded !== false) return first;
  await ensureBuiltInTemplates(workspace_id);
  return coalesceRequest(url, load);
}

export async function listKnowledgeTables(workspace_id: string): Promise<KbDocTypeDTO[]> {
  const data = await readDocTypeList<DocTypeListResponse>(workspace_id, "", "listKnowledgeTables");
  return data.items;
}

// Sprint 2 Phase C: doc_types + aggregate record stats (single round-trip).
// Backend cross-DB query — see KbDocTypeStore.list_with_stats.
export async function listDocTypesWithStats(
  workspace_id: string,
): Promise<{ items: KbDocTypeDTO[]; stats: Record<string, DocTypeStats> }> {
  const data = await readDocTypeList<DocTypeListResponse & { stats: Record<string, DocTypeStats> }>(
    workspace_id,
    "&include_stats=1",
    "listDocTypesWithStats",
  );
  return { items: data.items, stats: data.stats };
}

export async function createKnowledgeTable(body: {
  workspace_id: string;
  name: string;
  description?: string;
  fields?: ExtractField[];
  extractor_model?: string;
  cross_verify_enabled?: boolean;
}): Promise<KbDocTypeDTO> {
  const r = await fetch("/api/v1/kb/doc_types", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...getWorkspaceHeader(),
    },
    body: JSON.stringify({
      workspace_id: body.workspace_id,
      name: body.name,
      description: body.description ?? "",
      fields: body.fields ?? [],
      extractor_model: body.extractor_model ?? "qwen-plus",
      cross_verify_enabled: body.cross_verify_enabled ?? true,
    }),
  });
  return jsonOrThrow<KbDocTypeDTO>(r, "createKnowledgeTable");
}

export async function updateKnowledgeTable(
  doc_type_id: string,
  body: Partial<
    Pick<KbDocTypeDTO, "name" | "description" | "fields" | "extractor_model" | "cross_verify_enabled">
  >,
): Promise<KbDocTypeDTO> {
  const r = await fetch(`/api/v1/kb/doc_types/${encodeURIComponent(doc_type_id)}`, {
    method: "PUT",
    headers: {
      "content-type": "application/json",
      ...getWorkspaceHeader(),
    },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<KbDocTypeDTO>(r, "updateKnowledgeTable");
}

export async function deleteKnowledgeTable(doc_type_id: string): Promise<void> {
  const r = await fetch(`/api/v1/kb/doc_types/${encodeURIComponent(doc_type_id)}`, {
    method: "DELETE",
    headers: { ...getWorkspaceHeader() },
  });
  if (!r.ok) {
    throw await readApiError(r);
  }
}

export async function draftKnowledgeTableFields(body: {
  workspace_id: string;
  user_query: string;
  sample_text?: string;
}): Promise<{ fields: ExtractField[]; raw_llm_output: string }> {
  const r = await fetch("/api/v1/kb/doc_types/draft", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...getWorkspaceHeader(),
    },
    body: JSON.stringify({
      workspace_id: body.workspace_id,
      user_query: body.user_query,
      sample_text: body.sample_text ?? "",
    }),
  });
  return jsonOrThrow<{ fields: ExtractField[]; raw_llm_output: string }>(
    r,
    "draftKnowledgeTableFields",
  );
}

// ─── records ──────────────────────

export async function listRecords(
  doc_type_id: string,
  workspace_id: string,
  opts: { limit?: number; offset?: number } = {},
): Promise<{ items: KbRecordDTO[]; total: number }> {
  const qs = new URLSearchParams({
    workspace_id,
    limit: String(opts.limit ?? 100),
    offset: String(opts.offset ?? 0),
  });
  const r = await fetch(
    `/api/v1/kb/doc_types/${encodeURIComponent(doc_type_id)}/records?${qs}`,
    { headers: { ...getWorkspaceHeader() } },
  );
  return jsonOrThrow<{ items: KbRecordDTO[]; total: number }>(r, "listRecords");
}

export async function getRecord(
  record_id: string,
  workspace_id: string,
): Promise<KbRecordDTO> {
  const r = await fetch(
    `/api/v1/kb/records/${encodeURIComponent(record_id)}?workspace_id=${encodeURIComponent(workspace_id)}`,
    { headers: { ...getWorkspaceHeader() } },
  );
  return jsonOrThrow<KbRecordDTO>(r, "getRecord");
}

export async function editRecordField(
  record_id: string,
  workspace_id: string,
  body: { field_name: string; new_value: unknown },
): Promise<KbRecordDTO> {
  const r = await fetch(
    `/api/v1/kb/records/${encodeURIComponent(record_id)}/field?workspace_id=${encodeURIComponent(workspace_id)}`,
    {
      method: "PATCH",
      headers: {
        "content-type": "application/json",
        ...getWorkspaceHeader(),
      },
      body: JSON.stringify(body),
    },
  );
  return jsonOrThrow<KbRecordDTO>(r, "editRecordField");
}

export async function updateRecordVerifyStatus(
  record_id: string,
  workspace_id: string,
  status: VerifyStatus,
): Promise<KbRecordDTO> {
  const r = await fetch(
    `/api/v1/kb/records/${encodeURIComponent(record_id)}/verify_status?workspace_id=${encodeURIComponent(workspace_id)}`,
    {
      method: "PATCH",
      headers: {
        "content-type": "application/json",
        ...getWorkspaceHeader(),
      },
      body: JSON.stringify({ status }),
    },
  );
  return jsonOrThrow<KbRecordDTO>(r, "updateRecordVerifyStatus");
}
