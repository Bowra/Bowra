import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * Smoke: the work home shows the brand once, in the sidebar (P2 batch 6 removed the home
 * watermark, wordmark and tagline). Mock mode; no backend dependency.
 */
test("home page shows the brand once, in the sidebar", async ({ page }) => {
  await mockAuthedApp(page);
  await page.goto("/");
  // Wait for the app to replace the boot skeleton, which carries its own wordmark.
  await expect(page.locator("textarea").first()).toBeVisible({ timeout: 10_000 });

  const brand = page.getByText("Bowhead", { exact: true });
  await expect(brand).toHaveCount(1);
  await expect(page.getByTestId("unified-sidebar").getByText("Bowhead", { exact: true })).toBeVisible();
});

test("home page mounts composer in empty state", async ({ page }) => {
  await mockAuthedApp(page);
  await page.goto("/");
  // Composer textarea visible alongside EmptyState
  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 10_000 });
});
