import { expect, test } from "@playwright/test";

/**
 * WP-0 smoke (canvas-comprehensive Batch 1) — prove acceptance infrastructure works end to end:
 *   1. Browser exposes window.__CANVAS_SNAPSHOT__ / __CANVAS_PROJECTION__ / __CANVAS_RECONCILE_STATS__
 *   2. Real server SSE (mock engine) drives a report turn → browser canvas store receives sections
 *   3. Drop one section_added frame → live store lacks a section, but by-session (reconciliation source) stays complete
 *
 * Run only with `canvas` profile (SSE_MODE=sse, against node mock server):
 *   BOWHEAD_E2E_PROFILE=canvas npx playwright test
 * This is prerequisite evidence for WP-2 reconciliation e2e; reconciliation assertions live in canvas-reconcile.spec.ts.
 */

async function resetMock(page: import("@playwright/test").Page) {
  await page.request.post("http://localhost:18080/api/v1/_test/reset");
}

// Send through composer (SSE mode really calls /chat/send → EventSource).
async function sendMessage(page: import("@playwright/test").Page, msg: string) {
  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(msg);
  await textarea.press("Enter");
}

test("WP-0: E2E hooks exposed on window", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  const hooks = await page.evaluate(() => ({
    snapshot: typeof (window as unknown as Record<string, unknown>).__CANVAS_SNAPSHOT__,
    projection: typeof (window as unknown as Record<string, unknown>).__CANVAS_PROJECTION__,
    stats: typeof (window as unknown as Record<string, unknown>).__CANVAS_RECONCILE_STATS__,
  }));
  expect(hooks.snapshot).toBe("function");
  expect(hooks.projection).toBe("function");
  expect(hooks.stats).toBe("function");
});

test("WP-0: report round drives canvas sections into the browser store", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  await sendMessage(page, "report");

  // Wait for browser canvas store to receive sections (accumulated by real SSE reducer).
  await expect
    .poll(
      async () =>
        page.evaluate(
          () =>
            (
              (window as unknown as Record<string, () => { sections: unknown[] }>).__CANVAS_SNAPSHOT__()
            ).sections.length,
        ),
      { timeout: 20_000 },
    )
    .toBeGreaterThanOrEqual(4);

  const proj = await page.evaluate(() =>
    (window as unknown as Record<string, () => Array<{ kind: string }>>).__CANVAS_PROJECTION__(),
  );
  expect(proj.map((p) => p.kind)).toContain("kpi_row");

  // de-hollow (review P1-3): prove live SSE reducer state is correct itself, not masked by reconciliation.
  // lastDiff==0 = by-session reconciliation finds no live difference = live path is healthy.
  await page.waitForTimeout(1_200);
  const stats = await page.evaluate(
    () => (window as unknown as Record<string, () => { count: number; lastDiff: number }>).__CANVAS_RECONCILE_STATS__(),
  );
  expect(stats.lastDiff).toBe(0);
});

test("WP-0: drop injection is server-side — by-session stays complete despite a dropped frame", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  // Inject ::drop via message → mock encodes it in stream_url and drops the first section_added SSE frame.
  // by-session (reconciliation source / persisted state) stays complete despite frame loss, prerequisite for WP-2 reconciliation healing.
  // (Live-session healing belongs to WP-2, see canvas-reconcile.spec.ts; here prove only the drop mechanism +
  //  complete by-session infrastructure.)
  await sendMessage(page, "report ::drop=canvas.section_added:1");
  await page.waitForTimeout(1_500);
  const serverDto = await page.request.get("http://localhost:18080/api/v1/canvas/by-session/s_e2e");
  const server = await serverDto.json();
  expect(server.sections.length).toBe(4);
});
