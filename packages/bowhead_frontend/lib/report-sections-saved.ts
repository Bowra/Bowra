/**
 * Terminal `done` `report_sections_saved`: number of sections persisted this turn and visible after refreshing (server counts the database; criteria in
 * docs/design/chat-pipeline.md, “终态 done 带 report_sections_saved”). Live (SSE done) and F5 (last done in `/turns/{id}/events`)
 * share this reader and criteria. If missing or not a nonnegative integer, return null; always treat this as unknown count, not 0.
 */
export function reportSectionsSavedFromDone(payload: unknown): number | null {
  if (!payload || typeof payload !== "object") return null;
  const value = (payload as Record<string, unknown>).report_sections_saved;
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}
