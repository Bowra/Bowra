import {
  Extension,
  Node,
  type EditorOptions,
  type JSONContent,
} from "@tiptap/core";
import { closeHistory, history, redo, undo } from "@tiptap/pm/history";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import {
  Plugin,
  PluginKey,
  TextSelection,
  type Command,
  type EditorState,
  type Transaction,
} from "@tiptap/pm/state";
import {
  parseInputDocument,
  type InputDocument,
  type InputNode,
  type ReferenceNode,
} from "./composer-input-document";
import {
  referenceTriggerExtension,
  type ReferenceTriggerQuery,
} from "./composer-reference-trigger";

const referenceEdit = new PluginKey("composerReferenceEdit");

function referenceText(reference: ReferenceNode): string {
  return `${reference.resource.kind === "agent" ? "@" : "/"}${reference.label}`;
}

/** Editor state is a projection, never a replacement for an untouched recovery envelope.
 * Empty text placeholders have no PM equivalent. Mounting must not persist this projection.
 * Keep newlines in a single text node to avoid expanding a large paste into thousands of nodes.
 */
export function inputDocumentToEditorContent(raw: InputDocument): JSONContent {
  const document = parseInputDocument(raw);
  return {
    type: "doc",
    content: [
      {
        type: "paragraph",
        content: document.nodes.flatMap((node): JSONContent[] =>
          node.type === "reference"
            ? [{ type: "reference", attrs: { reference: node } }]
            : node.text
              ? [{ type: "text", text: node.text }]
              : [],
        ),
      },
    ],
  };
}

/** Reject unsupported editor structure instead of silently reducing it to display text. */
export function editorContentToInputDocument(
  document: ProseMirrorNode,
): InputDocument {
  document.check();
  if (document.type.name !== "doc" || !document.childCount)
    throw new Error("reference_editor_invalid_document");
  const nodes: InputNode[] = [];
  const appendText = (text: string) => {
    const last = nodes.at(-1);
    if (last?.type === "text") last.text += text;
    else if (text) nodes.push({ type: "text", text });
  };
  document.forEach((paragraph, _offset, index) => {
    if (paragraph.type.name !== "paragraph" || paragraph.marks.length)
      throw new Error("reference_editor_invalid_document");
    if (index) appendText("\n");
    paragraph.forEach((node) => {
      if (node.marks.length)
        throw new Error("reference_editor_invalid_document");
      if (node.isText) appendText(node.text!);
      else if (node.type.name === "hardBreak") appendText("\n");
      else if (node.type.name === "reference") nodes.push(node.attrs.reference);
      else throw new Error("reference_editor_invalid_document");
    });
  });
  return parseInputDocument({ schema_version: 1, nodes });
}

export type ReferenceInsertionTarget = {
  document: ProseMirrorNode;
  from: number;
  to: number;
};

/** Capture the query/selection before an asynchronous menu or parameter panel opens. */
export function captureReferenceInsertion(
  state: EditorState,
  from = state.selection.from,
  to = state.selection.to,
): ReferenceInsertionTarget {
  if (
    !Number.isInteger(from) ||
    !Number.isInteger(to) ||
    from < 0 ||
    to < from ||
    to > state.doc.content.size
  )
    throw new Error("reference_editor_invalid_target");
  const start = state.doc.resolve(from);
  if (!start.parent.isTextblock || !start.sameParent(state.doc.resolve(to)))
    throw new Error("reference_editor_invalid_target");
  return { document: state.doc, from, to };
}

function finishReferenceEdit(
  transaction: Transaction,
  dispatch?: (tr: Transaction) => void,
): boolean {
  try {
    editorContentToInputDocument(transaction.doc);
  } catch {
    return false;
  }
  dispatch?.(
    closeHistory(transaction).setMeta(referenceEdit, true).scrollIntoView(),
  );
  return true;
}

