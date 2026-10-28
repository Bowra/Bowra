import { expect, test, type Locator, type Page } from "@playwright/test";

import { enableResultsWorkspace, mockAuthedApp } from "./_app-mocks";

/**
 * Chat Workbench/report canvas batch 4b (canvas behavior): real-browser toolbar status group, sharing entry, chapter menu, source cards,
 * selection-toolbar keyboard entry, revision-history wording, read-only sharing view. Chromium and WebKit (CROSS_BROWSER_SPECS),
 * widths 1440 and 412. Mock only, no real chat: reports use e2e-only __CANVAS_TEST_HYDRATE__ (same F5 hydration entry)
 * and __CANVAS_TEST_EVENTS__ (live writing, same store entries as SSE canvas events) to fill canvas store.
 */

const SESSION = "s_wb4b";
const CANVAS = `cv_${SESSION}`;
const SECTIONS = [
  { id: "sec_wb_h1", kind: "h1", text: "华东 Q3 下滑归因" },
  { id: "sec_wb_narr", kind: "narrative", markdown: "华东区 GMV 环比下降 8.4%，主要来自家电与数码两个品类。" },
];
const REVISIONS = [
  { id: "rev_2", sectionId: "sec_wb_narr", op: "patch", byTurnId: null, createdAt: "2026-09-26T06:25:00Z", prevPayload: { markdown: "旧" }, newPayload: { markdown: "新" } },
  { id: "rev_1", sectionId: "sec_wb_narr", op: "append", byTurnId: "t_first", createdAt: "2026-09-26T06:02:00Z" },
];

const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });

async function routeReportApis(page: Page) {
  await page.route(`**/api/v1/canvas/${CANVAS}/status`, (route) =>
    route.fulfill(json({ status: "active", effectiveStatus: "active", quarantinedCount: 0 })),
  );
  await page.route(`**/api/v1/canvas/${CANVAS}/revisions`, (route) => route.fulfill(json({ items: REVISIONS })));
  await page.route(`**/api/v1/shares/report/${SESSION}`, (route) =>
    route.fulfill(json({
      kind: "report",
      resource_id: SESSION,
      label: "华东 Q3 下滑归因",
      audience: "private",
      owner_id: "u_smoke",
      owner_name: "Smoke User",
      can_manage: true,
      grants: [],
    })),
  );
  await page.route("**/api/v1/team/directory**", (route) => route.fulfill(json({ items: [] })));
  await page.route("**/api/v1/team/groups-directory", (route) => route.fulfill(json({ items: [] })));
}

async function open(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await routeReportApis(page);
  // The Results toolbar belongs to the Results workspace (deployment flag); the e2e mock reports it on.
  await enableResultsWorkspace(page);
  await page.goto(`/?session=${SESSION}`);
  await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
}

async function hydrate(page: Page, sections: unknown[] = SECTIONS) {
  await page.waitForFunction(() => "__CANVAS_TEST_HYDRATE__" in window, undefined, { timeout: 15_000 });
  await page.evaluate(([sid, secs]) => {
    (window as unknown as { __CANVAS_TEST_HYDRATE__: (p: unknown) => void }).__CANVAS_TEST_HYDRATE__({
      canvas: { id: `cv_${sid}`, sessionId: sid, title: "华东 Q3 下滑归因" },
      sections: secs,
    });
  }, [SESSION, sections] as const);
}

/** At mobile width, switch to “成果” first (segmented control’s second item). */
async function showResults(page: Page, width: number) {
  if (width >= 1200) return;
  await page.getByRole("tab", { name: /^成果/ }).click();
  // Mobile Results first shows navigation (list/details): open this report.
  const nav = page.getByRole("navigation", { name: "成果导航" });
  if (await nav.isVisible().catch(() => false)) {
    await nav.getByRole("button", { name: /(可编辑|只读)报告$/ }).first().click();
  }
}

