// Tool catalog — display metadata for each backend tool. Mirrors Claude
// Design v8 scene.js BHScene.tools so visual diff stays grep-able.
//
// Keys = backend tool names emitted on step_started.tool_name. Renderers
// fall back to the raw tool name if a key isn't in this table (defensive
// against the catalog drifting behind the backend).
//
// Phase 1.7-A audit-fill (tech-design §2.4): added 22 entries enumerated
// from `bowhead_core/tool_registry.py` and `subagent.py` step_started
// emit sites. ToolName union (types.ts) stays narrow for FE-internal
// type discrimination; the catalog is keyed by `string` (`TOOL_CATALOG_KEYS`)
// so runtime lookup handles wider backend names.
//
// i18n: `name`/`activeName` are rendered directly by StepRow/SubstepRow.
// This module is NOT in the React tree (called from a pure helper), so we
// can't useTranslations: names come from the tool_aliases.yaml generated table, selected by UI language;
// the text overrides below live in messages `tool_text.catalog`, retrieved synchronously via `staticTranslator()`;
// `getToolMeta()` selects the language via getCurrentLocale(). Untranslated German uses English placeholders,
// **never falling back to Chinese**.

import { getCurrentLocale, type Locale } from "@/lib/locale";
import { staticTranslator } from "@/lib/locale-messages";

import { canonicalToolName, TOOL_DISPLAY } from "@/lib/_generated/tool-display";
import type { ToolCategory } from "@/lib/tool-display";

export interface ToolMeta {
  icon: string;
  name: string;
  /** Verb form used while the step is active — e.g. "正在写代码". */
  activeName?: string;
  cat: "parse" | "query" | "agent" | "write" | "run";
  /** Substeps run concurrently (e.g. bi_query batch). */
  batch?: boolean;
  /** Substeps run sequentially nested under this step (subagent). */
  nested?: boolean;
  /** Language tag for code blocks. */
  lang?: "Python" | "SQL";
}

// Step-card semantics (icon/category/concurrency/nesting/code language) are language-independent; names and active names live in
// messages `tool_text.catalog.<tool>.name / active_name`.
const CATALOG_OVERRIDES: Record<string, Partial<Omit<ToolMeta, "name" | "activeName">>> = {
  parse_file: { icon: "🔄", cat: "parse" },
  // Name/icon come from the generated table; add only step-card semantics here: spawn N parallel substeps at once.
  bi_query: { batch: true },
  subagent_spawn: { icon: "🤖", cat: "agent", nested: true },
  lead_thinking_writing_python: { icon: "✏️", cat: "write", lang: "Python" },
  lead_thinking_writing_sql: { icon: "✏️", cat: "write", lang: "SQL" },
  // Phase 1.7-A audit-fill — BI meta + extended query tools
  bi_list_measures: { icon: "🔍", cat: "query" },
  bi_list_dimensions: { icon: "🔍", cat: "query" },
  bi_fetch_rows: { icon: "🔍", cat: "query" },
  bi_attribute: { icon: "🔍", cat: "query" },
  bi_breakdown: { icon: "🔍", cat: "query" },
  bi_get_card: { icon: "🔍", cat: "query" },
  bi_get_cards: { icon: "🔍", cat: "query" },
  bi_explain: { icon: "🔍", cat: "query" },
  bi_plan_query: { icon: "🔍", cat: "query" },
  bi_fetch_account: { icon: "🔍", cat: "query" },
  // Retired canvas-writing tools, retained only for historical trace replay (absent from the generated table).
  canvas_add_kpi_row: { icon: "📊", cat: "write" },
  canvas_add_table: { icon: "📊", cat: "write" },
  canvas_add_data_table: { icon: "📊", cat: "write" },
  canvas_add_recommendations: { icon: "📊", cat: "write" },
  // BI async LLM call (rarely user-visible)
  bi_call_llm_async: { icon: "✏️", cat: "write" },
  bi_get_llm_result: { icon: "✏️", cat: "write" },
  // chat-polish-r2 T-1 (2026-05-26) — skill / subagent-return / misc tools
  // missing from catalog. Without these, StepRow falls through to args
  // preview rendering as label (`· {}` rows on trace expand).
  // Registry alias for `skill_list`; the bare `skill_list` name is covered by the generated table.
  skill_list_for_role: { icon: "📒", cat: "query" },
  subagent_return: { icon: "✅", cat: "agent" },
  memory_search: { icon: "🧠", cat: "query" },
  memory_write: { icon: "🧠", cat: "write" },
  // `remember` / `recall` names and icons come from tool_aliases.yaml; the generated category (kb) reads as a
  // query, so only the write classification of `remember` stays here.
  remember: { cat: "write" },
};

