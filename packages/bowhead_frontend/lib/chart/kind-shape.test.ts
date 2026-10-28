/**
 * **Case-by-case parity** between kind-shape.ts and buildChartJSConfig.
 *
 * These cases exist solely because kind-shape claims to be the sole renderability criterion,
 * while builder success is authoritative. Prove the claim, or it is merely a third implementation.
 *
 * Commit shapes **synthesized from column-type combinations**: shape checks use only types/counts, ignoring values,
 * so synthetic sets match actual corpora and cover rare combinations (geo+geo, date-only etc.).
 * Development checked all 1873 real 116 charts × 12 kinds = **22,476 comparisons, zero divergences**;
 * that probe is excluded from the repository (contains production data).
 */
import { describe, expect, it } from "vitest";

import { buildChartJSConfig } from "./build-config";
import { canBuildKind, checkKindShape, checkKindUnits, shapeCounts } from "./kind-shape";
import { resolveChartTheme } from "@/lib/chart-theme";
import type { ChartColumn, ChartSpec } from "./types";

const THEME = resolveChartTheme();

describe("native value-axis unit admission", () => {
  const spec: ChartSpec = {
    kind: "line", title: "Monthly observations", row_count: 2,
    columns: [
      { key: "month", header: "Month", type: "date" },
      { key: "revenue", header: "Revenue", type: "numeric", unit: "USD", y_axis_id: 0 },
      { key: "rate", header: "Rate", type: "numeric", format: "percent", percent_scale: "fraction", y_axis_id: 1 },
      { key: "visits", header: "Visits", type: "numeric", unit: "people", y_axis_id: 1 },
    ],
    rows: [{ month: "2026-01", revenue: 100, rate: 0.2, visits: 50 }, { month: "2026-02", revenue: 120, rate: 0.3, visits: 55 }],
  };

  it.each(["line", "bar", "hbar", "stacked_bar", "area_stacked", "combo"] as const)("rejects mixed units sharing the %s value axis", (kind) => {
    expect(checkKindUnits(spec, kind)).toEqual([
      { axis: kind === "hbar" ? "x" : "y", measureKeys: ["revenue", "rate", "visits"], units: ["USD", "%", "people"] },
    ]);
  });

  it("checks every dual-axis assignment instead of trusting the first measure", () => {
    expect(checkKindUnits(spec, "dual_axis")).toEqual([{ axis: "y1", measureKeys: ["rate", "visits"], units: ["%", "people"] }]);
    expect(checkKindUnits({ ...spec, columns: spec.columns.slice(0, 3) }, "dual_axis")).toEqual([]);
  });

  it("allows same-unit axes, fraction/point percentage alignment and legacy undeclared measures", () => {
    const columns: ChartColumn[] = spec.columns.map((column) => column.type === "numeric" ? { ...column, header: `Measure ${column.key}`, percent_shape_unknown: true, format: undefined, percent_scale: undefined, unit: "USD" } : column);
    expect(checkKindUnits({ ...spec, columns }, "line")).toEqual([]);
    expect(checkKindUnits({ ...spec, columns: columns.map((column) => ({ ...column, unit: undefined })) }, "line")).toEqual([]);
    expect(checkKindUnits({ ...spec, columns: columns.map((column) => column.type === "numeric" ? { ...column, percent_shape_unknown: undefined, unit: "%", format: "percent", percent_scale: column.key === "rate" ? "fraction" : "points" } : column) }, "line")).toEqual([]);
    expect(checkKindUnits({ ...spec, columns: columns.map((column) => column.key === "visits" ? { ...column, unit: undefined } : column) }, "line")).toHaveLength(1);
  });

  it.each(["scatter", "pie", "heatmap", "waterfall", "sankey", "kpi", "table", "geo"] as const)("keeps separate coordinates or single-measure encodings for %s", (kind) => {
    expect(checkKindUnits(spec, kind)).toEqual([]);
  });
});

/** Kinds covered by builder (table/kpi use dedicated components, bypassing buildChartJSConfig). */
const BUILT_KINDS: ChartSpec["kind"][] = [
  "line", "area_stacked", "bar", "stacked_bar", "hbar",
  "dual_axis", "combo", "pie", "scatter", "waterfall", "sankey", "heatmap",
];

type ColType = ChartColumn["type"];

/** Build spec from column-type combinations; shape checks use only types/counts, ignoring values. */
function specOf(types: ColType[], nRows = 6): ChartSpec {
  const cols: ChartColumn[] = types.map((t, i) => ({
    key: `c${i}`,
    header: `c${i}`,
    type: t,
  }));
  const rows = Array.from({ length: nRows }, (_, r) => {
    const row: Record<string, string | number | null> = {};
    types.forEach((t, i) => {
      row[`c${i}`] = t === "numeric" ? r + 1 : t === "date" ? `2026-0${(r % 9) + 1}` : `v${r}`;
    });
    return row;
  });
  return { kind: "bar", title: "t", columns: cols, rows, row_count: nRows };
}

/** Shape combinations covering every branch (including geo/date versus categorical; three builders
 *  differ on what counts as a dimension column, the easiest criterion to copy incorrectly). */
