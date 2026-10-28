"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { fetchSessions, type SessionListResponseDTO } from "./api";
import { SESSIONS as MOCK_SESSIONS } from "./scenarios";
import { adaptSessionGroups, mergeSessionGroups } from "./session-groups";
import { notifySessionListChanged, SESSION_LIST_CHANGED_EVENT } from "./session-list-events";
import { createSharedResource, useSharedResource } from "./shared-resource";
import { getActiveSpaceId } from "./space-storage";
import { TEAM_CHANGED_EVENT } from "./team-storage";
import type { SessionGroup } from "./types";
import { useSpaceSelection } from "./use-space";

// Read per call rather than at module load: Next inlines the literal at build
// time either way, and tests can select the branch they exercise.
function sseMode(): "sse" | "mock" {
  return (process.env.NEXT_PUBLIC_BOWHEAD_SSE_MODE ?? "mock") as "sse" | "mock";
}

/** Turn-done broadcast; the canvas fires it whenever a session's state moved. */
const CANVAS_CHANGED_EVENT = "bowhead.canvas.changed";

/**
 * Longest a first load shows skeleton rows. 116 phone cold load: a hanging `/auth/me/workspaces` kept the session
 * list provisional, so the drawer showed the skeleton indefinitely; after this bound it shows an actionable error.
 */
export const SESSION_LIST_WAIT_MS = 15_000;

export interface SessionListState {
  /** First page merged with every older page loaded through `loadMore`. */
  groups: SessionGroup[];
  archivedCount: number;
  /** First load has not landed yet (a background refresh does not set this). */
  loading: boolean;
  /** The freshest attempt failed. A previously loaded list stays in `groups`. */
  error: boolean;
  /**
   * The first load waited longer than {@link SESSION_LIST_WAIT_MS} with nothing to show, so `loading` gave way to
   * `error`: "spaces" while the Space list (`/auth/me/workspaces`) is what has not answered, otherwise "sessions".
   * A late answer still fills the list.
   */
  stalledOn: "spaces" | "sessions" | null;
  /** Retry the first load: re-requests the Space list when it is the one still pending, and the session list. */
  retry: () => void;
  /** More (older) sessions exist beyond what is loaded. */
  hasMore: boolean;
  loadingMore: boolean;
  /** The last `loadMore` failed; loaded rows stay and the cursor is kept for a retry. */
  loadMoreError: boolean;
  /** Older pages loaded so far (0 = first page only); the sidebar's end state needs at least one. */
  olderPagesLoaded: number;
  /** Sessions the latest older page added (in page order, without ids shown before it). */
  latestOlderPageIds: string[];
  /** Fetch the next older page and append it (the sidebar's "Show older" row). */
  loadMore: () => Promise<void>;
}

interface SessionListData {
  groups: SessionGroup[];
  archivedCount: number;
  /** Cursor after the first page; null when the first page holds everything. */
  nextCursor: string | null;
  /**
   * Space the first page was actually fetched for ("" = unscoped). Cursors are
   * bound to it, so older pages are requested for this Space explicitly rather
   * than whatever Space is active by then.
   */
  scope: string;
}

const EMPTY_GROUPS: SessionGroup[] = [];

function toPageData(resp: SessionListResponseDTO, scope = ""): SessionListData {
  return {
    groups: adaptSessionGroups(resp.groups),
    archivedCount: resp.archived_count ?? 0,
    // Older gateways send neither field: treat that as a single page.
    nextCursor: resp.next_cursor ?? null,
    scope,
  };
}

/**
 * The session list is owned here, not by each surface that shows it.
 *
 * The sidebar, the mobile drawer and the workspace page all render the same
 * list; each of them used to run its own fetch + its own refresh triggers, so a
 * cold load asked `/api/v1/sessions` several times and every turn-done broadcast
 * multiplied by the number of mounted surfaces. One entry per space now serves
 * all of them, with a single 10s interval and a single set of refresh listeners.
 *
 * This resource is the FIRST page only. Older pages live in `sessionPages`
 * below and survive every poll of the first page.
 */
