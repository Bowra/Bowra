/**
 * useReportCenterStyle: unified AI rewrite dialog positioning (feedback-0713 #8).
 *
 * Background: floating-ui anchored to rewritten text/block + flip() moved dialogs above
 * paragraphs near the viewport bottom. In one operation, input anchored to the whole block while confirmation
 * anchored to rewritten text (coordsAtPos), producing two positions that alternated above/below for users.
 *
 * Reporter decided the final position: fixed at the report area's center. Use the center
 * of the report container's visible portion (.canvas-report-shell, found via editor DOM closest),
 * with position:fixed; fall back to viewport center when no container is available.
 */

"use client";

import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import type { Editor } from "@tiptap/react";

const VIEWPORT_CENTER: CSSProperties = {
  position: "fixed",
  left: "50%",
  top: "50%",
  transform: "translate(-50%, -50%)",
};

export function useReportCenterStyle(editor: Editor | null, open: boolean): CSSProperties {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  const recompute = useCallback(() => {
    if (!editor || editor.isDestroyed) {
      // review P2: symmetric with no-shell branch; during editor destruction, fall back to viewport center,
      // rather than pinning stale coordinates.
      setPos(null);
      return;
    }
    let shell: Element | null = null;
    try {
      shell = editor.view.dom.closest(".canvas-report-shell");
    } catch {
      // editor.view may throw while unmounted/destroyed; fall back to viewport center
    }
    if (!shell) {
      setPos(null);
      return;
    }
    const rect = shell.getBoundingClientRect();
    // Vertically center the container/viewport intersection (dialog remains within visible area while long reports scroll)
    const visTop = Math.max(rect.top, 0);
    const visBottom = Math.min(rect.bottom, window.innerHeight);
    setPos({
      x: rect.left + rect.width / 2,
      y: visTop + Math.max(0, visBottom - visTop) / 2,
    });
  }, [editor]);

  useEffect(() => {
    if (!open) return;
    recompute();
    window.addEventListener("resize", recompute);
    // Capture scroll events from inner scroll containers (report area itself scrolls)
    window.addEventListener("scroll", recompute, true);
    return () => {
      window.removeEventListener("resize", recompute);
      window.removeEventListener("scroll", recompute, true);
    };
  }, [open, recompute]);

  if (!pos) return VIEWPORT_CENTER;
  return {
    position: "fixed",
    left: pos.x,
    top: pos.y,
    transform: "translate(-50%, -50%)",
  };
}
