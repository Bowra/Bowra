import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

/**
 * Real-browser Save-bar check (docs/design/组件统一-技术设计.md “逐页验收 A”): < 1200 settings shell is single-column,
 * the whole document scrolls, Save bar fixes to viewport bottom. After changes it appears; at scroll end the last item's bottom stays at or above bar top
 * (WebKit once failed to recalculate `:has()` after portal insertion, hiding the last item); replace visualViewport to simulate the keyboard,
 * lifting the bar above the visible-area bottom without obscuring the input field. Fixture: `_settings-savebar-entry.tsx`.
 */
async function open(page: Page, width: number) {
  await page.setViewportSize({ width, height: 700 });
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 404, contentType: "application/json", body: "{}" }));
  await installReferenceComponent(page, "", "_settings-savebar-entry.tsx");
  await page.goto("/__reference_component_test");
  await expect(page.getByRole("heading", { name: "settings savebar" })).toBeVisible();
}

const bar = (page: Page) => page.getByRole("region", { name: "有未保存的更改" });

// Since the navigation breakpoints moved to 1024/768, the shell is single-column and the bar is
// viewport-fixed only below 1024; 1023 and 1024 pin both sides of that boundary.
for (const width of [390, 768, 1023, 1024]) {
  test.describe(`保存栏 ${width}px`, () => {
    test("改动后出现，滚到底时最后一项底边不低于栏顶", async ({ page }) => {
      await open(page, width);
      await expect(bar(page)).toHaveCount(0);
      const field = page.getByTestId("last-field");
      await field.fill("x");
      await expect(bar(page)).toBeVisible();
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      await expect.poll(async () => {
        const [fieldBox, barBox] = await Promise.all([field.boundingBox(), bar(page).boundingBox()]);
        return fieldBox && barBox ? barBox.y - (fieldBox.y + fieldBox.height) : -1;
      }).toBeGreaterThanOrEqual(0);
      // Bar sits flush with viewport bottom.
      const barBox = (await bar(page).boundingBox())!;
      expect(Math.abs(barBox.y + barBox.height - 700)).toBeLessThanOrEqual(1);
      // Discard removes the bar and its spacer.
      await page.getByRole("button", { name: "放弃" }).click();
      await expect(bar(page)).toHaveCount(0);
      await expect(page.locator(".ui-savebar-spacer")).toHaveCount(0);
    });

    // Only the viewport-fixed bar (< 1024) has to clear the soft keyboard; from 1024 it is sticky inside the content.
    if (width < 1024) test("软键盘弹起时栏在键盘上方，正在输入的字段不被挡住", async ({ page }) => {
      await open(page, width);
      const field = page.getByTestId("last-field");
      await field.fill("x");
      await expect(bar(page)).toBeVisible();
      // Simulate iOS: layout viewport unchanged, visualViewport height reduced by 300.
      await page.evaluate(() => {
        const target = new EventTarget();
        const fake = Object.assign(target, { height: window.innerHeight, width: window.innerWidth, offsetTop: 0, offsetLeft: 0, scale: 1 });
        Object.defineProperty(window, "visualViewport", { configurable: true, get: () => fake });
        (window as unknown as { __vv: typeof fake }).__vv = fake;
      });
      // Remount the subscription: discard, then change again.
      await page.getByRole("button", { name: "放弃" }).click();
      await field.fill("y");
      await expect(bar(page)).toBeVisible();
      await page.evaluate(() => {
        const vv = (window as unknown as { __vv: EventTarget & { height: number } }).__vv;
        vv.height = window.innerHeight - 300;
        vv.dispatchEvent(new Event("resize"));
      });
      await expect.poll(async () => {
        const box = await bar(page).boundingBox();
        return box ? Math.round(box.y + box.height) : 0;
      }).toBeLessThanOrEqual(401);
      await field.focus();
      await page.evaluate(() => {
        const vv = (window as unknown as { __vv: EventTarget }).__vv;
        vv.dispatchEvent(new Event("resize"));
      });
      await expect.poll(async () => {
        const [fieldBox, barBox] = await Promise.all([field.boundingBox(), bar(page).boundingBox()]);
        return fieldBox && barBox ? barBox.y - (fieldBox.y + fieldBox.height) : -1;
      }).toBeGreaterThanOrEqual(0);
    });
  });
}
