/**
 * Sprint ρ Phase D3 — e2e State 09: peer upraises HITL (3-frame sequence).
 *
 * Compute peer runs → upraises clarification → user picks → peer resumes.
 * Assert: the production ClarificationCard surfaces and peer identity persists.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 09 peer-upraise HITL — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1&scenario=peer-hitl");
    const input = page.locator('textarea, [role="textbox"]').first();
    await input.fill("拉一下竞品数据帮我对比");
    await input.press("Enter").catch(() => { });
    // Production ClarificationCard surfaces after the peer up-raise.
    const clarification = page.getByTestId("clarification-card").first();
    await expect(clarification).toBeVisible({ timeout: 15_000 });
    const peerIdentity = page.locator(".bh-step-list .agb-name").first();
    await expect(peerIdentity).toBeVisible();
    const identityBeforeResume = (await peerIdentity.textContent())?.trim();
    expect(identityBeforeResume).toBeTruthy();
    // Pick first option; peer resumes and the same configured identity remains.
    await clarification.locator("button").first().click();
    await expect(peerIdentity).toBeVisible();
    await expect(peerIdentity).toHaveText(identityBeforeResume!);
    await page.screenshot({ path: `test-results/rho-state-09-${vp.name}.png` });
  });
}
