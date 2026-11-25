"use client";

/**
 * Result pins (docs/sprints/result-pins-2026-11-24/tech-design.md §5.1): a person's sidebar shortcuts to files and
 * reports. A file pin follows the file to its newest live version; the server rechecks access on every load and leaves
 * out what the person can no longer open.
 *
 * One list per tab is shared by every surface (sidebar, Results, Files, Reports) through a small store, so a pin made
 * in Results shows in the sidebar at once without a second request.
 */
import { useCallback, useEffect, useSyncExternalStore } from "react";

import { ApiError, apiJson, apiVoid } from "@/lib/api-client";
import { requestCanvasDataFocus, requestCanvasResultOpen } from "@/lib/canvas-workspace-focus";
import { reportHrefForSession } from "@/lib/report-share";
import { reportDisplayTitle } from "@/lib/report-title";
import { getActiveSpaceId, spaceHeader } from "@/lib/space-storage";

export type ResultPinKind = "file" | "report";

export interface ResultPin {
  kind: ResultPinKind;
  /** The pinned identity: a file's first version (lineage root) or a report's canvas ID. */
  pin_id: string;
  /** Files only: the newest live version, which the pin opens. */
  artifact_id?: string;
  /** A file's current name; a report's own title, which may be empty (name rows with `resultPinTitle`). */
  title: string;
  /** Reports only: the inputs of `reportDisplayTitle`, the name the Reports list shows. */
  first_section_title?: string | null;
  created_at?: string;
  mime?: string;
  version_no?: number;
  session_id: string | null;
  /** Where the pin opens: its conversation, the Files drawer, or the shared-file viewer. */
  opens: "session" | "files" | "shared_view";
  changed_at: string;
  pinned_at: string;
}

export const RESULT_PINS_CHANGED_EVENT = "bowhead:result-pins-changed";
const WORKSPACE_CHANGED_EVENT = "bowhead:workspace-changed";

export const RESULT_PIN_LIMIT = 100;

export type ResultPinFailure = "gone" | "limit" | "unavailable" | "other";

/** Classify a refused Pin/Unpin so the caller can show the matching sentence (PRD §4.5). */
export function resultPinFailure(error: unknown): ResultPinFailure {
  if (error instanceof ApiError) {
    if (error.code === "result_pin_target_not_found" || error.status === 404) return "gone";
    if (error.code === "result_pin_limit") return "limit";
    if (error.code === "result_pins_unavailable") return "unavailable";
  }
  return "other";
}

/** The limit the server named in a `result_pin_limit` refusal, else the documented one. */
export function resultPinLimit(error: unknown): number {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: { error?: { limit?: unknown } } } | undefined;
    const limit = body?.detail?.error?.limit ?? error.details?.limit;
    if (typeof limit === "number" && Number.isFinite(limit) && limit > 0) return limit;
  }
  return RESULT_PIN_LIMIT;
}

function resolvedWorkspaceId(workspaceId?: string | null): string {
  return workspaceId ?? getActiveSpaceId();
}

export async function fetchResultPins(workspaceId?: string | null, signal?: AbortSignal): Promise<ResultPin[]> {
  const id = resolvedWorkspaceId(workspaceId);
  const query = id ? `?workspace_id=${encodeURIComponent(id)}` : "";
  const body = await apiJson<{ items?: unknown }>(`/api/v1/result-pins${query}`, {
    headers: spaceHeader(),
    cache: "no-store",
    signal,
  });
  return Array.isArray(body?.items) ? body.items.filter(isResultPin) : [];
}

function isResultPin(value: unknown): value is ResultPin {
  if (!value || typeof value !== "object") return false;
  const pin = value as Partial<ResultPin>;
  return (pin.kind === "file" || pin.kind === "report") && typeof pin.pin_id === "string" && pin.pin_id.length > 0
    && typeof pin.title === "string" && typeof pin.changed_at === "string";
}

/** The name a row shows: a file's name; a report named the way the Reports list names it. */
export function resultPinTitle(pin: ResultPin): string {
  if (pin.kind === "file") return pin.title;
  return reportDisplayTitle({
    title: pin.title,
    first_section_title: pin.first_section_title ?? null,
    created_at: pin.created_at ?? "",
    id: pin.pin_id,
  });
}

