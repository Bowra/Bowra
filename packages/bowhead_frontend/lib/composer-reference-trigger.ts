import { Extension } from "@tiptap/core";
import {
  Plugin,
  PluginKey,
  TextSelection,
  type Command,
  type EditorState,
  type Transaction,
} from "@tiptap/pm/state";
import { ReplaceStep } from "@tiptap/pm/transform";
import type { EditorView } from "@tiptap/pm/view";

export type ReferenceTriggerQuery = Readonly<{
  trigger: "/" | "@";
  query: string;
  from: number;
  to: number;
}>;
type TriggerAction =
  | { type: "close" }
  | { type: "open"; from: number; trigger: "/" | "@" }
  | { type: "typed"; open?: { from: number; trigger: "/" | "@" } };
const triggerKey = new PluginKey<ReferenceTriggerQuery | null>(
  "composerReferenceTrigger",
);

/** Recognize explicit Markdown code and link targets while users are still typing.
 * Incomplete delimiters remain literal contexts; this does not guess whether prose is code.
 * Only called on opening, never on each ordinary character in a long message.
 */
function isOrdinaryContext(prefix: string): boolean {
  let fence: { character: string; size: number } | null = null;
  let ticks = 0;
  let linkDepth = 0;
  for (const line of prefix.split("\n")) {
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      if (
        marker &&
        marker[1][0] === fence.character &&
        marker[1].length >= fence.size &&
        !marker[2].trim()
      )
        fence = null;
      continue;
    }
    if (marker && !ticks && !linkDepth) {
      fence = { character: marker[1][0], size: marker[1].length };
      continue;
    }
    for (let i = 0; i < line.length; i++) {
      const character = line[i];
      if (character === "`") {
        let length = 1;
        while (line[i + length] === "`") length++;
        if (!ticks) ticks = length;
        else if (ticks === length) ticks = 0;
        i += length - 1;
        continue;
      }
      if (ticks) continue;
      if (character === "\\") {
        i++;
        continue;
      }
      if (linkDepth) {
        if (character === "(") linkDepth++;
        else if (character === ")") linkDepth--;
      } else if (character === "]" && line[i + 1] === "(") {
        linkDepth = 1;
        i++;
      }
    }
  }
  return !fence && !ticks && !linkDepth;
}

function textLeaf(node: { type: { name: string } }): string {
  return node.type.name === "hardBreak" ? "\n" : "\ufffc";
}

function readQuery(
  state: EditorState,
  from: number,
  to: number,
  trigger: "/" | "@",
): ReferenceTriggerQuery | null {
  if (
    !Number.isInteger(from) ||
    !Number.isInteger(to) ||
    from < 1 ||
    to <= from ||
    to > state.doc.content.size ||
    !state.selection.empty
  )
    return null;
  const start = state.doc.resolve(from);
  const end = state.doc.resolve(to);
  if (
    !start.parent.isTextblock ||
    !start.sameParent(end) ||
    state.selection.from <= from ||
    state.selection.from > to
  )
    return null;
  const value = state.doc.textBetween(from, to, "\n", textLeaf);
  if (value[0] !== trigger) return null;
  const query = value.slice(1);
  // A second slash/backslash makes a path; references, newlines and code never join a query.
  if (/[\r\n/@\\`\ufffc]/.test(query) || [...query].length > 128) return null;
  return Object.freeze({ trigger, query, from, to });
}

function openQuery(
  state: EditorState,
  from: number,
  to: number,
  trigger: "/" | "@",
): ReferenceTriggerQuery | null {
  const query = readQuery(state, from, to, trigger);
  if (!query) return null;
  const start = state.doc.resolve(from);
  const before = start.parent.textBetween(
    0,
    start.parentOffset,
    "\n",
    textLeaf,
  );
  if (before && !/\s$/u.test(before)) return null;
  // Include the trigger itself: typing on a fence-closing line invalidates that closing marker.
  if (!isOrdinaryContext(state.doc.textBetween(0, from + 1, "\n", textLeaf)))
    return null;
  return query;
}

function onlyDeletesQuery(
  transaction: Transaction,
  query: ReferenceTriggerQuery,
): boolean {
  return (
    transaction.steps.length > 0 &&
    transaction.steps.every(
      (step) =>
        step instanceof ReplaceStep &&
        step.slice.size === 0 &&
        step.from > query.from &&
        step.to <= query.to,
    )
  );
}

/** Visible query only. The query state is ephemeral and conveys no resource identity or permission. */
export function getReferenceTrigger(
  view: EditorView,
): ReferenceTriggerQuery | null {
  return view.composing ? null : (triggerKey.getState(view.state) ?? null);
}

export const closeReferenceTrigger: Command = (state, dispatch) => {
  if (!triggerKey.getState(state)) return false;
  dispatch?.(
    state.tr
      .setMeta(triggerKey, { type: "close" } satisfies TriggerAction)
      .setMeta("addToHistory", false),
  );
  return true;
};

/** Explicit UI action only: re-open the query ending at the current cursor after paste/escape. */
export const reopenReferenceTrigger: Command = (state, dispatch, view) => {
  if (
    triggerKey.getState(state) === undefined ||
    view?.composing ||
    !state.selection.empty ||
    !state.selection.$from.parent.isTextblock
  )
    return false;
  const { $from } = state.selection;
  const before = $from.parent.textBetween(
    0,
    $from.parentOffset,
    "\n",
    textLeaf,
  );
  const match = /([/@])([^\r\n/@\\`\ufffc]{0,128})$/u.exec(before);
  if (!match) return false;
  const from = $from.start() + match.index;
  const trigger = match[1] as "/" | "@";
  if (!openQuery(state, from, $from.pos, trigger)) return false;
  dispatch?.(
    state.tr
      .setMeta(triggerKey, {
        type: "open",
        from,
        trigger,
      } satisfies TriggerAction)
      .setMeta("addToHistory", false),
  );
  return true;
};

