import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor, getSchema } from "@tiptap/core";
import { Node as PMNode } from "@tiptap/pm/model";
import { AllSelection, NodeSelection, TextSelection } from "@tiptap/pm/state";
import { redo, undo } from "@tiptap/pm/history";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";
import {
  inputDocumentFromText,
  inputDocumentSemanticProjection,
  parseInputDocument,
  type InputDocument,
  type ReferenceNode,
} from "./composer-input-document";
import {
  captureReferenceInsertion,
  editorContentToInputDocument,
  inputDocumentToEditorContent,
  insertReference,
  referenceEditorOptions,
  updateReferenceParameters,
  type ReferenceEditorHooks,
} from "./composer-reference-editor";

const editors: Editor[] = [];
const agent = (
  id = "analyst",
  nodeId = "11111111-1111-4111-8111-111111111111",
): ReferenceNode => ({
  type: "reference",
  node_id: nodeId,
  label: "经营分析师",
  parameters: {},
  resource: { kind: "agent", source_kind: "system", id, version: "revision-1" },
});
const skill = (id = "dupont", n = 1): ReferenceNode => ({
  ...agent(),
  node_id: `00000000-0000-4000-8000-00000000000${n}`,
  label: "杜邦分析",
  resource: {
    kind: "skill",
    source_kind: "team",
    id,
    version: `sha256:${"a".repeat(64)}`,
  },
  parameters: { year: 2026, optional: null },
});
const documentWith = (...nodes: InputDocument["nodes"]): InputDocument =>
  parseInputDocument({ schema_version: 1, nodes });
function setup(
  document = inputDocumentFromText(""),
  overrides: Partial<ReferenceEditorHooks> = {},
) {
  const hooks = {
    ariaLabel: "输入消息",
    onChange: vi.fn(),
    onRejected: vi.fn(),
    ...overrides,
  };
  const element = window.document.createElement("div");
  window.document.body.append(element);
  const editor = new Editor({
    ...referenceEditorOptions(document, hooks),
    element,
  });
  editors.push(editor);
  return {
    editor,
    hooks,
    read: () => editorContentToInputDocument(editor.state.doc),
  };
}
function key(editor: Editor, name: string, extra: KeyboardEventInit = {}) {
  const event = new KeyboardEvent("keydown", {
    key: name,
    bubbles: true,
    cancelable: true,
    ...extra,
  });
  editor.view.dom.dispatchEvent(event);
  return event;
}
function paste(editor: Editor, text: string, html = "", hasPlain = true) {
  const event = new Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clipboardData", {
    value: {
      types: hasPlain ? ["text/plain", "text/html"] : ["text/html"],
      getData: (type: string) => (type === "text/plain" ? text : html),
    },
  });
  editor.view.dom.dispatchEvent(event);
  return event;
}
afterEach(() => {
  for (const editor of editors.splice(0)) {
    editor.destroy();
    if (editor.options.element instanceof Element) editor.options.element.remove();
  }
  vi.restoreAllMocks();
});

