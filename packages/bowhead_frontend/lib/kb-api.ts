import { appFetch as fetch, appPath } from "@/lib/base-path";

/**
 * V7 Knowledge Base API client.
 *
 * Backend: /api/v1/kb/*
 * Mirrors backend Pydantic schemas in packages/bowhead_core/src/bowhead_core/kb/schemas.py
 * - KbFileDTO: P0-P1 includes workspace_id / team_id (round 4 fix)
 * - KbSearchRequest / KbSearchResult / KbSearchResultChunk
 * - KbFileScopePatchRequest: P1-N4 admin override requires reason ≥ 10 char
 */

import { getUserSettings, isMultipartUploadRequired, setUserSetting } from "./api";
import { AuthError } from "./auth-api";
import {
  ApiError,
  readApiError,
  readApiErrorBody,
} from "./api-client";
import { genericUserFacingErrorMessage } from "./user-facing-error";

// ─── types ───────────────────────────────────────────────

// Batch 3/c1 (D3): canonical='space' (BE API accepts legacy 'workspace' for one version).
export type KbScope = "personal" | "space" | "team";

export type KbParseStatus =
  | "pending"
  | "parsing"
  | "chunking"
  | "embedding"
  | "done"
  | "failed"
  // presign upload flow (backend KbFileDTO.parse_status) — FE type was missing these.
  | "awaiting_upload"
  | "queued";

export interface KbSourceHealth {
  state: "eligible" | "warning" | "paused" | "unchecked";
  reason: string;
  checked_at: string | null;
}

export interface KbFileDTO {
  source_health?: KbSourceHealth | null;
  file_id: string;
  artifact_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  scope: KbScope;
  scope_id: string;
  workspace_id: string;
  team_id: string;
  uploaded_by: string;
  uploaded_by_name?: string | null;
  uploaded_at: string;
  parse_status: KbParseStatus;
  parse_progress_pct: number | null;
  parse_layer: "L0" | "L1" | "L2" | null;
  parse_error: string | null;
  /** User-facing failure-reason code (backend FR1); null without failure. Absent from old backend. */
  parse_error_code?: string | null;
  /** In progress with no executor remaining (backend FR1 stuck detection); frontend displays failure and permits retry. */
  parse_stuck?: boolean;
  parse_duration_ms: number | null;
  chunks_count: number;
  cite_count: number;
  last_cited_at: string | null;
  use_llm_marker: boolean;
  doc_type_id: string | null;
  // Sprint 2 Phase A (kb-structured-tables): classifier writes here; rollback-safe.
  auto_classified_doc_type_id?: string | null;
  // Sprint 25 kb-per-row-extract: cardinality=many per-row extract status.
  // enum: na/queued/running/succeeded/failed/cancelled. Pill state derivation.
  extracted_rows_status?: "na" | "queued" | "running" | "succeeded" | "failed" | "cancelled" | null;
  summary_json: string | null;
  /** Table exceeds indexed-row limit: actual knowledge-base indexed count and total; null / absent without truncation. */
  truncated_rows?: { indexed: number; total: number } | null;
  version: number;
  soft_deleted_at: string | null;
  can_delete: boolean;
  can_change_scope: boolean;
}

export interface KbFileListResponse {
  files: KbFileDTO[];
  total: number;       // post-filter full count (kb-management REQ-1.1) — NOT page size
  has_more?: boolean;  // more rows beyond this page
  /** Opaque keyset cursor for the next page; null on the last page. Absent from old backends. */
  next_cursor?: string | null;
}

/** File-list scope filter: one scope, or "all" = every scope the caller can read. */
export type KbListScope = KbScope | "all";

export const KB_LIST_SCOPES: readonly KbListScope[] = ["all", "personal", "space", "team"];

// kb-management REQ-1.1/1.2: paginated + filtered list params.
export interface KbFileListParams {
  scope: KbListScope;
  /** Keyset cursor from `next_cursor`; replaces `offset` and must keep the same sort. */
  cursor?: string;
  offset?: number;
  limit?: number;
  status?: string;          // comma-separated parse_status values
  mime?: string;            // comma-separated mime prefixes
  doc_type_id?: string;
  uploaded_after?: string;  // ISO
  uploaded_before?: string;
  q?: string;               // filename substring
  order_by?: "uploaded_at" | "filename" | "size_bytes";
  order_dir?: "asc" | "desc";
}

