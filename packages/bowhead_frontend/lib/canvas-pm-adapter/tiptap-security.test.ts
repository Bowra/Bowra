import { Editor, mergeAttributes } from "@tiptap/core";
import { closeHistory } from "@tiptap/pm/history";
import { DOMSerializer } from "@tiptap/pm/model";
import { describe, expect, it, vi } from "vitest";
import StarterKit from "./starter-kit";

describe("editor security and persistence boundaries", () => {
  it("does not turn JSON prototype keys into executable DOM attributes", () => {
    const input = JSON.parse('{"__proto__":{"onerror":"void(0)","data-inherited":"unsafe"},"class":"reviewed"}');
    const attributes = mergeAttributes(input);
    expect(Object.getPrototypeOf(attributes)).toBe(Object.prototype);
    const element = DOMSerializer.renderSpec(document, ["img", attributes]).dom as HTMLElement;
    expect(element.hasAttribute("onerror")).toBe(false);
    expect(element.hasAttribute("data-inherited")).toBe(false);
    expect(element.className).toBe("reviewed");
  });

  it("loads without saving and preserves edit undo and redo", () => {
    const update = vi.fn();
    const editor = new Editor({
      element: document.createElement("div"),
      extensions: [StarterKit],
      content: "<p>Before</p>",
      onUpdate: update,
    });
    try {
      editor.commands.setContent("<p>Loaded</p>", { emitUpdate: false });
      expect(update).not.toHaveBeenCalled();
      editor.view.dispatch(closeHistory(editor.state.tr));
      editor.commands.insertContentAt(7, " text");
      expect(editor.getText()).toBe("Loaded text");
      expect(update).toHaveBeenCalledOnce();
      expect(editor.commands.undo()).toBe(true);
      expect(editor.getText()).toBe("Loaded");
      expect(editor.commands.redo()).toBe(true);
      expect(editor.getText()).toBe("Loaded text");
    } finally {
      editor.destroy();
    }
  });
});
