"use client";

import { createContext, useContext, useEffect, useLayoutEffect, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { useSheetViewport } from "@/components/ui/sheet";
import { CanvasObjectFooter } from "@/components/canvas/object/footer";
import { useCanvasObjectFooter, useReportCanvasSave } from "@/components/canvas/object/surface";

/**
 * Save bar appears only after changes (appearance decision 17; Page Layout Spec “保存模型分两种”).
 *
 * Unsaved text changes (`dirty`) place it at the content area's bottom, sticky,
 * showing “有未保存的更改”, with “放弃” and “保存” on the right. Immediately applied switches/radios bypass it.
 *
 * `SettingsShell` reserves a slot at the content area's end, passed through `SaveBarSlotContext`;
 * portal into it: short pages use content's bottom edge, long pages stick to scroll area's bottom. Without a slot, render locally, sticky (`is-inline`).
 *
 * Leave guard: with `guard` (true by default), unsaved changes intercept `beforeunload` (refresh/close tab);
 * pages with their own leave confirmation (models, Feishu, chart styles, etc.) pass `guard={false}` to retain that guard.
 */
export const SaveBarSlotContext = createContext<HTMLElement | null>(null);

export function SaveBar({
  dirty,
  saving = false,
  onSave,
  onDiscard,
  saveDisabled = false,
  guard = true,
  saveLabel,
  discardLabel,
  message,
  failed = false,
}: {
  dirty: boolean;
  saving?: boolean;
  onSave: () => void;
  onDiscard: () => void;
  /** Unsaved changes failing validation disable Save; Discard remains available. */
  saveDisabled?: boolean;
  guard?: boolean;
  saveLabel?: ReactNode;
  /** Replaces "Discard" when the consequence needs naming (for example "Discard my changes" after a conflict). */
  discardLabel?: string;
  message?: ReactNode;
  /** The last save failed; edits are kept (Canvas footer shows “Couldn’t save” and “Retry save”). */
  failed?: boolean;
}) {
  const slot = useContext(SaveBarSlotContext);
  const canvasFooter = useCanvasObjectFooter();
  const t = useTranslations("common");
  const tCanvas = useTranslations("canvas_object");
  const visible = dirty || saving;
  // Inside a Canvas object surface the bar is the object footer (C9 save variant) and reports its state
  // to the object tab marker.
  useReportCanvasSave({ dirty, saving, failed: failed && dirty }, canvasFooter !== null);
  const [bar, setBar] = useState<HTMLDivElement | null>(null);
  const [barHeight, setBarHeight] = useState(0);
  // Avoid the soft keyboard (as with behavior batch B sheets): lift bottom-stuck Save bar above keyboard covering layout viewport bottom.
  const { keyboardInset } = useSheetViewport(visible && Boolean(slot), null);

  // Narrow screens (< 1200) fix bar to viewport bottom; equal-height slot spacer keeps last content item uncovered.
  // Avoid relying on dynamic :has() invalidation (WebKit does not recalculate after portal mounts into slot).
  useLayoutEffect(() => {
    if (!bar) return;
    const measure = () => setBarHeight(Math.ceil(bar.getBoundingClientRect().height));
    measure();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(measure);
    observer.observe(bar);
    return () => observer.disconnect();
  }, [bar]);

  // Keep edited field clear of bar: on focus/visual-area change (keyboard opening), scroll up if field bottom is below bar top.
  useEffect(() => {
    if (!visible || !slot || !bar) return;
    const content = slot.parentElement;
    const reveal = () => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement) || !content?.contains(active) || bar.contains(active)) return;
      if (!active.matches("input, textarea, select, [contenteditable=''], [contenteditable='true']")) return;
      const overlap = active.getBoundingClientRect().bottom - bar.getBoundingClientRect().top + SAVEBAR_FIELD_GAP;
      if (overlap > 0 && getComputedStyle(bar).position === "fixed") window.scrollBy({ top: overlap });
    };
    const viewport = window.visualViewport;
    content?.addEventListener("focusin", reveal);
    viewport?.addEventListener("resize", reveal);
    reveal();
    return () => {
      content?.removeEventListener("focusin", reveal);
      viewport?.removeEventListener("resize", reveal);
    };
  }, [visible, slot, bar, keyboardInset]);

  useEffect(() => {
    if (!visible || !guard || !dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Older browsers need returnValue to show confirmation.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [visible, guard, dirty]);

  if (!visible) return null;

  if (canvasFooter) {
    if (!canvasFooter.slot) return null;
    return createPortal(
      <CanvasObjectFooter
        message={saving ? tCanvas("footer_saving") : failed ? tCanvas("save_failed") : (message ?? tCanvas("footer_unsaved"))}
        tone={saving ? "neutral" : failed ? "danger" : "warning"}
        secondary={[{ label: discardLabel ?? t("discard"), onClick: onDiscard, disabled: saving }]}
        primary={{
          label: failed && !saving ? tCanvas("retry_save") : (saveLabel ?? t("save")),
          onClick: onSave,
          disabled: saveDisabled,
          busy: saving,
        }}
      />,
      canvasFooter.slot,
    );
  }

  const barNode = (
    <div
      ref={setBar}
      className={`ui-savebar${slot ? "" : " is-inline"}`}
      role="region"
      aria-label={t("unsaved_changes")}
      style={slot ? ({ "--savebar-keyboard-inset": `${keyboardInset}px` } as CSSProperties) : undefined}
    >
      <span className="ui-savebar-dot" aria-hidden="true" />
      <span className="ui-savebar-text" role="status">
        {message ?? t("unsaved_changes")}
      </span>
      <span className="ui-savebar-actions">
        <Button type="button" onClick={onDiscard} disabled={saving}>
          {discardLabel ?? t("discard")}
        </Button>
        <Button
          type="button"
          variant="primary"
          onClick={onSave}
          disabled={saveDisabled || saving}
          loading={saving}
        >
          {saving ? t("saving") : (saveLabel ?? t("save"))}
        </Button>
      </span>
    </div>
  );
  if (!slot) return barNode;
  return createPortal(
    <>
      {/* Keyboard lifts bar; taller spacer lets page scroll its last item above bar. */}
      <div className="ui-savebar-spacer" aria-hidden="true" style={{ height: barHeight + keyboardInset }} />
      {barNode}
    </>,
    slot,
  );
}

/** Minimum gap between field bottom and Save bar top. */
const SAVEBAR_FIELD_GAP = 8;
