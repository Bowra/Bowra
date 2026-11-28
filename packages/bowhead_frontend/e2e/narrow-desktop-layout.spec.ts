import { expect, test, type Page } from "@playwright/test";

import { enableResultsWorkspace, mockAuthedApp } from "./_app-mocks";
import { rememberResultsOpen } from "./_results-open";
import zh from "../edition/shared-messages/zh-CN.json";
import de from "../edition/shared-messages/de-DE.json";

/**
 * Narrow desktop layout (docs/sprints/narrow-desktop-layout-2026-11-24, design §9 C): the report toolbar and the
 * file header keep one control row at 1280–1536 px windows, with the sidebar expanded, at the default split and
 * with the chat at its minimum, in Chinese and German, idle, while writing and in edit mode. Nothing overlaps
 * or is clipped, and every action the toolbar moved is in More under the same name.
 * Pure mock: the report is hydrated through `__CANVAS_TEST_HYDRATE__`, writing through `__CANVAS_TEST_EVENTS__`.
 */

const SESSION = "s_wb4b";
const CANVAS = `cv_${SESSION}`;
const SECTIONS = [
  { id: "sec_nd_h1", kind: "h1", text: "华东区 2026 年第三季度销售额同比下滑的归因分析与后续建议" },
  { id: "sec_nd_narr", kind: "narrative", markdown: "家电 [1]、数码 [2] 与服装 [3] 合计贡献了大部分降幅。", unverified_numbers: ["8.4%"] },
  { id: "sec_nd_src", kind: "sources", entries: [{ label: "订单明细" }, { label: "门店表" }, { label: "品类表" }] },
];
const LOCALES = { "zh-CN": zh, "de-DE": de } as const;
type Locale = keyof typeof LOCALES;
type Split = "default" | "chat-minimum";
const SPLIT_RATIO: Record<Split, string> = { default: "0.4", "chat-minimum": "0.2" };

const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

async function prepare(page: Page, width: number, split: Split) {
  await page.setViewportSize({ width, height: 800 });
  await page.addInitScript((ratio) => window.localStorage.setItem("bowhead.split_ratio", ratio), SPLIT_RATIO[split]);
  await mockAuthedApp(page);
  await enableResultsWorkspace(page);
  await rememberResultsOpen(page, [SESSION]);
  await page.route(`**/api/v1/canvas/${CANVAS}/status`, (route) =>
    route.fulfill(json({ status: "active", effectiveStatus: "active", quarantinedCount: 0 })),
  );
  await page.route(`**/api/v1/canvas/${CANVAS}/revisions`, (route) => route.fulfill(json({ items: [] })));
}

async function hydrate(page: Page) {
  await page.waitForFunction(() => "__CANVAS_TEST_HYDRATE__" in window, undefined, { timeout: 15_000 });
  await page.evaluate(([sid, sections]) => {
    (window as unknown as { __CANVAS_TEST_HYDRATE__: (p: unknown) => void }).__CANVAS_TEST_HYDRATE__({
      canvas: { id: `cv_${sid}`, sessionId: sid, title: "华东区 Q3 销售额同比下滑归因" },
      sections,
    });
  }, [SESSION, SECTIONS] as const);
  // A report no longer opens Results by itself (chat running state R2.5): a live scenario's reader opens the strip.
  const edge = page.locator(".v2-ws-edge--canvas");
  if (await edge.isVisible().catch(() => false)) await edge.click();
  await expect(page.locator(".results-report-unified-toolbar .rc-trust")).toBeVisible({ timeout: 15_000 });
}

async function openReport(page: Page, width: number, split: Split) {
  await prepare(page, width, split);
  await page.goto(`/?session=${SESSION}`);
  await expect(page.locator("textarea").first()).toBeEditable({ timeout: 15_000 });
  await hydrate(page);
}

/** Live writing: keep the mock turn running and stream one section into the report. */
async function openWriting(page: Page, width: number, split: Split) {
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = ((fn: TimerHandler, delay?: number, ...args: unknown[]) =>
      typeof delay === "number" && delay > 1500 && delay < 30_000 ? 0 : original(fn, delay, ...args)) as typeof window.setTimeout;
  });
  await prepare(page, width, split);
  await page.route("**/api/v1/canvas/by-session/**", (route) =>
    route.fulfill(json({
      canvas: { id: CANVAS, sessionId: SESSION, workspaceId: "ws_smoke", title: "华东 Q3", status: "done", createdAt: "2026-09-26T00:00:00Z", updatedAt: "2026-09-26T00:00:00Z" },
      sections: SECTIONS,
      sourceRefs: [],
    })),
  );
  await page.goto("/?scenario=peer");
  await expect(page.locator(".bh-band__bar").first()).toBeVisible({ timeout: 15_000 });
  await hydrate(page);
  await page.evaluate(() => {
    const events = (window as unknown as { __CANVAS_TEST_EVENTS__: Record<string, (p: unknown) => void> }).__CANVAS_TEST_EVENTS__;
    events.turnStreaming({ turn_id: "t_nd" });
    events.sectionStreaming({ section_id: "sec_nd_writing", kind: "narrative" });
    events.chunk({ section_id: "sec_nd_writing", delta: "## 闭店影响\n闭店影响约占 1.2 个百分点" });
  });
  await expect(page.locator(".results-report-unified-toolbar .rc-toolbar-stream")).toBeVisible();
}

