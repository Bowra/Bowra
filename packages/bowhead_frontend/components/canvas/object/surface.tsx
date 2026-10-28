"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { CanvasToolbarSlotContext } from "./toolbar";
import styles from "./canvas-object.module.css";

/** Save-bar state reported by hosted editors through the shared SaveBar (C9 save variant). */
export interface CanvasSaveReport {
  dirty: boolean;
  saving: boolean;
  failed: boolean;
}

interface CanvasObjectFooterSlot {
  slot: HTMLElement | null;
  report: (state: CanvasSaveReport) => void;
}

/** SaveBar renders into this slot (canvas appearance) when it is inside a canvas object surface. */
export const CanvasObjectFooterContext = createContext<CanvasObjectFooterSlot | null>(null);

export function useCanvasObjectFooter(): CanvasObjectFooterSlot | null {
  return useContext(CanvasObjectFooterContext);
}

/**
 * C3 · object surface: toolbar (sticky) + body (the only scroll area) + footer (sticky; hidden when
 * the object has no primary action). Padding follows the container width through
 * --canvas-pad-block / --canvas-pad-inline (wide 20/24, medium 16/20, narrow 16/16).
 */
export function CanvasObjectSurface({
  toolbar,
  footer,
  children,
  labelledBy,
  label,
  width = "default",
  busy,
  onSaveStateChange,
  toolbarSlot,
}: {
  toolbar?: ReactNode | ((slot: (element: HTMLElement | null) => void) => ReactNode);
  footer?: ReactNode;
  children: ReactNode;
  labelledBy?: string;
  label?: string;
  /** Document previews may be wider (880) than forms (760). */
  width?: "default" | "document";
  busy?: boolean;
  /** Receives SaveBar state from hosted editors (drives the tab marker). */
  onSaveStateChange?: (state: CanvasSaveReport) => void;
  /** Toolbar action slot for hosted editors; pass the element returned by the toolbar ref. */
  toolbarSlot?: HTMLElement | null;
}) {
  const [footerSlot, setFooterSlot] = useState<HTMLElement | null>(null);
  const [actionSlot, setActionSlot] = useState<HTMLElement | null>(null);
  const reportRef = useRef(onSaveStateChange);
  reportRef.current = onSaveStateChange;
  const report = useCallback((state: CanvasSaveReport) => reportRef.current?.(state), []);
  const footerContext = useMemo(() => ({ slot: footerSlot, report }), [footerSlot, report]);
  const toolbarNode = typeof toolbar === "function" ? toolbar(setActionSlot) : toolbar;
  return (
    <CanvasObjectFooterContext.Provider value={footerContext}>
      <CanvasToolbarSlotContext.Provider value={toolbarSlot ?? actionSlot}>
        <section className={styles.surface} aria-labelledby={labelledBy} aria-label={labelledBy ? undefined : label} aria-busy={busy || undefined}>
          {toolbarNode}
          <div className={styles.body}>
            <div className={styles.bodyInner} data-width={width}>
              {children}
            </div>
          </div>
          {footer}
          <div className={styles.footerSlot} ref={setFooterSlot} data-canvas-footer-slot="" />
        </section>
      </CanvasToolbarSlotContext.Provider>
    </CanvasObjectFooterContext.Provider>
  );
}

/** Places a footer (C9) rendered deep inside an object into the surface footer slot. */
export function CanvasFooterPortal({ children }: { children: ReactNode }) {
  const footer = useCanvasObjectFooter();
  if (!footer) return <>{children}</>;
  return footer.slot ? createPortal(children, footer.slot) : null;
}

/** Reports `dirty` / `saving` / `failed` to the surrounding surface while mounted. */
export function useReportCanvasSave(state: CanvasSaveReport, enabled = true) {
  const footer = useCanvasObjectFooter();
  const { dirty, saving, failed } = state;
  useEffect(() => {
    if (!footer || !enabled) return;
    footer.report({ dirty, saving, failed });
  }, [footer, enabled, dirty, saving, failed]);
  useEffect(() => {
    if (!footer || !enabled) return;
    return () => footer.report({ dirty: false, saving: false, failed: false });
  }, [footer, enabled]);
}
