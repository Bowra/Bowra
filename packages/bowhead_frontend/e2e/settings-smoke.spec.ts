import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";

import { E2E_PERSONAL_APPEARANCE } from "./_app-mocks";

async function getBlockingAxeViolations(page: Page, target: string) {
  return page.evaluate(async (selector) => {
    const axeRunner = (window as unknown as {
      axe: {
        run: (
          target: string,
          options: unknown,
        ) => Promise<{
          violations: Array<{ impact: string | null; id: string }>;
        }>;
      };
    }).axe;
    const result = await axeRunner.run(selector, {});
    return result.violations.filter(
      (violation) =>
        violation.impact === "serious" || violation.impact === "critical",
    );
  }, target);
}

/** Smoke: settings entry and standalone settings pages are visible without crashing; mock mode avoids real backend calls. */
test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });

    if (path === "/api/v1/auth/me") {
      await json({
        id: "user_test",
        email: "test@example.com",
        display_name: "Test User",
        avatar_url: null,
        team_id: "team_test",
        tier: "pro",
        role: "admin",
        status: "active",
        email_verified: true,
        credit_balance: 25,
        credit_quota: 25,
        locale: "zh-CN",
        timezone: "Asia/Shanghai",
        is_team_admin: true,
        workspace_count: 1,
        needs_onboarding: false,
      });
      return;
    }

    if (path === "/api/v1/auth/me/workspaces") {
      await json({
        active_id: "ws_demo",
        items: [
          {
            workspace_id: "ws_demo",
            name: "Demo Workspace",
            description: "Playwright fixture workspace",
            role: "admin",
          },
        ],
      });
      return;
    }

    if (path === "/api/v1/theme/effective") {
      await json({
        effective: {
          version: 1,
          mode: "system",
          colorProfile: { type: "preset", id: "bowhead" },
          typographyProfile: { type: "preset", id: "bowhead" },
          typeScale: "standard",
          density: "comfortable",
          radius: "balanced",
          motion: "standard",
          chartPalette: "default",
        },
        locks: {},
        source: {
          mode: "product",
          colorProfile: "product",
          typographyProfile: "product",
          typeScale: "product",
          density: "product",
          radius: "product",
          motion: "product",
          chartPalette: "product",
        },
        userConfig: { version: 1 },
        team: {},
      });
      return;
    }

    if (path === "/api/v1/theme/personal") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { ETag: `"personal-v3:${"b".repeat(64)}"` },
        body: JSON.stringify(E2E_PERSONAL_APPEARANCE),
      });
      return;
    }

    if (path === "/api/v1/user-settings") {
      await json({ settings: {} });
      return;
    }

    if (path === "/api/v1/team-settings/theme-policy") {
      await json({ value: null });
      return;
    }

    if (path === "/api/v1/skills") {
      await json({
        builtin: [
          {
            name: "sample-analysis",
            title: "示例分析技能",
            description: "用于验证技能列表交互",
            version: "1.0.0",
            source_type: "builtin",
            enabled: true,
            size_bytes: 128,
            created_at: "2026-09-06T00:00:00Z",
            category: "analysis",
          },
        ],
        user_upload: [],
        user_generated: [],
      });
      return;
    }

    if (path === "/api/v1/team-skills") {
      await json({ items: [] });
      return;
    }

    if (path === "/api/v1/admin/routing") {
      await json({
        providers: ["openai_main"],
        bindings: [
          {
            agent_type: "lead",
            profile: null,
            primary_effective: ["openai_main", "gpt-4.1-mini"],
            primary_original: ["openai_main", "gpt-4.1-mini"],
            fallback: [],
            overridden: false,
          },
        ],
      });
      return;
    }

    // The authenticated root polls pending email confirmations on every page.
    if (path === "/api/v1/email-send-confirmations") {
      await json({ confirmations: [] });
      return;
    }

    // Built-in third-party tools (Tavily, Datarc, Power BI) are a list.
    if (path === "/api/v1/team-builtin-tools") {
      await json([]);
      return;
    }

    if (path === "/api/v1/admin/spaces") {
      await json({ items: [], next_cursor: null });
      return;
    }

    if (path.endsWith("/members")) {
      await json({ members: [] });
      return;
    }

    if (path.endsWith("/invites")) {
      await json({ invites: [] });
      return;
    }

    await json({});
  });
});

test("settings → spaces page shows the Space governance list", async ({
  page,
}) => {
  await page.goto("/team/settings/spaces/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: /空间|Spaces|Workspaces/i, level: 1 }),
  ).toBeVisible();
});

