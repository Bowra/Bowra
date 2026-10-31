import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

async function open(page: Page, locale = "en-US") {
  await installReferenceComponent(page, "", "_configuration-field-guidance-entry.tsx");
  await page.goto(`/__reference_component_test?locale=${locale}`);
  await expect(page.getByRole("heading", { name: "Configuration guidance" })).toBeVisible();
}
async function insideViewport(page: Page) {
  const box = (await page.locator(".ui-help-popover__surface").boundingBox())!;
  const viewport = page.viewportSize()!;
  expect(box.x).toBeGreaterThanOrEqual(15);
  expect(box.y).toBeGreaterThanOrEqual(15);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width - 15);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height - 15);
}

for (const width of [320, 390, 1280])
  test(`help remains inside all viewport edges at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 360 });
    await open(page);
    for (const corner of ["top-left", "top-right", "bottom-left", "bottom-right"]) {
      const trigger = page.getByRole("button", { name: `About ${corner}`, exact: true });
      await trigger.click();
      await expect(page.getByRole("dialog", { name: corner, exact: true })).toBeVisible();
      await insideViewport(page);
      await page.keyboard.press("Escape");
      await expect(trigger).toBeFocused();
      await expect(page.locator(".ui-help-popover__surface")).toHaveCount(0);
    }
  });

test("preview pins, survives pointer exit, and keeps the parent editor and draft intact", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD"].includes(request.method())) writes.push(request.url());
  });
  await open(page);
  await page.getByLabel("Retries", { exact: true }).fill("4");
  const trigger = page.getByRole("button", { name: "About Retries", exact: true });
  await trigger.hover();
  await expect(page.getByRole("dialog", { name: "Retries", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("Retries", { exact: true })).toBeFocused();
  await trigger.click();
  await page.mouse.move(800, 300);
  await expect(page.getByRole("dialog", { name: "Retries", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Open editor" }).click();
  const nested = page.getByRole("button", { name: "About Dialog retries" });
  await nested.click();
  await expect(page.getByRole("dialog", { name: "Dialog retries", exact: true })).toBeVisible();
  await insideViewport(page);
  const hit = await page.locator(".ui-help-popover__surface").evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(rect.x + 20, rect.y + 20));
  });
  expect(hit).toBe(true);
  await page.keyboard.press("Escape");
  await expect(nested).toBeFocused();
  await expect(page.getByRole("dialog", { name: "Original editor", exact: true })).toBeVisible();
  await expect(page.getByLabel("Dialog draft")).toHaveValue("4");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes).toEqual([]);
});

for (const locale of ["zh-CN", "de-DE", "fr-FR"])
  test(`localized guidance fits at 200% text size: ${locale}`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 440 });
    await open(page, locale);
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "32px";
    });
    await page.locator("[data-configuration-field] button").first().click();
    await expect(page.locator(".ui-help-popover__surface")).toBeVisible();
    await insideViewport(page);
    expect(await page.locator(".ui-help-popover__surface").evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  });

test("coarse-pointer targets stay 44px without overlapping the input", async ({ browser }) => {
  const context = await browser.newContext({ hasTouch: true, viewport: { width: 390, height: 700 } });
  const page = await context.newPage();
  await open(page);
  const trigger = page.getByRole("button", { name: "About Retries", exact: true });
  const box = (await trigger.boundingBox())!;
  expect(box.width).toBeGreaterThanOrEqual(44);
  expect(box.height).toBeGreaterThanOrEqual(44);
  await trigger.tap();
  await expect(page.getByRole("dialog", { name: "Retries", exact: true })).toBeVisible();
  await trigger.tap();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await context.close();
});
