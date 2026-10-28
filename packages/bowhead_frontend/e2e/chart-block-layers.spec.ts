import { expect, test, type Page, type Route } from "@playwright/test";
import { mockAuthedApp } from "./_app-mocks";
import { probeLayer } from "./_layer-probe";

/**
 * Real-browser report-chart stacking/layout (docs/design/组件统一-技术设计.md “图层” item 5):
 * - Type menu portals to body: no .v2-chart-table clipping, no canvas covering, bottom-edge flip, Esc focus return;
 * - Mobile 390px six-month line chart never scrolls horizontally; all six points/max label fit canvas, report toolbar never covers chart title.
 * jsdom lacks stacking/hit testing/layout; enforce these only here.
 */

const SHOTS = process.env.CHART_FIX_SHOTS;

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const H1_TREND = [
  { month: "2026-01", revenue: 19.9 },
  { month: "2026-02", revenue: 17.7 },
  { month: "2026-03", revenue: 14.1 },
  { month: "2026-04", revenue: 13.9 },
  { month: "2026-05", revenue: 13.0 },
  { month: "2026-06", revenue: 12.6 },
];

const TABLE_ROWS = Array.from({ length: 1 }, (_, i) => ({ store: `门店 ${i + 1}`, gmv: 100 - i * 7 }));

async function mockReport(page: Page) {
  await mockAuthedApp(page);
  // Results workspace: sticky report toolbar above report (the one covering title in 116 mobile screenshot).
  await page.route("**/health", (route: Route) => route.fulfill(json({ feature_flags: { results_workspace: true } })));
  await page.route("**/api/v1/results/capabilities", (route: Route) => route.fulfill(json({ detail: "not found" }, 404)));
  await page.addInitScript(() => {
    window.localStorage.setItem("bowhead.feature.results_workspace", "1");
  });
  await page.route("**/api/v1/sessions/s_chart", (route: Route) =>
    route.fulfill(json({
      session: {
        id: "s_chart",
        workspace_id: "ws_smoke",
        title: "零售经营分析",
        started_at: "2026-07-03T00:00:00Z",
        last_active_at: "2026-07-03T01:00:00Z",
        status: "done",
        cost_usd: 0,
        tokens_total: 0,
        intents: ["analysis"],
      },
      turns: [{
        id: "t_chart",
        seq: 1,
        user_text: "做一份上半年经营分析报告",
        intent: "analysis",
        status: "done",
        started_at: "2026-07-03T00:00:00Z",
        ended_at: "2026-07-03T01:00:00Z",
        final_answer: "报告已生成",
        agent_type: "lead",
        attachments: [],
        summary: null,
      }],
    })),
  );
  await page.route("**/api/v1/turns/t_chart/events", (route: Route) => route.fulfill(json({ events: [] })));
  await page.route("**/api/v1/canvas/by-session/s_chart**", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/artifacts")) {
      await route.fulfill(json({ items: [] }));
      return;
    }
    await route.fulfill(json({
      canvas: {
        id: "cv_chart",
        sessionId: "s_chart",
        workspaceId: "ws_smoke",
        title: "零售经营分析",
        status: "done",
        createdAt: "2026-07-03T00:00:00Z",
        updatedAt: "2026-07-03T00:00:00Z",
      },
      sections: [
        { id: "h1_chart", kind: "h1", text: "零售经营分析" },
        { id: "n_chart", kind: "narrative", markdown: "上半年营业额逐月回落。" },
        {
          id: "chart_line",
          kind: "chart",
          spec: {
            kind: "line",
            title: "2026年上半年（1-6月）月度营业额趋势",
            row_count: 6,
            columns: [
              { key: "month", header: "月份", type: "date" },
              { key: "revenue", header: "营业额(亿元)", type: "numeric" },
            ],
            rows: H1_TREND,
          },
        },
        {
          id: "chart_table",
          kind: "chart",
          spec: {
            kind: "table",
            title: "门店 GMV",
            row_count: TABLE_ROWS.length,
            columns: [
              { key: "store", header: "门店", type: "categorical" },
              { key: "gmv", header: "GMV", type: "numeric" },
            ],
            rows: TABLE_ROWS,
          },
        },
        // Vertical bars whose largest value sits just under the axis maximum (980 under 1000): the bar-end label
        // must get top room on the very first frame (Chart Embed Spec review, 390 report clipping).
        {
          id: "chart_bar3",
          kind: "chart",
          spec: {
            kind: "bar",
            title: "三城营业额",
            row_count: 3,
            columns: [
              { key: "city", header: "城市", type: "categorical" },
              { key: "gmv", header: "营业额", type: "numeric", unit: "万元" },
            ],
            rows: [{ city: "上海", gmv: 980 }, { city: "北京", gmv: 680 }, { city: "广州", gmv: 380 }],
            show_data_labels: true,
          },
        },
        {
          id: "chart_bar12",
          kind: "chart",
          spec: {
            kind: "bar",
            title: "12 城门店营业额",
            row_count: 12,
            columns: [
              { key: "city", header: "城市", type: "categorical" },
              { key: "gmv", header: "营业额", type: "numeric", unit: "万元" },
            ],
            rows: Array.from({ length: 12 }, (_, i) => ({ city: `城市${i + 1}`, gmv: 980 - i * 75 })),
            show_data_labels: true,
          },
        },
        { id: "n_tail", kind: "narrative", markdown: "结论：下半年需关注客流。" },
      ],
      sourceRefs: [],
    }));
  });
}