test("user menu → appearance renders controls and preview", async ({
  page,
}) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page
    .getByTestId("unified-sidebar")
    .getByRole("button", { name: /账户|Account/ })
    .click();
  await page
    .locator('[data-user-menu="true"]')
    .getByRole("menuitem", { name: /^(设置|Settings)$/ })
    .click();
  // Personal settings are route pages with links in the settings sidebar, not a dialog.
  await expect(page).toHaveURL(/\/settings\/profile\/?$/);
  await page.getByRole("link", { name: /^(外观|Appearance)$/ }).click();
  await expect(page).toHaveURL(/\/settings\/appearance\/?$/);
  const surface = page.getByRole("main");
  await expect(
    surface.getByRole("heading", { name: /外观|Appearance/, level: 1 }),
  ).toBeVisible();
  await expect(
    surface.getByRole("radio", { name: /跟随系统|System/i }),
  ).toBeVisible();
  await expect(
    surface.getByRole("button", { name: /海蓝|Ocean/i }),
  ).toBeVisible();
  await expect(
    surface.locator("strong").filter({ hasText: /当前效果|Current effect/i }),
  ).toBeVisible();
});

test("skills page renders without crash", async ({ page }) => {
  await page.goto("/skills", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("heading", { name: /Skills|技能/i, level: 1 }),
  ).toBeVisible();
  // The eyebrow is gone (Header, Language & Paging Spec §2c); the header names no space.
  await expect(page.locator(".ph-eyebrow")).toHaveCount(0);
  await expect(page.locator(".ui-page-header")).not.toContainText(
    /Current space|当前空间/i,
  );

  const analysisGroup = page.locator(".sgroup-toggle").filter({
    hasText: /分析方法|Analysis methods/i,
  });
  await expect(analysisGroup).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("示例分析技能")).toHaveCount(0);
  await page
    .getByRole("searchbox", { name: /搜索技能|Search skills/i })
    .fill("示例分析");
  await expect(page.getByText("示例分析技能")).toBeVisible();

  await expect(page.getByRole("button", { name: /清除搜索|Clear search/i })).toHaveCount(1);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(analysisGroup.locator(".gname")).toHaveCSS(
    "white-space",
    "nowrap",
  );
  await expect(analysisGroup.locator(".gcount")).toHaveCSS(
    "white-space",
    "nowrap",
  );
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);

  await page.addScriptTag({ content: axe.source });
  const seriousViolations = await getBlockingAxeViolations(page, ".sset");
  expect(seriousViolations).toEqual([]);
});

test("settings → tools reveals operation matches and keeps the create dialog accessible", async ({
  page,
}) => {
  await page.route("**/api/v1/team-tool-sources", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id: "source_e2e",
            team_id: "team_test",
            name: "CRM",
            base_url: "https://api.example.com",
            kind: "openapi",
            selected_op_ids: ["listCustomers", "uploadFile"],
            auth_type: "none",
            has_auth: false,
            visibility: "team_wide",
            status: "enabled",
            tools_count: 2,
            op_count: 2,
            created_at: "2026-09-01T00:00:00Z",
            updated_at: "2026-09-06T00:00:00Z",
          },
        ],
      }),
    });
  });
  await page.route("**/api/v1/team-tool-sources/presets", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    });
  });

  await page.goto("/team/settings/tools/", { waitUntil: "domcontentloaded" });
  const manager = page.getByTestId("tools-manager");
  await expect(
    manager.getByRole("heading", { name: /工具|Tools/i, level: 1 }),
  ).toBeVisible();
  await manager.getByRole("searchbox").fill("upload");
  await expect(
    manager.getByText(/命中 1 个操作|1 matching operations/i),
  ).toBeVisible();
  await expect(manager.getByText("uploadFile")).toBeVisible();
  await expect(manager.getByText("listCustomers")).toHaveCount(0);

  const expand = manager.getByRole("button", {
    name: /展开工具清单|Expand tool list/i,
  });
  await expect(expand).toHaveAttribute("aria-expanded", "false");
  await expand.click();
  await expect(
    manager.getByRole("button", { name: /收起工具清单|Collapse tool list/i }),
  ).toHaveAttribute("aria-expanded", "true");
  await expect(manager.getByText("listCustomers")).toBeVisible();

  await page.addScriptTag({ content: axe.source });
  const managerViolations = await getBlockingAxeViolations(
    page,
    '[data-testid="tools-manager"]',
  );
  expect(managerViolations).toEqual([]);

  await page.setViewportSize({ width: 390, height: 844 });
  const createTrigger = manager
    .getByRole("button", { name: /添加 MCP 工具|Add MCP tool/i })
    .first();
  await createTrigger.focus();
  await createTrigger.click();
  const dialog = page.getByRole("dialog", { name: /添加 MCP 工具|Add MCP tool/i });
  await expect(dialog).toBeVisible();
  await expect
    .poll(() =>
      dialog.evaluate((node) => node.contains(document.activeElement)),
    )
    .toBe(true);
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);

  const dialogViolations = await getBlockingAxeViolations(
    page,
    '[role="dialog"][aria-modal="true"]',
  );
  expect(dialogViolations).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(createTrigger).toBeFocused();
});

