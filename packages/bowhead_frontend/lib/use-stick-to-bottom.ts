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
 *   Detach on real input (wheel / touchmove / paging keys) and on an **upward** scroll that leaves
 *   the bottom; our own following only ever scrolls down to the bottom (see below).
 * - Returning to bottom (manually or “回到最新”) restores following.
 *
 * ## After the Turn ends, and disclosures (2026-11-08 dogfood F8)
 *
 * Following used to pin the bottom on **any** growth, running or not. After a Turn ended, clicking
 * "View trace" grew the content, the observer pinned the bottom, and the clicked row (still focused)
 * left the screen: on 116 it moved from 120 px to −283 px. Scrollbar drags and Tab focus moves never
 * released following either, because only wheel/touch/paging keys did.
 *
 * - While the Turn runs, growth is followed as before.
 * - After it ends, growth is followed only until the reader **engages** with the conversation
 *   (pointer, key, wheel or touch inside it). A conversation that was just opened or just finished
 *   stays on its latest content while late layout (charts, images) settles; a reader who has started
 *   inspecting it is never pulled away. ⚠ Do not gate following on the running state alone: opening a
 *   finished conversation relies on this observer to land on its latest content.
 * - A disclosure the reader toggles (an `aria-expanded` control or a `<summary>`) keeps its on-screen
 *   position while the content resizes, instead of being pushed by following. Following is released
 *   when that leaves the view away from the bottom, so the “回到最新” floater offers the way back.
 * - An upward scroll that leaves the bottom releases following whatever caused it (scrollbar drag,
 *   focus moving to an earlier element, find-in-page, a link to an earlier result). Following scrolls
 *   only move down, and a content shrink clamps the position at the bottom, so neither is mistaken
 *   for the reader.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/** Bottom slack in pixels, allowing subpixels/inertial scrolling completion. */
const AT_BOTTOM_SLACK = 24;

/** Detachment keys express intent to look back. */
const RELEASE_KEYS = new Set(["PageUp", "ArrowUp", "Home"]);

/** Controls whose activation opens or closes content in place. */
const DISCLOSURE_SELECTOR = "[aria-expanded], summary";

/**
 * How long resizes after a disclosure click are attributed to it. The toggle commits and lays out in
 * the same frame; the margin covers short height transitions without holding the view for long.
 */
const DISCLOSURE_HOLD_MS = 400;

interface DisclosureAnchor {
  el: Element;
  /** Offset of the control from the scroll container's top edge when it was activated. */
  offset: number;
  /** Open state when it was activated; a resize is the toggle's only once this has changed. */
  state: string | null;
  until: number;
}

function offsetWithin(scroller: HTMLElement, el: Element): number {
  return el.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
}

function disclosureState(control: Element): string | null {
  if (control.localName === "summary") {
    const details = control.parentElement;
    return details instanceof HTMLDetailsElement ? String(details.open) : null;
  }
  return control.getAttribute("aria-expanded");
}

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

/**
 * @param enabled The Turn is running. While true, growth is always followed (unless the reader looked back);
 *   once false, growth is followed only until the reader engages with the conversation.
 */
