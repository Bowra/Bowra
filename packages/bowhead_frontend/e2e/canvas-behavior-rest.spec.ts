import { expect, test, type Locator, type Page } from "@playwright/test";

import { enableResultsWorkspace, mockAuthedApp } from "./_app-mocks";

/**
 * Conversation workbench/report canvas batch 4b remainder (canvas behavior): writing pause/interruption, why export is unavailable while writing, narrow/mobile reading toolbars,
 * source details in a Dialog drawer (citation hover cards unchanged), revision-history arrows, and Esc in selection-toolbar font dropdowns.
 * Runs in Chromium and WebKit (CROSS_BROWSER_SPECS), widths 1440/412.
 * Pure mock without real conversations: report uses `__CANVAS_TEST_HYDRATE__` (same F5 hydration entry) and `__CANVAS_TEST_EVENTS__` (live writing);
 * transport reconnect/give-up and current-turn terminal state use mock runner `__TURN_TEST_STATE__` (mock mode lacks SSE transport), all available only in E2E builds.
 */

const SESSION = "s_wb4b";
const CANVAS = `cv_${SESSION}`;
/** Turn supplied in mockAuthedApp session details (runner's current turn after F5). */
const HYDRATED_TURN = "t_composer_workbench";
/** Current mock-scenario peer turn (runner's current turn in live mode). */
const LIVE_TURN = "t_rho_peer_1";
const sectionsFor = (turnId: string | null) => [
  { id: "sec_wb_h1", kind: "h1", text: "华东区 2026 年第三季度销售额同比下滑的归因分析与后续建议", ...(turnId ? { turn_id: turnId } : null) },
  { id: "sec_wb_narr", kind: "narrative", markdown: "华东区 GMV 环比下降 8.4%，主要来自家电与数码两个品类。", ...(turnId ? { turn_id: turnId } : null) },
];
const REVISIONS = [
  { id: "rev_3", sectionId: "sec_wb_narr", op: "patch", byTurnId: null, createdAt: "2026-09-26T06:25:00Z", prevPayload: { markdown: "旧" }, newPayload: { markdown: "新" } },
  { id: "rev_2", sectionId: "sec_wb_narr", op: "patch", byTurnId: "t_x", createdAt: "2026-09-26T06:18:00Z", prevPayload: { markdown: "更旧" }, newPayload: { markdown: "旧" } },
  { id: "rev_1", sectionId: "sec_wb_narr", op: "append", byTurnId: "t_first", createdAt: "2026-09-26T06:02:00Z" },
];

const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });

async function routeReportApis(page: Page) {
  await page.route(`**/api/v1/canvas/${CANVAS}/status`, (route) =>
    route.fulfill(json({ status: "active", effectiveStatus: "active", quarantinedCount: 0 })),
  );
  await page.route(`**/api/v1/canvas/${CANVAS}/revisions`, (route) => route.fulfill(json({ items: REVISIONS })));
}

/** Add terminal done to this turn's replay (page.tsx reads section count from the last done after F5). */
async function routeTurnDone(page: Page, turnId: string, done: Record<string, unknown>) {
  await page.route("**/api/v1/turns/*/events", (route) => {
    if (!route.request().url().includes(`/turns/${turnId}/events`)) return route.fulfill(json({ events: [] }));
    return route.fulfill(json({
      events: [{ seq: 1, event_type: "done", payload: { turn_id: turnId, ...done }, occurred_at: "2026-08-03T00:00:01Z" }],
    }));
  });
}

async function hydrate(page: Page, sections: unknown[]) {
  await page.waitForFunction(() => "__CANVAS_TEST_HYDRATE__" in window, undefined, { timeout: 15_000 });
  await page.evaluate(([sid, secs]) => {
    (window as unknown as { __CANVAS_TEST_HYDRATE__: (p: unknown) => void }).__CANVAS_TEST_HYDRATE__({
      canvas: { id: `cv_${sid}`, sessionId: sid, title: "华东区 2026 年第三季度销售额同比下滑的归因分析与后续建议" },
      sections: secs,
    });
  }, [SESSION, sections] as const);
}

async function open(page: Page, width: number, sessionTurnStatus?: string) {
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await routeReportApis(page);
  await enableResultsWorkspace(page);
  if (sessionTurnStatus) {
    // Replace the last turn in session details with the specified terminal state (F5 hydrates runner's current-turn state from here).
    await page.route(`**/api/v1/sessions/${SESSION}`, (route) =>
      route.fulfill(json({
        session: {
          id: SESSION,
          workspace_id: "ws_smoke",
          title: "Composer workbench fixture",
          started_at: "2026-08-03T00:00:00Z",
          last_active_at: "2026-08-03T00:00:00Z",
          status: "completed",
          cost_usd: 0,
          tokens_total: 0,
          intents: [],
        },
        turns: [{
          id: HYDRATED_TURN,
          seq: 1,
          user_text: "Existing workbench turn",
          intent: "open_qa",
          status: sessionTurnStatus,
          execution_generation: 1,
          started_at: "2026-08-03T00:00:00Z",
          ended_at: "2026-08-03T00:00:01Z",
          final_answer: "Existing answer",
          error: null,
          error_kind: null,
          agent_type: "lead",
          attachments: [],
          trace_steps: [],
          summary: null,
        }],
      })),
    );
  }
  await page.goto(`/?session=${SESSION}`);
  await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
}

