import type { ChartColumn } from "./types";

/**
 * Dimension columns with the producer's declared axis roles first: `role: "x"`, then `role: "color"`.
 *
 * The backend declares these for stacked bars over a two-level hierarchy (`chart/infer.py` `_resolve_stack_dims`):
 * the identifying child goes on the category axis and the parent becomes the series, so each bar is one segment
 * coloured by its group. Before, that decision was computed and dropped, and every consumer re-guessed it from column
 * order (`catCols[0]` = x, `catCols[1]` = series), which can reverse the intended axes.
 *
 * Every consumer that picks "which dimension is x, which is the series" must go through this function: `buildBar`,
 * `pivotLongSpec`, the normalized render shape and the caption lead resolver. The backend normalizer mirrors it
 * (`chart_style_presentation._role_ordered_dimensions`); export freezing compares the two shapes byte-for-byte.
 *
 * Specs without both roles (every chart persisted before this change, and crossed dimensions) keep column order.
 */
export function roleOrderedDims(dims: readonly ChartColumn[]): ChartColumn[] {
  const x = dims.find((c) => c.role === "x");
  const color = dims.find((c) => c.role === "color" && c !== x);
  if (!x || !color) return [...dims];
  return [x, color, ...dims.filter((c) => c !== x && c !== color)];
}
