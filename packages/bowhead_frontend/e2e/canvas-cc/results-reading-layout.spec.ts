import { expect, test, type Page } from "@playwright/test";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";
import de from "@/messages/de-DE.json";
import fr from "@/messages/fr-FR.json";

async function openReport(page: Page) {
  await page.request.post(`http://127.0.0.1:${process.env.PLAYWRIGHT_API_PORT ?? "18080"}/api/v1/_test/reset`);
  await page.route("**/api/v1/results/capabilities", (route) => route.fulfill({
    json: { dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50 },
  }));
  await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill({ json: { items: [] } }));
  await page.goto("/");
  const composer = page.locator("textarea").first();
  await composer.fill("resultsworkspace");
  const sent = page.waitForResponse((response) => response.url().endsWith("/api/v1/chat/send"));
  await composer.press("Enter");
  const { turn_id: turnId, session_id: sessionId } = await (await sent).json();
  const detail = await (await page.request.get(`/api/v1/sessions/${sessionId}`)).json();
  await page.route(`**/api/v1/sessions/${sessionId}`, (route) => route.fulfill({ json: {
    ...detail,
    turns: [{ id: turnId, seq: 1, user_text: "resultsworkspace", intent: "analysis", status: "completed",
      started_at: "2026-10-27T00:00:00Z", ended_at: "2026-10-27T00:00:01Z", final_answer: "Report ready", trace_steps: [], summary: null }],
  } }));
  await expect(page.locator(".canvas-report-article")).toBeAttached();
}

