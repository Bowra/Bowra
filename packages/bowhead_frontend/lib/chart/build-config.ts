import { analyticalDateTicks } from "./analytical";
/**
 * Chart.js configuration generator — pure function: ChartSpec + Theme → Chart.js options.
 *
 * Design principles:
 *   - No side effects (no window / localStorage reads; theme comes from an argument)
 *   - One builder per kind, sharing base options to avoid duplication
 *   - All colors come from theme; no hardcoded color values
 */

import { buildAnalyticalSeries } from "./analytical-series";
import type { CartesianScaleOptions, Chart, ChartData, ChartOptions, ChartType, FontSpec, LegendItem, Plugin, Scale } from "chart.js";
import { toFont, toPadding } from "chart.js/helpers";

import { CHART_FONT_SANS_FALLBACK, type ChartTheme } from "../chart-theme";
import { applyChartJsStylePresentation } from "./chartjs-style-adapter";
import { roleOrderedDims } from "./stack-dims";
import {
  effectiveHiddenSeries,
  seriesKeysOfDatasets,
  type SeriesKey,
} from "./series-visibility";
import {
  resolveChartPresentation,
  type ResolvedChartPresentationV2,
} from "./chart-style-presentation";
import {
  abbrTick,
  formatObservedValue,
  formatSourceValue,
  formatSourcePercent,
  isUnsafeIntegerCell,
  formatDisplayValue,
  resolveDisplayMagnitude,
  resolveAxisMagnitude,
  formatAxisTick,
  measureDisplayName,
  type DisplayMagnitude,
  formatDateLabel,
  formatPercentCell,
  calendarAxisTickLabels,
  axisUnitOf,
  isDisplayableUnit,
  resolvePercentColumn,
  resolvePercentScale,
  type PercentScale,
} from "./format";
import { seriesColor, withAlpha } from "./color";
import {
  edgeLabelPadding,
  tickAvoidAlign,
  topLabelPadding,
  type AxisTickTextBox,
} from "./edge-label-padding";
import { pickScatterLabels, type LabelAlign } from "./scatter-labels";
import {
  placePointLabels,
  type LabelBox,
  type PointLabelMarker,
  type PointLabelRequest,
  type PointLabelSegment,
} from "./point-label-placement";
import { mutedOf, resolvePresentation, type Presentation } from "./presentation";
import { lineEndpointLabelsPlugin } from "./line-endpoint-labels";
import {
  defaultChartRenderBounds,
  resolveChartRenderRecipe,
  type ChartRenderContext,
  type ChartRenderRecipe,
} from "./render-context";
import {
  deriveNormalizedChartRenderShapeV1,
  dualAxisColumns,
  normalizeChartForRenderingV1,
} from "./normalize-chart-for-rendering";
import type { ChartColumn, ChartSpec } from "./types";
import { markBindings, type FocusBinding, type MarkBinding } from "./linked-focus";
import type { AxisDomains } from "./shared-scales";
import { annotationScopePlugin, observedAnnotationPlugin } from "./annotations";

export interface ChartJSConfig {
  markBindings?: MarkBinding[][];
  type: ChartType;
  data: ChartData;
  options: ChartOptions;
  /** Batch 1 2.3 (P2-7): heatmap color-scale legend; ChartBlock renders an HTML gradient below the chart. */
  heatmapLegend?: { min: number; max: number; from: string; to: string; unit?: string; percent?: PercentScale; display?: DisplayMagnitude | null };
  /** Batch 1 2.1b: per-chart plugins (pie center headline number, etc.), forwarded through ReactChart plugins. */
  plugins?: Plugin[];
}

/** Batch 1 2.2: build options. surface sets the default data-label policy (static exports stay readable → always on);
 *  ChartBlock supplies the measured render context; explicit callers may still request labels.
 *  captionText = attached caption text (2.1), input to protagonist resolution (2.1b), defaulting to spec.subtitle. */
export interface BuildOpts {
  visibleShareLabel?: string;
  activeAnnotationId?: string;
  axisDomains?: AxisDomains | null;
  focusBinding?: { descriptor: FocusBinding; datasetDigest: string; scopeSource?: ChartSpec };
  surface?: "canvas" | "chat" | "export";
  forceDataLabels?: boolean;
  captionText?: string | null;
  /** Current UI/output language (`useLocale()`). For en, translate Chinese x-axis time buckets (`2026年1月`)
   *  and time-grain column names (`每月`) into English: both are **closed sets**, mapped mechanically without invention.
   *  A pure function cannot read window, so this is its only input (like theme). Default = no translation. */
  locale?: string;
  /** Real renderer capabilities used to adapt the visual recipe per surface. */
  renderContext?: ChartRenderContext;
  /** Server-frozen export presentation. Public export surfaces must supply it. */
  resolvedPresentation?: ResolvedChartPresentationV2;
  /** Currently hidden series (numeric column keys). **Single source of state**, see `series-visibility.ts`:
   *  canvas legend and inspection chips write the same state; this pure function only applies `hidden`. */
  hiddenSeries?: readonly SeriesKey[];
  /** Legend-click callback (receives series key, not datasetIndex). Override legend.onClick only when provided;
   *  otherwise retain the Chart.js default handler. */
  onToggleSeries?: (key: SeriesKey) => void;
}

interface InternalBuildOpts extends BuildOpts {
  normalizedShape?: ReturnType<typeof deriveNormalizedChartRenderShapeV1>;
  /** Resolved before the builders run, so legends and colors follow the same series encodings. */
  presentation?: ResolvedChartPresentationV2;
}

function renderShape(spec: ChartSpec, opts: BuildOpts): ReturnType<typeof deriveNormalizedChartRenderShapeV1> {
  return (opts as InternalBuildOpts).normalizedShape ?? deriveNormalizedChartRenderShapeV1(spec);
}

function renderRecipe(
  spec: ChartSpec,
  theme: ChartTheme,
  opts: BuildOpts,
): ChartRenderRecipe | null {
  if (!opts.renderContext) return null;
  return resolveChartRenderRecipe(opts.renderContext, renderShape(spec, opts));
}

/** Batch 1 2.2 (P1-5): honor explicit spec.show_data_labels; without an explicit value,
 *  always enable labels where no alternative exists for reading values (exports/embeds lack hover and "查看数据", PRD §3.9)
 *  → likewise on mobile (passed by ChartBlock)
 *  → otherwise use a soft guideline: one series and ≤12 categories default to enabled (no hard limit).
 *
 *  ★★ Batch 2: the above **never executed once** after launch. Server `ChartSpec.show_data_labels`
 *  defaulted to `False`, and `spec_to_dict` persisted it unchanged (`_drop_empty` only drops None/[]/{}),
 *  so the first explicit-boolean-wins branch always matched, leaving the next four levels dead: production 116
 *  had `false` on 2781/2781 charts, with no assignments anywhere in Python. The server default is now
 *  `None` (chart/types.py), allowing this ladder to run for the first time.
 *  ⇒ Corpus measurements: 38.0% of canvas charts and 77.4% of export charts gain data labels. */
export function resolveDataLabels(
  spec: ChartSpec,
  opts: BuildOpts,
  nSeries: number,
  nCats: number,
  recipe: ChartRenderRecipe | null = null,
): boolean {
  if (typeof spec.show_data_labels === "boolean") return spec.show_data_labels;
  if (hasNoReadbackPath(opts)) return true;
  if (opts.forceDataLabels) return true;
  if (recipe?.forceDataLabels) return true;
  const capacity = recipe?.widthClass === "compact" ? 6 : recipe?.widthClass === "wide" ? 16 : 12;
  return nSeries === 1 && nCats <= capacity;
}

/** Whether this surface offers another way to read exact numbers.
 *
 *  `surface === "export"` is only a proxy; `renderContext` holds the actual facts: raster output
 *  (`output.static`), or neither hover nor a "查看数据" table
 *  (`exactDataAlternative.kind === "none"`, e.g. an embed without chrome).
 *  Both alternatives unavailable ⇒ labels are the only reading path; prefer dense labels and let `"auto"` hide those
 *  that do not fit (see `labelDisplay`), rather than predicting density here. */
function hasNoReadbackPath(opts: BuildOpts): boolean {
  if (opts.surface === "export") return true;
  const ctx = opts.renderContext;
  if (!ctx) return false;
  return ctx.output.static || (!ctx.input.hover && ctx.exactDataAlternative.kind === "none");
}

/** Centralize the three datalabels `display` states.
 *
 *  `false` omits labels / `true` draws and **never hides** them / `"auto"` draws but hides them
 *  when they overlap other labels: `"auto"` is an existing plugin primitive (`dist/chartjs-plugin-datalabels.js`
 *  `_hidable` + `collide()`: test hitbox intersections after layout).
 *
 *  ★ `collide()` only hides the hidable side. Give the protagonist `true` and others `"auto"`,
 *  so **others yield to the protagonist** when crowded, matching the 2.1b protagonist design.
 *
 *  ⚠ Do not put these states directly into `showLabels && X`: `"auto" && true === true` silently
 *  degrades to never-hidden. Every call site computes a boolean first, then gets its display value here.
 *
 *  ⚠ Per-point identity labels (scatter) **cannot** use `"auto"`: it does not reposition them (top-edge points get clipped),
 *  and priority is merely reverse dataIndex, not importance. See `scatter-labels.ts` for that path. */
function labelDisplay(on: boolean, isLead = false): boolean | "auto" {
  if (!on) return false;
  return isLead ? true : "auto";
}

function defaultPresentationContext(opts: BuildOpts): ChartRenderContext {
  const surface = opts.surface ?? "canvas";
  const bounds = defaultChartRenderBounds(surface);
  const isStatic = surface === "export";
  return {
    placement: surface === "chat" ? "chat" : surface === "export" ? "embed" : "canvas",
    bounds: { ...bounds, devicePixelRatio: 1 },
    input: { hover: !isStatic, pointer: isStatic ? "none" : "fine", keyboard: !isStatic },
    output: { medium: isStatic ? "raster" : "screen", static: isStatic },
    mode: "light",
    locale: opts.locale ?? "zh-CN",
    reducedMotion: isStatic,
    forcedColors: false,
    exactDataAlternative: { kind: "none" },
  };
}

function themeWithEffectiveSeriesPalette(theme: ChartTheme): ChartTheme {
  const palette = theme.chartStyleV2?.categoricalSeriesPalette;
  if (!palette?.length || (palette.length === theme.palette.length && palette.every((color, index) => color === theme.palette[index]))) {
    return theme;
  }
  return { ...theme, palette: [...palette], primary: palette[0]! };
}

/** The same selected dimension drives category data and its display format. */
function categoryColumn(spec: ChartSpec): ChartColumn | undefined {
  const dims = spec.columns.filter((c) => c.type === "date" || c.type === "categorical" || c.type === "geo");
  if (["bar", "stacked_bar", "hbar"].includes(spec.kind)) {
    const categories = roleOrderedDims(dims.filter((c) => c.type === "categorical" || c.type === "geo"));
    if (spec.kind === "stacked_bar" && categories.length >= 2) return categories[0];
    const ordered = roleOrderedDims(dims);
    return preferIdentifyingDim(spec.rows ?? [], ordered[0], ordered);
  }
  return preferIdentifyingDim(
    spec.rows ?? [], spec.columns.find((c) => c.type === "date")
      ?? spec.columns.find((c) => c.type === "categorical" || c.type === "geo"), dims,
  );
}

/** Entry point: dispatch by kind. kpi / table / geo use their own components and bypass this function. */
export function buildChartJSConfig(spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {}): ChartJSConfig {
  if (spec.columns.some((column) => column.type === "numeric" && (spec.rows ?? []).some((row) => isUnsafeIntegerCell(row[column.key])))) {
    throw new Error("Exact integers exceed the native chart numeric range; use Table to preserve them.");
  }
  const axisDomains = opts.axisDomains ?? spec._export_axis_domains;
  const sourceSpec = spec;
  const normalized = normalizeChartForRenderingV1(spec, { trackOrigins: !!opts.focusBinding, scopeSource: opts.focusBinding?.scopeSource });
  spec = normalized.spec;
  theme = themeWithEffectiveSeriesPalette(theme);
  const presentation = opts.resolvedPresentation ?? resolveChartPresentation(
      normalized.shape,
      theme,
      opts.renderContext ?? defaultPresentationContext(opts),
    );
  // Builders keep painting series i with `palette[i % length]`: the presentation's `paletteIndex`
  // is that same positional color by contract (pinned in `series_encoding_grid.json`), so the
  // palette is never reordered or expanded. Expanding it once made `buildScatter`'s identity-mode
  // test (`groups > palette.length`) always false: 30 groups ⇒ 30 datasets and a 30-entry legend.
  const buildOpts: InternalBuildOpts = { ...opts, axisDomains: axisDomains?.encoding === spec.kind ? axisDomains : undefined, normalizedShape: normalized.shape, presentation };
  // Batch 1 2.1b: protagonist resolution (D5 contract). null = all full color; current behavior fails open.
  const pres = resolvePresentation(spec, opts.captionText ?? null);
  const config = spec._export_analytical?.preset === "series_plot" ? buildAnalyticalLine(spec, theme, buildOpts) : buildByKind(spec, theme, buildOpts, pres);
  // review R4-1 / R5-1: tooltip percentage scaling must be decided for the **whole column** (cellwise treatment of [0.83, 13.5]
  // differs by 100×). The original implementation attached whole-column rows to `config.data._rows` for the tooltip,
  // but **this never worked in the browser**: react-chartjs-2 constructs charts with `cloneData()`,
  // which only creates `{labels: [], datasets: []}` + setLabels/setDatasets
  // (node_modules/react-chartjs-2/dist/index.js:38), dropping every custom key on `data`.
  // `ctx.chart.data._rows` is always undefined → resolvePercentScale([]) → "points" → no ×100.
  // R4-1 merely relocated an already-dead mechanism; my test manually constructed
  // `chart: {data: cfg.data}`, bypassing cloning, so it still passed (a falsely passing guard test).
  //
  // Fix: attach scaling to the **dataset**. `setDatasets` spreads `{...nextDataset}`
  // (same file :22), preserving custom dataset keys; existing `_yCol` has always worked
  // for exactly this reason. Precompute column-level context here; tooltip reads it directly, without rows crossing layers.
  const formatted = unifyPercentScalePerAxis(withColumnFormatCtx(config, spec.rows ?? []));
  if (normalized.observedSpec) {
    const original = normalized.observedSpec;
    formatted.data.datasets = formatted.data.datasets.map((dataset) => {
      const column = (dataset as { _yCol?: ChartColumn })._yCol;
      const source = original.columns.find((item) => item.key === column?.key);
      const rows = original.rows ?? [];
      const pivotedStack = spec.kind === "stacked_bar" && spec.columns.filter((item) => item.type === "categorical" || item.type === "geo").length >= 2;
      if (!column || !source || source.unit === column.unit || pivotedStack || dataset.data.length !== rows.length
        || dataset.data.some((value, index) => value !== (spec.rows?.[index]?.[column.key] ?? null))) return dataset;
      return { ...dataset, _observedValues: rows.map((row) => row[source.key]), _observedFmtCtx: columnFormatCtx(source, rows) };
    }) as ChartData["datasets"];
  }
  const styled = applyChartJsStylePresentation(formatted, spec, normalized.shape, theme, presentation);
  if (spec._export_analytical) styled.data.datasets = styled.data.datasets.map((dataset) => ({ ...dataset, ...(dataset as unknown as { _analyticalStyle?: object })._analyticalStyle })) as ChartData["datasets"];
  for (const scale of Object.values(styled.options.scales ?? {})) {
    if (!scale) continue;
    const previousFit = scale.afterFit;
    scale.afterFit = (axis) => { previousFit?.call(axis, axis); fitNumericAxisLabels(axis); };
  }
  if (spec._export_frame_entities) styled.options.animation = false;
  if (axisDomains?.encoding === spec.kind) {
    for (const [axis, domain] of Object.entries(axisDomains.domains)) {
      const scale = styled.options.scales?.[axis];
      if (scale && domain?.length === 2 && domain.every(Number.isFinite) && domain[0] < domain[1]) {
        scale.min = domain[0]; scale.max = domain[1];
        // Bounds frame the data; generated interior ticks use readable decimal steps.
        scale.ticks = { ...scale.ticks, includeBounds: false };
      }
    }
  }
  const categoryScale = styled.options.scales?.[styled.options.indexAxis === "y" ? "y" : "x"];
  if (!spec._export_analytical && ["line", "bar", "hbar", "stacked_bar", "area_stacked", "combo", "dual_axis"].includes(spec.kind) && spec.columns.some((column) => column.type === "date") && styled.data.labels && categoryScale && (!categoryScale.type || categoryScale.type === "category") && categoryScale.ticks) {
    // Match the actual category labels, not an unrelated date column or repeated pivot rows.
    const labels = styled.data.labels.map(String);
    const axisColumn = categoryColumn(spec);
    const patterned = axisColumn?.type === "date" && axisColumn.date_pattern ? axisColumn : undefined;
    if (patterned) {
      const originals = new Map((spec.rows ?? []).flatMap((row) => {
        const raw = String(row[patterned.key] ?? "");
        return [[raw, raw], [formatDateLabel(raw, opts.locale), raw]] as const;
      }));
      styled.data.labels = labels.map((label) => formatDateLabel(originals.get(label)!, opts.locale, patterned.date_pattern));
    }
    const dateLabels = spec.columns.filter((column) => column.type === "date").map((column) => {
      const originals = new Map((spec.rows ?? []).flatMap((row) => {
        const raw = String(row[column.key] ?? "");
        return [[raw, raw], [formatDateLabel(raw, opts.locale), raw]] as const;
      }));
      return labels.every((label) => originals.has(label)) ? labels.map((label) => originals.get(label)!) : null;
    }).find((mapped) => mapped !== null);
    const monthTicks = dateLabels ? calendarAxisTickLabels(dateLabels, `${spec.title} ${spec.subtitle ?? ""}`, opts.locale) : null;
    if (monthTicks && !patterned) categoryScale.ticks.callback = (value) => monthTicks[Number(value)] ?? String(value);
  }
  if (spec.kind === "line" || spec.kind === "combo" || spec.kind === "dual_axis") {
    styled.data = {
      ...styled.data,
      datasets: styled.data.datasets.map((dataset, index) => {
        const type = dataset.type ?? styled.type;
        return type === "line" ? { ...dataset, _endpointLabelStyle: {
          font: `500 12px ${fontSans(theme)}`, ink: inkMid(theme),
          minimumPlotWidth: renderRecipe(spec, theme, buildOpts)?.endpointPlotWidthFloorPx ?? 160,
          priority: pres.lead?.kind === "dataset" && pres.lead.index === index ? 0 : 1,
        } } : dataset;
      }),
    } as ChartData;
  }
  // Batch 2 wrap-up: category-axis end labels can be clipped by canvas edges (116 dogfood photo: `-76.9` lost half of `9`).
  // Attach once for all builders, rather than repeating in four builders; plugin determines whether a chart needs adjustment.
  // Charts that already fit retain padding 0 and remain identical pixel by pixel.
  // Shift point labels sideways when they overlap value-axis ticks (116 mobile photo: first point 19.9 covered tick 20); adjust only conflicting labels.
  const withPlugins = {
    ...styled,
    plugins: [
      ...(styled.plugins ?? []),
      lineEndpointLabelsPlugin(),
      EDGE_LABEL_PADDING_PLUGIN,
      HBAR_LABEL_PADDING_PLUGIN,
      TICK_LABEL_AVOID_PLUGIN,
      // Point labels next to bars (dual_axis / combo) step off the bars, or hide when nothing fits. On-screen only:
      // an export has no tooltip to fall back on, and its pixels are pinned by the export goldens
      // (EXPORT_VISUAL_RENDERER_VERSION); bringing it there is a separate, versioned change.
      ...(opts.surface === "export" ? [] : [pointLabelPlacementPlugin(hasNoReadbackPath(opts))]),
    ],
  };
  const annotationScope = annotationScopePlugin(spec, withPlugins.data, theme);
  if (annotationScope) withPlugins.plugins.push(annotationScope);
  const annotationPlugin = observedAnnotationPlugin(spec, withPlugins.data, theme);
  if (annotationPlugin) {
    withPlugins.plugins.push(annotationPlugin);
    if (opts.surface !== "export") withPlugins.options = {
      ...withPlugins.options, plugins: { ...withPlugins.options.plugins,
        nativeObservedAnnotations: { activeId: opts.activeAnnotationId },
      } as NonNullable<ChartJSConfig["options"]>["plugins"],
    };
  }
  const visible = applySeriesVisibility(withPlugins, opts);
  if (spec._export_analytical) return visible;
  return opts.focusBinding && opts.surface !== "export"
    ? { ...visible, markBindings: markBindings(sourceSpec, visible.data, opts.focusBinding.descriptor, opts.focusBinding.datasetDigest, spec, normalized.origins) }
    : visible;
}

