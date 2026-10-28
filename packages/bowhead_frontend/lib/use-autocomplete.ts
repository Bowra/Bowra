/**
 * HITL Tab autocomplete hook (#88).
 *
 * Pure frontend history-based prefix matching, zero LLM cost / zero latency.
 * Candidate sources (priority):
 *   1. Latest 5 current-session user_text entries (useCurrentSessionHistory)
 *   2. Latest 20 cross-session titles (shared list; localStorage cache TTL 1h supplies first frame)
 * Tab accepts; Esc or further typing dismisses current ghost.
 */

"use client";

import { useEffect, useMemo, useState } from "react";

import type { SessionGroup } from "@/lib/types";
import { useSessionList } from "@/lib/use-sessions";

const LS_CACHE_KEY = "bowhead.autocomplete.history.v1";
const LS_ENABLED_KEY = "bowhead.hitl_suggestions_enabled";
const LS_CACHE_TTL_MS = 60 * 60 * 1000; // 1h
// History stores display text only; @Agent /Skill there are not restorable exact references.
// Exclude these messages from Tab completion, or apparent resource selection inserts only ordinary text.
const RESOURCE_LOOKALIKE = /(?:^|\s)[@/]\S/u;

interface CachedHistory {
  texts: string[];
  fetched_at: number;
}

export function isAutocompleteEnabled(): boolean {
  if (typeof window === "undefined") return false;
  const raw = window.localStorage.getItem(LS_ENABLED_KEY);
  // default on; explicit "false" disables
  return raw !== "false";
}

/** Previously computed history (TTL 1h) supplies first-frame candidates without waiting for session list. */
export function readCachedHistory(): string[] {
  try {
    const raw = window.localStorage.getItem(LS_CACHE_KEY);
    if (!raw) return [];
    const cached = JSON.parse(raw) as CachedHistory;
    if (Date.now() - cached.fetched_at >= LS_CACHE_TTL_MS) return [];
    return Array.isArray(cached.texts) ? cached.texts : [];
  } catch {
    return [];
  }
}

/**
 * Session titles → completion history.
 *
 * Session list has only title (AI-generated short title); user_text details require /sessions/:id;
 * v1 compromises by using titles as history (short but precise).
 */
export function historyFromSessions(groups: SessionGroup[]): string[] {
  const texts = groups.flatMap((group) =>
    group.items
      .map((item) => item.title)
      .filter((title): title is string =>
        typeof title === "string"
        && title.length >= 3
        && !title.startsWith("s_")),  // Skip temporary session_id entries lacking generated titles.
  );
  return Array.from(new Set(texts)).slice(0, 20);
}

function writeCachedHistory(texts: string[]): void {
  try {
    window.localStorage.setItem(LS_CACHE_KEY, JSON.stringify({
      texts, fetched_at: Date.now(),
    } satisfies CachedHistory));
  } catch { /* ignore quota */ }
}

/** Find all history prefix matches, returning top N ghost suffixes (case-insensitive). */
export function pickCandidates(
  value: string, history: string[], maxN = 3,
): string[] {
  const v = value.trim();
  if (!v || v.length < 2) return [];
  const lo = v.toLowerCase();
  const out: string[] = [];
  for (const h of history) {
    if (!h || h === v) continue;
    if (RESOURCE_LOOKALIKE.test(h)) continue;
    if (h.toLowerCase().startsWith(lo) && h.length > v.length) {
      out.push(h.slice(v.length));
      if (out.length >= maxN) break;
    }
  }
  return out;
}

/**
 * Hook wraps history fetch + candidates + cycling + dismissal state.
 *
 * Match Claude Code autocomplete UX (claude-code-guide research):
 * - Tab / Right arrow (at end) accepts highlighted candidate
 * - Up / Down cycles among top N candidates
 * - Esc / changed first character dismisses
 *
 * @param value Current input text
 * @param currentSessionTexts Latest current-session user_texts (higher priority)
 */
export function useAutocomplete(
  value: string,
  currentSessionTexts: string[] = [],
): {
  suggestion: string | null;
  candidates: string[];
  index: number;
  cycle: (delta: 1 | -1) => void;
  dismiss: () => void;
} {
  // Read once on mount, like the fetch-on-mount effect this replaced.
  const [enabled] = useState(() => isAutocompleteEnabled());
  const [cachedHistory] = useState<string[]>(() =>
    enabled ? readCachedHistory() : [],
  );
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const [index, setIndex] = useState(0);

  // History uses shared session list (same sidebar/drawer/workspace-page data), no separate
  // `/api/v1/sessions?limit=20`; composer and sidebar always mount together, making that request pure duplication.
  const { groups } = useSessionList(0, enabled);
  const derived = useMemo(() => historyFromSessions(groups), [groups]);
  // Merge, rather than choose one: cached old titles outside current list remain valid candidates.
  const history = useMemo(
    () => Array.from(new Set([...derived, ...cachedHistory])),
    [derived, cachedHistory],
  );

  useEffect(() => {
    if (derived.length > 0) writeCachedHistory(derived);
  }, [derived]);

  const merged = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const t of [...currentSessionTexts, ...history]) {
      if (t && !seen.has(t)) {
        seen.add(t);
        out.push(t);
      }
    }
    return out;
  }, [currentSessionTexts, history]);

  const candidates = useMemo(() => {
    if (!enabled) return [];
    if (dismissedFor !== null && value.startsWith(dismissedFor)) return [];
    return pickCandidates(value, merged, 3);
  }, [enabled, value, merged, dismissedFor]);

  // Reset index on value change (avoid stale index targeting absent candidate).
  useEffect(() => {
    setIndex(0);
  }, [value]);

  const cycle = (delta: 1 | -1) => {
    if (candidates.length === 0) return;
    setIndex((i) => (i + delta + candidates.length) % candidates.length);
  };

  return {
    suggestion: candidates[index] ?? null,
    candidates,
    index,
    cycle,
    dismiss: () => setDismissedFor(value),
  };
}
