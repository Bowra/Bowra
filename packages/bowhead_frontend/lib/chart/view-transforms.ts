import { isUnsafeIntegerCell, comparePreparedNumbers } from "./format";
import { chartDatasetAvailable } from "./presentation-plan";
import { formatExactSourceNumber } from "../source-content";
/**
 * Batch 3 (chart-table-ux tech-design §3.2) · Pure derived-view functions **shared** by batch 2 inspection
 * and batch 3 tables/mobile.
 *
 * Two hard conventions:
 *  1. `applyView` returns **derived spec without mutating original** (view state lives only in component
 *     state, never database or URL — retain #298 decision against persistence).
 *  2. Top-N's "其余" sums only **additive measures**; ratio/percentage columns get `null`
 *     — averaging five stores' refund rates fabricates a number, forbidden by honesty principle (same
 *     percentage detection as 0-E, relying only on column-name wording).
 */

import { fetchFilePreview } from "../api";
import { compareDateAxisValues } from "./date-order";
import { resolvePercentColumn } from "./format";
import { effectiveHiddenSeries, numericSeriesKeys } from "./series-visibility";
import type { ChartColumn, ChartSpec } from "./types";

export interface ChartViewState {
  sort?: { key: string; dir: "asc" | "desc" };
  topN?: number;
  showLabels?: boolean;
  hiddenSeries?: string[];
}

type Row = Record<string, string | number | null>;

/** **Fallback** category label for rest row.
 *
 *  review R4-6: comments promised callers could override label by locale, but provided no
 *  override entry — sole production caller did not translate, so `__rest__` sentinel rendered as category
 *  label in chart, data-tab table and downloaded CSV. `applyView` now accepts
 *  `restLabel`; callers must supply translation, while this constant is fallback and test anchor only. */
export const REST_BUCKET_KEY = "__rest__";

export interface ApplyViewOptions {
  /** Rest-row category label (caller supplies i18n). */
  restLabel?: string;
  /** Only partial data is available (`ensureFullRows` did not complete).
   *
   *  When true, rest bucket **never sums** (set `null`, UI shows "—"): tail is incomplete,
   *  so subtotal is loaded tail's sum, not sum of all remaining rows,
   *  fabricating a number just like averaging five stores' refund rates.
   *  user 2026-09-02: "算不准还不如不要聚合值算 subtotal". */
  partial?: boolean;
}

// review R3: ratios are not the only nonadditive values — means (per-customer sales/average/median) and ordinal values (rank/
// placing/year) also fabricate numbers when summed. Four corpus tables have "客单价_元".
//
// review R4-10: English and Chinese formerly shared one `/…/i`, thus using **substring** matching
// — `per\s` matched `Copper Tonnage` and `Paper Sales`; likewise `index`/`score`/`rank`.
// This repeats in another regex the collision R0-1 just fixed for `rate` (corporate/separate).
// Chinese lacks word boundaries and still uses substrings; English uses tokens, split the same way as hasPercentWord.
const NON_ADDITIVE_CJK_RE = /客单价|单价|平均|均值|中位|中位数|排名|名次|年份|评分|指数|每/;
// review R5-4: token matching must explicitly include plurals — old substring regex incidentally covered
// `Scores` / `Rankings` / `Averages`; keeping only singular tokens **regresses coverage**: these columns
// become additive, and Top-N rest bucket sums 90 scores into a fabricated total (same reason PERCENT_TOKENS
// deliberately includes both rate / rates).
const NON_ADDITIVE_TOKENS = new Set([
  "avg", "avgs", "average", "averages",
  "median", "medians",
  "rank", "ranks", "ranking", "rankings",
  "score", "scores",
  "index", "indexes", "indices",
  "per",
]);

function isNonAdditiveName(text: string): boolean {
  if (!text) return false;
  if (NON_ADDITIVE_CJK_RE.test(text)) return true;
  return text.toLowerCase().split(/[^a-z]+/).some((token) => NON_ADDITIVE_TOKENS.has(token));
}

function isAdditive(col: ChartColumn): boolean {
  if (col.type !== "numeric") return false;
  if (resolvePercentColumn(col)) return false;
  return !isNonAdditiveName(col.header ?? "") && !isNonAdditiveName(col.key ?? "");
}

