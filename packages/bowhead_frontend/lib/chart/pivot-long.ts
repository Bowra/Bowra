/**
 * Long (tidy / long format) → wide table: expand one categorical dimension into multiple series (chart-config-ux ⑪).
 *
 * ## Why this exists
 *
 * Data retrieval often returns long tables: `每月 × 种类大类 × 营业额`, one row per (month, category) combination.
 * Chart.js rendering pipeline assumes **one column = one series**, drawing long tables as
 * one x point per row: real-device `Top 10品类大类月度趋势` on 116 (70 rows = 10 categories × 7 months)
 * had this x-axis —
 *
 *     2026年1月 2026年1月 2026年1月 … (10 repetitions) 2026年2月 2026年2月 …
 *
 * Legend had one "营业额 / 元" line. Title promised monthly trends for ten categories, but output was one
 * sawtooth line concatenating ten categories, **completely unreadable**. `2026 vs 2025 月度营业额趋势对比`
 * comparison titles were especially misleading — two lines to compare connected into one.
 * Measurements across 1877 charts on 116 found **29 cases** (1.5%), typical titles "各省 GMV 月度趋势"
 * (50 rows = 5 provinces × 10 months) and "省×品类营业额 Top 20". Every one wholly unreadable.
 *
 * ## Boundaries (why only these kinds)
 *
 * Applies only to multiple series of one measure on one x-axis: `line` / `area_stacked` /
 * `bar` / `hbar` / `stacked_bar`.
 *
 * Semantics, not convenience:
 *  - `pie` — after expanding ten series, which should the pie draw?
 *  - `scatter` / `dual_axis` / `combo` need **two different measures**,
 *    while pivot produces N slices of one measure, fundamentally different.
 *
 * Another benefit: these kinds require only ≥1 numeric column before/after pivot, leaving
 * `kind-shape.ts` buildability criterion **unaffected** — it still sees pre-pivot shape,
 * without learning this transformation. (Pivoting `dual_axis` could make unbuildable become buildable,
 * requiring both criteria to synchronize — precisely the coupling this sprint keeps dismantling.)
 *
 * ## Two deliberate differences from existing bar pivot
 *
 * `buildBar` already pivots (stacked bar + two category dimensions), but:
 *  - Accepts `categorical | geo`, **not date**, so cannot handle time-series long tables;
 *  - Fills missing cells with `0`.
 *
 * Here missing cells become **`null`**: a category has no data in a month; drawing 0 fabricates a drop-to-zero trough.
 * Chart.js breaks lines/leaves gaps for null, reflecting reality. Leave `buildBar` unchanged — existing stacked-bar behavior;
 * changing missing-cell semantics affects shipped charts and belongs to another task.
 */
import type { ChartColumn, ChartSpec } from "./types";
import { CHART_STYLE_PRESENTATION_POLICY } from "./chart-style-registry";
import { roleOrderedDims } from "./stack-dims";
import { formatDatePattern } from "./format";

/** Pivotable chart kinds: one x-axis + multiple series of same measure. */
export const PIVOTABLE_KINDS: ReadonlySet<ChartSpec["kind"]> = new Set(
  CHART_STYLE_PRESENTATION_POLICY.normalization.pivotableKinds,
);

const DIM_TYPES = new Set<ChartColumn["type"]>(["date", "categorical", "geo"]);

export interface PivotResult {
  spec: ChartSpec;
  /** Dimension column expanded into series (for subtitles/captions). */
  seriesCol: ChartColumn;
  /** Number of expanded series. */
  seriesCount: number;
  /** Were multiple rows summed in one cell (source has dimensions not expanded)? */
  aggregated: boolean;
  origins?: CellOrigins;
}

type Row = NonNullable<ChartSpec["rows"]>[number];
export type CellOrigins = ReadonlyMap<Row, ReadonlyMap<string, Array<{ row: Row; measureKey: string }>>>;

/** Required intersection between dimensions for a long table. See corpus derivation in `detectLong` ②. */
const MIN_FILL_RATIO = CHART_STYLE_PRESENTATION_POLICY.normalization.minimumLongTableFillRatio;

/**
 * Detect long table via four conditions, each blocking a real corpus class:
 *
 *  1. **Exactly 1 numeric column** — with ≥2, existing one-column/one-series convention already applies; leave unchanged.
 *  2. **Exactly 2 dimension columns** — ≥3 leaves dimensions after pivot, making x-axis ambiguous again.
 *  3. **At least 2 x values** — a single value provides no meaningful axis (see ①).
 *  4. **Dimensions genuinely intersect** (fill ratio ≥ `MIN_FILL_RATIO`, see ②).
 */
