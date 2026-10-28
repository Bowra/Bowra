/** Numeric / time formatting — all chart components share these functions for consistent display.
 *
 * i18n v1: chart axis formatter switches to lib/locale.ts (Intl.NumberFormat CLDR-driven).
 * Shares CLDR data source with backend bowhead_core/format_locale.py babel;
 * prose and chart number formats follow user output_language consistently (Chinese "1.2万" / English "12K").
 */

import {
  formatNumber as _formatLocaleNumber,
  getCurrentLocale,
} from "../locale";
import type { ChartColumn, ChartColumnFormat, NumericDisplay } from "./types";
import { formatExactSourceNumber } from "../source-content";

export function formatNumber(
  v: number | null | undefined,
  fmt?: ChartColumnFormat,
  unit?: string,
): string {
  if (v === null || v === undefined) return "—";
  if (typeof v !== "number" || Number.isNaN(v)) return String(v);

  if (fmt === "percent") {
    // Percent always 1 decimal; Intl determines locale grouping (zh "12.3%" / de "12,3%")
    return _formatLocaleNumber(v * 100, getCurrentLocale(), { decimals: 1 }) + "%";
  }
  if (fmt === "currency") {
    // prod-bug-bash-038 #8: if LLM stamps unit="decimal" on a currency column,
    // withUnit would strip it and lose the ¥ symbol. Force ¥ when unit is
    // dimensionless on a currency-formatted column.
    const safeUnit = unit && DIMENSIONLESS_UNITS.has(unit.toLowerCase()) ? "¥" : (unit ?? "¥");
    return withUnit(abbrChinese(v), safeUnit);
  }
  if (fmt === "float") {
    return withUnit(abbrChinese(v, 2), unit);
  }
  // int / default
  // chat-truth Phase L-FE (L2): walkthrough Session 4 rounded -0.09% to "-0".
  // Cause: int/default decimals=0 → -0.09 → -0 (compact + round). Fix:
  // 0 < |v| < 1 forces 2 decimals for readability (e.g. -0.09 → "-0.09");
  // |v| >= 1 / v == 0 retains decimals=0 (large compact "19.9 亿" / "12K" /
  // integers → "0" / "100"). Preserve currency / percent / float paths (guarded by tests).
  if (v !== 0 && Math.abs(v) < 1) {
    return withUnit(abbrChinese(v, 2), unit);
  }
  return withUnit(abbrChinese(v, 0), unit);
}

/**
 * Locale-aware compact numbers (i18n v1: switched to Intl.NumberFormat CLDR).
 *
 * Retain `abbrChinese` name because build-config.ts / chart-table etc. already import it;
 * actual behavior follows user output_language (Chinese "1.2万" / English "12K" / German "1,2 Tsd.").
 *
 * Historical interface:
 *   abbrChinese(1234567, 1) === "123.5万"  (zh)
 *   abbrChinese(1234567, 1) === "1.2M"     (en)
 */
export function abbrChinese(v: number, decimals = 1): string {
  return _formatLocaleNumber(v, getCurrentLocale(), {
    compact: true,
    decimals,
  });
}

/** Batch 1 1-D: axis ticks only — omit integer ".0" noise (10.0万 → 10万), retain 1 decimal for nonintegers.
 *  Strip at string level (single 0 after decimal separator), independent of compact suffix; separator comes from current
 *  locale Intl format (German/French comma: "10,0 Tsd." → "10 Tsd.", "10,0 k" → "10 k").
 *
 *  ★★ |v| < 1 uses **significant digits**, not fixed 1 decimal. Fixed 1 rounds 0.0004 to `"0"`,
 *  making **whole y-axis a string of zeros**; -0.0004 even becomes `"-0"`, perceived as bug.
 *  116 measurement: among 2753 buildable charts, **103 columns** had all ticks rendered 0, another 217 some ticks 0,
 *  181 rendered "-0". Nearly all affected were **decimal-stored ratios** like "同比变化率 / 同比增长"
 *  (0.0064 = 0.64% year-over-year; no percent format on column, so percentage formatting path unavailable).
 *
 *  Changing only here suffices: axis ticks (`build-config.ts` tick callback), data labels and summary rows
 *  (`formatCompactValue`) all flow here; KPI approximate-magnitude line calls `abbrChinese` directly,
 *  appearing only for |v| ≥ `KPI_APPROX_THRESHOLD`, outside this path. */
export function abbrTick(v: number): string {
  // Render `-0` as `0`: JSON parses `-0` unchanged (`JSON.parse("-0")`), and **both**
  // formatting paths render "-0" (observed in `Intl` compact and significant-digit modes).
  // `-0 === 0` is true, so this line handles both `0` and `-0`.
  if (v === 0) return "0";
  if (Number.isFinite(v) && Math.abs(v) < 1) return smallTick(v);
  const sep = escapeRegExp(decimalSeparator(getCurrentLocale()));
  return abbrChinese(v, 1).replace(new RegExp(`(\\d)${sep}0(?=\\D|$)`), "$1");
}

/** Intl decimal separator for locale (zh/en ".", de/fr ","). */
function decimalSeparator(locale: string): string {
  try {
    return new Intl.NumberFormat(locale).formatToParts(1.5).find((part) => part.type === "decimal")?.value ?? ".";
  } catch {
    return ".";
  }
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** |v| < 1 uses 2 **significant digits**: 0.0004 → `0.0004`, 0.049 → `0.049`, 0.4 → `0.4`.
 *  Significant digits instead of fixed decimals because scale varies (year-over-year change 0.0006 and
 *  share 0.35 may coexist in one chart); fixed precision always rounds one end to 0. */
function smallTick(v: number): string {
  try {
    return new Intl.NumberFormat(getCurrentLocale(), { maximumSignificantDigits: 2 }).format(v);
  } catch {
    return String(v);
  }
}

/** Compact in-chart values: independently readable magnitude with an explicit unit.
 *  For space-limited reference lines/narrow-width summaries; tables/tooltips still use precision mode. */
export function formatCompactValue(v: number | null | undefined, unit?: string): string {
  if (v === null || v === undefined) return "—";
  if (typeof v !== "number" || Number.isNaN(v)) return String(v);
  const display = resolveDisplayMagnitude([v], unit);
  if (display) return formatDisplayValue(v, display);
  return withUnit(abbrTick(v), unit);
}

/** Preserve the available original value; compact axes and labels use their own formatter. */
export function formatExact(v: number | null | undefined, unit?: string): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  if (v < 0 && (unit === "¥" || unit === "$" || unit === "€")) return `-${withUnit(formatExactSourceNumber(-v), unit)}`;
  return withUnit(formatExactSourceNumber(v), unit);
}

