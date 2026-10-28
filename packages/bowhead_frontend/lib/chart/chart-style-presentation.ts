import type { ChartTheme } from "../chart-theme";
import { CHART_STYLE_PRESENTATION_POLICY } from "./chart-style-registry";
import type { NormalizedChartRenderShapeV1 } from "./normalize-chart-for-rendering";
import { resolveChartRenderRecipe, type ChartRenderContext } from "./render-context";
import type { ChartKind } from "./types";

export type ChartTooltipToken = "enabled" | "disabled";
export type ChartGridToken = "hidden" | "quiet" | "standard";
export type ChartLineCurveToken = "straight" | "monotone";
export type ChartLinePointsToken = "auto" | "show" | "hide";

export interface ChartKindPresentationTokensV2 {
  tooltip: ChartTooltipToken;
  grid: ChartGridToken;
  line?: {
    curve: ChartLineCurveToken;
    widthPx: number;
    points: ChartLinePointsToken;
    pointRadiusPx: number;
  };
  bar?: { radiusPx: number };
}

export interface ChartStyleThemeV2 {
  presentationPolicyVersion: "chart-presentation/1";
  kindTokens: Partial<Record<ChartKind, ChartKindPresentationTokensV2>>;
  categoricalSeriesPalette: string[];
  maxDistinctSeriesColors: number;
  paletteOverflowRecipeVersion: "palette-overflow/2";
  /** Leading palette colors that stay distinguishable by color alone (server-measured
   *  `finalPaletteQA.colorOnlySeriesCapacity`). Absent ⇒ the whole palette. */
  colorOnlySeriesCapacity?: number;
}

export type ChartFillPattern = "solid" | "diagonal" | "dots" | "crosshatch";

export interface ChartSeriesPresentationV2 {
  key: string;
  paletteIndex: number;
  lineDash: number[];
  pointStyle: "circle" | "rect" | "triangle" | "rectRot" | "crossRot" | "star";
  /** Texture painted over the series color on filled marks (bar, area, pie slice). */
  fillPattern: ChartFillPattern;
  boundaryWidthPx: number;
}

export interface ChartPresentationTokensV2 {
  tooltip: ChartTooltipToken;
  grid: ChartGridToken;
  line?: {
    curve: ChartLineCurveToken;
    widthPx: number;
    points: ChartLinePointsToken;
    pointRadiusPx: number;
    hitRadiusPx: number;
    pointHoverRadiusPx: number;
  };
  bar?: { radiusPx: number };
  series: ChartSeriesPresentationV2[];
}

export interface ChartStyleAdjustmentV2 {
  path: string;
  requested: string | number | boolean | string[];
  effective: string | number | boolean | string[];
  adjustedBy: "context" | "accessibility" | "correctness" | "capability";
  reasonCodes: string[];
}

export interface ResolvedChartPresentationV2 {
  requested: ChartPresentationTokensV2;
  effective: ChartPresentationTokensV2;
  adjustments: ChartStyleAdjustmentV2[];
  renderShapeVersion: "chart-shape/1";
  presentationPolicyVersion: "chart-presentation/1";
}

function tokensFor(theme: ChartTheme, kind: ChartKind): ChartKindPresentationTokensV2 {
  const supplied = theme.chartStyleV2?.kindTokens[kind];
  if (supplied) return supplied;
  const grid = theme.gridDensity === "strong" ? "standard" : (theme.gridDensity ?? "quiet");
  const tokens: ChartKindPresentationTokensV2 = { tooltip: "enabled", grid };
  if (["line", "area_stacked", "dual_axis", "combo"].includes(kind)) {
    tokens.line = {
      curve: "monotone",
      widthPx: theme.lineWidth ?? 2,
      points: "auto",
      pointRadiusPx: theme.pointRadius ?? 3,
    };
  }
  if (["bar", "hbar", "stacked_bar", "waterfall"].includes(kind)) {
    tokens.bar = { radiusPx: theme.barRadius ?? 4 };
  }
  return tokens;
}