for (const [locale, messages] of [["en-US", en], ["zh-CN", zh], ["de-DE", de], ["fr-FR", fr]] as const) {
  test.describe(`Canvas UI repairs in ${locale}`, () => {
    test.use({ hasTouch: true });
    test("keeps inspection actions reachable and shares Table metadata styles", async ({ page }) => {
      await page.context().addCookies([{ name: "NEXT_LOCALE", value: locale, domain: "localhost", path: "/" }]);
      await page.route("**/api/v1/auth/me", async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...await response.json(), ui_language: locale } });
      });
      // Reproduce the deployed HTTP clipboard fallback without requesting a download.
      await page.addInitScript(() => Object.defineProperty(window, "ClipboardItem", { configurable: true, value: undefined }));
      await page.route("**/api/v1/canvas/by-session/s_e2e**", async (route) => {
        const response = await route.fetch();
        const dto = await response.json();
        if (!Array.isArray(dto.sections)) {
          await route.fulfill({ response });
          return;
        }
        const section = dto.sections.find((item: { id: string }) => item.id === "sec_results_data");
        section.spec = {
          kind: "table", title: "Unit table", row_count: 3,
          columns: [{ key: "month", header: "Month", type: "date" }, { key: "revenue", header: "Revenue", type: "numeric", unit: "万元" }],
          rows: [{ month: "2026-01", revenue: 120.5 }, { month: "2026-02", revenue: 101.2 }, { month: "2026-03", revenue: 99.3 }],
        };
        dto.sections.push({ id: "sec_ui_trend", kind: "chart", spec: { ...section.spec, kind: "line", title: "Revenue trend" } });
        dto.sections.push({ id: "sec_ui_sources", kind: "sources", entries: [{ label: "Synthetic fixture" }] });
        await route.fulfill({ json: dto });
      });
      await page.setViewportSize({ width: 1280, height: 844 });
      await openReport(page);
      await page.goto("/?session=s_e2e&result=report");
      const toolbar = page.locator(".results-report-object-toolbar");
      await expect(toolbar).toHaveCount(1);
      await expect(toolbar).toHaveClass(/results-report-unified-toolbar/);
      await expect(toolbar.getByTestId("source-linkage-strip")).toBeVisible();
      await expect(page.locator(".canvas-report-history-btn")).toHaveCount(0);
      await expect.poll(() => toolbar.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        return Array.from(element.querySelectorAll("button")).every((button) => {
          const rect = button.getBoundingClientRect();
          return rect.y >= bounds.y && rect.bottom <= bounds.bottom + 1;
        });
      })).toBe(true);
      await page.screenshot({ path: test.info().outputPath(`report-toolbar-${locale}.jpg`) });
      const sourceStyles = await page.locator(".canvas-report-article .v2-chart-table").first().evaluate((element) => {
        const style = (selector: string) => {
          const value = getComputedStyle(element.querySelector(selector)!);
          return { fontSize: value.fontSize, line: value.borderBottomWidth, color: value.borderBottomColor, background: value.backgroundColor };
        };
        return { header: style("th"), cell: style("td"), note: style("[data-chart-unit-note]") };
      });
      const figure = page.getByRole("figure", { name: "Revenue trend", exact: true });
      await figure.locator(".is-more").click();
      await page.getByRole("menuitem", { name: messages.chart_inspect.open, exact: true }).click();
      const dialog = page.locator(".rc-inspect-panel");
      await expect(dialog).toBeVisible();
      for (const width of [1280, 768, 390, 320]) {
        await page.setViewportSize({ width, height: 844 });
        const actions = dialog.locator(".rc-inspect-actions button");
        await expect(actions).toHaveCount(4);
        await expect(actions.nth(2)).toHaveText(messages.chart_inspect.copy_degraded_download);
        await expect.poll(async () => {
          const panel = (await dialog.boundingBox())!;
          return actions.evaluateAll((buttons, bounds) => buttons.flatMap((button) => {
            const rect = button.getBoundingClientRect();
            const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
            const fits = rect.x >= bounds.x && rect.right <= bounds.x + bounds.width + 1
              && rect.y >= bounds.y && rect.bottom <= bounds.y + bounds.height + 1
              && rect.height >= 44 && button.scrollWidth <= button.clientWidth + 1 && !!hit && button.contains(hit);
            return fits ? [] : [{ text: button.textContent, whiteSpace: getComputedStyle(button).whiteSpace, width: rect.width, scrollWidth: button.scrollWidth, panel: bounds }];
          }), panel);
        }).toEqual([]);
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
      }
      await page.screenshot({ path: test.info().outputPath(`inspection-chart-${locale}.jpg`) });
      await dialog.getByRole("tab", { name: messages.chart_inspect.tab_data, exact: true }).click();
      const inspectStyles = await dialog.evaluate((element) => {
        const style = (selector: string) => {
          const value = getComputedStyle(element.querySelector(selector)!);
          return { fontSize: value.fontSize, line: value.borderBottomWidth, color: value.borderBottomColor, background: value.backgroundColor };
        };
        return { header: style("th"), cell: style("td"), note: style("[data-chart-unit-note]") };
      });
      expect(inspectStyles).toEqual(sourceStyles);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await page.locator(".results-report-actions .ws-menu-wrap > .v2-icon-btn").click();
      const menu = page.getByRole("menu");
      await expect(menu.locator(".results-report-menu-group .danger")).toHaveCount(2);
      await page.keyboard.press("End");
      await expect(menu.getByRole("menuitem").last()).toBeFocused();
      await page.screenshot({ path: test.info().outputPath(`report-more-${locale}.jpg`) });
      await page.keyboard.press("Escape");
    });
  });
}

test("desktop report reading width expands prose and keeps a local reversible margin choice", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await openReport(page);
  await page.getByRole("button", { name: "放大报告（隐藏对话）", exact: true }).click();
  const paragraph = page.locator('.canvas-report-article [data-section-kind="narrative"] p').first();
  await expect(paragraph).toBeVisible();
  const standard = (await paragraph.boundingBox())!.width;
  expect(standard).toBeGreaterThan(700);
  await page.getByRole("button", { name: "更多操作", exact: true }).first().click();
  await page.getByRole("menuitem", { name: "缩小边距并加宽正文" }).click();
  await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-reading-width", "wide");
  expect((await paragraph.boundingBox())!.width).toBeGreaterThan(standard + 150);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  await page.reload();
  await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-reading-width", "wide");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("tab", { name: "成果", exact: true }).click();
  await page.getByRole("button", { name: "华东区销售归因分析 可编辑报告" }).click();
  await expect(paragraph).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  expect((await paragraph.boundingBox())!.width).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.getByRole("button", { name: "更多操作", exact: true }).first().click();
  await page.getByRole("menuitem", { name: "恢复默认边距和正文宽度" }).click();
  await expect(page.locator(".canvas-report-shell")).toHaveAttribute("data-reading-width", "standard");
});

