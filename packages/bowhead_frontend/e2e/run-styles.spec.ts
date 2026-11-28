import { expect, test, type Page } from "@playwright/test";

import { mockAuthedApp } from "./_app-mocks";

/**
 * Chat running state R3.2: a style picked on the running card is an account setting. It survives a reload, another
 * browser opens with it, the expanded trace shows the same style, and selecting a mark there brings its step into view.
 * The mock timeline is held running by swallowing its later timers.
 */
async function openRunning(page: Page, settings: Record<string, string>, width = 1280) {
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = ((fn: TimerHandler, delay?: number, ...args: unknown[]) =>
      typeof delay === "number" && delay > 1500 && delay < 30_000 ? 0 : original(fn, delay, ...args)) as typeof window.setTimeout;
  });
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await page.route("**/api/v1/user-settings", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ settings }) }));
  await page.route("**/api/v1/user-settings/running_view_style", async (route) => {
    if (route.request().method() === "PUT") settings.running_view_style = JSON.parse(route.request().postData() ?? "{}").value;
    else delete settings.running_view_style;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ value: settings.running_view_style ?? "" }) });
  });
  await page.goto("/?scenario=peer");
  await expect(page.locator(".bh-band").first()).toBeVisible({ timeout: 20_000 });
}

test("a style picked on the card stays the account's style, in this browser and another", async ({ page, browser }) => {
  const settings: Record<string, string> = {};
  await openRunning(page, settings);
  await page.getByRole("radio", { name: "胶囊" }).click();
  await expect(page.locator("[data-run-style=chips]").first()).toBeVisible();
  expect(settings.running_view_style).toBe("chips");

  await page.reload();
  await expect(page.getByRole("radio", { name: "胶囊" })).toHaveAttribute("aria-checked", "true", { timeout: 20_000 });

  const other = await browser.newContext();
  const second = await other.newPage();
  await openRunning(second, settings);
  await expect(second.locator("[data-run-style=chips]").first()).toBeVisible();
  await other.close();
});

test("the expanded trace shows the style, and a mark brings its step into view", async ({ page }) => {
  await openRunning(page, { running_view_style: "timeline" });
  await page.locator(".bh-band__bar").first().click();
  const trace = page.locator(".ag-trace").first();
  await expect(trace.getByTestId("trace-style-overview").locator("[data-run-style=timeline]")).toBeVisible();
  const segment = trace.locator(".bh-seg").first();
  const stepId = await segment.getAttribute("data-step-id");
  await segment.click();
  await expect.poll(() => page.evaluate(() => document.activeElement?.closest("[data-step-id]")?.getAttribute("data-step-id")
    ?? document.activeElement?.getAttribute("data-step-id"))).toBe(stepId);
});

test("phone width: the switcher stays and chips wrap inside the card", async ({ page }) => {
  await openRunning(page, { running_view_style: "chips" }, 412);
  const band = page.locator(".bh-band").first();
  await expect(band.getByTestId("run-style-switcher")).toBeVisible();
  const overflow = await band.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