function sortRows(rows: Row[], key: string, dir: "asc" | "desc", columnType?: ChartColumn["type"]): Row[] {
  const copy = [...rows];
  copy.sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    const cmp = columnType === "numeric"
      ? comparePreparedNumbers(av, bv)
      : columnType === "date"
        ? compareDateAxisValues(av, bv)
        : String(av).localeCompare(String(bv));
    return dir === "asc" ? cmp : -cmp;
  });
  return copy;
}

/** Top-N **default** ranking — same criterion as backend `_take_top_n` (`bowhead_core/chart/infer.py`):
 *  descending **first numeric column**, nonnumeric/null as -∞ last, ties retain original order
 *  (both sorts are stable). Backend guarded by `test_hbar_restore_takes_top_by_value_not_head_slice`;
 *  each language can guard only its half.
 *
 *  ⚠ Why sort here: report chart is backend-ranked Top-N, but on inspection entry,
 *  `ensureFullRows` replaces it through `/preview` (`files.py` reads CSV row by row with **zero sorting**)
 *  in original file order. Merely doing `rows.slice(0, topN)` here gives first 5 CSV rows
 *  when users open "细看" and select Top-5 — exactly recorded customer complaint "Top-N 结果很奇怪".
 *  Two inconsistent implementations of the same Top-N concept cause this defect. */
function rankForTopN(rows: Row[], key: string): Row[] {
  const score = (r: Row): number | string => {
    const v = r[key];
    return (typeof v === "string" && isUnsafeIntegerCell(v)) || (typeof v === "number" && Number.isFinite(v)) ? v : Number.NEGATIVE_INFINITY;
  };
  return [...rows].sort((a, b) => comparePreparedNumbers(score(b), score(a)));
}

/** Did derived view **actually** remove something from source spec (Top-N reduced rows or hid series)?
 *
 *  review R5-5: criterion must use **actual applyView output**, not whether user clicked Top-N.
 *  applyView truncates only if `rows.length > topN` and a nonnumeric dimension exists — Top-10 on 8 rows,
 *  or legacy spec without dimension column, truncates nothing. Marking reference line
 *  "（基于当前视图）" would incorrectly cast doubt on an honest number.
 *
 *  Extract pure function instead of leaving in component: jsdom cannot test this logic there (chart falls back to table,
 *  never drawing reference lines); component tests would remain green even with fix removed. */
export function isViewTruncated(source: ChartSpec, derived: ChartSpec): boolean {
  // review R6-4: **cannot compare row counts alone**. applyView Top-N outputs `topN + 1` rows
  // (head + rest); Top-5 on 6 rows has 6 rows before and after, so count comparison falsely says no truncation
  // → reference line merely says "均值" although it averages 5 points plus one aggregate bucket.
  // applyView marks folding itself (`_view_folded`); here only read it.
  if (derived._view_folded) return true;
  const srcRows = (source.rows ?? []).length;
  const outRows = (derived.rows ?? []).length;
  const srcCols = (source.columns ?? []).length;
  const outCols = (derived.columns ?? []).length;
  return outRows < srcRows || outCols < srcCols;
}