/** Short month ticks require an explicitly supplied, single-year scope; readouts keep full labels. */
export function monthlyAxisTickLabels(labels: readonly unknown[], scope: string, locale = "zh-CN"): string[] | null {
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const parsed = labels.map((label) => {
    const text = String(label);
    const numeric = /^(\d{4})[-年](\d{1,2})月?$/.exec(text);
    const named = /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})$/.exec(text);
    const year = Number(numeric?.[1] ?? named?.[2]);
    const month = numeric ? Number(numeric[2]) : named ? months.indexOf(named[1]!) + 1 : 0;
    return year >= 1000 && month >= 1 && month <= 12 ? { year, month } : null;
  });
  const year = parsed[0]?.year;
  if (!year || parsed.some((period) => !period || period.year !== year)
    || !new RegExp(`(^|\\D)${year}(\\D|$)`).test(scope)) return null;
  const formatter = new Intl.DateTimeFormat(locale, { month: "short", timeZone: "UTC" });
  return parsed.map((period) => formatter.format(new Date(Date.UTC(period!.year, period!.month - 1, 1))));
}

interface CalendarCell { date: Date; year: number; month: number; day: number; grain: "month" | "date" | "minute" | "second"; offset: string }
function calendarCell(raw: string): CalendarCell | null {
  const match = /^(\d{4})[-年](\d{1,2})(?:月|-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?)?$/.exec(raw);
  if (!match) return null;
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3] ?? 1);
  const hour = Number(match[4] ?? 0), minute = Number(match[5] ?? 0), second = Number(match[6] ?? 0);
  const date = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day || hour > 23 || minute > 59 || second > 59) return null;
  return { date, year, month, day, grain: match[6] ? "second" : match[4] ? "minute" : match[3] ? "date" : "month", offset: match[7] ?? "" };
}

/** Calendar membership controls context; local timestamps never acquire an invented zone. */
export function calendarAxisTickLabels(labels: readonly unknown[], scope: string, locale: string = getCurrentLocale()): string[] | null {
  const cells = labels.map((label) => calendarCell(String(label)));
  const first = cells[0];
  if (!first || cells.some((cell) => !cell || cell.grain !== first.grain)) return null;
  const sameYear = cells.every((cell) => cell!.year === first.year);
  const sameMonth = sameYear && cells.every((cell) => cell!.month === first.month);
  const sameDay = sameMonth && cells.every((cell) => cell!.day === first.day);
  const statedYear = sameYear && new RegExp(`(^|\\D)${first.year}(\\D|$)`).test(scope);
  const timed = first.grain === "minute" || first.grain === "second";
  const statedDay = sameDay && labels.some((label) => scope.includes(String(label).slice(0, 10)));
  const options: Intl.DateTimeFormatOptions = {
    timeZone: "UTC",
    ...(!statedYear && !(timed && statedDay) ? { year: "numeric" } : {}),
    ...(!(timed && statedDay) ? { month: "short" } : {}),
    ...(first.grain !== "month" && !(timed && statedDay) ? { day: "numeric" } : {}),
    ...(timed ? { hour: "numeric", minute: "2-digit" } : {}),
    ...(first.grain === "second" ? { second: "2-digit" } : {}),
  };
  const formatter = new Intl.DateTimeFormat(locale, options);
  const rendered = cells.map((cell) => formatter.format(cell!.date));
  return rendered.map((label, index) => cells[index]!.offset && rendered.filter((candidate) => candidate === label).length > 1
    ? `${label} ${cells[index]!.offset}` : label);
}

// prod-bug-bash-038 #8: dimensionless units LLM hallucinates (despite prompt now
// telling it not to). FE defense-in-depth blacklist; user should never see
// "0.72 decimal" / "0.51 correlation" suffix on KPI cards.
// prod-bug-bash-039 P2-5: tightened — dropped "index" / "score" / "percentage" /
// "percent" / "multiplier" / "factor" — too broad, would suppress legitimate
// CPI/HSI/PMI "index" or rate/score column units.
const DIMENSIONLESS_UNITS = new Set([
  "decimal", "ratio", "correlation", "coefficient",
  "p_value", "z_score",
]);

/** Should unit display? (LLM-hallucinated dimensionless units never enter user-visible text.)
 *  Chart surface previously bypassed blacklist, printing `unit="decimal"` directly as `0.2decimal`. */
export function isDisplayableUnit(unit?: string): boolean {
  return Boolean(unit) && !DIMENSIONLESS_UNITS.has(unit!.toLowerCase());
}

