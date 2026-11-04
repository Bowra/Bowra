import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import type { Editor } from "@tiptap/react";
import { NextIntlClientProvider } from "next-intl";

import zhMessages from "@/messages/zh-CN.json";

import { renderIntl } from "@/__tests__/intl-render";
import { resetAiRewriteDrafts } from "@/lib/ai-rewrite-drafts";
import { BlockAIBar, sectionRewriteContext } from "./block-ai-bar";
import { Editor as TiptapEditor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import { makeTextBasedSectionNode, makeAtomicSectionNode } from "@/lib/canvas-pm-adapter/schema";
import { CanvasDocument, canvasToPMDoc, getAllCanvasExtensions } from "@/lib/canvas-pm-adapter";
import { AiRewriteDecorationsExtension } from "@/lib/canvas-pm-adapter/extensions/ai-rewrite-decorations";
import type { ReportSection } from "@/lib/types";
import { blockRewriteScopes } from "@/lib/ai-rewrite-scope";

vi.mock("./use-report-center", () => ({ useReportCenterStyle: () => ({}) }));

const editors: TiptapEditor[] = [];

function fakeEditor(): Editor {
  return makeEditor([
    { id: "sec_a", kind: "narrative", markdown: "PassageA" },
    { id: "sec_b", kind: "narrative", markdown: "PassageB" },
  ]);
}

function makeEditor(sections: ReportSection[]): TiptapEditor {
  const editor = new TiptapEditor({
    extensions: [CanvasDocument, StarterKit.configure({ document: false }), ...getAllCanvasExtensions(), AiRewriteDecorationsExtension],
    content: canvasToPMDoc(sections),
  });
  editors.push(editor);
  return editor;
}

afterEach(() => {
  cleanup();
  editors.splice(0).forEach((editor) => editor.destroy());
  resetAiRewriteDrafts();
});

function renderBar(sectionId: string | null, draftScope = "s_1", onSubmit = vi.fn(() => true)) {
  const editor = fakeEditor();
  const onClose = vi.fn();
  const view = renderIntl(
    <BlockAIBar editor={editor} sectionId={sectionId} onClose={onClose} onSubmit={onSubmit} draftScope={draftScope} />,
  );
  const rerender = (next: string | null, scope = draftScope) => view.rerender(
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      <BlockAIBar editor={editor} sectionId={next} onClose={onClose} onSubmit={onSubmit} draftScope={scope} />
    </NextIntlClientProvider>,
  );
  return { rerender, onSubmit };
}

const input = () => screen.getByRole("textbox") as HTMLInputElement;

describe("整块 AI 改写输入的草稿（第 4a 批）", () => {
  it("keeps the draft when the same section is closed and reopened; sections and sessions keep separate drafts; sending clears it", () => {
    const { rerender, onSubmit } = renderBar("sec_a");
    fireEvent.change(input(), { target: { value: "压到三句话" } });
    rerender(null);
    rerender("sec_a");
    expect(input().value).toBe("压到三句话");
    rerender("sec_b");
    expect(input().value).toBe("");
    rerender("sec_a", "s_2");
    expect(input().value).toBe("");
    rerender("sec_a", "s_1");
    expect(input().value).toBe("压到三句话");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("压到三句话", { from: 1, to: 11 });
    rerender(null);
    rerender("sec_a");
    expect(input().value).toBe("");
  });
});

describe("rewrite target context", () => {
  it("shows the chapter and current prose excerpt while retaining the original submit range", () => {
    const editor = new TiptapEditor({
      extensions: [StarterKit, makeTextBasedSectionNode({ kind: "narrative" }), makeAtomicSectionNode({ kind: "h1", nodeViewComponent: () => null })],
      content: { type: "doc", content: [
        { type: "section_h1", attrs: { sectionId: "title", payload: { text: "Operating review" } } },
        { type: "section_narrative", attrs: { sectionId: "summary" }, content: [
          { type: "heading", attrs: { level: 2 }, content: [{ type: "text", text: "Revenue drivers" }] },
          { type: "paragraph", content: [{ type: "text", text: "Revenue declined after the holiday." }] },
        ] },
      ] },
    });
    try {
      expect(sectionRewriteContext(editor, "summary")).toEqual({ title: "Revenue drivers", excerpt: "Revenue declined after the holiday." });
      const onSubmit = vi.fn(() => true);
      renderIntl(<BlockAIBar editor={editor} sectionId="summary" onClose={vi.fn()} onSubmit={onSubmit} />);
      const dialog = screen.getByRole("dialog");
      expect(dialog.textContent).toContain("Revenue drivers");
      expect(dialog.textContent).toContain("Revenue declined after the holiday.");
      expect(dialog.getAttribute("aria-describedby")).toBeTruthy();
      fireEvent.change(input(), { target: { value: "Explain the decline" } });
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
      expect(onSubmit).toHaveBeenCalledWith("Explain the decline", { from: 2, to: editor.state.doc.content.size - 1 });
    } finally {
      editor.destroy();
    }
  });

  it("uses the preceding heading, bounds the excerpt and ignores another block's content", () => {
    const editor = new TiptapEditor({
      extensions: [StarterKit, makeTextBasedSectionNode({ kind: "narrative" })],
      content: { type: "doc", content: [
        { type: "section_narrative", attrs: { sectionId: "first" }, content: [
          { type: "heading", attrs: { level: 2 }, content: [{ type: "text", text: "Cost drivers" }] },
          { type: "paragraph", content: [{ type: "text", text: "Other block" }] },
        ] },
        { type: "section_narrative", attrs: { sectionId: "second" }, content: [
          { type: "paragraph", content: [{ type: "text", text: "😀".repeat(180) }] },
        ] },
      ] },
    });
    try {
      expect(sectionRewriteContext(editor, "second")).toEqual({ title: "Cost drivers", excerpt: `${"😀".repeat(160)}…` });
      expect(sectionRewriteContext(editor, "missing")).toBeNull();
    } finally {
      editor.destroy();
    }
  });
});

describe("mixed-block rewrite admission", () => {
  const mixed = (): TiptapEditor => makeEditor([
    { id: "mixed", kind: "narrative", markdown: "Before the chart.\n\n[figure:fig]\n\nAfter the chart." },
    { id: "fig", kind: "chart", spec: { kind: "line", title: "Revenue", columns: [], rows: [] } },
  ] as ReportSection[]);

  it("requires an explicit passage, retains rejected instructions and clears only after admission", () => {
    const editor = mixed();
    const original = editor.getJSON();
    const onSubmit = vi.fn((_instruction: string, _range: { from: number; to: number }) => false);
    const onClose = vi.fn();
    renderIntl(<BlockAIBar editor={editor} sectionId="mixed" onClose={onClose} onSubmit={onSubmit} draftScope="cv_mixed" />);
    fireEvent.change(input(), { target: { value: "Expand these facts" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: /After the chart/ }));
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("Expand these facts", expect.objectContaining({ from: expect.any(Number), to: expect.any(Number) }));
    const range = onSubmit.mock.calls[0][1];
    expect(editor.state.doc.textBetween(range.from, range.to)).toBe("After the chart.");
    expect(input().value).toBe("Expand these facts");
    expect(onClose).not.toHaveBeenCalled();
    expect(editor.getJSON()).toEqual(original);
    onSubmit.mockReturnValue(true);
    expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(input().value).toBe("");
  });

  it("maps a chosen passage around unrelated edits, invalidates changed targets and keeps the draft", () => {
    const editor = mixed();
    const onSubmit = vi.fn(() => true);
    renderIntl(<BlockAIBar editor={editor} sectionId="mixed" onClose={vi.fn()} onSubmit={onSubmit} />);
    fireEvent.change(input(), { target: { value: "Explain" } });
    fireEvent.click(screen.getByRole("radio", { name: /After the chart/ }));
    act(() => { editor.view.dispatch(editor.state.tr.insertText("Other ", 2)); });
    expect(screen.getByRole("radio", { name: /After the chart/ }).getAttribute("aria-checked")).toBe("true");
    const scope = blockRewriteScopes(editor, "mixed").scopes.at(-1)!;
    act(() => { editor.view.dispatch(editor.state.tr.insertText("Changed", scope.from, scope.to)); });
    expect(input().value).toBe("Explain");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toContain("目标文字已变化");
    fireEvent.click(screen.getByRole("radio", { name: /Changed/ }));
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("auto-selects a single eligible passage and rejects an over-limit instruction locally", () => {
    const editor = makeEditor([{ id: "single", kind: "narrative", markdown: "One paragraph.\n\n```python\nkeep_code()\n```" }]);
    const onSubmit = vi.fn((_instruction: string, _range: { from: number; to: number }) => true);
    renderIntl(<BlockAIBar editor={editor} sectionId="single" onClose={vi.fn()} onSubmit={onSubmit} />);
    expect(screen.queryByRole("radiogroup")).toBeNull();
    fireEvent.change(input(), { target: { value: "a".repeat(1001) } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input().value).toHaveLength(1001);
    fireEvent.change(input(), { target: { value: "Explain" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    const range = onSubmit.mock.calls[0][1];
    expect(editor.state.doc.textBetween(range.from, range.to)).toBe("One paragraph.");
  });

  it("retains the instruction and refuses submission after the target section is deleted", () => {
    const editor = mixed();
    const onSubmit = vi.fn(() => true);
    renderIntl(<BlockAIBar editor={editor} sectionId="mixed" onClose={vi.fn()} onSubmit={onSubmit} />);
    fireEvent.change(input(), { target: { value: "Keep this instruction" } });
    fireEvent.click(screen.getByRole("radio", { name: /After the chart/ }));
    act(() => { editor.view.dispatch(editor.state.tr.delete(0, editor.state.doc.firstChild!.nodeSize)); });
    expect(input().value).toBe("Keep this instruction");
    expect(screen.getByRole("status").textContent).toBe(zhMessages.ai_edit.scope_missing);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
