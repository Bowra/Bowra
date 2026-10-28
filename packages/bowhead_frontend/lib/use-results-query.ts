"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  queryResults,
  ResultsApiError,
  type ResultItemDTO,
  type ResultsApiErrorCode,
  type ResultsFiltersDTO,
  type ResultsQueryResponse,
  type ResultsScope,
} from "@/lib/results-api";

export const RESULTS_EXPLORER_SEARCH_DEBOUNCE_MS = 200;
const PAGE_SIZE = 50;
const DEFAULT_FILTERS: ResultsFiltersDTO = {
  lens: "all",
  primary: "all",
  secondary: "all",
  technical_only: false,
};

export type ResultsCompletedView = {
  query: string;
  filters: ResultsFiltersDTO;
};

type QueryState = {
  contextKey: string | null;
  targetRequestKey: string | null;
  completedView: ResultsCompletedView | null;
  completedRequestKey: string | null;
  errorAnchorKey: string | null;
  items: ResultItemDTO[];
  loading: boolean;
  loadingMore: boolean;
  loadingPrevious: boolean;
  checkingChanges: boolean;
  error: ResultsApiErrorCode | null;
  authorizedTotal: number | null;
  matchedTotal: number | null;
  groupCounts: ResultsQueryResponse["group_counts"] | null;
  projectionStatus: ResultsQueryResponse["projection_status"] | null;
  pageStart: number;
  hasMore: boolean;
  hasPrevious: boolean;
  newResultsCount: number | null;
  projectionChanged: boolean;
  unavailable: boolean;
  fallbackAllowed: boolean;
};

const EMPTY_STATE: QueryState = {
  contextKey: null,
  targetRequestKey: null,
  completedView: null,
  completedRequestKey: null,
  errorAnchorKey: null,
  items: [],
  loading: false,
  loadingMore: false,
  loadingPrevious: false,
  checkingChanges: false,
  error: null,
  authorizedTotal: null,
  matchedTotal: null,
  groupCounts: null,
  projectionStatus: null,
  pageStart: 0,
  hasMore: false,
  hasPrevious: false,
  newResultsCount: null,
  projectionChanged: false,
  unavailable: false,
  fallbackAllowed: false,
};

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function asResultsError(error: unknown): ResultsApiError {
  return error instanceof ResultsApiError
    ? error
    : new ResultsApiError("results_request_failed");
}

function appendUnique(
  current: readonly ResultItemDTO[],
  next: readonly ResultItemDTO[],
): ResultItemDTO[] {
  const byKey = new Map(current.map((item) => [item.key, item]));
  for (const item of next) byKey.set(item.key, item);
  return Array.from(byKey.values());
}

function prependUnique(
  current: readonly ResultItemDTO[],
  previous: readonly ResultItemDTO[],
): ResultItemDTO[] {
  const previousKeys = new Set(previous.map((item) => item.key));
  return [...previous, ...current.filter((item) => !previousKeys.has(item.key))];
}

function filtersKey(filters: ResultsFiltersDTO): string {
  return [
    filters.lens,
    filters.primary,
    filters.secondary,
    filters.technical_only ? "1" : "0",
  ].join(":");
}

function isDefaultFilters(filters: ResultsFiltersDTO): boolean {
  return filtersKey(filters) === filtersKey(DEFAULT_FILTERS);
}

// Commit the displayed conditions, window and counts in one state transition.
// Pending intent never relabels the last successfully completed collection.
function reduceResultsPage(
  current: QueryState,
  response: ResultsQueryResponse,
  view: ResultsCompletedView,
  contextKey: string,
  direction: "base" | "next" | "previous" = "base",
): QueryState {
  return {
    ...EMPTY_STATE,
    contextKey,
    targetRequestKey: `${view.query}\n${filtersKey(view.filters)}`,
    completedView: view,
    completedRequestKey: `${view.query}\n${filtersKey(view.filters)}`,
    items: direction === "next"
      ? appendUnique(current.items, response.items)
      : direction === "previous"
        ? prependUnique(current.items, response.items)
        : response.items,
    authorizedTotal: response.authorized_total,
    matchedTotal: response.matched_total,
    groupCounts: response.group_counts,
    projectionStatus: response.projection_status,
    pageStart: direction === "next" ? current.pageStart : response.page_start,
    hasMore: direction === "previous" ? current.hasMore : response.next_cursor !== null,
    hasPrevious: direction === "next" ? current.hasPrevious : response.previous_cursor !== null,
    // Changes is an independent notification, not the page request's loading state.
    checkingChanges: direction === "base" ? false : current.checkingChanges,
    newResultsCount: direction === "base" ? null : current.newResultsCount,
    projectionChanged: direction === "base" ? false : current.projectionChanged,
  };
}

