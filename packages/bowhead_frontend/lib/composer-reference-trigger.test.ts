import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { AllSelection, TextSelection } from "@tiptap/pm/state";
import { redo, undo } from "@tiptap/pm/history";
import {
  editorContentToInputDocument,
  referenceEditorOptions,
} from "./composer-reference-editor";
import {
  inputDocumentFromText,
  inputDocumentTextContent,
  type InputDocument,
} from "./composer-input-document";
import {
  closeReferenceTrigger,
  getReferenceTrigger,
  reopenReferenceTrigger,
} from "./composer-reference-trigger";

const editors: Editor[] = [];
function setup(value: string | InputDocument = "") {
  const onQueryChange = vi.fn();
  const element = document.createElement("div");
  document.body.append(element);
  const editor = new Editor({
    element,
    ...referenceEditorOptions(
      typeof value === "string" ? inputDocumentFromText(value) : value,
      {
        ariaLabel: "输入消息",
        onChange: vi.fn(),
        onRejected: vi.fn(),
        onQueryChange,
      },
    ),
  });
  editors.push(editor);
  editor.view.dispatch(
    editor.state.tr.setSelection(TextSelection.atEnd(editor.state.doc)),
  );
  return {
    editor,
    onQueryChange,
    query: () => getReferenceTrigger(editor.view),
    text: () =>
      inputDocumentTextContent(editorContentToInputDocument(editor.state.doc)),
  };
}
/** Exercise PM's real text-input boundary; browser tests additionally drive native input. */
function type(editor: Editor, text: string) {
  const { from, to } = editor.state.selection;
  let handled = false;
  editor.view.someProp("handleTextInput", (handler) => {
    handled = Boolean(
      handler(editor.view, from, to, text, () =>
        editor.state.tr.insertText(text, from, to),
      ),
    );
    return handled;
  });
  expect(handled).toBe(true);
}
function paste(editor: Editor, text: string) {
  const event = new Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clipboardData", {
    value: { types: ["text/plain"], getData: () => text },
  });
  editor.view.dom.dispatchEvent(event);
}
afterEach(() => {
  for (const editor of editors.splice(0)) {
    editor.destroy();
    if (editor.options.element instanceof Element) editor.options.element.remove();
  }
});

