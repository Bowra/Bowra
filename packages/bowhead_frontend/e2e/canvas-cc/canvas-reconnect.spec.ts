import { expect, test } from "@playwright/test";

/**
 * WP-1 (TD-2 / FR-1.3) — reconnect without double accumulation; real-browser evt_seq gate verification.
 *
 * Mock `::cut=N` sends N frames on first connection then disconnects without done → FE supervisor backs off and reconnects →
 * server fully replays → canvasGate held by attachStream drops replayed frames via evt_seq →
 * final narrative never doubles (F2 root fix). streaming-chunk accumulates narrative chunk by chunk,
 * reproducing the historical double-accumulation shape.
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

function narrativeMarkdown(page: import("@playwright/test").Page): Promise<string | null> {
  return page.evaluate(() => {
    const snap = (window as unknown as Record<string, () => { sections: Array<Record<string, unknown>> }>).__CANVAS_SNAPSHOT__();
    const sec = snap.sections.find((s) => s.id === "sec_stream_narr");
    return (sec?.markdown as string | undefined) ?? null;
  });
}

test("reconnect mid-stream → full replay dropped by gate → narrative not doubled", async ({ page }) => {
  await resetMock(page);
  await page.goto("/");
  // streaming-chunk event order: turn_started, opened, section_streaming, chunk, chunk, chunk,
  // section_done, canvas.done, final_answer, done. cut=5 disconnects **after the second chunk** on first connection
  // (review P1-1: old cut=3 preceded first chunk, never exercising a chunk seen initially then replayed, which gate must prevent).
  // Reconnect fully replays those 2 chunks; evt_seq gate must discard them or narrative accumulates twice.
  await sendMessage(page, "stream ::cut=5");

  // Wait for terminal state after reconnect: narrative converges to complete and **never doubled**.
  await expect
    .poll(async () => narrativeMarkdown(page), { timeout: 20_000 })
    .toBe("第一段。第二段。第三段。");

  // Extra safeguard: no paragraph may repeat (signature of double accumulation).
  const md = (await narrativeMarkdown(page)) ?? "";
  expect(md.match(/第一段。/g)?.length ?? 0).toBe(1);
  expect(md.match(/第三段。/g)?.length ?? 0).toBe(1);

  // Decisive de-hollow (review P1-1): lastDiff==0 proves **gate** blocked replayed chunks on reconnect,
  // narrative never accumulated twice; reconcile-on-done did not merely repair it afterward. Gate failure makes reconnect
  // double-accumulate → local narrative ≠ server → reconciliation detects drift>0 (then repairs, but lastDiff already records >0).
  await page.waitForTimeout(1_200);
  const stats = await page.evaluate(
    () => (window as unknown as Record<string, () => { lastDiff: number }>).__CANVAS_RECONCILE_STATS__(),
  );
  expect(stats.lastDiff).toBe(0);
});