for (const width of [1280, 768, 390]) {
  test(`HTML preview follows the pane height at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await openReport(page);
    await page.goto("/?session=s_e2e&result=art_delivery&mode=preview");
    const frame = page.locator('iframe[title="executive-brief.html"]');
    await expect(frame).toBeVisible();
    const measure = () => frame.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      const body = element.closest(".results-body--artifact")!;
      const pane = body.getBoundingClientRect();
      const controls = body.querySelector(".safe-html-preview-status")!.getBoundingClientRect();
      const hint = body.querySelector(".safe-html-preview-hint")!.getBoundingClientRect();
      return { height: bounds.height, unused: pane.bottom - hint.bottom, bottom: hint.bottom, paneBottom: pane.bottom,
        overlap: Math.max(controls.bottom - bounds.top, bounds.bottom - hint.top) };
    });
    await expect.poll(async () => Math.abs((await measure()).unused)).toBeLessThan(20);
    const tall = await measure();
    await page.setViewportSize({ width, height: 680 });
    await expect.poll(async () => tall.height - (await measure()).height).toBeGreaterThan(180);
    const short = await measure();
    expect(short.height).toBeGreaterThan(100);
    expect(short.overlap).toBeLessThanOrEqual(1);
    expect(short.bottom).toBeLessThanOrEqual(short.paneBottom + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    await page.screenshot({ path: test.info().outputPath(`html-pane-height-${width}.jpg`) });
  });

  test(`artifact full view fills the viewport and returns focus at ${width}px`, async ({ page }) => {
    const height = 844;
    await page.setViewportSize({ width, height });
    await openReport(page);
    await page.goto("/?session=s_e2e&result=art_delivery&mode=preview");
    await expect(page.locator('iframe[title="executive-brief.html"]')).toBeVisible();
    const trigger = page.getByRole("button", { name: "全屏查看", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "executive-brief.html", exact: true });
    await expect(dialog).toBeVisible();
    const panel = dialog;
    await expect.poll(() => panel.boundingBox()).toEqual({ x: 0, y: 0, width, height });
    await expect(panel).toHaveCSS("border-radius", "0px");
    await expect(panel).toHaveCSS("box-shadow", "none");
    await expect(panel).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    const frame = await dialog.locator("iframe").boundingBox();
    expect(frame).not.toBeNull();
    expect(frame!.height).toBeGreaterThan(height / 2);
    expect(frame!.x + frame!.width).toBeLessThanOrEqual(width);
    expect(frame!.y + frame!.height).toBeLessThanOrEqual(height);
    await page.screenshot({ path: test.info().outputPath(`artifact-full-view-${width}.jpg`) });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });
}

test("retained reports do not add gutters to file or code viewers", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openReport(page);
  await expect(page.locator(".canvas-report-article")).toBeVisible();
  const reportPadding = await page.locator(".results-body--report").evaluate((element) => getComputedStyle(element).paddingInlineStart);
  for (const [title, bodyClass, padding] of [
    ["executive-brief.html", "results-body--artifact", "0px"],
    ["代码与运行", "results-body--code", "16px"],
  ]) {
    await page.getByRole("button", { name: "成果", exact: true }).click();
    await page.getByRole("dialog", { name: "成果导航", exact: true })
      .locator(".results-nav-item", { hasText: title }).click();
    const body = page.locator(`.${bodyClass}`);
    await expect(body).toBeVisible();
    await expect(body.locator(".results-object-surface[hidden] .canvas-report-shell")).toBeAttached();
    await expect(body).toHaveCSS("padding-inline-start", padding);
    await expect(body).toHaveCSS("padding-inline-end", padding);
  }
  await page.goBack();
  await page.goBack();
  await expect(page.locator(".results-body--report")).toHaveCSS("padding-inline-start", reportPadding);
  await expect(page.locator(".canvas-report-article")).toBeVisible();
});

for (const width of [320, 390]) {
  test(`long fixed row identities leave numeric cells readable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const identity = "International manufacturing and distribution operations in Northern Europe";
    await page.route("**/api/v1/canvas/by-session/s_e2e**", async (route) => {
      const response = await route.fetch();
      const dto = await response.json();
      const section = dto.sections.find((item: { id: string }) => item.id === "sec_results_data");
      section.spec.columns = [
        { key: "market", header: "Market", type: "categorical" },
        { key: "revenue", header: "Revenue", type: "numeric" },
        { key: "cost", header: "Cost", type: "numeric" },
      ];
      section.spec.rows = [{ market: identity, revenue: 123456789, cost: 98765432 }];
      section.spec.row_count = 1;
      await route.fulfill({ json: dto });
    });
    await openReport(page);
    await page.getByRole("tab").nth(1).click();
    await page.locator(".results-nav-item", { hasText: "华东区销售归因分析" }).click();
    const table = page.getByRole("region", { name: "各市场净营收", exact: true });
    await expect(table).toContainText(identity);
    await table.scrollIntoViewIfNeeded();
    const scroller = table.locator(".v2-chart-table-scroll");
    await scroller.evaluate((element) => { element.scrollLeft = element.scrollWidth; });
    const row = table.locator("tbody tr").first();
    await expect(row.locator("td").first()).toHaveCSS("position", "sticky");
    const bounds = await row.evaluate((element) => {
      const fixed = element.querySelector("td")!.getBoundingClientRect();
      const value = element.querySelector("td:last-child")!;
      const range = document.createRange();
      range.selectNodeContents(value);
      const text = range.getBoundingClientRect();
      const viewport = element.closest(".v2-chart-table-scroll")!.getBoundingClientRect();
      return { fixedRight: fixed.right, textLeft: text.left, textRight: text.right, viewportRight: viewport.right };
    });
    expect(bounds.textLeft).toBeGreaterThanOrEqual(bounds.fixedRight);
    expect(bounds.textRight).toBeLessThanOrEqual(bounds.viewportRight + 1);
    await expect(row.locator("td").last()).toContainText("98,765,432");
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    await table.screenshot({ path: test.info().outputPath("table-scroll.png") });
  });
}


