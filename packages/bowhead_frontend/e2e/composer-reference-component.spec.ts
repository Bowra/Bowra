import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";
import fixtures from "../../../contracts/chat-references/catalog-v1.json";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import type {} from "./_composer-reference-component-entry";
import type { ReferenceCatalogResponse } from "../lib/reference-catalog-api";

function response() {
  const result = structuredClone(fixtures.cases[0].response);
  const original = result.groups[0].items[0];
  result.groups[0].items = [
    "经营分析师 · Profitability and operating performance analyst",
    "第二位分析师 · Another analyst",
  ].map((label, i) => ({
    ...original,
    id: `osa_${i}`,
    label,
    summary:
      "解释收入、成本与利润变化，核对计算口径和数据来源。Review profitability, definitions and source evidence.",
    resource: {
      ...original.resource!,
      id: `osa_${i}`,
      version: `arv_${i}`,
      assignment_id: `asa_${i}`,
      release_id: `arl_${i}`,
    },
  }));
  return result;
}
async function open(page: Page, prefix = "") {
  await installReferenceComponent(page, prefix);
  await page.route("**/api/v1/chat/references/catalog", (route) =>
    route.fulfill({ json: response() }),
  );
  await page.goto(`${prefix}/__reference_component_test`);
  await expect(page.getByRole("combobox")).toBeVisible();
}
async function typeQuery(page: Page) {
  await page.getByRole("combobox").focus();
  await page.keyboard.press("@");
  await expect(page.getByRole("option")).toHaveCount(2);
}

test("real component catalog, keyboard, focus and root/prefix HTTP", async ({
  page,
}) => {
  for (const prefix of ["", "/bowhead"]) {
    await open(page, prefix);
    const request = page.waitForRequest((req) =>
      req.url().endsWith(`${prefix}/api/v1/chat/references/catalog`),
    );
    await typeQuery(page);
    expect((await request).postDataJSON()).toMatchObject({
      trigger: "@",
      query: "",
      workspace_id: "ws_test",
    });
    const beforeGhost = await page.evaluate(() => [
      ...window.referenceComponent.ghostKeys,
    ]);
    const options = page.getByRole("option");
    await expect(options.first()).toHaveAttribute("aria-disabled", "true");
    await page.keyboard.press("End");
    await expect(page.getByRole("combobox")).toHaveAttribute(
      "aria-activedescendant",
      (await options.last().getAttribute("id"))!,
    );
    await page.keyboard.press("Enter");
    await options.first().click({ force: true });
    expect(
      await page.evaluate(() => window.referenceComponent.submits),
    ).toEqual([]);
    expect(
      await page.evaluate(() => window.referenceComponent.changes.at(-1)),
    ).toEqual({ schema_version: 1, nodes: [{ type: "text", text: "@" }] });
    await page.keyboard.press("Tab");
    await expect(page.locator("#after")).toBeFocused();
    await expect(page.getByRole("listbox")).toHaveCount(0);
    expect(
      await page.evaluate(() => window.referenceComponent.ghostKeys),
    ).toEqual(beforeGhost);
  }
});

test("selects and replaces exact Agents with native keys, keeps undo and never sends a repeated selection key", async ({ page }) => {
  // Plain HTTP deployments do not expose the secure-context UUID API.
  await page.addInitScript(() => Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true }));
  for (const prefix of ["", "/bowhead"]) {
    await installReferenceComponent(page, prefix);
    const data = response() as any;
    Object.assign(data.entry_support[0], { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
    data.groups[0].items.forEach((item: any) => Object.assign(item, { availability: "baseline", reason_code: null }));
    await page.route("**/api/v1/chat/references/catalog", (route) => route.fulfill({ json: data }));
    await page.goto(`${prefix}/__reference_component_test`);
    const input = page.getByRole("combobox");
    await input.pressSequentially("Review @");
    await expect(page.getByRole("option").first()).toHaveAttribute("aria-disabled", "false");
    await page.keyboard.down("Enter");
    await expect(page.getByRole("listbox")).toHaveCount(0);
    await page.keyboard.down("Enter");
    await page.keyboard.up("Enter");
    expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual([]);
    await expect(input).toBeFocused();
    await input.pressSequentially("@second");
    await expect(page.getByRole("option")).toHaveCount(2);
    await page.keyboard.press("End");
    await page.keyboard.press("Tab");
    const selected = await page.evaluate(() => window.referenceComponent.changes.at(-1)!);
    expect(selected.nodes.filter((node) => node.type === "reference").map((node) => node.resource.kind === "agent" ? node.resource.id : null)).toEqual(["osa_1"]);
    await expect(input).toBeFocused();
    await page.keyboard.press("ControlOrMeta+z");
    const undone = await page.evaluate(() => window.referenceComponent.changes.at(-1)!);
    expect(undone.nodes.filter((node) => node.type === "reference").map((node) => node.resource.kind === "agent" ? node.resource.id : null)).toEqual(["osa_0"]);
    await expect(input).toContainText("@second");
    expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual([]);
    // Redo, then explicit fresh Enter submits the exact final document once.
    await page.keyboard.press("ControlOrMeta+Shift+z");
    await page.keyboard.press("Enter");
    expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual([selected]);
  }
});

