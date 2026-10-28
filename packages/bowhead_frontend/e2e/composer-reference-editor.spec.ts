import { expect, test } from "@playwright/test";
import type { Editor } from "@tiptap/core";
import type {
  InputDocument,
  ReferenceNode,
} from "../lib/composer-input-document";
import { installReferenceEditorModules } from "./_composer-reference-editor-modules";
import type { ReferenceTriggerQuery } from "../lib/composer-reference-trigger";

declare global {
  interface Window {
    referenceEditor: {
      editor: Editor;
      state: {
        changes: InputDocument[];
        rejected: string[];
        menu: boolean;
        emptyMenu: boolean;
        menuKeys: string[];
        submits: number;
        query: ReferenceTriggerQuery | null;
        queryChanges: (ReferenceTriggerQuery | null)[];
      };
      read: () => InputDocument;
      reopen: () => boolean;
      select: (from: number, to?: number) => void;
      insert: (reference: ReferenceNode) => boolean;
      parameters: (
        id: string,
        parameters: ReferenceNode["parameters"],
      ) => boolean;
    };
  }
}
const skill: ReferenceNode = {
  type: "reference",
  node_id: "11111111-1111-4111-8111-111111111111",
  label: "杜邦分析",
  parameters: { year: 2026, secret: "private-material" },
  resource: {
    kind: "skill",
    source_kind: "team",
    id: "dupont",
    version: `sha256:${"a".repeat(64)}`,
  },
};
const initial: InputDocument = {
  schema_version: 1,
  nodes: [
    { type: "text", text: "按 " },
    skill,
    { type: "text", text: " 分析\n" },
  ],
};

test("menu newline and empty-menu Tab preserve input and move real browser focus", async ({
  page,
}) => {
  await installReferenceEditorModules(page, {
    schema_version: 1,
    nodes: [{ type: "text", text: "@query" }],
  });
  await page.goto("/__composer_reference_editor_test");
  await expect(page.locator("#status")).toHaveText("ready");
  const editor = page.getByRole("textbox", { name: "输入消息" });
  await editor.focus();
  await page.evaluate(() => {
    window.referenceEditor.select(7);
    window.referenceEditor.state.menu = true;
  });
  await page.keyboard.press("Shift+Enter");
  expect(await page.evaluate(() => window.referenceEditor.state.menu)).toBe(
    false,
  );
  expect(
    await page.evaluate(() => window.referenceEditor.state.menuKeys),
  ).toEqual([]);
  const input = {
    schema_version: 1,
    nodes: [{ type: "text", text: "@query\n" }],
  };
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.read()))
    .toEqual(input);
  await page.evaluate(() => {
    window.referenceEditor.state.menu = true;
    window.referenceEditor.state.emptyMenu = true;
  });
  await page.keyboard.press("Enter");
  expect(await page.evaluate(() => window.referenceEditor.state.submits)).toBe(
    0,
  );
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "After editor" }),
  ).toBeFocused();
  expect(await page.evaluate(() => window.referenceEditor.state.menu)).toBe(
    false,
  );
  await editor.focus();
  await page.evaluate(() => {
    window.referenceEditor.state.menu = true;
    window.referenceEditor.state.emptyMenu = false;
  });
  await page.keyboard.press("Shift+Tab");
  await expect(
    page.getByRole("button", { name: "Before editor" }),
  ).toBeFocused();
  expect(await page.evaluate(() => window.referenceEditor.state.menu)).toBe(
    false,
  );
  expect(await page.evaluate(() => window.referenceEditor.read())).toEqual(
    input,
  );
});

test("choosing a reference leaves a separator for typing and one undo restores the original query", async ({
  page,
}) => {
  const original: InputDocument = {
    schema_version: 1,
    nodes: [{ type: "text", text: "/dupont" }],
  };
  await installReferenceEditorModules(page, original);
  await page.goto("/__composer_reference_editor_test");
  await expect(page.locator("#status")).toHaveText("ready");
  await page.getByRole("textbox", { name: "输入消息" }).focus();
  expect(
    await page.evaluate((referenceJson) => {
      window.referenceEditor.select(1, 8);
      return window.referenceEditor.insert(JSON.parse(referenceJson));
    }, JSON.stringify(skill)),
  ).toBe(true);
  await page.keyboard.insertText("继续");
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.read()))
    .toEqual({
      schema_version: 1,
      nodes: [skill, { type: "text", text: " 继续" }],
    });
  await page.keyboard.press("ControlOrMeta+z");
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.read()))
    .toEqual({
      schema_version: 1,
      nodes: [skill, { type: "text", text: " " }],
    });
  await page.keyboard.press("ControlOrMeta+z");
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.read()))
    .toEqual(original);
});

