import { Schema } from "@tiptap/pm/model";
import { EditorState } from "@tiptap/pm/state";
import { EditorView } from "@tiptap/pm/view";
import { describe, expect, it } from "vitest";

describe("editor clipboard context validation", () => {
  it("rejects invalid wrapper attributes while retaining pasted text", () => {
    const schema = new Schema({
      nodes: {
        doc: { content: "block+" },
        paragraph: {
          group: "block",
          content: "inline*",
          parseDOM: [{ tag: "p" }],
          toDOM: () => ["p", 0],
        },
        blockquote: {
          group: "block",
          content: "block+",
          attrs: { label: { default: "quote", validate: "string" } },
          parseDOM: [{ tag: "blockquote" }],
          toDOM: (node) => ["blockquote", { "data-label": node.attrs.label }, 0],
        },
        text: { group: "inline" },
      },
    });
    const mount = document.createElement("div");
    document.body.append(mount);
    const pastedLabels: unknown[] = [];
    const editor = new EditorView(mount, {
      state: EditorState.create({ schema }),
      transformPasted: (slice) => {
        slice.content.descendants((node) => {
          if (node.type.name === "blockquote") pastedLabels.push(node.attrs.label);
        });
        return slice;
      },
    });
    const paragraph = document.createElement("p");
    paragraph.dataset.pmSlice = `1 1 ${JSON.stringify(["blockquote", { label: ["unexpected", "markup"] }])}`;
    paragraph.textContent = "Pasted text";

    try {
      editor.pasteHTML(paragraph.outerHTML, new Event("paste") as ClipboardEvent);
      expect(editor.state.doc.textContent).toBe("Pasted text");
      expect(pastedLabels.every((label) => typeof label === "string")).toBe(true);
      editor.state.doc.descendants((node) => {
        if (node.type.name === "blockquote") expect(typeof node.attrs.label).toBe("string");
      });
      expect(editor.dom.querySelector("[data-label='unexpected,markup']")).toBeNull();
    } finally {
      editor.destroy();
      mount.remove();
    }
  });
});