describe("literal and ordinary text boundaries", () => {
  it.each([
    "",
    "分析 ",
    "分析\n",
    "\t",
    "👩‍💻 ",
    "`code` ",
    "[x](https://a/b) ",
    "```js\ncode\n```\n",
    "~~~code\ncode\n~~~\n",
    "\\` literal ",
  ])(
    "opens only a typed halfwidth trigger at ordinary boundary %j",
    (prefix) => {
      for (const trigger of ["/", "@"] as const) {
        const { editor, query, text } = setup(prefix);
        type(editor, trigger);
        expect(query()).toEqual({
          trigger,
          query: "",
          from: prefix.length + 1,
          to: prefix.length + 2,
        });
        expect(text()).toBe(prefix + trigger);
      }
    },
  );
  it.each([
    "a",
    "https:",
    "src",
    "2026",
    "(",
    "\\",
    "`code ",
    "`` code ` ",
    "```\n",
    "~~~\n",
    "[x](url ",
    "[x](url/(nested) ",
    "```\ncode\n``` ",
  ])("keeps a trigger literal inside %j", (prefix) => {
    for (const trigger of ["/", "@"] as const) {
      const { editor, query, text, onQueryChange } = setup(prefix);
      type(editor, trigger);
      expect(query()).toBeNull();
      expect(onQueryChange).not.toHaveBeenCalled();
      expect(text()).toBe(prefix + trigger);
    }
  });
  it.each(["＠", "／", "/query", "@Agent", "pasted /", "a@b.com"])(
    "does not open from multi-character or fullwidth input %j",
    (text) => {
      const { editor, query } = setup();
      type(editor, text);
      expect(query()).toBeNull();
    },
  );
  it("does not promote restored text, programmatic changes, paste, or history", () => {
    const { editor, query, onQueryChange } = setup("@restored ");
    expect(query()).toBeNull();
    editor.view.dispatch(editor.state.tr.insertText("/"));
    expect(query()).toBeNull();
    paste(editor, "\n@pasted");
    expect(query()).toBeNull();
    undo(editor.state, editor.view.dispatch);
    redo(editor.state, editor.view.dispatch);
    expect(query()).toBeNull();
    expect(onQueryChange).not.toHaveBeenCalled();
  });
  it("opens a typed trigger when replacing a selected reference draft", () => {
    const initial: InputDocument = {
      schema_version: 1,
      nodes: [
        {
          type: "reference",
          node_id: "11111111-1111-4111-8111-111111111111",
          label: "分析师",
          parameters: {},
          resource: {
            kind: "agent",
            source_kind: "system",
            id: "analyst",
            version: "r1",
          },
        },
        { type: "text", text: " 旧问题" },
      ],
    };
    const { editor, query, text } = setup(initial);
    editor.view.dispatch(editor.state.tr.setSelection(new AllSelection(editor.state.doc)));
    type(editor, "/");
    expect(text()).toBe("/");
    expect(query()).toEqual({ trigger: "/", query: "", from: 1, to: 2 });
  });
  it("treats reference atoms as boundaries, not searchable label text", () => {
    const initial: InputDocument = {
      schema_version: 1,
      nodes: [
        {
          type: "reference",
          node_id: "11111111-1111-4111-8111-111111111111",
          label: "ends in a space ",
          parameters: {},
          resource: {
            kind: "agent",
            source_kind: "system",
            id: "analyst",
            version: "r1",
          },
        },
      ],
    };
    const { editor, query } = setup(initial);
    type(editor, "/");
    expect(query()).toBeNull();
    type(editor, " ");
    type(editor, "@");
    expect(query()?.trigger).toBe("@");
  });
});

describe("query lifecycle", () => {
  it("keeps multi-word and Chinese queries while filtering literal paths", () => {
    const { editor, query, text } = setup();
    type(editor, "/");
    type(editor, "Profit analysis 盈利");
    expect(query()?.query).toBe("Profit analysis 盈利");
    type(editor, "/");
    expect(query()).toBeNull();
    expect(text()).toBe("/Profit analysis 盈利/");
  });
  it.each(["Users/", "folder\\", "a@b.com", "code`", "next\nline"])(
    "closes a query that becomes literal content: %j",
    (suffix) => {
      const { editor, query, text } = setup();
      type(editor, "/");
      for (const character of suffix) type(editor, character);
      expect(query()).toBeNull();
      expect(text()).toBe("/" + suffix);
    },
  );
  it("preserves the full query when moving within it, and closes outside it", () => {
    const { editor, query, onQueryChange } = setup("task ");
    type(editor, "@");
    type(editor, "analyst");
    const original = query();
    const count = onQueryChange.mock.calls.length;
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.create(editor.state.doc, 8)),
    );
    expect(query()).toEqual(original);
    expect(onQueryChange).toHaveBeenCalledTimes(count);
    type(editor, "X");
    expect(query()?.query).toBe("aXnalyst");
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.create(editor.state.doc, 1)),
    );
    expect(query()).toBeNull();
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.atEnd(editor.state.doc)),
    );
    expect(query()).toBeNull();
  });
  it("deleting query text filters; deleting the trigger closes without changing other text", () => {
    const { editor, query, text } = setup("task ");
    type(editor, "/");
    type(editor, "abc");
    editor.view.dispatch(editor.state.tr.delete(9, 10));
    expect(query()?.query).toBe("ab");
    editor.view.dispatch(editor.state.tr.delete(7, 9));
    expect(query()?.query).toBe("");
    editor.view.dispatch(editor.state.tr.delete(6, 7));
    expect(query()).toBeNull();
    expect(text()).toBe("task ");
  });
  it("programmatic replacement and paste close an existing query without reopening", () => {
    const { editor, query, text } = setup();
    type(editor, "/");
    type(editor, "query");
    paste(editor, " @pasted");
    expect(query()).toBeNull();
    expect(text()).toBe("/query @pasted");
    expect(
      reopenReferenceTrigger(editor.state, editor.view.dispatch, editor.view),
    ).toBe(true);
    expect(query()?.query).toBe("pasted");
    editor.view.dispatch(editor.state.tr.insertText("more"));
    expect(query()).toBeNull();
  });
  it("explicit close and blur preserve the query; redo cannot reopen it", () => {
    const { editor, query, text } = setup();
    type(editor, "/");
    type(editor, "query");
    expect(closeReferenceTrigger(editor.state, editor.view.dispatch)).toBe(
      true,
    );
    expect(query()).toBeNull();
    expect(text()).toBe("/query");
    undo(editor.state, editor.view.dispatch);
    redo(editor.state, editor.view.dispatch);
    expect(query()).toBeNull();
    expect(
      reopenReferenceTrigger(editor.state, editor.view.dispatch, editor.view),
    ).toBe(true);
    editor.view.dom.dispatchEvent(new FocusEvent("blur"));
    expect(query()).toBeNull();
    expect(text()).toBe("/query");
  });
  it.each([
    "a@b.com",
    "src/api",
    "`code /query",
    "[link]( /query",
    "/Users/",
    "(@query)",
  ])("explicit reopening also respects literal boundaries %j", (value) => {
    const { editor, query } = setup(value);
    expect(
      reopenReferenceTrigger(editor.state, editor.view.dispatch, editor.view),
    ).toBe(false);
    expect(query()).toBeNull();
  });
  it("allows 128 Unicode query characters, then closes without truncating input", () => {
    const { editor, query, text } = setup();
    type(editor, "@");
    type(editor, "😀".repeat(128));
    expect(query()?.query).toBe("😀".repeat(128));
    type(editor, "😀");
    expect(query()).toBeNull();
    expect(text()).toBe("@" + "😀".repeat(129));
  });
});

