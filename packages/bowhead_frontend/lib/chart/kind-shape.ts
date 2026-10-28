/**
 * **Sole** criterion for whether these data can render this chart kind (chart-config-ux ⑦).
 *
 * ## Why this file exists
 *
 * Two implementations answered the same question: each build-config.ts builder's initial guards (authoritative,
 * because throwing determines chart versus fallback table), and chart-block.tsx
 * _shapeKinds (for switcher). They evolved independently, unaware of the other's criteria.
 *
 * Observed in 116 corpus (1873 real charts × ten inspection kinds):
 * **26.9% of combinations hit "图表配置生成失败"**, with dual_axis/combo/scatter at 64–66% each.
 * Builder errors were precise (dual_axis requires x + ≥2 numeric), but users saw
 * only "图表配置生成失败，先保留原始数据", presenting **data-shape mismatch** as
 * **system failure**. Choosing pie yielded failure, rather than an explanation that these data cannot form pie.
 *
 * ## Boundaries
 *
 * Answer only **can it render**, not **should it render**. Recommendation belongs to backend infer.py;
 * frontend _shapeKinds also retains that recommendation layer, correctly **narrower** than this
 * (hbar can render 244 date points, but should not be recommended). Keep these questions separate.
 *
 * ## Ensuring builder parity
 *
 * Use kind-shape.corpus.test.ts rather than trust manual copying: run each
 * (spec, kind) from actual 116 corpus; decisions here must match buildChartJSConfig throwing **case by case**.
 * Changing builder guards without updating this must fail that test.
 */
