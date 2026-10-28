import { appFetch as fetch } from "@/lib/base-path";

/**
 * kb-matrix M1 — FE assembly of the template-record read-only matrix view
 * (tech-design-m1 §3: doc_types + records + files joined client-side; the M2
 * snapshot endpoint replaces this module without touching components).
 */

import { fetchKbFiles } from "@/lib/kb-api";
import {
  type KbDocTypeDTO,
  type KbRecordDTO,
  type Variant,
  getPrimaryField,
  listDocTypesWithStats,
  listRecords,
} from "@/lib/kb-knowledge-tables-api";

import type { MatrixCellVM, MatrixRowVM, MatrixSnapshot, TemplateViewSummary } from "./types";

const PAGE = 200;

const _VIEWER_ROLES = new Set(["viewer", "readonly", "read_only"]);
// Raw workspace roles the server folds into "ws_admin" (mirror of
// bowhead_core.access_control._WORKSPACE_ROLE_ALIASES) — the set the doc_type
// PUT/DELETE gate _require_admin_or_creator treats as admin.
const _WS_ADMIN_ROLES = new Set(["ws_admin", "admin", "owner", "workspace_admin"]);

export interface SpaceEditPerms {
  /** non-viewer space member — may create/apply matrices & templates. */
  canEdit: boolean;
  /** current user id, or null when /auth/me is unavailable. */
  uid: string | null;
  /** team_admin OR ws_admin — may edit/delete ANY template in the space. */
  isAdmin: boolean;
}

/** Raw workspace role → {canEdit, isAdmin}; mirrors the server role aliases so
 * the FE hides exactly what the server's admin-or-creator gate would 403. */
export function classifyWsRole(rawRole: string | null | undefined): { canEdit: boolean; isAdmin: boolean } {
  const r = (rawRole ?? "").trim().toLowerCase();
  return { canEdit: !_VIEWER_ROLES.has(r), isAdmin: _WS_ADMIN_ROLES.has(r) };
}

/** May the current user edit/delete THIS template? Mirrors the server gate
 * _require_admin_or_creator (kb_doc_types.py): creator OR team_admin OR ws_admin. */
export function canManageTemplate(createdBy: string | null | undefined, uid: string | null, isAdmin: boolean): boolean {
  if (isAdmin) return true;
  return !!createdBy && !!uid && createdBy === uid;
}

/** Derive edit permissions from current user/space role (same rules as resolveSpaceEditPerms, no requests). */
export function spaceEditPermsFrom(
  me: { id?: string | null; is_team_admin?: boolean | null } | null,
  rawRole: string | null | undefined,
): SpaceEditPerms {
  const uid = me?.id ?? null;
  if (me?.is_team_admin) return { canEdit: true, uid, isAdmin: true };
  const { canEdit, isAdmin } = classifyWsRole(rawRole);
  return { canEdit, uid, isAdmin };
}

/** Resolve the current user's space edit permissions in one /auth/me +
 * /auth/me/workspaces fetch. viewer/read_only members are read-only in the
 * matrix UI (PRD FR-34); edit/delete of a template additionally needs
 * creator-or-admin (see canManageTemplate). The server enforces the same floors
 * — this only decides which affordances to show. */
export async function resolveSpaceEditPerms(workspaceId: string): Promise<SpaceEditPerms> {
  try {
    const [meR, wsR] = await Promise.all([fetch("/api/v1/auth/me"), fetch("/api/v1/auth/me/workspaces")]);
    const me = meR.ok ? ((await meR.json()) as { id?: string; is_team_admin?: boolean }) : null;
    if (me?.is_team_admin) return spaceEditPermsFrom(me, null);
    if (!wsR.ok) return { canEdit: true, uid: me?.id ?? null, isAdmin: false }; // fail-open for entry points only — server still enforces
    const data = (await wsR.json()) as { items?: Array<{ workspace_id: string; role?: string | null }> };
    return spaceEditPermsFrom(me, (data.items ?? []).find((i) => i.workspace_id === workspaceId)?.role);
  } catch {
    return { canEdit: true, uid: null, isAdmin: false };
  }
}

export function viewIdFor(docTypeId: string): string {
  return `tpl_${docTypeId}`;
}

export function docTypeIdFromViewId(viewId: string): string | null {
  return viewId.startsWith("tpl_") ? viewId.slice(4) : null;
}

export async function listTemplateViews(workspaceId: string): Promise<TemplateViewSummary[]> {
  return templateViewsFrom(await listDocTypesWithStats(workspaceId));
}

/**
 * Knowledge-base sidebar Templates count = objects flat-listed by Templates view: all space templates (built-in / mine / extraction)
 * + team-template-library entries (caller passes empty array when team library is off). Synchronize here when view grouping changes.
 */