/**
 * live: current turn running (mock timeline advances via setTimeout; swallow timers after 1.5 seconds to hold it running;
 * otherwise turn completion makes Workbench fallback mark canvas complete), then __CANVAS_TEST_EVENTS__ writes through SSE canvas store entries.
 */
async function openLive(page: Page, width: number) {
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = ((fn: TimerHandler, delay?: number, ...args: unknown[]) =>
      typeof delay === "number" && delay > 1500 && delay < 30_000 ? 0 : original(fn, delay, ...args)) as typeof window.setTimeout;
  });
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await routeReportApis(page);
  await enableResultsWorkspace(page);
  await page.goto("/?scenario=peer");
  await expect(page.locator(".bh-band__bar").first()).toBeVisible({ timeout: 15_000 });
  await hydrate(page);
  await showResults(page, width);
}

type Events = Record<string, (payload: unknown) => void>;
async function emit(page: Page, name: string, payload: unknown) {
  await page.evaluate(([n, p]) => {
    (window as unknown as { __CANVAS_TEST_EVENTS__: Events }).__CANVAS_TEST_EVENTS__[n](p);
  }, [name, payload] as const);
}

/** Live writing: current turn starts streaming, one new chapter being written. */
async function startWriting(page: Page) {
  await emit(page, "turnStreaming", { turn_id: "t_wb4b" });
  await emit(page, "sectionStreaming", { section_id: "sec_wb_writing", kind: "narrative" });
  await emit(page, "chunk", { section_id: "sec_wb_writing", delta: "## 闭店影响\n闭店影响约占 1.2 个百分点" });
}

async function finishWriting(page: Page) {
  await emit(page, "sectionDone", { section_id: "sec_wb_writing", payload: { markdown: "## 闭店影响\n闭店影响约占 1.2 个百分点。" } });
  await emit(page, "done", { errors: [] });
}

async function hitsItself(target: Locator): Promise<boolean> {
  return target.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return Boolean(hit && (hit === el || el.contains(hit)));
  });
}

const toolbar = (page: Page) => page.locator(".results-report-object-toolbar");

