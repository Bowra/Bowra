"use client";

/**
 * Report library data hook: first page + cursor pagination over GET /api/v1/canvas.
 *
 * It only owns data state (items, true total, next cursor); the reports page
 * decides how a "load more" affordance looks. A query change or `reload()`
 * restarts from the first page; responses for a superseded query are dropped.
 * A silent `reload({ silent: true })` (window focus, `bowhead.canvas.changed`)
 * refetches only the first page and merges it by id on top of the rows already
 * loaded, so later batches and the scroll position stay (Header, Language &
 * Paging Spec §3a).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchReportsPage,
  type ReportListItem,
  type ReportListPage,
  type ReportListQuery,
} from "./api";

/**
 * Put a refreshed first page on top of the loaded rows. The old first batch is replaced entirely (a row
 * missing from the refreshed first page was deleted or moved out of the filter), while rows of later
 * batches stay unless the fresh page now contains them.
 */
export function mergeRefreshedFirstPage(
  loaded: readonly ReportListItem[],
  firstPage: readonly ReportListItem[],
  oldFirstPageSize: number,
): ReportListItem[] {
  const fresh = new Set(firstPage.map((item) => item.id));
  return [...firstPage, ...loaded.slice(oldFirstPageSize).filter((item) => !fresh.has(item.id))];
}

/** Append a page to already-loaded rows, keeping the first occurrence of each id.
 * Rows can repeat when a report is updated between page requests. */
export function mergeReportPages(
  loaded: readonly ReportListItem[],
  next: readonly ReportListItem[],
): ReportListItem[] {
  const seen = new Set(loaded.map((item) => item.id));
  const merged = [...loaded];
  for (const item of next) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    merged.push(item);
  }
  return merged;
}

export interface ReportsListState {
  items: ReportListItem[];
  /** True count from the first page; cursor pages do not recount. */
  total: number;
  /** Reports hidden for having no sections (first page with include_empty=false), or null. */
  emptyTotal: number | null;
  /** Rows the latest first-page response delivered (the "first batch" a silent refresh replaces). */
  firstPageSize: number;
  nextCursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  /** First-page failure: there is nothing loaded to show. */
  error: unknown;
  /** "Load more" failure: already loaded rows stay visible and the cursor is kept for a retry. */
  loadMoreError: unknown;
}

const INITIAL: ReportsListState = {
  items: [],
  total: 0,
  emptyTotal: null,
  firstPageSize: 0,
  nextCursor: null,
  loading: true,
  loadingMore: false,
  error: null,
  loadMoreError: null,
};

export function useReportsList(
  query: ReportListQuery,
  fetchPage: (
    opts: ReportListQuery & { cursor?: string | null },
  ) => Promise<ReportListPage> = fetchReportsPage,
) {
  const key = JSON.stringify(query);
  const [state, setState] = useState<ReportsListState>(INITIAL);
  // Bumped on every query change / non-silent reload so late responses can be discarded.
  // Silent refreshes use their own counter: they must not discard an in-flight "load more".
  const generation = useRef(0);
  const silentGeneration = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;
  // Generation of the in-flight "load more" request. It guards double invocation
  // before React re-renders, and a query change or reload (new generation)
  // releases it so the new list can page immediately.
  const moreInFlightGen = useRef<number | null>(null);

  const loadFirst = useCallback(
    async (opts?: { silent?: boolean }) => {
      const silent = Boolean(opts?.silent);
      const gen = silent ? generation.current : ++generation.current;
      const silentGen = ++silentGeneration.current;
      if (!silent)
        setState((s) => ({
          ...s,
          loading: true,
          loadingMore: false,
          error: null,
          loadMoreError: null,
        }));
      try {
        const page = await fetchRef.current(JSON.parse(key) as ReportListQuery);
        if (gen !== generation.current || silentGen !== silentGeneration.current) return;
        setState((s) => {
          // Silent refresh with later batches loaded: replace the old first batch, keep the later ones and
          // their cursor. An in-flight "load more" keeps running and merges when it lands.
          const keepTail = silent && !s.loading && !s.error && s.items.length > s.firstPageSize;
          return {
            items: keepTail ? mergeRefreshedFirstPage(s.items, page.items, s.firstPageSize) : page.items,
            total: page.total ?? page.items.length,
            emptyTotal: page.empty_total ?? null,
            firstPageSize: page.items.length,
            nextCursor: keepTail ? s.nextCursor : page.next_cursor,
            loading: false,
            loadingMore: silent ? s.loadingMore : false,
            error: null,
            loadMoreError: silent ? s.loadMoreError : null,
          };
        });
      } catch (error) {
        if (gen !== generation.current || silentGen !== silentGeneration.current) return;
        // A failed silent refresh leaves the list as it is.
        if (silent) return;
        setState((s) => ({ ...s, loading: false, loadingMore: false, error }));
      }
    },
    [key],
  );

  useEffect(() => {
    void loadFirst();
    return () => {
      generation.current += 1;
    };
  }, [loadFirst]);

  const loadMore = useCallback(async () => {
    const current = stateRef.current;
    const gen = generation.current;
    if (
      !current.nextCursor ||
      current.loading ||
      moreInFlightGen.current === gen
    )
      return;
    moreInFlightGen.current = gen;
    const cursor = current.nextCursor;
    setState((s) => ({ ...s, loadingMore: true, loadMoreError: null }));
    try {
      const page = await fetchRef.current({
        ...(JSON.parse(key) as ReportListQuery),
        cursor,
      });
      if (gen !== generation.current) return;
      setState((s) => ({
        ...s,
        items: mergeReportPages(s.items, page.items),
        // Cursor pages return total=null; keep the first-page count.
        total: page.total ?? s.total,
        nextCursor: page.next_cursor,
        loadingMore: false,
      }));
    } catch (loadMoreError) {
      if (gen !== generation.current) return;
      setState((s) => ({ ...s, loadingMore: false, loadMoreError }));
    } finally {
      if (moreInFlightGen.current === gen) moreInFlightGen.current = null;
    }
  }, [key]);

  /** A deleted row leaves the list: the shown and total counts both drop by one. */
  const removeItem = useCallback((id: string) => {
    setState((s) =>
      s.items.some((item) => item.id === id)
        ? { ...s, items: s.items.filter((item) => item.id !== id), total: Math.max(0, s.total - 1) }
        : s,
    );
  }, []);

  return {
    ...state,
    hasMore: state.nextCursor !== null,
    reload: loadFirst,
    loadMore,
    removeItem,
  };
}
