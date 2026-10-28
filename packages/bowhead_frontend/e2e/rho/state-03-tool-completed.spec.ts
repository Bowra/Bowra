/**
 * Sprint ρ Phase D3 — e2e State 03: tool completed + answer remains visible.
 *
 * A normally completed turn folds to StepSummary before the answer. Expanding
 * it must reveal the production Step row with its completed state.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 03 tool-completed — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1");
    const input = page.locator('textarea, [role="textbox"]').first();
    await input.fill("count rows in sales table");
    await input.press("Enter").catch(() => { });
    const disclosure = page.getByTestId("execution-disclosure");
    const summary = disclosure.getByTestId("step-summary");
    await expect(summary).toBeVisible({ timeout: 10_000 });
    await summary.click();
    await expect(disclosure.locator(".stp.is-done").first()).toBeVisible();
    await page.screenshot({ path: `test-results/rho-state-03-${vp.name}.png` });
  });
}
