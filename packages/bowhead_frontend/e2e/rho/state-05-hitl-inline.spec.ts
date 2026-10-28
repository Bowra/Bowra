/**
 * Sprint ρ Phase D3 — e2e State 05: HITL inline.
 *
 * User journey J1: send ambiguous query → see the production ClarificationCard
 * in the current turn → click candidate → next turn replays.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 05 HITL inline — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1&scenario=hitl");
    const input = page.locator('textarea, [role="textbox"]').first();
    await input.fill("最近几年的销售情况");
    await input.press("Enter").catch(() => { });
    // Production ClarificationCard surfaces.
    const clarification = page.getByTestId("clarification-card").first();
    await expect(clarification).toBeVisible({ timeout: 10_000 });
    // Pick option A
    await clarification.locator("button").first().click();
    // Next turn surfaces (running)
    await expect(page.locator('[data-status="running"]').first()).toBeVisible({ timeout: 10_000 });
    await page.screenshot({ path: `test-results/rho-state-05-${vp.name}.png` });
  });
}
