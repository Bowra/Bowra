/**
 * i18n v1 · locale-aware number/date formatting (frontend Intl.* wrappers)
 *
 * Counterpart to backend bowhead_core/format_locale.py; both use CLDR data (Intl native in browser,
 * babel Python CLDR), ensuring consistent formatting across ends.
 *
 * Locale strings: BCP 47 ("zh-CN" / "en-US"), natively supported by Intl, no conversion needed.
 *
 * Use for:
 * - Any UI displaying numbers/dates/currency (chat, files, canvas chart axis labels)
 * - Never hardcode `toLocaleString("zh-CN")` or call `toLocale*String()` / `Intl.DateTimeFormat` without the
 *   interface locale: pass next-intl `useLocale()` in components, or `getCurrentLocale()` outside React.
 *
 * Dates and times shown to users go through the formatters in this module ({@link formatTime},
 * {@link formatDate}, {@link formatDateTime}, {@link formatShortDateTime}, {@link formatMessageTime},
 * {@link formatListDate}, {@link formatCalendarDay}), so every surface follows one clock rule
 * ({@link clockOptions}): English reads a 12-hour clock ("6:39 PM"); Chinese, German and French read 24-hour
 * time ("18:39"). Machine formats (ISO strings, file names, logs, form input values) stay outside it.
 */

export type Locale = "zh-CN" | "en-US" | "de-DE" | "fr-FR";

/** Default language for product copy and builds without an explicit locale. */
export const DEFAULT_LOCALE: Locale = "en-US";

/** Currently supported locales. */
export const SUPPORTED_LOCALES: readonly Locale[] = ["zh-CN", "en-US", "de-DE", "fr-FR"] as const;

/** Locale baked into a static build; unknown or unset values use the product default. */
export function buildLocale(): Locale {
  return (
    SUPPORTED_LOCALES.find(
      (locale) => locale === process.env.NEXT_PUBLIC_BUILD_LOCALE,
    ) ?? DEFAULT_LOCALE
  );
}

/** Resolve the browser's preferred supported language, with English fallback. */
export function browserLocale(): Locale {
  if (typeof navigator === "undefined") return DEFAULT_LOCALE;
  for (const language of [navigator.language, ...(navigator.languages ?? [])]) {
    const primary = language.toLowerCase().replace("_", "-").split("-", 1)[0];
    const locale = SUPPORTED_LOCALES.find((candidate) => candidate.split("-", 1)[0] === primary);
    if (locale) return locale;
  }
  return DEFAULT_LOCALE;
}

const BUILD_LOCALE_DESCRIPTIONS: Record<Locale, string> = {
  "zh-CN": "面向企业数据决策的 Verified Intelligence",
  "en-US": "Verified Intelligence for enterprise data decisions",
  "de-DE": "Verified Intelligence für fundierte datenbasierte Entscheidungen in Unternehmen",
  "fr-FR": "Verified Intelligence pour les décisions fondées sur les données en entreprise",
};

/** Localized metadata shared by the public and authenticated root layouts. */
export function buildLocaleDescription(locale: Locale = buildLocale()): string {
  return BUILD_LOCALE_DESCRIPTIONS[locale];
}

export interface FormatNumberOptions {
  /** true → short compact notation: zh "1.2万" / en "12K". */
  compact?: boolean;
  /** Decimal places; omitted uses locale defaults (Chinese/English both up to 3). */
  decimals?: number;
}

/**
 * Format an ordinary number.
 *
 * @example
 *   formatNumber(12345, "zh-CN")                  // "12,345"
 *   formatNumber(12345, "zh-CN", {compact: true}) // "1.2万"
 *   formatNumber(12345, "en-US", {compact: true}) // "12K"
 *   formatNumber(1234.5678, "en-US", {decimals: 2}) // "1,234.57"
 */
export function formatNumber(
  value: number,
  locale: Locale = DEFAULT_LOCALE,
  opts: FormatNumberOptions = {},
): string {
  const fmtOpts: Intl.NumberFormatOptions = {};
  if (opts.compact) {
    fmtOpts.notation = "compact";
    fmtOpts.compactDisplay = "short";
  }
  if (opts.decimals !== undefined) {
    fmtOpts.minimumFractionDigits = opts.decimals;
    fmtOpts.maximumFractionDigits = opts.decimals;
  }
  return new Intl.NumberFormat(locale, fmtOpts).format(value);
}

/**
 * Format a percentage.
 *
 * @example
 *   formatPercent(0.123, "zh-CN", 1) // "12.3%"
 *   formatPercent(0.5, "en-US", 0)   // "50%"
 */
export function formatPercent(
  value: number,
  locale: Locale = DEFAULT_LOCALE,
  decimals: number = 1,
): string {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

/**
 * Format currency.
 *
 * @example
 *   formatCurrency(1234.56, "CNY", "zh-CN") // "¥1,234.56"
 *   formatCurrency(1234.56, "USD", "en-US") // "$1,234.56"
 *   formatCurrency(1234.56, "EUR", "de-DE") // "1.234,56 €"
 */
export function formatCurrency(
  value: number,
  currency: string,
  locale: Locale = DEFAULT_LOCALE,
): string {
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
  }).format(value);
}

