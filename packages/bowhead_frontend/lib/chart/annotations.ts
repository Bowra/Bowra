import type { ChartSpec } from "./types";
import type { FocusBinding, TypedCell } from "./linked-focus";
import { markBindings, tupleKey } from "./linked-focus";
import { axisUnitOf, formatObservedPercentCell, formatObservedValue, resolvePercentColumn, resolvePercentScale } from "./format";
import type { ChartData, Plugin } from "chart.js";
import type { ChartTheme } from "../chart-theme";
import { observedAnnotationLayoutPlugin } from "./annotation-layout";

interface AnnotationBase {
  id: string;
  view_id: string;
  binding_id: string;
  measure_key: string;
  label?: string;
  note_ids?: string[];
  role?: "observation" | "event" | "coverage";
}
export type AnnotationInput = AnnotationBase &
  ({ kind: "point"; tuple: TypedCell[] } | { kind: "interval"; lower: string | number; upper: string | number });
export type ObservationAnnotation = AnnotationInput & { observation_keys: string[]; encoding: ChartSpec["kind"]; availability: "available" };
export type ProjectedAnnotation = ObservationAnnotation | { id: string; view_id: string; availability: "unavailable" };
export type ObservationTarget = { measureKey: string; annotationId?: string } &
  ({ kind: "point"; tuple: TypedCell[] } | { kind: "interval"; lower: string | number; upper: string | number });

export function annotationTarget(item: ObservationAnnotation): ObservationTarget {
  return { measureKey: item.measure_key, annotationId: item.id, ...(item.kind === "point"
    ? { kind: "point", tuple: item.tuple } as const
    : { kind: "interval", lower: item.lower, upper: item.upper } as const) };
}

/** Resolve original cells again on every read; pins never retain copied source values. */
export function observationRows(target: ObservationTarget, spec: ChartSpec, binding: FocusBinding): ChartSpec["rows"] {
  if (!binding.measure_keys.includes(target.measureKey) || !spec.columns.some((column) => column.key === target.measureKey && column.type === "numeric")) return [];
  if (target.kind === "point") return (spec.rows ?? []).filter((row) => tupleKey(binding.observation_keys.map((key) => row[key])) === tupleKey(target.tuple));
  if (binding.observation_keys.length !== 1) return [];
  const key = binding.observation_keys[0];
  const rows = spec.rows ?? [];
  if (![target.lower, target.upper].every((bound) => rows.some((row) => row[key] === bound))) return [];
  return rows.filter((row) => typeof row[key] === typeof target.lower && row[key] !== null && row[key] >= target.lower && row[key] <= target.upper);
}

/** An admitted annotation carries its own original-grain descriptor, even without linked views. */
export function annotationBinding(spec: ChartSpec): FocusBinding | null {
  const item = spec._export_annotations?.find((item) => item.encoding === spec.kind && annotationCaption(item, spec) !== null);
  return item ? {
    kind: "row", availability: "available", dimension_keys: item.observation_keys, observation_keys: item.observation_keys,
    measure_keys: spec.columns.filter((column) => column.type === "numeric").map((column) => column.key), eligible_views: [item.view_id],
    coverage: { artifact_rows: { available: spec.rows?.length ?? 0, total: spec.rows?.length ?? 0 }, views: {} },
  } : null;
}
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const cell = (v: unknown) =>
  v === null || typeof v === "string" || (typeof v === "number" && Number.isFinite(v) && (!Number.isInteger(v) || Number.isSafeInteger(v)));
const kinds = new Set(["table", "line", "bar", "hbar", "scatter", "dual_axis", "combo"]);

export function validAnnotations(value: unknown, version = 1): value is ProjectedAnnotation[] {
  if (!Array.isArray(value) || !value.length || value.length > 6) return false;
  const ids = new Set<string>();
  return value.every((item) => {
    if (
      !object(item) ||
      typeof item.id !== "string" ||
      !/^[a-z][a-z0-9_]{0,31}$/.test(item.id) ||
      ids.has(item.id) ||
      typeof item.view_id !== "string" ||
      !item.view_id
    )
      return false;
    ids.add(item.id);
    if (item.availability === "unavailable") return Object.keys(item).sort().join() === "availability,id,view_id";
    const fields = [
      "id",
      "view_id",
      "binding_id",
      "kind",
      "measure_key",
      "observation_keys",
      "encoding",
      "availability",
      ...(item.kind === "point" ? ["tuple"] : item.kind === "interval" ? ["lower", "upper"] : []),
      ...(item.label !== undefined ? ["label"] : []),
      ...(version >= 2 && item.note_ids !== undefined ? ["note_ids"] : []),
      ...(version >= 2 && item.role !== undefined ? ["role"] : []),
    ];
    if (
      !["point", "interval"].includes(String(item.kind)) ||
      item.availability !== "available" ||
      Object.keys(item).sort().join() !== fields.sort().join() ||
      !kinds.has(String(item.encoding)) ||
      typeof item.binding_id !== "string" ||
      !item.binding_id ||
      typeof item.measure_key !== "string" ||
      !item.measure_key ||
      !Array.isArray(item.observation_keys) ||
      item.observation_keys.length !== 1 ||
      typeof item.observation_keys[0] !== "string" ||
      !item.observation_keys[0]
    )
      return false;
    if (item.label !== undefined && (typeof item.label !== "string" || !item.label.trim() || Array.from(item.label).length > 80)) return false;
    return item.kind === "point"
      ? Array.isArray(item.tuple) && item.tuple.length === 1 && cell(item.tuple[0])
      : item.lower !== null &&
          item.upper !== null &&
          cell(item.lower) &&
          cell(item.upper) &&
          typeof item.lower === typeof item.upper &&
          (item.lower as string | number) <= (item.upper as string | number);
  });
}

