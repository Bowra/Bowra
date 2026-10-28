/**
 * Language names shared by every language menu (Header, Language & Paging Spec D1): the account menu,
 * Settings › Account and the public pages list the same options in the same fixed order, each written in its
 * own language and never translated with the interface. This module has no API imports so the public
 * `(public)` layout can use it without pulling authenticated code.
 */

import type { Locale } from "./locale";

/** Fixed option order, independent of the current interface language. */
export const LOCALE_ORDER: readonly Locale[] = ["zh-CN", "en-US", "de-DE", "fr-FR"];

/** Each language's own name ("中文", not "中文（简体）"). */
export const LOCALE_NATIVE_NAME: Record<Locale, string> = {
  "zh-CN": "中文",
  "en-US": "English",
  "de-DE": "Deutsch",
  "fr-FR": "Français",
};

/** One year, the lifetime the Gateway also uses for `NEXT_LOCALE`. */
export const LOCALE_COOKIE_MAX_AGE_SEC = 60 * 60 * 24 * 365;

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALE_ORDER as readonly string[]).includes(value);
}

/** Write the interface-language cookie read by the Gateway's static-build selection and the client provider. */
export function writeLocaleCookie(locale: Locale): void {
  document.cookie = `NEXT_LOCALE=${locale}; path=/; max-age=${LOCALE_COOKIE_MAX_AGE_SEC}; SameSite=Lax`;
}

/** The concrete interface language a cookie pins, or null when the interface follows the browser. */
export function readLocaleCookie(): Locale | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(/(?:^|;\s*)NEXT_LOCALE=([^;]+)/);
  return match && isLocale(match[1]) ? match[1] : null;
}

/** Remove the interface-language cookie so the next load follows the browser language again. */
export function clearLocaleCookie(): void {
  document.cookie = "NEXT_LOCALE=; path=/; max-age=0; SameSite=Lax";
}
