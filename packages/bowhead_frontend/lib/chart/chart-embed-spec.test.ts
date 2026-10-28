/**
 * Chart Embed Spec drawing rules shared by every host (report, chat, previews, Agent overview, export):
 *  - D1 every text the chart draws is the interface sans (no monospace), data labels 12 / 500;
 *  - D2 gridlines are one solid separator hairline, the zero / base line one tier stronger;
 *  - D3 bar thickness caps (24 report/chat/export, 20 embed/compact) and row-pitch heights for horizontal bars;
 *  - axis titles state only the unit, at the axis end.
 */
import { describe, expect, it } from "vitest";

import { separatorGrid } from "../chart-theme";
import { buildChartJSConfig } from "./build-config";
import { colorAtOpacity, parseCssColor } from "./color";
import { headerUnitOf } from "./format";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import { resolveChartRenderRecipe, type ChartRenderContext } from "./render-context";
import type { ChartSpec } from "./types";

const THEME = {
  ink: "#061223",
  ink2: "#2B394D",
  inkSoft: "#57544D",
  primary: "#2C5F2D",
  palette: ["#2C5F2D", "#97BC62", "#F4A261", "#E76F51"],
  surface: "#FFFFFF",
  ...separatorGrid("#061223", "light"),
  fontSans: "Inter, system-ui, sans-serif",
  gridDensity: "quiet" as const,
};

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

const hbar = (n: number, extra: Partial<ChartSpec> = {}): ChartSpec => ({
  kind: "hbar",
  title: "华东 Q3 各品类销售额",
  row_count: n,
  columns: [
    { key: "c", header: "品类", type: "categorical" },
    { key: "v", header: "销售额", type: "numeric", unit: "亿元" },
  ],
  rows: Array.from({ length: n }, (_, i) => ({ c: `品类${i + 1}`, v: 5 - i * 0.5 })),
  show_data_labels: true,
  ...extra,
});

/** Every font family string reachable from a Chart.js config (static objects and scriptable font callbacks). */
function fontFamilies(cfg: ReturnType<typeof buildChartJSConfig>): string[] {
  const found: string[] = [];
  const visit = (value: unknown, depth: number) => {
    if (depth > 8 || value === null || value === undefined) return;
    if (typeof value === "function") return;
    if (Array.isArray(value)) {
      value.forEach((v) => visit(v, depth + 1));
      return;
    }
    if (typeof value !== "object") return;
    for (const [key, v] of Object.entries(value as Record<string, unknown>)) {
      if (key === "family" && typeof v === "string") found.push(v);
      else if ((key === "font" || key === "titleFont" || key === "bodyFont") && typeof v === "function") {
        const resolved = (v as (ctx: unknown) => unknown)({ dataIndex: 0, datasetIndex: 0 });
        visit(resolved, depth + 1);
      } else visit(v, depth + 1);
    }
  };
  visit(cfg.options, 0);
  cfg.data.datasets.forEach((ds) => visit(ds, 0));
  return found;
}

describe("D1 chart text uses the interface sans", () => {
  it.each(["bar", "hbar", "line", "pie", "scatter", "waterfall"] as const)("%s draws no monospace text", (kind) => {
    const spec: ChartSpec = kind === "scatter"
      ? {
          kind, title: "t", row_count: 3,
          columns: [
            { key: "n", header: "门店", type: "categorical" },
            { key: "x", header: "面积", type: "numeric" },
            { key: "y", header: "营业额", type: "numeric" },
          ],
          rows: [{ n: "A", x: 1, y: 2 }, { n: "B", x: 2, y: 3 }, { n: "C", x: 3, y: 1 }],
          show_data_labels: true,
        }
      : { ...hbar(4), kind };
    const families = fontFamilies(buildChartJSConfig(spec, THEME as never));
    expect(families.length).toBeGreaterThan(0);
    for (const family of families) {
      expect(family).toBe(THEME.fontSans);
      expect(family).not.toMatch(/mono/i);
    }
  });

  it("value ticks 11 tertiary ink, category ticks 12 secondary ink, data labels 12 / 500 secondary ink", () => {
    const cfg = buildChartJSConfig(hbar(3), THEME as never, { renderContext: context() }) as any;
    expect(cfg.options.scales.x.ticks.font.size).toBe(11);
    expect(cfg.options.scales.x.ticks.color).toBe(THEME.inkSoft);
    expect(cfg.options.scales.y.ticks.font.size).toBe(12);
    expect(cfg.options.scales.y.ticks.color).toBe(THEME.ink2);
    const dl = cfg.data.datasets[0].datalabels;
    expect(dl.font({ dataIndex: 0, datasetIndex: 0 })).toMatchObject({ size: 12, weight: 500 });
    expect(dl.color({ dataIndex: 0, datasetIndex: 0 })).toBe(THEME.ink2);
    expect(dl.backgroundColor).toBeUndefined();
    expect(cfg.options.plugins.tooltip.titleFont).toMatchObject({ size: 11, weight: 600, family: THEME.fontSans });
  });
});