/** On mobile, first switch to "成果", then open this report through deliverable navigation. */
async function showResults(page: Page, width: number) {
  if (width >= 1200) return;
  await page.getByRole("tab", { name: /^(成果|报告)/ }).click();
  const nav = page.getByRole("navigation", { name: "成果导航" });
  if (await nav.isVisible().catch(() => false)) {
    await nav.getByRole("button", { name: /(可编辑|只读)报告$/ }).first().click();
  }
}

/** Live: keep the current turn running (swallow mock timeline timers beyond 1.5s); write the report through canvas events. */
async function openLive(page: Page, width: number, sections: unknown[] = sectionsFor(null)) {
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = ((fn: TimerHandler, delay?: number, ...args: unknown[]) =>
      typeof delay === "number" && delay > 1500 && delay < 30_000 ? 0 : original(fn, delay, ...args)) as typeof window.setTimeout;
  });
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await routeReportApis(page);
  await enableResultsWorkspace(page);
  // "刷新报告" fetches the report again; server returns the same report.
  await page.route("**/api/v1/canvas/by-session/**", (route) =>
    route.fulfill(json({
      canvas: { id: CANVAS, sessionId: SESSION, workspaceId: "ws_smoke", title: "华东 Q3 下滑归因", status: "done", createdAt: "2026-09-26T00:00:00Z", updatedAt: "2026-09-26T00:00:00Z" },
      sections,
      sourceRefs: [],
    })),
  );
  await page.goto("/?scenario=peer");
  await expect(page.locator(".bh-band__bar").first()).toBeVisible({ timeout: 15_000 });
  await hydrate(page, sections);
  await showResults(page, width);
}

type Events = Record<string, (payload: unknown) => void>;
async function emit(page: Page, name: string, payload: unknown) {
  await page.evaluate(([n, p]) => {
    (window as unknown as { __CANVAS_TEST_EVENTS__: Events }).__CANVAS_TEST_EVENTS__[n](p);
  }, [name, payload] as const);
}
async function turnState(page: Page, patch: Record<string, unknown>) {
  await page.waitForFunction(() => "__TURN_TEST_STATE__" in window, undefined, { timeout: 15_000 });
  await page.evaluate((p) => {
    (window as unknown as { __TURN_TEST_STATE__: (patch: unknown) => void }).__TURN_TEST_STATE__(p);
  }, patch);
}
async function startWriting(page: Page) {
  await emit(page, "turnStreaming", { turn_id: "t_wb4b" });
  await emit(page, "sectionStreaming", { section_id: "sec_wb_writing", kind: "narrative" });
  await emit(page, "chunk", { section_id: "sec_wb_writing", delta: "## 闭店影响\n闭店影响约占 1.2 个百分点" });
}

async function hitsItself(target: Locator): Promise<boolean> {
  return target.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return Boolean(hit && (hit === el || el.contains(hit)));
  });
}

const toolbar = (page: Page) => page.locator(".results-report-object-toolbar");
const moreButton = (page: Page) => toolbar(page).getByRole("button", { name: "更多操作" });

