import type { ChartKind } from "./types";
import type { ChartFamily, NormalizedChartRenderShapeV1 } from "./normalize-chart-for-rendering";

export type { ChartFamily } from "./normalize-chart-for-rendering";

export type ChartPlacement = "chat" | "canvas" | "settings-preview" | "embed";
export interface ChartRenderContext {
  placement: ChartPlacement;
  bounds: {
    widthPx: number;
    heightPx: number;
    devicePixelRatio: number;
  };
  input: {
    hover: boolean;
    pointer: "fine" | "coarse" | "none";
    keyboard: boolean;
  };
  output: {
    medium: "screen" | "raster" | "print";
    static: boolean;
  };
  mode: "light" | "dark";
  locale: string;
  reducedMotion: boolean;
  forcedColors: boolean;
  exactDataAlternative: {
    kind: "inspect-table" | "adjacent-table" | "none";
    elementId?: string;
  };
}

export interface ChartContentProfile {
  family: ChartFamily;
  kind: ChartKind;
  seriesCount: number;
  categoryCount: number;
  longestLabelGraphemes: number;
  hasNegativeValues: boolean;
}

export interface ChartRenderRecipe {
  widthClass: "compact" | "regular" | "wide";
  densityTier: "compact" | "standard" | "spacious";
  animate: boolean;
  showLegend: boolean;
  legendPosition: "bottom" | "right";
  legendFontSize: number;
  tickFontSize: number;
  maxXTicks: number;
  maxYTicks: number;
  tooltipIntersect: boolean;
  forceDataLabels: boolean;
  minimumPlotWidthPx: number;
  /** Endpoint identity must not consume the readable plot or its existing minimum width. */
  endpointPlotWidthFloorPx: number;
  minimumPlotHeightPx: number;
  /** Bar thickness cap (Chart Embed Spec D3): 24 in reports and chat, 20 in embeds and compact widths; in exports
   *  sized by the category band, `clamp(24, band × 0.35, 96)` (D3 amendment, see `exportBarThicknessPx`). */
  maxBarThicknessPx: number;
  /** Exact bar thickness for height-fitted horizontal bars on screen (the cap), so rows of `hbarRowPitchPx` always
   *  draw capped bars whatever the axis title and paddings take; null elsewhere (Chart.js band percentages). */
  fixedBarThicknessPx: number | null;
  /** Horizontal-bar row pitch per category (D3): 40 in reports, 36 in embeds and compact widths. */
  hbarRowPitchPx: number;
}

/** Height of the value axis (ticks) under a horizontal bar chart, added to the rows (D3: `n × pitch + 22`). */
export const HBAR_AXIS_HEIGHT_PX = 22;
/** Room for the value-axis title row (unit at the axis end) under a horizontal bar chart. */
export const HBAR_AXIS_TITLE_HEIGHT_PX = 18;
/** Room for a bottom legend row when a grouped horizontal bar chart shows one. */
const HBAR_LEGEND_HEIGHT_PX = 28;

/**
 * Compatibility projection. The normalized shape is the only content owner;
 * callers may not derive a second profile directly from raw ChartSpec.
 */
export function resolveChartContentProfile(shape: NormalizedChartRenderShapeV1): ChartContentProfile {
  return {
    family: shape.family,
    kind: shape.kind,
    seriesCount: shape.seriesCount,
    categoryCount: shape.categoryCount,
    longestLabelGraphemes: shape.longestLabelGraphemes,
    hasNegativeValues: shape.series.some((series) => series.hasNegative),
  };
}

