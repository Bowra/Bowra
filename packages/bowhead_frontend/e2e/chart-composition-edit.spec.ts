import { expect, test } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import type { ReportSection } from "../lib/types";
import type {} from "./_chart-composition-edit-entry";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import postcss from "postcss";
import tailwindcss from "@tailwindcss/postcss";
import { linkedFocusFixture } from "./_chart-linked-focus-fixture";
import { timeWindowFixture } from "./_chart-time-window-fixture";
import { sharedScaleFixture } from "./_chart-shared-scale-fixture";
import { evidencePlanForViewer } from "../lib/chart/presentation-plan";
import { preparedFramesFixture } from "./_chart-prepared-frames-fixture";
import { annotationsFixture } from "./_chart-annotations-fixture";
import { contextualEvidenceFixture, trendTableFixture } from "./_chart-contextual-reading-fixture";
import { guidedReadingFixture } from "./_chart-guided-reading-fixture";
import { observationAdaptersFixture } from "./_chart-observation-adapters-fixture";
import { cpus, platform, release, totalmem } from "node:os";


const nativeStyles = readFile(resolve(process.cwd(), "app/globals.css"), "utf8")
  .then((source) => postcss([tailwindcss()]).process(source, { from: resolve(process.cwd(), "app/globals.css") }))
  .then((result) => result.css);

for (const [width, mode] of [[1000, "light"], [320, "dark"]] as const) {
  test(`contextual reading completes source scope, exact dates and native Table trends at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    await page.evaluate(({ fixture, mode }) => { window.chartCompositionHost.configure({ mode }); window.chartCompositionHost.hydrate(fixture); window.chartCompositionHost.embedded(true); }, { fixture: contextualEvidenceFixture(), mode });
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    await expect(page.locator('[data-chart-annotation="period"]')).toContainText("Partial reporting period");
    await page.locator('[data-chart-annotation="period"]').getByRole("button").click();
    await expect(page.locator("[data-chart-observation-details]")).toContainText("Cited source text");
    await page.locator("[data-chart-observation-details] .rc-source").click();
    await expect(page.getByText("Sources and definitions", { exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("[data-chart-observation-details]")).toContainText("Partial reporting period");
    await page.keyboard.press("Escape");
    await page.screenshot({ path: test.info().outputPath(`source-scope-${width}-${mode}.png`), fullPage: true });
    await page.evaluate((fixture) => window.chartCompositionHost.hydrate(fixture), timeWindowFixture());
    const controls = page.locator("[data-chart-time-window]");
    await controls.getByRole("button", { name: "Enter a date…" }).click();
    const inputs = page.locator("[data-chart-date-entry] input");
    await inputs.first().fill("2026-99"); await inputs.last().fill("2026-03");
    await page.locator("[data-chart-date-entry]").getByRole("button", { name: "Apply", exact: true }).click();
    await expect(page.locator("[data-chart-date-entry] [role=alert]")).toBeVisible();
    await expect(controls.locator("select").first()).toHaveValue("0");
    await inputs.first().fill("2026-02"); await inputs.last().press("Enter");
    await expect(controls.locator("select").first()).toHaveValue("1");
    await expect(controls.getByRole("button", { name: "Enter a date…" })).toBeFocused();
    await page.evaluate((fixture) => window.chartCompositionHost.hydrate(fixture), trendTableFixture());
    const table = page.locator('[data-chart-view="detail"]');
    await expect(table.getByRole("columnheader", { name: /Start/ })).toContainText("2026-01");
    await expect(table.getByRole("columnheader", { name: /End/ })).toContainText("2026-03");
    await expect(table.getByRole("row")).toHaveCount(3);
    await expect(table.getByRole("columnheader", { name: /End/ })).toContainText("USD");
    await expect(table.getByRole("row").nth(1).getByRole("cell").nth(2)).toHaveText("12.12");
    await expect(table.getByRole("row").nth(1)).toContainText("—");
    await table.getByRole("columnheader", { name: /End/ }).click();
    await table.getByRole("button", { name: "View observations for A" }).click();
    await expect(table.getByRole("columnheader", { name: "Month", exact: true })).toBeVisible();
    await expect(table.getByRole("row")).toHaveCount(4);
    expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
    await expect(table.getByRole("row")).toHaveCount(3);
    await expect(table.getByRole("columnheader", { name: /Start/ })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath(`table-trends-${width}-${mode}.png`), fullPage: true });
    const denied = trendTableFixture(), plan = evidencePlanForViewer(denied.spec.presentation_plan)!;
    plan.dataset = { artifact_id: "frames.fixture", availability: "unavailable" };
    plan.table_trends = [{ view_id: "detail", availability: "unavailable" }];
    await page.evaluate((fixture) => window.chartCompositionHost.update({ ...fixture, rev: 2 }), denied);
    await expect(table.locator("[data-chart-trend-scope]")).toHaveCount(0);
    expect(writes).toEqual([]); expect(errors).toEqual([]);
  });
}


for (const [width, mode] of [[1000, "light"], [320, "dark"]] as const) {
  test(`pinned observations preserve dual units, table identity and keyboard access at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = annotationsFixture();
    fixture.spec.views![0].spec.kind = "dual_axis";
    for (const view of fixture.spec.views!) for (const column of view.spec.columns) {
      if (column.key === "cost") { column.header = "Margin"; column.unit = "%"; }
    }
    const plan = evidencePlanForViewer(fixture.spec.presentation_plan)!;
    for (const item of plan.annotations!) if (item.availability === "available" && item.view_id === "relationship") item.encoding = "dual_axis";
    await page.evaluate(({ fixture, mode }) => { window.chartCompositionHost.configure({ mode }); window.chartCompositionHost.hydrate(fixture); }, { fixture, mode });
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const caption = page.locator('[data-chart-annotation="observed"]').getByRole("button");
    const canvas = page.locator('[data-chart-view="relationship"] canvas');
    await canvas.scrollIntoViewIfNeeded();
    const mark = await page.evaluate(() => window.chartCompositionHost.firstMarkPosition("relationship"));
    expect(mark).not.toBeNull();
    await canvas.hover({ position: mark! });
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.tooltipLines("relationship").sort())).toEqual([
      "Margin: 30.17%", "Revenue: USD 52.12",
    ]);
    await page.screenshot({ path: test.info().outputPath(`dual-units-hover-${width}-${mode}.png`), fullPage: true });
    await caption.focus(); await page.keyboard.press("Enter");
    const pinned = page.locator("[data-chart-observation-details]");
    await expect(pinned).toContainText("USD 52.12");
    await expect(pinned).toContainText("30.17%");
    await expect(pinned).toContainText("Margin");
    await page.screenshot({ path: test.info().outputPath(`dual-units-details-${width}-${mode}.png`), fullPage: true });
    await page.keyboard.press("Escape"); await expect(caption).toBeFocused();
    const table = page.locator('[data-chart-view="detail"]');
    await table.getByRole("columnheader", { name: /Revenue/ }).click();
    const row = table.getByRole("row").filter({ hasText: "2026-05" });
    await row.click();
    await expect(pinned).toContainText("USD 0.000012");
    await expect(pinned).toContainText("45%");
    await page.keyboard.press("Escape"); await expect(row).toBeFocused();
    await row.press("Enter"); await expect(pinned).toContainText("USD 0.000012");
    expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
    await expect(pinned).toHaveCount(0);
    expect(writes).toEqual([]); expect(errors).toEqual([]);
  });
}

