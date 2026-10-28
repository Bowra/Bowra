import type { ChartSpec } from "./types";
import type { FocusBinding, TypedCell } from "./linked-focus";

export interface TimeRange { start: number; end: number }
export const MAX_WINDOW_OBSERVATIONS = 1200;

/** Derive options from retained observations, never inferred gaps or display labels. */
export function timeWindowDomain(views: ChartSpec[], binding: FocusBinding): Array<string | number> | null {
  if (binding.kind !== "time" || !binding.time_key || !binding.observation_keys.includes(binding.time_key)) return null;
  if (views.some((spec) => !spec.columns.some((column) => column.key === binding.time_key && column.type === "date") || new Set((spec.rows ?? []).map((row) => JSON.stringify(binding.observation_keys.map((key) => row[key])))).size !== spec.rows?.length)) return null;
  const values: TypedCell[] = views.flatMap((spec) => (spec.rows ?? []).map((row) => row[binding.time_key!]));
  if (!values.length) return null;
  if (binding.order === "numeric") {
    if (!values.every((value) => typeof value === "number" && Number.isInteger(value) && value >= 1000 && value <= 9999)) return null;
  } else if (binding.order === "date") {
    if (!values.every((value) => typeof value === "string" && /^\d{4}-\d{2}(?:-\d{2})?$/.test(value))) return null;
    const strings = values as string[];
    if (new Set(strings.map((value) => value.length)).size !== 1 || strings.some((value) => {
      const day = value.length === 7 ? `${value}-01` : value;
      const parsed = new Date(`${day}T00:00:00Z`);
      return !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== day;
    })) return null;
  } else return null;
  const unique = [...new Set(values)] as Array<string | number>;
  if (unique.length > MAX_WINDOW_OBSERVATIONS) return null;
  return unique.sort((a, b) => typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b)));
}

export function validTimeRange(range: TimeRange, domain: Array<string | number>): boolean {
  return Number.isSafeInteger(range.start) && Number.isSafeInteger(range.end) && range.start >= 0 && range.end < domain.length && range.start <= range.end;
}

/** Preserve original cells and child grain; filtering performs no aggregate or new calculation. */
export function applyTimeWindow(spec: ChartSpec, binding: FocusBinding, domain: Array<string | number>, range: TimeRange): ChartSpec {
  if (!validTimeRange(range, domain) || !binding.time_key) return spec;
  const selected = new Set(domain.slice(range.start, range.end + 1));
  const rows = (spec.rows ?? []).filter((row) => selected.has(row[binding.time_key!] as string | number));
  return { ...spec, rows, row_count: rows.length };
}

/** Accept only an exact displayed prepared key; never snap to a nearby date. */
export function exactTimeIndex(text: string, domain: Array<string | number>): number {
  return domain.findIndex((key) => String(key) === text.trim());
}
