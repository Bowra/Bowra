import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { TableKit } from "@tiptap/extension-table";
import StarterKit from "./canvas-pm-adapter/starter-kit";
import { CanvasDocument, canvasToPMDoc, getAllCanvasExtensions } from "./canvas-pm-adapter";
import type { ReportSection } from "./types";
import { blockRewriteScopes, isProseRange } from "./ai-rewrite-scope";

const editors: Editor[] = [];
afterEach(() => editors.splice(0).forEach((editor) => editor.destroy()));

function makeEditor(markdown: string): Editor {
  const editor = new Editor({
    extensions: [CanvasDocument, StarterKit.configure({ document: false }), TableKit, ...getAllCanvasExtensions()],
    content: canvasToPMDoc([{ id: "text", kind: "narrative", markdown },
      { id: "figure", kind: "chart", spec: { kind: "line", title: "Revenue", columns: [], rows: [] } },
    ] as ReportSection[]),
  });
  editors.push(editor);
  return editor;
}

describe("block rewrite scope admission", () => {
  it("retains whole-block rewriting for pure prose with multiple paragraphs", () => {
    const editor = makeEditor("## Drivers\n\nFirst paragraph.\n\nSecond paragraph.");
    const result = blockRewriteScopes(editor, "text");
    expect(result.whole).toBe(true);
    expect(result.scopes).toHaveLength(1);
    expect(result.scopes[0].text).toContain("First paragraph.\nSecond paragraph.");
  });

  it("offers separate before/after passages while excluding figures, table cells and code", () => {
    const editor = makeEditor("Before [1].\n\n[figure:figure]\n\n| Label | Value |\n| --- | --- |\n| Keep cell | 12 |\n\n```python\nkeep_code()\n```\n\nAfter the figure.");
    const original = editor.getJSON();
    const result = blockRewriteScopes(editor, "text");
    expect(result.whole).toBe(false);
    expect(result.scopes.map((scope) => scope.text)).toEqual(["Before [1].", "After the figure."]);
    expect(result.scopes.every((scope) => isProseRange(editor, scope.from, scope.to))).toBe(true);
    expect(editor.getJSON()).toEqual(original);
  });

  it("admits list and blockquote paragraphs individually without merging structural boundaries", () => {
    const editor = makeEditor("[figure:figure]\n\n- First item\n  - Nested item\n\n> Quoted prose");
    const result = blockRewriteScopes(editor, "text");
    expect(result.scopes.map((scope) => scope.text)).toEqual(["First item", "Nested item", "Quoted prose"]);
    expect(result.scopes.every((scope) => isProseRange(editor, scope.from, scope.to))).toBe(true);
  });

  it("explains absent, empty, protected-only and over-limit targets without truncation", () => {
    expect(blockRewriteScopes(makeEditor(""), "text").reason).toBe("no_prose");
    expect(blockRewriteScopes(makeEditor("[figure:figure]"), "text").reason).toBe("no_prose");
    expect(blockRewriteScopes(makeEditor("Text"), "missing").reason).toBe("scope_missing");
    const tooLong = blockRewriteScopes(makeEditor("a".repeat(12001)), "text");
    expect(tooLong.scopes).toHaveLength(0);
    expect(tooLong.reason).toBe("text_too_long");
  });
});
