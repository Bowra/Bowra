/**
 * Normalize a canvas PM doc to the form this editor will actually hold after loading it.
 *
 * ── Why this is needed ─────────────────────────────────────────────────
 * To decide whether this doc needs setContent, `report-view.tsx` compares
 *   `JSON.stringify(editor.getJSON()) !== JSON.stringify(initialDoc)`
 * — the left is **serialized through the editor schema**, the right is **raw output** from `canvasToPMDoc(...)`.
 * These are **different representations**; any producer attr absent from the schema makes this comparison always true.
 *
 * Observed on 116 prod, 2026-08-08: a real 30-section, 50–150kb report had **only 1 difference**
 *   `$.content[17].content[19].attrs: {"tight":true} -> undefined`
 * — `prosemirror-markdown` gives lists `tight`, which Tiptap StarterKit `bulletList` does not declare,
 * so parsing drops it. This one attr keeps the gate true: any rerender during page loading that changes
 * `initialDoc` object identity triggers another **full-document** setContent (observed ~13 times).
 * Of 288 canvases, 183 (64%) contained Markdown lists, all affected.
 *
 * ── Why fix here, rather than remove `tight` or change the producer ──────
 * `tight` **is not** noise: the custom serializer's `bullet_list` calls `state.renderList(...)`,
 * and `prosemirror-markdown` renderList reads `node.attrs.tight` to choose tight/loose lists.
 * Removing it writes tight lists back as loose ones; we tried this and existing round-trip tests caught it immediately.
 *
 * The gate's **intent** is to ask whether applying this doc changes editor content. An honest answer must compare
 * the form the editor would hold after application, rather than raw output. This therefore **corrects the gate**,
 * without bypassing it: `setContent(jsonObject)` internally uses `schema.nodeFromJSON(content)`
 * (@tiptap/core `createNodeFromContent`), the same basis as this function, giving byte-identical results.
 *
 * Additional benefit: this handles **any** producer/schema drift, beyond `tight`; the next new attr
 * will not needlessly rebuild the whole document again.
 *
 * ⚠ Another issue remains (a separate defect): after the editor drops `tight`, the **save path**
 * (`pmDocToSections(editor.getJSON())`) writes tight lists back as loose ones. This requires adding a `tight` attr
 * to the editor schema in another batch. See
 * docs/canvas-report-optimization/setcontent-gate-spike-2026-08-08.md §3.
 */

import { Node as PMNode, type Schema } from "@tiptap/pm/model";
import type { JSONContent } from "@tiptap/react";

export function normalizeDocForEditor(
  schema: Schema,
  doc: JSONContent,
): JSONContent {
  try {
    return PMNode.fromJSON(schema, doc as never).toJSON() as JSONContent;
  } catch {
    // On parse failure, return unchanged: comparison can only report a difference → one extra setContent,
    // matching pre-fix behavior, never suppressing an update that should be applied.
    return doc;
  }
}
