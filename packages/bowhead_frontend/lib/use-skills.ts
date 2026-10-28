"use client";

/**
 * chat-page-polish B4 — shared skill-list hook.
 *
 * Fix the P0 UX inconsistency: /profile showed "Enabled Skills 0/0", while /settings showed "ENABLED SKILLS 25/25".
 * Cause: both pages called fetchSkills separately; one raced / failed,
 * silently caught the error and retained null state, while the other succeeded. Users were left asking how many skills they actually had.
 *
 * Design:
 *   - Module-level cache (`_cache`) shared across components; the second mounter receives the first one's result
 *   - Module-level in-flight promise (`_inflight`) deduplicates concurrent mounts into 1 HTTP request
 *   - 5min stale TTL; also force-refresh on the 'bowhead:skills-changed' window event
 *     (settings/skills should dispatch after enabling/disabling)
 *
 * Every page displaying a skill count uses this hook, inherently keeping data consistent.
 */

import { useCallback, useEffect, useState } from "react";

import { fetchSkills, type SkillListDTO, type SkillSummaryDTO } from "./api";

const STALE_MS = 5 * 60_000;
const REFRESH_EVENT = "bowhead:skills-changed";

interface CacheEntry {
  data: SkillListDTO;
  ts: number;
}

let _cache: CacheEntry | null = null;
let _inflight: Promise<SkillListDTO> | null = null;

/** Visible for tests: reset module cache. */
export function _resetSkillsCacheForTest(): void {
  _cache = null;
  _inflight = null;
}

async function _loadFresh(): Promise<SkillListDTO> {
  if (_inflight) return _inflight;
  _inflight = fetchSkills()
    .then((data) => {
      _cache = { data, ts: Date.now() };
      return data;
    })
    .finally(() => { _inflight = null; });
  return _inflight;
}

/** Get cached data immediately if fresh, otherwise return null. */
function _getCachedIfFresh(): SkillListDTO | null {
  if (!_cache) return null;
  if (Date.now() - _cache.ts > STALE_MS) return null;
  return _cache.data;
}

export interface SkillCounts {
  enabled: number;
  total: number;
}

/** Pure helper — given a SkillListDTO compute counts. Exported for tests. */
export function computeSkillCounts(data: SkillListDTO | null): SkillCounts {
  if (!data) return { enabled: 0, total: 0 };
  const all = [...data.builtin, ...data.user_upload, ...data.user_generated];
  const enabled = all.filter((s: SkillSummaryDTO) => s.enabled).length;
  return { enabled, total: all.length };
}

/**
 * Subscribe to the shared skills list. Returns:
 *   { data, counts, loading, refresh }
 *
 * - data: the SkillListDTO or null while still loading first fetch
 * - counts: { enabled, total } convenient for header / overview cards
 * - loading: true only on initial fetch (cached / refresh do NOT flip this true)
 * - refresh: force reload bypass cache
 */
export function useSkills(): {
  data: SkillListDTO | null;
  counts: SkillCounts;
  loading: boolean;
  refresh: () => Promise<void>;
} {
  const [data, setData] = useState<SkillListDTO | null>(() => _getCachedIfFresh());
  const [loading, setLoading] = useState<boolean>(() => _getCachedIfFresh() === null);

  // Mount: hit cache or fetch fresh.
  useEffect(() => {
    let cancelled = false;
    const cached = _getCachedIfFresh();
    if (cached) {
      // Already have it, no work to do. setData is just a no-op if state equals.
      if (data === null) setData(cached);
      setLoading(false);
      return () => { cancelled = true; };
    }
    setLoading(true);
    void _loadFresh()
      .then((fresh) => {
        if (cancelled) return;
        setData(fresh);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        // On failure keep previous data (don't blank UI); just stop loading.
        setLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cross-component sync: any 'bowhead:skills-changed' triggers refresh
  // (settings/skills page should dispatch this after toggle).
  useEffect(() => {
    const onChange = (): void => {
      _cache = null;  // invalidate
      void _loadFresh().then((fresh) => setData(fresh)).catch(() => {});
    };
    if (typeof window !== "undefined") {
      window.addEventListener(REFRESH_EVENT, onChange);
      return () => window.removeEventListener(REFRESH_EVENT, onChange);
    }
    return () => { /* no-op SSR */ };
  }, []);

  const refresh = useCallback(async () => {
    _cache = null;
    const fresh = await _loadFresh();
    setData(fresh);
  }, []);

  return { data, counts: computeSkillCounts(data), loading, refresh };
}

/** External helpers can use this to notify other components a toggle happened. */
export function dispatchSkillsChanged(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(REFRESH_EVENT));
  }
}
