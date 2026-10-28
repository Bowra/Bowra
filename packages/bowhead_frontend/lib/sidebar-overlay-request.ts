"use client";

/**
 * Opens the persistent sidebar as an overlay from outside it (P2 batch 4): between
 * 768 and 1023 the sidebar is hidden and the top bar's menu button asks for it. The sidebar returns focus
 * to the opener when the overlay closes.
 */
type Listener = (opener: HTMLElement | null) => void;

const listeners = new Set<Listener>();

export function onSidebarOverlayRequest(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Returns false when no sidebar is listening (the caller then falls back to the drawer). */
export function requestSidebarOverlay(opener: HTMLElement | null): boolean {
  if (listeners.size === 0) return false;
  for (const listener of Array.from(listeners)) listener(opener);
  return true;
}
