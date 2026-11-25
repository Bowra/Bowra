import { describe, expect, it } from "vitest";

import { HBAR_SEGMENT_SIZE, hbarSegment, orderedCategories } from "./segments";
import type { ChartSpec } from "./types";

/** Explicit horizontal bar of `n` brands in prepared (not value) order, as `add_chart` saves it. */
function ranking(n: number, value: (index: number) => number = (index) => 1000 - index): ChartSpec {
  return {
    kind: "hbar",
    title: "品牌营业额",
    row_count: n,
    columns: [
      { key: "brand", header: "品牌", type: "categorical" },
      { key: "revenue", header: "营业额", type: "numeric" },
    ],
    rows: Array.from({ length: n }, (_, index) => ({ brand: `B${index}`, revenue: value(index) })),
    explicit_presentation: { version: 1, mode: "plot", axes: { x: "brand", y: ["revenue"], x_scale: "category" }, group_by: [] },
  };
}

const brands = (spec: ChartSpec) => (spec.rows ?? []).map((row) => row.brand);

describe("hbarSegment", () => {
  it("draws a chart of up to 50 categories whole", () => {
    expect(HBAR_SEGMENT_SIZE).toBe(50);
    expect(hbarSegment(ranking(50), 0)).toBeNull();
  });

  it("windows a longer chart into contiguous segments in the prepared order, the last one partial", () => {
    // 116 1,082-brand ranking: 22 segments, the last holding 32 brands.
    const spec = ranking(1082);
    const first = hbarSegment(spec, 0)!;
    expect([first.start, first.end, first.total, first.index]).toEqual([1, 50, 1082, 0]);
    expect(brands(first.spec)).toEqual(Array.from({ length: 50 }, (_, index) => `B${index}`));
    expect(first.spec.row_count).toBe(50);

    const second = hbarSegment(spec, 1)!;
    expect(brands(second.spec)[0]).toBe("B50");
    const last = hbarSegment(spec, 21)!;
    expect([last.start, last.end]).toEqual([1051, 1082]);
    expect(brands(last.spec)).toHaveLength(32);
    // Out-of-range positions clamp instead of drawing an empty chart.
    expect(hbarSegment(spec, 99)!.index).toBe(21);
    expect(hbarSegment(spec, -3)!.index).toBe(0);
  });

  it("pins the value axis to the whole chart's range with zero included, so segments compare", () => {
    const spec = ranking(120, (index) => (index === 7 ? -30 : 500 - index));
    const domains = [0, 1, 2].map((index) => hbarSegment(spec, index)!.spec._export_axis_domains);
    for (const domain of domains) expect(domain).toEqual({ encoding: "hbar", domains: { x: [-30, 500] } });

    const positive = hbarSegment(ranking(60, (index) => index + 5), 1)!;
    expect(positive.spec._export_axis_domains).toEqual({ encoding: "hbar", domains: { x: [0, 64] } });
  });

  it("keeps a shared scale that already spans every view", () => {
    const shared = { encoding: "hbar" as const, domains: { x: [0, 5000] as [number, number] } };
    expect(hbarSegment({ ...ranking(60), _export_axis_domains: shared }, 1)!.spec._export_axis_domains).toBe(shared);
  });

  it("moves long data by whole categories: every series of a category stays in its segment", () => {
    const spec: ChartSpec = {
      ...ranking(0),
      columns: [
        { key: "brand", header: "品牌", type: "categorical" },
        { key: "year", header: "年份", type: "categorical" },
        { key: "revenue", header: "营业额", type: "numeric" },
      ],
      rows: Array.from({ length: 60 }, (_, index) => ["2024", "2025"].map((year) => ({ brand: `B${index}`, year, revenue: index }))).flat(),
      row_count: 120,
      explicit_presentation: { version: 1, mode: "plot", axes: { x: "brand", y: ["revenue"], x_scale: "category" }, group_by: ["year"] },
    };
    expect(orderedCategories(spec)!.categories).toHaveLength(60);
    const second = hbarSegment(spec, 1)!;
    expect([second.start, second.end, second.total]).toEqual([51, 60, 60]);
    expect(second.spec.rows).toHaveLength(20);
    expect(new Set(brands(second.spec))).toEqual(new Set(Array.from({ length: 10 }, (_, index) => `B${50 + index}`)));
  });

  it("leaves charts it cannot page whole", () => {
    // Fewer rows than the total: the rest is not here, and the chart keeps its "first N" disclosure.
    expect(hbarSegment({ ...ranking(80), row_count: 200 }, 0)).toBeNull();
    // Observations addressed by a presentation plan, and other kinds.
    expect(hbarSegment({ ...ranking(80), presentation_plan: {} as ChartSpec["presentation_plan"] }, 0)).toBeNull();
    expect(hbarSegment({ ...ranking(80), kind: "bar" }, 0)).toBeNull();
    expect(hbarSegment({ ...ranking(80), kind: "line" }, 0)).toBeNull();
  });

  it("gives different data a different signature, so a new chart starts at its first segment", () => {
    const signature = (spec: ChartSpec) => hbarSegment(spec, 0)!.signature;
    expect(signature(ranking(80))).toBe(signature(ranking(80)));
    expect(signature(ranking(80))).not.toBe(signature(ranking(81)));
    expect(signature(ranking(80))).not.toBe(signature({ ...ranking(80), rows: [...ranking(80).rows!].reverse() }));
  });
});
