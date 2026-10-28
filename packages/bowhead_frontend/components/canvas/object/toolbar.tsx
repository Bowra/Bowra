"use client";

import { createContext, useContext, useLayoutEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLocale, useTranslations } from "next-intl";

import { Check, ChevronLeft, AlertCircle } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { RowActionsMenu, type RowAction } from "@/components/ui/row-actions-menu";
import { Spinner } from "./status-badge";
import styles from "./canvas-object.module.css";

export type CanvasSaveState = "new" | "saved" | "saving" | "dirty" | "failed";

/** Hosted editors portal their toolbar actions here instead of rendering their own header. */
export const CanvasToolbarSlotContext = createContext<HTMLElement | null>(null);

/** Render children into the surrounding object toolbar's action slot (no-op outside a canvas object). */
export function CanvasToolbarActions({ children }: { children: ReactNode }) {
  const slot = useContext(CanvasToolbarSlotContext);
  return slot ? createPortal(children, slot) : null;
}

export function formatSavedTime(value: number | string | Date, locale: string): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

/** C2 · save state text (12 ink3, tabular); failures add “Retry save”. */
export function SaveState({ state, savedAt, onRetry }: { state: CanvasSaveState; savedAt?: number | string | Date | null; onRetry?: () => void }) {
  const t = useTranslations("canvas_object");
  const locale = useLocale();
  if (state === "saving") return <span className={styles.toolbarMeta} role="status"><Spinner />{t("save_state_saving")}</span>;
  if (state === "dirty") return <span className={styles.toolbarMeta} data-tone="warning" role="status"><span className={styles.dot} aria-hidden />{t("save_state_dirty")}</span>;
  if (state === "failed") return <>
    <span className={styles.toolbarMeta} data-tone="danger" role="alert"><AlertCircle size={14} aria-hidden />{t("save_state_failed")}</span>
    {onRetry ? <Button size="sm" variant="ghost" onClick={onRetry}>{t("retry_save")}</Button> : null}
  </>;
  if (state === "saved") {
    const time = savedAt ? formatSavedTime(savedAt, locale) : "";
    return <span className={styles.toolbarMeta} role="status"><Check size={14} aria-hidden />{time ? t("save_state_saved_at", { time }) : t("save_state_saved")}</span>;
  }
  return <span className={styles.toolbarMeta} role="status">{t("save_state_new")}</span>;
}

/**
 * C2 · object toolbar (40, bar material). Left: status badge + save state or time; when entered from
 * another object, a back button first. Right: at most two secondary actions and “⋯”. Never holds the
 * primary action (footer) or close (tab ×). In medium and narrow containers text actions fold into “⋯”.
 */
export function CanvasObjectToolbar({
  status,
  save,
  savedAt,
  onRetrySave,
  meta,
  note,
  back,
  actions,
  overflow,
  overflowLabel,
  actionSlotRef,
  statusInBodyWhenNarrow = false,
}: {
  status?: ReactNode;
  save?: CanvasSaveState;
  savedAt?: number | string | Date | null;
  onRetrySave?: () => void;
  /** Plain meta such as a generation time; hidden in narrow containers when `metaCollapsible`. */
  meta?: ReactNode;
  /** A pending-progress note that stays visible at every width (e.g. “closing once the save lands”). */
  note?: ReactNode;
  back?: { label: string; onClick: () => void };
  /** At most two text actions (secondary buttons). */
  actions?: ReactNode;
  overflow?: RowAction[];
  overflowLabel?: string;
  /** Slot that hosted editors fill through <CanvasToolbarActions>. */
  actionSlotRef?: (element: HTMLElement | null) => void;
  /** Narrow canvases show the status in the object body instead (e.g. the document review progress row). */
  statusInBodyWhenNarrow?: boolean;
}) {
  const common = useTranslations("common");
  const endRef = useRef<HTMLSpanElement>(null);
  // Narrow canvases (< 480): the actions and “⋯” join the object row beside the tab ×, so the toolbar row only
  // remains when it has status to show. The pane reserves the actions' width in that row.
  useLayoutEffect(() => {
    const end = endRef.current;
    const pane = end?.closest<HTMLElement>("[data-canvas-pane]");
    if (!end || !pane || typeof ResizeObserver !== "function") return;
    const report = () => {
      if (end.offsetParent === null) return;
      pane.style.setProperty("--canvas-toolbar-end", `${Math.ceil(end.getBoundingClientRect().width)}px`);
    };
    report();
    const observer = new ResizeObserver(report);
    observer.observe(end);
    return () => observer.disconnect();
  }, []);
  return (
    <div className={styles.toolbar} role="toolbar" aria-label={overflowLabel}>
      <span className={styles.toolbarStart}>
        {back ? (
          <>
            <Button size="sm" variant="plain" className={styles.backButton} onClick={back.onClick}>
              <ChevronLeft size={14} aria-hidden /><span>{back.label}</span>
            </Button>
            <span className={styles.navSeparator} aria-hidden />
          </>
        ) : null}
        {status ? (statusInBodyWhenNarrow ? <span className={styles.narrowHidden}>{status}</span> : status) : null}
        {save ? <SaveState state={save} savedAt={savedAt} onRetry={onRetrySave} /> : null}
        {note ? <span className={styles.toolbarMeta} role="status"><Spinner />{note}</span> : null}
        {meta ? <span className={`${styles.toolbarMeta} ${styles.toolbarTime}`}>{meta}</span> : null}
      </span>
      <span className={styles.toolbarEnd} ref={endRef}>
        {actions ? <span className={styles.toolbarActions}>{actions}</span> : null}
        {actionSlotRef ? <span className={styles.toolbarActions} ref={actionSlotRef} /> : null}
        {overflow?.length ? <RowActionsMenu label={overflowLabel ?? common("more_actions")} actions={overflow} className={styles.iconButton} /> : null}
      </span>
    </div>
  );
}

/** A toolbar text action that collapses to its icon (same tooltip and name) in medium and narrow containers. */
export function ToolbarAction({ icon, label, onClick, disabled }: { icon: ReactNode; label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <Button variant="secondary" className={styles.collapsibleAction} aria-label={label} title={label} onClick={onClick} disabled={disabled}>
      {icon}<span className={styles.collapsibleText}>{label}</span>
    </Button>
  );
}
