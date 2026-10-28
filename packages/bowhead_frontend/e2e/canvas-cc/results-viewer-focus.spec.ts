import { expect, test } from "@playwright/test";

for (const fullscreen of [false, true]) {
  test(`HTML reading state survives frame-to-parent focus (${fullscreen ? "fullscreen" : "split"})`, async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.request.post(`http://127.0.0.1:${process.env.PLAYWRIGHT_API_PORT ?? "18080"}/api/v1/_test/reset`);
    await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({
      json: { dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50 },
    }));
    let metadataReads = 0;
    let contentReads = 0;
    page.on("request", (request) => {
      if (request.url().includes("/art_delivery/representations/details")) metadataReads += 1;
    });
    await page.route("**/api/v1/sessions/*/artifacts/art_delivery/content", (route) => {
      contentReads += 1;
      return route.fulfill({
        contentType: "text/html",
        body: '<!doctype html><html><body><details><summary>展开内容</summary><p>展开后的原文</p></details><div style="height:1800px">长报告</div></body></html>',
      });
    });
    await page.goto("/");
    const composer = page.locator("textarea").first();
    await composer.fill("resultsworkspace");
    await composer.press("Enter");
    await expect(page.locator('[data-presentation="results-reading"]')).toBeVisible();
    await page.getByRole("button", { name: "成果", exact: true }).click();
    await page.getByRole("dialog", { name: "成果导航", exact: true })
      .locator(".results-nav-item", { hasText: "executive-brief.html" }).click();
    await expect(page.locator('iframe[title="executive-brief.html"]')).toBeVisible();
    if (fullscreen) await page.getByRole("button", { name: "全屏查看", exact: true }).click();
    const frame = page.frameLocator('iframe[title="executive-brief.html"]');
    const frameElement = await page.locator('iframe[title="executive-brief.html"]').elementHandle();
    await frame.locator("summary").click();
    await frame.locator("body").evaluate(() => window.scrollTo(0, 600));
    await expect.poll(() => frame.locator("body").evaluate(() => window.scrollY)).toBe(600);
    const reads = [metadataReads, contentReads];
    if (fullscreen) {
      await page.getByRole("dialog", { name: "executive-brief.html" })
        .locator(".artifact-viewer-title strong").click();
    } else {
      await composer.click();
    }
    // Flush browser/React event work, then check the original frame, not a replacement
    // with an identical title. No synthetic focus event or force-click here.
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    expect(await frameElement!.evaluate((element) => element.isConnected)).toBe(true);
    await expect(frame.locator("details")).toHaveAttribute("open", "");
    expect(await frame.locator("body").evaluate(() => window.scrollY)).toBe(600);
    expect([metadataReads, contentReads]).toEqual(reads);
    if (fullscreen) {
      await page.keyboard.press("Escape");
      await expect(page.getByRole("dialog", { name: "executive-brief.html" })).toBeHidden();
      await expect(page.getByRole("button", { name: "全屏查看", exact: true })).toBeFocused();
    }
  });
}