/**
 * Apply series visibility (the single state in `series-visibility.ts`) to Chart.js:
 *  - Set dataset `hidden` (**do not remove columns**: removal breaks shape requirements for dual_axis/combo/area_stacked
 *    and triggers inspection's automatic kind downgrade; a canvas legend click never changes chart kind);
 *  - Override `legend.onClick` to write that same state instead of changing the Chart.js instance's
 *    `meta.hidden`. Instance state cannot survive remounts (`chartKey` includes theme and all dataset data; streaming updates/
 *    theme switches recreate the instance); React state survives.
 *
 * ⚠ `hidden` must be an **explicit boolean for every dataset**, never true-only with others omitted:
 *   react-chartjs-2 updates datasets via `Object.assign(current, next)` (dist/index.js
 *   `setDatasets`), overwriting keys without deleting them; omission leaves the previous `hidden: true` on
 *   the instance forever, making unhide clicks ineffective.
 *
 * Exports follow the **exporting user's** screen: the export request carries the series they hid in each report chart's
 * legend (`options.chart_hidden_series` → the chart's visual payload `hidden_series`, part of the visual identity), and
 * the export page passes them back here as `hiddenSeries`. Exports never get a legend click handler (static output),
 * and an export without hidden series renders every series as before.
 */
function applySeriesVisibility(config: ChartJSConfig, opts: BuildOpts): ChartJSConfig {
  if (opts.surface === "export") {
    if (!opts.hiddenSeries?.length) return config;
    return applySeriesVisibility(config, { ...opts, surface: undefined, onToggleSeries: undefined });
  }
  if (opts.hiddenSeries === undefined && !opts.onToggleSeries) return config;
  const datasets = config.data?.datasets ?? [];
  // null = this legend is not a numeric-column series legend (pie=categories, grouped scatter/stacked pivot=slices,
  // single series) ⇒ leave it untouched and retain the Chart.js default handler.
  const keys = seriesKeysOfDatasets(datasets);
  if (!keys) return config;
  const hidden = effectiveHiddenSeries(keys, opts.hiddenSeries);
  const onToggle = opts.onToggleSeries;
  const legend = {
    ...(config.options.plugins?.legend ?? {}),
    ...(onToggle
      ? {
          onClick: (_event: unknown, item: LegendItem) => {
            const index = item?.datasetIndex;
            const key = typeof index === "number" ? keys[index] : undefined;
            if (key) onToggle(key);
          },
        }
      : {}),
  };
  const plugins = {
    ...(config.options.plugins as Record<string, unknown> | undefined),
    legend,
  } as ChartOptions["plugins"];
  return {
    ...config,
    data: {
      ...config.data,
      datasets: datasets.map((ds, i) => ({ ...ds, hidden: hidden.has(keys[i]!) })),
    } as ChartData,
    options: { ...config.options, plugins },
  };
}

/** Pre-attach column-level formatting context (percentage scale + displayable unit) to every dataset with `_yCol`.
 *  Must attach to dataset rather than data: react-chartjs-2 cloneData only preserves
 *  labels/datasets, dropping custom data keys (see buildChartJSConfig comments). */
/** Percentage columns sharing a value axis must use **one scale**.
 *
 *  116 dogfood photo (`8月营业额Top 3门店`): four percentage columns on the right axis, each declared correctly:
 *  `营业额环比变化率 = 6.24` (points) / `客流环比变化率 = 4.02` (points) /
 *  `利润环比变化率 = -1.84` (points) / **`利润率 = 0.199` (fraction)**. All four were plotted using **raw values**
 *  on one axis, placing the 19.9% profit margin at 0.199: an almost-zero flat line
 *  on the -5~10 axis, making the most important series look like zero.
 *
 *  The data is correct; rendering assigned scales incorrectly: fraction and points differ by 100× and must align before sharing an axis.
 *  Convert fraction columns to points via ×100 before plotting (**change only plotted positions, not data**),
 *  and set their dataset formatting scale to points, or labels/tooltips multiply again.
 *  Replace that axis's tick callback with an unscaled one too: the entire axis is already in points.
 *
 *  ⚠ Only adjust **mixed scales**; single-scale axes (the vast majority) return original configuration with identical behavior.
 */
function unifyPercentScalePerAxis(config: ChartJSConfig): ChartJSConfig {
  const datasets = config.data?.datasets ?? [];
  if (datasets.length < 2) return config;
  const valueAxisOf = (ds: unknown): string => {
    const id = (ds as { yAxisID?: string }).yAxisID;
    if (id) return id;
    return (config.options as { indexAxis?: string } | undefined)?.indexAxis === "y" ? "x" : "y";
  };
  const scalesByAxis = new Map<string, Set<string>>();
  for (const ds of datasets) {
    const pct = (ds as { _fmtCtx?: ColumnFormatCtx })._fmtCtx?.percent;
    if (!pct) continue;
    const axis = valueAxisOf(ds);
    if (!scalesByAxis.has(axis)) scalesByAxis.set(axis, new Set());
    scalesByAxis.get(axis)!.add(pct);
  }
  const mixed = new Set([...scalesByAxis].filter(([, v]) => v.size > 1).map(([k]) => k));
  if (mixed.size === 0) return config;

  const nextDatasets = datasets.map((ds) => {
    const ctx = (ds as { _fmtCtx?: ColumnFormatCtx })._fmtCtx;
    if (!ctx || ctx.percent !== "fraction" || !mixed.has(valueAxisOf(ds))) return ds;
    const data = ((ds as { data?: unknown[] }).data ?? []).map((v) =>
      typeof v === "number" ? v * 100 : v,
    );
    const nextCtx = { ...ctx, percent: "points" as const };
    // Tooltip reads `_fmtCtx` at runtime and follows automatically; datalabels formatter instead
    // **captured** the old context in its builder closure (`(v) => labelText(v, columnFormatCtx(yc, rows))`),
    // so leaving it unchanged multiplies already-scaled values again: 19.9% renders as 1990.7%.
    const labels = (ds as { datalabels?: Record<string, unknown> }).datalabels;
    return {
      ...ds,
      data,
      _fmtCtx: nextCtx,
      _observedValues: ds.data,
      _observedFmtCtx: ctx,
      ...(labels
        ? { datalabels: { ...labels, formatter: (v: unknown) => labelText(v, nextCtx) } }
        : {}),
    };
  });

  const scales: Record<string, unknown> = {
    ...((config.options as { scales?: Record<string, unknown> } | undefined)?.scales ?? {}),
  };
  for (const axis of mixed) {
    const scale = scales[axis] as { ticks?: Record<string, unknown> } | undefined;
    if (!scale) continue;
    scales[axis] = {
      ...scale,
      ticks: { ...(scale.ticks ?? {}), callback: (v: unknown) => abbrTick(v as number) },
    };
  }
  return {
    ...config,
    data: { ...config.data, datasets: nextDatasets } as unknown as ChartData,
    options: { ...config.options, scales } as ChartJSConfig["options"],
  };
}

function withColumnFormatCtx(
  config: ChartJSConfig,
  rows: ReadonlyArray<Record<string, unknown>>,
): ChartJSConfig {
  const datasets = (config.data?.datasets ?? []).map((ds) => {
    const yCol = (ds as { _yCol?: ChartColumn })._yCol;
    const xCol = (ds as { _xCol?: ChartColumn })._xCol;
    if (!yCol) return ds;
    return { ...ds, _fmtCtx: columnFormatCtx(yCol, rows), ...(xCol ? { _xFmtCtx: columnFormatCtx(xCol, rows) } : {}) };
  });
  return { ...config, data: { ...config.data, datasets } as unknown as ChartData };
}

function buildByKind(
  spec: ChartSpec,
  theme: ChartTheme,
  opts: BuildOpts,
  pres: ReturnType<typeof resolvePresentation>,
): ChartJSConfig {
  switch (spec.kind) {
    case "line":
    case "area_stacked":
      return buildLine(spec, theme, spec.kind === "area_stacked", opts, pres);
    case "bar":
    case "stacked_bar":
      return buildBar(spec, theme, {
        horizontal: false,
        stacked: spec.kind === "stacked_bar",
      }, opts, pres);
    case "hbar":
      return buildBar(spec, theme, { horizontal: true, stacked: false }, opts, pres);
    case "dual_axis":
      return buildDualAxis(spec, theme, opts, pres);
    case "combo":
      return buildCombo(spec, theme, opts, pres);
    case "pie":
      return buildPie(spec, theme, opts, pres);
    case "scatter":
      return buildScatter(spec, theme, opts, pres);
    case "waterfall":
      return buildWaterfall(spec, theme, opts);
    case "sankey":
      return buildSankey(spec, theme, opts);
    case "heatmap":
      return buildHeatmap(spec, theme, opts);
    default:
      throw new Error(`buildChartJSConfig: unsupported kind '${spec.kind}'`);
  }
}

// ─────────────────────────────────────────────────────────────
// Base options (shared by all charts), following Claude Design Charts.html specifications.
// ─────────────────────────────────────────────────────────────
/** Chart Embed Spec D1: every text the chart draws on canvas (ticks, data labels, axis titles, tooltip titles, legend)
 *  uses the interface sans-serif stack. Monospace is for code and aligned columns; canvas ticks are centred one by one
 *  and never rely on equal widths, and two typefaces in one chart read as two systems. The stack is resolved from
 *  `--font-v2-sans` by the theme (canvas cannot read CSS variables). */
function fontSans(theme: Pick<ChartTheme, "fontSans">): string {
  return theme.fontSans || CHART_FONT_SANS_FALLBACK;
}
/** Secondary ink (`--c-ink-2`) for category ticks and data labels; falls back to the tertiary ink. */
function inkMid(theme: ChartTheme): string {
  return theme.ink2 || theme.inkSoft;
}

/** Bar family: marks are rectangles, without point shapes. */
const BAR_FAMILY_KINDS = new Set(["bar", "hbar", "stacked_bar"]);

function baseOptions(spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {}): ChartOptions {
  const recipe = renderRecipe(spec, theme, opts);
  // Trend series share category rows; compare their observed values in one tooltip.
  const groupedTooltip = ["line", "area_stacked", "combo", "dual_axis"].includes(spec.kind);
  const tooltipIntersect = groupedTooltip ? false : (recipe?.tooltipIntersect ?? true);
  const tooltipMode = groupedTooltip || (recipe && !tooltipIntersect && spec.kind !== "scatter")
    ? "index" : "nearest";
  const tooltipAxis = groupedTooltip ? "x" : undefined;
  // Same decision as the presentation resolver (it assigns the dashes/points the legend shows);
  // a second copy of the rule here had already drifted (`seriesCount > palette.length`).
  const nonColorSeries = (opts as InternalBuildOpts).presentation?.adjustments
    .some((adjustment) => adjustment.path === "seriesEncodings") ?? false;
  return {
    responsive: true,
    maintainAspectRatio: false,
    // D3: bar thickness cap (report/chat/export 24, embed/compact 20). Set once at the chart root so every bar
    // dataset — bar, hbar, stacked, grouped, dual_axis/combo bars, waterfall — resolves it; the category band keeps
    // its width and the spare room stays empty rather than stretching the bar.
    datasets: {
      bar: {
        maxBarThickness: recipe?.maxBarThicknessPx ?? 24,
        ...(recipe?.fixedBarThicknessPx ? { barThickness: recipe.fixedBarThicknessPx } : {}),
      },
    },
    animation: { duration: recipe?.animate === false ? 0 : 400, easing: "easeOutQuart" },
    interaction: {
      mode: tooltipMode,
      axis: tooltipAxis,
      intersect: tooltipIntersect,
      includeInvisible: false,
    },
    transitions: {
      active: { animation: { duration: 0 } },
      resize: { animation: { duration: 0 } },
    },
    plugins: {
      legend: {
        display: spec.show_legend !== false && (recipe?.showLegend ?? true),
        position: recipe?.legendPosition ?? "bottom",
        align: "start",
        labels: {
          color: theme.inkSoft,
          // ★ Bar family does not use point-style legends. `pointStyle` belongs to **point/line elements**; bars are rectangles.
          //   Chart.js never draws bars as ✕ or ✳; those shapes appear only in the legend, with
          //   no chart counterpart. A reader seeing "✳ = 6月" cannot find any stars on the chart.
          //   Furthermore, policy point styles 4/5 (crossRot / star) are **stroke-only** shapes, while bar
          //   `borderColor` in this branch is `theme.surface` (segment separator), drawing the background color
          //   on the background ⇒ 2 of every 6 legend entries were **invisible**.
          //   Triggered by `nonColorSeries`, i.e. whenever the presentation resolver assigned non-color encodings:
          //   color-safe / forced colors, or more series than the color-only capacity (116 measured 26 bar-family
          //   charts on the latter and 47 on color-safe with ≥5 series). `contrast: "high"` alone does not.
          //   Disabling point styles makes Chart.js draw filled blocks matching actual bars (now carrying the
          //   series' fill pattern): visible and corresponding.
          //   Line/area/scatter retain point styles because those charts **really have** points.
          usePointStyle: nonColorSeries && !BAR_FAMILY_KINDS.has(spec.kind),
          pointStyle: undefined,
          boxWidth: 8,
          boxHeight: 8,
          padding: recipe?.densityTier === "compact" ? 10 : 12,
          font: { size: recipe?.legendFontSize ?? 11, family: fontSans(theme) },
        },
      },
      // Dark tooltip (dark even in light theme), following the design specification.
      tooltip: {
        mode: tooltipMode,
        axis: tooltipAxis,
        intersect: tooltipIntersect,
        backgroundColor: "#1B1A17",
        titleColor: "#A8A49B",
        bodyColor: "#ECEAE3",
        borderColor: "#2A2824",
        borderWidth: 1,
        padding: 10,
        caretSize: 4,
        cornerRadius: 8,
        displayColors: true,
        boxWidth: 6,
        boxHeight: 6,
        boxPadding: 6,
        titleFont: { size: 11, family: fontSans(theme), weight: 600 },
        bodyFont: { size: 12, family: fontSans(theme) },
        usePointStyle: false,
        callbacks: {
          label: (ctx) => {
            // `_pointLabels` = per-point identity (attached when scatter folds row identifiers into one series; see `buildScatter`).
            // Use it when present; otherwise all 30 points' tooltips repeat the same measure name, conveying nothing.
            const perPoint = (ctx.dataset as { _pointLabels?: string[] })._pointLabels;
            const yCol = (ctx.dataset as { _yCol?: ChartColumn })._yCol;
            const label = perPoint?.[ctx.dataIndex] ?? (yCol && ctx.dataset.label === yCol.header ? measureDisplayName(yCol) : ctx.dataset.label) ?? "";
            // #130 fix: hbar (indexAxis='y') stores values in parsed.x; do not blindly read parsed.y.
            const isHorizontal = ctx.chart.options?.indexAxis === "y";
            const raw = isHorizontal
              ? (ctx.parsed as { x?: number }).x
              : (ctx.parsed as { y?: number }).y;
            // Fallback: parsed is directly a number (pie/donut, etc.).
            const observed = (ctx.dataset as { _observedValues?: unknown[] })._observedValues?.[ctx.dataIndex];
            const v = typeof observed === "number" ? observed : typeof raw === "number"
              ? raw
              : (typeof ctx.parsed === "number" ? ctx.parsed : null);
            // review R0-27: tooltip uses exact formatting, but percentage columns require % and dimensionless units must stay hidden.
            // R5-1: buildChartJSConfig pre-attaches scaling to the **dataset**; custom keys on data
            // cannot survive react-chartjs-2 cloneData (see that function's comments). Datasets without `_fmtCtx`
            // (those lacking `_yCol`) already have no column-level semantics; fall back to no scaling.
            const fmtCtx = (ctx.dataset as { _observedFmtCtx?: ColumnFormatCtx })._observedFmtCtx
              ?? (ctx.dataset as { _fmtCtx?: ColumnFormatCtx })._fmtCtx
              ?? { percent: null, unit: isDisplayableUnit(yCol?.unit) ? yCol?.unit : undefined };
            const exact = fmtCtx.percent
              ? formatSourcePercent(v, fmtCtx.percent)
              : formatSourceValue(v, fmtCtx.unit);
            const xCol = (ctx.dataset as { _xCol?: ChartColumn })._xCol;
            if (xCol) {
              const x = (ctx.parsed as { x?: number }).x;
              const xFmt = (ctx.dataset as { _xFmtCtx?: ColumnFormatCtx })._xFmtCtx;
              const xText = xFmt?.percent ? formatSourcePercent(x, xFmt.percent) : formatSourceValue(x, xFmt?.unit);
              return `${label}: ${measureDisplayName(xCol)} ${xText}; ${yCol ? measureDisplayName(yCol) : ""} ${exact}`;
            }
            return `${label}: ${exact}`;
          },
        },
      },
    },
  };
}

// Shared axis styles (D1): value ticks 11 tertiary ink, category ticks 12 secondary ink, both interface sans.
const valueTickFont = (theme: ChartTheme, recipe: ChartRenderRecipe | null) => ({
  size: recipe?.tickFontSize ?? 11,
  family: fontSans(theme),
});
const categoryTickFont = (theme: ChartTheme, recipe: ChartRenderRecipe | null) => ({
  size: (recipe?.tickFontSize ?? 11) + 1,
  family: fontSans(theme),
});

function usesPresetHighContrast(theme: ChartTheme): boolean {
  return theme.accessibility?.contrast === "high";
}
// Batch 3 4.3 (P2-1): grid/axis borders previously hardcoded light hex colors, making grids nearly invisible in dark themes
// (theme already had grid/gridSoft tokens, but both axes bypassed them). Get colors from theme instead.
// D2: gridlines are one solid hairline tier (`gridSoft` = separator). The former quiet 4/4 dash and the standard
// line-colour solid merge; dashes blur on 1x screens and a background grid must not be louder than the data.
// High contrast (`strong`) keeps the stronger `grid` colour.
const gridDashed = (theme: ChartTheme) => {
  if (theme.gridDensity === "strong") return { color: theme.grid, borderDash: [] };
  return { color: theme.gridSoft, borderDash: [] };
};
const axisBorder = (theme: ChartTheme) => ({ color: theme.grid });

// ─────────────────────────────────────────────────────────────
// Batch 1 2.2/2.3 shared pieces: base data-label styles + axis titles.
// ─────────────────────────────────────────────────────────────

/** Ordinary data-label font (D1): interface sans 12 / 500. */
const LABEL_FONT = { size: 12, weight: 500 } as const;
/** Protagonist data-label font (batch 1 2.1b): sans 12.5 / 650 in full ink. */
const LEAD_LABEL_FONT = { size: 12.5, weight: 650 } as const;

/** Base chart-label styles (D1): sans 12 / 500 in secondary ink, **no backing plate** — labels sit outside the mark
 *  (bar ends with `offset` 6, above points), on the plot background, so a plate only added a second shape.
 *  `padding` is explicit zero: datalabels' default 4 would silently widen every label box the layout plugins measure.
 *  Presentation resolver (2.1b) overrides per-element differences such as larger protagonist labels.
 *  `plate: true` keeps the surface backing for labels drawn **inside** marks (pie slices). */