function baseSeries(shape: NormalizedChartRenderShapeV1): ChartSeriesPresentationV2[] {
  return shape.series.map((series, index) => ({
    key: series.key,
    paletteIndex: index,
    lineDash: [],
    pointStyle: "circle",
    fillPattern: "solid",
    boundaryWidthPx: 0,
  }));
}

/** Roles whose marks are areas; they carry the fill-pattern channel. */
const FILLED_ROLES = new Set(["bar", "area", "segment"]);

/** How many leading palette colors may identify series by color alone. */
export function colorOnlySeriesCapacity(theme: ChartTheme): number {
  const paletteLength = Math.max(1, theme.chartStyleV2?.categoricalSeriesPalette.length ?? theme.palette.length);
  const declared = theme.chartStyleV2?.colorOnlySeriesCapacity;
  return typeof declared === "number" && Number.isInteger(declared) && declared >= 1
    ? Math.min(declared, paletteLength)
    : paletteLength;
}

export interface SeriesEncodingInput {
  roles: readonly string[];
  paletteLength: number;
  capacity: number;
  colorSafe: boolean;
  /** Stacked layout: segments touch and need a visible seam. */
  stacked: boolean;
}

export type SeriesEncoding = Pick<
  ChartSeriesPresentationV2,
  "paletteIndex" | "lineDash" | "pointStyle" | "fillPattern" | "boundaryWidthPx"
>;

/** Measured capacity, clamped to the palette and to the product-wide `nonColor.colorOnlySeriesCap`
 *  (`null` = no cap; `6` = "texture from the 7th series"). One policy value switches both runtimes. */
function effectiveCapacity(capacity: number, paletteLength: number): number {
  const cap: number | null = CHART_STYLE_PRESENTATION_POLICY.nonColor.colorOnlySeriesCap;
  const clamped = cap === null ? Math.min(capacity, paletteLength) : Math.min(capacity, paletteLength, cap);
  return Math.max(1, clamped);
}

/**
 * Assign every series its (color, second channel) pair, or `null` when color alone is enough.
 *
 * Mirrors `assign_series_encodings` in `bowhead_core/chart_style_presentation.py`; both run the
 * shared `presentation_fixtures.json` and `series_encoding_grid.json`, so the screen and the
 * export paint the same pattern on the same series.
 *
 *  - Color never changes meaning: series i keeps palette color `i % paletteLength`, as before
 *    textures existed, so no chart shows fewer distinct colors than it used to (a low-capacity
 *    team palette must not collapse two series into one color).
 *  - `seriesCount <= capacity` (and not color-safe) keeps color only: unchanged pixels.
 *  - Above it the second channel is layered on top: series i asks for channel `floor(i / capacity)`
 *    (color-safe: `i`), so block b of `capacity` series gets the b-th pattern / dash.
 *  - The (color, channel) pair stays unique per mark family as long as no color is used by more
 *    series than there are variants (always true up to `paletteLength × variants` series): a taken
 *    pair moves to the color's next free channel. The color itself never moves.
 *  - Only stacked bar segments get the 1px surface-colored separator: segments touch, and every
 *    palette color has ≥3:1 against the surface. Grouped bars do not touch; a border there only
 *    ate into thin bars.
 */
