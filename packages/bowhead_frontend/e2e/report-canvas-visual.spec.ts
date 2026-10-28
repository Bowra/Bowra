/**
 * report-refactor P1 visual self-check—/spike/p31 (real-canvas mock), full-page screenshots
 * at 1440×900 + 412×915, asserting 0 console errors (investigate dev-overlay "Issue").
 * Design reference: docs/sprints/report-refactor-2026-06-11/design/
 */

import { expect, test } from "@playwright/test";

const PNG_1X1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
  "base64",
);

const IGNORED_CONSOLE = [
  /Download the React DevTools/,
  /Image with src .* has either width or height modified/,
  /preloaded with link preload/,
  // Preexisting (before P1, store-layer useSyncExternalStore caching, not introduced by this sprint;
  // see implementation-map.md risk table).
  /getServerSnapshot should be cached/,
];

for (const vp of [
  { name: "desktop-1440", width: 1440, height: 900 },
  { name: "mobile-412", width: 412, height: 915 },
]) {
  test(`p31 visual @${vp.name}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const text = msg.text();
      if (IGNORED_CONSOLE.some((re) => re.test(text))) return;
      errors.push(text);
    });
    await page.setViewportSize({ width: vp.width, height: vp.height });
    // The editable design sample on /spike/p31 lazily loads its background art_000000000001,
    // which the mock backend does not have; serve a 1×1 PNG so the console check stays about
    // the report itself (same fixture as feishu-feedback-regression and editable-design).
    await page.route("**/api/v1/files/art_000000000001?**", (route) =>
      route.fulfill({ status: 200, contentType: "image/png", body: PNG_1X1 }),
    );
    await page.goto("/spike/p31");
    // citation-audit M3: gen2 `kind='num'` pill removed in this batch; waiting for it guarantees a 15s timeout.
    // gen3 `[N]` chips are now the only citation decoration.
    await page.waitForSelector("[data-citation-kind='numbered']", { state: "attached", timeout: 15000 });
    await page.waitForTimeout(800);
    await page.screenshot({
      path: `test-results/report-canvas-p1-${vp.name}.png`,
      fullPage: false,
    });
    // Render B1a source-linkage summary.
    await expect(page.getByTestId("source-linkage-strip")).toBeVisible();
    expect(errors, `console errors: ${errors.join("\n")}`).toEqual([]);
  });
}
