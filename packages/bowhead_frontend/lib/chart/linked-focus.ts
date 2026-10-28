import { validFrameBinding, type FrameInteraction } from "./prepared-frames";
import type { ChartData, Plugin } from "chart.js";
import type { ChartColumn, ChartSpec } from "./types";
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import type { CellOrigins } from "./pivot-long";

export type TypedCell = string | number | null;
export interface FocusBinding {
  kind: "row" | "entity" | "time";
  availability: "available";
  dimension_keys: string[];
  observation_keys: string[];
  measure_keys: string[];
  time_key?: string;
  order?: "date" | "numeric";
  eligible_views: string[];
  coverage: { artifact_rows: { available: number; total: number | null }; views: Record<string, { retained_rows: number; top_n?: number; series_shown?: number; series_total?: number; points_shown?: number; points_total?: number }> };
}
export interface FocusInteraction { preset: "linked_focus"; from_view: string; to_views: string[]; binding_id: string }
export interface TimeWindowInteraction { preset: "time_window"; view_ids: string[]; binding_id: string }
export type NativeInteraction = FocusInteraction | TimeWindowInteraction | FrameInteraction;
export interface FocusSelection { bindingId: string; tuple: TypedCell[] }
export interface MarkBinding {
  focusTuple: TypedCell[];
  observations: Array<{ datasetDigest: string; keyTuple: TypedCell[]; measureKey: string; value: number | null }>;
  representation: "original";
}
export const focusKinds = new Set(["table", "line", "bar", "hbar", "scatter", "dual_axis", "combo"]);
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const keys = (value: unknown, max: number): value is string[] => Array.isArray(value) && value.length > 0 && value.length <= max && value.every((key) => typeof key === "string" && !!key) && new Set(value).size === value.length;

