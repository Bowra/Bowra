import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import catalogFixtures from "../../../contracts/chat-references/catalog-v1.json";
import { parseInputDocument } from "../lib/composer-input-document";
import { referenceDraftInput } from "./_composer-reference-fixtures";

const started = { status: "started", session_id: "s_reference_v2", turn_id: "t_reference_v2", stream_url: "/api/v1/chat/stream/t_reference_v2", execution_generation: 1 };
async function load(page: Page, query = "?seed=1", prefix = "") {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    (globalThis as unknown as { process: { env: Record<string, string> } }).process = { env: {} };
    class QuietEventSource {
      static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
      readyState = 1; onerror = null;
      addEventListener() {} removeEventListener() {} close() { this.readyState = 2; }
    }
    window.EventSource = QuietEventSource as unknown as typeof EventSource;
  });
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: { id: "u_v2_reference", team_id: "team_ref", workspaces: [], capabilities: { composer: { model_panel_v2: false, thinking_effort: false, reference_input: !query.includes("auth_off") }, planning: false } } }));
  await installReferenceComponent(page, prefix, "_composer-reference-v2-entry.tsx");
  await page.goto(`${prefix}/__reference_component_test${query}`);
  try { await expect(page.getByRole(query.includes("auth_off") ? "textbox" : "combobox")).toBeVisible(); }
  catch (error) { throw new Error(JSON.stringify(errors) + String(error)); }
  if (!query.includes("handoff") && !query.includes("auth_off")) {
    await expect(page.getByRole("combobox")).toHaveAttribute("contenteditable", "true");
    if (query.includes("seed")) await expect(page.getByRole("combobox")).toContainText("分析本季度盈利能力");
  }
  return errors;
}

async function caretAtEnd(page: Page) {
  const editor = page.getByRole("combobox");
  await editor.evaluate((element) => {
    (element as HTMLElement).focus();
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
  });
  await expect(editor).toBeFocused();
}

test("production Composer hydrates full source, saves before POST and clears its accepted revision", async ({ page }) => {
  const errors = await load(page);
  const posts: unknown[] = [];
  await page.route("**/api/v1/chat/send", async (route) => {
    const body = route.request().postDataJSON();
    const state = await page.evaluate(() => window.referenceV2.read());
    expect(state.draft?.input_document).toEqual(referenceDraftInput.input_document);
    expect(state.pending?.input_document).toEqual(referenceDraftInput.input_document);
    expect(state.pending?.clientSubmissionId).toEqual(body.client_submission_id);
    posts.push(body);
    await route.fulfill({ json: started });
  });
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
  await expect.poll(() => page.evaluate(() => window.referenceV2.read())).toEqual({ draft: null, pending: null });
  expect(posts).toHaveLength(1);
  expect(await page.evaluate(() => window.referenceV2.calls)).toMatchObject([{ message: referenceDraftInput.message, context: { writeProtocol: "legacy_v0", referenceInputDocument: referenceDraftInput.input_document } }]);
  expect(errors).toEqual([]);
});

test("editing and reload preserve exact identities; deleting tokens returns to the ordinary writer", async ({ page }) => {
  await load(page, "?seed=1&locale=en", "/bowhead");
  const editor = page.getByRole("combobox");
  await caretAtEnd(page);
  await editor.pressSequentially(" next");
  await expect.poll(() => page.evaluate(async () => (await window.referenceV2.read()).draft?.message)).toBe(referenceDraftInput.message + " next");
  await page.reload();
  await expect(editor).toContainText(" next");
  const saved = await page.evaluate(() => window.referenceV2.read());
  expect(saved.draft?.input_document.nodes.filter((node) => node.type === "reference")).toEqual(referenceDraftInput.input_document.nodes.filter((node) => node.type === "reference"));
  await editor.press("ControlOrMeta+A");
  await editor.pressSequentially("ordinary question");
  await expect.poll(() => page.evaluate(async () => (await window.referenceV2.read()).draft?.message)).toBe("ordinary question");
  await page.evaluate(() => window.referenceV2.configure({ enabled: false }));
  await page.reload();
  await expect(page.getByRole("combobox")).toContainText("ordinary question");
});