function labelBase(theme: ChartTheme, { plate = false }: { plate?: boolean } = {}) {
  return {
    color: inkMid(theme),
    font: { family: fontSans(theme), ...LABEL_FONT },
    ...(plate
      ? {
          // R7: **Do not write `theme.surface + "eb"`**. Theme colors come from CSS variables; production builds
          // compress `#ffffff` into `#fff`, yielding five-digit `#fffeb` ⇒ canvas rejects it ⇒ retains the previous
          // fillStyle ⇒ every data-label background becomes **solid black**. Development skips compression, so it always works locally.
          backgroundColor: withAlpha(theme.surface, "eb"),
          borderRadius: 3,
          padding: { top: 1, bottom: 1, left: 3, right: 3 },
        }
      : { padding: { top: 0, bottom: 0, left: 0, right: 0 } }),
    clamp: true,
  };
}

/** Batch 1 2.1b (D5 label column): protagonist uses sans 12.5 / 650 in full ink; others sans 12 / 500 in secondary ink.
 *  Since D1 both share one typeface — the protagonist differs by size and weight only. */
function leadAwareLabelStyle(
  theme: ChartTheme,
  isLead: (ctx: { dataIndex: number; datasetIndex: number }) => boolean,
) {
  return {
    ...labelBase(theme),
    color: (ctx: { dataIndex: number; datasetIndex: number }) =>
      isLead(ctx) ? theme.ink : inkMid(theme),
    font: (ctx: { dataIndex: number; datasetIndex: number }) =>
      isLead(ctx)
        ? { family: fontSans(theme), ...LEAD_LABEL_FONT }
        : { family: fontSans(theme), ...LABEL_FONT },
  };
}

/** review R0-27: **column-level** formatting context for chart labels/axes/tooltips.
 *
 * Root cause: build-config previously knew nothing about percentages or the unit blacklist; `利润率`
 * (unit="decimal") rendered as `0.2decimal` or `-0decimal` in labels, with axis title
 * "利润率（decimal）", while the table showed `17.4%` for the same cell (100× difference on one screen).
 * Batch 1 enables labels by default and always in exports, spreading discovery E-3 to every exported chart.
 */
interface ColumnFormatCtx {
  percent: PercentScale | null;
  unit?: string;
  format?: ChartColumn["format"];
  display?: ChartColumn["display"];
  currency_code?: string;
}

function columnFormatCtx(col: ChartColumn | undefined, rows: ReadonlyArray<Record<string, unknown>>): ColumnFormatCtx {
  if (!col) return { percent: null };
  const unit = axisUnitOf(col);
  if (!resolvePercentColumn(col)) return { percent: null, unit, format: col.format, display: col.display, currency_code: col.currency_code };
  return {
    // Shape comes from the declaration, never the name or observed range.
    percent: resolvePercentScale(
      rows.map((r) => r[col.key] as number | null | undefined),
      col,
    ),
    // Percentage sign already supplies the dimension; do not append another unit.
    unit: undefined,
    display: col.display,
  };
}

/** Value-axis tick callback. **Axis ticks were the last surface lacking column-level percentage scaling** (observed on device 116):
 * fraction columns (persisted refund rate 0.292) use formatPercentCell for labels/tooltips/tables →
 * `29.2%`, while ticks alone use bare `abbrTick` → `0.3`; `axisTitleOf` renders the axis title
 * "退款率（%）" ⇒ readers interpret 0.3% rather than actual 29.2%, a **full 100× difference**.
 *
 * Only scale ×100, **do not append `%`**: axis title already provides the dimension, and repeating %
 * on every tick adds noise (consistent with 2.3's value-axis-title unit responsibility). Points (13.5 means 13.5%)
 * are already 0–100, so remain unchanged; non-percentage columns also remain unchanged. */
function axisTickOf(
  col: ChartColumn | undefined,
  rows: ReadonlyArray<Record<string, unknown>>,
  display?: DisplayMagnitude | null,
): (v: unknown, index?: number, ticks?: { value: number }[]) => string {
  const scaled = columnFormatCtx(col, rows).percent === "fraction";
  return (v, _index, ticks) => {
    if (!display) {
      const factor = scaled ? 100 : 1;
      const values = (ticks?.length ? ticks.map((tick) => tick.value) : [Number(v)]).map((value) => value * factor);
      // A percent axis already names %, so its ticks cannot silently acquire K/万.
      const magnitude = resolveAxisMagnitude(values);
      return formatAxisTick(Number(v) * factor, { ...magnitude, shift: 0, prefix: "", suffix: "", unit: "", base: "" }, values);
    }
    return formatAxisTick(v as number, display, ticks?.length ? ticks.map((tick) => tick.value) : [Number(v)]);
  };
}

/** Compact label text: percentage columns use % (column-level scaling); others use abbr + displayable unit. */
function labelText(v: unknown, ctx: ColumnFormatCtx | string | undefined): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "";
  const resolved: ColumnFormatCtx = typeof ctx === "string" || ctx === undefined
    ? { percent: null, unit: isDisplayableUnit(ctx as string | undefined) ? (ctx as string) : undefined }
    : ctx;
  if (resolved.percent) return formatPercentCell(v, resolved.percent, resolved.display);
  // Values may use a smaller explicit magnitude than their common axis: 780元
  // remains readable on a 万元 axis, while hover uses the ordinary base-unit profile.
  const display = resolveDisplayMagnitude([v], resolved.unit, resolved.display, resolved.format);
  if (display) return formatDisplayValue(v, display);
  const s = abbrTick(v);
  const unit = resolved.unit;
  if (!unit) return s;
  if (unit === "¥" || unit === "$" || unit === "€") return `${unit}${s}`;
  return `${s}${unit}`;
}

/** Line labels mark endpoints + extrema only (D5 line/area row). */
function lineLabelIndexes(data: Array<number | null>): Set<number> {
  const idx = new Set<number>();
  let first = -1;
  let last = -1;
  let maxI = -1;
  let minI = -1;
  for (let i = 0; i < data.length; i += 1) {
    const v = data[i];
    if (typeof v !== "number") continue;
    if (first < 0) first = i;
    last = i;
    if (maxI < 0 || v > (data[maxI] as number)) maxI = i;
    if (minI < 0 || v < (data[minI] as number)) minI = i;
  }
  for (const i of [first, last, maxI, minI]) if (i >= 0) idx.add(i);
  return idx;
}

/** Batch 1 2.3 (1-D): value-axis title includes unit, avoiding bare numbers with guessed dimensions.
 *  Ordinary charts: show only with unit; dual_axis always shows both (the problem is guessing dimensions of two axes). */
/** No fixed category-axis tick limit.
 *
 *  `recipe.maxXTicks` (compact 4 / wide 12 / otherwise 9) is a **constant estimated for a 344px chat plot**
 *  (`render-context.ts` original comment: "A 344px chat plot loses roughly 90–110px…
 *  Four full date/category labels remain legible there; six already collide").
 *  It treats date and category **identically**, despite asymmetric costs: thinned time-axis labels still permit interpolation
 *  from neighbors, but **omitting nominal categories loses identity**; 465 bar charts measured on 116 therefore labeled
 *  only 5 of 10 categories, leaving the middle five groups unidentified.
 *
 *  That estimate is also a fixed value based on the **narrowest surface**: it cannot measure how much wider "保健食品滋补品" is than "女装",
 *  or how much wider a 1456px export surface is than a 344px chat plot.
 *
 *  Let Chart.js autoSkip by **actual label width**: it can measure how much wider "保健食品滋补品"
 *  is than "女装", whereas a constant cannot. hbar already does this in the same file (`autoSkip: false`).
 *  Before/after measurements on 158 real specs: 26 charts gain labels, none lose labels; even the densest 32 province names do not overlap after rotation,
 *  and no time-axis charts change.
 */
/** Is this value axis's name **honest**? It must describe the **whole axis**, not its first series.
 *
 *  One series can always be named. With multiple series, pivoted series columns share `measure_header`
 *  (slice name in `header`, measure name in `measure_header`), permitting a title; but model-provided
 *  wide tables (`省 | 一次性内裤 | 一次性餐具`) have different column names on one axis, so `[0].header`
 *  as axis name **misrepresents a slice name as a dimension**.
 *
 *  Measurements on 158 real specs: without this guard, 50 axes gain names but 3 are wrong; with it,
 *  29 gain names and none are wrong. The 21 omitted axes have same-family differently named columns (`营业额_百万元 + 利润_百万元`),
 *  whose correct title is the **unit**, not either measure name, but production has zero unit-field matches.
 */
function axisNameIsHonest(cols: readonly ChartColumn[]): boolean {
  if (cols.length <= 1) return true;
  const measure = cols[0]?.measure_header;
  return !!measure && cols.every((c) => c.measure_header === measure);
}

/** Dimension shared by all columns on an axis; return undefined for inconsistent (or absent) dimensions.
 *
 *  Percentage columns have dimension `%` (same criterion as `axisTitleOf`, via `resolvePercentColumn`,
 *  not the LLM's `unit`). */
function sharedAxisUnit(cols: readonly ChartColumn[]): string | undefined {
  if (!cols.length) return undefined;
  let shared: string | undefined;
  for (const col of cols) {
    const unit = axisUnitOf(col);
    if (!unit) return undefined;
    if (shared === undefined) shared = unit;
    else if (shared !== unit) return undefined;
  }
  return shared;
}

/** Native fitting caps each vertical axis to a fraction of the canvas. Locale-specific
 * full-number ticks can exceed that cap on narrow dual axes; reserve their measured width. */
function fitNumericAxisLabels(scale: Scale): void {
  if (scale.type !== "linear" || scale.isHorizontal()) return;
  const { ticks, title, grid } = scale.options as CartesianScaleOptions;
  if (!ticks.display || ticks.mirror || scale.options.display === false) return;
  const font = toFont(ticks.font as Partial<FontSpec>, scale.chart.options.font);
  scale.ctx.save();
  scale.ctx.font = font.string;
  const widest = Math.max(0, ...scale.ticks.flatMap((tick) =>
    (Array.isArray(tick.label) ? tick.label : [tick.label ?? ""]).map((label) => scale.ctx.measureText(String(label)).width)));
  scale.ctx.restore();
  const titlePadding = typeof title.padding === "number" ? title.padding : { ...title.padding, left: 0, right: 0 };
  const titleWidth = title.display ? toFont(title.font as Partial<FontSpec>, scale.chart.options.font).lineHeight
    * (Array.isArray(title.text) ? title.text.length : 1) + toPadding(titlePadding).height : 0;
  const required = widest + 2 * ticks.padding + titleWidth + (grid.drawTicks ? grid.tickLength ?? 0 : 0);
  if (required > scale.width + 0.5) scale.width = Math.ceil(required);
}

function axisDisplay(
  cols: readonly ChartColumn[], rows: ReadonlyArray<Record<string, unknown>>, opts: BuildOpts,
  axis: "x" | "y" | "y1" | "value", extraValues: readonly number[] = [],
): DisplayMagnitude | null {
  if (!cols.length || cols.some(resolvePercentColumn)) return null;
  const unit = sharedAxisUnit(cols);
  if (!unit && cols.some((column) => axisUnitOf(column))) return null;
  const domain = axis === "value" ? undefined : opts.axisDomains?.domains[axis];
  if (domain) return resolveAxisMagnitude(domain, unit);
  return resolveAxisMagnitude([
    ...rows.flatMap((row) => cols.map((column) => row[column.key])),
    ...extraValues,
  ], unit);
}

/**
 * Title for one dual_axis axis.
 *
 * Always naming both axes (batch 1 2.3) has the right motivation: guessing two axes' dimensions is dual_axis's root problem.
 * But the original `axisTitleOf(cols[0], theme, true)` **named only the first column
 * when an axis had several columns**. On 116, 53 dual_axis charts / 106 axes included 17 multi-column axes,
 * none sharing `measure_header` ⇒ all 17 used one column's name to impersonate the whole axis.
 * Worst example: `单店客流量(人次)` and `单店IPT(元)` shared an axis named only for the first.
 *
 * Three tiers:
 *  1. One column, or multiple columns sharing `measure_header` → unchanged (measure (unit)).
 *  2. Multiple columns sharing a dimension → **name only the dimension** (`元` / `%`). This is precisely
 *     axisNameIsHonest's explanation that same-family columns need unit titles; percentage columns now obtain
 *     `%` from `resolvePercentColumn`, making this path effective (measured recovery: 6 axes).
 *  3. Multiple columns with inconsistent dimensions → **no title**. The remaining 11 axes move from inaccurate titles to no titles,
 *     avoiding misleading axis names; this tier itself signals mixed dimensions on one axis.
 */
function dualAxisTitle(
  cols: readonly ChartColumn[],
  theme: ChartTheme,
  display?: DisplayMagnitude | null,
): ReturnType<typeof axisTitleOf> {
  if (axisNameIsHonest(cols)) return axisTitleOf(cols[0], theme, true, display);
  const unit = display?.unit ?? sharedAxisUnit(cols);
  if (!unit) return undefined;
  return axisTitleText(unit, theme);
}

/** Unit-only value-axis title for single-measure axes (Chart Embed Spec): the shared unit of every column actually
 *  plotted on the axis, or no title (counts, mixed units). See `axisTitleOf` for why names are no longer shown. */
function unitAxisTitle(cols: readonly ChartColumn[], theme: ChartTheme, display?: DisplayMagnitude | null): ReturnType<typeof axisTitleOf> {
  const unit = display?.unit ?? sharedAxisUnit(cols);
  return unit ? axisTitleText(unit, theme) : undefined;
}

/** Axis title style (D1): interface sans 11 in tertiary ink, at the axis end. */
function axisTitleText(text: string, theme: ChartTheme): NonNullable<ReturnType<typeof axisTitleOf>> {
  return { display: true, text, color: theme.inkSoft, font: { family: fontSans(theme), size: 11 }, align: "end" };
}

function categoryTickLimit(
  xCol: ChartColumn | undefined,
  recipe: ChartRenderRecipe | null,
): number | undefined {
  return xCol?.type === "date" ? recipe?.maxXTicks : undefined;
}

/**
 * Value-axis title.
 *
 * Chart Embed Spec (axis titles): an ordinary single-measure axis states **only its unit** (「亿元」「%」), at the axis
 * end, and only when there is one. A count column has no unit, and its name repeats the chart title or the
 * column header — 「管理条目」 under the Agent overview's bars was a column name, not a dimension. So `always` no longer
 * applies to the single-series "honest name" case (line / bar / combo pass `false`).
 *
 * `always` stays for axes whose **identity** a reader cannot get elsewhere: scatter (two different measures on x and
 * y) and dual_axis (which series reads against which side). Those keep `measure（unit）` / `measure`.
 */
function axisTitleOf(
  col: ChartColumn | undefined,
  theme: ChartTheme,
  always = false,
  display?: DisplayMagnitude | null,
): { display: boolean; text: string; color: string; font: { family: string; size: number }; align: "end" } | undefined {
  if (!col) return undefined;
  // review R0-27: percentage columns have dimension %, not the LLM's unit (formerly rendered as "利润率（decimal）");
  // hide all dimensionless "units" (same blacklist shared with tables/KPI).
  if (!always) return unitAxisTitle([col], theme, display);
  const unit = display?.unit ?? axisUnitOf(col);
  // Pivoted series-column header is the slice name (`外套`); the value axis still measures the original measure (`营业额`).
  // ⚠ Explicitly empty `measure_header` = **source has no measure name** (transposed time-wide tables
  // with month-only column names). Show only the unit, without empty parentheses like "（元）".
  const name = measureDisplayName(col);
  if (!name) return unit ? axisTitleText(unit, theme) : undefined;
  return axisTitleText(unit ? `${name}（${unit}）` : name, theme);
}

// ─────────────────────────────────────────────────────────────
// line / area_stacked
// ─────────────────────────────────────────────────────────────
/**
 * x-axis column: **default to column order** (existing `review[2]` contract stays unchanged), switching only when
 * the selected column **cannot distinguish rows** and another dimension can identify each row.
 *
 * The server selects the primary dimension by cardinality (`_lead_dim_col`). The frontend must
 * apply the same criterion instead of independently choosing `columns.find(date|cat|geo)`:
 * one concept implemented twice with different criteria. User-visible examples:
 *   · `bar | 2026年省市营业额排名 Top10`, dimensions [省(5), 市(10)]: 10 bars, only 5 labels
 *   · `bar | 2025年各区域+分行农户相关客户数`, dimensions [区域(7), 一级分行(30)]: 30 bars, 7 labels
 * Readers cannot identify each bar's city or branch.
 *
 * Deliberately narrow criteria, two guardrails:
 *  - **Switch only when another column identifies every row**. Real long tables (category × month × revenue)
 *    have no such single column, so this never applies to long tables: that is `pivotLongSpec`'s job.
 *  - **Never change date columns**. Time-series x axes should be time, even if a category happens to be row-unique
 *    (`[每月(3), 门店(9)]`, 9 rows: switching destroys the trend chart).
 *  - **The column-order choice must repeat excessively** (at least two rows per value on average) before yielding.
 *    ⚠ Full-corpus comparisons forced this guard: row-unique does not establish row identity;
 *    IDs, codes, and formatted numbers misclassified as categories are all row-unique. Without this guard, the first version
 *    changed `dual_axis | Top15 城市月坪效排名` (15 rows, `市` with 11 values, `门店面积` with 15 values)
 *    from city names to `1,135 / 24,303 / …`, worse than before.
 *    `市` has 11/15 values and already distinguishes rows; yielding is unjustified.
 */
function preferIdentifyingDim(
  rows: ReadonlyArray<Record<string, unknown>>,
  picked: ChartColumn | undefined,
  candidates: readonly ChartColumn[],
): ChartColumn | undefined {
  if (!picked || picked.type === "date" || rows.length < 2) return picked;
  const distinct = (c: ChartColumn) => new Set(rows.map((r) => String(r[c.key] ?? ""))).size;
  if (distinct(picked) * 2 > rows.length) return picked;
  const better = candidates.find(
    (c) => c.key !== picked.key && c.type !== "date" && distinct(c) === rows.length && !looksAllNumeric(rows, c),
  );
  return better ?? picked;
}

/** A whole column that looks numeric (possibly with thousands separators / currency / percent) is not row identity.
 *
 *  ⚠ A second guard forced by full-corpus comparisons: in `bar | 门店面积与营业额关系`,
 *  `门店面积` was classified categorical (values like `24,303`), unique across 39 rows,
 *  replacing `省` as x axis and yielding area numbers. Server parsing already restores these columns
 *  to numeric (`_coerce_formatted_numeric_columns`), but **existing specs rerender every time they open**,
 *  so an upstream fix cannot substitute for frontend selection criteria. */
const NUMERIC_LOOKING = /^[¥$€]?\s*-?[\d,]+(\.\d+)?\s*%?$/;

function looksAllNumeric(rows: ReadonlyArray<Record<string, unknown>>, col: ChartColumn): boolean {
  const vals = rows.map((r) => String(r[col.key] ?? "").trim()).filter(Boolean);
  return vals.length > 0 && vals.every((v) => NUMERIC_LOOKING.test(v));
}

