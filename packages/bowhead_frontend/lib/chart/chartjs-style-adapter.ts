import type { ChartDataset, ChartOptions } from "chart.js";

import type { ChartTheme } from "../chart-theme";
import type { ChartJSConfig } from "./build-config";
import { CHART_STYLE_PRESENTATION_POLICY } from "./chart-style-registry";
import type { ChartFillPattern, ResolvedChartPresentationV2 } from "./chart-style-presentation";
import { fillPatternPaint, patternStrokeFor } from "./fill-pattern";
import type { NormalizedChartRenderShapeV1 } from "./normalize-chart-for-rendering";
import type { ChartSpec } from "./types";

type MutableDataset = ChartDataset & Record<string, unknown>;
type GridContext = { tick?: { value?: unknown } };
type BarContext = { dataIndex: number; raw?: unknown; parsed?: { x?: number; y?: number } };
type CornerRadius = { topLeft: number; topRight: number; bottomLeft: number; bottomRight: number };

/** Legend swatch edge while any series carries a texture: an 8px box shows barely one tile. */
const PATTERN_LEGEND_BOX_PX = 12;

/** Stroke candidates in preference order; the first with ≥3:1 against the painted fill wins. */
function strokeCandidates(theme: ChartTheme, preferred: readonly string[] = []): string[] {
  return [...preferred, theme.surface, theme.ink];
}

/** Paint `value` (a color, a scriptable color, or a per-element color list) with `pattern`;
 *  the stroke is picked per painted color, so faded (non-lead) fills keep a visible texture. */
function patternized(value: unknown, pattern: ChartFillPattern, candidates: readonly string[], backdrop: string): unknown {
  if (pattern === "solid") return value;
  if (typeof value === "string") return fillPatternPaint(value, pattern, patternStrokeFor(value, candidates, backdrop));
  if (Array.isArray(value)) return value.map((item) => patternized(item, pattern, candidates, backdrop));
  if (typeof value === "function") {
    return (...args: unknown[]) =>
      patternized((value as (...inner: unknown[]) => unknown)(...args), pattern, candidates, backdrop);
  }
  return value;
}

function lineStyle(
  dataset: MutableDataset,
  seriesIndex: number,
  presentation: ResolvedChartPresentationV2,
  theme: ChartTheme,
): MutableDataset {
  const line = presentation.effective.line;
  if (!line) return dataset;
  const encoding = presentation.effective.series[seriesIndex];
  const themeWidth = theme.lineWidth ?? presentation.requested.line?.widthPx ?? line.widthPx;
  const currentWidth = typeof dataset.borderWidth === "number" ? dataset.borderWidth : themeWidth;
  const emphasisDelta = currentWidth - themeWidth;
  const hasAccessibilityFloor = presentation.adjustments.some((adjustment) =>
    adjustment.path === "line.widthPx" && adjustment.adjustedBy === "accessibility"
  );
  const minimum = hasAccessibilityFloor
    ? CHART_STYLE_PRESENTATION_POLICY.line.highContrastMinimumWidthPx
    : CHART_STYLE_PRESENTATION_POLICY.line.minimumWidthPx;
  const next: MutableDataset = {
    ...dataset,
    borderWidth: Math.max(minimum, line.widthPx + emphasisDelta),
    pointRadius: line.pointRadiusPx,
    hitRadius: Math.max(line.hitRadiusPx, Number(dataset.hitRadius ?? 0)),
    pointHoverRadius: Math.max(line.pointHoverRadiusPx, Number(dataset.pointHoverRadius ?? 0)),
    stepped: false,
    ...(encoding
      ? { borderDash: [...encoding.lineDash], pointStyle: encoding.pointStyle }
      : {}),
  };
  // Stacked area: the fill is a 20% tint, so surface-colored strokes would vanish into it;
  // prefer the opaque series color (≥3:1 against the surface by the palette gate).
  if (encoding && encoding.fillPattern !== "solid" && next.fill) {
    const preferred = typeof next.borderColor === "string" ? [next.borderColor] : [];
    next.backgroundColor = patternized(
      next.backgroundColor,
      encoding.fillPattern,
      strokeCandidates(theme, preferred),
      theme.surface,
    ) as MutableDataset["backgroundColor"];
  }
  if (line.curve === "monotone") {
    delete next.tension;
    next.cubicInterpolationMode = "monotone";
  } else {
    delete next.cubicInterpolationMode;
    next.tension = 0;
  }
  return next;
}

