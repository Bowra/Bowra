import { expect, test } from "@playwright/test";
import { installReferenceComponent } from "./_composer-reference-component-modules";
import type {} from "./_chart-composition-edit-entry";
import type { ReportSection } from "../lib/types";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import postcss from "postcss";
import tailwindcss from "@tailwindcss/postcss";
import { readFileSync } from "node:fs";

const fixtures: Record<string, { section: ReportSection }> = JSON.parse(readFileSync(resolve(process.cwd(), "../../config/chart_style/analytical_fixtures.json"), "utf8"));

const styles = readFile(resolve(process.cwd(), "app/globals.css"), "utf8").then((source) => postcss([tailwindcss()]).process(source, { from: resolve(process.cwd(), "app/globals.css") })).then((result) => result.css);
for (const locale of ["en-US", "zh-CN", "de-DE", "fr-FR"] as const) for (const width of [320, 390, 600, 760, 1180]) {
  test(`analytical figures retain native marks and original details at ${width}px ${locale}`, async ({ page }) => {
    test.setTimeout(120_000);
    const errors: string[] = [], writes: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewportSize({ width, height: 1100 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.route("**/api/v1/**", async (route) => { if (route.request().method() !== "GET" && !route.request().url().endsWith("/metrics/event")) writes.push(route.request().url()); await route.fulfill({ contentType: "application/json", body: "{}" }); });
    await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
    await page.goto("/__reference_component_test");
    await page.addStyleTag({ content: await styles });
    await page.evaluate(({ width, locale }) => window.chartCompositionHost.configure({ width, locale, mode: width < 400 ? "dark" : "light", contrast: locale === "de-DE" ? "high" : "standard" }), { width, locale });
    for (const name of ["scenario_rows", "benchmark_rows", "aligned_cycles", "scenario_facets"] as const) {
      await page.evaluate((section) => window.chartCompositionHost.hydrate(section as unknown as ReportSection), fixtures[name].section);
      const root = page.locator('[data-chart-view="comparison"]');
      if (name.endsWith("rows")) {
        await expect(root.locator('[data-chart-interval="row"]')).toHaveCount(10);
        const row = root.locator('[data-chart-interval="row"]').first().locator("xpath=ancestor::tr");
        await row.click();
        await expect(page.locator("[data-chart-observation-details]")).toBeVisible();
        await expect(page.getByRole("heading", { name: "City 01", exact: true })).toBeVisible();
        await page.keyboard.press("Escape");
        const button = root.locator(".v2-chart-table-footer button");
        await button.click();
        await expect(root.locator('[data-chart-interval="row"]')).toHaveCount(29);
        await expect(root.locator("[data-chart-interval-group]")).toHaveCount(3);
        await root.getByRole("columnheader", { name: "Price (CNY)", exact: true }).click();
        await expect(root.locator('[data-chart-interval="row"]').first().locator("xpath=ancestor::tr")).toContainText("City 02");
        expect(await page.evaluate(() => window.chartCompositionHost.prepareExport())).toBe(true);
      } else {
        await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(name === "scenario_facets" ? 3 : 1);
        const canvas = root.locator("canvas").first();
        await canvas.scrollIntoViewIfNeeded();
        const point = await page.evaluate(() => window.chartCompositionHost.firstMarkPosition("comparison"));
        const bounds = await canvas.boundingBox();
        expect(point).not.toBeNull(); expect(bounds).not.toBeNull();
        await page.mouse.move(bounds!.x + point!.x, bounds!.y + point!.y);
        await expect.poll(() => page.evaluate(() => window.chartCompositionHost.tooltipLines("comparison")).then((lines) => lines.join(" "))).toContain(name === "aligned_cycles" ? "74" : "80");
        const tickBounds = await page.evaluate(() => window.chartCompositionHost.numericTickBounds("comparison"));
        expect(tickBounds.every((box) => box.left >= -1 && box.right <= box.width + 1)).toBe(true);
        if (name === "scenario_facets") await expect(root.locator("[data-chart-facet]")).toHaveCount(3);
      }
      await page.screenshot({ path: test.info().outputPath(`${name}-${locale}-${width}.png`), fullPage: true });
    }
    const denied = structuredClone(fixtures.scenario_facets.section) as unknown as Extract<ReportSection, { kind: "chart" }>;
    const plan = denied.spec.presentation_plan as { analytical_views: unknown[] };
    plan.analytical_views = [{ view_id: "comparison", availability: "unavailable" }];
    await page.evaluate((section) => window.chartCompositionHost.update(section), denied);
    await expect(page.locator("[data-chart-analytical-unavailable]")).toBeVisible();
    await expect(page.locator('[data-chart-view="comparison"] canvas')).toHaveCount(0);
    expect(writes).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const name of ["scenario_rows", "scenario_facets"] as const) test(`analytical ${name} survives native settings, reload and undo`, async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 1000 });
  const fixture = structuredClone(fixtures[name].section) as unknown as Extract<ReportSection, { kind: "chart" }>;
  let saved = fixture;
  const patches: Array<{ patch: { spec: typeof fixture.spec }; expected_rev: number }> = [];
  await page.route("**/api/v1/**", async (route) => {
    if (route.request().method() === "PATCH" && route.request().url().endsWith(`/sections/${fixture.id}`)) {
      const body = route.request().postDataJSON(); patches.push(body);
      saved = { ...saved, spec: body.patch.spec, rev: body.expected_rev + 1 };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ section: saved, rev: saved.rev }) });
    } else await route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
  await page.goto("/__reference_component_test");
  await page.addStyleTag({ content: await styles });
  await page.evaluate((fixture) => window.chartCompositionHost.hydrate(fixture), fixture);
  await page.getByTitle(/Chart settings \(/).click();
  const dialog = page.getByRole("dialog", { name: "Chart settings" });
  await dialog.getByRole("combobox", { name: "View", exact: true }).selectOption("comparison");
  await dialog.getByRole("textbox", { name: "View title", exact: true }).fill("Prepared analytical comparison");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => patches.length).toBe(1);
  expect(saved.spec.presentation_plan).toEqual(fixture.spec.presentation_plan);
  expect(saved.spec.views?.[0].spec.rows).toEqual(fixture.spec.views?.[0].spec.rows);
  expect(saved.spec.views?.[0].spec.title).toBe("Prepared analytical comparison");
  await page.locator(".ProseMirror").focus();
  await page.keyboard.press(process.platform === "darwin" ? "Meta+z" : "Control+z");
  await expect.poll(() => patches.length).toBe(2);
  expect(saved.spec.views?.[0].spec.title).toBe(fixture.spec.views?.[0].spec.title);
  expect(saved.spec.presentation_plan).toEqual(fixture.spec.presentation_plan);
  await page.evaluate(async (saved) => { window.chartCompositionHost.hydrate(saved); await new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))); }, saved);
  if (name === "scenario_facets") await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(3);
  else {
    const row = page.locator('[data-chart-interval="row"]').first().locator("xpath=ancestor::tr");
    await row.focus(); await page.keyboard.press("Enter");
    await expect(page.locator('[data-chart-observation-details]')).toContainText("-40%");
    await page.keyboard.press("Escape"); await expect(row).toBeFocused();
  }
});