export function validProjectedFocus(plan: Record<string, unknown>): boolean {
  if (!object(plan.view_bindings) || !Array.isArray(plan.interactions) || plan.interactions.length < 1 || plan.interactions.length > 3 || !object(plan.bindings)) return false;
  const used: Record<string, string> = Object.create(null), sources = new Set<string>();
  let windows = 0, players = 0;
  for (const interaction of plan.interactions) {
    if (!object(interaction) || typeof interaction.binding_id !== "string" || !interaction.binding_id) return false;
    let participants: string[];
    if (interaction.preset === "linked_focus" && Object.keys(interaction).sort().join() === "binding_id,from_view,preset,to_views") {
      if (typeof interaction.from_view !== "string" || sources.has(interaction.from_view) || !keys(interaction.to_views, 5) || interaction.to_views.includes(interaction.from_view)) return false;
      sources.add(interaction.from_view);
      participants = [interaction.from_view, ...interaction.to_views];
    } else if (interaction.preset === "time_window" && Object.keys(interaction).sort().join() === "binding_id,preset,view_ids") {
      if (++windows > 1 || !keys(interaction.view_ids, 6) || interaction.view_ids.length < 2) return false;
      participants = interaction.view_ids;
    } else if (interaction.preset === "frame_player" && Object.keys(interaction).sort().join() === "binding_id,preset,static_frame,view_ids") {
      if (++players > 1 || !keys(interaction.view_ids, 6) || (typeof interaction.static_frame !== "string" && (typeof interaction.static_frame !== "number" || !Number.isFinite(interaction.static_frame)))) return false;
      participants = interaction.view_ids;
    } else return false;
    for (const view of participants) {
      if (plan.view_bindings[view] !== interaction.binding_id || (used[view] && used[view] !== interaction.binding_id)) return false;
      used[view] = interaction.binding_id;
    }
  }
  if (Object.keys(used).length < (players ? 1 : 2) || Object.keys(used).length > 6 || Object.keys(plan.view_bindings).length !== Object.keys(used).length || Object.keys(plan.bindings).length !== new Set(Object.values(used)).size) return false;
  for (const [bid, descriptor] of Object.entries(plan.bindings)) {
    if (!Object.values(used).includes(bid) || !object(descriptor) || !["row", "entity", "time", "frame"].includes(String(descriptor.kind))) return false;
    if (plan.interactions.some((interaction) => object(interaction) && interaction.preset === "time_window" && interaction.binding_id === bid) && descriptor.kind !== "time") return false;
    const player = plan.interactions.find((item) => object(item) && item.preset === "frame_player" && item.binding_id === bid);
    if (!!player !== (descriptor.kind === "frame")) return false;
    if (descriptor.availability === "unavailable") {
      if (Object.keys(descriptor).sort().join() !== "availability,kind") return false;
      continue;
    }
    if (descriptor.kind === "frame") {
      const participants = Object.entries(used).filter(([, id]) => id === bid).map(([view]) => view);
      if (!validFrameBinding(descriptor, participants) || !object(player) || !(descriptor.frame_keys as Array<string | number>).includes(player.static_frame as string | number) || (plan.interactions as unknown[]).some((item) => object(item) && item.binding_id === bid && item.preset !== "frame_player")) return false;
      continue;
    }
    const fields = ["availability", "coverage", "dimension_keys", "eligible_views", "kind", "measure_keys", "observation_keys", ...(descriptor.kind === "time" ? ["order", "time_key"] : [])].sort().join();
    if (descriptor.availability !== "available" || Object.keys(descriptor).sort().join() !== fields || !keys(descriptor.dimension_keys, 6) || !keys(descriptor.observation_keys, 6) || !keys(descriptor.measure_keys, 12) || !keys(descriptor.eligible_views, 6)) return false;
    if (descriptor.kind === "time" && (typeof descriptor.time_key !== "string" || descriptor.dimension_keys.length !== 1 || descriptor.dimension_keys[0] !== descriptor.time_key || !descriptor.observation_keys.includes(descriptor.time_key) || !["date", "numeric"].includes(String(descriptor.order)))) return false;
    const observationKeys = descriptor.observation_keys;
    if (descriptor.dimension_keys.some((key) => !observationKeys.includes(key))) return false;
    const participants = Object.entries(used).filter(([, id]) => id === bid).map(([view]) => view);
    if (participants.length !== descriptor.eligible_views.length || participants.some((view) => !(descriptor.eligible_views as string[]).includes(view)) || !object(descriptor.coverage)) return false;
    const coverage = descriptor.coverage;
    if (Object.keys(coverage).sort().join() !== "artifact_rows,views" || !object(coverage.artifact_rows) || Object.keys(coverage.artifact_rows).sort().join() !== "available,total" || !Number.isInteger(coverage.artifact_rows.available) || (coverage.artifact_rows.available as number) < 0 || (coverage.artifact_rows.total !== null && (!Number.isInteger(coverage.artifact_rows.total) || (coverage.artifact_rows.total as number) < (coverage.artifact_rows.available as number))) || !object(coverage.views) || Object.keys(coverage.views).length !== participants.length) return false;
    for (const view of participants) {
      const counts = coverage.views[view];
      if (!object(counts) || !Number.isInteger(counts.retained_rows) || Object.keys(counts).some((key) => !["retained_rows", "top_n", "series_shown", "series_total", "points_shown", "points_total"].includes(key)) || Object.values(counts).some((count) => !Number.isInteger(count) || (count as number) < 0) || (counts.retained_rows as number) > (coverage.artifact_rows.available as number)) return false;
      if (plan.interactions.some((interaction) => object(interaction) && interaction.preset === "time_window" && Array.isArray(interaction.view_ids) && interaction.view_ids.includes(view)) && (counts.retained_rows !== coverage.artifact_rows.available || (counts.retained_rows as number) > 1200 || counts.top_n || ["points", "series"].some((key) => ((counts[`${key}_shown`] ?? 0) as number) < ((counts[`${key}_total`] ?? 0) as number)))) return false;
    }
  }
  return true;
}