export const sessionsResource = createSharedResource<SessionListData>({
  name: "sessions",
  fetcher: async (key) => {
    // "" = no explicit space: fetchSessions falls back to the stored active id,
    // which is what the sidebar did before it shared this entry.
    // Resolve the stored active id here (what fetchSessions would do) so the
    // page records which Space its cursor belongs to.
    const scope = key || getActiveSpaceId() || "";
    return toPageData(await fetchSessions({ workspaceId: scope }), scope);
  },
  // Short TTL: this only collapses the mount burst (sidebar + page + drawer),
  // the 10s interval keeps the list live.
  ttlMs: 3_000,
  pollMs: 10_000,
  refreshOnFocus: true,
  // Most 10s polls return the same list. One fieldwise comparison is much cheaper than needless rerenders of every subscriber
  // (sidebar, drawer, workspace page, plus composers subscribed to the same list).
  equals: (previous, next) =>
    previous.archivedCount === next.archivedCount &&
    previous.nextCursor === next.nextCursor &&
    previous.scope === next.scope &&
    JSON.stringify(previous.groups) === JSON.stringify(next.groups),
  invalidateOnEvents: [
    SESSION_LIST_CHANGED_EVENT,
    CANVAS_CHANGED_EVENT,
    TEAM_CHANGED_EVENT,
  ],
  // Another team's list is never shown, not even as a first answer.
  contextEvents: [TEAM_CHANGED_EVENT],
});

// ─── Older pages (cursor pagination) ─────────────────────────────────────

interface LoadedPage {
  groups: SessionGroup[];
  nextCursor: string | null;
}

export interface SessionPagesSnapshot {
  /** First-page cursor the loaded chain continues from. */
  anchor: string | null;
  /** Space the chain's cursors are bound to. */
  scope: string;
  pages: LoadedPage[];
  loadingMore: boolean;
  loadMoreError: boolean;
}

interface PagesEntry {
  snapshot: SessionPagesSnapshot;
  /** Bumped whenever the chain is replaced, so late responses are dropped. */
  generation: number;
  /** A deliberate list change (pin/archive/rename/delete) since the last walk. */
  dirty: boolean;
  rewalking: boolean;
  /** Id of the latest load-more request; only that request may clear its spinner. */
  moreRequest: number;
  listeners: Set<() => void>;
}

const EMPTY_PAGES: SessionPagesSnapshot = {
  anchor: null,
  scope: "",
  pages: [],
  loadingMore: false,
  loadMoreError: false,
};

type PageFetcher = (scope: string, cursor: string) => Promise<LoadedPage>;

const defaultPageFetcher: PageFetcher = async (scope, cursor) => {
  // Always explicit ("" = unscoped): never fall back to the now-active Space.
  const page = toPageData(await fetchSessions({ workspaceId: scope, cursor }));
  return { groups: page.groups, nextCursor: page.nextCursor };
};

/**
 * Older pages per space key, shared by every surface like the first page.
 *
 * The 10s poll refreshes the first page only. Loaded older pages stay; when the
 * first page's boundary moves (a session was created or became active and the
 * last first-page row slid down) or the user changed the list, the loaded
 * chain is re-fetched from the new first-page cursor with the same number of
 * pages, so no row falls into a gap between the first page and the older ones.
 */