for (const layout of ["grid2", "split"] as const) {
  test(`native peer chart plots align with wrapped headings and kind changes in ${layout}`, async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 1000 });
    const errors: string[] = [], writes: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const child = {
      kind: "line" as const, title: "Monthly revenue", subtitle: "Series names at line endpoints", row_count: 3,
      columns: [{ key: "month", header: "Month", type: "date" as const }, { key: "revenue", header: "Revenue", type: "numeric" as const, unit: "USD" }, { key: "cost", header: "Cost", type: "numeric" as const, unit: "USD" }],
      rows: [{ month: "2026-01", revenue: 52.12345, cost: 30.16789 }, { month: "2026-02", revenue: 63.45678, cost: 37.12345 }, { month: "2026-03", revenue: 68.34567, cost: 40.45678 }],
    };
    const fixture: Extract<ReportSection, { kind: "chart" }> = {
      id: "sec_peer_layout", kind: "chart", rev: 1,
      spec: { ...child, title: "Revenue and cost",
        views: [{ id: "trend", kind: "chart", spec: child }, { id: "relationship", kind: "chart", spec: { ...child, kind: "scatter", title: "Revenue versus cost across each monthly observation", subtitle: "Each point uses the original prepared observation, with revenue and cost in USD." } }, { id: "details", kind: "chart", spec: { ...child, kind: "table", title: "Original observations" } }],
        layout: { type: "stack", children: [{ type: layout, children: [{ view: "trend" }, { view: "relationship" }] }, { view: "details" }] } },
    };
    await page.evaluate((value) => { window.chartCompositionHost.hydrate(value); window.chartCompositionHost.readOnly(true); }, fixture);
    const trend = page.locator('[data-chart-view="trend"]'), relationship = page.locator('[data-chart-view="relationship"]');
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const geometry = () => page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>("[data-chart-view]"), (view) => {
      const body = view.querySelector(".v2-chart-canvas-wrap, .v2-chart-table-scroll")!.getBoundingClientRect();
      return { top: body.top, bottom: body.bottom, width: view.getBoundingClientRect().width };
    }));
    await expect.poll(async () => { const [a, b] = await geometry(); return Math.abs(a.top - b.top); }).toBeLessThan(1);
    const [a, b] = await geometry();
    expect(Math.abs(a.bottom - b.bottom)).toBeLessThan(1);
    expect(a.width / b.width).toBeCloseTo(layout === "grid2" ? 1 : 2, 1);
    await page.locator("[data-chart-composition]").screenshot({ path: test.info().outputPath(`peer-layout-${layout}.png`) });
    await relationship.getByRole("button", { name: /^Scatter/ }).click();
    await page.getByRole("menuitemradio", { name: "Table", exact: true }).click();
    await expect(relationship.locator("tbody tr")).toHaveCount(3);
    await expect(relationship).toContainText("52.12");
    await expect.poll(async () => { const [a, b] = await geometry(); return Math.abs(a.top - b.top); }).toBeLessThan(1);
    const tallerHeader = await trend.locator(".v2-chart-header").evaluate((header) => header.getBoundingClientRect().height);
    const shortened = structuredClone(fixture);
    shortened.rev = 2;
    shortened.spec.views![1].spec.title = "Relationship";
    shortened.spec.views![1].spec.subtitle = undefined;
    await page.evaluate((value) => window.chartCompositionHost.update(value), shortened);
    await expect.poll(async () => trend.locator(".v2-chart-header").evaluate((header) => header.getBoundingClientRect().height)).toBeLessThan(tallerHeader);
    await expect.poll(async () => { const [a, b] = await geometry(); return Math.abs(a.top - b.top); }).toBeLessThan(1);
    await page.setViewportSize({ width: 390, height: 1000 });
    await expect.poll(async () => { const [a, b] = await geometry(); return b.top > a.bottom; }).toBe(true);
    await expect.poll(() => trend.locator(".v2-chart-header").evaluate((header) => (header as HTMLElement).style.getPropertyValue("--chart-peer-header-height"))).toBe("");
    await expect.poll(() => page.evaluate(() => document.querySelector("[data-chart-composition]")!.getBoundingClientRect().right <= innerWidth)).toBe(true);
    expect(writes).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const width of [1000, 390]) {
  test(`native compound observations keep pivot identity in one-period windows at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    const writes: string[] = [],
      reads: string[] = [],
      errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download|\/files\//.test(route.request().url())) reads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = observationAdaptersFixture();
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), fixture);
    const details = page.locator('[data-chart-view="detail"]');
    await expect(details).toContainText("0.000012345");
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const position = await page.evaluate(() => window.chartCompositionHost.firstMarkPosition("trend"));
    expect(position).not.toBeNull();
    await page.locator('[data-chart-view="trend"] canvas').click({ position: position! });
    await expect(details.locator('tr[aria-selected="true"]')).toHaveCount(2);
    await expect(page.locator("[data-chart-observation-details]")).toContainText("52.12");
    await page.keyboard.press("Escape");
    await page.getByLabel("Start", { exact: true }).selectOption("1");
    await expect(details.locator("tbody tr")).toHaveCount(2);
    await expect(details.locator('tr[aria-selected="true"]')).toHaveCount(0);
    await expect(details).not.toContainText("52.12");
    await expect(details.locator("tbody tr").filter({ hasText: "West" })).toContainText("—");
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.chartLabels("trend"))).toEqual(["2026-02"]);
    await expect(page.locator('[data-chart-view="trend"] [data-testid="chart-incompatible-units"]')).toHaveCount(0);
    await details.locator("tbody tr").filter({ hasText: "East" }).click();
    await expect(details.locator('tr[aria-selected="true"]')).toHaveCount(2);
    expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
    await expect(details.locator("tbody tr")).toHaveCount(4);
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("0");
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.chartLabels("trend"))).toEqual(["2026-01", "2026-02"]);
    await expect.poll(() => page.evaluate(() => [...document.querySelectorAll("canvas")].every((canvas) => {
      const pixels = canvas.getContext("2d")!.getImageData(0, 0, canvas.width, canvas.height).data;
      for (let index = 0; index < pixels.length; index += 16) {
        if (pixels[index + 3]! > 0 && pixels[index]! + pixels[index + 1]! + pixels[index + 2]! < 720) return true;
      }
      return false;
    }))).toBe(true);
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.scatterPoints("relationship").values)).toEqual(expect.arrayContaining([{ x: 0, y: 5 }, { x: 0.000012345, y: 0.05 }, { x: 52.12345, y: 30.16789 }]));
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.scatterPoints("relationship").settled)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`compound-observations-${width}.png`), fullPage: true });
    expect(writes).toEqual([]);
    expect(reads).toEqual([]);
    expect(errors).toEqual([]);
  });
  test(`native observed annotations retain evidence through guide, scope and access at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [], reads: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download|\/files\//.test(route.request().url())) reads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ type: "text", content: "Definition\nRevenue includes returns." }) });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = annotationsFixture(true);
    const noteAlias = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    evidencePlanForViewer(fixture.spec.presentation_plan)!.evidence_notes[0].source_ref = {
      id: noteAlias, source_alias: noteAlias, access_state: "linked_accessible", safe_label: "Definition",
      safe_preview_url: "/api/v1/report-artifacts/note/preview", safe_url: "/api/v1/report-artifacts/note/download",
    };
    await page.evaluate((value) => { window.chartCompositionHost.hydrate(value); window.chartCompositionHost.embedded(true); }, fixture);
    const point = page.locator('[data-chart-annotation="observed"]');
    await expect(point).toContainText("52.12");
    await expect(page.locator('[data-chart-annotation="period"]')).toContainText("2026-01 – 2026-06");
    const missing = page.locator('[data-chart-annotation-cell="missing"]');
    await expect(missing).toHaveText("—");
    await expect(missing).toHaveAttribute("title", /Missing observation/);
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    await page.screenshot({ path: test.info().outputPath(`observed-annotations-${width}.png`), fullPage: true });
    await point.getByRole("button").click();
    const pinned = page.locator("[data-chart-observation-details]");
    await expect(pinned).toContainText("52.12");
    await expect(pinned).toContainText("30.17");
    await page.screenshot({ path: test.info().outputPath(`pinned-observation-${width}.png`), fullPage: true });
    expect(reads).toEqual([]);
    await pinned.locator(".rc-source").click();
    await expect(page.getByText("Sources and definitions", { exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(pinned).toContainText("52.12");
    await page.keyboard.press("Escape");
    await expect(pinned).toHaveCount(0);
    await expect(point.getByRole("button")).toBeFocused();

    await page.getByLabel("Start", { exact: true }).selectOption("2");
    await expect(point).toHaveCount(0);
    const guide = page.locator("[data-chart-guide]");
    await guide.getByRole("button", { name: "Next step", exact: true }).click();
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("0");
    await expect(point).toContainText("52.12");
    await expect(pinned).toContainText("Supplied period");
    await expect(pinned).toContainText("80");
    await expect(pinned).toContainText("no interval total");
    await page.keyboard.press("Escape");
    await expect(pinned).toHaveCount(0);
    await guide.getByRole("button", { name: "Next step", exact: true }).click();
    await expect(guide).toContainText("Inspect the missing value");
    await expect(pinned).toContainText("Missing observation");
    await expect(pinned).toContainText("—");
    await page.keyboard.press("Escape");
    await expect(page.locator('[data-chart-annotation="missing"]')).toBeInViewport();
    expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
    await expect(guide).toContainText("Step 1 of 3");
    await page.getByLabel("Start", { exact: true }).selectOption("2");
    await page.locator('[data-chart-annotation="missing"]').getByRole("button").click();
    await expect(pinned).toContainText("Missing observation");
    const denied = structuredClone(fixture);
    const plan = denied.spec.presentation_plan as import("../lib/chart/presentation-plan").EvidencePlanV1;
    plan.evidence_notes = [{ id: "methodology", measure_keys: ["revenue"], source_ref: { id: "methodology.fixture", access_state: "linked_restricted" }, availability: "unavailable" }];
    plan.annotations = plan.annotations!.map((item) => ({ id: item.id, view_id: item.view_id, availability: "unavailable" }));
    plan.guide_steps!.forEach((step) => { step.title = ""; });
    denied.spec.title = ""; denied.spec.views!.forEach((view) => { view.spec.title = ""; });
    await page.evaluate((value) => window.chartCompositionHost.update(value), denied);
    await expect(page.locator("[data-chart-annotation], [data-chart-annotation-cell]")).toHaveCount(0);
    await expect(guide).toHaveCount(0);
    await expect(pinned).toHaveCount(0);
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("2");
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 2 }), fixture);
    await expect(point).toContainText("52.12");
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("0");
    await point.getByRole("button").click();
    await expect(pinned).toContainText("52.12");
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 3 }), fixture);
    await expect(pinned).toHaveCount(0);
    if (width === 1000) {
      await point.getByRole("button").click();
      const legend = await page.evaluate(() => window.chartCompositionHost.legendPosition("relationship", 0));
      expect(legend).not.toBeNull();
      await page.locator('[data-chart-view="relationship"] canvas').click({ position: legend! });
      await expect(pinned).toHaveCount(0);
    }
    expect(writes).toEqual([]); expect(reads).toHaveLength(1); expect(errors).toEqual([]);
  });
  test(`native prepared frames keep exact scope and fixed axes at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [], reads: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download|\/files\//.test(route.request().url())) reads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = preparedFramesFixture();
    await page.evaluate((value) => {
      window.chartCompositionHost.hydrate(value);
      const host = window as unknown as { frameFollowups: unknown[] };
      host.frameFollowups = [];
      window.addEventListener("bowhead:chart-followup", (event) => host.frameFollowups.push((event as CustomEvent).detail));
    }, fixture);
    const player = page.locator("[data-chart-frame-player]");
    const seek = player.getByLabel("Time frame", { exact: true });
    const details = page.locator('[data-chart-view="detail"]');
    await expect(seek).toHaveValue("1");
    await expect(player.getByRole("button", { name: "Play", exact: true })).toBeVisible();
    await expect(details.locator("tbody tr")).toHaveCount(2);
    await expect(details).toContainText("0.000012345");
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(1);
    const axes = await page.evaluate(() => [window.chartCompositionHost.chartScale("relationship", "x"), window.chartCompositionHost.chartScale("relationship", "y")]);
    await seek.selectOption("0");
    await expect(details).toContainText("-5");
    await player.getByRole("button", { name: "Play", exact: true }).click();
    await expect(seek).toHaveValue("2", { timeout: 5000 });
    await expect(details.locator("tbody tr")).toHaveCount(1);
    await expect(details).toContainText("12.123456");
    await expect(player.getByRole("button", { name: "Play", exact: true })).toBeDisabled();
    expect(await page.evaluate(() => [window.chartCompositionHost.chartScale("relationship", "x"), window.chartCompositionHost.chartScale("relationship", "y")])).toEqual(axes);
    await seek.selectOption("0");
    await player.getByRole("button", { name: "Play", exact: true }).click();
    await page.evaluate(() => { (document.querySelector("[data-chart-composition]") as HTMLElement).style.marginTop = "4000px"; });
    await expect(player.getByRole("button", { name: "Play", exact: true })).toBeVisible();
    await page.evaluate(() => { (document.querySelector("[data-chart-composition]") as HTMLElement).style.marginTop = ""; });
    await expect(seek).toHaveValue("0");
    expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
    await expect(seek).toHaveValue("1");
    for (const index of [0, 2]) {
      await seek.selectOption(String(index));
      const plot = page.locator('[data-chart-view="relationship"]');
      await plot.hover();
      await plot.getByRole("button", { name: /More/ }).click();
      await page.getByRole("menuitem", { name: "Inspect", exact: true }).click();
      await page.getByRole("button", { name: "Ask about this chart", exact: true }).click();
    }
    const followups = await page.evaluate(() => (window as unknown as { frameFollowups: Array<{ selectedContext: unknown }> }).frameFollowups);
    expect(followups.map((item) => item.selectedContext)).toEqual(["2026-01", "2026-03"].map((frameKey) => ({ version: 1, sectionId: fixture.id, sectionRev: fixture.rev, viewId: "relationship", bindingId: "frame.fixture", datasetDigest: "a".repeat(64), frameKey, measureKeys: ["revenue", "people"] })));
    await seek.selectOption("2");
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 2 }), fixture);
    await expect(seek).toHaveValue("1");
    await page.screenshot({ path: test.info().outputPath(`prepared-frames-${width}.png`), fullPage: true });
    const revoked = structuredClone(fixture);
    const plan = evidencePlanForViewer(revoked.spec.presentation_plan)!;
    plan.dataset = { artifact_id: "frames.fixture", availability: "unavailable" };
    plan.bindings = { "frame.fixture": { kind: "frame", availability: "unavailable" } };
    await page.evaluate((value) => window.chartCompositionHost.update(value), revoked);
    await expect(player).toHaveCount(0);
    expect(writes).toEqual([]); expect(reads).toEqual([]); expect(errors).toEqual([]);
  });
  test(`native chart follow-up captures selected point and window at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    const writes: string[] = [], reads: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download|\/files\//.test(route.request().url())) reads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = timeWindowFixture();
    await page.evaluate((value) => {
      window.chartCompositionHost.hydrate(value);
      const host = window as unknown as { followups: unknown[] };
      host.followups = [];
      window.addEventListener("bowhead:chart-followup", (event) => host.followups.push((event as CustomEvent).detail));
    }, fixture);
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    await page.getByLabel("Start", { exact: true }).selectOption("1");
    await page.getByLabel("End", { exact: true }).selectOption("2");
    await page.locator('[data-chart-view="detail"]').getByRole("row").filter({ hasText: "2026-02" }).click();
    const trend = page.locator('[data-chart-view="relationship"]');
    await page.keyboard.press("Escape");
    await trend.hover();
    await trend.getByRole("button", { name: /More/ }).click();
    await page.getByRole("menuitem", { name: "Inspect", exact: true }).click();
    await page.getByRole("dialog").getByRole("group", { name: /Linked chart/ }).focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("[data-chart-observation-details]")).toHaveCount(0);
    await page.getByRole("button", { name: "Ask about this chart", exact: true }).click();
    const events = await page.evaluate(() => (window as unknown as { followups: Array<{ selectedContext: unknown }> }).followups);
    expect(events).toHaveLength(1);
    expect(events[0].selectedContext).toEqual({ version: 1, sectionId: fixture.id, sectionRev: fixture.rev, viewId: "relationship", bindingId: "time.fixture", datasetDigest: evidencePlanForViewer(fixture.spec.presentation_plan)!.dataset.content_digest, window: { lower: "2026-02", upper: "2026-03" }, tuples: [["2026-02"]], measureKeys: ["revenue", "cost"] });
    expect(writes).toEqual([]); expect(reads).toEqual([]); expect(errors).toEqual([]);
  });
  test(`native reading steps survive prose updates and restore one embedded figure at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const writes: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    await page.evaluate((value) => { window.chartCompositionHost.hydrate(value); window.chartCompositionHost.readOnly(true); window.chartCompositionHost.embedded(true); }, guidedReadingFixture());
    await expect(page.locator("[data-chart-composition]")).toHaveCount(1);
    const guide = page.locator("[data-chart-guide]");
    await expect(guide).toContainText("Step 1 of 3");
    await guide.getByRole("button", { name: "Next step", exact: true }).click();
    await expect(guide).toContainText("Step 2 of 3");
    await page.evaluate(() => window.chartCompositionHost.prose("Updated introduction"));
    await expect(page.getByText("Updated introduction", { exact: true })).toBeVisible();
    await expect(guide).toContainText("Step 2 of 3");
    expect(await page.evaluate(() => {
      const figure = document.querySelector("[data-chart-composition]")!;
      const prose = [...document.querySelectorAll("p")];
      const before = prose.find((node) => node.textContent === "Updated introduction")!;
      const after = prose.find((node) => node.textContent === "After the figure")!;
      return !!(before.compareDocumentPosition(figure) & Node.DOCUMENT_POSITION_FOLLOWING) && !!(figure.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING);
    })).toBe(true);
    await page.evaluate(() => window.chartCompositionHost.missing(true));
    await expect(page.locator("[data-chart-composition]")).toHaveCount(0);
    await page.evaluate(() => window.chartCompositionHost.missing(false));
    await expect(page.locator("[data-chart-composition]")).toHaveCount(1);
    await expect(guide).toContainText("Step 1 of 3");
    await expect(page.locator("[data-chart-view]")).toHaveCount(3);
    expect(writes).toEqual([]); expect(errors).toEqual([]);
  });
  test(`native reading steps restore canonical scope without saving at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [], reads: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download|\/files\//.test(route.request().url())) reads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = guidedReadingFixture(true);
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), fixture);
    await page.evaluate(() => window.chartCompositionHost.readOnly(true));
    const guide = page.locator("[data-chart-guide]");
    await expect(guide).toContainText("Step 1 of 3");
    await expect(page.locator('[data-chart-guide-active="true"]')).toHaveAttribute("data-chart-view", "relationship");
    await page.getByLabel("Start", { exact: true }).selectOption("2");
    await expect(page.locator('[data-chart-view="detail"] tbody tr')).toHaveCount(4);
    await guide.getByRole("button", { name: "Next step", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(guide).toContainText("Compare the full period");
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("0");
    await expect(page.locator('[data-chart-view="detail"] tbody tr')).toHaveCount(6);
    await expect(page.locator('[data-chart-guide-active="true"]')).toHaveAttribute("data-chart-view", "comparison");
    await guide.getByRole("button", { name: "Next step", exact: true }).click();
    await expect(guide).toContainText("Inspect the original values");
    await expect(guide.getByRole("button", { name: "Next step", exact: true })).toBeDisabled();
    await page.screenshot({ path: test.info().outputPath(`guided-reading-${width}.png`), fullPage: true });
    expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
    await expect(guide).toContainText("Step 1 of 3");
    await guide.getByRole("button", { name: "Next step", exact: true }).click();
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 2 }), fixture);
    await expect(guide).toContainText("Step 1 of 3");
    await guide.getByRole("button", { name: "Next step", exact: true }).click();
    await page.getByLabel("Start", { exact: true }).selectOption("2");
    await page.locator('[data-chart-view="detail"]').getByRole("row").filter({ hasText: "2026-04" }).click();
    await expect(page.locator("[data-chart-focus-status]")).toContainText("2026-04");
    const noteDenied = structuredClone(fixture);
    const notePlan = noteDenied.spec.presentation_plan as import("../lib/chart/presentation-plan").EvidencePlanV1;
    notePlan.evidence_notes = [{ id: "methodology", measure_keys: ["revenue"], source_ref: { id: "methodology.fixture", access_state: "linked_restricted" }, availability: "unavailable" }];
    notePlan.guide_steps!.forEach((step) => { step.title = ""; });
    noteDenied.spec.title = "";
    noteDenied.spec.views!.forEach((view) => { view.spec.title = ""; });
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 2 }), noteDenied);
    await expect(guide).toHaveCount(0);
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("2");
    await expect(page.locator('[data-chart-view="detail"] tbody tr')).toHaveCount(4);
    await expect(page.locator("[data-chart-focus-status]")).toContainText("2026-04");
    await expect(page.locator("[data-chart-composition]")).not.toContainText("Revenue includes returns.");
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 2 }), fixture);
    await expect(guide).toContainText("Step 1 of 3");
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("2");
    const denied = structuredClone(fixture);
    const plan = denied.spec.presentation_plan as { dataset: Record<string, unknown>; bindings: Record<string, unknown>; guide_steps: Array<{ title: string }> };
    plan.dataset = { artifact_id: "source_unavailable", availability: "unavailable" };
    plan.bindings = { "time.fixture": { kind: "time", availability: "unavailable" } };
    plan.guide_steps.forEach((step) => { step.title = ""; });
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 3 }), denied);
    await expect(guide).toHaveCount(0);
    await expect(page.locator("[data-chart-guide-summary]")).toHaveCount(0);
    await expect(page.locator("[data-chart-composition]")).not.toContainText("Inspect the original values");
    expect(writes).toEqual([]); expect(reads).toEqual([]); expect(errors).toEqual([]);
  });
  test(`native shared axes retain comparison through window and legend at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    const writes: string[] = [], reads: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download|\/files\//.test(route.request().url())) reads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), sharedScaleFixture());
    await page.evaluate(() => window.chartCompositionHost.readOnly(true));
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const expected = { min: -9.25, max: 84.25 };
    expect(await page.evaluate(() => window.chartCompositionHost.chartScale("relationship", "y"))).toEqual(expected);
    expect(await page.evaluate(() => window.chartCompositionHost.chartScale("comparison", "y"))).toEqual(expected);
    await page.getByLabel("Start", { exact: true }).selectOption("1");
    await page.getByLabel("End", { exact: true }).selectOption("2");
    await expect(page.locator('[data-chart-view="detail"] tbody tr')).toHaveCount(2);
    await page.evaluate(() => window.chartCompositionHost.settleCharts());
    expect(await page.evaluate(() => window.chartCompositionHost.chartScale("relationship", "y"))).toEqual(expected);
    const legend = await page.evaluate(() => window.chartCompositionHost.legendPosition("relationship", 0));
    expect(legend).not.toBeNull();
    const chart = page.locator('[data-chart-view="relationship"] canvas');
    await chart.click({ position: legend! });
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.seriesHidden("relationship", 0))).toBe(true);
    await page.evaluate(() => window.chartCompositionHost.settleCharts());
    expect(await page.evaluate(() => window.chartCompositionHost.chartScale("relationship", "y"))).toEqual(expected);
    expect(await page.evaluate(() => window.chartCompositionHost.chartScale("comparison", "y"))).toEqual(expected);
    await page.screenshot({ path: test.info().outputPath(`shared-scales-${width}.png`), fullPage: true });
    const primary = page.locator('[data-chart-view="relationship"]');
    await primary.getByRole("button", { name: "Line", exact: true }).click();
    await page.locator('[data-kind="bar"][role="menuitemradio"]').click();
    await expect(primary.locator("[data-chart-shared-scale-paused]")).toContainText("Shared scale is unavailable");
    expect(await page.evaluate(() => window.chartCompositionHost.chartScale("comparison", "y"))).toEqual(expected);
    expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
    await expect(primary.locator("[data-chart-shared-scale-paused]")).toHaveCount(0);
    await expect(page.locator('[data-chart-view="detail"] tbody tr')).toHaveCount(6);
    await expect(primary.locator("[data-chart-window-scope]")).toHaveCount(0);
    await page.evaluate(() => window.chartCompositionHost.settleCharts());
    expect(await page.evaluate(() => window.chartCompositionHost.chartScale("relationship", "y"))).toEqual(expected);
    expect(writes).toEqual([]); expect(reads).toEqual([]); expect(errors).toEqual([]);
  });
  test(`native time window preserves saved scope, downloads and reset at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    const writes: string[] = [], dataReads: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download|\/files\//.test(route.request().url())) dataReads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = timeWindowFixture();
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), fixture);
    await page.evaluate(() => window.chartCompositionHost.readOnly(true));
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const detail = page.locator('[data-chart-view="detail"]');
    await detail.getByRole("row").filter({ hasText: "2026-01" }).click();
    await expect(page.locator("[data-chart-focus-status]")).toContainText("2026-01");
    await page.keyboard.press("Escape");
    await page.getByLabel("Start", { exact: true }).selectOption("1");
    await page.getByLabel("End", { exact: true }).selectOption("2");
    await expect(page.locator("[data-chart-focus-status]")).toHaveCount(0);
    await expect(detail.locator("tbody tr")).toHaveCount(2);
    await expect(detail).toContainText("2026-02");
    await expect(detail).not.toContainText("52.12");
    await expect(page.locator('[data-chart-view="comparison"] [data-chart-window-scope]')).toHaveCount(0);
    expect(await page.evaluate(() => window.chartCompositionHost.chartLabels("comparison"))).toContain("2026-01");
    await expect(page.locator("[data-chart-window-scope]")).toHaveCount(2);
    await expect(page.locator("[data-chart-time-window]")).toContainText("2026-02–2026-03");
    await detail.getByRole("row").filter({ hasText: "2026-02" }).click();
    await page.keyboard.press("Escape");
    await expect(page.locator("[data-chart-focus-status]")).toContainText("2026-02");
    await detail.getByRole("button", { name: /More/ }).click();
    const downloaded = page.waitForEvent("download");
    await page.getByRole("menuitem", { name: /CSV/ }).click();
    const csv = await downloaded;
    expect(csv.suggestedFilename()).toContain("2026-02_2026-03");
    const content = await readFile((await csv.path())!, "utf8");
    expect(content).toContain("2026-02,-5,12");
    expect(content).toContain("2026-03,,9");
    expect(content).not.toContain("2026-01");
    const trend = page.locator('[data-chart-view="relationship"]');
    await trend.hover();
    await trend.getByRole("button", { name: /More/ }).click();
    const pngDownload = page.waitForEvent("download");
    await page.getByRole("menuitem", { name: /PNG/ }).click();
    const png = await pngDownload;
    expect(png.suggestedFilename()).toContain("2026-02_2026-03");
    await png.saveAs(test.info().outputPath("time-window-download.png"));
    const bytes = [...await readFile((await png.path())!)];
    const scopePixels = await page.evaluate(async (data) => {
      const bitmap = await createImageBitmap(new Blob([new Uint8Array(data)], { type: "image/png" }));
      const canvas = document.createElement("canvas"); canvas.width = bitmap.width; canvas.height = 36;
      const context = canvas.getContext("2d")!; context.drawImage(bitmap, 0, 0);
      const header = context.getImageData(0, 0, canvas.width, 36).data;
      return { height: bitmap.height, ink: Array.from(header).filter((value, index) => index % 4 !== 3 && value < 150).length };
    }, bytes);
    expect(scopePixels.ink).toBeGreaterThan(0);
    expect(scopePixels.height).toBe(await trend.locator("canvas").evaluate((canvas: HTMLCanvasElement) => canvas.height + 36));
    await page.screenshot({ path: test.info().outputPath(`time-window-${width}.png`), fullPage: true });
    await page.getByRole("button", { name: "Show full period", exact: true }).click();
    await expect(detail.locator("tbody tr")).toHaveCount(6);
    await expect(detail).toContainText("52.12");
    await page.getByLabel("Start", { exact: true }).selectOption("4");
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 2 }), fixture);
    await expect(page.getByLabel("Start", { exact: true })).toHaveValue("0");
    await expect(detail.locator("tbody tr")).toHaveCount(6);
    await page.getByLabel("Start", { exact: true }).selectOption("4");
    const denied = structuredClone(fixture);
    const plan = denied.spec.presentation_plan as { dataset: Record<string, unknown>; bindings: Record<string, unknown> };
    plan.dataset = { artifact_id: "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", availability: "unavailable" };
    plan.bindings = { "time.fixture": { kind: "time", availability: "unavailable" } };
    await page.evaluate((value) => window.chartCompositionHost.update(value), denied);
    await expect(page.locator("[data-chart-time-window]")).toHaveCount(0);
    await expect(detail.locator("tbody tr")).toHaveCount(6);
    await page.waitForTimeout(1800);
    expect(writes).toEqual([]);
    expect(dataReads).toEqual([]);
    expect(errors).toEqual([]);
  });
}