/** Local editing only; a ResourceRef does not confer catalog availability or execution permission. */
export function insertReference(
  reference: ReferenceNode,
  target: ReferenceInsertionTarget,
): Command {
  const { document: targetDocument, from: targetFrom, to: targetTo } = target;
  // Validate and detach before a caller can mutate a pending menu choice.
  let node: ReferenceNode;
  try {
    node = parseInputDocument({ schema_version: 1, nodes: [reference] })
      .nodes[0] as ReferenceNode;
    if (node.type !== "reference") return () => false;
  } catch {
    return () => false;
  }
  return (state, dispatch) => {
    if (state.doc !== targetDocument) return false;
    let transaction: Transaction;
    try {
      captureReferenceInsertion(state, targetFrom, targetTo);
      transaction = state.tr;
      if (
        node.resource.kind === "agent" ||
        node.resource.kind === "capability"
      ) {
        const positions: number[] = [];
        state.doc.descendants((existing, position) => {
          if (
            existing.type.name === "reference" &&
            existing.attrs.reference.resource.kind === node.resource.kind
          )
            positions.push(position);
        });
        for (const position of positions.reverse())
          transaction.delete(position, position + 1);
      }
      const from = transaction.mapping.map(targetFrom, -1);
      const to = transaction.mapping.map(targetTo, 1);
      transaction.replaceWith(
        from,
        to,
        state.schema.nodes.reference.create({ reference: node }),
      );
      const next = transaction.doc.resolve(from + 1).nodeAfter;
      const hasSeparator = next?.isText && /^[ \t]/.test(next.text!);
      if (!hasSeparator) transaction.insertText(" ", from + 1);
      transaction.setSelection(TextSelection.create(transaction.doc, from + 2));
    } catch {
      return false;
    }
    // A dispatch failure may occur after application; never report it as a safe rejected edit.
    return finishReferenceEdit(transaction, dispatch);
  };
}

/** The panel applies to the exact captured document, not a node from a different draft. */
export function updateReferenceParameters(
  document: ProseMirrorNode,
  nodeId: string,
  parameters: ReferenceNode["parameters"],
): Command {
  let replacement: ReferenceNode | undefined;
  let position = -1;
  document.descendants((node, offset) => {
    if (
      node.type.name === "reference" &&
      node.attrs.reference.node_id === nodeId
    ) {
      position = offset;
      try {
        replacement = parseInputDocument({
          schema_version: 1,
          nodes: [{ ...node.attrs.reference, parameters }],
        }).nodes[0] as ReferenceNode;
      } catch {
        /* Rejected below without changing the draft. */
      }
    }
  });
  return (state, dispatch) => {
    if (state.doc !== document || !replacement || position < 0) return false;
    return finishReferenceEdit(
      state.tr.setNodeMarkup(position, undefined, { reference: replacement }),
      dispatch,
    );
  };
}

export type ReferenceEditorHooks = {
  ariaLabel: string;
  onChange: (document: InputDocument) => void;
  onQueryChange?: (query: ReferenceTriggerQuery | null) => void;
  onRejected: (
    reason:
      "invalid_document" | "plain_text_required" | "composition_in_progress",
  ) => void;
  isMenuOpen?: () => boolean;
  /** Only Tab may return move_focus (no selectable item). Enter always stays consumed. */
  onMenuKeyDown?: (key: string) => "move_focus" | void;
  onMenuClose?: () => void;
  onGhostKeyDown?: (event: KeyboardEvent) => boolean;
  onSubmit?: () => void;
};

const Doc = Node.create({ name: "doc", topNode: true, content: "paragraph+" });
const Paragraph = Node.create({
  name: "paragraph",
  group: "block",
  content: "inline*",
  marks: "",
  whitespace: "pre",
  parseHTML: () => [{ tag: "p" }],
  renderHTML: () => ["p", 0],
});
const Text = Node.create({ name: "text", group: "inline" });
const HardBreak = Node.create({
  name: "hardBreak",
  inline: true,
  group: "inline",
  selectable: false,
  parseHTML: () => [{ tag: "br" }],
  renderHTML: () => ["br"],
  renderText: () => "\n",
});
const Reference = Node.create({
  name: "reference",
  inline: true,
  group: "inline",
  atom: true,
  selectable: true,
  draggable: false,
  addAttributes: () => ({ reference: { default: null, rendered: false } }),
  // No parseHTML rule: pasted markup can never manufacture a resource identity.
  renderHTML: ({ node }) => [
    "span",
    {
      class: "composer-reference-preview__reference",
      contenteditable: "false",
    },
    referenceText(node.attrs.reference),
  ],
  renderText: ({ node }) => referenceText(node.attrs.reference),
});

/** Minimal Tiptap options, intentionally independent of HTTP, storage and authorization.
 * Callers must remount on persistence-scope changes and retain pending snapshots separately.
 */
