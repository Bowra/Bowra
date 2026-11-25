/**
 * Bar-end value labels must fit the canvas on the very first frame — with a real Chart.js instance, not a stub of
 * the plugin inputs. jsdom has no 2d context, so the context is a recording stand-in: measureText returns a width
 * proportional to the text, every drawing call is a no-op. Geometry (scales, layout padding) is Chart.js's own.
 */
import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  LinearScale,
  Tooltip,
  Legend,
  type ChartConfiguration,
} from "chart.js";
import { afterEach, describe, expect, it } from "vitest";

import { separatorGrid } from "../chart-theme";
import { buildChartJSConfig } from "./build-config";
import { HBAR_AXIS_TITLE_HEIGHT_PX, resolveChartRenderRecipe, type ChartRenderContext } from "./render-context";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import type { ChartSpec } from "./types";

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip, Legend);

const THEME = {
  ink: "#061223",
  ink2: "#2B394D",
  inkSoft: "#57544D",
  primary: "#2C5F2D",
  palette: ["#2C5F2D", "#97BC62", "#F4A261", "#E76F51"],
  surface: "#FFFFFF",
  ...separatorGrid("#061223", "light"),
  fontSans: "Inter, sans-serif",
  gridDensity: "quiet" as const,
};

function fakeContext(canvas: HTMLCanvasElement): CanvasRenderingContext2D {
  const state = { font: "12px sans-serif" };
  const noop = () => undefined;
  return new Proxy({} as Record<string, unknown>, {
    get(_target, key) {
      if (key === "canvas") return canvas;
      if (key === "font") return state.font;
      if (key === "measureText") {
        return (text: string) => {
          const size = Number(/([\d.]+)px/.exec(state.font)?.[1] ?? 12);
          return { width: String(text).length * size * 0.6, actualBoundingBoxAscent: size * 0.8, actualBoundingBoxDescent: size * 0.2 };
        };
      }
      if (key === "getTransform") return () => ({ a: 1, d: 1, e: 0, f: 0 });
      if (key === "createLinearGradient" || key === "createPattern") return () => ({ addColorStop: noop });
      if (key === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
      if (key === "getLineDash") return () => [];
      return noop;
    },
    set(_target, key, value) {
      if (key === "font") state.font = String(value);
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
}

const charts: Chart[] = [];
afterEach(() => {
  charts.splice(0).forEach((chart) => chart.destroy());
});

function context(widthPx: number, heightPx: number): ChartRenderContext {
  return {
    placement: "canvas",
    bounds: { widthPx, heightPx, devicePixelRatio: 1 },
    input: { hover: false, pointer: "coarse", keyboard: true },
    output: { medium: "screen", static: false },
    mode: "light",
    locale: "zh-CN",
    reducedMotion: true,
    forcedColors: false,
    exactDataAlternative: { kind: "inspect-table" },
  };
}

/** Build the product config, render it into a real Chart instance of the given CSS size, return the topmost
 *  bar-end label box (CSS pixels) computed from the chart's own scales and the label style. */
function topLabelTop(spec: ChartSpec, widthPx: number, heightPx: number): { top: number; padTop: number } {
  const cfg = buildChartJSConfig(spec, THEME as never, { renderContext: context(widthPx, heightPx) });
  const canvas = document.createElement("canvas");
  canvas.width = widthPx;
  canvas.height = heightPx;
  canvas.getContext = (() => fakeContext(canvas)) as never;
  const config = {
    type: cfg.type,
    data: cfg.data,
    options: { ...cfg.options, responsive: false, animation: false, devicePixelRatio: 1 },
    plugins: cfg.plugins,
  } as unknown as ChartConfiguration;
  const chart = new Chart(canvas, config);
  charts.push(chart);
  const y = chart.scales.y!;
  const ds = chart.data.datasets[0] as unknown as { data: number[]; datalabels: { offset: number; font: unknown } };
  const fontOf = typeof ds.datalabels.font === "function"
    ? (ds.datalabels.font as (c: unknown) => { size: number })({ dataIndex: 0, datasetIndex: 0 })
    : (ds.datalabels.font as { size: number });
  const max = Math.max(...ds.data);
  const anchor = y.getPixelForValue(max);
  const top = anchor - ds.datalabels.offset - fontOf.size * 1.2;
  const padTop = ((chart.options.layout?.padding ?? {}) as { top?: number }).top ?? 0;
  return { top, padTop };
}

const bars = (n: number): ChartSpec => ({
  kind: "bar",
  title: "城市营业额",
  row_count: n,
  columns: [
    { key: "city", header: "城市", type: "categorical" },
    { key: "gmv", header: "销售额（亿元）", type: "numeric" },
  ],
  rows: Array.from({ length: n }, (_, i) => ({ city: `城市${i + 1}`, gmv: 980 - i * 60 })),
  show_data_labels: true,
});

describe("vertical bar-end labels fit on the first frame (390 report, real Chart instance)", () => {
  it.each([3, 12])("%i categories: the 980 label under a 1000 axis maximum stays inside the canvas", (n) => {
    const { top } = topLabelTop(bars(n), 344, 300);
    expect(top).toBeGreaterThanOrEqual(0);
  });

  it("charts whose labels already fit keep their plot (no reserve)", () => {
    const spec = bars(3);
    spec.rows = [{ city: "A", gmv: 610 }, { city: "B", gmv: 400 }, { city: "C", gmv: 300 }];
    const { padTop } = topLabelTop(spec, 344, 300);
    expect(padTop).toBe(0);
  });
});

/** Real Chart instance of a horizontal bar chart sized exactly as ChartBlock sizes it on screen (rows + axis + title
 *  row); returns every bar's drawn thickness (CSS px). */
function hbarThicknesses(n: number, placement: ChartRenderContext["placement"]): number[] {
  const spec: ChartSpec = {
    kind: "hbar",
    title: "领域覆盖",
    row_count: n,
    columns: [
      { key: "d", header: "领域", type: "categorical" },
      { key: "v", header: "金额", type: "numeric", unit: "万元" },
    ],
    rows: Array.from({ length: n }, (_, i) => ({ d: `领域${i + 1}`, v: 12 - i })),
    show_data_labels: true,
  };
  const ctx = { ...context(560, 300), placement };
  const recipe = resolveChartRenderRecipe(ctx, normalizeChartForRenderingV1(spec).shape);
  const height = recipe.minimumPlotHeightPx + HBAR_AXIS_TITLE_HEIGHT_PX;
  const cfg = buildChartJSConfig(spec, THEME as never, { renderContext: { ...ctx, bounds: { ...ctx.bounds, heightPx: height } } });
  const canvas = document.createElement("canvas");
  canvas.width = 560;
  canvas.height = height;
  canvas.getContext = (() => fakeContext(canvas)) as never;
  const chart = new Chart(canvas, {
    type: cfg.type,
    data: cfg.data,
    options: { ...cfg.options, responsive: false, animation: false, devicePixelRatio: 1 },
    plugins: cfg.plugins,
  } as unknown as ChartConfiguration);
  charts.push(chart);
  return chart.getDatasetMeta(0).data.map((bar) => Math.round((bar as unknown as { height: number }).height));
}

describe("horizontal bars reach the D3 caps whatever the axis title and paddings take (real Chart instance)", () => {
  it.each([2, 3, 6])("report, %i categories: every bar is 24px", (n) => {
    expect(hbarThicknesses(n, "canvas")).toEqual(Array(n).fill(24));
  });

  it.each([2, 6])("embed, %i categories: every bar is 20px", (n) => {
    expect(hbarThicknesses(n, "embed")).toEqual(Array(n).fill(20));
  });
});

/** Real Chart instance of a single-series horizontal bar chart sized as ChartBlock sizes it in chat; returns the right
 *  edge (CSS px) of every displayed value label — a label starts at its bar's right end, which for a negative bar is
 *  the zero line — and the canvas width. */
function hbarLabelRightEdges(values: number[], widthPx: number): { rights: number[]; canvasWidth: number; padRight: number } {
  const spec: ChartSpec = {
    kind: "hbar",
    title: "门店订单数同比增速",
    row_count: values.length,
    columns: [
      { key: "store", header: "门店", type: "categorical" },
      { key: "growth", header: "同比增速", type: "numeric", unit: "%" },
    ],
    rows: values.map((growth, i) => ({ store: `门店${i + 1}`, growth })),
  };
  const ctx = { ...context(widthPx, 300), placement: "chat" as const };
  const recipe = resolveChartRenderRecipe(ctx, normalizeChartForRenderingV1(spec).shape);
  const height = recipe.minimumPlotHeightPx + HBAR_AXIS_TITLE_HEIGHT_PX;
  const cfg = buildChartJSConfig(spec, THEME as never, { renderContext: { ...ctx, bounds: { ...ctx.bounds, heightPx: height } } });
  const canvas = document.createElement("canvas");
  canvas.width = widthPx;
  canvas.height = height;
  canvas.getContext = (() => fakeContext(canvas)) as never;
  const chart = new Chart(canvas, {
    type: cfg.type,
    data: cfg.data,
    options: { ...cfg.options, responsive: false, animation: false, devicePixelRatio: 1 },
    plugins: cfg.plugins,
  } as unknown as ChartConfiguration);
  charts.push(chart);
  const x = chart.scales.x!;
  const ds = chart.data.datasets[0] as unknown as {
    data: number[];
    datalabels: { display: unknown; offset: number; font: unknown; formatter: (v: unknown, c: unknown) => string };
  };
  const rights: number[] = [];
  ds.data.forEach((value, dataIndex) => {
    const arg = { dataIndex, datasetIndex: 0, dataset: ds, chart };
    const read = (v: unknown) => (typeof v === "function" ? (v as (a: unknown) => unknown)(arg) : v);
    if (!read(ds.datalabels.display)) return;
    const size = (read(ds.datalabels.font) as { size: number }).size;
    const text = ds.datalabels.formatter(value, arg);
    const start = value < 0 ? x.getBasePixel() : x.getPixelForValue(value);
    rights.push(start + ds.datalabels.offset + text.length * size * 0.6);
  });
  const padRight = ((chart.options.layout?.padding ?? {}) as { right?: number }).right ?? 0;
  return { rights, canvasWidth: widthPx, padRight };
}

describe("horizontal bar value labels stay inside the canvas whatever the sign (real Chart instance)", () => {
  // 116 (1.7.66) year-over-year growth of the top 15 stores: every value negative, so the zero line is the plot's
  // right edge and every label starts there.
  const allNegative = [-0.0112, -0.0112, -0.0128, -0.0132, -0.0132, -0.0144, -0.0151, -0.0151, -0.0158, -0.016,
    -0.016, -0.0166, -0.0182, -0.0199, -0.0204];

  it.each([549, 830])("every value negative, %ipx: all 15 labels fit", (width) => {
    const { rights, canvasWidth } = hbarLabelRightEdges(allNegative, width);
    expect(rights).toHaveLength(15);
    for (const right of rights) expect(right).toBeLessThanOrEqual(canvasWidth);
  });

  it("mixed signs keep the reserve of their widest positive label: negative labels already sit inside the plot", () => {
    const mixed = [0.7671, 0.4775, 0.2842, 0.1879, 0.1295, -0.0706, -0.1315, -0.243, -0.2601, -0.279];
    const { rights, canvasWidth, padRight } = hbarLabelRightEdges(mixed, 830);
    for (const right of rights) expect(right).toBeLessThanOrEqual(canvasWidth);
    const positiveOnly = hbarLabelRightEdges(mixed.map((v) => Math.abs(v)), 830).padRight;
    expect(padRight).toBe(positiveOnly);
  });
});
