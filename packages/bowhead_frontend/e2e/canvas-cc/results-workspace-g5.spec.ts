import { expect, test } from "@playwright/test";

async function resetMock(page: import("@playwright/test").Page) {
  await page.request.post(`http://127.0.0.1:${process.env.PLAYWRIGHT_API_PORT ?? "18080"}/api/v1/_test/reset`);
}

async function enableResultsWorkspace(page: import("@playwright/test").Page) {
  await page.route(
    (url) => url.pathname === "/api/v1/results/capabilities",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          dto_version: 1,
          workspace: true,
          complete_list: false,
          scale_nav: false,
          max_page_size: 50,
        }),
      });
    },
  );
  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.feature.results_workspace", "1");
  });
}

async function sendMessage(page: import("@playwright/test").Page, message: string) {
  const textarea = page.locator("textarea").first();
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(message);
  await textarea.press("Enter");
}

async function openNavigator(page: import("@playwright/test").Page) {
  await page.getByRole("button", { name: "成果", exact: true }).click();
  const navigator = page.getByRole("dialog", { name: "成果导航", exact: true });
  await expect(navigator).toBeVisible();
  return navigator;
}

/** Pins the explicit compact sidebar choice so recent sessions open from the rail flyout. */
async function pinCompactSidebar(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.sidebar.mode.u_e2e", "compact");
  });
}

test.beforeEach(async ({ page }) => {
  await resetMock(page);
  await enableResultsWorkspace(page);
});