// kb-management REQ-1.4: bulk op per-item result envelope.
export interface KbBulkItemResult {
  file_id: string;
  ok: boolean;
  /** Deprecated server compatibility field; never render this value. */
  error: string | null;
  error_code?: string | null;
}
export interface KbBulkResponse {
  results: KbBulkItemResult[];
  succeeded: number;
  failed: number;
}

export interface KbSearchResultChunk {
  chunk_id: string;
  content: string;
  file_id: string;
  file_name: string;
  page: number | null;
  section_title: string | null;
  role: string;
  score: number;
  scope: string;
  generation_id?: string | null;
  source_warning?: string | null;
  /** 1-based segment number within file; absent in old chunks or backend. */
  chunk_index?: number | null;
}

export interface KbSearchResult {
  chunks: KbSearchResultChunk[];
  total_candidates: number;
  partial?: boolean;
  rerank_used: boolean;
  latency_ms: number;
}

// ─── helpers ─────────────────────────────────────────────

function getWorkspaceHeader(): HeadersInit {
  // layout-redesign P5 workspace-id consolidation: read the canonical active-workspace key
  // Active space id from space-storage.ts (written by the SpacePicker /
  // useSpace, same key kb-knowledge-tables-api uses).
  // The old sessionStorage "bowhead_workspace_id" key is NEVER written, so these
  // KB reads silently ignored the selected workspace and fell back to team_id.
  if (typeof window === "undefined") return {};
  // optional-chain the read: some test envs expose a window.localStorage stub
  // whose getItem isn't a real function (node --localstorage-file) → would throw.
  // Batch 3/c2: consolidate into space-storage (read new with legacy fallback + dual headers).
  return spaceHeader();
}

async function jsonOrThrow<T>(r: Response, _what: string): Promise<T> {
  if (!r.ok) {
    throw await readApiError(r);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

// ─── endpoints ───────────────────────────────────────────

export async function fetchKbFiles(opts: KbFileListParams): Promise<KbFileListResponse> {
  const qs = new URLSearchParams({ scope: opts.scope });
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.status) qs.set("status", opts.status);
  if (opts.mime) qs.set("mime", opts.mime);
  if (opts.doc_type_id) qs.set("doc_type_id", opts.doc_type_id);
  if (opts.uploaded_after) qs.set("uploaded_after", opts.uploaded_after);
  if (opts.uploaded_before) qs.set("uploaded_before", opts.uploaded_before);
  if (opts.q) qs.set("q", opts.q);
  if (opts.order_by) qs.set("order_by", opts.order_by);
  if (opts.order_dir) qs.set("order_dir", opts.order_dir);
  const r = await fetch(`/api/v1/kb/files?${qs.toString()}`, {
    headers: { ...getWorkspaceHeader() },
  });
  return jsonOrThrow<KbFileListResponse>(r, "fetchKbFiles");
}

/** Files from every scope the caller can read (personal, current space, team); each item keeps its `scope`. */
export function fetchAllScopeKbFiles(opts: Omit<KbFileListParams, "scope"> = {}): Promise<KbFileListResponse> {
  return fetchKbFiles({ ...opts, scope: "all" });
}

/** Per-user setting that remembers the last file-list scope filter (follows the account across browsers). */
export const KB_FILE_SCOPE_SETTING = "kb_file_scope";

export function parseKbListScope(value: unknown): KbListScope | null {
  return typeof value === "string" && (KB_LIST_SCOPES as readonly string[]).includes(value) ? (value as KbListScope) : null;
}

export async function loadKbFileScopePreference(): Promise<KbListScope | null> {
  return parseKbListScope((await getUserSettings())[KB_FILE_SCOPE_SETTING]);
}

export function saveKbFileScopePreference(scope: KbListScope): Promise<void> {
  return setUserSetting(KB_FILE_SCOPE_SETTING, scope);
}

export async function getKbFile(file_id: string): Promise<KbFileDTO> {
  const r = await fetch(`/api/v1/kb/files/${encodeURIComponent(file_id)}`, {
    headers: { ...getWorkspaceHeader() },
  });
  return jsonOrThrow<KbFileDTO>(r, "getKbFile");
}

export interface KbGoogleImportResult {
  file_id: string;
  name: string | null;
  ok: boolean;
  kb_file_id?: string;
  error?: string;
}

/** Import Picker-selected Google Drive files into the knowledge base as the current user. */
export async function importKbFromGoogleDrive(
  fileIds: string[],
  opts: { scope: KbScope; scope_id: string; use_llm?: boolean },
): Promise<KbGoogleImportResult[]> {
  const r = await fetch("/api/v1/google/kb-import", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getWorkspaceHeader() },
    body: JSON.stringify({ file_ids: fileIds, scope: opts.scope, scope_id: opts.scope_id, use_llm: Boolean(opts.use_llm) }),
  });
  const payload = await jsonOrThrow<{ results: KbGoogleImportResult[] }>(r, "importKbFromGoogleDrive");
  return payload.results;
}

