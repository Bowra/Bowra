import type { SeriesKey } from "./series-visibility";

/**
 * The series this browser's user has hidden through a report chart's legend, by section id — so an export they start
 * shows the chart the way their screen does.
 *
 * The hidden state is per-viewer screen state (React state in `ChartBlock`), never persisted to the section: other
 * readers and other people's exports are unaffected. When the user exports, the snapshot of this map goes into the
 * export request (`options.chart_hidden_series`); the server folds it into the chart's visual payload
 * (`hidden_series`), which is part of the visual identity hash, so a cached receipt can never hand back the image
 * of a different hidden set.
 *
 * Only report charts write here (not the inspection layer, whose view is separate and transient).
 */
const hiddenBySection = new Map<string, readonly SeriesKey[]>();

/** Record the hidden series of a report chart section (an empty list clears it). */
export function setExportHiddenSeries(sectionId: string, hidden: readonly SeriesKey[]): void {
  if (hidden.length) hiddenBySection.set(sectionId, [...new Set(hidden)].sort());
  else hiddenBySection.delete(sectionId);
}

/** Hidden series for the given sections, for the export request; sections with nothing hidden are omitted. */
export function exportHiddenSeriesFor(sectionIds: Iterable<string>): Record<string, SeriesKey[]> {
  const out: Record<string, SeriesKey[]> = {};
  for (const id of sectionIds) {
    const hidden = hiddenBySection.get(id);
    if (hidden?.length) out[id] = [...hidden];
  }
  return out;
}

/** Test seam. */
export function resetExportHiddenSeries(): void {
  hiddenBySection.clear();
}