export function focusTuple(row: Record<string, TypedCell>, binding: FocusBinding): TypedCell[] { return binding.dimension_keys.map((key) => row[key]); }
export function tupleKey(tuple: TypedCell[]): string { return JSON.stringify(tuple); }
export function sameFocus(row: Record<string, TypedCell>, binding: FocusBinding, selection: FocusSelection | null): boolean {
  return !!selection && tupleKey(focusTuple(row, binding)) === tupleKey(selection.tuple);
}

/** Validate exact source grain before mapping native render coordinates. */
export function observationViewEligible(spec: ChartSpec, binding: FocusBinding): boolean {
  const analytical = spec._export_analytical;
  if (analytical) return spec.kind === (analytical.preset === "interval_rows" ? "table" : "line") &&
    tupleKey(binding.observation_keys) === tupleKey(analytical.binding.observation_keys) &&
    tupleKey(binding.measure_keys) === tupleKey(analytical.binding.measure_keys) &&
    (spec.rows ?? []).every((row) => binding.observation_keys.every((key) => row[key] !== null && row[key] !== undefined)) &&
    new Set((spec.rows ?? []).map((row) => tupleKey(binding.observation_keys.map((key) => row[key])))).size === spec.rows?.length;
  const dimensions = spec.columns.filter((column) => column.type !== "numeric").map((column) => column.key);
  const rows = spec.rows ?? [], numeric = spec.columns.filter((column) => column.type === "numeric");
  if (!focusKinds.has(spec.kind) || !rows.length || spec._view_folded || JSON.stringify(dimensions) !== JSON.stringify(binding.observation_keys) || !numeric.length || numeric.some((column) => !binding.measure_keys.includes(column.key)) || rows.some((row) => dimensions.some((key) => !(key in row))) || new Set(rows.map((row) => tupleKey(binding.observation_keys.map((key) => row[key])))).size !== rows.length) return false;
  if (spec.kind === "table") return true;
  if (dimensions.some((key) => new Set(rows.map((row) => String(row[key] ?? ""))).size !== new Set(rows.map((row) => tupleKey([row[key]]))).size)) return false;
  if (spec.kind === "scatter") return numeric.length >= 2;
  if (dimensions.length === 1) return true;
  const normalized = normalizeChartForRenderingV1(spec, { trackOrigins: true });
  return !!normalized.origins && new Set(normalized.spec.columns.map((column) => column.key)).size === normalized.spec.columns.length;
}

/** The normalizer moves source cells with values; labels and array positions are never identities. */
export function markBindings(spec: ChartSpec, data: ChartData, binding: FocusBinding, digest: string, rendered = spec, origins?: CellOrigins): MarkBinding[][] {
  const dimensions = spec.columns.filter((column) => column.type !== "numeric").map((column) => column.key);
  if (JSON.stringify(dimensions) !== JSON.stringify(binding.observation_keys) || (spec.rows ?? []).some((row) => dimensions.some((key) => !(key in row))) || new Set((spec.rows ?? []).map((row) => tupleKey(dimensions.map((key) => row[key])))).size !== spec.rows?.length) return [];
  if (!observationViewEligible(spec, binding) && !origins) return [];
  const rows = rendered.rows ?? [];
  return data.datasets.map((dataset) => {
    const source = dataset as typeof dataset & { _xCol?: ChartColumn; _yCol?: ChartColumn };
    const measures = [source._xCol, source._yCol].filter((column): column is ChartColumn => !!column);
    return dataset.data.map((point, index) => {
      const originIndex = point && typeof point === "object" && "_sourceRowIndex" in point ? point._sourceRowIndex as number : index;
      const row = rows[originIndex];
      const cells = row ? measures.flatMap((column) => origins ? origins.get(row)?.get(column.key) ?? [] : [{ row, measureKey: column.key }]) : [];
      const tuple = cells[0] ? binding.observation_keys.map((key) => cells[0].row[key]) : [];
      if (!cells.length || cells.length !== measures.length || cells.some((cell) => tupleKey(binding.observation_keys.map((key) => cell.row[key])) !== tupleKey(tuple) || !binding.measure_keys.includes(cell.measureKey))) return { focusTuple: [], observations: [], representation: "original" };
      return {
        focusTuple: focusTuple(cells[0]!.row, binding),
        observations: cells.map(({ row: original, measureKey }) => ({ datasetDigest: digest, keyTuple: tuple, measureKey, value: typeof original[measureKey] === "number" && Number.isFinite(original[measureKey]) ? original[measureKey] as number : null })),
        representation: "original",
      };
    });
  });
}