for (const width of [1440, 412]) {
  test.describe(`写作中：导出不可用的原因、暂停与中断（${width}）`, () => {
    test("live: export is unavailable while writing with the reason shown; reconnecting shows paused, giving up shows interrupted with Refresh report", async ({ page }) => {
      // This turn wrote report sections (their turn_id is current); "连接中断" remains on the report after canvas completes.
      await openLive(page, width, sectionsFor(LIVE_TURN));
      await startWriting(page);
      const hint = toolbar(page).locator(".results-report-writing-hint");
      await expect(hint).toHaveText("写作完成后可以编辑和导出");
      expect(await hitsItself(hint)).toBe(true);
      if (width >= 1200) {
        const exportButton = toolbar(page).locator(".ws-export");
        await expect(exportButton).toBeDisabled();
        await expect(exportButton).toHaveAttribute("aria-describedby", (await hint.getAttribute("id"))!);
      } else {
        // Below 560: export is in "更多", disabled while writing with an explanation.
        await expect(toolbar(page).locator(".ws-export")).toHaveCount(0);
        await moreButton(page).click();
        const item = page.getByRole("menuitem", { name: "导出" });
        await expect(item).toHaveAttribute("aria-disabled", "true");
        await expect(page.locator(".ui-menu-reason", { hasText: "报告写完后可以导出" })).toBeVisible();
        expect(await hitsItself(item)).toBe(true);
        await page.keyboard.press("Escape");
      }
      // First row (status group) remains 44 (touchscreen 61); explanation occupies the second row.
      const rowTop = (await toolbar(page).boundingBox())!.y;
      expect(Math.round((await hint.boundingBox())!.y - rowTop)).toBeLessThanOrEqual(width >= 1200 ? 44 : 61);

      // Transport disconnected/reconnecting: writing progress becomes paused.
      await turnState(page, { streamingState: "error" });
      const stream = toolbar(page).getByTestId("report-toolbar-streaming");
      await expect(stream).toHaveAttribute("data-state", "paused");
      await expect(stream).toContainText("输出已暂停，正在重新连接");
      expect(await hitsItself(stream)).toBe(true);
      // Reconnect succeeds: resume.
      await turnState(page, { streamingState: "active" });
      await expect(stream).not.toHaveAttribute("data-state", "paused");
      // Give up reconnecting: interruption explanation (error color) + "刷新报告", immediately announced by screen readers.
      await turnState(page, { streamingState: "error", running: false });
      const outcome = toolbar(page).getByTestId("report-toolbar-outcome");
      await expect(outcome).toHaveAttribute("data-outcome", "lost");
      await expect(outcome).toContainText("连接中断，已写的部分已保存");
      const refresh = outcome.getByRole("button", { name: "刷新报告" });
      expect(await hitsItself(refresh)).toBe(true);
      await expect(toolbar(page).getByTestId("report-toolbar-announcer")).toHaveText("连接中断，已写的部分已保存");
      // Canvas subsequently marked complete by fallback: export enabled again, reason row gone; explanation remains (not overwritten by "写作已结束").
      await expect(hint).toHaveCount(0);
      // Entire explanation visible (wrap at 412 without truncating to "…").
      const noteText = outcome.locator(".rc-toolbar-outcome-text");
      expect(await noteText.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
      expect(await hitsItself(noteText)).toBe(true);
      await page.waitForTimeout(2_000);
      await expect(toolbar(page).getByTestId("report-toolbar-announcer")).toHaveText("连接中断，已写的部分已保存");
      // "刷新报告" refetches this report by-session; explanation remains because the current turn's connection interruption survives report refresh.
      const refetched = page.waitForRequest((request) => request.url().includes("/api/v1/canvas/by-session/"));
      await refresh.click();
      await refetched;
      await expect(page.getByText("华东区 GMV 环比下降 8.4%")).toBeVisible();
      await expect(outcome).toBeVisible();
    });

    test("live: a disconnect in a turn unrelated to the report (not writing, did not write this report) does not say connection lost on the report toolbar", async ({ page }) => {
      await openLive(page, width, sectionsFor(null));
      await expect(toolbar(page).locator(".rc-status-trigger")).toBeVisible();
      await turnState(page, { streamingState: "error", running: false });
      await page.waitForTimeout(500);
      await expect(toolbar(page).getByTestId("report-toolbar-outcome")).toHaveCount(0);
      // Unreadable terminal frame (runner records failed) after this turn wrote a report: terminal-state explanation "写作中断", not a connection interruption (same as F5).
      await hydrate(page, sectionsFor(LIVE_TURN));
      await turnState(page, { streamingState: "error", running: false, terminalStatus: "failed" });
      await expect(toolbar(page).getByTestId("report-toolbar-outcome")).toHaveAttribute("data-outcome", "failed");
      await expect(toolbar(page).getByTestId("report-toolbar-outcome")).toHaveText("写作中断，已写的部分已保存");
    });

    test("live: a stopped turn that wrote this report keeps a note next to the badge; with a section count in done it says N sections saved", async ({ page }) => {
      await openLive(page, width, sectionsFor(LIVE_TURN));
      await expect(toolbar(page).getByTestId("report-toolbar-outcome")).toHaveCount(0);
      // done lacks report_sections_saved (old protocol / bypass path): display no number.
      await turnState(page, { running: false, terminalStatus: "aborted" });
      const outcome = toolbar(page).getByTestId("report-toolbar-outcome");
      await expect(outcome).toHaveText("已停止，已写的部分已保存");
      expect(await hitsItself(outcome)).toBe(true);
      await expect(toolbar(page).locator(".rc-status-trigger")).toBeVisible();
      // done carries report_sections_saved=3 (live SSE done supplies runner).
      await turnState(page, { reportSectionsSaved: 3 });
      await expect(outcome).toHaveText("已停止，已保存 3 节");
    });

    test("F5: the same note is rebuilt from the hydrated last-turn terminal state, the done section count and section turns", async ({ page }) => {
      await open(page, width, "aborted");
      await routeTurnDone(page, HYDRATED_TURN, { status: "aborted", report_sections_saved: 3 });
      await hydrate(page, sectionsFor(HYDRATED_TURN));
      await page.reload();
      await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
      await hydrate(page, sectionsFor(HYDRATED_TURN));
      await showResults(page, width);
      const outcome = toolbar(page).getByTestId("report-toolbar-outcome");
      await expect(outcome).toHaveText("已停止，已保存 3 节");
      // F5 does not reannounce terminal state.
      await expect(toolbar(page).getByTestId("report-toolbar-announcer")).toHaveText("");
    });

    test("F5: without a section count no number is written; a revision turn (0 sections, none added this turn) shows no note", async ({ page }) => {
      await open(page, width, "aborted");
      await routeTurnDone(page, HYDRATED_TURN, { status: "aborted" });
      await hydrate(page, sectionsFor(HYDRATED_TURN));
      await showResults(page, width);
      await expect(toolbar(page).getByTestId("report-toolbar-outcome")).toHaveText("已停止，已写的部分已保存");
      await page.unroute("**/api/v1/turns/*/events");
      await routeTurnDone(page, HYDRATED_TURN, { status: "aborted", report_sections_saved: 0 });
      await page.reload();
      await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
      // Revision turn only edits previous-turn sections; report has no sections from this turn.
      await hydrate(page, sectionsFor("t_earlier"));
      await showResults(page, width);
      await expect(toolbar(page).locator(".rc-status-trigger")).toBeVisible();
      await expect(toolbar(page).getByTestId("report-toolbar-outcome")).toHaveCount(0);
    });
  });

  test.describe(`窄工具条与手机阅读工具条（${width}）`, () => {
    test("wide screens keep Export in the toolbar; at 412 Export and Edit move into More, and Done returns to the toolbar after editing from More", async ({ page }) => {
      await open(page, width);
      // Refetch report after "完成": server returns the same report (an empty report closes deliverables, a different state).
      await page.route("**/api/v1/canvas/by-session/**", (route) =>
        route.fulfill(json({
          canvas: { id: CANVAS, sessionId: SESSION, workspaceId: "ws_smoke", title: "华东区 2026 年第三季度销售额同比下滑的归因分析与后续建议", status: "done", createdAt: "2026-09-26T00:00:00Z", updatedAt: "2026-09-26T00:00:00Z" },
          sections: sectionsFor(null),
          sourceRefs: [],
        })),
      );
      await hydrate(page, sectionsFor(null));
      await showResults(page, width);
      if (width >= 1200) {
        await expect(toolbar(page).locator(".ws-export")).toBeVisible();
        await expect(toolbar(page).getByRole("button", { name: "编辑此报告" })).toBeVisible();
        await expect(toolbar(page)).not.toHaveAttribute("data-phone-reading", "true");
        return;
      }
      await expect(toolbar(page)).toHaveAttribute("data-phone-reading", "true");
      await expect(toolbar(page).locator(".ws-export")).toHaveCount(0);
      await expect(toolbar(page).getByRole("button", { name: "编辑此报告" })).toHaveCount(0);
      // Merge deliverables header/toolbar into one row: back · title · status · share · ⋯, all individually hittable on the same line.
      await expect(page.locator(".v2-ws-right-head")).toBeHidden();
      const back = toolbar(page).getByRole("button", { name: "返回成果列表" });
      const title = toolbar(page).locator(".results-report-phone-title");
      const chip = toolbar(page).locator(".rc-status-trigger");
      const share = toolbar(page).getByRole("button", { name: "分享报告" });
      for (const el of [back, title, chip, share, moreButton(page)]) {
        await expect(el).toBeVisible();
        expect(await hitsItself(el)).toBe(true);
      }
      const rowY = async (el: Locator) => Math.round((await el.boundingBox())!.y + (await el.boundingBox())!.height / 2);
      const y = await rowY(back);
      for (const el of [title, chip, share, moreButton(page)]) expect(Math.abs((await rowY(el)) - y)).toBeLessThanOrEqual(2);
      // Long title ellipsized on one line; full title remains in title attribute and screen-reader text.
      await expect(title).toHaveAttribute("title", "华东区 2026 年第三季度销售额同比下滑的归因分析与后续建议");
      expect(await title.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
      expect(await toolbar(page).ariaSnapshot()).toMatch(/button "返回成果列表"[\s\S]*华东区 2026 年第三季度销售额同比下滑的归因分析与后续建议[\s\S]*button "分享报告"/);
      await moreButton(page).click();
      const exportItem = page.getByRole("menuitem", { name: "导出" });
      await expect(exportItem).toBeVisible();
      expect(await hitsItself(exportItem)).toBe(true);
      await exportItem.click();
      const pdf = page.getByRole("menuitem", { name: /PDF/ });
      await expect(pdf).toBeVisible();
      expect(await hitsItself(pdf)).toBe(true);
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      await expect(page.getByRole("menuitem", { name: /PDF/ })).toHaveCount(0);
      await moreButton(page).click();
      await page.getByRole("menuitem", { name: "编辑此报告" }).click();
      await expect(toolbar(page).getByRole("button", { name: "完成" })).toBeVisible();
      await expect(toolbar(page)).not.toHaveAttribute("data-phone-reading", "true");
      await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "on");
      // Editing restores the header (back is there); toolbar shows edit actions.
      await expect(page.locator(".v2-ws-right-head")).toBeVisible();
      // Click "完成": stay on deliverables, restore merged bar, focus toolbar "更多操作" (edit entry), never drop focus onto the page.
      await toolbar(page).getByRole("button", { name: "完成" }).click();
      await expect(toolbar(page)).toHaveAttribute("data-phone-reading", "true");
      await expect(page.getByRole("tab", { selected: true })).toHaveText(/成果/);
      await expect(moreButton(page)).toBeFocused();
    });

    test("Back in the merged phone reading toolbar returns to the results list", async ({ page }) => {
      test.skip(width >= 1200, "只在手机宽度");
      await open(page, width);
      await hydrate(page, sectionsFor(null));
      await showResults(page, width);
      await toolbar(page).getByRole("button", { name: "返回成果列表" }).click();
      await expect(page.getByRole("navigation", { name: "成果导航" })).toBeVisible();
    });
  });

  test.describe(`修改历史列表的方向键（${width}）`, () => {
    test("↑/↓ move between records, End / Home go to the ends, Enter expands the change; no row is covered", async ({ page, browserName }) => {
      await open(page, width);
      await hydrate(page, sectionsFor(null));
      await showResults(page, width);
      await page.locator(".canvas-report-history-btn").click();
      const list = page.getByRole("listbox", { name: "修改记录" });
      await expect(list).toBeVisible();
      const rows = list.getByRole("option");
      await expect(rows).toHaveCount(3);
      for (let i = 0; i < 3; i += 1) expect(await hitsItself(rows.nth(i))).toBe(true);
      await rows.first().focus();
      await page.keyboard.press("ArrowDown");
      await expect(rows.nth(1)).toBeFocused();
      await page.keyboard.press("End");
      await expect(rows.nth(2)).toBeFocused();
      await page.keyboard.press("Home");
      await expect(rows.nth(0)).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(rows.nth(0)).toHaveAttribute("aria-selected", "true");
      await expect(rows.nth(0).getByText("正文", { exact: true })).toBeVisible();
      // Tab leaves the list for the sheet footer's restore action (WebKit skips buttons by default; use ⌥Tab).
      await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
      await expect(page.getByRole("dialog", { name: "修改历史" }).getByRole("button", { name: "恢复此版本…" })).toBeFocused();
      expect(await list.ariaSnapshot()).toMatch(/listbox "修改记录"[\s\S]*option/);
    });
  });
}

test.describe("手机合并工具条的触控区（360，触屏）", () => {
  test.use({ hasTouch: true, isMobile: true });

  test("share, More and the status badge each have a hit area of at least 44 and hit themselves", async ({ page }) => {
    await open(page, 360);
    await hydrate(page, sectionsFor(null));
    await showResults(page, 360);
    await expect(toolbar(page)).toHaveAttribute("data-phone-reading", "true");
    const targets = [
      toolbar(page).getByRole("button", { name: "分享报告" }),
      moreButton(page),
      toolbar(page).locator(".rc-status-trigger"),
      toolbar(page).getByRole("button", { name: "返回成果列表" }),
    ];
    for (const el of targets) {
      const box = (await el.boundingBox())!;
      expect(Math.round(box.width)).toBeGreaterThanOrEqual(44);
      expect(Math.round(box.height)).toBeGreaterThanOrEqual(44);
      expect(await hitsItself(el)).toBe(true);
    }
    // Entire row has no horizontal overflow.
    expect(await toolbar(page).evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  });
});

const NUMBERED = "[data-citation-kind='numbered']";
const SOURCE_URL = "/api/v1/canvas/by-session/sess_spike_p31/artifacts/art_demo_numbered_001";

async function openSpike(page: Page, width: number, path = "/spike/p31") {
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await page.route(`**${SOURCE_URL}/preview**`, (route) => route.fulfill(json({ family: "text", mode: "text", text: "P0-A numbered source preview" })));
  await page.goto(path);
  await page.waitForSelector(NUMBERED, { state: "attached", timeout: 20_000 });
  // Body keeps shifting until fixture charts finish asynchronous rendering, misplacing hover/clicks; await stable layout.
  await expect.poll(async () => {
    const a = await page.locator(NUMBERED).first().boundingBox();
    await page.waitForTimeout(400);
    const b = await page.locator(NUMBERED).first().boundingBox();
    return Boolean(a && b && a.y === b.y);
  }, { timeout: 15_000 }).toBe(true);
}

for (const width of [1440, 412]) {
  test.describe(`来源详情抽屉与角标（${width}）`, () => {
    test("hover still shows the source card; click opens a body-portaled Dialog drawer (a bottom sheet at 412) on top with the card closed; Esc returns to the badge", async ({ page }) => {
      await openSpike(page, width);
      const chip = page.locator(NUMBERED).first();
      await chip.scrollIntoViewIfNeeded();
      await chip.hover();
      await expect(page.locator(".citation-popover")).toBeVisible();
      await chip.click();
      await expect(page.locator(".citation-popover")).toHaveCount(0);
      const drawer = page.getByRole("dialog");
      await expect(drawer).toBeVisible();
      await expect(drawer).toHaveClass(/settings-dialog--drawer/);
      expect(await drawer.evaluate((el) => !el.closest(".canvas-report-shell, .v2-ws-right-body"))).toBe(true);
      await expect(page.locator("aside.legacy-source-panel")).toHaveCount(0);
      await expect(drawer.getByTestId("source-panel-drawer")).toContainText("P0-A numbered source preview");
      if (width < 640) await expect(drawer).toHaveAttribute("data-dialog-presentation", "sheet");
      else {
        const box = (await drawer.boundingBox())!;
        expect(Math.round(box.width)).toBeLessThanOrEqual(420);
        expect(Math.round(box.x + box.width)).toBe(width);
      }
      expect(await hitsItself(drawer.getByRole("button", { name: "关闭" }))).toBe(true);
      expect(await hitsItself(drawer.getByTestId("source-panel-drawer"))).toBe(true);
      await page.keyboard.press("Escape");
      // Drawer closes; hovering the citation still shows its card when the pointer remains (production interaction). Inspect only the drawer here.
      await expect(page.locator(".settings-dialog--drawer")).toHaveCount(0);
      await expect(chip).toBeFocused();
      // Keyboard opening: focus enters drawer.
      await page.mouse.move(0, 0);
      await chip.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(".settings-dialog--drawer")).toBeVisible();
      await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest("[role='dialog']")))).toBe(true);
      await page.keyboard.press("Escape");
      await expect(chip).toBeFocused();
    });
  });
}

