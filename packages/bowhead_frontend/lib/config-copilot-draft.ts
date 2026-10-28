/** Configuration copilot batch 1-b — ConfigDrawer payload store + server state orchestration.
 *
 * (tech-design-batch1b-drawer.md §4.1; same trio as batch-1a lite)
 * - Payload store: module Map (dispatcher writes, chip/drawer reads), rendering/delta source for replay.
 * - Server owns state (config_copilot_draft.status): mount-time GET hydration; structured 404 → stale,
 *   plain 404 (flag off)/network error → uncached, retryable.
 * - consume/discard forward POST; saving uses existing form API (draft records only saved receipt).
 * - dismissedDraftIds prevent closed drawers reopening during SSE reconnect replay within this session.
 */

import { ApiError, apiJson } from "@/lib/api-client";
import type { ConfigDraftUpdatedPayload } from "@/lib/types";

export type DraftServerState = "draft" | "consumed" | "discarded" | "expired" | "stale";

export interface DraftResultReceipt {
  job_id?: string;
  kind?: "team_subagent";
  stage?: "working_copy";
  ref_id?: string;
  label?: string;
  name?: string | null;
  human_schedule?: string | null;
  next_run_at?: string | null;
}

export interface ConfigDraftDto {
  draft_id: string;
  form_id: string;
  status: string;
  workspace_id: string | null;
  fields: Record<string, unknown>;
  field_meta: ConfigDraftUpdatedPayload["field_meta"];
  notes: Record<string, string>;
  result: DraftResultReceipt | null;
  row_version: number;
  target?: { id: string; label: string };
  changed_fields_cumulative?: string[];
  replayed?: boolean;
}

const _payloads = new Map<string, ConfigDraftUpdatedPayload>();
const _states = new Map<string, DraftServerState>();
const _results = new Map<string, DraftResultReceipt | null>();
const _hydrating = new Map<string, Promise<void>>();
const _dismissed = new Set<string>();
const _listeners = new Set<() => void>();
let _version = 0;

function _notify(): void {
  _version += 1;
  _listeners.forEach((fn) => fn());
}

export function subscribeDraft(listener: () => void): () => void {
  _listeners.add(listener);
  return () => _listeners.delete(listener);
}

export function draftVersion(): number {
  return _version;
}

/** Dispatcher writes (same-draftId overwrite idempotent; final value is newest suggestion). */
export function applyDraftEvent(payload: ConfigDraftUpdatedPayload): void {
  _payloads.set(payload.draft_id, payload);
  if (!_states.has(payload.draft_id)) _states.set(payload.draft_id, "draft");
  _notify();
}

export function getDraftPayload(draftId: string): ConfigDraftUpdatedPayload | undefined {
  return _payloads.get(draftId);
}

export function getDraftState(draftId: string): DraftServerState | null {
  return _states.get(draftId) ?? null;
}

export function getDraftResult(draftId: string): DraftResultReceipt | null {
  return _results.get(draftId) ?? null;
}

export function isDraftDismissed(draftId: string): boolean {
  return _dismissed.has(draftId);
}

/** User closes drawer: draft never autoopens again this session, including SSE replay of same draftId. */
export function markDraftDismissed(draftId: string): void {
  _dismissed.add(draftId);
}

export function resetDraftForTests(): void {
  _payloads.clear();
  _states.clear();
  _results.clear();
  _hydrating.clear();
  _dismissed.clear();
  _notify();
}

/** Test-only: drop state cache (simulate rehydration after F5). */
export function _dropDraftStateCacheForTests(): void {
  _states.clear();
  _results.clear();
  _hydrating.clear();
}

function _toState(status: string): DraftServerState {
  return status === "consumed" || status === "discarded" || status === "expired" ? status : "draft";
}

function _absorb(dto: ConfigDraftDto): void {
  _states.set(dto.draft_id, _toState(dto.status));
  _results.set(dto.draft_id, dto.result ?? null);
  _notify();
}

function _isStructuredNotFound(error: unknown): boolean {
  // Accept only structured server 404 (config_draft_not_found), not plain 404 for flag-off route absence (same as batch 1-a P2-3).
  if (!(error instanceof ApiError) || error.status !== 404) return false;
  const body = error.body as { detail?: { error?: { code?: string } } } | undefined;
  return body?.detail?.error?.code === "config_draft_not_found";
}

function _conflictStatus(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const body = error.body as { detail?: { error?: { code?: string; details?: { status?: string } } } } | undefined;
  const inner = body?.detail?.error;
  if (inner?.code !== "draft_state_conflict") return null;
  return typeof inner.details?.status === "string" ? inner.details.status : null;
}

/** Mount-time state hydration (deduplicate in-flight). */
export function hydrateDraftState(draftId: string): Promise<void> {
  if (_states.has(draftId) && _results.has(draftId)) return Promise.resolve();
  const inflight = _hydrating.get(draftId);
  if (inflight) return inflight;
  const task = fetchDraft(draftId)
    .then((dto) => _absorb(dto))
    .catch((error: unknown) => {
      if (_isStructuredNotFound(error)) {
        _states.set(draftId, "stale");
        _notify();
      }
    })
    .finally(() => {
      _hydrating.delete(draftId);
    });
  _hydrating.set(draftId, task);
  return task;
}

/** Drawer mount fetch (server authority: complete fields/field_meta/notes). */
export async function fetchDraft(draftId: string): Promise<ConfigDraftDto> {
  const dto = await apiJson<ConfigDraftDto>(`/api/v1/config-drafts/${draftId}`, { cache: "no-store" });
  return dto;
}

/** Saved-success receipt (draft→consumed state transition). */
export async function consumeDraft(draftId: string, result: DraftResultReceipt): Promise<void> {
  try {
    _absorb(await apiJson<ConfigDraftDto>(`/api/v1/config-drafts/${draftId}/consume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result }),
    }));
  } catch (error) {
    const conflict = _conflictStatus(error);
    if (conflict) {
      _states.set(draftId, _toState(conflict));
      _notify();
      return;
    }
    throw error;
  }
}

/** Discard draft (draft→discarded); failure silent, converges through natural 24h expiry. */
export function discardDraft(draftId: string): void {
  void apiJson<ConfigDraftDto>(`/api/v1/config-drafts/${draftId}/discard`, { method: "POST" })
    .then((dto) => _absorb(dto))
    .catch((error: unknown) => {
      const conflict = _conflictStatus(error);
      if (conflict) {
        _states.set(draftId, _toState(conflict));
        _notify();
      }
    });
}

// ── Drawer-open request channel (chip click = explicit user; live wire event = one autoopen) ──
// Host page.tsx is sole subscriber; module channel avoids prop drilling from two TurnContainer callers.
const _openListeners = new Set<(draftId: string) => void>();

export function subscribeOpenDraft(listener: (draftId: string) => void): () => void {
  _openListeners.add(listener);
  return () => _openListeners.delete(listener);
}

/** User clicks Open chip; ignore dismissed because intent is explicit. */
export function requestOpenDraft(draftId: string): void {
  _openListeners.forEach((fn) => fn(draftId));
}

/** Autoopen on live SSE; never reopen user-dismissed draft (full reconnect replay, review P3-1). */
export function requestAutoOpenDraft(draftId: string): void {
  if (_dismissed.has(draftId)) return;
  _openListeners.forEach((fn) => fn(draftId));
}
