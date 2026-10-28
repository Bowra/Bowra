import type { ChartSpec } from "./types";
import { tupleKey, type TypedCell } from "./linked-focus";
import { validFrameBinding, type FrameBinding } from "./prepared-frames";
import { resolvePercentColumn, resolvePercentScale } from "./format";

export interface TableTrendInput { view_id: string; binding_id: string; measure_key: string }
export type TableTrend = TableTrendInput & { binding: Omit<FrameBinding, "availability">; availability: "available" };
export type ProjectedTableTrend = TableTrend | { view_id: string; availability: "unavailable" };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);

export function validTableTrends(value: unknown): value is ProjectedTableTrend[] {
  if (!Array.isArray(value) || !value.length || value.length > 3) return false;
  const views = new Set<string>();
  return value.every((item) => {
    if (!object(item) || typeof item.view_id !== "string" || !item.view_id || views.has(item.view_id)) return false;
    views.add(item.view_id);
    if (item.availability === "unavailable") return Object.keys(item).sort().join() === "availability,view_id";
    return Object.keys(item).sort().join() === "availability,binding,binding_id,measure_key,view_id" && item.availability === "available" &&
      typeof item.binding_id === "string" && !!item.binding_id && typeof item.measure_key === "string" && object(item.binding) &&
      !("availability" in item.binding) && validFrameBinding({ ...item.binding, availability: "available" }, [item.view_id]) &&
      (item.binding.measure_keys as string[]).includes(item.measure_key) && (item.binding.axis_domains as Record<string, unknown>)[item.view_id] === null;
  });
}

export interface TrendSeries { entity: TypedCell; values: Array<number | null>; labels: Array<string | number> }
export interface TrendTable { spec: ChartSpec; series: Map<string, TrendSeries>; domain: [number, number] }

/** Compare the same supplied endpoints across entities, never first/last nonmissing values. */
export function buildTrendTable(spec: ChartSpec, labels: { start: string; end: string; change: string; trend: string; points: string }): TrendTable | null {
  const trend = spec._export_table_trend;
  if (!trend || spec.kind !== "table" || spec.row_count !== spec.rows?.length) return null;
  const { binding, measure_key: measure } = trend;
  const column = spec.columns.find((column) => column.key === measure && column.type === "numeric");
  const entity = spec.columns.find((column) => column.key === binding.entity_keys[0]);
  if (!column || !entity) return null;
  const periods = binding.frame_keys.filter((period) => spec.rows!.some((row) => row[binding.time_key] === period));
  if (!periods.length) return null;
  const groups = new Map<string, Map<string, Record<string, TypedCell>>>();
  for (const row of spec.rows!) {
    const key = tupleKey([row[entity.key]]), time = tupleKey([row[binding.time_key]]);
    const group = groups.get(key) ?? new Map();
    if (group.has(time) || !binding.frame_keys.includes(row[binding.time_key] as string | number)) return null;
    group.set(time, row); groups.set(key, group);
  }
  const percentage = resolvePercentColumn(column) && !column.percent_shape_unknown;
  const scale = resolvePercentScale(spec.rows!.map((row) => row[measure]), column);
  const series = new Map<string, TrendSeries>();
  let low = Infinity, high = -Infinity;
  const rows = binding.entity_values.filter((value) => groups.has(tupleKey([value]))).map((value) => {
    const key = tupleKey([value]), group = groups.get(key)!;
    const values = periods.map((period) => {
      const raw = group.get(tupleKey([period]))?.[measure];
      const value = typeof raw === "number" && Number.isFinite(raw) ? raw : null;
      if (value !== null) { low = Math.min(low, value); high = Math.max(high, value); }
      return value;
    });
    const start = values[0], end = values[values.length - 1];
    const change = start !== null && end !== null ? endpointDifference(end, start, percentage && scale === "fraction" ? 2 : 0) : null;
    series.set(key, { entity: value, values, labels: periods });
    return { __trend_entity: value, __trend_start: start, __trend_end: end, __trend_change: change, __trend_series: key };
  });
  return { spec: { ...spec, columns: [
    { ...entity, key: "__trend_entity" },
    { ...column, key: "__trend_start", header: `${labels.start} · ${periods[0]}` },
    { ...column, key: "__trend_end", header: `${labels.end} · ${periods[periods.length - 1]}` },
    { ...column, key: "__trend_change", header: labels.change, ...(percentage ? { format: "float" as const, unit: labels.points, percent_scale: undefined, percent_shape_unknown: true, source_header: labels.change } : {}) },
    { key: "__trend_series", header: labels.trend, type: "categorical" },
  ], rows, row_count: rows.length, _export_annotations: undefined }, series, domain: Number.isFinite(low) ? [low, high] : [0, 1] };
}

/** Subtract supplied decimal representations without adding binary subtraction tails. */
function endpointDifference(end: number, start: number, exponentOffset: number): number | null {
  const parts = (value: number) => {
    const [mantissa, exponent = "0"] = String(value).split("e");
    const fraction = mantissa.split(".")[1]?.length ?? 0;
    return { integer: BigInt(mantissa.replace(".", "")), exponent: Number(exponent) - fraction };
  };
  const a = parts(end), b = parts(start), exponent = Math.min(a.exponent, b.exponent);
  const difference = a.integer * 10n ** BigInt(a.exponent - exponent) - b.integer * 10n ** BigInt(b.exponent - exponent);
  const value = Number(`${difference}e${exponent + exponentOffset}`);
  return Number.isFinite(value) ? value : null;
}

/** Gaps break the line. Positions follow elapsed time, not an evenly spaced rank. */
export function trendGeometry(series: TrendSeries, domain: [number, number]): { path: string; points: Array<[number, number]> } {
  const times = series.labels.map((value) => typeof value === "number" ? value : Date.parse(`${value.length === 7 ? value + "-01" : value}T00:00:00Z`));
  const width = times[times.length - 1] - times[0];
  const [low, high] = domain;
  const points: Array<[number, number]> = [];
  let connected = false;
  const path = series.values.map((value, i) => {
    if (value === null) { connected = false; return ""; }
    const x = width ? 4 + 104 * (times[i] - times[0]) / width : 56;
    const y = high === low ? 16 : 28 - 24 * (value - low) / (high - low);
    points.push([x, y]);
    const segment = `${connected ? "L" : "M"}${x},${y}`; connected = true; return segment;
  }).join(" ");
  return { path, points };
}