function valueOfBarContext(context: BarContext, horizontal: boolean): number {
  if (typeof context.raw === "number") return context.raw;
  if (Array.isArray(context.raw) && context.raw.length >= 2) {
    const first = Number(context.raw[0]);
    const second = Number(context.raw[1]);
    return Number.isFinite(first) && Number.isFinite(second) ? second - first : 0;
  }
  const parsed = horizontal ? context.parsed?.x : context.parsed?.y;
  return typeof parsed === "number" && Number.isFinite(parsed) ? parsed : 0;
}

function corners(radius: number, horizontal: boolean, positive: boolean): CornerRadius {
  if (horizontal) {
    return positive
      ? { topLeft: 0, bottomLeft: 0, topRight: radius, bottomRight: radius }
      : { topLeft: radius, bottomLeft: radius, topRight: 0, bottomRight: 0 };
  }
  return positive
    ? { topLeft: radius, topRight: radius, bottomLeft: 0, bottomRight: 0 }
    : { topLeft: 0, topRight: 0, bottomLeft: radius, bottomRight: radius };
}

function barStyle(
  dataset: MutableDataset,
  seriesIndex: number,
  spec: ChartSpec,
  shape: NormalizedChartRenderShapeV1,
  presentation: ResolvedChartPresentationV2,
  theme: ChartTheme,
  horizontal: boolean,
): MutableDataset {
  const bar = presentation.effective.bar;
  if (!bar) return dataset;
  const encoding = presentation.effective.series[seriesIndex];
  const boundaryWidth = encoding?.boundaryWidthPx ?? 0;
  const pattern = encoding?.fillPattern ?? "solid";
  return {
    ...dataset,
    ...(pattern !== "solid"
      ? {
          backgroundColor: patternized(dataset.backgroundColor, pattern, strokeCandidates(theme), theme.surface),
          hoverBackgroundColor: patternized(
            dataset.hoverBackgroundColor ?? dataset.backgroundColor,
            pattern,
            strokeCandidates(theme),
            theme.surface,
          ),
        }
      : {}),
    borderSkipped: false,
    borderWidth: Math.max(typeof dataset.borderWidth === "number" ? dataset.borderWidth : 0, boundaryWidth),
    ...(boundaryWidth > 0
      ? { borderColor: theme.accessibility?.contrast === "high" ? theme.ink : theme.surface }
      : {}),
    ...(encoding ? { pointStyle: encoding.pointStyle } : {}),
    borderRadius: (context: BarContext) => {
      const waterfallMetric = spec.kind === "waterfall"
        ? spec.columns.find((column) => column.type === "numeric")
        : undefined;
      const waterfallValue = waterfallMetric && context.dataIndex < (spec.rows ?? []).length
        ? spec.rows?.[context.dataIndex]?.[waterfallMetric.key]
        : undefined;
      const value = typeof waterfallValue === "number"
        ? waterfallValue
        : valueOfBarContext(context, horizontal);
      const positive = value >= 0;
      if (shape.flags.stacked && shape.barStackEdges) {
        const outer = positive
          ? shape.barStackEdges.positiveOuterSeriesIndexByCategory[context.dataIndex]
          : shape.barStackEdges.negativeOuterSeriesIndexByCategory[context.dataIndex];
        if (outer !== seriesIndex) return corners(0, horizontal, positive);
      }
      return corners(bar.radiusPx, horizontal, positive);
    },
  } as unknown as MutableDataset;
}

function valueAxisKeys(spec: ChartSpec): string[] {
  if (spec.kind === "hbar") return ["x"];
  if (spec.kind === "scatter") return ["x", "y"];
  if (spec.kind === "dual_axis") return ["y"];
  if (["line", "area_stacked", "bar", "stacked_bar", "combo", "waterfall"].includes(spec.kind)) return ["y"];
  return [];
}

