/**
 * Tool display-name **types and resolution entry**. Data lives elsewhere:
 * tool_aliases.yaml → codegen → lib/_generated/tool-display.ts.
 *
 * Historically handwritten TABLE here (14 entries, covering 11 of 35 active tools)
 * coexisted with generated table but had no production consumers. capability-surface-audit 2026-09-06 found
 * eight naming tables (frontend six + backend two); only fully covering generated table
 * had no readers, while highest-priority table lived backend-side (subagent.py _TOOL_HEADER_ZH,
 * overriding frontend catalog in Chinese UI).
 * Handwritten table now deleted, module handles **types** and **locale-based names** only.
 *
 * Add tools in yaml only: Chinese/English names, icon, category together.
 */

import { getCurrentLocale, type Locale } from "./locale";

import { TOOL_DISPLAY } from "@/lib/_generated/tool-display";

export type ToolCategory =
  | "bi" // business-intelligence data tools
  | "orchestrate" // subagent_spawn / subagent_return
  | "compute" // python_sandbox
  | "skill" // skill_read / skill_invoke
  | "canvas" // canvas_*
  | "kb" // knowledge_base_*
  | "web" // web_search (Sprint ω Phase 4 codegen 2026-05-12)
  | "media" // image_generate (multimodal-v1 2026-06-05)
  | "document" // attachment_read (Word/PowerPoint attachment text)
  | "unknown";

/** `tool` = user-visible built-in Tool; `part` = internal system part that is never listed on its own. */
export type ToolKind = "tool" | "part";

export type ToolRisk = "read" | "compute" | "artifact_write" | "external_write" | "destructive";

/** Generated-table entry: UI wording by locale (German uses English placeholder until translated; see yaml header). */
export interface GeneratedToolDisplay {
  /** Display name. */
  label: Record<Locale, string>;
  /** User-facing one-sentence explanation, for tool-choice lists needing capability explanation. */
  blurb: Record<Locale, string>;
  /** Chip prefix icon (`.tchip .lam`). */
  sigil: string;
  /** Category for ToolGroup / drawer grouping. */
  category: ToolCategory;
  kind: ToolKind;
  /** Always present for `kind: "tool"`. */
  risk?: ToolRisk;
}

/** Display attributes selected for current UI language. */
export interface ToolDisplay {
  /** Display name. */
  label: string;
  /** User-facing one-sentence explanation. */
  blurb: string;
  /** Chip prefix icon (`.tchip .lam`). */
  sigil: string;
  /** Category for ToolGroup / drawer grouping. */
  category: ToolCategory;
}

/**
 * Resolve tool display attributes (localized name/explanation, current UI language by default).
 *
 * Unknown tool falls back to unchanged name + λ + unknown; still renders without styling,
 * **never pretending recognition** (actual stored string enables diagnosis).
 */
export function toolDisplay(toolName: string, locale: Locale = getCurrentLocale()): ToolDisplay {
  const hit = TOOL_DISPLAY[toolName];
  if (!hit) {
    return { label: toolName, blurb: "", sigil: "λ", category: "unknown" };
  }
  return { label: hit.label[locale], blurb: hit.blurb[locale], sigil: hit.sigil, category: hit.category };
}

/** Read-only export for tests and dev-mode flags. */
export const TOOL_DISPLAY_TABLE = TOOL_DISPLAY;

/**
 * Built-in Tools shown in the catalog are the manifest entries with `kind: "tool"`; system parts
 * (`kind: "part"`, such as subagent_return, python_exec, skill_*, agent_* and configuration_*) are never
 * listed. Never reuse NATIVE_ALLOWLIST: it governs granting a Tool to an Agent, not what the product can do.
 * See docs/design/capability-center-plugins-tech-design.md §4.
 */

export interface BuiltinTool {
  /** Tool identifier for keys/diagnosis, not necessarily displayed. */
  ref: string;
  name: string;
  blurb: string;
  sigil: string;
  category: ToolCategory;
}

/**
 * Built-in Tool list, names/explanations selected by current locale.
 *
 * All data from generated tool_aliases.yaml table, **same source** as step cards/chips/tool-choice lists.
 * Never author wording separately at call sites (this sprint consolidated eight handwritten tables into one).
 */
/** Lead delivery tools that write to Google; only meaningful while the team's Google connection is enabled. */
export const GOOGLE_DELIVERY_TOOL_REFS: ReadonlySet<string> = new Set([
  "google_doc_write",
  "google_slides_write",
  "google_sheet_write",
]);

/**
 * Built-in Tool states the Capability Center shows: the Google delivery Tools only while the team's
 * Google connection is enabled. The overview counts and the Tools page must use this same filter.
 */
export function visibleBuiltinToolStates<T extends { name: string }>(tools: readonly T[], googleWorkspace: boolean): T[] {
  return tools.filter((tool) => googleWorkspace || !GOOGLE_DELIVERY_TOOL_REFS.has(tool.name));
}

export function builtinTools(options: { googleWorkspace?: boolean } = {}): BuiltinTool[] {
  return Object.keys(TOOL_DISPLAY)
    .filter((ref) => TOOL_DISPLAY[ref].kind === "tool")
    .filter((ref) => options.googleWorkspace || !GOOGLE_DELIVERY_TOOL_REFS.has(ref))
    .map((ref) => {
      const d = toolDisplay(ref);
      return { ref, name: d.label, blurb: d.blurb, sigil: d.sigil, category: d.category };
    })
    // ⚠ Within group sort by **ref** (tool identifier), never translated name.
    // Observed translated sorting: three Power BI entries in “取数与指标” split among “查看 / 查询 / 读取”
    // positions, separating same capability family into seemingly random order; Chinese/English also produce different orders.
    // Sorting by ref naturally keeps `bi_*` / `pbi_*` families adjacent and both languages ordered identically.
    .sort((a, b) => a.category.localeCompare(b.category) || a.ref.localeCompare(b.ref));
}

/** Select built-in catalog translations; external tool labels keep their original text. */
export function localizedToolText(tool: {
  display_name: string;
  description: string;
  display_name_en?: string;
  description_en?: string;
  display_name_zh?: string;
  description_zh?: string;
  display_name_de?: string;
  description_de?: string;
  display_name_fr?: string;
  description_fr?: string;
}, locale: string = getCurrentLocale()): { name: string; description: string } {
  if (locale === "zh-CN") {
    return { name: tool.display_name_zh || tool.display_name, description: tool.description_zh || tool.description };
  }
  const translations = {
    "en-US": { name: tool.display_name_en, description: tool.description_en },
    "de-DE": { name: tool.display_name_de, description: tool.description_de },
    "fr-FR": { name: tool.display_name_fr, description: tool.description_fr },
  };
  const selected = translations[locale as keyof typeof translations] ?? translations["en-US"];
  return {
    name: selected.name || tool.display_name_en || tool.display_name,
    description: selected.description || tool.description_en || tool.description,
  };
}