/** These kinds choose x-axis **by column order**, without date priority — `buildBar` `orderedX`. */
const X_BY_COLUMN_ORDER: ReadonlySet<ChartSpec["kind"]> = new Set(["bar", "hbar", "stacked_bar"]);

/** Only suppress an implicit split when both columns prove the same distinct calendar dates. */
function duplicateDateDimension(rows: readonly Row[], xCol: ChartColumn, seriesCol: ChartColumn): boolean {
  if (xCol.type !== "date" || seriesCol.type !== "categorical" || seriesCol.role === "color") return false;
  const seen = new Set<string>();
  for (const row of rows) {
    const x = row[xCol.key], group = row[seriesCol.key];
    if (typeof x !== "string" || typeof group !== "string"
      || !/^(?:\d{8}|\d{4}-\d{2}-\d{2})$/.test(x)
      || !/^(?:\d{8}|\d{4}-\d{2}-\d{2})$/.test(group)) return false;
    const day = formatDatePattern(x, "yyyyMMdd");
    if (!day || day !== formatDatePattern(group, "yyyyMMdd") || seen.has(day)) return false;
    seen.add(day);
  }
  return seen.size >= 2;
}

function detectLong(spec: ChartSpec): { xCol: ChartColumn; seriesCol: ChartColumn } | null {
  const cols = spec.columns ?? [];
  const rows = spec.rows ?? [];
  // Declared axis roles first (see `stack-dims.ts`); without them, column order as before.
  const dims = roleOrderedDims(cols.filter((c) => DIM_TYPES.has(c.type)));
  const nums = cols.filter((c) => c.type === "numeric");
  if (nums.length !== 1 || dims.length !== 2 || rows.length < 2) return null;

  // ⚠ x selection **must follow target builder**; the two builders differ:
  //   - `buildLine`: date first, otherwise categorical/geo;
  //   - `buildBar`: first date/categorical/geo **in column order** — deliberately fixed in `review[2]`
  //     (formerly unconditional date priority changed `[分行, 月份, 销量]` x-axis from "分行"
  //     silently to "月份").
  // My first version unified date-first and immediately failed existing case. Pivot should change only
  // treatment of other dimension (ignored → series), **never which column is x-axis**.
  const xCol = X_BY_COLUMN_ORDER.has(spec.kind)
    ? dims[0]!
    : (dims.find((c) => c.type === "date") ?? dims[0]!);
  const seriesCol = dims.find((c) => c.key !== xCol.key)!;

  if ((spec.kind === "line" || spec.kind === "area_stacked") && duplicateDateDimension(rows, xCol, seriesCol)) return null;

  const xVals = new Set<string>();
  const sVals = new Set<string>();
  for (const r of rows) {
    xVals.add(String(r[xCol.key] ?? ""));
    sVals.add(String(r[seriesCol.key] ?? ""));
  }

  // ① Only one x value ⇒ no meaningful axis. Real-device `示例药品甲 Top 20 医院` on 116 (20 rows, identical drug
  //    names throughout) would become 1 x point × 20 series, destroying Top20 horizontal bars.
  if (xVals.size < 2) return null;

  // ② Dimensions must **genuinely intersect**. Real long table is mostly filled cross-product: rows ≈ x count × series count.
  //    Low fill means second dimension is not shared series axis, merely a per-row label; expansion creates
  //    diagonal-like, mostly empty matrix, harder to read than original.
  //    116 corpus measurements separate the classes clearly:
  //      Real long tables: 0.36 / 0.43 / 0.59 / 0.67 / 1.00 (monthly provincial GMV/category trends…)
  //      False long tables: 0.13 / 0.14 / 0.20 (`门店面积与营业额关系`: 39 rows become 39 series)
  //    1/3 falls in this gap, rather than being arbitrary.
  const fill = rows.length / (xVals.size * sVals.size);
  if (fill < MIN_FILL_RATIO) return null;

  return { xCol, seriesCol };
}

/**
 * Expand long table into wide. Return `null` for non-long tables (or nonpivotable kinds), leaving callers unchanged.
 *
 * ⚠ **Do not cap series count.** Ten categories become ten lines, exceeding six-color palette and repeating colors —
 * but excess series are a **separate** issue (backend `_MAX_SERIES` owns it and records
 * reason in spec on truncation). Silently cutting four categories here gives users four missing lines
 * without explanation — exactly the silent truncation this sprint eliminates.
 */
