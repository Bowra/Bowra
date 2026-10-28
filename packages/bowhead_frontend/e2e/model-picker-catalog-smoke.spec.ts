import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

test("composer model picker uses server catalog and can reset to team default", async ({ page }) => {
  await mockAuthedApp(page, {
    catalog: {
      version: 3,
      providers: [{
        id: "deepseek_anthropic",
        display_name: "DeepSeek",
        type: "anthropic_compatible",
        models: ["deepseek-v4-pro"],
        supports: { tool_use: true },
      }],
      default_model: { provider: "deepseek_anthropic", model: "deepseek-v4-pro" },
      kb: { active: {}, reindex: { state: "idle" } },
    },
  });

  await page.goto("/");
  await expect(page.getByRole("button", { name: /deepseek-v4-pro/i })).toBeVisible();

  await page.getByRole("button", { name: /deepseek-v4-pro/i }).click();
  await expect(page.getByRole("menuitemradio", { name: /使用团队默认|Use team default/i })).toBeVisible();
  await expect(page.getByRole("menuitemradio", { name: /deepseek-v4-pro/i })).toBeVisible();

  await page.getByRole("menuitemradio", { name: /deepseek-v4-pro/i }).click();
  await expect(page.getByRole("button", { name: /deepseek-v4-pro/i })).toBeVisible();

  await page.getByRole("button", { name: /deepseek-v4-pro/i }).click();
  await page.getByRole("menuitemradio", { name: /使用团队默认|Use team default/i }).click();
  await expect(page.getByRole("button", { name: /deepseek-v4-pro/i })).toBeVisible();
});
