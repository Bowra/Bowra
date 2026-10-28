import { expect, test } from "@playwright/test";

/**
 * W11-11 e2e: dark mode (W4-2)—set dark in localStorage + reload → persistence / inline bootstrap prevents flashing.
 */

// Cross-test isolation: clear storage at each test's start so dirty state cannot affect inline bootstrap.
test.beforeEach(async ({ context }) => {
  await context.clearCookies();
});

test("dark mode: localStorage persists across reload", async ({ page }) => {
  // This case validates the legacy localStorage fallback. Keep it isolated from
  // the shared mock server's mutable effective-theme state.
  await page.route("**/api/v1/theme/effective", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ ok: false }),
    });
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  // Default light (no dark attribute, or light)—flexible check
  const initialDark = await page.evaluate(
    () => document.documentElement.dataset.theme === "dark",
  );
  expect(initialDark).toBe(false);

  // Legacy storage is a reload-time fallback; the live mode control now lives in Appearance.
  await page.evaluate(() => {
    localStorage.setItem("bowhead.theme", "dark");
  });

  // Reload → inline bootstrap applies dark before hydration
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.documentElement.dataset.theme === "dark",
    null,
    { timeout: 2000 },
  );

  // cleanup
  await page.evaluate(() => localStorage.removeItem("bowhead.theme"));
});

test("theme config bootstrap applies custom attributes before hydration", async ({ page }) => {
  await page.route("**/api/v1/theme/effective", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        effective: {
          version: 1,
          mode: "dark",
          colorProfile: { type: "preset", id: "forest" },
          typographyProfile: { type: "preset", id: "readable" },
          typeScale: "large",
          density: "compact",
          radius: "sharp",
          motion: "reduced",
          chartPalette: "high-contrast",
        },
        locks: {},
        source: {
          mode: "user",
          colorProfile: "user",
          typographyProfile: "user",
          typeScale: "user",
          density: "user",
          radius: "user",
          motion: "user",
          chartPalette: "user",
        },
        userConfig: {
          version: 1,
          mode: "dark",
          colorProfile: { type: "preset", id: "forest" },
          typographyProfile: { type: "preset", id: "readable" },
          typeScale: "large",
          density: "compact",
          radius: "sharp",
          motion: "reduced",
          chartPalette: "high-contrast",
        },
        team: {},
      }),
    });
  });
  await page.addInitScript(() => {
    localStorage.setItem("bowhead.theme_config", JSON.stringify({
      mode: "dark",
      accent: "forest",
      typographyProfile: { type: "preset", id: "readable" },
      typeScale: "large",
      density: "compact",
      radius: "sharp",
      motion: "reduced",
      chartPalette: "high-contrast",
    }));
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");

  const attrs = await page.evaluate(() => ({
    theme: document.documentElement.dataset.theme,
    mode: document.documentElement.dataset.themeMode,
    accent: document.documentElement.dataset.themeAccent,
    colorProfile: document.documentElement.dataset.colorProfile,
    typographyProfile: document.documentElement.dataset.typographyProfile,
    typeScale: document.documentElement.dataset.typeScale,
    density: document.documentElement.dataset.themeDensity,
    radius: document.documentElement.dataset.themeRadius,
    motion: document.documentElement.dataset.themeMotion,
    chartPalette: document.documentElement.dataset.chartPalette,
    colorScheme: document.documentElement.style.colorScheme,
  }));

  expect(attrs).toEqual({
    theme: "dark",
    mode: "dark",
    accent: "forest",
    colorProfile: "forest",
    typographyProfile: "readable",
    typeScale: "large",
    density: "compact",
    radius: "sharp",
    motion: "reduced",
    chartPalette: "high-contrast",
    colorScheme: "dark",
  });

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.documentElement.dataset.themeAccent === "forest");
  await expect.poll(async () => page.evaluate(() => document.documentElement.dataset.theme)).toBe("dark");
});
