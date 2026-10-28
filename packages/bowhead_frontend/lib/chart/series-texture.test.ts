import { describe, expect, it, vi } from "vitest";

import { chartThemeBaseForMode, resolveChartThemeForStyle, type ChartTheme } from "../chart-theme";
import { buildChartJSConfig } from "./build-config";
import { parseHexColor } from "./color";
import { contrastRatio } from "./fill-pattern";
import type { ChartRenderContext } from "./render-context";
import type { ChartSpec } from "./types";

// jsdom has no canvas backend; record what the adapter asks to paint instead.
vi.mock("./fill-pattern", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./fill-pattern")>()),
  fillPatternPaint: (fill: string, pattern: string, stroke: string) =>
    pattern === "solid" ? fill : { pattern, fill, stroke },
}));

const PALETTE = [
  "#2563A7", "#2F7F87", "#52607B", "#B06B33", "#718356", "#3B4654", "#7A2117", "#6F5C55",
  "#592C76", "#1F86C1", "#6E5F37", "#009381", "#6D2C39", "#8E4000", "#97686E", "#485E62",
];

function context(): ChartRenderContext {
  return {
    placement: "canvas",
    bounds: { widthPx: 760, heightPx: 380, devicePixelRatio: 2 },
    input: { hover: true, pointer: "fine", keyboard: true },
    output: { medium: "screen", static: false },
    mode: "light",
    locale: "en-US",
    reducedMotion: false,
    forcedColors: false,
    exactDataAlternative: { kind: "inspect-table" },
  };
}

function theme(capacity: number | undefined, colorVision: "standard" | "color-safe" = "standard"): ChartTheme {
  const base = resolveChartThemeForStyle("clean-blue", { contrast: "standard", colorVision }, chartThemeBaseForMode("light"), "light");
  const palette = colorVision === "color-safe" ? base.palette : PALETTE;
  const { colorOnlySeriesCapacity: _builtIn, ...style } = base.chartStyleV2!;
  return {
    ...base,
    palette,
    chartStyleV2: {
      ...style,
      categoricalSeriesPalette: palette,
      ...(capacity === undefined ? {} : { colorOnlySeriesCapacity: capacity }),
    },
  };
}

function stackedSpec(series: number): ChartSpec {
  const categories = ["Q1", "Q2", "Q3"];
  return {
    kind: "stacked_bar",
    title: "segments",
    row_count: categories.length * series,
    columns: [
      { key: "q", header: "Quarter", type: "categorical" },
      { key: "region", header: "Region", type: "categorical" },
      { key: "v", header: "Value", type: "numeric" },
    ],
    rows: categories.flatMap((q, qi) => Array.from({ length: series }, (_, si) => ({ q, region: `R${si + 1}`, v: 10 + qi + si }))),
  };
}

function pieSpec(slices: number): ChartSpec {
  return {
    kind: "pie",
    title: "share",
    row_count: slices,
    columns: [
      { key: "k", header: "K", type: "categorical" },
      { key: "v", header: "V", type: "numeric" },
    ],
    rows: Array.from({ length: slices }, (_, index) => ({ k: `S${index + 1}`, v: 30 - index })),
  };
}

type Painted = string | { pattern: string; fill: string; stroke: string };

function backgrounds(spec: ChartSpec, chartTheme: ChartTheme): Painted[] {
  const config = buildChartJSConfig(spec, chartTheme, { renderContext: context() });
  return config.data.datasets.map((dataset) => (dataset as unknown as { backgroundColor: Painted }).backgroundColor);
}