const SHAPES: ColType[][] = [
  ["categorical", "numeric"],
  ["categorical", "numeric", "numeric"],
  ["categorical", "categorical", "numeric"],
  ["categorical", "categorical", "numeric", "numeric"],
  ["date", "numeric"],
  ["date", "numeric", "numeric"],
  ["date", "categorical", "numeric"],
  ["date", "date", "numeric"],
  ["geo", "numeric"],
  ["geo", "numeric", "numeric"],
  ["geo", "geo", "numeric"],
  ["numeric"],
  ["numeric", "numeric"],
  ["numeric", "numeric", "numeric"],
  ["categorical"],
  ["categorical", "categorical"],
  ["date"],
];

describe("kind-shape 与 builder 逐个一致", () => {
  it("★★ 每个 (形状 × 图种) 的判断都必须与 buildChartJSConfig 抛不抛一致", () => {
    const mismatches: string[] = [];
    for (const types of SHAPES) {
      const spec = specOf(types);
      for (const kind of BUILT_KINDS) {
        const claims = canBuildKind(spec, kind);
        let builds = true;
        let err = "";
        try {
          buildChartJSConfig({ ...spec, kind }, THEME);
        } catch (e) {
          builds = false;
          err = (e as Error).message;
        }
        if (claims !== builds) {
          mismatches.push(
            `[${types.join("+")}] ${kind}: kind-shape 说 ${claims ? "画得出" : "画不出"}，` +
              `builder ${builds ? "建成了" : `抛了「${err}」`}`,
          );
        }
      }
    }
    expect(mismatches, mismatches.join("\n")).toEqual([]);
  });

  it("正对照：至少真有一批组合是画不出的（否则上面那条恒真）", () => {
    let cannot = 0;
    for (const types of SHAPES) {
      for (const kind of BUILT_KINDS) {
        if (!canBuildKind(specOf(types), kind)) cannot += 1;
      }
    }
    expect(cannot).toBeGreaterThan(20);
  });
});

describe("checkKindShape 返回的是缺口，不是布尔", () => {
  it("★ 单数值列选 dual_axis → 说清楚差一个数值列", () => {
    const gap = checkKindShape(specOf(["categorical", "numeric"]), "dual_axis");
    expect(gap).not.toBeNull();
    expect(gap!.needNumeric).toBe(1);
    expect(gap!.needDims).toBe(0);
    expect(gap!.counts.numeric).toBe(1);
  });

  it("★ 纯数值表选 pie → 差的是分类列", () => {
    const gap = checkKindShape(specOf(["numeric", "numeric"]), "pie");
    expect(gap).not.toBeNull();
    expect(gap!.needDims).toBe(1);
    expect(gap!.needNumeric).toBe(0);
  });

  it("★ pie 不认 date 当分类列（builder 里就是这么写的）", () => {
    expect(canBuildKind(specOf(["date", "numeric"]), "pie")).toBe(false);
    // bar accepts it: same spec, different kind criteria; expose this existing behavior explicitly.
    expect(canBuildKind(specOf(["date", "numeric"]), "bar")).toBe(true);
  });

  it("★ heatmap/sankey 认 date 当维度列、却不认 geo（pickAxes 的口径）", () => {
    expect(canBuildKind(specOf(["date", "categorical", "numeric"]), "heatmap")).toBe(true);
    expect(canBuildKind(specOf(["geo", "geo", "numeric"]), "heatmap")).toBe(false);
    // Control: bar accepts geo.
    expect(canBuildKind(specOf(["geo", "geo", "numeric"]), "bar")).toBe(true);
  });

  it("table / kpi 不经 builder，永远画得出", () => {
    expect(canBuildKind(specOf(["numeric"]), "table")).toBe(true);
    expect(canBuildKind(specOf(["numeric"]), "kpi")).toBe(true);
  });

  it("形状够了就返回 null", () => {
    expect(checkKindShape(specOf(["categorical", "numeric"]), "bar")).toBeNull();
    expect(checkKindShape(specOf(["categorical", "numeric", "numeric"]), "dual_axis")).toBeNull();
  });
});

describe("shapeCounts", () => {
  it("按列类型数数，date/geo/categorical 分开", () => {
    const c = shapeCounts(specOf(["date", "geo", "categorical", "numeric", "numeric"]));
    expect(c).toMatchObject({ date: 1, geo: 1, categorical: 1, numeric: 2 });
  });
});


it("keeps unsafe integer tokens in Table instead of enabling lossy chart math", () => {
  const spec: ChartSpec = {
    kind: "bar", title: "Exact amounts", row_count: 2,
    columns: [{ key: "category", header: "Category", type: "categorical" }, { key: "value", header: "Amount", type: "numeric" }],
    rows: [{ category: "A", value: "9007199254740993" }, { category: "B", value: "9007199254740995" }],
  };
  expect(canBuildKind(spec, "table")).toBe(true);
  expect(checkKindShape(spec, "bar")?.unsafeIntegerKeys).toEqual(["value"]);
  expect(() => buildChartJSConfig(spec, THEME)).toThrow("use Table");
});