interface RowAudit {
  fit: string;
  oneRow: boolean;
  overflow: number;
  overlaps: string[];
  clipped: string[];
  hidden: { share: boolean; export: boolean };
}

/** Read-only layout audit of one control row (the spike's harness/layout-audit.js, reduced to one container). */
async function auditRow(page: Page, selector: string, secondRow: string): Promise<RowAudit> {
  return page.locator(selector).first().evaluate((bar, extraSkip) => {
    const fit = bar.getAttribute("data-fit") ?? "";
    const skip = [
      ".results-report-writing-hint",
      fit.includes("mode-status") ? ".results-report-status-group" : "",
      fit.includes("mode-edit") ? ".results-report-edit-controls" : "",
      extraSkip,
    ].filter(Boolean).join(", ");
    const rendered = (el: Element) => {
      const box = el.getBoundingClientRect();
      return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== "hidden";
    };
    const items = (row: Element): Element[] => Array.from(row.children)
      .flatMap((child) => getComputedStyle(child).display === "contents" ? items(child) : [child]);
    const oneLine = (row: Element | null) => {
      if (!row) return true;
      const list = items(row).filter((item) => !(skip && item.matches(skip)) && rendered(item));
      if (list.length < 2) return true;
      const firstBottom = Math.min(...list.map((item) => item.getBoundingClientRect().bottom));
      return list.every((item) => item.getBoundingClientRect().top < firstBottom - 2);
    };
    const name = (el: Element) => (el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 40);
    const controls = Array.from(bar.querySelectorAll("button, a[href]")).filter(rendered);
    const overlap = (a: DOMRect, b: DOMRect) =>
      Math.min(a.right, b.right) - Math.max(a.left, b.left) > 3 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 3;
    const overlaps: string[] = [];
    controls.forEach((a, i) => controls.slice(i + 1).forEach((b) => {
      if (!a.contains(b) && !b.contains(a) && overlap(a.getBoundingClientRect(), b.getBoundingClientRect())) overlaps.push(`${name(a)} x ${name(b)}`);
    }));
    // Painted text only: text inside a visually hidden label (clip-path) is skipped, and text cut by an
    // ellipsis or `overflow: hidden` counts only where its clipping box lets it paint.
    const paintBox = (host: Element): DOMRect | null => {
      let box = new DOMRect(-1e6, -1e6, 2e6, 2e6);
      for (let el: Element | null = host; el && el !== bar.parentElement; el = el.parentElement) {
        const style = getComputedStyle(el);
        if (style.clipPath !== "none") return null;
        if (/(hidden|clip)/.test(style.overflowX)) {
          const r = el.getBoundingClientRect();
          const left = Math.max(box.left, r.left);
          const right = Math.min(box.right, r.right);
          box = new DOMRect(left, box.top, Math.max(0, right - left), box.height);
        }
      }
      return box;
    };
    const walker = document.createTreeWalker(bar, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const host = node.parentElement;
      if (!host || !node.textContent?.trim() || !rendered(host)) continue;
      const clip = paintBox(host);
      if (!clip) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const raw of Array.from(range.getClientRects())) {
        const left = Math.max(raw.left, clip.left);
        const right = Math.min(raw.right, clip.right);
        if (right - left <= 0) continue;
        const line = new DOMRect(left, raw.top, right - left, raw.height);
        const under = controls.find((c) => !c.contains(host) && overlap(c.getBoundingClientRect(), line));
        if (under) overlaps.push(`"${node.textContent.trim().slice(0, 20)}" under ${name(under)}`);
      }
    }
    const box = bar.getBoundingClientRect();
    const clipped = controls
      .filter((c) => { const r = c.getBoundingClientRect(); return r.right > box.right + 1 || r.left < box.left - 1; })
      .map(name);
    const share = bar.querySelector(".results-report-share");
    const exportButton = bar.querySelector(".ws-export");
    return {
      fit,
      oneRow: oneLine(bar) && oneLine(bar.querySelector(".results-report-actions")) && oneLine(bar.querySelector(".rc-trust"))
        && oneLine(bar.querySelector(".artifact-viewer-actions")) && oneLine(bar.querySelector(".artifact-object-actions")),
      overflow: bar.scrollWidth - bar.clientWidth,
      overlaps,
      clipped,
      hidden: {
        share: Boolean(share) && !rendered(share!),
        export: !exportButton || !rendered(exportButton),
      },
    };
  }, secondRow);
}