export interface KbHealthObservation {
  file_id: string;
  file_version: number;
  checked_at: string;
  source_health: "present" | "confirmed_absent" | "temporarily_unavailable" | "unchecked";
  source_reason: string;
  index: { state: "unknown" | "processing" | "missing_or_corrupt"; reason: string; observed_chunks: number | null; count_limited: boolean };
  binding_state: "unverified" | "verified";
  evidence_state: "unknown";
  eligibility_enforced: boolean;
  use_state?: "eligible" | "warning" | "paused" | null;
  use_reason?: string | null;
  generation_id?: string | null;
  primary_action?: "check" | "restore" | "rebuild";
}

export async function getKbFileHealth(fileId: string, verify = false): Promise<KbHealthObservation> {
  const response = await fetch(`/api/v1/kb/files/${encodeURIComponent(fileId)}/${verify ? "verify" : "health"}`, { method: verify ? "POST" : "GET", headers: getWorkspaceHeader(), cache: "no-store" });
  return jsonOrThrow<KbHealthObservation>(response, "getKbFileHealth");
}

export async function restoreKbSource(fileId: string, file: File): Promise<{ outcome: "same_content_restored" | "different_content_requires_new_file" }> {
  const body = new FormData();
  body.append("upload", file);
  const response = await fetch(`/api/v1/kb/files/${encodeURIComponent(fileId)}/restore-source`, { method: "POST", headers: getWorkspaceHeader(), body });
  return jsonOrThrow(response, "restoreKbSource");
}

export async function rebuildKbSource(fileId: string): Promise<KbFileDTO> {
  const response = await fetch(`/api/v1/kb/files/${encodeURIComponent(fileId)}/rebuild`, { method: "POST", headers: getWorkspaceHeader() });
  return jsonOrThrow(response, "rebuildKbSource");
}

import { PRESIGN_THRESHOLD, type PresignUploadEnvelope } from "./api";
import { UploadCutOffError, watchUploadTransfer } from "./upload-xhr";
import { spaceHeader } from "@/lib/space-storage";

// MIRRORS: packages/bowhead_gateway/.../routes/kb_presign.py
//   KbPresignRequest / KbPresignResponse / KbConfirmRequest /
//   KbBundlePresignRequest / KbBundlePresignResponse / KbBundleConfirmRequest
export interface KbPresignResponse {
  file_id: string;
  artifact_id: string;
  upload: PresignUploadEnvelope;
  expires_at: string;
  confirm_url: string;
}

export interface KbBundlePresignResponse {
  file_id: string;       // bundle marker kbf_*
  artifact_id: string;   // bundle staging art_*
  upload: PresignUploadEnvelope;
  expires_at: string;
  confirm_url: string;
}

export interface KbUploadOptions {
  /** Optional immutable caller context; generic KB callers keep their existing defaults. */
  spaceHeaders?: Record<string, string>;
  use_llm?: boolean;
  onProgress?: (pct: number) => void;
  signal?: AbortSignal;
}

export async function uploadKbFile(
  file: File,
  opts: {
    scope: KbScope;
    scope_id: string;
    use_llm: boolean;
    doc_type_id?: string | null;
  } & KbUploadOptions,
): Promise<KbFileDTO> {
  if (file.size < PRESIGN_THRESHOLD) {
    return uploadKbSmall(file, opts);
  }
  return uploadLargeKbFile(file, opts);
}

