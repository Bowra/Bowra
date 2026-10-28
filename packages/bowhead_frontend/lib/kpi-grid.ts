/**
 * Column counts for a report KPI row so its rows balance (116 dogfood 1.5.25: five KPIs rendered 4 + 1, the fifth
 * alone on a second row).
 *
 * `kpiColumns` is the rule: as many KPIs as fit the measured row width stay on one row; otherwise they spread over
 * the fewest rows that width allows, evenly (5 → 3 + 2, 7 → 4 + 3, 8 → 4 + 4). The minimum column width and gap
 * mirror styles/report-canvas.css. `kpiMaxColumns` is the width-independent fallback used before the row has been
 * measured (static prerender, first paint): at most four per row, balanced.
 */

/** Minimum KPI column width in px (styles/report-canvas.css `.v2-kpi-row`). */
export const KPI_MIN_COLUMN_PX = 130;
/** Column gap in px (styles/report-canvas.css `.v2-kpi-row`). */
export const KPI_COLUMN_GAP_PX = 32;

/** How many minimum-width columns fit in `width`. */
export function kpiColumnsThatFit(width: number): number {
  return Math.max(1, Math.floor((width + KPI_COLUMN_GAP_PX) / (KPI_MIN_COLUMN_PX + KPI_COLUMN_GAP_PX)));
}

/** Balanced column count for `count` KPIs in a row `width` px wide. */
export function kpiColumns(count: number, width: number): number {
  if (count <= 1) return 1;
  const fit = kpiColumnsThatFit(width);
  if (count <= fit) return count;
  const rows = Math.ceil(count / fit);
  return Math.ceil(count / rows);
}

/** Fallback cap before measurement: up to four per row, balanced (5 → 3, 6 → 3, 7 → 4, 8 → 4, 9 → 3). */
export function kpiMaxColumns(count: number): number {
  if (count <= 4) return Math.max(1, count);
  const rows = Math.ceil(count / 4);
  return Math.ceil(count / rows);
}
