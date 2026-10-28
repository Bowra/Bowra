"use client";

import { useLayoutEffect, type RefObject } from "react";
import { THEME_CONFIG_CHANGED_EVT } from "@/lib/theme-config";

export function useTextareaAutosize(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
  locale: string,
): void {
  useLayoutEffect(() => {
    const textarea = ref.current;
    if (!textarea) return;

    const resize = () => {
      const selectionStart = textarea.selectionStart;
      const selectionEnd = textarea.selectionEnd;
      const direction = textarea.selectionDirection;
      textarea.style.height = "0px";
      const styles = getComputedStyle(textarea);
      const lineHeight = Number.parseFloat(styles.lineHeight) || 22;
      const padding =
        (Number.parseFloat(styles.paddingTop) || 0)
        + (Number.parseFloat(styles.paddingBottom) || 0);
      const maxHeight = Math.min(180, lineHeight * 6 + padding);
      const nextHeight = Math.min(textarea.scrollHeight, maxHeight);
      textarea.style.height = `${nextHeight}px`;
      textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
      if (document.activeElement === textarea) {
        textarea.setSelectionRange(selectionStart, selectionEnd, direction);
      }
    };

    resize();
    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(resize);
    observer?.observe(textarea);
    document.fonts?.ready.then(resize).catch(() => undefined);
    document.fonts?.addEventListener?.("loadingdone", resize);
    window.addEventListener(THEME_CONFIG_CHANGED_EVT, resize);
    return () => {
      observer?.disconnect();
      document.fonts?.removeEventListener?.("loadingdone", resize);
      window.removeEventListener(THEME_CONFIG_CHANGED_EVT, resize);
    };
  }, [locale, ref, value]);
}
