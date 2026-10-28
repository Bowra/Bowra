import { expect, test, type Page, type Route } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const healthyConfig = {
  source: "db",
  tenant_key: "",
  app_type: "internal",
  enabled: true,
  event_ingress: "webhook",
  app_id: "cli_smoke",
  bot_open_id: "ou_bot",
  group_public_answers_enabled: false,
  app_secret_configured: true,
  verification_token_configured: true,
  encrypt_key_configured: true,
  secret_sources: {
    app_secret: "db",
    verification_token: "db",
    encrypt_key: "db",
  },
  updated_at: null,
  updated_by_user_id: null,
};

const healthyDiagnostics = {
  ok: true,
  enabled: true,
  degraded: false,
  app_type: "internal",
  event_ingress: "webhook",
  group_public_answers_enabled: false,
  auth_mode: "oidc",
  prod_auth_mode: true,
  long_connection: null,
  missing_credentials: [],
  missing_config: [],
  app_id_configured: true,
  app_secret_configured: true,
  verification_token_configured: true,
  encrypt_key_configured: true,
  bot_open_id_configured: true,
  event_store_configured: true,
  event_subscription_url: "https://example.com/api/v1/integrations/feishu/events",
  card_callback_url: "https://example.com/api/v1/integrations/feishu/card-callback",
  tenant_token_cache_keys: [],
  required_event_subscriptions: ["im.message.receive_v1"],
  required_permissions: ["im:message"],
  setup_checklist: [
    {
      key: "app_id",
      label: "APP_ID",
      status: "ok",
      detail: "Configured",
    },
    {
      key: "console",
      label: "飞书控制台",
      status: "manual",
      detail: "MANUAL_CONSOLE_MARKER",
    },
  ],
  launch_record_template: "# Launch record",
};

async function asAdmin(page: Page): Promise<void> {
  await page.route("**/api/v1/auth/me", async (route: Route) => {
    await route.fulfill(
      json({
        id: "u_admin",
        email: "admin@example.com",
        name: "Admin",
        tier: "ultra",
        credit_balance: 1_000,
        credit_quota: 10_000,
        is_team_admin: true,
      }),
    );
  });
}

async function mockFeishu(
  page: Page,
  state: "stopped" | "healthy",
): Promise<void> {
  const stopped = state === "stopped";
  await page.route(
    "**/api/v1/integrations/feishu/health",
    async (route: Route) => {
      await route.fulfill(
        json({
          ok: true,
          enabled: !stopped,
          degraded: false,
        }),
      );
    },
  );
  await page.route(
    "**/api/v1/integrations/feishu/admin/diagnostics",
    async (route: Route) => {
      await route.fulfill(
        json(
          stopped
            ? {
                ...healthyDiagnostics,
                enabled: false,
                missing_credentials: [
                  "app_id",
                  "app_secret",
                  "verification_token",
                  "bot_open_id",
                ],
                app_id_configured: false,
                app_secret_configured: false,
                verification_token_configured: false,
                bot_open_id_configured: false,
              }
            : healthyDiagnostics,
        ),
      );
    },
  );
  await page.route(
    "**/api/v1/integrations/feishu/admin/config",
    async (route: Route) => {
      await route.fulfill(
        json(
          stopped
            ? {
                ...healthyConfig,
                source: "env",
                enabled: false,
                app_id: "",
                bot_open_id: "",
                app_secret_configured: false,
                verification_token_configured: false,
              }
            : healthyConfig,
        ),
      );
    },
  );
}

test.beforeEach(async ({ page }) => {
  await mockAuthedApp(page);
  await asAdmin(page);
});

test("integration overview keeps stopped, readiness, and runtime states independent", async ({
  page,
}) => {
  await mockFeishu(page, "stopped");
  // Since the 09-16 overview redesign, the overview reads per-provider team
  // connection state; the readiness detail lives on the Feishu page below.
  await page.route(
    "**/api/v1/team-settings/integrations/overview",
    async (route: Route) => {
      await route.fulfill(
        json({
          items: [
            {
              provider: "feishu",
              available: true,
              configured: true,
              enabled: false,
              healthy: false,
            },
          ],
        }),
      );
    },
  );

  await page.goto("/team/settings/integrations/");

  await expect(
    page.getByRole("heading", { level: 1, name: "集成" }),
  ).toBeVisible({ timeout: 30_000 });
  // A stopped integration is not listed as connected, keeps its saved
  // configuration state, and does not claim a healthy runtime.
  await expect(
    page.getByText("当前团队还没有已启用且运行正常的集成。"),
  ).toBeVisible();
  const feishuCard = page
    .locator("article")
    .filter({ hasText: "在飞书私聊或群聊中与 Bowhead 对话。" });
  await expect(feishuCard).toContainText("已保存配置，可继续完成接入。");
  await expect(page.getByText("运行正常", { exact: true })).toHaveCount(0);
  await expect(page.getByText("自动检查已通过")).toHaveCount(0);

  await feishuCard.getByRole("button", { name: /开始接入/ }).click();
  await expect(page).toHaveURL(/\/team\/settings\/integrations\/feishu\/?$/, {
    timeout: 30_000,
  });
  await expect(
    page.getByRole("list", { name: "飞书集成当前状态" }),
  ).toContainText("需要配置 · 4 项待处理");
  await expect(page.getByText("下一步 / 待处理")).toBeVisible();
  await expect(page.getByRole("table", { name: "主开关与接入方式规则" })).toBeVisible();
});

