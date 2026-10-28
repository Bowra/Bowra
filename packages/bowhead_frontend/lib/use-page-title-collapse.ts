"use client";

import { createContext, useEffect, useLayoutEffect, useState } from "react";

import { SCROLL_EDGE_SCOPE_ID } from "./use-scroll-edge";

/**
 * Large-title pattern (iOS navigation bar): while the page's own H1 is on screen it is the title, so the
 * shell's top-bar titles (MobileTopBar below 1024, desktop Topbar title) stay hidden; once the H1 scrolls
 * under the top chrome they fade in. AppShell computes the state once for every route and hands it to
 * MobileTopBar directly and to the page-built Topbar through this context.
 */
export const PageTitleCollapsedContext = createContext(false);

/** Top chrome that can cover the top of the page; the H1 counts as scrolled away once it is under these. */
const TOP_CHROME_SELECTOR = ".v2-mobile-topbar, .v2-main-head";

function normalizeTitle(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

/** Visually hidden headings (PageStateHeading) and headings hidden by a breakpoint are not a visible title. */
function isVisiblyRendered(element: HTMLElement): boolean {
  const rect = element.getBoundingClientRect();
  if (rect.width <= 1 || rect.height <= 1) return false;
  return window.getComputedStyle(element).visibility !== "hidden";
}

/**
 * The page H1 that repeats `title`: the first visible H1 in the main content whose text is the title, or the
 * title followed by inline extras (Approvals "· 3"). An H1 naming something else (a settings tab, an entity on
 * a detail page, a report heading inside the conversation) leaves the top-bar title in place.
 */
export function findPageTitleHeading(scope: ParentNode, title: string): HTMLElement | null {
  const wanted = normalizeTitle(title);
  if (!wanted) return null;
  for (const heading of scope.querySelectorAll<HTMLElement>("h1")) {
    const text = normalizeTitle(heading.textContent ?? "");
    if (text !== wanted && !text.startsWith(`${wanted} `)) continue;
    if (isVisiblyRendered(heading)) return heading;
  }
  return null;
}

function topChromeBottom(): number {
  let bottom = 0;
  for (const element of document.querySelectorAll(TOP_CHROME_SELECTOR)) {
    const rect = element.getBoundingClientRect();
    if (rect.height > 0) bottom = Math.max(bottom, rect.bottom);
  }
  return Math.max(0, Math.round(bottom));
}

// Measure before the first paint in the browser so a page that opens with its H1 shows no title flash.
const useBrowserLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

/**
 * Returns true while a visible page H1 that repeats `title` is in view below the top chrome.
 * Pages without such an H1 always return false, so their top-bar title stays visible.
 */
export function usePageTitleCollapse(title: string): boolean {
  const [collapsed, setCollapsed] = useState(false);

  useBrowserLayoutEffect(() => {
    const scope = document.getElementById(SCROLL_EDGE_SCOPE_ID);
    if (!scope || typeof IntersectionObserver === "undefined") {
      setCollapsed(false);
      return;
    }
    let target: HTMLElement | null = null;
    let observer: IntersectionObserver | null = null;
    let frame = 0;
    let scheduled = false;

    const attach = (force = false) => {
      scheduled = false;
      const next = findPageTitleHeading(scope, title);
      if (next === target && !force) return;
      observer?.disconnect();
      observer = null;
      target = next;
      if (!next) {
        setCollapsed(false);
        return;
      }
      const chromeBottom = topChromeBottom();
      // First estimate synchronously (no flash); the observer then reports scroll-container clipping exactly.
      const rect = next.getBoundingClientRect();
      setCollapsed(rect.bottom > chromeBottom && rect.top < window.innerHeight);
      observer = new IntersectionObserver(
        (entries) => {
          const entry = entries[entries.length - 1];
          if (entry) setCollapsed(entry.isIntersecting);
        },
        { rootMargin: `-${chromeBottom}px 0px 0px 0px`, threshold: 0 },
      );
      observer.observe(next);
    };

    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      frame = window.requestAnimationFrame(() => attach());
    };

    // Pages render their H1 after data loads and swap it between states; follow the main content.
    const mutations = new MutationObserver(schedule);
    mutations.observe(scope, { childList: true, subtree: true, characterData: true });
    // The top chrome height changes across breakpoints; rebuild the observer margin.
    const onResize = () => attach(true);
    window.addEventListener("resize", onResize);
    attach();

    return () => {
      mutations.disconnect();
      observer?.disconnect();
      window.removeEventListener("resize", onResize);
      if (scheduled) window.cancelAnimationFrame(frame);
    };
  }, [title]);

  return collapsed;
}
