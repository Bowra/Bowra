import { expect, test, type Page, type Route, type TestInfo } from "@playwright/test";

// The mobile Results tab announces unseen output as `成果，有新内容`.
const RESULTS_TAB = /^成果(，有新内容)?$/;

const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";
const capabilities = {
  dto_version: 1,
  workspace: true,
  complete_list: false,
  scale_nav: false,
  max_page_size: 50,
};

async function fulfillCapability(route: Route, workspace = true) {
  await route.fulfill({ json: { ...capabilities, workspace } });
}

async function screenshot(page: Page, testInfo: TestInfo, name: string) {
  const path = testInfo.outputPath(`${name}.png`);
  await page.screenshot({ path });
  await testInfo.attach(name, { path, contentType: "image/png" });
}

async function seedReport(page: Page) {
  await page.request.post(`http://127.0.0.1:${apiPort}/api/v1/_test/reset`);
  await page.route("**/api/v1/results/capabilities", (route) => fulfillCapability(route));
  await page.goto("/");
  const composer = page.locator("textarea").first();
  await expect(composer).toBeVisible();
  await composer.fill("resultsworkspace");
  const sent = page.waitForResponse((response) => response.url().endsWith("/api/v1/chat/send"));
  await composer.press("Enter");
  const { turn_id: turnId, session_id: sessionId } = await (await sent).json();
  await expect(page.locator('[data-presentation="results-reading"]')).toHaveCount(1);
  await expect(page).toHaveURL(/session=/);
  // The shared mock persists canvas sections but returns turns: [] for every
  // session. Supply the completed turn at that external boundary so F5 actually
  // mounts ChatWorkbench (an empty session otherwise opens the work homepage).
  const detail = await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json();
  await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
    ...detail,
    turns: [{
      id: turnId, seq: 1, user_text: "resultsworkspace", intent: "analysis", status: "completed",
      started_at: "2026-09-08T00:00:00Z", ended_at: "2026-09-08T00:00:01Z",
      final_answer: "报告已生成", trace_steps: [], summary: null,
    }],
  } }));
  await page.unroute("**/api/v1/results/capabilities");
}

// A final DOM assertion alone misses a one-frame flash. Observe painted frames
// throughout hydration, not just the screen after the capability response.
async function observeLegacyFrames(page: Page) {
  await page.addInitScript(() => {
    const probe = window as typeof window & { resultsLegacyFrames: number };
    probe.resultsLegacyFrames = 0;
    const inspect = () => {
      const tabs = document.querySelector<HTMLElement>(".v2-ws-right-head .tabs");
      if (tabs && tabs.getBoundingClientRect().width > 0 && tabs.getBoundingClientRect().height > 0) {
        probe.resultsLegacyFrames += 1;
      }
      requestAnimationFrame(inspect);
    };
    requestAnimationFrame(inspect);
  });
}

for (const width of [412, 1280, 1440]) {
  test(`ordinary report reload stays in Results while capabilities are delayed (${width}px)`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 800 });
    await seedReport(page);
    await observeLegacyFrames(page);
    let pending: Route | undefined;
    await page.route("**/api/v1/results/capabilities", (route) => { pending = route; });
    await page.reload();
    // Mobile opens on Chat; the Results entry must also use the stable label.
    if (width === 412) await page.getByRole("tab", { name: RESULTS_TAB }).click();
    const state = page.locator(".results-capability-state");
    await expect(state).toBeVisible();
    await expect(state).toContainText("正在加载成果");
    await expect(page.locator(".results-trigger")).toBeDisabled();
    await expect.poll(() => Boolean(pending)).toBe(true);
    await screenshot(page, testInfo, `results-pending-${width}`);
    await fulfillCapability(pending!);
    await expect(state).toHaveCount(0);
    await expect(page.locator('[data-presentation="results-reading"]')).toBeVisible();
    await expect(page.locator(".results-trigger")).toBeEnabled();
    expect(await page.evaluate(() => (window as typeof window & { resultsLegacyFrames: number }).resultsLegacyFrames)).toBe(0);
  });
}

test("cold error has a working Retry; only explicit disable enables legacy tabs", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await seedReport(page);
  let failed = true;
  await page.route("**/api/v1/results/capabilities", (route) => failed
    ? route.fulfill({ status: 503, body: "temporarily unavailable" })
    : fulfillCapability(route, false));
  await page.reload();
  const state = page.locator(".results-capability-state");
  await expect(state).toContainText("暂时无法加载成果");
  await expect(state.getByRole("alert")).toBeVisible();
  await expect(page.locator(".v2-ws-right-head .tabs")).toHaveCount(0);
  await screenshot(page, testInfo, "results-error");
  failed = false;
  await state.getByRole("button", { name: "重试", exact: true }).click();
  await expect(state).toHaveCount(0);
  await expect(page.locator(".v2-ws-right-head .tabs")).toBeVisible();
  await expect(page.getByRole("tab", { name: "报告", exact: true })).toBeVisible();
});
