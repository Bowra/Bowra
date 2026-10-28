"use client";

/**
 * Knowledge-base sections (knowledge-base UX cleanup P1, interaction design §2): Files / Document Matrix /
 * Extracted Results / Templates, addressed by `?tab=`. Scope (Personal/Space/Team) filters files and stays in the
 * file view. The sections render as the page's underline tabs (PageTabs, Header, Language & Paging Spec §2, D5):
 * loading counts show placeholders, never 0; failed reads show no count.
 */

export type KbSection = "files" | "matrix" | "records" | "templates";

export const KB_SECTIONS: readonly KbSection[] = ["files", "matrix", "records", "templates"];

/** Item counts; null = loading (placeholder), undefined = hidden (files item, read failure). */
export type KbNavCounts = Partial<Record<KbSection, number | null>>;

/**
 * URL → left-navigation item. `?tab=files|matrix|records|templates`; support old links:
 * `tab=matrix&view=templates` → Templates, `tab=matrix&view=tpl_*` (old links) → Extracted Results,
 * no tab → Files.
 */
export function resolveKbSection(tab: string | null, view: string | null): KbSection {
  if (tab === "templates") return "templates";
  if (tab === "records") return "records";
  if (tab === "matrix") {
    if (view === "templates") return "templates";
    if (view?.startsWith("tpl_")) return "records";
    return "matrix";
  }
  return "files";
}
