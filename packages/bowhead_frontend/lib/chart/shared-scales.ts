import { createContext } from "react";
import type { ChartSpec } from "./types";
import { dualAxisColumns } from "./normalize-chart-for-rendering";

export interface SharedScaleInput { view_ids: string[]; axis: "x" | "y" | "y1" }
export interface SharedScale extends SharedScaleInput {
  measure_keys: string[];
  encoding: ChartSpec["kind"];
  domain: [number, number];
  scope_digest: string;
}
export interface AxisDomains {
  encoding: ChartSpec["kind"];
  domains: Partial<Record<"x" | "y" | "y1", [number, number]>>;
}
export const ChartSharedScaleContext = createContext<AxisDomains | null>(null);
const kinds = new Set(["line", "bar", "hbar", "combo", "dual_axis", "scatter", "stacked_bar", "area_stacked"]);
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);

export function validSharedScales(value: unknown): value is SharedScale[] {
  if (!Array.isArray(value) || !value.length || value.length > 3) return false;
  const used = new Set<string>();
  return value.every((group) => {
    if (!object(group) || Object.keys(group).sort().join(",") !== "axis,domain,encoding,measure_keys,scope_digest,view_ids" || !["x", "y", "y1"].includes(String(group.axis)) || !kinds.has(String(group.encoding))) return false;
    if (!Array.isArray(group.view_ids) || group.view_ids.length < 2 || group.view_ids.length > 6 || group.view_ids.some((id) => typeof id !== "string") || new Set(group.view_ids).size !== group.view_ids.length) return false;
    if (!Array.isArray(group.measure_keys) || !group.measure_keys.length || group.measure_keys.length > 12 || group.measure_keys.some((key) => typeof key !== "string" || !key) || new Set(group.measure_keys).size !== group.measure_keys.length) return false;
    if (!Array.isArray(group.domain) || group.domain.length !== 2 || group.domain.some((value) => typeof value !== "number" || !Number.isFinite(value)) || group.domain[0] >= group.domain[1] || typeof group.scope_digest !== "string" || !/^[a-f0-9]{64}$/.test(group.scope_digest)) return false;
    for (const id of group.view_ids) { const key = JSON.stringify([id, group.axis]); if (used.has(key)) return false; used.add(key); }
    return true;
  });
}

export function sharedDomainsForView(groups: readonly SharedScale[], viewId: string): AxisDomains | null {
  const selected = groups.filter((group) => group.view_ids.includes(viewId));
  return selected.length ? { encoding: selected[0].encoding, domains: Object.fromEntries(selected.map((group) => [group.axis, group.domain])) } : null;
}

/** Validate display edits before the existing PM/CAS path; numeric data stays canonical. */
export function sharedScaleAcceptsEdit(groups: readonly SharedScale[], viewId: string, chart: ChartSpec): boolean {
  return groups.filter((group) => group.view_ids.includes(viewId)).every((group) => {
    if (chart.kind !== group.encoding || chart.percent_stack) return false;
    const numeric = chart.columns.filter((column) => column.type === "numeric");
    const split = dualAxisColumns(chart.columns);
    const columns = chart.kind === "scatter" ? [numeric[group.axis === "x" ? 0 : 1]]
      : chart.kind === "dual_axis" ? group.axis === "y" ? split.bars : split.lines : numeric;
    return columns.length === group.measure_keys.length && columns.every((column, index) => column?.key === group.measure_keys[index]);
  });
}