/** A displayable instant: a Date, an ISO-8601 string or epoch milliseconds. Missing values format as "". */
export type DateInput = Date | string | number | null | undefined;

/** The instant `value` names, or null when it is missing or unparseable. */
function toValidDate(value: DateInput): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Interface languages whose everyday clock is 12-hour; Chinese, German and French read 24-hour time. */
const TWELVE_HOUR_LANGUAGES: ReadonlySet<string> = new Set(["en"]);

/**
 * Hour and minute options in the locale's own clock convention, the one clock rule for every displayed time:
 * en-US "6:39 PM" / "8:05 AM" (12-hour, no leading zero); zh-CN, de-DE and fr-FR "18:39" / "08:05" (24-hour).
 * The cycle is explicit so a browser or OS clock preference cannot make one surface differ from another.
 */
export function clockOptions(locale: string, { seconds = false }: { seconds?: boolean } = {}): Intl.DateTimeFormatOptions {
  const twelveHour = TWELVE_HOUR_LANGUAGES.has(locale.split("-", 1)[0].toLowerCase());
  return {
    hour: twelveHour ? "numeric" : "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" as const } : null),
    hourCycle: twelveHour ? "h12" : "h23",
  };
}

/** Time of day only ("6:39 PM" / "18:39"; with `seconds`, "6:39:05 PM" / "18:39:05"). "" when missing or invalid. */
export function formatTime(
  value: DateInput,
  locale: string,
  opts: { seconds?: boolean; timeZone?: string } = {},
): string {
  const date = toValidDate(value);
  if (!date) return "";
  return new Intl.DateTimeFormat(locale, { ...clockOptions(locale, opts), timeZone: opts.timeZone }).format(date);
}

/**
 * Format a date without a time ("Nov 8, 2026" / "2026年11月8日" for "medium"). "" when missing or invalid.
 *
 * @param style "short" | "medium" | "long" | "full"
 */
export function formatDate(
  value: DateInput,
  locale: string = DEFAULT_LOCALE,
  style: "short" | "medium" | "long" | "full" = "medium",
  timeZone?: string,
): string {
  const date = toValidDate(value);
  if (!date) return "";
  return new Intl.DateTimeFormat(locale, { dateStyle: style, timeZone }).format(date);
}

/**
 * Full date and time ("Nov 8, 2026, 6:39 PM" / "2026年11月8日 18:39" for a "medium" date). The time part follows the
 * locale's clock (see {@link clockOptions}). "" when missing or invalid.
 */
export function formatDateTime(
  value: DateInput,
  locale: string = DEFAULT_LOCALE,
  opts: {
    dateStyle?: "short" | "medium" | "long" | "full";
    timeStyle?: "short" | "medium" | "long" | "full";
    timeZone?: string;
  } = {},
): string {
  const date = toValidDate(value);
  if (!date) return "";
  return new Intl.DateTimeFormat(locale, {
    dateStyle: opts.dateStyle ?? "short",
    timeStyle: opts.timeStyle ?? "short",
    timeZone: opts.timeZone,
  }).format(date);
}

/**
 * Short date and time for lists, cards and details (P2 rule): "9月26日 14:23" / "Sep 26, 2:23 PM", with the year only
 * when it differs from the current one. Empty string for a missing or unparseable value.
 */
export function formatShortDateTime(value: DateInput, locale: string, now: Date = new Date(), timeZone?: string): string {
  const date = toValidDate(value);
  if (!date) return "";
  return new Intl.DateTimeFormat(locale, {
    ...(calendarDay(date, timeZone).year !== calendarDay(now, timeZone).year ? { year: "numeric" } : null),
    month: "short",
    day: "numeric",
    ...clockOptions(locale),
    timeZone,
  }).format(date);
}

/**
 * Chat byline time: the time alone on the viewer's current calendar day ("6:39 PM" / "18:39"); an earlier day adds a
 * short date in the locale's order ("Nov 8, 6:39 PM" / "11月8日 18:39" / "8. Nov., 18:39" / "8 nov., 18:39"); another
 * year adds the year. A value slightly ahead of `now` (server/browser clock skew) still reads as today.
 * "" when missing or invalid.
 */
export function formatMessageTime(
  value: DateInput,
  locale: string,
  { now = new Date(), timeZone }: { now?: Date; timeZone?: string } = {},
): string {
  const date = toValidDate(value);
  if (!date) return "";
  if (calendarDay(date, timeZone).day >= calendarDay(now, timeZone).day) {
    return formatTime(date, locale, { timeZone });
  }
  return formatShortDateTime(date, locale, now, timeZone);
}

/** Relative-time copy for {@link formatListDate} (the caller's existing `rel_*` messages). */
export interface ListDateCopy {
  justNow: () => string;
  minutesAgo: (count: number) => string;
  hoursAgo: (count: number) => string;
  yesterday: () => string;
  daysAgo: (count: number) => string;
}

