/**
 * Sprint ρ Phase D3 — e2e State 02: streaming (user just sent, RUNNING chip visible).
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 02 streaming — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1");
    // Send a query via composer (mock SSE).
    const input = page.locator('textarea, [role="textbox"]').first();
    await input.fill("show me Q1 sales by region");
    await input.press("Enter").catch(() => { /* fallback to button */ });
    // Expect a running agent block to surface within 5s.
    await expect(page.locator('[data-status="running"]').first()).toBeVisible({ timeout: 8000 });
    await page.screenshot({ path: `test-results/rho-state-02-${vp.name}.png` });
  });
}
