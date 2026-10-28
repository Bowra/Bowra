import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * #88 HITL Tab-completion smoke, mocked mode—no dependency on actual backend history.
 */
test("tab completion ghost appears after enabling + typing", async ({ page }) => {
  await mockAuthedApp(page);
  await page.goto("/");
  // Seed localStorage: one history entry + enabled flag
  await page.addInitScript(() => {
    localStorage.setItem("bowhead.hitl_suggestions_enabled", "true");
    localStorage.setItem("bowhead.autocomplete.history.v1", JSON.stringify({
      texts: ["自定义 GMV 同比多少"],
      fetched_at: Date.now(),
    }));
  });
  await page.goto("/");

  const ta = page.locator(".v2-composer textarea").first();
  await ta.fill("自定义");
  // Ghost span contains "GMV 同比多少"
  await expect(page.getByTestId("composer-autocomplete-ghost")).toContainText("GMV 同比多少", { timeout: 3000 });
});

test("autocomplete respects disabled toggle", async ({ page }) => {
  await mockAuthedApp(page);
  await page.addInitScript(() => {
    localStorage.setItem("bowhead.hitl_suggestions_enabled", "false");
    localStorage.setItem("bowhead.autocomplete.history.v1", JSON.stringify({
      texts: ["自定义 GMV 同比多少"],
      fetched_at: Date.now(),
    }));
  });
  await page.goto("/");
  const ta = page.locator(".v2-composer textarea").first();
  await ta.fill("自定义");
  // Ghost must not appear
  await expect(page.getByTestId("composer-autocomplete-ghost")).toHaveCount(0);
});
