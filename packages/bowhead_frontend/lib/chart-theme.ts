"use client";

/**
 * Chart theme — chart color system, defaulting to product CSS vars, automatically following light/dark
 * themes and using server-resolved team/personal chart-style configuration.
 *
 * Palette (WP2/C3): report-refactor design D8 chart palette — cream/terracotta
 * document-language chart family (terracotta primary/slate secondary/dijon anomaly/earth/sand/ink).
 * UI backgrounds (ink/surface/grid) still read --c-* tokens and follow theme.
 */

import { createContext, createElement, useContext, useEffect, useState, type ReactNode } from "react";

import { getBrandSnapshot } from "./brand-runtime";
import { chartStylePreset, type ChartStylePresetPackage } from "./chart/chart-style-registry";
import { colorAtOpacity } from "./chart/color";
import type { ChartStyleThemeV2 } from "./chart/chart-style-presentation";
import type { EffectiveChartStyleV2 } from "./chart/chart-style-export";
import type {
  EffectiveSurfacePaletteProjectionV2,
  ResolvedChartStyleSnapshotV2,
} from "./chart/chart-style-export";
import type { ChartKind } from "./chart/types";
import {
  BRAND_BUNDLE_CHANGED_EVT,
  THEME_CONFIG_CHANGED_EVT,
  type ChartColorVision,
  type ChartContrast,
  type ChartStylePresetId,
} from "./theme-config";

export interface ChartTheme {
  accessibility: {
    contrast: ChartContrast;
    colorVision: ChartColorVision;
  };
  /** Sequential colors for multiple series; >6 cycles (prompt LLM to consider stacking/faceting). */
  palette: string[];
  /** Primary color for single-series charts (line / single-metric bar / KPI card). */
  primary: string;
  /** Continuous gradient (heatmap, low → high). */
  sequential: { from: string; to: string };
  /** Diverging gradient (growth / decline / midpoint). */
  diverging: { neg: string; mid: string; pos: string };
  /** Text / background / grid. `grid` is the strong hairline (value-axis zero line, category-axis base line,
   *  high-contrast grid); `gridSoft` is the ordinary gridline. Both follow the UI separator tokens
   *  (`--c-separator-strong` / `--c-separator`: ink at 20 / 12% light, 24 / 14% dark), see `separatorGrid`. */
  ink: string;
  /** Secondary ink (`--c-ink-2`) for category ticks and data labels (Chart Embed Spec D1); omitted = `inkSoft`. */
  ink2?: string;
  inkSoft: string;
  surface: string;
  grid: string;
  gridSoft: string;
  /** Interface sans-serif stack (`--font-v2-sans`) for every text the chart draws on canvas (Chart Embed Spec D1):
   *  ticks, data labels, axis titles and tooltip titles. Canvas cannot resolve CSS variables, so the resolved
   *  stack is read here; omitted = `CHART_FONT_SANS_FALLBACK`. */
  fontSans?: string;
  /** Semantic colors. */
  positive: string;
  negative: string;
  warn: string;
  /** Renderer-only geometry; never written to ChartSpec. */
  lineWidth: number;
  pointRadius: number;
  barRadius: number;
  gridDensity: "quiet" | "standard" | "strong";
  /** Renderer-independent V2 intent. Legacy scalar fields remain adapter inputs only. */
  chartStyleV2?: ChartStyleThemeV2;
}

const CHART_STYLE_KINDS: ChartKind[] = [
  "line", "area_stacked", "bar", "hbar", "stacked_bar", "pie", "dual_axis",
  "combo", "scatter", "heatmap", "waterfall", "sankey",
];
const LINE_KINDS = new Set<ChartKind>(["line", "area_stacked", "dual_axis", "combo"]);
const BAR_KINDS = new Set<ChartKind>(["bar", "hbar", "stacked_bar", "waterfall"]);

