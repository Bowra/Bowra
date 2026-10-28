/**
 * format-inline-tokens—pre-render fallback for non-Tiptap plain-string fields (kpi_row.cells[].val,
 * insight_card non-Markdown text slots).
 *
 * citation-audit M3-1: the three private regexes and `unitToFormat` chart-formatter
 * rules formerly here **were removed**; use the single `citation-marks.stripLegacy` module (character-for-character
 * backend-bake semantics, cross-end golden interlock). Old rules rendered `[num:155031947.26:CNY]` as `¥1.55亿`,
 * unlike M2's persisted `155031947.26元`—one frontend instance of debt 5.
 *
 * Tiptap narrative uses Citation Plugin (PM Decoration, gen3 `[N]` only).
 */

import { stripCiteMarkers, stripLegacy, stripProvenanceTags } from "./citation-marks";

/** Plain-string pre-render fallback: bake retired markers to text + strip chat [cite:N]. */
export function formatInlineTokens(s: string | undefined | null): string {
  if (!s) return s ?? "";
  // [cite:N] is a chat-path marker; canvas has no resolver for leaked markers (it uses [N]+sources)
  // → strip. Not retired, so excluded from stripLegacy, but centralize its regex in citation-marks
  // (previously `\d+` here, `\d{1,3}` in citation-plugin, and another backend copy: three divergent rules).
  // Provenance tags ([llm-std:…] / [bi:art_…]) are machine markers parsed by verification, also hidden from users.
  return stripProvenanceTags(stripCiteMarkers(stripLegacy(s)));
}
