/**
 * Splits a rendered KPI value into number and unit (P2 batch 6, "P2 Icons & Type Spec": KPI number 30 / 600,
 * unit 13 in --c-ink-3). Only clear "number + short unit" values are split; anything else (ranges, ratios,
 * prose such as 示例品｜23.45亿元) returns null and is shown unchanged.
 *
 *   "12.84亿元"  → number "12.84",  unit "亿元"
 *   "−8.4%"      → number "−8.4",   unit "%"      (no gap: the percent sign stays tight)
 *   "¥ 186.20"   → prefix "¥",      number "186.20"
 *   "824.9 ¥M"   → number "824.9",  unit "¥M"
 */
export interface KpiValueParts {
  /** Currency sign written before the number, if any. */
  prefix: string;
  number: string;
  /** Unit written after the number, if any. */
  unit: string;
}

const CURRENCY_PREFIX = String.raw`(?:US\$|HK\$|NT\$|RMB|CNY|USD|EUR|GBP|JPY|[¥￥$€£₩₹])`;
const NUMBER = String.raw`[+\-−±]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?`;
// A short unit: percent / per-mille / percentage points, CJK magnitude and counting words, Latin abbreviations,
// optionally a currency sign (824.9 ¥M) and one "/" (元/人). No digits and no spaces inside.
const UNIT = String.raw`(?:%|‰|pp|bp|[¥￥$€£]?[\p{Script=Han}A-Za-z×]{1,6}(?:\/[\p{Script=Han}A-Za-z]{1,3})?)`;
const PATTERN = new RegExp(String.raw`^(${CURRENCY_PREFIX})?\s?(${NUMBER})(?:\s?(${UNIT}))?$`, "u");

export function splitKpiValue(text: string | null | undefined): KpiValueParts | null {
  const value = (text ?? "").trim();
  if (!value) return null;
  const match = PATTERN.exec(value);
  if (!match) return null;
  const [, prefix = "", number, unit = ""] = match;
  // A bare number has nothing to set apart.
  if (!prefix && !unit) return null;
  return { prefix, number, unit };
}
