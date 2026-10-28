import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { ModuleKind, ScriptTarget, transpileModule } from "typescript";
import type { Page } from "@playwright/test";
import type { InputDocument } from "../lib/composer-input-document";

/** Serve installed Tiptap/PM ESM and production TS directly, with no app test route,
 * CDN, editor mock or extra bundler dependency. The page is only an editor harness;
 * it does not stand in for Composer storage, authorization or a working send entry.
 */
export async function installReferenceEditorModules(
  page: Page,
  document: InputDocument,
  enableTrigger = false,
) {
  const prefix = "/__composer_reference_editor_modules/";
  const packages = [
    "@tiptap/core",
    "prosemirror-state",
    "prosemirror-view",
    "prosemirror-keymap",
    "prosemirror-model",
    "prosemirror-transform",
    "prosemirror-commands",
    "prosemirror-schema-list",
    "prosemirror-history",
    "orderedmap",
    "rope-sequence",
    "w3c-keyname",
  ];
  const imports: Record<string, string> = {};
  const modules = new Map<string, string>();
  for (const name of packages) {
    const root = resolve(__dirname, "../node_modules", name);
    const manifest = JSON.parse(
      await readFile(resolve(root, "package.json"), "utf8"),
    );
    if (!manifest.module) throw new Error(`Missing ESM entry: ${name}`);
    const url = `${prefix}${encodeURIComponent(name)}`;
    imports[name] = url;
    if (name.startsWith("prosemirror-"))
      imports[`@tiptap/pm/${name.slice("prosemirror-".length)}`] = url;
    modules.set(url, await readFile(resolve(root, manifest.module), "utf8"));
  }
  for (const name of [
    "composer-reference-editor",
    "composer-input-document",
    "composer-reference-trigger",
  ]) {
    const source = await readFile(
      resolve(__dirname, `../lib/${name}.ts`),
      "utf8",
    );
    modules.set(
      `${prefix}${name}`,
      transpileModule(source, {
        compilerOptions: {
          module: ModuleKind.ESNext,
          target: ScriptTarget.ES2022,
        },
      }).outputText,
    );
  }
  await page.route(`**${prefix}*`, (route) => {
    const source = modules.get(new URL(route.request().url()).pathname);
    if (source === undefined)
      throw new Error(`Unexpected editor module: ${route.request().url()}`);
    return route.fulfill({ contentType: "text/javascript", body: source });
  });
  await page.route("**/__composer_reference_editor_test", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Reference editor contract</title>
<style>body{margin:16px;font:16px system-ui} #editor{border:1px solid #aaa;padding:12px} p{margin:0} .composer-reference-preview__reference{border:1px solid #888;border-radius:4px;padding:0 4px;box-decoration-break:clone}</style>
<button id="before">Before editor</button><div id="editor"></div><button id="after">After editor</button><output id="status"></output>
<script type="importmap">${JSON.stringify({ imports })}</script>
<script type="module">
import { Editor } from "@tiptap/core";
import { TextSelection } from "@tiptap/pm/state";
import { referenceEditorOptions, editorContentToInputDocument, captureReferenceInsertion, insertReference, updateReferenceParameters } from "${prefix}composer-reference-editor";
import { closeReferenceTrigger, reopenReferenceTrigger } from "${prefix}composer-reference-trigger";
const original = ${JSON.stringify(document).replace(/</g, "\\u003c")};
const enableTrigger = ${JSON.stringify(enableTrigger)};
const state = { changes: [], rejected: [], menu: false, emptyMenu: false, menuKeys: [], submits: 0, query: null, queryChanges: [] };
const editor = new Editor({ element: document.getElementById("editor"), ...referenceEditorOptions(original, {
  ariaLabel: "输入消息", onChange: doc => state.changes.push(doc), onRejected: reason => state.rejected.push(reason),
  onQueryChange: enableTrigger ? query => { state.query = query; state.queryChanges.push(query); state.menu = query !== null; state.emptyMenu = true; } : undefined,
  isMenuOpen: () => state.menu,
  onMenuKeyDown: key => { state.menuKeys.push(key); if (enableTrigger && key === "Escape") closeReferenceTrigger(editor.state, editor.view.dispatch); return state.emptyMenu ? "move_focus" : undefined; },
  onMenuClose: () => { state.menu = false; if (enableTrigger) closeReferenceTrigger(editor.state, editor.view.dispatch); }, onSubmit: () => state.submits++,
}) });
window.referenceEditor = { editor, state,
  read: () => editorContentToInputDocument(editor.state.doc),
  select: (from, to = from) => editor.view.dispatch(editor.state.tr.setSelection(TextSelection.create(editor.state.doc, from, to))),
  insert: reference => insertReference(reference, captureReferenceInsertion(editor.state))(editor.state, editor.view.dispatch),
  parameters: (id, parameters) => updateReferenceParameters(editor.state.doc, id, parameters)(editor.state, editor.view.dispatch),
  reopen: () => reopenReferenceTrigger(editor.state, editor.view.dispatch, editor.view),
};
document.getElementById("status").textContent = "ready";
</script></html>`,
    }),
  );
}