export function annotationMatchesRow(item: ObservationAnnotation, row: Record<string, TypedCell>): boolean {
  const value = row[item.observation_keys[0]];
  return item.kind === "point"
    ? tupleKey([value]) === tupleKey(item.tuple)
    : typeof value === typeof item.lower && value !== null && value >= item.lower && value <= item.upper;
}

/** Reuse exact-value/unit owners; a filtered-out point is absent rather than mislabeled missing. */
export function annotationCaption(item: ObservationAnnotation, spec: ChartSpec): string | null {
  if (spec.kind !== item.encoding || !(spec.rows ?? []).some((row) => annotationMatchesRow(item, row))) return null;
  const column = spec.columns.find((column) => column.key === item.measure_key && column.type === "numeric");
  if (!column) return null;
  const unit = axisUnitOf(column);
  if (item.kind === "interval") return [item.label, column.header, `${item.lower} – ${item.upper}`, unit].filter(Boolean).join(" · ");
  const row = spec.rows!.find((row) => annotationMatchesRow(item, row))!;
  const raw = typeof row[column.key] === "number" && Number.isFinite(row[column.key]) ? (row[column.key] as number) : null;
  const value = resolvePercentColumn(column)
    ? formatObservedPercentCell(
        raw,
        resolvePercentScale(
          spec.rows!.map((row) => row[column.key]),
          column,
        ),
        column.display,
      )
    : formatObservedValue(raw, unit, column.format, true, column.display, column.currency_code);
  return [item.label, item.tuple.map((cell) => (cell === null ? "—" : String(cell))).join(), column.header, value].filter(Boolean).join(" · ");
}

export function observedAnnotationPlugin(spec: ChartSpec, data: ChartData, theme: ChartTheme): Plugin | null {
  const items = (spec._export_annotations ?? []).filter((item) => item.encoding === spec.kind && annotationCaption(item, spec) !== null);
  if (!items.length) return null;
  const keys = items[0].observation_keys;
  const binding: FocusBinding = {
    kind: "row",
    availability: "available",
    dimension_keys: keys,
    observation_keys: keys,
    measure_keys: spec.columns.filter((c) => c.type === "numeric").map((c) => c.key),
    eligible_views: [items[0].view_id],
    coverage: { artifact_rows: { available: spec.rows?.length ?? 0, total: spec.rows?.length ?? 0 }, views: {} },
  };
  const marks = markBindings(spec, data, binding, "");
  return observedAnnotationLayoutPlugin(items.map((item) => ({ ...item, label: item.label ?? spec.columns.find((column) => column.key === item.measure_key)!.header })), marks, theme);
}

/** Source-qualified time bands use original mark identities, including missing endpoints. */
export function annotationScopePlugin(spec: ChartSpec, data: ChartData, theme: ChartTheme): Plugin | null {
  const items = (spec._export_annotations ?? []).filter((item) => item.encoding === spec.kind && ["event", "coverage"].includes(item.role ?? ""));
  if (!items.length || !["line", "bar", "hbar", "dual_axis", "combo"].includes(spec.kind)) return null;
  const binding = annotationBinding(spec);
  if (!binding) return null;
  const marks = markBindings(spec, data, binding, "");
  return { id: "nativeAnnotationScope", beforeDatasetsDraw(chart) {
    const horizontal = spec.kind === "hbar", area = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.beginPath(); ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top); ctx.clip();
    for (const item of items) {
      const endpoints = item.kind === "point" ? item.tuple : [item.lower, item.upper];
      const positions = endpoints.map((value) => {
        for (let dataset = 0; dataset < marks.length; dataset++) {
          if (!chart.isDatasetVisible(dataset)) continue;
          const index = marks[dataset].findIndex((mark) => mark.focusTuple[0] === value && mark.observations.some((observation) => observation.measureKey === item.measure_key));
          if (index >= 0) return chart.getDatasetMeta(dataset).data[index]?.getProps([horizontal ? "y" : "x"], false)[horizontal ? "y" : "x"] as number | undefined;
        }
        return undefined;
      });
      if (positions.some((value) => value === undefined || !Number.isFinite(value))) continue;
      ctx.fillStyle = theme.ink; ctx.strokeStyle = theme.ink; ctx.lineWidth = 1;
      if (item.kind === "interval") {
        const lower = Math.min(...positions as number[]), upper = Math.max(...positions as number[]);
        ctx.globalAlpha = 0.07;
        if (horizontal) ctx.fillRect(area.left, lower, area.right - area.left, Math.max(1, upper - lower));
        else ctx.fillRect(lower, area.top, Math.max(1, upper - lower), area.bottom - area.top);
      } else {
        ctx.globalAlpha = 0.45; ctx.setLineDash([3, 3]); ctx.beginPath();
        if (horizontal) { ctx.moveTo(area.left, positions[0]!); ctx.lineTo(area.right, positions[0]!); }
        else { ctx.moveTo(positions[0]!, area.top); ctx.lineTo(positions[0]!, area.bottom); }
        ctx.stroke(); ctx.setLineDash([]);
      }
    }
    ctx.restore();
  } };
}