test("skills shared count follows approval, team status, and personal enablement", async ({
  page,
}) => {
  await page.route("**/api/v1/team-skills", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id: "oskill_enabled",
            name: "enabled-for-me",
            display_name: "Enabled for me",
            description: "approved and enabled at both layers",
            visibility: "team_wide",
            status: "enabled",
            verdict: "pass",
            size_bytes: 100,
            approval: "approved",
            my_enabled: true,
          },
          {
            id: "oskill_personal_off",
            name: "disabled-for-me",
            display_name: "Disabled for me",
            description: "team enabled but disabled by this user",
            visibility: "team_wide",
            status: "enabled",
            verdict: "pass",
            size_bytes: 100,
            approval: "approved",
            my_enabled: false,
          },
          {
            id: "oskill_pending",
            name: "pending-review",
            display_name: "Pending review",
            description: "not available until approved",
            visibility: "team_wide",
            status: "enabled",
            verdict: null,
            size_bytes: 100,
            approval: "pending",
            my_enabled: true,
          },
          {
            id: "oskill_team_off",
            name: "disabled-by-team",
            display_name: "Disabled by team",
            description: "approved but disabled by team governance",
            visibility: "team_wide",
            status: "disabled",
            verdict: "pass",
            size_bytes: 100,
            approval: "approved",
            my_enabled: true,
          },
        ],
      }),
    });
  });

  await page.goto("/skills", { waitUntil: "domcontentloaded" });
  const sharedGroup = page
    .locator(".sgroup")
    .filter({ hasText: /团队技能|Team skills/i });
  await expect(sharedGroup.locator(".gcount")).toHaveText(
    /启用 1\/4|1\/4 enabled/i,
  );

  await page.getByRole("button", { name: /只看已启用|Enabled only/i }).click();
  await expect(sharedGroup).toContainText("Enabled for me");
  await expect(sharedGroup).not.toContainText("Disabled for me");
  await expect(sharedGroup).not.toContainText("Pending review");
  await expect(sharedGroup).not.toContainText("Disabled by team");
});

test("mobile OAuth tool actions stay readable and share one row with availability", async ({
  page,
}) => {
  await page.route("**/api/v1/team-tool-sources", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id: "source_github",
            team_id: "team_test",
            name: "GitHub",
            base_url: "https://api.githubcopilot.com/mcp/",
            kind: "mcp",
            selected_op_ids: [],
            auth_type: "oauth",
            has_auth: false,
            visibility: "team_wide",
            status: "disabled",
            tools_count: 0,
            op_count: 0,
            created_at: "2026-09-06T00:00:00Z",
            updated_at: "2026-09-06T00:00:00Z",
          },
        ],
      }),
    });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/team/settings/tools/", { waitUntil: "domcontentloaded" });

  const authorize = page.getByRole("button", { name: /^(授权|Authorize)$/i });
  await expect(authorize).toBeVisible();
  await expect(authorize).toHaveCSS("white-space", "nowrap");
  // Secondary actions such as Validate live in the card's row actions menu.
  await page
    .getByRole("button", { name: /GitHub 的更多操作|More actions for GitHub/i })
    .click();
  await expect(
    page.getByRole("menuitem", { name: /^(验证|Validate)$/i }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menuitem", { name: /^(验证|Validate)$/i })).toHaveCount(0);

  const management = page.locator(".tool-source-card-management-actions");
  const availability = page.locator(".tool-source-card-availability");
  const [managementBox, availabilityBox] = await Promise.all([
    management.boundingBox(),
    availability.boundingBox(),
  ]);
  expect(managementBox).not.toBeNull();
  expect(availabilityBox).not.toBeNull();
  // Since the 09-29 mobile card refresh, availability sits left of the actions on one
  // row (toggle, then the management actions); they must not overlap.
  expect(availabilityBox!.x + availabilityBox!.width).toBeLessThanOrEqual(
    managementBox!.x,
  );
  expect(availabilityBox!.y).toBeLessThan(managementBox!.y + managementBox!.height);
  expect(managementBox!.y).toBeLessThan(availabilityBox!.y + availabilityBox!.height);
});
