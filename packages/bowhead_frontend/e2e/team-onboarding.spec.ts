import { expect, test, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

const generatedAt = "2026-07-26T08:30:00Z";

function readinessItem({
  id,
  group,
  state,
  reasonCode,
  href,
  checks = [],
}: {
  id: string;
  group: "core" | "conditional" | "recommended";
  state: "missing" | "ready";
  reasonCode: string;
  href?: string;
  checks?: Array<{
    id: string;
    state: "missing" | "ready";
    required: boolean;
  }>;
}) {
  return {
    id,
    group,
    state,
    required: group === "core",
    required_scope: group === "core" ? "core" : group === "conditional" ? "capability" : null,
    satisfied: state === "ready",
    blocking: group === "core" && state !== "ready",
    reason_code: reasonCode,
    required_roles: ["owner", "team_admin"],
    can_act: true,
    observed_at: generatedAt,
    action: href ? { kind: "internal_link", href } : null,
    checks: checks.map((check) => ({
      ...check,
      satisfied: check.state === "ready",
      reason_code: check.state === "ready" ? "primary_model_configured" : reasonCode,
    })),
  };
}

const status = {
  schema_version: 1,
  team_id: "team_onboarding",
  milestone: "core_configuring",
  partial: false,
  generated_at: generatedAt,
  core: { satisfied: 2, total: 5 },
  items: [
    readinessItem({
      id: "license_active",
      group: "core",
      state: "ready",
      reasonCode: "license_active",
    }),
    readinessItem({
      id: "public_entry_ready",
      group: "core",
      state: "ready",
      reasonCode: "public_entry_ready",
      href: "/team/settings/public-entries/?from=setup",
    }),
    readinessItem({
      id: "llm_primary_configured",
      group: "core",
      state: "missing",
      reasonCode: "primary_model_missing",
      href: "/team/settings/models/?from=setup",
      checks: [
        { id: "llm_provider_present", state: "ready", required: true },
        {
          id: "llm_provider_credential_present",
          state: "ready",
          required: true,
        },
        { id: "llm_primary_selected", state: "missing", required: true },
        {
          id: "llm_primary_chat_capable",
          state: "missing",
          required: true,
        },
        {
          id: "llm_primary_tool_capable",
          state: "missing",
          required: true,
        },
      ],
    }),
    readinessItem({
      id: "space_ready",
      group: "core",
      state: "missing",
      reasonCode: "no_active_space",
      href: "/team/settings/spaces/?from=setup",
    }),
    readinessItem({
      id: "first_turn_succeeded",
      group: "core",
      state: "missing",
      reasonCode: "no_successful_turn",
      href: "/",
    }),
    readinessItem({
      id: "knowledge_base_ready",
      group: "conditional",
      state: "missing",
      reasonCode: "kb_embedding_missing",
      href: "/team/settings/models/?from=setup",
      checks: [
        { id: "kb_feature_licensed", state: "ready", required: true },
        { id: "kb_embedding_configured", state: "missing", required: true },
        { id: "kb_content_present", state: "missing", required: false },
      ],
    }),
    readinessItem({
      id: "datarc_connection_configured",
      group: "recommended",
      state: "missing",
      reasonCode: "datarc_connection_missing",
      href: "/team/settings/datasource/?from=setup",
    }),
    readinessItem({
      id: "llm_fallback_configured",
      group: "recommended",
      state: "missing",
      reasonCode: "fallback_model_missing",
      href: "/team/settings/models/?from=setup",
    }),
    readinessItem({
      id: "email_delivery_configured",
      group: "recommended",
      state: "missing",
      reasonCode: "email_not_configured",
      href: "/team/settings/integrations/?from=setup",
    }),
    readinessItem({
      id: "secondary_admin_present",
      group: "recommended",
      state: "missing",
      reasonCode: "secondary_admin_missing",
      href: "/team/settings/people/?from=setup",
    }),
  ],
  context: {
    team_name: "北辰研究院",
    current_role: "owner",
    active_spaces: 0,
    active_seats: 1,
    max_seats: 20,
    license_health: "active",
    license_expires_at: "2027-07-26T00:00:00Z",
    grace_until: null,
    current_public_entry: "https://bowhead.example/",
    account_creation_policy: "invite_only",
    auth_methods: ["password"],
    email_source: "none",
    kb_feature_licensed: true,
    optional_capabilities: {
      image_generation: "disabled",
      web_search: "disabled",
      primary_vision: "unknown",
      primary_think: "unknown",
    },
  },
};

const me = {
  id: "u_onboarding",
  email: "owner@example.com",
  name: "Owner",
  display_name: "Owner",
  team_id: "team_onboarding",
  team_role: "owner",
  is_team_admin: true,
  role: "admin",
  status: "active",
  email_verified: true,
  tier: "pro",
  credit_balance: 1_000,
  credit_quota: 10_000,
  needs_onboarding: false,
  capabilities: { team_onboarding: true },
};

async function mockOnboardingApp(page: Page) {
  const json = (body: unknown, responseStatus = 200) => ({
    status: responseStatus,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await mockAuthedApp(page);
  await page.unroute("**/api/v1/auth/me");
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill(json(me));
  });
  await page.route("**/api/v1/team-onboarding/status", async (route) => {
    await route.fulfill(json(status));
  });
  await page.route("**/api/v1/admin/llm-config", async (route) => {
    await route.fulfill(
      json({
        ok: true,
        version: 0,
        config: {
          schema_version: 2,
          providers: {},
          tiers: {},
          kb: { models: {}, active: {}, pending_embed: null },
        },
        providers: [],
        reindex: null,
      }),
    );
  });
}

test("setup presents one recommended action and returns from its source settings", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockOnboardingApp(page);
  await page.goto("/setup/");

  await expect(page.getByRole("heading", { level: 1, name: "完成团队启用" })).toBeVisible();
  await expect(page.getByText("2 / 5")).toBeVisible();
  await expect(page.getByRole("region", { name: "主要模型可以运行智能体" })).toBeVisible();
  await expect(page.getByTestId("primary-setup-action")).toHaveCount(1);
  await expect(page.getByRole("button", { name: /已完成的检查/ })).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: "能力配置与推荐项" })).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText(/已配置不等于实时验证/)).toBeVisible();
  const primaryBox = await page.getByTestId("primary-setup-action").boundingBox();
  expect(primaryBox!.y + primaryBox!.height).toBeLessThan(450);
  await page.screenshot({
    path: "test-results/team-onboarding-1440.png",
    fullPage: true,
  });

  await page.getByTestId("primary-setup-action").click();
  await expect(page).toHaveURL(/\/team\/settings\/models\/?\?from=setup$/);
  await expect(page.getByText("正在完成团队启用")).toBeVisible();
  await page.getByRole("button", { name: "返回启用状态" }).click();
  await expect(page).toHaveURL(/\/setup\/?$/);
  await expect(page.getByRole("heading", { level: 1, name: "完成团队启用" })).toBeVisible();
});