test("automatic diagnostics exclude manual console checks and remain usable on mobile", async ({
  page,
}) => {
  await mockFeishu(page, "healthy");
  await page.setViewportSize({ width: 390, height: 844 });

  await page.goto("/team/settings/integrations/feishu/?stage=03");

  const readyChecks = page.getByRole("button", { name: "已就绪 · 1 项" });
  await expect(readyChecks).toBeVisible({ timeout: 30_000 });
  await expect(readyChecks).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("MANUAL_CONSOLE_MARKER")).toHaveCount(0);
  await readyChecks.click();
  await expect(readyChecks).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText(/Configured/)).toBeVisible();
  await expect(page.getByText("MANUAL_CONSOLE_MARKER")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "待人工完成" })).toBeVisible();
  await expect(page.locator("details").nth(2).locator("summary")).toBeInViewport();

  await page.evaluate(() => window.scrollTo(0, 0));
  const [nextStepsBox, firstStageBox] = await Promise.all([
    page.getByRole("heading", { name: "待人工完成" }).boundingBox(),
    page.locator("details").first().boundingBox(),
  ]);
  expect(nextStepsBox).not.toBeNull();
  expect(firstStageBox).not.toBeNull();
  expect(nextStepsBox!.y).toBeLessThan(firstStageBox!.y);
});

test("stopped mobile deep links focus the requested stage and remain skippable", async ({
  page,
}) => {
  await mockFeishu(page, "stopped");
  await page.setViewportSize({ width: 390, height: 844 });

  await page.goto("/team/settings/integrations/feishu/?stage=02");

  const consoleStageDetails = page.locator("details").nth(1);
  const consoleStage = consoleStageDetails.locator("summary");
  await expect(consoleStage).toBeVisible({ timeout: 30_000 });
  await expect(consoleStage).toBeInViewport();
  await expect(consoleStage).toBeFocused();
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0);

  const quickExit = page.getByTestId("stage-02-quick-exit");
  await expect(quickExit).toBeVisible();
  await expect(quickExit).toBeInViewport();
  await expect(
    consoleStageDetails
      .locator("div[class*='actionBar']")
      .getByRole("button", { name: "暂时跳过" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "打开飞书开放平台" }),
  ).toHaveAttribute("href", "https://open.feishu.cn/app");
  await expect(
    page.getByRole("link", { name: "打开 Lark Developer Console" }),
  ).toHaveAttribute("href", "https://open.larksuite.com/app");
  await expect(
    page.getByRole("button", { name: "复制事件回调" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "复制卡片回调" }),
  ).toBeVisible();
});

test("desktop deep links keep stage navigation available while scrolling", async ({
  page,
}) => {
  await mockFeishu(page, "stopped");
  await page.setViewportSize({ width: 1280, height: 720 });

  await page.goto("/team/settings/integrations/feishu/?stage=02");

  const consoleStage = page.locator("details").nth(1);
  await expect(consoleStage.locator("summary")).toBeVisible({
    timeout: 30_000,
  });
  await expect(consoleStage.locator("summary")).toBeInViewport();
  await expect(
    consoleStage.getByRole("button", { name: "暂时跳过" }),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "配置阶段" }),
  ).toBeInViewport();
  const [sideColumnBox, settingsContentBox] = await Promise.all([
    page.locator("aside[class*='sideColumn']").boundingBox(),
    page.locator(".settings-content").boundingBox(),
  ]);
  expect(sideColumnBox).not.toBeNull();
  expect(settingsContentBox).not.toBeNull();
  expect(sideColumnBox!.y + sideColumnBox!.height).toBeLessThanOrEqual(
    settingsContentBox!.y + settingsContentBox!.height + 0.5,
  );
});
