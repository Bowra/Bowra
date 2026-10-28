import { expect, test } from "@playwright/test";

// The mobile Results tab announces unseen output as `成果，有新内容`.
const RESULTS_TAB = /^成果(，有新内容)?$/;

for (const width of [412, 1280]) {
  test(`Preview failure → independent Raw → Details → report (${width}px)`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 800 });
    await page.request.post(
      `http://127.0.0.1:${process.env.PLAYWRIGHT_API_PORT ?? "18080"}/api/v1/_test/reset`,
    );
    await page.route("**/api/v1/results/capabilities", (route) =>
      route.fulfill({
        json: {
          dto_version: 1,
          workspace: true,
          complete_list: false,
          scale_nav: false,
          max_page_size: 50,
        },
      }),
    );
    let previewRequests = 0,
      rawRequests = 0;
    await page.route(
      "**/api/v1/files/art_stdout/representations/preview",
      (route) => {
        previewRequests += 1;
        return route.fulfill({
          status: 422,
          json: {
            detail: { error: { code: "artifact_representation_corrupt" } },
          },
        });
      },
    );
    await page.route(
      "**/api/v1/files/art_stdout/representations/raw",
      (route) => {
        rawRequests += 1;
        return route.fulfill({
          json: {
            representation: "raw",
            revision: "raw-1",
            family: "text",
            mode: "text",
            text: "  original CSV\n品牌,营业额\nA,42\n  ",
            encoding: "utf-8",
            truncated: false,
          },
        });
      },
    );
    await page.goto("/");
    const composer = page.locator("textarea").first();
    await expect(composer).toBeVisible();
    await composer.fill("resultsworkspace");
    const sent = page.waitForResponse((response) =>
      response.url().endsWith("/api/v1/chat/send"),
    );
    await composer.press("Enter");
    const { turn_id: turnId, session_id: sessionId } = await (
      await sent
    ).json();
    await expect(
      page.locator('[data-presentation="results-reading"]'),
    ).toHaveCount(1);
    if (width < 768)
      await page.getByRole("tab", { name: RESULTS_TAB }).click();
    const detail = await (
      await page.request.get(`/api/v1/sessions/${sessionId}`)
    ).json();
    await page.route(`**/api/v1/sessions/${sessionId}`, (route) =>
      route.fulfill({
        json: {
          ...detail,
          turns: [
            {
              id: turnId,
              seq: 1,
              user_text: "resultsworkspace",
              status: "completed",
              intent: "analysis",
              started_at: "2026-09-08T00:00:00Z",
              ended_at: "2026-09-08T00:00:01Z",
              final_answer: "报告已生成",
              trace_steps: [],
            },
          ],
        },
      }),
    );
    // Actual current table action is in its quiet overflow menu, not an always-visible button.
    const closeNavigator = page.getByRole("button", {
      name: "关闭成果导航",
      exact: true,
    });
    if (await closeNavigator.isVisible()) await closeNavigator.click();
    const table = page
      .locator(".canvas-report-article .v2-chart-table")
      .first();
    await table.locator(".is-more").click();
    await page.getByRole("menuitem", { name: "打开数据", exact: true }).click();
    const failure = page.locator(".artifact-viewer-note.error");
    await expect(failure).toBeVisible();
    await expect(
      failure.getByRole("link", { name: "下载", exact: true }),
    ).toHaveAttribute("href", "/api/v1/files/art_stdout");
    await page.screenshot({
      path: testInfo.outputPath(`recovery-${width}.png`),
      fullPage: true,
    });
    await failure
      .getByRole("button", { name: "原始内容", exact: true })
      .click();
    await expect(page).toHaveURL(/mode=raw/);
    const original = page.locator(".artifact-text-preview pre");
    await expect(original).toHaveText("  original CSV\n品牌,营业额\nA,42\n  ", {
      useInnerText: false,
    });
    expect(previewRequests).toBe(1);
    expect(rawRequests).toBe(1);
    await page.getByRole("button", { name: "全屏查看", exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText("original CSV");
    expect(rawRequests).toBe(1);
    await page.keyboard.press("Escape");
    await page.reload();
    // Wait for route/session hydration before selecting the mobile surface;
    // an early click during the default Chat shell is superseded by hydrate.
    await expect(original).toContainText("original CSV");
    if (
      width < 768 &&
      (await page.getByRole("tab", { name: RESULTS_TAB }).isVisible())
    )
      await page.getByRole("tab", { name: RESULTS_TAB }).click();
    if (await closeNavigator.isVisible()) await closeNavigator.click();
    await expect(original).toBeVisible();
    await expect(original).toContainText("original CSV");
    await page.getByRole("tab", { name: "详情", exact: true }).click();
    await expect(page.locator(".artifact-details")).toContainText("art_stdout");
    await page.getByRole("button", { name: /返回报告/ }).click();
    await expect(
      page.locator('[data-presentation="results-reading"]'),
    ).toBeVisible();
  });
}
