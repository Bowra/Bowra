/**
 * Batch 1 2.1 (chart-table-ux): attach captions and strip their prefix using decorations.
 *
 * Production fact (discovery §4.2): 97% of captions start with "图N：" immediately after a [figure:] anchor, but the system
 * has never numbered any figure; the body references nonexistent numbering. Decision (PRD §3.2): introduce
 * no figure numbers; [N] means provenance only. The view **attaches the adjacent caption paragraph with caption styling**
 * and hides its "图N：" prefix.
 *
 * Approach: ProseMirror decorations (view-only, zero document changes, zero round-trip risk; precedents:
 * citation-plugin.ts / ai-rewrite-decorations.ts):
 *   - Read-only mode: a sibling paragraph immediately after figure_ref matching CAPTION_RE gets a node decoration
 *     `rc-fig-caption` (caption styling) plus prefix inline decoration `rc-fig-caption-prefix`
 *     (CSS display:none).
 *   - Edit mode: **zero decorations**; users see and can edit the original text (including "图1："),
 *     which is saved unchanged. Idempotent, no migration.
 *
 * ⚠ CAPTION_RE and FIGURE_CAPTION_PREFIX_RE in `bowhead_core/export_ir.py` are
 * **duplicate constants across languages**: changes to either must also change the other. The guard is
 * `packages/bowhead_core/tests/test_figure_caption_parity.py`, which **extracts the regex
 * literal from this file**, compares it verbatim and runs both engines on the same samples. (This previously claimed
 * matching golden literals on both sides guaranteed parity, but that mechanism did not exist then: the golden sets
 * were independent copies; changing one regex and its golden stayed green, while behavior actually diverged.)
 */

import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

// (\*\*)? supports two representations: PM textContent lacks Markdown syntax ("图1：…"), while export
// uses raw Markdown ("**图1**：…").
// ⚠ `[0-9]` and `[ \t]` explicitly narrow the sets; do not use `\d` / `\s`: Python `\d` includes fullwidth digits,
// and its `\s` whitespace set also differs from JS (`\ufeff` / `\x85` diverge). Identical literals behave differently:
// review measured 648 divergences across 986 samples. Both sides now use explicit ASCII sets.
export const CAPTION_RE = /^(\*\*)?图[ \t]?[0-9]+(\*\*)?[ \t]*[:：][ \t]*/;

/** Return prefix length in textContent units on a match, otherwise 0. */
export function captionPrefixLength(text: string): number {
  const m = CAPTION_RE.exec(text);
  return m ? m[0].length : 0;
}

/** Strip the "图N：" prefix (return unchanged on no match), feeding frontend caption text to the presentation resolver. */
export function stripCaptionPrefix(text: string): string {
  return text.replace(CAPTION_RE, "");
}

export const FigureCaptionPluginKey = new PluginKey("figureCaption");

export const FigureCaptionExtension = Extension.create({
  name: "figureCaption",

  addProseMirrorPlugins() {
    const editor = this.editor;
    // Same as citation-plugin #9: do not rescan selection-only transactions.
    let cacheDoc: import("@tiptap/pm/model").Node | null = null;
    let cacheEditable: boolean | null = null;
    let cacheSet = DecorationSet.empty;
    return [
      new Plugin({
        key: FigureCaptionPluginKey,
        props: {
          decorations(state) {
            const editable = editor?.isEditable ?? false;
            if (state.doc === cacheDoc && editable === cacheEditable) return cacheSet;
            cacheDoc = state.doc;
            cacheEditable = editable;
            if (editable) {
              cacheSet = DecorationSet.empty;
              return cacheSet;
            }
            const decos: Decoration[] = [];
            state.doc.descendants((node, pos) => {
              if (node.type.name !== "figure_ref") return;
              const $pos = state.doc.resolve(pos);
              const parent = $pos.parent;
              const index = $pos.index();
              if (index + 1 >= parent.childCount) return;
              const next = parent.child(index + 1);
              if (next.type.name !== "paragraph") return;
              const prefixLen = captionPrefixLength(next.textContent);
              if (prefixLen === 0) return;
              const nextPos = pos + node.nodeSize;
              decos.push(
                Decoration.node(nextPos, nextPos + next.nodeSize, { class: "rc-fig-caption" }),
                // +1 enters paragraph content; plain-text textContent offsets map 1:1 to PM positions
                Decoration.inline(nextPos + 1, nextPos + 1 + prefixLen, {
                  class: "rc-fig-caption-prefix",
                }),
              );
            });
            cacheSet = DecorationSet.create(state.doc, decos);
            return cacheSet;
          },
        },
      }),
    ];
  },
});
