"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { appFetch, appPath } from "@/lib/base-path";
import { reportDisplayTitle } from "@/lib/report-title";
import { spaceHeader } from "@/lib/space-storage";

export type UnifiedWorkKind =
  | "session"
  | "report"
  | "file"
  | "knowledge";

export interface UnifiedWorkSearchItem {
  kind: UnifiedWorkKind;
  id: string;
  title: string;
  session_id: string | null;
  snippet: string | null;
  updated_at: string | null;
  /** Reports only: names an untitled report the way the Reports list does. */
  created_at: string | null;
  /** Sessions only: the hit came from the title or from one of the user's own messages (quoted in `snippet`). */
  match: "title" | "content" | null;
}

export interface UnifiedWorkSearchGroup {
  kind: UnifiedWorkKind;
  unavailable: boolean;
  /** The source ran out of time: empty items mean "not searched", not "no matches". */
  timedOut: boolean;
  items: UnifiedWorkSearchItem[];
  /** Every readable match, not just these items (sessions only; null when the source cannot count). */
  total: number | null;
  /** Continues this group with {@link fetchUnifiedSessionSearchPage} (sessions only). */
  nextCursor: string | null;
}

export interface UnifiedWorkSearchResponse {
  query: string;
  partial: boolean;
  groups: UnifiedWorkSearchGroup[];
}

export type UnifiedWorkSearchState =
  | { status: "idle" }
  | { status: "typing"; remaining: number }
  | { status: "loading" }
  | { status: "ready"; response: UnifiedWorkSearchResponse }
  | { status: "error" };

const SEARCH_DEBOUNCE_MS = 250;
const SEARCH_REQUEST_TIMEOUT_MS = 7_000;
const KINDS: UnifiedWorkKind[] = [
  "session",
  "report",
  "file",
  "knowledge",
];

export async function fetchUnifiedWorkSearch(
  query: string,
  options: { signal?: AbortSignal; limit?: number } = {},
): Promise<UnifiedWorkSearchResponse> {
  const normalizedQuery = query.trim();
  const response = await appFetch("/api/v1/search", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...spaceHeader() },
    body: JSON.stringify({
      query: normalizedQuery,
      limit: options.limit ?? SEARCH_GROUP_LIMIT,
    }),
    signal: options.signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`unified search: ${response.status}`);
  const result = normalizeResponse(await response.json());
  if (result.query !== normalizedQuery) throw new Error("stale search response");
  if (result.groups.every((group) => group.unavailable)) {
    throw new Error("unified search unavailable");
  }
  return result;
}

/** Items per group in the first response (Header, Language & Paging spec 4b: each group shows 5 first). */
export const SEARCH_GROUP_LIMIT = 5;

/**
 * How many results the search found, for the result-count announcement. Sums each group's own total so it agrees
 * with the group headings; the session total is the server's count of every match (pass the latest one from the
 * paged responses as `sessionTotal`: it does not grow with "Show more", it only follows the server). A group
 * without a total counts the items it shows. The sum is a floor (`atLeast`) when those items may be a cut (a full
 * first page, or a session cursor) or when a source was unavailable and could not be counted at all.
 */
export function searchResultCount(
  groups: readonly UnifiedWorkSearchGroup[],
  sessionTotal: number | null = null,
): { count: number; atLeast: boolean } {
  let count = 0;
  let atLeast = false;
  for (const group of groups) {
    if (group.unavailable) {
      atLeast = true;
      continue;
    }
    const total = group.kind === "session" ? (sessionTotal ?? group.total) : group.total;
    if (total !== null) {
      count += total;
      continue;
    }
    count += group.items.length;
    if (group.items.length >= SEARCH_GROUP_LIMIT || group.nextCursor) atLeast = true;
  }
  return { count, atLeast };
}

/** Page size of "Show more sessions" (Header, Language & Paging spec 4b: first 5, then 20 at a time). */
export const SESSION_SEARCH_PAGE_SIZE = 20;

export interface UnifiedSessionSearchPage {
  items: UnifiedWorkSearchItem[];
  total: number | null;
  nextCursor: string | null;
}

/** Next page of the session group for `query`, continuing from `cursor` (bound to that query by the server). */
export async function fetchUnifiedSessionSearchPage(
  query: string,
  cursor: string,
  options: { signal?: AbortSignal; limit?: number } = {},
): Promise<UnifiedSessionSearchPage> {
  const normalizedQuery = query.trim();
  const response = await appFetch("/api/v1/search", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...spaceHeader() },
    body: JSON.stringify({
      query: normalizedQuery,
      limit: options.limit ?? SESSION_SEARCH_PAGE_SIZE,
      kinds: ["session"],
      cursor,
    }),
    signal: options.signal,
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`unified search: ${response.status}`);
  const result = normalizeResponse(await response.json());
  if (result.query !== normalizedQuery) throw new Error("stale search response");
  const group = result.groups.find((candidate) => candidate.kind === "session");
  if (!group || group.unavailable) throw new Error("session search unavailable");
  return { items: group.items, total: group.total, nextCursor: group.nextCursor };
}