for (const width of [1440, 412]) {
  test.describe(`工具条状态组与分享（${width}）`, () => {
    test("live: writing progress is in the toolbar, the chapter being written is outlined, the body is not dimmed; when done the status badge is in the toolbar", async ({ page }) => {
      await openLive(page, width);
      await expect(toolbar(page)).toBeVisible();
      await startWriting(page);
      const stream = toolbar(page).getByTestId("report-toolbar-streaming");
      await expect(stream).toBeVisible();
      await expect(page.locator(".rc-stream-banner")).toHaveCount(0);
      expect(await hitsItself(stream)).toBe(true);
      // Screen reader: persistent polite region in status group announces first writing progress; visible text hidden from screen readers.
      const announcer = toolbar(page).getByTestId("report-toolbar-announcer");
      await expect(announcer).toHaveText(/./);
      // “复制轮次 ID” in “更多” (writing only), then shows “已复制” in place.
      await page.context().grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
      await toolbar(page).getByRole("button", { name: "更多操作" }).click();
      const copyTurn = page.getByRole("menuitem", { name: "复制轮次 ID" });
      await expect(copyTurn).toBeVisible();
      expect(await hitsItself(copyTurn)).toBe(true);
      await copyTurn.click();
      await expect(page.getByRole("menuitem", { name: "已复制" })).toBeVisible();
      await page.keyboard.press("Escape");
      const writing = page.locator(".sp-block--writing");
      await expect(writing).toBeVisible();
      await expect(writing.locator(".sp-writing-label")).toHaveText("正在写「闭店影响」");
      const art = page.locator("article.canvas-report-article");
      await expect(art).toHaveAttribute("aria-busy", "true");
      expect(await art.evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
      // Batch 4b remainder: writing toolbar adds “写作完成后可以编辑和导出”; measure first row (status group).
      const hintBox = (await toolbar(page).locator(".results-report-writing-hint").boundingBox())!;
      expect(Math.round(hintBox.y - (await toolbar(page).boundingBox())!.y)).toBeLessThanOrEqual(width >= 1200 ? 44 : 61);
      await finishWriting(page);
      // Writing ends: turn still running (mock timeline frozen), no turn terminal state ⇒ never guess; announce neutral “写作已结束”.
      await expect(announcer).toHaveText("写作已结束", { timeout: 5_000 });
      const chip = toolbar(page).locator(".rc-status-trigger");
      await expect(chip).toBeVisible();
      await expect(page.locator(".rc-report-header-meta .rc-status-control")).toHaveCount(0);
      await expect(art).not.toHaveAttribute("aria-busy", "true");
    });

    test("F5: hydration rebuilds it; the status badge is in the toolbar and the report header keeps only History", async ({ page }) => {
      await open(page, width);
      await hydrate(page);
      await page.reload();
      await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
      await hydrate(page);
      await showResults(page, width);
      const chip = toolbar(page).locator(".rc-status-trigger");
      await expect(chip).toBeVisible();
      expect(await hitsItself(chip)).toBe(true);
      await expect(page.locator(".rc-report-header-meta .rc-status-control")).toHaveCount(0);
      await expect(page.locator(".rc-report-header-meta .canvas-report-history-btn")).toBeVisible();
      await expect(page.locator(".rc-stream-banner, [data-testid='report-toolbar-streaming']")).toHaveCount(0);
      expect(await toolbar(page).ariaSnapshot()).toContain('button "分享报告"');
    });

    test("the share icon opens the existing share dialog on top; closing returns focus to the share button", async ({ page }) => {
      await open(page, width);
      await hydrate(page);
      await showResults(page, width);
      const share = toolbar(page).getByRole("button", { name: "分享报告" });
      await expect(share).toBeVisible();
      expect(await hitsItself(share)).toBe(true);
      await share.click();
      const dialog = page.getByRole("dialog", { name: /分享/ });
      await expect(dialog).toBeVisible();
      expect(await hitsItself(dialog)).toBe(true);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(share).toBeFocused();
      // “复制权限链接” remains in “更多”.
      await toolbar(page).getByRole("button", { name: "更多操作" }).click();
      await expect(page.getByRole("menuitem", { name: "复制权限链接" })).toBeVisible();
    });
  });
}

test.describe("修改历史", () => {
  test("title, operation names and “who · which section” use user wording, without 📜 ↺ or internal ids", async ({ page }) => {
    await open(page, 1440);
    await hydrate(page);
    await page.locator(".canvas-report-history-btn").click();
    // D14: revision history is a sheet (drawer dialog); restore sits in its footer.
    const drawer = page.getByRole("dialog", { name: "修改历史" });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText(/^你编辑 · 华东区 GMV 环比下降 8.4%/)).toBeVisible();
    // API omits turnOrdinal: show “AI 撰写” only, no ordinal.
    await expect(drawer.getByText(/^AI 撰写 · 华东区 GMV/)).toBeVisible();
    await drawer.getByRole("option").first().click();
    await expect(drawer.getByRole("button", { name: "恢复此版本…" })).toBeEnabled();
    expect(await drawer.textContent()).not.toMatch(/📜|↺|turn:|section:|sec_wb|patch|append/);
    expect(await hitsItself(drawer.getByRole("button", { name: "关闭" }))).toBe(true);
  });
});

async function openFixture(page: Page, url: string, width: number) {
  await page.setViewportSize({ width, height: 900 });
  await mockAuthedApp(page);
  await page.goto(url);
  await expect(page.locator(".v2-section-chrome").first()).toBeAttached({ timeout: 20_000 });
}

/**
 * Open chapter menu. Actions sit outside chapter; pointer travel may end chapter hover first, disabling action pointers,
 * so dispatch click directly on button (same onClick path as pointer click).
 */
