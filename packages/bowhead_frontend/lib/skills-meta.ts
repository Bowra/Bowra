/**
 * skills-meta—pure helpers for /skills IA redesign (2026-07-10).
 *
 * The server supplies presentation titles and categories without changing Skill packages.
 * Missing metadata falls back to the slug. Document subgroups organize built-in
 * catalog rows only; they do not route model requests or change execution grants.
 * Also defines the two skeleton skills requiring confirmation to disable.
 */

import type { SkillSummaryDTO } from "./api";
import type { AgentAuthoringSkill } from "./agents-api";
import { getCurrentLocale } from "./locale";

export type SkillCategory = "analysis" | "documents" | "design" | "platform";

/** Category display order (design §3: Analysis Methods → Documents and Export → Design and Visualization → Platform and Tools). */
export const CATEGORY_ORDER: SkillCategory[] = ["analysis", "documents", "design", "platform"];

/** Presentation-only sections within the built-in Documents category. */
export const DOCUMENT_SKILL_GROUP_ORDER = ["guides", "create", "export"] as const;
export type DocumentSkillGroup = typeof DOCUMENT_SKILL_GROUP_ORDER[number];

const REPORT_EXPORT_SKILLS = new Set(["canvas-docx", "canvas-pdf", "canvas-pptx", "canvas-xlsx"]);
const BASIC_DOCUMENT_SKILLS = new Set(["bowra-docx", "bowra-pdf", "bowra-pptx", "bowra-xlsx"]);

/** Group catalog rows, never route requests or change Skill execution permissions. */
export function documentSkillGroup(name: string): DocumentSkillGroup {
  if (REPORT_EXPORT_SKILLS.has(name)) return "export";
  if (BASIC_DOCUMENT_SKILLS.has(name)) return "create";
  return "guides";
}

/** Built-in skeleton skills requiring confirmation to disable (analysis-pipeline skeleton capabilities). */
export const SKELETON_SLUGS = new Set(["analyst-framework", "analyst-sop-output-contract"]);

const DOC_SLUGS = new Set(["docx", "pdf", "pptx", "xlsx", "doc-coauthoring"]);
const DESIGN_SLUGS = new Set([
  "canvas-design",
  "frontend-design",
  "theme-factory",
  "web-artifacts-builder",
  "building-dashboards",
]);

/** Infer category by slug prefix (fallback for missing frontmatter category; unknown → platform). */
export function inferCategory(slug: string): SkillCategory {
  if (slug.startsWith("analyst-") || slug === "bi-concept-resolve") return "analysis";
  if (slug.startsWith("canvas-") && slug !== "canvas-design") return "documents";
  if (DOC_SLUGS.has(slug)) return "documents";
  if (DESIGN_SLUGS.has(slug)) return "design";
  return "platform";
}

export function skillCategory(s: Pick<SkillSummaryDTO, "name" | "category">): SkillCategory {
  const c = (s.category || "").toLowerCase();
  if (c === "analysis" || c === "documents" || c === "design" || c === "platform") return c;
  return inferCategory(s.name);
}

type SkillDisplay = Pick<SkillSummaryDTO, "name" | "title"> & Partial<Pick<SkillSummaryDTO, "source_type" | "title_zh">>;

/** Reviewed listing descriptions shipped with built-in Skills (SKILL.md `listing`), by interface language. */
export interface SkillListingDescriptions {
  description: string;
  description_en?: string;
  description_zh?: string;
  description_de?: string;
  description_fr?: string;
}

/**
 * The user-facing description for a built-in Skill in the interface language.
 * `description` is the model-facing routing text: it is only the fallback for an
 * older server or a personal override, which arrives without listing fields.
 */
function listingDescription(s: SkillListingDescriptions, locale: string): string {
  const byLocale: Record<string, string | undefined> = {
    "zh-CN": s.description_zh,
    "en-US": s.description_en,
    "de-DE": s.description_de,
    "fr-FR": s.description_fr,
  };
  return byLocale[locale] || s.description_en || s.description;
}

export function skillTitle(s: SkillDisplay, locale: string = getCurrentLocale()): string {
  const title = s.source_type === "builtin" && locale === "zh-CN"
    ? s.title_zh || s.title
    : s.title;
  return (title || "").trim() || humanizeSkillSlug(s.name);
}

/** Skills page and Capability Center rows: localized for built-ins, authored text otherwise. */
export function skillDescription(
  s: SkillListingDescriptions & Partial<Pick<SkillSummaryDTO, "source_type">>,
  locale: string = getCurrentLocale(),
): string {
  return s.source_type === "builtin" ? listingDescription(s, locale) : s.description;
}

/**
 * Readable name for a skill without a title: "comprehensive-business-report" → "Comprehensive business report".
 * The slug itself stays the identifier (shown in the drawer); list rows should not read like code (116 Skills page).
 */
export function humanizeSkillSlug(slug: string): string {
  const words = slug.split(/[-_]+/).filter(Boolean);
  if (words.length === 0) return slug;
  const text = words.join(" ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

type AuthoringSkillDisplay = Pick<AgentAuthoringSkill, "name" | "display_name"> &
  Partial<Pick<AgentAuthoringSkill, "display_name_zh" | "source_kind">> &
  SkillListingDescriptions;

/** Authoring catalogs localize shipped Skills while preserving team and personal names. */
export function authoringSkillTitle(
  s: Pick<AgentAuthoringSkill, "name" | "display_name"> & Partial<Pick<AgentAuthoringSkill, "display_name_zh" | "source_kind">>,
  locale: string = getCurrentLocale(),
): string {
  return (s.source_kind === "builtin" && locale === "zh-CN" ? s.display_name_zh : "") || s.display_name || s.name;
}

/** Picker rows: the reviewed listing text for built-ins, the authored description for team and personal Skills. */
export function authoringSkillDescription(s: AuthoringSkillDisplay, locale: string = getCurrentLocale()): string {
  return s.source_kind === "builtin" ? listingDescription(s, locale) : s.description;
}

function includesQuery(values: Array<string | undefined>, query: string, locale: string): boolean {
  const needle = query.trim().toLocaleLowerCase(locale);
  if (!needle) return true;
  return values.some((value) => (value || "").toLocaleLowerCase(locale).includes(needle));
}

/** Picker search: the title and description shown in the current interface language, plus the stable slug. */
export function authoringSkillMatches(s: AuthoringSkillDisplay, query: string, locale: string = getCurrentLocale()): boolean {
  return includesQuery([authoringSkillTitle(s, locale), authoringSkillDescription(s, locale), s.display_name, s.name], query, locale);
}

/** Search the shown title / description, the default title and the slug (case-insensitive). */
export function skillMatches(
  s: SkillDisplay & SkillListingDescriptions,
  query: string,
  locale: string = getCurrentLocale(),
): boolean {
  return includesQuery([s.name, skillTitle(s, locale), s.title, skillDescription(s, locale)], query, locale);
}
