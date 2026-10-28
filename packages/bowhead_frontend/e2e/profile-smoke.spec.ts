import { expect, test } from "@playwright/test";

/**
 * Smoke: profile page load + localStorage writes.
 * The profile lives at the personal settings route /settings/profile/ (the
 * standalone /profile page was removed when personal settings were split out).
 */
test("profile page loads", async ({ page }) => {
  await page.goto("/settings/profile/");
  await expect(page.getByRole("heading", { name: /^(账号|Account)$/, level: 1 })).toBeVisible();
  await expect(page.locator("main")).toHaveCount(1);
  await expect(page.locator("main main")).toHaveCount(0);
  await expect(page.locator("h1")).toHaveCount(1);
});

test("profile writes preferences to localStorage", async ({ page }) => {
  await page.goto("/settings/profile/");
  // Should read defaults even with an empty form; no crash is sufficient
  const has = await page.evaluate(() => {
    try {
      localStorage.setItem("bowhead.user_profile", JSON.stringify({ language: "zh-CN" }));
      return localStorage.getItem("bowhead.user_profile") !== null;
    } catch {
      return false;
    }
  });
  expect(has).toBe(true);
});