// Units a header qualifier may name (Chart Embed Spec review): anything else in brackets is a qualifier — 同比, 不含税,
// 去重, YoY, 估算, avg, a region — and must not become an axis "unit".
const CJK_MAGNITUDES = ["", "十", "百", "千", "万", "十万", "百万", "千万", "亿", "十亿", "百亿", "千亿", "万亿"];
const CJK_BASE_UNITS = [
  // currency
  "元", "人民币", "美元", "港元", "港币", "欧元", "日元", "英镑", "韩元", "澳元", "加元", "新台币",
  // counts / measure words
  "个", "件", "单", "笔", "人", "人次", "次", "家", "台", "辆", "张", "份", "户", "位", "名", "条", "箱", "盒", "瓶",
  "包", "套", "间", "店", "门店", "台次", "架次", "例", "项", "篇", "页", "行", "批", "单位",
  // time
  "秒", "分", "分钟", "小时", "时", "天", "日", "周", "月", "季度", "年",
  // weight / length / area / volume / energy
  "克", "千克", "公斤", "斤", "吨", "米", "厘米", "毫米", "千米", "公里", "平方米", "平米", "平方公里", "亩", "公顷",
  "升", "毫升", "立方米", "方", "千瓦时", "度", "瓦", "千瓦",
];
const SYMBOL_UNITS = new Set([
  "%", "％", "‰", "pp", "百分点", "个百分点", "bp", "bps", "基点",
  "¥", "￥", "$", "€", "£", "rmb", "cny", "usd", "eur", "hkd", "jpy", "gbp",
  "k", "m", "b", "mn", "bn", "mm", "mio", "mrd", "thousand", "thousands", "million", "millions", "billion", "billions",
  "s", "ms", "sec", "min", "mins", "h", "hr", "hrs", "hour", "hours", "day", "days", "week", "weeks", "month", "months",
  "year", "years", "g", "kg", "t", "ton", "tons", "lb", "lbs", "oz", "cm", "km", "mi", "ft", "l", "ml",
  "㎡", "m²", "m2", "km²", "sqm", "kwh", "mwh", "pcs", "units", "orders", "users", "people", "visits",
]);
const CJK_UNITS = new Set(CJK_MAGNITUDES.flatMap((m) => CJK_BASE_UNITS.map((u) => m + u)).concat(
  CJK_MAGNITUDES.filter(Boolean),
));

/** One unit token: an allowlisted CJK unit (with an optional magnitude, `万元`, `亿人次`), a symbol / code
 *  (`%`, `bps`, `USD`), or a currency glued to a magnitude (`$M`, `USD mn`, `¥万`). */
function isUnitToken(token: string): boolean {
  const t = token.trim();
  if (!t) return false;
  if (CJK_UNITS.has(t)) return true;
  const lower = t.toLowerCase();
  if (SYMBOL_UNITS.has(lower)) return true;
  // Currency or code followed by a magnitude: `$M`, `$ bn`, `USD mn`, `¥万`, `RMB 亿`.
  const glued = /^([¥￥$€£]|rmb|cny|usd|eur|hkd|jpy|gbp)\s*(.+)$/i.exec(t);
  if (glued && (SYMBOL_UNITS.has(glued[2]!.toLowerCase()) || CJK_MAGNITUDES.includes(glued[2]!))) return true;
  return false;
}

/**
 * Unit stated in a column header when the column has no `unit` field: a trailing bracket (`营业额（万元）`,
 * `Revenue ($M)`) or a spaced slash qualifier (`营业额 / 元`). 116 measured 835 charts whose dimension lives only in
 * the header, so a unit-only axis title (Chart Embed Spec) must read it there.
 *
 * Only an **allowlisted unit** counts (currencies, magnitudes, %/‰/pp/百分点/bp, counts and measure words, time,
 * weight / length / area / volume), alone or as a rate of two units (`元/人`). Everything else in the qualifier —
 * `（同比）`, `（不含税）`, `（去重）`, `(YoY)`, `(估算)`, `(avg)`, `/ 华东` — is a qualifier, so no axis title.
 * An unspaced slash is a ratio name (`销售额/面积`) and is not read as a qualifier at all.
 */
export function headerUnitOf(header?: string): string | undefined {
  if (!header) return undefined;
  const match = /[(（]([^()（）]{1,12})[)）]\s*$/.exec(header) ?? /\s[/／]\s*([^/／]{1,12})$/.exec(header);
  const unit = match?.[1]?.trim();
  if (!unit || !isDisplayableUnit(unit)) return undefined;
  const parts = unit.split(/\s*[/／]\s*/);
  if (parts.length > 2 || !parts.every(isUnitToken)) return undefined;
  return unit;
}

/** Resolve the unit actually represented on a value axis, without converting source values. */
export function axisUnitOf(col: ChartColumn): string | undefined {
  if (resolvePercentColumn(col)) return "%";
  if (col.currency_code && (!col.unit || /^[¥￥$€£]$/.test(col.unit))) return col.currency_code;
  if (isDisplayableUnit(col.unit)) return col.unit;
  return headerUnitOf(col.measure_header ?? col.header) ?? headerUnitOf(col.source_header)
    ?? [col.measure_header ?? col.header, col.source_header].map((header) => header?.includes("_") ? header.split("_").at(-1) : undefined)
      .find((suffix) => suffix && isUnitToken(suffix));
}

const DECIMAL_MAGNITUDES: Record<string, number> = {
  "": 0, "千": 3, "万": 4, "十万": 5, "百万": 6, "千万": 7,
  "亿": 8, "十亿": 9, "百亿": 10, "千亿": 11, "万亿": 12,
  k: 3, "10k": 4, m: 6, mn: 6, mm: 6, b: 9, bn: 9, t: 12,
  thousand: 3, million: 6, billion: 9, trillion: 12,
};
interface UnitMagnitude { base: string; exponent: number; }
const compactPatterns = new Map<string, Array<{ power: number; prefix: string; suffix: string }>>();

function localeCompactPatterns() {
  const locale = getCurrentLocale();
  const cached = compactPatterns.get(locale);
  if (cached) return cached;
  const compact = new Intl.NumberFormat(locale, { notation: "compact", numberingSystem: "latn", maximumFractionDigits: 0 });
  const patterns = [{ power: 0, prefix: "", suffix: "" }];
  for (let power = 3; power <= 12; power++) {
    const parts = compact.formatToParts(10 ** power);
    const mantissa = parts.filter((part) => part.type === "integer").map((part) => part.value).join("");
    if (mantissa !== "1" || !parts.some((part) => part.type === "compact")) continue;
    const numberIndex = parts.findIndex((part) => part.type === "integer");
    patterns.push({ power, prefix: parts.slice(0, numberIndex).map((part) => part.value).join(""), suffix: parts.slice(numberIndex + 1).map((part) => part.value).join("") });
  }
  compactPatterns.set(locale, patterns);
  return patterns;
}

