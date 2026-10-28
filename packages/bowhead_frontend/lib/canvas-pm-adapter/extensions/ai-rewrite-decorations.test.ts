/**
 * AI rewrite revision-mode visuals (Feishu 07.06 #15b) — decoration-plugin unit tests.
 */
import { describe, expect, it, vi } from "vitest";
import { EditorState } from "@tiptap/pm/state";
import { DecorationSet } from "@tiptap/pm/view";
import { getSchema } from "@tiptap/core";
import StarterKit from "../starter-kit";

const schema = getSchema([StarterKit]);

import {
  AiRewriteDecoKey,
  aiRewritePlugin,
  setAiRewriteDeco,
} from "./ai-rewrite-decorations";

function mkState(text: string): EditorState {
  const doc = schema.node("doc", null, [schema.node("paragraph", null, [schema.text(text)])]);
  return EditorState.create({ doc, plugins: [aiRewritePlugin()] });
}

function decoClasses(state: EditorState): string[] {
  const set = aiRewritePlugin().props!.decorations!.call(
    { getState: () => AiRewriteDecoKey.getState(state) } as never,
    state,
  ) as DecorationSet | null;
  if (!set) return [];
  return set
    .find()
    .map((d) => (d as unknown as { type: { attrs?: { class?: string } } }).type.attrs?.class ?? "")
    .filter(Boolean);
}

describe("aiRewritePlugin decorations (#15b)", () => {
  it("挂起态 → 旧文划线 + 新文高亮两条 decoration", () => {
    let state = mkState("旧文新文占位内容一二三");
    const tr = state.tr.setMeta(AiRewriteDecoKey, {
      old: { from: 1, to: 3 },
      new: { from: 3, to: 5 },
    });
    state = state.apply(tr);
    expect(AiRewriteDecoKey.getState(state)).toEqual({
      old: { from: 1, to: 3 },
      new: { from: 3, to: 5 },
    });
    expect(decoClasses(state).sort()).toEqual(["ai-rewrite-new", "ai-rewrite-old"]);
  });

  it("setMeta(null) → 清空, 无 decoration", () => {
    let state = mkState("内容内容内容");
    state = state.apply(state.tr.setMeta(AiRewriteDecoKey, { old: { from: 1, to: 3 }, new: null }));
    expect(decoClasses(state)).toEqual(["ai-rewrite-old"]);
    state = state.apply(state.tr.setMeta(AiRewriteDecoKey, null));
    expect(AiRewriteDecoKey.getState(state)).toBeNull();
    expect(decoClasses(state)).toEqual([]);
  });

  it("空区间(from==to)不产生 decoration", () => {
    let state = mkState("内容内容");
    state = state.apply(state.tr.setMeta(AiRewriteDecoKey, { old: { from: 2, to: 2 }, new: null }));
    expect(decoClasses(state)).toEqual([]);
  });

  it("文档编辑后 ranges 随 mapping 跟随(在旧文前插入 → 区间右移)", () => {
    let state = mkState("abcdef");
    state = state.apply(state.tr.setMeta(AiRewriteDecoKey, { old: { from: 3, to: 5 }, new: null }));
    // Insert 2 characters at pos 1 → old range should shift right by 2
    state = state.apply(state.tr.insertText("XY", 1));
    expect(AiRewriteDecoKey.getState(state)).toEqual({ old: { from: 5, to: 7 }, new: null });
  });
});

describe("setAiRewriteDeco helper", () => {
  it("view 为 null/undefined → 静默不抛(测试 mock / 未挂载)", () => {
    expect(() => setAiRewriteDeco(null, { old: { from: 1, to: 2 }, new: null })).not.toThrow();
    expect(() => setAiRewriteDeco(undefined, null)).not.toThrow();
  });

  it("view 存在 → dispatch 带 setMeta 的 tr", () => {
    const dispatch = vi.fn();
    let state = mkState("内容内容");
    const view = { dispatch, state: { tr: state.tr } } as never;
    setAiRewriteDeco(view, { old: { from: 1, to: 3 }, new: null });
    expect(dispatch).toHaveBeenCalledTimes(1);
    const tr = dispatch.mock.calls[0][0];
    expect(tr.getMeta(AiRewriteDecoKey)).toEqual({ old: { from: 1, to: 3 }, new: null });
  });
});


it("keeps boundary edits outside the original range while retaining the preview", () => {
  let state = mkState("Alpha selected omega");
  state = state.apply(state.tr.setMeta(AiRewriteDecoKey, {
    old: { from: 7, to: 15 }, new: null, preview: "Candidate",
  }));
  state = state.apply(state.tr.insertText("!", 15));
  state = state.apply(state.tr.insertText("Prefix ", 7));
  expect(AiRewriteDecoKey.getState(state)).toEqual({
    old: { from: 14, to: 22 }, new: null, preview: "Candidate",
  });
  expect(state.doc.textBetween(14, 22)).toBe("selected");
});