describe("real minimal editor and shared InputDocument fixtures", () => {
  for (const fixture of fixtures.valid)
    it(fixture.name, () => {
      const document = parseInputDocument(fixture.input);
      const { read, hooks } = setup(document);
      const expected = {
        ...document,
        nodes: document.nodes.filter(
          (node) => node.type !== "text" || node.text !== "",
        ),
      };
      expect(read()).toEqual(expected);
      expect(inputDocumentSemanticProjection(read())).toEqual(fixture.semantic);
      expect(hooks.onChange).not.toHaveBeenCalled();
    });
  it("keeps large newline pastes compact and preserves whitespace, emoji and trailing blank lines", () => {
    const text = ` \t👩‍💻é 中文\n${"\n".repeat(10000)}尾\n\n`;
    const { editor, read } = setup(inputDocumentFromText(text));
    expect(editor.state.doc.childCount).toBe(1);
    expect(editor.state.doc.firstChild?.childCount).toBe(1);
    expect(read()).toEqual(inputDocumentFromText(text));
  });
  it("flattens native paragraphs and hard breaks without losing empty lines", () => {
    const { editor, read } = setup();
    const schema = editor.schema;
    const paragraph = (text?: string) =>
      schema.nodes.paragraph.create(null, text ? schema.text(text) : undefined);
    const doc = schema.nodes.doc.create(null, [
      paragraph(),
      schema.nodes.paragraph.create(null, [
        schema.text("a"),
        schema.nodes.hardBreak.create(),
        schema.text("b"),
      ]),
      paragraph(),
    ]);
    editor.view.dispatch(
      editor.state.tr.replaceWith(
        0,
        editor.state.doc.content.size,
        doc.content,
      ),
    );
    expect(read()).toEqual(inputDocumentFromText("\na\nb\n"));
  });
  it("renders hostile labels as text while keeping parameters and identities out of HTML", () => {
    const reference = {
      ...skill(),
      label: '<img src=x onerror="alert(1)">',
      parameters: { secret: "private-material" },
    };
    const { editor } = setup(documentWith(reference));
    expect(editor.view.dom.querySelector("img:not(.ProseMirror-separator)")).toBeNull();
    expect(editor.view.dom.textContent).toBe(`/${reference.label}`);
    expect(editor.getHTML()).not.toContain("private-material");
    expect(editor.getHTML()).not.toContain(reference.node_id);
    expect(editor.getHTML()).not.toContain("sha256:");
    expect(
      editor.view.dom.querySelector("span")?.hasAttribute("reference"),
    ).toBe(false);
  });
  it("rejects malformed initial data and unsupported PM structure", () => {
    expect(() =>
      inputDocumentToEditorContent({
        schema_version: 2,
      } as unknown as InputDocument),
    ).toThrow();
    const { editor } = setup();
    const schema = getSchema(editor.options.extensions);
    const doc = PMNode.fromJSON(schema, {
      type: "doc",
      content: [
        {
          type: "paragraph",
          content: [{ type: "reference", attrs: { reference: null } }],
        },
      ],
    });
    expect(() => editorContentToInputDocument(doc)).toThrow();
  });
});