/** Overrides carrying text (keys in messages `tool_text.catalog`). */
const CATALOG_TEXT_KEYS = [
  "agent_message",
  "parse_file",
  "subagent_spawn",
  "lead_thinking_writing_python",
  "lead_thinking_writing_sql",
  "python_sandbox",
  "python_exec",
  "bi_list_measures",
  "bi_list_dimensions",
  "bi_fetch_rows",
  "bi_attribute",
  "bi_breakdown",
  "bi_get_card",
  "bi_get_cards",
  "bi_explain",
  "bi_plan_query",
  "bi_fetch_account",
  "canvas_add_kpi_row",
  "canvas_add_table",
  "canvas_add_data_table",
  "canvas_add_recommendations",
  "bi_call_llm_async",
  "bi_get_llm_result",
  "skill_list_for_role",
  // chat-ux-2026-08 batch A: "回传 payload" → "整理结果". The old name exposed the internal protocol term `payload`
  // directly to users (observed on 116: `✅回传 payload · {"payload": {"delivery_surface"…`
  // made the entire row internal machinery). Tell users what this step does, not which protocol it uses.
  "subagent_return",
  "memory_search",
  "memory_write",
] as const;

// ─── Catalog = generated table (base) + the two override tables above (top) ────────────────────────
//
// capability-surface-audit 2026-09-06: this catalog was handwritten; its 42 entries covered only 19 of 35 active
// tools. Missing tools (`canvas_add_spec_block`, `canvas_create_design`,
// `pbi_*`, `web_fetch`…) did not even qualify as `isStaticTool` in `humanLabel()`, exposing
// raw snake_case tool names on step cards.
//
// The base now uses TOOL_DISPLAY generated from `tool_aliases.yaml` (100% coverage of active tools,
// with Chinese/English names + icons + categories). Overrides retain only what the generated table **cannot express**:
//   · Step-card-specific semantics: `activeName` (ongoing action), `batch`, `nested`, `lang`
//   · Context-specific wording: `subagent_return` is "整理结果" on step cards, rather than "子代理完成"
//     (chat-ux-2026-08 batch A: the old copy exposed the internal protocol term payload to users)
//   · Retired tools / pseudo-steps (`parse_file`, `lead_thinking_*`, `memory_*`, old bi_*)
//     do not and should not appear in the generated table; they exist only for historical trace replay.
//
// ⇒ Add tools by changing only yaml; this file needs no change.

const CATEGORY_TO_CAT: Record<ToolCategory, ToolMeta["cat"]> = {
  bi: "query",
  kb: "query",
  web: "query",
  orchestrate: "agent",
  compute: "run",
  skill: "run",
  canvas: "write",
  media: "write",
  document: "parse",
  unknown: "query",
};

function buildCatalog(locale: Locale): Record<string, ToolMeta> {
  const out: Record<string, ToolMeta> = {};
  for (const [tool, d] of Object.entries(TOOL_DISPLAY)) {
    out[tool] = { icon: d.sigil, name: d.label[locale], cat: CATEGORY_TO_CAT[d.category] ?? "query" };
  }
  const t = staticTranslator("tool_text.catalog", locale);
  for (const tool of new Set<string>([...Object.keys(CATALOG_OVERRIDES), ...CATALOG_TEXT_KEYS])) {
    const base = out[tool] ?? { icon: "⚙️", name: tool, cat: "query" };
    const merged: ToolMeta = { ...base, ...CATALOG_OVERRIDES[tool] };
    if (t.hasMessage(`${tool}.name`)) merged.name = t(`${tool}.name`);
    if (t.hasMessage(`${tool}.active_name`)) merged.activeName = t(`${tool}.active_name`);
    out[tool] = merged;
  }
  return out;
}

