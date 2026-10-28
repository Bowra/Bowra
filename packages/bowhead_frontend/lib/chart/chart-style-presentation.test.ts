import { describe, expect, it } from "vitest";

import fixtures from "../../../../config/chart_style/normalization_fixtures.json";
import presentationFixtures from "../../../../config/chart_style/presentation_fixtures.json";
import { chartThemeBaseForMode, resolveChartThemeForStyle, type ChartTheme } from "../chart-theme";
import { buildChartJSConfig } from "./build-config";
import {
  resolveChartPresentation,
  type ChartKindPresentationTokensV2,
  type ChartStyleThemeV2,
} from "./chart-style-presentation";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import type { ChartRenderContext } from "./render-context";
import type { ChartSpec } from "./types";

function context(overrides: Partial<ChartRenderContext> = {}): ChartRenderContext {
  return {
    placement: "canvas",
    bounds: { widthPx: 720, heightPx: 360, devicePixelRatio: 2 },
    input: { hover: true, pointer: "fine", keyboard: true },
    output: { medium: "screen", static: false },
    mode: "light",
    locale: "zh-CN",
    reducedMotion: false,
    forcedColors: false,
    exactDataAlternative: { kind: "inspect-table" },
    ...overrides,
  };
}

function themeFor(
  kind: ChartSpec["kind"],
  tokens: Partial<Omit<ChartKindPresentationTokensV2, "line" | "bar">> & {
    line?: Partial<NonNullable<ChartKindPresentationTokensV2["line"]>>;
    bar?: Partial<NonNullable<ChartKindPresentationTokensV2["bar"]>>;
  } = {},
  accessibility: ChartTheme["accessibility"] = { contrast: "standard", colorVision: "standard" },
): ChartTheme {
  const base = resolveChartThemeForStyle("bowhead-editorial", accessibility, chartThemeBaseForMode("light"), "light");
  const current = base.chartStyleV2!.kindTokens[kind]!;
  return {
    ...base,
    chartStyleV2: {
      ...base.chartStyleV2!,
      kindTokens: {
        ...base.chartStyleV2!.kindTokens,
        [kind]: {
          ...current,
          ...tokens,
          ...(tokens.line ? { line: { ...current.line!, ...tokens.line } } : {}),
          ...(tokens.bar ? { bar: { ...current.bar!, ...tokens.bar } } : {}),
        },
      },
    },
  };
}

function lineSpec(seriesCount = 1, pointCount = 4): ChartSpec {
  const numeric = Array.from({ length: seriesCount }, (_, index) => ({
    key: `s${index}`,
    header: `S${index}`,
    type: "numeric" as const,
  }));
  return {
    kind: "line",
    title: "趋势",
    row_count: pointCount,
    columns: [{ key: "x", header: "X", type: "date" }, ...numeric],
    rows: Array.from({ length: pointCount }, (_, rowIndex) => Object.fromEntries([
      ["x", `M${rowIndex + 1}`],
      ...numeric.map((column, seriesIndex) => [column.key, rowIndex - seriesIndex]),
    ])),
  };
}

describe("normalized chart render shape", () => {
  it.each(fixtures.fixtures)("matches the shared raw-spec fixture: $name", ({ rawSpec, shape }) => {
    const original = structuredClone(rawSpec);
    expect(normalizeChartForRenderingV1(rawSpec as ChartSpec).shape).toEqual(shape);
    expect(rawSpec).toEqual(original);
  });

  it("does not copy labels, values, units or business column keys into the shape", () => {
    const shape = normalizeChartForRenderingV1({
      kind: "line",
      title: "机密收入",
      row_count: 2,
      columns: [
        { key: "secret_month", header: "机密月份", type: "date" },
        { key: "secret_revenue", header: "机密收入", type: "numeric", unit: "元" },
      ],
      rows: [{ secret_month: "秘密一月", secret_revenue: 123456 }, { secret_month: "秘密二月", secret_revenue: 654321 }],
    }).shape;
    const serialized = JSON.stringify(shape);
    for (const secret of ["机密", "secret_", "秘密一月", "123456", "元"]) {
      expect(serialized).not.toContain(secret);
    }
  });

  it("keeps the finalized ChartSpec byte-for-byte unchanged while resolving presentation", () => {
    const spec = lineSpec(3, 40);
    const original = structuredClone(spec);
    buildChartJSConfig(spec, themeFor("line", {
      tooltip: "disabled",
      grid: "hidden",
      line: { curve: "straight", widthPx: 2.5, points: "hide" },
    }), { renderContext: context() });
    expect(spec).toEqual(original);
  });
});

