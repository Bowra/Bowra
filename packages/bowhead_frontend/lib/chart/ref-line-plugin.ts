/**
 * Batch 2 §3.3 · reference lines — custom per-chart plugin without new dependencies.
 *
 * `chartjs-plugin-annotation` is not a dependency; do not add a package for one horizontal line (tech-design §3.3).
 * Same pattern as pie center numbers (build-config afterDraw): pass per-chart plugin through
 * ChartJSConfig.plugins, without global registration.
 *
 * Honesty: mean is the **current view's** mean. After Top-N truncation (applyView other row),
 * calling it simply mean misleads — caller passes `partial: true`, and label includes scope suffix.
 */

import type { Chart, Plugin } from "chart.js";

import { staticTranslator } from "@/lib/locale-messages";

import { CHART_FONT_SANS_FALLBACK, type ChartTheme } from "../chart-theme";

export type RefLineKind = "mean" | "value";

export interface RefLineSpec {
  kind: RefLineKind;
  /** Target value for kind === "value"; ignored for mean. */
  value?: number;
  /** Left label half (e.g. "均值"). Right half is formatted value, using caller-provided formatter. */
  label?: string;
  /** Truncated/aggregated data → label states scope rather than claiming full-data mean. */
  partial?: boolean;
  /** Scope suffix for this line (caller supplies i18n), overriding `RefLineOptions.partialNote`.
   *  review R4-5: two truncation sources — only some rows loaded vs view choosing Top-N / hiding
   *  series — require distinct scope messages, independently supplied by caller. */
  partialNote?: string;
}

export interface RefLineOptions {
  theme: ChartTheme;
  /** Value → display text (column-level semantics; caller takes format mode from build-config). */
  format: (v: number) => string;
  /** Scope suffix appended when partial, e.g. "（基于前 1,000 行）". */
  partialNote?: string;
}

/** Reference-line label: interface sans like every other chart text (Chart Embed Spec D1). */
const labelFont = (theme: ChartTheme) => `500 11px ${theme.fontSans || CHART_FONT_SANS_FALLBACK}`;

/** review R4-4: **value** axis, not `scales.y`. hbar uses `indexAxis: "y"`, making y the
 *  category axis and x the value axis; calling y `getPixelForValue(mean)` treats mean as row index, putting dashed line
 *  on an arbitrary row while labeling it "均值 ¥X". hbar is in VIEW_KINDS, selectable from one dropdown. */
function valueScaleOf(chart: Chart) {
  const wanted = isHorizontal(chart) ? "x" : "y";
  const scales = chart.scales ?? {};
  return scales[wanted] ?? Object.values(scales).find((s) => s.axis === wanted) ?? null;
}

/** Value-axis ID. With no scales (before layout / axisless kinds), fall back to default axis name — values must not
 *  degrade to no mean just because scale is unavailable; that concerns drawing, not calculation. */
function valueAxisId(chart: Chart): string {
  const wanted = isHorizontal(chart) ? "x" : "y";
  return valueScaleOf(chart)?.id ?? wanted;
}

function isHorizontal(chart: Chart): boolean {
  return chart.options?.indexAxis === "y";
}

/** Get values from visible datasets **bound to the same value axis** (exclude hidden series).
 *
 *  review R4-4b: old implementation averaged all datasets. dual_axis / combo binds
 *  "营业额（亿）" to y and "利润率（%）" to y1; their combined mean adds quantities with different
 *  dimensions and divides by count — the same fabricated aggregate this sprint just eliminated
 *  in stacked bars/waterfalls. Count only series on the axis used to draw this line. */
