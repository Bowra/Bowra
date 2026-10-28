/**
 * Shared number-marker regex + format util.
 *
 * Two consumers:
 *   1. canvas-pm-adapter/extensions/citation-plugin.ts — Decoration.inline with
 *      data-display attr; raw text hidden via CSS font-size:0, ::before shows
 *      formatted value.
 *   2. components/chat/v3/agent-section.tsx — plain HTML .insight div; needs
 *      raw text REPLACED with formatted value since CSS rule doesn't apply.
 *
 * Pre-0.24.3 these had divergent regex (loose vs strict) and divergent format
 * output (¥1.00亿 vs 1.00亿元). Centralized here to prevent split-brain UX.
 *
 * Native narrative markers share the chart Card profile and current output locale.
 */

import { getCurrentLocale } from "@/lib/locale";
import { formatDisplayValue, resolveDisplayMagnitude, formatObservedPercentCell, isUnsafeIntegerCell, formatObservedValue } from "@/lib/chart/format";

// Compat: with/without unit, with/without src, sprint γ adds #highlight=.
//   [num:V]                                     no unit, no src
//   [num:V:U]                                   unit, no src
//   [num:V:]                                    LLM trailing colon empty unit
//   [num:V:src:S]                               no unit, with src
//   [num:V:U:src:S]                             unit, with src
//   [num:V:U:src:S#highlight=specific-sentence] ← γ sentence-level (m[4]=highlight)
// Value class is `[^:\]]+` (any char except `:` / `]`) so scientific notation
// (e.g. `1.2e10` from str(float)) parses; 0.24.2 used strict `\d+(?:\.\d+)?`
// which silently leaked these markers to chat insight row.
export const NUM_NEW_RE = /\[num:([^:\]]+)(?::([^:#\]]*))?:src:([^#\]]+)(?:#highlight=([^\]]+))?\]/g;
// Unit class is `[^:\]]*` (any char except `:` / `]`) so Chinese units
// like 次/件/亿元/万元 match. Pre-fix used `[A-Za-z_]*` which silently
// failed on CJK and leaked raw `[num:V:U]` to user (prod t_c966ebc3).
export const NUM_OLD_RE = /\[num:([^:\]]+)(?::([^:\]]*))?\]/g;
export const REF_RE = /\[ref:[^\]]+\]/g;
export const CHART_RE = /\[chart:[^\]]+\]/g;

/**
 * CNY identity stays explicit; magnitude and separators follow the surface locale.
 */
export function formatCnyAmount(n: number): string {
  const locale = getCurrentLocale();
  const unit = locale.startsWith("zh") ? "元" : "CNY";
  return formatDisplayValue(n, resolveDisplayMagnitude([n], unit));
}

/**
 * Format number + unit as locale-friendly display text.
 * Format only structured markers; authored prose remains unchanged.
 */
export function formatNumValue(value: string, unit: string): string {
  if (isUnsafeIntegerCell(value)) return formatObservedValue(value, unit === "int" ? undefined : unit);
  const n = Number(value);
  if (!Number.isFinite(n) || !value.trim()) return value;
  if (unit === "percent") return formatObservedPercentCell(n, "fraction");
  if (unit === "CNY") return formatCnyAmount(n);
  const actualUnit = unit === "int" ? undefined : unit;
  return formatDisplayValue(n, resolveDisplayMagnitude([n], actualUnit, undefined, unit === "int" ? "int" : undefined));
}

/**
 * Strip / replace marker tokens for plain-text contexts (chat insight row,
 * dimmed fallback). For `[num:..]` replaces with formatted value; for
 * `[ref:S]` / `[chart:S]` drops the marker (no clickable target in plain
 * context).
 *
 * Use this OUTSIDE ProseMirror only. Inside PM, citation-plugin handles via
 * CSS-based hide-and-replace.
 */
export function stripMarkersForPlainText(text: string): string {
  NUM_NEW_RE.lastIndex = 0;
  NUM_OLD_RE.lastIndex = 0;
  REF_RE.lastIndex = 0;
  CHART_RE.lastIndex = 0;
  return text
    .replace(NUM_NEW_RE, (_full, v, u) => formatNumValue(v, u || ""))
    .replace(NUM_OLD_RE, (_full, v, u) => formatNumValue(v, u || ""))
    .replace(REF_RE, "")
    .replace(CHART_RE, "")
    .replace(/\s+/g, " ")
    .trim();
}
