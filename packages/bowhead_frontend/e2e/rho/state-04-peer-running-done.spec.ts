/**
 * Sprint ρ Phase D3 — e2e State 04: peer subagent running → done collapsed.
 *
 * Assertion: at least one peer block (data-peer="1") that goes
 * running → done within the spec.
 */
import { test, expect } from "@playwright/test";

const VIEWPORTS = [
  { name: "mobile", width: 412, height: 915 },
  { name: "desktop", width: 1440, height: 900 },
];

for (const vp of VIEWPORTS) {
  test(`State 04 peer running→done — ${vp.name}`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto("/?rho=1&scenario=peer");
    const input = page.locator('textarea, [role="textbox"]').first();
    await input.fill("帮我分析下 Q3 营收的同比变化");
    await input.press("Enter").catch(() => { });
    // Peer block appears
    await expect(page.locator('[data-peer="1"]').first()).toBeVisible({ timeout: 10_000 });
    // Production done(completed) archives and folds in the same reducer update.
    // Re-open the archived disclosure before asserting the peer terminal.
    const summary = page.getByTestId("step-summary");
    await expect(summary).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('.bh-bot-block[data-archived="false"]')).toHaveCount(0);
    const archived = page.locator('.bh-bot-block[data-archived="true"]').first();
    // The folded summary owns the turn-level peer marker + completed glyph.
    await expect(archived.locator('[data-testid="step-summary"][data-peer="1"]')).toBeVisible();
    await expect(archived.locator('[data-peer="1"] [data-status="done"]')).toBeVisible();
    await summary.click();
    // Expanding replaces the summary button with the full Agent trace.
    await expect(archived.locator(".agb-name")).toContainText("营收分析专家");
    await page.screenshot({ path: `test-results/rho-state-04-${vp.name}.png` });
  });
}
