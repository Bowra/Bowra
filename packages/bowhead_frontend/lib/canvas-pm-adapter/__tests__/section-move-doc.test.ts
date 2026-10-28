/**
 * Apply applySectionMove to a **real PM document**, beyond pure functions — mobile r1 §2.1.
 *
 * Initial device verification reported chain success without document changes or a notice, suggesting the transaction was lost outside pure functions.
 * Build a doc with a real schema + Editor and run it, locking whether the move actually reaches the doc.
 */

import { describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";

import { CanvasDocument } from "../schema";
import { getAllCanvasExtensions } from "../extensions";
import { canvasToPMDoc } from "../canvas-to-pm";
import { applySectionMove } from "../section-move";
import { SectionDeleteGuardExtension } from "../extensions/section-delete-guard";
import { CitationExtension } from "../extensions/citation-plugin";
import { VerifiedEditGuardExtension } from "../extensions/verified-edit-guard";
import { AiRewriteDecorationsExtension } from "../extensions/ai-rewrite-decorations";
import type { ReportSection } from "@/lib/types";

const SECTIONS: ReportSection[] = [
  { id: "sec_a", kind: "h1", text: "标题" } as unknown as ReportSection,
  { id: "sec_b", kind: "narrative", markdown: "第一段正文。" } as unknown as ReportSection,
  { id: "sec_c", kind: "narrative", markdown: "第二段正文。" } as unknown as ReportSection,
];

function makeEditor() {
  return new Editor({
    // Match the real report-view combination (CanvasDocument provides doc, so disable StarterKit document)
    extensions: [
      CanvasDocument,
      StarterKit.configure({ document: false }),
      // Full report-view.tsx plugin combination: moves must pass every guard/decoration
      CitationExtension.configure({ getNumberedSourcesLength: () => 3 }),
      VerifiedEditGuardExtension.configure({
        getNumberedSourcesLength: () => 3,
        confirmEdit: () => Promise.resolve(false),
      }),
      AiRewriteDecorationsExtension,
      SectionDeleteGuardExtension,
      ...getAllCanvasExtensions(),
    ],
    content: canvasToPMDoc(SECTIONS),
  });
}

function topIds(editor: Editor): string[] {
  const out: string[] = [];
  editor.state.doc.forEach((node) => {
    const sid = (node.attrs as { sectionId?: string } | undefined)?.sectionId;
    if (sid) out.push(sid);
  });
  return out;
}

describe("applySectionMove on a real PM doc", () => {
  it("基线: 三个 section 都在顶层", () => {
    const editor = makeEditor();
    expect(topIds(editor)).toEqual(["sec_a", "sec_b", "sec_c"]);
    editor.destroy();
  });

  it("上移: 事务真落到 doc（不被守卫/schema 静默丢弃）", () => {
    const editor = makeEditor();
    const ok = editor.commands.command(({ tr, state, dispatch }) => {
      const moved = applySectionMove(tr, state.doc, "sec_c", "up");
      return dispatch ? moved : moved;
    });
    expect(ok).toBe(true);
    expect(topIds(editor)).toEqual(["sec_a", "sec_c", "sec_b"]);
    editor.destroy();
  });

  it("下移: 同样真落到 doc", () => {
    const editor = makeEditor();
    const ok = editor.commands.command(({ tr, state, dispatch }) => {
      const moved = applySectionMove(tr, state.doc, "sec_a", "down");
      return dispatch ? moved : moved;
    });
    expect(ok).toBe(true);
    expect(topIds(editor)).toEqual(["sec_b", "sec_a", "sec_c"]);
    editor.destroy();
  });

  it("越界返回 false 且 doc 不变（调用方据此提示）", () => {
    const editor = makeEditor();
    const ok = editor.commands.command(({ tr, state }) =>
      applySectionMove(tr, state.doc, "sec_a", "up"),
    );
    expect(ok).toBe(false);
    expect(topIds(editor)).toEqual(["sec_a", "sec_b", "sec_c"]);
    editor.destroy();
  });

  it("report-view 的真实调用姿势 chain().focus().command() 必须真的落 doc", () => {
    const editor = makeEditor();
    const moved = editor
      .chain()
      .focus()
      .command(({ tr, state }) => applySectionMove(tr, state.doc, "sec_c", "up"))
      .run();
    expect(moved).toBe(true);
    expect(topIds(editor)).toEqual(["sec_a", "sec_c", "sec_b"]);
    editor.destroy();
  });

  it("移动后 section id 集合不变——删除守卫不该拦它", () => {
    const editor = makeEditor();
    editor.commands.command(({ tr, state }) => applySectionMove(tr, state.doc, "sec_c", "up"));
    expect([...topIds(editor)].sort()).toEqual(["sec_a", "sec_b", "sec_c"]);
    editor.destroy();
  });
});
