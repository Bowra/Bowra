"use client";

import { useEffect, useSyncExternalStore } from "react";

/**
 * Pages ask the persistent sidebar to collapse to its icon rail (P2 batch 4): the
 * workbench does so while its results canvas is open. The sidebar applies it only on narrow desktops
 * (`sidebarAutoCompactActive`).
 *
 * A counter rather than a boolean so two requesters (or a remount racing an unmount) cannot clear each
 * other's request.
 */
let requests = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of Array.from(listeners)) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): boolean {
  return requests > 0;
}

export function useSidebarAutoCompactRequested(): boolean {
  return useSyncExternalStore(subscribe, snapshot, () => false);
}

/** Holds a request while `active` is true and the calling component is mounted. */
export function useRequestSidebarAutoCompact(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    requests += 1;
    emit();
    return () => {
      requests -= 1;
      emit();
    };
  }, [active]);
}