test("native IME suspends the actual popup and resumes Chinese query", async ({
  page,
  context,
}) => {
  await open(page);
  await typeQuery(page);
  const cdp = await context.newCDPSession(page);
  await cdp.send("Input.imeSetComposition", {
    text: "经营分析",
    selectionStart: 4,
    selectionEnd: 4,
  });
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await page.keyboard.press("Enter");
  const request = page.waitForRequest(
    (req) =>
      req.url().endsWith("/references/catalog") &&
      req.postDataJSON().query === "经营分析",
  );
  await cdp.send("Input.insertText", { text: "经营分析" });
  await request;
  await expect(page.getByRole("option")).toHaveCount(2);
  expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual(
    [],
  );
  await page.getByRole("button", { name: "继续输入文字" }).click();
  await expect(page.getByRole("combobox")).toBeFocused();
  await expect(page.getByRole("combobox")).toHaveText("@经营分析");
  await expect(page.getByRole("listbox")).toHaveCount(0);
});

for (const locale of ["zh-CN", "en-US"] as const) {
  for (const width of [320, 390, 412, 768, 1440]) {
    test(`popup viewport, disabled rows and ARIA at ${locale} ${width}px`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 850 });
      await page.emulateMedia({
        colorScheme: width === 390 ? "dark" : "light",
        reducedMotion: "reduce",
      });
      await open(page);
      await page.evaluate(
        ({ locale, doubled, dark }) => {
          window.referenceComponent.update({ locale });
          document.documentElement.lang = locale;
          document.documentElement.dataset.theme = dark ? "dark" : "light";
          if (doubled) {
            const styles = getComputedStyle(document.documentElement);
            const sizes = Array.from(styles)
              .filter((name) => name.startsWith("--fs-"))
              .map((name) => [name, styles.getPropertyValue(name).trim()]);
            for (const [name, value] of sizes) {
              if (value.endsWith("px"))
                document.documentElement.style.setProperty(
                  name,
                  `${parseFloat(value) * 2}px`,
                );
            }
            document.documentElement.style.fontSize = "200%";
          }
        },
        { locale, doubled: width === 412, dark: width === 390 },
      );
      await typeQuery(page);
      const menu = page.locator(".composer-reference-menu");
      for (const height of [850, 520]) {
        await page.setViewportSize({ width, height });
        await expect
          .poll(async () => {
            const box = await menu.boundingBox();
            return Boolean(
              box &&
              box.x >= 7 &&
              box.y >= 7 &&
              box.x + box.width <= width - 7 &&
              box.y + box.height <= height - 7,
            );
          })
          .toBe(true);
        expect(
          await page
            .getByRole("option")
            .first()
            .evaluate((el) => el.getBoundingClientRect().height),
        ).toBeGreaterThanOrEqual(44);
        expect(
          await page.evaluate(() => document.documentElement.scrollWidth),
        ).toBeLessThanOrEqual(width);
      }
      const scroller = page.locator(".composer-reference-menu__results");
      // Page keys scroll the results only when they overflow; empty groups no longer render their own
      // messages, so two results fit without scrolling on wide screens at normal text size.
      const overflows = await scroller.evaluate(
        (element) => element.scrollHeight > element.clientHeight + 1,
      );
      if (overflows) {
        const beforeScroll = await scroller.evaluate(
          (element) => element.scrollTop,
        );
        await page.keyboard.press("PageDown");
        await expect
          .poll(() => scroller.evaluate((element) => element.scrollTop))
          .toBeGreaterThan(beforeScroll);
        const afterScroll = await scroller.evaluate(
          (element) => element.scrollTop,
        );
        await page.keyboard.press("PageUp");
        await expect
          .poll(() => scroller.evaluate((element) => element.scrollTop))
          .toBeLessThan(afterScroll);
      } else {
        expect(width).toBeGreaterThanOrEqual(768);
      }
      await expect(page.getByRole("combobox")).toHaveText("@");
      await page.addScriptTag({ content: axe.source });
      const violations = await page.evaluate(async () =>
        (
          await (window as unknown as { axe: typeof axe }).axe.run({
            include: [
              [".composer-reference-editor"],
              [".composer-reference-menu"],
            ],
          })
        ).violations.map((item) => ({
          id: item.id,
          nodes: item.nodes.map((node) => node.target),
        })),
      );
      expect(violations).toEqual([]);
      if (
        (locale === "en-US" && [320, 412].includes(width)) ||
        (locale === "zh-CN" && width === 1440)
      )
        await page.screenshot({
          path: info.outputPath("reference-menu.png"),
          fullPage: true,
        });
    });
  }
}