function buildAnalyticalLine(spec: ChartSpec, theme: ChartTheme, opts: InternalBuildOpts): ChartJSConfig {
  const a = spec._export_analytical!;
  const columns = a.binding.measure_keys.map((key) => spec.columns.find((column) => column.key === key)!);
  const value = spec.columns.find((column) => column.key === a.value_key)!;
  const display = axisDisplay(columns, spec.rows ?? [], opts, "y");
  const options = baseOptions(spec, theme, opts);
  const granularity = String(spec.rows?.[0]?.[a.x_key!] ?? "").length;
  const datePattern = spec.columns.find((column) => column.key === a.x_key)?.date_pattern;
  options.scales = {
    x: { type: "linear", ...(a.x_kind === "date" ? { afterBuildTicks: (scale: import("chart.js").Scale) => { scale.ticks = analyticalDateTicks(a.binding.x_domain!, granularity).map((value) => ({ value })); } } : {}), min: a.binding.x_domain![0], max: a.binding.x_domain![1],
      title: { display: true, text: spec.columns.find((column) => column.key === a.x_key)?.header, color: theme.inkSoft, font: valueTickFont(theme, null) },
      ticks: { includeBounds: false, maxTicksLimit: 6, color: theme.inkSoft, font: valueTickFont(theme, null), callback: (v) => a.x_kind === "date" ? (datePattern && (granularity === 4 || granularity === 7 || granularity === 10) ? formatDateLabel(new Date(Number(v)).toISOString().slice(0, granularity), opts.locale, datePattern) : new Intl.DateTimeFormat(opts.locale ?? "en-US", { year: "numeric", ...(granularity >= 7 ? { month: "short" as const } : {}), ...(granularity === 10 ? { day: "numeric" as const } : {}), timeZone: "UTC" }).format(Number(v))) : formatObservedValue(Number(v)) },
      grid: gridDashed(theme), border: axisBorder(theme) },
    y: { type: "linear", min: a.binding.value_domain[0], max: a.binding.value_domain[1], title: unitAxisTitle([value], theme, display),
      ticks: { includeBounds: false, callback: axisTickOf(value, spec.rows ?? [], display), maxTicksLimit: 6, color: theme.inkSoft, font: valueTickFont(theme, null) }, grid: gridDashed(theme), border: { display: false } },
  };
  return buildAnalyticalSeries(spec, theme, options, opts.locale, opts.focusBinding?.datasetDigest);
}

function buildLine(
  spec: ChartSpec, theme: ChartTheme, stacked: boolean, opts: BuildOpts = {},
  pres: Presentation = { lead: null },
): ChartJSConfig {
  const rows = spec.rows ?? [];
  const xCol = categoryColumn(spec);
  const yCols = spec.columns.filter((c) => c.type === "numeric");
  if (!xCol || yCols.length === 0) {
    throw new Error("line chart requires x (date/cat) + ≥1 numeric");
  }

  const labels = rows.map((r) => formatDateLabel(String(r[xCol.key] ?? ""), opts.locale));
  const recipe = renderRecipe(spec, theme, opts);
  const showLabels = resolveDataLabels(spec, opts, yCols.length, rows.length, recipe);
  // Labeling endpoints/extrema of many lines in a compact canvas stacks unreadable numbers at both ends.
  // Legend + color + line style + point style already provide redundant identification; if a caption selects a protagonist series,
  // retain only its labels. This tightens presentation density without changing series/data/spec.
  const suppressDenseCompactLabels = spec.show_data_labels !== true
    && recipe?.widthClass === "compact"
    && yCols.length > 4;
  // 2.1b (D5 line/area row + design D1): protagonist line width 2.5 with full color; others 1.5 with subdued color.
  const leadDs = pres.lead?.kind === "dataset" ? pres.lead.index : null;
  const datasets = yCols.map((yc, i) => {
    const data = rows.map((r) => (r[yc.key] as number | null) ?? null);
    // D5 line/area row: label only endpoints + extrema (labeling every point is the noisiest line-chart failure).
    const labelIdx = lineLabelIndexes(data);
    const fullColor = seriesColor(theme.palette, i, theme.surface, theme.ink);
    const isLead = leadDs === null || leadDs === i;
    const color = isLead ? fullColor : mutedOf(fullColor, theme.surface);
    return {
      label: yc.header,
      data,
      borderColor: color,
      backgroundColor: stacked
        ? withAlpha(color, "33")  // 20% opacity fill (R7: no string concatenation).
        : "transparent",
      borderWidth: leadDs === null ? (theme.lineWidth ?? 2) : (isLead ? (theme.lineWidth ?? 2) + 0.5 : Math.max(1, (theme.lineWidth ?? 2) - 0.5)),
      pointRadius: rows.length <= 30 ? (theme.pointRadius ?? 3) : 0,
      pointHoverRadius: 5,
      tension: 0.25,
      fill: stacked ? "origin" : false,
      _yCol: yc,
      datalabels: {
        ...leadAwareLabelStyle(theme, () => leadDs !== null && leadDs === i),
        display: (ctx: { dataIndex: number }) =>
          labelDisplay(
            ((!suppressDenseCompactLabels && showLabels) || leadDs === i)
            && labelIdx.has(ctx.dataIndex),
            leadDs === i,
          ),
        align: "top" as const,
        offset: 4,
        formatter: (v: unknown) => labelText(v, columnFormatCtx(yc, rows)),
      },
    };
  });

  const display = axisDisplay(yCols, rows, opts, "y", stacked ? rows.flatMap((row) => [
    yCols.reduce((sum, col) => sum + Math.max(0, Number(row[col.key] ?? 0)), 0),
    yCols.reduce((sum, col) => sum + Math.min(0, Number(row[col.key] ?? 0)), 0),
  ]) : []);
  const options = baseOptions(spec, theme, opts);
  options.scales = {
    x: {
      ticks: {
        color: inkMid(theme),
        maxRotation: recipe?.widthClass === "compact" ? 0 : 30,
        autoSkip: true,
        maxTicksLimit: categoryTickLimit(xCol, recipe),
        font: categoryTickFont(theme, recipe),
      },
      grid: { display: false },
      border: axisBorder(theme),
    },
    y: {
      stacked,
      title: unitAxisTitle(yCols, theme, display),
      ticks: {
        color: theme.inkSoft,
        callback: axisTickOf(yCols[0], rows, display),
        font: valueTickFont(theme, recipe),
        maxTicksLimit: recipe?.maxYTicks ?? 5,
      },
      grid: gridDashed(theme),
      border: { display: false },
    },
  };

  // Line specification: monotone cubic + hollow points + 8% shaded area.
  for (const ds of datasets) {
    (ds as { tension?: number }).tension = 0.35;
    // 2.1b: when lead exists, preserve map's 2.5/1.5 widths instead of writing both back to 2.
    if (leadDs === null) (ds as { borderWidth?: number }).borderWidth = theme.lineWidth ?? 2;
    (ds as { borderCapStyle?: string }).borderCapStyle = "round";
    (ds as { borderJoinStyle?: string }).borderJoinStyle = "round";
    (ds as { pointRadius?: number }).pointRadius = rows.length <= 30 ? (theme.pointRadius ?? 3) : 0;
    (ds as { pointHoverRadius?: number }).pointHoverRadius = 4;
    (ds as { pointBackgroundColor?: string }).pointBackgroundColor = theme.surface;
    (ds as { pointBorderColor?: string }).pointBorderColor = (ds as { borderColor?: string }).borderColor ?? theme.primary;
    (ds as { pointBorderWidth?: number }).pointBorderWidth = 1.5;
    (ds as { pointHoverBackgroundColor?: string }).pointHoverBackgroundColor = (ds as { borderColor?: string }).borderColor ?? theme.primary;
    // Give 8% shaded area to **one** line only, which must qualify for emphasis:
    //   Single series         → that series (Line specification effect)
    //   Multiple + protagonist → protagonist (named by caption)
    //   Multiple + no protagonist → none
    // Previously always assigned to `datasets[0]`: first column order has no semantics; area fill implies
    // it is the total, while the filled region covers the other lines' entire area (215 multi-series line charts in 116's full corpus).
    const fillTarget = datasets.length === 1 ? 0 : leadDs;
    if (!stacked && fillTarget !== null && ds === datasets[fillTarget]) {
      (ds as { fill?: string | boolean }).fill = "origin";
      const color = (ds as { borderColor?: string }).borderColor ?? theme.primary;
      (ds as { backgroundColor?: string }).backgroundColor = withAlpha(color, "14"); // 8%
    }
  }

  // buildChartJSConfig attaches `_rows` centrally (R4-1); no separate attachment here.
  return { type: "line", data: { labels, datasets } as unknown as ChartData, options };
}

// ─────────────────────────────────────────────────────────────
// bar / stacked_bar / hbar
// ─────────────────────────────────────────────────────────────
/** D3: a bar-end label sits 6px outside the bar end — never inside the bar. */
const BAR_LABEL_OFFSET_PX = 6;

function buildBar(
  spec: ChartSpec, theme: ChartTheme,
  opts: { horizontal: boolean; stacked: boolean },
  buildOpts: BuildOpts = {},
  pres: Presentation = { lead: null },
): ChartJSConfig {
  // 2.1b (D5 bar/hbar/stacked_bar row + design D2):
  //   Element lead (bar/hbar) = caption-mentioned bar uses full color; others are subdued;
  //   Dataset lead (stacked segment) = protagonist segment uses full color; others each remain distinguishable but subdued.
  const leadEl = pres.lead?.kind === "element" ? pres.lead.index : null;
  const leadDs = pres.lead?.kind === "dataset" ? pres.lead.index : null;
  const rows = spec.rows ?? [];
  // #298 fix: recognize "geo" as x-col too.
  // Declared axis roles first (stacked hierarchies: child = x, parent = series); see `stack-dims.ts`.
  const catCols = roleOrderedDims(spec.columns.filter(
    (c) => c.type === "categorical" || c.type === "geo",
  ));
  const numCols = spec.columns.filter((c) => c.type === "numeric");
  const recipe = renderRecipe(spec, theme, buildOpts);
  const highContrast = usesPresetHighContrast(theme);
  // review[2]: non-pivot path selects first date|categorical|geo in **column order** (restores original .find behavior).
  // Original `dateCol ?? catCols[0]` unconditionally preferred date over earlier categorical/geo columns,
  // silently switching old specs like [分行, 月份, 销量] from "分行" to "月份". Pivot path forces x=catCols[0].

  // Feishu 07.06 #3: stacked bars + two category dimensions → pivot dimension 2 into stacked series; bar height uses one
  // primary measure. Correct stacking semantics = split the same measure by dimension 2; segment sum = dimension 1's actual
  // total (e.g. card amounts across acquisition channels sum to branch total). Original implementation stacked all numeric columns (different dimensions,
  // e.g. card amount + customer count) into one bar: meaningless sum, invisible small measures, and discarded second dimension
  // (backend infer.py §8 already specifies cat2=series grouping, with multiple nums using only the sort_by primary series).
  const seriesCol = opts.stacked && catCols.length >= 2 ? catCols[1] : null;
  const xCol = categoryColumn(spec);
  if (!xCol || numCols.length === 0) {
    throw new Error("bar chart requires date/categorical/geo + ≥1 numeric");
  }
  const primaryMetric = numCols.find((c) => c.key === spec.sort_by) ?? numCols[0];

  const labels: string[] = [];
  const datasets = seriesCol
    ? (() => {
        // Pivot: deduplicate dimension 1 values (labels) and dimension 2 values (series) preserving order; fill cells with primary measure.
        const sVals: string[] = [];
        const cell = new Map<string, number>();
        for (const r of rows) {
          const x = String(r[xCol.key] ?? "");
          const s = String(r[seriesCol.key] ?? "");
          if (!labels.includes(x)) labels.push(x);
          if (!sVals.includes(s)) sVals.push(s);
          // review[5]: aggregate by sum; multiple rows for one (dimension 1, dimension 2) pair (third dimension not pivoted /
          // no preaggregation) must accumulate, not overwrite. (Separator \u0000 avoids collisions for values containing spaces.)
          const cellKey = `${x} ${s}`;
          cell.set(cellKey, (cell.get(cellKey) ?? 0) + ((r[primaryMetric.key] as number | null) ?? 0));
        }
        return sVals.map((s, i) => {
          const full = seriesColor(theme.palette, i, theme.surface, theme.ink);
          // 2.1b dataset lead (design D2 stacked segment): protagonist uses full color; each other segment is subdued.
          const color = leadDs !== null && leadDs !== i ? mutedOf(full, theme.surface) : full;
          const top = i === sVals.length - 1 ? (theme.barRadius ?? 4) : 0; // Round only the top of the topmost segment.
          return {
            label: s,
            data: labels.map((x) => cell.get(`${x} ${s}`) ?? 0),
            backgroundColor: color,
            borderColor: highContrast ? theme.ink : color,
            borderWidth: highContrast ? 1 : 0,
            hoverBackgroundColor: full,
            hoverBorderColor: full,
            hoverBorderWidth: 1.5,
            borderRadius: { topLeft: top, topRight: top, bottomLeft: 0, bottomRight: 0 },
            borderSkipped: false as const,
            _yCol: primaryMetric,
          };
        });
      })()
    : (() => {
        // Non-pivot: one dimension + N numeric columns (unstacked=grouped; stacked=same-unit components such as costs, valid).
        for (const r of rows) labels.push(String(r[xCol.key] ?? ""));
        return numCols.map((yc, i) => {
          const color = seriesColor(theme.palette, i, theme.surface, theme.ink);
          // Vertical bars round top corners; horizontal bars round all four (stacked: only topmost segment rounds top corners).
          const radius = opts.horizontal
            ? (theme.barRadius ?? 4)
            : (opts.stacked && numCols.length > 1 && i < numCols.length - 1 ? 0 : (theme.barRadius ?? 4));
          // 2.1b element lead: protagonist bar uses full color; others are subdued (scriptable per-element).
          const bg = leadEl === null || opts.stacked
            ? color
            : (ctx: { dataIndex: number }) =>
                ctx.dataIndex === leadEl ? color : mutedOf(color, theme.surface);
          // Dataset lead (stacked non-pivot segments): subdue other segments.
          const stackedBg = leadDs !== null && opts.stacked && leadDs !== i
            ? mutedOf(color, theme.surface)
            : bg;
          return {
            label: yc.header,
            data: rows.map((r) => (r[yc.key] as number | null) ?? null),
            backgroundColor: stackedBg,
            borderColor: highContrast ? theme.ink : color,
            borderWidth: highContrast ? 1 : 0,
            hoverBackgroundColor: color,
            hoverBorderColor: color,
            hoverBorderWidth: 1.5,
            borderRadius: opts.horizontal
              ? radius
              : { topLeft: radius, topRight: radius, bottomLeft: 0, bottomRight: 0 },
            borderSkipped: false as const,
            _yCol: yc,
          };
        });
      })();

  // Batch 1 2.2: bar value labels. Stacked = label total column height only on topmost segment (D5 stacked_bar row; labeling every
  // segment is characteristic stacked-chart noise); ordinary/grouped = one label per bar.
  const showLabels = resolveDataLabels(spec, buildOpts, datasets.length, labels.length, recipe);
  if (opts.stacked && datasets.length > 1) {
    const totals = labels.map((_, li) =>
      datasets.reduce((a, ds) => a + ((ds.data[li] as number | null) ?? 0), 0));
    const stackedCols = datasets.map((ds) => (ds as { _yCol?: ChartColumn })._yCol);
    // review R1-7①: pivot-stacked segments split one measure (additive); non-pivot stacking multiple numeric columns
    // into one bar mixes dimensions, already described here as a meaningless sum, yet batch 1 added a total
    // to that bar (营业额_亿 + 利润率_pct = 121.4%). Label totals only when all segments share one column (pivot)
    // or all have the same unit and are not ratios.
    const firstCol = stackedCols[0];
    const additive = stackedCols.every(
      (c) => c && firstCol && c.unit === firstCol.unit && !resolvePercentColumn(c),
    );
    const totalCtx = columnFormatCtx(stackedCols[stackedCols.length - 1], rows);
    datasets.forEach((ds, i) => {
      (ds as { datalabels?: unknown }).datalabels = i === datasets.length - 1
        ? {
            ...labelBase(theme),
            display: labelDisplay(showLabels && additive),
            anchor: "end" as const,
            align: "end" as const,
            offset: BAR_LABEL_OFFSET_PX,
            formatter: (_v: unknown, ctx: { dataIndex: number }) => labelText(totals[ctx.dataIndex], totalCtx),
          }
        : { display: false };
    });
  } else {
    for (const ds of datasets) {
      const fmtCtx = columnFormatCtx((ds as { _yCol?: ChartColumn })._yCol, rows);
      (ds as { datalabels?: unknown }).datalabels = {
        // 2.1b: protagonist bar labels use large full ink; label the protagonist even when overall labels are off if lead exists
        // (editorial principle: the bar referenced by the conclusion must carry its own number).
        ...leadAwareLabelStyle(theme, (ctx) => leadEl !== null && ctx.dataIndex === leadEl),
        display: leadEl === null
          ? labelDisplay(showLabels)
          : (ctx: { dataIndex: number }) =>
              labelDisplay(showLabels || ctx.dataIndex === leadEl, ctx.dataIndex === leadEl),
        anchor: "end" as const,
        align: "end" as const,
        offset: BAR_LABEL_OFFSET_PX,
        formatter: (v: unknown) => labelText(v, fmtCtx),
      };
    }
  }

  const display = axisDisplay(seriesCol ? [primaryMetric] : numCols, rows, buildOpts, opts.horizontal ? "x" : "y", opts.stacked ? labels.flatMap((_, i) => [
    datasets.reduce((sum, ds) => sum + Math.max(0, Number(ds.data[i] ?? 0)), 0),
    datasets.reduce((sum, ds) => sum + Math.min(0, Number(ds.data[i] ?? 0)), 0),
  ]) : [0]);
  const options = baseOptions(spec, theme, buildOpts);
  options.indexAxis = opts.horizontal ? "y" : "x";
  // First/last vertical bars in a 344px chart can have value labels' trailing units clipped near canvas edges.
  // Reserve safety margins only when compact with visible labels, preserving plot areas for unlabeled/horizontal bars.
  if (!opts.horizontal && showLabels && recipe?.widthClass === "compact") {
    options.layout = { padding: { left: 12, right: 16 } };
  }
  // Plugin adjustment: hide legend for a single series (per design).
  if (datasets.length === 1) {
    options.plugins = {
      ...options.plugins,
      legend: { ...options.plugins?.legend, display: false },
    };
  }
  const valueAxis = opts.horizontal ? "x" : "y";
  const catAxis = opts.horizontal ? "y" : "x";
  options.scales = {
    [catAxis]: {
      stacked: opts.stacked,
      ticks: {
        color: inkMid(theme),
        autoSkip: !opts.horizontal,
        maxTicksLimit: opts.horizontal ? undefined : categoryTickLimit(xCol, recipe),
        font: categoryTickFont(theme, recipe),
        crossAlign: opts.horizontal ? "far" : "center",
      },
      grid: { display: false },
      border: axisBorder(theme),
    },
    [valueAxis]: {
      stacked: opts.stacked,
      // Unit only (see `axisTitleOf`): the measure name is in the chart title, the legend or the exact-value table.
      title: unitAxisTitle(seriesCol ? [primaryMetric] : numCols, theme, display),
      ticks: {
        color: theme.inkSoft,
        callback: axisTickOf(primaryMetric, rows, display),
        font: valueTickFont(theme, recipe),
        maxTicksLimit: recipe?.maxYTicks ?? 5,
      },
      grid: gridDashed(theme),
      border: { display: false },
      beginAtZero: true,
    },
  };
  // Gap ratio approximately 35-40% → barPercentage 0.66-0.68.
  (options as { barPercentage?: number }).barPercentage = 0.66;
  (options as { categoryPercentage?: number }).categoryPercentage = 0.9;

  // buildChartJSConfig attaches `_rows` centrally (R4-1); no separate attachment here.
  return { type: "bar", data: { labels, datasets } as unknown as ChartData, options };
}

