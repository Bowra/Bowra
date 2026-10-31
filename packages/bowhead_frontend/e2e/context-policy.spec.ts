import { expect, test } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

for (const scenario of [
  { width: 1280, locale: "en-US", theme: "light" },
  { width: 360, locale: "en-US", theme: "light" },
  { width: 390, locale: "zh-CN", theme: "light" },
  { width: 1280, locale: "zh-CN", theme: "dark" },
]) {
  test(`model context preferences: ${scenario.width} ${scenario.locale} ${scenario.theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: scenario.width, height: 800 });
    await installReferenceComponent(page, "", "_context-policy-entry.tsx");
    await page.goto(`/__reference_component_test?locale=${scenario.locale}&theme=${scenario.theme}`);
    const toggle = page.getByRole("switch");
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(toggle).toHaveAccessibleName(/Auto-compact|自动整理上下文/);
    await expect(page.getByTestId("saved-preferences")).toHaveText("null");
    await expect(page.locator("details")).not.toHaveAttribute("open", "");
    await page.screenshot({ path: info.outputPath("collapsed.png") });
    await toggle.focus();
    await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await page.locator("summary").click();
    const trigger = page.locator("input[id$='-trigger_input_tokens']");
    const recent = page.locator("input[id$='-keep_recent_tokens']");
    await trigger.fill("30000");
    await recent.fill("6000");
    await expect(page.getByTestId("saved-preferences")).toHaveText(JSON.stringify({ enabled: false, trigger_input_tokens: 30000, keep_recent_tokens: 6000 }));
    await page.screenshot({ path: info.outputPath("expanded.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await recent.fill("35000");
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(recent).toHaveAttribute("aria-invalid", "true");
    await page.getByRole("button", { name: /Reset|重置/ }).click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(trigger).toHaveValue("");
    await expect(recent).toHaveValue("");
    await expect(page.getByTestId("saved-preferences")).toHaveText("null");
    await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  });
}