export function createSessionPagesStore(
  fetchPage: PageFetcher = defaultPageFetcher,
) {
  const entries = new Map<string, PagesEntry>();
  let eventsAttached = false;

  const entryFor = (key: string): PagesEntry => {
    let entry = entries.get(key);
    if (!entry) {
      entry = {
        snapshot: EMPTY_PAGES,
        generation: 0,
        dirty: false,
        rewalking: false,
        moreRequest: 0,
        listeners: new Set(),
      };
      entries.set(key, entry);
    }
    return entry;
  };

  const publish = (entry: PagesEntry, next: SessionPagesSnapshot) => {
    entry.snapshot = next;
    for (const listener of [...entry.listeners]) listener();
  };

  const markDirty = () => {
    for (const entry of entries.values()) entry.dirty = true;
  };
  const dropAll = () => {
    for (const entry of entries.values()) {
      entry.generation += 1;
      entry.dirty = false;
      entry.rewalking = false;
      publish(entry, EMPTY_PAGES);
    }
  };

  const attachEvents = () => {
    if (eventsAttached || typeof window === "undefined") return;
    eventsAttached = true;
    window.addEventListener(SESSION_LIST_CHANGED_EVENT, markDirty);
    // Another team is another list: nothing loaded applies any more.
    window.addEventListener(TEAM_CHANGED_EVENT, dropAll);
  };

  const rewalk = async (entry: PagesEntry, anchor: string, scope: string) => {
    const count = Math.max(1, entry.snapshot.pages.length);
    const generation = ++entry.generation;
    entry.dirty = false;
    entry.rewalking = true;
    try {
      const pages: LoadedPage[] = [];
      let cursor: string | null = anchor;
      while (cursor && pages.length < count) {
        const page = await fetchPage(scope, cursor);
        if (generation !== entry.generation) return;
        pages.push(page);
        cursor = page.nextCursor;
      }
      // The rewalk supersedes any load-more that was in flight (its response is
      // dropped by generation), so it also owns clearing that state.
      publish(entry, {
        anchor,
        scope,
        pages,
        loadingMore: false,
        loadMoreError: false,
      });
    } catch {
      // Keep the previous chain on screen; the next first-page change retries.
      if (generation === entry.generation) {
        entry.dirty = true;
        publish(entry, { ...entry.snapshot, loadingMore: false });
      }
    } finally {
      if (generation === entry.generation) entry.rewalking = false;
    }
  };

  return {
    peek(key: string): SessionPagesSnapshot {
      return entries.get(key)?.snapshot ?? EMPTY_PAGES;
    },
    subscribe(key: string, listener: () => void): () => void {
      attachEvents();
      const entry = entryFor(key);
      entry.listeners.add(listener);
      return () => {
        entry.listeners.delete(listener);
      };
    },
    /** Called whenever the first page changed; keeps the older chain contiguous. */
    reconcile(
      key: string,
      firstPageCursor: string | null,
      scope: string,
    ): void {
      const entry = entries.get(key);
      if (!entry || entry.snapshot.pages.length === 0 || entry.rewalking)
        return;
      if (firstPageCursor === null) {
        // Everything fits on the first page now.
        entry.generation += 1;
        entry.dirty = false;
        publish(entry, EMPTY_PAGES);
        return;
      }
      if (
        entry.snapshot.anchor === firstPageCursor &&
        entry.snapshot.scope === scope &&
        !entry.dirty
      )
        return;
      void rewalk(entry, firstPageCursor, scope);
    },
    async loadMore(
      key: string,
      firstPageCursor: string | null,
      scope: string,
    ): Promise<void> {
      attachEvents();
      const entry = entryFor(key);
      const { pages, loadingMore } = entry.snapshot;
      const cursor = pages.length
        ? pages[pages.length - 1].nextCursor
        : firstPageCursor;
      if (!cursor || loadingMore || entry.rewalking) return;
      const generation = entry.generation;
      const request = ++entry.moreRequest;
      const chainScope = pages.length ? entry.snapshot.scope : scope;
      publish(entry, {
        ...entry.snapshot,
        anchor: pages.length ? entry.snapshot.anchor : firstPageCursor,
        scope: chainScope,
        loadingMore: true,
        loadMoreError: false,
      });
      try {
        const page = await fetchPage(chainScope, cursor);
        if (generation !== entry.generation) {
          // Superseded by a rewalk or a reset. Whoever replaced the chain may not
          // have published yet; never leave this request's spinner behind.
          if (
            entry.moreRequest === request &&
            entry.snapshot.loadingMore &&
            !entry.rewalking
          ) {
            publish(entry, { ...entry.snapshot, loadingMore: false });
          }
          return;
        }
        publish(entry, {
          ...entry.snapshot,
          pages: [...entry.snapshot.pages, page],
          loadingMore: false,
        });
      } catch {
        if (generation !== entry.generation) {
          if (
            entry.moreRequest === request &&
            entry.snapshot.loadingMore &&
            !entry.rewalking
          ) {
            publish(entry, { ...entry.snapshot, loadingMore: false });
          }
          return;
        }
        publish(entry, {
          ...entry.snapshot,
          loadingMore: false,
          loadMoreError: true,
        });
      }
    },
    /** Test seam. */
    reset(): void {
      entries.clear();
      if (eventsAttached && typeof window !== "undefined") {
        window.removeEventListener(SESSION_LIST_CHANGED_EVENT, markDirty);
        window.removeEventListener(TEAM_CHANGED_EVENT, dropAll);
      }
      eventsAttached = false;
    },
  };
}