describe("D2 gridlines", () => {
  it("separator tokens: ink 12 / 20% light, 14 / 24% dark, as canvas-readable rgba", () => {
    expect(separatorGrid("#061223", "light")).toEqual({
      gridSoft: "rgba(6, 18, 35, 0.12)",
      grid: "rgba(6, 18, 35, 0.2)",
    });
    expect(separatorGrid("#D5E6FF", "dark")).toEqual({
      gridSoft: "rgba(213, 230, 255, 0.14)",
      grid: "rgba(213, 230, 255, 0.24)",
    });
    expect(colorAtOpacity("rgb(1, 2, 3)", 0.5)).toBe("rgba(1, 2, 3, 0.5)");
    // Inks written in other CSS color forms still become translucent separators.
    expect(colorAtOpacity("hsl(215 71% 8%)", 0.12)).toBe("rgba(6, 18, 35, 0.12)");
    expect(colorAtOpacity("hsla(0, 0%, 100%, 1)", 0.14)).toBe("rgba(255, 255, 255, 0.14)");
    expect(colorAtOpacity("rgb(100% 0% 0%)", 0.2)).toBe("rgba(255, 0, 0, 0.2)");
    // Unparseable stays unchanged — opaque but never an invalid canvas colour.
    expect(colorAtOpacity("color-mix(in srgb, red 10%, transparent)", 0.5)).toBe("color-mix(in srgb, red 10%, transparent)");
  });

  it("named / oklch inks resolve through the browser (a painted 1 × 1 probe), not to the opaque ink", () => {
    // jsdom has no 2d context; stand in a context that paints the color the browser would resolve.
    const painted: Record<string, [number, number, number]> = { navy: [0, 0, 128], "oklch(0.2 0.03 255)": [12, 24, 40] };
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function () {
      let style = "#010203";
      return {
        get fillStyle() { return style; },
        set fillStyle(value: string) { if (painted[value] || value === "#010203") style = value; },
        fillRect() {},
        getImageData: () => ({ data: [...(painted[style] ?? [1, 2, 3]), 255] }),
      } as unknown as CanvasRenderingContext2D;
    } as never;
    try {
      expect(separatorGrid("navy", "dark").gridSoft).toBe("rgba(0, 0, 128, 0.14)");
      expect(colorAtOpacity("oklch(0.2 0.03 255)", 0.2)).toBe("rgba(12, 24, 40, 0.2)");
      // A color the browser rejects stays as-is rather than being read as the sentinel.
      expect(parseCssColor("not-a-color")).toBeNull();
    } finally {
      HTMLCanvasElement.prototype.getContext = original;
    }
  });

  it.each(["quiet", "standard"] as const)("%s grid is one solid hairline; the base line is the strong tier", (grid) => {
    const theme = { ...THEME, gridDensity: grid };
    const cfg = buildChartJSConfig({ ...hbar(3), kind: "bar" }, theme as never) as any;
    expect(cfg.options.scales.y.grid.borderDash).toEqual([]);
    expect(cfg.options.scales.y.grid.color).toBe(THEME.gridSoft);
    expect(cfg.options.scales.x.border.color).toBe(THEME.grid);
  });

  it("high contrast keeps the stronger grid colour", () => {
    const cfg = buildChartJSConfig({ ...hbar(3), kind: "bar" }, { ...THEME, gridDensity: "strong" } as never) as any;
    expect(cfg.options.scales.y.grid.color).toBe(THEME.grid);
  });
});

