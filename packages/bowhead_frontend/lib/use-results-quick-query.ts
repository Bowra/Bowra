"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  queryResults,
  ResultsApiError,
  type ResultItemDTO,
  type ResultsApiErrorCode,
  type ResultsScope,
} from "@/lib/results-api";

export const RESULTS_QUICK_SEARCH_DEBOUNCE_MS = 200;

type QuickQueryState = {
  items: ResultItemDTO[];
  loading: boolean;
  error: ResultsApiErrorCode | null;
  authorizedTotal: number | null;
  matchedTotal: number | null;
  hasMore: boolean;
};

const EMPTY_STATE: QuickQueryState = {
  items: [],
  loading: false,
  error: null,
  authorizedTotal: null,
  matchedTotal: null,
  hasMore: false,
};

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function asResultsError(error: unknown): ResultsApiError {
  return error instanceof ResultsApiError
    ? error
    : new ResultsApiError("results_request_failed");
}

export function useResultsQuickQuery({
  currentKey,
  enabled,
  onScopePurged,
  open,
  query,
  recentKeys,
  scope,
  sessionId,
}: {
  currentKey: string | null;
  enabled: boolean;
  onScopePurged?: () => void;
  open: boolean;
  query: string;
  recentKeys: readonly string[];
  scope: ResultsScope;
  sessionId: string | null;
}) {
  const normalizedQuery = query.trim();
  const recentSignature = recentKeys.join("\u001f");
  const scopeKey = `${scope}:${sessionId ?? ""}`;
  const [state, setState] = useState<QuickQueryState>(EMPTY_STATE);
  const controllerRef = useRef<AbortController | null>(null);
  const generationRef = useRef(0);
  const requestIdentityRef = useRef("");
  const onScopePurgedRef = useRef(onScopePurged);
  onScopePurgedRef.current = onScopePurged;

  const load = useCallback(async (): Promise<void> => {
    if (!enabled || !open || !sessionId) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const requestIdentity = [
      scopeKey,
      normalizedQuery,
      currentKey ?? "",
      recentSignature,
    ].join("\u001e");
    requestIdentityRef.current = requestIdentity;
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await queryResults({
        sessionId,
        scope,
        request: {
          surface: "quick",
          query: normalizedQuery,
          page_size: normalizedQuery ? 8 : 10,
          current_key: currentKey ?? undefined,
          recent_keys: recentSignature ? recentSignature.split("\u001f") : [],
        },
        signal: controller.signal,
      });
      if (
        generationRef.current !== generation
        || requestIdentityRef.current !== requestIdentity
      ) return;
      setState({
        items: response.items,
        loading: false,
        error: null,
        authorizedTotal: response.authorized_total,
        matchedTotal: response.matched_total,
        hasMore: response.has_more,
      });
    } catch (error) {
      if (isAbort(error) || generationRef.current !== generation) return;
      const resultsError = asResultsError(error);
      if (resultsError.purgesScope) {
        onScopePurgedRef.current?.();
        setState({
          ...EMPTY_STATE,
          error: resultsError.code,
        });
        return;
      }
      setState((current) => ({
        ...current,
        loading: false,
        error: resultsError.code,
      }));
    }
  }, [
    currentKey,
    enabled,
    normalizedQuery,
    open,
    recentSignature,
    scope,
    scopeKey,
    sessionId,
  ]);

  useEffect(() => {
    generationRef.current += 1;
    controllerRef.current?.abort();
    if (!enabled || !open || !sessionId) {
      requestIdentityRef.current = "";
      setState(EMPTY_STATE);
      return;
    }
    setState((current) => ({ ...current, loading: true, error: null }));
    const timer = window.setTimeout(
      () => { void load(); },
      normalizedQuery ? RESULTS_QUICK_SEARCH_DEBOUNCE_MS : 0,
    );
    return () => {
      window.clearTimeout(timer);
      controllerRef.current?.abort();
    };
  }, [enabled, load, normalizedQuery, open, sessionId]);

  useEffect(() => () => controllerRef.current?.abort(), []);

  return useMemo(() => ({
    ...state,
    retry: load,
  }), [load, state]);
}
