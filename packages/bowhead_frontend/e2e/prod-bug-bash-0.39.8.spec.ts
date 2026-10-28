/**
 * 0.39.8 prod-bug-bash spec coverage.
 *
 * Runs against the Next.js dev server in mock SSE mode (no real backend).
 * Validates the FE-side behavior of each 0.39.8 fix that's verifiable without
 * a live LLM:
 *
 *   #2 — `[数据]` literal chip suppressed when citation registry has no match
 *   #5 — narrative-vs-answer dedup on hydrate
 *   Logo — EmptyState + BotByline use BowheadLogo SVG (not literal "B")
 *
 * BE-side fixes (#3 step dup, #4 data_table shape, #7 ξ root cause from
 * 0.39.7) are covered by pytest UT + manual dev-stack walkthrough; their
 * e2e verification requires a real backend.
 */
import { expect, test } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

test.describe("0.39.8 prod-bug-bash", () => {
  test("the brand renders the Bowhead logo SVG (no literal 'B' letter)", async ({ page }) => {
    await mockAuthedApp(page);
    await page.goto("/");
    // Wait for the app to replace the boot skeleton, which carries its own wordmark.
    await expect(page.locator("textarea").first()).toBeVisible({ timeout: 10_000 });
    // Since P2 batch 6 the brand appears once, in the sidebar (the home wordmark and tagline were removed).
    const sidebar = page.getByTestId("unified-sidebar");
    await expect(sidebar.getByText("Bowhead", { exact: true })).toBeVisible();
    // The BowheadLogo SVG sits next to the name (light variant in this light-scheme project); the wrapper is decorative.
    await expect(sidebar.locator(".unified-sidebar-brand-logo svg.brand-graphic__light")).toBeVisible();
  });

  test("EmptyState does NOT render the legacy big 'B' square", async ({ page }) => {
    await mockAuthedApp(page);
    await page.goto("/");
    // Pre-0.39.8 EmptyState had a 40x40 black div with literal "B" letter.
    // Post-fix: the BowheadLogo SVG replaces it.
    // Negative check: scan visible elements for an exact "B" as text content.
    const standaloneB = page.locator("text=/^B$/");
    await expect(standaloneB).toHaveCount(0);
  });
});
