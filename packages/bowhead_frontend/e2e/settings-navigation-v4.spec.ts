import { expect, test, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

async function mockMember(page: Page, role = "member") {
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        id: "u_v4",
        email: "v4@example.com",
        display_name: "V4 User",
        avatar_url: null,
        team_id: "team_v4",
        tier: "pro",
        role: "user",
        team_role: role,
        status: "active",
        email_verified: true,
        capabilities: {},
        credit_balance: 1_000,
        credit_quota: 10_000,
        locale: "zh-CN",
        timezone: "Asia/Singapore",
        is_team_admin: role === "owner" || role === "team_admin",
        workspace_count: 1,
        needs_onboarding: false,
      }),
    });
  });
}

test.describe("personal settings navigation", () => {
  test.beforeEach(async ({ page }) => {
    await mockAuthedApp(page);
    await mockMember(page);
  });

  test("opens a route-backed personal settings page from the account menu", async ({
    page,
  }) => {
    await page.goto("/");
    await page
      .getByTestId("unified-sidebar")
      .getByRole("button", { name: /账户|Account/ })
      .click();
    await page
      .locator('[data-user-menu="true"]')
      .getByRole("menuitem", { name: /^(设置|Settings)$/ })
      .click();

    await expect(page).toHaveURL(/\/settings\/profile\/?$/);
    await expect(
      page.getByRole("dialog", { name: /^(个人设置|设置|Settings|Personal settings)$/i }),
    ).toHaveCount(0);
  });

  test("navigates each personal section by URL and keeps the memory deep link reachable", async ({
    page,
  }) => {
    await page.goto("/settings/profile/");
    await page.getByRole("link", { name: /记忆|Memory/ }).click();

    await expect(page).toHaveURL(/\/settings\/memory\/?$/);
    await expect(page.getByTestId("memory-v2")).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/\/settings\/profile\/?$/);
  });
});


async function mockSettingsScope(page: Page, teamRole: string, spaceRole: string) {
  await mockAuthedApp(page);
  await mockMember(page, teamRole);
  await page.route("**/api/v1/auth/me/workspaces", (route) => route.fulfill({ json: {
    items: [{ id: "ws_smoke", workspace_id: "ws_smoke", name: "Navigation Space", role: spaceRole }],
    active_id: "ws_smoke",
  } }));
  await page.route("**/api/v1/auth/capabilities", (route) => route.fulfill({ json: {
    deployment_mode: "saas", team_openapi: false,
  } }));
}

for (const surface of ["desktop", "phone", "compact"] as const) {
  test(`settings scopes follow Personal, Space, Team on ${surface}`, async ({ page }) => {
    await mockSettingsScope(page, "owner", "admin");
    await page.setViewportSize(surface === "phone" ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
    await page.goto("/settings/profile/");
    if (surface === "phone") await page.getByTestId("mobile-topbar-hamburger").click();
    if (surface === "compact") {
      await page.getByRole("button", { name: "收起侧栏", exact: true }).click();
      await expect.poll(() => page.locator('[data-testid^="settings-nav-group-"]').evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-testid")))).toEqual([
        "settings-nav-group-personal", "settings-nav-group-workspace", "settings-nav-group-team_people",
        "settings-nav-group-team_ai", "settings-nav-group-team_access", "settings-nav-group-team_appearance", "settings-nav-group-team_operations",
      ]);
      return;
    }
    const nav = page.locator(`.unified-settings[data-surface="${surface === "phone" ? "drawer" : "sidebar"}"]`);
    await expect(nav.locator('.unified-sidebar-section-label')).toHaveText([
      "个人", "空间 · Navigation Space", "团队 · 成员与空间", "团队 · AI 与数据", "团队 · 接入与安全", "团队 · 外观与策略", "团队 · 日常管理",
    ]);
    for (const tab of ["access-requests", "feedback-inbox", "openapi", "oidc", "license"]) {
      await expect(nav.getByTestId(`settings-nav-tab:${tab}`)).toHaveCount(0);
    }
  });
}

test("member deep links cannot reveal Team settings or unavailable features", async ({ page }) => {
  await mockSettingsScope(page, "member", "viewer");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/team/settings/people/");
  const nav = page.locator('.unified-settings[data-surface="sidebar"]');
  await expect(nav.locator('.unified-sidebar-section-label')).toHaveText(["个人", "空间 · Navigation Space"]);
  await expect(nav.getByTestId("settings-nav-tab:people")).toHaveCount(0);
  await expect(nav.getByTestId("settings-nav-tab:members")).toHaveCount(0);
  await expect(nav.getByTestId("settings-nav-tab:instructions")).toBeVisible();
  await page.goto("/settings/access-requests/");
  await expect(nav.getByTestId("settings-nav-tab:access-requests")).toHaveCount(0);
});