test("empty and failed catalog keep accessible status and cannot submit", async ({
  page,
}) => {
  await open(page);
  await page.route("**/api/v1/chat/references/catalog", (route) =>
    route.fulfill({ json: fixtures.cases[1].response }),
  );
  await page.getByRole("combobox").focus();
  await page.keyboard.press("/");
  await expect(
    page.getByText("此类资源暂未开放。", { exact: true }),
  ).toHaveCount(3);
  await page.addScriptTag({ content: axe.source });
  expect(
    await page.evaluate(async () =>
      (
        await (window as unknown as { axe: typeof axe }).axe.run({
          include: [
            [".composer-reference-editor"],
            [".composer-reference-menu"],
          ],
        })
      ).violations.map((item) => item.id),
    ),
  ).toEqual([]);
  await page.keyboard.press("Enter");
  expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual(
    [],
  );
  await page.keyboard.press("Escape");
  await page.keyboard.press("Space");
  await page.route("**/api/v1/chat/references/catalog", (route) =>
    route.fulfill({
      status: 500,
      json: { detail: "internal private traceback" },
    }),
  );
  await page.keyboard.press("@");
  await expect(page.getByRole("alert")).toHaveText("资源加载失败，请重试。");
  expect(await page.locator("body").textContent()).not.toContain(
    "internal private traceback",
  );
  expect(
    await page.evaluate(async () =>
      (
        await (window as unknown as { axe: typeof axe }).axe.run({
          include: [
            [".composer-reference-editor"],
            [".composer-reference-menu"],
          ],
        })
      ).violations.map((item) => item.id),
    ),
  ).toEqual([]);
  await page.route("**/api/v1/chat/references/catalog", (route) =>
    route.fulfill({ json: response() }),
  );
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option")).toHaveCount(2);
  expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual(
    [],
  );
  await page.keyboard.press("Tab");
  await expect(page.locator("#after")).toBeFocused();
});

test("a pending catalog and editor disappear together when the space changes", async ({
  page,
}) => {
  await open(page);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/chat/references/catalog", async (route) => {
    await pending;
    await route.fulfill({ json: response() });
  });
  await page.getByRole("combobox").focus();
  const request = page.waitForRequest((req) =>
    req.url().endsWith("/references/catalog"),
  );
  await page.keyboard.press("@");
  await request;
  await expect(
    page.getByText("正在加载可用资源…", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    window.referenceComponent.update({
      scope: {
        userId: "u_test",
        teamId: "t_test",
        workspaceId: "ws_other",
        requestedSessionId: null,
        authSessionGeneration: "auth_test",
      },
    }),
  );
  await expect(page.getByRole("combobox")).toHaveCount(0);
  await expect(page.locator(".composer-reference-menu")).toHaveCount(0);
  release();
  await expect(page.getByText("正在读取当前空间的草稿…")).toBeVisible();
  expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual(
    [],
  );
});