export function pivotLongSpec(spec: ChartSpec, trackOrigins = false, scopeSource = spec): PivotResult | null {
  if (!PIVOTABLE_KINDS.has(spec.kind)) return null;
  const hit = detectLong(scopeSource);
  if (!hit) return null;
  const { xCol, seriesCol } = hit;
  const numCol = (spec.columns ?? []).find((c) => c.type === "numeric")!;
  const rows = spec.rows ?? [];

  const xVals: string[] = [];
  const sVals: string[] = [...new Set((scopeSource.rows ?? []).map((row) => String(row[seriesCol.key] ?? "")))];
  // Use Map<x, Map<series, number>>, never concatenated keys: separators within x/series values
  // cause collisions (`punctuation doubles as data separator` family).
  const cells = new Map<string, Map<string, number>>();
  const sources = new Map<string, Map<string, Array<{ row: Row; measureKey: string }>>>();
  let aggregated = false;

  for (const r of rows) {
    const x = String(r[xCol.key] ?? "");
    const s = String(r[seriesCol.key] ?? "");
    if (!cells.has(x)) {
      cells.set(x, new Map());
      xVals.push(x);
    }
    if (!sVals.includes(s)) sVals.push(s);
    if (trackOrigins) {
      if (!sources.has(x)) sources.set(x, new Map());
      const origin = sources.get(x)!;
      origin.set(s, [...(origin.get(s) ?? []), { row: r, measureKey: numCol.key }]);
    }
    const raw = r[numCol.key];
    if (raw === null || raw === undefined || (typeof raw === "string" && !raw.trim())) continue;
    const v = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(v)) continue;
    const bucket = cells.get(x)!;
    if (bucket.has(s)) aggregated = true; // Multiple rows per cell ⇒ source contains unexpanded dimensions
    bucket.set(s, (bucket.get(s) ?? 0) + v);
  }
  if (sVals.length < 2) return null; // Only one series; pivot is pointless

  // Series columns inherit numeric-column units/format/percentage mode — each expanded column remains same measure.
  const seriesColumns: ChartColumn[] = sVals.map((s) => ({
    ...numCol,
    key: `__pivot__${s}`,
    header: s,
    // Column name changed; percentage mode partly relies on names (when `format`/`unit` absent).
    // Preserve original column name for `resolvePercentColumn` lexical detection.
    source_header: numCol.source_header ?? numCol.header,
    // Value axis still represents original measure. Without this, y-axis title becomes first category name
    // — caught locally in real browser: `各品类月度趋势` y-axis said "外套（万）".
    measure_header: numCol.header,
  }));

  const wideRows = xVals.map((x) => {
    const bucket = cells.get(x)!;
    const row: Record<string, string | number | null> = { [xCol.key]: x };
    for (let i = 0; i < sVals.length; i += 1) {
      // Missing cell = null, not 0: no data for that category/month, not zero sales.
      row[seriesColumns[i]!.key] = bucket.has(sVals[i]!) ? bucket.get(sVals[i]!)! : null;
    }
    return row;
  });
  const origins = trackOrigins ? new Map(wideRows.map((row) => [row, new Map(seriesColumns.map((column, index) =>
    [column.key, sources.get(String(row[xCol.key]))?.get(sVals[index]!) ?? []]))])) : undefined;

  return {
    spec: { ...spec, columns: [xCol, ...seriesColumns], rows: wideRows, row_count: wideRows.length },
    seriesCol,
    seriesCount: sVals.length,
    aggregated,
    ...(origins ? { origins } : {}),
  };
}


// ── ⑯ Transpose back: time became series, categories became x-axis ────────────────────────────

/** Does column name resemble a time point (`1月` / `2026年3月` / `2026-01` / `Q2`)? */
const TIMEY_HEADER = new RegExp(CHART_STYLE_PRESENTATION_POLICY.normalization.timeHeaderPattern);

/** Only line family implies connection = continuity; bars remain readable either way. */
const TRANSPOSABLE_KINDS: ReadonlySet<ChartSpec["kind"]> = new Set(
  CHART_STYLE_PRESENTATION_POLICY.normalization.transposableKinds,
);

/** Minimum time columns for a trend — connecting two points alone is not a trend,
 *  and coincidental month-like column names are more likely with two columns. */
const MIN_TIME_COLUMNS = CHART_STYLE_PRESENTATION_POLICY.normalization.minimumTimeColumns;

