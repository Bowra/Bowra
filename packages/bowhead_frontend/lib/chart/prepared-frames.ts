import type { ChartSpec } from "./types";
import { tupleKey, type TypedCell } from "./linked-focus";
import type { AxisDomains } from "./shared-scales";
import { timeWindowDomain } from "./time-window";

export interface FrameInteraction { preset: "frame_player"; view_ids: string[]; binding_id: string; static_frame?: string | number }
export interface FrameBinding {
  kind: "frame";
  availability: "available";
  dimension_keys: string[];
  observation_keys: string[];
  measure_keys: string[];
  time_key: string;
  entity_keys: string[];
  order: "date" | "numeric";
  frame_keys: Array<string | number>;
  entity_values: Array<string | number>;
  axis_domains: Record<string, AxisDomains | null>;
  eligible_views: string[];
  coverage: { artifact_rows: { available: number; total: number }; views: Record<string, { retained_rows: number }> };
}
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const keys = (value: unknown, max: number): value is string[] => Array.isArray(value) && !!value.length && value.length <= max && value.every((key) => typeof key === "string" && !!key) && new Set(value).size === value.length;
const cell = (value: unknown): value is string | number => typeof value === "string" || (typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER);

export function validFrameBinding(value: Record<string, unknown>, participants: string[]): boolean {
  if (Object.keys(value).sort().join() !== ["availability", "kind", "dimension_keys", "observation_keys", "measure_keys", "time_key", "entity_keys", "order", "frame_keys", "entity_values", "axis_domains", "eligible_views", "coverage"].sort().join() || value.availability !== "available") return false;
  if (!keys(value.dimension_keys, 1) || !keys(value.entity_keys, 1) || !keys(value.observation_keys, 2) || !keys(value.measure_keys, 12) || !keys(value.eligible_views, 6) || typeof value.time_key !== "string" || value.observation_keys.length !== 2 || value.dimension_keys[0] !== value.entity_keys[0] || !value.observation_keys.includes(value.time_key) || !value.observation_keys.includes(value.entity_keys[0]) || participants.length !== value.eligible_views.length || participants.some((view) => !(value.eligible_views as string[]).includes(view))) return false;
  if (!Array.isArray(value.frame_keys) || !value.frame_keys.length || value.frame_keys.length > 120 || !value.frame_keys.every(cell) || !Array.isArray(value.entity_values) || !value.entity_values.length || value.entity_values.length > 1200 || !value.entity_values.every(cell) || new Set(value.entity_values.map(String)).size !== value.entity_values.length) return false;
  const frames = value.frame_keys;
  const domain = timeWindowDomain([{ columns: [{ key: value.time_key, type: "date" }], rows: frames.map((frame) => ({ [value.time_key as string]: frame })) } as ChartSpec], { kind: "time", time_key: value.time_key, observation_keys: [value.time_key], order: value.order } as Parameters<typeof timeWindowDomain>[1]);
  if (JSON.stringify(domain) !== JSON.stringify(frames)) return false;
  if (!object(value.axis_domains) || Object.keys(value.axis_domains).length !== participants.length || !object(value.coverage)) return false;
  for (const view of participants) {
    const axes = value.axis_domains[view];
    if (axes !== null && (!object(axes) || Object.keys(axes).sort().join() !== "domains,encoding" || axes.encoding !== "scatter" || !object(axes.domains) || Object.keys(axes.domains).sort().join() !== "x,y" || Object.values(axes.domains).some((bounds) => !Array.isArray(bounds) || bounds.length !== 2 || bounds.some((v) => typeof v !== "number" || !Number.isFinite(v)) || bounds[0] >= bounds[1]))) return false;
  }
  const coverage = value.coverage;
  if (Object.keys(coverage).sort().join() !== "artifact_rows,views" || !object(coverage.artifact_rows) || Object.keys(coverage.artifact_rows).sort().join() !== "available,total" || !Number.isSafeInteger(coverage.artifact_rows.available) || (coverage.artifact_rows.available as number) < 1 || (coverage.artifact_rows.available as number) > 1200 || coverage.artifact_rows.available !== coverage.artifact_rows.total || !object(coverage.views) || Object.keys(coverage.views).length !== participants.length) return false;
  return participants.every((view) => object((coverage.views as Record<string, unknown>)[view]) && Object.keys((coverage.views as Record<string, object>)[view]).join() === "retained_rows" && (coverage.views as Record<string, { retained_rows: unknown }>)[view].retained_rows === (coverage.artifact_rows as Record<string, unknown>).available);
}

/** Pre-index existing rows once; each tick picks references without querying or aggregating. */
export function prepareFrameViews(views: Map<string, ChartSpec>, binding: FrameBinding, participants: string[]): Map<string, Map<string, ChartSpec>> | null {
  const result = new Map<string, Map<string, ChartSpec>>();
  const positions = new Map(binding.entity_values.map((value, index) => [tupleKey([value]), index]));
  for (const view of participants) {
    const spec = views.get(view);
    if (!spec || !["scatter", "table"].includes(spec.kind) || spec.row_count !== spec.rows?.length || spec.rows.length !== binding.coverage.artifact_rows.available || tupleKey(spec.columns.filter((column) => column.type !== "numeric").map((column) => column.key).sort()) !== tupleKey([...binding.observation_keys].sort())) return null;
    const slices = new Map(binding.frame_keys.map((frame) => [tupleKey([frame]), [] as Array<Record<string, TypedCell>>]));
    const seen = new Set<string>();
    for (const row of spec.rows) {
      const key = tupleKey(binding.observation_keys.map((key) => row[key]));
      const slice = slices.get(tupleKey([row[binding.time_key]]));
      if (seen.has(key) || !slice || !positions.has(tupleKey([row[binding.entity_keys[0]]]))) return null;
      seen.add(key); slice.push(row);
    }
    result.set(view, new Map([...slices].map(([key, rows]) => {
      rows.sort((a, b) => positions.get(tupleKey([a[binding.entity_keys[0]]]))! - positions.get(tupleKey([b[binding.entity_keys[0]]]))!);
      return [key, { ...spec, rows, row_count: rows.length, _export_frame_scope: { time_key: binding.time_key, key: JSON.parse(key)[0] as string | number }, columns: spec.kind === "scatter" ? spec.columns.filter((column) => column.key !== binding.time_key) : spec.columns,
        ...(spec.kind === "scatter" ? { _export_axis_domains: binding.axis_domains[view], _export_frame_entities: { key: binding.entity_keys[0], values: binding.entity_values } } : {}) }];
    })));
  }
  return result;
}