for (const width of [320, 390, 412, 1440])
  test(`atomic reference editing and undo at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installReferenceEditorModules(page, initial);
    await page.goto("/__composer_reference_editor_test");
    await expect(page.locator("#status")).toHaveText("ready");
    const editor = page.getByRole("textbox", { name: "输入消息" });
    await editor.focus();
    await page.evaluate(() => window.referenceEditor.select(4));
    await page.keyboard.press("Backspace");
    await expect
      .poll(() => page.evaluate(() => window.referenceEditor.read()))
      .toEqual({
        schema_version: 1,
        nodes: [{ type: "text", text: "按  分析\n" }],
      });
    await page.keyboard.press("ControlOrMeta+z");
    await expect
      .poll(() => page.evaluate(() => window.referenceEditor.read()))
      .toEqual(initial);
    await page.evaluate(() => window.referenceEditor.select(3));
    await page.keyboard.press("Delete");
    await expect(
      editor.locator(".composer-reference-preview__reference"),
    ).toHaveCount(0);
    await page.keyboard.press("ControlOrMeta+z");
    await expect(
      editor.locator(".composer-reference-preview__reference"),
    ).toHaveText("/杜邦分析");
    await page.evaluate(() => {
      const kernel = window.referenceEditor;
      kernel.select(kernel.editor.state.doc.content.size - 1);
    });
    await page.keyboard.press("Shift+Enter");
    await page.keyboard.insertText("中文 👩‍💻");
    await expect
      .poll(() => page.evaluate(() => window.referenceEditor.read()))
      .toEqual({
        ...initial,
        nodes: [
          ...initial.nodes.slice(0, 2),
          { type: "text", text: " 分析\n\n中文 👩‍💻" },
        ],
      });
    await page.evaluate(() => {
      document.body.style.fontSize = "32px";
    });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(overflow).toBe(false);
    expect(await editor.innerHTML()).not.toContain("private-material");
  });

test("real browser composition, menu priority and untrusted clipboard", async ({
  page,
  context,
}) => {
  await installReferenceEditorModules(page, { schema_version: 1, nodes: [] });
  await page.goto("/__composer_reference_editor_test");
  await expect(page.locator("#status")).toHaveText("ready");
  const editor = page.getByRole("textbox", { name: "输入消息" });
  await editor.focus();
  await page.evaluate(() => {
    window.referenceEditor.state.menu = true;
  });
  // Chromium's native composition path, not a mocked editor or manually flipped PM state.
  const cdp = await context.newCDPSession(page);
  await page.evaluate(() => {
    const trace: unknown[] = [];
    Object.assign(window, { compositionTrace: trace });
    for (const type of ["compositionstart", "compositionend", "keydown"])
      document.addEventListener(
        type,
        (event) => {
          trace.push({
            type,
            time: event.timeStamp,
            key: (event as KeyboardEvent).key,
            composing: (event as KeyboardEvent).isComposing,
            code: (event as KeyboardEvent).keyCode,
          });
        },
        true,
      );
  });
  await cdp.send("Input.imeSetComposition", {
    text: "盈利",
    selectionStart: 2,
    selectionEnd: 2,
  });
  await page.keyboard.press("Enter");
  expect(
    await page.evaluate(() => window.referenceEditor.state.menuKeys),
    JSON.stringify(
      await page.evaluate(() => Reflect.get(window, "compositionTrace")),
    ),
  ).toEqual([]);
  expect(await page.evaluate(() => window.referenceEditor.state.submits)).toBe(
    0,
  );
  await cdp.send("Input.insertText", { text: "盈利" });
  await expect
    .poll(() =>
      page.evaluate(() => window.referenceEditor.editor.view.composing),
    )
    .toBe(false);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  expect(
    await page.evaluate(() => window.referenceEditor.state.menuKeys),
  ).toEqual(["Enter", "Tab"]);
  expect(await page.evaluate(() => window.referenceEditor.state.submits)).toBe(
    0,
  );
  await page.evaluate(() => {
    window.referenceEditor.state.menu = false;
    const data = new DataTransfer();
    data.setData("text/plain", " @admin /skill\r\n<img src=x>");
    data.setData(
      "text/html",
      '<span data-type="reference" data-reference="forged">@管理员</span><img src=x onerror="alert(1)">',
    );
    window.referenceEditor.editor.view.dom.dispatchEvent(
      new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: data,
      }),
    );
  });
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.read()))
    .toEqual({
      schema_version: 1,
      nodes: [{ type: "text", text: "盈利 @admin /skill\n<img src=x>" }],
    });
  await expect(editor.locator("img")).toHaveCount(0);
  await expect(
    editor.locator(".composer-reference-preview__reference"),
  ).toHaveCount(0);
  await page.keyboard.press("Enter");
  expect(await page.evaluate(() => window.referenceEditor.state.submits)).toBe(
    1,
  );
});