export function useResultsQuery({
  enabled,
  entryId = null,
  selectionKey,
  filters = DEFAULT_FILTERS,
  query,
  scope,
  sessionId,
}: {
  enabled: boolean;
  entryId?: string | null;
  selectionKey?: string | null;
  filters?: ResultsFiltersDTO;
  query: string;
  scope: ResultsScope;
  sessionId: string | null;
}) {
  const normalizedQuery = query.trim();
  const normalizedFilters = useMemo<ResultsFiltersDTO>(() => ({
    lens: filters.lens,
    primary: filters.primary,
    secondary: filters.secondary,
    technical_only: filters.technical_only,
  }), [filters.lens, filters.primary, filters.secondary, filters.technical_only]);
  const activeRequestKey = `${normalizedQuery}\n${filtersKey(normalizedFilters)}`;
  const scopeKey = JSON.stringify([scope, sessionId, entryId]);
  const [state, setState] = useState<QueryState>(EMPTY_STATE);
  const [catalogItems, setCatalogItems] = useState<ResultItemDTO[]>([]);
  const catalogRef = useRef(new Map<string, ResultItemDTO>());
  const catalogScopeKeyRef = useRef(scopeKey);
  const responseRef = useRef<ResultsQueryResponse | null>(null);
  const nextCursorRef = useRef<string | null>(null);
  const previousCursorRef = useRef<string | null>(null);
  const visibleControllerRef = useRef<AbortController | null>(null);
  const backgroundControllerRef = useRef<AbortController | null>(null);
  const locateControllerRef = useRef<AbortController | null>(null);
  const changesControllerRef = useRef<AbortController | null>(null);
  const visibleGenerationRef = useRef(0);
  const backgroundGenerationRef = useRef(0);
  const locateGenerationRef = useRef(0);
  const changesGenerationRef = useRef(0);
  const scopeKeyRef = useRef(scopeKey);
  const enabledRef = useRef(enabled);
  const selectionKeyRef = useRef(selectionKey);
  const visibleAnchorRef = useRef<string | null>(null);
  const activeRequestKeyRef = useRef(activeRequestKey);
  const activeQueryRef = useRef(normalizedQuery);
  const activeFiltersRef = useRef(normalizedFilters);
  const debouncedQueryRef = useRef<string | null>(null);
  const lastVisibleActionRef = useRef<"base" | "next" | "previous">("base");
  scopeKeyRef.current = scopeKey;
  enabledRef.current = enabled;
  selectionKeyRef.current = selectionKey;
  activeRequestKeyRef.current = activeRequestKey;
  activeQueryRef.current = normalizedQuery;
  activeFiltersRef.current = normalizedFilters;

  const requestIsCurrent = useCallback((
    controller: AbortController,
    requestedScope: string,
    requestedKey?: string,
  ) => (
    enabledRef.current
    && !controller.signal.aborted
    && scopeKeyRef.current === requestedScope
    && (requestedKey === undefined || activeRequestKeyRef.current === requestedKey)
  ), []);

  const mergeCatalog = useCallback((items: readonly ResultItemDTO[]) => {
    if (items.length === 0) return;
    let changed = false;
    for (const item of items) {
      if (catalogRef.current.get(item.key) !== item) {
        catalogRef.current.set(item.key, item);
        changed = true;
      }
    }
    if (changed) setCatalogItems(Array.from(catalogRef.current.values()));
  }, []);

  const resetPageRefs = useCallback((response: ResultsQueryResponse | null) => {
    responseRef.current = response;
    nextCursorRef.current = response?.next_cursor ?? null;
    previousCursorRef.current = response?.previous_cursor ?? null;
  }, []);

  const purgeScope = useCallback(() => {
    visibleControllerRef.current?.abort();
    backgroundControllerRef.current?.abort();
    locateControllerRef.current?.abort();
    changesControllerRef.current?.abort();
    visibleAnchorRef.current = null;
    resetPageRefs(null);
    catalogRef.current.clear();
    setCatalogItems([]);
    setState({
      ...EMPTY_STATE,
      contextKey: scopeKeyRef.current,
      targetRequestKey: activeRequestKeyRef.current,
      error: "results_scope_unavailable",
      unavailable: true,
    });
  }, [resetPageRefs]);

  const loadVisible = useCallback(async (
    targetQuery: string,
    targetFilters: ResultsFiltersDTO,
  ): Promise<ResultItemDTO[]> => {
    if (!enabled || !sessionId) return [];
    visibleControllerRef.current?.abort();
    changesControllerRef.current?.abort();
    visibleAnchorRef.current = null;
    const controller = new AbortController();
    visibleControllerRef.current = controller;
    const generation = visibleGenerationRef.current + 1;
    visibleGenerationRef.current = generation;
    const requestedScope = scopeKey;
    const requestedKey = `${targetQuery}\n${filtersKey(targetFilters)}`;
    resetPageRefs(null);
    lastVisibleActionRef.current = "base";
    setState((current) => ({
      ...current,
      contextKey: requestedScope,
      targetRequestKey: requestedKey,
      loading: true,
      loadingMore: false,
      loadingPrevious: false,
      checkingChanges: false,
      error: null,
      errorAnchorKey: null,
      newResultsCount: null,
      projectionChanged: false,
      unavailable: false,
      fallbackAllowed: false,
    }));
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "explorer",
          query: targetQuery,
          filters: targetFilters,
          page_size: PAGE_SIZE,
        },
        signal: controller.signal,
      });
      if (
        visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)
      ) return [];
      resetPageRefs(response);
      mergeCatalog(response.items);
      setState((current) => reduceResultsPage(current, response, {
        query: targetQuery, filters: targetFilters,
      }, requestedScope));
      return response.items;
    } catch (error) {
      if (isAbort(error) || visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)) return [];
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) {
        purgeScope();
        return [];
      }
      setState((current) => ({
        ...current,
        loading: false,
        loadingMore: false,
        loadingPrevious: false,
        error: resultsError.code,
        fallbackAllowed: current.completedView === null
          && resultsError.allowsLegacyFallback,
      }));
      return [];
    }
  }, [enabled, mergeCatalog, purgeScope, requestIsCurrent, resetPageRefs, scope, scopeKey, sessionId]);

  const loadNext = useCallback(async (): Promise<void> => {
    const currentResponse = responseRef.current;
    const cursor = nextCursorRef.current;
    if (
      !enabled
      || !sessionId
      || state.loading
      || state.loadingMore
      || state.loadingPrevious
      || state.contextKey !== scopeKey
      || state.completedRequestKey !== activeRequestKeyRef.current
      || !currentResponse
      || !cursor
    ) return;
    visibleControllerRef.current?.abort();
    visibleAnchorRef.current = null;
    const controller = new AbortController();
    visibleControllerRef.current = controller;
    const generation = visibleGenerationRef.current + 1;
    visibleGenerationRef.current = generation;
    const requestedScope = scopeKey;
    const requestedKey = activeRequestKeyRef.current;
    const targetQuery = activeQueryRef.current;
    const targetFilters = activeFiltersRef.current;
    lastVisibleActionRef.current = "next";
    setState((current) => ({ ...current, loadingMore: true, error: null }));
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "explorer",
          query: targetQuery,
          filters: targetFilters,
          page_size: PAGE_SIZE,
          snapshot_token: currentResponse.snapshot_token,
          cursor,
        },
        signal: controller.signal,
      });
      if (
        visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)
      ) return;
      responseRef.current = response;
      nextCursorRef.current = response.next_cursor;
      mergeCatalog(response.items);
      setState((current) => reduceResultsPage(current, response, {
        query: targetQuery, filters: targetFilters,
      }, requestedScope, "next"));
    } catch (error) {
      if (isAbort(error) || visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)) return;
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) {
        purgeScope();
        return;
      }
      if (resultsError.code === "results_snapshot_stale") resetPageRefs(null);
      setState((current) => ({
        ...current,
        loadingMore: false,
        error: resultsError.code,
        hasMore: resultsError.code === "results_snapshot_stale" ? false : current.hasMore,
        hasPrevious: resultsError.code === "results_snapshot_stale" ? false : current.hasPrevious,
      }));
    }
  }, [enabled, mergeCatalog, purgeScope, requestIsCurrent, resetPageRefs, scope, scopeKey, sessionId, state.contextKey, state.completedRequestKey, state.loading, state.loadingMore, state.loadingPrevious]);

  const loadPrevious = useCallback(async (): Promise<void> => {
    const currentResponse = responseRef.current;
    const cursor = previousCursorRef.current;
    if (
      !enabled
      || !sessionId
      || state.loading
      || state.loadingMore
      || state.loadingPrevious
      || state.contextKey !== scopeKey
      || state.completedRequestKey !== activeRequestKeyRef.current
      || !currentResponse
      || !cursor
    ) return;
    visibleControllerRef.current?.abort();
    visibleAnchorRef.current = null;
    const controller = new AbortController();
    visibleControllerRef.current = controller;
    const generation = visibleGenerationRef.current + 1;
    visibleGenerationRef.current = generation;
    const requestedScope = scopeKey;
    const requestedKey = activeRequestKeyRef.current;
    const targetQuery = activeQueryRef.current;
    const targetFilters = activeFiltersRef.current;
    lastVisibleActionRef.current = "previous";
    setState((current) => ({ ...current, loadingPrevious: true, error: null }));
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "explorer",
          query: targetQuery,
          filters: targetFilters,
          page_size: PAGE_SIZE,
          snapshot_token: currentResponse.snapshot_token,
          cursor,
        },
        signal: controller.signal,
      });
      if (
        visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)
      ) return;
      responseRef.current = response;
      previousCursorRef.current = response.previous_cursor;
      mergeCatalog(response.items);
      setState((current) => reduceResultsPage(current, response, {
        query: targetQuery, filters: targetFilters,
      }, requestedScope, "previous"));
    } catch (error) {
      if (isAbort(error) || visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)) return;
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) {
        purgeScope();
        return;
      }
      if (resultsError.code === "results_snapshot_stale") resetPageRefs(null);
      setState((current) => ({
        ...current,
        loadingPrevious: false,
        error: resultsError.code,
        hasMore: resultsError.code === "results_snapshot_stale" ? false : current.hasMore,
        hasPrevious: resultsError.code === "results_snapshot_stale" ? false : current.hasPrevious,
      }));
    }
  }, [enabled, mergeCatalog, purgeScope, requestIsCurrent, resetPageRefs, scope, scopeKey, sessionId, state.contextKey, state.completedRequestKey, state.loading, state.loadingMore, state.loadingPrevious]);

  const refreshBase = useCallback(async (): Promise<ResultItemDTO[]> => {
    if (!enabled || !sessionId) return [];
    if (activeQueryRef.current === "" && isDefaultFilters(activeFiltersRef.current)) {
      return loadVisible("", DEFAULT_FILTERS);
    }
    backgroundControllerRef.current?.abort();
    const controller = new AbortController();
    backgroundControllerRef.current = controller;
    const generation = backgroundGenerationRef.current + 1;
    backgroundGenerationRef.current = generation;
    const requestedScope = scopeKey;
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "explorer",
          query: "",
          filters: DEFAULT_FILTERS,
          page_size: PAGE_SIZE,
        },
        signal: controller.signal,
      });
      if (
        backgroundGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope)
      ) return [];
      mergeCatalog(response.items);
      return response.items;
    } catch (error) {
      if (isAbort(error) || backgroundGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope)) return [];
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) purgeScope();
      return [];
    }
  }, [enabled, loadVisible, mergeCatalog, purgeScope, requestIsCurrent, scope, scopeKey, sessionId]);

  const locate = useCallback(async (key: string): Promise<ResultItemDTO | null> => {
    if (!enabled || !sessionId) return null;
    const cached = catalogScopeKeyRef.current === scopeKey ? catalogRef.current.get(key) : null;
    if (cached) return cached;
    locateControllerRef.current?.abort();
    const controller = new AbortController();
    locateControllerRef.current = controller;
    const generation = locateGenerationRef.current + 1;
    locateGenerationRef.current = generation;
    const requestedScope = scopeKey;
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "locate",
          query: "",
          filters: DEFAULT_FILTERS,
          page_size: PAGE_SIZE,
          anchor_key: key,
        },
        signal: controller.signal,
      });
      if (
        locateGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope)
      ) return null;
      mergeCatalog(response.items);
      return response.items.find((item) => item.key === key) ?? null;
    } catch (error) {
      if (isAbort(error) || locateGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope)) return null;
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) purgeScope();
      return null;
    }
  }, [enabled, mergeCatalog, purgeScope, requestIsCurrent, scope, scopeKey, sessionId]);

  const locatePage = useCallback(async (
    key: string,
    fallbackToBase = false,
  ): Promise<ResultItemDTO | null> => {
    if (!enabled || !sessionId) return null;
    if (selectionKeyRef.current !== undefined && selectionKeyRef.current !== key) return null;
    visibleControllerRef.current?.abort();
    changesControllerRef.current?.abort();
    visibleAnchorRef.current = key;
    const controller = new AbortController();
    visibleControllerRef.current = controller;
    const generation = visibleGenerationRef.current + 1;
    visibleGenerationRef.current = generation;
    const requestedScope = scopeKey;
    const requestedKey = activeRequestKeyRef.current;
    const targetQuery = activeQueryRef.current;
    const targetFilters = activeFiltersRef.current;
    lastVisibleActionRef.current = "base";
    setState((current) => ({
      ...current, contextKey: requestedScope, loading: true, loadingMore: false,
      targetRequestKey: requestedKey,
      loadingPrevious: false, checkingChanges: false, error: null, errorAnchorKey: null,
    }));
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "locate",
          query: targetQuery,
          filters: targetFilters,
          page_size: PAGE_SIZE,
          anchor_key: key,
        },
        signal: controller.signal,
      });
      if (
        visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)
        || (selectionKeyRef.current !== undefined && selectionKeyRef.current !== key)
      ) return null;
      resetPageRefs(response);
      mergeCatalog(response.items);
      setState((current) => reduceResultsPage(current, response, {
        query: targetQuery, filters: targetFilters,
      }, requestedScope));
      return response.items.find((item) => item.key === key) ?? null;
    } catch (error) {
      if (isAbort(error) || visibleGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)
        || (selectionKeyRef.current !== undefined && selectionKeyRef.current !== key)) return null;
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) {
        purgeScope();
        return null;
      }
      if (fallbackToBase && resultsError.code === "results_anchor_not_in_view") {
        const fallback = loadVisible(targetQuery, targetFilters);
        const fallbackGeneration = visibleGenerationRef.current;
        const fallbackItems = await fallback;
        if (
          visibleGenerationRef.current === fallbackGeneration
          && scopeKeyRef.current === requestedScope
          && activeRequestKeyRef.current === requestedKey
          && (selectionKeyRef.current === undefined || selectionKeyRef.current === key)
        ) {
          setState((current) => {
            if (responseRef.current === null) return current;
            const found = fallbackItems.some((item) => item.key === key);
            return {
              ...current,
              error: found ? null : resultsError.code,
              errorAnchorKey: found ? null : key,
            };
          });
        }
        return null;
      }
      setState((current) => ({
        ...current, loading: false, error: resultsError.code, errorAnchorKey: key,
      }));
      return null;
    }
  }, [enabled, loadVisible, mergeCatalog, purgeScope, requestIsCurrent, resetPageRefs, scope, scopeKey, sessionId]);

  useEffect(() => {
    if (selectionKey === undefined || visibleAnchorRef.current === null
      || visibleAnchorRef.current === selectionKey) return;
    visibleControllerRef.current?.abort();
    visibleAnchorRef.current = null;
    setState((current) => ({ ...current, loading: false, error: null, errorAnchorKey: null }));
  }, [selectionKey]);

  const checkChanges = useCallback(async (): Promise<void> => {
    const currentResponse = responseRef.current;
    if (!enabled || !sessionId || !currentResponse || state.loading
      || state.contextKey !== scopeKey
      || state.completedRequestKey !== activeRequestKeyRef.current) return;
    changesControllerRef.current?.abort();
    const controller = new AbortController();
    changesControllerRef.current = controller;
    const generation = changesGenerationRef.current + 1;
    changesGenerationRef.current = generation;
    const requestedScope = scopeKey;
    const requestedKey = activeRequestKeyRef.current;
    setState((current) => ({ ...current, checkingChanges: true }));
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "changes",
          query: activeQueryRef.current,
          filters: activeFiltersRef.current,
          snapshot_token: currentResponse.snapshot_token,
        },
        signal: controller.signal,
      });
      if (
        changesGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)
      ) return;
      setState((current) => ({
        ...current,
        checkingChanges: false,
        newResultsCount: response.new_results.count,
        projectionChanged: response.new_results.projection_changed,
      }));
    } catch (error) {
      if (isAbort(error) || changesGenerationRef.current !== generation
        || !requestIsCurrent(controller, requestedScope, requestedKey)) return;
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) {
        purgeScope();
        return;
      }
      setState((current) => ({ ...current, checkingChanges: false }));
    }
  }, [enabled, purgeScope, requestIsCurrent, scope, scopeKey, sessionId, state.contextKey, state.completedRequestKey, state.loading]);

  const acceptChanges = useCallback(async (): Promise<ResultItemDTO[]> => (
    loadVisible(activeQueryRef.current, activeFiltersRef.current)
  ), [loadVisible]);

  useEffect(() => {
    visibleControllerRef.current?.abort();
    backgroundControllerRef.current?.abort();
    locateControllerRef.current?.abort();
    changesControllerRef.current?.abort();
    resetPageRefs(null);
    catalogRef.current.clear();
    catalogScopeKeyRef.current = scopeKey;
    setCatalogItems([]);
    setState({ ...EMPTY_STATE, contextKey: scopeKey });
  }, [resetPageRefs, scopeKey]);

  useEffect(() => {
    if (!enabled || !sessionId) {
      visibleControllerRef.current?.abort();
      backgroundControllerRef.current?.abort();
      locateControllerRef.current?.abort();
      changesControllerRef.current?.abort();
      resetPageRefs(null);
      catalogRef.current.clear();
      setCatalogItems([]);
      setState(EMPTY_STATE);
      return;
    }
    visibleControllerRef.current?.abort();
    changesControllerRef.current?.abort();
    // Unfiltered catalog reads serve the independently selected Viewer. They
    // survive a filter change, but are cancelled on scope/entry change above.
    resetPageRefs(null);
    setState((current) => ({
      ...current,
      contextKey: scopeKey,
      targetRequestKey: activeRequestKey,
      loading: true,
      loadingMore: false,
      loadingPrevious: false,
      checkingChanges: false,
      error: null,
      errorAnchorKey: null,
      newResultsCount: null,
      projectionChanged: false,
      unavailable: false,
      fallbackAllowed: false,
    }));
    const delay = normalizedQuery && debouncedQueryRef.current !== normalizedQuery
      ? RESULTS_EXPLORER_SEARCH_DEBOUNCE_MS : 0;
    debouncedQueryRef.current = normalizedQuery;
    const timer = window.setTimeout(() => {
      const latestSelectionKey = selectionKeyRef.current ?? null;
      if (latestSelectionKey) {
        void locatePage(latestSelectionKey, true);
        return;
      }
      void loadVisible(normalizedQuery, normalizedFilters);
    }, delay);
    return () => {
      window.clearTimeout(timer);
      visibleControllerRef.current?.abort();
    };
  }, [activeRequestKey, enabled, loadVisible, locatePage, normalizedFilters, normalizedQuery, resetPageRefs, scopeKey, sessionId]);

  useEffect(() => () => {
    visibleControllerRef.current?.abort();
    backgroundControllerRef.current?.abort();
    locateControllerRef.current?.abort();
    changesControllerRef.current?.abort();
    catalogRef.current.clear();
  }, []);

  const retry = useCallback((): Promise<void | ResultItemDTO[]> => {
    if (responseRef.current && lastVisibleActionRef.current === "next") return loadNext();
    if (responseRef.current && lastVisibleActionRef.current === "previous") return loadPrevious();
    return loadVisible(activeQueryRef.current, activeFiltersRef.current);
  }, [loadNext, loadPrevious, loadVisible]);

  // Scope changes must hide metadata on the first render, before effects reset it.
  const scopedState = enabled && state.contextKey === scopeKey ? state : EMPTY_STATE;
  const intentChanged = scopedState.targetRequestKey !== activeRequestKey;
  const displayedState = useMemo(() => intentChanged ? {
    ...scopedState, error: null, errorAnchorKey: null, loadingMore: false,
    loadingPrevious: false, checkingChanges: false, newResultsCount: null, projectionChanged: false,
  } : scopedState, [intentChanged, scopedState]);
  const targetPending = enabled && Boolean(sessionId)
    && (displayedState.contextKey !== scopeKey
      || intentChanged || (displayedState.completedRequestKey !== activeRequestKey && !displayedState.error));
  return useMemo(() => ({
    ...displayedState,
    loading: displayedState.loading || targetPending,
    catalogItems: scopedState === EMPTY_STATE ? [] : catalogItems,
    errorDirection: displayedState.error === null ? null : lastVisibleActionRef.current,
    loadedCount: displayedState.items.length,
    retry,
    loadMore: loadNext,
    loadNext,
    loadPrevious,
    refreshBase,
    locate,
    locatePage,
    checkChanges,
    acceptChanges,
  }), [acceptChanges, catalogItems, checkChanges, displayedState, loadNext, loadPrevious, locate, locatePage, refreshBase, retry, scopedState, targetPending]);
}
