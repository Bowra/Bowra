"use client";

import { FloatingFocusManager, useDismiss, useFloating, useInteractions } from "@floating-ui/react";
import { useCallback, useEffect, useLayoutEffect, useState, type ReactNode, type RefObject } from "react";
import { cycleTabWithin, focusFirstWithin } from "@/components/ui/dialog";
import { lockBodyScroll } from "@/lib/body-scroll-lock";

export const EMBED_FULLSCREEN_ATTR = "data-rc-embed-fullscreen";
let fullscreenCount = 0;
export function useFullscreenMarker(active: boolean) {
  useLayoutEffect(() => {
    if (!active) return;
    fullscreenCount += 1;
    document.documentElement.setAttribute(EMBED_FULLSCREEN_ATTR, "");
    return () => {
      fullscreenCount = Math.max(0, fullscreenCount - 1);
      if (!fullscreenCount) document.documentElement.removeAttribute(EMBED_FULLSCREEN_ATTR);
    };
  }, [active]);
}

/** Keep the same iframe node and browsing context while expanding it. */
export function InPlaceFullView({ active, label, onClose, returnFocusRef, children }: {
  active: boolean; label: string; onClose: () => void;
  returnFocusRef: RefObject<HTMLElement | null>; children: ReactNode;
}) {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const { refs, context } = useFloating({ open: active, onOpenChange: (open) => { if (!open) onClose(); } });
  const dismiss = useDismiss(context, { enabled: active, outsidePress: false, bubbles: { escapeKey: false } });
  const { getFloatingProps } = useInteractions([dismiss]);
  const setRef = useCallback((node: HTMLDivElement | null) => {
    refs.setFloating(node); setElement(node);
  }, [refs]);
  useFullscreenMarker(active);
  useEffect(() => {
    if (!active) return;
    const unlock = lockBodyScroll();
    if (element && !element.contains(document.activeElement)) element.focus({ preventScroll: true });
    return unlock;
  }, [active, element]);
  return <FloatingFocusManager context={context} disabled={!active} modal guards={false}
    outsideElementsInert initialFocus={-1} returnFocus={returnFocusRef}>
    <div ref={setRef} className={active ? "artifact-viewer artifact-viewer-modal-panel artifact-viewer--fullscreen" : "artifact-viewer"}
      role={active ? "dialog" : undefined} aria-modal={active || undefined}
      aria-label={active ? label : undefined} tabIndex={active ? -1 : undefined}
      {...getFloatingProps({ onKeyDown: (event) => {
        if (active && event.key === "Tab" && element) cycleTabWithin(element, event);
      } })}>
      {children}
      {active ? <span tabIndex={0} aria-hidden="true" className="rc-embed-focus-sentinel"
        onFocus={() => { if (element) focusFirstWithin(element); }} /> : null}
    </div>
  </FloatingFocusManager>;
}