async function expectToolbarHolds(page: Page, locale: Locale) {
  const copy = LOCALES[locale].canvas;
  const audit = await auditRow(page, ".results-report-unified-toolbar", "");
  expect(audit.oneRow, `one control row (data-fit="${audit.fit}")`).toBe(true);
  expect(audit.overflow).toBeLessThanOrEqual(1);
  expect(audit.overlaps).toEqual([]);
  expect(audit.clipped).toEqual([]);
  if (audit.hidden.share || audit.hidden.export) {
    await page.locator(".results-report-unified-toolbar").getByRole("button", { name: copy.hdr_more_aria, exact: true }).click();
    if (audit.hidden.export) await expect(page.getByRole("menuitem", { name: copy.hdr_export })).toBeVisible();
    if (audit.hidden.share) await expect(page.getByRole("menuitem", { name: `${copy.hdr_share}…` })).toBeVisible();
    await page.keyboard.press("Escape");
  }
}

for (const locale of Object.keys(LOCALES) as Locale[]) {
  test.describe(`report toolbar (${locale})`, () => {
    test.use({ locale });
    for (const width of [1280, 1366, 1440, 1536]) {
      for (const split of ["default", "chat-minimum"] as const) {
        test(`idle report keeps one control row at ${width} px, ${split}`, async ({ page }) => {
          await openReport(page, width, split);
          await expectToolbarHolds(page, locale);
        });
      }
    }

    test("while writing, the progress takes the second row as one piece and the control row holds (1280, default)", async ({ page }) => {
      await openWriting(page, 1280, "default");
      await expectToolbarHolds(page, locale);
      // The status group is never squashed or moved into More (Stop itself lives in the composer).
      const progress = page.locator(".results-report-unified-toolbar .rc-toolbar-stream");
      await expect(progress).toBeVisible();
      expect((await progress.boundingBox())!.width).toBeGreaterThan(40);
      await expect(page.locator(".results-report-unified-toolbar .results-report-writing-hint")).toBeVisible();
    });

    test("in edit mode the edit bar keeps one line and the control row holds (1280, chat minimum)", async ({ page }) => {
      await openReport(page, 1280, "chat-minimum");
      await page.locator(".results-report-unified-toolbar .report-edit-btn").first().click();
      await expect(page.locator(".results-report-unified-toolbar .report-edit-btn--active")).toBeVisible();
      await expectToolbarHolds(page, locale);
    });
  });
}

test.describe("toolbar around the sidebar's own breakpoint", () => {
  for (const width of [1279, 1281]) {
    test(`idle report keeps one control row at ${width} px`, async ({ page }) => {
      await openReport(page, width, "default");
      await expectToolbarHolds(page, "zh-CN");
    });
  }
});

/** A real one-page PDF stands in for the deck render (as in pptx-preview-busy.spec.ts). */
function onePagePdf(): Buffer {
  const content = "BT /F1 24 Tf 40 180 Td (Slide) Tj ET\n";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Count 1 /Kids [4 0 R] >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 480 270] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
    `<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}endstream`,
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  pdf += offsets.slice(1).map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("");
  pdf += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