/** Highlight keeps the native geometry, series colors, legend and exact-value hover intact. */
export function focusHighlightPlugin(read: () => { marks: MarkBinding[][]; selection: FocusSelection | null; ink: string; annotations?: Array<{ tuples: TypedCell[][]; measureKey: string; label: string }>; font?: string; surface?: string }): Plugin {
  return { id: "nativeLinkedFocus", afterDatasetsDraw(chart) {
    const { marks, selection, ink, annotations = [], font, surface } = read();
    if (!selection && !annotations.length) return;
    const targets = annotations.map((item) => ({ ...item, keys: new Set(item.tuples.map(tupleKey)) }));
    const labeled = new Set<number>();
    const labelBoxes: Array<{ left: number; top: number; width: number }> = [];
    const ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = ink; ctx.lineWidth = 2;
    chart.data.datasets.forEach((_, datasetIndex) => {
      if (!chart.isDatasetVisible(datasetIndex)) return;
      chart.getDatasetMeta(datasetIndex).data.forEach((element, index) => {
        const mark = marks[datasetIndex]?.[index];
        if (!mark?.observations.some((observation) => observation.value !== null)) return;
        const matched = targets.findIndex((item) => item.keys.has(tupleKey(mark.focusTuple)) && mark.observations.some((observation) => observation.measureKey === item.measureKey && observation.value !== null));
        if (matched < 0 && (!selection || tupleKey(mark.focusTuple) !== tupleKey(selection.tuple))) return;
        const position = element.getProps(["x", "y", "width", "height", "base", "horizontal"], true) as { x: number; y: number; width?: number; height?: number; base?: number; horizontal?: boolean };
        if (!Number.isFinite(position.x) || !Number.isFinite(position.y)) return;
        if (position.width && position.height && position.base !== undefined) {
          const x = position.horizontal ? (position.x + position.base) / 2 : position.x;
          const y = position.horizontal ? position.y : (position.y + position.base) / 2;
          ctx.strokeRect(x - position.width / 2 - 2, y - position.height / 2 - 2, position.width + 4, position.height + 4);
        }
        else { ctx.beginPath(); ctx.arc(position.x, position.y, 8, 0, Math.PI * 2); ctx.stroke(); }
        if (matched >= 0 && !labeled.has(matched)) {
          labeled.add(matched);
          ctx.font = `500 11px ${font || "sans-serif"}`;
          let text = targets[matched].label;
          const area = chart.chartArea, maxWidth = Math.max(0, area.right - area.left - 8);
          while (text.length && ctx.measureText(text).width > maxWidth) text = Array.from(text).slice(0, -1).join("");
          if (!text) return;
          if (text !== targets[matched].label) text = `${Array.from(text).slice(0, -1).join("")}…`;
          const width = ctx.measureText(text).width;
          const left = Math.max(area.left + 2, Math.min(position.x + 12, area.right - width - 4));
          const initialTop = Math.max(area.top + 2, Math.min(position.y - 24, area.bottom - 18));
          const top = [initialTop, ...Array.from({ length: 6 }, (_, i) => area.top + 2 + i * 20)].find((candidate) => candidate + 16 <= area.bottom && !labelBoxes.some((box) => left < box.left + box.width + 4 && left + width + 4 > box.left && candidate < box.top + 20 && candidate + 20 > box.top));
          // Full captions remain below the figure when the plot cannot fit a label safely.
          if (top === undefined) return;
          labelBoxes.push({ left, top, width });
          ctx.fillStyle = surface || "white"; ctx.fillRect(left - 2, top - 1, width + 4, 16);
          ctx.fillStyle = ink; ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.fillText(text, left, top);
        }
      });
    });
    ctx.restore();
  } };
}
