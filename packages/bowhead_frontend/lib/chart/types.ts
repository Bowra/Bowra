/**
 * Frontend ChartSpec mirror — keep synchronized with backend bowhead_core/chart/types.py.
 * Any field additions/removals must change both sides, or canvas hydration fails.
 */

export type ChartKind =
  | "kpi"
  | "line"
  | "bar"
  | "hbar"
  | "pie"
  | "stacked_bar"
  | "dual_axis"
  | "combo"        // V2.9 B1: same-axis bar/line combo (Target bar + Actual/Forecast line, same unit)
  | "scatter"
  | "heatmap"
  | "waterfall"   // WP2: waterfall/financial bridge (hint=bridge; floating bar)
  | "sankey"      // WP2: Sankey flow (hint=flow; chartjs-chart-sankey)
  | "area_stacked"
  | "geo"
  | "table";

export type ChartColumnType = "numeric" | "categorical" | "date" | "geo";
export type ChartColumnRole = "x" | "y" | "color" | "size" | "facet";
export type ChartColumnFormat = "int" | "float" | "percent" | "currency" | "datetime";

/** Persisted percentage-column shape. `fraction` = 0.83 means 83%; `points` = 13.5 means 13.5%.
 *  Shape is a **column property**, not inferable from values alone — in one Datarc workspace, achievement rate 1.02 is
 *  fraction (102%), while market share 2.45 is points (2.45%): overlapping ranges, opposite meanings.
 *  Prefer **explicit producer declaration**; only undeclared columns fall back to resolvePercentScale inference. */
export type ChartPercentScale = "fraction" | "points";

export interface NumericDisplay {
  decimal_places: number;
  decimal_mode: "max" | "fixed";
}

export interface ChartColumn {
  key: string;
  header: string;
  type: ChartColumnType;
  /** Declared axis role; omitted = positional order. Producer today: backend stacked bars over a two-level hierarchy
   *  (`x` = child on the category axis, `color` = parent as series). Read through `roleOrderedDims` only. */
  role?: ChartColumnRole;
  format?: ChartColumnFormat;
  unit?: string;
  currency_code?: string;
  display?: NumericDisplay;
  /** Only declared percentage shape or canonical percent input establishes scaling. */
  percent_scale?: ChartPercentScale;
  percent_shape_unknown?: boolean;
  source_header?: string;
  /** Name for this column on the **value axis**; defaults to `header`.
   *  Only pivot-produced series columns set it: legend shows slice names (`外套`/`上装`),
   *  while value axis still measures original metric (`营业额`). This field separates the names.
   *  ⚠ `source_header` cannot substitute: it maps translated → original name,
   *  whereas value axis should show the **translated** name; mixing them undoes i18n. */
  measure_header?: string;
  /** Sprint chat-v4-streaming Phase 4E — dual_axis per-column axis assignment.
   *  0 = left axis (default), 1 = right axis. Only populated when ChartSpec.kind
   *  === "dual_axis"; other kinds ignore this field. Older canvas artifacts
   *  predating Phase 4E lack this field — buildDualAxis falls back to first-num
   *  bar / rest-num line by column order for those. */
  y_axis_id?: 0 | 1;
}

export interface ChartSpec {
  kind: ChartKind;
  title: string;
  subtitle?: string;

  rows?: Array<Record<string, string | number | null>>;
  data_artifact_id?: string;
  row_count: number;

  columns: ChartColumn[];
  /** Static ExportIR leaf only; authoring and persisted plans own shared scales at the root. */
  _export_axis_domains?: import("./shared-scales").AxisDomains | null;
  /** Derived frame leaves only; authoring/persistence rejects these export fields. */
  _export_frame_entities?: { key: string; values: Array<string | number> };
  _export_frame_scope?: { time_key: string; key: string | number };
  _export_analytical?: import("./analytical").AnalyticalView;
  _export_table_trend?: import("./table-trends").TableTrend;
  _export_annotations?: import("./annotations").ObservationAnnotation[];

  limit_rows?: number;
  top_n?: number;
  /** Series truncation counts (backend `_cap_time_series`). Distinct from `top_n` (first N **rows** rendered):
   *  truncating 59 provinces to 15 lines reduces rows 708→180; `top_n` would say
   *  "仅显示前 180 项", confusing series counts with rows. Both can occur simultaneously, each with its own field. */
  series_shown?: number;
  series_total?: number;
  /** Time-axis LTTB downsampling counts (backend `_downsample_time_axis`), independent of `series_*` and `top_n`:
   *  a chart can truncate series and downsample simultaneously; merging any two counts makes false claims. */
  points_shown?: number;
  points_total?: number;
  sort_by?: string;
  secondary_axis?: boolean;
  show_legend?: boolean;
  show_data_labels?: boolean;
  percent_stack?: boolean;
  fallback_iframe_url?: string;
  fallback_kind?: ChartKind;

  _infer_reason?: string;

  /** Batch 2 §3.2: marker set during `applyView` derivation — this view **actually** folded its tail (Top-N groups tail
   *  into other bucket). Row counts cannot prove it: applyView outputs `topN + 1` rows; Top-5 on six rows
   *  has six input/output rows, so count comparison misses it (review R6-4). Only applyView writes; others read. */
  _view_folded?: boolean;

  /** Set only by `fitHbarRowsForExport`: this export draws the `shown` of `total` bars (categories for long-format
   *  data, rows otherwise) with the largest absolute value of `measure` (header), not the first rows. The truncation
   *  subcaption reports exactly these counts. Never persisted. */
  _export_top?: { measure: string; shown: number; total: number };

  /** P3.6: chart top source row (opens P3.3 popover style). Backend spec.source_meta supports #66 followup */
  source_meta?: import("../canvas-pm-adapter/citation-types").SourceRef;
  /** Viewer-projected native evidence. Unknown expressions remain opaque and cannot be edited. */
  presentation_plan?: import("./presentation-plan").EvidencePlanV1 | { version?: unknown; availability: "unavailable" };

  /** Sprint ι (PRD §4.C.2): true if backend auto-emitted via 5-AND-gate
   *  (LLM forgot canvas.add_chart hint). Frontend renders an i18n subcaption
   *  "由 sandbox 数据自动生成" / "Auto-generated from sandbox data". */
  auto_charted?: boolean;
  /** Bounded views of one dataset, compiled by the existing add_chart tool. */
  views?: Array<{ id: string; kind: "chart"; spec: ChartSpec }>;
  layout?: ChartLayout;
  derived_columns?: Array<{
    key: string;
    header: string;
    expression: string;
    inputs: Record<string, string>;
    unit: string;
    percent_scale?: ChartPercentScale;
  }>;
  calculation_missing?: Record<string, number>;
}

export type ChartLayout =
  { view: string } | { type: "stack" | "split" | "grid2"; children: ChartLayout[] };
