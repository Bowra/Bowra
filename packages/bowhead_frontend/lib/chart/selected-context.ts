import type { ChartSpec } from "./types";
import type { FocusBinding, FocusSelection, TypedCell } from "./linked-focus";
import { tupleKey } from "./linked-focus";
import type { FrameBinding } from "./prepared-frames";

export interface ChartSelectedContext {
  version: 1;
  sectionId: string;
  sectionRev: number;
  viewId: string;
  bindingId: string;
  datasetDigest: string;
  tuples?: TypedCell[][];
  window?: { lower: string | number; upper: string | number };
  frameKey?: string | number;
  measureKeys: string[];
}
export type ChartFollowupScope = Omit<ChartSelectedContext, "tuples" | "measureKeys">;

const cell = (value: unknown): value is TypedCell => value === null || typeof value === "string" || (typeof value === "number" && Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value)));
const id = (value: unknown, max: number) => typeof value === "string" && value.length > 0 && value.length <= max;

/** The same closed wire is used by drafts, Send, history and recovery. */
export function canonicalizeChartSelectedContext(value: unknown): ChartSelectedContext | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  if (Object.keys(v).some((key) => !["version", "sectionId", "sectionRev", "viewId", "bindingId", "datasetDigest", "tuples", "window", "frameKey", "measureKeys"].includes(key)) || v.version !== 1 || !id(v.sectionId, 128) || !id(v.viewId, 80) || !id(v.bindingId, 128) || !Number.isSafeInteger(v.sectionRev) || (v.sectionRev as number) < 1 || typeof v.datasetDigest !== "string" || !/^[a-f0-9]{64}$/.test(v.datasetDigest)) return null;
  if (!Array.isArray(v.measureKeys) || !v.measureKeys.length || v.measureKeys.length > 12 || !v.measureKeys.every((key) => id(key, 512)) || new Set(v.measureKeys).size !== v.measureKeys.length) return null;
  if (v.tuples !== undefined && (!Array.isArray(v.tuples) || !v.tuples.length || v.tuples.length > 20 || !v.tuples.every((tuple) => Array.isArray(tuple) && tuple.length >= 1 && tuple.length <= 6 && tuple.every(cell)))) return null;
  if (v.window !== undefined) {
    if (!v.window || typeof v.window !== "object" || Array.isArray(v.window)) return null;
    const window = v.window as Record<string, unknown>;
    if (Object.keys(window).length !== 2 || !cell(window.lower) || window.lower === null || !cell(window.upper) || window.upper === null) return null;
  }
  if (v.frameKey !== undefined && (!cell(v.frameKey) || v.frameKey === null || v.tuples !== undefined || v.window !== undefined)) return null;
  if (v.tuples === undefined && v.window === undefined && v.frameKey === undefined) return null;
  const wire = JSON.stringify(v);
  if (new TextEncoder().encode(wire).length > 2000) return null;
  return JSON.parse(wire) as ChartSelectedContext;
}

/** Reuse the existing observation grain; transformed aggregates have no selector. */
export function selectedContextForView(scope: ChartFollowupScope | undefined, binding: FocusBinding | FrameBinding | undefined, selection: FocusSelection | null, spec: ChartSpec): ChartSelectedContext | null {
  if (!scope || !binding || binding.availability !== "available" || !["table", "line", "bar", "hbar", "scatter", "dual_axis", "combo"].includes(spec.kind) || spec._view_folded) return null;
  if (binding.kind === "frame") {
    if (scope.frameKey === undefined || selection || !binding.frame_keys.includes(scope.frameKey) || !(spec.rows ?? []).length || spec.rows!.some((row) => row[binding.time_key] !== scope.frameKey)) return null;
    return canonicalizeChartSelectedContext({ ...scope, measureKeys: spec.columns.filter((column) => column.type === "numeric" && binding.measure_keys.includes(column.key)).map((column) => column.key) });
  }
  if (scope.frameKey !== undefined) return null;
  const matching = selection ? (spec.rows ?? []).filter((row) => tupleKey(binding.dimension_keys.map((key) => row[key])) === tupleKey(selection.tuple)) : [];
  if (selection && (selection.bindingId !== scope.bindingId || !matching.length)) return null;
  if (!selection && !scope.window) return null;
  return canonicalizeChartSelectedContext({ ...scope, ...(selection ? { tuples: matching.map((row) => binding.observation_keys.map((key) => row[key])) } : {}), measureKeys: spec.columns.filter((column) => column.type === "numeric" && binding.measure_keys.includes(column.key)).map((column) => column.key) });
}


/** One observation, read the same way as the observation-details heading: missing cells show "—". */
export function observationTupleLabel(tuple: readonly TypedCell[]): string {
  return tuple.map((value) => value ?? "—").join(" · ");
}

/** A selected or inspected range between two observations. */
export function observationRangeLabel(lower: string | number, upper: string | number): string {
  return `${lower} – ${upper}`;
}

function localizedList(items: readonly string[], locale: string | undefined): string {
  if (items.length < 2) return items[0] ?? "";
  try {
    return new Intl.ListFormat(locale, { style: "long", type: "conjunction" }).format(items);
  } catch {
    // Unsupported locale tag or missing Intl.ListFormat: keep every value readable.
    return items.join(", ");
  }
}

/**
 * The readable selected scope shared by the composer chip and the follow-up prefill.
 * Several observations become a localized list; a prepared frame reads as its frame value.
 */
export function chartSelectedScopeLabel(scope: ChartSelectedContext, locale?: string): string {
  if (scope.frameKey !== undefined) return String(scope.frameKey);
  if (scope.tuples) return localizedList([...new Set(scope.tuples.map(observationTupleLabel))], locale);
  return scope.window ? observationRangeLabel(scope.window.lower, scope.window.upper) : "";
}
