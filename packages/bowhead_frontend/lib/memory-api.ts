import { appFetch as fetch, appPath } from "@/lib/base-path";
import { responseRequestId } from "./error-presentation";
import { randomUuid } from "./random-id";

/**
 * V3 PRD-B · Memory page API wrapper.
 *
 * Backend: /api/v1/memory/* (snapshot / trash / restore / reset / export /
 * dreaming pause / candidates).
 * Spike #15: reuse `remember(kind, key, value)` from ./api.ts; do not redeclare.
 */

// ─── types ───────────────────────────────────────────────

export type MemoryOrigin = "memory";

export interface UnifiedMemoryItem {
  id: string;
  origin: MemoryOrigin;
  kind: string;            // preference | constraint | datarc_binding
  key: string;
  value: unknown;
  locked: boolean;
  lock_reason: string | null;
  source: string;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  workspace_id: string | null;  // legacy
  scope: string | null;          // legacy
  language: string | null;       // legacy
}

export interface MemoryStats {
  active_count: number;
  trash_count: number;
  pending_candidates: number;
}

export interface DreamingState {
  paused: boolean;
}

export interface MemorySnapshot {
  user_id: string;
  items: UnifiedMemoryItem[];
  stats: MemoryStats;
  dreaming: DreamingState;
  notes?: MemoryNote | null;  // V3.5 PRD-C; absent on V3 backends
}

export interface MemoryCapabilitiesV2 {
  v2_enabled: true;
  v2_ready: boolean;
  presentation: "v2";
  schema_version: number;
  kind_policy_version: number;
  actions: {
    edit: boolean;
    permanent_delete: boolean;
    recent_resets: boolean;
    destructive_operations: boolean;
  };
  permanent_delete_ready: boolean;
  permanent_delete_reason: string | null;
  reason: string | null;
}

export interface CanonicalMemoryScope {
  type: "global" | "space";
  space_id: string | null;
  space_name: string | null;
  availability: "available" | "unavailable";
  label: string;
}

export interface CanonicalMemoryItem {
  id: string;
  resource_type:
    | "memory_entry"
    | "memory_note_recovery";
  display_type:
    | "preference"
    | "work_rule"
    | "background"
    | "data_binding";
  display_value: string;
  display_schema: {
    name: string;
    payload: Record<string, unknown>;
    editable_fields: string[];
  };
  scope: CanonicalMemoryScope;
  provenance: {
    created_from: {
      type: string;
      source_label: string;
      /** Stable key of the source line; the client renders it in the interface language (`memory_page.v2.source_<key>`). */
      source_label_key?: string | null;
      source_available: boolean;
      source_open_token: string | null;
    };
    last_modified: {
      actor_type: string | null;
      actor_id: string | null;
      modified_at: string | null;
    };
  };
  capabilities: {
    edit: boolean;
    soft_delete: boolean;
    restore: boolean;
    permanent_delete: boolean;
  };
  locked: boolean;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  purge_at: string | null;
  version: string;
}

export interface CanonicalMemoryItemsPage {
  items: CanonicalMemoryItem[];
  next_cursor: string | null;
}

export interface MemoryOverviewV2 {
  schema_version: number;
  about_you: {
    content: string;
    scope: CanonicalMemoryScope;
    version: string;
    updated_at: string | null;
    is_locked: boolean;
    capabilities: CanonicalMemoryItem["capabilities"];
  };
  suggestions: PendingCandidate[];
  stats: {
    notes_configured: boolean;
    saved_entries: number;
    pending_candidates: number;
    spaces_represented: number;
    trash_count: number;
    recoverable_resets: number;
  };
  learning: {
    automatic_learning: "active" | "paused";
  };
  capabilities: MemoryCapabilitiesV2;
}

export type MemoryAboutYouV2 = MemoryOverviewV2["about_you"];

export interface MemoryAboutYouEditSession {
  edit_token: string;
  version: string;
  expires_at: string;
}

export interface TrashList {
  user_id: string;
  items: UnifiedMemoryItem[];
}

export interface RestoreResult {
  id: string;
  restored: boolean;
}

