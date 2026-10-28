/**
 * useVirtualViewport — scroll the cursor into view when the mobile virtual keyboard opens.
 *
 * visualViewport API supported on iOS 13+ / Android Chrome.
 * Old-browser fallback: ignore resize; users scroll manually.
 */

"use client";

import { useEffect } from "react";

export function useVirtualViewport() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const vv = (window as any).visualViewport as VisualViewport | undefined;
    if (!vv) return;

    const onResize = () => {
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount) return;
      try {
        const range = sel.getRangeAt(0);
        const rect = range.getBoundingClientRect();
        // Once keyboard opens, vv.height < window.innerHeight.
        // Scroll the cursor into view when below the keyboard.
        if (rect.bottom > vv.height && rect.bottom > 0) {
          const target = (sel.focusNode as Element)?.parentElement;
          target?.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      } catch {
        // Some selection states cannot be read; ignore.
      }
    };
    vv.addEventListener("resize", onResize);
    return () => vv.removeEventListener("resize", onResize);
  }, []);
}