/** Pure projection from real capabilities + final content shape to Chart.js layout decisions. */
export function resolveChartRenderRecipe(
  context: ChartRenderContext,
  shape: NormalizedChartRenderShapeV1,
): ChartRenderRecipe {
  const profile = resolveChartContentProfile(shape);
  const width = Math.max(1, context.bounds.widthPx);
  const height = Math.max(1, context.bounds.heightPx);
  const widthClass = width <= 420 ? "compact" : width >= 1000 ? "wide" : "regular";
  const densityTier = widthClass === "compact" || height <= 280
    ? "compact"
    : widthClass === "wide" && height >= 560
      ? "spacious"
      : "standard";
  const compositionLegend = profile.kind === "pie" && profile.categoryCount > 1;
  const showLegend = compositionLegend || profile.seriesCount > 1;
  const staticOrNoHover = context.output.static || !context.input.hover || context.input.pointer !== "fine";
  const labelCapacity = widthClass === "compact" ? 6 : widthClass === "wide" ? 16 : 12;
  const forceDataLabels = context.output.static
    || (staticOrNoHover && profile.seriesCount === 1 && profile.categoryCount <= labelCapacity);
  const compactScreen = !context.output.static && context.output.medium === "screen" && width <= 600;
  const horizontal = profile.kind === "hbar";
  // Pie and Sankey have no category axis; widening creates meaningless horizontal scrolling.
  const noCategoryAxis = profile.kind === "scatter"
    || profile.kind === "heatmap"
    || profile.kind === "pie"
    || profile.kind === "sankey";
  // Slot width follows required contents: bars need each category's name and top value; line / area
  // x ticks thin by maxXTicks, numeric labels show only endpoints/extrema; points need only separate from neighbors.
  // Actual 116 mobile capture: six line-chart months at 56px each stretched to 336px, scrolling in a ~270px plot,
  // moving May, June and the final point outside the visible region.
  // combo / dual_axis belong to trends but draw bars, so use bar criteria.
  const slotWidth = profile.kind === "line" || profile.kind === "area_stacked"
    ? 20
    : widthClass === "compact" ? 56 : 46;
  const neededPlotWidth = profile.categoryCount * slotWidth;
  const minimumPlotWidthPx = !horizontal && !noCategoryAxis && neededPlotWidth > width
    ? Math.min(960, neededPlotWidth)
    : 0;
  // D3: bars are capped instead of stretching with the box. Static output (export) keeps the report cap even though
  // its placement is "embed": the export frame is a report page, not a host panel.
  const narrow = widthClass === "compact" || (context.placement === "embed" && !context.output.static);
  const hbarRowPitchPx = narrow ? 36 : 40;
  const maxBarThicknessPx = context.output.static
    ? exportBarThicknessPx(horizontal ? height : width, profile.categoryCount, horizontal)
    : narrow ? 20 : 24;
  const fixedBarThicknessPx = horizontal && !context.output.static ? maxBarThicknessPx : null;
  // D3: a horizontal bar chart is as tall as its rows — `n × pitch + axis` — rather than a fixed 300–420px box that
  // turned two or three bars into 50–80px slabs. Grouped bars add one capped bar per extra series to each row.
  const rowPitch = hbarRowPitchPx + Math.max(0, profile.seriesCount - 1) * maxBarThicknessPx;
  const minimumPlotHeightPx = horizontal && profile.categoryCount > 0
    ? profile.categoryCount * rowPitch + HBAR_AXIS_HEIGHT_PX + (showLegend ? HBAR_LEGEND_HEIGHT_PX : 0)
    : 0;

  return {
    widthClass,
    densityTier,
    animate: context.output.medium === "screen"
      && !context.output.static
      && !context.reducedMotion
      && !context.forcedColors,
    showLegend,
    legendPosition: profile.kind === "pie" && widthClass !== "compact" ? "right" : "bottom",
    legendFontSize: compactScreen ? 12 : densityTier === "spacious" ? 11.5 : 11,
    tickFontSize: compactScreen ? 12 : densityTier === "spacious" ? 11.5 : 11,
    // A 344px chat plot loses roughly 90–110px to its value axis and padding.
    // Four full date/category labels remain legible there; six already collide.
    maxXTicks: widthClass === "compact" ? 4 : widthClass === "wide" ? 12 : 9,
    maxYTicks: widthClass === "compact" ? 4 : widthClass === "wide" ? 6 : 5,
    tooltipIntersect: context.input.hover && context.input.pointer === "fine",
    forceDataLabels,
    minimumPlotWidthPx,
    endpointPlotWidthFloorPx: Math.max(160, minimumPlotWidthPx),
    minimumPlotHeightPx,
    maxBarThicknessPx,
    fixedBarThicknessPx,
    hbarRowPitchPx,
  };
}

/**
 * D3 amendment (owner, 2026-10-06): in the fixed 1600 × 900 export frame a 24px cap turns a few single-series bars into
 * toothpicks across a 1456px plot, so exports — and only exports — size bars by their category band:
 * `thickness = clamp(24, band × 0.35, 96)`. The band is the plot extent along the category axis divided by the category
 * count; the extent deducts the value-axis ticks (vertical bars) or the category axis and value ticks (horizontal).
 * Deterministic from the frozen export bounds, so identical inputs keep identical pixels. Screens keep 24 / 20.
 */
export function exportBarThicknessPx(extentPx: number, categoryCount: number, horizontal: boolean): number {
  const plotExtent = Math.max(1, extentPx - (horizontal ? 48 : 72));
  const band = plotExtent / Math.max(1, categoryCount);
  return Math.min(96, Math.max(24, Math.round(band * 0.35)));
}

export function defaultChartRenderBounds(surface: "canvas" | "chat" | "export" = "canvas") {
  if (surface === "chat") return { widthPx: 344, heightPx: 320 };
  if (surface === "export") return { widthPx: 1456, heightPx: 772 };
  return { widthPx: 720, heightPx: 360 };
}
