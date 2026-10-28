"use client";

/**
 * memory-revive S1 · data layer for the per-turn memory receipt (candidate cards + disclosure strip).
 *
 * Delivery (discovery §6.3): post-done live SSE physically impossible (bus closes at done),
 * receipt truth = `GET /memory/turn-receipt`, a REST projection of persisted memory state; briefly poll
 * after the turn ends; approve/reject/undo change server state; a single refetch on F5 mount restores the
 * same receipt (including `undone` entries). No turn event, zero chat-pipeline event contract impact.
 *
 * Review-fix batch (three-perspective review):
 * - completedTurnsRef marked only once polling cycle **actually ends**; StrictMode double mount's
 *   run1 interrupted by cleanup remains unmarked, run2 reruns (old marked at start, broke all dev F5 paths).
 * - Timers private to each effect cycle: old shared ref let previous turn's late hanging response clear
 *   next turn's polling.
 * - Clear items at cycle start, removing old turn's cards from new turn.
 * - Approve checks write_error: backend write failure (full store/lock) not success, card retained for retry.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  approveCandidate,
  deleteEntry,
  getTurnMemoryReceipt,
  MemoryAPIError,
  redraftCandidate,
  rejectCandidate,
  type PendingCandidate,
  type TurnMemoryExplicitRequest,
  type TurnMemoryReceiptEntry,
} from "@/lib/memory-api";

/** Post-done polling: distillation is one LLM call (seconds), three attempts cover ~15s window. */
const LIVE_POLL_DELAYS_MS = [2500, 6500, 15000];

export type MemoryCandidateMode = "live" | "archived" | "off";

/**
 * Write-gate refusal reasons (stable backend memory_write_gate enum).
 * ★ Wording localized **frontend-side**: backend returns reason code only. Its message supports LLM paraphrasing
 * (in user output_language); UI wording belongs to next-intl, each owns its own surface.
 */
export type CandidateRefusalReason =
  | "one_off"
  | "language_is_a_setting"
  | "no_attribution"
  | "contains_secret";

/**
 * decide result. ★ Boolean insufficient: write-gate rejection is **policy refusal**,
 * retry can never succeed, yet card previously grouped it with transient full-store/locked failures as
 * “操作失败，请稍后再试”, encouraging retries of operation that cannot succeed by design.
 */
export type CandidateDecision =
  | { ok: true; entryId?: string | null }
  | { ok: false; refusal?: CandidateRefusalReason; error?: unknown };

const _REFUSAL_REASONS: readonly string[] = [
  "one_off",
  "language_is_a_setting",
  "no_attribution",
  "contains_secret",
];

/** Extract gate reason from MemoryAPIError; absent treated as ordinary failure (exported for unit tests only). */
export function _gateRefusal(err: unknown): CandidateRefusalReason | undefined {
  if (!(err instanceof MemoryAPIError) || err.status !== 422) return undefined;
  // MemoryAPIError.payload already contains entire response body; no shared `_check` changes needed
  // (12 files use it, each branches by status only).
  const detail = (
    err.payload as { detail?: { code?: string; reason?: string } } | undefined
  )?.detail;
  if (detail?.code !== "memory_write_gate_refused") return undefined;
  const reason = detail?.reason;
  return typeof reason === "string" && _REFUSAL_REASONS.includes(reason)
    ? (reason as CandidateRefusalReason)
    : undefined;
}

/**
 * Apply Undo done in this view over a polled receipt. The DELETE already succeeded, so the server
 * derives `undone` for these entries; a response that was in flight before it must not revive them.
 */
export function mergeLocallyUndone(
  entries: TurnMemoryReceiptEntry[],
  undoneIds: ReadonlySet<string>,
): TurnMemoryReceiptEntry[] {
  return entries.map((e) =>
    undoneIds.has(e.id) ? { ...e, state: "undone", undoable: false } : e,
  );
}

export interface MemoryCandidatesState {
  items: PendingCandidate[];
  /** Candidate id being approved/rejected (disables buttons). */
  actingId: string | null;
  decide: (
    cid: string,
    action: "remember" | "dismiss",
  ) => Promise<CandidateDecision>;
  /** “改一下”: redraft with feedback, success **replaces in place** in list. */
  redraft: (cid: string, comment: string) => Promise<CandidateDecision>;
  /** Entries this turn created, with server-derived `remembered` / `undone` state (any origin). */
  entries: TurnMemoryReceiptEntry[];
  /** Structured explicit request of this turn; null for ordinary and old turns. */
  explicitRequest: TurnMemoryExplicitRequest | null;
  /** Undo an entry; success marks it `undone` in place, matching what a refresh derives. */
  undoEntry: (entryId: string) => Promise<boolean>;
}

