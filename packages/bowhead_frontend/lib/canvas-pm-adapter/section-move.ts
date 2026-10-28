/**
 * Adjacent moves of top-level sections (mobile-tech-design §2.1).
 *
 * No touch dragging (long-press drag is a separate interaction); use Move Up/Down in action sheets. Deliberately change
 * **PM doc order**, rather than add a backend endpoint: existing debounced sync diffs top-level order
 * changes → `mapDisplayOrderToFullOrder` → `reorderCanvas` (use-debounced-sync.ts:513),
 * sharing one pipeline for touch reordering and desktop dragging (same baseline / full-order completion and rebuilding / conflict handling).
 *
 * Moving preserves the section id set, so does not trigger the deletion guard (section-delete-guard).
 */

import type { Node as PMNode } from "@tiptap/pm/model";
import type { Transaction } from "@tiptap/pm/state";

export type MoveDirection = "up" | "down";

/** Pure function: compute moved top-level order; out of bounds/not found → null (callers show a notice rather than staying silent). */
export function planSectionMove(
  order: string[],
  sectionId: string,
  dir: MoveDirection,
): string[] | null {
  const i = order.indexOf(sectionId);
  if (i < 0) return null;
  const j = dir === "up" ? i - 1 : i + 1;
  if (j < 0 || j >= order.length) return null;
  const next = [...order];
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}

interface TopLevelEntry {
  node: PMNode;
  pos: number;
  sectionId: string | null;
}

function topLevelEntries(doc: PMNode): TopLevelEntry[] {
  const out: TopLevelEntry[] = [];
  doc.forEach((node, offset) => {
    const sid = (node.attrs as { sectionId?: unknown } | undefined)?.sectionId;
    out.push({ node, pos: offset, sectionId: typeof sid === "string" ? sid : null });
  });
  return out;
}

/**
 * Swap the top-level node for sectionId with its neighbor on tr. Success true / out of bounds false.
 *
 * Use deletion + mapped reinsertion rather than full-document replaceWith: the latter rebuilds all
 * NodeViews (flashes + lost selection), while the former changes only the moved node.
 */
export function applySectionMove(
  tr: Transaction,
  doc: PMNode,
  sectionId: string,
  dir: MoveDirection,
): boolean {
  const entries = topLevelEntries(doc);
  const i = entries.findIndex((e) => e.sectionId === sectionId);
  if (i < 0) return false;
  const j = dir === "up" ? i - 1 : i + 1;
  if (j < 0 || j >= entries.length) return false;

  const moving = entries[i];
  const anchor = entries[j];
  // up: insert before the preceding node; down: after the following node. Both use **original** coordinates,
  // then map through deletion via tr.mapping: down's target follows the deletion and automatically shifts earlier.
  const targetOriginalPos =
    dir === "up" ? anchor.pos : anchor.pos + anchor.node.nodeSize;
  tr.delete(moving.pos, moving.pos + moving.node.nodeSize);
  tr.insert(tr.mapping.map(targetOriginalPos), moving.node);
  return true;
}