test("slash lists builtin Skill metadata across pages without inserting or executing", async ({
  page,
}) => {
  await installReferenceComponent(page, "/bowhead");
  const data = structuredClone(
    fixtures.cases.find((item) => item.name === "builtin_skill")!.response,
  ) as unknown as ReferenceCatalogResponse;
  const original = data.groups[0].items[0];
  if (original.kind !== "skill")
    throw new Error("expected builtin Skill fixture");
  const requests: any[] = [];
  await page.route("**/api/v1/chat/references/catalog", (route) => {
    const request = route.request().postDataJSON();
    requests.push(request);
    const next = structuredClone(data);
    next.groups[0].items = request.cursor
      ? [
          {
            ...original,
            id: "builtin:last",
            label: "最后一项",
            resource: { ...original.resource!, id: "builtin:last" },
          },
        ]
      : Array.from({ length: 20 }, (_, i) => ({
          ...original,
          id: `builtin:method_${i}`,
          label: `杜邦分析 ${i}`,
          resource: { ...original.resource!, id: `builtin:method_${i}` },
        }));
    next.next_cursor = (request.cursor ? null : "opaque_cursor") as any;
    return route.fulfill({ json: next });
  });
  await page.goto("/bowhead/__reference_component_test");
  const editor = page.getByRole("combobox");
  await editor.focus();
  await page.keyboard.type("/");
  await expect(page.getByRole("option")).toHaveCount(21);
  await expect(page.getByRole("option").first()).toContainText("内置 Skill");
  await page.getByRole("option", { name: /更多|More/i }).click();
  await expect(page.getByRole("option")).toHaveCount(21);
  await expect(page.getByRole("option").last()).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await page.getByRole("option").last().click({ force: true });
  expect(requests.map((r) => [r.trigger, r.cursor])).toEqual([
    ["/", null],
    ["/", "opaque_cursor"],
  ]);
  expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual(
    [],
  );
  await expect(editor).toHaveText("/");
});


