import { pivotLongSpec } from "./pivot-long";
import type { ChartSpec } from "./types";

/** Smallest category pitch at which a horizontal bar's name (11px tick font) and its bar stay legible. */
export const EXPORT_HBAR_MIN_ROW_PX = 18;
/** Extra pitch per additional numeric series: grouped bars share the category's band. */
export const EXPORT_HBAR_EXTRA_SERIES_PX = 6;
/** Frame height not available to the category axis: chart header (title + subtitle), value-axis ticks and title,
 *  the canvas wrapper's padding and up to two subcaption lines. Measured on the 1600 × 900 export frame. */
export const EXPORT_HBAR_CHROME_PX = 180;

/**
 * How many horizontal-bar categories fit the fixed export frame legibly — and the spec cut to that many rows.
 *
 * On screen a long horizontal bar chart grows taller and scrolls (`minimumPlotHeightPx`, `.is-tall`). The export frame
 * is a fixed 1600 × 900 screenshot that cannot grow, so it never did (`tallH = 0`): 50 categories were squeezed into
 * ~590px, 11.8px each, with 11px names touching and every other value label dropped. Cropping the overflow would be
 * worse (silent), and shrinking fonts further makes a printed page unreadable.
 *
 * Instead the export keeps the N rows with the **largest absolute value of the primary measure** (the first numeric
 * column the reader can see — the one the backend ranks by unless hidden) in their original relative order, and marks the spec (`_export_top`) so
 * the subcaption says exactly that: "N of M with the largest |measure|". Row order alone proves nothing: the backend
 * only sorts rows by value when it has to truncate them (`_take_top_n` above its cap), so the first N rows of a
 * persisted chart are not necessarily its top N. Capacity depends on the series count (32 rows for one measure,
 * 19 for three in the current frame).
 *
 * Returns the spec unchanged when it already fits or is not a horizontal bar.
 */
export function fitHbarRowsForExport(
  spec: ChartSpec,
  frameContentHeightPx: number,
  hiddenSeries: readonly string[] = [],
): ChartSpec {
  if (spec.kind !== "hbar") return spec;
  const rows = spec.rows ?? [];
  const hidden = new Set(hiddenSeries);
  const capacityFor = (series: number) => {
    const pitch = EXPORT_HBAR_MIN_ROW_PX + EXPORT_HBAR_EXTRA_SERIES_PX * (Math.max(1, series) - 1);
    return Math.max(1, Math.floor((frameContentHeightPx - EXPORT_HBAR_CHROME_PX) / pitch));
  };
  const magnitude = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? Math.abs(v) : 0);

  // Long format (category × series × one value): the renderer pivots it into one bar per category with one segment
  // per series (`pivotLongSpec`). Cutting *rows* would drop some series of a category (missing segments read as 0),
  // count cells instead of categories, and could push the fill below the pivot threshold so the chart changes shape.
  // Select whole categories instead, ranked by the sum of |value| over their visible series.
  const pivot = pivotLongSpec(spec);
  if (pivot) {
    const xKey = pivot.spec.columns[0]!.key;
    const seriesKey = pivot.seriesCol.key;
    const valueCol = (spec.columns ?? []).find((c) => c.type === "numeric")!;
    const categories: string[] = [];
    const score = new Map<string, number>();
    for (const row of rows) {
      const category = String(row[xKey] ?? "");
      if (!score.has(category)) {
        categories.push(category);
        score.set(category, 0);
      }
      if (hidden.has(`__pivot__${String(row[seriesKey] ?? "")}`)) continue;
      score.set(category, score.get(category)! + magnitude(row[valueCol.key]));
    }
    const capacity = capacityFor(pivot.seriesCount);
    if (categories.length <= capacity) return spec;
    const keep = new Set(
      categories
        .map((category, index) => ({ category, index, m: score.get(category)! }))
        .sort((a, b) => b.m - a.m || a.index - b.index)
        .slice(0, capacity)
        .map((c) => c.category),
    );
    return {
      ...spec,
      rows: rows.filter((row) => keep.has(String(row[xKey] ?? ""))),
      row_count: Math.max(spec.row_count ?? rows.length, rows.length),
      _export_top: { measure: valueCol.header, shown: keep.size, total: categories.length },
    };
  }

  const numeric = (spec.columns ?? []).filter((c) => c.type === "numeric");
  const capacity = capacityFor(numeric.length);
  if (rows.length <= capacity) return spec;
  // Rank by the first value column the reader can see: a series hidden in the legend must not decide which bars stay.
  const primary = numeric.find((c) => !hidden.has(c.key)) ?? numeric[0];
  // Stable: ties keep their original order; the kept rows are then restored to their original relative order.
  const keep = new Set(
    rows
      .map((row, index) => ({ index, m: primary ? magnitude(row[primary.key]) : 0 }))
      .sort((a, b) => b.m - a.m || a.index - b.index)
      .slice(0, capacity)
      .map((r) => r.index),
  );
  return {
    ...spec,
    rows: rows.filter((_, index) => keep.has(index)),
    row_count: Math.max(spec.row_count ?? rows.length, rows.length),
    _export_top: primary
      ? { measure: primary.header, shown: keep.size, total: Math.max(spec.row_count ?? rows.length, rows.length) }
      : undefined,
  };
}