test.describe("compact report entry and reading", () => {
  test.use({ hasTouch: true });

  test("explicit report, file and code links retain the addressed pane through hydration and history", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openReport(page);
    await page.goto("/?session=s_e2e&result=report");
    await expect(page.locator(".canvas-report-article")).toBeVisible();
    await expect(page.getByRole("tab").nth(1)).toHaveAttribute("aria-selected", "true");
    await page.reload();
    await expect(page.locator(".canvas-report-article")).toBeVisible();
    await expect(page.locator("[data-phone-reading-bar]")).toBeVisible();
    await expect(page.locator(".canvas-report-history-btn")).toHaveCount(0);
    await page.goto("/?session=s_e2e&result=code");
    await expect(page.locator(".results-body--code")).toBeVisible();
    await page.goto("/?session=s_e2e&result=art_stdout");
    await expect(page.locator(".results-body--artifact")).toBeVisible();
    await page.goBack();
    await expect(page.locator(".results-body--code")).toBeVisible();
    await page.goBack();
    await expect(page.locator(".canvas-report-article")).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("mobile-report-entry.png") });
    await page.getByRole("tab", { name: "对话", exact: true }).click();
    await page.setViewportSize({ width: 1440, height: 844 });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole("tab", { name: "对话", exact: true })).toHaveAttribute("aria-selected", "true");
  });

  test("revenue reading retains every observation across touch widths and repeated resizing", async ({ page }) => {
    await page.route("**/api/v1/canvas/by-session/s_e2e**", async (route) => {
      const response = await route.fetch();
      const dto = await response.json();
      const section = dto.sections.find((item: { id: string }) => item.id === "sec_results_data");
      section.spec = {
        kind: "line", title: "Revenue and cost", row_count: 12,
        columns: [
          { key: "month", header: "Month", type: "date" },
          { key: "revenue", header: "Revenue", type: "numeric", unit: "万元" },
          { key: "cost", header: "Cost", type: "numeric", unit: "万元" },
        ],
        rows: Array.from({ length: 12 }, (_, index) => ({ month: `2026年${index + 1}月`, revenue: 52.12345 + index * 2, cost: 30.16789 + index })),
      };
      await route.fulfill({ json: dto });
    });
    await openReport(page);
    await page.goto("/?session=s_e2e&result=report");
    const chart = page.getByRole("img", { name: "Revenue and cost", exact: true });
    await expect(chart).toBeVisible();
    for (const width of [1440, 1280, 768, 390, 320, 1280]) {
      await page.setViewportSize({ width, height: 844 });
      await chart.scrollIntoViewIfNeeded();
      await expect.poll(async () => chart.evaluate((element) => Math.round(element.getBoundingClientRect().width))).toBeGreaterThan(200);
      await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
      let previousPixels = "";
      await expect.poll(async () => {
        const pixels = await chart.evaluate((element) => (element as HTMLCanvasElement).toDataURL());
        const stable = pixels === previousPixels;
        previousPixels = pixels;
        return stable;
      }).toBe(true);
      await page.locator(".canvas-report-article").screenshot({ path: test.info().outputPath(`revenue-${width}.png`) });
    }
    await page.setViewportSize({ width: 390, height: 500 });
    await chart.tap();
    await expect(chart).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("touch-landscape.png") });
  });
});