/**
 * Where a pin opens, without the deployment prefix (callers navigate through `useAppRouter()` or wrap it in
 * `appPath()`): reports and files with an openable conversation open there; other files open in Files or, when
 * shared by someone else, in the shared-file viewer.
 */
export function resultPinRoute(pin: ResultPin): string {
  if (pin.kind === "report") {
    return pin.session_id ? reportHrefForSession(pin.session_id) : "/reports/";
  }
  // The shared-file viewer accepts only the file's identity (its first version); the others open the version shown.
  if (pin.opens === "shared_view") return `/files/view/?file=${encodeURIComponent(pin.pin_id)}`;
  const fileId = encodeURIComponent(pin.artifact_id ?? pin.pin_id);
  if (pin.opens === "session" && pin.session_id) {
    return `/?session=${encodeURIComponent(pin.session_id)}&result=${fileId}&mode=preview`;
  }
  return `/files/?focus=${fileId}`;
}

/** Whether a pin opens in the conversation already on screen. */
export function resultPinOpensHere(pin: ResultPin, currentSessionId: string | null | undefined): boolean {
  return Boolean(currentSessionId) && pin.opens === "session" && pin.session_id === currentSessionId;
}

/**
 * Open a pin of the conversation on screen inside its Results. That conversation's Results does not follow a soft URL
 * change of `result=`, so the pin asks it directly, as in-page links (citations, chat cards) do.
 */
export function openResultPinHere(pin: ResultPin): void {
  if (pin.kind === "report") {
    requestCanvasResultOpen({ type: "report" }, { history: "push" });
    return;
  }
  requestCanvasDataFocus(pin.artifact_id ?? pin.pin_id, { history: "push", mode: "preview" });
}

// ─── Shared store ─────────────────────────────────────────

export type ResultPinsStatus = "idle" | "loading" | "ready" | "error";

interface ResultPinsSnapshot {
  workspaceId: string;
  status: ResultPinsStatus;
  pins: readonly ResultPin[];
  error: unknown;
  /** Whether a load ever succeeded for this Space: a failure then still knows the person has pins. */
  loadedOnce: boolean;
}

const EMPTY: ResultPinsSnapshot = { workspaceId: "", status: "idle", pins: [], error: null, loadedOnce: false };

let snapshot: ResultPinsSnapshot = EMPTY;
let inflight: { workspaceId: string; controller: AbortController } | null = null;
const listeners = new Set<() => void>();
/** Version IDs a pin was made from in this tab, so surfaces showing an older version still see it as pinned. */
const aliases = new Map<string, string>();

function publish(next: ResultPinsSnapshot): void {
  snapshot = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function sortPins(pins: readonly ResultPin[]): ResultPin[] {
  return [...pins].sort((a, b) => (a.changed_at < b.changed_at ? 1 : a.changed_at > b.changed_at ? -1 : 0));
}

/** Load the list for a Space; a running load for the same Space is reused. */
export function loadResultPins(workspaceId?: string | null): void {
  const id = resolvedWorkspaceId(workspaceId);
  if (inflight?.workspaceId === id) return;
  inflight?.controller.abort();
  const controller = new AbortController();
  inflight = { workspaceId: id, controller };
  const sameSpace = snapshot.workspaceId === id;
  publish({
    workspaceId: id,
    status: "loading",
    pins: sameSpace ? snapshot.pins : [],
    error: null,
    loadedOnce: sameSpace && snapshot.loadedOnce,
  });
  fetchResultPins(id, controller.signal)
    .then((pins) => {
      if (inflight?.controller !== controller) return;
      inflight = null;
      publish({ workspaceId: id, status: "ready", pins: sortPins(pins), error: null, loadedOnce: true });
    })
    .catch((error: unknown) => {
      if (inflight?.controller !== controller) return;
      inflight = null;
      if (controller.signal.aborted) return;
      publish({ ...snapshot, workspaceId: id, status: "error", error });
    });
}

function notifyResultPinsChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(RESULT_PINS_CHANGED_EVENT));
}

