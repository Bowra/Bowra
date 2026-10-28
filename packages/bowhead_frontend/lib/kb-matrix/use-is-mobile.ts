"use client";

/**
 * kb-matrix M3 §4.8 (FE-P2-8) — isMobile hook: matchMedia(639px) + change listener +
 * SSR-safe false. Replaces matrix-view-m2's per-render inline `window.innerWidth < 640`
 * (no listener → rotating/resizing does not switch views).
 *
 * Deliberately do not reuse components/canvas/source-panel.useIsMobileViewport (767px): different
 * breakpoints and no cross-domain dependency on canvas components (registered in tech-design-m3 §1).
 */

import { useSyncExternalStore } from "react";

const QUERY = "(max-width: 639px)";

export function useIsMobile(): boolean {
  return useSyncExternalStore(_subscribe, _get, _getServer);
}

function _subscribe(cb: () => void): () => void {
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", cb);
  return () => mql.removeEventListener("change", cb);
}

function _get(): boolean {
  return window.matchMedia(QUERY).matches;
}

function _getServer(): boolean {
  return false;
}