async function openEditFixture(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockAuthedApp(page);
  await page.goto("/spike/p31?designEdit=1");
  await expect(page.locator(".v2-section-chrome").first()).toBeAttached({ timeout: 20_000 });
}

async function selectFirstWords(page: Page, length = 6) {
  const para = page.locator(".ProseMirror p").first();
  await para.scrollIntoViewIfNeeded();
  await para.evaluate((el, n) => {
    const node = document.createTreeWalker(el, NodeFilter.SHOW_TEXT).nextNode()!;
    const range = document.createRange();
    range.setStart(node, 0);
    range.setEnd(node, Math.min(n, node.textContent!.length));
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
  }, length);
  const box = (await para.boundingBox())!;
  await page.mouse.move(box.x + 5, box.y + 5);
  await page.mouse.up();
}

/** Count selection-toolbar node removals/additions (whether it reopens after dismissal). */
async function watchToolbar(page: Page) {
  await page.evaluate(() => {
    const w = window as unknown as { __tb: string[] };
    w.__tb = [];
    new MutationObserver((records) => {
      for (const r of records) {
        for (const n of Array.from(r.addedNodes)) if (n instanceof HTMLElement && (n.matches(".rc-sel-toolbar") || n.querySelector(".rc-sel-toolbar"))) w.__tb.push("add");
        for (const n of Array.from(r.removedNodes)) if (n instanceof HTMLElement && (n.matches(".rc-sel-toolbar") || n.querySelector(".rc-sel-toolbar"))) w.__tb.push("rm");
      }
    }).observe(document.body, { childList: true, subtree: true });
  });
  return () => page.evaluate(() => (window as unknown as { __tb: string[] }).__tb);
}

