"use client";

/**
 * agent-feedback-loop batch 5 · inline chat suggestion-result receipt data layer.
 *
 * Why chat rather than notification center (116 measurements, 2026-09-13):
 * 139 notifications received **0 clicks** (including 55 action notifications, also zero; more than an unlit bell
 * issue); **all 8 manual decisions** on memory candidates during that period happened on inline chat cards.
 * Notification-only receipts mean proposers rarely learn suggestion outcomes; feedback loop fails at the last mile.
 *
 * Delivery follows [[use-memory-candidates]]: post-done live SSE is physically impossible (bus closes on
 * done); server state is card authority. No turn_events writes or EventSource allowlist changes,
 * hence no chat-pipeline contract impact.
 *
 * ★ Key difference from memory candidates: receipt is **not this turn's output**; suggestion was made days ago, just decided by admin.
 * No short post-done polling window awaiting arrival: polling will not produce anything.
 * Fetch once on mount + once after every turn (decision might occur while user chats).
 *
 * Acknowledgement marks notification read (reuse existing `POST /notifications/{id}/read`), so viewing in chat
 * and notification center share one state, avoiding separate alerts on both surfaces.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { markNotificationRead } from "@/lib/api";
import { listFeedbackReceipts, type FeedbackReceipt } from "@/lib/feedback-candidates-api";

export interface FeedbackReceiptsState {
  items: FeedbackReceipt[];
  /** Notification id being acknowledged (disables button). */
  actingId: string | null;
  /** Acknowledge: mark read and remove from list. Failure keeps card available for retry. */
  acknowledge: (notificationId: string) => Promise<boolean>;
}

/**
 * Acknowledgement failures **that retries cannot recover**.
 *
 * `markNotificationRead` throws `HttpStatusError` (lib/api-client.ts) with message
 * `"notification read: <status>"` and `status` field; notification center also checks this message
 * (`notification-center.tsx` `isDisabledError`). 404/503 means center disabled or notification gone;
 * asking users to keep clicking something designed never to succeed reproduces
 * the [[feedback_soft_failure_swallowed_as_transient]] lesson exactly.
 */
function _permanent(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err);
  return message.includes(": 404") || message.includes(": 503");
}

/**
 * @param enabled No requests when off. Only current caller passes `!runner.running`:
 *   no fetching during a running turn. ⚠ **Stops fetching only, never clears existing cards** (see caller comment).
 * @param refreshKey Refetch whenever it changes; pass latest completed turn id.
 */
export function useFeedbackReceipts(
  enabled: boolean,
  refreshKey: string | null,
): FeedbackReceiptsState {
  const [items, setItems] = useState<FeedbackReceipt[]>([]);
  const [actingId, setActingId] = useState<string | null>(null);
  // Acknowledged ids: fetching is idempotent, but in-flight requests still let server report just-acknowledged items unread;
  // late response would restore the card. Local tombstone blocks that transition.
  const ackedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void listFeedbackReceipts()
      .then((res) => {
        if (cancelled) return;
        setItems(res.items.filter((r) => !ackedRef.current.has(r.notification_id)));
      })
      .catch(() => {
        // Silent: receipts enhance chat; disabled endpoint returns 404, absence must not disturb chat (same approach as candidates).
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, refreshKey]);

  const acknowledge = useCallback(
    async (notificationId: string): Promise<boolean> => {
      if (actingId) return false;
      setActingId(notificationId);
      try {
        await markNotificationRead(notificationId);
        ackedRef.current.add(notificationId);
        setItems((prev) => prev.filter((r) => r.notification_id !== notificationId));
        return true;
      } catch (err) {
        if (_permanent(err)) {
          // Permanent failure: still tombstone and remove, or every fetch brings it back and user **cannot dismiss** it.
          ackedRef.current.add(notificationId);
          setItems((prev) => prev.filter((r) => r.notification_id !== notificationId));
          return true;
        }
        // Transient failure (offline/5xx): keep card so user can click again.
        return false;
      } finally {
        setActingId(null);
      }
    },
    [actingId],
  );

  return { items, actingId, acknowledge };
}
