import { pivotLongSpec } from "./pivot-long";
import type { ChartSpec } from "./types";

/**
 * Long horizontal bar charts on screen draw their categories in segments (chart display range, 2026-11-23, D4).
 *
 * ☠ 116: charts of 1,072–1,082 bars asked for a canvas of 86,640 device px, above Chrome's limit, and rendered blank;
 * 113 bars scrolled 4,592 px inside a 611 px bubble. New charts stop at 100 categories (`chart/validation.py`), so a
 * segment changes only charts above 50 — and every saved chart above the limit still becomes readable.
 *
 * A segment is a contiguous window of whole categories in the prepared order: nothing is ranked, sampled or dropped,
 * and the previous/next controls reach every category. The value axis keeps the full data's range, so a bar in the
 * second segment compares with one in the first.
 */
export const HBAR_SEGMENT_SIZE = 50;

export interface ChartSegment {
  /** Spec drawing only this segment's categories, value axis pinned to the full range. */
  spec: ChartSpec;
  /** 1-based first and last category of the segment, and the category count of the whole chart. */
  start: number;
  end: number;
  total: number;
  index: number;
  /** Identifies the chart's data: a new value means a new chart, whose segments start again at the first. */
  signature: string;
}

/** Key of the category axis: the declared `x`, the pivoted category of long data, else the first dimension. */
function categoryKey(spec: ChartSpec): string | null {
  const binding = spec.explicit_presentation;
  if (binding?.mode === "plot" && typeof binding.axes?.x === "string") return binding.axes.x;
  const pivot = pivotLongSpec(spec);
  if (pivot) return pivot.spec.columns[0]?.key ?? null;
  return (spec.columns ?? []).find((column) => column.type !== "numeric")?.key ?? null;
}

/** Categories in their first-appearance order (long data repeats a category once per series). */
export function orderedCategories(spec: ChartSpec): { key: string; categories: string[] } | null {
  const key = categoryKey(spec);
  if (!key) return null;
  const seen = new Set<string>();
  for (const row of spec.rows ?? []) seen.add(String(row[key] ?? ""));
  return { key, categories: [...seen] };
}

/** Rows of the given categories, in their original order. */
export function rowsOfCategories(spec: ChartSpec, key: string, keep: ReadonlySet<string>): NonNullable<ChartSpec["rows"]> {
  return (spec.rows ?? []).filter((row) => keep.has(String(row[key] ?? "")));
}

/** Range of the plotted values with zero included, as the bars start at zero. Null when there is no span. */
function valueDomain(spec: ChartSpec): [number, number] | null {
  const binding = spec.explicit_presentation;
  const declared = binding?.mode === "plot" && Array.isArray(binding.axes?.y) ? new Set(binding.axes.y) : null;
  const keys = (spec.columns ?? []).filter((column) => column.type === "numeric" && (!declared || declared.has(column.key))).map((column) => column.key);
  let lo = 0;
  let hi = 0;
  for (const row of spec.rows ?? []) {
    for (const key of keys) {
      const value = row[key];
      if (typeof value !== "number" || !Number.isFinite(value)) continue;
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    }
  }
  return lo < hi ? [lo, hi] : null;
}

/**
 * The `index`-th segment of a long horizontal bar chart, or null when the chart draws whole.
 *
 * Only complete charts are segmented: with fewer rows than `row_count` the remainder is not here to page to, and the
 * chart keeps its "showing the first N" disclosure. Charts with a presentation plan (annotations, linked focus) or an
 * analytical profile address their observations by row and keep drawing whole.
 */
export function hbarSegment(spec: ChartSpec, index: number, size = HBAR_SEGMENT_SIZE): ChartSegment | null {
  if (spec.kind !== "hbar" || spec.presentation_plan !== undefined || spec._export_analytical) return null;
  const rows = spec.rows ?? [];
  if (rows.length < (spec.row_count ?? rows.length)) return null;
  const ordered = orderedCategories(spec);
  if (!ordered || ordered.categories.length <= size) return null;
  const { key, categories } = ordered;
  const pages = Math.ceil(categories.length / size);
  const page = Math.min(Math.max(0, Math.trunc(index)), pages - 1);
  const first = page * size;
  const shown = categories.slice(first, first + size);
  const window = rowsOfCategories(spec, key, new Set(shown));
  const domain = valueDomain(spec);
  return {
    spec: {
      ...spec,
      rows: window,
      row_count: window.length,
      // A shared scale across views already spans every view's data; it stays in charge.
      ...(domain && !spec._export_axis_domains ? { _export_axis_domains: { encoding: "hbar", domains: { x: domain } } } : {}),
    },
    start: first + 1,
    end: first + shown.length,
    total: categories.length,
    index: page,
    signature: [spec.kind, key, categories.length, categories[0], categories[categories.length - 1], rows.length].join("\u0001"),
  };
}
