import { describe, expect, it } from "vitest";

import {
  resolveChartContentProfile,
  resolveChartRenderRecipe,
  type ChartRenderContext,
} from "./render-context";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
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

const multiLine: ChartSpec = {
  kind: "line",
  title: "区域月度收入",
  row_count: 8,
  columns: [
    { key: "month", header: "月份", type: "date" },
    { key: "east", header: "华东", type: "numeric" },
    { key: "south", header: "华南", type: "numeric" },
    { key: "north", header: "华北", type: "numeric" },
    { key: "west", header: "西部", type: "numeric" },
  ],
  rows: Array.from({ length: 8 }, (_, index) => ({
    month: `2026-${String(index + 1).padStart(2, "0")}`,
    east: 20 + index,
    south: 17 + index,
    north: 14 - index,
    west: index === 3 ? -2 : 10 + index,
  })),
};

describe("chart render context", () => {
  it("derives a data-free content profile from finalized rows and columns", () => {
    expect(resolveChartContentProfile(normalizeChartForRenderingV1(multiLine).shape)).toEqual({
      family: "trend",
      kind: "line",
      seriesCount: 4,
      categoryCount: 8,
      longestLabelGraphemes: 7,
      hasNegativeValues: true,
    });
  });

  it("uses the real 344px chat bounds for compact density without hiding series identity", () => {
    const recipe = resolveChartRenderRecipe(
      context({
        placement: "chat",
        bounds: { widthPx: 344, heightPx: 320, devicePixelRatio: 2 },
        input: { hover: false, pointer: "coarse", keyboard: true },
      }),
      normalizeChartForRenderingV1(multiLine).shape,
    );

    expect(recipe).toMatchObject({
      widthClass: "compact",
      densityTier: "compact",
      showLegend: true,
      legendPosition: "bottom",
      maxXTicks: 4,
      tooltipIntersect: false,
    });
  });

  it("keeps a 720px report readable without manufacturing scroll for eight points", () => {
    const recipe = resolveChartRenderRecipe(context(), normalizeChartForRenderingV1(multiLine).shape);
    expect(recipe.widthClass).toBe("regular");
    expect(recipe.minimumPlotWidthPx).toBe(0);
    expect(recipe.maxXTicks).toBe(9);
  });

  it("does not make a phone-width six-month line swipe, but still widens six bars", () => {
    const monthly = (kind: ChartSpec["kind"]): ChartSpec => ({
      kind,
      title: "上半年营业额",
      row_count: 6,
      columns: [
        { key: "month", header: "月份", type: "date" },
        { key: "revenue", header: "营业额", type: "numeric" },
      ],
      rows: [19.9, 17.7, 14.1, 13.9, 13.0, 12.6].map((revenue, index) => ({
        month: `2026-0${index + 1}`,
        revenue,
      })),
    });
    // Mobile on 116: the chart container in a report is about 300px wide.
    const phone = context({ bounds: { widthPx: 300, heightPx: 320, devicePixelRatio: 3 } });
    const line = resolveChartRenderRecipe(phone, normalizeChartForRenderingV1(monthly("line")).shape);
    expect(line.widthClass).toBe("compact");
    expect(line.minimumPlotWidthPx).toBe(0);
    const bar = resolveChartRenderRecipe(phone, normalizeChartForRenderingV1(monthly("bar")).shape);
    expect(bar.minimumPlotWidthPx).toBe(336);
    const pie = resolveChartRenderRecipe(phone, normalizeChartForRenderingV1(monthly("pie")).shape);
    expect(pie.minimumPlotWidthPx).toBe(0);
  });

  it("still lets a dense line swipe on a phone", () => {
    const dense: ChartSpec = {
      ...multiLine,
      row_count: 40,
      rows: Array.from({ length: 40 }, (_, index) => ({
        month: `2026-01-${String(index + 1).padStart(2, "0")}`,
        east: index,
        south: index,
        north: index,
        west: index,
      })),
    };
    const recipe = resolveChartRenderRecipe(
      context({ bounds: { widthPx: 300, heightPx: 320, devicePixelRatio: 3 } }),
      normalizeChartForRenderingV1(dense).shape,
    );
    expect(recipe.minimumPlotWidthPx).toBe(800);
  });

  it("gives long horizontal comparisons vertical room instead of shrinking labels", () => {
    const hbar: ChartSpec = {
      kind: "hbar",
      title: "分公司排名",
      row_count: 12,
      columns: [
        { key: "branch", header: "分公司", type: "categorical" },
        { key: "value", header: "金额", type: "numeric" },
      ],
      rows: Array.from({ length: 12 }, (_, index) => ({
        branch: `华东第${index + 1}分公司营销中心`,
        value: 100 - index,
      })),
    };
    const recipe = resolveChartRenderRecipe(
      context({ bounds: { widthPx: 344, heightPx: 320, devicePixelRatio: 2 } }),
      normalizeChartForRenderingV1(hbar).shape,
    );
    expect(recipe.minimumPlotWidthPx).toBe(0);
    // D3: n × pitch + axis — compact width uses the 36px pitch: 12 × 36 + 22.
    expect(recipe.hbarRowPitchPx).toBe(36);
    expect(recipe.maxBarThicknessPx).toBe(20);
    expect(recipe.minimumPlotHeightPx).toBe(12 * 36 + 22);
    expect(recipe.tickFontSize).toBeGreaterThanOrEqual(11);
  });

  it("turns animation off for reduced-motion and static output", () => {
    const shape = normalizeChartForRenderingV1(multiLine).shape;
    expect(resolveChartRenderRecipe(context({ reducedMotion: true }), shape).animate).toBe(false);
    expect(resolveChartRenderRecipe(context({
      output: { medium: "raster", static: true },
    }), shape).animate).toBe(false);
  });
});