export function assignSeriesEncodings(input: SeriesEncodingInput): SeriesEncoding[] | null {
  const policy = CHART_STYLE_PRESENTATION_POLICY.nonColor;
  const paletteLength = Math.max(1, input.paletteLength);
  const capacity = effectiveCapacity(input.capacity, paletteLength);
  if (!input.colorSafe && input.roles.length <= capacity) return null;
  const lineVariants = Math.min(policy.lineDashes.length, policy.pointStyles.length);
  const used = new Map<string, Set<number>>();
  return input.roles.map((role, index) => {
    const family = FILLED_ROLES.has(role) ? "fill" : "line";
    const variants = family === "fill" ? policy.fillPatterns.length : lineVariants;
    const color = index % paletteLength;
    const key = `${family}:${color}`;
    const taken = used.get(key) ?? new Set<number>();
    used.set(key, taken);
    if (taken.size >= variants) taken.clear();
    const preferredChannel = (input.colorSafe ? index : Math.floor(index / capacity)) % variants;
    let channel = preferredChannel;
    for (let step = 0; step < variants; step += 1) {
      const candidate = (preferredChannel + step) % variants;
      if (!taken.has(candidate)) {
        channel = candidate;
        break;
      }
    }
    taken.add(channel);
    return {
      paletteIndex: color,
      lineDash: [...policy.lineDashes[channel % policy.lineDashes.length]!],
      pointStyle: policy.pointStyles[channel % policy.pointStyles.length]!,
      fillPattern: family === "fill" ? policy.fillPatterns[channel]! : "solid",
      boundaryWidthPx: input.stacked && role === "bar" ? policy.filledMarkBoundaryWidthPx : 0,
    };
  });
}

function encodingLabel(series: ChartSeriesPresentationV2): string {
  return `${series.key}:dash=${series.lineDash.join("-")}:point=${series.pointStyle}:fill=${series.fillPattern}:boundary=${series.boundaryWidthPx}`;
}

function adjustmentKey(adjustment: ChartStyleAdjustmentV2): string {
  const rank = CHART_STYLE_PRESENTATION_POLICY.adjusterRanks[adjustment.adjustedBy];
  return `${adjustment.path}\u0000${String(rank).padStart(2, "0")}\u0000${adjustment.reasonCodes.join("\u0000")}`;
}

