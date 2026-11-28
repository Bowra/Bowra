"use client";

/**
 * Whether Results was open in a conversation, kept per conversation in this browser (chat running state R2.7).
 *
 * Opening a conversation restores the reader's last choice for it instead of opening whenever a report exists;
 * with no record Results stays closed and its strip shows the count. Only the reader's own opening and closing,
 * and the one file Lead names at the end of a Turn, write it, so switching conversations never copies one
 * conversation's state onto another. Reading state, not an account setting: it stays in this browser.
 */

export const RESULTS_OPEN_STORAGE_KEY = "bowhead.results_open.v1";
const MAX_SESSIONS = 40;

type OpenState = "open" | "closed";
type OpenStore = Record<string, { state: OpenState; at: number }>;

function readStore(): OpenStore {
  try {
    const raw = window.localStorage.getItem(RESULTS_OPEN_STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as OpenStore) : {};
  } catch {
    return {};
  }
}

/** The reader's last choice for this conversation, or null when there is none. */
export function readResultsOpen(sessionId: string | null | undefined): OpenState | null {
  if (!sessionId) return null;
  const state = readStore()[sessionId]?.state;
  return state === "open" || state === "closed" ? state : null;
}

export function writeResultsOpen(sessionId: string | null | undefined, state: OpenState, now = Date.now()): void {
  if (!sessionId) return;
  try {
    const store = readStore();
    store[sessionId] = { state, at: now };
    const kept = Object.entries(store)
      .sort((a, b) => (b[1]?.at ?? 0) - (a[1]?.at ?? 0))
      .slice(0, MAX_SESSIONS);
    window.localStorage.setItem(RESULTS_OPEN_STORAGE_KEY, JSON.stringify(Object.fromEntries(kept)));
  } catch {
    /* Private mode or a full quota: the choice lasts for this page only. */
  }
}

/** The file Lead's delivery review named for the reader (`lead.final.payload.open_for_reader`), if any. */
export function openForReader(finalPayload: unknown): string | null {
  if (!finalPayload || typeof finalPayload !== "object") return null;
  const value = (finalPayload as { open_for_reader?: unknown }).open_for_reader;
  return typeof value === "string" && value.trim() ? value.trim() : null;
}
