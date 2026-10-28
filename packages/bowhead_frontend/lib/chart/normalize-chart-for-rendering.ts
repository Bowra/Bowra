import { quantityUnitOf } from "./format";
import { CHART_STYLE_PRESENTATION_POLICY } from "./chart-style-registry";
import { compareDateAxisValues } from "./date-order";
import { pivotLongSpec, transposeTimeWideSpec, type CellOrigins } from "./pivot-long";
import { roleOrderedDims } from "./stack-dims";
import type { ChartColumn, ChartKind, ChartSpec } from "./types";

export type ChartFamily =
  | "trend"
  | "comparison"
  | "composition"
  | "distribution"
  | "relationship"
  | "flow"
  | "matrix"
  | "table";

export type NormalizedChartSeriesRole = "line" | "bar" | "point" | "area" | "segment";

export interface NormalizedChartRenderShapeV1 {
  version: "chart-shape/1";
  kind: ChartKind;
  family: ChartFamily;
  rowCount: number;
  categoryCount: number;
  seriesCount: number;
  longestLabelGraphemes: number;
  series: Array<{
    key: string;
    role: NormalizedChartSeriesRole;
    pointCount: number;
    stackKey: string | null;
    hasPositive: boolean;
    hasNegative: boolean;
  }>;
  barStackEdges?: {
    positiveOuterSeriesIndexByCategory: Array<number | null>;
    negativeOuterSeriesIndexByCategory: Array<number | null>;
  };
  flags: {
    stacked: boolean;
    crossesZero: boolean;
    hasMissingValues: boolean;
  };
}

export interface NormalizedChartForRenderingV1 {
  spec: ChartSpec;
  shape: NormalizedChartRenderShapeV1;
  origins?: CellOrigins;
}

type Cell = string | number | null;
type Row = Record<string, Cell>;

const DATE_AXIS_KINDS = new Set<ChartKind>([
  "line",
  "area_stacked",
  "bar",
  "stacked_bar",
  "dual_axis",
  "combo",
  "waterfall",
]);

function dateAxisColumn(spec: ChartSpec): ChartColumn | undefined {
  if (!DATE_AXIS_KINDS.has(spec.kind)) return undefined;
  const dimensions = spec.columns.filter((column) =>
    column.type === "categorical" || column.type === "date" || column.type === "geo"
  );
  if (spec.kind === "bar" || spec.kind === "stacked_bar" || spec.kind === "waterfall") {
    return dimensions[0]?.type === "date" ? dimensions[0] : undefined;
  }
  return dimensions.find((column) => column.type === "date");
}

function orderDateAxisRows(spec: ChartSpec): ChartSpec {
  const dateColumn = dateAxisColumn(spec);
  if (!dateColumn || !spec.rows || spec.rows.length < 2) return spec;
  return {
    ...spec,
    rows: [...spec.rows].sort((left, right) =>
      compareDateAxisValues(left[dateColumn.key], right[dateColumn.key])
    ),
  };
}

function inferColumnsFromRows(rows: ReadonlyArray<Row>): ChartColumn[] {
  const sample = rows.find((row) => row && Object.keys(row).length > 0);
  if (!sample) return [];
  return Object.keys(sample).map((key) => ({
    key,
    header: key,
    type: typeof sample[key] === "number" ? "numeric" : "categorical",
  }));
}

function graphemeLength(value: string): number {
  if (typeof Intl !== "undefined" && "Segmenter" in Intl) {
    const Segmenter = Intl.Segmenter;
    return Array.from(new Segmenter(undefined, { granularity: "grapheme" }).segment(value)).length;
  }
  return Array.from(value).length;
}

