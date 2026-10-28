"use client";

import { useEffect } from "react";
import type { Editor } from "@tiptap/react";

/** Meta key on empty transactions: identifies decoration-recalculation-only transactions to the citation plugin. */
export const CITATION_DENIED_REFRESH_META = "citationDeniedRefresh";

/**
 * Canvas batch 3: when the restricted-source set (sources for `data-source-denied`) changes without body changes, ProseMirror does not recompute
 * decorations itself; `decorations()` runs only when the view receives a transaction. On each set-object replacement (flag enabled after hydration, source reassessment),
 * dispatch a meta-only empty transaction to rerun the citation plugin: no document changes, undo-history entries or saving.
 *
 * Callers memoize the set (same contents, same object); the empty set is a shared constant, avoiding repeated transactions when nothing is denied.
 */
export function useCitationDecorationRefresh(
  editor: Pick<Editor, "isDestroyed" | "state" | "view"> | null | undefined,
  deniedSourceIndexes: ReadonlySet<number>,
): void {
  useEffect(() => {
    if (!editor || editor.isDestroyed) return;
    editor.view.dispatch(
      editor.state.tr.setMeta("addToHistory", false).setMeta(CITATION_DENIED_REFRESH_META, true),
    );
  }, [editor, deniedSourceIndexes]);
}
