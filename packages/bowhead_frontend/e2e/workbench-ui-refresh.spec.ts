import { expect, test, type Locator, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * Chat workbench/report canvas batch 2 (workbench appearance): key visuals and layers in real browsers.
 * Runs in Chromium and WebKit (CROSS_BROWSER_SPECS). Mock scenarios only, no real chat requests.
 * Running band retains three rows (PRD A3), only appearance changes; completed summary/failure bar share a frame; latest-answer actions stay visible,
 * icon buttons are 28 square and uncovered; clarification free-input fields are not flattened in narrow cards.
 */

async function open(page: Page, url: string, width = 1440) {
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await page.goto(url);
}

/** Center-point hit is the element itself (or its descendant)—uncovered by other layers. */
async function hitsItself(target: Locator): Promise<boolean> {
  return target.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return Boolean(hit && (hit === el || el.contains(hit)));
  });
}

/** Mock timeline advances via setTimeout: swallow timers after 1.5 seconds to keep this turn running. */
async function freezeRunning(page: Page) {
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = ((fn: TimerHandler, delay?: number, ...args: unknown[]) =>
      typeof delay === "number" && delay > 1500 && delay < 30_000 ? 0 : original(fn, delay, ...args)) as typeof window.setTimeout;
  });
}

test("running band keeps its three-row structure (40px disclosure bar + two history rows + current row); Stop is not accent-filled", async ({ page }) => {
  await freezeRunning(page);
  await open(page, "/?scenario=peer");
  const bar = page.locator(".bh-band__bar").first();
  await expect(bar).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".bh-band__hist").first()).toBeVisible();
  await expect(page.locator(".bh-band__now").first()).toBeVisible();
  // After the first frame the band remounts (Preparing → In Progress); judge its settled height.
  await expect.poll(async () => (await bar.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(40);
  expect((await bar.boundingBox())!.height).toBeLessThan(48);
  expect(await hitsItself(bar)).toBe(true);
  const stop = page.locator(".v2-send-btn--cancel").first();
  await expect(stop).toBeVisible();
  const [stopBg, fill] = await stop.evaluate((el) => {
    const probe = document.createElement("div");
    probe.style.background = "var(--c-fill-2)";
    document.body.appendChild(probe);
    const f = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return [getComputedStyle(el).backgroundColor, f];
  });
  expect(stopBg).toBe(fill);
  expect(await hitsItself(stop)).toBe(true);
});

test("completed summary and failure bar share a frame; latest-answer actions stay visible, 28 square and uncovered", async ({ page }) => {
  await open(page, "/?scenario=causal");
  const summary = page.getByTestId("step-summary").first();
  await expect(summary).toBeVisible({ timeout: 15_000 });
  const actions = page.locator(".bh-msg-actions").first();
  await expect(actions).toHaveAttribute("data-latest", "1");
  await page.mouse.move(0, 0);
  await expect.poll(() => actions.evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
  const copy = page.getByTestId("msg-action-copy");
  const box = await copy.boundingBox();
  expect(Math.round(box!.width)).toBe(28);
  expect(Math.round(box!.height)).toBe(28);
  expect(await hitsItself(copy)).toBe(true);
  const summaryRadius = await summary.evaluate((el) => getComputedStyle(el).borderTopLeftRadius);

  await page.goto("/?scenario=fail");
  const fail = page.getByTestId("final-fail").first();
  await expect(fail).toBeVisible({ timeout: 15_000 });
  expect(await fail.evaluate((el) => getComputedStyle(el).borderTopLeftRadius)).toBe(summaryRadius);
  // Failure bar is not entirely red: background uses the content surface, not the pale error surface.
  const [bg, surface] = await fail.evaluate((el) => {
    const probe = document.createElement("div");
    probe.style.background = "var(--c-surface)";
    document.body.appendChild(probe);
    const s = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return [getComputedStyle(el).backgroundColor, s];
  });
  expect(bg).toBe(surface);
});

test("clarification free-input field is not flattened in a narrow card (at least 32 high)", async ({ page }) => {
  await open(page, "/?scenario=hitl", 412);
  const input = page.locator(".ui-card-option-input").first();
  await expect(input).toBeVisible({ timeout: 15_000 });
  expect((await input.boundingBox())!.height).toBeGreaterThanOrEqual(32);
  expect(await hitsItself(input)).toBe(true);
});
