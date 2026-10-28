import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

test("home without a session does not show a follow-up queue load error after refresh", async ({ page }) => {
  await mockAuthedApp(page);
  let queueRequests = 0;
  await page.route(/\/api\/v1\/chat\/sessions\/[^/]+\/reference-queue/, async (route) => {
    queueRequests += 1;
    await route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "你的工作进展" })).toBeVisible();
  await expect(page.locator(".reference-follow-up-queue")).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name: "你的工作进展" })).toBeVisible();
  await expect(page.locator(".reference-follow-up-queue")).toHaveCount(0);
  expect(queueRequests).toBe(0);
});
