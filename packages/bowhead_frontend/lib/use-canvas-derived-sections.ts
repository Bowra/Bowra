"use client";

/**
 * **Reference-stable** wrapper for derived canvas section lists.
 *
 * Break the production React #185 (Maximum update depth exceeded) root-cause chain here
 * (`docs/sprints/chat-interaction-ux-2026-08-07/spike-两条真机新发现-2026-08-10.md` §2.4):
 *
 * ```
 * chat-workbench:294  subscribe to whole canvas state → every canvas commit rerenders the whole subtree
 * chat-workbench:298  reportSections = selectReportSurfaceSections(canvas)
 *                     ★ computed directly in render, without memo → **new array on every render**
 * chat-workbench:1512 <ReportView sections={reportSections}>
 * report-view:287     useMemo(…, [canonicalSections, …])  ← root dep changes on every render
 *       :360/374/378  currentSections → filtered → initialDoc: entire memo chain invalidated
 * report-view:943     useEffect(…setState…, [initialDoc, …]) → runs every render → loop
 * ```
 *
 * Exactly the second cause React itself names in dev wording:
 * “useEffect … **or one of the dependencies changes on every render**”.
 * `canvas.section_chunk` is frequent (241 same-turn `partial_answer` events observed, comparable frequency),
 * with each event triggering this loop—116 prod observed **32** #185 errors in a single turn.
 *
 * ⚠ Why fix here rather than `report-view:943` deps: that is the **symptom endpoint**.
 * The unstable root dependency would remain and hit the next consumer.
 *
 * ⚠ Why retain `chat-workbench:294`'s whole-object subscription: replacing it is more thorough (the other five
 * canvas subscribers return primitive keys per repository convention), but broader; once this derived
 * reference is stable, the loop is already broken. Leave whole-object subscription for later debt cleanup.
 *
 * deps use `sections` + `bufferedLeadSectionIds`—all inputs to both selectors
 * (see `canvas-store.ts` `selectVisibleSections` / `selectReportSurfaceSections`).
 * Actual content changes (chunks committed) still invalidate correctly, as they should; block only
 * false changes caused by a parent rerender when the canvas is unchanged.
 */
import { useMemo } from "react";

import {
  selectReportSurfaceSections,
  selectVisibleSections,
  type CanvasState,
} from "./canvas-store";
import type { ReportSection } from "./types";

/** Report-surface sections (exclude SQL/code/bare tables). Reference-stable: unchanged canvas gives the same reference. */
export function useReportSurfaceSections(canvas: CanvasState): ReportSection[] {
  return useMemo(
    () => selectReportSurfaceSections(canvas),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [canvas.sections, canvas.bufferedLeadSectionIds],
  );
}

/** Visible export sections (including SQL/code/tables). Reference-stable as above. */
export function useVisibleSections(canvas: CanvasState): ReportSection[] {
  return useMemo(
    () => selectVisibleSections(canvas),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [canvas.sections, canvas.bufferedLeadSectionIds],
  );
}