test("reference submission keeps its own contract when the ordinary resolver chooses Terminal", async ({ page }) => {
  await load(page);
  await page.evaluate(() => window.referenceV2.configure({ protocol: "terminal_v1" }));
  await page.route("**/api/v1/chat/send", async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({ input_document: referenceDraftInput.input_document, submission_contract_version: "chat-references.v1" });
    await route.fulfill({ json: started });
  });
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
  expect(await page.evaluate(() => window.referenceV2.calls)).toMatchObject([{ context: { writeProtocol: "legacy_v0", referenceInputDocument: referenceDraftInput.input_document } }]);
});

for (const choice of ["local", "latest", "changed_again", "disabled"] as const) {
  test(`reference conflict ${choice} preserves the chosen full document and the shown revision`, async ({ page }) => {
    await load(page);
    const latest = { schema_version: 1 as const, nodes: [{ type: "text" as const, text: "remote draft" }] };
    await page.evaluate((document) => window.referenceV2.seed(document), latest);
    const editor = page.getByRole("combobox");
    await caretAtEnd(page);
    await editor.pressSequentially(" local");
    await page.getByRole("button", { name: "合并最新草稿" }).click();
    await expect(page.getByRole("region", { name: "另一个标签页的草稿" })).toContainText("remote draft");
    if (choice === "disabled") {
      await page.evaluate(() => window.referenceV2.configure({ enabled: false }));
      await expect(editor).toHaveAttribute("contenteditable", "false");
      expect((await page.evaluate(() => window.referenceV2.read())).draft?.input_document).toEqual(latest);
      return;
    }
    if (choice === "changed_again") {
      await page.evaluate(() => window.referenceV2.seed({ schema_version: 1, nodes: [{ type: "text", text: "newer remote" }] }));
    }
    await page.getByRole("button", { name: choice === "latest" ? "使用最新草稿" : "保留当前草稿", exact: true }).click();
    if (choice === "changed_again") {
      await expect(page.getByRole("button", { name: "合并最新草稿" })).toBeVisible();
      expect((await page.evaluate(() => window.referenceV2.read())).draft?.message).toBe("newer remote");
      await expect(editor).toContainText(" local");
    } else {
      await expect(page.getByRole("button", { name: "合并最新草稿" })).toHaveCount(0);
      const saved = (await page.evaluate(() => window.referenceV2.read())).draft!;
      if (choice === "latest") expect(saved.input_document).toEqual(latest);
      else {
        expect(saved.message).toBe(referenceDraftInput.message + " local");
        expect(saved.input_document.nodes.filter((node) => node.type === "reference")).toEqual(referenceDraftInput.input_document.nodes.filter((node) => node.type === "reference"));
      }
    }
    expect(await page.evaluate(() => window.referenceV2.calls)).toEqual([]);
  });
}

test("ghost completion of ordinary history remains visual until accepted with Tab", async ({ page }) => {
  await load(page, "?blank=1");
  await page.evaluate(() => window.referenceV2.configure({ sessionTexts: ["分析本季度盈利能力 with details"] }));
  await caretAtEnd(page);
  await page.keyboard.type("分析本季度");
  await expect(page.getByTestId("composer-autocomplete-ghost")).toContainText("盈利能力 with details");
  await expect(page.getByRole("combobox")).toHaveText(/^分析本季度/);
  await expect.poll(() => page.evaluate(async () => (await window.referenceV2.read()).draft?.message)).toBe("分析本季度");
  await page.getByRole("combobox").press("Tab");
  await expect(page.getByRole("combobox")).toBeFocused();
  await expect.poll(() => page.evaluate(async () => (await window.referenceV2.read()).draft?.message)).toBe("分析本季度盈利能力 with details");
  await expect(page.getByTestId("composer-autocomplete-ghost")).toHaveCount(0);
  expect(await page.evaluate(() => window.referenceV2.calls)).toEqual([]);
});

