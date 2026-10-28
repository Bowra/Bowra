/**
 * Canvas batch 3: restricted / missing / unsupported `[N]` citations carry `data-source-denied` (outline + strike-through,
 * using the same criterion as chat citations' `is-denied`). Run decorations with a real schema + Editor, locking which
 * citations carry it and cache invalidation.
 */

import { describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";

import { CanvasDocument } from "../schema";
import { getAllCanvasExtensions } from "../extensions";
import { CitationExtension, deniedSourceIndexes } from "../extensions/citation-plugin";
import { canvasToPMDoc } from "../canvas-to-pm";
import type { ReportSection } from "@/lib/types";

const SECTIONS: ReportSection[] = [
  { id: "sec_n", kind: "narrative", markdown: "营收增长 [1]，毛利回落 [2]，费用持平 [3]。" } as unknown as ReportSection,
];

function makeEditor(getDenied: () => ReadonlySet<number>) {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      CanvasDocument,
      StarterKit.configure({ document: false }),
      CitationExtension.configure({ getNumberedSourcesLength: () => 3, getNumberedSourcesDenied: getDenied }),
      ...getAllCanvasExtensions().map((extension) => extension.extend({ addNodeView: () => null })),
    ],
    content: canvasToPMDoc(SECTIONS),
  });
}

const chips = (editor: Editor) =>
  Array.from(editor.view.dom.querySelectorAll<HTMLElement>("[data-citation-kind='numbered'].citation-num--numbered")).map(
    (el) => [el.textContent, el.hasAttribute("data-source-denied")],
  );

describe("deniedSourceIndexes：与对话角标 is-denied 同一判据", () => {
  it("受限 / 失效 / 不支持算受限；可查看、有名无出口（linked_labeled）、无出口兜底（linked_unavailable）与未知态不算", () => {
    const set = deniedSourceIndexes([
      { access_state: "linked_accessible" },
      { access_state: "linked_restricted" },
      { access_state: "linked_labeled" },
      { access_state: "linked_missing" },
      { access_state: "unsupported" },
      {},
      // Canvas projection fallback for no exit, regardless of viewer identity (also used for BI-query sources without artifacts in the owner's own report),
      // Chat citations do not strike through this source, so neither do these.
      { access_state: "linked_unavailable" },
    ]);
    expect([...set].sort()).toEqual([1, 3, 4]);
  });

  it("没有受限条目时恒返回同一个空集合（装饰缓存按 identity 判断，不能每次换新对象）", () => {
    expect(deniedSourceIndexes(undefined)).toBe(deniedSourceIndexes([{ access_state: "linked_accessible" }]));
  });
});

describe("画布 [N] 角标的受限标记", () => {
  it("只有来源受限的 [N] 带 data-source-denied", () => {
    const editor = makeEditor(() => new Set([1]));
    expect(chips(editor)).toEqual([
      ["[1]", false],
      ["[2]", true],
      ["[3]", false],
    ]);
    editor.destroy();
  });

  it("marks no chip when no source is restricted", () => {
    const editor = makeEditor(() => new Set());
    expect(chips(editor).every(([, denied]) => denied === false)).toBe(true);
    editor.destroy();
  });

  it("受限集合换了（来源权限重新判过）即使正文没变也重算装饰", () => {
    let denied: ReadonlySet<number> = new Set();
    const editor = makeEditor(() => denied);
    expect(chips(editor).some(([, d]) => d)).toBe(false);
    denied = new Set([0]);
    editor.view.dispatch(editor.state.tr);
    expect(chips(editor)[0]).toEqual(["[1]", true]);
    editor.destroy();
  });
});
