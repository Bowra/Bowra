"use client";

/**
 * Global Toast container mounted in authenticated shell (authenticated-app-chrome / root).
 *
 * Duration rules live in lib/use-toast.ts; this handles rendering, hover/focus pause, actions, and manual dismissal.
 *
 * Accessibility: errors use aria-live="assertive"; other messages use role="status" (polite).
 * Both regions have aria-live. Dialogs (DialogShell / FloatingFocusManager) making background inert
 * exempt `[aria-live],[role=status],output`, so existing Toasts remain readable and clickable with a dialog open.
 * Do not use role="alert": floating-ui's exemption selector excludes it, so it would become inert too.
 */

import {
  AnimatePresence,
  motion,
  useReducedMotion,
} from "framer-motion";
import clsx from "clsx";
import { useTranslations } from "next-intl";
import {
  useEffect,
  useRef,
  useState,
  type FocusEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { X } from "@/components/icons";
import {
  dismissToast,
  pauseToast,
  resumeToast,
  useToasts,
  type Toast,
} from "@/lib/use-toast";

/**
 * Touch-swipe dismissal (behavior batch B, Narrow Spec「Toast 位置」: swipe up to dismiss; either horizontal direction also works).
 * Accept only touch/stylus pointers, not mouse dragging; keyboard/screen readers retain the dismiss button unchanged.
 */
export const TOAST_SWIPE_DISTANCE_UP = 32;
export const TOAST_SWIPE_DISTANCE_SIDE = 80;
export const TOAST_SWIPE_VELOCITY = 0.5;
const TOAST_SWIPE_SLOP = 8;

/** Whether releasing this swipe dismisses the Toast: up far/fast enough, or sideways far/fast enough. Never dismiss downward. */
export function isToastSwipeDismiss(dx: number, dy: number, elapsedMs: number): boolean {
  const elapsed = Math.max(1, elapsedMs);
  if (Math.abs(dx) > Math.abs(dy)) {
    const distance = Math.abs(dx);
    return distance >= TOAST_SWIPE_DISTANCE_SIDE || (distance > 16 && distance / elapsed > TOAST_SWIPE_VELOCITY);
  }
  if (dy >= 0) return false;
  const distance = -dy;
  return distance >= TOAST_SWIPE_DISTANCE_UP || (distance > 12 && distance / elapsed > TOAST_SWIPE_VELOCITY);
}

// layout-redesign v10 (shell-chrome.jsx Toast): an ink pill with a colored
// status dot (was a colored-bg white-text pill in non-cream blue/amber/red/green).
// Move original inline styles to styles/ui-controls-legacy.css .legacy-toast* (appearance-refresh batch 3):
// Base-layer values item by item; styles/ui-refresh.css switches the toasts to control-layer materials.

export function ToastContainer() {
  const toasts = useToasts();
  // chat-redesign Phase 1.2 CTO P2 close — the previous
  // `typeof window === "undefined" → return null` branch caused an SSR
  // vs client tree mismatch (server emitted nothing where the client
  // emitted the live region). Gate on a mount flag instead so server
  // and client both initially render `null`, then the client hydrates
  // the live region on first effect.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return null;
  const errors = toasts.filter((t) => t.kind === "error");
  const others = toasts.filter((t) => t.kind !== "error");
  return (
    <div data-testid="toast-container" className="legacy-toast-container">
      <div
        aria-live="assertive"
        aria-atomic="false"
        data-testid="toast-region-assertive"
        className="legacy-toast-region"
      >
        <AnimatePresence initial={false}>
          {errors.map((t) => (
            <ToastItem key={t.id} toast={t} />
          ))}
        </AnimatePresence>
      </div>
      <div
        role="status"
        aria-live="polite"
        aria-atomic="false"
        data-testid="toast-region-polite"
        className="legacy-toast-region"
      >
        <AnimatePresence initial={false}>
          {others.map((t) => (
            <ToastItem key={t.id} toast={t} />
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

function ToastItem({ toast }: { toast: Toast }) {
  const tCommon = useTranslations("common");
  const reduceMotion = useReducedMotion();
  const [actionPending, setActionPending] = useState(false);
  const [swipe, setSwipe] = useState<{ x: number; y: number } | null>(null);
  const swipeRef = useRef<{ pointerId: number; startX: number; startY: number; startTime: number; moved: boolean } | null>(null);
  useEffect(() => () => {
    // An unmounted item cannot emit mouseleave or blur to release its paused timer.
    resumeToast(toast.id);
  }, [toast.id]);
  // Browser sends a click after swipe release; non-action Toast clicks already dismiss it, requiring no extra handling;
  // if the gesture misses the threshold and springs back, that click must not dismiss it (pressing only intended a drag).
  const suppressClickRef = useRef(false);
  const swipeHandlers = {
    onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.pointerType === "mouse" || !event.isPrimary) return;
      swipeRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startTime: event.timeStamp,
        moved: false,
      };
    },
    onPointerMove: (event: ReactPointerEvent<HTMLDivElement>) => {
      const state = swipeRef.current;
      if (!state || state.pointerId !== event.pointerId) return;
      const dx = event.clientX - state.startX;
      const dy = event.clientY - state.startY;
      if (!state.moved) {
        if (Math.hypot(dx, dy) < TOAST_SWIPE_SLOP) return;
        state.moved = true;
        pauseToast(toast.id);
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }
      // Do not track downward movement (Toast is at top, so downward is meaningless); apply light damping only.
      setSwipe(Math.abs(dx) > Math.abs(dy) ? { x: dx, y: 0 } : { x: 0, y: dy < 0 ? dy : dy / 4 });
    },
    onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => {
      const state = swipeRef.current;
      if (!state || state.pointerId !== event.pointerId) return;
      swipeRef.current = null;
      if (!state.moved) return;
      suppressClickRef.current = true;
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
      const dx = event.clientX - state.startX;
      const dy = event.clientY - state.startY;
      if (isToastSwipeDismiss(dx, dy, event.timeStamp - state.startTime)) {
        dismissToast(toast.id);
        return;
      }
      setSwipe(null);
      resumeToast(toast.id);
    },
    onPointerCancel: () => {
      const state = swipeRef.current;
      swipeRef.current = null;
      setSwipe(null);
      if (state?.moved) resumeToast(toast.id);
    },
  };
  const resumeWhenFocusLeaves = (
    event: FocusEvent<HTMLDivElement>,
  ) => {
    if (
      event.relatedTarget instanceof Node &&
      event.currentTarget.contains(event.relatedTarget)
    ) {
      return;
    }
    resumeToast(toast.id);
  };
  const invokeAction = async () => {
    if (!toast.action || actionPending) return;
    pauseToast(toast.id);
    setActionPending(true);
    try {
      await toast.action.onAction();
      dismissToast(toast.id, "action");
    } catch {
      setActionPending(false);
      resumeToast(toast.id);
    }
  };

  return (
    <motion.div
      initial={reduceMotion ? false : { opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: -8 }}
      transition={{ duration: reduceMotion ? 0 : 0.18 }}
      className={clsx(
        "rho-toast",
        `rho-toast-${toast.kind}`,
        "legacy-toast",
        toast.action && "legacy-toast--action",
        toast.undo && toast.action && "ui-toast--undo",
      )}
      data-testid={`toast-${toast.kind}`}
      data-toast-id={toast.id}
      data-swiping={swipe ? "true" : undefined}
      style={swipe ? { translate: `${swipe.x}px ${swipe.y}px` } : undefined}
      {...swipeHandlers}
      onClickCapture={(event) => {
        if (!suppressClickRef.current) return;
        suppressClickRef.current = false;
        event.stopPropagation();
        event.preventDefault();
      }}
      onClick={
        toast.action ? undefined : () => dismissToast(toast.id)
      }
      onMouseEnter={() => pauseToast(toast.id)}
      onMouseLeave={() => resumeToast(toast.id)}
      onFocusCapture={() => pauseToast(toast.id)}
      onBlurCapture={resumeWhenFocusLeaves}
    >
      <span aria-hidden className="legacy-toast-dot" />
      <span>{toast.text}</span>
      {toast.count && toast.count > 1 ? (
        <span
          className="rho-toast-count legacy-toast-count"
          data-testid="toast-repeat-count"
        >
          {tCommon("toast_repeat_count", { count: toast.count })}
        </span>
      ) : null}
      {toast.action ? (
        <button
          type="button"
          className="rho-toast-action"
          disabled={actionPending}
          onClick={(event) => {
            event.stopPropagation();
            void invokeAction();
          }}
        >
          {toast.action.label}
        </button>
      ) : null}
      <button
        type="button"
        className="rho-toast-close"
        aria-label={tCommon("close")}
        onClick={(event) => {
          event.stopPropagation();
          dismissToast(toast.id);
        }}
      >
        <X size={12} aria-hidden />
      </button>
    </motion.div>
  );
}