describe("atomic editing and undo boundaries", () => {
  it.each(["", " ", "  after", "\tafter", "after", "\nafter"])(
    "inserts only a necessary separator and puts subsequent input after it: %j",
    (suffix) => {
      const original = inputDocumentFromText(`@query${suffix}`);
      const { editor, read } = setup(original);
      expect(
        insertReference(agent(), captureReferenceInsertion(editor.state, 1, 7))(
          editor.state,
          editor.view.dispatch,
        ),
      ).toBe(true);
      const separator = /^[ \t]/.test(suffix) ? suffix[0] : " ";
      const remainder = /^[ \t]/.test(suffix) ? suffix.slice(1) : suffix;
      expect(read()).toEqual(
        documentWith(agent(), { type: "text", text: separator + remainder }),
      );
      editor.view.dispatch(editor.state.tr.insertText("继续"));
      expect(read()).toEqual(
        documentWith(agent(), {
          type: "text",
          text: `${separator}继续${remainder}`,
        }),
      );
      undo(editor.state, editor.view.dispatch);
      undo(editor.state, editor.view.dispatch);
      expect(read()).toEqual(original);
    },
  );
  it.each(["agent", "capability"] as const)(
    "replaces the second %s and restores the whole query and original with one undo",
    (kind) => {
      const old =
        kind === "agent"
          ? agent()
          : {
              ...agent(),
              resource: { kind: "capability" as const, id: "old", version: 1 },
            };
      const next =
        kind === "agent"
          ? agent("next", "22222222-2222-4222-8222-222222222222")
          : {
              ...agent("", "22222222-2222-4222-8222-222222222222"),
              resource: { kind: "capability" as const, id: "next", version: 2 },
            };
      const original = documentWith(
        old,
        { type: "text", text: " 分析 @new" },
        skill(),
      );
      const { editor, read } = setup(original);
      const target = captureReferenceInsertion(editor.state, 6, 10);
      expect(
        insertReference(next, target)(editor.state, editor.view.dispatch),
      ).toBe(true);
      expect(read()).toEqual(
        documentWith(
          { type: "text", text: " 分析 " },
          next,
          { type: "text", text: " " },
          skill(),
        ),
      );
      editor.view.dispatch(editor.state.tr.insertText("!"));
      expect(undo(editor.state, editor.view.dispatch)).toBe(true);
      expect(read()).toEqual(
        documentWith(
          { type: "text", text: " 分析 " },
          next,
          { type: "text", text: " " },
          skill(),
        ),
      );
      expect(undo(editor.state, editor.view.dispatch)).toBe(true);
      expect(read()).toEqual(original);
      expect(redo(editor.state, editor.view.dispatch)).toBe(true);
      expect(read()).toEqual(
        documentWith(
          { type: "text", text: " 分析 " },
          next,
          { type: "text", text: " " },
          skill(),
        ),
      );
    },
  );
  it("replaces an Agent inside the selected range without deleting its neighbor", () => {
    const { editor, read } = setup(
      documentWith(
        { type: "text", text: "A" },
        agent(),
        { type: "text", text: "B" },
        skill(),
      ),
    );
    const next = agent("next");
    expect(
      insertReference(next, captureReferenceInsertion(editor.state, 2, 3))(
        editor.state,
        editor.view.dispatch,
      ),
    ).toBe(true);
    expect(read()).toEqual(
      documentWith(
        { type: "text", text: "A" },
        next,
        { type: "text", text: " B" },
        skill(),
      ),
    );
  });
  it("parameter editing is detached, undoable and rejects a stale panel", () => {
    const original = documentWith(skill());
    const { editor, read } = setup(original);
    const parameters = { year: 2025, items: ["利润"] };
    const command = updateReferenceParameters(
      editor.state.doc,
      skill().node_id,
      parameters,
    );
    parameters.items.push("caller mutation");
    expect(command(editor.state, editor.view.dispatch)).toBe(true);
    expect((read().nodes[0] as ReferenceNode).parameters).toEqual({
      year: 2025,
      items: ["利润"],
    });
    expect(command(editor.state, editor.view.dispatch)).toBe(false);
    undo(editor.state, editor.view.dispatch);
    expect(read()).toEqual(original);
  });
  it("rejects duplicate skills, quotas and invalid parameters without changing history or draft", () => {
    const original = documentWith(skill(), skill("s2", 2), skill("s3", 3));
    const { editor, read, hooks } = setup(original);
    const target = captureReferenceInsertion(editor.state);
    expect(
      insertReference(skill("s4", 4), target)(
        editor.state,
        editor.view.dispatch,
      ),
    ).toBe(false);
    expect(
      insertReference({ ...skill(), node_id: skill("x", 4).node_id }, target)(
        editor.state,
        editor.view.dispatch,
      ),
    ).toBe(false);
    expect(
      updateReferenceParameters(editor.state.doc, skill().node_id, {
        bad: NaN,
      })(editor.state, editor.view.dispatch),
    ).toBe(false);
    expect(read()).toEqual(original);
    expect(undo(editor.state, editor.view.dispatch)).toBe(false);
    expect(hooks.onChange).not.toHaveBeenCalled();
  });
  it("does not apply a stale menu target or caller mutations", () => {
    const { editor, read } = setup(inputDocumentFromText("@query"));
    const reference = agent();
    const target = captureReferenceInsertion(editor.state, 1, 7);
    const command = insertReference(reference, target);
    reference.label = "changed";
    target.from = 6;
    expect(command(editor.state, editor.view.dispatch)).toBe(true);
    expect(read()).toEqual(documentWith(agent(), { type: "text", text: " " }));
    expect(command(editor.state, editor.view.dispatch)).toBe(false);
  });
  it("propagates an uncertain dispatch failure instead of returning a retryable rejection", () => {
    const { editor } = setup();
    const command = insertReference(
      agent(),
      captureReferenceInsertion(editor.state),
    );
    expect(() =>
      command(editor.state, () => {
        throw new Error("dispatch failed");
      }),
    ).toThrow("dispatch failed");
  });
  it("rejects a stale menu even when its old offsets still fit the edited document", () => {
    const { editor, read } = setup(inputDocumentFromText("@query remains"));
    const command = insertReference(
      agent(),
      captureReferenceInsertion(editor.state, 1, 7),
    );
    editor.view.dispatch(editor.state.tr.insertText("new ", 1));
    expect(command(editor.state, editor.view.dispatch)).toBe(false);
    expect(read()).toEqual(inputDocumentFromText("new @query remains"));
  });
  it.each(["Backspace", "Delete"])(
    "%s removes an adjacent reference as one atom",
    (keyName) => {
      const original = documentWith({ type: "text", text: "A" }, skill(), {
        type: "text",
        text: "B",
      });
      const { editor, read } = setup(original);
      editor.view.dispatch(
        editor.state.tr.setSelection(
          TextSelection.create(
            editor.state.doc,
            keyName === "Backspace" ? 3 : 2,
          ),
        ),
      );
      expect(key(editor, keyName).defaultPrevented).toBe(true);
      expect(read()).toEqual(inputDocumentFromText("AB"));
      undo(editor.state, editor.view.dispatch);
      expect(read()).toEqual(original);
    },
  );
  it("deletes a node selection and a cross-reference text selection", () => {
    const original = documentWith({ type: "text", text: "A" }, skill(), {
      type: "text",
      text: "B",
    });
    const { editor, read } = setup(original);
    editor.view.dispatch(
      editor.state.tr.setSelection(NodeSelection.create(editor.state.doc, 2)),
    );
    key(editor, "Backspace", { keyCode: 8 });
    expect(read()).toEqual(inputDocumentFromText("AB"));
    undo(editor.state, editor.view.dispatch);
    editor.view.dispatch(
      editor.state.tr.setSelection(
        TextSelection.create(editor.state.doc, 1, 4),
      ),
    );
    key(editor, "Delete", { keyCode: 46 });
    expect(read()).toEqual(documentWith());
    undo(editor.state, editor.view.dispatch);
    expect(read()).toEqual(original);
  });
});