for (const locale of Object.keys(LOCALES) as Locale[]) {
  test.describe(`file header (${locale})`, () => {
    test.use({ locale });
    for (const [width, split] of [[1280, "default"], [1280, "chat-minimum"], [1366, "default"], [1440, "default"]] as const) {
      test(`a deck's header keeps title and actions on one row without the MIME type at ${width} px, ${split}`, async ({ page }) => {
        await prepare(page, width, split);
        const artifact = {
          // A generated deck (deliverable): Download is a primary action next to Reference, as on real decks.
          id: "art_nd_deck", filename: "B-区域销售-表格图表-4页.pptx", size: 44_544, source: "python_sandbox", kind: "deliverable",
          mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          created_at: "2026-11-24T00:00:00Z", preview_available: true,
        };
        await page.route("**/api/v1/chat/sessions/*/reference-queue?*", (route) => route.fulfill(json({ items: [] })));
        await page.route("**/api/v1/results/capabilities", (route) => route.fulfill(json({
          dto_version: 1, workspace: true, complete_list: false, scale_nav: false, max_page_size: 50,
        })));
        await page.route("**/api/v1/sessions/s_nd_deck/artifacts*", (route) => route.fulfill(json({ items: [artifact] })));
        await page.route("**/api/v1/files/art_nd_deck/representations/details", (route) => route.fulfill(json({
          representation: "details", family: "presentation", revision: "fixture-1", details: artifact,
        })));
        await page.route("**/api/v1/files/art_nd_deck/representations/pptx-pdf", (route) =>
          route.fulfill({ contentType: "application/pdf", body: onePagePdf() }));
        await page.goto("/?session=s_nd_deck&result=art_nd_deck&mode=preview");
        const head = page.locator(".result-file-viewer .artifact-viewer-head").first();
        await expect(head).toBeVisible({ timeout: 15_000 });
        const reopen = page.getByRole("button", { name: LOCALES[locale].split_pane.reopen.replace("{label}", "").trim() });
        if (await reopen.count()) await reopen.first().click();
        await expect(page.locator(".v2-ws-left")).toBeVisible();
        await expect(head.locator(".artifact-viewer-title > span")).toHaveText(/^PPTX · /);
        // Above 640 px the header is one row. The ≤640 px layout stacks the title over the actions, which
        // still keep one row (1280 px at the default split leaves the Results column about 595 px).
        const results = await page.locator(".v2-ws-right").boundingBox();
        const column = await head.evaluate((el) => getComputedStyle(el).flexDirection === "column");
        expect(column).toBe(results!.width <= 640);
        const audit = await auditRow(page, ".result-file-viewer .artifact-viewer-head", column ? ".artifact-viewer-title" : "");
        expect(audit.oneRow, `one action row (data-fit="${audit.fit}")`).toBe(true);
        expect(audit.overflow).toBeLessThanOrEqual(1);
        expect(audit.overlaps).toEqual([]);
        expect(audit.clipped).toEqual([]);
        const box = await head.boundingBox();
        if (!column) expect(box!.height).toBeLessThan(60);
      });
    }
  });
}

test.describe("composer in a narrow chat column", () => {
  test("a mouse keeps 32 px controls and a 40 px Send at the chat minimum", async ({ page }) => {
    await openReport(page, 1280, "chat-minimum");
    const composer = page.locator(".v2-ws-left [data-composer-layout-v2]");
    await expect(composer).toHaveAttribute("data-layout-mode", /compact|ultra/);
    const heights = await composer.locator(".composer-v2__toolbar button:not([disabled])").evaluateAll((buttons) =>
      buttons.filter((b) => b.getBoundingClientRect().width > 0).map((b) => Math.round(b.getBoundingClientRect().height)));
    expect(heights.length).toBeGreaterThan(1);
    for (const height of heights) expect(height).toBeLessThanOrEqual(40);
  });
});

test.describe("chart card header in a narrow chat card", () => {
  // The chat column at its minimum leaves a 294 px card. A table chart and a chart with a kind switch keep their
  // actions beside the title: the title wraps instead of leaving the actions on a row of their own (design §7).
  for (const fixture of ["hbar-long-label", "table-long"] as const) {
    test(`${fixture}: the actions stay beside the title at 294 px`, async ({ page }) => {
      await mockAuthedApp(page);
      // The board asks for 320 px (a chat surface) and is capped by the page: 20 px padding each side of a
      // 334 px viewport leaves the 294 px card of a chat column at its minimum.
      await page.setViewportSize({ width: 334, height: 900 });
      await page.goto(`/spike/chart-audit?only=${fixture}&width=320`, { waitUntil: "domcontentloaded" });
      const header = page.locator(".v2-chart-header").first();
      await expect(header.locator(".v2-chart-actions")).toBeAttached({ timeout: 15_000 });
      // In chat the block sits directly in `.chat-inline-chart`; the audit board renders it without that wrapper.
      // Poll: a chart that cannot draw here re-renders as its table, which replaces the header once.
      await expect.poll(() => header.evaluate((el) => {
        el.parentElement!.parentElement!.classList.add("chat-inline-chart");
        const title = el.querySelector(".v2-chart-title")!.getBoundingClientRect();
        const actions = el.querySelector(".v2-chart-actions")!.getBoundingClientRect();
        return { narrow: el.getBoundingClientRect().width <= 294, beside: actions.top < title.bottom - 2 };
      }), { timeout: 5_000 }).toEqual({ narrow: true, beside: true });
    });
  }
});