async function openReport(page: Page) {
  await mockReport(page);
  await page.goto("/?session=s_chart&report=1");
  await expect(page.getByRole("heading", { name: "零售经营分析", level: 1 })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("figure", { name: /月度营业额趋势/ }).locator("canvas")).toBeVisible();
  // Report first frame rerenders with result list (chart remount); wait for stability before measuring.
  await page.waitForTimeout(500);
}

/**
 * Record all fillText geometry in each canvas's **last frame** (device pixels); Chart.js clears whole canvas with clearRect each frame.
 * `ink` = actual glyph ink box (`actualBoundingBox*`, includes textAlign / textBaseline);
 * `frame` = datalabels backing box: text layout (line height 1.2 × font size) expanded by `labelBase.padding` (horizontal 3, vertical 1).
 * Never read Chart.js internal state; same measurement for chromium / webkit.
 */
async function recordCanvasText(page: Page) {
  await page.addInitScript(() => {
    type Box = { left: number; top: number; right: number; bottom: number };
    type Entry = { text: string; align: string; ink: Box; frame: Box };
    const store = new WeakMap<HTMLCanvasElement, Entry[]>();
    (window as unknown as { __canvasText: (c: HTMLCanvasElement) => Entry[] }).__canvasText = (c) => store.get(c) ?? [];
    const proto = CanvasRenderingContext2D.prototype;
    const clearRect = proto.clearRect;
    proto.clearRect = function (x: number, y: number, w: number, h: number) {
      const t = this.getTransform();
      if (x <= 0 && y <= 0 && w * t.a >= this.canvas.width - 1 && h * t.d >= this.canvas.height - 1) {
        store.set(this.canvas, []);
      }
      return clearRect.call(this, x, y, w, h);
    };
    const fillText = proto.fillText;
    proto.fillText = function (text: string, x: number, y: number, maxWidth?: number) {
      const m = this.measureText(text);
      const t = this.getTransform();
      const map = (b: Box): Box => ({
        left: t.a * b.left + t.e,
        right: t.a * b.right + t.e,
        top: t.d * b.top + t.f,
        bottom: t.d * b.bottom + t.f,
      });
      const size = Number(/([\d.]+)px/.exec(this.font)?.[1] ?? 10);
      const align = this.textAlign;
      const start = align === "right" || align === "end" ? x - m.width : align === "center" ? x - m.width / 2 : x;
      const entry: Entry = {
        text: String(text),
        align,
        ink: map({
          left: x - m.actualBoundingBoxLeft,
          right: x + m.actualBoundingBoxRight,
          top: y - m.actualBoundingBoxAscent,
          bottom: y + m.actualBoundingBoxDescent,
        }),
        frame: map({ left: start - 3, right: start + m.width + 3, top: y - size * 0.6 - 1, bottom: y + size * 0.6 + 1 }),
      };
      const list = store.get(this.canvas) ?? [];
      list.push(entry);
      store.set(this.canvas, list);
      return maxWidth === undefined ? fillText.call(this, text, x, y) : fillText.call(this, text, x, y, maxWidth);
    };
  });
}

/** Intersect 19.9 label backing box with each y-axis tick (right-aligned numbers); return hits and overlap areas (device pixels²). */
async function maxLabelTickOverlap(page: Page) {
  const figure = page.getByRole("figure", { name: /月度营业额趋势/ });
  await figure.scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);
  return figure.locator("canvas").first().evaluate((el) => {
    type Box = { left: number; top: number; right: number; bottom: number };
    type Entry = { text: string; align: string; ink: Box; frame: Box };
    const entries = (window as unknown as { __canvasText: (c: HTMLCanvasElement) => Entry[] }).__canvasText(
      el as HTMLCanvasElement,
    );
    const label = entries.find((e) => e.text === "19.9");
    const ticks = entries.filter((e) => e.align === "right" && /^-?[\d.,]+$/.test(e.text));
    const hits = ticks
      .map((tick) => {
        const w = Math.min(label!.frame.right, tick.ink.right) - Math.max(label!.frame.left, tick.ink.left);
        const h = Math.min(label!.frame.bottom, tick.ink.bottom) - Math.max(label!.frame.top, tick.ink.top);
        return { tick: tick.text, area: w > 0 && h > 0 ? Math.round(w * h) : 0 };
      })
      .filter((hit) => hit.area > 0);
    return { found: Boolean(label), ticks: ticks.map((t) => t.text), hits, label: label?.frame };
  });
}

