"use client";

/**
 * Question-mark help popover (component spec "Help Popover Spec", decision 21): long explanations open from a
 * question mark in a small non-modal popover that works on touch; Tooltips keep only names and shortcuts.
 *
 * - Trigger: a button with a 15px CircleHelp icon, 24×24 hit area (44×44 on coarse pointers), named
 *   "About {field}", with aria-expanded / aria-controls.
 * - Surface: the menu material (`--ui-material-bg`, `--shadow-overlay`, radius 12), width min(320px, 100vw − 32px),
 *   anchored 8px below the trigger and shifted into the viewport when it would overflow.
 * - Opens and closes on press (Enter / Space too); Esc, a press outside or the trigger again close it and return
 *   focus to the trigger. Only one is open at a time.
 */

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { CircleHelp } from "lucide-react";

const OPEN_EVENT = "bowhead:help-popover-open";
const VIEWPORT_GUTTER = 16;

export function HelpPopover({ label, title, children, className }: {
  /** Accessible name of the trigger, for example "About held out". */
  label: string;
  /** Optional heading; it also names the popover. */
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  const popoverId = `${id}-help`;
  const titleId = `${id}-title`;
  const [open, setOpen] = useState(false);
  const [shift, setShift] = useState(0);
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    // Only one help popover is open at a time.
    window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: popoverId }));
    const onOther = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== popoverId) setOpen(false);
    };
    const onPointer = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close(true);
      }
    };
    window.addEventListener(OPEN_EVENT, onOther);
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener(OPEN_EVENT, onOther);
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [close, open, popoverId]);

  // Keep the popover inside the viewport, 16px from either edge.
  useLayoutEffect(() => {
    if (!open || !popoverRef.current) {
      setShift(0);
      return;
    }
    const rect = popoverRef.current.getBoundingClientRect();
    const width = typeof window === "undefined" ? 0 : window.innerWidth;
    if (rect.right > width - VIEWPORT_GUTTER) setShift(width - VIEWPORT_GUTTER - rect.right);
    else if (rect.left < VIEWPORT_GUTTER) setShift(VIEWPORT_GUTTER - rect.left);
  }, [open]);

  return (
    <span ref={rootRef} className={`ui-help-popover${className ? ` ${className}` : ""}`}>
      <button
        ref={triggerRef}
        type="button"
        className="ui-help-popover__trigger"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        data-open={open || undefined}
        onClick={() => (open ? close(false) : setOpen(true))}
      >
        <CircleHelp size={15} strokeWidth={1.5} aria-hidden />
      </button>
      {open ? (
        <div
          ref={popoverRef}
          id={popoverId}
          role="dialog"
          aria-modal="false"
          aria-labelledby={title ? titleId : undefined}
          aria-label={title ? undefined : label}
          className="ui-help-popover__surface"
          style={shift ? { transform: `translateX(${shift}px)` } : undefined}
        >
          {title ? <strong id={titleId} className="ui-help-popover__title">{title}</strong> : null}
          <div className="ui-help-popover__body">{children}</div>
        </div>
      ) : null}
    </span>
  );
}