test.describe("选区工具栏：字体下拉里按 Esc（两种浏览器统一）", () => {
  for (const how of ["pointer", "keyboard"] as const) {
    test(`${how} opens the dropdown: the first Esc closes only the dropdown and returns focus to Font; the second closes the toolbar back to the body, keeping the selection without reopening`, async ({ page }) => {
      await openEditFixture(page);
      await selectFirstWords(page);
      const bar = page.locator(".rc-sel-toolbar");
      await expect(bar).toBeVisible();
      const selected = await page.evaluate(() => window.getSelection()!.toString());
      if (how === "pointer") {
        await bar.getByRole("button", { name: "文字样式" }).click();
        await bar.getByRole("button", { name: "字体" }).click();
      } else {
        await page.keyboard.press("Alt+Enter");
        const aa = bar.getByRole("button", { name: "文字样式" });
        for (let i = 0; i < 8 && !(await aa.evaluate((el) => el === document.activeElement)); i += 1) await page.keyboard.press("ArrowRight");
        await page.keyboard.press("Enter");
        await page.keyboard.press("ArrowRight");
        await expect(bar.getByRole("button", { name: "字体" })).toBeFocused();
        await page.keyboard.press("Enter");
      }
      const menu = page.getByRole("menu", { name: "字体" });
      await expect(menu).toBeVisible();
      const events = await watchToolbar(page);
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      await expect(bar.getByRole("button", { name: "字体" })).toBeFocused();
      await expect(bar).toBeVisible();
      expect(await events()).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(bar).toHaveCount(0);
      await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest(".ProseMirror")))).toBe(true);
      expect(await page.evaluate(() => window.getSelection()!.toString())).toBe(selected);
      await page.waitForTimeout(500);
      expect(await events()).toEqual(["rm"]);
      await expect(bar).toHaveCount(0);
      // A different selection still opens normally.
      await selectFirstWords(page, 4);
      await expect(bar).toBeVisible();
    });
  }
});