test("history that only spells @Agent or /Skill names is never offered as a ghost completion", async ({ page }) => {
  // History stores display text only; completing it would present unbound
  // "@Agent /Skill" text as if it were a selected reference (0f9716c4c).
  await load(page);
  await page.evaluate((message) => window.referenceV2.configure({ sessionTexts: [message + " with details"] }), referenceDraftInput.message);
  // Wait until the configured history has reached the editor (one render cycle).
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.getByTestId("composer-autocomplete-ghost")).toHaveCount(0);
  await caretAtEnd(page);
  await page.getByRole("combobox").press("Tab");
  await expect(page.getByTestId("composer-autocomplete-ghost")).toHaveCount(0);
  const saved = (await page.evaluate(() => window.referenceV2.read())).draft!;
  expect(saved.input_document).toEqual(referenceDraftInput.input_document);
  expect(saved.message).toBe(referenceDraftInput.message);
  expect(await page.evaluate(() => window.referenceV2.calls)).toEqual([]);
});

test("browser IME keeps toolbar sending fenced until the final text settles", async ({ page, context }) => {
  await load(page);
  const cdp = await context.newCDPSession(page);
  await caretAtEnd(page);
  await cdp.send("Input.imeSetComposition", { text: "利润", selectionStart: 2, selectionEnd: 2 });
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => window.referenceV2.calls)).toEqual([]);
  await cdp.send("Input.insertText", { text: "利润" });
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
  await expect.poll(() => page.evaluate(async () => (await window.referenceV2.read()).draft?.message)).toBe(referenceDraftInput.message + "利润");
  await cdp.detach();
});

test("a scoped handoff reaches the production editor only after explicit durable acceptance", async ({ page }) => {
  await load(page, "?handoff=1");
  const editor = page.getByRole("combobox");
  await expect(editor).toHaveAttribute("contenteditable", "false");
  expect((await page.evaluate(() => window.referenceV2.read())).draft).toBeNull();
  await page.getByRole("button", { name: "接收引用草稿" }).click();
  await expect(editor).toHaveAttribute("contenteditable", "true");
  expect((await page.evaluate(() => window.referenceV2.read())).draft?.input_document).toEqual(referenceDraftInput.input_document);
  expect(await page.evaluate(() => window.referenceV2.calls)).toEqual([]);
});

for (const withAttachment of [false, true]) {
  test(`a lost response restores the original journal identity after reload attachment=${withAttachment}`, async ({ page }) => {
    await load(page);
    const attachments = withAttachment ? [{ fileId: "art_reference_profit", filename: "profit.csv", mime: "text/csv", size: 42, referenceKind: "upload" as const }] : [];
    if (withAttachment) {
      await page.evaluate((payload) => { const { document, attachments } = JSON.parse(payload); return window.referenceV2.seed(document, attachments); }, JSON.stringify({ document: referenceDraftInput.input_document, attachments }));
      await page.reload();
      await expect(page.getByText("profit.csv", { exact: true })).toBeVisible();
    }
    const ids: string[] = [];
    await page.route("**/api/v1/chat/submissions/**", (route) => route.fulfill({ status: 404, json: {} }));
    await page.route("**/api/v1/chat/send", (route) => {
      const body = route.request().postDataJSON();
      expect(body.input_document).toEqual(referenceDraftInput.input_document);
      expect(body.attachments ?? []).toEqual(attachments.map((item) => item.fileId));
      expect(body.attachment_refs ?? []).toEqual(attachments);
      ids.push(body.client_submission_id);
      return ids.length === 1 ? route.abort("failed") : route.fulfill({ json: started });
    });
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect.poll(() => page.evaluate(() => window.referenceV2.settled)).toEqual([false]);
    const original = (await page.evaluate(() => window.referenceV2.read())).pending!;
    await page.reload();
    await expect(page.getByRole("combobox")).toBeVisible();
    expect(ids).toHaveLength(1);
    await page.locator("#recover").click();
    await expect(page.locator("#turn")).toHaveText(started.turn_id);
    expect(ids).toEqual([original.clientSubmissionId, original.clientSubmissionId]);
    await expect.poll(() => page.evaluate(() => window.referenceV2.read())).toEqual({ draft: null, pending: null });
  });
}

