import { expect, test } from "@playwright/test";

import {
  E2E_PERSONAL_APPEARANCE,
  mockAuthedApp,
} from "./_app-mocks";

const sessionUser = {
  id: "u_cold_start",
  email: "cold-start@example.com",
  display_name: "Cold Start",
  avatar_url: null,
  team_id: "team_cold_start",
  tier: "pro",
  role: "member",
  team_role: "member",
  status: "active",
  email_verified: true,
  credit_balance: 1_000,
  credit_quota: 10_000,
  locale: "zh-CN",
  timezone: "Asia/Singapore",
  is_team_admin: false,
  workspace_count: 1,
  needs_onboarding: false,
  capabilities: {
    workflow_creator: true,
    subagent_creator: true,
    access_requests: true,
  },
};

test("delayed identity keeps one stable shell and issues one /auth/me request", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockAuthedApp(page);
  await page.unroute("**/api/v1/auth/me");
  const startupApiRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/")) {
      startupApiRequests.push(url.pathname);
    }
  });

  let releaseIdentity = () => {};
  const identityGate = new Promise<void>((resolve) => {
    releaseIdentity = resolve;
  });
  let identityRequests = 0;
  await page.route("**/api/v1/auth/me", async (route) => {
    identityRequests += 1;
    await identityGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(sessionUser),
    });
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });

  const bootRoot = page.locator(".app-boot-root");
  const bootMain = page.locator(".app-boot-main");
  await expect(bootRoot).toBeVisible();
  await expect(page.locator(".app-boot-sidebar")).toBeVisible();
  await expect(page.locator(".unified-sidebar")).toHaveCount(0);
  await expect.poll(() => identityRequests).toBe(1);
  expect(startupApiRequests).toEqual(["/api/v1/auth/me"]);
  const bootMainBox = await bootMain.boundingBox();
  expect(bootMainBox).not.toBeNull();

  releaseIdentity();

  await expect(bootRoot).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "你的工作进展",
    }),
  ).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(".unified-sidebar")).toBeVisible();
  const productMainBox = await page
    .locator("#bowhead-main-content")
    .boundingBox();
  expect(productMainBox).not.toBeNull();
  expect(Math.abs(bootMainBox!.x - productMainBox!.x)).toBeLessThanOrEqual(1);
  expect(identityRequests).toBe(1);
  expect(
    await page.evaluate(() =>
      performance
        .getEntriesByName("bowhead_auth_resolution")
        .some((entry) => entry.duration >= 0),
    ),
  ).toBe(true);
});

for (const viewport of [
  // From 1024 the sidebar is expanded by default (248 wide from 1280).
  { name: "desktop", width: 1280, height: 800, mainX: 248 },
  { name: "mobile", width: 390, height: 844, mainX: 0 },
] as const) {
  test(`${viewport.name} fallback preserves the final main-column geometry`, async ({
    page,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await mockAuthedApp(page);
    await page.unroute("**/api/v1/auth/me");
    let releaseIdentity = () => {};
    const identityGate = new Promise<void>((resolve) => {
      releaseIdentity = resolve;
    });
    await page.route("**/api/v1/auth/me", async (route) => {
      await identityGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessionUser),
      });
    });

    await page.goto("/", { waitUntil: "domcontentloaded" });

    await expect(page.locator(".app-boot-root")).toBeVisible();
    if (viewport.width < 1024) {
      await expect(page.locator(".app-boot-sidebar")).toBeHidden();
      await expect(page.locator(".app-boot-mobile-bar")).toBeVisible();
    } else {
      await expect(page.locator(".app-boot-sidebar")).toBeVisible();
    }
    const bootMainBox = await page
      .locator(".app-boot-main")
      .boundingBox();
    expect(bootMainBox).not.toBeNull();
    expect(Math.abs(bootMainBox!.x - viewport.mainX)).toBeLessThanOrEqual(
      1,
    );

    releaseIdentity();

    await expect(page.locator(".app-boot-root")).toHaveCount(0);
    await expect(
      page.getByRole("heading", {
        level: 1,
        name: "你的工作进展",
      }),
    ).toBeVisible({ timeout: 20_000 });
    const productMainBox = await page
      .locator("#bowhead-main-content")
      .boundingBox();
    expect(productMainBox).not.toBeNull();
    expect(
      Math.abs(bootMainBox!.x - productMainBox!.x),
    ).toBeLessThanOrEqual(1);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  });
}