export function applyView(spec: ChartSpec, view: ChartViewState, opts: ApplyViewOptions = {}): ChartSpec {
  let rows: Row[] = [...(spec.rows ?? [])];
  let columns = spec.columns ?? [];
  let folded = false;

  if (view.sort) {
    const col = columns.find((c) => c.key === view.sort!.key);
    rows = sortRows(rows, view.sort.key, view.sort.dir, col?.type);
  }

  // review R3①: no Top-N without dimension column (legacy spec lacks columns), otherwise rest
  // row has no label at all and renders as blank row.
  const dimColForTop = columns.find((c) => c.type !== "numeric");
  // Ranking column = **first numeric column**, matching backend `_take_top_n` using `num_cols[0]`.
  const primaryNumCol = columns.find((c) => c.type === "numeric");
  if (
    typeof view.topN === "number"
    && view.topN > 0
    && rows.length > view.topN
    && dimColForTop
    && primaryNumCol
  ) {
    const dimCol = dimColForTop;
    // Explicit user sorting takes precedence (`rows` already sorted above); default ranking must not override it;
    // otherwise descending primary numeric column — Top-5 means five largest values, not first five current rows.
    const ranked = view.sort ? rows : rankForTopN(rows, primaryNumCol.key);
    let head: Row[];
    let tail: Row[];
    if (!view.sort && dimCol.type === "date") {
      // Time axis: **select** top N by value, but display in **original temporal order** — rearranging 12 months by magnitude
      // makes line chart cease to be a trend. Backend skips time-axis top-N entirely (`infer.py`:
      // selecting top-N by value removes arbitrary months from time series, making no sense); here user
      // explicitly requested it, so select without changing axis order.
      const picked = new Set(ranked.slice(0, view.topN));
      head = rows.filter((r) => picked.has(r));
      tail = rows.filter((r) => !picked.has(r));
    } else {
      head = ranked.slice(0, view.topN);
      tail = ranked.slice(view.topN);
    }
    const rest: Row = {};
    rest[dimCol.key] = opts.restLabel ?? REST_BUCKET_KEY;
    for (const col of columns) {
      if (col.type !== "numeric") continue;
      // With incomplete source data, rest values are absent for every column — not nonadditive, but **incompletely summed**.
      if (opts.partial || !isAdditive(col)) {
        // Ratio-column rest = null (neither 0 nor mean) — UI shows "—"
        rest[col.key] = null;
        continue;
      }
      if (tail.some((row) => isUnsafeIntegerCell(row[col.key]))) {
        const values = tail.map((row) => row[col.key]).filter((value) => value != null);
        // Integer-only sums are exact. Mixed decimal/unsafe-integer totals are
        // unavailable in this renderer, rather than silently dropping integer rows.
        rest[col.key] = values.every((value) => isUnsafeIntegerCell(value) || (typeof value === "number" && Number.isSafeInteger(value)))
          ? values.reduce<bigint>((total, value) => total + BigInt(value!), 0n).toString() : null;
        continue;
      }
      let sum = 0;
      let seen = false;
      for (const r of tail) {
        const v = r[col.key];
        if (typeof v === "number" && !Number.isNaN(v)) {
          sum += v;
          seen = true;
        }
      }
      rest[col.key] = seen ? sum : null;
    }
    rows = [...head, rest];
    folded = true;
  }

  if (view.hiddenSeries?.length) {
    // Series = numeric columns. Criteria (including review R3③ fail-open keeping at least one column) live in
    // `series-visibility.ts`, **same as chart**: chart keeps columns and marks dataset
    // `hidden`; table removes columns here, using the same state and rule.
    const hidden = effectiveHiddenSeries(numericSeriesKeys(columns), view.hiddenSeries);
    columns = columns.filter((c) => c.type !== "numeric" || !hidden.has(c.key));
  }

  // review R3②: **leave row_count unchanged** — total rows in source data, used as footer
  // total (chart-table.tsx). Setting 11 after Top-10 makes a 98-row table footer say
  // "共 11 行", exactly the misrepresentation batch 3 4.1 eliminates. Derived views change **display**, not total.
  const derived: ChartSpec = { ...spec, rows, columns, _view_folded: folded || undefined };
  if (typeof view.showLabels === "boolean") derived.show_data_labels = view.showLabels;
  return derived;
}

/** **Chart** full-row fetch limit. Inspection uses it — Chart.js draws a geometric primitive per category;
 *  tens of thousands of points/bars cannot render or be read, so 1000 is practical ceiling for this path. */
export const FULL_ROWS_LIMIT = 1000;

/** **Table** full-row fetch limit. Virtual scrolling removes DOM row limits, allowing more than charts.
 *
 *  Why 10000: 2057-chart corpus on 116 has `row_count` **max 9183, p99=235** ⇒ covers
 *  all observations with room. Cost is response size only (~126B/row ⇒ ~1.2MB); server
 *  already reads whole file for accurate `total_rows`, so parsing cost does not change with this limit.
 *
 *  ⚠ **Must equal** backend `/preview` `max_rows` ceiling (`files.py` / `canvas.py` both
 *  `Query(..., le=10_000)`): requesting more yields 422. */
export const TABLE_FULL_ROWS_LIMIT = 10_000;

export interface EnsureFullRowsOptions {
  /** Maximum rows fetched; conservative default is chart limit. Tables explicitly pass `TABLE_FULL_ROWS_LIMIT`. */
  maxRows?: number;
}