describe("input event boundary", () => {
  it("gives an open menu priority over ghost and submit", () => {
    const onMenuKeyDown = vi.fn(),
      onGhostKeyDown = vi.fn(() => true),
      onSubmit = vi.fn();
    const { editor, read } = setup(inputDocumentFromText("@"), {
      isMenuOpen: () => true,
      onMenuKeyDown,
      onGhostKeyDown,
      onSubmit,
    });
    for (const name of [
      "Enter",
      "Tab",
      "ArrowDown",
      "ArrowUp",
      "Escape",
      "Home",
      "End",
      "PageUp",
      "PageDown",
    ])
      expect(key(editor, name).defaultPrevented).toBe(true);
    expect(onMenuKeyDown.mock.calls.map(([name]) => name)).toEqual([
      "Enter",
      "Tab",
      "ArrowDown",
      "ArrowUp",
      "Escape",
      "Home",
      "End",
      "PageUp",
      "PageDown",
    ]);
    expect(onGhostKeyDown).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(read()).toEqual(inputDocumentFromText("@"));
  });
  it("Shift-Enter closes the menu and inserts a newline without choosing, accepting ghost or sending", () => {
    const onMenuClose = vi.fn(),
      onMenuKeyDown = vi.fn(),
      onGhostKeyDown = vi.fn(() => true),
      onSubmit = vi.fn();
    const { editor, read } = setup(inputDocumentFromText("@query"), {
      isMenuOpen: () => true,
      onMenuClose,
      onMenuKeyDown,
      onGhostKeyDown,
      onSubmit,
    });
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.atEnd(editor.state.doc)),
    );
    expect(key(editor, "Enter", { shiftKey: true }).defaultPrevented).toBe(
      true,
    );
    expect(read()).toEqual(inputDocumentFromText("@query\n"));
    expect(onMenuClose).toHaveBeenCalledOnce();
    expect(onMenuKeyDown).not.toHaveBeenCalled();
    expect(onGhostKeyDown).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
  });
  it.each([false, true])(
    "Tab leaves an empty menu; Shift-Tab always moves focus (shift=%s)",
    (shiftKey) => {
      const onMenuClose = vi.fn(),
        onGhostKeyDown = vi.fn(() => true),
        onSubmit = vi.fn();
      const { editor, read } = setup(inputDocumentFromText("@"), {
        isMenuOpen: () => true,
        onMenuClose,
        onMenuKeyDown: () => (shiftKey ? undefined : "move_focus"),
        onGhostKeyDown,
        onSubmit,
      });
      expect(
        key(editor, "Tab", { shiftKey, keyCode: 9 }).defaultPrevented,
      ).toBe(false);
      expect(onMenuClose).toHaveBeenCalledOnce();
      expect(onGhostKeyDown).not.toHaveBeenCalled();
      expect(onSubmit).not.toHaveBeenCalled();
      expect(read()).toEqual(inputDocumentFromText("@"));
    },
  );
  it("an empty menu cannot forward Enter to sending, and other arrows never reach ghost", () => {
    const onGhostKeyDown = vi.fn(() => true),
      onSubmit = vi.fn();
    const { editor } = setup(inputDocumentFromText("@"), {
      isMenuOpen: () => true,
      onMenuKeyDown: () => "move_focus",
      onGhostKeyDown,
      onSubmit,
    });
    expect(key(editor, "Enter").defaultPrevented).toBe(true);
    key(editor, "ArrowRight");
    expect(onGhostKeyDown).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
  });
  it.each([{ isComposing: true }, { keyCode: 229 }])(
    "lets native IME confirm without selecting or sending %j",
    (event) => {
      const onMenuKeyDown = vi.fn(),
        onSubmit = vi.fn(),
        onGhostKeyDown = vi.fn();
      const { editor, read } = setup(inputDocumentFromText("中"), {
        isMenuOpen: () => true,
        onMenuKeyDown,
        onSubmit,
        onGhostKeyDown,
      });
      expect(key(editor, "Enter", event).defaultPrevented).toBe(false);
      expect(onMenuKeyDown).not.toHaveBeenCalled();
      expect(onSubmit).not.toHaveBeenCalled();
      expect(onGhostKeyDown).not.toHaveBeenCalled();
      expect(read()).toEqual(inputDocumentFromText("中"));
    },
  );
  it("isolates a real composition lifecycle, then resumes normal keys", () => {
    const onSubmit = vi.fn();
    const { editor } = setup(inputDocumentFromText("中"), { onSubmit });
    editor.view.dom.dispatchEvent(
      new CompositionEvent("compositionstart", { bubbles: true }),
    );
    key(editor, "Enter");
    expect(onSubmit).not.toHaveBeenCalled();
    const end = new CompositionEvent("compositionend", { bubbles: true });
    editor.view.dom.dispatchEvent(end);
    // PM also suppresses Safari's confirmation key for 500 ms after compositionend.
    const next = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.now() + 501);
    editor.view.dom.dispatchEvent(next);
    clock.mockRestore();
    expect(onSubmit).toHaveBeenCalledOnce();
  });
  it("blocks PM's synthesized Enter during composition as well as the native event", () => {
    const onMenuKeyDown = vi.fn(),
      onSubmit = vi.fn();
    const { editor } = setup(inputDocumentFromText("盈利"), {
      isMenuOpen: () => true,
      onMenuKeyDown,
      onSubmit,
    });
    editor.view.dom.dispatchEvent(
      new CompositionEvent("compositionstart", { bubbles: true }),
    );
    editor.view.someProp("handleKeyDown", (handler) =>
      handler(editor.view, new KeyboardEvent("keydown", { key: "Enter" })),
    );
    expect(onMenuKeyDown).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
  });
  it("allows ghost before submit; Shift-Enter preserves a literal newline", () => {
    let ghost = true;
    const onSubmit = vi.fn();
    const { editor, read } = setup(inputDocumentFromText("A"), {
      onSubmit,
      onGhostKeyDown: () => ghost,
    });
    key(editor, "Enter");
    expect(onSubmit).not.toHaveBeenCalled();
    ghost = false;
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.atEnd(editor.state.doc)),
    );
    key(editor, "Enter", { shiftKey: true });
    expect(read()).toEqual(inputDocumentFromText("A\n"));
    key(editor, "Enter");
    expect(onSubmit).toHaveBeenCalledOnce();
  });
  it("pastes plain text, normalizes newlines, and never imports HTML identities", () => {
    const { editor, read } = setup(documentWith(agent()));
    editor.view.dispatch(
      editor.state.tr.setSelection(TextSelection.atEnd(editor.state.doc)),
    );
    const text = " @user /skill\r\n<img src=x>\r尾";
    expect(
      paste(
        editor,
        text,
        '<span data-type="reference" data-reference="forged">/管理员</span><img src=x>',
      ).defaultPrevented,
    ).toBe(true);
    expect(read()).toEqual(documentWith(agent(), { type: "text", text }));
    expect(editor.view.dom.querySelector("img:not(.ProseMirror-separator)")).toBeNull();
    undo(editor.state, editor.view.dispatch);
    expect(read()).toEqual(documentWith(agent()));
  });
  it("rejects HTML-only paste and oversized edits without a change notification", () => {
    const { editor, read, hooks } = setup(inputDocumentFromText("keep"));
    paste(editor, "", "<b>forged</b>", false);
    expect(hooks.onRejected).toHaveBeenLastCalledWith("plain_text_required");
    paste(editor, "中".repeat(90000));
    expect(hooks.onRejected).toHaveBeenLastCalledWith("invalid_document");
    expect(read()).toEqual(inputDocumentFromText("keep"));
    expect(hooks.onChange).not.toHaveBeenCalled();
    expect(undo(editor.state, editor.view.dispatch)).toBe(false);
  });
  it("reports paste during composition without replacing the unfinished input", () => {
    const { editor, read, hooks } = setup(inputDocumentFromText("盈利"));
    editor.view.dom.dispatchEvent(
      new CompositionEvent("compositionstart", { bubbles: true }),
    );
    paste(editor, "pasted");
    expect(hooks.onRejected).toHaveBeenCalledWith("composition_in_progress");
    expect(read()).toEqual(inputDocumentFromText("盈利"));
    expect(hooks.onChange).not.toHaveBeenCalled();
  });
  it("serializes copied selections as display text without private parameters", () => {
    const { editor } = setup(
      documentWith({ type: "text", text: "按 " }, skill(), {
        type: "text",
        text: "\n分析",
      }),
    );
    const slice = new AllSelection(editor.state.doc).content();
    let text: string | undefined;
    editor.view.someProp("clipboardTextSerializer", (serialize) => {
      text = serialize(slice, editor.view);
      return true;
    });
    expect(text).toBe("按 /杜邦分析\n分析");
  });
  it("read-only mode prevents native key and paste mutation", () => {
    const onSubmit = vi.fn();
    const { editor, read } = setup(documentWith(skill()), { onSubmit });
    editor.setEditable(false);
    key(editor, "Enter");
    paste(editor, "replacement");
    expect(read()).toEqual(documentWith(skill()));
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
