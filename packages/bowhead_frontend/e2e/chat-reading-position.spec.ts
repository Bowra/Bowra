import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

/**
 * F3 (2026-11-24 dogfood): opening Results while reading Turn 9 jumped the chat to Turn 5. The column
 * narrows, the text reflows, and the browser's own scroll anchoring is suppressed by the width change.
 */
async function offsetOf(page: Page, id: string): Promise<number> {
  return page.evaluate((target) => {
    const scroller = document.querySelector(".v2-messages")!;
    return document.getElementById(target)!.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
  }, id);
}

test("the paragraph being read stays in place when Results opens and closes", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await installReferenceComponent(page, "", "_reading-position-entry.tsx");
  await page.goto("/__reference_component_test");
  const scroller = page.locator(".v2-messages");
  await expect(scroller).toBeVisible();

  // Read Turn 9 the way a reader gets there: scroll up with the wheel, then settle on a paragraph.
  await scroller.hover();
  await page.mouse.wheel(0, -1200);
  // Let the wheel's smooth scroll finish before settling on the paragraph.
  await expect.poll(() => scroller.evaluate((el) => el.scrollTop), { intervals: [200] }).toBe(
    await scroller.evaluate(async (el) => { await new Promise((r) => setTimeout(r, 600)); return el.scrollTop; }),
  );
  await page.evaluate(() => {
    const el = document.querySelector<HTMLElement>(".v2-messages")!;
    const paragraph = document.getElementById("t_9-p2")!;
    const top = paragraph.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop - 40;
    el.scrollTo({ top, behavior: "instant" });
  });
  await expect.poll(() => offsetOf(page, "t_9-p2")).toBeCloseTo(40, 0);
  await page.waitForTimeout(300);
  expect(Math.abs((await offsetOf(page, "t_9-p2")) - 40)).toBeLessThanOrEqual(1);

  await page.locator("#toggle-results").click();
  await page.waitForTimeout(400);
  expect(Math.abs((await offsetOf(page, "t_9-p2")) - 40)).toBeLessThanOrEqual(2);

  await page.locator("#toggle-results").click();
  await page.waitForTimeout(400);
  expect(Math.abs((await offsetOf(page, "t_9-p2")) - 40)).toBeLessThanOrEqual(2);
});
