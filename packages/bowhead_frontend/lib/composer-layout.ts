"use client";

import { useLayoutEffect, useState, type RefObject } from "react";

export const BOWHEAD_LAYOUT_CHANGED_EVT = "bowhead:layout-changed";

export type ComposerLayoutMode = "comfortable" | "compact" | "ultra";

export function classifyComposerWidth(width: number): ComposerLayoutMode {
  if (width >= 560) return "comfortable";
  if (width >= 360) return "compact";
  return "ultra";
}

function borderBoxInlineSize(entry: ResizeObserverEntry): number | null {
  const box = entry.borderBoxSize;
  const first = Array.isArray(box) ? box[0] : box;
  return first && Number.isFinite(first.inlineSize) ? first.inlineSize : null;
}

export function useComposerLayoutMode(
  hostRef: RefObject<HTMLElement | null>,
): ComposerLayoutMode {
  const [mode, setMode] = useState<ComposerLayoutMode>("compact");

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let frame = 0;
    let observer: ResizeObserver | null = null;
    let intersectionObserver: IntersectionObserver | null = null;
    let visible = document.visibilityState !== "hidden";
    let intersecting = true;
    let observerStale = typeof ResizeObserver === "undefined";
    let expectedWidth: number | null = null;
    let lastObservedWidth: number | null = null;
    let recoverySampleWidth: number | null = null;
    let healthyRecoverySamples = 0;
    let staleTimer = 0;
    let pollTimer = 0;
    let observerHealthTimer = 0;

    const commitWidth = (width: number) => {
      if (!Number.isFinite(width) || width <= 0) return;
      const next = classifyComposerWidth(width);
      setMode((current) => (current === next ? current : next));
    };
    const directWidth = () => host.getBoundingClientRect().width;
    const stopPolling = () => {
      window.clearTimeout(pollTimer);
      pollTimer = 0;
    };
    const pollIfNeeded = () => {
      stopPolling();
      if (!observerStale || !visible || !intersecting) return;
      pollTimer = window.setTimeout(() => {
        scheduleMeasure(false);
        pollIfNeeded();
      }, 250);
    };
    const scheduleObserverHealthCheck = () => {
      window.clearTimeout(observerHealthTimer);
      observerHealthTimer = 0;
      if (!observer || !visible || !intersecting) return;
      observerHealthTimer = window.setTimeout(() => {
        const width = directWidth();
        commitWidth(width);
        if (
          lastObservedWidth === null ||
          Math.abs(lastObservedWidth - width) >= 1
        ) {
          observerStale = true;
          expectedWidth = width;
          recoverySampleWidth = null;
          healthyRecoverySamples = 0;
          pollIfNeeded();
        }
        scheduleObserverHealthCheck();
      }, 500);
    };
    const expectObserverWidth = (width: number) => {
      if (!Number.isFinite(width) || width <= 0 || !observer) return;
      if (
        lastObservedWidth !== null &&
        Math.abs(lastObservedWidth - width) < 1
      ) {
        expectedWidth = null;
        window.clearTimeout(staleTimer);
        if (!observerStale) stopPolling();
        return;
      }
      expectedWidth = width;
      window.clearTimeout(staleTimer);
      staleTimer = window.setTimeout(() => {
        if (expectedWidth === width) {
          observerStale = true;
          recoverySampleWidth = null;
          healthyRecoverySamples = 0;
          pollIfNeeded();
        }
      }, 500);
    };
    const scheduleMeasure = (trackObserver = true) => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const width = directWidth();
        commitWidth(width);
        if (trackObserver) expectObserverWidth(width);
      });
    };
    const acceptObserverSample = (width: number) => {
      if (!Number.isFinite(width) || width <= 0) return;
      lastObservedWidth = width;
      commitWidth(width);
      if (expectedWidth !== null && Math.abs(expectedWidth - width) >= 1) {
        recoverySampleWidth = null;
        healthyRecoverySamples = 0;
        return;
      }
      if (observerStale) {
        if (
          recoverySampleWidth !== null &&
          Math.abs(recoverySampleWidth - width) < 1
        ) {
          healthyRecoverySamples += 1;
        } else {
          recoverySampleWidth = width;
          healthyRecoverySamples = 1;
        }
        if (healthyRecoverySamples < 2) return;
      }
      observerStale = false;
      expectedWidth = null;
      recoverySampleWidth = null;
      healthyRecoverySamples = 0;
      window.clearTimeout(staleTimer);
      stopPolling();
    };

    const initialWidth = directWidth();
    commitWidth(initialWidth);
    try {
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver((entries) => {
          const entry = entries.find((candidate) => candidate.target === host);
          const width = entry ? borderBoxInlineSize(entry) : null;
          // Older WebViews may omit borderBoxSize even though the callback is
          // healthy.  Their direct border-box measurement must count as the
          // observer sample; otherwise polling can never recover.
          acceptObserverSample(width ?? directWidth());
        });
        observer.observe(host, { box: "border-box" });
        // Some WebViews accept observe() but never invoke the callback.  Arm
        // the watchdog immediately so silent observers fall back to polling
        // even when no resize/layout event is dispatched.
        expectObserverWidth(initialWidth);
        // A WebView can emit the initial sample and then go permanently
        // silent.  A low-frequency visible-only health sample is the only way
        // to detect that failure without relying on another window event.
        scheduleObserverHealthCheck();
      }
    } catch {
      observer?.disconnect();
      observer = null;
      observerStale = true;
      pollIfNeeded();
    }

    if (typeof IntersectionObserver !== "undefined") {
      intersectionObserver = new IntersectionObserver((entries) => {
        const entry = entries.find((candidate) => candidate.target === host);
        if (!entry) return;
        intersecting = entry.isIntersecting;
        pollIfNeeded();
        scheduleObserverHealthCheck();
      });
      intersectionObserver.observe(host);
    }
    const onVisibilityChange = () => {
      visible = document.visibilityState !== "hidden";
      pollIfNeeded();
      scheduleObserverHealthCheck();
    };
    const onLayoutChanged = () => scheduleMeasure();
    window.addEventListener("resize", onLayoutChanged);
    window.addEventListener(BOWHEAD_LAYOUT_CHANGED_EVT, onLayoutChanged);
    document.addEventListener("visibilitychange", onVisibilityChange);
    pollIfNeeded();
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(staleTimer);
      window.clearTimeout(observerHealthTimer);
      stopPolling();
      observer?.disconnect();
      intersectionObserver?.disconnect();
      window.removeEventListener("resize", onLayoutChanged);
      window.removeEventListener(BOWHEAD_LAYOUT_CHANGED_EVT, onLayoutChanged);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [hostRef]);

  return mode;
}