function finiteNumber(value: Cell | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function seriesEntry(
  key: string,
  role: NormalizedChartSeriesRole,
  values: ReadonlyArray<number | null>,
  stackKey: string | null,
): NormalizedChartRenderShapeV1["series"][number] {
  return {
    key,
    role,
    pointCount: values.length,
    stackKey,
    hasPositive: values.some((value) => value !== null && value > 0),
    hasNegative: values.some((value) => value !== null && value < 0),
  };
}

export function dualAxisColumns(columns: readonly ChartColumn[]): { bars: ChartColumn[]; lines: ChartColumn[] } {
  const numeric = columns.filter((column) => column.type === "numeric");
  if (numeric.some((column) => typeof column.y_axis_id === "number")) {
    const bars = numeric.filter((column) => (column.y_axis_id ?? 0) === 0);
    const lines = numeric.filter((column) => (column.y_axis_id ?? 0) === 1);
    if (bars.length > 0 && lines.length > 0) return { bars, lines };
  }
  return { bars: numeric.slice(0, 1), lines: numeric.slice(1) };
}

function barStackEdges(
  valuesBySeries: ReadonlyArray<ReadonlyArray<number | null>>,
  categoryCount: number,
): NonNullable<NormalizedChartRenderShapeV1["barStackEdges"]> {
  const positiveOuterSeriesIndexByCategory: Array<number | null> = [];
  const negativeOuterSeriesIndexByCategory: Array<number | null> = [];
  for (let categoryIndex = 0; categoryIndex < categoryCount; categoryIndex += 1) {
    let positive: number | null = null;
    let negative: number | null = null;
    valuesBySeries.forEach((values, seriesIndex) => {
      const value = values[categoryIndex];
      if (value !== null && value > 0) positive = seriesIndex;
      if (value !== null && value < 0) negative = seriesIndex;
    });
    positiveOuterSeriesIndexByCategory.push(positive);
    negativeOuterSeriesIndexByCategory.push(negative);
  }
  return { positiveOuterSeriesIndexByCategory, negativeOuterSeriesIndexByCategory };
}

function deriveSeries(spec: ChartSpec): {
  categoryCount: number;
  series: NormalizedChartRenderShapeV1["series"];
  valuesBySeries: Array<Array<number | null>>;
} {
  const rows = spec.rows ?? [];
  const numeric = spec.columns.filter((column) => column.type === "numeric");
  const dimensions = spec.columns.filter((column) =>
    column.type === "categorical" || column.type === "date" || column.type === "geo"
  );
  const values = (column: ChartColumn) => rows.map((row) => finiteNumber(row[column.key]));

  if (spec.kind === "pie") {
    const metric = numeric[0];
    const perSegment = rows.map((row, index) =>
      seriesEntry(`segment:${index}`, "segment", [metric ? finiteNumber(row[metric.key]) : null], null)
    );
    return {
      categoryCount: rows.length,
      series: perSegment,
      valuesBySeries: perSegment.map((_, index) => [metric ? finiteNumber(rows[index]?.[metric.key]) : null]),
    };
  }

  if (spec.kind === "scatter") {
    const [xColumn, yColumn] = numeric;
    // Identity columns include geo, matching buildScatter; otherwise provincial scatter counts here as
    // one series (showLegend disabled), while the builder creates five provincial datasets:
    // five chart colors with no matching legend, worse than a uniform color.
    const groupColumn = dimensions.find(
      (column) => column.type === "categorical" || column.type === "geo",
    );
    if (!xColumn || !yColumn) return { categoryCount: rows.length, series: [], valuesBySeries: [] };
    if (!groupColumn) {
      const valid = rows.filter((row) => finiteNumber(row[xColumn.key]) !== null && finiteNumber(row[yColumn.key]) !== null);
      const yValues = valid.map((row) => finiteNumber(row[yColumn.key]));
      return {
        categoryCount: valid.length,
        series: [seriesEntry("point:0", "point", yValues, null)],
        valuesBySeries: [yValues],
      };
    }
    const groups = new Map<string, Array<number | null>>();
    for (const row of rows) {
      if (finiteNumber(row[xColumn.key]) === null || finiteNumber(row[yColumn.key]) === null) continue;
      const key = String(row[groupColumn.key] ?? "");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(finiteNumber(row[yColumn.key]));
    }
    const valuesBySeries = [...groups.values()];
    return {
      categoryCount: valuesBySeries.reduce((total, group) => total + group.length, 0),
      series: valuesBySeries.map((group, index) => seriesEntry(`point:${index}`, "point", group, null)),
      valuesBySeries,
    };
  }

  if (spec.kind === "stacked_bar" && dimensions.length >= 2 && numeric.length > 0) {
    const [xColumn, groupColumn] = roleOrderedDims(dimensions);
    const metric = numeric.find((column) => column.key === spec.sort_by) ?? numeric[0]!;
    // Deduplicate with order-preserving Set (O(rows)), same fix as server chart_style_presentation._derive_series:
    // Previously categories.includes cost rows × categories; rendering stalls now that charts carry all rows (up to 10,000).
    const categories: string[] = [];
    const groups: string[] = [];
    const seenCategories = new Set<string>();
    const seenGroups = new Set<string>();
    const cells = new Map<string, number>();
    for (const row of rows) {
      const category = String(row[xColumn!.key] ?? "");
      const group = String(row[groupColumn!.key] ?? "");
      if (!seenCategories.has(category)) {
        seenCategories.add(category);
        categories.push(category);
      }
      if (!seenGroups.has(group)) {
        seenGroups.add(group);
        groups.push(group);
      }
      const key = `${category}\u0000${group}`;
      cells.set(key, (cells.get(key) ?? 0) + (finiteNumber(row[metric.key]) ?? 0));
    }
    const valuesBySeries = groups.map((group) => categories.map((category) => cells.get(`${category}\u0000${group}`) ?? 0));
    return {
      categoryCount: categories.length,
      series: valuesBySeries.map((group, index) => seriesEntry(`bar:${index}`, "bar", group, "stack:0")),
      valuesBySeries,
    };
  }

  if (spec.kind === "dual_axis") {
    const split = dualAxisColumns(spec.columns);
    const ordered = [
      ...split.bars.map((column) => ({ column, role: "bar" as const })),
      ...split.lines.map((column) => ({ column, role: "line" as const })),
    ];
    const valuesBySeries = ordered.map(({ column }) => values(column));
    return {
      categoryCount: rows.length,
      series: ordered.map(({ column, role }, index) => seriesEntry(`${role}:${index}`, role, values(column), null)),
      valuesBySeries,
    };
  }

  if (spec.kind === "combo") {
    const valuesBySeries = numeric.map((column) => values(column));
    return {
      categoryCount: rows.length,
      series: numeric.map((_column, index) =>
        seriesEntry(`${index === 0 ? "bar" : "line"}:${index}`, index === 0 ? "bar" : "line", valuesBySeries[index]!, null)
      ),
      valuesBySeries,
    };
  }

  if (spec.kind === "waterfall") {
    const metricValues = numeric[0] ? values(numeric[0]) : [];
    const total = metricValues.reduce<number>((sum, value) => sum + (value ?? 0), 0);
    const valuesBySeries = [[...metricValues, total]];
    return {
      categoryCount: rows.length + 1,
      series: [seriesEntry("bar:0", "bar", valuesBySeries[0]!, null)],
      valuesBySeries,
    };
  }

  if (spec.kind === "heatmap" || spec.kind === "sankey") {
    const metricValues = numeric[0] ? values(numeric[0]) : [];
    return {
      categoryCount: rows.length,
      series: [seriesEntry("segment:0", "segment", metricValues, null)],
      valuesBySeries: [metricValues],
    };
  }

  const role: NormalizedChartSeriesRole = spec.kind === "area_stacked"
    ? "area"
    : spec.kind === "line"
      ? "line"
      : "bar";
  const stacked = spec.kind === "area_stacked" || spec.kind === "stacked_bar";
  const valuesBySeries = numeric.map((column) => values(column));
  return {
    categoryCount: rows.length,
    series: numeric.map((_column, index) =>
      seriesEntry(`${role}:${index}`, role, valuesBySeries[index]!, stacked ? "stack:0" : null)
    ),
    valuesBySeries,
  };
}

/** Derive the renderer-only shape from an already normalized temporary spec. */
export function deriveNormalizedChartRenderShapeV1(spec: ChartSpec): NormalizedChartRenderShapeV1 {
  const rows = spec.rows ?? [];
  const dimensions = spec.columns.filter((column) =>
    column.type === "categorical" || column.type === "date" || column.type === "geo"
  );
  const numeric = spec.columns.filter((column) => column.type === "numeric");
  const analytical = spec._export_analytical?.preset === "series_plot" ? spec._export_analytical : null;
  const derived = analytical ? {
    categoryCount: new Set(rows.map((row) => JSON.stringify(row[analytical.x_key!]))).size,
    series: analytical.binding.series_values!.map((value) => {
      const values = rows.filter((row) => row[analytical.series_key!] === value).map((row) => finiteNumber(row[analytical.value_key!]));
      return { key: `analytical:${JSON.stringify(value)}`, role: "line" as const, pointCount: values.length, stackKey: null, hasPositive: values.some((v) => v !== null && v > 0), hasNegative: values.some((v) => v !== null && v < 0) };
    }),
    valuesBySeries: analytical.binding.series_values!.map((value) => rows.filter((row) => row[analytical.series_key!] === value).map((row) => finiteNumber(row[analytical.value_key!]))),
  } : deriveSeries(spec);
  const stacked = spec.kind === "area_stacked" || spec.kind === "stacked_bar";
  const allSeriesValues = derived.valuesBySeries.flat();
  const hasPositive = allSeriesValues.some((value) => value !== null && value > 0);
  const hasNegative = allSeriesValues.some((value) => value !== null && value < 0);
  const shape: NormalizedChartRenderShapeV1 = {
    version: CHART_STYLE_PRESENTATION_POLICY.renderShapeVersion,
    kind: spec.kind,
    family: CHART_STYLE_PRESENTATION_POLICY.familiesByKind[spec.kind],
    rowCount: rows.length,
    categoryCount: derived.categoryCount,
    seriesCount: derived.series.length,
    longestLabelGraphemes: rows.reduce((longest, row) => Math.max(
      longest,
      ...dimensions.map((column) => graphemeLength(String(row[column.key] ?? ""))),
    ), 0),
    series: derived.series,
    flags: {
      stacked,
      crossesZero: hasPositive && hasNegative,
      hasMissingValues: rows.some((row) => numeric.some((column) => finiteNumber(row[column.key]) === null)),
    },
  };
  if (stacked && derived.series.some((series) => series.role === "bar")) {
    shape.barStackEdges = barStackEdges(derived.valuesBySeries, derived.categoryCount);
  }
  return shape;
}

/** Project only proven compatible quantities on a shared axis. Source rows and
 * stored metadata remain untouched; unknown/different quantities use the existing fallback. */
function normalizeAxisQuantities(spec: ChartSpec): ChartSpec {
  if (!["line", "bar", "hbar", "combo", "dual_axis", "stacked_bar", "area_stacked"].includes(spec.kind)) return spec;
  const numeric = spec.columns.filter((column) => column.type === "numeric");
  const split = dualAxisColumns(spec.columns);
  const groups = spec.kind === "dual_axis" ? [split.bars, split.lines] : [numeric];
  const projected = new Map<string, { factor: number; unit: string }>();
  for (const columns of groups) {
    const quantities = columns.map(quantityUnitOf);
    const identity = quantities[0]?.identity;
    if (columns.length < 2 || !identity || quantities.some((q) => q.identity !== identity)) continue;
    if (new Set(columns.map((column) => column.unit)).size <= 1) continue;
    const unit = quantities.find((q) => q.exponent === 0)?.base ?? quantities[0]!.base!;
    columns.forEach((column, i) => projected.set(column.key, { factor: 10 ** quantities[i]!.exponent, unit }));
  }
  if (!projected.size) return spec;
  return { ...spec,
    columns: spec.columns.map((column) => projected.has(column.key) ? { ...column, unit: projected.get(column.key)!.unit } : column),
    rows: spec.rows?.map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [key, typeof value === "number" && projected.has(key) ? value * projected.get(key)!.factor : value]))),
  };
}

/** The sole render normalization entry; temporary projection never mutates the caller. */
export function normalizeChartForRenderingV1(rawSpec: ChartSpec, opts: { trackOrigins?: boolean; scopeSource?: ChartSpec } = {}): NormalizedChartForRenderingV1 {
  let spec = rawSpec;
  if (!spec.columns || spec.columns.length === 0) {
    spec = { ...spec, columns: inferColumnsFromRows(spec.rows ?? []) };
  }
  if (spec._export_analytical) return { spec, shape: deriveNormalizedChartRenderShapeV1(spec) };
  const pivot = pivotLongSpec(spec, opts.trackOrigins, opts.scopeSource ?? spec);
  spec = pivot?.spec ?? spec;
  const transpose = transposeTimeWideSpec(spec, opts.trackOrigins, pivot?.origins);
  spec = transpose?.spec ?? spec;
  spec = normalizeAxisQuantities(orderDateAxisRows(spec));
  const origins = transpose?.origins ?? pivot?.origins;
  return { spec, shape: deriveNormalizedChartRenderShapeV1(spec), ...(origins ? { origins } : {}) };
}