function builtInStyleV2(
  preset: Readonly<ChartStylePresetPackage>,
  palette: string[],
): ChartStyleThemeV2 {
  const geometry = preset.tokens.geometry;
  const grid = geometry.gridDensity === "strong" ? "standard" : geometry.gridDensity;
  return {
    presentationPolicyVersion: "chart-presentation/1",
    categoricalSeriesPalette: [...palette],
    maxDistinctSeriesColors: palette.length,
    paletteOverflowRecipeVersion: "palette-overflow/2",
    // Built-in base palettes are held to the distinctness targets by the registry check, so the
    // whole local palette is color-only capable.
    colorOnlySeriesCapacity: palette.length,
    kindTokens: Object.fromEntries(CHART_STYLE_KINDS.map((kind) => [kind, {
      tooltip: "enabled" as const,
      grid,
      ...(LINE_KINDS.has(kind)
        ? {
            line: {
              curve: "monotone" as const,
              widthPx: geometry.lineWidth,
              points: "auto" as const,
              pointRadiusPx: geometry.pointRadius,
            },
          }
        : {}),
      ...(BAR_KINDS.has(kind) ? { bar: { radiusPx: geometry.barRadius } } : {}),
    }])) as ChartStyleThemeV2["kindTokens"],
  };
}

/** Product default chart style when nothing else is known (no team policy loaded yet, SSR, tests).
 *  Mirrors `PRODUCT_DEFAULT_PRESET_ID` in `bowhead_core/chart_style.py`; the palettes are read from
 *  the generated registry so they can never drift from the reviewed preset. */
export const PRODUCT_DEFAULT_CHART_STYLE_ID = "clean-blue" as const;
const PRODUCT_DEFAULT_TOKENS = chartStylePreset(PRODUCT_DEFAULT_CHART_STYLE_ID).tokens;
const PALETTE_LIGHT: string[] = [...PRODUCT_DEFAULT_TOKENS.light.palette];
const PALETTE_DARK: string[] = [...PRODUCT_DEFAULT_TOKENS.dark.palette];

/** Mirror of `--font-v2-sans` (globals.css) for SSR, tests and surfaces without the token. */
export const CHART_FONT_SANS_FALLBACK =
  'Inter, -apple-system, "PingFang SC", "Source Han Sans SC", "Noto Sans SC", system-ui, sans-serif';

/**
 * Chart gridlines are hairline separators, not control borders (Chart Embed Spec D2): the ordinary gridline is
 * `--c-separator` (ink 12% light / 14% dark) and the zero / base line `--c-separator-strong` (20% / 24%).
 * The tokens are `color-mix()` expressions that canvas cannot parse, so the same mix is computed from the ink here;
 * this also keeps export (light paper, print ink) and local previews on the same rule without reading the DOM.
 */
export function separatorGrid(ink: string, mode: "light" | "dark"): { grid: string; gridSoft: string } {
  return mode === "dark"
    ? { grid: colorAtOpacity(ink, 0.24), gridSoft: colorAtOpacity(ink, 0.14) }
    : { grid: colorAtOpacity(ink, 0.2), gridSoft: colorAtOpacity(ink, 0.12) };
}

const FALLBACK: ChartTheme = {
  accessibility: { contrast: "standard", colorVision: "standard" },
  palette: PALETTE_LIGHT,
  primary: PALETTE_LIGHT[0]!,
  sequential: { ...PRODUCT_DEFAULT_TOKENS.light.sequential },
  diverging: { neg: "#9f2438", mid: "#e8e2d4", pos: "#3a724a" },
  ink: "#161310",
  ink2: "#3a342c",
  inkSoft: "#56503f",
  surface: "#ffffff",
  ...separatorGrid("#161310", "light"),
  fontSans: CHART_FONT_SANS_FALLBACK,
  positive: "#3a724a",
  negative: "#9f2438",
  warn: "#a98028",
  lineWidth: 2,
  pointRadius: 3,
  barRadius: 4,
  gridDensity: "quiet",
};

/** Read current theme from CSS vars; fall back to FALLBACK for SSR / testing.
 *
 * Palette follows Claude Design Charts.html (five colors derived from #2E5D4F).
 * CSS vars override only UI backgrounds such as ink / surface / grid; palette remains hardcoded
 * to ensure charts pass WCAG in every theme.
 */