/**
 * Multipart POST to a KB endpoint via XHR (not fetch) so the common small-file
 * path can report real upload progress — fetch() has no upload progress event.
 * Sets the workspace header, supports abort, and preserves the structured
 * error code (401 stays AuthError for compatibility; other failures use
 * ApiError). Returns the parsed JSON body.
 */
function xhrPostFormJson<T>(url: string, fd: FormData, _what: string, opts: KbUploadOptions): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", appPath(url));
    for (const [k, v] of Object.entries(opts.spaceHeaders ?? getWorkspaceHeader())) xhr.setRequestHeader(k, v);
    // FR5.2: transfer progress distinguishes disconnection before body upload completes from request never sent.
    const cutOff = watchUploadTransfer(xhr, opts.onProgress);
    const onAbort = () => xhr.abort();
    const cleanup = () => opts.signal?.removeEventListener("abort", onAbort);
    xhr.onload = () => {
      cleanup();
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
        return;
      }
      const parsed = readApiErrorBody(
        body,
        genericUserFacingErrorMessage(),
      );
      reject(
        xhr.status === 401
          ? new AuthError(401, parsed.message, parsed.code)
          : new ApiError(xhr.status, parsed.message, {
            body,
            code: parsed.code,
          }),
      );
    };
    xhr.onerror = () => {
      cleanup();
      reject(cutOff() ? new UploadCutOffError() : new ApiError(0, genericUserFacingErrorMessage()));
    };
    xhr.ontimeout = () => {
      cleanup();
      reject(new ApiError(0, genericUserFacingErrorMessage()));
    };
    // xhr.abort() fires `abort`, not `error` — without this a cancelled upload never settles.
    xhr.onabort = () => {
      cleanup();
      reject(new ApiError(0, genericUserFacingErrorMessage()));
    };
    if (opts.signal) {
      if (opts.signal.aborted) {
        reject(new ApiError(0, genericUserFacingErrorMessage()));
        return;
      }
      opts.signal.addEventListener("abort", onAbort);
    }
    xhr.send(fd);
  });
}

async function uploadKbSmall(
  file: File,
  opts: {
    scope: KbScope;
    scope_id: string;
    use_llm: boolean;
    doc_type_id?: string | null;
  } & KbUploadOptions,
): Promise<KbFileDTO> {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("scope", opts.scope);
  fd.append("scope_id", opts.scope_id);
  fd.append("use_llm", String(opts.use_llm));
  if (opts.doc_type_id) fd.append("doc_type_id", opts.doc_type_id);
  return xhrPostFormJson<KbFileDTO>("/api/v1/kb/files", fd, "uploadKbFile", opts);
}