export interface ResetMemoryResult {
  snapshot_id: string;
  affected_memory: number;
  affected_notes: number;
  counts: MemoryResetCounts;
  expires_at: string | null;
  undo_available: boolean;
}

export interface MemoryResetCounts {
  notes: number;
  entries: number;
  anchors: number;
  locked_skipped: number;
  pending_preserved: number;
}

export interface MemoryResetSummary {
  id: string;
  created_at: string;
  expires_at: string;
  counts: MemoryResetCounts;
  state_counts: Record<string, number>;
  remaining_recoverable: number;
  recoverable: boolean;
  reason: string | null;
}

export interface MemoryResetList {
  items: MemoryResetSummary[];
  next_cursor: string | null;
}

export interface MemoryResetRestoreResult {
  snapshot_id: string;
  status: "restored" | "partially_restored";
  restored_counts: Record<string, number>;
  conflicts: Array<{ resource_type: string; reason: string }>;
  unavailable_counts: Record<string, number>;
  remaining_recoverable: number;
}

export interface MemoryDestructiveOperation {
  operation_id: string;
  idempotency_key: string;
  action:
    | "permanent_delete_item"
    | "permanent_delete_trash"
    | "permanent_delete_reset"
    | "permanent_delete_all";
  target_id: string | null;
  status: "running" | "completed" | "failed" | "partially_failed";
  phase: string;
  cutoff_at: string;
  privacy_epoch: number;
  progress_current: number;
  progress_total: number;
  retryable: boolean;
  last_error_code: string | null;
  completed_at: string | null;
  counts: Record<string, number>;
  skipped_locked: number;
  dreaming_paused: boolean;
  partial_failures: Array<Record<string, unknown>>;
  logical_scrub_completed: boolean;
  physical_purge_completed: boolean;
  deletion_receipt_committed: boolean;
}

export interface PendingCandidate {
  id: string;
  userId: string;
  kind: string | null;
  key: string | null;
  value: unknown;
  reason: string | null;
  sourceTurnId: string | null;
  status: "pending" | "approved" | "rejected";
  decidedBy: string | null;
  decidedAt: string | null;
  createdAt: string;
  updatedAt: string;
  write_error?: string | null;
  source_space?: CandidateScope | null;
  source_turn?: CandidateSourceTurn | null;
  default_target?: CandidateScope | null;
  allowed_targets?: CandidateScope[];
  selected_target?: CandidateScope | null;
  display_value?: string | null;
  display_reason?: string | null;
  /** Memory id written after approval: chat-card Undo uses it to locate its target. */
  approved_entry_id?: string | null;
  /** Candidate pool of origin; only `feedback_draft` supports Revise. */
  origin?: string | null;
  expires_at?: string | null;
  canonical_state?: "pending" | "approved" | "rejected";
  capabilities?: CandidateCapabilities;
  version?: string;
}

export interface CandidateScope {
  type: "global" | "space";
  space_id: string | null;
  space_name: string | null;
  availability: "available" | "unavailable";
  label: string;
  requires_confirmation: boolean;
}

export interface CandidateSourceTurn {
  availability: "available" | "unavailable";
  label: string;
  source_open_token: string | null;
}

export interface CandidateCapabilities {
  approve: boolean;
  reject: boolean;
  reason: string | null;
}

export interface CandidateList {
  items: PendingCandidate[];
  scope: string;
}

export type CandidateStatusFilter = "pending" | "approved" | "rejected" | "all";

// ─── error ───────────────────────────────────────────────

export class MemoryAPIError extends Error {
  status: number;
  detail: string;
  payload: unknown;
  /** Server request ID, diagnostics only (Copy diagnostics). */
  requestId?: string;
  constructor(status: number, detail: string, payload?: unknown, requestId?: string) {
    super(detail || `memory_api_${status}`);
    this.status = status;
    this.detail = detail;
    this.payload = payload;
    this.requestId = requestId;
  }
}

async function _check(r: Response): Promise<Response> {
  if (r.ok) return r;
  let detail = `${r.status}`;
  let payload: unknown;
  try {
    const j = await r.json();
    payload = j;
    const rawDetail = j && (j.detail || j.message);
    detail = typeof rawDetail === "string"
      ? rawDetail
      : rawDetail?.code || detail;
  } catch {
    /* swallow */
  }
  throw new MemoryAPIError(r.status, detail, payload, responseRequestId(r));
}

