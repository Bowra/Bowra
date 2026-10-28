import { expect, test, type Page } from "@playwright/test";

/**
 * Lock page scrolling while dialogs are open (docs/design/组件统一-技术设计.md “行为批 A：菜单”, scroll-lock section).
 * Narrow (< 1200) globals.css allows body scrolling with `!important`; dialogs formerly used non-important
 * `overflow: hidden`, failing to override it, so the background still scrolled on phones. Test on the real knowledge-base Add knowledge dialog
 * (`DialogShell`): wheel cannot scroll background while open; closing restores position/scrolling; desktop 1440 scrollbar compensation prevents jumps.
 */

async function openKnowledge(page: Page) {
  await page.goto("/knowledge");
  await expect(page.getByRole("button", { name: "添加知识" }).first()).toBeVisible();
}

/** Page too short: append a body spacer to enable document scrolling (height only, no style-rule changes). */
async function makeScrollable(page: Page) {
  await page.evaluate(() => {
    const spacer = document.createElement("div");
    spacer.id = "e2e-scroll-spacer";
    spacer.style.height = "3000px";
    document.body.append(spacer);
  });
  await expect
    .poll(() => page.evaluate(() => document.scrollingElement!.scrollHeight))
    .toBeGreaterThan(2000);
}

const scrollY = (page: Page) => page.evaluate(() => Math.round(window.scrollY));

async function openDialog(page: Page) {
  // Avoid click(): it first scrolls the button into view, changing the page position configured by the test.
  await page.getByRole("button", { name: "添加知识" }).first().dispatchEvent("click");
  const dialog = page.locator("[data-settings-dialog-portal] [role=dialog]");
  await expect(dialog).toBeVisible();
  return dialog;
}

test.describe("390 触屏", () => {
  // Omit isMobile: mobile WebKit lacks wheel support; narrow styles depend only on viewport width, independently.
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test("while the dialog is open the wheel does not scroll the background; closing restores the position and scrolling", async ({ page }) => {
    await openKnowledge(page);
    await makeScrollable(page);
    await page.evaluate(() => window.scrollTo(0, 600));
    await expect.poll(() => scrollY(page)).toBe(600);

    const dialog = await openDialog(page);
    expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
    // Wheel down over the backdrop (outside the dialog panel): locked page does not move.
    await page.mouse.move(195, 20);
    await page.mouse.wheel(0, 800);
    await page.waitForTimeout(300);
    expect(await scrollY(page)).toBe(600);

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(await scrollY(page)).toBe(600);
    expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("visible");
    await page.mouse.wheel(0, 400);
    await expect.poll(() => scrollY(page)).toBeGreaterThan(600);
  });
});

test.describe("桌面 1440", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("while the dialog is open the page is locked without a scrollbar-compensation jump; closing restores it", async ({ page }) => {
    await openKnowledge(page);
    const anchor = page.getByRole("button", { name: "添加知识" }).first();
    const before = (await anchor.boundingBox())!;
    const dialog = await openDialog(page);
    expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
    // Desktop body already cannot scroll (overflow: hidden), so no scrollbar compensation: 0, no horizontal shift.
    expect(await page.evaluate(() => document.body.style.getPropertyValue("--floating-ui-scrollbar-width"))).toBe("0px");
    expect(await page.evaluate(() => document.body.style.paddingRight)).toBe("");
    const during = (await anchor.boundingBox())!;
    expect(Math.round(during.x)).toBe(Math.round(before.x));
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(await page.evaluate(() => document.body.getAttribute("style") ?? "")).toBe("");
    expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
  });
});
