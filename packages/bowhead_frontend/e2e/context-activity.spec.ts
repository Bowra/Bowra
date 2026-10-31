import { expect, test } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

for (const scenario of [{ width: 360, locale: "en-US", theme: "light" }, { width: 1280, locale: "zh-CN", theme: "dark" }]) {
  test(`quiet context activity: ${scenario.width} ${scenario.locale} ${scenario.theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: scenario.width, height: 800 });
    await installReferenceComponent(page, "", "_context-activity-entry.tsx");
    const query = `locale=${scenario.locale}&theme=${scenario.theme}`;
    await page.goto(`/__reference_component_test?${query}`);
    const row = page.locator(".ag-context-activity");
    await expect(row).toHaveCount(1);
    await expect(row).toContainText(/Organizing context|正在整理上下文/);
    await expect(page.getByRole("status")).toContainText(/Organizing context|正在整理上下文/);
    await expect(page.getByRole("alert")).toHaveCount(0);
    await page.screenshot({ path: info.outputPath("running.png") });
    await page.locator("#complete-context").focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#complete-context")).toBeFocused();
    // The existing Agent disclosure folds after completion; the user can reopen it.
    const disclosure = page.locator(".agb-head");
    if (!await row.isVisible()) await disclosure.click();
    await expect(row).toContainText(/Context organized|上下文已整理/);
    await expect(row.locator(".spin")).toHaveCount(0);
    await page.screenshot({ path: info.outputPath("completed.png") });
    await page.goto(`/__reference_component_test?${query}&phase=committed`);
    if (!await row.isVisible()) await page.locator(".agb-head").click();
    await expect(row).toHaveCount(1);
    await expect(row).toContainText(/Context organized|上下文已整理/);
    await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  });
}