export function referenceEditorOptions(
  document: InputDocument,
  hooks: ReferenceEditorHooks,
): Partial<EditorOptions> {
  const behavior = Extension.create({
    name: "composerReferenceBehavior",
    priority: 1000,
    addProseMirrorPlugins() {
      return [
        history(),
        new Plugin({
          key: referenceEdit,
          filterTransaction(transaction) {
            if (!transaction.docChanged) return true;
            try {
              editorContentToInputDocument(transaction.doc);
              return true;
            } catch {
              hooks.onRejected("invalid_document");
              return false;
            }
          },
          appendTransaction(transactions, _old, state) {
            // Isolate both sides: one undo restores replacement/parameters, later typing stays separate.
            return transactions.some((tr) => tr.getMeta(referenceEdit))
              ? closeHistory(state.tr)
              : null;
          },
          props: {
            handleDOMEvents: {
              // Returning true here skips PM keymaps WITHOUT preventing native IME confirmation.
              keydown: (view, event) =>
                view.composing || event.isComposing || event.keyCode === 229,
              paste(view, event) {
                event.preventDefault();
                if (!view.editable) return true;
                if (view.composing) {
                  hooks.onRejected("composition_in_progress");
                  return true;
                }
                if (!event.clipboardData?.types.includes("text/plain")) {
                  hooks.onRejected("plain_text_required");
                  return true;
                }
                const text = event.clipboardData
                  .getData("text/plain")
                  .replace(/\r\n?/g, "\n");
                view.dispatch(
                  closeHistory(view.state.tr.insertText(text)).setMeta(
                    referenceEdit,
                    true,
                  ),
                );
                return true;
              },
              // Drag payloads, including internal atoms, are not an authorized insertion path.
              drop: (_view, event) => {
                event.preventDefault();
                return true;
              },
            },
            clipboardTextSerializer(slice) {
              return slice.content.textBetween(
                0,
                slice.content.size,
                "\n",
                (node) =>
                  node.type.name === "reference"
                    ? referenceText(node.attrs.reference)
                    : "\n",
              );
            },
            handleKeyDown(view, event) {
              // PM also synthesizes keys from DOM changes (including mobile Enter).
              // Those bypass handleDOMEvents and can arrive during native composition.
              if (view.composing || event.isComposing || event.keyCode === 229)
                return true;
              // A held selection key must not become a send after the popup closes.
              if (event.repeat && !event.shiftKey && (event.key === "Enter" || event.key === "Tab")) return true;
              const menuOpen = hooks.isMenuOpen?.() ?? false;
              if (menuOpen && event.key === "Enter" && event.shiftKey) {
                hooks.onMenuClose?.();
                view.dispatch(view.state.tr.insertText("\n"));
                return true;
              }
              if (menuOpen && event.key === "Tab" && event.shiftKey) {
                hooks.onMenuClose?.();
                return false;
              }
              if (
                menuOpen &&
                [
                  "Enter",
                  "Tab",
                  "ArrowUp",
                  "ArrowDown",
                  "Escape",
                  "Home",
                  "End",
                  "PageUp",
                  "PageDown",
                ].includes(event.key)
              ) {
                const action = hooks.onMenuKeyDown?.(event.key);
                if (event.key === "Tab" && action === "move_focus") {
                  hooks.onMenuClose?.();
                  return false;
                }
                return true;
              }
              if (!menuOpen && hooks.onGhostKeyDown?.(event)) return true;
              if ((event.metaKey || event.ctrlKey) && !event.altKey) {
                if (event.key.toLowerCase() === "z")
                  return (event.shiftKey ? redo : undo)(
                    view.state,
                    view.dispatch,
                  );
                if (event.key.toLowerCase() === "y")
                  return redo(view.state, view.dispatch);
              }
              if (event.key === "Enter") {
                if (
                  !event.shiftKey &&
                  !event.metaKey &&
                  !event.ctrlKey &&
                  !event.altKey &&
                  hooks.onSubmit
                )
                  hooks.onSubmit();
                else view.dispatch(view.state.tr.insertText("\n"));
                return true;
              }
              const { selection } = view.state;
              if (
                selection.empty &&
                !event.metaKey &&
                !event.ctrlKey &&
                !event.altKey &&
                ["Backspace", "Delete"].includes(event.key)
              ) {
                const previous = event.key === "Backspace";
                const adjacent = previous
                  ? selection.$from.nodeBefore
                  : selection.$from.nodeAfter;
                if (adjacent?.type.name === "reference") {
                  const from = selection.from - (previous ? 1 : 0);
                  return finishReferenceEdit(
                    view.state.tr.delete(from, from + 1),
                    view.dispatch,
                  );
                }
              }
              return false;
            },
          },
        }),
      ];
    },
  });
  return {
    content: inputDocumentToEditorContent(document),
    extensions: [
      Doc,
      Paragraph,
      Text,
      HardBreak,
      Reference,
      behavior,
      ...(hooks.onQueryChange
        ? [referenceTriggerExtension(hooks.onQueryChange)]
        : []),
    ],
    enableInputRules: false,
    enablePasteRules: false,
    editorProps: {
      attributes: {
        role: "textbox",
        "aria-label": hooks.ariaLabel,
        "aria-multiline": "true",
        style: "white-space: pre-wrap; overflow-wrap: anywhere;",
      },
    },
    onUpdate: ({ editor, transaction }) => {
      // Tiptap emits update even when a filter rejects the transaction.
      if (!transaction.before.eq(editor.state.doc))
        hooks.onChange(editorContentToInputDocument(editor.state.doc));
    },
  };
}