test.describe("native linked touch exploration", () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 1100 }, colorScheme: "dark" });
  test("tap and native reset preserve saved scope in dark reduced-motion Canvas", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH" || /\/preview|\/download/.test(route.request().url())) writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), linkedFocusFixture());
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const point = await page.evaluate(() => window.chartCompositionHost.firstMarkPosition("relationship"));
    expect(point).not.toBeNull();
    await page.locator('[data-chart-view="relationship"] canvas').tap({ position: point! });
    await expect(page.locator('[data-chart-view="detail"] tr[aria-selected="true"]')).toContainText("East");
    await expect(page.locator("[data-chart-observation-details]")).toContainText("52.12");
    await page.getByRole("dialog").getByRole("button", { name: "Close", exact: true }).tap();
    await expect(page.locator("[data-chart-observation-details]")).toHaveCount(0);
    await page.getByRole("button", { name: "Clear selection", exact: true }).tap();
    await expect(page.locator("[data-chart-focus-status]")).toHaveCount(0);
    await page.locator('[data-chart-view="detail"]').getByRole("row").filter({ hasText: "South" }).tap();
    await expect(page.locator("[data-chart-focus-status]")).toContainText("South");
    await page.screenshot({ path: test.info().outputPath("linked-focus-dark-touch.png"), fullPage: true });
    await page.waitForTimeout(1800);
    expect(writes).toEqual([]);
    expect(errors).toEqual([]);
  });
});

