/**
 * Sprint ρ Phase D3 — e2e State 10: historical turn compact.
 *
 * Replay a session with a ≥5min-old turn; assert HistoricalTurn 1-row
 * compact pill renders for it.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 10 historical compact — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1&scenario=historical");
    // HistoricalTurn pill is rendered for the replayed old turn
    await expect(page.locator('.rho-historical-turn').first()).toBeVisible({ timeout: 10_000 });
    // Click expands legacy body
    await page.locator('.rho-historical-turn button').first().click();
    await page.screenshot({ path: `test-results/rho-state-10-${vp.name}.png` });
  });
}
