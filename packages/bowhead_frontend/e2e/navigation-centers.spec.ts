import { expect, test, type Page } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";

async function prepare(page: Page, role = "member", marketplaceEnabled = true) {
  await mockAuthedApp(page);
  for (const target of [page, page.context()]) await target.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_centers",
        email: "centers@example.com",
        display_name: "Centers",
        avatar_url: null,
        team_id: "team_centers",
        tier: "pro",
        role: "user",
        team_role: role,
        status: "active",
        email_verified: true,
        capabilities: {
          subagent_creator: true,
          subagent_management_v2: marketplaceEnabled,
          personal_mcp: true,
          workflow_creator: true,
          credit: { credits_read: role === "owner" },
        },
        credit_balance: 1000,
        credit_quota: 10000,
        locale: "zh-CN",
        timezone: "UTC",
        is_team_admin: role === "owner",
        workspace_count: 1,
        needs_onboarding: false,
      }),
    }),
  );
  await page.route("**/api/v1/capability-catalog", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        tools: [],
        capabilities: [
          {
            id: "tcap_1",
            name: "Research",
            description: "Read sources",
            tool_count: 2,
            skill_count: 1,
          },
        ],
      }),
    }),
  );
  await page.route("**/api/v1/agents/catalog**", (route) => route.fulfill({ json: {
    kind: "agent_catalog_collection", schema_version: 1, items: [], collections: [],
    total_count: 0, facets: { capabilities: [], domains: [], jobs: [], team_tags: [] },
    actions: { create: true }, next_cursor: null,
  } }));
  await page.route("**/api/v1/agents/manage?**", (route) => route.fulfill({ json: {
    kind: "agent_management_collection", schema_version: 1,
    view: new URL(route.request().url()).searchParams.get("view"), items: [], total_count: 0, next_cursor: null,
    views: { hired: true, created: true, shared: true, archived: true, team: role === "owner" },
    actions: { create: true, manage_team_defaults: role === "owner" },
  } }));
}

async function openCapabilityOverview(page: Page) {
  const entry = page.getByTestId("unified-nav-capabilities");
  const isCompact = await entry.getAttribute("aria-haspopup") === "menu";
  await entry.click();
  if (isCompact) await page.getByRole("menuitem", { name: "能力中心", exact: true }).click();
}

test("member discovery and personal resources retain capability context on desktop", async ({
  page,
}) => {
  await prepare(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/capabilities/?type=capabilities");
  await expect(page.getByRole("heading", { name: "Research" })).toBeVisible();
  await expect(page.getByTestId("unified-sidebar").getByRole("navigation", { name: "主导航" })).toBeVisible();
  const center = page.getByTestId("capability-center");
  await expect(center.getByRole("link", { name: "团队管理", exact: true })).toHaveCount(0);
  await expect(page.getByTestId("unified-nav-capabilities_catalog")).toHaveAttribute("aria-current", "page");
  await page.getByTestId("unified-nav-capabilities").click();
  await center.getByRole("link", { name: /打开工具/ }).click();
  await expect(page).toHaveURL(/type=tools/);
  await expect(center.getByRole("list").locator("li").first()).toBeVisible();
  await center.getByRole("link", { name: "我的能力", exact: true }).click();
  await expect(page).toHaveURL(/view=my/);
  await expect(page.locator('a[href="/team/settings/tools/"]')).toHaveCount(0);
  await center.getByRole("link", { name: /打开我的智能体/ }).click();
  await expect(page).toHaveURL(/agents\/manage\/created\/?$/);
  await expect(page.getByTestId("unified-nav-my_agents")).toHaveAttribute("aria-current", "page");
});

test("compact capability directory retains personal destinations", async ({ page }) => {
  await prepare(page);
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.goto("/capabilities/?view=my");
  await page.getByRole("button", { name: "收起侧栏", exact: true }).click();
  await expect(page.getByTestId("unified-sidebar").getByRole("navigation", { name: "主导航" })).toBeVisible();
  await page.getByTestId("unified-nav-capabilities").click();
  await expect(page.getByRole("menuitem", { name: "我的连接", exact: true })).toHaveAttribute("href", "/connections/");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("unified-nav-capabilities")).toBeFocused();
  await page.getByTestId("unified-nav-capabilities").press("Enter");
  await page.getByRole("menuitem", { name: "工具", exact: true }).click();
  await expect(page).toHaveURL(/type=tools/);
  await page.getByTestId("unified-nav-capabilities").click();
  await expect(page.getByRole("menuitem", { name: "工具", exact: true })).toHaveAttribute("aria-current", "page");
});