/** Sprint file-upload-presign Phase 2 — KB single doc presign path. */
export async function uploadLargeKbFile(
  file: File,
  opts: {
    scope: KbScope;
    scope_id: string;
    use_llm: boolean;
    doc_type_id?: string | null;
  } & KbUploadOptions,
): Promise<KbFileDTO> {
  opts = { ...opts, spaceHeaders: { ...(opts.spaceHeaders ?? spaceHeader()) } };
  const {
    sha256ChunkedHex, uploadViaEnvelopeWithRetry, PresignExpiredError,
    ConfirmExpiredError, sleep,
  } = await import("./upload-xhr");
  const checksum = await sha256ChunkedHex(file);

  let attempt = 0;
  for (;;) {
    opts.signal?.throwIfAborted();
    const r = await fetch("/api/v1/kb/presign", {
      method: "POST",
      signal: opts.signal,
      headers: { "Content-Type": "application/json", ...opts.spaceHeaders },
      body: JSON.stringify({
        filename: file.name,
        mime: file.type || "application/octet-stream",
        size_bytes: file.size,
        scope: opts.scope,
        scope_id: opts.scope_id,
        use_llm: opts.use_llm ?? false,
        doc_type_id: opts.doc_type_id ?? null,
        checksum_sha256: checksum || null,
      }),
    });
    let presign: KbPresignResponse;
    try {
      presign = await jsonOrThrow<KbPresignResponse>(r, "uploadLargeKbFile.presign");
    } catch (error) {
      if (isMultipartUploadRequired(error)) return uploadKbSmall(file, opts);
      throw error;
    }

    try {
      await uploadViaEnvelopeWithRetry(presign.upload, file, {
        onProgress: opts.onProgress,
        signal: opts.signal,
      });
    } catch (e) {
      if (e instanceof PresignExpiredError && attempt < 1) {
        attempt++;
        continue;
      }
      throw e;
    }

    // Confirm with 409 'confirming' retry-once + 410 expired → re-presign
    let confirmAttempt = 0;
    let confirmExpired = false;
    for (;;) {
      opts.signal?.throwIfAborted();
      const cr = await fetch(presign.confirm_url, {
        method: "POST",
        signal: opts.signal,
        headers: { "Content-Type": "application/json", ...opts.spaceHeaders },
        body: JSON.stringify({
          uploaded_size: file.size,
          checksum_sha256: checksum || null,
        }),
      });
      if (cr.ok) return cr.json();
      if (cr.status === 409 && confirmAttempt < 1) {
        confirmAttempt++;
        await sleep(1000);
        continue;
      }
      // Phase 2 review P1-1: 410 → outer loop re-presigns once
      if (cr.status === 410 && attempt < 1) {
        confirmExpired = true;
        break;
      }
      // Surface 404/415/503 etc. to caller
      return jsonOrThrow<KbFileDTO>(cr, "uploadLargeKbFile.confirm");
    }
    if (confirmExpired) {
      attempt++;
      continue;
    }
  }
}

/** 0.24.4 (kb-zip-bundle): upload a .zip → server unzips → per-file ingest.
 * Response includes uploaded DTOs + skipped list (garbage / unsupported MIME). */
export interface KbBundleUploadResponse {
  uploaded: KbFileDTO[];
  skipped: Array<{
    filename: string;
    /** Deprecated compatibility field; backend always returns null. */
    reason: null;
    reason_code: string;
  }>;
  total_entries: number;
  bundle_filename: string;
  bundle_artifact_id: string;
  /** Exact persisted root metadata; optional for older Gateway responses. */
  bundle_mime?: string;
  bundle_size?: number;
  /** skill-import batch 1b: whole-package inventory (meaningful for capability-shape components); absent from old backend. */
  inventory?: Record<string, unknown> | null;
  /** skill-import batch 1b: per-skill personal-scope landing results (name/path/status/import_status). */
  landed_skills?: Array<{
    name: string;
    path: string;
    status: "landed" | "exists" | "denied" | "invalid";
    import_status: string | null;
    code?: string;
  }>;
}

export async function uploadKbBundle(
  file: File,
  opts: { scope: KbScope; scope_id: string; use_llm: boolean } & KbUploadOptions,
): Promise<KbBundleUploadResponse> {
  opts = { ...opts, spaceHeaders: { ...(opts.spaceHeaders ?? spaceHeader()) } };
  if (file.size < PRESIGN_THRESHOLD) {
    return uploadKbBundleSmall(file, opts);
  }
  return uploadLargeKbBundle(file, opts);
}

async function uploadKbBundleSmall(
  file: File,
  opts: { scope: KbScope; scope_id: string; use_llm: boolean } & KbUploadOptions,
): Promise<KbBundleUploadResponse> {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("scope", opts.scope);
  fd.append("scope_id", opts.scope_id);
  fd.append("use_llm", String(opts.use_llm));
  return xhrPostFormJson<KbBundleUploadResponse>("/api/v1/kb/files/bundle", fd, "uploadKbBundle", opts);
}