for (const width of [412, 1440]) {
  test(`read-only shared view (${width}): no More and no edit entry, no writing reason line`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockAuthedApp(page);
    await page.route("**/api/v1/sessions/s_shared", (route) => route.fulfill(json({ detail: "forbidden" }, 403)));
    await page.route("**/api/v1/canvas/cv_shared/status", (route) =>
      route.fulfill(json({ status: "verified", effectiveStatus: "verified", quarantinedCount: 0 })),
    );
    await page.route("**/api/v1/canvas/cv_shared/revisions", (route) => route.fulfill(json({ items: REVISIONS })));
    await page.route("**/api/v1/canvas/by-session/s_shared**", (route) =>
      route.fulfill(json({
        canvas: { id: "cv_shared", sessionId: "s_shared", workspaceId: "ws_shared", title: "Shared H1 Report", status: "done", createdAt: "2026-07-03T00:00:00Z", updatedAt: "2026-07-03T00:00:00Z" },
        sections: [
          { id: "h1_shared", kind: "h1", text: "Shared H1 Report" },
          { id: "n_shared", kind: "narrative", markdown: "Dogfood shared report body is visible." },
        ],
        sourceRefs: [],
      })),
    );
    await enableResultsWorkspace(page);
    await page.goto("/?session=s_shared");
    await expect(page.getByText("Dogfood shared report body is visible.")).toBeVisible({ timeout: 15_000 });
    await showResults(page, width);
    const bar = toolbar(page);
    await expect(bar).toBeVisible();
    await expect(bar.getByRole("button", { name: "更多操作" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "编辑此报告" })).toHaveCount(0);
    await expect(page.getByRole("menuitem", { name: "编辑此报告" })).toHaveCount(0);
    await expect(bar.locator(".results-report-writing-hint")).toHaveCount(0);
    await expect(page.locator(".rc-actions, .rc-handle, .rc-mdots")).toHaveCount(0);
    // Revision history is read-only: no "回滚到此版本"/"恢复已删区块" (server already rejects nonowners).
    await page.locator(".canvas-report-history-btn").click();
    const sheet = page.getByRole("dialog", { name: "修改历史" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole("option").first()).toBeVisible();
    await expect(sheet.getByRole("button", { name: /恢复/ })).toHaveCount(0);
  });
}