import type { ChartSpec } from "./types";
import { quantityUnitOf, isUnsafeIntegerCell } from "./format";
import { dualAxisColumns, normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";

export interface ValueAxisUnitGap {
  axis: "x" | "y" | "y1";
  measureKeys: string[];
  units: Array<string | null>;
}

/** Check actual native measure assignments, including pivoting and the existing dual-axis split. */
export function checkKindUnits(rawSpec: ChartSpec, kind: ChartSpec["kind"]): ValueAxisUnitGap[] {
  const sharedAxisKinds = ["line", "bar", "hbar", "stacked_bar", "area_stacked", "combo", "dual_axis"];
  if (!sharedAxisKinds.includes(kind)) return [];
  // Normalization cannot introduce a second unit when every supplied measure already has the same unit.
  if (new Set((rawSpec.columns ?? []).filter((column) => column.type === "numeric").map((column) => quantityUnitOf(column).identity)).size <= 1) return [];
  const spec = normalizeChartForRenderingV1({ ...rawSpec, kind }).spec;
  const numeric = spec.columns.filter((column) => column.type === "numeric");
  const groups = kind === "dual_axis"
    ? (() => {
        const { bars, lines } = dualAxisColumns(spec.columns);
        return [{ axis: "y" as const, columns: bars }, { axis: "y1" as const, columns: lines }];
      })()
    : [{ axis: kind === "hbar" ? "x" as const : "y" as const, columns:
        kind === "stacked_bar" && spec.columns.filter((column) => column.type === "categorical" || column.type === "geo").length >= 2
          ? [numeric.find((column) => column.key === spec.sort_by) ?? numeric[0]!].filter(Boolean)
          : numeric }];
  return groups.flatMap(({ axis, columns }) => {
    const units = columns.map((column) => quantityUnitOf(column).identity ?? null);
    // Preserve legacy axes with no declared units. A known unit must never impersonate an undeclared measure.
    if (columns.length <= 1 || units.every((unit) => unit === null) || new Set(units).size <= 1) return [];
    return [{ axis, measureKeys: columns.map((column) => column.key), units }];
  });
}

export interface ShapeCounts {
  date: number;
  categorical: number;
  geo: number;
  numeric: number;
  rows: number;
}

export function shapeCounts(spec: ChartSpec): ShapeCounts {
  const cols = spec.columns ?? [];
  return {
    date: cols.filter((c) => c.type === "date").length,
    categorical: cols.filter((c) => c.type === "categorical").length,
    geo: cols.filter((c) => c.type === "geo").length,
    numeric: cols.filter((c) => c.type === "numeric").length,
    rows: (spec.rows ?? []).length,
  };
}

/**
 * Required shape for each kind.
 *
 * ⚠ Copy groups from builder **code**, not error copy; several mismatches exist:
 *  - waterfall/sankey/heatmap use pickAxes(), counting **date as dimensions**
 *    but **excluding geo**; bar/line accept geo, pie excludes date. All errors say
 *    N categorical columns, though three accepted sets differ.
 *  - bar's stacked branch uses catCols[0] separately, but requires catCols≥2, a stricter subset
 *    with no extra impact on renderability.
 * These are existing behaviors, **only exposed explicitly here**, without unifying them; changing criteria changes
 * renderability, a separate task.
 */
type Need = { dims: number; dimTypes: Array<keyof ShapeCounts>; numeric: number };

const NEEDS: Partial<Record<ChartSpec["kind"], Need>> = {
  line: { dims: 1, dimTypes: ["date", "categorical", "geo"], numeric: 1 },
  area_stacked: { dims: 1, dimTypes: ["date", "categorical", "geo"], numeric: 1 },
  bar: { dims: 1, dimTypes: ["date", "categorical", "geo"], numeric: 1 },
  stacked_bar: { dims: 1, dimTypes: ["date", "categorical", "geo"], numeric: 1 },
  hbar: { dims: 1, dimTypes: ["date", "categorical", "geo"], numeric: 1 },
  dual_axis: { dims: 1, dimTypes: ["date", "categorical", "geo"], numeric: 2 },
  combo: { dims: 1, dimTypes: ["date", "categorical", "geo"], numeric: 2 },
  pie: { dims: 1, dimTypes: ["categorical", "geo"], numeric: 1 },
  scatter: { dims: 0, dimTypes: [], numeric: 2 },
  waterfall: { dims: 1, dimTypes: ["categorical", "date"], numeric: 1 },
  sankey: { dims: 2, dimTypes: ["categorical", "date"], numeric: 1 },
  heatmap: { dims: 2, dimTypes: ["categorical", "date"], numeric: 1 },
  // table / kpi bypass buildChartJSConfig (dedicated components), always renderable.
};

export interface ShapeGap {
  kind: ChartSpec["kind"];
  /** Missing dimension-column count (0 = sufficient dimensions). */
  needDims: number;
  /** Column types usable as dimensions. */
  dimTypes: Array<keyof ShapeCounts>;
  /** Missing numeric-column count (0 = sufficient numeric columns). */
  needNumeric: number;
  counts: ShapeCounts;
  unsafeIntegerKeys?: string[];
  signedShare?: boolean;
}

/**
 * Can these data render this kind? Return null if yes, otherwise **the missing requirements**.
 *
 * Return gaps rather than boolean so callers can explain what is missing;
 * boolean only says no, exactly the problem with today's "图表配置生成失败" notice.
 */
export function checkKindShape(spec: ChartSpec, kind: ChartSpec["kind"]): ShapeGap | null {
  const need = NEEDS[kind];
  if (!need) return null;
  const counts = shapeCounts(spec);
  const unsafeIntegerKeys = spec.columns.filter((column) => column.type === "numeric"
    && (spec.rows ?? []).some((row) => isUnsafeIntegerCell(row[column.key]))).map((column) => column.key);
  if (unsafeIntegerKeys.length) return { kind, needDims: 0, dimTypes: need.dimTypes, needNumeric: 0, counts, unsafeIntegerKeys };
  const measure = spec.columns.find((column) => column.type === "numeric");
  if (kind === "pie" && measure && (spec.rows ?? []).some((row) => typeof row[measure.key] === "number" && Number(row[measure.key]) < 0)) {
    return { kind, needDims: 0, dimTypes: need.dimTypes, needNumeric: 0, counts, signedShare: true };
  }
  const haveDims = need.dimTypes.reduce((n, t) => n + counts[t], 0);
  const needDims = Math.max(0, need.dims - haveDims);
  const needNumeric = Math.max(0, need.numeric - counts.numeric);
  if (!needDims && !needNumeric) return null;
  return { kind, needDims, dimTypes: need.dimTypes, needNumeric, counts };
}

/** Boolean wrapper for checkKindShape. */
export function canBuildKind(spec: ChartSpec, kind: ChartSpec["kind"]): boolean {
  return checkKindShape(spec, kind) === null;
}
