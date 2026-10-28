/** Prepared analytical bindings shared by native Table, Line and export. */
import type { ChartColumn, ChartSpec } from "./types";
import { tupleKey, type FocusBinding, type TypedCell } from "./linked-focus";
import { formatObservedValue, formatDisplayValue, resolveDisplayMagnitude, resolvePercentScale, resolvePercentColumn, formatPercentCell, formatObservedPercentCell, axisUnitOf, formatDateLabel } from "./format";

export type AnalyticalRole = "observed" | "reference" | "primary" | "current" | "optimistic" | "baseline" | "pessimistic";
export interface AnalyticalRange { lower_key: string; upper_key: string; kind: "scenario" | "estimate" | "confidence"; note_ids: string[]; origin_key?: string; coverage_key?: string }
export interface AnalyticalInput {
  view_id: string; preset: "interval_rows" | "series_plot"; binding_id?: string;
  range?: AnalyticalRange; row_keys?: string[]; group_key?: string;
  points?: Array<{ key: string; role: "primary" | "current" | "reference" }>;
  detail_keys?: string[]; x_key?: string; x_kind?: "numeric" | "date"; value_key?: string; series_key?: string;
  facet?: { key: string; order?: TypedCell[] }; series_roles?: Array<{ series_value: TypedCell; role: AnalyticalRole; note_ids?: string[] }>;
  summary_keys?: string[];
}
export interface AnalyticalView extends AnalyticalInput {
  availability: "available"; binding_id: string;
  binding: { observation_keys: string[]; measure_keys: string[]; value_domain: [number, number]; x_domain?: [number, number]; series_values?: TypedCell[]; group_values?: TypedCell[] };
}
export type ProjectedAnalyticalView = AnalyticalView | { view_id: string; availability: "unavailable" };
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const closed = (v: Record<string, unknown>, keys: string[]) => Object.keys(v).every((k) => keys.includes(k));
const keys = (v: unknown, max: number, min = 1): v is string[] => Array.isArray(v) && v.length >= min && v.length <= max && v.every((k) => typeof k === "string" && !!k) && new Set(v).size === v.length;
const scalar = (v: unknown) => typeof v === "string" || (typeof v === "number" && Number.isFinite(v) && (!Number.isInteger(v) || Number.isSafeInteger(v)));
const domain = (v: unknown) => Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === "number" && Number.isFinite(n)) && v[0] < v[1];
const values = (v: unknown, max: number) => Array.isArray(v) && v.length > 0 && v.length <= max && v.every(scalar) && new Set(v.map((n) => JSON.stringify(n))).size === v.length;
export function validAnalyticalViews(value: unknown): value is ProjectedAnalyticalView[] {
  if (!Array.isArray(value) || !value.length || value.length > 6) return false;
  const ids = new Set<string>();
  return value.every((v) => {
    if (!object(v) || typeof v.view_id !== "string" || !v.view_id || ids.has(v.view_id)) return false;
    ids.add(v.view_id);
    if (v.availability === "unavailable") return closed(v, ["view_id", "availability"]);
    const common = ["view_id", "preset", "binding_id", "binding", "range", "availability"];
    const fields = v.preset === "interval_rows" ? ["row_keys", "group_key", "points", "detail_keys"] : v.preset === "series_plot" ? ["x_key", "x_kind", "value_key", "series_key", "facet", "series_roles", "summary_keys"] : null;
    if (!fields || !closed(v, [...common, ...fields]) || v.availability !== "available" || typeof v.binding_id !== "string" || !object(v.binding)) return false;
    const b = v.binding;
    if (!closed(b, ["observation_keys", "measure_keys", "value_domain", "x_domain", "series_values", "group_values"]) || !keys(b.observation_keys, 6) || !keys(b.measure_keys, 12) || !domain(b.value_domain)) return false;
    if (v.range !== undefined) {
      const r = v.range;
      if (!object(r) || !closed(r, ["lower_key", "upper_key", "kind", "note_ids", "origin_key", "coverage_key"]) || !keys([r.lower_key, r.upper_key], 2, 2) || !["scenario", "estimate", "confidence"].includes(String(r.kind)) || !keys(r.note_ids, 3)) return false;
      if ((r.kind === "confidence") !== (typeof r.coverage_key === "string") || ["origin_key", "coverage_key"].some((k) => k in r && (typeof r[k] !== "string" || !r[k]))) return false;
    }
    if (v.preset === "interval_rows") {
      if (!keys(v.row_keys, 6) || !v.range || (v.detail_keys !== undefined && !keys(v.detail_keys, 6, 0))) return false;
      if (v.group_key !== undefined && (typeof v.group_key !== "string" || !values(b.group_values, 1200))) return false;
      if (v.points !== undefined && (!Array.isArray(v.points) || v.points.length > 3 || !v.points.every((p) => object(p) && closed(p, ["key", "role"]) && typeof p.key === "string" && ["primary", "current", "reference"].includes(String(p.role))) || new Set(v.points.map((p) => p.key)).size !== v.points.length || new Set(v.points.map((p) => p.role)).size !== v.points.length)) return false;
    } else {
      if (!keys([v.x_key, v.value_key, v.series_key], 3, 3) || !["numeric", "date"].includes(String(v.x_kind)) || !domain(b.x_domain) || !values(b.series_values, 12)) return false;
      if (v.summary_keys !== undefined && !keys(v.summary_keys, 2, 0)) return false;
      if (v.facet !== undefined && (!object(v.facet) || !closed(v.facet, ["key", "order"]) || typeof v.facet.key !== "string" || !values(b.group_values, 6) || (v.facet.order !== undefined && !values(v.facet.order, 6)))) return false;
      if (v.series_roles !== undefined && (!Array.isArray(v.series_roles) || v.series_roles.length > 12 || !v.series_roles.every((r) => object(r) && closed(r, ["series_value", "role", "note_ids"]) && scalar(r.series_value) && ["observed", "reference", "primary", "current", "optimistic", "baseline", "pessimistic"].includes(String(r.role)) && (r.note_ids === undefined || keys(r.note_ids, 3))))) return false;
    }
    return true;
  });
}

