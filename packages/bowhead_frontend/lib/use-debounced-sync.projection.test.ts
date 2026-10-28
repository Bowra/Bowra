/**
 * Untouched report sections must never be written back.
 *
 * Real adapter, real Tiptap editor, mocked PATCH. Stored Markdown such as `  \n` hard breaks, a heading
 * followed by a hard break, or a chapter wrapper without `body` does not survive canonical → PM → canonical
 * byte for byte. Diffing the editor output against the stored text therefore marked those sections
 * changed, and the first edit anywhere (a design layer, one character elsewhere) PATCHed the
 * normalization over content nobody had edited (editable-design e2e: 13 untouched sections rewritten).
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CanvasDocument, canvasToPMDoc, getAllCanvasExtensions, pmDocToSections } from "@/lib/canvas-pm-adapter";
import { patchCanvasSection } from "@/lib/canvas-pm-adapter/canvas-patch";
import { projectSectionsThroughEditor } from "@/lib/canvas-pm-adapter/projected-baseline";
import type { ReportSection } from "@/lib/types";

import { useDebouncedSync } from "./use-debounced-sync";

vi.mock("@/lib/canvas-pm-adapter/canvas-patch", () => ({
  patchCanvasSection: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  reorderCanvas: vi.fn(),
}));

/** Stored shapes taken from the sections the editable-design e2e saw rewritten. */
const STORED: ReportSection[] = [
  { id: "sec_hard_breaks", kind: "narrative", markdown: "**Next step**:  \nAttribute the increase:  \n→ Find the drivers.\n\nShould I run this?", rev: 3 },
  { id: "sec_wrapper", kind: "section", label: "", h2: "Key findings", rev: 4 } as ReportSection,
  { id: "sec_heading_break", kind: "narrative", markdown: "#### 1. Why visits fell  \n→ **80% from stores**:", rev: 5 },
  { id: "sec_edit_me", kind: "narrative", markdown: "Revenue grew.", rev: 6 },
];

function newEditor(sections: ReportSection[]) {
  return new Editor({
    extensions: [CanvasDocument, StarterKit.configure({ document: false }), ...getAllCanvasExtensions()],
    content: canvasToPMDoc(sections) as never,
  });
}

/** Replace the text of one narrative section the way typing would. */
function typeInto(editor: Editor, sectionId: string, text: string) {
  const doc = editor.getJSON();
  const content = (doc.content ?? []).map((node) =>
    node.attrs?.sectionId === sectionId
      ? { ...node, content: [{ type: "paragraph", content: [{ type: "text", text }] }] }
      : node,
  );
  editor.commands.setContent({ ...doc, content } as never, { emitUpdate: false });
}

beforeEach(() => {
  vi.mocked(patchCanvasSection).mockReset();
  vi.mocked(patchCanvasSection).mockImplementation(async () => ({ ok: true, rev: 99 }));
});

describe("diffing against the stored text (the old reference)", () => {
  it("flags every one of these untouched shapes, which is why the reference had to change", () => {
    // Positive control for the cases below: without the projection the same untouched doc reports
    // changes, so "nothing changed" below is the projection's doing, not an always-stable fixture.
    const editor = newEditor(STORED);
    const changed = pmDocToSections(editor.getJSON(), STORED).filter((p) => p.changed).map((p) => p.sectionId);
    expect(changed).toEqual(["sec_hard_breaks", "sec_wrapper", "sec_heading_break"]);
    editor.destroy();
  });
});

describe("projected baseline", () => {
  it("reports no change for an untouched report", () => {
    const editor = newEditor(STORED);
    const projected = projectSectionsThroughEditor(STORED, editor.schema);
    expect(pmDocToSections(editor.getJSON(), STORED, projected).filter((p) => p.changed)).toEqual([]);
    editor.destroy();
  });

  it("is stable: projecting the projection changes nothing", () => {
    const editor = newEditor(STORED);
    const once = projectSectionsThroughEditor(STORED, editor.schema);
    const onceList = STORED.map((s) => ({ ...once.get(s.id)!, rev: s.rev }) as ReportSection);
    const twice = projectSectionsThroughEditor(onceList, editor.schema);
    for (const section of STORED) {
      expect(twice.get(section.id)).toEqual(once.get(section.id));
    }
    editor.destroy();
  });

  it("an edit yields exactly that section, with only the field the user changed", () => {
    const editor = newEditor(STORED);
    typeInto(editor, "sec_edit_me", "Revenue grew 8%.");
    const projected = projectSectionsThroughEditor(STORED, editor.schema);
    const changed = pmDocToSections(editor.getJSON(), STORED, projected).filter((p) => p.changed);
    expect(changed).toEqual([
      expect.objectContaining({ sectionId: "sec_edit_me", patch: { markdown: "Revenue grew 8%." } }),
    ]);
    editor.destroy();
  });
});

describe("useDebouncedSync writes only what the user edited", () => {
  it("one edit PATCHes one section; untouched sections keep their stored text", async () => {
    const editor = newEditor(STORED);
    typeInto(editor, "sec_edit_me", "Revenue grew 8%.");
    const hook = renderHook(() =>
      useDebouncedSync({ editor, baseline: STORED, canvasId: "cv_projection", debounceMs: 60_000 }),
    );

    let result;
    await act(async () => { result = await hook.result.current.flushNow(); });

    expect(result).toEqual({ ok: true, saved_revisions: { sec_edit_me: 99 } });
    expect(vi.mocked(patchCanvasSection).mock.calls).toEqual([
      ["cv_projection", "sec_edit_me", { markdown: "Revenue grew 8%." }, 6],
    ]);
    hook.unmount();
    editor.destroy();
  });

  it("an untouched report flushes without any write", async () => {
    const editor = newEditor(STORED);
    const hook = renderHook(() =>
      useDebouncedSync({ editor, baseline: STORED, canvasId: "cv_projection", debounceMs: 60_000 }),
    );

    let result;
    await act(async () => { result = await hook.result.current.flushNow(); });

    expect(result).toEqual({ ok: true, saved_revisions: {} });
    expect(patchCanvasSection).not.toHaveBeenCalled();
    hook.unmount();
    editor.destroy();
  });
});