// ── Render source-detail document excerpts as Markdown (existing bug fix, both appearances) ──
const MD_ALIAS = `source-ref-v1:${"wb4bMarkdownSource".padEnd(43, "0")}`;
const MD_PREVIEW_URL = "/api/v1/canvas/by-session/s_md/artifacts/art_md_001/preview";
const MD_TEXT = [
  "## 华东区经营月报",
  "",
  "- 门店 **42** 家",
  "- 营业额 `3.86` 亿元",
  "",
  "| 区域 | 营业额 | 同比 | 环比 | 门店数 | 客单价 | 备注说明文字较长 |",
  "| --- | --- | --- | --- | --- | --- | --- |",
  "| 上海 | 1.2 | +3% | -1% | 12 | 256 | 这一列故意写得很长用来验证横向滚动 |",
  "",
  "```",
  "select region, sum(gmv) from sales group by region order by 2 desc limit 100 -- 一行很长的 SQL 用来验证代码块横向滚动",
  "```",
  "",
  "[原文](https://example.com/report)",
  "",
  "![像素](https://attacker.example/pixel.png)",
  "",
  `${">".repeat(300)} 深层引用`,
  "",
  "<script>window.__pwned = 1</script>",
  "<img src=x onerror=\"window.__pwned = 2\">",
].join("\n");
const mdSections = (turnId: string | null) => [
  { id: "sec_md_h1", kind: "h1", text: "来源摘录报告", ...(turnId ? { turn_id: turnId } : null) },
  { id: "sec_md_narr", kind: "narrative", markdown: "三季度营业额 3.86 亿元 [1]。", ...(turnId ? { turn_id: turnId } : null) },
  {
    id: "sec_md_sources",
    kind: "sources",
    label: "数据来源",
    entries: [{
      id: MD_ALIAS,
      source_alias: MD_ALIAS,
      access_state: "linked_accessible",
      generic_type: "document",
      ordinal: 1,
      safe_label: "华东区经营月报.md",
      safe_preview: MD_TEXT,
      safe_preview_url: MD_PREVIEW_URL,
    }],
  },
];

