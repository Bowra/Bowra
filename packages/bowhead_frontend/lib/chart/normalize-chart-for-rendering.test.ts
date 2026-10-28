import { describe, expect, it } from "vitest";

import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import type { ChartSpec } from "./types";

function monthlySpec(kind: ChartSpec["kind"] = "dual_axis"): ChartSpec {
  return {
    kind,
    title: "2025年月度营业额与环比变化趋势",
    columns: [
      { key: "month", header: "月份", type: "date" },
      { key: "revenue", header: "营业额", type: "numeric" },
      { key: "mom", header: "环比变化率", type: "numeric" },
    ],
    rows: [
      { month: "2025-1", revenue: 19, mom: 38 },
      { month: "2025-11", revenue: 13, mom: -10 },
      { month: "2025-2", revenue: 14, mom: 14 },
      { month: "2025-10", revenue: 14, mom: 9 },
    ],
    row_count: 4,
  };
}

describe("normalizeChartForRenderingV1 date-axis order", () => {
  it("orders non-zero-padded month labels chronologically", () => {
    const source = monthlySpec();
    const normalized = normalizeChartForRenderingV1(source).spec;

    expect(normalized.rows?.map((row) => row.month)).toEqual([
      "2025-1",
      "2025-2",
      "2025-10",
      "2025-11",
    ]);
    expect(source.rows?.map((row) => row.month)).toEqual([
      "2025-1",
      "2025-11",
      "2025-2",
      "2025-10",
    ]);
  });

  it("keeps a categorical bar axis in its authored order when another dimension is a date", () => {
    const source: ChartSpec = {
      kind: "bar",
      title: "区域对比",
      columns: [
        { key: "region", header: "区域", type: "categorical" },
        { key: "month", header: "月份", type: "date" },
        { key: "revenue", header: "营业额", type: "numeric" },
        { key: "profit", header: "利润", type: "numeric" },
      ],
      rows: [
        { region: "华北", month: "2025-11", revenue: 11, profit: 2 },
        { region: "华南", month: "2025-2", revenue: 9, profit: 1 },
      ],
      row_count: 2,
    };

    expect(normalizeChartForRenderingV1(source).spec.rows?.map((row) => row.region))
      .toEqual(["华北", "华南"]);
  });
});

describe("scatter 身份列认 geo（与 buildScatter 同口径）", () => {
  it("省份散点算出 5 条系列，图例才会显示", () => {
    const shape = normalizeChartForRenderingV1({
      kind: "scatter", title: "各省营业额与客流量",
      columns: [
        { key: "省", header: "省", type: "geo" },
        { key: "a", header: "营业额", type: "numeric" },
        { key: "b", header: "客流量", type: "numeric" },
      ],
      rows: [
        { 省: "上海", a: 1, b: 10 }, { 省: "北京", a: 2, b: 20 },
        { 省: "江苏", a: 3, b: 30 }, { 省: "天津", a: 4, b: 40 },
        { 省: "河南", a: 5, b: 50 },
      ],
    } as any).shape;
    expect(shape.seriesCount).toBe(5);
  });
});