export function useStickToBottom(enabled: boolean): StickToBottomApi {
  const scrollElRef = useRef<HTMLElement | null>(null);
  const contentElRef = useRef<HTMLElement | null>(null);
  const observerRef = useRef<ResizeObserver | null>(null);
  const followingRef = useRef(true);
  const enabledRef = useRef(enabled);
  /** The reader has interacted with the conversation since it opened or since the last Turn ended. */
  const readerEngagedRef = useRef(false);
  const anchorRef = useRef<DisclosureAnchor | null>(null);
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
    // An explicit request for the latest content: keep it in view while it settles, as on opening.
    readerEngagedRef.current = false;
    anchorRef.current = null;
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
    let lastTop = scrollEl.scrollTop;
    const engage = () => {
      readerEngagedRef.current = true;
    };

    /** Upward wheel expresses look-back intent. Downward never detaches (user follows progress). */
    const onWheel = (e: WheelEvent) => {
      engage();
      anchorRef.current = null;
      if (e.deltaY < 0) onUserIntentUp();
    };
    /** Touch events do not directly reveal direction; compromise: any touch drag takes control. */
    const onTouchMove = () => {
      anchorRef.current = null;
      onUserIntentUp();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      engage();
      if (RELEASE_KEYS.has(e.key)) {
        anchorRef.current = null;
        onUserIntentUp();
      }
    };
    /**
     * A disclosure the reader activates (pointer, keyboard or assistive technology all dispatch `click`)
     * keeps its on-screen position through the resize it causes. Capture phase: measure before the
     * React handler commits the new content.
     */
    const onClickCapture = (e: MouseEvent) => {
      engage();
      const control = e.target instanceof Element ? e.target.closest(DISCLOSURE_SELECTOR) : null;
      if (!control || !scrollEl.contains(control)) return;
      anchorRef.current = {
        el: control,
        offset: offsetWithin(scrollEl, control),
        state: disclosureState(control),
        until: Date.now() + DISCLOSURE_HOLD_MS,
      };
    };
    /**
     * Reaching the bottom restores following. Moving **up** away from it releases following, whatever
     * moved it: scrollbar drags, focus moving to an earlier element, find-in-page or a link to an earlier
     * result. Our own following only scrolls down (a smooth scroll mid-animation is still below its
     * target, moving down), and a content shrink clamps the position at the bottom.
     */
    const onScroll = () => {
      const top = scrollEl.scrollTop;
      const movedUp = top < lastTop - 1;
      lastTop = top;
      if (isAtBottom(scrollEl)) setFollow(true);
      else if (movedUp) setFollow(false);
    };

    scrollEl.addEventListener("wheel", onWheel, { passive: true });
    scrollEl.addEventListener("touchstart", engage, { passive: true });
    scrollEl.addEventListener("touchmove", onTouchMove, { passive: true });
    scrollEl.addEventListener("pointerdown", engage);
    scrollEl.addEventListener("keydown", onKeyDown);
    scrollEl.addEventListener("click", onClickCapture, true);
    scrollEl.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      scrollEl.removeEventListener("wheel", onWheel);
      scrollEl.removeEventListener("touchstart", engage);
      scrollEl.removeEventListener("touchmove", onTouchMove);
      scrollEl.removeEventListener("pointerdown", engage);
      scrollEl.removeEventListener("keydown", onKeyDown);
      scrollEl.removeEventListener("click", onClickCapture, true);
      scrollEl.removeEventListener("scroll", onScroll);
    };
  }, [scrollEl, onUserIntentUp, isAtBottom, setFollow]);

  /**
   * Keep a just-toggled disclosure where the reader activated it. Returns true while the toggle owns
   * this resize, so following does not push the row away.
   */
  const holdDisclosure = useCallback((scroller: HTMLElement): boolean => {
    const anchor = anchorRef.current;
    if (!anchor) return false;
    if (Date.now() > anchor.until || !anchor.el.isConnected) {
      anchorRef.current = null;
      return false;
    }
    // Not toggled (yet): this resize has another cause, such as streaming output.
    if (disclosureState(anchor.el) === anchor.state) return false;
    const drift = offsetWithin(scroller, anchor.el) - anchor.offset;
    if (Math.abs(drift) >= 1) scroller.scrollTop += drift;
    if (!isAtBottom(scroller)) setFollow(false);
    return true;
  }, [isAtBottom, setFollow]);

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
        const scroller = scrollElRef.current;
        if (scroller && holdDisclosure(scroller)) return;
        if (!followingRef.current) return;
        // After the Turn ends, a reader who is inspecting the conversation is never pulled to the bottom.
        if (!enabledRef.current && readerEngagedRef.current) return;
        scrollToBottom(false);
      });
      ro.observe(el);
      observerRef.current = ro;
    },
    [holdDisclosure, scrollToBottom],
  );

  useEffect(() => () => observerRef.current?.disconnect(), []);

  // Layout effect: the resize observer runs after layout effects, so the commit that ends the Turn
  // already reads the new state.
  useLayoutEffect(() => {
    const wasEnabled = enabledRef.current;
    enabledRef.current = enabled;
    // The Turn just ended: its closing render stays in view for a reader who is still following,
    // until they engage with the conversation.
    if (wasEnabled && !enabled) readerEngagedRef.current = false;
  }, [enabled]);

  // Never force the bottom when the Turn ends; the reader may be reading, do not compete.
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