/** Only explicit monetary tokens authorize undoing a stored decimal magnitude. */
function monetaryMagnitude(unit?: string): UnitMagnitude | null {
  if (!unit) return null;
  const text = unit.trim();
  const cjk = /^(万亿|千亿|百亿|十亿|亿|千万|百万|十万|万|千)?(元|人民币|美元|港元|港币|欧元|日元|英镑|韩元|澳元|加元|新台币)$/.exec(text);
  if (cjk) return { base: cjk[2]!, exponent: DECIMAL_MAGNITUDES[cjk[1] ?? ""]! };
  const currency = "(?:CNY|RMB|USD|EUR|HKD|JPY|GBP|CHF|CAD|AUD|[¥￥$€£])";
  const suffix = new RegExp(`^(${currency})\\s*(.*)$`, "i").exec(text);
  const prefix = new RegExp(`^(.*?)\\s*(${currency})$`, "i").exec(text);
  const base = suffix?.[1] ?? prefix?.[2];
  const magnitude = (suffix?.[2] ?? prefix?.[1] ?? "").toLowerCase();
  return base && Object.hasOwn(DECIMAL_MAGNITUDES, magnitude)
    ? { base, exponent: DECIMAL_MAGNITUDES[magnitude]! } : null;
}

/** Comparable quantity identity requires an explicit unit or ISO code. Symbols
 * remain their own ambiguous identity; USD and CAD never share a dollar axis. */
export function quantityUnitOf(column: ChartColumn): { identity: string | undefined; base: string | undefined; exponent: number } {
  const unit = axisUnitOf(column);
  const magnitude = monetaryMagnitude(unit);
  const aliases: Record<string, string> = { "元": "CNY", "人民币": "CNY", "RMB": "CNY", "美元": "USD", "欧元": "EUR", "日元": "JPY", "港元": "HKD", "港币": "HKD", "英镑": "GBP", "加元": "CAD", "澳元": "AUD" };
  const base = magnitude?.base ?? unit;
  return { identity: column.currency_code ?? (base ? aliases[base] ?? base : undefined), base, exponent: magnitude?.exponent ?? 0 };
}

// Cache only locale/format metadata; never retain chart values or source identity.
const readoutFormatters = new Map<string, Intl.NumberFormat>();
function readoutFormatter(options: Intl.NumberFormatOptions = {}): Intl.NumberFormat {
  const locale = getCurrentLocale();
  const key = `${locale}:${JSON.stringify(options)}`;
  let formatter = readoutFormatters.get(key);
  if (!formatter) {
    if (readoutFormatters.size >= 64) readoutFormatters.clear();
    formatter = new Intl.NumberFormat(locale, options);
    readoutFormatters.set(key, formatter);
  }
  return formatter;
}

/** Preserve integer tokens before any Number conversion, including lazy row completion. */
export function isUnsafeIntegerCell(value: unknown): boolean {
  if (typeof value !== "string" || !/^[+-]?\d+$/.test(value.trim())) return false;
  const integer = BigInt(value.trim());
  return integer > BigInt(Number.MAX_SAFE_INTEGER) || integer < BigInt(Number.MIN_SAFE_INTEGER);
}

/** Relational comparison keeps BigInt versus Number exact without rounding the integer token. */
export function comparePreparedNumbers(left: unknown, right: unknown): number {
  const a = isUnsafeIntegerCell(left) ? BigInt(left as string) : Number(left);
  const b = isUnsafeIntegerCell(right) ? BigInt(right as string) : Number(right);
  return a < b ? -1 : a > b ? 1 : 0;
}

export function validNumericDisplay(value: unknown): NumericDisplay | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const v = value as NumericDisplay;
  return Object.keys(v).sort().join(",") === "decimal_mode,decimal_places" && Number.isInteger(v.decimal_places)
    && v.decimal_places >= 0 && v.decimal_places <= 12 && (v.decimal_mode === "max" || v.decimal_mode === "fixed") ? v : undefined;
}
function precisionOptions(display: NumericDisplay): Intl.NumberFormatOptions {
  return { maximumFractionDigits: display.decimal_places, minimumFractionDigits: display.decimal_mode === "fixed" ? display.decimal_places : 0 };
}