export function defaultChartTheme(): ChartTheme {
  if (typeof window === "undefined" || typeof document === "undefined") {
    return FALLBACK;
  }
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string, fb: string) => cs.getPropertyValue(name).trim() || fb;
  const isDark = document.documentElement.dataset.theme === "dark";
  const palette = isDark ? PALETTE_DARK : PALETTE_LIGHT;
  const ink = v("--c-ink", FALLBACK.ink);

  return {
    accessibility: { contrast: "standard", colorVision: "standard" },
    palette,
    primary: palette[0],
    sequential: {
      from: v("--c-accent-tint", FALLBACK.sequential.from),
      to:   v("--c-accent", palette[0]),
    },
    diverging: {
      neg: v("--c-neg", FALLBACK.diverging.neg),
      mid: v("--c-line", FALLBACK.diverging.mid),
      pos: v("--c-pos", FALLBACK.diverging.pos),
    },
    ink,
    ink2:     v("--c-ink-2",     v("--c-ink-3", FALLBACK.inkSoft)),
    inkSoft:  v("--c-ink-3",     FALLBACK.inkSoft),
    surface:  v("--c-surface",   FALLBACK.surface),
    ...separatorGrid(ink, isDark ? "dark" : "light"),
    fontSans: v("--font-v2-sans", CHART_FONT_SANS_FALLBACK),
    positive: v("--c-pos",       FALLBACK.positive),
    negative: v("--c-neg",       FALLBACK.negative),
    warn:     v("--c-warn",      FALLBACK.warn),
    lineWidth: FALLBACK.lineWidth,
    pointRadius: FALLBACK.pointRadius,
    barRadius: FALLBACK.barRadius,
    gridDensity: FALLBACK.gridDensity,
  };
}

/** Resolve the single supported chart-style path. */
export function resolveChartTheme(): ChartTheme {
  const base = defaultChartTheme();
  const effectiveStyle = getBrandSnapshot().chartStyle;
  if (effectiveStyle?.schemaVersion === 2) {
    return resolveChartThemeForEffectiveV2(effectiveStyle, base);
  }
  return resolveChartThemeForStyle(
    effectiveStyle?.preset.id ?? PRODUCT_DEFAULT_CHART_STYLE_ID,
    effectiveStyle?.accessibility,
    base,
  );
}

/** Build the live renderer theme from the server-resolved V2 display snapshot. */
export function resolveChartThemeForEffectiveV2(
  effective: EffectiveChartStyleV2,
  base = defaultChartTheme(),
  modeOverride?: "light" | "dark",
): ChartTheme {
  const mode = modeOverride ?? (
    typeof document !== "undefined" && document.documentElement.dataset.theme === "dark"
      ? "dark"
      : "light"
  );
  const projection = effective.modeColors[mode];
  return resolveChartThemeForV2Snapshot(
    effective.style.resolvedPresetSnapshot,
    projection,
    effective.accessibility,
    base,
    mode,
  );
}

/** The gateway-measured color-only prefix (`finalPaletteQA.colorOnlySeriesCapacity`), when present. */
export function colorOnlyCapacityOf(projection: EffectiveSurfacePaletteProjectionV2): { colorOnlySeriesCapacity?: number } {
  const value = projection.finalPaletteQA.colorOnlySeriesCapacity;
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? { colorOnlySeriesCapacity: value } : {};
}

export function resolveChartThemeForV2Snapshot(
  snapshot: ResolvedChartStyleSnapshotV2,
  projection: EffectiveSurfacePaletteProjectionV2,
  accessibility: { contrast: ChartContrast; colorVision: ChartColorVision },
  base = defaultChartTheme(),
  mode: "light" | "dark" = "light",
): ChartTheme {
  const kindTokens = snapshot.kindTokens as ChartStyleThemeV2["kindTokens"];
  const line = kindTokens.line?.line;
  const bar = kindTokens.bar?.bar;
  const grid = kindTokens.line?.grid ?? "quiet";
  const palette = [...projection.categoricalSeriesPalette];
  return {
    ...base,
    accessibility: {
      contrast: accessibility.contrast,
      colorVision: accessibility.colorVision,
    },
    palette,
    primary: palette[0] ?? base.primary,
    sequential: { ...projection.colors.sequential },
    diverging: { ...projection.colors.diverging },
    lineWidth: line?.widthPx ?? base.lineWidth,
    pointRadius: line?.pointRadiusPx ?? base.pointRadius,
    barRadius: bar?.radiusPx ?? base.barRadius,
    gridDensity: grid === "standard" ? "standard" : "quiet",
    chartStyleV2: {
      presentationPolicyVersion: "chart-presentation/1",
      kindTokens,
      categoricalSeriesPalette: palette,
      maxDistinctSeriesColors: projection.maxDistinctSeriesColors,
      paletteOverflowRecipeVersion: projection.paletteOverflowRecipeVersion,
      ...colorOnlyCapacityOf(projection),
    },
    ...(accessibility.contrast === "high"
      ? {
          ink: mode === "dark" ? "#FFFFFF" : "#000000",
          grid: mode === "dark" ? "#8A8A8A" : "#777777",
          gridDensity: "strong" as const,
        }
      : {}),
  };
}

