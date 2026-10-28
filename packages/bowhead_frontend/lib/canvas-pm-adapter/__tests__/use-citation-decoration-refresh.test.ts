/**
 * Canvas batch 3 dispatches an empty transaction to recompute decorations when the restricted set changes; batch 4b extracted a hook (batch 3 review: the original effect in
 * report-view had no unit test). Real schema + Editor: replacing the set object without changing body text must update citation restriction markers;
 * the same object causes no redispatch; the transaction stays out of undo history and does not alter the document. Also lock wiring: report-view uses it with the same set passed to the plugin.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { renderHook } from "@testing-library/react";
import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import { describe, expect, it, vi } from "vitest";

import type { ReportSection } from "@/lib/types";

import { canvasToPMDoc } from "../canvas-to-pm";
import { getAllCanvasExtensions } from "../extensions";
import { CitationExtension, deniedSourceIndexes } from "../extensions/citation-plugin";
import { CanvasDocument } from "../schema";
import { CITATION_DENIED_REFRESH_META, useCitationDecorationRefresh } from "../use-citation-decoration-refresh";

const SECTIONS: ReportSection[] = [
  { id: "sec_n", kind: "narrative", markdown: "营收增长 [1]，毛利回落 [2]。" } as unknown as ReportSection,
];

function makeEditor(getDenied: () => ReadonlySet<number>) {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      CanvasDocument,
      StarterKit.configure({ document: false }),
      CitationExtension.configure({ getNumberedSourcesLength: () => 2, getNumberedSourcesDenied: getDenied }),
      ...getAllCanvasExtensions().map((extension) => extension.extend({ addNodeView: () => null })),
    ],
    content: canvasToPMDoc(SECTIONS),
  });
}

const deniedFlags = (editor: Editor) =>
  Array.from(editor.view.dom.querySelectorAll<HTMLElement>(".citation-num--numbered")).map((el) =>
    el.hasAttribute("data-source-denied"),
  );

describe("useCitationDecorationRefresh", () => {
  it("集合换了对象、正文没变：装饰跟着重算；事务不改文档、不进撤销栈", () => {
    let denied: ReadonlySet<number> = deniedSourceIndexes(undefined);
    const editor = makeEditor(() => denied);
    const before = editor.state.doc;
    const seen: Array<{ docChanged: boolean; history: unknown; meta: unknown }> = [];
    editor.on("transaction", ({ transaction }) => {
      seen.push({
        docChanged: transaction.docChanged,
        history: transaction.getMeta("addToHistory"),
        meta: transaction.getMeta(CITATION_DENIED_REFRESH_META),
      });
    });
    const { rerender } = renderHook(({ set }) => useCitationDecorationRefresh(editor, set), {
      initialProps: { set: denied },
    });
    expect(deniedFlags(editor)).toEqual([false, false]);
    denied = new Set([1]);
    rerender({ set: denied });
    expect(deniedFlags(editor)).toEqual([false, true]);
    expect(editor.state.doc).toBe(before);
    expect(seen.length).toBeGreaterThan(0);
    expect(seen.every((tr) => !tr.docChanged && tr.history === false && tr.meta === true)).toBe(true);
    editor.destroy();
  });

  it("同一个集合对象不重发；没有编辑器或编辑器已销毁时什么都不做", () => {
    const editor = makeEditor(() => deniedSourceIndexes(undefined));
    const dispatch = vi.spyOn(editor.view, "dispatch");
    const set = deniedSourceIndexes(undefined);
    const { rerender } = renderHook(({ s }) => useCitationDecorationRefresh(editor, s), { initialProps: { s: set } });
    const afterMount = dispatch.mock.calls.length;
    rerender({ s: set });
    rerender({ s: deniedSourceIndexes([{ access_state: "linked_accessible" }]) });
    expect(dispatch.mock.calls.length).toBe(afterMount);
    editor.destroy();
    rerender({ s: new Set([0]) });
    expect(dispatch.mock.calls.length).toBe(afterMount);
    expect(() => renderHook(() => useCitationDecorationRefresh(null, new Set([0])))).not.toThrow();
  });
});

describe("接线合同：report-view 用这个 hook 刷新画布角标的受限标记", () => {
  const source = readFileSync(resolve(__dirname, "../../../components/canvas/report-view.tsx"), "utf8");

  it("传的是交给 citation 插件的同一个受限集合", () => {
    expect(source).toMatch(/useCitationDecorationRefresh\(editor, numberedSourcesDenied\);/);
    expect(source).toMatch(/numberedSourcesDeniedRef\.current = numberedSourcesDenied;/);
    expect(source).toMatch(/getNumberedSourcesDenied: \(\) => numberedSourcesDeniedRef\.current/);
  });
});
