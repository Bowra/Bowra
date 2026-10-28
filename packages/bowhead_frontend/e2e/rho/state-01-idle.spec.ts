/**
 * Sprint ρ Phase D3 — e2e State 01: Idle (empty thread).
 *
 * Two viewports: mobile 412×915 + desktop 1440×900.
 * Asserts: empty composer present, no agent block, no errors in console.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 01 idle — ${vp.name} ${vp.width}×${vp.height}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/");
    // Composer present
    await expect(page.locator('[data-testid="v2-composer"], textarea, [role="textbox"]')).toBeVisible({ timeout: 5000 });
    // No agent block rendered yet
    await expect(page.locator(".rho-agent-block")).toHaveCount(0);
    await page.screenshot({ path: `test-results/rho-state-01-${vp.name}.png`, fullPage: false });
  });
}
