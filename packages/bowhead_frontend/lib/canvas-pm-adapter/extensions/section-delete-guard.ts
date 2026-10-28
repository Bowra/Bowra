/**
 * SectionDeleteGuardExtension — A0-2 (report-edit-audit P0-2 option B, immediate mitigation).
 *
 * Prevent deletion of top-level section blocks in the editor: deletion was never persisted (`pm-to-canvas.ts` diff
 * visits only sections still in the document, so disappearance produces no patch). Previously, Backspace deleted a block →
 * DOM disappeared + status falsely reported "已保存" → F5 resurrected it. Real deletion (including cascading/undo semantics)
 * is implemented in batch A with the W1 projection contract; this guard first removes the illusion of deletion.
 *
 * Implementation: filterTransaction compares top-level `section_*` node id sets before and after a transaction,
 * blocking any disappearance and showing a toast. This also blocks the P1-3 vector of typing directly on a NodeSelection
 * to replace an entire eyebrow (also a section-removal transaction).
 *
 * Allowed paths (only meta paths remain since guard-fulldoc-hole r1):
 * 1. `preventUpdate` meta: tiptap `setContent` always writes it (canvas switch / SSE refresh /
 *    conflict reload / split-merge reconciliation all use it); check **presence**, not value.
 * 2. Explicit `ALLOW_SECTION_DELETE_META` (used when wiring actual deletion in batch A).
 * The structural path of one step replacing the full doc was removed: after the meta path shipped, it protected no legitimate traffic,
 * serving only user Cmd+A delete-all (see docs/sprints/report-edit-audit-2026-07-29/
 * guard-fulldoc-hole-tech-design.md).
 */

import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import type { Node as PMNode } from "@tiptap/pm/model";

import { staticTranslator } from "@/lib/locale-messages";
import { toast } from "@/lib/use-toast";

import { kindHasDeleteMenu } from "./section-chrome";

export const ALLOW_SECTION_DELETE_META = "bowheadAllowSectionDelete";

const SectionDeleteGuardKey = new PluginKey("sectionDeleteGuard");

export function topLevelSectionIds(doc: PMNode): Set<string> {
  const ids = new Set<string>();
  doc.forEach((child) => {
    if (child.type.name.startsWith("section_")) {
      const sid = child.attrs?.sectionId;
      if (typeof sid === "string" && sid) ids.add(sid);
    }
  });
  return ids;
}

/** True if any top-level section in before disappears in after. */
export function sectionRemovedBetween(
  beforeDoc: PMNode,
  afterDoc: PMNode,
): boolean {
  return removedSectionKinds(beforeDoc, afterDoc).length > 0;
}

/** r3 §5.1: kinds of removed top-level sections (`section_` prefix stripped), used by the toast
 * to branch its guidance by kind. */
export function removedSectionKinds(
  beforeDoc: PMNode,
  afterDoc: PMNode,
): string[] {
  const before = topLevelSectionIds(beforeDoc);
  if (before.size === 0) return [];
  const after = topLevelSectionIds(afterDoc);
  const missing = new Set<string>();
  for (const sid of before) {
    if (!after.has(sid)) missing.add(sid);
  }
  if (missing.size === 0) return [];
  const kinds: string[] = [];
  beforeDoc.forEach((child) => {
    const sid = child.attrs?.sectionId;
    if (typeof sid === "string" && missing.has(sid)) {
      kinds.push(child.type.name.replace(/^section_/, ""));
    }
  });
  return kinds;
}

export type RemovalDecision =
  | "allow"
  | "block_multi"
  | "block_menu"
  | "block_unsupported";

/**
 * guard-fulldoc-hole §4.1: allow/block decision (pure function, enforced by unit tests).
 *
 * A second allowed path once existed: `isFullDocReplace` (a single ReplaceStep covering the full doc),
 * a fallback **before** the preventUpdate meta check shipped. tiptap `setContent` always writes
 * `setMeta('preventUpdate', !emitUpdate)` (@tiptap/core 2.27.2 dist:2515), and the sole repository
 * setContent call is report-view rehydration. The structural path therefore protected no legitimate traffic,
 * serving only user Cmd+A delete-all: the entire report vanished + status falsely reported "已保存" (observed on 116 1.0.44).
 * Removed; meta paths cover all programmatic operations.
 *
 * Check meta **presence**, not value: its meaning is `!emitUpdate`; a future setContent(x, true)
 * writes false (falsy), still a programmatic path that a truthiness check would wrongly block.
 */
export function decideSectionRemoval(args: {
  hasAllowMeta: boolean;
  hasPreventUpdateMeta: boolean;
  removedKinds: string[];
}): RemovalDecision {
  if (args.hasAllowMeta || args.hasPreventUpdateMeta) return "allow";
  if (args.removedKinds.length === 0) return "allow";
  // Removing multiple blocks (delete-all is the sole entry) cannot be addressed by single-block menu guidance.
  if (args.removedKinds.length > 1) return "block_multi";
  return kindHasDeleteMenu(args.removedKinds[0]) ? "block_menu" : "block_unsupported";
}

/**
 * Guidance shown on blocking (section_delete_guard messages in the UI language); empty string when allowed.
 * A2: deletion exists but only through the section menu (explicit entry + confirmation + server cascade). Inferred
 * keyboard deletion misclassifies superseded/inline blocks, so it remains blocked with guidance.
 * r3 §5.1: confirm that the kind actually has a menu before directing users there; meta and non-chrome kinds report unsupported.
 * guard-fulldoc-hole §3.4: delete-all removes multiple kinds; single-block guidance misses the issue → third category.
 */
export function sectionDeleteBlockedText(decision: RemovalDecision): string {
  if (decision === "allow") return "";
  return staticTranslator("section_delete_guard")(decision);
}

// Throttle toasts to avoid flooding on held/repeated keys.
let _lastToastAt = 0;
function notifyBlocked(decision: RemovalDecision): void {
  const now = Date.now();
  if (now - _lastToastAt < 1500) return;
  _lastToastAt = now;
  const text = sectionDeleteBlockedText(decision);
  if (!text) return;
  toast({ kind: "info", text });
}

export const SectionDeleteGuardExtension = Extension.create({
  name: "sectionDeleteGuard",

  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: SectionDeleteGuardKey,
        props: {
          // Batch B (P1-3 fallback): typing on a NodeSelection creates a transaction replacing the whole section;
          // filterTransaction would show "请用菜单删除", misleading for typing. Here, swallow input
          // **before** the transaction exists (nothing happens, like read-only mode). The main fix clears NodeSelection
          // on entering edit mode (report-view); this covers other paths (typing after manually selecting a node).
          handleTextInput(view) {
            const sel = view.state.selection;
            const node = (sel as { node?: { type: { name: string } } }).node;
            return Boolean(node && node.type.name.startsWith("section_"));
          },
        },
        filterTransaction(tr, state) {
          if (!tr.docChanged) return true;
          const decision = decideSectionRemoval({
            hasAllowMeta: Boolean(tr.getMeta(ALLOW_SECTION_DELETE_META)),
            // Presence, not truthiness: meta is `!emitUpdate`; setContent(x, true) writes false,
            // but remains a programmatic path (guard-fulldoc-hole §3.2).
            hasPreventUpdateMeta: tr.getMeta("preventUpdate") !== undefined,
            removedKinds: removedSectionKinds(state.doc, tr.doc),
          });
          if (decision === "allow") return true;
          notifyBlocked(decision);
          return false;
        },
      }),
    ];
  },
});