describe("presentation resolver and Chart.js adapter", () => {
  it.each(presentationFixtures.fixtures)("matches the shared presentation fixture: $name", (fixture) => {
    const base = themeFor("line");
    const theme: ChartTheme = {
      ...base,
      accessibility: fixture.accessibility as ChartTheme["accessibility"],
      palette: [...fixture.categoricalSeriesPalette],
      chartStyleV2: {
        presentationPolicyVersion: "chart-presentation/1",
        kindTokens: fixture.kindTokens as ChartStyleThemeV2["kindTokens"],
        categoricalSeriesPalette: [...fixture.categoricalSeriesPalette],
        maxDistinctSeriesColors: 16,
        paletteOverflowRecipeVersion: "palette-overflow/2",
        ...("colorOnlySeriesCapacity" in fixture
          ? { colorOnlySeriesCapacity: (fixture as { colorOnlySeriesCapacity: number }).colorOnlySeriesCapacity }
          : {}),
      },
    };
    expect(resolveChartPresentation(
      fixture.shape as Parameters<typeof resolveChartPresentation>[0],
      theme,
      fixture.renderContext as ChartRenderContext,
    )).toEqual(fixture.expected);
  });

  it("implements the tooltip capability matrix without replacing formatter or interaction mode", () => {
    const spec = lineSpec(2);
    const shape = normalizeChartForRenderingV1(spec).shape;
    const disabled = themeFor("line", { tooltip: "disabled" });
    const enabled = themeFor("line", { tooltip: "enabled" });

    expect(resolveChartPresentation(shape, enabled, context()).effective.tooltip).toBe("enabled");
    expect(resolveChartPresentation(shape, disabled, context()).effective.tooltip).toBe("disabled");
    const unavailable = resolveChartPresentation(shape, disabled, context({ exactDataAlternative: { kind: "none" } }));
    expect(unavailable.effective.tooltip).toBe("enabled");
    expect(unavailable.adjustments[0]?.reasonCodes).toContain("capability.tooltip_exact_data_unavailable");
    expect(resolveChartPresentation(shape, enabled, context({
      output: { medium: "raster", static: true },
      exactDataAlternative: { kind: "none" },
    })).effective.tooltip).toBe("disabled");

    const config = buildChartJSConfig(spec, disabled, { renderContext: context() });
    expect((config.options.plugins?.tooltip as { enabled?: boolean }).enabled).toBe(false);
    expect((config.options.plugins?.tooltip as { callbacks?: unknown }).callbacks).toBeDefined();
    expect(config.options.interaction).toMatchObject({ mode: "index", intersect: false });
  });

  it("maps monotone without tension and clears it when switching to straight", () => {
    const spec = lineSpec();
    const monotone = buildChartJSConfig(spec, themeFor("line", { line: { curve: "monotone" } }), {
      renderContext: context(),
    }).data.datasets[0] as unknown as Record<string, unknown>;
    expect(monotone.cubicInterpolationMode).toBe("monotone");
    expect(monotone.tension).toBeUndefined();

    const straight = buildChartJSConfig(spec, themeFor("line", { line: { curve: "straight" } }), {
      renderContext: context(),
    }).data.datasets[0] as unknown as Record<string, unknown>;
    expect(straight.cubicInterpolationMode).toBeUndefined();
    expect(straight.tension).toBe(0);
  });

  it("limits requested points by density while retaining hit targets", () => {
    const spec = lineSpec(1, 40);
    const compact = context({ bounds: { widthPx: 344, heightPx: 320, devicePixelRatio: 2 } });
    const theme = themeFor("line", { line: { points: "show", pointRadiusPx: 3 } });
    const shape = normalizeChartForRenderingV1(spec).shape;
    const resolved = resolveChartPresentation(shape, theme, compact);
    expect(resolved.effective.line).toMatchObject({ points: "auto", pointRadiusPx: 0, hitRadiusPx: 8 });
    expect(resolved.adjustments.find((item) => item.path === "line.points")?.reasonCodes)
      .toContain("legibility.points_density_limited");
    const dataset = buildChartJSConfig(spec, theme, { renderContext: compact }).data.datasets[0] as unknown as Record<string, unknown>;
    expect(dataset.pointRadius).toBe(0);
    expect(dataset.hitRadius).toBeGreaterThanOrEqual(8);
    expect(dataset.pointHoverRadius).toBeGreaterThanOrEqual(4);
  });

  it("enforces the high-contrast line-width floor", () => {
    const spec = lineSpec();
    const high = themeFor(
      "line",
      { line: { widthPx: 1.5 } },
      { contrast: "high", colorVision: "standard" },
    );
    const resolved = resolveChartPresentation(normalizeChartForRenderingV1(spec).shape, high, context());
    expect(resolved.effective.line?.widthPx).toBe(2);
    expect(resolved.adjustments.find((item) => item.path === "line.widthPx")?.adjustedBy).toBe("accessibility");
  });

  it.each([
    ["thin", 1.5],
    ["standard", 2],
    ["strong", 2.5],
  ] as const)("maps the %s line width to %spx", (_token, widthPx) => {
    const config = buildChartJSConfig(lineSpec(), themeFor("line", { line: { widthPx } }), {
      renderContext: context(),
    });
    expect(config.data.datasets[0]?.borderWidth).toBe(widthPx);
  });

  it.each([
    ["square", 0],
    ["soft", 3],
    ["round", 6],
  ] as const)("maps the %s bar radius to %spx", (_token, radiusPx) => {
    const spec: ChartSpec = {
      kind: "bar",
      title: "bar",
      row_count: 1,
      columns: [
        { key: "x", header: "X", type: "categorical" },
        { key: "y", header: "Y", type: "numeric" },
      ],
      rows: [{ x: "A", y: 2 }],
    };
    const config = buildChartJSConfig(spec, themeFor("bar", { bar: { radiusPx } }), {
      renderContext: context(),
    });
    const borderRadius = (config.data.datasets[0] as unknown as {
      borderRadius: (value: unknown) => unknown;
    }).borderRadius;
    expect(borderRadius({ dataIndex: 0, raw: 2 })).toEqual({
      topLeft: radiusPx,
      topRight: radiusPx,
      bottomLeft: 0,
      bottomRight: 0,
    });
  });

  it.each([
    ["auto", 3],
    ["show", 3],
    ["hide", 0],
  ] as const)("maps point visibility %s without deleting hit data", (points, radius) => {
    const config = buildChartJSConfig(lineSpec(), themeFor("line", { line: { points, pointRadiusPx: 3 } }), {
      renderContext: context(),
    });
    const dataset = config.data.datasets[0] as unknown as Record<string, unknown>;
    expect(dataset.pointRadius).toBe(radius);
    expect(dataset.hitRadius).toBeGreaterThanOrEqual(8);
  });

  // Chart Embed Spec D2: quiet and standard merged into one solid hairline tier.
  it.each([
    ["quiet", []],
    ["standard", []],
  ] as const)("maps %s grid density", (grid, borderDash) => {
    const config = buildChartJSConfig(lineSpec(), themeFor("line", { grid }), {
      renderContext: context(),
    });
    expect((config.options.scales as Record<string, { grid: { borderDash: number[] } }>).y.grid.borderDash)
      .toEqual(borderDash);
  });

  it("rounds only the sign-aware outer edge of stacked bars", () => {
    const spec: ChartSpec = {
      kind: "stacked_bar",
      title: "正负堆叠",
      row_count: 2,
      columns: [
        { key: "x", header: "X", type: "categorical" },
        { key: "a", header: "A", type: "numeric" },
        { key: "b", header: "B", type: "numeric" },
      ],
      rows: [{ x: "P", a: 2, b: 3 }, { x: "N", a: -2, b: -3 }],
    };
    const config = buildChartJSConfig(spec, themeFor("stacked_bar", { bar: { radiusPx: 6 } }), {
      renderContext: context(),
    });
    const inner = (config.data.datasets[0] as unknown as { borderRadius: (value: unknown) => unknown }).borderRadius;
    const outer = (config.data.datasets[1] as unknown as { borderRadius: (value: unknown) => unknown }).borderRadius;
    expect(inner({ dataIndex: 0, raw: 2 })).toEqual({ topLeft: 0, topRight: 0, bottomLeft: 0, bottomRight: 0 });
    expect(outer({ dataIndex: 0, raw: 3 })).toEqual({ topLeft: 6, topRight: 6, bottomLeft: 0, bottomRight: 0 });
    expect(outer({ dataIndex: 1, raw: -3 })).toEqual({ topLeft: 0, topRight: 0, bottomLeft: 6, bottomRight: 6 });
  });

  it("keeps the zero line and axis border when ordinary grid lines are hidden", () => {
    const spec = lineSpec(2);
    const config = buildChartJSConfig(spec, themeFor("line", { grid: "hidden" }), {
      renderContext: context(),
    });
    const y = (config.options.scales as Record<string, {
      grid: { color: (value: unknown) => string };
      border?: unknown;
    }>).y;
    expect(y.grid.color({ tick: { value: -1 } })).toBe("transparent");
    expect(y.grid.color({ tick: { value: 0 } })).not.toBe("transparent");
    expect(y.border).toBeDefined();
  });

  it("uses stable non-color encodings after palette overflow", () => {
    const spec = lineSpec(8);
    const base = themeFor("line");
    const theme: ChartTheme = {
      ...base,
      palette: base.palette.slice(0, 2),
      chartStyleV2: {
        ...base.chartStyleV2!,
        categoricalSeriesPalette: base.palette.slice(0, 2),
      },
    };
    const config = buildChartJSConfig(spec, theme, { renderContext: context() });
    const first = config.data.datasets[0] as unknown as Record<string, unknown>;
    const third = config.data.datasets[2] as unknown as Record<string, unknown>;
    expect(first.borderColor).toBe(third.borderColor);
    expect(first.borderDash).not.toEqual(third.borderDash);
    expect(first.pointStyle).not.toEqual(third.pointStyle);
    const resolved = resolveChartPresentation(normalizeChartForRenderingV1(spec).shape, theme, context());
    expect(resolved.adjustments.find((item) => item.path === "seriesEncodings")?.reasonCodes)
      .toContain("correctness.palette_overflow_non_color_encoding");
  });

  it("uses the server-owned effective palette without changing data or bordering grouped bars", () => {
    const spec: ChartSpec = {
      kind: "bar",
      title: "expanded palette",
      row_count: 2,
      columns: [
        { key: "x", header: "X", type: "categorical" },
        { key: "a", header: "A", type: "numeric" },
        { key: "b", header: "B", type: "numeric" },
        { key: "c", header: "C", type: "numeric" },
      ],
      rows: [{ x: "P", a: 1, b: 2, c: 3 }, { x: "Q", a: 4, b: 5, c: 6 }],
    };
    const base = themeFor("bar", {}, { contrast: "standard", colorVision: "color-safe" });
    const effectivePalette = ["#112233", "#445566", "#778899"];
    const theme: ChartTheme = {
      ...base,
      palette: base.palette.slice(0, 2),
      chartStyleV2: {
        ...base.chartStyleV2!,
        categoricalSeriesPalette: effectivePalette,
      },
    };
    const config = buildChartJSConfig(spec, theme, { renderContext: context() });
    expect(config.data.datasets.map((dataset) =>
      (dataset as unknown as { backgroundColor?: unknown }).backgroundColor
    )).toEqual(effectivePalette);
    expect(config.data.datasets.map((dataset) => dataset.data)).toEqual([[1, 4], [2, 5], [3, 6]]);
    // Grouped bars do not touch: no surface-colored border eating into each bar (stacked segments keep one).
    expect(config.data.datasets.every((dataset) => dataset.borderWidth === 0)).toBe(true);
  });

  it("keeps the 200-point × 12-series config-build median inside 50ms", () => {
    // A coarse regression guard, not a frame budget: the full suite runs in parallel on shared
    // machines, where tail samples (p95) swing past 100ms. The median of warmed-up runs stays
    // stable under load, while a real algorithmic regression still lands far above 50ms.
    const spec = lineSpec(12, 200);
    const theme = themeFor("line");
    for (let index = 0; index < 5; index += 1) buildChartJSConfig(spec, theme, { renderContext: context() });
    const samples: number[] = [];
    for (let index = 0; index < 30; index += 1) {
      const started = performance.now();
      buildChartJSConfig(spec, theme, { renderContext: context() });
      samples.push(performance.now() - started);
    }
    samples.sort((first, second) => first - second);
    expect(samples[Math.floor(samples.length / 2)]).toBeLessThan(50);
  });
});