for (const kind of ["capability", "mcp_tool"] as const) {
  test(`saved ${kind} parameters use native keyboard editing and preserve submitted identity`, async ({ page }) => {
    await page.setViewportSize({ width: kind === "mcp_tool" ? 412 : 1280, height: 900 });
    await installReferenceComponent(page, "/bowhead");
    const resource = kind === "capability" ? { kind, id: "cap_visualize", version: 1 } : {
      kind, source_id: "ots_finance", connection_binding_id: "ots_finance",
      local_contract_revision: `sha256:${"1".repeat(64)}`, connection_subject_fingerprint: `sha256:${"2".repeat(64)}`,
      operation_name: "teammcp.ots_finance.margin", operation_contract_revision: `sha256:${"3".repeat(64)}`,
    };
    const data: any = structuredClone(fixtures.cases.find((item) => item.name === "builtin_skill")!.response);
    data.groups.forEach((group: any) => { group.items = []; });
    const item = {
      kind, source_kind: kind === "capability" ? "platform" : "team",
      id: kind === "capability" ? "cap_visualize" : "teammcp.ots_finance.margin",
      label: "Margin", summary: "Review margin", resource,
      parameter_schema: { type: "object", properties: { year: { type: "integer", minimum: 2000 } }, required: ["year"], additionalProperties: false },
      availability: "unavailable", reason_code: "unsupported_entry",
      ...(kind === "mcp_tool" ? { source_id: "ots_finance", remote_version: { status: "unknown", value: null, evidence: null } } : {}),
    };
    data.groups[kind === "capability" ? 1 : 2] = { id: kind === "capability" ? "capabilities" : "mcp", error: null, items: [item] };
    const requests: unknown[] = [];
    await page.route("**/api/v1/chat/references/catalog", async (route) => {
      requests.push(route.request().postDataJSON());
      await route.fulfill({ json: data });
    });
    await page.goto("/bowhead/__reference_component_test");
    await expect(page.getByRole("combobox")).toBeVisible();
    await page.evaluate((resource) => {
      const scope = { userId: "u_test", teamId: "t_test", workspaceId: "ws_test", requestedSessionId: null, authSessionGeneration: "auth_test" };
      window.referenceComponent.update({ scope, snapshot: { scope, resetKey: "saved-parameters", document: { schema_version: 1, nodes: [
        { type: "text", text: "Review " },
        { type: "reference", node_id: "12345678-1234-4123-8123-123456789abc", resource, label: "Margin", parameters: {} },
      ] } } });
    }, resource);
    const tag = page.getByRole("button", { name: /Margin/ });
    await tag.focus();
    await tag.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    expect(requests).toEqual([expect.objectContaining({ exact_resource: resource, query: "", cursor: null })]);
    const year = page.getByRole("textbox", { name: /year/ });
    await year.fill("1999");
    await expect(page.getByRole("button", { name: "应用参数" })).toBeDisabled();
    await year.fill("2026");
    await page.getByRole("button", { name: "应用参数" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("combobox")).toBeFocused();
    const saved = await page.evaluate(() => window.referenceComponent.changes.at(-1));
    expect(saved!.nodes[1]).toMatchObject({ resource, parameters: { year: 2026 } });
    await page.keyboard.press("ControlOrMeta+z");
    expect((await page.evaluate(() => window.referenceComponent.changes.at(-1)))!.nodes[1]).toMatchObject({ resource, parameters: {} });
    await page.keyboard.press("ControlOrMeta+Shift+z");
    await page.keyboard.press("Enter");
    expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual([saved]);
    expect(requests).toHaveLength(1);
  });
}


for (const [width, entryMode] of [[412, "ordinary"], [1280, "ordinary"], [412, "steer"], [1280, "steer"]] as const) {
  test(`select Prompt, fill required arguments and submit ${entryMode} with the exact Agent at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installReferenceComponent(page, "/bowhead");
    const data: any = structuredClone(fixtures.cases.find((item) => item.name === "slash_unavailable")!.response);
    const resource = { kind: "mcp_prompt" as const, source_id: "ots_finance", connection_binding_id: "ots_finance",
      local_contract_revision: `sha256:${"1".repeat(64)}`, connection_subject_fingerprint: `sha256:${"2".repeat(64)}`,
      prompt_name: "profitability", prompt_contract_revision: `sha256:${"3".repeat(64)}` };
    const prompt = { kind: "mcp_prompt", source_kind: "team", id: "mcpprompt.ots_finance.profitability", source_id: "ots_finance", prompt_name: "profitability",
      label: "Profitability", summary: "Review profit", resource,
      parameter_schema: { type: "object", properties: { period: { type: "string" } }, required: ["period"], additionalProperties: false },
      remote_version: { status: "unknown", value: null, evidence: null }, availability: "baseline", reason_code: null };
    for (const cell of data.entry_support) if (["agent", "mcp_prompt"].includes(cell.resource_kind)) Object.assign(cell, {
      adapter_id: cell.resource_kind === "agent" ? "initial-agent-skill" : "initial-mcp-prompt", adapter_version: 1, supported: true, reason_code: null,
    });
    data.entry_support.forEach((cell: any) => { cell.entry_mode = entryMode; });
    data.groups[2] = { id: "mcp", error: null, items: [prompt] };
    const requests: any[] = [];
    await page.route("**/api/v1/chat/references/catalog", async (route) => {
      const request = route.request().postDataJSON();
      requests.push(request);
      const result = structuredClone(data);
      if (request.exact_resource) Object.assign(result.groups[2].items[0], { availability: "unavailable", reason_code: "unsupported_entry" });
      await route.fulfill({ json: result });
    });
    await page.goto("/bowhead/__reference_component_test");
    await expect(page.getByRole("combobox")).toBeVisible();
    const agent = fixtures.cases[0].response.groups[0].items[0].resource as any;
    await page.evaluate(({ agent, entryMode }) => {
      const scope = { userId: "u_test", teamId: "t_test", workspaceId: "ws_test", requestedSessionId: null, authSessionGeneration: "auth_test" };
      window.referenceComponent.update({ scope, entryMode, snapshot: { scope, resetKey: "choose-prompt", document: { schema_version: 1, nodes: [
        { type: "reference", node_id: "12345678-1234-4123-8123-123456789abc", resource: agent, label: "Analyst", parameters: {} },
        { type: "text", text: " Review profitability " },
      ] } } });
    }, { agent, entryMode });
    const input = page.getByRole("combobox");
    await input.focus();
    await input.press("ControlOrMeta+End");
    await page.keyboard.type("/");
    await expect(page.getByRole("option", { name: /Profitability/ })).toHaveAttribute("aria-disabled", "false");
    await page.keyboard.press("Enter");
    expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual([]);
    const inserted = await page.evaluate(() => window.referenceComponent.changes.at(-1)!);
    await page.getByRole("button", { name: /Profitability/ }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByRole("textbox", { name: /period/ })).toHaveValue("");
    await page.getByRole("textbox", { name: /period/ }).fill("FY2026");
    await page.getByRole("button", { name: "应用参数" }).click();
    await expect(input).toBeFocused();
    await page.keyboard.press("Enter");
    const submits = await page.evaluate(() => window.referenceComponent.submits);
    expect(submits).toHaveLength(1);
    const references = submits[0].nodes.filter((node) => node.type === "reference");
    expect(references).toHaveLength(2);
    expect(references).toEqual(expect.arrayContaining([
      expect.objectContaining({ resource: agent, parameters: {} }), expect.objectContaining({ resource, parameters: { period: "FY2026" } }),
    ]));
    expect(submits[0]).toEqual({ ...inserted, nodes: inserted.nodes.map((node) => node.type === "reference" && node.resource.kind === "mcp_prompt"
      ? { ...node, parameters: { period: "FY2026" } } : node) });
    expect(requests.find((request) => request.exact_resource)?.exact_resource).toEqual(resource);
    expect(requests.length).toBeGreaterThan(1);
    expect(requests.every((request) => request.entry_mode === entryMode)).toBe(true);
  });
}

for (const width of [390, 1280]) {
  test(`refreshed catalog keeps compact source labels, readable reasons and useful empty states at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 850 });
    await installReferenceComponent(page);
    const data = response() as ReferenceCatalogResponse;
    Object.assign(data.entry_support[0], { adapter_id: "initial-agent-skill", adapter_version: 1, supported: true, reason_code: null });
    Object.assign(data.groups[0].items[0], { availability: "baseline", reason_code: null });
    data.groups[1].error = null;
    await page.route("**/api/v1/chat/references/catalog", (route) => route.fulfill({ json: data }));
    await page.goto("/__reference_component_test");
    await page.evaluate(() => window.referenceComponent.update({ locale: "en-US" }));
    await typeQuery(page);
    const options = page.getByRole("option");
    const first = options.first();
    await expect(first).toHaveAttribute("aria-disabled", "false");
    await expect(first).toContainText("Team Agent");
    await expect(first).toHaveAccessibleDescription("Available for this task");
    await expect(options.last().locator(".composer-reference-menu__reason")).toBeVisible();
    const geometry = await first.evaluate((element) => {
      const name = element.querySelector(".composer-reference-menu__name")!.getBoundingClientRect();
      const source = element.querySelector(".composer-reference-menu__source")!.getBoundingClientRect();
      return { height: element.getBoundingClientRect().height, nameTop: name.top, sourceTop: source.top };
    });
    expect(geometry.height).toBeGreaterThanOrEqual(44);
    expect(geometry.height).toBeLessThan(75);
    expect(Math.abs(geometry.nameTop - geometry.sourceTop)).toBeLessThan(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: info.outputPath("compact-reference-menu.png"), fullPage: true });
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("combobox")).toHaveText("@");
    await page.keyboard.press("Home");
    await page.keyboard.press("Enter");
    const selected = await page.evaluate(() => window.referenceComponent.changes.at(-1)!);
    expect(selected.nodes.filter((node) => node.type === "reference").map((node) => node.resource)).toEqual([data.groups[0].items[0].resource]);
    expect(await page.evaluate(() => window.referenceComponent.submits)).toEqual([]);
    data.groups[0].items = [];
    await page.getByRole("combobox").pressSequentially(" @missing");
    await expect(page.getByRole("status").filter({ hasText: "No matches found." })).toHaveCount(1);
    data.groups[0].error = "catalog_metadata_unavailable";
    await page.getByRole("combobox").pressSequentially("x");
    await expect(page.getByRole("option", { name: "Retry", exact: true })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: "could not" })).toBeVisible();
  });
}
