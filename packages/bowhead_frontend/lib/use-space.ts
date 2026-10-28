"use client";

/**
 * Space picker state: persist local active space and fetch list.
 *
 * Design:
 *   - `spacesResource` solely owns space list: one `/api/v1/auth/me/workspaces` request per cold load,
 *     shared by all useSpace() instances (picker/pages/settings),
 *     previously one request per instance, homepage = 2.
 *   - active_id priority: tab context > persisted preference > gateway active_id > items[0]
 *   - setActiveId() writes localStorage and rerenders all subscribers
 *   - Space switching emits "bowhead:workspace-changed" custom event for API layer
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchWorkspaces, setLastWorkspace, type WorkspaceDTO } from "./api";
import { useLocale } from "next-intl";
import { DEFAULT_LOCALE, SUPPORTED_LOCALES, type Locale } from "./locale";
import { staticTranslator } from "./locale-messages";
import { createSharedResource, useSharedResource } from "./shared-resource";
import { TEAM_CHANGED_EVENT } from "./team-storage";

// Batch 3/c2: consolidate storage in space-storage (new read/legacy fallback, dual write); retain module API.
import { getActiveSpaceId as _getLS, setActiveSpaceId as _setLS, setTabSpaceId } from "@/lib/space-storage";
const EVT_CHANGED = "bowhead:workspace-changed";
const EVT_INVALIDATED = "bowhead:workspaces-invalidated";

interface SpaceListResult {
  items: WorkspaceDTO[];
  active_id: string | null;
  error?: string;
  error_detail?: { status: number; requestId?: string };
}

// Space list omits X-Team-Id; server resolves current session team, so one key suffices;
// TEAM_CHANGED_EVENT invalidates on team switch (invalidate clears all entry TTLs).
const SPACES_KEY = "self";

export const spacesResource = createSharedResource<SpaceListResult>({
  name: "spaces",
  fetcher: () => fetchWorkspaces(),
  // Deduplicate cold loads/reuse across route mounts (sidebar/picker/page), without long cache:
  // create/delete/edit space use invalidateSpaces(); team switching uses TEAM_CHANGED_EVENT.
  ttlMs: 30_000,
  invalidateOnEvents: [EVT_INVALIDATED, TEAM_CHANGED_EVENT],
  // Another team's spaces are never shown, not even as a first answer.
  contextEvents: [TEAM_CHANGED_EVENT],
  // Unchanged refresh preserves snapshot identity: space list rarely changes, yet every useSpace()
  // consumer (picker/page/settings) would otherwise rerender.
  equals: (previous, next) =>
    previous.error === next.error &&
    previous.active_id === next.active_id &&
    JSON.stringify(previous.items) === JSON.stringify(next.items),
});

// Stable identity so `items` does not change reference on every render.
const EMPTY_ITEMS: WorkspaceDTO[] = [];

export interface SpaceState {
  items: WorkspaceDTO[];
  activeId: string | null;
  activeName: string;
  loading: boolean;
  error: string | null;
  /** Raw failure details (status/request ID), Copy diagnostics only, never direct display. */
  errorCause?: unknown;
  setActiveId: (id: string) => void;
  refresh: () => void;
}

function readLS(): string | null {
  const v = _getLS();
  return v || null;
}

function writeLS(id: string) {
  _setLS(id);
}

/** Restore an authorized Session context without changing the user's saved preference. */
export function restoreActiveSpace(id: string) {
  setTabSpaceId(id);
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(EVT_CHANGED, { detail: id }));
  }
}

export function switchActiveSpace(id: string) {
  writeLS(id);
  restoreActiveSpace(id);
  // B1: persist to server (preferences.last_workspace_id) so /me/workspaces active_id —
  // read by Overview / Workspaces-list / Members / Glossary / turn-runner — reflects the
  // choice and survives reload / new device. Non-fatal on failure (localStorage cache).
  void setLastWorkspace(id).catch(() => {});
}

/**
 * One-shot read of the shared space list for non-React callers (settings
 * drawers, profile form). Shares the cache and the in-flight request with every
 * mounted useSpace(), so opening a settings surface no longer re-asks the
 * gateway for a list the picker already has.
 */
export function readSpaces(): Promise<SpaceListResult> {
  return spacesResource.read(SPACES_KEY);
}

export function invalidateSpaces() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(EVT_INVALIDATED));
  }
}

