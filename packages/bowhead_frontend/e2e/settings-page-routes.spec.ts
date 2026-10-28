import { expect, test, type Page } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

const prefix = process.env.BOWHEAD_ROUTE_TEST_PREFIX ?? "";
const routePath = (path: string) => `${prefix}${path}`;

async function expectSettingsPath(page: Page, path: string) {
  await expect(page).toHaveURL((url) => url.pathname.replace(/\/$/, "") === routePath(path).replace(/\/$/, ""));
}

async function expectSpaceContent(page: Page, section: string) {
  if (section === "general") {
    await expect(page.locator("#space-general-name")).toHaveValue("Example Space");
    await expect(page.getByRole("combobox", { name: "周首日", exact: true })).toHaveValue("monday");
  } else if (section === "instructions") {
    await expect(page.locator("textarea")).toHaveValue("Explain your sources.");
  } else {
    await expect(page.getByText("Example Owner", { exact: true }).last()).toBeVisible();
  }
}

async function mockSettings(page: Page) {
  await mockAuthedApp(page, { subagentManagement: true });
  await page.route("**/api/v1/auth/me/workspaces", (route) =>
    route.fulfill({
      json: {
        items: [{ id: "ws_smoke", workspace_id: "ws_smoke", name: "Example Space", role: "admin" }],
        active_id: "ws_smoke",
      },
    }),
  );
  await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) =>
    route.fulfill({ json: { name: "Example Space", description: "Example description", revision: 1 } }),
  );
  await page.route("**/api/v1/workspace-settings/ws_smoke", (route) => route.fulfill({ json: { settings: {} } }));
  await page.route("**/api/v1/space-instructions/ws_smoke", (route) =>
    route.fulfill({
      json: {
        workspace_id: "ws_smoke",
        version: 1,
        content: "Explain your sources.",
        updated_by_name: "Owner",
        updated_at: "2026-10-19T00:00:00Z",
        max_chars: 10000,
        can_edit: true,
        archived: false,
        administrators: ["Owner"],
      },
    }),
  );
  await page.route("**/api/v1/space-instructions/ws_smoke/versions", (route) => route.fulfill({ json: { versions: [] } }));
  await page.route("**/api/v1/workspaces/ws_smoke/members", (route) =>
    route.fulfill({
      json: {
        members: [
          { user_id: "u_smoke", role: "admin", display_name: "Example Owner", email: "owner@example.test", joined_at: "2026-10-19T00:00:00Z" },
        ],
      },
    }),
  );
  await page.route("**/api/v1/workspaces/ws_smoke/member-candidates", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/admin/llm-config", (route) =>
    route.fulfill({
      json: {
        version: 1,
        features: { model_discovery_v2: true },
        config: {
          schema_version: 2,
          providers: {
            example: {
              type: "openai_compatible",
              base_url: "https://models.example.test/v1",
              models: [{ id: "example-chat", capabilities: ["chat", "tool"] }],
            },
          },
          tiers: { primary: { provider: "example", model: "example-chat" } },
          kb: { models: {}, active: {} },
        },
        providers: [],
      },
    }),
  );
  await page.route("**/api/v1/admin/llm-config/validate", (route) =>
    route.fulfill({ json: { valid: true, version: 1, issues: [], has_changes: false } }),
  );
  await page.route("**/api/v1/admin/llm-config/outbound-adaptations", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/admin/llm-config/verification-batches?**", (route) => route.fulfill({ json: { batches: [] } }));
}

for (const width of [412, 1440]) {
  test(`Space pages render on direct navigation and through the settings sidebar at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockSettings(page);
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    for (const [section, title] of [
      ["general", "Space 常规设置"],
      ["instructions", "空间说明"],
      ["members", "空间访问"],
    ]) {
      await page.goto(routePath(`/space/settings/${section}/`));
      await expect(page.getByRole("heading", { name: title, exact: true, level: 1 })).toBeVisible();
      await expectSpaceContent(page, section);
      await page.reload();
      await expect(page.getByRole("heading", { name: title, exact: true, level: 1 })).toBeVisible();
      await expectSpaceContent(page, section);
      if (width < 768)
        await page
          .getByRole("button", { name: /打开.*导航|打开侧栏|Open.*navigation|Open sidebar|菜单/ })
          .first()
          .click();
      const link = page.locator(`nav a[href$="/space/settings/${section}/"]`).first();
      await expect(link).toHaveAttribute("aria-current", "page");
      const next = section === "general" ? "instructions" : "general";
      await page.locator(`nav a[href$="/space/settings/${next}/"]`).first().click();
      await expectSettingsPath(page, `/space/settings/${next}/`);
      await expect(page.getByRole("heading", { name: next === "general" ? "Space 常规设置" : "空间说明", level: 1, exact: true })).toBeVisible();
      await expectSpaceContent(page, next);
    }
    await page.goto(routePath("/space/settings/"));
    await expectSettingsPath(page, "/space/settings/general/");
    await expectSpaceContent(page, "general");
    await page.goto(routePath("/team/settings/"));
    await expectSettingsPath(page, "/team/settings/people/");
    expect(errors).toEqual([]);
  });

  test(`model external capabilities and its editors use real page routes at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockSettings(page);
    await page.goto(routePath("/team/settings/models/"));
    await page.getByRole("tab", { name: "外部能力", exact: true }).click();
    await expectSettingsPath(page, "/team/settings/models/external/");
    await expect(page.getByRole("heading", { name: "外部能力", exact: true, level: 1 })).toBeVisible();
    await page.reload();
    await expect(page.getByText("图片生成", { exact: true })).toBeVisible();
    await expect(page.getByText("联网搜索", { exact: true })).toBeVisible();
    for (const [index, section, title] of [
      [0, "image", "图像生成配置"],
      [1, "search", "联网搜索配置"],
    ] as const) {
      await page.getByRole("button", { name: "配置", exact: true }).nth(index).click();
      await expectSettingsPath(page, `/team/settings/models/external/${section}/`);
      await expect(page.getByRole("heading", { name: title, exact: true, level: 1 })).toBeVisible();
      await page.getByRole("button", { name: "返回概览", exact: true }).click();
      await expectSettingsPath(page, "/team/settings/models/external/");
      await expect(page.getByRole("heading", { name: "外部能力", exact: true, level: 1 })).toBeVisible();
    }
  });
}
