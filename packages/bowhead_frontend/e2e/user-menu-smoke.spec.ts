import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * W11-11 e2e: UserMenu (W1-8)—avatar click opens popup with sign-out button.
 * After the main auth gate, smoke explicitly mocks authenticated state to cover normal user-menu behavior.
 */

test("user menu: avatar click opens popup", async ({ page }) => {
  await mockAuthedApp(page);
  await page.goto("/");
  // Wait for shared Topbar UserMenu to mount completely (avoid React hydration race).
  const avatar = page.locator(".v2-sb-avatar").first();
  await expect(avatar).toBeVisible();
  // Use evaluate dispatch to trigger React onClick directly (avoid Next.js dev-portal interception).
  await avatar.evaluate((btn) => (btn as HTMLButtonElement).click());
  await expect(page.locator('[data-user-menu="true"]')).toBeVisible({ timeout: 3000 });
  const items = page.locator('[data-user-menu="true"] [role="menuitem"]');
  expect(await items.count()).toBeGreaterThanOrEqual(1);
});

test("user menu: click outside closes popup", async ({ page }) => {
  await mockAuthedApp(page);
  await page.goto("/");
  const avatar = page.locator(".v2-sb-avatar").first();
  await expect(avatar).toBeVisible();
  await avatar.evaluate((btn) => (btn as HTMLButtonElement).click());
  await expect(page.locator('[data-user-menu="true"]')).toBeVisible();
  // Real pointer press far away from the menu: the shared menu primitive dismisses on pointerdown outside.
  await page.mouse.click(800, 500);
  await expect(page.locator('[data-user-menu="true"]')).not.toBeVisible();
});

test("release notes: direct navigation, account entry and About link", async ({ page }) => {
  await mockAuthedApp(page);
  await page.goto("/release-notes/");
  await expect(page.locator("article h1")).toHaveText(/更新说明|Release notes/);
  await expect(page.locator("article h2").first()).toBeVisible();
  // Match the existing smoke tests: the development-tools portal covers the account trigger.
  await page.locator(".v2-sb-avatar").first().evaluate((button) => (button as HTMLButtonElement).click());
  const entry = page.getByRole("menuitem", { name: /更新说明|Release Notes/ });
  await expect(entry).toHaveAttribute("href", "/release-notes/");
  await entry.click();
  await expect(page).toHaveURL(/\/release-notes\/?$/);
  await page.goto("/help/?article=about");
  await page.getByRole("link", { name: /更新说明|Release notes/ }).first().click();
  await expect(page).toHaveURL(/\/release-notes\/?$/);
  await expect(page.locator("article h1")).toHaveText(/更新说明|Release notes/);
});
