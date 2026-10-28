/**
 * Tool presentation: machine name (bi.ask_question) → locale-aware short name + one-line action summary.
 *
 * Outside the React tree, so useTranslations is unavailable. Use `staticTranslator()` (lib/locale-messages.ts)
 * with getCurrentLocale() to synchronously build short copy from messages.
 *
 * i18n scope (messages namespace `tool_text`; untranslated German uses English placeholders, **never Chinese fallback**):
 *   - legacy_names: old tool names in historical traces (问 BI / Ask BI etc.); active tools use the generated table
 *   - action: toolActionLine user questions / call arguments
 *   - result: toolResultLine result counts / status summaries
 */

import { getCurrentLocale, type Locale } from "./locale";
import { normalizeReasonCode } from "./error-messages";
import { staticTranslator } from "./locale-messages";

import { canonicalToolName, TOOL_DISPLAY } from "@/lib/_generated/tool-display";
import type { ToolCategory } from "@/lib/tool-display";

export interface ToolLabel {
  /** Short name on chips/titles. */
  name: string;
  /** Colored-dot family (for CSS classification). */
  family: "bi" | "skill" | "subagent" | "canvas" | "web" | "media" | "other";
  /** V5 PRD-E1: single emoji as a strong semantic prefix. Fallback ⚙️. */
  emoji: string;
}

// V5 PRD-E1: matches Day 0 spike #1 audit, covering 29 static tool names.
// Each emoji is unique (avoid visually identical chips); see PRD §9.3 decision.
const LEGACY_EMOJI: Record<string, string> = {
  // BI queries (read-only)
  "bi.ask_question": "🔢",
  "bi.fetch_rows": "🔢",
  "bi.profile": "🔍",
  "bi.attribute": "🔢",
  "bi.breakdown": "🔢",
  "bi.list_measures": "🔎",
  "bi.list_dimensions": "🔎",
  "bi.get_card": "📌",
  "bi.get_cards": "📌",
  "bi.explain": "💬",
  "bi.plan_query": "📋",
  "bi.fetch_account": "👤",
  "bi.call_llm_async": "🧠",
  "bi.get_llm_result": "🧠",
  // BI writes (needs_approval)
  "bi.update_measures": "✏️",
  "bi.create_measures": "✏️",
  "bi.update_dimensions": "✏️",
  "bi.delete_measures": "🗑",
  // Skill
  "skill.list": "📚",
  "skill.read": "📖",
  "skill.invoke": "✨",
  "skill.save_generated": "💾",
  // Subagent
  "subagent.spawn": "🤖",
  "subagent.return": "↩️",
  // Canvas
  "canvas.add_chart": "📊",
  "canvas.add_insight": "💡",
  "canvas.add_iframe": "🖼",
  "canvas.add_html_artifact": "📄",
  "canvas.add_external_iframe": "🌐",
  "canvas.add_image": "🖼️",
  "add_image": "🖼️",   // multimodal-v1
  "image.generate": "🎨",
  "image_generate": "🎨",     // multimodal-v1
  // Web
  "web.search": "🌐",
  "web_search": "🌐",      // Legacy alias compatibility
  // tool-domain P2e: post-omicron UNDERSCORE names (dot keys above kept for old-trace replay)
  "bi_query": "🔢", "bi_ask_question": "🔢", "bi_fetch_rows": "🔢", "bi_schema": "🔎",
  "bi_profile": "🔍", "bi_attribute": "🔢", "bi_breakdown": "🔢", "bi_list_measures": "🔎",
  "bi_list_dimensions": "🔎", "bi_get_card": "📌", "bi_get_cards": "📌", "bi_explain": "💬",
  "bi_plan_query": "📋", "bi_fetch_account": "👤", "bi_call_llm_async": "🧠", "bi_get_llm_result": "🧠",
  "skill_list": "📚", "skill_read": "📖", "skill_invoke": "✨", "skill_save_generated": "💾",
  "kb_search": "🔎", "kb_read": "📖",
  "python_sandbox": "🐍",
  "subagent_spawn": "🤖", "subagent_return": "↩️",
  "add_chart": "📊", "canvas_add_insight": "💡",
  "canvas_add_html_artifact": "📄", "canvas_add_external_iframe": "🌐",
  "canvas_add_narrative": "📝", "canvas_patch_section": "✒️",
  "canvas_delete_section": "🗑️", "canvas_reorder_sections": "🔀",
  "web_fetch": "🌐",
};