/** Pin a file (any version ID) or a report (canvas ID). Resolves with the pin as the sidebar will show it. */
export async function pinResult(kind: ResultPinKind, id: string): Promise<ResultPin> {
  const pin = await apiJson<ResultPin>("/api/v1/result-pins", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...spaceHeader() },
    body: JSON.stringify({ kind, id }),
  });
  if (isResultPin(pin)) {
    aliases.set(`${kind}:${id}`, pin.pin_id);
    // A shared result shows in every Space; an own one only in its Space, which the next load settles.
    publish({
      ...snapshot,
      pins: sortPins([...snapshot.pins.filter((item) => !(item.kind === pin.kind && item.pin_id === pin.pin_id)), pin]),
    });
  }
  notifyResultPinsChanged();
  return pin;
}

/** Unpin by the pin's identity or by any version ID of the file. */
export async function unpinResult(kind: ResultPinKind, id: string): Promise<void> {
  await apiVoid(`/api/v1/result-pins/${kind}/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: spaceHeader(),
  });
  const pinId = aliases.get(`${kind}:${id}`) ?? id;
  for (const [key, value] of aliases) if (value === pinId) aliases.delete(key);
  publish({
    ...snapshot,
    pins: snapshot.pins.filter((item) => !(item.kind === kind && (item.pin_id === pinId || item.artifact_id === id))),
  });
  notifyResultPinsChanged();
}

/** The pin a surface's result belongs to: matched by the version shown, the pin identity or a version pinned here. */
export function findResultPin(
  pins: readonly ResultPin[],
  kind: ResultPinKind,
  id: string | null | undefined,
): ResultPin | undefined {
  if (!id) return undefined;
  const alias = aliases.get(`${kind}:${id}`);
  return pins.find(
    (pin) => pin.kind === kind && (pin.pin_id === id || pin.artifact_id === id || (alias !== undefined && pin.pin_id === alias)),
  );
}

export interface ResultPinsView {
  status: ResultPinsStatus;
  pins: readonly ResultPin[];
  error: unknown;
  loadedOnce: boolean;
  reload: () => void;
}

/**
 * The person's pins for a Space (the active Space when omitted). Loads on first use and on Space change, and reloads
 * when any surface pins or unpins. `enabled: false` reads nothing and never requests.
 */
export function useResultPins(
  workspaceId?: string | null,
  { enabled = true }: { enabled?: boolean } = {},
): ResultPinsView {
  const current = useSyncExternalStore(subscribe, () => snapshot, () => EMPTY);
  const requested = enabled ? (workspaceId ?? null) : null;

  useEffect(() => {
    if (!enabled) return;
    const id = resolvedWorkspaceId(requested);
    if (snapshot.workspaceId !== id || snapshot.status === "idle") loadResultPins(id);
  }, [enabled, requested]);

  useEffect(() => {
    if (!enabled) return;
    const reload = () => loadResultPins(resolvedWorkspaceId(requested));
    const reloadForSpace = () => {
      if (requested === null) loadResultPins(getActiveSpaceId());
    };
    window.addEventListener(RESULT_PINS_CHANGED_EVENT, reload);
    window.addEventListener(WORKSPACE_CHANGED_EVENT, reloadForSpace);
    return () => {
      window.removeEventListener(RESULT_PINS_CHANGED_EVENT, reload);
      window.removeEventListener(WORKSPACE_CHANGED_EVENT, reloadForSpace);
    };
  }, [enabled, requested]);

  const reload = useCallback(() => loadResultPins(resolvedWorkspaceId(requested)), [requested]);
  if (!enabled) return { status: "idle", pins: [], error: null, loadedOnce: false, reload };
  return { status: current.status, pins: current.pins, error: current.error, loadedOnce: current.loadedOnce, reload };
}

/** Tests only: forget the shared list between cases. */
export function resetResultPinsForTests(): void {
  inflight?.controller.abort();
  inflight = null;
  aliases.clear();
  snapshot = EMPTY;
}
