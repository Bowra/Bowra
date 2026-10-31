"use client";

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { autoUpdate, flip, FloatingPortal, offset, shift, size, useFloating } from "@floating-ui/react";
import { CircleHelp } from "lucide-react";
import { rootStackingLayer } from "@/lib/layer";

const OPEN_EVENT = "bowhead:help-popover-open";
type State = "closed" | "preview" | "pinned";

/** Read-only, nonmodal help. Existing consumers remain press-only unless preview is requested. */
export function HelpPopover({
  label,
  title,
  children,
  className,
  preview = false,
}: {
  label: string;
  title?: string;
  children: ReactNode;
  className?: string;
  preview?: boolean;
}) {
  const id = useId();
  const [state, setState] = useState<State>("closed");
  const open = state !== "closed";
  const trigger = useRef<HTMLButtonElement | null>(null);
  const surface = useRef<HTMLDivElement | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const suppressFocus = useRef(false);
  const { refs, floatingStyles } = useFloating({
    open,
    placement: "bottom-start",
    strategy: "fixed",
    whileElementsMounted: autoUpdate,
    middleware: [
      offset(8),
      flip({ padding: 16 }),
      shift({ padding: 16 }),
      size({
        padding: 16,
        apply({ availableHeight, elements }) {
          elements.floating.style.maxHeight = `${Math.max(0, availableHeight)}px`;
        },
      }),
    ],
  });
  const cancel = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  const close = useCallback(
    (restore: boolean) => {
      cancel();
      setState("closed");
      if (restore) {
        suppressFocus.current = true;
        trigger.current?.focus();
      }
    },
    [cancel],
  );
  useEffect(() => cancel, [cancel]);
  useEffect(() => {
    if (!open) return;
    window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: id }));
    const other = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== id) close(false);
    };
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !surface.current?.contains(event.target as Node)) close(false);
    };
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Capture before the containing sheet/dialog so Escape dismisses this top layer only.
      event.preventDefault();
      event.stopImmediatePropagation();
      const focused = document.activeElement;
      close(state === "pinned" || !preview || focused === trigger.current || Boolean(surface.current?.contains(focused)));
    };
    window.addEventListener(OPEN_EVENT, other);
    document.addEventListener("pointerdown", outside);
    window.addEventListener("keydown", key, true);
    return () => {
      window.removeEventListener(OPEN_EVENT, other);
      document.removeEventListener("pointerdown", outside);
      window.removeEventListener("keydown", key, true);
    };
  }, [open, close, id, preview, state]);
  const enter = () => {
    cancel();
    if (preview && state === "closed" && window.matchMedia("(hover: hover) and (pointer: fine)").matches)
      timer.current = setTimeout(() => setState("preview"), 300);
  };
  const leave = () => {
    cancel();
    if (state === "preview") timer.current = setTimeout(() => {
      const focused = document.activeElement;
      if (focused !== trigger.current && !surface.current?.contains(focused)) close(false);
    }, 150);
  };
  return (
    <span className={`ui-help-popover${className ? ` ${className}` : ""}`}>
      <button
        ref={(node) => {
          trigger.current = node;
          refs.setReference(node);
        }}
        type="button"
        className="ui-help-popover__trigger"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        data-open={open || undefined}
        onPointerEnter={enter}
        onPointerLeave={leave}
        onFocus={() => {
          if (suppressFocus.current) {
            suppressFocus.current = false;
            return;
          }
          if (preview) {
            cancel();
            setState((s) => (s === "pinned" ? s : "preview"));
          }
        }}
        onBlur={(event) => {
          suppressFocus.current = false;
          if (!surface.current?.contains(event.relatedTarget)) close(false);
        }}
        onClick={() => {
          cancel();
          if (state === "pinned") close(false);
          else setState("pinned");
        }}
      >
        <CircleHelp size={15} strokeWidth={1.5} aria-hidden />
      </button>
      {open ? (
        <FloatingPortal>
          <div
            ref={(node) => {
              surface.current = node;
              refs.setFloating(node);
            }}
            id={id}
            role="dialog"
            tabIndex={-1}
            aria-modal="false"
            aria-label={title || label}
            className="ui-help-popover__surface"
            style={{ ...floatingStyles, zIndex: Math.max(40, rootStackingLayer(trigger.current) + 1), overflowY: "auto" }}
            onPointerEnter={cancel}
            onPointerLeave={leave}
            onBlur={(event) => {
              if (!surface.current?.contains(event.relatedTarget) && event.relatedTarget !== trigger.current) close(false);
            }}
          >
            {title ? <strong className="ui-help-popover__title">{title}</strong> : null}
            <div className="ui-help-popover__body">{children}</div>
          </div>
        </FloatingPortal>
      ) : null}
    </span>
  );
}