const LEGACY_FAMILY: Record<string, ToolLabel["family"]> = {
  "bi.ask_question": "bi",
  "bi.fetch_rows": "bi",
  "bi.profile": "bi",
  "bi.attribute": "bi",
  "bi.breakdown": "bi",
  "bi.list_measures": "bi",
  "bi.list_dimensions": "bi",
  "bi.get_card": "bi",
  "bi.get_cards": "bi",
  "bi.explain": "bi",
  "bi.plan_query": "bi",
  "bi.fetch_account": "bi",
  "bi.call_llm_async": "bi",
  "bi.get_llm_result": "bi",
  "bi.update_measures": "bi",
  "bi.create_measures": "bi",
  "bi.update_dimensions": "bi",
  "bi.delete_measures": "bi",
  "skill.list": "skill",
  "skill.read": "skill",
  "skill.invoke": "skill",
  "skill.save_generated": "skill",
  "subagent.spawn": "subagent",
  "subagent.return": "subagent",
  "canvas.add_chart": "canvas",
  "canvas.add_insight": "canvas",
  "canvas.add_iframe": "canvas",
  "canvas.add_html_artifact": "canvas",
  "canvas.add_external_iframe": "canvas",
  "canvas.add_image": "canvas",
  "add_image": "canvas",
  "image.generate": "media",
  "image_generate": "media",
  "web.search": "web",
  "web_search": "web",
  "external_iframe": "other",
  "html_artifact": "other",
  // tool-domain P2e: post-omicron UNDERSCORE names (dot kept above for old-trace replay)
  "bi_query": "bi", "bi_ask_question": "bi", "bi_fetch_rows": "bi", "bi_schema": "bi",
  "bi_profile": "bi", "bi_attribute": "bi", "bi_breakdown": "bi", "bi_list_measures": "bi",
  "bi_list_dimensions": "bi", "bi_get_card": "bi", "bi_get_cards": "bi", "bi_explain": "bi",
  "bi_plan_query": "bi", "bi_fetch_account": "bi", "bi_call_llm_async": "bi", "bi_get_llm_result": "bi",
  "skill_list": "skill", "skill_read": "skill", "skill_invoke": "skill", "skill_save_generated": "skill",
  "subagent_spawn": "subagent", "subagent_return": "subagent",
  "add_chart": "canvas", "canvas_add_insight": "canvas",
  "canvas_add_html_artifact": "canvas", "canvas_add_external_iframe": "canvas",
  "canvas_add_narrative": "canvas", "canvas_patch_section": "canvas",
  "canvas_delete_section": "canvas", "canvas_reorder_sections": "canvas",
  "web_fetch": "web",
};

// ─── Active tool display names: derive from the generated table ──────────────────────────────────
//
// capability-surface-audit 2026-09-06: six tool-name tables existed; the four handwritten tables above
// had many entries (47–69), mostly **retired tool names**, covering only 9–19 of the 35 active
// tools. Nobody read the sole full-coverage generated table that automatically follows YAML additions/deletions.
//
// Now two layers:
//   Old names    — exist only for **historical trace replay** (dotted names such as `bi.ask_question`,
//                  and retired underscored names). These do not belong in the generated table; their copy lives in
//                  messages `tool_text.legacy_names` (dotted names nested by hierarchy).
//   TOOL_DISPLAY — sole source for active tools, covering those names.
//
// ⇒ New tools only change tool_aliases.yaml: Chinese/English names, icon and family together; no change here.