function visibleValues(chart: Chart, axisId: string): number[] {
  const horizontal = isHorizontal(chart);
  const fallbackAxis = horizontal ? "x" : "y";
  const out: number[] = [];
  chart.data.datasets.forEach((ds, i) => {
    if (!chart.isDatasetVisible(i)) return;
    const bound = ds as { yAxisID?: string; xAxisID?: string };
    const dsAxis = (horizontal ? bound.xAxisID : bound.yAxisID) ?? fallbackAxis;
    if (dsAxis !== axisId) return;
    for (const raw of (ds.data ?? []) as unknown[]) {
      const point = raw as { x?: number; y?: number } | null;
      const v = typeof raw === "number" ? raw : (horizontal ? point?.x : point?.y);
      if (typeof v === "number" && !Number.isNaN(v)) out.push(v);
    }
  });
  return out;
}

export function resolveRefLineValue(chart: Chart, spec: RefLineSpec): number | null {
  if (spec.kind === "value") {
    return typeof spec.value === "number" && !Number.isNaN(spec.value) ? spec.value : null;
  }
  const values = visibleValues(chart, valueAxisId(chart));
  if (values.length === 0) return null;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

export function createRefLinePlugin(specs: RefLineSpec[], opts: RefLineOptions): Plugin {
  return {
    id: "bowheadRefLines",
    afterDatasetsDraw(chart) {
      if (specs.length === 0) return;
      const { ctx, chartArea } = chart;
      const scale = valueScaleOf(chart);
      // No Cartesian value axis (pie/donut etc.) → do not draw. Caller must withhold entry point from these kinds,
      // otherwise users check it and see no effect.
      if (!ctx || !chartArea || !scale) return;
      const horizontal = isHorizontal(chart);

      for (const spec of specs) {
        const value = resolveRefLineValue(chart, spec);
        if (value === null) continue;
        const at = scale.getPixelForValue(value);
        if (!Number.isFinite(at)) continue;
        const outside = horizontal
          ? at < chartArea.left || at > chartArea.right
          : at < chartArea.top || at > chartArea.bottom;
        if (outside) continue;

        ctx.save();
        ctx.strokeStyle = opts.theme.inkSoft;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        // Horizontal value axis (hbar) → vertical reference line; otherwise it parallels bars and permits no comparison
        if (horizontal) {
          ctx.moveTo(at, chartArea.top);
          ctx.lineTo(at, chartArea.bottom);
        } else {
          ctx.moveTo(chartArea.left, at);
          ctx.lineTo(chartArea.right, at);
        }
        ctx.stroke();

        const head = spec.label ?? (spec.kind === "mean" ? staticTranslator("ref_line_label")("mean") : "");
        const noteText = spec.partialNote ?? opts.partialNote;
        const note = spec.partial && noteText ? ` ${noteText}` : "";
        const text = `${head} ${opts.format(value)}${note}`.trim();
        ctx.setLineDash([]);
        ctx.font = labelFont(opts.theme);
        ctx.textAlign = "right";
        const width = ctx.measureText(text).width;
        // Label background prevents dashed-line overlap from becoming illegible
        if (horizontal) {
          // review R5-6: with vertical line near left edge, right-aligned label and background extend left of chartArea
          // — covering category ticks or leaving bounds. Flip to right side of line when it cannot fit.
          const fitsLeft = at - width - 6 >= chartArea.left;
          ctx.textAlign = fitsLeft ? "right" : "left";
          ctx.textBaseline = "top";
          const boxLeft = fitsLeft ? at - width - 6 : at;
          ctx.fillStyle = opts.theme.surface;
          ctx.fillRect(boxLeft, chartArea.top + 1, width + 6, 14);
          ctx.fillStyle = opts.theme.inkSoft;
          ctx.fillText(text, fitsLeft ? at - 3 : at + 3, chartArea.top + 3);
        } else {
          ctx.textBaseline = "bottom";
          ctx.fillStyle = opts.theme.surface;
          ctx.fillRect(chartArea.right - width - 6, at - 15, width + 6, 14);
          ctx.fillStyle = opts.theme.inkSoft;
          ctx.fillText(text, chartArea.right - 3, at - 2);
        }
        ctx.restore();
      }
    },
  };
}