for (const colorMode of ["palette", "HEX"] as const) for (const width of [390, 1440]) {
  test(`native ${colorMode} color survives reload at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/canvas/by-session/s_e2e**", async (route) => {
      const response = await route.fetch();
      const dto = await response.json();
      if (Array.isArray(dto.sections)) {
        const section = dto.sections.find((item: { id: string }) => item.id === "sec_results_data");
        section.spec = { kind: "bar", title: "Native green counts", row_count: 2,
          columns: [{ key: "range", header: "Range", type: "categorical" }, { key: "count", header: "Count", type: "numeric", ...(colorMode === "HEX" ? { color_hex: "#00A651" } : { color_index: 4 }) }],
          rows: [{ range: "1-100", count: 25 }, { range: "101-200", count: 21 }],
          explicit_presentation: { version: 1, mode: "plot", axes: { x: "range", y: ["count"], x_scale: "category" }, group_by: [] } };
      }
      await route.fulfill({ json: dto });
    });
    await openReport(page);
    await page.goto("/?session=s_e2e&result=report");
    const figure = page.getByRole("figure", { name: "Native green counts", exact: true });
    const greenPixels = () => figure.locator("canvas").first().evaluate((canvas: HTMLCanvasElement, exactHex: boolean) => {
      const pixels = canvas.getContext("2d")!.getImageData(0, 0, canvas.width, canvas.height).data;
      let count = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i + 3]! > 200 && (exactHex
          ? pixels[i] === 0 && pixels[i + 1] === 166 && pixels[i + 2] === 81
          : pixels[i + 1]! > pixels[i]! * 1.05 && pixels[i + 1]! > pixels[i + 2]! * 1.1)) count += 1;
      }
      return count;
    }, colorMode === "HEX");
    await expect(figure.locator("canvas").first()).toBeVisible();
    await expect.poll(greenPixels).toBeGreaterThan(500);
    await page.reload();
    await expect.poll(greenPixels).toBeGreaterThan(500);
    await page.screenshot({ path: test.info().outputPath(`native-${colorMode}-${width}.png`) });
  });
}