/** Sprint file-upload-presign Phase 2 — KB zip bundle presign path. */
export async function uploadLargeKbBundle(
  file: File,
  opts: { scope: KbScope; scope_id: string; use_llm: boolean } & KbUploadOptions,
): Promise<KbBundleUploadResponse> {
  opts = { ...opts, spaceHeaders: { ...(opts.spaceHeaders ?? spaceHeader()) } };
  const {
    sha256ChunkedHex, uploadViaEnvelopeWithRetry, PresignExpiredError,
  } = await import("./upload-xhr");
  const checksum = await sha256ChunkedHex(file);

  let attempt = 0;
  for (;;) {
    opts.signal?.throwIfAborted();
    const r = await fetch("/api/v1/kb/presign/bundle", {
      method: "POST",
      signal: opts.signal,
      headers: { "Content-Type": "application/json", ...opts.spaceHeaders },
      body: JSON.stringify({
        filename: file.name,
        mime: file.type || "application/zip",
        size_bytes: file.size,
        scope: opts.scope,
        scope_id: opts.scope_id,
        use_llm: opts.use_llm,
        checksum_sha256: checksum || null,
      }),
    });
    let presign: KbBundlePresignResponse;
    try {
      presign = await jsonOrThrow<KbBundlePresignResponse>(r, "uploadLargeKbBundle.presign");
    } catch (error) {
      if (isMultipartUploadRequired(error)) return uploadKbBundleSmall(file, opts);
      throw error;
    }

    try {
      await uploadViaEnvelopeWithRetry(presign.upload, file, {
        onProgress: opts.onProgress,
        signal: opts.signal,
      });
    } catch (e) {
      if (e instanceof PresignExpiredError && attempt < 1) {
        attempt++;
        continue;
      }
      throw e;
    }

    // Bundle confirm does NOT support 'confirming' retry — decompose is
    // expensive, so 409 is terminal per backend (kb_presign.py:267).
    const cr = await fetch(presign.confirm_url, {
      method: "POST",
      signal: opts.signal,
      headers: { "Content-Type": "application/json", ...opts.spaceHeaders },
      body: JSON.stringify({
        uploaded_size: file.size,
        checksum_sha256: checksum || null,
      }),
    });
    // Phase 2 review P1-1: 410 → outer loop re-presigns once
    if (cr.status === 410 && attempt < 1) {
      attempt++;
      continue;
    }
    return jsonOrThrow<KbBundleUploadResponse>(cr, "uploadLargeKbBundle.confirm");
  }
}

export async function deleteKbFile(file_id: string): Promise<void> {
  const r = await fetch(`/api/v1/kb/files/${encodeURIComponent(file_id)}`, {
    method: "DELETE",
    headers: { ...getWorkspaceHeader() },
  });
  return jsonOrThrow<void>(r, "deleteKbFile");
}

export async function changeKbFileScope(
  file_id: string,
  body: { new_scope: KbScope; new_scope_id: string; reason?: string },
): Promise<KbFileDTO> {
  const r = await fetch(`/api/v1/kb/files/${encodeURIComponent(file_id)}/scope`, {
    method: "PATCH",
    headers: { "content-type": "application/json", ...getWorkspaceHeader() },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<KbFileDTO>(r, "changeKbFileScope");
}

/** kb-management REQ-2.1: retry a failed parse. */
export async function reparseKbFile(file_id: string): Promise<KbFileDTO> {
  const r = await fetch(`/api/v1/kb/files/${encodeURIComponent(file_id)}/reparse`, {
    method: "POST",
    headers: { ...getWorkspaceHeader() },
  });
  return jsonOrThrow<KbFileDTO>(r, "reparseKbFile");
}

export interface KbExtractRowsResponse {
  job_id?: string;
  status?: string;
}

/** Retry row extraction through the same typed error parser as other KB calls. */
export async function extractRowsKbFile(
  file_id: string,
): Promise<KbExtractRowsResponse> {
  const r = await fetch(
    `/api/v1/kb/files/${encodeURIComponent(file_id)}/extract_rows`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...getWorkspaceHeader(),
      },
      body: JSON.stringify({ force: true }),
    },
  );
  return jsonOrThrow<KbExtractRowsResponse>(r, "extractRowsKbFile");
}

export interface KbExtractRowsJobStatus {
  job_id: string;
  status: string;
  total_batches: number;
  completed_batches: number;
  rows_extracted: number;
}

/** Batch polling for row-extraction task status. Endpoint authorizes against current space (missing space header 400, nonadmin 403),
 *  so space header required; errors throw ApiError, caller decides whether to stop polling. */