export async function fetchMemoryOverview(): Promise<MemoryOverviewV2> {
  const r = await fetch("/api/v1/memory/overview", {
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
  return r.json();
}

export async function fetchCanonicalMemoryItems(opts: {
  state?: "active" | "trashed";
  query?: string;
  /**
   * Source-line keys whose label, in the interface language, contains `query`. Sent with every search so the
   * Gateway matches the source line the user sees instead of its Chinese `source_label`.
   */
  sourceLabelKeys?: readonly string[];
  displayType?: string | null;
  scopeType?: "global" | "space" | null;
  spaceId?: string | null;
  limit?: number;
  cursor?: string | null;
} = {}): Promise<CanonicalMemoryItemsPage> {
  const qs = new URLSearchParams({
    state: opts.state ?? "active",
    limit: String(opts.limit ?? 50),
  });
  if (opts.query) {
    qs.set("query", opts.query);
    if (opts.sourceLabelKeys) qs.set("source_label_keys", opts.sourceLabelKeys.join(","));
  }
  if (opts.displayType) qs.set("display_type", opts.displayType);
  if (opts.scopeType) qs.set("scope_type", opts.scopeType);
  if (opts.spaceId) qs.set("space_id", opts.spaceId);
  if (opts.cursor) qs.set("cursor", opts.cursor);
  const r = await fetch(`/api/v1/memory/items?${qs}`, {
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
  return r.json();
}

export async function patchCanonicalMemoryItem(
  id: string,
  input: {
    schema: string;
    patch: Record<string, unknown>;
    version: string;
  },
): Promise<CanonicalMemoryItem> {
  const r = await fetch(`/api/v1/memory/items/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    cache: "no-store",
    body: JSON.stringify(input),
  });
  await _check(r);
  return r.json();
}

export async function beginAboutYouEditSession(
  expectedVersion: string,
  editToken?: string,
): Promise<MemoryAboutYouEditSession> {
  const r = await fetch("/api/v1/memory/about-you/edit-session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    cache: "no-store",
    body: JSON.stringify({
      expected_version: expectedVersion,
      ...(editToken ? { edit_token: editToken } : {}),
    }),
  });
  await _check(r);
  return r.json();
}

export async function patchAboutYou(input: {
  content: string;
  version: string;
  editToken: string;
}): Promise<MemoryAboutYouV2> {
  const r = await fetch("/api/v1/memory/about-you", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    cache: "no-store",
    body: JSON.stringify({
      content: input.content,
      version: input.version,
      edit_token: input.editToken,
    }),
  });
  await _check(r);
  return r.json();
}

export async function deleteAboutYou(
  version: string,
): Promise<{
  deleted: string;
  recovery_id: string;
  purge_at: string;
  version: string;
}> {
  const r = await fetch("/api/v1/memory/about-you", {
    method: "DELETE",
    headers: { "If-Match": `"${version}"` },
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
  return r.json();
}

export async function releaseAboutYouEditSession(
  editToken: string,
): Promise<void> {
  const r = await fetch("/api/v1/memory/about-you/edit-session", {
    method: "DELETE",
    headers: { "X-Memory-Edit-Token": editToken },
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
}

// ─── snapshot / trash / mutations ────────────────────────

export async function fetchSnapshot(): Promise<MemorySnapshot> {
  const r = await fetch("/api/v1/memory/snapshot", { credentials: "include" });
  await _check(r);
  return r.json();
}

export async function fetchTrash(): Promise<TrashList> {
  const r = await fetch("/api/v1/memory/trash", { credentials: "include" });
  await _check(r);
  return r.json();
}

export async function deleteEntry(
  id: string,
  version?: string,
): Promise<{ deleted: string }> {
  const headers: Record<string, string> = {};
  if (version) headers["If-Match"] = `"${version}"`;
  const r = await fetch(`/api/v1/memory/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers,
    credentials: "include",
  });
  await _check(r);
  return r.json();
}

export async function restoreEntry(
  id: string,
  version?: string,
): Promise<RestoreResult> {
  const headers: Record<string, string> = {};
  if (version) headers["If-Match"] = `"${version}"`;
  const r = await fetch(
    `/api/v1/memory/${encodeURIComponent(id)}/restore`,
    {
      method: "POST",
      headers,
      credentials: "include",
    },
  );
  await _check(r);
  return r.json();
}

export function createMemoryIdempotencyKey(): string {
  return randomUuid();
}

export async function resetMemory(
  idempotencyKey = createMemoryIdempotencyKey(),
): Promise<ResetMemoryResult> {
  const r = await fetch("/api/v1/memory/reset", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
  return r.json();
}

export async function previewMemoryReset(): Promise<{
  counts: MemoryResetCounts;
}> {
  const r = await fetch("/api/v1/memory/reset-preview", {
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
  return r.json();
}

export async function listMemoryResets(): Promise<MemoryResetList> {
  const r = await fetch("/api/v1/memory/resets", {
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
  return r.json();
}

export async function restoreMemoryReset(
  snapshotId: string,
  idempotencyKey = createMemoryIdempotencyKey(),
): Promise<MemoryResetRestoreResult> {
  const r = await fetch(
    `/api/v1/memory/resets/${encodeURIComponent(snapshotId)}/restore`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      credentials: "include",
      cache: "no-store",
    },
  );
  await _check(r);
  return r.json();
}

async function permanentDelete(
  path: string,
  idempotencyKey: string,
): Promise<MemoryDestructiveOperation> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    credentials: "include",
    cache: "no-store",
  });
  await _check(r);
  return r.json();
}

export async function permanentDeleteMemoryItem(
  itemId: string,
  idempotencyKey = createMemoryIdempotencyKey(),
): Promise<MemoryDestructiveOperation> {
  return permanentDelete(
    `/api/v1/memory/items/${encodeURIComponent(itemId)}/permanent-delete`,
    idempotencyKey,
  );
}

export async function permanentlyClearMemoryTrash(
  idempotencyKey = createMemoryIdempotencyKey(),
): Promise<MemoryDestructiveOperation> {
  return permanentDelete(
    "/api/v1/memory/trash/permanent-delete",
    idempotencyKey,
  );
}

export async function permanentDeleteMemoryReset(
  snapshotId: string,
  idempotencyKey = createMemoryIdempotencyKey(),
): Promise<MemoryDestructiveOperation> {
  return permanentDelete(
    `/api/v1/memory/resets/${encodeURIComponent(snapshotId)}/permanent-delete`,
    idempotencyKey,
  );
}

export async function permanentDeleteAllMemory(
  idempotencyKey = createMemoryIdempotencyKey(),
): Promise<MemoryDestructiveOperation> {
  return permanentDelete(
    "/api/v1/memory/permanent-delete-all",
    idempotencyKey,
  );
}

/** Returns the download URL — caller can <a href> or window.open() it. */
export function exportMemoryUrl(): string {
  return appPath("/api/v1/memory/export");
}

// ─── dreaming pause toggle ──────────────────────────────

export async function fetchDreamingState(): Promise<DreamingState> {
  const r = await fetch("/api/v1/memory/dreaming", { credentials: "include" });
  await _check(r);
  return r.json();
}

export async function pauseDreaming(): Promise<DreamingState> {
  const r = await fetch("/api/v1/memory/dreaming/pause", {
    method: "POST",
    credentials: "include",
  });
  await _check(r);
  return r.json();
}

export async function resumeDreaming(): Promise<DreamingState> {
  const r = await fetch("/api/v1/memory/dreaming/resume", {
    method: "POST",
    credentials: "include",
  });
  await _check(r);
  return r.json();
}

// ─── candidates ─────────────────────────────────────────

export async function listCandidates(
  status: CandidateStatusFilter = "pending",
  limit = 50,
): Promise<CandidateList> {
  // Per-turn chat cards read `getTurnMemoryReceipt` instead of filtering this list by turn.
  const qs = new URLSearchParams({ status, limit: String(limit) });
  const r = await fetch(`/api/v1/memory/candidates?${qs}`, {
    credentials: "include",
  });
  await _check(r);
  return r.json();
}

export async function approveCandidate(
  cid: string,
  reason?: string,
  target?: CandidateScope | null,
): Promise<PendingCandidate> {
  const r = await fetch(
    `/api/v1/memory/candidates/${encodeURIComponent(cid)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({
        reason: reason ?? null,
        target: target
          ? {
            type: target.type,
            space_id: target.space_id,
          }
          : null,
      }),
    },
  );
  await _check(r);
  return r.json();
}

export async function rejectCandidate(
  cid: string, reason?: string,
): Promise<PendingCandidate> {
  const r = await fetch(
    `/api/v1/memory/candidates/${encodeURIComponent(cid)}/reject`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
  await _check(r);
  return r.json();
}

// ─── bulk-decide (V3 PRD-B Day 6-7) ─────────────────────

export interface BulkDecideResultItem {
  cid: string;
  ok: boolean;
  status: string | null;
  error: string | null;
}

export interface BulkDecideResult {
  action: "approve" | "reject";
  total: number;
  succeeded: number;
  failed: number;
  items: BulkDecideResultItem[];
}

/** Structured "the user explicitly asked to remember" source recorded for a turn. */
export type TurnMemoryRequestSource =
  | "remember_tool"
  | "capture"
  | "language_setting"
  | "nl_intent";

export interface TurnMemoryExplicitRequest {
  source: TurnMemoryRequestSource;
  outcome: "remembered" | "candidate" | "not_remembered";
  /** Finite refusal code (`memory_card.not_remembered_reason.*`); null renders the generic copy. */
  reason: string | null;
}

/** A memory entry this turn created. `state` is derived server-side from the trash. */
export interface TurnMemoryReceiptEntry {
  id: string;
  kind: string;
  key: string;
  display_value?: string | null;
  origin: "remember_tool" | "capture" | "candidate";
  state: "remembered" | "undone";
  undoable: boolean;
  /** Set when the entry was approved from a candidate card of this turn. */
  candidate_id?: string | null;
}

/**
 * Per-turn memory receipt (`GET /memory/turn-receipt`).
 *
 * D7 display rule, applied by the two renderers: candidate cards show pending candidates and
 * candidate-origin entries; the disclosure strip shows other entries (any state) and an explicit
 * request whose outcome is `not_remembered`. Nothing else renders.
 *
 * ★ A REST projection of persisted memory state, polled after `done`: the live view, a refresh and a
 * replay read the same response, so an undone entry still reads as undone after a refresh.
 */
export interface TurnMemoryReceipt {
  schema_version: 1;
  turn_id: string;
  explicit_request: TurnMemoryExplicitRequest | null;
  pending_candidates: PendingCandidate[];
  entries: TurnMemoryReceiptEntry[];
}

export async function getTurnMemoryReceipt(
  turnId: string,
): Promise<TurnMemoryReceipt> {
  const r = await fetch(
    `/api/v1/memory/turn-receipt?turn_id=${encodeURIComponent(turnId)}`,
    { credentials: "include" },
  );
  await _check(r);
  return r.json();
}

/**
 * Batch-1c Revise: redraft with user feedback; old candidate gives way, new candidate asks again.
 *
 * ★ This is **not** rejection: backend records `decision_kind='feedback_redrafted'`;
 * asking for different wording must not be recorded as rejecting the candidate.
 */
export async function redraftCandidate(
  cid: string,
  comment: string,
): Promise<PendingCandidate> {
  const r = await fetch(
    `/api/v1/memory/candidates/${encodeURIComponent(cid)}/redraft`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ comment }),
    },
  );
  await _check(r);
  return r.json();
}

export async function bulkDecideCandidates(
  cids: string[], action: "approve" | "reject",
): Promise<BulkDecideResult> {
  const r = await fetch("/api/v1/memory/candidates/bulk-decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ cids, action }),
  });
  await _check(r);
  return r.json();
}

// ─── V3.5 PRD-C memory_notes ────────────────────────────

// V4 PRD-D-5: per-row verification anchor (Sprint 1 limitation, deprecated in Sprint 2).
export interface VerificationAnchor {
  canvas_id: string;
  section_id: string;
  verifier_result_hash: string;
  saved_at: string;
}

// V4 Sprint 2 PRD-D-4-1: per-bullet anchor (sidecar). Replaces Sprint 1 row-level.
export interface BulletAnchorDTO {
  bullet_text: string;
  canvas_id: string;
  section_id: string;
  verifier_result_hash: string;
  saved_at: string;
  status: "valid" | "stale" | "unknown";
  invalidated_at: string | null;
  invalidation_reason: string | null;
}

export interface MemoryNote {
  user_id: string;
  version: string;
  content: string;
  char_count: number;
  /** memory-revive S1: server budget cap (absent on old backends → frontend fallback 3000). */
  char_limit?: number;
  pinned_count: number;
  bullet_count: number;
  free_text_lines: number;
  last_edited_at: string | null;
  last_edited_source: string;
  is_locked: boolean;
  lock_expires_at: string | null;
  // V4 Sprint 2 PRD-D-4-1: per-bullet anchors (preferred path)
  bullet_anchors?: BulletAnchorDTO[];
  // V4 Sprint 1 deprecated row-level fields (compatibility retained, always null/'valid').
  verification_anchor?: VerificationAnchor | null;
  verification_status?: "valid" | "stale" | "unknown";
}

export interface NotesLockResult {
  locked_until: string;
  edit_token: string;
  version: string;
}

export async function fetchNotes(): Promise<MemoryNote> {
  const r = await fetch("/api/v1/memory/notes", { credentials: "include" });
  await _check(r);
  return r.json();
}

export async function patchNotes(
  content: string,
  opts: { version: string; editToken: string },
): Promise<MemoryNote> {
  const r = await fetch("/api/v1/memory/notes", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      content,
      version: opts.version,
      edit_token: opts.editToken,
      ignore_lock: false,
    }),
  });
  await _check(r);
  return r.json();
}

// V4 PRD-D-5: save a verified-fact bullet anchored to a canvas section.
// summaryText must be 1..200 char. pinned defaults true. Backend computes
// verifier_result_hash from section.payload (canonical JSON SHA256).
export async function saveFromCanvas(opts: {
  canvasId: string;
  sectionId: string;
  summaryText: string;
  pinned?: boolean;
}): Promise<MemoryNote> {
  const r = await fetch("/api/v1/memory/notes/save-from-canvas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      canvas_id: opts.canvasId,
      section_id: opts.sectionId,
      summary_text: opts.summaryText,
      pinned: opts.pinned ?? true,
    }),
  });
  await _check(r);
  return r.json();
}

export async function appendBullet(opts: {
  bullet: string;
  pinned?: boolean;
  source?: string;
  ignoreLock?: boolean;
}): Promise<MemoryNote> {
  const r = await fetch("/api/v1/memory/notes/append", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      bullet: opts.bullet,
      pinned: opts.pinned ?? false,
      source: opts.source ?? "user_explicit",
      ignore_lock: opts.ignoreLock ?? false,
    }),
  });
  await _check(r);
  return r.json();
}

export async function deleteBullet(
  bulletIndex: number,
  opts: { version: string; editToken: string },
): Promise<MemoryNote> {
  const qs = new URLSearchParams({
    bullet_index: String(bulletIndex),
    ignore_lock: "false",
  });
  const r = await fetch(`/api/v1/memory/notes/bullet?${qs}`, {
    method: "DELETE",
    headers: {
      "If-Match": `"${opts.version}"`,
      "X-Memory-Edit-Token": opts.editToken,
    },
    credentials: "include",
  });
  await _check(r);
  return r.json();
}

export async function lockNotes(
  expectedVersion: string,
  editToken?: string,
): Promise<NotesLockResult> {
  const r = await fetch("/api/v1/memory/notes/lock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      expected_version: expectedVersion,
      ...(editToken ? { edit_token: editToken } : {}),
    }),
  });
  await _check(r);
  return r.json();
}

export async function unlockNotes(editToken: string): Promise<void> {
  const r = await fetch("/api/v1/memory/notes/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ edit_token: editToken }),
  });
  await _check(r);
}