export interface ListDate {
  /** Visible text: relative within 7 days, then "Sep 28" / "9月28日", with the year only for other years. */
  label: string;
  /** Full date and time for `title` (hover / long press), e.g. "Sep 28, 2026, 2:23 PM" / "2026年9月28日 14:23". */
  title: string;
  /** Machine-readable value for `<time dateTime>`. */
  dateTime: string;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
/** Server/browser clocks drift; a timestamp up to this far in the future still reads "just now". */
const CLOCK_SKEW_MS = 5 * MINUTE_MS;

/** Calendar day (year, month, day) of `value` in `timeZone`, as a day number for differences. */
function calendarDay(value: Date, timeZone: string | undefined): { day: number; year: number } {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone, year: "numeric", month: "numeric", day: "numeric" }).formatToParts(value);
  const part = (type: string) => Number(parts.find((entry) => entry.type === type)?.value);
  const year = part("year");
  return { day: Date.UTC(year, part("month") - 1, part("day")) / (24 * HOUR_MS), year };
}

/**
 * One date rule for lists (Agents & Reports polish spec §3a, D8): under a minute "just now" (as is a timestamp
 * slightly in the future, clock skew), under an hour minutes, earlier today hours, the previous calendar day
 * "yesterday", 2–6 days "N days ago"; from 7 days on an absolute month and day, adding the year only when it differs
 * from the current one. Calendar days and the absolute text use `timeZone` — pass next-intl `useTimeZone()`, which
 * is the viewer's browser zone (see `lib/time-zone.ts`). Returns null for a missing or unparseable value.
 */
export function formatListDate(
  value: Date | string | null | undefined,
  locale: string,
  copy: ListDateCopy,
  now: Date = new Date(),
  timeZone?: string,
): ListDate | null {
  if (!value) return null;
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return null;
  const title = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone }).format(date);
  const dateTime = date.toISOString();
  const elapsed = now.getTime() - date.getTime();
  const today = calendarDay(now, timeZone);
  const then = calendarDay(date, timeZone);
  const dayDelta = today.day - then.day;
  let label: string;
  if (elapsed > -CLOCK_SKEW_MS && elapsed < MINUTE_MS) label = copy.justNow();
  else if (elapsed >= 0 && elapsed < HOUR_MS) label = copy.minutesAgo(Math.floor(elapsed / MINUTE_MS));
  else if (elapsed >= 0 && dayDelta === 0) label = copy.hoursAgo(Math.max(1, Math.floor(elapsed / HOUR_MS)));
  else if (dayDelta === 1) label = copy.yesterday();
  else if (dayDelta >= 2 && dayDelta < 7) label = copy.daysAgo(dayDelta);
  else {
    label = new Intl.DateTimeFormat(locale, {
      ...(then.year !== today.year ? { year: "numeric" } : null),
      month: "short",
      day: "numeric",
      timeZone,
    }).format(date);
  }
  return { label, title, dateTime };
}

const CALENDAR_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * A server calendar day ("2026-10-01", already in the viewer's zone) in the list-date absolute form:
 * "Oct 1" / "10月1日", adding the year only when it differs from `now`'s year. Returns null for any other shape.
 */
export function formatCalendarDay(value: string, locale: string, now: Date = new Date(), timeZone?: string): string | null {
  const match = CALENDAR_DAY.exec(value.trim());
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const date = new Date(Date.UTC(year, month - 1, day, 12));
  if (Number.isNaN(date.getTime()) || date.getUTCDate() !== day) return null;
  const currentYear = calendarDay(now, timeZone).year;
  return new Intl.DateTimeFormat(locale, {
    ...(year !== currentYear ? { year: "numeric" } : null),
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

/**
 * Resolve the rendered UI locale, then explicit cookie/legacy preferences.
 * Missing/automatic preferences follow browser language, with English fallback.
 * Server rendering follows the selected static build.
 */
export function getCurrentLocale(): Locale {
  if (typeof window === "undefined") return buildLocale(); // Match the selected static build.
  // 1. <html lang>: written by Gateway+ClientLocaleProvider, consistent with next-intl useLocale().
  if (typeof document !== "undefined" && document.documentElement) {
    const htmlLang = document.documentElement.lang;
    if (htmlLang && (SUPPORTED_LOCALES as readonly string[]).includes(htmlLang)) {
      return htmlLang as Locale;
    }
  }
  // 2. NEXT_LOCALE cookie (LocaleSwitcher transition state).
  if (typeof document !== "undefined" && document.cookie) {
    const m = document.cookie.match(/(?:^|;\s*)NEXT_LOCALE=([^;]+)/);
    if (m && (SUPPORTED_LOCALES as readonly string[]).includes(m[1])) {
      return m[1] as Locale;
    }
  }
  // 3. localStorage (legacy; profile page + LocaleSwitcher write together).
  try {
    const raw = window.localStorage.getItem("bowhead.user_profile");
    if (raw) {
      const profile = JSON.parse(raw) as { language?: string };
      const lang = profile?.language;
      if (lang && (SUPPORTED_LOCALES as readonly string[]).includes(lang)) {
        return lang as Locale;
      }
      if (lang === "auto") return browserLocale();
    }
  } catch {
    // Unavailable preferences follow the browser language.
  }
  return browserLocale();
}
