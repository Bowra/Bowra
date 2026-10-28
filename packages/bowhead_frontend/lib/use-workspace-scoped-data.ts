"use client";

/**
 * Sprint β G2 — useWorkspaceScopedData hook.
 *
 * Wraps any (workspace_id: string) => Promise<T> fetcher so it:
 *   1. Auto-fetches on mount + when active workspace changes (via
 *      `bowhead:workspace-changed` custom event)
 *   2. Coalesces rapid switches via single 50ms setTimeout (P1-T-2 fix:
 *      no RAF — RAF + setTimeout double-debounce was buggy)
 *   3. Stale-response guard via generation counter — older fetch results
 *      that resolve AFTER newer ones are discarded (P0-T-1 G2 stale guard)
 *   4. Fallback retry up to 2 times on 403 (workspace revoked) — then
 *      redirects to /empty-workspace (P1-T-9)
 *
 * Usage:
 *   const { data, loading, error, refetch } = useWorkspaceScopedData(
 *     (ws_id) => fetchSomething(ws_id),
 *   );
 *
 * Replaces the manual pattern: `useEffect(() => fetcher(getActiveSpace()))`
 * scattered across 22 components.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { appPath } from "./base-path";
import { useSpace } from "./use-space";

const EVT_CHANGED = "bowhead:workspace-changed";
const COALESCE_MS = 50;
const MAX_FALLBACK_RETRIES = 2;

export interface ScopedDataState<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refetch: () => void;
}

export function useWorkspaceScopedData<T>(
  fetcher: (workspaceId: string) => Promise<T>,
  options: { skipIfNoWorkspace?: boolean } = {},
): ScopedDataState<T> {
  const { activeId } = useSpace();
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  // Generation counter for stale-response guard (P0-T-1)
  const genRef = useRef(0);
  // Coalesce timer (P1-T-2: single setTimeout, no RAF)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Fallback retry count (P1-T-9: max 2 then bail)
  const fallbackCountRef = useRef(0);

  const doFetch = useCallback(
    async (gen: number, wsId: string) => {
      setLoading(true);
      try {
        const result = await fetcher(wsId);
        // Stale-response guard — if a newer fetch has been scheduled, discard
        if (genRef.current !== gen) return;
        setData(result);
        setError(null);
        fallbackCountRef.current = 0;
      } catch (err) {
        if (genRef.current !== gen) return;
        const e = err instanceof Error ? err : new Error(String(err));
        // P1-T-9: 403 → fallback retry (workspace revoked while user idle)
        const status = (err as { status?: number })?.status;
        if (status === 403 && fallbackCountRef.current < MAX_FALLBACK_RETRIES) {
          fallbackCountRef.current++;
          setError(null);
          const nextGen = ++genRef.current;
          if (timerRef.current) clearTimeout(timerRef.current);
          timerRef.current = setTimeout(() => {
            timerRef.current = null;
            void doFetch(nextGen, wsId);
          }, COALESCE_MS);
          return;
        }
        if (status === 403) {
          // Exhausted — redirect to empty-workspace (server has no member rows for us)
          if (typeof window !== "undefined") {
            window.location.href = appPath("/empty-workspace/");
          }
          return;
        }
        setError(e);
      } finally {
        if (genRef.current === gen) {
          setLoading(false);
        }
      }
    },
    [fetcher],
  );

  const scheduleFetch = useCallback((workspaceIdOverride?: string) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    fallbackCountRef.current = 0;
    const gen = ++genRef.current;
    const wsId = workspaceIdOverride ?? activeId;
    if (!wsId) {
      if (options.skipIfNoWorkspace) {
        setLoading(false);
        return;
      }
      // Without ws_id, can't fetch — surface as no-op
      setData(null);
      setLoading(false);
      return;
    }
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      void doFetch(gen, wsId);
    }, COALESCE_MS);
  }, [activeId, doFetch, options.skipIfNoWorkspace]);

  // Initial + activeId-change auto-fetch
  useEffect(() => {
    scheduleFetch();
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [scheduleFetch]);

  // Listen for workspace-changed event (e.g. from another tab via storage event)
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<string>).detail;
      scheduleFetch(typeof detail === "string" ? detail : undefined);
    };
    window.addEventListener(EVT_CHANGED, handler);
    return () => window.removeEventListener(EVT_CHANGED, handler);
  }, [scheduleFetch]);

  return { data, loading, error, refetch: scheduleFetch };
}