test("G5 desktop: IA, Report persistence, Code output and Unsupported actions", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/");
  await sendMessage(page, "resultsworkspace");

  await expect(page.locator(".v2-ws-right")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("tablist", { name: "报告视图" })).toHaveCount(0);
  await expect(page.locator('[data-presentation="results-reading"]')).toHaveCount(1);
  await expect(page.locator(".results-report-object-toolbar")).toBeVisible();
  await expect(page.locator(".results-report-edit-controls button").first()).toBeVisible();

  const reportWidth = await page.locator(".canvas-report-article").evaluate((element) => (
    element.getBoundingClientRect().width
  ));
  expect(reportWidth).toBeLessThanOrEqual(661);
  const reportLayout = await page.locator(
    ".v2-ws-right-body.results-body--report",
  ).evaluate((body) => {
    const title = body.querySelector<HTMLElement>(".v2-report-h1");
    return {
      clientWidth: body.clientWidth,
      scrollWidth: body.scrollWidth,
      titleFontSize: title
        ? Number.parseFloat(getComputedStyle(title).fontSize)
        : 0,
    };
  });
  expect(reportLayout.scrollWidth).toBe(reportLayout.clientWidth);
  // Content type scale: the report title is 28px (2 × the 14px base) at every pane width.
  expect(reportLayout.titleFontSize).toBe(28);

  // 「打开数据」 lives in the table's quiet 「更多操作」 menu; returning restores focus to that trigger.
  const tableMoreButton = page.locator(".canvas-report-article .v2-chart-table").first().locator(".is-more");
  const openReportData = async () => {
    await tableMoreButton.click();
    await page.getByRole("menuitem", { name: "打开数据", exact: true }).click();
  };
  await openReportData();
  await expect(page).toHaveURL(/result=art_stdout/);
  const returnToReport = page.getByRole("button", { name: "返回报告：各市场净营收", exact: true });
  await expect(returnToReport).toBeVisible();
  await expect(page.locator(".artifact-viewer-head").getByRole("button", { name: "关闭", exact: true })).toHaveCount(0);
  await expect(page.locator(".artifact-viewer-body")).toBeVisible();
  const artifactWidths = await page.evaluate(() => ({
    head: document.querySelector<HTMLElement>(".artifact-viewer-head")?.getBoundingClientRect().width ?? 0,
    body: document.querySelector<HTMLElement>(".artifact-viewer-body")?.getBoundingClientRect().width ?? 0,
  }));
  expect(artifactWidths.head).toBeGreaterThan(0);
  expect(artifactWidths.body).toBeGreaterThanOrEqual(artifactWidths.head * 0.9);
  await returnToReport.click();
  await expect(page).not.toHaveURL(/result=art_stdout/);
  await expect(page.locator('[data-presentation="results-reading"]')).toBeVisible();
  await expect(tableMoreButton).toBeFocused();

  // Browser Back and the visible return control share the same source restore path.
  await openReportData();
  await expect(page).toHaveURL(/result=art_stdout/);
  await page.goBack();
  await expect(page.locator('[data-presentation="results-reading"]')).toBeVisible();
  await expect(tableMoreButton).toBeFocused();

  let navigator = await openNavigator(page);
  const navigatorWidth = await navigator.evaluate((element) => element.getBoundingClientRect().width);
  expect(navigatorWidth).toBeLessThanOrEqual(306);
  await expect(navigator.getByRole("button", { name: "固定成果导航", exact: true })).toHaveCount(0);
  await navigator.getByRole("button", { name: "关闭成果导航", exact: true }).click();

  await page.getByRole("button", { name: "放大报告（隐藏对话）", exact: true }).click();
  const maximizedOverflow = await page.evaluate(() => Object.fromEntries(Object.entries({
    document: document.documentElement,
    splitArea: document.querySelector<HTMLElement>(".v2-split-area"),
    splitCanvas: document.querySelector<HTMLElement>(".v2-split-canvas"),
    canvasPane: document.querySelector<HTMLElement>(".v2-ws-right"),
    workspaceBody: document.querySelector<HTMLElement>(".results-workspace-body"),
    reportBody: document.querySelector<HTMLElement>(".v2-ws-right-body.results-body--report"),
  }).map(([name, element]) => [
    name,
    element ? element.scrollWidth - element.clientWidth : null,
  ])));
  expect(maximizedOverflow).toEqual({
    document: 0,
    splitArea: 0,
    splitCanvas: 0,
    canvasPane: 0,
    workspaceBody: 0,
    reportBody: 0,
  });
  navigator = await openNavigator(page);
  await navigator.getByRole("button", { name: "固定成果导航", exact: true }).click();
  await expect(page.locator(".results-navigator--pinned")).toBeVisible();
  await page.getByRole("button", { name: "退出放大", exact: true }).click();
  navigator = page.getByRole("dialog", { name: "成果导航", exact: true });
  await expect(navigator).toBeVisible();
  await expect(page.locator(".results-navigator--pinned")).toHaveCount(0);
  await navigator.getByRole("button", { name: "关闭成果导航", exact: true }).click();

  navigator = await openNavigator(page);
  await expect(navigator.getByText("交付内容", { exact: true })).toBeVisible();
  await expect(navigator.getByText("分析依据", { exact: true })).toBeVisible();
  await expect(navigator.getByText("数据", { exact: true })).toBeVisible();
  await expect(navigator.getByText("代码", { exact: true })).toBeVisible();
  await expect(navigator.getByText("输入文件", { exact: true })).toBeVisible();
  await expect(navigator.getByText("executive-brief.html", { exact: true })).toBeVisible();
  await expect(navigator.getByText("source-notes.txt", { exact: true })).toBeVisible();

  await navigator.locator(".results-nav-item", { hasText: "代码与运行" }).click();
  await expect(page.locator(".code-result-viewer")).toBeVisible();
  await expect(page.getByRole("button", { name: "复制代码", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "打开输出", exact: true })).toBeVisible();
  await expect(page.locator('[data-presentation="results-reading"]')).toHaveCount(1);
  await expect(page.locator('.results-object-surface[hidden] [data-presentation="results-reading"]')).toHaveCount(1);

  const openOutputButton = page.getByRole("button", { name: "打开输出", exact: true });
  await openOutputButton.click();
  await expect(page).toHaveURL(/result=art_stdout/);
  await expect(page.getByText("19×29=551", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "返回代码", exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.locator(".code-result-viewer")).toBeVisible();
  await expect(openOutputButton).toBeFocused();

  navigator = await openNavigator(page);
  await navigator.locator(".results-nav-item", { hasText: "source-bundle.zip" }).click();
  await expect(page).toHaveURL(/result=art_unsupported.*mode=details/);
  await expect(page.getByRole("link", { name: "下载", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "更多操作", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "在 Files 中管理", exact: true })).toBeVisible();
  await page.getByRole("menuitem", { name: "反馈此格式", exact: true }).click();
  await expect(page.getByText("已记录此格式需求", { exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.locator(".code-result-viewer")).toBeVisible();
});

test("G5 session switch closes navigation before hydrate and drops source Results route", async ({ page }) => {
  const targetSessionId = "s_target";
  const targetTurnId = "t_target";
  let releaseTurnEvents = () => {};
  let turnEventsRequested = false;
  const turnEventsGate = new Promise<void>((resolve) => {
    releaseTurnEvents = resolve;
  });

  await page.route(
    (url) => url.pathname === "/api/v1/sessions",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          groups: [{
            id: "today",
            label: "Today",
            items: [
              {
                id: "s_e2e",
                title: "E2E mock session",
                intents: ["lookup"],
                time: "09:00",
                state: "done",
              },
              {
                id: targetSessionId,
                title: "Target persisted session",
                intents: ["lookup"],
                time: "09:01",
                state: "done",
              },
            ],
          }],
        }),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/v1/sessions/${targetSessionId}`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: {
            id: targetSessionId,
            workspace_id: "ws_test",
            title: "Target persisted session",
            started_at: "2026-08-10T09:00:00Z",
            last_active_at: "2026-08-10T09:01:00Z",
            status: "completed",
            cost_usd: 0,
            tokens_total: 0,
            intents: ["lookup"],
          },
          turns: [{
            id: targetTurnId,
            seq: 1,
            user_text: "Target persisted question",
            intent: "lookup",
            status: "completed",
            started_at: "2026-08-10T09:00:00Z",
            ended_at: "2026-08-10T09:01:00Z",
            final_answer: "Target persisted answer",
            trace_steps: [],
            summary: null,
          }],
        }),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/v1/canvas/by-session/${targetSessionId}`,
    async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 600));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ canvas: null, sections: [], sourceRefs: [] }),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/v1/turns/${targetTurnId}/events`,
    async (route) => {
      turnEventsRequested = true;
      await turnEventsGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ events: [] }),
      });
    },
  );

  try {
    await pinCompactSidebar(page);
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto("/");
    await sendMessage(page, "resultsworkspace");
    const navigator = await openNavigator(page);
    await navigator.locator(".results-nav-item", { hasText: "代码与运行" }).click();
    await expect(page).toHaveURL(/result=code/);

    const recentButton = page.getByRole("button", { name: /最近会话|Recent Sessions/ });
    await recentButton.click();
    await page.getByRole("button", { name: /^Target persisted session/ }).click();

    await expect(recentButton).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("session-loading")).toBeVisible();
    await expect(page.getByText("Target persisted answer", { exact: true })).toBeVisible();
    await expect.poll(() => turnEventsRequested).toBe(true);
    await expect(page).toHaveURL(new RegExp(`session=${targetSessionId}`));
    await expect(page).not.toHaveURL(/[?&](result|mode|tab|focus)=/);
  } finally {
    releaseTurnEvents();
  }
});

test("G5 latest session transition wins when an older hydrate resolves last", async ({ page }) => {
  const slowSessionId = "s_slow";
  const fastSessionId = "s_fast";
  let releaseSlow = () => {};
  const slowGate = new Promise<void>((resolve) => {
    releaseSlow = resolve;
  });
  const sessionDetail = (sessionId: string, title: string, answer: string) => ({
    session: {
      id: sessionId,
      workspace_id: "ws_test",
      title,
      started_at: "2026-08-10T09:00:00Z",
      last_active_at: "2026-08-10T09:01:00Z",
      status: "completed",
      cost_usd: 0,
      tokens_total: 0,
      intents: ["lookup"],
    },
    turns: [{
      id: `t_${sessionId}`,
      seq: 1,
      user_text: `${title} question`,
      intent: "lookup",
      status: "completed",
      started_at: "2026-08-10T09:00:00Z",
      ended_at: "2026-08-10T09:01:00Z",
      final_answer: answer,
      trace_steps: [],
      summary: null,
    }],
  });

  await page.route(
    (url) => url.pathname === "/api/v1/sessions",
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          groups: [{
            id: "today",
            label: "Today",
            items: [
              { id: slowSessionId, title: "Slow session", intents: [], time: "09:00", state: "done" },
              { id: fastSessionId, title: "Fast session", intents: [], time: "09:01", state: "done" },
            ],
          }],
        }),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/v1/sessions/${slowSessionId}`,
    async (route) => {
      await slowGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessionDetail(slowSessionId, "Slow session", "Stale slow answer")),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/v1/canvas/by-session/${slowSessionId}`,
    async (route) => {
      await slowGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ canvas: null, sections: [], sourceRefs: [] }),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/v1/sessions/${fastSessionId}`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessionDetail(fastSessionId, "Fast session", "Latest fast answer")),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/v1/canvas/by-session/${fastSessionId}`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ canvas: null, sections: [], sourceRefs: [] }),
      });
    },
  );

  try {
    await pinCompactSidebar(page);
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto("/");
    const recentButton = page.getByRole("button", { name: /最近会话|Recent Sessions/ });
    await recentButton.click();
    await page.getByRole("button", { name: /^Slow session/ }).click();
    await expect(page.getByTestId("session-loading")).toBeVisible();

    await recentButton.click();
    await page.getByRole("button", { name: /^Fast session/ }).click();
    await expect(page.getByText("Latest fast answer", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`session=${fastSessionId}`));

    const slowDetailResponse = page.waitForResponse(
      (response) => response.url().includes(`/sessions/${slowSessionId}`),
    );
    const slowCanvasResponse = page.waitForResponse(
      (response) => response.url().includes(`/canvas/by-session/${slowSessionId}`),
    );
    releaseSlow();
    await Promise.all([slowDetailResponse, slowCanvasResponse]);

    await expect(page.getByText("Stale slow answer", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Latest fast answer", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`session=${fastSessionId}`));
  } finally {
    releaseSlow();
  }
});

test("G5 mobile: list to detail keeps 44px targets without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await sendMessage(page, "resultsworkspace");
  // The Results tab announces unseen output as `成果，有新内容`.
  await page.getByRole("tab", { name: /^成果(，有新内容)?$/ }).click();

  const mobileNavigator = page.locator(".results-navigator--mobile");
  await expect(mobileNavigator).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("dialog", { name: "成果导航", exact: true })).toHaveCount(0);
  await mobileNavigator.locator(".results-nav-item", { hasText: "source-notes.txt" }).click();
  await expect(mobileNavigator).toBeHidden();
  const backButton = page.getByRole("button", { name: "返回成果列表", exact: true });
  await expect(backButton).toBeVisible();
  await expect(backButton).toBeFocused();

  const dimensions = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    backHeight: document.querySelector<HTMLElement>(".results-mobile-back")?.getBoundingClientRect().height ?? 0,
    modeHeights: Array.from(document.querySelectorAll<HTMLElement>(".data-viewer-tab"))
      .map((element) => element.getBoundingClientRect().height),
  }));
  expect(dimensions.overflow).toBe(false);
  expect(dimensions.backHeight).toBeGreaterThanOrEqual(44);
  expect(dimensions.modeHeights.every((height) => height >= 44)).toBe(true);
});

test("G5 Safe HTML full view keeps the sandbox frame inside the focus loop", async ({ page }) => {
  await page.goto("/");
  await sendMessage(page, "resultsworkspace");

  const navigator = await openNavigator(page);
  await navigator.locator(".results-nav-item", { hasText: "executive-brief.html" }).click();
  await expect(page.locator('iframe[title="executive-brief.html"]')).toBeVisible();
  await page.getByRole("button", { name: "全屏查看", exact: true }).click();

  const fullView = page.getByRole("dialog", { name: "executive-brief.html" });
  await expect(fullView).toBeVisible();
  await fullView.locator("button:not([disabled]), a[href], summary").first().focus();
  await page.keyboard.press("Shift+Tab");
  await expect(fullView.locator('iframe[title="executive-brief.html"]')).toBeFocused();
});

test("G5 native video is a default Preview and rejects a Raw route", async ({ page }) => {
  await page.goto("/");
  await sendMessage(page, "resultsworkspace");

  const navigator = await openNavigator(page);
  await navigator.locator(".results-nav-item", { hasText: "walkthrough.mp4" }).click();
  await expect(page.getByRole("tab", { name: "预览", exact: true })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("tab", { name: "原始内容", exact: true })).toHaveCount(0);
  await expect(page.locator("video")).toHaveAttribute("preload", "metadata");
  await expect(page.locator("video")).toHaveAttribute("playsinline", "");

  await page.evaluate(() => {
    const invalidRawUrl = new URL(window.location.href);
    invalidRawUrl.searchParams.set("mode", "raw");
    window.history.pushState(null, "", invalidRawUrl.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  });

  await expect(page).toHaveURL(/result=art_video/);
  await expect(page).not.toHaveURL(/mode=raw/);
  await expect(page.getByRole("tab", { name: "预览", exact: true })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});
