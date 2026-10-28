/**
 * canvas-update-turn batch B1-2 (2026-09-14): superseded sections have two origins:
 *   1. An entire old report replaced by a later turn (α folding / LATEST_WINS) → ReportHistory's N Earlier Reports;
 *   2. Old charts no longer referenced after revision-turn chart replacement (+ accompanying code blocks), marked by the backend with `_meta.folded_by="revise"`
 *      → a Replaced Charts (N) folding row at the current report's end.
 * Without separating them, every revision renders part of the **current** report as replaced by the latest report (design review P1-1).
 */
import type { ReportSection } from "@/lib/types";

type WithMeta = ReportSection & { _meta?: { folded_by?: string } };

export function isRevisionFoldedSection(s: ReportSection): boolean {
  return Boolean(s.superseded) && (s as WithMeta)._meta?.folded_by === "revise";
}

export interface SupersededSplit {
  /** whole prior reports — History accordion */
  prior: ReportSection[];
  /** figures / companion code the latest revision replaced — tail strip */
  replaced: ReportSection[];
}

export function splitSupersededSections(superseded: ReportSection[]): SupersededSplit {
  const prior: ReportSection[] = [];
  const replaced: ReportSection[] = [];
  for (const s of superseded) {
    if (isRevisionFoldedSection(s)) replaced.push(s);
    else prior.push(s);
  }
  return { prior, replaced };
}

/** Figure section kinds — same shape as backend `canvas_revise._FIGURE_KINDS_LOCAL` / `report_tools._FIGURE_KINDS`.
 *  Accompanying code_block / sql_block sections are not figures and do not contribute to the folded count. */
const FIGURE_KINDS = new Set([
  "chart",
  "image",
  "design",
  "html_artifact",
  "datarc_iframe",
  "external_iframe",
  "data_table",
]);

export function isFigureKind(kind: string): boolean {
  return FIGURE_KINDS.has(kind);
}

/** a figure row's label: chart/image caption, else the spec title, else empty */
export function replacedFigureLabel(s: ReportSection): string {
  const any = s as unknown as { caption?: unknown; spec?: { title?: unknown }; title?: unknown; lang?: unknown };
  for (const v of [any.caption, any.spec?.title, any.title]) {
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return "";
}