const CATEGORY_TO_FAMILY: Record<ToolCategory, ToolLabel["family"]> = {
  bi: "bi",
  orchestrate: "subagent",
  // compute / kb have no ToolLabel["family"] color, so use "other", as before
  // (neither was in FAMILY_MAP). Separate colors require CSS first, outside this change.
  compute: "other",
  skill: "skill",
  canvas: "canvas",
  kb: "other",
  web: "web",
  media: "media",
  document: "other",
  unknown: "other",
};

const GEN_EMOJI: Record<string, string> = {};
const GEN_FAMILY: Record<string, ToolLabel["family"]> = {};
for (const [tool, d] of Object.entries(TOOL_DISPLAY)) {
  GEN_EMOJI[tool] = d.sigil;
  GEN_FAMILY[tool] = CATEGORY_TO_FAMILY[d.category] ?? "other";
}

/** Tool display name: generated table for active tools, messages for historical names; unknown returns undefined. */
function labelName(toolName: string, locale: Locale = getCurrentLocale()): string | undefined {
  if (Object.hasOwn(TOOL_DISPLAY, toolName)) return TOOL_DISPLAY[toolName].label[locale];
  const t = staticTranslator("tool_text.legacy_names", locale);
  return t.hasMessage(toolName) ? t(toolName) : undefined;
}

const EMOJI_MAP: Record<string, string> = { ...LEGACY_EMOJI, ...GEN_EMOJI };
const FAMILY_MAP: Record<string, ToolLabel["family"]> = { ...LEGACY_FAMILY, ...GEN_FAMILY };

interface ActionPhrases {
  list_intro: string;
  with_text: (label: string, text: string) => string;
  with_compare: (label: string, q: string, cmp: string) => string;
  subagent_profile_suffix: (profile: string) => string;
}

interface ResultPhrases {
  fail: (err: string) => string;
  metric_kv: (name: string, value: string) => string;
  metric_count: (n: number) => string;
  image_returned: string;
  unsupported: string;
  type_returned: (t: string) => string;
  generic_returned: string;
  attribute: (n: number, measure?: string | null) => string;
  got_measures: (n: number) => string;
  got_dimensions: (n: number) => string;
  got_cards: (n: number) => string;
  skill_count: (n: number) => string;
  skill_read: (n: number) => string;
  subagent_done: (tools: number, tokens: number) => string;
  done: string;
}

// Copy lives in messages `tool_text.action` / `tool_text.result`.
// V5 PRD-E1: separator ` │ ` matches Kimi chip visuals.
function _action(): ActionPhrases {
  const t = staticTranslator("tool_text.action");
  return {
    list_intro: t("list_intro"),
    with_text: (label, text) => t("with_text", { label, text }),
    with_compare: (label, q, cmp) => t("with_compare", { label, q, cmp }),
    subagent_profile_suffix: (profile) => t("subagent_profile_suffix", { profile }),
  };
}
function _result(): ResultPhrases {
  const t = staticTranslator("tool_text.result");
  return {
    fail: (err) => t("fail", { err: err || t("unknown_error") }),
    metric_kv: (name, value) => t("metric_kv", { name, value }),
    metric_count: (n) => t("metric_count", { n }),
    image_returned: t("image_returned"),
    unsupported: t("unsupported"),
    type_returned: (type) => t("type_returned", { t: type || "?" }),
    generic_returned: t("generic_returned"),
    attribute: (n, m) => (m ? t("attribute_with_measure", { n, measure: m }) : t("attribute", { n })),
    got_measures: (n) => t("got_measures", { n }),
    got_dimensions: (n) => t("got_dimensions", { n }),
    got_cards: (n) => t("got_cards", { n }),
    skill_count: (n) => t("skill_count", { n }),
    skill_read: (n) => (n ? t("skill_read", { n }) : t("skill_read_empty")),
    subagent_done: (tools, tokens) => t("subagent_done", { tools, tokens }),
    done: t("done"),
  };
}