export const sessionPages = createSessionPagesStore();

/**
 * useSessionList — session list shared by sidebar / drawer / workspace page.
 *  - mock mode: directly return static scenarios.ts data
 *  - sse mode: subscribe to `sessionsResource` (one first-paint fetch + every 10s, shared by subscribers)
 *    plus any older pages loaded through `loadMore` (shared the same way).
 *
 * Omitted `workspaceId` follows the current space; an explicit value wins (workspace page already owns it).
 */
export function useSessionList(
  refreshKey = 0,
  enabled = true,
  workspaceId?: string,
): SessionListState {
  const space = useSpaceSelection();
  // Both call shapes must land on the same cache entry, otherwise the sidebar
  // and the page would each own a request for the same space. An empty string
  // is "not known yet", not a scope — the workspace page renders it for one
  // frame before the space list resolves.
  const explicitId = workspaceId || undefined;
  // A returning browser knows its space from localStorage on the first render;
  // only a first-time load has to wait for the list, and waiting is what keeps
  // it from fetching the unscoped list and then immediately fetching again.
  const spaceKnown = space.activeId !== null || !space.loading;
  const key =
    enabled && (explicitId !== undefined || spaceKnown)
      ? (explicitId ?? space.activeId ?? "")
      : null;
  const subscribed = enabled && sseMode() !== "mock";
  const remote = useSharedResource(sessionsResource, key, {
    enabled: subscribed,
    refreshToken: refreshKey,
  });

  const pagesKey = subscribed ? key : null;
  const subscribePages = useCallback(
    (listener: () => void) =>
      pagesKey === null ? () => {} : sessionPages.subscribe(pagesKey, listener),
    [pagesKey],
  );
  const getPages = useCallback(
    () => (pagesKey === null ? EMPTY_PAGES : sessionPages.peek(pagesKey)),
    [pagesKey],
  );
  const pages = useSyncExternalStore(
    subscribePages,
    getPages,
    () => EMPTY_PAGES,
  );

  const firstPage = remote.data;
  const firstCursor = firstPage?.nextCursor ?? null;
  useEffect(() => {
    if (pagesKey === null || firstPage === undefined) return;
    sessionPages.reconcile(pagesKey, firstPage.nextCursor, firstPage.scope);
  }, [pagesKey, firstPage]);

  const loadMore = useCallback(async () => {
    if (pagesKey === null || firstPage === undefined) return;
    await sessionPages.loadMore(pagesKey, firstCursor, firstPage.scope);
  }, [pagesKey, firstPage, firstCursor]);

  // While the space list is still loading, the scope is the tab's remembered id, not yet validated against the
  // account's spaces (116 cold load of `/?session=…`: a stale remembered id answered with an empty list and the
  // sidebar said "No sessions yet" before the real space resolved). The fetch still starts early, but an empty or
  // failed answer for an unvalidated scope is not a verdict: stay in the loading state until the space resolves.
  const scopeProvisional =
    space.loading && (explicitId === undefined || explicitId === space.activeId);
  const hasRows = Boolean(remote.data?.groups.some((group) => group.items.length > 0));
  // A disabled consumer is not loading anything. Reporting `idle` as loading
  // left the mobile drawer stuck on "正在加载会话…" whenever the page supplied
  // an empty list (a brand-new account), because its own hook is disabled
  // exactly then and would never settle.
  const waiting =
    subscribed &&
    (remote.status === "idle" ||
      (remote.loading && !remote.data) ||
      (scopeProvisional && !hasRows));

  // Bounded wait (116 phone cold load, second cause): a hanging Space list keeps the key null (no remembered
  // space) or the scope provisional, and nothing else ever ends the wait. Each attempt (scope + retry count) gets
  // SESSION_LIST_WAIT_MS of skeleton, then reports an error naming what is still pending.
  const [attempt, setAttempt] = useState(0);
  const attemptToken = `${key === null ? "pending" : `scope:${key}`}:${attempt}`;
  const [stalledToken, setStalledToken] = useState<string | null>(null);
  // A wait that ended (an answer, or the consumer was disabled) forgets its stall, so a later wait for the same
  // scope and attempt (e.g. the drawer reopened after a team switch emptied the entry) gets its full skeleton time
  // again. Adjusting state during render, not in an effect, avoids one frame of a stale error.
  if (!waiting && stalledToken !== null) setStalledToken(null);
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setTimeout(() => setStalledToken(attemptToken), SESSION_LIST_WAIT_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, attemptToken]);
  const stalled = waiting && stalledToken === attemptToken;
  const spaceLoading = space.loading;
  const refreshSpaces = space.refresh;
  const retry = useCallback(() => {
    setAttempt((value) => value + 1);
    if (spaceLoading) refreshSpaces();
    notifySessionListChanged();
  }, [refreshSpaces, spaceLoading]);

  const groupsRef = useRef<{
    first: SessionGroup[] | undefined;
    pages: SessionPagesSnapshot;
    merged: SessionGroup[];
    latestIds: string[];
  } | null>(null);

  if (sseMode() === "mock") {
    return {
      groups: MOCK_SESSIONS,
      archivedCount: 0,
      loading: false,
      error: false,
      stalledOn: null,
      retry,
      hasMore: false,
      loadingMore: false,
      loadMoreError: false,
      olderPagesLoaded: 0,
      latestOlderPageIds: EMPTY_IDS,
      loadMore,
    };
  }

  const firstGroups = firstPage?.groups;
  let groups: SessionGroup[] = firstGroups ?? EMPTY_GROUPS;
  let latestOlderPageIds = EMPTY_IDS;
  if (firstGroups && pages.pages.length) {
    // Keep identity stable between renders so subscribers' memo/effects do not churn.
    const cached = groupsRef.current;
    if (cached && cached.first === firstGroups && cached.pages === pages) {
      groups = cached.merged;
      latestOlderPageIds = cached.latestIds;
    } else {
      groups = mergeSessionGroups(
        firstGroups,
        ...pages.pages.map((page) => page.groups),
      );
      latestOlderPageIds = newIdsOfLastPage(firstGroups, pages.pages);
      groupsRef.current = {
        first: firstGroups,
        pages,
        merged: groups,
        latestIds: latestOlderPageIds,
      };
    }
  }
  const tailCursor = pages.pages.length
    ? pages.pages[pages.pages.length - 1].nextCursor
    : firstCursor;

  return {
    groups,
    archivedCount: firstPage?.archivedCount ?? 0,
    loading: waiting && !stalled,
    error:
      stalled ||
      (subscribed && !scopeProvisional && remote.error !== null && remote.data === undefined),
    stalledOn: stalled ? (space.loading ? "spaces" : "sessions") : null,
    retry,
    hasMore: tailCursor !== null,
    loadingMore: pages.loadingMore,
    loadMoreError: pages.loadMoreError,
    olderPagesLoaded: pages.pages.length,
    latestOlderPageIds,
    loadMore,
  };
}