for (const width of [1000, 390]) {
  test(`native linked charts use typed local focus without saves or data requests at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    const errors: string[] = [], writes: string[] = [], dataReads: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      if (/\/preview|\/download/.test(route.request().url())) dataReads.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture = linkedFocusFixture();
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), fixture);
    await page.evaluate(() => window.chartCompositionHost.readOnly(true));
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const readPixels = () => page.evaluate(() => Array.from(document.querySelectorAll<HTMLCanvasElement>("[data-chart-view] canvas")).map((canvas) => canvas.toDataURL()));
    const initialPixels = await readPixels();
    const detail = page.locator('[data-chart-view="detail"]');
    const position = await page.evaluate(() => window.chartCompositionHost.firstMarkPosition("relationship"));
    expect(position).not.toBeNull();
    await page.locator('[data-chart-view="relationship"] canvas').click({ position: position! });
    await expect(page.locator("[data-chart-focus-status]")).toContainText("Selected: East");
    await expect(detail.locator('tr[aria-selected="true"]')).toContainText("52.12");
    await expect(page.locator('[data-chart-view="relationship"] canvas')).toHaveCount(1);
    await expect.poll(async () => (await readPixels()).map((pixels, index) => pixels !== initialPixels[index])).toEqual([true, true]);
    await page.keyboard.press("Escape");
    const plot = page.locator('[data-chart-view="relationship"]').getByRole("group", { name: /Linked chart/ });
    await plot.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.locator("[data-chart-view=\"relationship\"] .chart-keyboard-hint")).toBeVisible();
    await expect(page.locator("[data-chart-focus-status]")).toContainText("Selected: West");
    await expect(detail.locator('tr[aria-selected="true"]')).toContainText("West");
    const mark = await page.evaluate(() => window.chartCompositionHost.firstMarkPosition("relationship"));
    await page.locator('[data-chart-view="relationship"] canvas').hover({ position: mark! });
    await page.screenshot({ path: test.info().outputPath(`linked-focus-${width}.png`), fullPage: true });
    await page.keyboard.press("Escape");
    await expect(page.locator("[data-chart-focus-status]")).toHaveCount(0);
    // A same-payload section revision clears browsing state through the existing NodeView.
    await detail.getByRole("row").filter({ hasText: "South" }).click();
    await page.evaluate((value) => window.chartCompositionHost.update({ ...value, rev: 2 }), fixture);
    await expect(page.locator("[data-chart-focus-status]")).toHaveCount(0);
    await detail.getByRole("row").filter({ hasText: "East" }).click();
    const denied = structuredClone(fixture);
    const plan = denied.spec.presentation_plan as NonNullable<typeof fixture.spec.presentation_plan> & { bindings: Record<string, unknown>; dataset: Record<string, unknown> };
    plan.dataset = { artifact_id: "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", availability: "unavailable" };
    plan.bindings = { "entity.fixture": { kind: "entity", availability: "unavailable" } };
    denied.spec.title = "";
    denied.spec.views!.forEach((view) => { view.spec.title = ""; });
    await page.evaluate((value) => window.chartCompositionHost.update(value), denied);
    await expect(page.locator("[data-chart-focus-status]")).toHaveCount(0);
    await expect(detail.locator('tr[aria-selected="true"]')).toHaveCount(0);
    await expect(detail).toContainText("52.12");
    await page.waitForTimeout(1800);
    expect(writes).toEqual([]);
    expect(dataReads).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const width of [1000, 390]) {
  test(`native Canvas child settings save and reload at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let saved: ReportSection | null = null;
    const patches: Array<{ patch: { spec: unknown }; expected_rev: number }> = [];
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH" && route.request().url().endsWith("/sections/sec_composition")) {
        const body = route.request().postDataJSON();
        patches.push(body);
        saved = { id: "sec_composition", kind: "chart", spec: body.patch.spec, rev: body.expected_rev + 1 };
        await route.fulfill({ contentType: "application/json", body: JSON.stringify({ section: saved, rev: saved.rev }) });
      } else await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    // The fixture injects component CSS; compile the actual Tailwind container utilities too.
    await page.addStyleTag({ content: await nativeStyles });
    await expect(page.locator('[data-chart-view="details"]')).toBeVisible();
    await page.getByTitle(/Chart settings \(/).click();
    const dialog = page.getByRole("dialog", { name: "Chart settings" });
    await dialog.getByRole("combobox", { name: "View", exact: true }).selectOption("details");
    await dialog.getByRole("textbox", { name: "View title", exact: true }).fill("Original revenue values");
    await dialog.getByRole("combobox", { name: "Type", exact: true }).selectOption("kpi");
    await dialog.getByRole("button", { name: "Save", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect.poll(() => patches.length).toBe(1);
    expect(patches[0]!.expected_rev).toBe(1);
    const persisted = saved as Extract<ReportSection, { kind: "chart" }> | null;
    expect(persisted?.spec.views?.map((view) => [view.id, view.spec.kind])).toEqual([
      ["comparison", "table"], ["details", "kpi"],
    ]);
    expect(persisted?.spec.views?.[1]?.spec.title).toBe("Original revenue values");
    expect(persisted?.spec.views?.[1]?.spec.columns[1]?.display).toEqual({ decimal_places: 3, decimal_mode: "fixed" });
    expect(persisted?.spec.views?.[1]?.spec.columns[1]?.currency_code).toBe("USD");
    await page.evaluate((section) => window.chartCompositionHost.hydrate(section!), saved);
    await expect(page.locator('[data-chart-view="details"]').getByRole("button", { name: "KPI", exact: true })).toBeVisible();
    await expect(page.locator('[data-chart-view="comparison"]').getByRole("button", { name: "Table", exact: true })).toBeVisible();
    await expect(page.locator('[data-chart-view="comparison"]')).toContainText("12.000");
    const comparison = await page.locator('[data-chart-view="comparison"]').boundingBox();
    const details = await page.locator('[data-chart-view="details"]').boundingBox();
    expect(comparison).not.toBeNull();
    expect(details).not.toBeNull();
    if (width > 760) {
      expect(Math.abs(comparison!.y - details!.y)).toBeLessThan(2);
      expect(details!.x).toBeGreaterThan(comparison!.x + comparison!.width);
    } else {
      expect(details!.y).toBeGreaterThanOrEqual(comparison!.y + comparison!.height);
    }
    await expect(page.locator('[data-section-id="sec_composition"]')).toHaveCount(1);
    await page.getByTitle(/Chart settings \(/).click();
    await dialog.getByRole("combobox", { name: "View", exact: true }).selectOption("");
    await dialog.getByRole("combobox", { name: "Layout", exact: true }).selectOption("stack");
    await dialog.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => patches.length).toBe(2);
    expect(patches[1]!.expected_rev).toBe(2);
    expect((saved as Extract<ReportSection, { kind: "chart" }> | null)?.spec.views).toEqual(persisted?.spec.views);
    await page.evaluate((section) => window.chartCompositionHost.hydrate(section!), saved);
    await expect(page.locator('[data-chart-group="stack"]')).toBeVisible();
    await expect(page.locator('[data-chart-view="comparison"]')).toBeVisible();
    await expect(page.locator('[data-chart-view="comparison"]')).toBeVisible();
    await expect(page.locator('[data-chart-view="details"]')).toBeVisible();
    const stackedFirst = await page.locator('[data-chart-view="comparison"]').boundingBox();
    const stackedSecond = await page.locator('[data-chart-view="details"]').boundingBox();
    expect(stackedSecond!.y).toBeGreaterThanOrEqual(stackedFirst!.y + stackedFirst!.height);
    await page.evaluate(() => window.chartCompositionHost.readOnly(true));
    await expect(page.getByTitle(/Chart settings \(/)).toHaveCount(0);
    expect(patches).toHaveLength(2);
    expect(errors).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`composition-${width}.png`), fullPage: true });
  });
}