// ─────────────────────────────────────────────────────────────
// dual_axis (mixed bars and lines).
// ─────────────────────────────────────────────────────────────
function buildDualAxis(
  spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {},
  pres: Presentation = { lead: null },
): ChartJSConfig {
  const rows = spec.rows ?? [];
  const recipe = renderRecipe(spec, theme, opts);
  const highContrast = usesPresetHighContrast(theme);
  // dual_axis x-col: prefer date (monthly trend) then categorical/geo — matches
  // buildLine. A date x-col (e.g. 每月) previously returned undefined here and
  // the chart silently degraded to ChartTable.
  const xCol = categoryColumn(spec);
  const yCols = spec.columns.filter((c) => c.type === "numeric");
  if (!xCol || yCols.length < 2) {
    throw new Error("dual_axis requires x (date/categorical/geo) + ≥2 numeric");
  }
  const labels = rows.map((r) => String(r[xCol.key] ?? ""));

  const { bars: barCols, lines: lineCols } = dualAxisColumns(spec.columns);

  // Batch 1 2.2 (D5 dual_axis/combo row): label all bars, only line endpoints.
  const showLabels = resolveDataLabels(spec, opts, yCols.length, rows.length, recipe);
  // 2.1b: lead is an index in numCols order, but datasets reorder bars/lines; map by column key.
  const leadKey = pres.lead?.kind === "dataset" ? (yCols[pres.lead.index]?.key ?? null) : null;
  const datasets: ChartData["datasets"] = [
    ...barCols.map((bc, i) => {
      const full = seriesColor(theme.palette, i, theme.surface, theme.ink);
      const muted = leadKey !== null && bc.key !== leadKey;
      return {
        type: "bar" as const,
        label: bc.header,
        data: rows.map((r) => (r[bc.key] as number | null) ?? null),
        backgroundColor: muted ? mutedOf(full, theme.surface) : full,
        borderColor: highContrast ? theme.ink : full,
        borderWidth: highContrast ? 1 : 0,
        yAxisID: "y",
        borderRadius: theme.barRadius ?? 4,
        order: 2,
        _yCol: bc,
        datalabels: {
          ...leadAwareLabelStyle(theme, () => leadKey !== null && bc.key === leadKey),
          display: labelDisplay(showLabels, leadKey !== null && bc.key === leadKey),
          anchor: "end" as const,
          align: "end" as const,
          offset: 2,
          formatter: (v: unknown) => labelText(v, columnFormatCtx(bc, rows)),
        },
      } as unknown as ChartData["datasets"][number];
    }),
    ...lineCols.map((yc, i) => {
      const data = rows.map((r) => (r[yc.key] as number | null) ?? null);
      const labelIdx = lineLabelIndexes(data);
      const full = seriesColor(theme.palette, barCols.length + i, theme.surface, theme.ink);
      const muted = leadKey !== null && yc.key !== leadKey;
      return {
        type: "line" as const,
        label: yc.header,
        data,
        borderColor: muted ? mutedOf(full, theme.surface) : full,
        backgroundColor: "transparent",
        borderWidth: muted ? Math.max(1, (theme.lineWidth ?? 2) - 0.5) : (theme.lineWidth ?? 2),
        pointRadius: theme.pointRadius ?? 3,
        yAxisID: "y1",
        order: 1,
        tension: 0.2,
        _yCol: yc,
        datalabels: {
          ...leadAwareLabelStyle(theme, () => leadKey !== null && yc.key === leadKey),
          display: (ctx: { dataIndex: number }) =>
            labelDisplay(showLabels && labelIdx.has(ctx.dataIndex), leadKey !== null && yc.key === leadKey),
          align: "top" as const,
          offset: 4,
          formatter: (v: unknown) => labelText(v, columnFormatCtx(yc, rows)),
        },
      } as unknown as ChartData["datasets"][number];
    }),
  ];

  const leftDisplay = axisDisplay(barCols, rows, opts, "y", [0]);
  const rightDisplay = axisDisplay(lineCols, rows, opts, "y1");
  const options = baseOptions(spec, theme, opts);
  options.scales = {
    x: {
      ticks: {
        color: inkMid(theme),
        maxTicksLimit: categoryTickLimit(xCol, recipe),
        font: categoryTickFont(theme, recipe),
      },
      grid: { display: false },
    },
    // Batch 1 2.3 (1-D): name dual axes where possible; guessing two axes' dimensions is dual_axis's root problem.
    // Multi-column axes cannot use the first column's name to impersonate the whole axis; see `dualAxisTitle` tiers.
    y: {
      position: "left",
      title: dualAxisTitle(barCols, theme, leftDisplay),
      ticks: {
        color: theme.inkSoft,
        callback: axisTickOf(barCols[0], rows, leftDisplay),
        maxTicksLimit: recipe?.maxYTicks,
        font: valueTickFont(theme, recipe),
      },
      grid: { color: theme.grid },
      beginAtZero: true,
    },
    y1: {
      position: "right",
      title: dualAxisTitle(lineCols, theme, rightDisplay),
      ticks: {
        color: theme.inkSoft,
        callback: axisTickOf(lineCols[0], rows, rightDisplay),
        maxTicksLimit: recipe?.maxYTicks,
        font: valueTickFont(theme, recipe),
      },
      grid: { drawOnChartArea: false },
      beginAtZero: true,
    },
  };

  return { type: "bar", data: { labels, datasets }, options };
}

// ─────────────────────────────────────────────────────────────
// combo (V2.9 B1): mixed bars and lines on the same axis.
// First numeric → bar; subsequent numeric → line; all share the y axis (same unit).
// Suitable for Target/Actual/Forecast series sharing unit (Mn RMB).
// Unlike dual_axis, whose second series uses the right axis (different unit).
// ─────────────────────────────────────────────────────────────
function buildCombo(
  spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {},
  pres: Presentation = { lead: null },
): ChartJSConfig {
  const rows = spec.rows ?? [];
  const recipe = renderRecipe(spec, theme, opts);
  const highContrast = usesPresetHighContrast(theme);
  const xCol = categoryColumn(spec);
  const yCols = spec.columns.filter((c) => c.type === "numeric");
  if (!xCol || yCols.length < 2) {
    throw new Error("combo requires x (date/categorical/geo) + ≥2 numeric (1 bar + ≥1 line)");
  }
  const labels = rows.map((r) => String(r[xCol.key] ?? ""));
  const [primary, ...rest] = yCols;
  // Batch 1 2.2 (D5 dual_axis/combo row): label all bars, only line endpoints.
  const showLabels = resolveDataLabels(spec, opts, yCols.length, rows.length, recipe);
  // 2.1b: map lead by column key (same as dual_axis).
  const leadKey = pres.lead?.kind === "dataset" ? (yCols[pres.lead.index]?.key ?? null) : null;
  const primaryMuted = leadKey !== null && primary.key !== leadKey;
  const datasets: ChartData["datasets"] = [
    {
      type: "bar" as const,
      label: primary.header,
      data: rows.map((r) => (r[primary.key] as number | null) ?? null),
      backgroundColor: primaryMuted ? mutedOf(theme.palette[0], theme.surface) : theme.palette[0],
      borderColor: highContrast ? theme.ink : theme.palette[0],
      borderWidth: highContrast ? 1 : 0,
      borderRadius: theme.barRadius ?? 4,
      order: 2,                    // Render bars below.
      _yCol: primary,
      datalabels: {
        ...leadAwareLabelStyle(theme, () => leadKey !== null && primary.key === leadKey),
        display: labelDisplay(showLabels, leadKey !== null && primary.key === leadKey),
        anchor: "end" as const,
        align: "end" as const,
        offset: 2,
        formatter: (v: unknown) => labelText(v, columnFormatCtx(primary, rows)),
      },
    } as ChartData["datasets"][number],
    ...rest.map((yc, i) => {
      const data = rows.map((r) => (r[yc.key] as number | null) ?? null);
      const labelIdx = lineLabelIndexes(data);
      const full = seriesColor(theme.palette, i + 1, theme.surface, theme.ink);
      const muted = leadKey !== null && yc.key !== leadKey;
      const color = muted ? mutedOf(full, theme.surface) : full;
      return {
        type: "line" as const,
        label: yc.header,
        data,
        borderColor: color,
        backgroundColor: "transparent",
        borderWidth: muted ? Math.max(1, (theme.lineWidth ?? 2) - 0.5) : (theme.lineWidth ?? 2),
        pointRadius: theme.pointRadius ?? 3,
        pointBackgroundColor: color,
        tension: 0.2,
        order: 1,                    // Render lines above.
        _yCol: yc,
        datalabels: {
          ...leadAwareLabelStyle(theme, () => leadKey !== null && yc.key === leadKey),
          display: (ctx: { dataIndex: number }) =>
            labelDisplay(showLabels && labelIdx.has(ctx.dataIndex), leadKey !== null && yc.key === leadKey),
          align: "top" as const,
          offset: 4,
          formatter: (v: unknown) => labelText(v, columnFormatCtx(yc, rows)),
        },
      } as unknown as ChartData["datasets"][number];
    }),
  ];

  const display = axisDisplay(yCols, rows, opts, "y", [0]);
  const options = baseOptions(spec, theme, opts);
  options.scales = {
    x: {
      ticks: {
        color: inkMid(theme),
        maxTicksLimit: categoryTickLimit(xCol, recipe),
        font: categoryTickFont(theme, recipe),
      },
      grid: { display: false },
    },
    y: {
      title: unitAxisTitle(yCols, theme, display),
      ticks: {
        color: theme.inkSoft,
        callback: axisTickOf(primary, rows, display),
        maxTicksLimit: recipe?.maxYTicks,
        font: valueTickFont(theme, recipe),
      },
      grid: { color: theme.grid },
      beginAtZero: true,
    },
  };
  return { type: "bar", data: { labels, datasets }, options };
}

// ─────────────────────────────────────────────────────────────
// pie
// ─────────────────────────────────────────────────────────────
function buildPie(
  spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {},
  pres: Presentation = { lead: null },
): ChartJSConfig {
  const rows = spec.rows ?? [];
  const recipe = renderRecipe(spec, theme, opts);
  const xCol = spec.columns.find((c) => c.type === "categorical" || c.type === "geo");
  const yCol = spec.columns.find((c) => c.type === "numeric");
  if (!xCol || !yCol) {
    throw new Error("pie requires 1 categorical + 1 numeric");
  }

  const labels = rows.map((r) => String(r[xCol.key] ?? ""));
  const data = rows.map((r) => (r[yCol.key] as number | null) ?? 0);
  if (data.some((v) => v < 0)) throw new Error("Pie shares require nonnegative values; use a bar chart or Table.");
  const visibleTotal = (chart?: { getDataVisibility?: (index: number) => boolean }) => data.reduce((sum, v, i) =>
    sum + ((!chart?.getDataVisibility || chart.getDataVisibility(i)) && Number.isFinite(v) && v >= 0 ? v : 0), 0);
  // 2.1b (D5 pie row) chose a protagonist slice (caption match, else the largest). It is emphasized by the centre
  // headline, its legend ink and a small offset — **not** by fading the other slices.
  //
  // Readability audit (pie, 71 charts): `mutedOf` pulls each supporting color 70% toward the surface, which collapses
  // distinct hues into near-identical pastels; on `2026 Revenue Share by Province` Jiangsu/Tianjin/Henan became three
  // beige wedges the legend could no longer map back. A bar's identity is its axis label, so fading its supporting
  // marks costs nothing; a slice's identity *is* its color. Textures only start from the 7th series, so they don't help
  // the first six slices. Every slice therefore keeps its full series color.
  const leadEl = pres.lead?.kind === "element" ? pres.lead.index : null;
  const colors = rows.map((_, i) => seriesColor(theme.palette, i, theme.surface, theme.ink));
  const LEAD_OFFSET_PX = 8;
  // Batch 1 2.2: pie labels = proportions (the full information); omit narrow <4% slices to avoid crowding.
  const showLabels = resolveDataLabels(spec, opts, 1, rows.length, recipe);
  const total = data.reduce((a, b) => a + (b || 0), 0);
  const pieLabels = {
    // Slice labels are drawn inside the coloured slice, so they keep the surface backing plate.
    ...labelBase(theme, { plate: true }),
    display: (ctx: { dataIndex: number; chart?: { getDataVisibility?: (i: number) => boolean } }) =>
      labelDisplay(showLabels && visibleTotal(ctx.chart) > 0 && (data[ctx.dataIndex] || 0) / visibleTotal(ctx.chart) >= 0.04),
    formatter: (v: unknown, ctx?: { chart?: { getDataVisibility?: (i: number) => boolean } }) =>
      typeof v === "number" && visibleTotal(ctx?.chart) > 0 ? formatPercentCell(v / visibleTotal(ctx?.chart), "fraction") : "",
  };

  const options = baseOptions(spec, theme, opts);
  // Leave donut center empty (cutout 62%); legend on the right.
  (options as { cutout?: string }).cutout = "62%";
  options.plugins = {
    ...options.plugins,
    legend: {
      ...options.plugins?.legend,
      position: recipe?.legendPosition ?? "right",
      align: "start",
      title: { display: true, text: opts.visibleShareLabel ?? "Share of visible items", color: theme.inkSoft, font: { size: 11, family: fontSans(theme) } },
      labels: {
        color: theme.inkSoft,
        boxWidth: 8,
        boxHeight: 8,
        padding: recipe?.densityTier === "compact" ? 10 : 14,
        font: { size: recipe?.legendFontSize ?? 11.5, family: fontSans(theme) },
        // Batch 3 4.2 (design C v2 C5): legend includes proportions; names-only pie legends force users
        // to estimate proportions from slice area. Data already exists in datasets; no new request.
        generateLabels: (chart) => {
          const ds = chart.data.datasets[0] as { data?: unknown; backgroundColor?: unknown } | undefined;
          const values = (ds?.data ?? []) as number[];
          const sum = visibleTotal(chart);
          const legendColors = (Array.isArray(ds?.backgroundColor) ? ds.backgroundColor : []) as string[];
          return (chart.data.labels ?? []).map((label, i) => {
            const pct = sum > 0 && chart.getDataVisibility(i) ? formatPercentCell((values[i] || 0) / sum, "fraction") : "";
            return {
              text: pct ? `${String(label)} ${pct}` : String(label),
              fillStyle: legendColors[i],
              strokeStyle: legendColors[i],
              lineWidth: 0,
              hidden: !chart.getDataVisibility(i),
              index: i,
              fontColor: leadEl !== null && i === leadEl ? theme.ink : theme.inkSoft,
            };
          });
        },
      },
    },
    tooltip: {
      ...options.plugins?.tooltip,
      callbacks: {
        label: (ctx) => {
          const total = visibleTotal(ctx.chart);
          const v = ctx.parsed as unknown as number;
          const pct = total > 0 ? formatPercentCell(v / total, "fraction") : "—";
          const fmt = columnFormatCtx(yCol, rows);
          const observed = fmt.percent ? formatSourcePercent(v, fmt.percent) : formatSourceValue(v, fmt.unit);
          return `${ctx.label}: ${observed} (${opts.visibleShareLabel ?? "Share of visible items"}: ${pct})`;
        },
      },
    },
  };
  options.animation = {
    animateRotate: true,
    animateScale: false,
    duration: recipe?.animate === false ? 0 : 400,
    easing: "easeOutQuart",
  } as never;

  // 2.1b (D5 pie row, center headline number): draw protagonist proportion + name in donut center, approximately 15 lines of
  // afterDraw (same registration block as reference-line plugin: per-chart plugins, no global registration).
  const centerPlugin: Plugin | null = leadEl !== null && total > 0
    ? {
        id: "bowheadPieCenter",
        afterDraw: (chart) => {
          const { ctx, chartArea } = chart;
          if (!chart.getDataVisibility(leadEl) || visibleTotal(chart) <= 0) return;
          if (!ctx || !chartArea) return;
          const cx = (chartArea.left + chartArea.right) / 2;
          const cy = (chartArea.top + chartArea.bottom) / 2;
          const pct = formatPercentCell((data[leadEl] || 0) / visibleTotal(chart), "fraction");
          ctx.save();
          ctx.textAlign = "center";
          ctx.font = `650 20px ${fontSans(theme)}`;
          ctx.fillStyle = theme.ink;
          ctx.fillText(pct, cx, cy - 2);
          ctx.font = `400 11px ${fontSans(theme)}`;
          ctx.fillStyle = theme.inkSoft;
          ctx.fillText(labels[leadEl] ?? "", cx, cy + 16);
          ctx.restore();
        },
      }
    : null;

  return {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors,
        borderColor: usesPresetHighContrast(theme) ? theme.ink : theme.surface,
        borderWidth: usesPresetHighContrast(theme) ? 1 : 2,
        offset: rows.map((_, i) => (i === leadEl ? LEAD_OFFSET_PX : 0)),
        hoverOffset: 6,
        datalabels: pieLabels,
      }],
    } as unknown as ChartData,
    options,
    ...(centerPlugin ? { plugins: [centerPlugin] } : {}),
  };
}

