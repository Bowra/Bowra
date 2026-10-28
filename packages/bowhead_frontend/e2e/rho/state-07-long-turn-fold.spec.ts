/**
 * Sprint ρ Phase D3 — e2e State 07: long-turn auto-fold.
 *
 * Trigger ≥30 tool calls in one bucket; assert the production StepSummary appears.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 07 long-turn fold — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1&scenario=long");
    const input = page.locator('textarea, [role="textbox"]').first();
    await input.fill("跑 35 个查询");
    await input.press("Enter").catch(() => { });
    // The production folded disclosure appears once the turn completes.
    await expect(page.getByTestId("execution-disclosure").getByTestId("step-summary")).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: `test-results/rho-state-07-${vp.name}.png` });
  });
}