for (const width of [1000, 390]) {
  test(`native chart source notes and denied probes do not save at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const patches: unknown[] = [];
    let deny = false;
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") patches.push(route.request().postDataJSON());
      const deniedNote = deny && route.request().url().includes("/note/preview");
      await route.fulfill({ status: deniedNote ? 403 : 200, contentType: "application/json", body: deniedNote ? '{}' : JSON.stringify({ type: "text", content: "Definition\nRevenue includes returns." }) });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const dataAlias = "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";
    const noteAlias = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    const source = { id: dataAlias, source_alias: dataAlias, access_state: "linked_accessible" as const, safe_label: "Regional data", safe_preview_url: "/api/v1/report-artifacts/data/preview", safe_url: "/api/v1/report-artifacts/data/download" };
    const spec = {
      kind: "table" as const, title: "Regional results", row_count: 2, data_artifact_id: "art_composition",
      columns: [{ key: "region", header: "Region", type: "categorical" as const }, { key: "value", header: "Value", type: "numeric" as const, unit: "USD" }],
      rows: [{ region: "A", value: 12 }, { region: "B", value: 21 }], source_meta: source,
      presentation_plan: {
        version: 1 as const, dataset: { artifact_id: dataAlias, availability: "available" as const, content_digest: "a".repeat(64), schema_digest: "b".repeat(64) },
        evidence_notes: [{ id: "definition", measure_keys: ["value"], excerpt: "Revenue includes returns.", locator: "Definition", verified_content_digest: "c".repeat(64), availability: "available" as const,
          source_ref: { id: noteAlias, source_alias: noteAlias, access_state: "linked_accessible" as const, safe_label: "Definition", safe_preview_url: "/api/v1/report-artifacts/note/preview", safe_url: "/api/v1/report-artifacts/note/download" } }],
        text_dependencies: { "/title": { note_ids: ["definition"], dataset: true } },
      },
    };
    const section: ReportSection = { id: "sec_composition", kind: "chart", rev: 1, spec };
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), section);
    await page.locator(".rc-source").click();
    await expect(page.getByText("Sources and definitions", { exact: true })).toBeVisible();
    await expect(page.locator('[data-evidence-note="definition"]').getByText("Revenue includes returns.", { exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath(`source-notes-${width}.png`), fullPage: true });
    await page.keyboard.press("Escape");
    // Remount resets the source registry; a definitive denied probe must redact locally.
    deny = true;
    await page.evaluate((value) => window.chartCompositionHost.hydrate(value), section);
    await page.locator(".rc-source").click();
    await expect(page.getByText("Revenue includes returns.", { exact: true })).toHaveCount(0);
    await expect(page.locator("[data-chart-plan-unavailable]")).toBeVisible();
    await expect(page.locator('[data-evidence-note="definition"]')).toContainText("The original note has changed or is currently unavailable.");
    await expect(page.locator(".v2-chart-table-el")).toContainText("12");
    await page.waitForTimeout(1800);
    expect(patches).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const appearance of [
  { locale: "en-US", mode: "light", contrast: "standard" },
  { locale: "en-US", mode: "dark", contrast: "high" },
  { locale: "zh-CN", mode: "light", contrast: "high" },
  { locale: "zh-CN", mode: "dark", contrast: "standard" },
] as const) {
  test(`native Canvas width matrix preserves readable figures in ${appearance.locale} ${appearance.mode} ${appearance.contrast}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1200 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    await page.evaluate(({ fixture, appearance }) => {
      window.chartCompositionHost.hydrate(fixture);
      window.chartCompositionHost.configure(appearance);
      window.chartCompositionHost.embedded(true);
      window.chartCompositionHost.readOnly(true);
    }, { fixture: observationAdaptersFixture(), appearance });
    for (const width of [320, 390, 600, 759, 760, 840, 1180]) {
      await page.evaluate((width) => window.chartCompositionHost.configure({ width }), width);
      const root = page.locator("[data-chart-composition]");
      await expect(root).toHaveCount(1);
      await expect(root.locator("[data-chart-view]")).toHaveCount(3);
      await expect(page.locator(".rc-source")).toHaveCount(1);
      await expect(page.getByText("Regional observations", { exact: true })).toHaveCount(1);
      await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
      await expect.poll(() => root.evaluate((element) => {
        const frame = element.getBoundingClientRect();
        return [...element.querySelectorAll("canvas")].every((canvas) => {
          const box = canvas.getBoundingClientRect();
          return box.width > 0 && box.left >= frame.left - 1 && box.right <= frame.right + 1;
        });
      })).toBe(true);
      const table = root.locator(".v2-chart-table");
      await expect(table).toHaveCSS("border-top-width", "0px");
      await expect(table).toHaveCSS("border-radius", "0px");
      await expect(root.locator('[data-chart-view="detail"]')).toContainText("0.000012345");
      const selects = root.locator("[data-chart-time-window] select");
      await selects.first().selectOption("1");
      await expect(root.locator("tbody tr")).toHaveCount(2);
      await selects.first().selectOption("0");
      await expect(root.locator("tbody tr")).toHaveCount(4);
    }
    await page.setViewportSize({ width: 320, height: 700 });
    await page.evaluate(() => {
      window.chartCompositionHost.embedded(false);
      window.chartCompositionHost.readOnly(false);
      window.chartCompositionHost.configure({ width: 320 });
    });
    await page.getByTitle(/Chart settings|图表设置/).click();
    const dialog = page.getByTestId("chart-settings-modal");
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(320);
    expect(box!.height).toBeLessThanOrEqual(700);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(writes).toEqual([]); expect(errors).toEqual([]);
  });
}

