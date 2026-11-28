"use client";

/**
 * Knowledge-base sections (knowledge-base UX cleanup P1, interaction design §2): Files / Document Matrix /
 * Extracted Results / Templates, addressed by `?tab=`. Scope (Personal/Space/Team) filters files and stays in the
 * file view. The sections render as the page's underline tabs (PageTabs, Header, Language & Paging Spec §2, D5):
 * loading counts show placeholders, never 0; failed reads show no count.
 *
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD §4.1 Q1): with the switch on the tabs are Knowledge bases (the
 * directory, `bases`) / Document matrix / Extraction tables (`records`: results plus the extraction-table
 * definitions) / Templates (matrix templates only). `bases` replaces `files`; old `?tab=files` links open it.
 */

import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";

export type KbSection = "files" | "bases" | "matrix" | "records" | "templates";

/** The sections with the knowledge base switch off (today's page). */
export const KB_SECTIONS: readonly KbSection[] = ["files", "matrix", "records", "templates"];

/** The sections with the knowledge base switch on (Q1). */
export const KB_BASES_SECTIONS: readonly KbSection[] = ["bases", "matrix", "records", "templates"];

/** Item counts; null = loading (placeholder), undefined = hidden (files item, read failure). */
export type KbNavCounts = Partial<Record<KbSection, number | null>>;

/**
 * URL → left-navigation item. `?tab=files|matrix|records|templates`; support old links:
 * `tab=matrix&view=templates` → Templates, `tab=matrix&view=tpl_*` (old links) → Extracted Results,
 * no tab → Files. With `knowledgeBases` the default (no tab, `tab=files`, `tab=bases`) is the directory.
 */
export function resolveKbSection(
  tab: string | null,
  view: string | null,
  opts: { knowledgeBases?: boolean } = {},
): KbSection {
  if (tab === "templates") return "templates";
  if (tab === "records") return "records";
  if (tab === "matrix") {
    if (view === "templates") return "templates";
    if (view?.startsWith("tpl_")) return "records";
    return "matrix";
  }
  return opts.knowledgeBases ? "bases" : "files";
}

type DocTypeCountSource = Pick<KbDocTypeDTO, "doc_type_id" | "kind" | "record_count">;

function isExtractionTable(dt: Pick<KbDocTypeDTO, "kind">): boolean {
  return (dt.kind ?? "manual") === "manual";
}

/**
 * Extraction tables tab (switch on): every extraction table (kind=manual) plus every other table with results,
 * counted once each — the objects the tab lists.
 */
export function extractionTablesListedCount(docTypes: {
  items: readonly DocTypeCountSource[];
  stats: Record<string, { total?: number } | undefined>;
}): number {
  return docTypes.items.filter(
    (dt) => isExtractionTable(dt) || (docTypes.stats[dt.doc_type_id]?.total ?? dt.record_count ?? 0) > 0,
  ).length;
}

/** Templates tab (switch on): matrix templates only (built-in, mine, the Team library). */
export function matrixTemplatesListedCount(items: readonly Pick<KbDocTypeDTO, "kind">[], teamEntries: readonly unknown[]): number {
  return items.filter((dt) => !isExtractionTable(dt)).length + teamEntries.length;
}