/** Truncate short text: add ellipsis beyond max. */
function clip(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

/** Canvas tools without the `canvas` prefix after renaming. See docs/design/工具改名迁移.md. */
const RENAMED_CANVAS_TOOLS = new Set(["add_chart", "add_image"]);

/** Is this a canvas-namespace tool?
 *
 * ⚠ **Never use `name.startsWith("canvas")` directly**: renamed `add_chart` / `add_image` lack
 * this prefix; prefix-only checks silently miss them (picker icons become raw names; plan steps stop switching to running).
 */
export function isCanvasTool(name: string): boolean {
  const n = canonicalToolName(name);
  return n.startsWith("canvas") || RENAMED_CANVAS_TOOLS.has(n);
}

export function toolLabel(rawToolName: string): ToolLabel {
  // C4 consolidation (presentation): historical turns retain **pre-rename** names forever; traces/event streams are immutable history.
  // Normalize only on read, never write back. Change the lookup function rather than individual table entries.
  // Specification: docs/design/工具改名迁移.md.
  const toolName = canonicalToolName(rawToolName);
  const name = labelName(toolName);
  const emoji = EMOJI_MAP[toolName] ?? "⚙️";  // V5 PRD-E1 fallback ⚙️ for unmapped tool
  // Fallback still displays the **original** name: unknown values show the actual stored string, without pretending recognition.
  if (!name) return { name: rawToolName, family: "other", emoji };
  return { name, family: FAMILY_MAP[toolName] ?? "other", emoji };
}

/** Extract key information from args preview JSON into a one-line action summary.
 *
 * V5 PRD-E1: redactSqlPii processes user-text fields (text / question / task / query / title),
 * preventing input such as "WHERE email='x@y.com'" from being echoed verbatim in chip source text.
 */
export function toolActionLine(toolName: string, argsPreview: string): string {
  const label = toolLabel(toolName).name;
  const args = safeParse(argsPreview);
  const ap = _action();
  // Helper: extract user-facing fields + redact + clip.
  const userText = (raw: string | undefined, max: number): string =>
    raw ? clip(redactSqlPii(raw), max) : "";

  if (toolName === "bi.ask_question") {
    const text = userText((args as { text?: string })?.text, 80);
    return text ? ap.with_text(label, text) : label;
  }
  if (toolName === "bi.attribute") {
    const q = userText((args as { question?: string })?.question, 70);
    const cmp = (args as { comparison_type?: string })?.comparison_type;
    if (!q) return label;
    return cmp ? ap.with_compare(label, q, cmp) : ap.with_text(label, q);
  }
  if (toolName === "bi.get_card" || toolName === "bi.get_cards"
      || toolName === "bi.explain"
      || toolName === "bi.fetch_rows" || toolName === "bi.profile"
      || toolName === "bi.breakdown") {
    const text = userText((args as { text?: string })?.text, 80);
    return text ? ap.with_text(label, text) : label;
  }
  if (toolName === "bi.list_measures" || toolName === "bi.list_dimensions") {
    return `${label}${ap.list_intro}`;
  }
  if (toolName === "skill.read" || toolName === "skill.invoke") {
    const name = (args as { name?: string })?.name;
    return name ? ap.with_text(label, name) : label;
  }
  if (toolName === "subagent.spawn") {
    const profile = (args as { profile?: string })?.profile;
    const task = userText((args as { task?: string })?.task, 60);
    const sfx = ap.subagent_profile_suffix(profile ?? "?");
    return `${label}${sfx}${ap.with_text("", task)}`.trim();
  }
  if (toolName === "web.search" || toolName === "web_search") {
    const q = userText(
      (args as { query?: string })?.query ?? (args as { q?: string })?.q,
      60,
    );
    return q ? ap.with_text(label, q) : label;
  }
  if (toolName === "external_iframe" || toolName === "html_artifact"
      || toolName.startsWith("canvas.add_")) {
    const t = userText((args as { title?: string })?.title, 60);
    return t ? ap.with_text(label, t) : label;
  }
  return label;
}

/** Take thinking's first sentence (~80 characters), rendering gray text to explain the next tool call. */
export function thinkingHint(thinking: string): string {
  if (!thinking) return "";
  const t = thinking.trim();
  // Split at the first period/question/exclamation mark (Chinese/English), then cap at 80 characters.
  const m = t.match(/^[^。.!?！？\n]{1,140}[。.!?！？]?/);
  const head = (m ? m[0] : t).trim();
  return clip(head, 80);
}

/** Build a one-line result summary from result preview. */
export function toolResultLine(
  toolName: string,
  resultPreview: string,
  ok: boolean,
  errorMsg?: string | null,
  resultMeta?: Record<string, unknown> | null,
): string {
  const rp = _result();
  if (!ok) {
    const code = errorMsg?.trim();
    return rp.fail(code && normalizeReasonCode(code) === code
      ? staticTranslator("error_messages.reasons")(code)
      : "");
  }
  const r = safeParse(resultPreview);
  const meta = resultMeta ?? null;
  if (toolName === "bi.ask_question") {
    if (meta && meta["type"] === "ask_question") {
      const mc = Number(meta["metric_count"] ?? 0);
      const qt = String(meta["q_type"] ?? "");
      if (qt === "metric" && mc > 0) {
        const rr = r as { metrics?: Array<{ name?: string; content?: string; value?: number }> } | null;
        const first = rr?.metrics?.[0];
        if (first) return rp.metric_kv(first.name ?? "", String(first.content ?? first.value ?? ""));
        return rp.metric_count(mc);
      }
      if (qt === "image") return rp.image_returned;
      if (qt === "unsupported") return rp.unsupported;
      return rp.type_returned(qt);
    }
    if (!r || typeof r !== "object") return rp.generic_returned;
    const rr = r as {
      type?: string;
      metrics?: Array<{ name?: string; content?: string; value?: number }>;
    };
    if (rr.type === "metric" && rr.metrics?.length) {
      const m = rr.metrics[0];
      return rp.metric_kv(m.name ?? "", String(m.content ?? m.value ?? ""));
    }
    if (rr.type === "image") return rp.image_returned;
    if (rr.type === "unsupported") return rp.unsupported;
    return rp.type_returned(rr.type ?? "");
  }
  if (toolName === "bi.attribute") {
    if (meta && meta["type"] === "attribute") {
      const n = Number(meta["row_count"] ?? 0);
      const mn = meta["measure_name"] ? String(meta["measure_name"]) : null;
      return rp.attribute(n, mn);
    }
    const rr = r as { rows?: unknown[]; measure_name?: string };
    const rows = rr?.rows?.length ?? 0;
    return rp.attribute(rows, rr?.measure_name ?? null);
  }
  if (toolName === "bi.list_measures") {
    const n = meta?.["type"] === "array"
      ? Number(meta["count"] ?? 0)
      : (Array.isArray(r) ? r.length : 0);
    return rp.got_measures(n);
  }
  if (toolName === "bi.list_dimensions") {
    const n = meta?.["type"] === "array"
      ? Number(meta["count"] ?? 0)
      : (Array.isArray(r) ? r.length : 0);
    return rp.got_dimensions(n);
  }
  if (toolName === "bi.get_card" || toolName === "bi.get_cards") {
    const urls = Array.isArray(r) ? r.length : r ? 1 : 0;
    return rp.got_cards(urls);
  }
  if (toolName === "skill.list") {
    const n = Array.isArray((r as { items?: unknown[] })?.items)
      ? (r as { items: unknown[] }).items.length
      : 0;
    return rp.skill_count(n);
  }
  if (toolName === "skill.read") {
    const n = (r as { body?: string })?.body?.length ?? 0;
    return rp.skill_read(n);
  }
  if (toolName === "subagent.spawn") {
    const rr = r as { ok?: boolean; tool_count?: number; tokens_input?: number };
    return rp.subagent_done(rr?.tool_count ?? 0, rr?.tokens_input ?? 0);
  }
  return rp.done;
}

function safeParse(s: string): unknown {
  if (!s) return null;
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

// ─── V5 PRD-E1 redact helpers ──────────────────────────

const REDACT_FIELD_NAMES = ["password", "token", "auth", "secret", "api_key"];
const REDACT_PII_FIELDS = ["email", "phone", "mobile", "ssn", "id_card"];

/**
 * Regex for PII embedded in SQL strings (E3 drawer; E1 chip source fallback).
 * V1 covers only `field='value'` / `field="value"`; complex LIKE/IN etc. are deferred to V2.
 */
const SQL_PII_PATTERN = /\b(email|phone|mobile|ssn|id_card)\s*=\s*(['"])[^'"]+\2/gi;

export function redactSqlPii(text: string): string {
  return text.replace(SQL_PII_PATTERN, (_m, field, q) => `${field}=${q}***${q}`);
}

/**
 * V5 PRD-E1 + E3: redact tool args (sensitive fields + PII embedded in SQL strings).
 *
 * - `password` / `token` / `auth` / `secret` / `api_key` fields → ***
 * - `email` / `phone` and other PII fields → first 2 characters + ***
 * - SQL PII embedded in any string field → regex replacement
 *
 * Matches PRD-E1 §3.3 + PRD-E3 §3.3.
 */
export function redactArgs(args: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(args)) {
    const kLower = k.toLowerCase();
    if (REDACT_FIELD_NAMES.some((f) => kLower.includes(f))) {
      out[k] = "***";
      continue;
    }
    if (REDACT_PII_FIELDS.some((f) => kLower.includes(f)) && typeof v === "string") {
      out[k] = v.length > 2 ? v.slice(0, 2) + "***" : "***";
      continue;
    }
    if (typeof v === "string") {
      out[k] = redactSqlPii(v);
    } else {
      out[k] = v;
    }
  }
  return out;
}

// ─── V5 PRD-E1 audit support ──────────────────────────

/** Static tool-name list (Day 0 spike #1: 29). Audit script cross-checks it against the dictionary. */
export const STATIC_TOOL_NAMES: readonly string[] = [
  // BI (17, includes 14 read + 3 write/delete writeback; includes bi.update_dimensions).
  "bi.ask_question", "bi.fetch_rows", "bi.list_measures", "bi.list_dimensions",
  "bi.attribute", "bi.breakdown", "bi.get_card", "bi.get_cards",
  "bi.explain", "bi.plan_query", "bi.fetch_account",
  "bi.call_llm_async", "bi.get_llm_result",
  "bi.create_measures", "bi.update_measures", "bi.update_dimensions", "bi.delete_measures",
  // Skill (4)
  "skill.list", "skill.read", "skill.invoke", "skill.save_generated",
  // Subagent (2)
  "subagent.spawn", "subagent.return",
  // Canvas (5)
  "canvas.add_chart", "canvas.add_insight", "canvas.add_iframe",
  "canvas.add_html_artifact", "canvas.add_external_iframe",
  // Web (1)
  "web.search",
] as const;

/** Audit: dictionary coverage, used by `scripts/audit-tool-labels.ts`. */
export function auditToolLabelCoverage(): {
  total: number;
  covered: number;
  missing: string[];
  coveragePct: number;
} {
  const missing = STATIC_TOOL_NAMES.filter(
    (t) => !labelName(t, "zh-CN") || !(t in EMOJI_MAP),
  );
  const total = STATIC_TOOL_NAMES.length;
  const covered = total - missing.length;
  return {
    total,
    covered,
    missing: [...missing],
    coveragePct: (covered / total) * 100,
  };
}

// Locale-aware exports for components that want them
// (not used directly; useTranslations is preferred in React tree)
export type { Locale };