const EMPTY_IDS: string[] = [];

/** Ids the last loaded page adds to the list, in page order (repeats of earlier rows excluded). */
function newIdsOfLastPage(first: SessionGroup[], pages: LoadedPage[]): string[] {
  const seen = new Set<string>();
  const remember = (groups: SessionGroup[]) => {
    for (const group of groups) for (const item of group.items) seen.add(item.id);
  };
  remember(first);
  pages.slice(0, -1).forEach((page) => remember(page.groups));
  const ids: string[] = [];
  for (const group of pages[pages.length - 1]?.groups ?? []) {
    for (const item of group.items) {
      if (!seen.has(item.id)) {
        seen.add(item.id);
        ids.push(item.id);
      }
    }
  }
  return ids;
}

export function useSessions(
  refreshKey = 0,
  enabled = true,
  workspaceId?: string,
): SessionGroup[] {
  return useSessionList(refreshKey, enabled, workspaceId).groups;
}

// ─── Paged search / archived listing ─────────────────────────────────────

export interface SessionSearchQuery {
  query: string;
  view?: "active" | "archived";
  workspaceId?: string;
  limit?: number;
}

export interface SessionSearchState {
  groups: SessionGroup[];
  hasMore: boolean;
  loading: boolean;
  loadingMore: boolean;
  /** First-page failure: nothing loaded to show. */
  error: boolean;
  /** "Load more" failure: loaded rows stay, the cursor is kept for a retry. */
  loadMoreError: boolean;
  loadMore: () => Promise<void>;
  reload: () => void;
}