describe("series textures in Chart.js configs", () => {
  it("leaves charts within the color-only capacity untouched (no texture, same colors, same legend box)", () => {
    const withCapacity = buildChartJSConfig(stackedSpec(6), theme(11), { renderContext: context() });
    const withoutCapacity = buildChartJSConfig(stackedSpec(6), theme(undefined), { renderContext: context() });
    const colors = withCapacity.data.datasets.map((dataset) => (dataset as unknown as { backgroundColor: unknown }).backgroundColor);
    expect(colors).toEqual(PALETTE.slice(0, 6));
    expect(colors).toEqual(withoutCapacity.data.datasets.map((dataset) => (dataset as unknown as { backgroundColor: unknown }).backgroundColor));
    expect(withCapacity.data.datasets.every((dataset) => dataset.borderWidth === 0)).toBe(true);
    expect(withCapacity.options.plugins?.legend?.labels?.boxWidth).toBe(8);
  });

  it("textures the block past the capacity while every series keeps its own color", () => {
    // Measured capacity 11, product cap 6: texture from the 7th series.
    const painted = backgrounds(stackedSpec(13), theme(11));
    expect(painted.slice(0, 6)).toEqual(PALETTE.slice(0, 6));
    expect(painted[6]).toEqual({ pattern: "diagonal", fill: PALETTE[6], stroke: theme(11).surface });
    expect(painted[12]).toEqual({ pattern: "dots", fill: PALETTE[12], stroke: theme(11).surface });
    const config = buildChartJSConfig(stackedSpec(13), theme(11), { renderContext: context() });
    expect(config.options.plugins?.legend?.labels?.boxWidth).toBe(12);
    // Segments get the surface-colored separator; every palette color has ≥3:1 against it.
    expect(config.data.datasets.every((dataset) => dataset.borderWidth === 1 && dataset.borderColor === theme(11).surface)).toBe(true);
  });

  it("gives every series its own texture cycle under color-safe", () => {
    const painted = backgrounds(stackedSpec(5), theme(undefined, "color-safe"));
    expect(painted.map((item) => (typeof item === "string" ? "solid" : item.pattern)))
      .toEqual(["solid", "diagonal", "dots", "crosshatch", "solid"]);
  });

  it("textures pie slices past the capacity", () => {
    const sixColorTheme: ChartTheme = {
      ...theme(undefined),
      palette: PALETTE.slice(0, 6),
      chartStyleV2: { ...theme(undefined).chartStyleV2!, categoricalSeriesPalette: PALETTE.slice(0, 6) },
    };
    const config = buildChartJSConfig(pieSpec(8), sixColorTheme, { renderContext: context() });
    const slices = (config.data.datasets[0] as unknown as { backgroundColor: Painted[] }).backgroundColor;
    // The largest slice is the protagonist; the others are faded, textured or not.
    expect(slices.slice(0, 6).every((slice) => typeof slice === "string")).toBe(true);
    expect(slices[6]).toMatchObject({ pattern: "diagonal" });
    expect(slices[7]).toMatchObject({ pattern: "diagonal", fill: slices[1] });
    // Faded slices sit at ~1.5:1 against the surface; their texture strokes with ink instead.
    for (const slice of slices.slice(6) as Array<{ fill: string; stroke: string }>) {
      expect(contrastRatio(parseHexColor(slice.stroke)!, parseHexColor(slice.fill)!)).toBeGreaterThanOrEqual(3);
    }
  });

  it("does not collapse colors for a low-capacity team palette", () => {
    const painted = backgrounds(stackedSpec(5), theme(1));
    expect(painted.map((item) => (typeof item === "string" ? item : item.fill))).toEqual(PALETTE.slice(0, 5));
    expect(painted.map((item) => (typeof item === "string" ? "solid" : item.pattern)))
      .toEqual(["solid", "diagonal", "dots", "crosshatch", "solid"]);
  });

  it("keeps scatter identity mode when groups outnumber the palette", () => {
    // 40 groups on a 6-color palette: more groups than (color, point style) pairs, the case where the
    // presentation reassigns colors and a palette remap would hide identity mode.
    const rows = Array.from({ length: 40 }, (_, index) => ({ name: `Store ${index + 1}`, x: index, y: (index * 7) % 11 }));
    const spec: ChartSpec = {
      kind: "scatter",
      title: "stores",
      row_count: rows.length,
      columns: [
        { key: "name", header: "Store", type: "categorical" },
        { key: "x", header: "X", type: "numeric" },
        { key: "y", header: "Y", type: "numeric" },
      ],
      rows,
    };
    const sixColors: ChartTheme = {
      ...theme(undefined),
      palette: PALETTE.slice(0, 6),
      chartStyleV2: { ...theme(undefined).chartStyleV2!, categoricalSeriesPalette: PALETTE.slice(0, 6) },
    };
    const config = buildChartJSConfig(spec, sixColors, { renderContext: context() });
    expect(config.data.datasets).toHaveLength(1);
  });
});