export async function fetchExtractRowsStatusBatch(
  file_ids: string[],
): Promise<{ jobs: Record<string, KbExtractRowsJobStatus> }> {
  const qs = new URLSearchParams({ file_ids: file_ids.join(",") });
  const r = await fetch(`/api/v1/kb/files/_batch/extract_rows_status?${qs.toString()}`, {
    headers: { ...getWorkspaceHeader() },
  });
  return jsonOrThrow<{ jobs: Record<string, KbExtractRowsJobStatus> }>(r, "fetchExtractRowsStatusBatch");
}

/** kb-management REQ-1.4b: bind/unbind a doc_type to one file (doc_type_id=null unbinds). */
export async function bindKbFileDocType(
  file_id: string,
  doc_type_id: string | null,
): Promise<KbFileDTO> {
  const r = await fetch(`/api/v1/kb/files/${encodeURIComponent(file_id)}/doc_type`, {
    method: "PATCH",
    headers: { "content-type": "application/json", ...getWorkspaceHeader() },
    body: JSON.stringify({ doc_type_id }),
  });
  return jsonOrThrow<KbFileDTO>(r, "bindKbFileDocType");
}

/** kb-management REQ-1.4: bulk soft-delete. Partial failure → per-item ok=false. */
export async function bulkDeleteKbFiles(file_ids: string[]): Promise<KbBulkResponse> {
  const r = await fetch("/api/v1/kb/files/bulk_delete", {
    method: "POST",
    headers: { "content-type": "application/json", ...getWorkspaceHeader() },
    body: JSON.stringify({ file_ids }),
  });
  return jsonOrThrow<KbBulkResponse>(r, "bulkDeleteKbFiles");
}

/** kb-management REQ-1.4: bulk scope change. */
export async function bulkChangeKbFilesScope(
  file_ids: string[],
  body: { new_scope: KbScope; new_scope_id: string; reason?: string },
): Promise<KbBulkResponse> {
  const r = await fetch("/api/v1/kb/files/bulk_scope", {
    method: "POST",
    headers: { "content-type": "application/json", ...getWorkspaceHeader() },
    body: JSON.stringify({ file_ids, ...body }),
  });
  return jsonOrThrow<KbBulkResponse>(r, "bulkChangeKbFilesScope");
}

/** kb-management REQ-1.4b + D5: bulk bind/unbind doc_type. Binding also triggers
 *  extraction server-side (rate-limited, async); unbind (null) does not. */
export async function bulkBindKbFilesDocType(
  file_ids: string[],
  doc_type_id: string | null,
): Promise<KbBulkResponse> {
  const r = await fetch("/api/v1/kb/files/bulk_doc_type", {
    method: "POST",
    headers: { "content-type": "application/json", ...getWorkspaceHeader() },
    body: JSON.stringify({ file_ids, doc_type_id }),
  });
  return jsonOrThrow<KbBulkResponse>(r, "bulkBindKbFilesDocType");
}

export async function searchKb(
  query: string,
  opts: { top_k?: number; scope_filter?: KbScope[] } = {},
): Promise<KbSearchResult> {
  const r = await fetch("/api/v1/kb/search", {
    method: "POST",
    headers: { "content-type": "application/json", ...getWorkspaceHeader() },
    body: JSON.stringify({
      query,
      ...(opts.top_k !== undefined ? { top_k: opts.top_k } : {}),
      ...(opts.scope_filter ? { scope_filter: opts.scope_filter } : {}),
    }),
  });
  return jsonOrThrow<KbSearchResult>(r, "searchKb");
}

export function downloadKbFileUrl(file_id: string): string {
  return `/api/v1/kb/files/${encodeURIComponent(file_id)}/download`;
}