export interface EnsureFullRowsResult {
  spec: ChartSpec;
  /** Available rows still fewer than row_count (whether limit exceeded, fetch failed or column mismatch). */
  truncated: boolean;
  /** Were rows actually completed? false = unchanged; caller must not treat it as full expansion. */
  ok: boolean;
}

/**
 * Complete rows truncated by `limit_rows` (97.2% of production charts need no requests; remainder one preview call).
 *
 * Real defect evidence: 37 charts have `rows.length < row_count` (footer /98 while only 50 rows available);
 * table "展开全部" and inspection sorting/Top-N over partial data **misrepresent results**.
 * Align column keys by headers (preview returns original artifact column names; chart spec keys
 * may have been renamed by backend); retain original rows on mismatch rather than expand into wrong columns.
 */
/** Accept only a whole string representing one number; others ("N/A", "1,2,3", empty) → null, rendered —.
 *
 * Accept thousands separators (device CSV contained `"1,234.5"`), but **match whole string**: confirm pattern
 * is a thousands-grouped number before removing commas. Direct `replace(/,/g,"")` would turn `"1,2,3"` into 123. */
function coerceNumericCell(v: string | number | null, column?: ChartSpec["columns"][number]): number | string | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (v === null) return null;
  const s = v.trim();
  if (!s) return null;
  // `15.37%` → 15.37: same rule as backend `_FMT_PERCENT_RE` (`canvas_tools.py`) — chart creation backend
  // converts entire column this way (points mode, no division by 100). Without matching parsing on completion,
  // entire column becomes null (existing charts become blank after completing rows).
  // Accept both signs, matching backend `_FMT_PERCENT_RE` exactly: backend treats `+1.50%` as numeric column;
  // rejecting it here renders whole column — after "展开 / 细看" row completion (incomplete alignment caught in review).
  const pct = /^[+-]?\d+(\.\d+)?\s*%$/.test(s) ? s.replace(/\s*%$/, "") : null;
  const plain = pct ?? (/^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$/.test(s) ? s.replace(/,/g, "") : s);
  if (!/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(plain)) return null;
  if (isUnsafeIntegerCell(plain)) return plain;
  const n = Number(plain);
  if (Number.isFinite(n) && Number.isInteger(n) && !Number.isSafeInteger(n)) {
    // CSV previews may spell exact integers with .0 or exponent notation. Inspect
    // the original digits before Number can round them at the safe-integer boundary.
    const [mantissa, exponent = "0"] = plain.replace(/^[+-]/, "").toLowerCase().split("e");
    const [whole, fraction = ""] = mantissa!.split(".");
    const digits = whole! + fraction;
    const point = whole!.length + Number(exponent);
    if (point > 0 && !/[1-9]/.test(digits.slice(point))) {
      return `${plain.startsWith("-") ? "-" : ""}${digits.slice(0, point).padEnd(point, "0")}`;
    }
  }
  return Number.isFinite(n) ? pct !== null && column?.percent_scale === "fraction" ? Number(formatExactSourceNumber(n, -2).replace(/,/g, "")) : n : null;
}