function applyGrid(
  options: ChartOptions,
  spec: ChartSpec,
  shape: NormalizedChartRenderShapeV1,
  theme: ChartTheme,
  presentation: ResolvedChartPresentationV2,
): ChartOptions {
  if (!options.scales) return options;
  const scales = { ...options.scales } as Record<string, Record<string, unknown>>;
  for (const key of valueAxisKeys(spec)) {
    const scale = scales[key];
    if (!scale) continue;
    const previousGrid = (scale.grid ?? {}) as Record<string, unknown>;
    if (previousGrid.drawOnChartArea === false) continue;
    const grid = { ...previousGrid };
    if (presentation.effective.grid === "hidden") {
      grid.display = true;
      grid.color = (context: GridContext) =>
        shape.flags.crossesZero && Number(context.tick?.value) === 0 ? theme.grid : "transparent";
      grid.borderDash = [];
    } else if (presentation.effective.grid === "quiet") {
      grid.color = theme.gridSoft;
      grid.borderDash = [...CHART_STYLE_PRESENTATION_POLICY.grid.quietDash];
    } else {
      // Chart Embed Spec D2: quiet and standard are one hairline tier (separator colour, solid). Only the
      // high-contrast theme (`gridDensity: "strong"`, which also forces `standard`) keeps the stronger line.
      grid.color = theme.gridDensity === "strong" ? theme.grid : theme.gridSoft;
      grid.borderDash = [...CHART_STYLE_PRESENTATION_POLICY.grid.standardDash];
    }
    scales[key] = { ...scale, grid };
  }
  return { ...options, scales };
}

/** Apply only presentation properties; datasets, values, order and formatters stay intact. */
export function applyChartJsStylePresentation(
  config: ChartJSConfig,
  spec: ChartSpec,
  shape: NormalizedChartRenderShapeV1,
  theme: ChartTheme,
  presentation: ResolvedChartPresentationV2,
): ChartJSConfig {
  const datasets = config.data.datasets.map((source, index) => {
    let dataset = { ...source } as MutableDataset;
    const role = shape.series[index]?.role;
    const declaredType = dataset.type ?? config.type;
    if (role === "line" || role === "area" || declaredType === "line") {
      dataset = lineStyle(dataset, index, presentation, theme);
    }
    if (role === "bar" || declaredType === "bar") {
      dataset = barStyle(dataset, index, spec, shape, presentation, theme, spec.kind === "hbar");
    } else if (role === "point") {
      const encoding = presentation.effective.series[index];
      if (encoding) dataset.pointStyle = encoding.pointStyle;
    }
    return dataset;
  });
  // Doughnut: one dataset, one presentation series per slice.
  if (config.type === "doughnut" && datasets.length === 1) {
    const slices = presentation.effective.series;
    const colors = datasets[0]!.backgroundColor;
    if (Array.isArray(colors) && slices.some((slice) => slice.fillPattern !== "solid")) {
      datasets[0]!.backgroundColor = colors.map((color, index) =>
        patternized(color, slices[index]?.fillPattern ?? "solid", strokeCandidates(theme), theme.surface)
      ) as MutableDataset["backgroundColor"];
    }
  }
  if (config.type === "scatter" && datasets.length === 1 && shape.seriesCount > 1) {
    const labels = datasets[0]?._pointLabels;
    if (Array.isArray(labels) && labels.length === shape.seriesCount) {
      datasets[0]!.pointStyle = presentation.effective.series.map((series) => series.pointStyle);
    }
  }
  const textured = presentation.effective.series.some((series) => series.fillPattern !== "solid");
  const legend = config.options.plugins?.legend;
  let options: ChartOptions = {
    ...config.options,
    plugins: {
      ...config.options.plugins,
      ...(textured && legend
        ? {
            legend: {
              ...legend,
              labels: { ...legend.labels, boxWidth: PATTERN_LEGEND_BOX_PX, boxHeight: PATTERN_LEGEND_BOX_PX },
            },
          }
        : {}),
      tooltip: {
        ...config.options.plugins?.tooltip,
        enabled: presentation.effective.tooltip === "enabled",
      },
    },
  };
  options = applyGrid(options, spec, shape, theme, presentation);
  return {
    ...config,
    data: { ...config.data, datasets },
    options,
  };
}