export function useMemoryCandidates(
  turnId: string | null,
  mode: MemoryCandidateMode,
): MemoryCandidatesState {
  const [items, setItems] = useState<PendingCandidate[]>([]);
  const [actingId, setActingId] = useState<string | null>(null);
  const [entries, setEntries] = useState<TurnMemoryReceiptEntry[]>([]);
  const [explicitRequest, setExplicitRequest] =
    useState<TurnMemoryExplicitRequest | null>(null);
  // Turns whose polling cycles completed (avoid repeated windows from mode jitter; cleanup never marks → StrictMode safe).
  const completedTurnsRef = useRef<Set<string>>(new Set());
  // Entries undone in this view. A poll that left before the DELETE landed can still report them as
  // remembered; merging keeps them undone instead of flipping back to an undoable 「已记住」.
  const undoneIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!turnId || mode === "off") return;
    if (completedTurnsRef.current.has(turnId)) return;

    let cancelled = false;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const clearTimers = () => {
      for (const t of timers) clearTimeout(t);
      timers.length = 0;
    };
    // Cycle starts: clear previous turn’s residual cards.
    setItems([]);
    setEntries([]);
    undoneIdsRef.current = new Set();
    setExplicitRequest(null);

    const markDone = () => {
      if (!cancelled) completedTurnsRef.current.add(turnId);
    };
    const fetchOnce = async (): Promise<boolean> => {
      // ★ One receipt per poll: candidates, created entries and the explicit request come from the same
      // persisted projection, so live polling and an F5 refetch cannot disagree.
      // ⚠ Entries/explicit request **do not participate in stop-on-results**: a zero-click write usually
      // leaves the candidate pool empty while a distilled candidate may still arrive later.
      try {
        const receipt = await getTurnMemoryReceipt(turnId);
        if (cancelled) return false; // Late hanging response must never affect subsequent cycles.
        setEntries(mergeLocallyUndone(receipt.entries, undoneIdsRef.current));
        setExplicitRequest(receipt.explicit_request);
        if (receipt.pending_candidates.length > 0) {
          setItems(receipt.pending_candidates);
          return true; // Results found: stop subsequent polling.
        }
      } catch {
        // Silent: the receipt is supplementary; unavailable never disrupts chat.
      }
      return false;
    };

    if (mode === "archived") {
      // F5 restoration: one fetch, no polling.
      void fetchOnce().then(() => markDone());
    } else {
      LIVE_POLL_DELAYS_MS.forEach((delay, i) => {
        const isLast = i === LIVE_POLL_DELAYS_MS.length - 1;
        timers.push(
          setTimeout(() => {
            void fetchOnce().then((hit) => {
              if (cancelled) return;
              if (hit) {
                clearTimers();
                markDone();
              } else if (isLast) {
                markDone(); // Window exhausted, no output still completes cycle.
              }
            });
          }, delay),
        );
      });
    }
    return () => {
      cancelled = true;
      clearTimers();
    };
  }, [turnId, mode]);

  const decide = useCallback(
    async (
      cid: string,
      action: "remember" | "dismiss",
    ): Promise<CandidateDecision> => {
      const target = items.find((c) => c.id === cid);
      if (!target || actingId) return { ok: false };
      setActingId(cid);
      try {
        if (action === "remember") {
          const selectedTarget = target.default_target;
          if (!selectedTarget || target.capabilities?.approve === false) {
            return { ok: false };
          }
          const res = await approveCandidate(cid, undefined, selectedTarget);
          if (res.write_error) {
            // Backend write failed (full store/locked): candidate remains pending/retryable, card stays visible.
            return { ok: false };
          }
          setItems((prev) => prev.filter((c) => c.id !== cid));
          const entryId = res.approved_entry_id ?? null;
          if (entryId) {
            // The approved entry is what the next receipt read returns for this turn (origin candidate),
            // so add it now instead of waiting for a poll that has already stopped.
            setEntries((prev) => [
              ...prev.filter((e) => e.id !== entryId),
              {
                id: entryId,
                kind: target.kind ?? "",
                key: target.key ?? "",
                display_value: target.display_value ?? null,
                origin: "candidate",
                state: "remembered",
                undoable: true,
                candidate_id: cid,
              },
            ]);
          }
          // ★ Return written entry id: card’s “撤销” needs target.
          return { ok: true, entryId };
        }
        await rejectCandidate(cid);
        setItems((prev) => prev.filter((c) => c.id !== cid));
        return { ok: true };
      } catch (err) {
        // ★ Retain card: policy refusal must not erase still-pending candidate from view;
        // user can finish via “忽略” after reading reason (reject bypasses gate, always available).
        return { ok: false, refusal: _gateRefusal(err), error: err };
      } finally {
        setActingId(null);
      }
    },
    [items, actingId],
  );

  /**
   * “改一下”: send user feedback for redrafting, **new candidate replaces** old list entry.
   *
   * ★ Replace in place, not remove then wait for next polling iteration: card would disappear then reappear,
   * making user think they dismissed it. Replacement keeps location, changes content.
   */
  const redraft = useCallback(
    async (cid: string, comment: string): Promise<CandidateDecision> => {
      if (actingId) return { ok: false };
      setActingId(cid);
      try {
        const fresh = await redraftCandidate(cid, comment);
        setItems((prev) => prev.map((c) => (c.id === cid ? fresh : c)));
        return { ok: true };
      } catch (err) {
        return { ok: false, refusal: _gateRefusal(err), error: err };
      } finally {
        setActingId(null);
      }
    },
    [actingId],
  );

  const undoEntry = useCallback(async (entryId: string): Promise<boolean> => {
    try {
      await deleteEntry(entryId);
      undoneIdsRef.current.add(entryId);
      // ★ Mark undone in place instead of removing: the server derives the same `undone` state from
      // the trash, so the receipt reads identically after a refresh.
      setEntries((prev) =>
        prev.map((e) =>
          e.id === entryId ? { ...e, state: "undone", undoable: false } : e,
        ),
      );
      return true;
    } catch {
      return false;
    }
  }, []);

  return {
    items,
    actingId,
    decide,
    redraft,
    entries,
    explicitRequest,
    undoEntry,
  };
}