export async function ensureFullRows(
  spec: ChartSpec,
  opts: EnsureFullRowsOptions = {},
): Promise<EnsureFullRowsResult> {
  const maxRows = opts.maxRows ?? FULL_ROWS_LIMIT;
  const have = (spec.rows ?? []).length;
  const total = spec.row_count ?? have;
  // review R1: every unsuccessful completion must return ok:false, with truncated reflecting reality.
  // Original always returned truncated:false; caller then enabled sorting → fake Top10 from partial data,
  // precisely what this sprint eliminates.
  const unchanged = (): EnsureFullRowsResult => ({ spec, truncated: have < total, ok: false });
  if (have >= total) return { spec, truncated: false, ok: true };
  // Planned linkage is defined on saved retained observations. The existing preview API
  // does not carry a byte seal, so it cannot broaden that population safely.
  if (!spec.data_artifact_id || !chartDatasetAvailable(spec) || spec.presentation_plan !== undefined) return unchanged();
  try {
    const preview = await fetchFilePreview(spec.data_artifact_id, {
      mode: "rows",
      maxRows,
    });
    const raw = preview.rows;
    if (!Array.isArray(raw) || raw.length === 0) return unchanged();
    const previewCols = preview.columns ?? Object.keys(raw[0] ?? {});
    // spec key → preview column name (header match first, then same-name key).
    // review R4-13: formerly keyed inversely by **preview column name**; two spec columns resolving to one
    // preview column (duplicate headers or A.header === B.key — neither rare in LLM-generated specs)
    // collapsed into one entry; `size < specCols.length` always held, so expansion permanently returned
    // "加载完整数据失败" even though every column matched. Key by spec key, one entry per
    // spec column, making size the actual match count.
    const previewColBySpecKey = new Map<string, string>();
    for (const col of spec.columns ?? []) {
      const hit = previewCols.find((p) => p === col.header) ?? previewCols.find((p) => p === col.key);
      if (hit) previewColBySpecKey.set(col.key, hit);
    }
    // review R1: **all** columns must match before replacement. Original needed only one; partial matching
    // rendered unmapped columns —, worse than no expansion (users cannot distinguish absent data from null values).
    // File comments already promised retaining original rows on mismatch, but implementation did so only with zero matches.
    const specCols = spec.columns ?? [];
    if (specCols.length === 0 || previewColBySpecKey.size < specCols.length) return unchanged();
    // Expanding CSV-backed rows must preserve numeric cells as well as category cells.
    // Inline rows hold genuine numbers (backend spec), while preview parses CSV,
    // returning strings (`"88494000.0"`). Old `typeof v === "string"` guard passed them unchanged,
    // but chart-table numeric columns accept only `typeof v === "number"` → whole column null → all —.
    //
    // **Same column must have consistent types across both arrival paths**. Normalize here (only entry
    // point into spec), not rendering layer: sorting / totals / percentages / export also require numbers;
    // fixing rendering alone leaves others wrong (expansion enables header sorting, but strings sort lexicographically).
    const numericKeys = new Set(specCols.filter((c) => c.type === "numeric").map((c) => c.key));
    for (const column of specCols.filter((column) => column.type === "numeric")) {
      const name = previewColBySpecKey.get(column.key)!;
      const hasPercentText = raw.some((row) => typeof row[name] === "string" && /%\s*$/.test(String(row[name])));
      if (hasPercentText && !column.percent_scale && column.unit !== "%") return unchanged();
    }
    const rows: Row[] = raw.map((r) => {
      const out: Row = {};
      for (const [specKey, previewCol] of previewColBySpecKey) {
        const v = (r as Record<string, unknown>)[previewCol];
        const cell = typeof v === "number" || typeof v === "string" || v === null ? (v as string | number | null) : null;
        out[specKey] = numericKeys.has(specKey) ? coerceNumericCell(cell, specCols.find((column) => column.key === specKey)) : cell;
      }
      return out;
    });
    // Fewer fetched rows than available → retain original (replacement would arbitrarily lose rows)
    if (rows.length < have) return unchanged();
    // feedback-triage-2026-08-26 (116 recorded footer "显示 1–1000 / 100 行"): formerly directly
    // set `row_count: total`, where `total` was old **pre-expansion** value. Device spec.row_count=100,
    // fetched 1000 rows (exactly FULL_ROWS_LIMIT), so footer said "1–1000 / 100 行" —
    // displayed count exceeded total; worse, `truncated: 1000 < 100` evaluated false,
    // so "展开全部" **falsely claimed full data**, precisely what comments throughout this file guard against.
    //
    // Authoritative total by trust: preview-reported `total_rows` > actual fetched row count > old row_count.
    // Use max instead of direct replacement: old value may be larger and accurate (upstream used genuine total,
    // merely absent in this preview); reducing total then also misrepresents it, in opposite direction.
    // Likewise only broaden truncation criterion: honor source-reported truncated, never override it via row counts.
    const reportedTotal = typeof preview.total_rows === "number" && Number.isFinite(preview.total_rows) ? preview.total_rows : 0;
    const fullTotal = Math.max(total, rows.length, reportedTotal);
    return {
      spec: { ...spec, rows, row_count: fullTotal },
      truncated: preview.truncated === true || rows.length < fullTotal,
      ok: true,
    };
  } catch {
    // Cannot fetch (shared report 403 / network) → retain current data; caller treats it as unexpanded
    return unchanged();
  }
}
