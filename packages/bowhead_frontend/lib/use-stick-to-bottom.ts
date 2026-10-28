"use client";

/**
 * use-stick-to-bottom — viewport following during streaming.
 *
 * chat-ux-2026-08 batch C. Replaces TurnContainer's stop-following-at->200px logic.
 *
 * ## Why old behavior fails (116 prod v1.0.71 measurements)
 *
 * Scrollbar placed exactly at bottom (gap=0), then untouched during a 270-second turn:
 *
 * | Time     | scrollTop | scrollHeight | Gap  |
 * | -------- | --------- | ------------ | ---- |
 * | Baseline | 1897      | 2564         | 0    |
 * | +20s     | 1897      | 2794         | 230  |
 * | +50s     | 3474      | 4726         | 581  |
 * | +95s     | 4575      | 6664         | 1418 |
 *
 * Two combined defects:
 *
 * 1. Old effect dependencies: `[user, answer, traceSteps.length, summary?.total]`,
 *    triggering **only on step-count changes**, while most growth came from existing steps'
 *    stdout/narration lengthening; never triggered for that (+20s: unchanged scrollTop).
 * 2. `dist > 200` hard-exits. Any single-step growth above 200px crosses threshold,
 *    blocking even later new steps, yielding **monotonically increasing lag**.
 *
 * ## Change here
 *
 * **Intent replaces distance**: detach only when **user scrolls**.
 *
 * - `ResizeObserver` on content container detects **growth within steps**,
 *   independent of React dependency arrays.
 * - Critical distinction: programmatic versus user scroll. `scrollTop` changes **cannot**
 *   establish intent because our `scrollTo` changes it too, detaching itself.
 *   Accept real input events only (wheel / touchmove / paging keys), never scroll events.
 * - Returning to bottom (manually or “回到最新”) restores following.
 */
import { useCallback, useEffect, useRef, useState } from "react";

/** Bottom slack in pixels, allowing subpixels/inertial scrolling completion. */
const AT_BOTTOM_SLACK = 24;

/** Detachment keys express intent to look back. */
const RELEASE_KEYS = new Set(["PageUp", "ArrowUp", "Home"]);

export interface StickToBottomApi {
  /** Attach to **scroll container**. */
  scrollRef: (el: HTMLElement | null) => void;
  /** Attach to **inner content root** (ResizeObserver observes height). */
  contentRef: (el: HTMLElement | null) => void;
  /** Currently following. false should show “回到最新” floater. */
  following: boolean;
  /** New-progress count accumulated after detachment (grows only while following=false). */
  pendingCount: number;
  /** “回到最新”: scroll to bottom and restore following. */
  jumpToLatest: () => void;
  /** Report new progress (new step / answer beginning), not character growth that would constantly change count. */
  notePendingProgress: () => void;
}

export function useStickToBottom(enabled: boolean): StickToBottomApi {
  const scrollElRef = useRef<HTMLElement | null>(null);
  const contentElRef = useRef<HTMLElement | null>(null);
  const observerRef = useRef<ResizeObserver | null>(null);
  const followingRef = useRef(true);
  const [following, setFollowing] = useState(true);
  const [pendingCount, setPendingCount] = useState(0);

  const isAtBottom = useCallback((el: HTMLElement) => {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= AT_BOTTOM_SLACK;
  }, []);

  const setFollow = useCallback((next: boolean) => {
    if (followingRef.current === next) return;
    followingRef.current = next;
    setFollowing(next);
    if (next) setPendingCount(0);
  }, []);

  const scrollToBottom = useCallback((smooth: boolean) => {
    const el = scrollElRef.current;
    if (!el) return;
    // jsdom lacks scrollTo; assign scrollTop as fallback so tests can verify behavior.
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }, []);

  const jumpToLatest = useCallback(() => {
    setFollow(true);
    scrollToBottom(true);
  }, [setFollow, scrollToBottom]);

  const notePendingProgress = useCallback(() => {
    if (followingRef.current) return;
    setPendingCount((n) => n + 1);
  }, []);

  /** User intentionally looks back → detach; **real input events only**. */
  const onUserIntentUp = useCallback(() => {
    const el = scrollElRef.current;
    if (!el) return;
    if (!isAtBottom(el)) setFollow(false);
  }, [isAtBottom, setFollow]);

  /**
   * Hold scroll container in state rather than manually adding/removing listeners in ref callback:
   * effect owns listener lifecycle, with React guaranteeing order/cleanup,
   * avoiding eslint suppression for handler-definition order.
   */
  const [scrollEl, setScrollEl] = useState<HTMLElement | null>(null);
  const scrollRef = useCallback((el: HTMLElement | null) => {
    scrollElRef.current = el;
    setScrollEl(el);
  }, []);

  useEffect(() => {
    if (!scrollEl) return;

    /** Upward wheel expresses look-back intent. Downward never detaches (user follows progress). */
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) onUserIntentUp();
    };
    /** Touch events do not directly reveal direction; compromise: any touch drag takes control. */
    const onTouchMove = () => onUserIntentUp();
    const onKeyDown = (e: KeyboardEvent) => {
      if (RELEASE_KEYS.has(e.key)) onUserIntentUp();
    };
    /**
     * scroll events **only restore following, never detach**.
     * Detachment must come from real input: our scrollTo also emits scroll,
     * so treating that as detachment would disable itself.
     */
    const onScroll = () => {
      if (isAtBottom(scrollEl)) setFollow(true);
    };

    scrollEl.addEventListener("wheel", onWheel, { passive: true });
    scrollEl.addEventListener("touchmove", onTouchMove, { passive: true });
    scrollEl.addEventListener("keydown", onKeyDown);
    scrollEl.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      scrollEl.removeEventListener("wheel", onWheel);
      scrollEl.removeEventListener("touchmove", onTouchMove);
      scrollEl.removeEventListener("keydown", onKeyDown);
      scrollEl.removeEventListener("scroll", onScroll);
    };
  }, [scrollEl, onUserIntentUp, isAtBottom, setFollow]);

  /** Content grows → stay bottom if following. **Key to fixing +20s row**:
   *  ResizeObserver sees actual container height, including growth within steps. */
  const contentRef = useCallback(
    (el: HTMLElement | null) => {
      if (contentElRef.current === el) return;
      observerRef.current?.disconnect();
      observerRef.current = null;
      contentElRef.current = el;
      if (!el || typeof ResizeObserver === "undefined") return;
      const ro = new ResizeObserver(() => {
        if (!followingRef.current) return;
        scrollToBottom(false);
      });
      ro.observe(el);
      observerRef.current = ro;
    },
    [scrollToBottom],
  );

  useEffect(() => () => observerRef.current?.disconnect(), []);

  // Never force bottom after turn ends; user may be reading, do not compete.
  useEffect(() => {
    if (!enabled) return;
    if (followingRef.current) scrollToBottom(false);
  }, [enabled, scrollToBottom]);

  return {
    scrollRef,
    contentRef,
    following,
    pendingCount,
    jumpToLatest,
    notePendingProgress,
  };
}
