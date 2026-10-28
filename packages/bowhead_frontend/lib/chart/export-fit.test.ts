/**
 * Long horizontal bars in the fixed export frame. Before, `tallH = 0` squeezed 50 categories into ~590px (11.8px
 * each, names touching, every other value label dropped); now the export draws the rows that fit and discloses it.
 */
import { describe, expect, test } from "vitest";

import { EXPORT_HBAR_CHROME_PX, EXPORT_HBAR_MIN_ROW_PX, fitHbarRowsForExport } from "./export-fit";
import type { ChartSpec } from "./types";

/** n rows whose primary values are **not** in descending order: a reversed ramp with one big negative outlier. */
function hbar(n: number, numeric = 1, rowCount = n): ChartSpec {
  return {
    kind: "hbar",
    title: "各品类营业额",
    row_count: rowCount,
    columns: [
      { key: "c", header: "品类", type: "categorical" },
      ...Array.from({ length: numeric }, (_, k) => ({ key: `v${k}`, header: k === 0 ? "营业额" : `v${k}`, type: "numeric" as const })),
    ],
    rows: Array.from({ length: n }, (_, i) => ({ c: `C${i}`, v0: i === 3 ? -1000 : i, v1: 1 })),
  };
}

const FRAME = 772; // 900 frame minus the export page's vertical padding.
const CAPACITY = Math.floor((FRAME - EXPORT_HBAR_CHROME_PX) / EXPORT_HBAR_MIN_ROW_PX);

describe("fitHbarRowsForExport", () => {
  test("keeps the rows with the largest |primary value|, in their original order, and the true total", () => {
    const fitted = fitHbarRowsForExport(hbar(50, 1, 111), FRAME);
    const kept = (fitted.rows ?? []).map((r) => r.c as string);
    expect(kept).toHaveLength(CAPACITY);
    // The −1000 outlier is the largest magnitude; then the CAPACITY−1 largest of the ascending ramp (49, 48, …).
    expect(kept).toContain("C3");
    expect(kept).not.toContain("C0");
    expect(kept).toContain("C49");
    // Original relative order preserved.
    const idx = kept.map((c) => Number(c.slice(1)));
    expect(idx).toEqual([...idx].sort((a, b) => a - b));
    expect(fitted.row_count).toBe(111);
    expect(fitted._export_top).toEqual({ measure: "营业额", shown: CAPACITY, total: 111 });
  });

  test("a chart that fits is returned unchanged", () => {
    const spec = hbar(CAPACITY);
    expect(fitHbarRowsForExport(spec, FRAME)).toBe(spec);
  });

  test("capacity depends on the series count: grouped bars need a wider band", () => {
    expect(fitHbarRowsForExport(hbar(50, 3), FRAME).rows).toHaveLength(19);
  });

  test("never touches other chart kinds", () => {
    const bar: ChartSpec = { ...hbar(50), kind: "bar" };
    expect(fitHbarRowsForExport(bar, FRAME)).toBe(bar);
  });

  test("ranks by the first value column the reader can see", () => {
    // v0 ascends, v1 descends: hiding v0 must keep the largest v1 rows and name v1 in the caption.
    const spec: ChartSpec = {
      ...hbar(50, 2),
      rows: Array.from({ length: 50 }, (_, i) => ({ c: `C${i}`, v0: i, v1: 50 - i })),
    };
    const fitted = fitHbarRowsForExport(spec, FRAME, ["v0"]);
    const kept = (fitted.rows ?? []).map((r) => r.c as string);
    expect(kept[0]).toBe("C0");
    expect(kept).not.toContain("C49");
    expect(fitted._export_top?.measure).toBe("v1");
  });
});

describe("fitHbarRowsForExport on long-format data", () => {
  // 50 stores × 2 channels × one value: the renderer pivots this into 50 bars of 2 segments each.
  const long = (): ChartSpec => ({
    kind: "hbar",
    title: "门店 × 渠道",
    row_count: 100,
    columns: [
      { key: "store", header: "门店", type: "categorical" },
      { key: "channel", header: "渠道", type: "categorical" },
      { key: "v", header: "营业额", type: "numeric" },
    ],
    rows: Array.from({ length: 50 }, (_, i) => [
      { store: `S${i}`, channel: "线上", v: i + 1 },
      { store: `S${i}`, channel: "线下", v: (i % 5) + 1 },
    ]).flat(),
  });
  const LONG_CAPACITY = Math.floor((FRAME - EXPORT_HBAR_CHROME_PX) / (EXPORT_HBAR_MIN_ROW_PX + 6));

  test("keeps whole categories — every series of a kept category — counted in categories", () => {
    const fitted = fitHbarRowsForExport(long(), FRAME);
    const rows = fitted.rows ?? [];
    const stores = new Set(rows.map((r) => r.store));
    expect(stores.size).toBe(LONG_CAPACITY);
    for (const store of stores) expect(rows.filter((r) => r.store === store)).toHaveLength(2);
    // Ranked by the category total, so the largest stores stay.
    expect(stores.has("S49")).toBe(true);
    expect(stores.has("S0")).toBe(false);
    expect(fitted._export_top).toEqual({ measure: "营业额", shown: LONG_CAPACITY, total: 50 });
  });

  test("a series hidden in the legend does not count toward a category's rank", () => {
    // Hiding 线上 leaves only 线下, whose value cycles 1..5: the S4/S9/… stores (value 5) rank first.
    const fitted = fitHbarRowsForExport(long(), FRAME, ["__pivot__线上"]);
    const stores = new Set((fitted.rows ?? []).map((r) => r.store));
    expect(stores.has("S4")).toBe(true);
    expect(stores.has("S0")).toBe(false);
  });
});

