/**
 * Asynchronous confirmation for editing verified numbers (design D8): other transactions can change the document while confirmation is open (autosave
 * normalization, SSE full-document replacement, edits elsewhere). After confirmation, map this edit onto the current document before applying; if the number
 * itself changed or mapping fails, abandon and notify the caller rather than silently dropping it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import Document from "@tiptap/extension-document";
import Paragraph from "@tiptap/extension-paragraph";
import Text from "@tiptap/extension-text";
import { Fragment, Slice } from "@tiptap/pm/model";
import { TextSelection } from "@tiptap/pm/state";
import { DecorationSet } from "@tiptap/pm/view";

import { VerifiedEditGuardExtension, VerifiedGuardKey } from "./verified-edit-guard";

const editors: Editor[] = [];
afterEach(() => {
  for (const editor of editors.splice(0)) editor.destroy();
});

function setup() {
  let resolveConfirm: ((ok: boolean) => void) | null = null;
  const onEditDropped = vi.fn();
  const editor = new Editor({
    extensions: [
      Document,
      Paragraph,
      Text,
      VerifiedEditGuardExtension.configure({
        getNumberedSourcesLength: () => 1,
        confirmEdit: () =>
          new Promise<boolean>((resolve) => {
            resolveConfirm = resolve;
          }),
        onEditDropped,
      }),
    ],
    content: "<p>Revenue 962.7 [1] rose</p>",
  });
  editors.push(editor);
  return {
    editor,
    onEditDropped,
    answer: async (ok: boolean) => {
      expect(resolveConfirm).not.toBeNull();
      resolveConfirm!(ok);
      await new Promise((resolve) => setTimeout(resolve, 0));
    },
  };
}

/** Document position after the final digit of "962.7". */
function afterNumber(editor: Editor): number {
  return 1 + editor.state.doc.textContent.indexOf("962.7") + "962.7".length;
}

function backspaceAt(editor: Editor, pos: number) {
  const view = editor.view;
  view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, pos)));
  const event = new KeyboardEvent("keydown", { key: "Backspace" });
  return view.someProp("handleKeyDown", (handler) => handler(view, event));
}

function typeAt(editor: Editor, pos: number, text: string) {
  const view = editor.view;
  view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, pos)));
  return view.someProp("handleTextInput", (handler) => handler(view, pos, pos, text, () => view.state.tr));
}

function downgradedText(editor: Editor): string[] {
  const set = VerifiedGuardKey.getState(editor.state) as DecorationSet;
  return set.find().map((decoration) => editor.state.doc.textBetween(decoration.from, decoration.to));
}

describe("verified 数字编辑的异步确认", () => {
  it("确认后应用编辑、删掉锚、标记待核对", async () => {
    const { editor, answer, onEditDropped } = setup();
    expect(backspaceAt(editor, afterNumber(editor))).toBe(true);
    expect(editor.state.doc.textContent).toBe("Revenue 962.7 [1] rose");

    await answer(true);

    expect(editor.state.doc.textContent).toBe("Revenue 962. rose");
    expect(downgradedText(editor)).toEqual(["962."]);
    expect(onEditDropped).not.toHaveBeenCalled();
  });

  it("取消时不改文档", async () => {
    const { editor, answer, onEditDropped } = setup();
    backspaceAt(editor, afterNumber(editor));
    await answer(false);
    expect(editor.state.doc.textContent).toBe("Revenue 962.7 [1] rose");
    expect(onEditDropped).not.toHaveBeenCalled();
  });

  it("确认框开着时只有非文档事务（失焦等），编辑照常应用", async () => {
    const { editor, answer } = setup();
    backspaceAt(editor, afterNumber(editor));
    editor.view.dispatch(editor.state.tr.setMeta("blur", {}));
    await answer(true);
    expect(editor.state.doc.textContent).toBe("Revenue 962. rose");
  });

  it("确认框开着时数字后面的内容变了，编辑映射后照常应用", async () => {
    const { editor, answer, onEditDropped } = setup();
    backspaceAt(editor, afterNumber(editor));
    editor.view.dispatch(editor.state.tr.insertText("!", editor.state.doc.content.size - 1));

    await answer(true);

    expect(editor.state.doc.textContent).toBe("Revenue 962. rose!");
    expect(downgradedText(editor)).toEqual(["962."]);
    expect(onEditDropped).not.toHaveBeenCalled();
  });

  it("确认框开着时数字前面插入了内容，编辑按映射后的位置应用", async () => {
    const { editor, answer, onEditDropped } = setup();
    typeAt(editor, afterNumber(editor), "8");
    editor.view.dispatch(editor.state.tr.insertText("Q3 ", 1));

    await answer(true);

    expect(editor.state.doc.textContent).toBe("Q3 Revenue 962.78 rose");
    expect(downgradedText(editor)).toEqual(["962.78"]);
    expect(onEditDropped).not.toHaveBeenCalled();
  });

  it("确认框开着时数字本身被改了：放弃这次编辑并通知，不静默丢弃", async () => {
    const { editor, answer, onEditDropped } = setup();
    backspaceAt(editor, afterNumber(editor));
    // For example, SSE replaced the entire paragraph with a new number.
    editor.commands.setContent("<p>Revenue 970.1 [1] rose</p>");

    await answer(true);

    expect(editor.state.doc.textContent).toBe("Revenue 970.1 [1] rose");
    expect(onEditDropped).toHaveBeenCalledTimes(1);
  });

  it("粘贴覆盖数字：确认期间别处有改动，粘贴内容仍落在数字上", async () => {
    const { editor, answer } = setup();
    const view = editor.view;
    const to = afterNumber(editor);
    view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, to - "962.7".length, to)));
    const slice = new Slice(Fragment.from(editor.schema.text("999.9")), 0, 0);
    const handled = view.someProp("handlePaste", (handler) => handler(view, new Event("paste") as ClipboardEvent, slice));
    expect(handled).toBe(true);
    view.dispatch(view.state.tr.insertText("Q3 ", 1));

    await answer(true);

    expect(editor.state.doc.textContent).toBe("Q3 Revenue 999.9 rose");
    expect(downgradedText(editor)).toEqual(["999.9"]);
  });
});