export function resolveChartThemeForStyle(
  presetId: ChartStylePresetId,
  accessibility: { contrast: ChartContrast; colorVision: ChartColorVision } = {
    contrast: "standard",
    colorVision: "standard",
  },
  base = defaultChartTheme(),
  modeOverride?: "light" | "dark",
): ChartTheme {
  const preset = chartStylePreset(presetId);
  return resolveChartThemeForPresetPackage(
    preset,
    accessibility,
    base,
    modeOverride,
  );
}

export function resolveChartThemeForPresetPackage(
  preset: Readonly<ChartStylePresetPackage>,
  accessibility: { contrast: ChartContrast; colorVision: ChartColorVision } = {
    contrast: "standard",
    colorVision: "standard",
  },
  base = defaultChartTheme(),
  modeOverride?: "light" | "dark",
): ChartTheme {
  const mode = modeOverride ?? (
    typeof document !== "undefined" && document.documentElement.dataset.theme === "dark"
      ? "dark"
      : "light"
  );
  const colors = preset.tokens[mode];
  const palette = accessibility.colorVision === "color-safe"
    ? ["#4477AA", "#EE6677", "#228833", "#CCBB44", "#66CCEE", "#AA3377"]
    : colors.palette;
  return {
    ...base,
    ...preset.tokens.geometry,
    accessibility,
    palette,
    primary: palette[0],
    sequential: colors.sequential,
    diverging: colors.diverging,
    chartStyleV2: builtInStyleV2(preset, palette),
    ...(accessibility.contrast === "high"
      ? {
          ink: mode === "dark" ? "#FFFFFF" : "#000000",
          grid: mode === "dark" ? "#8A8A8A" : "#777777",
          gridDensity: "strong" as const,
        }
      : {}),
  };
}

/** Neutral surface tokens for a local preview that must not change the app theme. */
export function chartThemeBaseForMode(mode: "light" | "dark"): ChartTheme {
  const base = defaultChartTheme();
  if (mode === "light") {
    return {
      ...base,
      ink: "#161310",
      ink2: "#3A342C",
      inkSoft: "#56503F",
      surface: "#FFFFFF",
      ...separatorGrid("#161310", "light"),
      positive: "#3A724A",
      negative: "#9F2438",
      warn: "#A98028",
    };
  }
  return {
    ...base,
    ink: "#ECEAE3",
    ink2: "#CAC7BF",
    inkSoft: "#A8A49B",
    surface: "#1B1A17",
    ...separatorGrid("#ECEAE3", "dark"),
    positive: "#62C491",
    negative: "#E88A98",
    warn: "#E5B65A",
  };
}

const ChartThemePreviewContext = createContext<ChartTheme | null>(null);

export function ChartThemePreviewProvider({
  theme,
  children,
}: {
  theme: ChartTheme;
  children: ReactNode;
}) {
  return createElement(ChartThemePreviewContext.Provider, { value: theme }, children);
}

/**
 * React hook: automatically subscribe to
 *  - data-theme / data-color-profile / data-chart-palette / class changes
 *  - theme config / chart theme events
 */
export function useChartTheme(): ChartTheme {
  const previewTheme = useContext(ChartThemePreviewContext);
  const [theme, setTheme] = useState<ChartTheme>(() => resolveChartTheme());

  useEffect(() => {
    const sync = () => setTheme(resolveChartTheme());
    const root = document.documentElement;
    const obs = new MutationObserver(sync);
    obs.observe(root, {
      attributes: true,
      attributeFilter: ["data-theme", "data-color-profile", "data-chart-palette", "data-typography-profile", "class"],
    });
    window.addEventListener(THEME_CONFIG_CHANGED_EVT, sync);
    window.addEventListener(BRAND_BUNDLE_CHANGED_EVT, sync);
    window.addEventListener("storage", sync); // Cross-tab sync.
    return () => {
      obs.disconnect();
      window.removeEventListener(THEME_CONFIG_CHANGED_EVT, sync);
      window.removeEventListener(BRAND_BUNDLE_CHANGED_EVT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  return previewTheme ?? theme;
}