export function analyticalBinding(spec: ChartSpec): FocusBinding | null {
  const a = spec._export_analytical;
  if (!a) return null;
  return { kind: "row", availability: "available", dimension_keys: a.binding.observation_keys, observation_keys: a.binding.observation_keys,
    measure_keys: a.binding.measure_keys, eligible_views: [a.view_id], coverage: { artifact_rows: { available: spec.rows?.length ?? 0, total: spec.row_count ?? null }, views: { [a.view_id]: { retained_rows: spec.rows?.length ?? 0 } } } };
}
export function analyticalX(value: TypedCell | undefined, kind: "numeric" | "date"): number {
  return kind === "numeric" ? Number(value) : Date.parse(String(value) + (String(value).length === 4 ? "-01-01" : String(value).length === 7 ? "-01" : "") + "T00:00:00Z");
}
/** Calendar-aligned ticks avoid duplicate month labels on daily observations. */
export function analyticalDateTicks(domain: [number, number], granularity: number): number[] {
  const index = (value: number) => {
    const date = new Date(value);
    return granularity === 4 ? date.getUTCFullYear() : granularity === 7 ? date.getUTCFullYear() * 12 + date.getUTCMonth() : Math.floor(value / 86400000);
  };
  const instant = (value: number) => {
    if (granularity === 10) return value * 86400000;
    const date = new Date(0);
    date.setUTCFullYear(granularity === 4 ? value : Math.floor(value / 12), granularity === 4 ? 0 : ((value % 12) + 12) % 12, 1);
    return date.getTime();
  };
  let first = index(domain[0]);
  if (instant(first) < domain[0]) first += 1;
  const last = index(domain[1]), step = Math.max(1, Math.ceil((last - first + 1) / 6));
  return Array.from({ length: Math.max(0, Math.floor((last - first) / step) + 1) }, (_, i) => instant(first + i * step));
}
export function analyticalValue(spec: ChartSpec, key: string, value: TypedCell | undefined, locale: string, exact = true): string {
  if (value == null) return "—";
  const c = spec.columns.find((column) => column.key === key);
  if (c?.type === "date") return formatDateLabel(String(value), locale);
  if (typeof value !== "number" || !c) return String(value);
  const percent = resolvePercentColumn(c) ? resolvePercentScale((spec.rows ?? []).map((row) => row[key]), c) : undefined;
  if (percent) return exact ? formatObservedPercentCell(value, percent, c.display) : formatPercentCell(value, percent, c.display);
  const unit = axisUnitOf(c);
  const display = resolveDisplayMagnitude([value], unit, c.display, c.format);
  return exact || !display ? formatObservedValue(value, unit, c.format, true, c.display, c.currency_code) : formatDisplayValue(value, display);
}
export function intervalColumn(spec: ChartSpec, label: string): ChartColumn {
  let key = "__interval";
  while (spec.columns.some((column) => column.key === key)) key += "_";
  return { key, header: label, type: "categorical" };
}
export interface AnalyticalTableEntry { key: string; row?: Record<string, TypedCell>; group?: TypedCell }
export function groupedEntries(rows: Record<string, TypedCell>[], profile?: AnalyticalView): AnalyticalTableEntry[] {
  if (!profile) return rows.map((row, index) => ({ key: String(index), row }));
  const groups = profile.group_key ? profile.binding.group_values! : [null];
  return groups.flatMap((group) => {
    const children = profile.group_key ? rows.filter((row) => tupleKey([row[profile.group_key!]]) === tupleKey([group])) : rows;
    return [...(profile.group_key && children.length ? [{ key: `group:${tupleKey([group])}`, group }] : []), ...children.map((row) => ({ key: `row:${tupleKey(profile.binding.observation_keys.map((key) => row[key]))}`, row }))];
  });
}
/** Domain coordinates remain identical for the header and every graphical cell. */
export function intervalPosition(value: number, domain: [number, number], width = 320): number { return 12 + (value - domain[0]) / (domain[1] - domain[0]) * (width - 24); }
