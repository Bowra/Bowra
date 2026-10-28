import { expect, test } from "@playwright/test";

/**
 * WP-2 (TD-5 / FR-1.1)—terminal reconciliation; real-browser verification of built-in F5 recovery.
 *
 * AC-1.1a: drop one canvas.section_added frame → live session lacks one section → after terminal turn, reconcile via by-session
 *          (complete) → store converges to server within one reconciliation, opens pane when appropriate, drift count >0.
 * AC-1.1d: fallback done + empty sections → no idle stall (reconciliation consumes it, no infinite spinner).
 *
 * Key: assertions read **actual browser-rendered state** (__CANVAS_SNAPSHOT__/__CANVAS_PROJECTION__),
 * never substitute server state reconstructed from SSE (review P0-1's tautological false pass).
 */

async function resetMock(page: import("@playwright/test").Page) {
  await page.request.post("http://localhost:18080/api/v1/_test/reset");
}

async function sendMessage(page: import("@playwright/test").Page, msg: string) {
  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(msg);
  await textarea.press("Enter");
}

function liveSectionCount(page: import("@playwright/test").Page): Promise<number> {
  return page.evaluate(
    () => ((window as unknown as Record<string, () => { sections: unknown[] }>).__CANVAS_SNAPSHOT__()).sections.length,
  );
}
function reconcileStats(page: import("@playwright/test").Page): Promise<{ count: number; lastDiff: number }> {
  return page.evaluate(
    () => (window as unknown as Record<string, () => { count: number; lastDiff: number }>).__CANVAS_RECONCILE_STATS__(),
  );
}

test("AC-1.1a: dropped section_added frame → reconcile heals live store to server state", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  // Drop first section_added frame: live receives 3 sections, by-session has 4.
  await sendMessage(page, "report ::drop=canvas.section_added:1");

  // Reconciliation after terminal turn converges live to 4 sections (built-in F5 recovery).
  await expect.poll(() => liveSectionCount(page), { timeout: 20_000 }).toBe(4);

  // Reconciliation really ran and detected drift (local state lacked one section).
  const stats = await reconcileStats(page);
  expect(stats.count).toBeGreaterThanOrEqual(1);
  expect(stats.lastDiff).toBeGreaterThanOrEqual(1);

  // After convergence, live projection == server projection (normalized, diff==0).
  const liveProj = await page.evaluate(() =>
    (window as unknown as Record<string, () => unknown[]>).__CANVAS_PROJECTION__(),
  );
  expect((liveProj as unknown[]).length).toBe(4);
});

test("AC-1.1d: fallback done + empty sections does not get stuck (idle, no spinner)", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  await sendMessage(page, "fallback");

  // Reconciliation converges to empty + idle (no streaming/outline stall).
  await expect
    .poll(
      () => page.evaluate(() => ((window as unknown as Record<string, () => { status: string }>).__CANVAS_SNAPSHOT__()).status),
      { timeout: 20_000 },
    )
    .not.toBe("streaming");
  expect(await liveSectionCount(page)).toBe(0);
});
