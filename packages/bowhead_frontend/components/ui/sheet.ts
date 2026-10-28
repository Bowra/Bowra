"use client";

/**
 * Shared narrow-screen bottom-sheet behavior (batch A menu sheets, batch B dialog sheets): breakpoint, swipe-dismiss thresholds/gestures,
 * soft-keyboard avoidance. Disabled hooks never subscribe to matchMedia / visualViewport.
 * See docs/design/组件统一-技术设计.md “行为批 A：菜单” / “行为批 B：窄屏弹窗与撤销”.
 */

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

/** Narrow breakpoint: < 640 is mobile (bottom sheets, list rows, top Toast). 640–767 is large-phone landscape, still overlays/centered dialogs. */
export const NARROW_SHEET_MEDIA_QUERY = "(max-width: 639.98px)";
/** Swipe dismiss: exceed this distance, or exceed SHEET_DISMISS_VELOCITY (px/ms) while dragging at least 16px. */
export const SHEET_DISMISS_DISTANCE = 72;
export const SHEET_DISMISS_VELOCITY = 0.5;
const SHEET_FLICK_MIN_DISTANCE = 16;
/** Keyboard threshold: visible bottom must be this far above layout-viewport bottom (address-bar changes only tens of pixels). */
export const KEYBOARD_THRESHOLD_PX = 120;

/**
 * Whether viewport is < 640. Subscribe to matchMedia only when `enabled`; otherwise never touch it, preserving caller behavior exactly.
 * Unsupported browsers count as wide.
 */
export function useNarrowViewport(enabled: boolean): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    if (!enabled || typeof window === "undefined" || typeof window.matchMedia !== "function") {
      setNarrow(false);
      return;
    }
    let query: MediaQueryList;
    try {
      query = window.matchMedia(NARROW_SHEET_MEDIA_QUERY);
    } catch {
      return;
    }
    setNarrow(Boolean(query?.matches));
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    if (typeof query?.addEventListener === "function") {
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    }
    if (typeof query?.addListener === "function") {
      query.addListener(onChange);
      return () => query.removeListener(onChange);
    }
  }, [enabled]);
  return enabled && narrow;
}

/** Whether release qualifies as swipe-dismiss: far enough or fast enough (flick). */
export function isSheetDismissGesture(distance: number, elapsedMs: number): boolean {
  if (distance >= SHEET_DISMISS_DISTANCE) return true;
  return distance > SHEET_FLICK_MIN_DISTANCE && distance / Math.max(1, elapsedMs) > SHEET_DISMISS_VELOCITY;
}

export interface SheetDragHandlers {
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerCancel: () => void;
}

/**
 * Drag handle/title downward: follow with `dragY`; on release call `onDismiss` at threshold, otherwise spring back.
 * Do not start if `canStart` returns false (e.g. busy). Reset displacement after `onDismiss` too:
 * caller may intercept closing for leave confirmation; sheet must return to original position.
 */
export function useSheetDrag({
  onDismiss,
  canStart,
}: {
  onDismiss: () => void;
  canStart?: () => boolean;
}): { dragY: number; handlers: SheetDragHandlers } {
  const [dragY, setDragY] = useState(0);
  const dragRef = useRef<{ startY: number; startTime: number; pointerId: number } | null>(null);
  const handlers: SheetDragHandlers = {
    onPointerDown: (event) => {
      if (event.button !== 0) return;
      if (canStart && !canStart()) return;
      dragRef.current = { startY: event.clientY, startTime: event.timeStamp, pointerId: event.pointerId };
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    onPointerMove: (event) => {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      setDragY(Math.max(0, event.clientY - drag.startY));
    },
    onPointerUp: (event) => {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      dragRef.current = null;
      const distance = Math.max(0, event.clientY - drag.startY);
      setDragY(0);
      if (isSheetDismissGesture(distance, event.timeStamp - drag.startTime)) onDismiss();
    },
    onPointerCancel: () => {
      dragRef.current = null;
      setDragY(0);
    },
  };
  return { dragY, handlers };
}

export interface SheetViewport {
  /** Height of layout viewport bottom obscured by keyboard/other system UI; raise sheet bottom by this amount. Zero without keyboard. */
  keyboardInset: number;
  /** Visible height limits sheet maximum. Null if unknown (CSS fallback 100dvh). */
  viewportHeight: number | null;
}

/** Compute sheet lift from layout viewport/visualViewport (pure, testable). No avoidance during zoom (scale ≠ 1). */
export function sheetViewportFrom({
  layoutHeight,
  visualHeight,
  offsetTop,
  scale,
}: {
  layoutHeight: number;
  visualHeight: number;
  offsetTop: number;
  scale: number;
}): SheetViewport {
  if (scale !== 1) return { keyboardInset: 0, viewportHeight: null };
  const occluded = Math.max(0, Math.round(layoutHeight - (offsetTop + visualHeight)));
  return {
    keyboardInset: occluded >= KEYBOARD_THRESHOLD_PX ? occluded : 0,
    viewportHeight: Math.round(visualHeight),
  };
}

/**
 * Soft-keyboard avoidance (Narrow Spec “底部操作栏避开安全区”: shrink to visible area, actions above keyboard).
 *
 * - iOS Safari: layout viewport remains unchanged; `visualViewport` shrinks and may shift down (offsetTop); fixed sheet stays at
 *   layout bottom, covered by keyboard. Lift by layout height − (offsetTop + visible height), cap at visible height.
 * - Android Chrome: viewport declares `interactive-widget=resizes-content`; keyboard shrinks layout viewport itself,
 *   formula yields zero; sheet follows viewport upward, visible height only caps maximum.
 *
 * Also scroll focused input into sheet visibility (keyboard opening/focus move). Subscribe only when `enabled`.
 */
export function useSheetViewport(enabled: boolean, container: HTMLElement | null): SheetViewport {
  const [state, setState] = useState<SheetViewport>({ keyboardInset: 0, viewportHeight: null });
  useEffect(() => {
    if (!enabled || typeof window === "undefined") {
      setState({ keyboardInset: 0, viewportHeight: null });
      return;
    }
    const viewport = window.visualViewport;
    let frame = 0;
    const revealFocused = () => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !container?.contains(active)) return;
      if (!active.matches("input, textarea, select, [contenteditable=''], [contenteditable='true']")) return;
      active.scrollIntoView?.({ block: "nearest" });
    };
    const update = () => {
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
      const run = () => {
        if (viewport) {
          const next = sheetViewportFrom({
            layoutHeight: window.innerHeight,
            visualHeight: viewport.height,
            offsetTop: viewport.offsetTop,
            scale: viewport.scale,
          });
          setState((prev) =>
            prev.keyboardInset === next.keyboardInset && prev.viewportHeight === next.viewportHeight ? prev : next,
          );
        }
        revealFocused();
      };
      if (typeof requestAnimationFrame === "function") frame = requestAnimationFrame(run);
      else run();
    };
    viewport?.addEventListener("resize", update);
    viewport?.addEventListener("scroll", update);
    container?.addEventListener("focusin", update);
    update();
    return () => {
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
      viewport?.removeEventListener("resize", update);
      viewport?.removeEventListener("scroll", update);
      container?.removeEventListener("focusin", update);
    };
  }, [enabled, container]);
  return enabled ? state : { keyboardInset: 0, viewportHeight: null };
}