/** Ordinary readouts retain the base quantity and round once for reading, not verification. */
export function formatObservedValue(
  value: number | string | null | undefined, unit?: string, format?: ChartColumnFormat, includeUnit = true, display?: NumericDisplay, currencyCode?: string,
): string {
  const magnitude = monetaryMagnitude(unit);
  const precision = validNumericDisplay(display);
  const baseUnit = currencyCode && /^[¥￥$€£]$/.test(magnitude?.base ?? unit ?? "") ? currencyCode : magnitude?.base ?? unit;
  if (isUnsafeIntegerCell(value)) {
    const integer = BigInt(value as string) * 10n ** BigInt(magnitude?.exponent ?? 0);
    const digits = readoutFormatter(precision ? precisionOptions(precision) : {}).format(integer);
    return includeUnit ? withLocalizedUnit(digits, baseUnit) : digits;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const decimals = precision?.decimal_places ?? (format === "int" ? 0 : format === "float" ? 4 : magnitude || format === "currency" || /^(?:pp|bps|‰|‱)$/i.test(unit ?? "") ? 2 : 4);
  const digits = ordinaryDigits(value, magnitude?.exponent ?? 0, decimals, !!precision || format === "int", precision?.decimal_mode === "fixed");
  const base = baseUnit;
  return includeUnit || inlineCellUnit(base) ? withLocalizedUnit(digits, base) : digits;
}

/** Intl uses half-expand ties. Shift decimal text before rounding to avoid arithmetic tails. */
function ordinaryDigits(value: number, shift: number, decimals: number, explicit = false, fixed = false): string {
  const scaled = Number(formatExactSourceNumber(value, shift).replaceAll(",", ""));
  if (!Number.isFinite(scaled)) return "—";
  const tiny = !explicit && scaled !== 0 && Math.abs(scaled) < 0.5 * 10 ** -decimals;
  const formatter = readoutFormatter(tiny
    ? { maximumSignificantDigits: 2, ...(Math.abs(scaled) < 1e-6 ? { notation: "scientific" as const } : {}) }
    : { maximumFractionDigits: decimals, minimumFractionDigits: fixed ? decimals : 0 });
  if (scaled >= 0) return formatter.format(scaled === 0 ? 0 : scaled);
  // A rounded zero must not retain a negative sign.
  const parts = formatter.formatToParts(scaled);
  return parts.some((part) => (part.type === "integer" || part.type === "fraction") && /[1-9]/.test(part.value))
    ? formatter.format(scaled) : formatter.format(0);
}

/** Currency and percent spacing/placement follow the active locale, without guessing currency identity. */
function withLocalizedUnit(digits: string, unit?: string): string {
  if (!unit || !isDisplayableUnit(unit)) return digits;
  if (/^(?:[A-Z]{3}|[¥￥$€£]|%)$/.test(unit)) {
    const formatter = readoutFormatter(unit === "%"
      ? { style: "percent" }
      : { style: "currency", currency: /^[A-Z]{3}$/.test(unit) ? unit : "USD", currencyDisplay: /^[A-Z]{3}$/.test(unit) ? "code" : "narrowSymbol" });
    let numberWritten = false;
    const negative = digits.startsWith("-");
    return formatter.formatToParts(negative ? -1 : 1).map((part) => {
      if (["integer", "group", "decimal", "fraction"].includes(part.type)) {
        if (numberWritten) return "";
        numberWritten = true;
        return negative ? digits.slice(1) : digits;
      }
      return part.type === "currency" || part.type === "percentSign" ? unit : part.value;
    }).join("");
  }
  return `${digits}${unit}`;
}

/** A render-only choice shared by one numeric axis. Source values and coordinates stay unchanged. */
export interface DisplayMagnitude {
  shift: number;
  unit: string;
  base: string;
  suffix: string;
  prefix: string;
  decimals: number;
  fixed?: boolean;
  explicit?: boolean;
}

function magnitudeContext(size: number, unit: string | undefined, surface: "card" | "axis", display?: NumericDisplay, format?: ChartColumnFormat): DisplayMagnitude {
  const source = monetaryMagnitude(unit) ?? { base: isDisplayableUnit(unit) ? unit! : "", exponent: 0 };
  const amount = Math.abs(size) * 10 ** source.exponent;
  const patterns = localeCompactPatterns();
  const chinese = getCurrentLocale().startsWith("zh");
  let index = Math.max(0, patterns.findLastIndex(({ power }) => 10 ** power <= amount));
  if (chinese && amount <= 10000) index = 0;
  // The reference's axis ranges have inclusive upper boundaries.
  if (surface === "axis" && chinese && amount === 1e8) index = patterns.findIndex(({ power }) => power === 4);
  const precision = validNumericDisplay(display);
  const decimalsAt = (power: number): number => {
    if (precision?.decimal_mode === "fixed" && surface === "card") return precision.decimal_places;
    if (surface === "card") return power === 0 ? format === "int" || amount > 1000 ? 0 : 2 : chinese && power === 4 && amount > 1e7 ? 0 : 2;
    if (chinese) return power === 0 ? amount <= 5 ? 2 : 0 : power === 4 ? amount <= 5e4 ? 2 : 0 : amount <= 5e8 ? 2 : 0;
    return amount / 10 ** power <= 5 ? 2 : 0;
  };
  if (surface === "card") {
    // Recompute from the original value after rounding reaches a larger magnitude.
    while (index + 1 < patterns.length) {
      if (chinese && amount <= 10000 && patterns[index + 1]!.power === 4) break;
      const power = patterns[index]!.power;
      const rounded = Number((amount / 10 ** power).toFixed(decimalsAt(power))) * 10 ** power;
      if (rounded < 10 ** patterns[index + 1]!.power) break;
      index++;
    }
  }
  const { power, prefix, suffix } = patterns[index]!;
  const base = source.base;
  const unitText = /^[¥￥$€£]$/.test(base) ? base + suffix : suffix ? `${suffix}${/^[A-Za-z]/.test(base) ? " " : ""}${base}` : base;
  return { shift: source.exponent - power, unit: unitText.trim(), base, prefix, suffix, decimals: decimalsAt(power),
    ...(surface === "card" && precision?.decimal_mode === "fixed" ? { explicit: true, fixed: true } : {}) };
}

/** Card/permanent-label magnitude depends on the value, never another observation. */
export function resolveDisplayMagnitude(values: readonly unknown[], unit?: string, display?: NumericDisplay, format?: ChartColumnFormat): DisplayMagnitude {
  const finite = values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  return magnitudeContext(finite.reduce((max, v) => Math.max(max, Math.abs(v)), 0), unit, "card", display, format);
}

/** Axis magnitude depends on the plotted domain, including supplied geometry endpoints. */
export function resolveAxisMagnitude(values: readonly unknown[], unit?: string): DisplayMagnitude {
  const finite = values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const low = finite.length ? Math.min(...finite) : 0;
  const high = finite.length ? Math.max(...finite) : 0;
  return magnitudeContext(high - low, unit, "axis");
}

/** Retain decimal tick positions, not merely different strings (2.5 must stay 2.5). */
export function formatAxisTick(value: number, display: DisplayMagnitude, ticks: readonly number[]): string {
  const scaled = ticks.filter(Number.isFinite).map((v) => v * 10 ** display.shift);
  let decimals = display.decimals;
  for (const tick of scaled) {
    for (let places = decimals; places <= 12; places++) {
      const rounded = Number(tick.toFixed(places));
      const tolerance = Number.EPSILON * Math.abs(tick) * 4;
      if (Math.abs(rounded - tick) <= tolerance) { decimals = places; break; }
      if (places === 12) decimals = 12;
    }
  }
  if (decimals > 6) {
    const scaledValue = value * 10 ** display.shift;
    return readoutFormatter({ notation: "scientific", maximumSignificantDigits: 15 }).format(scaledValue === 0 ? 0 : scaledValue);
  }
  return formatDisplayValue(value, { ...display, decimals, explicit: true }, false);
}

/** Format an explicit magnitude; axis ticks omit its separately stated unit. */
export function formatDisplayValue(value: number, display: DisplayMagnitude, includeUnit = true): string {
  if (!Number.isFinite(value)) return "—";
  const digits = ordinaryDigits(value, display.shift, display.decimals, display.explicit, display.fixed);
  if (!includeUnit) return digits;
  return withLocalizedUnit(`${display.prefix}${digits}${display.suffix}`, display.base);
}

/** Remove only an explicit unit qualifier when the readout already states its unit. */
export function measureDisplayName(column: ChartColumn): string {
  const name = column.measure_header ?? column.header;
  const unit = axisUnitOf(column);
  if (!unit) return name;
  for (const suffix of [`_${unit}`, `（${unit}）`, `(${unit})`, ` / ${unit}`]) {
    if (name.endsWith(suffix)) return name.slice(0, -suffix.length).trim();
  }
  return name;
}

function withUnit(s: string, unit?: string): string {
  if (!unit) return s;
  if (DIMENSIONLESS_UNITS.has(unit.toLowerCase())) return s;
  // Currency units prefix; others suffix
  if (unit === "¥" || unit === "$" || unit === "€") return `${unit}${s}`;
  return `${s}${unit}`;
}

// ─────────────────────────────────────────────────────────────
// Batch 0 0-E (chart-table-ux tech-design §1.5): three modes
//   Precision = table cells / KPI main value / tooltip / export — grouping + unit, never compact
//   Compact = ticks / in-chart labels — existing abbrChinese
//   Narrative = body prose (model-written, outside this module)
// Production incident evidence: KPI rendered profit margin "-0.03", net sales "3M"
// （cv_55d680eda3f7，discovery §4.7 E-3）。
// ─────────────────────────────────────────────────────────────

/** Shape is a source fact, never a name/range guess. Historical format=percent
 * alone does not specify fraction versus points; retain the prepared number. */
export type PercentScale = "fraction" | "points";
export function resolvePercentColumn(col: Pick<Partial<ChartColumn>, "format" | "unit" | "percent_scale" | "percent_shape_unknown" | "header" | "source_header">): boolean {
  if (col.format === "currency" || col.format === "datetime") return false;
  return col.percent_scale === "fraction" || col.percent_scale === "points" || col.unit === "%";
}

export function resolvePercentScale(
  _values: ReadonlyArray<number | string | null | undefined>,
  col: Pick<Partial<ChartColumn>, "header" | "unit" | "percent_scale" | "source_header"> | null,
): PercentScale {
  return col?.percent_scale === "fraction" ? "fraction" : "points";
}

export function formatPercentCell(value: number | null | undefined, scale: PercentScale = "points", display?: NumericDisplay): string {
  return formatObservedPercentCell(value, scale, display);
}

export function formatObservedPercentCell(value: number | string | null | undefined, scale: PercentScale, display?: NumericDisplay): string {
  const precision = validNumericDisplay(display);
  if (isUnsafeIntegerCell(value)) {
    const digits = readoutFormatter(precision ? precisionOptions(precision) : {}).format(BigInt(value as string) * (scale === "fraction" ? 100n : 1n));
    return withLocalizedUnit(digits, "%");
  }
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return withLocalizedUnit(ordinaryDigits(value, scale === "fraction" ? 2 : 0, precision?.decimal_places ?? 2, !!precision, precision?.decimal_mode === "fixed"), "%");
}

/** Compatibility entry point for ordinary, unabridged readouts. */
export function formatPrecise(value: number | null | undefined, unit?: string, format?: ChartColumnFormat): string {
  return formatObservedValue(value, unit, format);
}

/** Units a table cell repeats after every value: per-mille-style marks that read as part of the number. */
const INLINE_CELL_UNITS: ReadonlySet<string> = new Set(["%", "‰", "‱"]);

/** Unit a table cell repeats inline, or `undefined` when the unit belongs in the column header instead.
 *
 *  Table convention names the unit once in the column header (`销售额（亿元）`, `Revenue (USD)`), so only
 *  percentage-style marks stay after each value. Every other unit — currency symbols and codes, magnitude words
 *  (`亿元`, `万元`), physical units, or a model-written phrase such as `工作区原始金额单位` that was glued to every
 *  number and clipped mid-word (`170,789,558.1701工作区原始金额单`) — moves to the header via
 *  `tableHeaderWithUnit`. The unit's wording is never judged. */
export function inlineCellUnit(unit?: string): string | undefined {
  const trimmed = unit?.trim();
  return trimmed && INLINE_CELL_UNITS.has(trimmed) ? trimmed : undefined;
}

/** Column header for a table: appends a unit that `inlineCellUnit` keeps out of the cells, once, unless the header
 *  already names it (`营业额（万元）`). CJK headers use full-width brackets: `销售金额（亿元）`, `Revenue (USD)`. */
export function tableHeaderWithUnit(header: string, unit?: string): string {
  const source = unit?.trim();
  const trimmed = monetaryMagnitude(source)?.base ?? source;
  if (source && trimmed !== source) {
    for (const suffix of [`_${source}`, `（${source}）`, `(${source})`, ` / ${source}`]) {
      if (header.endsWith(suffix)) { header = header.slice(0, -suffix.length).trim(); break; }
    }
  }
  if (!trimmed || !isDisplayableUnit(trimmed) || inlineCellUnit(trimmed) || header.includes(trimmed)) return header;
  return /[㐀-鿿豈-﫿]/.test(header) ? `${header}（${trimmed}）` : `${header} (${trimmed})`;
}

/** KPI swap threshold: from 1 million upward the **approximate magnitude becomes the main line** (≈856万) and the exact
 *  value moves to the secondary mono line (8,559,795). Below it the exact value stays the main line, with no secondary line.
 *
 *  Why exactly here (design "KPI 主值截断 - 四条出路", option 1c): Inter 600/22px tabular digits are 14.0px, a comma
 *  5.7px; the narrowest KPI track (120px minus padding and border) leaves 90px, and the longest value that fits is
 *  `999,999` (89.8px). One million is therefore the first magnitude that can no longer fit, so the old ≈ secondary-line
 *  threshold is reused rather than adding a second criterion. The exact value is moved to a separate line instead
 *  of being clipped behind a neighbouring card, where a partial number could look complete. */
export const KPI_APPROX_THRESHOLD = 1_000_000;

/** Approximate magnitude for a KPI main line: compact notation with one decimal, **dropping the decimal once the
 *  mantissa has three or more integer digits** (`≈6396万`, `≈165亿`, `≈744万`, but `≈10.6亿`, `≈1.6亿`).
 *
 *  The main line is a reading aid (the exact value sits right below it), so four significant digits are enough; the extra
 *  decimal only pushed `≈6396.1万` (111.7px) past the 105px a canvas KPI track offers. */
export function kpiMagnitude(v: number): string {
  const locale = getCurrentLocale();
  const compact = (decimals: number) => _formatLocaleNumber(v, locale, { compact: true, decimals });
  const oneDecimal = compact(1);
  let integerDigits = 0;
  try {
    integerDigits = new Intl.NumberFormat(locale, {
      notation: "compact",
      compactDisplay: "short",
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    })
      .formatToParts(v)
      .filter((part) => part.type === "integer")
      .reduce((n, part) => n + part.value.length, 0);
  } catch {
    return oneDecimal;
  }
  return integerDigits >= 3 ? compact(0) : oneDecimal;
}

// Units that already carry a magnitude: `1,500,000 万元` is 1.5e10 yuan. Compact notation on such a value stacks a
// second magnitude on the first (`≈150万` reads as 1.5e6 — off by 10⁴), so these values are never swapped.
// ⚠ Deliberately generous (`m` also means metre): a false hit only keeps the exact value as the main line, while a
// miss prints a doubled magnitude (`≈150万（万元）`). Hence: any position, brackets ignored, simplified and traditional.
const MAGNITUDE_CJK = /[万萬亿億]|千(?:元|美元|欧元|港元|人民币|$)|^\s*[(（]?\s*千\s*[)）]?\s*$/;
const MAGNITUDE_TOKENS = new Set([
  "k", "m", "mn", "mm", "mio", "b", "bn", "mrd", "md", "tsd", "thousand", "thousands", "million", "millions",
  "billion", "billions",
]);
// Currency codes glued to a multiplier: `KUSD`, `TEUR`, `MRMB`, `mnUSD`.
const MAGNITUDE_CURRENCY_TOKEN = /^(?:k|t|m|mn|b|bn)(?:usd|eur|rmb|cny|hkd|gbp|jpy|chf)$/;

function textCarriesMagnitude(text: string): boolean {
  if (!text.trim()) return false;
  if (MAGNITUDE_CJK.test(text)) return true;
  return text
    .toLowerCase()
    .split(/[^a-z]+/)
    .some((token) => MAGNITUDE_TOKENS.has(token) || MAGNITUDE_CURRENCY_TOKEN.test(token));
}

/** The qualifier parts of a header that name its unit: a trailing bracket (`营业额(万元)`, `Revenue ($M)`), or the
 *  part after the last `/` or `_` (`营业额 / 万元`, `营业额_亿元`). Never the whole header: `万达广场营业额` is a name. */
function headerUnitQualifiers(header: string): string[] {
  const out: string[] = [];
  const bracket = /[(（]([^()（）]*)[)）]\s*$/.exec(header);
  if (bracket) out.push(bracket[1]!);
  for (const sep of ["/", "_"]) {
    const at = header.lastIndexOf(sep);
    if (at >= 0) out.push(header.slice(at + 1));
  }
  return out;
}

/** Does this value already carry a magnitude — in its unit (万元, （万元）, 萬元, USD mn, KUSD …) or, when the unit says
 *  nothing, in its header's unit qualifier (`营业额(万元)`, `Revenue ($M)`)? */
export function unitCarriesMagnitude(unit?: string, header?: string): boolean {
  if (unit && textCarriesMagnitude(unit)) return true;
  return headerUnitQualifiers(header ?? "").some(textCarriesMagnitude);
}

/** KPI main line from one million up: `≈` + compact magnitude, **with the column's unit** (`≈¥856万`, `≈856万人次`).
 *  Without the unit the line lost the currency/quantity it measures; currency signs lead (`≈-¥744万`, like
 *  `formatPrecise`). Dimensionless placeholder units are dropped (`withUnit`). */
export function kpiMagnitudeLine(v: number, unit?: string): string {
  const isCurrencySymbol = unit === "¥" || unit === "$" || unit === "€";
  if (isCurrencySymbol && v < 0) return `≈-${unit}${kpiMagnitude(-v)}`;
  return `≈${withUnit(kpiMagnitude(v), unit)}`;
}

/** Unabridged KPI secondary readout uses ordinary column precision and locale grouping. */
export function kpiExactLine(v: number, unit?: string, format?: ChartColumnFormat): string {
  return formatObservedValue(v, unit, format);
}

/**
 * Main and exact lines of one KPI / stat-tile value — the single rule shared by report KPI cards (`ChartKPI`) and page
 * stat tiles (`StatTiles`). `display` is the value already formatted for its column (precise number, percent).
 * From `KPI_APPROX_THRESHOLD` up (option 1c) the magnitude becomes the main line and the exact value the secondary
 * line; never for percentages or when the unit already carries a magnitude (`万元`, `Mn`).
 */
export function kpiValueLines(
  num: number | null,
  display: string,
  { unit, isPercent = false, header, format, display: precision, currency_code }: { unit?: string; isPercent?: boolean; header?: string; format?: ChartColumnFormat; display?: NumericDisplay; currency_code?: string } = {},
): { main: string; exact: string | null; swapped: boolean; ems: number } {
  const sourceUnit = unit ?? headerUnitOf(header);
  if (isPercent || num === null) return { main: display, exact: null, swapped: false, ems: kpiValueEms(display) };
  const magnitude = resolveDisplayMagnitude([num], sourceUnit, precision, format);
  const main = formatDisplayValue(num, magnitude);
  const exact = formatObservedValue(num, sourceUnit, format, true, precision, currency_code);
  return { main, exact: main === exact ? null : exact, swapped: main !== exact, ems: kpiValueEms(main) };
}

/** Conservative width of a KPI main-line string in **em** at Inter 600 / tabular-nums.
 *
 *  Feeds the CSS fit fallback (`--kpi-ems`, see `.v2-chart-kpi-value` in globals.css): the font only shrinks when the
 *  estimate says the string cannot fit its cell, so values that already fit keep 22px and the layout is unchanged.
 *  It exists for strings the swap cannot shorten enough — German `≈16,5 Mrd.` is 117.8px against 105px — and for exact
 *  values below the swap threshold that carry a currency prefix (`¥999,999`).
 *
 *  Character classes rather than a character count (comma 5.7px vs digit 14.0px vs CJK 22px at 22px font), and every
 *  class rounds **up**, so an estimate never under-reports: under-reporting is the overflow this fallback prevents;
 *  over-reporting only costs a fraction of a pixel of font size. Widths measured in Chromium and WebKit. */
export function kpiValueEms(text: string): number {
  let ems = 0;
  for (const ch of text) {
    if (/[0-9]/.test(ch)) ems += 0.64;
    else if (ch === "," || ch === "." || ch === "'") ems += 0.3;
    else if (ch === " " || ch === " " || ch === " ") ems += 0.3;
    else if (ch === "-" || ch === "−" || ch === "+" || ch === "≈") ems += 0.66;
    else if (ch === "%") ems += 0.9;
    else if (/[A-Z]/.test(ch)) ems += 0.86;
    else if (/[fijlrt]/.test(ch)) ems += 0.42;
    else if (/[a-z]/.test(ch)) ems += 0.64;
    else if (ch.charCodeAt(0) > 0x2e80) ems += 1.02;
    else ems += 0.72;
  }
  return Math.round(ems * 100) / 100;
}

const _EN_MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

/** "2025年4月" / "2025-04-30" / "2025-Q2" → brief display; en `locale` converts Chinese time buckets to English.
 *
 * Chinese-formatted time buckets such as `2026年1月 / 2026年2月` are returned as
 * **data values**; this function previously recognized already-friendly format and passed through unchanged.
 *
 * Why translation allowed here, unlike dimensions/products: time buckets form **closed set** with mechanical rules
 * (`2026年1月` ⇄ `Jan 2026` denote same fact); translation neither loses information nor fabricates.
 * Product `诺易1000IU` lacks English equivalent and datarc supplies none — preserve original.
 */
export function formatDateLabel(raw: string, locale: string = getCurrentLocale()): string {
  if (!raw) return "";
  if (/^\d{4}-(?:\d{2}(?:-\d{2})?|Q[1-4])$/.test(raw)) return raw;
  const cell = calendarCell(raw);
  if (cell) {
    const timed = cell.grain === "minute" || cell.grain === "second";
    const formatted = new Intl.DateTimeFormat(locale, { timeZone: "UTC", year: "numeric", month: "short",
      ...(cell.grain !== "month" ? { day: "numeric" } : {}),
      ...(timed ? { hour: "numeric", minute: "2-digit" } : {}),
      ...(cell.grain === "second" ? { second: "2-digit" } : {}),
    }).format(cell.date);
    return cell.offset ? `${formatted} ${cell.offset}` : formatted;
  }
  const quarter = /^(\d{4})(?:年第?([1-4])季度|-Q([1-4]))$/.exec(raw);
  if (quarter) return locale.startsWith("zh") ? `${quarter[1]}年第${quarter[2] ?? quarter[3]}季度` : `Q${quarter[2] ?? quarter[3]} ${quarter[1]}`;
  const year = /^(\d{4})年$/.exec(raw);
  return year && !locale.startsWith("zh") ? year[1]! : raw;
}

/** Time-**grain** dimension names (headers/legends). datarc supplies no en-US labels for these derived dimensions,
 * fixed vocabulary unlike product names requiring external knowledge; built-in mapping does not fabricate. */
const _GRAIN_EN: Record<string, string> = {
  每日: "Daily",
  每天: "Daily",
  每周: "Weekly",
  每月: "Monthly",
  每季度: "Quarterly",
  每年: "Yearly",
  年份: "Year",
  月份: "Month",
  季度: "Quarter",
  日期: "Date",
};

/** Localize time-grain dimension display name; return original outside vocabulary (never fabricate). */
export function formatGrainLabel(raw: string, locale?: string): string {
  if (!raw || !locale || locale.toLowerCase().startsWith("zh")) return raw;
  return _GRAIN_EN[raw.trim()] ?? raw;
}
