"use client";

/**
 * My Turns in a conversation that a team Agent answered: whether I may share each with the Agent's
 * Owner as an evaluation-case source, its share state, and my thumbs-down (evaluation-credibility
 * tech design §8). The answer's "⋯" menu and the thumbs-down dialog read and write the same entry, so
 * sharing from either place shows in both.
 *
 * One request per conversation, shared by every answer in it: the first answer that mounts loads the
 * list and the rest read the same result. An answer missing from a loaded list (the Turn that just
 * finished) reloads it once; Turns that aren't shareable stay checked and never reload.
 */

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { listTurnEvalShares, setTurnAnswerFeedback, setTurnEvalShare, type AnswerFeedback, type TurnEvalShare } from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";

interface SessionShares {
  items: Map<string, TurnEvalShare>;
  checked: Set<string>;
}

const sessions = new Map<string, SessionShares>();
const loading = new Map<string, Promise<void>>();
const waitingTurns = new Map<string, Set<string>>();
const listeners = new Set<() => void>();
let version = 0;

function emit() {
  version += 1;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function ensureLoaded(sessionId: string, turnId: string): void {
  const known = sessions.get(sessionId);
  if (known && (known.items.has(turnId) || known.checked.has(turnId))) return;
  const waiting = waitingTurns.get(sessionId) ?? new Set<string>();
  waiting.add(turnId);
  waitingTurns.set(sessionId, waiting);
  if (loading.has(sessionId)) return;
  // Answers that mount in the same render join one request; every Turn waiting when it settles is checked.
  const settle = (items: TurnEvalShare[] | null) => {
    const previous = sessions.get(sessionId);
    const checked = new Set([...(previous?.checked ?? []), ...(waitingTurns.get(sessionId) ?? [])]);
    waitingTurns.delete(sessionId);
    // Sharing is optional: a failed load shows no entry rather than an error on every answer.
    const map = items ? new Map(items.map((item) => [item.turn_id, item])) : (previous?.items ?? new Map());
    for (const id of map.keys()) checked.add(id);
    sessions.set(sessionId, { items: map, checked });
  };
  const request = Promise.resolve()
    .then(() => listTurnEvalShares(sessionId))
    .then((value) => settle(value.items), () => settle(null))
    .finally(() => {
      loading.delete(sessionId);
      emit();
    });
  loading.set(sessionId, request);
}

/** Test seam: forget every loaded conversation. */
export function _resetTurnEvalShares(): void {
  sessions.clear();
  loading.clear();
  waitingTurns.clear();
  emit();
}

export interface TurnEvalShareControl {
  item: TurnEvalShare;
  /** Whether this Turn can be shared with the Agent's Owner (false for the requester's own Agent). */
  shareable: boolean;
  busy: boolean;
  /** Share or withdraw; resolves to the new state, or rejects with the API error. */
  toggle: () => Promise<boolean>;
  /** Share (true) or withdraw (false) when that differs from the current state; resolves to the new state. */
  setShared: (shared: boolean) => Promise<boolean>;
  /** Give or update the thumbs-down (`value`) or clear it (`null`); rejects with the API error. */
  setFeedback: (value: { reason_codes: string[]; comment: string | null } | null) => Promise<AnswerFeedback | null>;
}

/** The entry for one completed team-Agent answer, or null for any other answer. */
export function useTurnEvalShare(sessionId: string | null | undefined, turnId: string | null | undefined): TurnEvalShareControl | null {
  useSyncExternalStore(subscribe, () => version, () => version);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (sessionId && turnId) ensureLoaded(sessionId, turnId);
  }, [sessionId, turnId]);

  const item = sessionId && turnId ? sessions.get(sessionId)?.items.get(turnId) : undefined;
  // Older servers listed shareable Turns only and sent no `shareable`.
  const shareable = item ? item.shareable !== false : false;

  const run = useCallback(async <T,>(action: (current: TurnEvalShare) => Promise<T>): Promise<T> => {
    const current = sessionId && turnId ? sessions.get(sessionId)?.items.get(turnId) : undefined;
    if (!sessionId || !current) throw new Error("turn_eval_share_missing");
    setBusy(true);
    try {
      return await action(current);
    } catch (cause) {
      // 404: the Turn can no longer be shared or rated (deleted, or its Space is gone); drop the entry.
      if (cause instanceof ApiError && cause.status === 404) {
        sessions.get(sessionId)?.items.delete(current.turn_id);
      }
      throw cause;
    } finally {
      setBusy(false);
      emit();
    }
  }, [sessionId, turnId]);

  const update = useCallback((current: TurnEvalShare, patch: Partial<TurnEvalShare>) => {
    if (!sessionId) return;
    const latest = sessions.get(sessionId)?.items.get(current.turn_id) ?? current;
    sessions.get(sessionId)?.items.set(current.turn_id, { ...latest, ...patch });
  }, [sessionId]);

  const setShared = useCallback((shared: boolean) => run(async (current) => {
    if (current.shared === shared) return shared;
    const value = await setTurnEvalShare(current.turn_id, shared);
    update(current, { shared: value.shared });
    return value.shared;
  }), [run, update]);

  const toggle = useCallback(() => {
    const current = sessionId && turnId ? sessions.get(sessionId)?.items.get(turnId) : undefined;
    return setShared(!current?.shared);
  }, [sessionId, turnId, setShared]);

  const setFeedback = useCallback((value: { reason_codes: string[]; comment: string | null } | null) => run(async (current) => {
    const result = await setTurnAnswerFeedback(current.turn_id, value);
    update(current, { feedback: result.feedback });
    return result.feedback;
  }), [run, update]);

  return item ? { item, shareable, busy, toggle, setShared, setFeedback } : null;
}