// Overloads keep both call shapes type-safe:
//   - getActiveSpace("ws_x")  → string   (v2-composer assigns into a non-null field)
//   - getActiveSpace()        → string | null (locale-switcher branches on null)
export function getActiveSpace(fallback: string): string;
export function getActiveSpace(fallback?: string | null): string | null;
export function getActiveSpace(fallback: string | null = null): string | null {
  return readLS() || fallback;
}

/**
 * The space list + resolved active id, without the localized display name.
 *
 * Non-UI callers (the session list, which only needs a scope key) use this so
 * they do not pull in the intl context that `activeName` requires.
 */
export function useSpaceSelection(): Omit<SpaceState, "activeName"> {
  // The list itself is owned by `spacesResource`; CRUD in a settings-local
  // useSpace() instance reaches the topbar picker through the same entry, so the
  // cross-instance refresh no longer needs a per-instance listener.
  const list = useSharedResource(spacesResource, SPACES_KEY);
  const items = list.data?.items ?? EMPTY_ITEMS;
  // Initialize from this tab's context so the first render already has an active id
  // for returning users. This removes the transient-null window that made the chat turn-runner
  // log "workspace_id missing". Authorized list hydration validates that selection.
  const [selectedId, setSelectedId] = useState<string | null>(() => readLS());
  // `idle` = the subscription effect has not run yet; reporting "not loading"
  // for that first frame would flash the "no space" label at every reader.
  // A refresh that already has a list to show is deliberately not "loading" —
  // consumers keep rendering the current spaces instead of blinking a spinner.
  const loading = list.status === "idle" || (list.loading && !list.data);
  const error = list.data?.error ?? list.error?.message ?? null;
  const errorCause = list.data?.error_detail ?? list.error ?? null;

  // B1: resolve the active id with precedence
  //   tab context (initially seeded by the persisted preference)
  //   > gateway active_id (server preferences.last_workspace_id, authoritative)
  //   > items[0] (auto-select first)
  // Resolved during render, not in an effect: an effect would publish one frame
  // where the list has arrived but the id has not, and space-scoped fetchers
  // (the session list) would spend a request on that intermediate scope.
  const activeId = useMemo(() => {
    const result = list.data;
    if (!result) return selectedId;
    const inList = (id: string | null | undefined): id is string =>
      !!id && result.items.some((w) => w.id === id);
    if (inList(selectedId)) return selectedId;
    if (inList(result.active_id)) return result.active_id;
    return result.items[0]?.id ?? null;
  }, [list.data, selectedId]);

  // Hydration is read-only: retain this tab's resolved context, but do
  // NOT persist server-side here. Auto-persisting on every reader's mount would fire a
  // write from read-only pages (e.g. Overview) and, on a swallowed PUT failure, leave the
  // picker diverged from server-active_id readers. Server persistence
  // happens only on an explicit switch (switchActiveSpace). The auto-selected items[0]
  // is deterministic, so every reader still resolves the same default without a write.
  useEffect(() => {
    if (activeId) setTabSpaceId(activeId);
  }, [activeId]);

  // Explicit selection or authorized Session recovery in this tab.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const h = (e: Event) => {
      const detail = (e as CustomEvent<string>).detail;
      if (typeof detail === "string") setSelectedId(detail);
    };
    window.addEventListener(EVT_CHANGED, h);
    return () => window.removeEventListener(EVT_CHANGED, h);
  }, []);

  const setActiveId = useCallback((id: string) => {
    setSelectedId(id);
    switchActiveSpace(id);
  }, []);

  return {
    items,
    activeId,
    loading,
    error,
    errorCause,
    setActiveId,
    refresh: invalidateSpaces,
  };
}

export function useSpace(): SpaceState {
  const selection = useSpaceSelection();
  const { items, activeId, loading } = selection;
  const locale = useLocale();
  const t = staticTranslator(
    "space_label",
    (SUPPORTED_LOCALES as readonly string[]).includes(locale) ? (locale as Locale) : DEFAULT_LOCALE,
  );
  const activeName =
    items.find((w) => w.id === activeId)?.name ||
    // B1: after lazy init, activeId may precede items; prioritize loading wording during load,
    // never flash raw `Workspace ws_xxx` id.
    (loading
      ? t("loading")
      // No match after loading: null activeId = account has no spaces (team-less/fetch failed,
      // never misleadingly show loading, 101 R6 dogfood); populated activeId absent from list = stale id.
      : activeId
        ? `Space ${activeId}`
        : t("none"));

  return { ...selection, activeName };
}
