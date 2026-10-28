"use client";

import { useEffect, useRef } from "react";

import { ApiError } from "@/lib/api-client";

/** Server confirmation windows are minute-scale; 5s polling is timely without piling up requests under heavy load. */
export const CONFIRMATION_POLL_MS = 5000;

/**
 * Poll an owner-only confirmation queue (personal MCP calls, outgoing email).
 * There is no push event, so this is the one polling policy for such queues:
 * read immediately once enabled, then every CONFIRMATION_POLL_MS; never start a
 * read while one is in flight (a restarted run joins that read instead); pause
 * while the page is hidden and read at once when it becomes visible again. 401/403/404 stops polling for this mount.
 * `gate` (optional) is checked once per mount before the first read; a false or
 * failed gate means this queue is not available and nothing is polled.
 * A read failure never implies consent — the server expires unanswered items.
 */
export function useConfirmationQueuePoll<T>({
  enabled = true,
  gate,
  load,
  onItems,
}: {
  enabled?: boolean;
  gate?: () => Promise<boolean>;
  load: () => Promise<T[]>;
  onItems: (items: T[]) => void;
}): void {
  const gateRef = useRef(gate);
  const gatePassed = useRef<Promise<boolean> | null>(null);
  const loadRef = useRef(load);
  const onItemsRef = useRef(onItems);
  const stopped = useRef(false);
  // Shared across effect runs so toggling `enabled` never overlaps a slow read. A run
  // that starts while an earlier run's read is still out joins that read and takes its
  // result: the earlier run is already inactive and drops it, so waiting a full
  // interval instead would hide an item that is already on its way (StrictMode's
  // mount-unmount-mount does exactly this on every first read).
  const inFlight = useRef<Promise<T[]> | null>(null);
  loadRef.current = load;
  onItemsRef.current = onItems;
  useEffect(() => {
    if (!enabled || stopped.current) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const hidden = () => document.visibilityState === "hidden";
    const schedule = () => {
      if (timer) clearTimeout(timer);
      timer = undefined;
      if (active && !hidden()) {
        timer = setTimeout(() => void poll(), CONFIRMATION_POLL_MS);
      }
    };
    const poll = async () => {
      if (!active || hidden()) return;
      let read = inFlight.current;
      if (!read) {
        const started = loadRef.current();
        read = started;
        inFlight.current = started;
        void started
          .finally(() => {
            if (inFlight.current === started) inFlight.current = null;
          })
          .catch(() => undefined);
      }
      try {
        const items = await read;
        if (active) onItemsRef.current(items);
      } catch (failure) {
        if (
          failure instanceof ApiError &&
          [401, 403, 404].includes(failure.status)
        ) {
          stopped.current = true;
          if (active) onItemsRef.current([]);
          active = false;
        }
      }
      schedule();
    };
    const onVisibility = () => {
      if (!active) return;
      if (hidden()) {
        if (timer) clearTimeout(timer);
        timer = undefined;
      } else {
        void poll();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    if (!gateRef.current) {
      void poll();
    } else {
      gatePassed.current ??= gateRef.current().catch(() => false);
      void gatePassed.current.then((open) => {
        if (!open) {
          stopped.current = true;
          active = false;
          return;
        }
        void poll();
      });
    }
    return () => {
      active = false;
      document.removeEventListener("visibilitychange", onVisibility);
      if (timer) clearTimeout(timer);
    };
  }, [enabled]);
}