describe("D3 bar thickness and horizontal-bar height", () => {
  const recipeFor = (spec: ChartSpec, overrides: Partial<ChartRenderContext> = {}) =>
    resolveChartRenderRecipe(context(overrides), normalizeChartForRenderingV1(spec).shape);

  it("caps bars at 24 in reports/chat and 20 in embeds and compact widths", () => {
    expect(recipeFor(hbar(3)).maxBarThicknessPx).toBe(24);
    expect(recipeFor(hbar(3), { placement: "chat" }).maxBarThicknessPx).toBe(24);
    expect(recipeFor(hbar(3), { placement: "embed" }).maxBarThicknessPx).toBe(20);
    expect(recipeFor(hbar(3), { bounds: { widthPx: 390, heightPx: 300, devicePixelRatio: 3 } }).maxBarThicknessPx).toBe(20);
  });

  it("D3 amendment: exports size bars by band, clamp(24, band × 0.35, 96)", () => {
    const exportCtx = {
      placement: "embed" as const,
      output: { medium: "raster" as const, static: true },
      bounds: { widthPx: 1456, heightPx: 772, devicePixelRatio: 1 },
    };
    const bars = (n: number) => ({ ...hbar(n), kind: "bar" as const });
    // 3 vertical bars: band (1456 − 72) / 3 ≈ 461 → 161 → capped at 96.
    expect(recipeFor(bars(3), exportCtx).maxBarThicknessPx).toBe(96);
    // 12 vertical bars: band ≈ 115 → 40.
    expect(recipeFor(bars(12), exportCtx).maxBarThicknessPx).toBe(Math.round(((1456 - 72) / 12) * 0.35));
    // 60 vertical bars: band ≈ 23 → floor of 24.
    expect(recipeFor(bars(60), exportCtx).maxBarThicknessPx).toBe(24);
    // 3 horizontal bars: band (772 − 48) / 3 ≈ 241 → 84.
    expect(recipeFor(hbar(3), exportCtx).maxBarThicknessPx).toBe(84);
    // Exports never pin an exact thickness; screens pin it for height-fitted horizontal bars only.
    expect(recipeFor(hbar(3), exportCtx).fixedBarThicknessPx).toBeNull();
    expect(recipeFor(hbar(3)).fixedBarThicknessPx).toBe(24);
    expect(recipeFor(hbar(3), { placement: "embed" }).fixedBarThicknessPx).toBe(20);
    expect(recipeFor(bars(3)).fixedBarThicknessPx).toBeNull();
  });

  it("the cap reaches every bar dataset through the chart's dataset options", () => {
    const report = buildChartJSConfig(hbar(3), THEME as never, { renderContext: context() }) as any;
    expect(report.options.datasets.bar.maxBarThickness).toBe(24);
    const embed = buildChartJSConfig(hbar(3), THEME as never, { renderContext: context({ placement: "embed" }) }) as any;
    expect(embed.options.datasets.bar.maxBarThickness).toBe(20);
  });

  it("horizontal bars are as tall as their rows: n × pitch + axis (40 report, 36 embed)", () => {
    expect(recipeFor(hbar(3)).minimumPlotHeightPx).toBe(3 * 40 + 22);
    expect(recipeFor(hbar(6), { placement: "embed" }).minimumPlotHeightPx).toBe(6 * 36 + 22);
    expect(recipeFor({ ...hbar(3), kind: "bar" }).minimumPlotHeightPx).toBe(0);
  });

  it("horizontal bar labels sit 6px outside the bar end and the plot reserves the widest label + 8", () => {
    const cfg = buildChartJSConfig(hbar(3), THEME as never) as any;
    expect(cfg.data.datasets[0].datalabels.offset).toBe(6);
    const plugin = cfg.plugins.find((p: { id: string }) => p.id === "hbar-label-padding");
    let updates = 0;
    const chart = {
      options: { indexAxis: "y", layout: {} },
      data: cfg.data,
      ctx: { save() {}, restore() {}, font: "", measureText: (text: string) => ({ width: text.length * 7 }) },
      isDatasetVisible: () => true,
      update: () => { updates += 1; },
    };
    plugin.afterLayout(chart);
    const widest = Math.max(...cfg.data.datasets[0].data.map((v: number) =>
      cfg.data.datasets[0].datalabels.formatter(v).length * 7 + 6));
    expect(chart.options.layout).toMatchObject({ padding: { right: Math.ceil(widest + 8) } });
    expect(updates).toBe(1);
  });
});

describe("axis titles state only the unit", () => {
  it("reads the unit field or the header's unit qualifier, at the axis end", () => {
    const cfg = buildChartJSConfig(hbar(3), THEME as never) as any;
    expect(cfg.options.scales.x.title).toMatchObject({ text: "亿元", align: "end" });
    expect(headerUnitOf("营业额（万元）")).toBe("万元");
    expect(headerUnitOf("营业额 / 元")).toBe("元");
    expect(headerUnitOf("销售额/面积")).toBeUndefined();
    expect(headerUnitOf("客流(近30天)")).toBeUndefined();
  });

  it.each([
    ["营业额（万元）", "万元"], ["营业额 / 元", "元"], ["GMV ($M)", "$M"], ["收入 (USD mn)", "USD mn"],
    ["客流（万人次）", "万人次"], ["转化率（%）", "%"], ["利差 (bps)", "bps"], ["同比（百分点）", "百分点"],
    ["订单（单）", "单"], ["时长（小时）", "小时"], ["重量 (kg)", "kg"], ["面积（㎡）", "㎡"], ["客单价（元/人）", "元/人"],
    ["门店数（家）", "家"], ["设备（台）", "台"], ["营收（亿元）", "亿元"],
  ])("header unit allowlist: %s → %s", (header, unit) => {
    expect(headerUnitOf(header)).toBe(unit);
  });

  it.each([
    "销售额（同比）", "收入（不含税）", "订单数（去重）", "GMV (YoY)", "销售额 (估算)", "评分 (avg)", "销售额 / 华东",
    "营业额（2025）", "客流（近30天）", "利润（调整后）", "成本 (est.)", "销售额（含退款）",
  ])("a qualifier is not a unit: %s", (header) => {
    expect(headerUnitOf(header)).toBeUndefined();
  });

  it("scatter keeps both measure names: its two axes are different measures", () => {
    const cfg = buildChartJSConfig({
      kind: "scatter", title: "t", row_count: 2,
      columns: [
        { key: "x", header: "面积", type: "numeric", unit: "㎡" },
        { key: "y", header: "营业额", type: "numeric" },
      ],
      rows: [{ x: 1, y: 2 }, { x: 2, y: 3 }],
    }, THEME as never) as any;
    expect(cfg.options.scales.x.title.text).toBe("面积（㎡）");
    expect(cfg.options.scales.y.title.text).toBe("营业额");
  });
});