const catalogs = new Map<Locale, Record<string, ToolMeta>>();
function catalogFor(locale: Locale): Record<string, ToolMeta> {
  let hit = catalogs.get(locale);
  if (!hit) {
    hit = buildCatalog(locale);
    catalogs.set(locale, hit);
  }
  return hit;
}

/** Tools present in the catalog (language-independent). Use `getToolMeta()` for localized names. */
export const TOOL_CATALOG_KEYS: ReadonlySet<string> = new Set([
  ...Object.keys(TOOL_DISPLAY),
  ...Object.keys(CATALOG_OVERRIDES),
  ...CATALOG_TEXT_KEYS,
]);

export function getToolMeta(rawName: string | null | undefined): ToolMeta | null {
  if (!rawName) return null;
  // C4 consolidation (display surface): historical turns retain **pre-rename** names forever; traces/events are immutable history.
  // Normalize only on read, never write back. Change the lookup function, not each table entry.
  // Specification: docs/design/工具改名迁移.md.
  const name = canonicalToolName(rawName);
  // CTO P2 close — was `TOOL_CATALOG[name as ToolName]` which casts away
  // the wider input type. Phase 2 backends may emit tool names not in
  // the union; we want to return `null` (and let renderers fall back)
  // rather than mis-narrow.
  const meta = TOOL_CATALOG_KEYS.has(name) ? catalogFor(getCurrentLocale())[name] : null;
  // team-tool-lead-visibility follow-up: team-connected tools are named
  // `teammcp.<source_id>.<operation>` and are created at runtime, so they can
  // never be in this static catalog. Without a branch here every team tool step
  // renders with the "unknown" glyph. The header text itself comes from the
  // backend's `headerZh` (subagent._tool_header_zh); this only supplies the icon
  // + category so the step block looks like a first-class tool call.
  if (!meta && name.startsWith("teammcp.")) {
    const operation = name.slice(name.lastIndexOf(".") + 1) || name;
    return { icon: "🔌", name: operation, cat: "query" };
  }
  return meta;
}

/**
 * Catalog key used to name a "准备中" row (`step_type=lead_thinking_writing`).
 *
 * A writing step's backend `tool_name` identifies the tool **about to** be called (`python_sandbox` / `bi_query`);
 * direct lookup would show "运行代码" while the model is still writing code, before anything has run.
 * Code / SQL writing are the two kinds with a writing action to describe; use `lead_thinking_writing_python/sql`
 * entries ("正在写代码" / "正在写查询"). `kb_search` / `web_search` write only a query phrase,
 * so keep the tool's name and let the right-side "准备中…" explain that it has not executed.
 * Once execution replaces the row (`step-view.ts::foldWritingSteps`), `stepType` is no longer writing,
 * naturally restoring the tool's name (e.g. "运行代码").
 *
 * Step rows (AgentTrace) and the running band (`running-band-model.ts::humanLabel`) both use this helper.
 * Live / F5 replay / archive consume the same `StepView`, so all three use consistent row names.
 */
const WRITING_DISPLAY_KEYS: Record<string, string> = {
  python_sandbox: "lead_thinking_writing_python",
  bi_query: "lead_thinking_writing_sql",
};

export function writingDisplayKey(
  stepType: string | null | undefined,
  toolName: string | null | undefined,
): string | null {
  if (stepType !== "lead_thinking_writing" || !toolName) return null;
  return WRITING_DISPLAY_KEYS[canonicalToolName(toolName)] ?? null;
}

/** Catalog lookup key for step rows: switch preparing code/query-writing rows to writing entries; leave others unchanged. */
export function stepDisplayToolName(
  stepType: string | null | undefined,
  toolName: string | null | undefined,
): string | null | undefined {
  return writingDisplayKey(stepType, toolName) ?? toolName;
}