// ─────────────────────────────────────────────────────────────
// scatter
// ─────────────────────────────────────────────────────────────
function buildScatter(
  spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {},
  pres: Presentation = { lead: null },
): ChartJSConfig {
  const rows = spec.rows ?? [];
  const recipe = renderRecipe(spec, theme, opts);
  const numCols = spec.columns.filter((c) => c.type === "numeric");
  // Identity column also recognizes `geo` (same as `buildBar` #298 fix). Scatter charts like `各省营业额与客流量分布关系`
  // using provinces as identity previously had no labels: five unnamed points, with Shanghai and Henan indistinguishable.
  // Find categorical first, then fall back to geo; **never combine || geo in one find**:
  // with column order `[省(geo), 门店(categorical), …]`, one find selects "省", coloring 40
  // stores as five provinces and losing store names entirely, worse than unnamed points (apparent identities
  // cannot distinguish points). geo serves as identity only when categorical is absent.
  const catCol = spec.columns.find((c) => c.type === "categorical")
    ?? spec.columns.find((c) => c.type === "geo");
  if (numCols.length < 2) {
    throw new Error("scatter requires ≥2 numeric");
  }
  const [xNum, yNum] = numCols;
  /** Whether the single-series + row-identity path was taken (determines attachment of per-point label plugin). */
  let identityLeadLabel: string | null = null;
  let hasIdentityLabels = false;
  // 2.1b (D5 scatter row + design D3): protagonist cluster uses large filled points + group label; others use small hollow points.
  // review R2-5: originally mapped by index, claiming index matched because it shared the resolver's Set order,
  // which **is false**: grouping here skips rows with nonnumeric x/y, while the resolver's Set
  // counts every row; an all-null group never enters datasets, shifting later group indices forward,
  // highlighting the wrong cluster (all-null group can also leave the whole chart without lead, fully muted). Map by group **name**
  // instead (dual_axis/combo already do this).
  const groupNames = catCol
    ? [...new Set((spec.rows ?? []).map((r) => String(r[catCol.key] ?? "")))]
    : [];
  const leadGroup = pres.lead?.kind === "dataset" ? (groupNames[pres.lead.index] ?? null) : null;

  // ★ Is the categorical column a grouping dimension or row identity?
  //
  // More groups than palette slots necessarily repeats `palette[i % 6]` colors: legend uses six colors
  // to claim distinction among N objects, leaving each point's store unclear. 116 corpus measurements: **16 scatter charts
  // with categorical columns, all 16 had one group per row** (stores / major categories / representatives, etc.), with 20~50 groups.
  // Thus the multi-group scatter path has never been legitimately used in production; every use misclassified row identity
  // as grouping.
  //
  // When groups exceed slots, use the **single-series** path (already used for empty `catCol`, with an existing design).
  // `_pointLabels` carries identity into tooltips; protagonist remains highlighted, changing from a cluster to one point.
  //   ⚠ Per-point custom keys cannot live on `data` (react-chartjs-2 cloneData drops them;
  //     see `_fmtCtx` comment), so attach **parallel arrays** to dataset, indexed by dataIndex.
  const identityMode = catCol !== undefined && (spec._export_frame_entities?.values.length ?? groupNames.length) > theme.palette.length;

  // Group categories into multiple series; no category / categorical row identity → single series.
  const datasets: ChartData["datasets"] = [];
  if (catCol && identityMode) {
    const pts: Array<{ x: number; y: number; _sourceRowIndex?: number }> = [];
    const labels: string[] = [];
    for (const [sourceIndex, r] of rows.entries()) {
      const x = r[xNum.key];
      const y = r[yNum.key];
      if (typeof x !== "number" || typeof y !== "number") continue;
      pts.push({ x, y, ...(opts.focusBinding || spec._export_annotations?.length ? { _sourceRowIndex: sourceIndex } : {}) });
      labels.push(String(r[catCol.key] ?? ""));
    }
    datasets.push({
      label: yNum.header,
      data: pts,
      backgroundColor: theme.palette[0],
      borderColor: theme.palette[0],
      pointRadius: Math.max(4, (theme.pointRadius ?? 3) + 2),
      _yCol: yNum,
      _pointLabels: labels,
    } as ChartData["datasets"][number]);
  } else if (catCol) {
    const groups = new Map<string, Array<{ x: number; y: number; _sourceRowIndex?: number }>>();
    for (const [sourceIndex, r] of rows.entries()) {
      const k = String(r[catCol.key] ?? "");
      const x = r[xNum.key];
      const y = r[yNum.key];
      if (typeof x !== "number" || typeof y !== "number") continue;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k)!.push({ x, y, ...(opts.focusBinding || spec._export_annotations?.length ? { _sourceRowIndex: sourceIndex } : {}) });
    }
    let i = 0;
    const frameEntities = spec._export_frame_entities;
    const orderedGroups = frameEntities?.key === catCol.key ? frameEntities.values.map((value) => [String(value), groups.get(String(value)) ?? []] as const) : groups;
    for (const [label, pts] of orderedGroups) {
      datasets.push({
        label,
        data: pts,
        backgroundColor: theme.palette[i % theme.palette.length],
        borderColor: theme.palette[i % theme.palette.length],
        pointRadius: Math.max(4, (theme.pointRadius ?? 3) + 2),
        _yCol: yNum,
      } as ChartData["datasets"][number]);
      i++;
    }
  } else {
    datasets.push({
      label: yNum.header,
      data: rows
        .map((r, sourceIndex) => ({
          ...(opts.focusBinding || spec._export_annotations?.length ? { _sourceRowIndex: sourceIndex } : {}),
          x: (r[xNum.key] as number | null) ?? NaN,
          y: (r[yNum.key] as number | null) ?? NaN,
        }))
        .filter((p) => !Number.isNaN(p.x) && !Number.isNaN(p.y)),
      backgroundColor: theme.palette[0],
      pointRadius: Math.max(4, (theme.pointRadius ?? 3) + 2),
      _yCol: yNum,
    } as ChartData["datasets"][number]);
  }

  // Scatter points: translucent fill + opaque stroke; hover enlarges points and thickens stroke (per design).
  datasets.forEach((ds) => {
    (ds as { _xCol?: ChartColumn })._xCol = xNum;
    const color = ((ds as { borderColor?: string }).borderColor
      ?? (ds as { backgroundColor?: string }).backgroundColor) ?? theme.primary;
    const pointLabels = (ds as { _pointLabels?: string[] })._pointLabels;
    if (pointLabels) {
      // Single series + row identity: protagonist is **one point**; arrays provide per-point styles (natively supported by Chart.js).
      const leadIdx = leadGroup === null ? -1 : pointLabels.indexOf(leadGroup);
      const dim = leadIdx >= 0;
      const frameEntities = spec._export_frame_entities;
      const identityColor = (label: string) => frameEntities ? theme.palette[frameEntities.values.findIndex((value) => String(value) === label) % theme.palette.length] : color;
      (ds as { backgroundColor?: string[] }).backgroundColor = pointLabels.map((label, i) =>
        i === leadIdx ? identityColor(label) : (dim ? theme.surface : withAlpha(identityColor(label), "90")),
      );
      (ds as { borderColor?: string[] }).borderColor = pointLabels.map((label, i) =>
        dim && i !== leadIdx ? mutedOf(identityColor(label), theme.surface) : identityColor(label),
      );
      (ds as { borderWidth?: number[] }).borderWidth = pointLabels.map((_, i) => (dim && i !== leadIdx ? 1.5 : 1));
      (ds as { pointRadius?: number[] }).pointRadius = pointLabels.map((_, i) =>
        i === leadIdx ? 6 : (dim ? 4 : 5),
      );
      (ds as { hoverBorderWidth?: number }).hoverBorderWidth = 1.8;
      (ds as { hoverRadius?: number }).hoverRadius = 7;
      (ds as { hitRadius?: number }).hitRadius = 12;
      // ★ Per-point identity labels: no longer label only the protagonist.
      //
      // Compute collision avoidance ourselves (datalabels has none); it needs actual pixels,
      // unavailable to pure `buildChartJSConfig` without scales ⇒ plugin `afterLayout`
      // fills `display` / `align` (see `scatterLabelPlugin`). Only default styles live here.
      // ⚠ Collision results live on the **dataset** (`_labelPicked` / `_labelAlign`),
      // never a closure-captured object: `buildChartJSConfig` runs each render and creates a new one,
      // while react-chartjs-2 reads `plugins` **only once during construction**. Rerender replaces dataset
      // (predicates point to the new object), but plugin remains old (writes to old object), so labels never appear.
      // Reproduced locally and on device: plugin reported picked=18 while live dataset display values were all false.
      // Use dataset because plugin `afterLayout` reads `chart.data.datasets[0]`
      // (write to the live object), following existing `_pointLabels` / `_fmtCtx` conventions.
      (ds as { datalabels?: unknown }).datalabels = {
        // ⚠ Protagonist predicate must **actually test**, never always return true: `leadAwareLabelStyle` gives protagonist
        // sans 12.5/650 and others mono 10. Always true enlarges every label to protagonist size,
        // while plugin measures mono 10 ⇒ measured width is narrower than drawn width ⇒ failed avoidance and overlapping labels.
        // Observed locally and on device (`姚家园` overlapping `马家堡`).
        ...leadAwareLabelStyle(theme, (c) => c.dataIndex === leadIdx),
        display: (ctx: { dataIndex: number; dataset?: { _labelPicked?: Set<number> } }) =>
          ctx.dataset?._labelPicked?.has(ctx.dataIndex) ?? false,
        align: (ctx: { dataIndex: number; dataset?: { _labelAlign?: Map<number, LabelAlign> } }) =>
          ctx.dataset?._labelAlign?.get(ctx.dataIndex) ?? "top",
        offset: 5,
        formatter: (_v: unknown, ctx: { dataIndex: number }) => pointLabels[ctx.dataIndex] ?? "",
      };
      hasIdentityLabels = true;
      identityLeadLabel = leadGroup;
      return;
    }
    const isLead = leadGroup !== null && String(ds.label ?? "") === leadGroup;
    const isDimmed = leadGroup !== null && !isLead;
    // Lead cluster: large filled points; other clusters: small hollow points (muted stroke).
    (ds as { backgroundColor?: string }).backgroundColor = isLead
      ? color
      : (isDimmed ? theme.surface : withAlpha(color, "90")); // Without lead, preserve current approximately 55% fill.
    (ds as { borderColor?: string }).borderColor = isDimmed ? mutedOf(color, theme.surface) : color;
    (ds as { borderWidth?: number }).borderWidth = isDimmed ? 1.5 : 1;
    (ds as { hoverBorderWidth?: number }).hoverBorderWidth = 1.8;
    (ds as { pointRadius?: number }).pointRadius = isLead ? 6 : (isDimmed ? 4 : 5);
    (ds as { hoverRadius?: number }).hoverRadius = 7;
    (ds as { hitRadius?: number }).hitRadius = 12;
    if (isLead) {
      // Label protagonist cluster's group name only at the highest point (maximum y), avoiding per-point noise.
      const pts = (ds.data ?? []) as Array<{ x: number; y: number; _sourceRowIndex?: number }>;
      let topIdx = -1;
      pts.forEach((p, i) => {
        if (topIdx < 0 || p.y > pts[topIdx].y) topIdx = i;
      });
      (ds as { datalabels?: unknown }).datalabels = {
        ...leadAwareLabelStyle(theme, () => true),
        display: (ctx: { dataIndex: number }) => ctx.dataIndex === topIdx,
        align: "top" as const,
        offset: 5,
        formatter: () => String(ds.label ?? ""),
      };
    }
  });

  const xDisplay = axisDisplay([xNum], rows, opts, "x");
  const yDisplay = axisDisplay([yNum], rows, opts, "y");
  const options = baseOptions(spec, theme, opts);
  options.scales = {
    x: {
      type: "linear",
      // Both scatter axes previously used **bare header** (the only kind bypassing axisTitleOf). After percentage scaling
      // changed 0.292→29.2 while the title remained "退款率", nothing explained the dimension.
      // axisTitleOf(always) gives parity with dual_axis: add "（%）" for percentage columns;
      // without a displayable unit, text remains identical character for character.
      title: axisTitleOf(xNum, theme, true, xDisplay),
      ticks: {
        color: theme.inkSoft,
        callback: axisTickOf(xNum, rows, xDisplay),
        maxTicksLimit: recipe?.maxXTicks,
        font: valueTickFont(theme, recipe),
      },
      grid: { display: false },
      border: axisBorder(theme),
    },
    y: {
      type: "linear",
      title: axisTitleOf(yNum, theme, true, yDisplay),
      ticks: {
        color: theme.inkSoft,
        callback: axisTickOf(yNum, rows, yDisplay),
        maxTicksLimit: recipe?.maxYTicks,
        font: valueTickFont(theme, recipe),
      },
      grid: gridDashed(theme),
      border: { display: false },
    },
  };

  return {
    type: "scatter",
    data: { datasets },
    options,
    plugins: hasIdentityLabels ? [scatterLabelPlugin(identityLeadLabel, theme)] : undefined,
  };
}

/**
 * Per-point label collision-avoidance plugin.
 *
 * One task: in `afterLayout` (scales ready), use real pixels to decide which points to label and which direction,
 * writing back to the **current live dataset** (`_labelPicked` / `_labelAlign`).
 * datalabels `display` / `align` predicates read `ctx.dataset`, keeping both sides on the same source.
 *
 * ⚠ Never in `afterDraw`: labels are already drawn then.
 */
/**
 * Category-axis end value labels can extend beyond canvas: measure actual text width and reserve margins **only** for overflowing charts.
 * Criteria and tradeoffs are documented atop `edge-label-padding.ts`.
 *
 * ⚠ Only handles charts with category x axes (vertical bars / lines / combos). hbar labels sit at **bar ends**,
 * with different geometry (touching the edge only when axis max equals data max); not observed this time, so leave unchanged.
 *
 * ★ Same discipline as `scatterLabelPlugin`: read everything from `chart` currently; closures store nothing
 * tied to an individual render, because react-chartjs-2 reads `plugins` only during construction (batch 1 encountered this).
 * Ask dataset's `datalabels.display / formatter / font` directly for label text and font;
 * do not duplicate those conventions.
 */
const H_PADDING = 6; // labelBase.padding is 3 on each horizontal side.

/** Stateless (all state lives on chart) ⇒ one global instance suffices; no recreation per build. */
const EDGE_LABEL_PADDING_PLUGIN: Plugin = (() => {
  return {
    id: "edge-label-padding",
    afterLayout(chart) {
      const guard = chart as unknown as { _edgePadBusy?: boolean };
      if (guard._edgePadBusy) return;
      if ((chart.options as { indexAxis?: string }).indexAxis === "y") return;
      // Only charts with an x axis have first/last tick labels. A pie or donut has none: on its first frame the
      // chartArea fallback below treated the plot edges as ticks and padded both sides, which shrank the legend and
      // left a fresh donut laid out differently from an updated one.
      if (!(chart.scales as Record<string, unknown> | undefined)?.x) return;
      const count = chart.data.labels?.length ?? 0;
      if (count < 1) return;

      const ctx = chart.ctx;
      const widest = { first: 0, last: 0 };
      ctx.save();
      chart.data.datasets.forEach((ds, datasetIndex) => {
        const dl = (ds as { datalabels?: Record<string, unknown> }).datalabels;
        if (!dl) return;
        const data = (ds as { data?: unknown[] }).data ?? [];
        for (const [side, dataIndex] of [["first", 0], ["last", count - 1]] as const) {
          const arg = { dataIndex, datasetIndex, dataset: ds };
          const display =
            typeof dl.display === "function" ? (dl.display as (a: unknown) => unknown)(arg) : dl.display;
          if (!display) continue;
          const text =
            typeof dl.formatter === "function"
              ? (dl.formatter as (v: unknown, a: unknown) => unknown)(data[dataIndex], arg)
              : String(data[dataIndex] ?? "");
          if (typeof text !== "string" || !text) continue;
          const font = (typeof dl.font === "function" ? (dl.font as (a: unknown) => unknown)(arg) : dl.font) as
            | { family?: string; size?: number; weight?: number }
            | undefined;
          ctx.font = `${font?.weight ? `${font.weight} ` : ""}${font?.size ?? 10}px ${font?.family ?? CHART_FONT_SANS_FALLBACK}`;
          widest[side] = Math.max(widest[side], ctx.measureText(text).width + H_PADDING);
        }
      });
      ctx.restore();

      const pad = (chart.options.layout?.padding ?? {}) as { left?: number; right?: number; top?: number };
      const curLeft = typeof pad.left === "number" ? pad.left : 0;
      const curRight = typeof pad.right === "number" ? pad.right : 0;
      const curTop = typeof pad.top === "number" ? pad.top : 0;
      // Top: how far the highest upward label pokes above the canvas **in this layout**, then raise the plot top by
      // that much. ⚠ `layout.padding.top` is not added to the plot top: Chart.js takes the larger of the padding and the
      // value axis's own tick overhang (about half a tick line). The former "natural slack" model assumed padding adds,
      // so a label 5.6px past the edge got 9px padding that disappeared inside a 9.6px overhang and stayed clipped
      // (Chart Embed Spec review: 980 under a 1000 maximum at 390). Padding below what is needed is never reduced
      // here: every new config starts from the builder's padding again.
      const overflowTop = topLabelPadding(...highestUpwardLabel(chart, count));
      const nextTop = overflowTop > 0
        ? Math.ceil(Math.max(curTop, chart.chartArea?.top ?? 0)) + overflowTop
        : curTop;
      if (!widest.first && !widest.last && Math.abs(nextTop - curTop) <= 1) return;
      // ★ Anchor must be **the element's own position**: labels anchor there.
      //   Neither alternative works: `chartArea` ends (line endpoints do sit at axis ends, but vertical bars'
      //   `offset:true` labels are band-centered, already inset half a cell; axis-end anchors needlessly reserve margins
      //   on bars that fit); `getPixelForTick(i)` (index refers to **displayed** ticks,
      //   thinned by autoSkip for many categories; observed `getPixelForTick(19)` returns 0 for 20 categories).
      //   Once elements exist, their position for **this** layout comes from the current scale (`indexPixelX`):
      //   at `afterLayout` `meta.data` still holds the previous frame. When a rerender resets `layout.padding`,
      //   the previous frame was the padded one, so the last label looked as if it fitted and was clipped.
      //   The first frame (no element coordinates yet) keeps the chartArea fallback, so exports are unchanged.
      const meta = chart.getDatasetMeta(0);
      const current = (dataIndex: number): number | undefined => {
        const previous = (meta?.data?.[dataIndex] as { x?: number } | undefined)?.x;
        if (!Number.isFinite(previous)) return undefined;
        return indexPixelX(meta, dataIndex) ?? previous;
      };
      const elFirst = current(0);
      const elLast = current(count - 1);
      const tickFirst = Number.isFinite(elFirst) ? (elFirst as number) : chart.chartArea.left;
      const tickLast = Number.isFinite(elLast) ? (elLast as number) : chart.chartArea.right;
      const next = edgeLabelPadding({
        // Subtract this plugin's own added margin to get natural slack, making the result largely independent of its effect.
        gapLeft: tickFirst - curLeft,
        gapRight: chart.width - tickLast - curRight,
        firstWidth: widest.first,
        lastWidth: widest.last,
      });
      // ⚠ 1px hysteresis: `offset:true` ticks scale with padding by (n-0.5)/n,
      //   so natural slack is not strictly invariant, differing by P/(2n). Without hysteresis it can oscillate around ±1px.
      if (
        Math.abs(next.left - curLeft) <= 1
        && Math.abs(next.right - curRight) <= 1
        && Math.abs(nextTop - curTop) <= 1
      ) return;
      chart.options.layout = {
        ...chart.options.layout,
        padding: { ...pad, left: next.left, right: next.right, top: nextTop },
      };
      guard._edgePadBusy = true;
      try {
        chart.update("none");
      } finally {
        guard._edgePadBusy = false;
      }
    },
  };
})();

/**
 * Horizontal bars (D3): value labels sit outside the bar end, so the plot keeps room on the right for the widest
 * displayed label plus 8px — a label never moves into the bar and is never clipped by the canvas edge.
 *
 * Measured with the label's own font and formatter (same discipline as `EDGE_LABEL_PADDING_PLUGIN`); charts without
 * displayed labels keep their padding. Labels of negative values point left and are not covered here.
 */
const HBAR_LABEL_RESERVE_PX = 8;
const HBAR_LABEL_PADDING_PLUGIN: Plugin = {
  id: "hbar-label-padding",
  afterLayout(chart) {
    const guard = chart as unknown as { _hbarPadBusy?: boolean };
    if (guard._hbarPadBusy) return;
    if ((chart.options as { indexAxis?: string }).indexAxis !== "y") return;
    const count = chart.data.labels?.length ?? 0;
    if (count < 1) return;
    const ctx = chart.ctx;
    let widest = 0;
    ctx.save();
    chart.data.datasets.forEach((ds, datasetIndex) => {
      const dl = (ds as { datalabels?: Record<string, unknown> }).datalabels;
      if (!dl || !chart.isDatasetVisible(datasetIndex)) return;
      const data = (ds as { data?: unknown[] }).data ?? [];
      for (let dataIndex = 0; dataIndex < count; dataIndex += 1) {
        const arg = { dataIndex, datasetIndex, dataset: ds, chart };
        const read = (value: unknown) => (typeof value === "function" ? (value as (a: unknown) => unknown)(arg) : value);
        if (!read(dl.display)) continue;
        const value = data[dataIndex];
        if (typeof value === "number" && value < 0) continue;
        const text = typeof dl.formatter === "function"
          ? (dl.formatter as (v: unknown, a: unknown) => unknown)(value, arg)
          : String(value ?? "");
        if (typeof text !== "string" || !text) continue;
        const font = read(dl.font) as { family?: string; size?: number; weight?: number } | undefined;
        const offset = read(dl.offset);
        ctx.font = `${font?.weight ? `${font.weight} ` : ""}${font?.size ?? 12}px ${font?.family ?? CHART_FONT_SANS_FALLBACK}`;
        widest = Math.max(widest, ctx.measureText(text).width + (typeof offset === "number" ? offset : 4));
      }
    });
    ctx.restore();
    const pad = (chart.options.layout?.padding ?? {}) as { right?: number };
    const curRight = typeof pad.right === "number" ? pad.right : 0;
    const nextRight = widest > 0 ? Math.ceil(widest + HBAR_LABEL_RESERVE_PX) : curRight;
    if (Math.abs(nextRight - curRight) <= 1) return;
    chart.options.layout = { ...chart.options.layout, padding: { ...pad, right: nextRight } };
    guard._hbarPadBusy = true;
    try {
      chart.update("none");
    } finally {
      guard._hbarPadBusy = false;
    }
  },
};

/**
 * Upward label nearest the canvas top: return `[anchor y in this layout, label box height + gap]` for `topLabelPadding`.
 * Upward = line `align: "top"`, or vertical bar `anchor/align: "end"` with nonnegative value (negative bar end points down).
 * Label box height uses datalabels default line height 1.2 plus vertical padding (`labelBase.padding`, 1 each).
 * No upward labels means Infinity slack ⇒ no extra padding.
 */
function highestUpwardLabel(chart: Chart, count: number): [number, number] {
  let best: [number, number] = [Number.POSITIVE_INFINITY, 0];
  chart.data.datasets.forEach((ds, datasetIndex) => {
    const dl = (ds as { datalabels?: Record<string, unknown> }).datalabels;
    if (!dl || !chart.isDatasetVisible(datasetIndex)) return;
    const meta = chart.getDatasetMeta(datasetIndex);
    const data = (ds as { data?: unknown[] }).data ?? [];
    for (let dataIndex = 0; dataIndex < count; dataIndex += 1) {
      const arg = { dataIndex, datasetIndex, dataset: ds, chart };
      const read = (value: unknown) => (typeof value === "function" ? (value as (a: unknown) => unknown)(arg) : value);
      if (!read(dl.display)) continue;
      // Read direction before sideways adjustment: shifted labels remain above their points, still requiring top margin.
      const align = read(baseAlignOf(ds as TickAvoidDataset));
      const value = data[dataIndex];
      const upward = align === "top"
        || (align === "end" && read(dl.anchor) === "end" && typeof value === "number" && value >= 0);
      if (!upward) continue;
      const y = valuePixelY(meta, dataIndex);
      if (y === null) continue;
      const font = read(dl.font) as { size?: number } | undefined;
      const offset = read(dl.offset);
      const height = (font?.size ?? 10) * 1.2 + 2 + (typeof offset === "number" ? offset : 4);
      if (y - height < best[0] - best[1]) best = [y, height];
    }
  });
  return best;
}