export interface SessionSearchMore {
  /** Sessions appended after the first response, in order, without ids already shown. */
  items: UnifiedWorkSearchItem[];
  /** Cursor for the next page; starts at the first response's. */
  nextCursor: string | null;
  total: number | null;
  loading: boolean;
  error: boolean;
  /** Ids the latest page appended, in order (focus and the announcement follow them). */
  lastPageIds: string[];
}

const NO_MORE_SESSIONS: SessionSearchMore = {
  items: [],
  nextCursor: null,
  total: null,
  loading: false,
  error: false,
  lastPageIds: [],
};

export function useUnifiedWorkSearch(
  query: string,
  enabled = true,
): UnifiedWorkSearchState & {
  retry: () => void;
  /** Sessions beyond the first response ("Show more sessions"); reset with every new query. */
  moreSessions: SessionSearchMore;
  loadMoreSessions: () => Promise<void>;
} {
  const [state, setState] = useState<UnifiedWorkSearchState>({
    status: "idle",
  });
  const [retryKey, setRetryKey] = useState(0);
  const [moreSessions, setMoreSessions] =
    useState<SessionSearchMore>(NO_MORE_SESSIONS);
  const moreRef = useRef(moreSessions);
  moreRef.current = moreSessions;
  const sequenceRef = useRef(0);
  const moreControllerRef = useRef<AbortController | null>(null);
  const retry = useCallback(() => setRetryKey((key) => key + 1), []);

  useEffect(() => {
    const normalized = query.trim();
    const sequence = ++sequenceRef.current;
    moreControllerRef.current?.abort();
    moreControllerRef.current = null;
    setMoreSessions(NO_MORE_SESSIONS);
    if (!enabled) {
      setState({ status: "idle" });
      return;
    }
    if (normalized.length === 0) {
      setState({ status: "idle" });
      return;
    }
    if (normalized.length < 2) {
      setState({ status: "typing", remaining: 2 - normalized.length });
      return;
    }

    setState({ status: "loading" });
    const controller = new AbortController();
    let requestTimer: number | null = null;
    const timer = window.setTimeout(() => {
      requestTimer = window.setTimeout(() => {
        controller.abort();
        if (sequenceRef.current === sequence) {
          setState({ status: "error" });
        }
      }, SEARCH_REQUEST_TIMEOUT_MS);
      void fetchUnifiedWorkSearch(normalized, { signal: controller.signal })
        .then((response) => {
          if (requestTimer !== null) window.clearTimeout(requestTimer);
          if (sequenceRef.current !== sequence || controller.signal.aborted) {
            return;
          }
          setState({ status: "ready", response });
          const sessions = response.groups.find(
            (group) => group.kind === "session",
          );
          setMoreSessions({
            ...NO_MORE_SESSIONS,
            nextCursor: sessions?.nextCursor ?? null,
            total: sessions?.total ?? null,
          });
        })
        .catch((error: unknown) => {
          if (requestTimer !== null) window.clearTimeout(requestTimer);
          if (
            sequenceRef.current !== sequence ||
            controller.signal.aborted ||
            (error instanceof DOMException && error.name === "AbortError")
          ) {
            return;
          }
          setState({ status: "error" });
        });
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      if (requestTimer !== null) window.clearTimeout(requestTimer);
      controller.abort();
    };
  }, [enabled, query, retryKey]);

  const loadMoreSessions = useCallback(async () => {
    const current = moreRef.current;
    if (!current.nextCursor || current.loading) return;
    const sequence = sequenceRef.current;
    const loading = { ...current, loading: true, error: false };
    moreRef.current = loading;
    setMoreSessions(loading);
    // Same budget as the first search: a hung request becomes a recoverable error.
    const controller = new AbortController();
    moreControllerRef.current = controller;
    const timer = window.setTimeout(
      () => controller.abort(),
      SEARCH_REQUEST_TIMEOUT_MS,
    );
    try {
      const page = await fetchUnifiedSessionSearchPage(
        query,
        current.nextCursor,
        { signal: controller.signal },
      );
      if (sequenceRef.current !== sequence) return;
      setMoreSessions((latest) => {
        const seen = new Set(latest.items.map((item) => item.id));
        const appended = page.items.filter((item) => !seen.has(item.id));
        return {
          items: [...latest.items, ...appended],
          nextCursor: page.nextCursor,
          // Cursor pages carry no total; the first response's count stands.
          total: page.total ?? latest.total,
          loading: false,
          error: false,
          lastPageIds: appended.map((item) => item.id),
        };
      });
    } catch {
      if (sequenceRef.current !== sequence) return;
      // Loaded rows stay and the cursor is kept, so Retry continues from the same place.
      setMoreSessions((latest) => ({ ...latest, loading: false, error: true }));
    } finally {
      window.clearTimeout(timer);
      if (moreControllerRef.current === controller) {
        moreControllerRef.current = null;
      }
    }
  }, [query]);

  return { ...state, retry, moreSessions, loadMoreSessions };
}

export function unifiedWorkResultHref(item: UnifiedWorkSearchItem): string {
  if (item.kind === "session") {
    return `/?session=${encodeURIComponent(item.session_id ?? item.id)}`;
  }
  if (item.kind === "report") {
    if (!item.session_id) return "/reports/";
    return `/?session=${encodeURIComponent(item.session_id)}&report=1`;
  }
  if (item.kind === "file") {
    return `/files/?focus=${encodeURIComponent(item.id)}`;
  }
  return `/knowledge/?file_id=${encodeURIComponent(item.id)}`;
}

export function unifiedWorkResultAppHref(
  item: UnifiedWorkSearchItem,
): string {
  return appPath(unifiedWorkResultHref(item));
}

function normalizeResponse(value: unknown): UnifiedWorkSearchResponse {
  if (!value || typeof value !== "object") throw new Error("invalid search");
  const raw = value as Record<string, unknown>;
  if (
    typeof raw.query !== "string" ||
    typeof raw.partial !== "boolean" ||
    !Array.isArray(raw.groups)
  ) {
    throw new Error("invalid search");
  }
  const byKind = new Map<UnifiedWorkKind, UnifiedWorkSearchGroup>();
  for (const group of raw.groups) {
    if (!group || typeof group !== "object") continue;
    const candidate = group as Record<string, unknown>;
    if (!isKind(candidate.kind) || !Array.isArray(candidate.items)) continue;
    const kind = candidate.kind;
    byKind.set(kind, {
      kind,
      unavailable: candidate.unavailable === true,
      timedOut: candidate.timed_out === true,
      total:
        typeof candidate.total === "number" && candidate.total >= 0
          ? candidate.total
          : null,
      nextCursor:
        typeof candidate.next_cursor === "string" && candidate.next_cursor
          ? candidate.next_cursor
          : null,
      items: candidate.items
        .map(normalizeItem)
        .filter(
          (item): item is UnifiedWorkSearchItem =>
            item !== null && item.kind === kind,
        ),
    });
  }
  const groups = KINDS.map(
    (kind) =>
      byKind.get(kind) ?? {
        kind,
        unavailable: true,
        timedOut: false,
        items: [],
        total: null,
        nextCursor: null,
      },
  );
  return {
    query: raw.query,
    partial: raw.partial || groups.some((group) => group.unavailable),
    groups,
  };
}

function normalizeItem(value: unknown): UnifiedWorkSearchItem | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (
    !isKind(raw.kind) ||
    typeof raw.id !== "string" ||
    typeof raw.title !== "string"
  ) {
    return null;
  }
  const id = raw.id.trim();
  const updatedAt = typeof raw.updated_at === "string" ? raw.updated_at : null;
  // An untitled report arrives with an empty title: show the same fallback the
  // report list shows ("YYYY-MM-DD HH:mm Report"), never the canvas id.
  const title =
    raw.kind === "report"
      ? reportDisplayTitle({
          id,
          title: raw.title,
          first_section_title: null,
          created_at:
            typeof raw.created_at === "string" && raw.created_at
              ? raw.created_at
              : (updatedAt ?? ""),
        })
      : raw.title.trim();
  if (!id || !title) return null;
  return {
    kind: raw.kind,
    id,
    title,
    created_at: typeof raw.created_at === "string" ? raw.created_at : null,
    session_id: typeof raw.session_id === "string" ? raw.session_id : null,
    snippet: typeof raw.snippet === "string" ? raw.snippet : null,
    updated_at: updatedAt,
    match:
      raw.match === "title" || raw.match === "content" ? raw.match : null,
  };
}

function isKind(value: unknown): value is UnifiedWorkKind {
  return typeof value === "string" && KINDS.includes(value as UnifiedWorkKind);
}