test("setup fills the wide application shell and centers its content", async ({ page }) => {
  const viewport = { width: 1857, height: 928 };
  await page.setViewportSize(viewport);
  await mockOnboardingApp(page);
  await page.goto("/setup/");

  const setupPage = page.getByRole("main");
  const frame = page.getByTestId("setup-frame");
  await expect(page.getByRole("heading", { level: 1, name: "完成团队启用" })).toBeVisible();

  const [pageBox, frameBox] = await Promise.all([setupPage.boundingBox(), frame.boundingBox()]);
  expect(pageBox).not.toBeNull();
  expect(frameBox).not.toBeNull();
  expect((pageBox?.x ?? 0) + (pageBox?.width ?? 0)).toBeCloseTo(viewport.width, 0);
  expect(frameBox?.width ?? 0).toBeLessThanOrEqual(1180);

  const leftInset = (frameBox?.x ?? 0) - (pageBox?.x ?? 0);
  const rightInset = (pageBox?.x ?? 0) + (pageBox?.width ?? 0) - (frameBox?.x ?? 0) - (frameBox?.width ?? 0);
  expect(Math.abs(leftInset - rightInset)).toBeLessThanOrEqual(1);
});

test("setup remains usable at 375px with keyboard and reduced motion", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockOnboardingApp(page);
  await page.goto("/setup/");

  const refresh = page.getByRole("button", { name: "刷新状态" }).first();
  await refresh.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByTestId("primary-setup-action")).toBeFocused();

  await page.screenshot({ path: "test-results/team-onboarding-375.png", fullPage: true });
  const model = page.locator('li[data-state="missing"]').filter({ has: page.getByRole("heading", { name: "主要模型可以运行智能体", exact: true }) });
  await model.getByText("查看说明", { exact: true }).click();
  await model.getByText("5 项检查", { exact: true }).click();
  await expect(page.getByText("工具调用能力", { exact: false })).toBeVisible();

  await page.getByRole("button", { name: "能力配置与推荐项" }).click();
  await expect(page.getByRole("heading", { name: "生产韧性" })).toBeVisible();
  await page.getByRole("button", { name: "团队信息" }).click();
  await expect(page.getByText("许可证状态", { exact: true })).toBeVisible();
  expect(await page.getByRole("progressbar").getAttribute("value")).toBe("2");
  const layout = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  await page.screenshot({
    path: "test-results/team-onboarding-375-expanded.png",
    fullPage: true,
  });
});

