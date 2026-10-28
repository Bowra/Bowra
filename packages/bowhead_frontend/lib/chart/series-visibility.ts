/**
 * **Single** source of truth for series visibility, shared by canvas legends and inspection chips.
 *
 * ## Why this file exists
 *
 * One action previously had two mutually unaware implementations:
 *  1. Canvas used Chart.js legends with no repository onClick override, running default library
 *     handlers that changed meta.hidden on the Chart.js **instance**;
 *  2. Inspection rc-inspect-series chips wrote view.hiddenSeries; applyView
 *     **removed numeric columns from spec**.
 * Different layers/states, unsynchronized, with chart blocks embedded in inspection panels, so two
 * series-click behaviors coexisted on one panel. Historical assessment (chart-table-ux prd.md:299): Chart.js
 * legends are clickable by default (not disabled here), but **were never designed**.
 *
 * ## Converged shape
 *
 * One state: hiddenSeries (a set of **numeric column keys**), held in React
 * (owned by ChartBlock or controlled from inspection), **never persisted or included in URLs**, per #298,
 * retaining the criterion at the start of view-transforms.ts.
 * One criterion: effectiveHiddenSeries (including fail-open).
 * Two application points address their respective rendering media but read the same state/criterion:
 *  - Charts: build-config sets dataset hidden (**without removing columns**, avoiding kind fallback);
 *  - Tables: applyView filters numeric columns (tables have no hidden-dataset concept).
 *
 * ⚠ Charts do not reuse column removal: removing columns violates dual_axis/combo/area_stacked
 * shape requirements, triggering canBuildKind automatic kind fallback (specifically fixed in the chart-config-ux
 * batch). Canvas legend clicks currently preserve kind; convergence must not spread this side effect
 * to all seven rendering surfaces.
 */

import type { ChartColumn } from "./types";

/** One series = one numeric column key. */
export type SeriesKey = string;

/** Click to hide ⇄ show: **toggle**, not solo/isolate.
 *  Customer requested fewer clicks to see only one line; that is a later product decision, unchanged here.
 *  Preserve insertion order for unchanged UI/test state restoration. */
export function toggleSeriesKey(hidden: readonly SeriesKey[] | undefined, key: SeriesKey): SeriesKey[] {
  const next = new Set(hidden ?? []);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return [...next];
}

/** Series independently hideable/showable in this chart, taken by **numeric column**. */
export function numericSeriesKeys(columns: readonly ChartColumn[] | undefined): SeriesKey[] {
  return (columns ?? []).filter((c) => c.type === "numeric").map((c) => c.key);
}

/**
 * Effective hidden set, with two rules:
 *  1. Ignore unknown keys (old keys after a spec change must not suppress a column);
 *  2. **Fail-open: keep at least one visible**; when all hidden, expose the first column in order.
 *
 * Rule 2 was decided in view-transforms review R3③ (then hiding all hid nothing,
 * making clicks appear ineffective). After convergence it applies to **both entries**; this is canvas legend's
 * sole alignment with inspection behavior: previously hiding the final series left an empty chart; now retain
 * the final series. Both sides need one rule, choosing the designed and reviewed rule.
 */
export function effectiveHiddenSeries(
  seriesKeys: readonly SeriesKey[],
  hidden: readonly SeriesKey[] | undefined,
): Set<SeriesKey> {
  const known = new Set(seriesKeys);
  const effective = new Set((hidden ?? []).filter((key) => known.has(key)));
  if (seriesKeys.length > 0 && effective.size >= seriesKeys.length) {
    effective.delete(seriesKeys[0]!);
  }
  return effective;
}

/** Source column (_yCol) attached to datasets by each builder, per existing build-config convention. */
interface SeriesDataset {
  _yCol?: { key?: string };
  _seriesKey?: string;
}

/**
 * Can this chart's datasets map one-to-one to numeric-column series? If so, return a key per dataset;
 * otherwise null: callers **must not** alter legend behavior, retaining Chart.js defaults.
 *
 * Three null cases (legends are not grouped by numeric columns; deliberately leave them to the library):
 *  - Pie/doughnut: one dataset without _yCol; legend items are **categories**, not series,
 *    correctly handled by default toggleDataVisibility(index);
 *  - Grouped scatter / stacked-bar pivot: every dataset carries the **same** _yCol
 *    (legend items are group/slice names), so hiding by key would hide many together;
 *  - Single series: hiding it empties the chart; use library defaults (consistent with chips appearing only for >1 numeric columns).
 */
export function seriesKeysOfDatasets(datasets: readonly unknown[] | undefined): SeriesKey[] | null {
  const list = datasets ?? [];
  if (list.length < 2) return null;
  const keys: SeriesKey[] = [];
  for (const ds of list) {
    const key = (ds as SeriesDataset | null)?._seriesKey ?? (ds as SeriesDataset | null)?._yCol?.key;
    if (typeof key !== "string" || !key) return null;
    keys.push(key);
  }
  return new Set(keys).size === keys.length ? keys : null;
}
