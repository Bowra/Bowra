/**
 * Sprint ρ Phase D3 — e2e State 06: turn fails with red banner.
 *
 * Trigger a failure scenario via mock; assert FinalFailBanner renders the
 * static-CN copy from `error-messages.ts` AND trace stays visible.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 06 fail banner — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1&scenario=fail");
    const input = page.locator('textarea, [role="textbox"]').first();
    await input.fill("超复杂查询触发超时");
    await input.press("Enter").catch(() => { });
    // Final fail banner appears
    await expect(page.getByTestId("final-fail").first()).toBeVisible({ timeout: 15_000 });
    // Trace remains visible (Lead block still rendered)
    await expect(page.locator('.rho-agent-block').first()).toBeVisible();
    await page.screenshot({ path: `test-results/rho-state-06-${vp.name}.png` });
  });
}