/**
 * Element's pixel x on the index axis for this layout: bars use the controller's own ruler (grouped bars sit
 * off the band centre), other controllers the index scale. Null when the scale or controller cannot answer.
 */
function indexPixelX(meta: ReturnType<Chart["getDatasetMeta"]> | undefined, dataIndex: number): number | null {
  const controller = meta?.controller as unknown as {
    getParsed?: (i: number) => Record<string, number> | undefined;
    _getRuler?: () => unknown;
    _calculateBarIndexPixels?: (i: number, ruler: unknown) => { center?: number };
  } | undefined;
  const iScale = meta?.iScale;
  if (!iScale || !controller?.getParsed) return null;
  if (controller._getRuler && controller._calculateBarIndexPixels) {
    const center = controller._calculateBarIndexPixels(dataIndex, controller._getRuler()).center;
    return typeof center === "number" && Number.isFinite(center) ? center : null;
  }
  const v = controller.getParsed(dataIndex)?.[iScale.axis];
  const x = iScale.getPixelForValue(typeof v === "number" ? v : dataIndex);
  return Number.isFinite(x) ? x : null;
}

/**
 * Point's pixel y on the value axis, computed from current scale (stacked value when stacked).
 *
 * Never read `meta.data[i].y`: at `afterLayout`, elements still have previous-frame coordinates, none on the first frame.
 * Top padding would take effect only on the second update; export disables animation and never updates after construction,
 * so top-edge labels would still be clipped in exports (found during code review).
 */
function valuePixelY(meta: ReturnType<Chart["getDatasetMeta"]> | undefined, dataIndex: number): number | null {
  const controller = meta?.controller as unknown as {
    getParsed?: (i: number) => Record<string, number> | undefined;
    applyStack?: (scale: unknown, parsed: unknown) => number;
  } | undefined;
  const vScale = meta?.vScale;
  if (!vScale || !controller?.getParsed) return null;
  const parsed = controller.getParsed(dataIndex);
  const v = parsed?.[vScale.axis];
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  const stacked = meta._stacked && controller.applyStack ? controller.applyStack(vScale, parsed) : v;
  const y = vScale.getPixelForValue(stacked);
  return Number.isFinite(y) ? y : null;
}

/**
 * Shift point labels sideways when they overlap value-axis tick text (criteria/tradeoffs in `tickAvoidAlign`).
 *
 * Results live on **live dataset** (`_tickAvoidAlign`), like `scatterLabelPlugin`: plugins are read only during construction,
 * so closures cannot store render-specific objects. Wrap datalabels `align` to read the result **only for conflicts**;
 * charts without conflicts retain even their configuration. On react-chartjs-2 rerenders, `Object.assign` replaces `datalabels`
 * with the original unwrapped object, so check the wrapper function's marker each time, not a dataset flag.
 *
 * ★ Compute during `afterLayout`, deriving anchors from current scales (not `meta.data`): scales already reflect this layout,
 * but elements update coordinates afterward, so reading them gives the previous frame (reset position on first frame).
 * Run after `EDGE_LABEL_PADDING_PLUGIN`: its top padding triggers relayout, and this sees the resulting ticks.
 */
type TickAvoidDataset = {
  datalabels?: Record<string, unknown>;
  data?: unknown[];
  type?: string;
  _tickAvoidAlign?: Map<number, number>;
  /** `pointLabelPlacementPlugin` result per data index: a new align/offset, or null = hidden. */
  _pointLabelPlacement?: Map<number, { align: string; offset: number } | null>;
  /** datalabels `align` / `display` / `offset` as the builder wrote them, before `wrapLabelOverrides`. */
  _labelOverrideBase?: { align: unknown; display: unknown; offset: unknown };
};
type LabelOverrideFn = ((ctx: { dataIndex: number; dataset?: TickAvoidDataset }) => unknown) & {
  _labelOverride?: true;
};

function isLabelOverrideWrapper(value: unknown): value is LabelOverrideFn {
  return typeof value === "function" && (value as LabelOverrideFn)._labelOverride === true;
}

/** Builder-written datalabels option before `wrapLabelOverrides` (the option itself when unwrapped). */
function baseLabelOption(ds: TickAvoidDataset, key: "align" | "display" | "offset"): unknown {
  const value = ds.datalabels?.[key];
  return isLabelOverrideWrapper(value) ? ds._labelOverrideBase?.[key] : value;
}

/** Original `align` before wrapping (itself when unwrapped). */
function baseAlignOf(ds: TickAvoidDataset): unknown {
  return baseLabelOption(ds, "align");
}

/**
 * One wrapper for every per-label override the layout plugins compute (tick avoidance, point-label placement), so
 * plugins never wrap each other's wrappers. Each wrapped option reads the live dataset's result maps and falls back to
 * the builder's option; with no results the drawn label is the builder's. Wrapping happens only once a plugin has a
 * result, so charts without conflicts keep even their configuration objects.
 */
function wrapLabelOverrides(ds: TickAvoidDataset): void {
  const dl = ds.datalabels;
  if (!dl || isLabelOverrideWrapper(dl.align)) return;
  const base = { align: dl.align, display: dl.display, offset: dl.offset };
  const call = (option: unknown, ctx: unknown) =>
    typeof option === "function" ? (option as (c: unknown) => unknown)(ctx) : option;
  const align: LabelOverrideFn = (ctx) => {
    const placed = ctx.dataset?._pointLabelPlacement?.get(ctx.dataIndex);
    if (placed) return placed.align;
    const avoided = ctx.dataset?._tickAvoidAlign?.get(ctx.dataIndex);
    if (avoided !== undefined) return avoided;
    return call(base.align, ctx);
  };
  const display: LabelOverrideFn = (ctx) =>
    ctx.dataset?._pointLabelPlacement?.get(ctx.dataIndex) === null ? false : call(base.display, ctx);
  const offset: LabelOverrideFn = (ctx) =>
    ctx.dataset?._pointLabelPlacement?.get(ctx.dataIndex)?.offset ?? call(base.offset, ctx);
  align._labelOverride = true;
  display._labelOverride = true;
  offset._labelOverride = true;
  ds._labelOverrideBase = base;
  ds.datalabels = { ...dl, align, display, offset };
}

/** Layout boxes of all displayed value-axis tick text (line-height boxes, slightly wider than ink; prefer extra detection over misses). */
function verticalTickTextBoxes(chart: Chart): AxisTickTextBox[] {
  const boxes: AxisTickTextBox[] = [];
  const ctx = chart.ctx;
  ctx.save();
  for (const scale of Object.values(chart.scales)) {
    if (scale.axis !== "y" || typeof scale.getLabelItems !== "function") continue;
    const opts = scale.options as { display?: unknown; ticks?: { display?: unknown } };
    if (opts.display === false || opts.ticks?.display === false) continue;
    const side = scale.position === "right" ? "right" : "left";
    for (const item of scale.getLabelItems()) {
      const lines = (Array.isArray(item.label) ? item.label : [item.label]).map(String);
      ctx.font = item.font.string;
      const width = Math.max(0, ...lines.map((line) => ctx.measureText(line).width));
      const [tx, ty] = item.options.translation ?? [0, 0];
      const textAlign = item.options.textAlign;
      const left = textAlign === "right" || textAlign === "end"
        ? tx - width
        : textAlign === "center" ? tx - width / 2 : tx;
      const lineHeight = Number(item.font.lineHeight);
      if (!Number.isFinite(lineHeight)) continue;
      const baseline = item.options.textBaseline;
      const firstLineTop = ty + item.textOffset
        - (baseline === "top" ? 0 : baseline === "bottom" ? lineHeight : lineHeight / 2);
      boxes.push({
        side,
        left,
        right: left + width,
        top: firstLineTop,
        bottom: firstLineTop + lines.length * lineHeight,
      });
    }
  }
  ctx.restore();
  return boxes;
}

const TICK_LABEL_AVOID_PLUGIN: Plugin = {
  id: "tick-label-avoid",
  afterLayout(chart) {
    if ((chart.options as { indexAxis?: string }).indexAxis === "y") return;
    const count = chart.data.labels?.length ?? 0;
    const ticks = count > 0 ? verticalTickTextBoxes(chart) : [];
    const ctx = chart.ctx;
    ctx.save();
    chart.data.datasets.forEach((raw, datasetIndex) => {
      const ds = raw as unknown as TickAvoidDataset;
      const dl = ds.datalabels;
      const next = new Map<number, number>();
      const meta = chart.getDatasetMeta(datasetIndex);
      const controller = meta?.controller as unknown as {
        getParsed?: (i: number) => Record<string, number> | undefined;
        applyStack?: (scale: unknown, parsed: unknown) => number;
      } | undefined;
      const iScale = meta?.iScale;
      const vScale = meta?.vScale;
      if (dl && ticks.length && iScale && vScale && controller?.getParsed && chart.isDatasetVisible(datasetIndex)) {
        const data = ds.data ?? [];
        const base = baseAlignOf(ds);
        for (let dataIndex = 0; dataIndex < count; dataIndex += 1) {
          const arg = { dataIndex, datasetIndex, dataset: ds, chart };
          const read = (value: unknown) =>
            typeof value === "function" ? (value as (a: unknown) => unknown)(arg) : value;
          if (!read(baseLabelOption(ds, "display"))) continue;
          const align = read(base);
          // Only point labels: anchored at point center, directly above/below. Bar-end labels (anchor: "end") sit at category-band centers,
          // already half a cell from the axis; different geometry, left untouched here.
          if (align !== "top" && align !== "bottom") continue;
          const anchor = read(dl.anchor);
          if (anchor !== undefined && anchor !== "center") continue;
          const parsed = controller.getParsed(dataIndex);
          const v = parsed?.[vScale.axis];
          const i = parsed?.[iScale.axis];
          if (typeof v !== "number" || !Number.isFinite(v) || typeof i !== "number") continue;
          const stacked = meta._stacked && controller.applyStack
            ? controller.applyStack(vScale, parsed)
            : v;
          const x = iScale.getPixelForValue(i);
          const y = vScale.getPixelForValue(stacked);
          if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
          const text = typeof dl.formatter === "function"
            ? (dl.formatter as (v: unknown, a: unknown) => unknown)(data[dataIndex], arg)
            : String(data[dataIndex] ?? "");
          if (typeof text !== "string" || !text) continue;
          const font = read(dl.font) as { family?: string; size?: number; weight?: number } | undefined;
          const size = font?.size ?? 10;
          ctx.font = `${font?.weight ? `${font.weight} ` : ""}${size}px ${font?.family ?? CHART_FONT_SANS_FALLBACK}`;
          const rawPad = read(dl.padding);
          // datalabels default padding is 4; `labelBase` provides { top: 1, bottom: 1, left: 3, right: 3 }.
          const side = (key: "top" | "bottom" | "left" | "right"): number => {
            if (typeof rawPad === "number") return rawPad;
            const one = (rawPad as Record<string, unknown> | undefined)?.[key];
            return typeof one === "number" ? one : 4;
          };
          const pad = { top: side("top"), bottom: side("bottom"), left: side("left"), right: side("right") };
          const width = ctx.measureText(text).width + pad.left + pad.right;
          const height = size * 1.2 + pad.top + pad.bottom;
          const rawOffset = read(dl.offset);
          const offset = typeof rawOffset === "number" ? rawOffset : 4;
          const centerY = align === "top" ? y - offset - height / 2 : y + offset + height / 2;
          const angle = tickAvoidAlign(
            { left: x - width / 2, right: x + width / 2, top: centerY - height / 2, bottom: centerY + height / 2 },
            align,
            ticks,
          );
          if (angle !== null) next.set(dataIndex, angle);
        }
      }
      if (next.size) wrapLabelOverrides(ds);
      ds._tickAvoidAlign = next.size ? next : undefined;
    });
    ctx.restore();
  },
};

/** Box size of one data label as datalabels draws it: measured text plus the label's own padding (default 4). */
function measureDataLabel(
  ctx: CanvasRenderingContext2D,
  dl: Record<string, unknown>,
  read: (value: unknown) => unknown,
  value: unknown,
  arg: unknown,
): { width: number; height: number } | null {
  const text = typeof dl.formatter === "function"
    ? (dl.formatter as (v: unknown, a: unknown) => unknown)(value, arg)
    : String(value ?? "");
  if (typeof text !== "string" || !text) return null;
  const font = read(dl.font) as
    | { family?: string; size?: number; weight?: number; lineHeight?: number | string }
    | undefined;
  const size = font?.size ?? 12;
  ctx.font = `${font?.weight ? `${font.weight} ` : ""}${size}px ${font?.family ?? CHART_FONT_SANS_FALLBACK}`;
  const rawPad = read(dl.padding);
  const side = (key: "top" | "bottom" | "left" | "right"): number => {
    if (typeof rawPad === "number") return rawPad;
    const one = (rawPad as Record<string, unknown> | undefined)?.[key];
    return typeof one === "number" ? one : 4;
  };
  return {
    width: ctx.measureText(text).width + side("left") + side("right"),
    height: labelLineHeight(font?.lineHeight, size) + side("top") + side("bottom"),
  };
}

/** datalabels line height (Chart.js `toLineHeight`): a multiplier, `"Npx"`, `"N%"`, or the default 1.2. */
export function labelLineHeight(lineHeight: number | string | undefined, size: number): number {
  if (typeof lineHeight === "number" && Number.isFinite(lineHeight)) return lineHeight * size;
  if (typeof lineHeight === "string") {
    const match = /^(-?[\d.]+)(px|em|%)?$/.exec(lineHeight.trim());
    if (match) {
      const value = Number(match[1]);
      if (match[2] === "px") return value;
      if (match[2] === "%") return (value / 100) * size;
      return value * size;
    }
  }
  return size * 1.2;
}

/** Private Chart.js BarController APIs the placement reads for bar rectangles (pinned by a unit test). */
let warnedMissingBarRuler = false;
function warnMissingBarRuler(): void {
  if (warnedMissingBarRuler || process.env.NODE_ENV === "production") return;
  warnedMissingBarRuler = true;
  console.warn(
    "[chart] BarController._getRuler/_calculateBarIndexPixels missing: point labels next to bars are not placed.",
  );
}

/** Chart-instance flag: while set, no point label hides (an image capture has no tooltip). */
export const POINT_LABEL_KEEP_ALL_KEY = "_pointLabelKeepAll";
function keepAllForCapture(chart: Chart): boolean {
  return (chart as unknown as Record<string, unknown>)[POINT_LABEL_KEEP_ALL_KEY] === true;
}

type PlacementRequest = PointLabelRequest & {
  datasetIndex: number;
  dataIndex: number;
  baseAlign: unknown;
  baseOffset: number;
};

/** Markers are drawn with a border; count one more pixel than the radius. */
const POINT_LABEL_MARKER_SLACK = 1;

/**
 * Bars mixed with lines (dual_axis / combo): keep point labels off bars, bar value labels and markers
 * (rules and geometry in `point-label-placement.ts`). 116 dogfood: `-24.0%` drawn on the blue bar at 390.
 *
 * Same discipline as `TICK_LABEL_AVOID_PLUGIN`: computed in `afterLayout` from the current scales (element coordinates
 * still belong to the previous frame), results on the live dataset (`_pointLabelPlacement`), applied through
 * `wrapLabelOverrides`. Runs after tick avoidance, so a moved or hidden label overrides its sideways tick shift.
 *
 * `keepAll`: surfaces without hover or an exact-value table cannot hide a label without losing the value, so every
 * label stays and only moves. Not attached to exports (static raster): see `buildChartJSConfig`.
 * A copy or PNG download of the live canvas is static too: `captureChartCanvas` sets `POINT_LABEL_KEEP_ALL_KEY` on
 * the chart for one redraw so the image carries every label (moved, never hidden).
 */
