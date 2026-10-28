import { execFile } from "node:child_process";
import { readFile, stat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { promisify } from "node:util";

import { expect, test } from "@playwright/test";
import {
  chartStyleExportThemeFixture,
  chartStyleExportThemeV2Fixture,
  frozenChartPresentationV2Fixture,
} from "../__tests__/chart-style-export-fixture";
import {
  CANVAS_VISUAL_RENDERER_VERSION,
  computeCanvasVisualInputSha256,
} from "../lib/canvas-visual-identity";
import { PRINT_THEME_SNAPSHOT } from "../lib/print-theme";
import type { ChartSpec } from "../lib/chart/types";
import type { ReportSection } from "../lib/types";

// Batch 0 0-B baseline correction: renderer version advanced to "3" (B1a static-safe contract), while
// spec/fixture still used "1", failing parse and both golden tests. Recompute sha with real
// computeCanvasVisualInputSha256 and freeze it (determinism lock).
// Batch 1 finish: version 4 (always-on numeric labels/axis titles/lead layer) → same sha recomputation.
// Batch 3 finish: version 5 (full-table export/pie-legend percentages/grid tokens) → recompute again.
// Renderer version 20 with the current print theme (blue product default accent, native PDF/PPTX
// colors from the print theme, white paper and ink cover) → recomputed again; the Node renderer
// fixture (eval/fixtures/canvas_export/browser_visual_request_v1.json) carries the same sha and version.
// Renderer version 21 (Chart Embed Spec: sans chart text, separator gridlines, bar caps) → recomputed again.
const RENDERER_VERSION = CANVAS_VISUAL_RENDERER_VERSION;
const visualInputSha256 =
  "sha256:c640f694edf1d74d5a905c191aee08150694a74c58ffc2ccf3f7e35bedec9324";
const execFileAsync = promisify(execFile);

const v2ChartSpec: ChartSpec = {
  kind: "bar",
  title: "Revenue by region",
  row_count: 2,
  columns: [
    {
      key: "region",
      header: "Region",
      type: "categorical",
      role: "x",
    },
    {
      key: "revenue",
      header: "Revenue",
      type: "numeric",
      role: "y",
      unit: "USD",
    },
  ],
  rows: [
    { region: "APAC", revenue: 120 },
    { region: "EMEA", revenue: 90 },
  ],
  show_legend: false,
};

test("export-only route renders the production Canvas chart surface", async ({
  baseURL,
  page,
}) => {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  const allowedOrigin = new URL(baseURL).origin;
  const browserErrors: string[] = [];
  const apiRequests: string[] = [];
  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      !message.text().startsWith("Failed to load resource:")
    ) {
      browserErrors.push(`console: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => {
    browserErrors.push(`pageerror: ${error.stack ?? error.message}`);
  });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) apiRequests.push(url.pathname);
  });
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (
      url.origin === allowedOrigin ||
      url.protocol === "data:" ||
      url.protocol === "blob:"
    ) {
      await route.continue();
      return;
    }
    await route.abort("blockedbyclient");
  });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.addInitScript(
    (payload) => {
      window.__BOWHEAD_EXPORT_VISUAL__ = payload;
    },
    {
      blockKey: "sec_export_chart",
      sectionKind: "chart",
      locale: "zh-CN",
      theme: chartStyleExportThemeFixture(),
      printTheme: PRINT_THEME_SNAPSHOT,
      payload: {
        spec: {
          kind: "bar",
          title: "Revenue by region",
          row_count: 2,
          columns: [
            {
              key: "region",
              header: "Region",
              type: "categorical",
              role: "x",
            },
            {
              key: "revenue",
              header: "Revenue",
              type: "numeric",
              role: "y",
              unit: "USD",
            },
          ],
          rows: [
            { region: "APAC", revenue: 120 },
            { region: "EMEA", revenue: 90 },
          ],
          show_legend: false,
        },
      },
      visualInputSha256,
      rendererVersion: RENDERER_VERSION,
    },
  );

  await page.goto("/internal/canvas-export/visual/", {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(1_000);
  expect(browserErrors, browserErrors.join("\n\n")).toEqual([]);
  const root = page.getByTestId("canvas-export-visual-root");
  await expect(root).toHaveAttribute("data-export-render-state", "ready", {
    timeout: 15_000,
  });
  await expect(root).toHaveAttribute(
    "data-export-visual-input-sha256",
    visualInputSha256,
  );
  await expect(root).toHaveAttribute(
    "data-export-block-key",
    "sec_export_chart",
  );
  await expect(root.locator("canvas")).toBeVisible();
  await expect(root.locator(".v2-chart-fallback-wrap")).toHaveCount(0);
  await root.screenshot({
    path: "test-results/canvas-export-visual-production.png",
    animations: "disabled",
  });

  expect(await root.boundingBox()).toMatchObject({
    width: 1600,
    height: 900,
  });
  expect(browserErrors).toEqual([]);
  expect(apiRequests).toEqual([]);
});

test("V2 export route renders only the frozen server presentation", async ({
  baseURL,
  page,
}) => {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  const apiRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) apiRequests.push(url.pathname);
  });
  const theme = chartStyleExportThemeV2Fixture();
  const frozenPresentation = await frozenChartPresentationV2Fixture(
    v2ChartSpec,
  );
  const section = {
    id: "sec_export_chart_v2",
    kind: "chart",
    spec: v2ChartSpec,
  } as ReportSection;
  const expectedHash = await computeCanvasVisualInputSha256({
    section,
    locale: "zh-CN",
    theme,
    printTheme: PRINT_THEME_SNAPSHOT,
    frozenChartPresentation: frozenPresentation,
    exportSurfaceContractVersion: "canvas-export-visual/2",
  });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.addInitScript(
    (payload) => {
      window.__BOWHEAD_EXPORT_VISUAL__ = payload;
    },
    {
      blockKey: section.id,
      sectionKind: section.kind,
      locale: "zh-CN",
      theme,
      printTheme: PRINT_THEME_SNAPSHOT,
      payload: { spec: v2ChartSpec },
      visualInputSha256: expectedHash,
      rendererVersion: RENDERER_VERSION,
      frozenPresentation,
      exportSurfaceContractVersion: "canvas-export-visual/2",
    },
  );

  await page.goto("/internal/canvas-export/visual/", {
    waitUntil: "domcontentloaded",
  });
  const root = page.getByTestId("canvas-export-visual-root");
  await expect(root).toHaveAttribute("data-export-render-state", "ready", {
    timeout: 15_000,
  });
  await expect(root).toHaveAttribute(
    "data-export-visual-input-sha256",
    expectedHash!,
  );
  await expect(root).toHaveAttribute(
    "data-export-surface-contract-version",
    "canvas-export-visual/2",
  );
  await expect(root).toHaveAttribute(
    "data-export-presentation-checksum",
    frozenPresentation.presentationChecksum,
  );
  await expect(root.locator("canvas")).toBeVisible();
  await root.screenshot({
    path: "test-results/canvas-export-visual-v2-frozen.png",
    animations: "disabled",
  });
  expect(apiRequests).toEqual([]);
});

test("production Node renderer returns a matching receipt and PNG", async ({
  baseURL,
}) => {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  const environment = { ...process.env };
  delete environment.FORCE_COLOR;
  delete environment.NO_COLOR;
  const outputPath = resolve(
    "test-results/canvas-export-visual-node-backend.png",
  );
  const fixturePath = resolve(
    "../../eval/fixtures/canvas_export/browser_visual_request_v1.json",
  );
  const requestPath = resolve(
    "test-results/canvas-export-visual-node-request.json",
  );
  const request = JSON.parse(await readFile(fixturePath, "utf8"));
  request.frontendBaseUrl = baseURL;
  await writeFile(requestPath, JSON.stringify(request), "utf8");
  const { stdout, stderr } = await execFileAsync(
    process.execPath,
    ["scripts/render-export-visual.mjs", requestPath, outputPath],
    {
      cwd: process.cwd(),
      env: environment,
      timeout: 30_000,
    },
  );

  expect(stderr).toBe("");
  expect(JSON.parse(stdout)).toEqual({
    mime: "image/png",
    rendererVersion: RENDERER_VERSION,
    visualInputSha256,
    width: 1600,
    height: 900,
    warnings: [],
  });
  expect((await stat(outputPath)).size).toBeGreaterThan(1_000);
});

// Batch 0 0-B quality-gate negative controls: above prove good charts pass; these prove bad charts
// are blocked. Removing page.tsx:81 failure detector (or renaming .v2-chart-fallback-wrap)
// must fail here, or gate is dead (feedback: assert-absence-needs-positive-control).
// Poisoned spec: bar has two categorical columns, no numeric → buildBar deterministically throws
// (build-config.ts "bar chart requires … ≥1 numeric") → config_error fallback table.
const poisonedChartSpec = {
  kind: "bar",
  title: "质量闸负对照",
  row_count: 2,
  columns: [
    { key: "province", header: "省份", type: "categorical", role: "x" },
    { key: "city", header: "城市", type: "categorical" },
  ],
  rows: [
    { province: "浙江", city: "杭州" },
    { province: "江苏", city: "南京" },
  ],
  show_legend: false,
};

test("broken chart flips the visual surface to error, never ready", async ({
  baseURL,
  page,
}) => {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.addInitScript(
    (payload) => {
      window.__BOWHEAD_EXPORT_VISUAL__ = payload;
    },
    {
      blockKey: "sec_export_chart_broken",
      sectionKind: "chart",
      locale: "zh-CN",
      theme: chartStyleExportThemeFixture(),
      printTheme: PRINT_THEME_SNAPSHOT,
      payload: { spec: poisonedChartSpec },
      // Parse gate checks format only (equality at receipt stage); negative control must pass parse
      // and fail render gate, otherwise tests contract validation instead of quality gate.
      visualInputSha256: `sha256:${"0".repeat(64)}`,
      rendererVersion: RENDERER_VERSION,
    },
  );

  await page.goto("/internal/canvas-export/visual/", {
    waitUntil: "domcontentloaded",
  });
  const root = page.getByTestId("canvas-export-visual-root");
  // Live gate: fallback table appears and classified error; dead gate: still renders fallback, status ready.
  await expect(root).toHaveAttribute("data-export-render-state", "error", {
    timeout: 15_000,
  });
  await expect(root.locator(".v2-chart-fallback-wrap")).toHaveCount(1);
});

test("production Node renderer refuses to emit a PNG for a broken chart", async ({
  baseURL,
}) => {
  if (!baseURL) throw new Error("Playwright baseURL is required");
  const environment = { ...process.env };
  delete environment.FORCE_COLOR;
  delete environment.NO_COLOR;
  const outputPath = resolve(
    "test-results/canvas-export-visual-node-negative.png",
  );
  const fixturePath = resolve(
    "../../eval/fixtures/canvas_export/browser_visual_request_v1.json",
  );
  const requestPath = resolve(
    "test-results/canvas-export-visual-node-negative-request.json",
  );
  const request = JSON.parse(await readFile(fixturePath, "utf8"));
  request.frontendBaseUrl = baseURL;
  request.payload.blockKey = "sec_export_chart_broken";
  request.payload.payload = { spec: poisonedChartSpec };
  await writeFile(requestPath, JSON.stringify(request), "utf8");

  // Same production script/gate: state=error → visual_route_render_failed + nonzero exit.
  // Swallowed error with generated image (false quality_status=clean) lets execFile succeed → assertion fails.
  const result = await execFileAsync(
    process.execPath,
    ["scripts/render-export-visual.mjs", requestPath, outputPath],
    { cwd: process.cwd(), env: environment, timeout: 30_000 },
  ).then(
    () => null,
    (error: Error & { stderr?: string }) => error,
  );
  expect(result, "renderer must exit non-zero for a broken chart").not.toBeNull();
  expect(result!.stderr ?? "").toContain("visual_route_render_failed");
  await expect(stat(outputPath)).rejects.toThrow();
});