/** P1-12-1 fix: fetch with space headers (both X-Space-Id+X-Workspace-Id) + blob download (browser <a download> sends no headers). */
export async function downloadKbFileBlob(file_id: string, filename: string): Promise<void> {
  const r = await fetch(downloadKbFileUrl(file_id), {
    headers: { ...getWorkspaceHeader() },
  });
  if (!r.ok) throw await readApiError(r);
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** P1-12-2 fix: fetch chunks list for drawer. */
export interface KbChunkSummary {
  chunk_id: string;
  content: string;
  page: number | null;
  section_title: string | null;
  role: string;
  chunk_index: number;
  generation_id?: string | null;
  historical_generation?: boolean;
  current_source_state?: string | null;
}

/** `spaceId`: file's space (chat KB citation supplies `kb_space_id`), allowing access when viewer currently
 *  uses another space; omitted uses current space. */
export async function fetchKbFileChunks(
  file_id: string,
  spaceId?: string,
): Promise<{ chunks: KbChunkSummary[]; total: number }> {
  const r = await fetch(`/api/v1/kb/files/${encodeURIComponent(file_id)}/chunks`, { headers: chunkHeaders(spaceId) });
  return jsonOrThrow<{ chunks: KbChunkSummary[]; total: number }>(r, "fetchKbFileChunks");
}

/** Fetch one chunk only (chat KB source card): `?chunk_id=` makes new backend return only it; old backend ignores parameter,
 *  returns full list; both search response by id, returning null if missing (chunk replaced during reprocessing). */
export async function fetchKbFileChunk(
  file_id: string,
  chunk_id: string,
  spaceId?: string,
): Promise<KbChunkSummary | null> {
  const qs = new URLSearchParams({ chunk_id });
  const r = await fetch(`/api/v1/kb/files/${encodeURIComponent(file_id)}/chunks?${qs.toString()}`, {
    headers: chunkHeaders(spaceId),
  });
  if (r.status === 404) {
    // File exists but chunk no longer does (file reprocessed) ⇒ same as absent from list; source card shows “原文已更新”.
    // Other 404s (file itself absent / no permission) remain read failures.
    const err = await readApiError(r);
    if (err.code === "kb_chunk_not_found") return null;
    throw err;
  }
  const res = await jsonOrThrow<{ chunks: KbChunkSummary[]; total: number }>(r, "fetchKbFileChunk");
  return (Array.isArray(res.chunks) ? res.chunks : []).find((c) => c.chunk_id === chunk_id) ?? null;
}

/** Chunk-read space headers: `spaceId` (file’s space) takes priority over current space. */
function chunkHeaders(spaceId?: string): Record<string, string> {
  const headers: Record<string, string> = { ...(getWorkspaceHeader() as Record<string, string>) };
  if (spaceId) {
    headers["X-Space-Id"] = spaceId;
    headers["X-Workspace-Id"] = spaceId;
  }
  return headers;
}

// ─── status helpers ──────────────────────────────────────

export function isParseInProgress(status: KbParseStatus): boolean {
  return status === "pending" || status === "parsing" || status === "chunking" || status === "embedding";
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export interface KbRebuildProgress {
  job_id: string;
  state: "running" | "cancelling" | "paused" | "completed" | "cancelled";
  total: number;
  processed: number;
  failed: number;
  changed: number;
}
export interface KbRebuildPreview { files: number; size_bytes: number; job_id?: string }
export async function previewKbRebuild(fileIds: string[], apply = false): Promise<KbRebuildPreview> {
  const r = await fetch("/api/v1/kb/maintenance/rebuilds", { method: "POST", headers: { ...getWorkspaceHeader(), "Content-Type": "application/json" }, body: JSON.stringify({ file_ids: fileIds, apply, accept_model_cost: apply }) });
  return jsonOrThrow(r, "previewKbRebuild");
}
export async function fetchKbRebuild(jobId: string): Promise<KbRebuildProgress> {
  return jsonOrThrow(await fetch(`/api/v1/kb/maintenance/rebuilds/${encodeURIComponent(jobId)}`, { headers: getWorkspaceHeader(), cache: "no-store" }), "fetchKbRebuild");
}
export async function controlKbRebuild(jobId: string, action: "resume" | "cancel"): Promise<void> {
  await jsonOrThrow(await fetch(`/api/v1/kb/maintenance/rebuilds/${encodeURIComponent(jobId)}/${action}`, { method: "POST", headers: getWorkspaceHeader() }), "controlKbRebuild");
}

export async function latestKbRebuild(): Promise<string | null> {
  const r = await fetch("/api/v1/kb/maintenance/rebuilds", { headers: getWorkspaceHeader(), cache: "no-store" });
  const body = await jsonOrThrow<{ job_id?: string | null }>(r, "latestKbRebuild");
  return body.job_id ?? null;
}
