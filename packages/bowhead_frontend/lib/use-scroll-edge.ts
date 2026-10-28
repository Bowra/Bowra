"use client";

import { useEffect, type RefObject } from "react";

/** Main-content ID (AppShell `<main>`); the top bar observes only scroll here and whole-page scroll. */
export const SCROLL_EDGE_SCOPE_ID = "bowhead-main-content";

/**
 * Top-bar scroll edge (P2 material, design “P2 Materials Spec” §2): when any main-content scroll container (or the whole page) leaves the top,
 * set `data-scrolled` on the `ref` element, removing it on return to top. Only the refreshed styles read it
 * (`styles/ui-refresh-materials.css`: transparent without a line at rest, bar material + hairline after scrolling).
 *
 * Main-content scroll containers vary by page (chat, canvas, settings body, whole page), so listen to scroll on document in capture phase
 * (scroll does not bubble, but capture receives it from any element), accepting only whole-page and
 * `#bowhead-main-content` containers; calculate at most once per frame.
 * Exclude the top bar's own scroll (horizontally overflowing buttons) and overlays mounted on body.
 * Switching sessions can replace entire scroll containers (detaching the old container emits no scroll), so main-content
 * node additions/removals also trigger recalculation:
 * remove detached or returned-to-top containers from the set, resetting the attribute accordingly. Do not write it if the value is unchanged.
 */
export function useScrollEdge(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    const scrolled = new Set<Element>();
    let frame = 0;
    let scheduled = false;
    let shown = false;

    const apply = () => {
      scheduled = false;
      let any = false;
      for (const node of scrolled) {
        if (!node.isConnected || node.scrollTop <= 0) scrolled.delete(node);
        else any = true;
      }
      if (any === shown) return;
      shown = any;
      if (any) host.setAttribute("data-scrolled", "true");
      else host.removeAttribute("data-scrolled");
    };

    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      frame = window.requestAnimationFrame(apply);
    };

    const track = (node: Element | null) => {
      if (!node || host.contains(node)) return;
      const page = document.scrollingElement;
      const scope = document.getElementById(SCROLL_EDGE_SCOPE_ID);
      if (node !== page && !(scope && scope.contains(node))) return;
      if (node.scrollTop > 0) scrolled.add(node);
      else scrolled.delete(node);
      schedule();
    };

    const onScroll = (event: Event) => {
      const target = event.target;
      track(target === document ? document.scrollingElement : target instanceof Element ? target : null);
    };

    // Recalculate when scroll containers are replaced (session/page switches); an empty set has nothing to reset, so skip calculation.
    const observer = new MutationObserver(() => {
      if (scrolled.size) schedule();
    });
    observer.observe(document.getElementById(SCROLL_EDGE_SCOPE_ID) ?? document.body, { childList: true, subtree: true });

    track(document.scrollingElement);
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => {
      observer.disconnect();
      document.removeEventListener("scroll", onScroll, { capture: true });
      if (scheduled) window.cancelAnimationFrame(frame);
      host.removeAttribute("data-scrolled");
    };
  }, [ref]);
}
