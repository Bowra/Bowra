"use client";

import { useEffect, useRef, useState } from "react";

/**
 * New-content dot for Results (Workbench Spec §10).
 *
 * Check only result identities (report chapter ids + result-list entry ids), never body text:
 * - Visible Results pane (desktop split / mobile Results tab) marks all current ids seen and clears dot.
 * - New ids appearing while hidden are new; mobile switch and reopen rail show dot.
 * - First opening an old session (no local record, neither running nor Results seen during this mount): all loaded results
 *   count as seen; lacking a local record must not mark all history new. After seeing Results or running, later arrivals count as new.
 * - Per-session localStorage records survive refresh: a live dot remains after F5; refresh after viewing still shows no dot.
 *
 * Pure local UI state, never sent to server or affecting chat data; when disabled it neither reads nor writes.
 */

export const RESULTS_SEEN_STORAGE_KEY = "bowhead.results_seen.v1";
const MAX_SESSIONS = 40;
const MAX_IDS_PER_SESSION = 500;

interface SeenRecord {
  seen: string[];
  at: number;
}
type SeenStore = Record<string, SeenRecord>;

function readStore(): SeenStore {
  try {
    const raw = window.localStorage.getItem(RESULTS_SEEN_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as SeenStore) : {};
  } catch {
    return {};
  }
}

export function readSeenRecord(sessionId: string): Set<string> | null {
  const record = readStore()[sessionId];
  if (!record || !Array.isArray(record.seen)) return null;
  return new Set(record.seen.filter((id): id is string => typeof id === "string"));
}

export function writeSeenRecord(sessionId: string, seen: Set<string>, now = Date.now()): void {
  try {
    const store = readStore();
    const ids = [...seen];
    store[sessionId] = { seen: ids.slice(Math.max(0, ids.length - MAX_IDS_PER_SESSION)), at: now };
    const kept = Object.entries(store)
      .sort((a, b) => (b[1]?.at ?? 0) - (a[1]?.at ?? 0))
      .slice(0, MAX_SESSIONS);
    window.localStorage.setItem(RESULTS_SEEN_STORAGE_KEY, JSON.stringify(Object.fromEntries(kept)));
  } catch {
    /* Private mode / quota exhausted: dot lasts only for this page. */
  }
}

export interface ResultsSeenStep {
  seen: Set<string>;
  hasNew: boolean;
  /** Whether seen must be persisted locally. */
  persist: boolean;
}

/**
 * Pure function: current result ids, pane visibility, and whether new ids count as new → next seen set and dot.
 * With `countNew=false` (first opening old session without seeing running), newly appearing ids join seen directly.
 */
export function stepResultsSeen(
  seen: Set<string>,
  ids: readonly string[],
  visible: boolean,
  countNew: boolean,
): ResultsSeenStep {
  const unseen = ids.filter((id) => !seen.has(id));
  if (visible || !countNew) {
    if (unseen.length === 0) return { seen, hasNew: false, persist: false };
    const next = new Set(seen);
    for (const id of unseen) next.add(id);
    return { seen: next, hasNew: false, persist: true };
  }
  return { seen, hasNew: unseen.length > 0, persist: false };
}

/**
 * @param enabled False (report-only view, creation panel open) always returns false and never accesses local storage.
 * @param sessionId Current session; absent (homepage before sending) always returns false.
 * @param ids All current result ids (report chapters + result list).
 * @param visible Whether Results pane is currently visible.
 * @param running Whether current turn is running (once observed, later results count as new).
 */
export function useResultsNewIndicator({
  enabled,
  sessionId,
  ids,
  visible,
  running,
}: {
  enabled: boolean;
  sessionId: string | null;
  ids: readonly string[];
  visible: boolean;
  running: boolean;
}): boolean {
  const [hasNew, setHasNew] = useState(false);
  const stateRef = useRef<{
    sessionId: string;
    seen: Set<string>;
    /** Local record existed at mount (determines whether arrivals count as new; fixed during this mount). */
    recordExisted: boolean;
    /** Whether local record exists now (only avoids duplicate writes). */
    persisted: boolean;
    sawRunning: boolean;
    /** Results pane was seen with content during this mount (later arrivals count as new). */
    sawVisible: boolean;
  } | null>(null);
  const idsKey = ids.join("\u0000");

  useEffect(() => {
    if (!enabled || !sessionId) {
      stateRef.current = null;
      setHasNew(false);
      return;
    }
    let state = stateRef.current;
    if (!state || state.sessionId !== sessionId) {
      const stored = readSeenRecord(sessionId);
      state = {
        sessionId,
        seen: stored ?? new Set<string>(),
        recordExisted: stored !== null,
        persisted: stored !== null,
        sawRunning: false,
        sawVisible: false,
      };
      stateRef.current = state;
    }
    if (running) state.sawRunning = true;
    const currentIds = idsKey ? idsKey.split("\u0000") : [];
    const countNew = state.recordExisted || state.sawRunning || state.sawVisible;
    const step = stepResultsSeen(state.seen, currentIds, visible, countNew);
    state.seen = step.seen;
    // Once counted as new, persist a local record so the dot survives refresh.
    if (step.persist || (step.hasNew && !state.persisted)) {
      writeSeenRecord(sessionId, state.seen);
      state.persisted = true;
    }
    if (visible && currentIds.length > 0) state.sawVisible = true;
    setHasNew(step.hasNew);
  }, [enabled, sessionId, idsKey, visible, running]);

  return enabled && Boolean(sessionId) && hasNew;
}
