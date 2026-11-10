/**
 * Chart types a declared-axis chart can switch to without re-binding a field.
 *
 * The same rule governs chat edits (`_explicit_plot_kinds` in `bowhead_core/chart/authoring_tools.py`), so the
 * report switcher and `edit_chart` offer the same types. `config/chart_style/explicit_switch_kinds_fixtures.json`
 * runs both implementations against the same cases.
 */
import { normalizeChartForRenderingV1 } from "./normalize-chart-for-rendering";
import type { ChartSpec } from "./types";

type Kind = ChartSpec["kind"];

/** Category-axis kinds that read one binding and its prepared rows the same way (`_EXPLICIT_SWITCH_KINDS`). */
const SWITCH_KINDS: readonly Kind[] = ["line", "bar", "hbar", "area_stacked", "stacked_bar"];

/** Native drawing limits from `bowhead_core/chart/validation.py`; a horizontal bar has no axis-slot limit. */
const MAX_AXIS_SLOTS: Partial<Record<Kind, number>> = { line: 3000, area_stacked: 3000, bar: 1000, stacked_bar: 1000 };
const MAX_DRAWN_SERIES = 100;
const MAX_DRAWN_SHAPE_BYTES = 30_000;

/** The saved rows are the complete plotted result: not truncated, ranked to a top N or sampled. */
function completeRows(spec: ChartSpec): ChartSpec["rows"] | null {
  const rows = spec.rows;
  if (!Array.isArray(rows) || !Number.isInteger(spec.row_count) || spec.row_count !== rows.length || spec.top_n) return null;
  const counts = [["series_shown", "series_total"], ["points_shown", "points_total"]] as const;
  for (const [shownKey, totalKey] of counts) {
    if (spec[shownKey] === undefined && spec[totalKey] === undefined) continue;
    const shown = spec[shownKey], total = spec[totalKey];
    if (!Number.isInteger(shown) || !Number.isInteger(total) || shown! < 0 || shown !== total) return null;
  }
  return rows;
}

function drawable(spec: ChartSpec, kind: Kind): boolean {
  try {
    const { shape } = normalizeChartForRenderingV1({ ...spec, kind });
    const slots = MAX_AXIS_SLOTS[kind];
    return shape.seriesCount <= MAX_DRAWN_SERIES
      && (slots === undefined || shape.categoryCount <= slots)
      && new TextEncoder().encode(JSON.stringify(shape)).length <= MAX_DRAWN_SHAPE_BYTES;
  } catch {
    return false;
  }
}

/** Offered types for a standalone chart including the current one, or `[]` when it keeps its type.
 *  A composed figure's views keep their types, as `edit_chart` changes the type only on the root. */
export function explicitSwitchKinds(spec: ChartSpec): Kind[] {
  const binding = spec.explicit_presentation;
  // Spread copies can carry an undefined key that saved JSON never has.
  if (!binding || binding.mode !== "plot" || spec.views !== undefined || spec.presentation_plan !== undefined) return [];
  if (!SWITCH_KINDS.includes(spec.kind)) return [];
  const rows = completeRows(spec);
  const axes = binding.axes;
  const groups = binding.group_by;
  if (!rows || !axes || !Array.isArray(groups) || !Array.isArray(axes.y)) return [];
  // Creation admits these five kinds under one shared binding rule (`explicit.validate_request`).
  const keys = [axes.x, ...groups, ...axes.y];
  if (Object.keys(axes).some((key) => !["x", "y", "x_scale"].includes(key)) || axes.x_scale !== "category"
    || !axes.y.length || axes.y.length > 12 || groups.length > 1 || (groups.length === 1 && axes.y.length !== 1)
    || keys.some((key) => typeof key !== "string" || !key) || new Set(keys).size !== keys.length) return [];
  const offered = SWITCH_KINDS.filter((kind) => {
    if ((kind === "line" || kind === "area_stacked") && rows.length < 2) return false;
    // One unstacked series draws the same chart as a bar.
    if (kind === "stacked_bar" && !groups.length && axes.y.length < 2) return false;
    return kind === spec.kind || drawable(spec, kind);
  });
  return offered.length > 1 && offered.includes(spec.kind) ? offered : [];
}