test("local activation hands the first admin through login into setup", async ({ page }) => {
  await page.addInitScript(() => {
    window.__BOWHEAD_BASE_URL__ = window.location.origin;
  });
  await mockOnboardingApp(page);

  let activated = false;
  await page.route("**/api/v1/bootstrap/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        required: !activated,
        deployment_mode: "local",
        license_team_ids: [],
      }),
    });
  });
  await page.route("**/api/v1/bootstrap/activate", async (route) => {
    activated = true;
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        user_id: "u_onboarding",
        max_seats: 20,
        next_route: "/setup",
      }),
    });
  });
  await page.route("**/api/v1/auth/login", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        user: {
          id: me.id,
          email: me.email,
          tier: me.tier,
        },
        token: "e2e-token",
      }),
    });
  });

  const authenticatedRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/auth/me") || request.url().includes("/api/v1/theme/effective")) {
      authenticatedRequests.push(request.url());
    }
  });

  await page.goto("/activate/");
  await expect(page.getByRole("heading", { level: 1, name: "激活 Bowhead" })).toBeVisible();
  expect(authenticatedRequests).toEqual([]);

  await page.getByLabel("授权文件内容").fill("signed-license-for-e2e");
  await page.getByLabel("团队名称").fill("北辰研究院");
  await page.getByLabel("管理员姓名").fill("Owner");
  await page.getByLabel("管理员邮箱").fill(me.email);
  await page.getByLabel("管理员密码").fill("Passw0rd!Passw0rd!");
  await page.getByRole("button", { name: "激活并创建管理员" }).click();

  await expect(page).toHaveURL(/\/login\/?\?next=%2Fsetup$/);
  await page.getByLabel("邮箱").fill(me.email);
  await page.getByLabel("密码", { exact: true }).fill("Passw0rd!Passw0rd!");
  await page.getByRole("button", { name: "登录", exact: true }).click();

  await expect(page).toHaveURL(/\/setup\/?$/);
  await expect(page.getByRole("heading", { level: 1, name: "完成团队启用" })).toBeVisible();
});

test("completed setup offers entry while keeping evidence available on demand", async ({ page }) => {
  await mockOnboardingApp(page);
  await page.goto("/setup/");
  await expect(page.getByTestId("primary-setup-action")).toBeVisible();
  const ready = structuredClone(status);
  ready.milestone = "core_ready";
  ready.core.satisfied = 5;
  ready.context.active_spaces = 1;
  const reasons: Record<string, string> = {
    llm_primary_configured: "primary_model_configured",
    space_ready: "space_ready",
    first_turn_succeeded: "successful_turn_exists",
  };
  for (const entry of ready.items) {
    if (entry.group === "core") {
      entry.state = "ready";
      entry.satisfied = true;
      entry.blocking = false;
      entry.reason_code = reasons[entry.id] ?? entry.reason_code;
    }
  }
  await page.route("**/api/v1/team-onboarding/status", (route) => route.fulfill({ json: ready }));
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "团队已就绪", exact: true })).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await expect(page.getByTestId("primary-setup-action")).toHaveText("进入 Bowhead");
  await expect(page.getByRole("button", { name: /已完成的检查/ })).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: "能力配置与推荐项" })).toHaveAttribute("aria-expanded", "false");
  await page.screenshot({ path: "test-results/team-onboarding-ready.png", fullPage: true });
  await page.getByRole("button", { name: /已完成的检查/ }).click();
  await expect(page.getByRole("heading", { name: "许可证处于可用状态" })).toBeVisible();
});
