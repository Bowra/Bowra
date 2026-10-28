/**
 * Visuals for AI rewrite revision mode (Feishu 07.06 #15b).
 *
 * Incident: while an AI rewrite awaited Accept/Revert, old and new text **coexisted with
 * identical styling**, leaving users unable to distinguish them (verbatim feedback: "前后没有区分").
 * The original implementation only promised "P3.4.5 actually adds a mark" in a comment; it never did.
 *
 * Fix: strike through and gray out old text (to be deleted), and highlight new text (added), as in Word revisions.
 * Use **Decoration (view-only)** rather than Mark: never include it in doc.getJSON or persistence, so PATCH saves
 * omit these styles; accepting/rejecting clears meta and removes them. Ranges follow document edits through mapping.
 */
import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import type { EditorView } from "@tiptap/pm/view";

export interface AiRewriteRanges {
  /** Original text range before rewriting (to be deleted → struck through and grayed out). */
  old: { from: number; to: number } | null;
  /** New text range after rewriting (added → highlighted background). */
  new: { from: number; to: number } | null;
  /** Literal candidate text rendered only in the view, outside the document and undo history. */
  preview?: string;
}

export const AiRewriteDecoKey = new PluginKey<AiRewriteRanges | null>("aiRewriteDeco");

/** Bare ProseMirror plugin, exported for tests to construct EditorState directly (without a tiptap Editor). */
export function aiRewritePlugin(): Plugin<AiRewriteRanges | null> {
  return new Plugin<AiRewriteRanges | null>({
    key: AiRewriteDecoKey,
    state: {
      init: () => null,
      apply(tr, value) {
        // Explicit setMeta(ranges | null) → use it directly (set / clear).
        const meta = tr.getMeta(AiRewriteDecoKey) as AiRewriteRanges | null | undefined;
        if (meta !== undefined) return meta;
        // Follow real edits; boundary insertions remain outside the original selection.
        if (!value) return value;
        const mapR = (r: { from: number; to: number } | null) =>
          r ? { from: tr.mapping.map(r.from, 1), to: tr.mapping.map(r.to, -1) } : null;
        return { ...value, old: mapR(value.old), new: mapR(value.new) };
      },
    },
    props: {
      decorations(state) {
        const ranges = AiRewriteDecoKey.getState(state);
        if (!ranges) return DecorationSet.empty;
        const decos: Decoration[] = [];
        if (ranges.old && ranges.old.to > ranges.old.from) {
          decos.push(
            Decoration.inline(ranges.old.from, ranges.old.to, { class: "ai-rewrite-old" }),
          );
        }
        if (ranges.new && ranges.new.to > ranges.new.from) {
          decos.push(
            Decoration.inline(ranges.new.from, ranges.new.to, { class: "ai-rewrite-new" }),
          );
        }
        if (ranges.old && ranges.preview) {
          decos.push(
            Decoration.widget(
              ranges.old.to,
              () => {
                const preview = document.createElement("span");
                preview.className = "ai-rewrite-new";
                preview.style.whiteSpace = "pre-wrap";
                preview.textContent = ranges.preview!;
                return preview;
              },
              { side: 1 },
            ),
          );
        }
        return DecorationSet.create(state.doc, decos);
      },
    },
  });
}

export const AiRewriteDecorationsExtension = Extension.create({
  name: "aiRewriteDecorations",
  addProseMirrorPlugins() {
    return [aiRewritePlugin()];
  },
});

/** Dispatch helper: set pending revision ranges or clear them (null). Silently skip a missing view (unmounted/tests). */
export function setAiRewriteDeco(
  view: EditorView | null | undefined,
  ranges: AiRewriteRanges | null,
): void {
  if (!view) return;
  view.dispatch(view.state.tr.setMeta(AiRewriteDecoKey, ranges).setMeta("addToHistory", false));
}
