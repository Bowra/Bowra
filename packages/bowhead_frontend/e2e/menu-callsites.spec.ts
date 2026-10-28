import { expect, test, type Page } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";

/**
 * Browser checks for menu callsites: search focus, export keyboard selection,
 * anchor visibility and chart-preview theming. Fixtures: `_menu-callsites-entry.tsx`.
 */
async function open(page: Page, scene: string) {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: "{}" }),
  );
  await page.route("**/api/v1/auth/me/workspaces**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: Array.from({ length: 12 }, (_, index) => ({
          workspace_id: index === 4 ? "ws_east" : `ws_${index}`,
          name: index === 4 ? "华东区" : `Space ${index + 1}`,
          role: "member",
        })),
        active_id: "ws_east",
      }),
    }),
  );
  await installReferenceComponent(page, "", "_menu-callsites-entry.tsx");
  await page.goto("/__reference_component_test");
  await expect(page.getByRole("heading", { name: "menu callsites" })).toBeVisible();
  await page.evaluate((value) => window.menuScene.show(value as never), scene);
}

for (const how of ["键盘", "鼠标"] as const) {
  test(`空间切换器（${how}打开）：过滤让当前空间换了位置，焦点仍留在搜索框`, async ({ page }) => {
    // Check both opening methods: Chromium keyboard and WebKit mouse (button click does not focus); list navigation
    // moves focus when selectedIndex changes, triggering the regression.
    await open(page, "space-picker");
    const chip = page.getByRole("button", { name: /华东区/ });
    await expect(chip).toBeEnabled();
    if (how === "键盘") {
      await chip.focus();
      await page.keyboard.press("Enter");
    } else {
      await chip.click();
    }
    const search = page.getByRole("searchbox");
    await search.click();
    await expect(search).toBeFocused();

    for (const text of ["华", "东"]) {
      await page.keyboard.type(text);
      await expect(search).toBeFocused();
    }
    await expect(search).toHaveValue("华东");
    await page.keyboard.press("Backspace");
    await page.keyboard.press("Backspace");
    await expect(search).toBeFocused();
    await expect(search).toHaveValue("");

    // After filtering, arrows enter filtered results.
    await page.keyboard.type("华");
    await page.keyboard.press("ArrowDown");
    await expect(page.getByRole("menuitemradio", { name: /华东区/ })).toBeFocused();
  });
}

test("report exports support keyboard format selection and restore focus", async ({ page }) => {
  await open(page, "export-menu");
  const trigger = page.getByRole("button", { name: "导出" });
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: /PDF 文档/ })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem", { name: /PowerPoint 演示文稿/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

for (const width of [412, 1440]) {
  test(`export guidance is readable at ${width}px and after refresh`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page, "export-settings");
    for (let visit = 0; visit < 2; visit += 1) {
      await expect(page.getByRole("heading", { name: "数据导出" })).toBeVisible();
      await expect(page.getByText(/按模板生成文档尚未开放/)).toBeVisible();
      await expect(page.getByText(/报告 PDF 是报告快照/)).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      if (visit === 0) {
        await page.reload();
        await expect(page.getByRole("heading", { name: "menu callsites" })).toBeVisible();
        await page.evaluate(() => window.menuScene.show("export-settings"));
      }
    }
  });
}

test("行菜单：行随列表滚出可见区时关闭，滚几像素不关", async ({ page }) => {
  // Credit-user/knowledge-file row “⋯” menus enable useMenu closeWhenAnchorHidden (unit tests enforce wiring);
  // real-browser test checks required IntersectionObserver semantics: scroll-container clipping also counts as hidden.
  await open(page, "row-menus");
  const scroller = page.getByTestId("row-scroller");
  const more = page.getByRole("button", { name: "第 1 行 更多" });
  await more.click();
  await expect(page.getByRole("menu")).toBeVisible();

  await scroller.evaluate((element) => { element.scrollTop = 4; });
  await page.waitForTimeout(300);
  await expect(page.getByRole("menu")).toBeVisible();

  await scroller.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await expect(page.getByRole("menu")).toHaveCount(0);
  // Focus return on closing must not scroll the container back.
  expect(await scroller.evaluate((element) => element.scrollTop)).toBeGreaterThan(100);
});

test("图表样式预览：深色预览里的图种菜单用预览区的配色", async ({ page }) => {
  await open(page, "chart-preview");
  await page.getByRole("radio", { name: "深色" }).click();
  const viewport = page.locator(".cs-preview__viewport");
  // The chart-kind switcher is the action button that ends in the chevron glyph.
  const trigger = viewport.locator(".v2-chart-action-btn", { has: page.locator("svg.lucide-chevron-down") }).first();
  await expect(trigger).toBeVisible({ timeout: 20_000 });
  await trigger.click();
  const menu = page.locator(".v2-chart-kind-menu");
  await expect(menu).toBeVisible();
  // Preview background transitions over 180ms; wait for it to settle before comparing.
  await expect
    .poll(async () => {
      const [menuBackground, viewportBackground] = await Promise.all([
        menu.evaluate((element) => getComputedStyle(element).backgroundColor),
        viewport.evaluate((element) => getComputedStyle(element).backgroundColor),
      ]);
      return menuBackground === viewportBackground ? "same" : `${menuBackground} vs ${viewportBackground}`;
    })
    .toBe("same");
});