/**
 * Transpose wide table: categories × month columns → months × category series.
 *
 * ## Symptoms
 *
 * Model pivots in sandbox to one category per row, one month per column, then passes months as `series`.
 * One-column/one-series convention renders it **transposed**: real device on 116,
 * `2026年各品类大类月度营业额趋势` x-axis is "女装 / 鞋包皮件 / 金银珠宝 …",
 * with nine lines for January–September. User/title requests each category's own trend.
 *
 * Lines on categorical axis imply false connections — `infer.py` comments already explain
 * that such lines imply nonexistent continuity. Correct orientation here.
 *
 * ## Relationship to `pivotLongSpec`
 *
 * Two directions of same operation: that expands one-row-per-(month,category) long tables into multiple series,
 * this turns incorrectly oriented wide tables back. Keep same file and seam.
 *
 * ## Boundaries
 *
 * Deliberately narrow criterion (3 matches in 116 corpus): line family + no date column + **all** numeric headers time-like
 * + ≥3 time columns + exactly 1 dimension. Any non-time numeric header (`合计` / `同比`)
 * indicates this is not a pure time-wide table; leave it unchanged.
 */
export function transposeTimeWideSpec(spec: ChartSpec, trackOrigins = false, previousOrigins?: CellOrigins): PivotResult | null {
  if (!TRANSPOSABLE_KINDS.has(spec.kind)) return null;
  const cols = spec.columns ?? [];
  const rows = spec.rows ?? [];
  if (rows.length < 2) return null;
  if (cols.some((c) => c.type === "date")) return null; // Existing real time axis excludes this path

  const dims = cols.filter((c) => c.type === "categorical" || c.type === "geo");
  const nums = cols.filter((c) => c.type === "numeric");
  if (dims.length !== 1 || nums.length < MIN_TIME_COLUMNS) return null;
  if (!nums.every((c) => TIMEY_HEADER.test(String(c.header ?? c.key)))) return null;

  const dim = dims[0]!;
  const xKey = dim.key;
  // New x-axis = original time-column names; new series = each original row.
  //
  // ⚠ Both names **lack genuine sources**, caught during self-review (invisible in code, revealed by rendering):
  //  - Measure name: source headers are months (`1月`/`2月`); **measure name never appeared**.
  //    First version used `nums[0].header` as `measure_header` ⇒ y-axis title "1月（元）".
  //    Omit rather than label with month/category; set empty string,
  //    letting `axisTitleOf` fall back to unit only.
  //    ⚠ **Chained transforms do have genuine measure names**: `pivotLongSpec` expands `品类 × 月份 × 营业额`
  //    long table into month columns (setting `measure_header='营业额'`); this function transposes back.
  //    Second version unconditionally blanked it, shrinking y-axis title "营业额（元）" to "元" —
  //    regression introduced by my own fix. Leave empty only when **upstream truly supplied nothing**.
  //  - x-axis name: column holds months, but `dim.header` is "品类". First version reused it, falsely naming column.
  //    No genuine name available here either; leave empty.
  const seriesCols: ChartColumn[] = rows.map((r, i) => ({
    ...nums[0]!,
    key: `__t__${i}`,
    header: String(r[xKey] ?? `#${i + 1}`),
    source_header: nums[0]!.source_header ?? nums[0]!.header,
    measure_header: nums[0]!.measure_header ?? "",
  }));
  const timeCol: ChartColumn = { key: xKey, header: "", type: "date" };
  const wideRows = nums.map((n) => {
    const row: Record<string, string | number | null> = { [xKey]: String(n.header ?? n.key) };
    rows.forEach((r, i) => {
      const raw = r[n.key];
      const v = raw === null || raw === undefined || (typeof raw === "string" && !raw.trim()) ? NaN : typeof raw === "number" ? raw : Number(raw);
      row[seriesCols[i]!.key] = Number.isFinite(v) ? v : null;
    });
    return row;
  });
  const origins = trackOrigins ? new Map(wideRows.map((row, timeIndex) => [row, new Map(seriesCols.map((column, index) =>
    [column.key, previousOrigins?.get(rows[index]!)?.get(nums[timeIndex]!.key) ?? [{ row: rows[index]!, measureKey: nums[timeIndex]!.key }]]))])) : undefined;

  return {
    spec: { ...spec, columns: [timeCol, ...seriesCols], rows: wideRows, row_count: wideRows.length },
    seriesCol: dim,
    seriesCount: seriesCols.length,
    aggregated: false,
    ...(origins ? { origins } : {}),
  };
}