export function templatesListedCount(docTypes: readonly unknown[], teamEntries: readonly unknown[]): number {
  return docTypes.length + teamEntries.length;
}

/** Template Records = templates with records; derive from one doc_types+stats fetch (sidebar counts/list share source). */
export function templateViewsFrom({
  items,
  stats,
}: Awaited<ReturnType<typeof listDocTypesWithStats>>): TemplateViewSummary[] {
  return items
    .filter((dt) => (stats[dt.doc_type_id]?.total ?? dt.record_count) > 0)
    .map((dt) => ({
      viewId: viewIdFor(dt.doc_type_id),
      docTypeId: dt.doc_type_id,
      name: dt.name,
      columnCount: dt.fields.length,
      recordCount: stats[dt.doc_type_id]?.total ?? dt.record_count,
      lastExtractedAt: stats[dt.doc_type_id]?.last_extracted_at ?? null,
    }));
}

/** Pull every page of records for a doc_type (route caps limit at 1000). */
async function listAllRecords(docTypeId: string, ws: string): Promise<KbRecordDTO[]> {
  const out: KbRecordDTO[] = [];
  let offset = 0;
  for (;;) {
    const { items, total } = await listRecords(docTypeId, ws, { limit: PAGE, offset });
    out.push(...items);
    offset += items.length;
    if (offset >= total || items.length === 0) return out;
  }
}

/**
 * Filename join across BOTH workspace(space) and team scopes — team files can be
 * bound to a doc_type and produce records (S5), a single-scope pull would
 * tombstone them as missing (tech-design-m1 review P1-1).
 */
async function loadFileNameMap(): Promise<Map<string, string>> {
  const map = new Map<string, string>();
  for (const scope of ["space", "team"] as const) {
    // Per-scope failure isolation (impl review FE P2-2): an team-scope 403
    // (grants enforce mode) must degrade those rows to the missing-tombstone,
    // not reject the whole snapshot.
    try {
      let offset = 0;
      for (;;) {
        const res = await fetchKbFiles({ scope, offset, limit: 200 });
        for (const f of res.files) map.set(f.file_id, f.filename);
        offset += res.files.length;
        if (!res.has_more || res.files.length === 0) break;
      }
    } catch {
      // skip this scope—rows referencing its files render as “文件不可见或已删除”.
    }
  }
  return map;
}

/** raw_string residual sentinel ⇒ column-type mismatch (produced only by currency/number). */
function isRawStringSentinel(variants: Variant[] | undefined): boolean {
  if (!variants || variants.length === 0) return false;
  const primary = variants.find((v) => v.is_primary) ?? variants[0];
  const norm = primary.normalized;
  return !!norm && "raw_string" in norm && !("amount" in norm) && !("value" in norm);
}

export function buildCell(rec: KbRecordDTO, fname: string, extractedAt: string): MatrixCellVM | undefined {
  const variants = rec.fields?.[fname];
  const pf = getPrimaryField(rec, fname);
  if (!variants || variants.length === 0 || pf === undefined) return undefined;
  const value = pf.value === undefined || pf.value === null ? "" : String(pf.value);
  const status = isRawStringSentinel(variants) ? "needs_review" : "done";
  return {
    status,
    value,
    variants,
    sourceExcerpt: pf.source_snippet || undefined,
    chunkId: pf.chunk_id,
    page: pf.source_page,
    confidence: pf.confidence,
    extractedAt,
    edited: rec.edited_by_user.includes(fname),
    // Decision 2: only done cells in approved rows get ✓; needs_review cells never pretend to be verified (FE P3-1).
    verified: status === "done" && rec.verify_status === "approved",
  };
}

export async function loadTemplateSnapshot(
  workspaceId: string,
  docType: KbDocTypeDTO,
): Promise<MatrixSnapshot> {
  const [records, fileNames] = await Promise.all([
    listAllRecords(docType.doc_type_id, workspaceId),
    loadFileNameMap(),
  ]);
  const rows: MatrixRowVM[] = records.map((rec) => {
    const failed = rec.verify_status === "extract_failed";
    const cells: Record<string, MatrixCellVM | undefined> = {};
    if (!failed) {
      for (const f of docType.fields) cells[f.name] = buildCell(rec, f.name, rec.extracted_at);
    }
    return {
      fileId: rec.file_id,
      fileName: fileNames.get(rec.file_id) ?? "",
      missing: !fileNames.has(rec.file_id),
      extractFailed: failed,
      recordId: rec.record_id,
      approved: rec.verify_status === "approved",
      cells,
    };
  });
  return {
    matrixId: viewIdFor(docType.doc_type_id),
    title: docType.name,
    readOnly: true,
    columns: docType.fields.map((f) => ({
      columnId: f.name, // M1: column identity == field name (records key by name)
      name: f.name,
      type: f.type,
      question: f.description || undefined,
    })),
    rows,
    seq: 0,
  };
}
