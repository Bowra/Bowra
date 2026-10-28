/**
 * W1-5 reorder helpers: extract top-level section order from PM doc + order diff.
 * Separate module for unit testing; consumed by use-debounced-sync.
 */

import type { JSONContent } from "@tiptap/react";

/** Top-level section order (attrs.sectionId on top-level nodes; skip paragraphs/etc. without sectionId). */
export function extractTopLevelSectionOrder(doc: JSONContent): string[] {
  const ids: string[] = [];
  const content = (doc.content ?? []) as JSONContent[];
  for (const node of content) {
    const sid = (node.attrs as { sectionId?: string } | undefined)?.sectionId;
    if (sid) ids.push(sid);
  }
  return ids;
}

/** Whether two order arrays match (sensitive to elements and positions). */
export function ordersEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * A3 (report-edit-audit): restore editor-visible top-level display order to **complete stored order**.
 *
 * Editor contains only 6 top-level nodes (charts inline in narrative, code_block folded into appendix); directly sending
 * those 6 IDs to `/reorder` collides with remaining sections' seq (P1-1 root cause; server now
 * completes/reindexes as backstop, but callers should send the correct full order).
 *
 * Algorithm: start with complete stored order, refill positions **belonging to the display set** in displayOrder sequence; keep non-display
 * slots (inline charts / folded code blocks) fixed—accurately representing relative reordering
 * among visible blocks without pushing inline/folded blocks to the end.
 */
export function mapDisplayOrderToFullOrder(
  fullOrder: string[],
  displayOrder: string[],
): string[] {
  const displaySet = new Set(displayOrder);
  // Replace only stored IDs belonging to the display set; ignore displayOrder IDs no longer in storage.
  const slots: number[] = [];
  for (let i = 0; i < fullOrder.length; i++) {
    if (displaySet.has(fullOrder[i])) slots.push(i);
  }
  const fullSet = new Set(fullOrder);
  const incoming = displayOrder.filter((id) => fullSet.has(id));
  if (slots.length !== incoming.length) {
    // Display set/storage order mismatch (streaming or recent additions/deletions) → do not guess; return stored order for server completion.
    return fullOrder;
  }
  const out = [...fullOrder];
  slots.forEach((slot, idx) => {
    out[slot] = incoming[idx];
  });
  return out;
}