test("same-label Canvas compositions isolate focus, windows and source lifecycle", async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 1200 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const writes: string[] = [], errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/v1/**", async (route) => {
    if (route.request().method() === "PATCH") writes.push(route.request().url());
    await route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
  await page.goto("/__reference_component_test");
  await page.addStyleTag({ content: await nativeStyles });
  const first = observationAdaptersFixture(), second = { ...observationAdaptersFixture(), id: "sec_second" };
  await page.evaluate(({ first, second }) => { window.chartCompositionHost.hydrate(first); window.chartCompositionHost.companions([second]); }, { first, second });
  const roots = page.locator("[data-chart-composition]"), a = roots.nth(0), b = roots.nth(1);
  await expect(roots).toHaveCount(2);
  await a.locator('tbody tr').filter({ hasText: "East" }).first().click();
  await expect(a.locator('tr[aria-selected="true"]')).toHaveCount(2);
  await expect(b.locator('tr[aria-selected="true"]')).toHaveCount(0);
  await a.locator('[data-chart-time-window] select').first().selectOption("1");
  await expect(a.locator("tbody tr")).toHaveCount(2);
  await expect(b.locator("tbody tr")).toHaveCount(4);
  await b.locator("tbody tr").filter({ hasText: "West" }).last().click();
  await expect(b.locator('tr[aria-selected="true"]')).toHaveCount(2);
  await page.evaluate((first) => window.chartCompositionHost.update({ ...first, rev: 2 }), first);
  await expect(a.locator('[data-chart-time-window] select').first()).toHaveValue("0");
  await expect(a.locator('tr[aria-selected="true"]')).toHaveCount(0);
  await expect(b.locator('tr[aria-selected="true"]')).toHaveCount(2);
  await page.evaluate(() => window.chartCompositionHost.missing(true));
  await expect(roots).toHaveCount(1);
  await expect(roots.locator('tr[aria-selected="true"]')).toHaveCount(2);
  await page.evaluate(() => window.chartCompositionHost.missing(false));
  await expect(roots).toHaveCount(2);
  await expect(a.locator('tr[aria-selected="true"]')).toHaveCount(0);
  await expect(b.locator("tbody tr")).toHaveCount(4);
  await expect(b.locator('[data-chart-time-window] select').first()).toHaveValue("0");
  await b.locator("tbody tr").filter({ hasText: "West" }).last().click();
  await expect(b.locator('tr[aria-selected="true"]')).toHaveCount(2);
  expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
  await expect(roots.locator('tr[aria-selected="true"]')).toHaveCount(0);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test("native local focus reports cold render and p95 feedback for 1000 original rows", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 1100 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const writes: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    if (route.request().method() === "PATCH") writes.push(route.request().url());
    await route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
  await page.goto("/__reference_component_test");
  await page.addStyleTag({ content: await nativeStyles });
  const fixture = observationAdaptersFixture();
  const rows = Array.from({ length: 1000 }, (_, index) => ({ region: index % 2 ? "West" : "East", month: `${2020 + Math.floor(index / 24)}-${String(Math.floor(index / 2) % 12 + 1).padStart(2, "0")}`, revenue: index / 1e7, cost: index / 50 }));
  const ids = ["trend", "detail"];
  fixture.spec.views = fixture.spec.views!.filter((view) => ids.includes(view.id));
  fixture.spec.views.forEach((view) => { view.spec.rows = rows; view.spec.row_count = rows.length; });
  fixture.spec.rows = rows; fixture.spec.row_count = rows.length;
  fixture.spec.layout = { type: "stack", children: ids.map((view) => ({ view })) };
  const plan = fixture.spec.presentation_plan as import("../lib/chart/presentation-plan").EvidencePlanV1;
  const binding = plan.bindings!["time.fixture"] as import("../lib/chart/linked-focus").FocusBinding;
  binding.eligible_views = ids;
  binding.coverage = { artifact_rows: { available: 1000, total: 1000 }, views: Object.fromEntries(ids.map((id) => [id, { retained_rows: 1000 }])) };
  plan.view_bindings = Object.fromEntries(ids.map((id) => [id, "time.fixture"]));
  plan.interactions = [{ preset: "linked_focus", from_view: "trend", to_views: ["detail"], binding_id: "time.fixture" }, { preset: "linked_focus", from_view: "detail", to_views: ["trend"], binding_id: "time.fixture" }, { preset: "time_window", view_ids: ids, binding_id: "time.fixture" }];
  const cold = await page.evaluate(async (fixture) => {
    const start = performance.now();
    window.chartCompositionHost.hydrate(fixture);
    for (let index = 0; index < 300; index++) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      if (document.querySelectorAll('[data-chart-view="detail"] tbody tr').length === 10 && window.chartCompositionHost.chartLabels("trend").length === 500) return performance.now() - start;
    }
    throw new Error("Native cold render did not become ready");
  }, fixture);
  await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(1);
  const samples = await page.evaluate(async () => {
    const samples: number[] = [];
    for (let index = 0; index < 40; index++) {
      const row = document.querySelectorAll<HTMLElement>('[data-chart-view="detail"] tbody tr')[(index % 5) * 2]!;
      const start = performance.now();
      row.click();
      for (let frame = 0; frame < 100; frame++) {
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        if (document.querySelectorAll('[data-chart-view="detail"] tr[aria-selected="true"]').length === 2) {
          await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
          samples.push(performance.now() - start);
          break;
        }
      }
    }
    return samples;
  });
  expect(samples).toHaveLength(40);
  const p95 = [...samples].sort((a, b) => a - b)[Math.ceil(samples.length * 0.95) - 1]!;
  const performanceEvidence = JSON.stringify({ cold_ms: cold, p95_ms: p95, samples_ms: samples, browser: test.info().project.name, user_agent: await page.evaluate(() => navigator.userAgent), device: { os: platform(), release: release(), cpu: cpus()[0]?.model, cores: cpus().length, memory_bytes: totalmem() }, available_rows: 1000, plotted_periods: 500, motion: "reduced", scope: "Table click to selected rows and next painted frame; no network completion" }, null, 2);
  const performancePath = test.info().outputPath("performance.json");
  await writeFile(performancePath, performanceEvidence);
  await test.info().attach("native-chart-performance", { contentType: "application/json", path: performancePath });
  process.stdout.write(`Native performance ${test.info().project.name}: cold=${cold.toFixed(1)}ms, p95=${p95.toFixed(1)}ms\n`);
  expect(p95).toBeLessThanOrEqual(200);
  expect(writes).toEqual([]);
});

test("replacing a same-title same-source root cannot inherit hidden native series", async ({ page }) => {
  const writes: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    if (route.request().method() === "PATCH") writes.push(route.request().url());
    await route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
  await page.goto("/__reference_component_test");
  await page.addStyleTag({ content: await nativeStyles });
  const fixture = observationAdaptersFixture();
  await page.evaluate((value) => window.chartCompositionHost.hydrate(value), fixture);
  await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
  const legend = await page.evaluate(() => window.chartCompositionHost.legendPosition("trend", 0));
  expect(legend).not.toBeNull();
  await page.locator('[data-chart-view="trend"] canvas').click({ position: legend! });
  await expect.poll(() => page.evaluate(() => window.chartCompositionHost.seriesHidden("trend", 0))).toBe(true);
  await page.evaluate((value) => window.chartCompositionHost.update({ ...value, id: "sec_replacement" }), fixture);
  await expect(page.locator('[data-section-id="sec_replacement"]')).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.chartCompositionHost.seriesHidden("trend", 0))).toBe(false);
  expect(writes).toEqual([]);
});

test("six native views report prepared-frame feedback for 120 frames and 1000 original rows", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 1400 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const writes: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    if (route.request().method() === "PATCH") writes.push(route.request().url());
    await route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
  await page.goto("/__reference_component_test");
  await page.addStyleTag({ content: await nativeStyles });
  const fixture = preparedFramesFixture();
  const frames = Array.from({ length: 120 }, (_, index) => `${2020 + Math.floor(index / 12)}-${String(index % 12 + 1).padStart(2, "0")}`);
  const entities = Array.from({ length: 9 }, (_, index) => `Region ${index + 1}`);
  const rows = frames.flatMap((month, frame) => entities.slice(0, frame < 40 ? 9 : 8).map((region, entity) => ({ month, region, revenue: frame + entity / 100000, people: entity + 1 })));
  const ids = ["plot_a", "plot_b", "plot_c", "plot_d", "plot_e", "detail"];
  const base = fixture.spec.views![0]!.spec;
  fixture.spec.rows = rows; fixture.spec.row_count = rows.length;
  fixture.spec.views = ids.map((id) => ({ id, kind: "chart", spec: { ...base, kind: id === "detail" ? "table" : "scatter", title: id === "detail" ? "Original frame observations" : "Revenue and people", rows, row_count: rows.length } }));
  fixture.spec.layout = { type: "grid2", children: ids.map((view) => ({ view })) };
  const plan = fixture.spec.presentation_plan as import("../lib/chart/presentation-plan").EvidencePlanV1;
  const binding = plan.bindings!["frame.fixture"] as import("../lib/chart/prepared-frames").FrameBinding;
  binding.frame_keys = frames; binding.entity_values = entities; binding.eligible_views = ids;
  binding.axis_domains = Object.fromEntries(ids.map((id) => [id, id === "detail" ? null : { encoding: "scatter" as const, domains: { x: [-1, 121] as [number, number], y: [0, 10] as [number, number] } }]));
  binding.coverage = { artifact_rows: { available: 1000, total: 1000 }, views: Object.fromEntries(ids.map((id) => [id, { retained_rows: 1000 }])) };
  plan.view_bindings = Object.fromEntries(ids.map((id) => [id, "frame.fixture"]));
  plan.text_dependencies = {};
  plan.interactions = [{ preset: "frame_player", view_ids: ids, binding_id: "frame.fixture", static_frame: frames[119]! }];
  await page.evaluate((fixture) => window.chartCompositionHost.hydrate(fixture), fixture);
  await expect(page.locator('[data-chart-view="detail"] tbody tr')).toHaveCount(8);
  await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(5);
  const samples = await page.evaluate(async (frames) => {
    const results: number[] = [];
    const select = document.querySelector<HTMLSelectElement>('[data-chart-frame-player] select')!;
    for (let index = 0; index < 40; index++) {
      const frame = index * 3, start = performance.now();
      select.value = String(frame); select.dispatchEvent(new Event("change", { bubbles: true }));
      for (let tick = 0; tick < 100; tick++) {
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        const first = document.querySelector('[data-chart-view="detail"] tbody tr');
        if (first?.textContent?.includes(frames[frame]!)) {
          await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
          results.push(performance.now() - start); break;
        }
      }
    }
    return results;
  }, frames);
  expect(samples).toHaveLength(40);
  const p95 = [...samples].sort((a, b) => a - b)[37]!;
  const path = test.info().outputPath("six-view-performance.json");
  await writeFile(path, JSON.stringify({ p95_ms: p95, samples_ms: samples, views: 6, frames: 120, available_rows: 1000, browser: test.info().project.name, device: { os: platform(), cpu: cpus()[0]?.model, cores: cpus().length, memory_bytes: totalmem() }, scope: "Seek to original frame rows and next painted frame; no network completion", motion: "reduced" }, null, 2));
  await test.info().attach("native-six-view-performance", { contentType: "application/json", path });
  process.stdout.write(`Native six-view performance ${test.info().project.name}: p95=${p95.toFixed(1)}ms\n`);
  expect(p95).toBeLessThanOrEqual(200);
  expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
  await expect(page.locator('[data-chart-view="detail"]')).toContainText(frames[119]!);
  expect(writes).toEqual([]);
});


for (const locale of ["zh-CN", "en-US", "de-DE", "fr-FR"] as const) for (const width of [1000, 320]) {
  test(`readable magnitudes and base-value hover in ${locale} at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1100 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const writes: string[] = [], errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() === "PATCH") writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const child = { kind: "hbar" as const, title: "Prepared changes", row_count: 3, show_data_labels: true,
      columns: [{ key: "month", header: "Month", type: "categorical" as const }, { key: "amount", header: "Revenue_亿元", type: "numeric" as const, unit: "亿元" }],
      rows: [{ month: "A", amount: 0.0009, growth: 0.123456 }, { month: "B", amount: 0.0000078, growth: -0.012345 }, { month: "C", amount: -0.00000025, growth: null }],
    };
    const fixture: ReportSection = { id: "sec_values", rev: 1, kind: "chart", spec: { ...child,
      title: "Readable values", views: [{ id: "loss", kind: "chart", spec: child },
        { id: "dual", kind: "chart", spec: { ...child, kind: "dual_axis", title: "Amounts and growth",
          columns: [...child.columns, { key: "growth", header: "Growth", type: "numeric", format: "percent", unit: "%", percent_scale: "fraction" }],
        } }], layout: { type: "stack", children: [{ view: "loss" }, { view: "dual" }] },
    } };
    await page.evaluate(({ fixture, locale, width }) => { window.chartCompositionHost.configure({ locale, width }); window.chartCompositionHost.hydrate(fixture); }, { fixture, locale, width });
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(2);
    const canvas = page.locator('[data-chart-view="loss"] canvas');
    await canvas.scrollIntoViewIfNeeded();
    const mark = await page.evaluate(() => window.chartCompositionHost.firstMarkPosition("loss"));
    expect(mark).not.toBeNull();
    await canvas.hover({ position: mark! });
    const amount = new Intl.NumberFormat(locale).format(90000);
    await expect.poll(() => page.evaluate(() => window.chartCompositionHost.tooltipLines("loss"))).toEqual([`Revenue: ${amount}元`]);
    const ticks = await page.evaluate(() => window.chartCompositionHost.numericTickBounds("dual"));
    expect(ticks.length).toBeGreaterThan(0);
    for (const tick of ticks) { expect(tick.left).toBeGreaterThanOrEqual(0); expect(tick.right).toBeLessThanOrEqual(tick.width); }
    await page.screenshot({ path: test.info().outputPath(`value-magnitude-${locale}-${width}.png`), fullPage: true });
    expect(writes).toEqual([]); expect(errors).toEqual([]);
  });
}

for (const width of [390, 1000]) {
  test(`table unit disclosure stays coherent through locale changes at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const writes: string[] = [];
    await page.route("**/api/v1/**", async (route) => {
      if (route.request().method() !== "GET") writes.push(route.request().url());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await nativeStyles });
    const fixture: ReportSection = { id: "sec_units", kind: "chart", rev: 1, spec: {
      kind: "table", title: "Revenue (万元)", subtitle: "单位：万元；synthetic values", row_count: 3,
      columns: [{ key: "region", header: "Region", type: "categorical" }, { key: "revenue", header: "Revenue", type: "numeric", unit: "万元" }],
      rows: [{ region: "West", revenue: 120.5 }, { region: "East", revenue: 87.25 }, { region: "North", revenue: 0 }],
    } };
    await page.evaluate(({ fixture, width }) => {
      window.chartCompositionHost.configure({ width: width - 24, locale: "en-US" });
      window.chartCompositionHost.readOnly(true); window.chartCompositionHost.embedded(true); window.chartCompositionHost.hydrate(fixture);
    }, { fixture, width });
    for (const locale of ["en-US", "zh-CN", "de-DE", "fr-FR"] as const) {
      await page.evaluate((locale) => window.chartCompositionHost.configure({ locale }), locale);
      const table = page.locator(".v2-chart-table");
      await expect(table.getByRole("columnheader", { name: "Revenue (元)" })).toBeVisible();
      await expect(table.getByRole("cell", { name: new Intl.NumberFormat(locale).format(1205000), exact: true })).toBeVisible();
      await expect(table.getByRole("row")).toHaveCount(4);
      await expect(table.locator("[data-chart-unit-note]")).toContainText("万元");
      await expect(table.locator(".v2-chart-subtitle")).not.toHaveText("单位：万元；synthetic values");
      const geometry = await table.evaluate((node) => ({ left: node.getBoundingClientRect().left, right: node.getBoundingClientRect().right, noteTop: node.querySelector("[data-chart-unit-note]")!.getBoundingClientRect().top, tableTop: node.querySelector("table")!.getBoundingClientRect().top }));
      expect(geometry.left).toBeGreaterThanOrEqual(0); expect(geometry.right).toBeLessThanOrEqual(width);
      expect(geometry.noteTop).toBeLessThan(geometry.tableTop);
    }
    expect(writes).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`table-units-${width}.png`), fullPage: true });
  });
}
