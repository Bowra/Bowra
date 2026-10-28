import { chartDatasetAvailable, evidencePlanForViewer } from "./presentation-plan";
/**
 * Complete rows for existing charts: specs containing partial rows without declaring truncation fetch missing rows before drawing.
 *
 * ## Why this exists
 *
 * Backend formerly inlined only first 50 rows for both charts and tables (**table** payload limit), while time-axis charts skip
 * top-N and thus have no `top_n`. A synthetic chart with 12 months × 6 brands has 72 rows;
 * drawing only the first 50 silently omits the final months;
 * same chart's inspection layer used `ensureFullRows` and drew all 12 months.
 *
 * Backend now sends every row required for charts (`chart/infer.py` `_CHART_ROWS_MAX`), so **new charts** do not need
 * this. But specs are materialized into database at creation; existing charts do not update themselves — cover those only.
 *
 * ## When to complete, and whether to accept results
 *
 * Complete: available rows < `row_count`, with no `top_n` (not **intentionally** truncated — when set, backend intentionally
 * draws first 30 bars; fetching full rows would instead draw hundreds).
 *
 * Accept: **file may not contain this chart's data**. `add_chart` `limit` truncates before chart creation (row_count is truncated,
 * file still has all rows), and `sort_by` reorders rows — replacing with entire file can produce a different chart. Replace only if
 * file total rows == `row_count`, and its first N rows match inline rows cell by cell
 * (`sameChartRows`). Otherwise retain original, honestly explained by "仅显示前 N 项" below chart.
 */
import { useEffect, useMemo, useState } from "react";

import type { ChartSpec } from "./types";
import { ensureFullRows, type EnsureFullRowsResult } from "./view-transforms";

/** Tables have expand-all; KPIs have one row — neither draws rows as a chart, so do not complete. */
export function isDrawnChartKind(kind: ChartSpec["kind"]): boolean {
  return kind !== "table" && kind !== "kpi";
}

/** Is this an existing chart containing partial rows without declaring truncation? */
export function needsRowCompletion(spec: ChartSpec): boolean {
  if (!chartDatasetAvailable(spec)) return false;
  if (evidencePlanForViewer(spec.presentation_plan)?.interactions?.length) return false;
  const have = (spec.rows ?? []).length;
  const total = spec.row_count ?? have;
  // ⚠ When `series_shown` exists, these rows were **intentionally selected by backend** (top 15 of 59 provinces by totals,
  // 708 rows → 180). Completing whenever rows appear insufficient would restore all 59 provinces,
  // bypassing backend readability gate entirely, reverting chart to colored ribbons with legend filling canvas.
  // Apply criterion **before fetching**: `sameChartRows` cannot block this (when retained rows happen to be first rows,
  // cell-by-cell checks pass), so relying on it relies on coincidence.
  // ⚠ This governs **main chart** only. Inspection layer (`chart-inspect.tsx`) calls `ensureFullRows` directly,
  // intentionally exempt: user opens that outlet explicitly; release notes promise "要看全部分组就去那里".
  if (spec.series_shown != null || spec.points_shown != null) return false;
  return have < total && Boolean(spec.data_artifact_id) && spec.top_n == null;
}

function sameCell(a: unknown, b: unknown, numeric: boolean): boolean {
  if (!numeric) return String(a ?? "") === String(b ?? "");
  const x = typeof a === "number" && Number.isFinite(a) ? a : null;
  const y = typeof b === "number" && Number.isFinite(b) ? b : null;
  if (x === null || y === null) return x === y;
  return Math.abs(x - y) <= 1e-9 * Math.max(1, Math.abs(x), Math.abs(y));
}

/** Does fetched `full` contain this chart `spec`'s own rows? See acceptance paragraph in file header. */
export function sameChartRows(spec: ChartSpec, full: ChartSpec): boolean {
  const inline = spec.rows ?? [];
  const rows = full.rows ?? [];
  if (full.row_count !== spec.row_count || rows.length < inline.length) return false;
  const cols = spec.columns ?? [];
  return inline.every((row, i) =>
    cols.every((c) => sameCell(row[c.key], rows[i]![c.key], c.type === "numeric")),
  );
}

/** Request identity: same file, column mapping, and row count ⇒ same `ensureFullRows` result.
 *  Include column signature: one artifact may produce amount or quantity charts. */
function completionKey(spec: ChartSpec): string {
  const cols = (spec.columns ?? []).map((c) => [c.key, c.header, c.type, c.unit ?? ""].join("")).join("");
  return [spec.data_artifact_id, spec.row_count, (spec.rows ?? []).length, cols].join("");
}

// Shared across mounts: same chart in chat/report/insight card, or remounts from streaming updates, fetches only once.
// Do not cache failures (next mount can retry); bounded cache evicts oldest first.
// `resolved` stores received results, directly usable on **first frame** after remount — otherwise asynchronous effect
// set first draws partial chart with "仅显示前 N 项", then full data next frame with animation replay.
const CACHE_MAX = 64;
const cache = new Map<string, Promise<EnsureFullRowsResult>>();
const resolved = new Map<string, EnsureFullRowsResult>();

function remember<V>(map: Map<string, V>, key: string, value: V): void {
  map.set(key, value);
  if (map.size > CACHE_MAX) map.delete(map.keys().next().value!);
}

function loadOnce(key: string, spec: ChartSpec): Promise<EnsureFullRowsResult> {
  // Both maps evict independently; `cache` may drop while `resolved` remains: check received results first to avoid refetching same data.
  const done = resolved.get(key);
  if (done) return Promise.resolve(done);
  const hit = cache.get(key);
  if (hit) return hit;
  const pending = ensureFullRows(spec);
  remember(cache, key, pending);
  void pending.then((result) => {
    if (result.ok) remember(resolved, key, result);
    else cache.delete(key);
  });
  return pending;
}

/** Test-only: clear shared mount cache between cases. */
export function resetChartRowCompletionCache(): void {
  cache.clear();
  resolved.clear();
}

/**
 * `enabled=false` returns unchanged (exports do not fetch live data; inspection completes independently; tables have expand-all).
 * Keep object identity for the same `spec` — downstream normalize / build / inspection effects compare references.
 */
export function useCompleteChartRows(spec: ChartSpec, enabled: boolean): ChartSpec {
  const key = enabled && needsRowCompletion(spec) ? completionKey(spec) : null;
  const [loaded, setLoaded] = useState<{ key: string; result: EnsureFullRowsResult } | null>(null);

  useEffect(() => {
    if (!key) return;
    let canceled = false;
    void loadOnce(key, spec).then((result) => {
      if (!canceled) setLoaded({ key, result });
    });
    return () => {
      canceled = true;
    };
    // Follow request identity only: other spec fields do not require refetch; replacements always use current spec.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // Prefer successful result from this mount; otherwise check successful results elsewhere (or previous mount) —
  // if own fetch failed but another later succeeded, use successful result.
  const own = key && loaded?.key === key ? loaded.result : undefined;
  const result = key ? (own?.ok ? own : (resolved.get(key) ?? own)) : undefined;
  return useMemo(() => {
    if (!result?.ok) return spec;
    const full = result.spec;
    if (!sameChartRows(spec, full)) return spec;
    return { ...spec, rows: full.rows, row_count: full.row_count };
  }, [spec, result]);
}