test("ranges without a value remain inspectable and long grouped tables retain original rows", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 1000 });
  await page.route("**/api/v1/**", (route) => route.fulfill({ contentType: "application/json", body: "{}" }));
  await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
  await page.goto("/__reference_component_test");
  await page.addStyleTag({ content: await styles });
  const fixture = structuredClone(fixtures.aligned_cycles.section) as unknown as Extract<ReportSection, { kind: "chart" }>;
  fixture.spec.views![0].spec.rows!.find((row) => row.series === "Primary" && row.elapsed_years === 5)!.value = null;
  await page.evaluate((fixture) => window.chartCompositionHost.hydrate(fixture), fixture);
  await expect.poll(() => page.evaluate(() => window.chartCompositionHost.settleCharts())).toBe(1);
  const canvas = page.locator('[data-chart-view="comparison"] canvas');
  // The supplied range sits at x=5; use the host's measured native scales through a real keyboard-independent pointer.
  const position = await page.evaluate(() => window.chartCompositionHost.rangePosition("comparison", 5, 64));
  expect(position).not.toBeNull();
  await canvas.hover({ position: position! });
  await expect.poll(() => page.evaluate(() => window.chartCompositionHost.tooltipLines("comparison")).then((lines) => lines.join(" "))).toContain("58");
  await canvas.click({ position: position! });
  await expect(page.locator("[data-chart-observation-details]")).toContainText("70");
  await page.keyboard.press("Escape");
  const large = structuredClone(fixtures.scenario_rows.section) as unknown as Extract<ReportSection, { kind: "chart" }>;
  const leaf = large.spec.views![0].spec;
  leaf.rows = Array.from({ length: 240 }, (_, index) => ({ ...leaf.rows![index % 29], city: `Entity ${String(index + 1).padStart(3, "0")}`, tier: `Tier ${Math.floor(index / 80) + 1}` }));
  leaf.row_count = 240;
  await page.evaluate((fixture) => window.chartCompositionHost.hydrate(fixture), large);
  const root = page.locator('[data-chart-view="comparison"]');
  await root.locator(".v2-chart-table-footer button").click();
  const scroll = root.locator(".v2-chart-table-scroll");
  await expect(scroll).toHaveClass(/is-virtual/);
  await scroll.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  const last = root.getByRole("row").filter({ hasText: "Entity 240" });
  await expect(last).toBeVisible();
  await last.click();
  await expect(page.getByRole("heading", { name: "Entity 240", exact: true })).toBeVisible();
});

test("analytical follow-up carries original selected keys without sending a turn", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 1000 });
  const requests: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    if (route.request().method() !== "GET" && !route.request().url().endsWith("/metrics/event")) requests.push(route.request().url());
    await route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await installReferenceComponent(page, "", "_chart-composition-edit-entry.tsx");
  await page.goto("/__reference_component_test");
  await page.addStyleTag({ content: await styles });
  const details: unknown[] = [];
  await page.exposeFunction("recordAnalyticalFollowup", (detail: unknown) => { details.push(detail); });
  await page.evaluate(() => {
    window.addEventListener("bowhead:chart-followup", (event) => {
      void (window as unknown as { recordAnalyticalFollowup: (detail: unknown) => Promise<void> }).recordAnalyticalFollowup((event as CustomEvent).detail);
    });
  });
  await page.evaluate((section) => window.chartCompositionHost.hydrate(section as unknown as ReportSection), fixtures.scenario_rows.section);
  await page.locator('[data-chart-interval="row"]').first().locator("xpath=ancestor::tr").click();
  await page.locator("[data-chart-observation-details]").getByRole("button", { name: "Ask about this chart", exact: true }).click();
  await expect.poll(() => details.length).toBe(1);
  expect(details[0]).toMatchObject({ selectedContext: { viewId: "comparison", tuples: [["City 01"]], measureKeys: expect.arrayContaining(["low", "high", "baseline", "current"]) } });
  expect(requests).toEqual([]);
});
