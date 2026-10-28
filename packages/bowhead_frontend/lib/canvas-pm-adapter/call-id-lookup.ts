/**
 * Sprint ι (PRD §10.1, tech-r2 §3.3): build call_id → section_id lookup
 * from current canvas section list, scoped to current turn_id (cross-turn safety).
 *
 * Used by 2-hop citation popover: when user clicks a `[num:V:U:src:tl_xxx]`
 * marker, popover scrolls to the matching code_block section.
 *
 * Reorder-resilient: re-indexes on every paint (memoized on sections array).
 */

import { useMemo } from "react";
import type { ReportSection } from "@/lib/types";

export interface CallIdLookupEntry {
  /** section_id of the code_block (sec_xxx). */
  sectionId: string;
  /** turn_id the section belongs to (cross-turn no-op uses this). */
  turnId: string;
  /** First input artifact for "View input data" 2-hop. */
  inputArtifactId: string | undefined;
}

/** Build a Map<call_id, CallIdLookupEntry> from canvas sections. */
export function buildCallIdLookup(
  sections: ReportSection[],
): Map<string, CallIdLookupEntry> {
  const m = new Map<string, CallIdLookupEntry>();
  for (const sec of sections) {
    if ((sec as { kind?: string }).kind !== "code_block") continue;
    const cb = sec as Extract<ReportSection, { kind: "code_block" }>;
    if (!cb.call_id || !cb.id) continue;
    m.set(cb.call_id, {
      sectionId: cb.id,
      turnId: cb.turn_id ?? "",
      inputArtifactId: cb.input_artifact_ids?.[0],
    });
  }
  return m;
}

/** React hook variant for use inside components. */
export function useCallIdLookup(
  sections: ReportSection[],
): Map<string, CallIdLookupEntry> {
  return useMemo(() => buildCallIdLookup(sections), [sections]);
}