test("accepted cleanup failure keeps the durable journal for original-receipt recovery", async ({ page }) => {
  await load(page);
  await page.evaluate(() => window.referenceV2.configure({ cleanupFailure: true }));
  const ids: string[] = [];
  await page.route("**/api/v1/chat/send", (route) => { ids.push(route.request().postDataJSON().client_submission_id); return route.fulfill({ json: started }); });
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.referenceV2.settled)).toEqual([false]);
  await expect(page.locator("#turn")).toHaveText("");
  expect((await page.evaluate(() => window.referenceV2.read())).pending?.input_document).toEqual(referenceDraftInput.input_document);
  await page.evaluate(() => window.referenceV2.configure({ cleanupFailure: false }));
  await page.locator("#recover").click();
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
  expect(ids).toHaveLength(2);
  expect(ids[1]).toBe(ids[0]);
  await expect.poll(() => page.evaluate(() => window.referenceV2.read())).toEqual({ draft: null, pending: null });
});


test("the production input follows auth capability without enabling the legacy submission protocol", async ({ page }) => {
  await load(page, "?auth=1&auth_off=1");
  await expect(page.getByRole("textbox")).toBeEditable();
  await expect(page.getByRole("combobox")).toHaveCount(0);
  await page.unroute("**/api/v1/auth/me");
  await load(page, "?auth=1");
  await expect(page.getByRole("combobox")).toBeEditable();
});

for (const width of [320, 1440]) {
  test(`reference input remains within the viewport at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await load(page, "?seed=1&locale=en");
    const editor = page.getByRole("combobox");
    const rect = await editor.boundingBox();
    expect(rect!.x).toBeGreaterThanOrEqual(0);
    expect(rect!.x + rect!.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}

for (const width of [320, 1440]) {
  test(`Skill parameters in production Composer persist, reopen and send at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const errors = await load(page);
    const original = parseInputDocument(referenceDraftInput.input_document);
    const skill = original.nodes.find((node) => node.type === "reference" && node.resource.kind === "skill")!;
    if (skill.type !== "reference" || skill.resource.kind !== "skill") throw new Error("Expected Skill");
    skill.parameters = { period: "2026Q3", detail: { units: 2 } };
    await page.evaluate((document) => window.referenceV2.seed(JSON.parse(document)), JSON.stringify(original));
    await page.reload();
    const catalog = catalogFixtures.cases.find((item) => item.name === "team_skill")!.response;
    const response = structuredClone(catalog);
    Object.assign(response.groups[0].items[0], { id: skill.resource.id, resource: skill.resource, read_only_parameter_schema: {
      type: "object", additionalProperties: false, properties: {
        period: { type: "string", title: "Period" },
        detail: { type: "object", additionalProperties: false, properties: { units: { type: "integer" } } },
      },
    } });
    let metadataRequests = 0;
    await page.route("**/api/v1/chat/references/catalog", async (route) => {
      metadataRequests++;
      expect(route.request().postDataJSON().exact_resource).toEqual(skill.resource);
      await route.fulfill({ json: response });
    });
    const token = page.getByRole("button", { name: "编辑 /杜邦分析 的参数" });
    await token.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => window.referenceV2.calls)).toEqual([]);
    const period = dialog.getByRole("textbox", { name: /Period/ });
    await expect(period).toHaveValue("2026Q3");
    await period.fill("2026Q4");
    await dialog.getByRole("button", { name: "应用参数" }).click();
    await expect(dialog).toHaveCount(0);
    const edited = structuredClone(original);
    const editedSkill = edited.nodes.find((node) => node.type === "reference" && node.resource.kind === "skill")!;
    if (editedSkill.type !== "reference") throw new Error("Expected Skill");
    editedSkill.parameters.period = "2026Q4";
    await expect.poll(() => page.evaluate(async () => (await window.referenceV2.read()).draft?.input_document)).toEqual(edited);
    await page.reload();
    await token.click();
    await expect(period).toHaveValue("2026Q4");
    await period.fill("not saved");
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    expect((await page.evaluate(() => window.referenceV2.read())).draft?.input_document).toEqual(edited);
    await page.route("**/api/v1/chat/send", async (route) => {
      expect(route.request().postDataJSON().input_document).toEqual(edited);
      expect((await page.evaluate(() => window.referenceV2.read())).pending?.input_document).toEqual(edited);
      await route.fulfill({ json: started });
    });
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.locator("#turn")).toHaveText(started.turn_id);
    expect(metadataRequests).toBe(2);
    expect(errors).toEqual([]);
  });
}

