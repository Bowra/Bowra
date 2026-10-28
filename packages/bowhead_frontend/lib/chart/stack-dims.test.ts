/**
 * Declared stacked axis roles (readability audit I).
 *
 * The backend decides which dimension of a two-level hierarchy is the category axis (child) and which the series
 * (parent) and writes it as `role: "x"` / `role: "color"`. Every frontend consumer must read the declaration; before,
 * each re-guessed from column order and could reverse the intended axes.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, expect, test } from "vitest";

import { buildChartJSConfig } from "./build-config";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import { resolvePresentation } from "./presentation";
import { roleOrderedDims } from "./stack-dims";
import type { ChartColumn, ChartSpec } from "./types";

const THEME = {
  palette: ["#111111", "#222222", "#333333", "#444444", "#555555", "#666666"],
  primary: "#111111",
  ink: "#000000",
  inkSoft: "#555555",
  surface: "#ffffff",
  grid: "#eeeeee",
  gridSoft: "#f5f5f5",
  positive: "#00aa00",
  negative: "#aa0000",
  sequential: { from: "#ffffff", to: "#000000" },
  barRadius: 4,
};

// Column order deliberately puts the parent first: column order alone would draw 2 province bars of 4 city segments.
function hierarchySpec(numeric: ChartColumn[], withRoles: boolean): ChartSpec {
  const values = (v: number) => Object.fromEntries(numeric.map((c, i) => [c.key, v * (i + 1)]));
  return {
    kind: "stacked_bar",
    title: "各市（按省着色）",
    row_count: 4,
    columns: [
      { key: "省", header: "省", type: "geo", ...(withRoles ? { role: "color" as const } : {}) },
      { key: "市", header: "市", type: "categorical", ...(withRoles ? { role: "x" as const } : {}) },
      ...numeric,
    ],
    rows: [
      { 省: "江苏", 市: "南京", ...values(30) },
      { 省: "江苏", 市: "无锡", ...values(20) },
      { 省: "浙江", 市: "杭州", ...values(25) },
      { 省: "浙江", 市: "宁波", ...values(5) },
    ],
  };
}

const ONE_MEASURE: ChartColumn[] = [{ key: "金额", header: "金额", type: "numeric" }];
const TWO_MEASURES: ChartColumn[] = [
  { key: "金额", header: "金额", type: "numeric" },
  { key: "客流", header: "客流", type: "numeric" },
];

describe("roleOrderedDims", () => {
  test("puts the declared x first and the declared series second", () => {
    const dims: ChartColumn[] = [
      { key: "a", header: "a", type: "categorical", role: "color" },
      { key: "b", header: "b", type: "categorical" },
      { key: "c", header: "c", type: "categorical", role: "x" },
    ];
    expect(roleOrderedDims(dims).map((c) => c.key)).toEqual(["c", "a", "b"]);
  });

  test("keeps column order unless both roles are declared", () => {
    const onlyX: ChartColumn[] = [
      { key: "a", header: "a", type: "categorical" },
      { key: "b", header: "b", type: "categorical", role: "x" },
    ];
    expect(roleOrderedDims(onlyX).map((c) => c.key)).toEqual(["a", "b"]);
  });
});

describe.each([
  ["one measure (long-table pivot path)", ONE_MEASURE],
  ["two measures (buildBar pivot path)", TWO_MEASURES],
])("stacked hierarchy with declared roles — %s", (_name, numeric) => {
  test("draws the child on the category axis and the parent as series", () => {
    const cfg = buildChartJSConfig(hierarchySpec(numeric, true), THEME as any);
    expect(cfg.data.labels).toEqual(["南京", "无锡", "杭州", "宁波"]);
    expect((cfg.data.datasets as any[]).map((d) => d.label)).toEqual(["江苏", "浙江"]);
  });

  test("negative control: the same spec without roles keeps the old column-order split", () => {
    const cfg = buildChartJSConfig(hierarchySpec(numeric, false), THEME as any);
    expect(cfg.data.labels).toEqual(["江苏", "浙江"]);
  });

  test("the normalized render shape (export freezing) counts the same split", () => {
    const { shape } = normalizeChartForRenderingV1(hierarchySpec(numeric, true));
    expect([shape.categoryCount, shape.seriesCount]).toEqual([4, 2]);
  });
});

test("the caption lead resolver looks for the declared series, not the second column", () => {
  const pres = resolvePresentation(hierarchySpec(ONE_MEASURE, true), "浙江增长最快");
  expect(pres.lead).toEqual({ kind: "dataset", index: 1 });
});