/** Chart-type menu has `data-kind` options (distinct from “⋯” actions menu). */
function kindMenu(page: Page) {
  return page.locator('[role="menu"]:has([data-kind])');
}

function probe(_label: string, target: ReturnType<typeof kindMenu>) {
  return probeLayer(target);
}

function shot(page: Page, name: string) {
  if (!SHOTS) return Promise.resolve();
  return page.screenshot({ path: `${SHOTS}/${test.info().project.name}-${name}.png` });
}

test.describe("图种切换下拉", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("表格形态：挂 body 不被表格裁掉，当前图种为选中项，Esc 回焦", async ({ page }) => {
    await openReport(page);
    const table = page.locator(".v2-chart-table", { hasText: "门店 GMV" });
    await table.scrollIntoViewIfNeeded();
    const trigger = table.getByRole("button", { name: /表格/ });
    await trigger.click();
    const menu = kindMenu(page);
    await expect(menu).toBeVisible();
    await shot(page, "table-menu");
    const probed = await probe("menu", menu);
    expect(probed.covered).toEqual([]);
    expect(probed.clipped).toBe(0);
    const current = menu.getByRole("menuitemradio", { name: "表格" });
    await expect(current).toHaveAttribute("aria-checked", "true");
    await expect(current).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("报告里：打开后动作条常显，菜单不被画布盖住；指针离开图表仍可见", async ({ page }) => {
    await openReport(page);
    const figure = page.getByRole("figure", { name: /月度营业额趋势/ });
    await figure.scrollIntoViewIfNeeded();
    await figure.hover();
    const trigger = figure.getByRole("button", { name: /折线/ });
    await trigger.click();
    const menu = kindMenu(page);
    await expect(menu).toBeVisible();
    // Measure within 140ms fade-in: body-mounted menu is independent of action-bar opacity.
    expect((await probe("menu-fading-in", menu)).covered).toEqual([]);
    await page.mouse.move(2, 2);
    await page.waitForTimeout(300);
    await shot(page, "report-menu-pointer-away");
    const actions = figure.locator(".v2-chart-actions");
    await expect(actions).toHaveClass(/is-open/);
    expect(await actions.evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
    expect((await probe("menu-pointer-away", menu)).covered).toEqual([]);
    await menu.getByRole("menuitemradio", { name: "柱状" }).click();
    await expect(menu).toHaveCount(0);
    await expect(figure.getByRole("button", { name: /柱状/ })).toBeVisible();
    await expect(actions).not.toHaveClass(/is-open/);
  });

  test("触发按钮贴近视口底部时向上展开", async ({ page }) => {
    await openReport(page);
    const table = page.locator(".v2-chart-table", { hasText: "门店 GMV" });
    const trigger = table.getByRole("button", { name: /表格/ });
    await trigger.scrollIntoViewIfNeeded();
    // Scroll trigger to 40px above viewport bottom.
    await trigger.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      let scroller: HTMLElement | null = el.parentElement;
      while (scroller && !(scroller.scrollHeight > scroller.clientHeight && /auto|scroll/.test(getComputedStyle(scroller).overflowY))) {
        scroller = scroller.parentElement;
      }
      const target = window.innerHeight - 40 - rect.bottom;
      if (scroller) scroller.scrollTop -= target;
      else window.scrollBy(0, -target);
    });
    const box = await trigger.boundingBox();
    expect(box!.y + box!.height).toBeGreaterThan(700);
    await trigger.click();
    const menu = kindMenu(page);
    await expect(menu).toBeVisible();
    await shot(page, "menu-flip-up");
    const menuBox = await menu.boundingBox();
    expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(box!.y);
    const probed = await probe("menu", menu);
    expect(probed.covered).toEqual([]);
    expect(probed.clipped).toBe(0);
  });
});