for (const theme of [
  {
    userMode: "dark",
    systemMode: "light",
    expectedBackground: "rgb(15, 23, 42)",
  },
  {
    userMode: "light",
    systemMode: "dark",
    expectedBackground: "rgb(244, 247, 251)",
  },
] as const) {
  test(`fallback follows explicit ${theme.userMode} theme over ${theme.systemMode} system preference`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: theme.systemMode });
    await page.addInitScript((userMode) => {
      localStorage.setItem(
        "bowhead.theme_config",
        JSON.stringify({ mode: userMode }),
      );
    }, theme.userMode);
    await mockAuthedApp(page);
    await page.unroute("**/api/v1/auth/me");
    let releaseIdentity = () => {};
    const identityGate = new Promise<void>((resolve) => {
      releaseIdentity = resolve;
    });
    await page.route("**/api/v1/auth/me", async (route) => {
      await identityGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessionUser),
      });
    });

    await page.goto("/", { waitUntil: "domcontentloaded" });

    const bootRoot = page.locator(".app-boot-root");
    await expect(bootRoot).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute(
      "data-theme",
      theme.userMode,
    );
    await expect(bootRoot).toHaveCSS(
      "background-color",
      theme.expectedBackground,
    );

    releaseIdentity();
    await expect(bootRoot).toHaveCount(0);
  });
}

test("fallback applies the cached custom appearance and identity before auth resolves", async ({
  page,
}) => {
  const baseScheme = E2E_PERSONAL_APPEARANCE.effective.colorScheme;
  const effectiveTheme = {
    schemaVersion: 2,
    brand: {
      applicationName: "Northstar",
      logoAlt: "Northstar",
      logoOnLight: { kind: "text", text: "Northstar" },
      logoOnDark: { kind: "text", text: "Northstar" },
      mark: { kind: "generatedInitial", text: "N" },
      favicon: { kind: "generatedInitial", text: "N" },
    },
    effective: {
      version: 1,
      mode: "light",
      colorProfile: { type: "preset", id: "bowhead" },
      colorScheme: {
        ...baseScheme,
        light: {
          ...baseScheme.light,
          background: "#F3F0FA",
          surface2: "#EAE5F5",
          line: "#CFC5E2",
          ink: "#221A32",
          ink3: "#6E617F",
          accent: "#365CF5",
          focus: "#365CF5",
        },
        checksum: `sha256:${"c".repeat(64)}`,
      },
      typographyProfile: { type: "preset", id: "system" },
      typeScale: "large",
      density: "spacious",
      radius: "soft",
      motion: "standard",
      chartPalette: "default",
    },
    locks: {},
    source: {
      mode: "user",
      colorProfile: "team_default",
      colorScheme: "team_default",
      typographyProfile: "team_default",
      typeScale: "user",
      density: "user",
      radius: "user",
      motion: "product",
      chartPalette: "product",
    },
    userConfig: {
      version: 1,
      mode: "light",
      typeScale: "large",
      density: "spacious",
      radius: "soft",
    },
    team: {
      id: "team_cold_start",
      themeRevision: 3,
      revisionId: "tbr_333333333333333333333333",
      configurationType: "custom",
    },
  };

  await page.addInitScript((response) => {
    localStorage.setItem(
      "bowhead.brand.active-team:v2",
      "team_cold_start",
    );
    localStorage.setItem(
      "bowhead.brand.bundle:v2:team_cold_start",
      JSON.stringify({
        version: 2,
        teamId: "team_cold_start",
        response,
      }),
    );
  }, effectiveTheme);
  await mockAuthedApp(page, { effectiveTheme });
  await page.unroute("**/api/v1/auth/me");
  let releaseIdentity = () => {};
  const identityGate = new Promise<void>((resolve) => {
    releaseIdentity = resolve;
  });
  await page.route("**/api/v1/auth/me", async (route) => {
    await identityGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(sessionUser),
    });
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });

  const bootRoot = page.locator(".app-boot-root");
  await expect(bootRoot).toBeVisible();
  await expect(bootRoot).toHaveCSS("background-color", "rgb(243, 240, 250)");
  await expect(page.locator(".app-boot-new")).toHaveCSS(
    "background-color",
    "rgb(234, 229, 245)",
  );
  await expect(page.locator(".app-boot-panel")).toHaveCSS(
    "border-radius",
    "20px",
  );
  await expect(page.locator(".app-boot-sidebar")).toHaveCSS(
    "padding-top",
    "20px",
  );
  await expect(page.locator(".app-boot-mark").first()).toHaveCSS(
    "background-color",
    "rgb(54, 92, 245)",
  );
  await expect(page.locator(".app-boot-wordmark").first()).toHaveText(
    "Northstar",
  );
  await expect(page.getByText("正在打开 Northstar")).toBeVisible();

  releaseIdentity();
  await expect(bootRoot).toHaveCount(0);
});

test("login stays outside authenticated theme and session APIs", async ({
  page,
}) => {
  let identityRequests = 0;
  let themeRequests = 0;
  await page.route("**/api/v1/auth/me", async (route) => {
    identityRequests += 1;
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "unauthorized" }),
    });
  });
  await page.route("**/api/v1/theme/effective", async (route) => {
    themeRequests += 1;
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "unauthorized" }),
    });
  });
  await page.route("**/api/v1/bootstrap/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        required: false,
        deployment_mode: "local",
        license_team_ids: ["team_cold_start"],
      }),
    });
  });

  await page.goto("/login/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  expect(identityRequests).toBe(0);
  expect(themeRequests).toBe(0);
});