export function referenceTriggerExtension(
  onChange: (query: ReferenceTriggerQuery | null) => void,
) {
  return Extension.create({
    name: "composerReferenceTrigger",
    priority: 1100,
    addProseMirrorPlugins() {
      let composing = false;
      let lastVisible = "";
      const publish = (view: EditorView) => {
        if (view.isDestroyed) return;
        const value = composing ? null : getReferenceTrigger(view);
        const signature = value ? JSON.stringify(value) : "";
        if (signature !== lastVisible) {
          lastVisible = signature;
          onChange(value);
        }
      };
      return [
        new Plugin<ReferenceTriggerQuery | null>({
          key: triggerKey,
          state: {
            init: () => null,
            apply(transaction, previous, _old, state) {
              const action = transaction.getMeta(triggerKey) as
                TriggerAction | undefined;
              if (action?.type === "close") return null;
              if (action?.type === "open")
                return openQuery(
                  state,
                  action.from,
                  state.selection.from,
                  action.trigger,
                );
              if (action?.type === "typed" && action.open)
                return openQuery(
                  state,
                  action.open.from,
                  state.selection.from,
                  action.open.trigger,
                );
              if (!previous) return null;
              if (transaction.docChanged) {
                // Programmatic replacement, paste, drop, restoration and history cannot create a query.
                if (
                  action?.type !== "typed" &&
                  !transaction.getMeta("composition") &&
                  !onlyDeletesQuery(transaction, previous)
                )
                  return null;
                return readQuery(
                  state,
                  transaction.mapping.map(previous.from, -1),
                  transaction.mapping.map(previous.to, 1),
                  previous.trigger,
                );
              }
              return readQuery(
                state,
                previous.from,
                previous.to,
                previous.trigger,
              );
            },
          },
          props: {
            handleTextInput(view, from, to, text, defaultTransaction) {
              const transaction = defaultTransaction();
              if (
                composing ||
                view.composing ||
                transaction.getMeta("composition")
              )
                return false;
              let open: Extract<TriggerAction, { type: "typed" }>["open"];
              if (text === "/" || text === "@") {
                // Cmd+A is an AllSelection (from=0). Replacing it puts the
                // delimiter inside the new first paragraph at position 1.
                const triggerFrom = from === 0 ? 1 : transaction.mapping.map(from, -1);
                if (from !== to) {
                  transaction.setSelection(TextSelection.create(transaction.doc, triggerFrom + 1));
                }
                open = { from: triggerFrom, trigger: text };
              }
              view.dispatch(
                transaction.setMeta(triggerKey, {
                  type: "typed",
                  open,
                } satisfies TriggerAction),
              );
              return true;
            },
            handleDOMEvents: {
              compositionstart(view) {
                composing = true;
                publish(view);
                return false;
              },
              compositionend(view) {
                composing = false;
                // Let PM commit native composition/DOM changes; do not mutate its state in this handler.
                queueMicrotask(() => publish(view));
                return false;
              },
              blur(view) {
                closeReferenceTrigger(view.state, view.dispatch);
                return false;
              },
            },
          },
          view: () => ({ update: publish }),
        }),
      ];
    },
  });
}