test("production slash choice preserves existing references and sends the added exact method", async ({ page }) => {
  await load(page);
  const response = structuredClone(catalogFixtures.cases.find((item) => item.name === "builtin_skill")!.response) as any;
  for (const cell of response.entry_support) if (["agent", "skill"].includes(cell.resource_kind)) Object.assign(cell, { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
  const item = response.groups[0].items[0];
  Object.assign(item, { label: "盈利比较", availability: "readonly_extension", reason_code: null });
  await page.route("**/api/v1/chat/references/catalog", async (route) => {
    const request = route.request().postDataJSON();
    expect(request.selected_refs).toEqual(parseInputDocument(referenceDraftInput.input_document).nodes.flatMap((node) => node.type === "reference" ? [node.resource] : []));
    await route.fulfill({ json: response });
  });
  await caretAtEnd(page);
  await page.getByRole("combobox").pressSequentially(" /");
  await page.getByRole("option", { name: /盈利比较/ }).click();
  await expect(page.getByRole("button", { name: "编辑 /盈利比较 的参数" })).toBeVisible();
  await expect.poll(async () => (await page.evaluate(() => window.referenceV2.read())).draft?.input_document.nodes.filter((node) => node.type === "reference").length).toBe(3);
  const document = (await page.evaluate(() => window.referenceV2.read())).draft!.input_document;
  expect(document.nodes.find((node) => node.type === "reference" && node.label === "盈利比较")).toMatchObject({ resource: item.resource, parameters: {} });
  expect(document.nodes.filter((node) => node.type === "reference").slice(0, 2)).toEqual(parseInputDocument(referenceDraftInput.input_document).nodes.filter((node) => node.type === "reference"));
  await page.route("**/api/v1/chat/send", async (route) => {
    expect(route.request().postDataJSON().input_document).toEqual(document);
    await route.fulfill({ json: started });
  });
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator("#turn")).toHaveText(started.turn_id);
});


for (const width of [412, 1280]) {
  for (const withText of [false, true]) {
    test(`ordinary references retain typed attachments through reload and send at ${width}px text=${withText}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await load(page);
      const document = parseInputDocument({ ...referenceDraftInput.input_document, nodes: referenceDraftInput.input_document.nodes.filter((node) => withText || node.type === "reference") })!;
      const attachments = [{ fileId: "art_reference_profit", filename: "profit.csv", mime: "text/csv", size: 42, referenceKind: "upload" as const }];
      await page.evaluate((payload) => { const { document, attachments } = JSON.parse(payload); return window.referenceV2.seed(document, attachments); }, JSON.stringify({ document, attachments }));
      await page.reload();
      await expect(page.getByRole("combobox")).toHaveAttribute("contenteditable", "true");
      await expect(page.getByText("profit.csv", { exact: true })).toBeVisible();
      const posts: any[] = [];
      await page.route("**/api/v1/chat/send", async (route) => {
        const body = route.request().postDataJSON();
        const state = await page.evaluate(() => window.referenceV2.read());
        expect(state.draft?.attachments).toEqual(attachments);
        expect(state.pending?.attachments).toEqual(attachments);
        expect(state.pending?.input_document).toEqual(document);
        posts.push(body);
        await route.fulfill({ json: started });
      });
      const send = page.getByRole("button", { name: "发送", exact: true });
      await expect(send).toBeEnabled();
      await send.click();
      await expect(page.locator("#turn")).toHaveText(started.turn_id);
      expect(posts).toHaveLength(1);
      expect(posts[0]).toMatchObject({ input_document: document, attachments: [attachments[0].fileId], attachment_refs: attachments, submission_kind: "start", submission_contract_version: "chat-references.v1" });
      await expect.poll(() => page.evaluate(() => window.referenceV2.read())).toEqual({ draft: null, pending: null });
    });
  }
}