/**
 * Paged `GET /sessions?q=` (title, id or the user's own messages). Not shared
 * and not polled: a query change or `reload()` restarts from the first page,
 * and responses for a superseded query are dropped.
 */
export function useSessionSearch(
  params: SessionSearchQuery,
  enabled = true,
  fetchPage: typeof fetchSessions = fetchSessions,
): SessionSearchState {
  const key = JSON.stringify([
    params.query.trim(),
    params.view ?? "active",
    params.workspaceId ?? null,
    params.limit ?? null,
  ]);
  const [pages, setPages] = useState<LoadedPage[]>([]);
  const [status, setStatus] = useState({
    loading: false,
    loadingMore: false,
    error: false,
    loadMoreError: false,
  });
  const [reloadToken, setReloadToken] = useState(0);
  const generation = useRef(0);
  const moreInFlight = useRef<number | null>(null);
  const pagesRef = useRef(pages);
  pagesRef.current = pages;
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;

  const request = useCallback(
    async (cursor: string | null): Promise<LoadedPage> => {
      const [query, view, workspaceId, limit] = JSON.parse(key) as [
        string,
        "active" | "archived",
        string | null,
        number | null,
      ];
      return toPageData(
        await fetchRef.current({
          query,
          view,
          workspaceId: workspaceId ?? undefined,
          limit: limit ?? undefined,
          cursor,
        }),
      );
    },
    [key],
  );

  useEffect(() => {
    const gen = ++generation.current;
    moreInFlight.current = null;
    setPages([]);
    if (!enabled || !(JSON.parse(key) as [string])[0]) {
      setStatus({
        loading: false,
        loadingMore: false,
        error: false,
        loadMoreError: false,
      });
      return;
    }
    setStatus({
      loading: true,
      loadingMore: false,
      error: false,
      loadMoreError: false,
    });
    request(null).then(
      (page) => {
        if (gen !== generation.current) return;
        setPages([page]);
        setStatus({
          loading: false,
          loadingMore: false,
          error: false,
          loadMoreError: false,
        });
      },
      () => {
        if (gen !== generation.current) return;
        setStatus({
          loading: false,
          loadingMore: false,
          error: true,
          loadMoreError: false,
        });
      },
    );
  }, [enabled, key, reloadToken, request]);

  const loadMore = useCallback(async () => {
    const gen = generation.current;
    const loaded = pagesRef.current;
    const cursor = loaded.length ? loaded[loaded.length - 1].nextCursor : null;
    if (!cursor || moreInFlight.current === gen) return;
    moreInFlight.current = gen;
    setStatus((s) => ({ ...s, loadingMore: true, loadMoreError: false }));
    try {
      const page = await request(cursor);
      if (gen !== generation.current) return;
      setPages((current) => [...current, page]);
      setStatus((s) => ({ ...s, loadingMore: false }));
    } catch {
      if (gen !== generation.current) return;
      setStatus((s) => ({ ...s, loadingMore: false, loadMoreError: true }));
    } finally {
      if (moreInFlight.current === gen) moreInFlight.current = null;
    }
  }, [request]);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);
  const last = pages[pages.length - 1];
  const groups = useMemo(
    () =>
      pages.length
        ? mergeSessionGroups(...pages.map((page) => page.groups))
        : EMPTY_GROUPS,
    [pages],
  );
  return {
    groups,
    hasMore: Boolean(last?.nextCursor),
    ...status,
    loadMore,
    reload,
  };
}