async function openSectionMenu(trigger: Locator) {
  await trigger.dispatchEvent("click");
}

/** Hover chapter containing first body paragraph and obtain its “⋯”. */
async function sectionMenuButton(page: Page): Promise<Locator> {
  const para = page.locator(".ProseMirror p").first();
  const actions = page.locator(".v2-section-chrome:hover .rc-actions").last();
  await expect(async () => {
    await para.scrollIntoViewIfNeeded();
    await para.hover();
    await expect(actions).toHaveCSS("opacity", "1", { timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
  // Hover-dependent locator invalidates as pointer moves: mark this button, locate by marker afterwards.
  await actions.getByRole("button", { name: /操作菜单/ }).evaluate((el) => el.setAttribute("data-e2e-section-menu", "1"));
  return page.locator("[data-e2e-section-menu='1']");
}

test.describe("章节菜单（窄屏触屏 412）", () => {
  test.use({ hasTouch: true, isMobile: true });

  test("the always-visible “⋯” opens the shared bottom sheet with 44 rows, uncovered; Esc closes it", async ({ page }) => {
    await openFixture(page, "/spike/p31?designEdit=1", 412);
    const dots = page.locator(".rc-mdots").first();
    await dots.scrollIntoViewIfNeeded();
    await dots.tap();
    const menu = page.locator(".ui-menu.rc-menu-pop");
    await expect(menu).toHaveClass(/is-sheet/);
    await expect(page.locator(".ui-menu-sheet-scrim")).toBeVisible();
    const items = menu.getByRole("menuitem");
    for (let i = 0; i < (await items.count()); i += 1) {
      expect(Math.round((await items.nth(i).boundingBox())!.height)).toBeGreaterThanOrEqual(44);
      expect(await hitsItself(items.nth(i))).toBe(true);
    }
    await expect(menu.getByRole("menuitem", { name: "删除…" })).toHaveAttribute("data-danger", "true");
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
  });
});

for (const width of [1440, 412]) {
  test.describe(`章节菜单（指针 ${width}）`, () => {
    test("the shared menu is portaled to body, not covered by body or charts; keyboard in and out; Delete is red with “…”", async ({ page }) => {
      await openFixture(page, "/spike/p31?designEdit=1", width);
      const trigger = await sectionMenuButton(page);
      // “⋯” inside editor content (contenteditable NodeView) unreachable by Tab (unchanged); pointer opens, keyboard handles rest.
      await openSectionMenu(trigger);
      const menu = page.locator(".ui-menu.rc-menu-pop");
      await expect(menu).toBeVisible();
      expect(await menu.evaluate((el) => !el.closest(".v2-section-chrome, .canvas-report-shell"))).toBe(true);
      const items = menu.getByRole("menuitem");
      // Opening puts focus within menu (container or first item); Home goes to first item.
      await expect.poll(() => menu.evaluate((el) => el.contains(document.activeElement))).toBe(true);
      await page.keyboard.press("Home");
      await expect(items.first()).toBeFocused();
      for (let i = 0; i < (await items.count()); i += 1) expect(await hitsItself(items.nth(i))).toBe(true);
      const del = menu.getByRole("menuitem", { name: "删除…" });
      await expect(del).toHaveAttribute("data-danger", "true");
      const box = (await menu.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
      await page.keyboard.press("ArrowDown");
      await expect(items.nth(1)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      // Return focus to “⋯”: inside editor, browser focuses editor itself (caret stays in this section).
      await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest(".ProseMirror")))).toBe(true);
      // Accessibility tree.
      await openSectionMenu(trigger);
      await expect(menu).toBeVisible();
      expect(await menu.ariaSnapshot()).toContain('menuitem "删除…"');
    });
  });

  test.describe(`图表来源卡（${width}）`, () => {
    test("portaled to body, uncovered, follows scrolling; Esc returns to the source line; a bottom sheet on narrow screens", async ({ page }) => {
      await openFixture(page, "/spike/p31", width);
      const line = page.locator(".rc-source[role='button']").first();
      await line.scrollIntoViewIfNeeded();
      await line.focus();
      await page.keyboard.press("Enter");
      const card = page.locator(".rc-prov:not(.citation-popover)").first();
      await expect(card).toBeVisible();
      expect(await card.evaluate((el) => !el.closest(".rc-source"))).toBe(true);
      expect(await hitsItself(card.locator(".rc-prov-head"))).toBe(true);
      if (width < 640) {
        await expect(card).toHaveAttribute("data-dialog-presentation", "sheet");
      } else {
        expect(await card.evaluate((el) => el.style.zIndex)).toBe("var(--z-menu)");
        // Scroll following: source-card/source-row relative position unchanged after scrolling report area.
        const before = await page.evaluate(() => {
          const l = document.querySelector(".rc-source[role='button'][aria-expanded='true']")!.getBoundingClientRect();
          const c = document.querySelector(".rc-prov:not(.citation-popover)")!.getBoundingClientRect();
          return Math.round(c.left - l.left);
        });
        await page.mouse.wheel(0, 40);
        await page.waitForTimeout(250);
        const after = await page.evaluate(() => {
          const l = document.querySelector(".rc-source[role='button'][aria-expanded='true']")?.getBoundingClientRect();
          const c = document.querySelector(".rc-prov:not(.citation-popover)")?.getBoundingClientRect();
          return l && c ? Math.round(c.left - l.left) : null;
        });
        if (after !== null) expect(after).toBe(before);
      }
      await page.keyboard.press("Escape");
      await expect(page.locator(".rc-prov:not(.citation-popover)")).toHaveCount(0);
      await expect(line).toBeFocused();
    });
  });
}

async function selectFirstWords(page: Page) {
  const para = page.locator(".ProseMirror p").first();
  await para.scrollIntoViewIfNeeded();
  await para.evaluate((el) => {
    const node = document.createTreeWalker(el, NodeFilter.SHOW_TEXT).nextNode()!;
    const range = document.createRange();
    range.setStart(node, 0);
    range.setEnd(node, Math.min(6, node.textContent!.length));
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
  });
  const box = (await para.boundingBox())!;
  await page.mouse.move(box.x + 5, box.y + 5);
  await page.mouse.up();
}

test.describe("选区工具栏：「Aa」与 ⌥⏎", () => {
  test("⌥⏎ enters the toolbar, ←/→ move, Esc returns to the body with the selection kept; “Aa” opens three uncovered dropdowns", async ({ page }) => {
    await openFixture(page, "/spike/p31?designEdit=1", 1440);
    await selectFirstWords(page);
    const bar = page.locator(".rc-sel-toolbar");
    await expect(bar).toBeVisible();
    const selected = await page.evaluate(() => window.getSelection()!.toString());
    expect(selected.length).toBeGreaterThan(0);
    await page.keyboard.press("Alt+Enter");
    await expect(bar.getByRole("button", { name: "改写" })).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(bar.getByRole("button", { name: "缩短" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest(".ProseMirror")))).toBe(true);
    expect(await page.evaluate(() => window.getSelection()!.toString())).toBe(selected);
    await selectFirstWords(page);
    const aa = page.locator(".rc-sel-toolbar").getByRole("button", { name: "文字样式" });
    await expect(aa).toBeVisible();
    expect(await hitsItself(aa)).toBe(true);
    await expect(page.locator(".rc-sel-toolbar").getByRole("button", { name: "字体" })).toHaveCount(0);
    await aa.click();
    for (const name of ["字体", "字号", "字重"]) {
      const btn = page.locator(".rc-sel-toolbar").getByRole("button", { name });
      await expect(btn).toBeVisible();
      expect(await hitsItself(btn)).toBe(true);
    }
    expect(await page.locator(".rc-sel-toolbar").ariaSnapshot()).toMatch(/toolbar[\s\S]*button "改写"[\s\S]*button "文字样式"/);
  });
});

async function mockSharedReport(page: Page) {
  await mockAuthedApp(page);
  await page.route("**/api/v1/sessions/s_shared", (route) => route.fulfill(json({ detail: "forbidden" }, 403)));
  await page.route("**/api/v1/canvas/cv_shared/status", (route) =>
    route.fulfill(json({ status: "verified", effectiveStatus: "verified", quarantinedCount: 0 })),
  );
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
}

for (const width of [1440, 412]) {
  test(`read-only shared view (${width}): the toolbar has only status and export, no share, edit or status menu`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockSharedReport(page);
    await enableResultsWorkspace(page);
    await page.goto("/?session=s_shared");
    await expect(page.getByText("Dogfood shared report body is visible.")).toBeVisible({ timeout: 15_000 });
    await showResults(page, width);
    const bar = toolbar(page);
    await expect(bar).toBeVisible();
    await expect(bar.getByTestId("report-status-static")).toBeVisible();
    await expect(bar.locator(".rc-status-trigger")).toHaveCount(0);
    await expect(bar.getByRole("button", { name: "分享报告" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "编辑此报告" })).toHaveCount(0);
    await expect(page.locator(".rc-actions, .rc-handle, .rc-mdots")).toHaveCount(0);
  });
}

const TABLE_ROWS = Array.from({ length: 6 }, (_, i) => ({
  store: `西单文化广场${i + 1}`,
  gmv: 151345651.94 - i * 9000000,
  traffic: 166723 - i * 9000,
  profit: 31234567.12 - i * 900000,
  ipt: 312.45 - i,
}));
const TABLE_SECTIONS = [
  ...SECTIONS,
  {
    id: "sec_wb_table",
    kind: "chart",
    spec: {
      kind: "table",
      title: "门店经营明细表",
      row_count: TABLE_ROWS.length,
      columns: [
        { key: "store", header: "门店", type: "categorical" },
        { key: "gmv", header: "营业额", type: "numeric" },
        { key: "traffic", header: "客流量", type: "numeric" },
        { key: "profit", header: "利润", type: "numeric" },
        { key: "ipt", header: "客单价", type: "numeric" },
      ],
      rows: TABLE_ROWS,
    },
  },
];

test("phone width: the maximized report keeps a 16px gutter and data tables scroll instead of squeezing", async ({ page }) => {
  const width = 390;
  await open(page, width);
  await hydrate(page, TABLE_SECTIONS);
  await showResults(page, width);
  const art = page.locator("article.canvas-report-article");
  await expect(art.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator(".v2-workspace.canvas-maxed")).toHaveCount(1);
  const gutter = await art.evaluate((el) => {
    const pane = el.closest(".v2-ws-right-body")!.getBoundingClientRect();
    const prose = el.querySelector(".ProseMirror")!.getBoundingClientRect();
    return { left: Math.round(prose.left - pane.left), right: Math.round(el.getBoundingClientRect().right - prose.right) };
  });
  // Canvas Objects Spec C3: narrow (< 480) panes use 16px side gutters; wide panes keep the desktop gutter.
  expect(gutter).toEqual({ left: 16, right: 16 });
  const table = art.locator("table.v2-chart-table-el");
  await expect(table).toBeVisible();
  const layout = await table.evaluate((el) => {
    const scroller = el.closest(".v2-chart-table-scroll") as HTMLElement;
    const lines = (cell: Element) => {
      const range = document.createRange();
      range.selectNodeContents(cell);
      return new Set(Array.from(range.getClientRects()).map((r) => Math.round(r.top))).size;
    };
    return {
      scrolls: scroller.scrollWidth > scroller.clientWidth,
      numberLines: Math.max(...Array.from(el.querySelectorAll("tbody td.is-num")).map(lines)),
    };
  });
  expect(layout).toEqual({ scrolls: true, numberLines: 1 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
});
