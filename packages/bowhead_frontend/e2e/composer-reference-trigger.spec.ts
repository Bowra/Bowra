import { expect, test } from "@playwright/test";
import { installReferenceEditorModules } from "./_composer-reference-editor-modules";

test("native typing opens a multi-word query, escape and paths preserve it as text", async ({
  page,
}) => {
  await installReferenceEditorModules(
    page,
    { schema_version: 1, nodes: [] },
    true,
  );
  await page.goto("/__composer_reference_editor_test");
  await expect(page.locator("#status")).toHaveText("ready");
  const editor = page.getByRole("textbox", { name: "输入消息" });
  await editor.focus();
  await page.keyboard.press("/");
  await page.keyboard.type("Profit analysis");
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.state.query?.query))
    .toBe("Profit analysis");
  await page.keyboard.press("Backspace");
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.state.query?.query))
    .toBe("Profit analysi");
  await page.keyboard.press("Escape");
  expect(
    await page.evaluate(() => window.referenceEditor.state.query),
  ).toBeNull();
  expect(await editor.textContent()).toBe("/Profit analysi");
  expect(await page.evaluate(() => window.referenceEditor.reopen())).toBe(true);
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.state.query?.query))
    .toBe("Profit analysi");
  await page.keyboard.press("/");
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.state.query))
    .toBeNull();
  expect(await editor.textContent()).toBe("/Profit analysi/");
  expect(await page.evaluate(() => window.referenceEditor.state.submits)).toBe(
    0,
  );
});

test("native paste and literal contexts never open a query without an explicit action", async ({
  page,
}) => {
  for (const [prefix, trigger] of [
    ["a", "@"],
    ["https:", "/"],
    ["src", "/"],
    ["2026", "/"],
    ["(", "@"],
    ["\\", "@"],
    ["`code ", "/"],
    ["```js\ncode ", "@"],
    ["[link](url ", "/"],
  ]) {
    await installReferenceEditorModules(
      page,
      { schema_version: 1, nodes: [{ type: "text", text: prefix }] },
      true,
    );
    await page.goto("/__composer_reference_editor_test");
    await expect(page.locator("#status")).toHaveText("ready");
    await page.getByRole("textbox", { name: "输入消息" }).focus();
    await page.evaluate(() => {
      const kernel = window.referenceEditor;
      kernel.select(kernel.editor.state.doc.content.size - 1);
    });
    await page.keyboard.press(trigger);
    expect(
      await page.evaluate(() => window.referenceEditor.state.queryChanges),
      prefix,
    ).toEqual([]);
  }
  await installReferenceEditorModules(
    page,
    { schema_version: 1, nodes: [] },
    true,
  );
  await page.goto("/__composer_reference_editor_test");
  await expect(page.locator("#status")).toHaveText("ready");
  await page.getByRole("textbox", { name: "输入消息" }).focus();
  await page.evaluate(() => {
    const data = new DataTransfer();
    data.setData("text/plain", "/pasted query");
    window.referenceEditor.editor.view.dom.dispatchEvent(
      new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: data,
      }),
    );
  });
  expect(
    await page.evaluate(() => window.referenceEditor.state.queryChanges),
  ).toEqual([]);
  expect(await page.evaluate(() => window.referenceEditor.reopen())).toBe(true);
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.state.query?.query))
    .toBe("pasted query");
});

test("native composition suspends a query, then resumes Chinese search without selection or sending", async ({
  page,
  context,
}) => {
  await installReferenceEditorModules(
    page,
    { schema_version: 1, nodes: [] },
    true,
  );
  await page.goto("/__composer_reference_editor_test");
  await expect(page.locator("#status")).toHaveText("ready");
  await page.getByRole("textbox", { name: "输入消息" }).focus();
  await page.keyboard.press("/");
  await expect
    .poll(() =>
      page.evaluate(() => window.referenceEditor.state.query?.trigger),
    )
    .toBe("/");
  const cdp = await context.newCDPSession(page);
  await cdp.send("Input.imeSetComposition", {
    text: "经营分析",
    selectionStart: 4,
    selectionEnd: 4,
  });
  expect(
    await page.evaluate(() => window.referenceEditor.state.query),
  ).toBeNull();
  await page.keyboard.press("Enter");
  expect(
    await page.evaluate(() => window.referenceEditor.state.menuKeys),
  ).toEqual([]);
  expect(await page.evaluate(() => window.referenceEditor.state.submits)).toBe(
    0,
  );
  await cdp.send("Input.insertText", { text: "经营分析" });
  await expect
    .poll(() => page.evaluate(() => window.referenceEditor.state.query?.query))
    .toBe("经营分析");
  await page.keyboard.press("Enter");
  expect(await page.evaluate(() => window.referenceEditor.state.submits)).toBe(
    0,
  );
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "After editor" }),
  ).toBeFocused();
  expect(
    await page.evaluate(() => window.referenceEditor.state.query),
  ).toBeNull();
  expect(await page.evaluate(() => window.referenceEditor.read())).toEqual({
    schema_version: 1,
    nodes: [{ type: "text", text: "/经营分析" }],
  });
});

test("native IME output consisting of a slash remains literal", async ({
  page,
  context,
}) => {
  await installReferenceEditorModules(
    page,
    { schema_version: 1, nodes: [] },
    true,
  );
  await page.goto("/__composer_reference_editor_test");
  await expect(page.locator("#status")).toHaveText("ready");
  await page.getByRole("textbox", { name: "输入消息" }).focus();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Input.imeSetComposition", {
    text: "/",
    selectionStart: 1,
    selectionEnd: 1,
  });
  await cdp.send("Input.insertText", { text: "/" });
  await expect
    .poll(() =>
      page.evaluate(() => window.referenceEditor.editor.view.composing),
    )
    .toBe(false);
  expect(
    await page.evaluate(() => window.referenceEditor.state.queryChanges),
  ).toEqual([]);
  expect(await page.evaluate(() => window.referenceEditor.read())).toEqual({
    schema_version: 1,
    nodes: [{ type: "text", text: "/" }],
  });
});