/** Resolve renderer-independent style intent against content and real surface capabilities. */
export function resolveChartPresentation(
  shape: NormalizedChartRenderShapeV1,
  theme: ChartTheme,
  renderContext: ChartRenderContext,
): ResolvedChartPresentationV2 {
  const policy = CHART_STYLE_PRESENTATION_POLICY;
  const reasons = policy.reasonCodes;
  const source = tokensFor(theme, shape.kind);
  const requested: ChartPresentationTokensV2 = {
    tooltip: source.tooltip,
    grid: source.grid,
    ...(source.line
      ? {
          line: {
            ...source.line,
            hitRadiusPx: policy.line.hiddenHitRadiusPx,
            pointHoverRadiusPx: Math.max(policy.line.minimumHoverRadiusPx, source.line.pointRadiusPx + 1),
          },
        }
      : {}),
    ...(source.bar ? { bar: { ...source.bar } } : {}),
    series: baseSeries(shape),
  };
  const effective: ChartPresentationTokensV2 = {
    ...requested,
    ...(requested.line ? { line: { ...requested.line } } : {}),
    ...(requested.bar ? { bar: { ...requested.bar } } : {}),
    series: requested.series.map((series) => ({ ...series, lineDash: [...series.lineDash] })),
  };
  const adjustments = new Map<string, ChartStyleAdjustmentV2>();
  const adjust = (
    path: string,
    requestedValue: ChartStyleAdjustmentV2["requested"],
    effectiveValue: ChartStyleAdjustmentV2["effective"],
    adjustedBy: ChartStyleAdjustmentV2["adjustedBy"],
    reasonCode: string,
  ) => {
    if (JSON.stringify(requestedValue) === JSON.stringify(effectiveValue)) return;
    const existing = adjustments.get(path);
    if (existing) {
      existing.effective = effectiveValue;
      existing.adjustedBy = adjustedBy;
      existing.reasonCodes = [...new Set([...existing.reasonCodes, reasonCode])].sort();
      return;
    }
    adjustments.set(path, {
      path,
      requested: requestedValue,
      effective: effectiveValue,
      adjustedBy,
      reasonCodes: [reasonCode],
    });
  };

  if (renderContext.output.static) {
    effective.tooltip = "disabled";
    adjust(
      "tooltip",
      requested.tooltip,
      effective.tooltip,
      "context",
      reasons.staticTooltip,
    );
  } else if (requested.tooltip === "disabled" && renderContext.exactDataAlternative.kind === "none") {
    effective.tooltip = "enabled";
    adjust(
      "tooltip",
      requested.tooltip,
      effective.tooltip,
      "capability",
      reasons.missingExactData,
    );
  }

  if (
    effective.grid !== "standard"
    && (renderContext.forcedColors || theme.accessibility?.contrast === "high")
  ) {
    effective.grid = "standard";
    adjust(
      "grid",
      requested.grid,
      effective.grid,
      "accessibility",
      reasons.gridStructure,
    );
  }

  if (requested.line && effective.line) {
    if (
      (renderContext.forcedColors || theme.accessibility?.contrast === "high")
      && effective.line.widthPx < policy.line.highContrastMinimumWidthPx
    ) {
      effective.line.widthPx = policy.line.highContrastMinimumWidthPx;
      adjust(
        "line.widthPx",
        requested.line.widthPx,
        effective.line.widthPx,
        "accessibility",
        reasons.lineWidthMinimum,
      );
    }
    const pointCount = Math.max(0, ...shape.series.filter((series) =>
      series.role === "line" || series.role === "area"
    ).map((series) => series.pointCount));
    const recipe = resolveChartRenderRecipe(renderContext, shape);
    const showMaximum = policy.line.showPointMaximumByWidthClass[recipe.widthClass];
    if (effective.line.points === "show" && pointCount > showMaximum) {
      effective.line.points = "auto";
      adjust(
        "line.points",
        requested.line.points,
        effective.line.points,
        "correctness",
        reasons.pointDensity,
      );
    }
    const visible = effective.line.points === "show"
      || (effective.line.points === "auto" && pointCount <= policy.line.autoPointMaximum);
    if (!visible) effective.line.pointRadiusPx = 0;
  }

  if (requested.bar && effective.bar && shape.categoryCount > 0) {
    const bars = shape.series.filter((series) => series.role === "bar");
    const groups = shape.flags.stacked
      ? Math.max(1, new Set(bars.map((series) => series.stackKey ?? series.key)).size)
      : Math.max(1, bars.length);
    const axisSpan = shape.kind === "hbar" ? renderContext.bounds.heightPx : renderContext.bounds.widthPx;
    const estimatedThickness = axisSpan / shape.categoryCount / groups * policy.bar.reviewedOccupancy;
    const limited = Math.max(0, Math.min(effective.bar.radiusPx, estimatedThickness / 2));
    effective.bar.radiusPx = Math.round(limited * 1000) / 1000;
    adjust(
      "bar.radiusPx",
      requested.bar.radiusPx,
      effective.bar.radiusPx,
      "correctness",
      reasons.barRadiusLimited,
    );
  }

  const colorSafe = theme.accessibility?.colorVision === "color-safe" || renderContext.forcedColors;
  const encodings = assignSeriesEncodings({
    roles: shape.series.map((series) => series.role),
    paletteLength: Math.max(1, theme.chartStyleV2?.categoricalSeriesPalette.length ?? theme.palette.length),
    capacity: colorOnlySeriesCapacity(theme),
    colorSafe,
    stacked: shape.flags.stacked,
  });
  if (encodings) {
    effective.series = effective.series.map((series, index) => ({ ...series, ...encodings[index]! }));
    adjust(
      "seriesEncodings",
      requested.series.map((series) => `${series.key}:color`),
      effective.series.map(encodingLabel),
      colorSafe ? "accessibility" : "correctness",
      colorSafe ? reasons.nonColorRequired : reasons.paletteOverflow,
    );
  }

  return {
    requested,
    effective,
    adjustments: [...adjustments.values()].sort((first, second) => {
      const firstKey = adjustmentKey(first);
      const secondKey = adjustmentKey(second);
      return firstKey < secondKey ? -1 : firstKey > secondKey ? 1 : 0;
    }),
    renderShapeVersion: policy.renderShapeVersion,
    presentationPolicyVersion: policy.presentationPolicyVersion,
  };
}