test.describe("手机报告里的折线图", () => {
  test.use({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, hasTouch: true, isMobile: true });

  test("六个点与最大值标签完整显示，不横滑，工具条不压标题", async ({ page }) => {
    await openReport(page);
    const figure = page.getByRole("figure", { name: /月度营业额趋势/ });
    await figure.scrollIntoViewIfNeeded();
    const canvas = figure.locator("canvas").first();
    await expect(canvas).toBeVisible();
    await page.waitForTimeout(600);
    await shot(page, "mobile-line-chart");
    const wrap = figure.locator(".v2-chart-canvas-wrap");
    const layout = await wrap.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
    // No horizontal scroll: all six points inside visible plot.
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
    // Outermost 1px at canvas top/right stays blank: numeric labels (including 19.9 backing) never extend outside and clip
    // (padding adds overhang + 1px, so label reaches at most second pixel).
    const edges = await canvas.evaluate((el) => {
      const c = el as HTMLCanvasElement;
      const ctx = c.getContext("2d");
      if (!ctx) return null;
      const inked = (x: number, y: number, w: number, h: number) => {
        const { data } = ctx.getImageData(x, y, w, h);
        let n = 0;
        for (let i = 3; i < data.length; i += 4) if (data[i] > 24) n += 1;
        return n;
      };
      const band = Math.max(1, Math.round(window.devicePixelRatio || 1));
      return { top: inked(0, 0, c.width, band), right: inked(c.width - band, 0, band, c.height) };
    });
    expect(edges).toEqual({ top: 0, right: 0 });
    // Scroll to chart top (navigation/focus does this too): title must not sit behind sticky report toolbar.
    await figure.evaluate((el) => el.scrollIntoView({ block: "start" }));
    await page.waitForTimeout(200);
    await shot(page, "mobile-line-chart-scrolled-to-top");
    const toolbar = await page.locator(".results-report-object-toolbar").boundingBox();
    const title = await figure.locator(".v2-chart-title").boundingBox();
    expect(title!.y).toBeGreaterThanOrEqual(toolbar!.y + toolbar!.height);
  });
});

for (const viewport of [
  { name: "手机 390", use: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, hasTouch: true, isMobile: true } },
  { name: "桌面 1280", use: { viewport: { width: 1280, height: 800 } } },
]) {
  test.describe(`最高点标签不压 y 轴刻度（${viewport.name}）`, () => {
    test.use(viewport.use);

    test("首点 19.9 的标签底片与刻度文字不相交", async ({ page }) => {
      await recordCanvasText(page);
      await openReport(page);
      const result = await maxLabelTickOverlap(page);
      if (SHOTS) {
        await page.getByRole("figure", { name: /月度营业额趋势/ }).locator("canvas").first().screenshot({
          path: `${SHOTS}/${test.info().project.name}-top-label-vs-ticks-${viewport.use.viewport.width}.png`,
        });
      }
      expect(result.found).toBe(true);
      // Positive control: ticks really measured (including top 20), or no intersection proves nothing.
      expect(result.ticks).toContain("20");
      expect(result.hits).toEqual([]);
    });
  });
}

/** Every recorded bar-end value label (`…万元`) of the named figure's canvas (ink box, device pixels). */
async function barLabelBoxes(page: Page, name: RegExp) {
  const figure = page.getByRole("figure", { name });
  await figure.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  return figure.locator("canvas").first().evaluate((el) => {
    type Box = { left: number; top: number; right: number; bottom: number };
    type Entry = { text: string; align: string; ink: Box; frame: Box };
    const entries = (window as unknown as { __canvasText: (c: HTMLCanvasElement) => Entry[] }).__canvasText(
      el as HTMLCanvasElement,
    );
    return entries.filter((e) => /万元$/.test(e.text)).map((e) => ({ text: e.text, top: Math.round(e.ink.top) }));
  });
}

for (const viewport of [
  { name: "手机 390", use: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, hasTouch: true, isMobile: true } },
  { name: "桌面窄窗 390", use: { viewport: { width: 390, height: 844 } } },
  { name: "桌面 1440", use: { viewport: { width: 1440, height: 900 } } },
]) {
  test.describe(`纵向柱顶标签不被画布顶边裁掉（${viewport.name}）`, () => {
    test.use(viewport.use);

    for (const name of [/三城营业额/, /12 城门店营业额/]) {
      test(`${name.source}：首帧与切换深色重绘后，柱顶标签都完整在画布内`, async ({ page }) => {
        await recordCanvasText(page);
        await openReport(page);
        const first = await barLabelBoxes(page, name);
        // Positive control: the 980 label is really drawn and measured.
        expect(first.map((l) => l.text)).toContain("980万元");
        expect(first.filter((l) => l.top < 0)).toEqual([]);
        // A theme switch rebuilds the chart instance; the rebuilt first frame must reserve the room too.
        await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
        const redrawn = await barLabelBoxes(page, name);
        expect(redrawn.map((l) => l.text)).toContain("980万元");
        expect(redrawn.filter((l) => l.top < 0)).toEqual([]);
      });
    }
  });
}
