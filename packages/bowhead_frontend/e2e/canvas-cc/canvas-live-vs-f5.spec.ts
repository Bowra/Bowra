import { expect, test } from "@playwright/test";

/**
 * WP-7 (FR-1.7b) — the main verification gate: "after live completion == after F5".
 *
 * Runtime A (live SSE reducer accumulates sections) vs runtime B (F5 → hydrate directly feeds the
 * server DTO) must produce the SAME normalized projection (diff==0). Self-diff, not
 * golden — section ids are uuid-ish but stable within a session. Projection exempts
 * rev/turn_id/superseded_by_turn (see canvas-projection.ts); this is exactly the
 * "displayed == persisted" invariant, machine-checked.
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
function projection(page: import("@playwright/test").Page): Promise<Array<Record<string, unknown>>> {
  return page.evaluate(
    () => (window as unknown as Record<string, () => Array<Record<string, unknown>>>).__CANVAS_PROJECTION__(),
  );
}
function sectionCount(page: import("@playwright/test").Page): Promise<number> {
  return page.evaluate(
    () => ((window as unknown as Record<string, () => { sections: unknown[] }>).__CANVAS_SNAPSHOT__()).sections.length,
  );
}

test("FR-1.7b: live report projection == F5-hydrate projection (diff==0)", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  await sendMessage(page, "report");
  await expect.poll(() => sectionCount(page), { timeout: 20_000 }).toBe(4);
  const live = await projection(page);

  // de-hollow (review P1-3): prove `live` is the correct state accumulated by the SSE reducer itself, rather than reconcile
  // overwriting it with by-session server state. lastDiff==0 means reconciliation changed nothing → comparing the two sides discriminates correctly.
  await page.waitForTimeout(1_200);
  const liveStats = await page.evaluate(
    () => (window as unknown as Record<string, () => { lastDiff: number }>).__CANVAS_RECONCILE_STATS__(),
  );
  expect(liveStats.lastDiff).toBe(0);

  // F5 on the session → the app hydrates canvas from GET /canvas/by-session (server DTO).
  await page.goto("/?session=s_e2e");
  await expect.poll(() => sectionCount(page), { timeout: 20_000 }).toBe(4);
  const afterF5 = await projection(page);

  // Normalized projections must be identical — same ids, kinds, order, contentHash.
  expect(afterF5).toEqual(live);
});