function pointLabelPlacementPlugin(keepAll: boolean): Plugin {
  return {
    id: "point-label-placement",
    afterLayout(chart) {
      if ((chart.options as { indexAxis?: string }).indexAxis === "y") return;
      const count = chart.data.labels?.length ?? 0;
      const area = chart.chartArea;
      const datasets = chart.data.datasets as unknown as TickAvoidDataset[];
      const kindOf = (i: number): string | undefined =>
        (chart.getDatasetMeta(i) as { type?: string } | undefined)?.type ?? datasets[i]?.type;
      const visible = datasets.map((_, i) => chart.isDatasetVisible(i));
      const hasBar = datasets.some((_, i) => visible[i] && kindOf(i) === "bar");
      const hasLine = datasets.some((_, i) => visible[i] && kindOf(i) === "line");
      if (count < 1 || !area || !hasBar || !hasLine) {
        datasets.forEach((ds) => {
          ds._pointLabelPlacement = undefined;
        });
        return;
      }

      const fills: LabelBox[] = [];
      const fixedLabels: LabelBox[] = [];
      const markers: PointLabelMarker[] = [];
      const segments: PointLabelSegment[] = [];
      const requests: PlacementRequest[] = [];
      let barGeometryMissing = false;
      const ctx = chart.ctx;
      ctx.save();
      datasets.forEach((ds, datasetIndex) => {
        if (!visible[datasetIndex]) return;
        const meta = chart.getDatasetMeta(datasetIndex);
        const data = ds.data ?? [];
        const dl = ds.datalabels;
        const kind = kindOf(datasetIndex);
        if (kind === "bar") {
          const controller = meta?.controller as unknown as {
            _getRuler?: () => unknown;
            _calculateBarIndexPixels?: (i: number, ruler: unknown) => { center?: number; size?: number };
          } | undefined;
          const vScale = meta?.vScale as { getBasePixel?: () => number } | undefined;
          if (!controller?._getRuler || !controller._calculateBarIndexPixels || !vScale?.getBasePixel) {
            // Without the bar rectangles every placement would ignore the bars: leave the builder's labels alone.
            if (controller && (!controller._getRuler || !controller._calculateBarIndexPixels)) warnMissingBarRuler();
            barGeometryMissing = true;
            return;
          }
          const ruler = controller._getRuler();
          const basePixel = vScale.getBasePixel();
          for (let dataIndex = 0; dataIndex < count; dataIndex += 1) {
            const end = valuePixelY(meta, dataIndex);
            const { center, size } = controller._calculateBarIndexPixels(dataIndex, ruler);
            if (end === null || typeof center !== "number" || typeof size !== "number") continue;
            if (!Number.isFinite(center) || !Number.isFinite(size)) continue;
            fills.push({
              left: center - size / 2,
              right: center + size / 2,
              top: Math.min(end, basePixel),
              bottom: Math.max(end, basePixel),
            });
            if (!dl) continue;
            const arg = { dataIndex, datasetIndex, dataset: ds, chart, active: false };
            const read = (value: unknown) =>
              typeof value === "function" ? (value as (a: unknown) => unknown)(arg) : value;
            if (!read(dl.display)) continue;
            const box = measureDataLabel(ctx, dl, read, data[dataIndex], arg);
            if (!box) continue;
            const rawOffset = read(dl.offset);
            const offset = typeof rawOffset === "number" ? rawOffset : 4;
            // datalabels' bar positioner orients every vertical bar upward (`orient` with origin.x null ⇒ {0, -1}), so
            // anchor/align "end" puts the label above the rectangle's top edge — for a negative bar that is the zero line.
            const rectTop = Math.min(end, basePixel);
            fixedLabels.push({
              left: center - box.width / 2,
              right: center + box.width / 2,
              top: rectTop - offset - box.height,
              bottom: rectTop - offset,
            });
          }
          return;
        }
        if (kind !== "line") return;
        const rawRadius = (ds as { pointRadius?: unknown }).pointRadius;
        const pointRadius = typeof rawRadius === "number" ? rawRadius : 3;
        // Lines break at null values (spanGaps off), so only consecutive drawn points form a segment.
        let previous: { x: number; y: number } | null = null;
        for (let dataIndex = 0; dataIndex < count; dataIndex += 1) {
          const x = indexPixelX(meta, dataIndex);
          const y = valuePixelY(meta, dataIndex);
          if (x === null || y === null) {
            previous = null;
            continue;
          }
          if (previous) segments.push({ x1: previous.x, y1: previous.y, x2: x, y2: y });
          previous = { x, y };
          if (pointRadius > 0) markers.push({ x, y, r: pointRadius + POINT_LABEL_MARKER_SLACK });
          if (!dl) continue;
          const arg = { dataIndex, datasetIndex, dataset: ds, chart, active: false };
          const read = (value: unknown) =>
            typeof value === "function" ? (value as (a: unknown) => unknown)(arg) : value;
          const display = read(baseLabelOption(ds, "display"));
          if (!display) continue;
          const baseAlign = read(baseLabelOption(ds, "align"));
          if (baseAlign !== "top" && baseAlign !== "bottom") continue;
          const anchor = read(dl.anchor);
          if (anchor !== undefined && anchor !== "center") continue;
          const box = measureDataLabel(ctx, dl, read, data[dataIndex], arg);
          if (!box) continue;
          const rawOffset = read(baseLabelOption(ds, "offset"));
          const baseOffset = typeof rawOffset === "number" ? rawOffset : 4;
          requests.push({
            id: `${datasetIndex}:${dataIndex}`,
            x,
            y,
            width: box.width,
            height: box.height,
            offset: baseOffset,
            // `true` is the protagonist (`labelDisplay`); "auto" labels may yield.
            keep: keepAll || keepAllForCapture(chart) || display === true,
            datasetIndex,
            dataIndex,
            baseAlign,
            baseOffset,
          });
        }
      });
      ctx.restore();
      if (barGeometryMissing) {
        datasets.forEach((ds) => {
          ds._pointLabelPlacement = undefined;
        });
        return;
      }

      // Value-axis tick text is an obstacle too: this plugin's result replaces the tick-avoid shift for every label
      // it places, so it must keep labels off the ticks itself.
      const text: LabelBox[] = verticalTickTextBoxes(chart).map(({ left, right, top, bottom }) => ({
        left,
        right,
        top,
        bottom,
      }));
      // Above the plot only the padding reserved for labels (by `EDGE_LABEL_PADDING_PLUGIN`) is usable.
      const pad = (chart.options.layout?.padding ?? {}) as { top?: unknown };
      const reservedTop = typeof pad.top === "number" ? pad.top : 0;
      // Protagonist labels claim their place first; then series order, then category order.
      const ordered = [...requests].sort((a, b) =>
        Number(b.keep) - Number(a.keep) || a.datasetIndex - b.datasetIndex || a.dataIndex - b.dataIndex);
      const placements = placePointLabels(
        ordered,
        { fills, labels: fixedLabels, markers, segments, text },
        { left: area.left, right: area.right, top: Math.max(0, area.top - reservedTop), bottom: area.bottom },
      );
      const results = new Map<number, Map<number, { align: string; offset: number } | null>>();
      // Every request gets an explicit entry, unchanged ones included, so the placement (which checked the ticks)
      // wins over a tick-avoid angle computed for the same label.
      placements.forEach((placement, i) => {
        const req = ordered[i]!;
        const perDataset = results.get(req.datasetIndex) ?? new Map<number, { align: string; offset: number } | null>();
        perDataset.set(
          req.dataIndex,
          placement.align === null ? null : { align: placement.align, offset: placement.offset },
        );
        results.set(req.datasetIndex, perDataset);
      });
      datasets.forEach((ds, datasetIndex) => {
        const next = results.get(datasetIndex);
        if (next?.size) wrapLabelOverrides(ds);
        ds._pointLabelPlacement = next?.size ? next : undefined;
      });
    },
  };
}

function scatterLabelPlugin(leadLabel: string | null, theme: ChartTheme): Plugin {
  return {
    id: "scatter-point-labels",
    afterLayout(chart) {
      const sx = chart.scales.x;
      const sy = chart.scales.y;
      const ds = chart.data.datasets[0] as unknown as {
        data?: Array<{ x: number; y: number }>;
        _pointLabels?: string[];
        _labelPicked?: Set<number>;
        _labelAlign?: Map<number, LabelAlign>;
      } | undefined;
      if (!sx || !sy || !ds) return;
      const labels = ds._pointLabels ?? [];
      const raw = ds.data ?? [];
      const pts = raw.map((d, i) => ({ x: d.x, y: d.y, label: labels[i] ?? "" }));
      const ctx = chart.ctx;
      ctx.save();
      // ★ **Measured font must match drawn font**, or collision avoidance describes a different chart.
      //   `leadAwareLabelStyle`: protagonist sans 12.5/650; others sans 12/500 (D1).
      //   `H_PADDING` keeps 3px of clearance on each horizontal side.
      const family = fontSans(theme);
      const LEAD_FONT = `${LEAD_LABEL_FONT.weight} ${LEAD_LABEL_FONT.size}px ${family}`;
      const REST_FONT = `${LABEL_FONT.weight} ${LABEL_FONT.size}px ${family}`;
      const H_PADDING = 6;
      // Every drawn marker, every visible dataset: a label must not cover another point. Positions come from the
      // scales like the label anchors do — element positions are only updated after this hook runs.
      const markers: Array<{ px: number; py: number; r: number }> = [];
      chart.data.datasets.forEach((dataset) => {
        const d = dataset as unknown as {
          hidden?: boolean;
          data?: Array<{ x: number; y: number }>;
          pointRadius?: number | number[];
          borderWidth?: number | number[];
        };
        // Series hidden from the legend draw no markers (`applySeriesVisibility` writes `hidden` on every dataset).
        if (d.hidden) return;
        (d.data ?? []).forEach((point, i) => {
          const radius = Array.isArray(d.pointRadius) ? (d.pointRadius[i] ?? 0) : (d.pointRadius ?? 3);
          const border = Array.isArray(d.borderWidth) ? (d.borderWidth[i] ?? 0) : (d.borderWidth ?? 0);
          if (radius <= 0) return;
          markers.push({ px: sx.getPixelForValue(point.x), py: sy.getPixelForValue(point.y), r: radius + border / 2 });
        });
      });
      const next = pickScatterLabels(pts, {
        markers,
        toPixel: (p) => ({ px: sx.getPixelForValue(p.x), py: sy.getPixelForValue(p.y) }),
        measure: (l) => {
          ctx.font = l === leadLabel ? LEAD_FONT : REST_FONT;
          return ctx.measureText(l).width + H_PADDING;
        },
        lineHeight: 14,
        area: chart.chartArea,
        lead: leadLabel,
      });
      ctx.restore();
      ds._labelPicked = next.picked;
      ds._labelAlign = next.align;
    },
  };
}


// ─────────────────────────────────────────────────────────────
// WP2 (design D3): waterfall / sankey / heatmap.
// ─────────────────────────────────────────────────────────────

/** Infer x column + numeric column (role first, type fallback), shared by waterfall/heatmap/sankey. */
function pickAxes(spec: ChartSpec): { catCols: ChartColumn[]; numCols: ChartColumn[] } {
  const cols = spec.columns ?? [];
  const catCols = cols.filter((c) => c.type === "categorical" || c.type === "date");
  const numCols = cols.filter((c) => c.type === "numeric");
  return { catCols, numCols };
}

/** Waterfall/financial bridge: rows = component increments (1 cat + 1 num), automatically append a Σ total bar.
 *  Floating bar [start,end]; positive=primary terracotta, negative=secondary slate, total=ink. */
function buildWaterfall(spec: ChartSpec, theme: ChartTheme, buildOpts: BuildOpts = {}): ChartJSConfig {
  const recipe = renderRecipe(spec, theme, buildOpts);
  const { catCols, numCols } = pickAxes(spec);
  const xKey = catCols[0]?.key;
  const vKey = numCols[0]?.key;
  if (!xKey || !vKey) throw new Error("waterfall needs 1 categorical + 1 numeric column");
  const rows = spec.rows ?? [];
  const labels: string[] = [];
  const data: Array<[number, number]> = [];
  const colors: string[] = [];
  let cum = 0;
  // Each floating bar is written [where the step starts, where it ends] (Chart.js draws the same rectangle either way;
  // the order documents the step and keeps `b - a` signed for the tooltip).
  for (const r of rows) {
    const v = Number(r[vKey] ?? 0);
    labels.push(String(r[xKey] ?? ""));
    data.push([cum, cum + v]);
    // 2.1b (D5 waterfall row + design D4): increases/decreases use semantic colors (positive green/negative red), not series palette.
    colors.push(v >= 0 ? theme.positive : theme.negative);
    cum += v;
  }
  labels.push("Σ");
  // Same convention: the total ends at its value, so a negative total is labelled below its bar, and the tooltip's
  // `b - a` is the signed total (it used to report −500 as 500).
  data.push([0, cum]);
  // Protagonist = final total (full-color terracotta, structurally built in).
  colors.push(theme.palette[0]);

  // Batch 1 2.2 (D5 waterfall row): label each segment's signed increment; Σ labels total.
  const showLabels = resolveDataLabels(spec, buildOpts, 1, labels.length, recipe);
  // review R1-6: Σ previously used data[i][1]-data[i][0], an always-positive **interval width**,
  // labeling negative total (-500) as 500. Use actual cumulative value.
  const deltaOf = (i: number) => (i < rows.length ? Number(rows[i][vKey] ?? 0) : cum);
  // review R1-7②: ratios are not additive (summing refund rates invents numbers); omit the Σ number,
  // while still drawing the chart (users see shape without a false total).
  const totalAdditive = !resolvePercentColumn(numCols[0]);

  const display = resolveAxisMagnitude(data.flat(), axisUnitOf(numCols[0]));
  const options = baseOptions(spec, theme, buildOpts);
  // Labels sit outside the bar ends, so the value axis needs headroom at both ends or the outermost labels (the
  // largest step, the total) are clipped by the plot edge — only when labels are drawn.
  // The value axis is declared for every waterfall, labelled or not: declaring it is what lets the chart-style
  // presentation (`applyGrid`) style its gridlines, so waterfalls now follow the team's gridline setting like every
  // other bar chart. Before, waterfalls had no `scales` and silently kept Chart.js default gridlines; declaring it only
  // for labelled ones would have given labelled and unlabelled waterfalls two different grids.
  options.scales = {
    x: { ticks: { color: inkMid(theme), font: categoryTickFont(theme, recipe) } },
    y: {
      ...(showLabels ? { grace: "8%" } : {}),
      title: unitAxisTitle([numCols[0]], theme, display),
      ticks: { color: theme.inkSoft, font: valueTickFont(theme, recipe), callback: axisTickOf(numCols[0], rows, display) },
    },
  };
  options.plugins = {
    ...options.plugins,
    legend: { display: false },
    tooltip: {
      ...options.plugins?.tooltip,
      callbacks: {
        label: (ctx) => {
          const [a, b] = ctx.raw as [number, number];
          const i = ctx.dataIndex;
          const delta = i < rows.length ? Number(rows[i][vKey] ?? 0) : b - a;
          const fmt = columnFormatCtx(numCols[0], rows);
          const value = fmt.percent ? formatSourcePercent(delta, fmt.percent) : formatSourceValue(delta, fmt.unit);
          return `${delta >= 0 ? "+" : ""}${value}`;
        },
      },
    },
  };
  return {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors,
        borderColor: usesPresetHighContrast(theme) ? theme.ink : colors,
        borderWidth: usesPresetHighContrast(theme) ? 1 : 0,
        borderRadius: 3,
        borderSkipped: false,
        datalabels: {
          // 2.1b: Σ protagonist uses large ink; component labels follow semantic colors (+green / −red, design D4).
          ...leadAwareLabelStyle(theme, (ctx) => ctx.dataIndex >= rows.length),
          color: (ctx: { dataIndex: number }) => {
            if (ctx.dataIndex >= rows.length) return theme.ink;
            return deltaOf(ctx.dataIndex) >= 0 ? theme.positive : theme.negative;
          },
          display: (ctx: { dataIndex: number }) =>
            labelDisplay(
              showLabels && (ctx.dataIndex < rows.length || totalAdditive),
              ctx.dataIndex >= rows.length,
            ),
          // Label each step at the edge where it **ends**: above a rise, below a drop (and below a negative total).
          // Readability audit (13 waterfalls): datalabels orients vertical bars upward regardless of sign, so the fixed
          // `end` put every drop's label on its top edge — level with the previous step's lower end, where it read as
          // that bar's label and collided with it — and pushed a first drop from zero above the plot, clipped.
          anchor: (ctx: { dataIndex: number }) => (deltaOf(ctx.dataIndex) < 0 ? "start" : "end"),
          align: (ctx: { dataIndex: number }) => (deltaOf(ctx.dataIndex) < 0 ? "start" : "end"),
          offset: 2,
          formatter: (_v: unknown, ctx: { dataIndex: number }) => {
            const i = ctx.dataIndex;
            const delta = deltaOf(i);
            const fmt = columnFormatCtx(numCols[0], rows);
            // Σ is total, not increment: omit +/− prefix, but **preserve a negative total's own minus sign**
            // (review R1-6: original implementation also applied Math.abs to Σ, labeling -500 as 500).
            if (i >= rows.length) return labelText(delta, fmt);
            return `${delta >= 0 ? "+" : "−"}${labelText(Math.abs(delta), fmt)}`;
          },
        },
      }],
    },
    options,
  };
}

/** Sankey flow: first two cat columns = from/to; first num column = flow. */
function buildSankey(spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {}): ChartJSConfig {
  const { catCols, numCols } = pickAxes(spec);
  if (catCols.length < 2 || numCols.length < 1) {
    throw new Error("sankey needs 2 categorical (from/to) + 1 numeric (flow) columns");
  }
  const [fromKey, toKey] = [catCols[0].key, catCols[1].key];
  const flowKey = numCols[0].key;
  const rows = spec.rows ?? [];
  const links = rows
    .map((r) => ({
      from: String(r[fromKey] ?? ""),
      to: String(r[toKey] ?? ""),
      flow: Number(r[flowKey] ?? 0),
    }))
    .filter((l) => l.from && l.to && l.flow > 0);
  // Stable node colors: from side selects palette by appearance order.
  const fromOrder: string[] = [];
  for (const l of links) if (!fromOrder.includes(l.from)) fromOrder.push(l.from);
  const colorOf = (name: string) =>
    theme.palette[Math.max(0, fromOrder.indexOf(name)) % theme.palette.length];

  const options = baseOptions(spec, theme, opts);
  options.plugins = {
    ...options.plugins,
    legend: { display: false },
    tooltip: {
      ...options.plugins?.tooltip,
      callbacks: {
        label: (ctx) => {
          const link = links[ctx.dataIndex];
          const fmt = columnFormatCtx(numCols[0], rows);
          const value = fmt.percent ? formatSourcePercent(link.flow, fmt.percent) : formatSourceValue(link.flow, fmt.unit);
          return `${link.from} → ${link.to}: ${value}`;
        },
      },
    },
  };
  return {
    type: "sankey" as ChartType,
    data: {
      datasets: [{
        data: links,
        colorFrom: (c: { dataset: { data: Array<{ from: string }> }; dataIndex: number }) =>
          colorOf(c.dataset.data[c.dataIndex].from),
        colorTo: () => theme.palette[5] ?? theme.ink,
        colorMode: "gradient",
        alpha: 0.55,
        borderWidth: 0,
        font: { size: 11, family: fontSans(theme) },
        color: theme.ink,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any],
    },
    options,
  };
}

/** Heatmap (chartjs-chart-matrix): cat1=x, cat2=y, num=value; sequential gradient coloring. */
function buildHeatmap(spec: ChartSpec, theme: ChartTheme, opts: BuildOpts = {}): ChartJSConfig {
  const { catCols, numCols } = pickAxes(spec);
  if (catCols.length < 2 || numCols.length < 1) {
    throw new Error("heatmap needs 2 categorical + 1 numeric columns");
  }
  const [xKey, yKey] = [catCols[0].key, catCols[1].key];
  const vKey = numCols[0].key;
  const rows = spec.rows ?? [];
  const xs: string[] = [];
  const ys: string[] = [];
  const cells = rows.flatMap((r) => {
    const x = String(r[xKey] ?? "");
    const y = String(r[yKey] ?? "");
    if (!xs.includes(x)) xs.push(x);
    if (!ys.includes(y)) ys.push(y);
    const value = r[vKey];
    return typeof value === "number" && Number.isFinite(value) ? [{ x, y, v: value }] : [];
  });
  // Range uses actual min/max (no zero anchor): index data (e.g. 31..99) wastes color range when anchored at zero.
  const vmax = cells.length ? Math.max(...cells.map((c) => c.v)) : 0;
  const vmin = cells.length ? Math.min(...cells.map((c) => c.v)) : 0;
  const mix = (t: number) => {
    // Linear interpolation (hex) from sequential.from → sequential.to.
    const hex = (h: string) => h.replace("#", "").match(/../g)!.map((b) => parseInt(b, 16));
    const [f, to] = [hex(theme.sequential.from), hex(theme.sequential.to)];
    const c = f.map((fv, i) => Math.round(fv + (to[i] - fv) * t));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  };

  const recipe = renderRecipe(spec, theme, opts);
  const options = baseOptions(spec, theme, opts);
  options.plugins = {
    ...options.plugins,
    legend: { display: false },
    tooltip: {
      ...options.plugins?.tooltip,
      callbacks: {
        title: () => "",
        label: (ctx) => {
          const c = (ctx.dataset.data as Array<{ x: string; y: string; v: number }>)[ctx.dataIndex];
          const fmt = columnFormatCtx(numCols[0], rows);
          const value = fmt.percent ? formatSourcePercent(c.v, fmt.percent) : formatSourceValue(c.v, fmt.unit);
          return `${c.y} × ${c.x}: ${value}`;
        },
      },
    },
  };
  options.scales = {
    x: {
      type: "category",
      labels: xs,
      offset: true,
      grid: { display: false },
      ticks: {
        color: inkMid(theme),
        maxTicksLimit: categoryTickLimit(catCols[0], recipe),
        font: categoryTickFont(theme, recipe),
      },
    },
    y: {
      type: "category",
      labels: ys,
      offset: true,
      grid: { display: false },
      ticks: { color: inkMid(theme), font: categoryTickFont(theme, recipe) },
    },
  };
  const config: ChartJSConfig = {
    type: "matrix" as ChartType,
    data: {
      datasets: [{
        data: cells,
        backgroundColor: (c: { dataset: { data: Array<{ v: number }> }; dataIndex: number }) => {
          const v = c.dataset.data[c.dataIndex].v;
          return mix(vmax === vmin ? 0.5 : (v - vmin) / (vmax - vmin));
        },
        borderWidth: 1,
        borderColor: theme.surface,
        width: (c: { chart: { chartArea?: { width: number } } }) =>
          ((c.chart.chartArea?.width ?? 300) / xs.length) - 2,
        height: (c: { chart: { chartArea?: { height: number } } }) =>
          ((c.chart.chartArea?.height ?? 200) / ys.length) - 2,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any],
    },
    options,
  };
  // Batch 1 2.3 (P2-7): color scale without legend forces guessing shade meaning. Return data with config;
  // ChartBlock renders the HTML gradient (a canvas legend is not worth it).
  const measureFormat = columnFormatCtx(numCols[0], rows);
  if (cells.length) config.heatmapLegend = {
    min: vmin, max: vmax, from: theme.sequential.from, to: theme.sequential.to,
    display: axisDisplay([numCols[0]], rows, opts, "value"),
    ...(measureFormat.percent ? { percent: measureFormat.percent } : measureFormat.unit ? { unit: measureFormat.unit } : {}),
  };
  return config;
}