test("phone drawer presents personal capabilities with the same ownership boundary", async ({
  page,
}) => {
  await prepare(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/capabilities/?view=my");
  await expect(page.getByTestId("capability-center")).toBeVisible();
  await page.getByTestId("mobile-topbar-hamburger").click();
  const drawer = page.getByTestId("sessions-drawer");
  await expect(drawer.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(drawer.getByTestId("unified-drawer-nav-scheduled")).toBeVisible();
  await expect(drawer.getByTestId("unified-drawer-nav-connections")).toHaveAttribute("href", "/connections/");
  await page.getByTestId("sessions-drawer-close").click();
  await page.goto("/agents/");
  await expect(page).toHaveURL(/agents\/manage\/hired\/?$/);
  await expect(page.getByRole("navigation", { name: "智能体管理视图" })).toBeVisible();
  await page.getByTestId("mobile-topbar-hamburger").click();
  const primary = drawer.locator('nav[aria-label="主导航"]');
  await expect(primary.locator(':scope > a, :scope > button, :scope > div > .unified-drawer-nav-group-row > :is(button,a):first-child')).toHaveText(["新工作", "我的报告", "资料库", "智能体", "能力中心"]);
  await expect(drawer.getByTestId("unified-drawer-nav-my_agents")).toHaveAttribute("aria-current", "page");
  await drawer.getByTestId("unified-drawer-nav-capabilities").click();
  await expect(page).toHaveURL(/\/capabilities\/?$/);
  await expect(page.getByTestId("sessions-drawer")).toHaveCount(0);
});

test("owner administration and settings have separate directories", async ({ page }) => {
  await prepare(page, "owner");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/admin/approvals/");
  await expect(
    page.locator('.unified-settings[data-context="administration"][data-surface="sidebar"]'),
  ).toBeVisible();
  await expect(page.getByTestId("settings-nav-admin:approvals")).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page.getByTestId("settings-nav-tab:profile")).toHaveCount(0);
  await page
    .getByTestId("unified-sidebar")
    .getByRole("button", { name: "账户", exact: true })
    .click();
  await expect(page.getByRole("menuitem", { name: "管理控制台", exact: true })).toHaveAttribute(
    "href",
    "/admin/",
  );
  await page.getByRole("menuitem", { name: "设置", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/profile\/?$/);
  await expect(
    page.locator('.unified-settings[data-context="settings"][data-surface="sidebar"]'),
  ).toBeVisible();
  await expect(page.getByTestId("settings-nav-tab:people")).toBeVisible();
  await expect(page.getByTestId("settings-nav-admin:approvals")).toHaveCount(0);
});

const agent = {
  kind: "agent_catalog_item", schema_version: 1, agent_id: "osa_navigation",
  display_name: "Navigation Agent", summary: "Explains monthly performance.",
  source: { kind: "team", owner_label: "Centers", owner_user_id: "u_centers" },
  facets: { domains: [], jobs: [], outputs: [] }, capabilities: [],
  risk: { level: "low", reasons: [] }, safety: { status: "pass", checked_subject: "release", reasons: [] },
  assignment: { status: "active", source: "user_hired", row_version: 1 },
  team_default: { enabled: false, policy_version: null, row_version: null },
  availability: { discoverable: true, hireable: false, runnable: true, reasons: [] },
  actions: { view_detail: true, hire: false, enable: false, disable: false, unhire: false, manage: false, manage_team_default: false, create: true },
  action_reasons: {}, relationship: "created", updated_label: { kind: "saved_at", at: "2026-10-02T00:00:00Z" }, manager_url: null,
};

test("Agent tabs and detail returns stay under the same sidebar resource", async ({ page }) => {
  await prepare(page, "owner");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route("**/api/v1/agents/manage?**", (route) => {
    const view = new URL(route.request().url()).searchParams.get("view");
    return route.fulfill({ json: {
      kind: "agent_management_collection", schema_version: 1, view,
      items: view === "archived" ? [] : [agent], total_count: view === "archived" ? 0 : 1, next_cursor: null,
      views: { hired: true, created: true, shared: true, archived: true, team: true }, actions: { create: true, manage_team_defaults: true },
    } });
  });
  await page.route("**/api/v1/agents/osa_navigation", (route) => route.fulfill({ json: agent }));
  await page.route("**/api/v1/agents/osa_navigation/capabilities?**", (route) => route.fulfill({ status: 404, json: {} }));
  await page.goto("/agents/manage/created/");
  await page.reload();
  await expect(page.getByRole("heading", { name: "Navigation Agent", exact: true })).toBeVisible();
  const activeResource = page.getByTestId("unified-nav-my_agents");
  for (const [name, path] of [["我聘用的", "hired"], ["我创建的", "created"], ["分享给我的", "shared"], ["已归档", "archived"], ["团队治理", "governance"]]) {
    const tab = page.getByRole("navigation", { name: "智能体管理视图" }).getByRole("link", { name, exact: true });
    await expect(tab).toHaveAttribute("href", `/agents/manage/${path}/`);
    await tab.click();
    await expect(page).toHaveURL(new RegExp(`/agents/manage/${path}/?$`));
    await expect(tab).toHaveAttribute("aria-current", "page");
    await expect(activeResource).toHaveAttribute("aria-current", "page");
  }
  await page.getByRole("navigation", { name: "智能体管理视图" }).getByRole("link", { name: "我创建的", exact: true }).click();
  await page.locator(".am-management-identity").click();
  await expect(page).toHaveURL(/return_view=created/);
  // Drill-down breadcrumb "‹ 我的智能体" (polish spec D1).
  await page.locator(".am-back").getByText("我的智能体", { exact: true }).click();
  await expect(page).toHaveURL(/manage\/created\/?$/);
  await expect(page.getByRole("navigation", { name: "智能体管理视图" }).getByRole("link", { name: "我创建的", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(activeResource).toHaveAttribute("aria-current", "page");
  await page.goBack();
  await expect(page).toHaveURL(/detail/);
  await page.goForward();
  await expect(page.getByRole("navigation", { name: "智能体管理视图" }).getByRole("link", { name: "我创建的", exact: true })).toHaveAttribute("aria-current", "page");
});

test("Tool source management returns to its catalog under Tools", async ({ page }) => {
  await prepare(page, "owner");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/capabilities/?type=tools");
  await page.getByRole("link", { name: "管理工具源", exact: true }).click();
  await expect(page).toHaveURL(/team\/settings\/tools/);
  await expect(page.getByTestId("unified-nav-tools_catalog")).toHaveAttribute("aria-current", "page");
  await page.getByRole("link", { name: /返回工具/ }).click();
  await expect(page).toHaveURL(/capabilities\/?\?type=tools/);
  await expect(page.getByTestId("unified-nav-tools_catalog")).toHaveAttribute("aria-current", "page");
});

test("Agent primary entry opens hired agents and keeps built-in configuration in the same area", async ({ page }) => {
  await prepare(page, "owner");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/reports/");
  const primary = page.getByTestId("unified-sidebar").getByRole("navigation", { name: "主导航", exact: true });
  await expect(primary.locator(':scope > a, :scope > button, :scope > div > .unified-sidebar-nav-group-row > :is(button,a):first-child')).toHaveText(["新工作", "我的报告", "资料库", "智能体", "能力中心"]);
  await page.getByTestId("unified-nav-agents").click();
  await page.waitForURL(/agents\/manage\/hired\/?$/);
  await expect(page.getByTestId("unified-nav-my_agents")).toHaveAttribute("aria-current", "page");
  const sidebar = page.getByTestId("unified-sidebar");
  await expect(sidebar).toHaveAttribute("data-presentation", "expanded");
  // Built-in configuration moved into the Agents header ⋯ (polish spec D2).
  await page.locator(".ui-page-header").getByRole("button", { name: "更多操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "管理内置智能体", exact: true }).click();
  await page.waitForURL(/team\/settings\/subagents/);
  await expect(page.getByTestId("unified-nav-agents").locator("..")).toHaveClass(/is-active/);
  // Built-in Agents are a team setting (D1): the breadcrumb leads to team settings; return to Agents from the sidebar.
  await expect(page.getByRole("link", { name: "团队设置", exact: true })).toHaveAttribute("href", "/team/settings/");
  await page.getByTestId("unified-nav-agents").click();
  await page.waitForURL(/agents\/manage\/hired\/?$/);
  await expect(page.getByTestId("unified-nav-my_agents")).toHaveAttribute("aria-current", "page");
  await expect(sidebar).toHaveAttribute("data-presentation", "expanded");
  await openCapabilityOverview(page);
  await page.waitForURL(/\/capabilities\/?$/);
});

test("existing Owner deep links keep their primary entry when the market is unavailable", async ({ page }) => {
  await prepare(page, "member", false);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route("**/api/v1/team-subagents/osa_owned?detail=manager", (route) => route.fulfill({ json: {
    kind: "team_owner_manager_detail", schema_version: 1, id: "osa_owned", definition_mode: "revisioned",
  } }));
  await page.goto("/agents/manage/?agent_id=osa_owned");
  const entry = page.getByTestId("unified-nav-agents");
  await expect(entry).toHaveAttribute("aria-current", "page");
  await expect(entry).toHaveAttribute("href", "/agents/manage/?agent_id=osa_owned");
  await expect(page.locator(".am-notice")).toContainText("尚未开放版本管理");
  await openCapabilityOverview(page);
  await expect(page).toHaveURL(/\/capabilities\/?$/);
});

for (const [resource, label, selected] of [
  ["tools", "工具源", "tools_catalog"],
  ["capabilities", "插件", "capabilities_catalog"],
  ["workflows", "团队流程", "skills"],
  ["subagents", "系统内置智能体", "agents"],
]) {
  test(`team overview returns from ${resource} management to the same view`, async ({ page }) => {
    await prepare(page, "owner");
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto("/capabilities/?view=team");
    const entry = page.getByTestId("capability-center").getByRole("link", { name: new RegExp(`打开${label}`) });
    await expect(entry).toHaveAttribute("href", `/team/settings/${resource}/?capability_return=team`);
    await entry.click();
    await expect(page).toHaveURL(new RegExp(`team/settings/${resource}/?\\?capability_return=team`));
    await expect(page.getByTestId(`unified-nav-${selected}`)).toHaveAttribute("aria-current", "page");
    await page.getByRole("link", { name: "← 返回团队管理", exact: true }).click();
    await expect(page).toHaveURL(/capabilities\/?\?view=team$/);
    await expect(page.getByTestId("unified-nav-capabilities")).toHaveAttribute("aria-current", "page");
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`team/settings/${resource}/?\\?capability_return=team`));
    await expect(page.getByRole("link", { name: "← 返回团队管理", exact: true })).toHaveAttribute("href", "/capabilities/?view=team");
    await page.goForward();
    await expect(page).toHaveURL(/capabilities\/?\?view=team$/);
  });
}

test("Skills management returns to the personal filter selected on the page", async ({ page }) => {
  await prepare(page, "owner");
  await page.route("**/api/v1/team-skills", (route) => route.fulfill({ json: { items: [] } }));
  await page.goto("/skills/");
  await page.locator(".sset").getByRole("button", { name: "我的", exact: true }).click();
  // The team-management link lives in the header ⋯ (Header, Language & Paging Spec D6).
  await page.locator(".ui-page-header").getByRole("button", { name: "更多操作", exact: true }).click();
  const entry = page.getByRole("menuitem", { name: "管理团队技能与流程", exact: true });
  await expect(entry).toHaveAttribute("href", "/team/settings/workflows/?capability_return=my_skills");
  await entry.click();
  await page.getByRole("link", { name: "← 返回我的技能", exact: true }).click();
  await expect(page).toHaveURL(/skills\/?\?scope=mine$/);
  await expect(page.locator(".sset").getByRole("button", { name: "我的", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("unified-nav-skills")).toHaveAttribute("aria-current", "page");
});

test("native management links preserve the overview in a new browser tab", async ({ page }) => {
  await prepare(page, "owner");
  await page.goto("/capabilities/?view=team");
  const link = page.getByTestId("capability-center").getByRole("link", { name: /打开工具源/ });
  const opened = page.context().waitForEvent("page");
  await link.click({ modifiers: ["ControlOrMeta"] });
  const popup = await opened;
  await expect(popup).toHaveURL(/team\/settings\/tools\/?\?capability_return=team$/);
  await expect(popup.getByRole("link", { name: "← 返回团队管理", exact: true })).toHaveAttribute("href", "/capabilities/?view=team");
  await popup.getByRole("link", { name: "← 返回团队管理", exact: true }).click();
  await expect(popup).toHaveURL(/capabilities\/?\?view=team$/);
  await popup.close();
  await expect(page).toHaveURL(/capabilities\/?\?view=team$/);
});

for (const width of [900, 390]) {
  test(`capability resource navigation remains reachable at width ${width}`, async ({ page }, testInfo) => {
    await prepare(page);
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/capabilities/?type=tools");
    await page.getByTestId("mobile-topbar-hamburger").click();
    const surface = width < 768 ? page.getByTestId("sessions-drawer") : page.getByTestId("unified-sidebar");
    const prefix = width < 768 ? "unified-drawer-nav" : "unified-nav";
    await expect(surface.getByRole("navigation", { name: "主导航" })).toBeVisible();
    await expect(surface.getByTestId(`${prefix}-tools_catalog`)).toHaveAttribute("aria-current", "page");
    await page.screenshot({ path: `/tmp/capability-organize-${width}-${testInfo.project.name}.png` });
    await surface.getByTestId(`${prefix}-capabilities_catalog`).click();
    await expect(page).toHaveURL(/capabilities\/?\?type=capabilities$/);
    await expect(page.getByRole("heading", { name: "Research", exact: true })).toBeVisible();
    if (width < 768) await expect(page.getByTestId("sessions-drawer")).toHaveCount(0);
  });
}

for (const width of [1440, 900, 390]) {
  test(`Agent sidebar pages and default destination at width ${width}`, async ({ page }) => {
    await prepare(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/agents/");
    await expect(page).toHaveURL(/agents\/manage\/hired\/?$/);
    const tabs = page.getByRole("navigation", { name: "智能体管理视图" });
    await expect(tabs.getByRole("link", { name: "我聘用的", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(tabs.getByRole("link", { name: "团队治理", exact: true })).toHaveCount(0);
    const prefix = width < 768 ? "unified-drawer-nav" : "unified-nav";
    const open = async () => {
      if (width < 1024) await page.getByTestId("mobile-topbar-hamburger").click();
    };
    await open();
    await expect(page.getByTestId(`${prefix}-agents`)).toHaveAttribute("href", "/agents/manage/hired/");
    for (const [id, path] of [["agent_marketplace", "marketplace"], ["agent_charts", "charts"], ["my_agents", "manage/hired"]]) {
      await expect(page.getByTestId(`${prefix}-${id}`)).toHaveAttribute("href", `/agents/${path}/`);
    }
    await page.getByTestId(`${prefix}-agent_marketplace`).click();
    await expect(page).toHaveURL(/agents\/marketplace\/?$/);
    await open();
    await expect(page.getByTestId(`${prefix}-agent_marketplace`)).toHaveAttribute("aria-current", "page");
    await page.getByTestId(`${prefix}-agent_charts`).click();
    await expect(page).toHaveURL(/agents\/charts\/?$/);
    await open();
    await expect(page.getByTestId(`${prefix}-agent_charts`)).toHaveAttribute("aria-current", "page");
    await page.getByTestId(`${prefix}-agents`).click();
    await expect(page).toHaveURL(/agents\/manage\/hired\/?$/);
    await expect(tabs.getByRole("link", { name: "我聘用的", exact: true })).toHaveAttribute("aria-current", "page");
    if (width === 1440) await page.screenshot({ path: "/tmp/agent-navigation-desktop.png" });
  });
}

test("Agent sidebar compact menu keeps all three destinations", async ({ page }) => {
  await prepare(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/agents/manage/hired/");
  await expect(page.getByRole("navigation", { name: "智能体管理视图" })).toBeVisible();
  await page.getByRole("button", { name: "收起侧栏", exact: true }).click();
  await page.getByTestId("unified-nav-agents").click();
  const menu = page.getByRole("menu", { name: /^智能体/ });
  await expect(menu.getByRole("menuitem")).toHaveCount(3);
  await expect(menu.getByRole("menuitem", { name: "市场", exact: true })).toHaveAttribute("href", "/agents/marketplace/");
  await expect(menu.getByRole("menuitem", { name: "我的智能体", exact: true })).toHaveAttribute("aria-current", "page");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("unified-nav-agents")).toBeFocused();
  await page.getByTestId("unified-nav-agents").press("Enter");
  await menu.getByRole("menuitem", { name: "市场", exact: true }).click();
  await expect(page).toHaveURL(/agents\/marketplace\/?$/);
});
