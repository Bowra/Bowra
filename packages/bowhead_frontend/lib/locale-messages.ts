/**
 * Message catalogs for each interface language and synchronous translation for **non-React pure functions**.
 *
 * `ClientLocaleProvider` and this module read the same bundled catalog (this is its only import point),
 * without duplicate bundling. React components still use `useTranslations`; pure functions outside the React tree
 * (step-row tool names, short tool-result copy, etc.) use `staticTranslator()`, with language from `getCurrentLocale()`
 * —matching `<html lang>` and these modules' original language selection.
 */

// Import from use-intl/core (next-intl's createTranslator reexports it): many component tests use
// `vi.mock("next-intl", …)` to replace only useTranslations; importing via next-intl would be unavailable in those tests.
import { createTranslator, type AbstractIntlMessages } from "use-intl/core";

import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";
import zhMessages from "@/messages/zh-CN.json";
import { getCurrentLocale, type Locale } from "@/lib/locale";

export const LOCALE_MESSAGES = {
  "zh-CN": zhMessages,
  "en-US": enMessages,
  "de-DE": deMessages,
  "fr-FR": frMessages,
} as const;

export type StaticTranslateValues = Record<string, string | number>;

export interface StaticTranslator {
  (key: string, values?: StaticTranslateValues): string;
  /** Whether the key exists and is a message (not a namespace). */
  hasMessage(key: string): boolean;
}

const cache = new Map<string, StaticTranslator>();

/**
 * Synchronous namespace translator, defaulting to current interface language. Cache by (language, namespace),
 * reusing ICU compilation results.
 *
 * ⚠ For numeric interpolation, use plain `{n}` (unchanged, no thousands separator); also use `{n}` in plural branches rather than `#`
 * (`#` adds locale-specific thousands separators, changing existing output).
 */
export function staticTranslator(namespace: string, locale: Locale = getCurrentLocale()): StaticTranslator {
  const cacheKey = `${locale}\u0000${namespace}`;
  const hit = cache.get(cacheKey);
  if (hit) return hit;
  const t = createTranslator({
    locale,
    messages: LOCALE_MESSAGES[locale] as unknown as AbstractIntlMessages,
    namespace: namespace as never,
  }) as unknown as {
    (key: string, values?: StaticTranslateValues): string;
    raw(key: string): unknown;
    has(key: string): boolean;
  };
  const translate = ((key: string, values?: StaticTranslateValues) => t(key, values)) as StaticTranslator;
  translate.hasMessage = (key: string) => t.has(key) && typeof t.raw(key) === "string";
  cache.set(cacheKey, translate);
  return translate;
}