async function routeMdPreview(page: Page) {
  await page.route(`**${MD_PREVIEW_URL}**`, (route) => route.fulfill(json({ family: "document", mode: "text", text: null })));
}

async function expectMarkdownDrawer(page: Page) {
  // Excerpt Markdown images never load automatically (no requests, avoiding tracking pixels/intranet requests).
  const imageRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("attacker.example")) imageRequests.push(request.url());
  });
  await page.evaluate(() => {
    (window as unknown as { __pwned?: number }).__pwned = undefined;
  });
  const chip = page.locator("[data-citation-kind='numbered']").first();
  await chip.scrollIntoViewIfNeeded();
  await chip.click();
  const md = page.getByTestId("source-panel-markdown");
  await expect(md).toBeVisible();
  await expect(md.getByRole("heading", { name: "华东区经营月报" })).toBeVisible();
  await expect(md.locator("li")).toHaveCount(2);
  await expect(md.locator("strong", { hasText: "42" })).toBeVisible();
  await expect(md.locator("table td").first()).toHaveText("上海");
  const link = md.getByRole("link", { name: "原文" });
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  expect(await md.locator("script, img, [onerror]").count()).toBe(0);
  expect(await md.textContent()).not.toMatch(/##|\*\*/);
  // Drawer headings are not enlarged; code blocks scroll inside their own boxes without widening the drawer.
  expect(parseFloat(await md.getByRole("heading").evaluate((el) => getComputedStyle(el).fontSize))).toBeLessThanOrEqual(14);
  // Lists have bullets; table cells do not wrap one character per line; oversized tables scroll horizontally themselves.
  expect(await md.locator("ul").evaluate((el) => getComputedStyle(el).listStyleType)).toBe("disc");
  const table = md.locator("table");
  expect(await table.evaluate((el) => getComputedStyle(el).overflowX)).toBe("auto");
  expect(await table.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
  const pre = md.locator("pre");
  expect(await pre.evaluate((el) => getComputedStyle(el).overflowX)).toBe("auto");
  expect(await pre.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
  // Deep nesting does not widen drawer/panel; excerpt area scrolls horizontally itself.
  expect(await md.evaluate((el) => getComputedStyle(el).overflowX)).toBe("auto");
  const panel = md.locator("xpath=ancestor::*[@role='dialog'][1]");
  expect(await panel.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  expect(await md.evaluate((el) => el.getBoundingClientRect().width)).toBeLessThanOrEqual(480);
  // Images become links labeled with alt text.
  expect(await md.locator("img").count()).toBe(0);
  const image = md.locator("[data-md-image]");
  await expect(image).toHaveText("像素");
  await expect(image).toHaveAttribute("rel", "noopener noreferrer");
  await page.waitForTimeout(500);
  expect(imageRequests).toEqual([]);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
}

for (const width of [1440, 412]) {
  test.describe(`来源详情摘录按 markdown 渲染（${width}）`, () => {
    test("F5: after refresh, clicking a badge shows the excerpt formatted in the drawer, without script / onerror reaching the page", async ({ page }) => {
      await routeMdPreview(page);
      await open(page, width);
      await hydrate(page, mdSections(null));
      await page.reload();
      await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
      await hydrate(page, mdSections(null));
      await showResults(page, width);
      await expectMarkdownDrawer(page);
    });

    test("live: a sources section written during the turn renders the same after writing", async ({ page }) => {
      await routeMdPreview(page);
      await openLive(page, width);
      await emit(page, "turnStreaming", { turn_id: "t_wb4b" });
      for (const section of mdSections(LIVE_TURN).slice(1)) await emit(page, "added", { section });
      await emit(page, "done", { errors: [] });
      await expectMarkdownDrawer(page);
    });

    test("read-only shared view: the excerpt renders the same", async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await mockAuthedApp(page);
      await page.route("**/api/v1/sessions/s_md", (route) => route.fulfill(json({ detail: "forbidden" }, 403)));
      await page.route("**/api/v1/canvas/cv_md/status", (route) =>
        route.fulfill(json({ status: "verified", effectiveStatus: "verified", quarantinedCount: 0 })),
      );
      await page.route("**/api/v1/canvas/by-session/s_md**", (route) =>
        route.fulfill(json({
          canvas: { id: "cv_md", sessionId: "s_md", workspaceId: "ws_shared", title: "来源摘录报告", status: "done", createdAt: "2026-07-03T00:00:00Z", updatedAt: "2026-07-03T00:00:00Z" },
          sections: mdSections(null),
          sourceRefs: [],
        })),
      );
      // Later registered routes match first; source probes use their own route.
      await routeMdPreview(page);
      await enableResultsWorkspace(page);
      await page.goto("/?session=s_md");
      await expect(page.getByText("三季度营业额").first()).toBeVisible({ timeout: 15_000 });
      await showResults(page, width);
      await expectMarkdownDrawer(page);
    });
  });
}
