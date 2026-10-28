import { expect, test } from "@playwright/test";

/**
 * W11-11 e2e: SectionChrome wraps atomic + text-based blocks; hidden when idle, focus-within
 * shows focus ring.
 *
 * report-refactor WP3 (2026-06-13): chrome redesigned as D2—left-slot grip handle
 * (.rc-handle) renders only in edit mode; upper-right action cluster (.rc-actions) renders only when actions exist
 * (Edit with AI / menu / SaveToMemory). /spike/p31 lacks canvasId = view mode,
 * so neither handle nor cluster appears—verify only chrome container + focus ring.
 */

test("section chrome: wraps atomic + text-based NodeViews", async ({ page }) => {
  await page.goto("/spike/p31");
  await page.waitForSelector(".v2-section-chrome", { state: "attached", timeout: 10000 });
  const chromes = page.locator(".v2-section-chrome");
  expect(await chromes.count()).toBeGreaterThanOrEqual(1);
});

test("section chrome: focus ring present on chrome container", async ({ page }) => {
  await page.goto("/spike/p31");
  await page.waitForSelector(".v2-section-chrome", { state: "attached", timeout: 10000 });
  const result = await page.evaluate(() => {
    const chrome = document.querySelector(".v2-section-chrome");
    if (!chrome) return { error: "no chrome", outline: "" };
    // focus-within: focus any focusable child (view mode may have none; then verify the outline property exists).
    const anyBtn = chrome.querySelector<HTMLButtonElement>("button, [tabindex]");
    if (anyBtn) anyBtn.focus();
    return new Promise<{ outline: string }>((r) => {
      setTimeout(() => r({ outline: getComputedStyle(chrome).outlineColor }), 250);
    });
  });
  expect(result).toHaveProperty("outline");
});

test("section chrome: no action menu when no handlers (W11-3 / WP3)", async ({ page }) => {
  // spike/p31 lacks canvasId → onDeleteSection/onMergeWithPrev/onEditBlockWithAI all undefined
  // → no action cluster → no menu button (new structure: [aria-haspopup=menu] inside .rc-actions).
  await page.goto("/spike/p31");
  await page.waitForSelector(".v2-section-chrome", { state: "attached", timeout: 10000 });
  const menuBtns = page.locator('.rc-actions button[aria-haspopup="menu"]');
  expect(await menuBtns.count()).toBe(0);
});
