/**
 * useMediaQuery — observe CSS media-query changes and return whether it currently matches.
 *
 * Example: const isMobile = useMediaQuery("(max-width: 1023px)");
 *
 * SSR-safe: server always returns false (desktop default); synchronize after client mount.
 */

"use client";

import { useEffect, useState } from "react";

export function useMediaQuery(query: string): boolean {
  // SSR: default false (desktop always-on edit mode remains always-on; SSR rendering is unaffected).
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    // Modern browsers: addEventListener; old Safari: addListener
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", handler);
      return () => mql.removeEventListener("change", handler);
    } else {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (mql as any).addListener(handler);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return () => (mql as any).removeListener(handler);
    }
  }, [query]);

  return matches;
}
