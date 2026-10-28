"use client";

import { useSyncExternalStore } from "react";

import { renameSession, SessionArchiveError } from "@/lib/api";
import { notifySessionListChanged } from "@/lib/session-list-events";

/**
 * Session rename, shared by every surface that offers it (sidebar row, phone drawer row, chat
 * top bar). The new title is shown at once on all of them through one window-wide optimistic
 * store; the server write follows. Each rename takes a per-session sequence number, so only the
 * latest rename of a session may roll its title back (on failure) or settle it (on success): an
 * older request finishing late can never undo a newer one. A settled title stays until the
 * refreshed session list reports the same title (`reconcileSessionTitles`); from then on the
 * loaded list is the source again.
 */

/** Upper bound the gateway enforces on an owner-chosen session title (trimmed characters). */
export const SESSION_TITLE_MAX_CHARS = 100;

interface TitleOverride {
  title: string;
  seq: number;
  settled: boolean;
}

let entries: ReadonlyMap<string, TitleOverride> = new Map();
let titles: ReadonlyMap<string, string> = new Map();
let nextSeq = 0;
const listeners = new Set<() => void>();

function commit(next: Map<string, TitleOverride>) {
  entries = next;
  titles = new Map([...next].map(([id, entry]) => [id, entry.title]));
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const EMPTY: ReadonlyMap<string, string> = new Map();

/** Optimistic titles by session id; apply as `overrides.get(id) ?? loadedTitle`. */
export function useSessionTitleOverrides(): ReadonlyMap<string, string> {
  return useSyncExternalStore(
    subscribe,
    () => titles,
    () => EMPTY,
  );
}

/**
 * Drop settled overrides whose title the loaded list now reports. Pending renames are kept
 * (a list fetched before the save may still carry the old title).
 */
export function reconcileSessionTitles(
  loaded: Iterable<{ id: string; title: string }>,
): void {
  let next: Map<string, TitleOverride> | null = null;
  for (const item of loaded) {
    const entry = entries.get(item.id);
    if (!entry?.settled || entry.title !== item.title) continue;
    next ??= new Map(entries);
    next.delete(item.id);
  }
  if (next) commit(next);
}

/** Tests only: forget every optimistic title. */
export function resetSessionTitleOverridesForTest() {
  nextSeq = 0;
  commit(new Map());
}

/**
 * Show `title` everywhere at once, then persist it. Throws the persistence error after
 * rolling the optimistic title back (only if no newer rename replaced it); callers report it.
 */
export async function submitSessionRename(
  sessionId: string,
  title: string,
  persist: (sessionId: string, title: string) => Promise<unknown> = renameSession,
): Promise<void> {
  const seq = ++nextSeq;
  commit(new Map(entries).set(sessionId, { title, seq, settled: false }));
  const isLatest = () => entries.get(sessionId)?.seq === seq;
  try {
    await persist(sessionId, title);
  } catch (error) {
    if (isLatest()) {
      const next = new Map(entries);
      next.delete(sessionId);
      commit(next);
    }
    throw error;
  }
  if (isLatest()) {
    commit(new Map(entries).set(sessionId, { title, seq, settled: true }));
  }
  notifySessionListChanged();
}

export type SessionRenameErrorKey =
  | "rename_too_long"
  | "rename_invalid"
  | "rename_not_found"
  | "rename_failed";

/** Message key (in the `session` namespace) for a failed rename. */
export function sessionRenameErrorKey(error: unknown): SessionRenameErrorKey {
  if (!(error instanceof SessionArchiveError)) return "rename_failed";
  if (error.code === "session_title_too_long") return "rename_too_long";
  if (error.code === "session_title_invalid") return "rename_invalid";
  if (error.code === "session_not_found") return "rename_not_found";
  return "rename_failed";
}

/** Prefill for the rename field: an untitled session lists its id as the title; start empty instead. */
export function renameInitialTitle(sessionId: string, title: string | null | undefined): string {
  return title && title !== sessionId ? title : "";
}
