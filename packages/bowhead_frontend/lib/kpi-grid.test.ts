import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { kpiColumns, kpiColumnsThatFit, kpiMaxColumns } from "./kpi-grid";

/** Row sizes a `count`-KPI row breaks into with `columns` columns, e.g. 7 in 4 → [4, 3]. */
function rows(count: number, columns: number): number[] {
  const out: number[] = [];
  for (let left = count; left > 0; left -= columns) out.push(Math.min(columns, left));
  return out;
}

describe("kpiColumns (measured width)", () => {
  it("counts the 130px columns with 32px gaps that fit", () => {
    expect(kpiColumnsThatFit(602)).toBe(3);
    expect(kpiColumnsThatFit(750)).toBe(4);
    expect(kpiColumnsThatFit(1110)).toBe(7);
    expect(kpiColumnsThatFit(100)).toBe(1);
  });

  it("keeps the row on one line when it fits (wide canvas)", () => {
    expect(kpiColumns(5, 1110)).toBe(5);
    expect(kpiColumns(6, 1110)).toBe(6);
    expect(kpiColumns(7, 1110)).toBe(7);
  });

  it("leaves no orphan when it has to wrap", () => {
    // 1110px article: 8 KPIs were 6 + 2 with auto-fit; now 4 + 4.
    expect(rows(8, kpiColumns(8, 1110))).toEqual([4, 4]);
    // 750px article (four fit): 5 → 3 + 2, 7 → 4 + 3, 8 → 4 + 4.
    expect(rows(5, kpiColumns(5, 750))).toEqual([3, 2]);
    expect(rows(7, kpiColumns(7, 750))).toEqual([4, 3]);
    expect(rows(8, kpiColumns(8, 750))).toEqual([4, 4]);
    // 602px split view (three fit): 4 → 2 + 2 instead of 3 + 1, 5 → 3 + 2.
    expect(rows(4, kpiColumns(4, 602))).toEqual([2, 2]);
    expect(rows(5, kpiColumns(5, 602))).toEqual([3, 2]);
  });

  it("never asks for more columns than fit", () => {
    for (const width of [200, 350, 602, 750, 1110]) {
      for (let count = 1; count <= 12; count += 1) {
        expect(kpiColumns(count, width)).toBeLessThanOrEqual(Math.max(1, kpiColumnsThatFit(width)));
      }
    }
  });
});

describe("kpiMaxColumns (fallback before measurement)", () => {
  it("keeps up to four KPIs on one row and balances longer rows", () => {
    expect([1, 2, 3, 4].map(kpiMaxColumns)).toEqual([1, 2, 3, 4]);
    expect([5, 6, 7, 8, 9].map(kpiMaxColumns)).toEqual([3, 3, 4, 4, 3]);
    expect(kpiMaxColumns(0)).toBe(1);
  });
});

describe("styles/report-canvas.css KPI rules", () => {
  const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../styles/report-canvas.css"), "utf8");

  it("has a fallback rule for every cap kpiMaxColumns can return above one", () => {
    for (const cap of [2, 3, 4]) expect(css).toContain(`.v2-kpi-row[data-max-cols="${cap}"]`);
  });

  it("has a measured rule for one to eight columns, declared after the fallbacks so it wins", () => {
    for (let columns = 1; columns <= 8; columns += 1) expect(css).toContain(`.v2-kpi-row[data-cols="${columns}"]`);
    expect(css.indexOf('.v2-kpi-row[data-cols="1"]')).toBeGreaterThan(css.lastIndexOf('.v2-kpi-row[data-max-cols="4"]'));
  });

  it("keeps the phone two-column rule ahead of both attribute rules (specificity)", () => {
    const phone = css.slice(css.indexOf("@media (max-width: 640px)"));
    expect(phone).toMatch(/\.v2-kpi-row\[data-max-cols\],\s*\.canvas-report-article \.v2-kpi-row\[data-cols\]\s*\{\s*grid-template-columns:\s*1fr 1fr/);
  });
});