describe("composition provenance", () => {
  it("suspends an existing query while composing and publishes the committed Chinese query", async () => {
    const { editor, query, text, onQueryChange } = setup();
    type(editor, "/");
    editor.view.dom.dispatchEvent(
      new CompositionEvent("compositionstart", { bubbles: true }),
    );
    expect(query()).toBeNull();
    expect(onQueryChange).toHaveBeenLastCalledWith(null);
    editor.view.dispatch(
      editor.state.tr.insertText("经营分析").setMeta("composition", 1),
    );
    expect(query()).toBeNull();
    editor.view.dom.dispatchEvent(
      new CompositionEvent("compositionend", { bubbles: true }),
    );
    await Promise.resolve();
    expect(query()?.query).toBe("经营分析");
    expect(onQueryChange).toHaveBeenLastCalledWith(query());
    expect(text()).toBe("/经营分析");
  });
  it("a composition output that happens to be a slash does not open a menu", async () => {
    const { editor, query, text, onQueryChange } = setup();
    editor.view.dom.dispatchEvent(
      new CompositionEvent("compositionstart", { bubbles: true }),
    );
    editor.view.dispatch(
      editor.state.tr.insertText("/").setMeta("composition", 1),
    );
    editor.view.dom.dispatchEvent(
      new CompositionEvent("compositionend", { bubbles: true }),
    );
    await Promise.resolve();
    expect(query()).toBeNull();
    expect(onQueryChange).not.toHaveBeenCalled();
    expect(text()).toBe("/");
  });
  it("a late composition transaction cannot masquerade as a typed slash", () => {
    const { editor, query, text } = setup();
    const defaultTransaction = () =>
      editor.state.tr.insertText("/").setMeta("composition", 1);
    // Follow the complete PM handler chain. An earlier input-rules handler also returns false.
    const handled = Boolean(
      editor.view.someProp("handleTextInput", (handler) =>
        handler(editor.view, 1, 1, "/", defaultTransaction),
      ),
    );
    expect(handled).toBe(false);
    if (!handled) editor.view.dispatch(defaultTransaction());
    expect(query()).toBeNull();
    expect(text()).toBe("/");
  });
});
