"use client";

/**
 * Hydrate the locale selected by the gateway's static build.
 * Explicit cookies take precedence; without one, use browser/system language.
 * The first render matches the static build before applying browser preferences.
 * Times follow the viewer's browser time zone (UTC while hydrating static HTML).
 */

import { NextIntlClientProvider } from "next-intl";
import { useEffect, useState } from "react";

import { browserLocale, buildLocale, SUPPORTED_LOCALES, type Locale } from "@/lib/locale";
import { LOCALE_MESSAGES as MESSAGES } from "@/lib/locale-messages";
import { useViewerTimeZone } from "@/lib/time-zone";

// Build-time locale and fallback behavior are shared with every build-time consumer.
const BUILD_LOCALE: Locale = buildLocale();

function detectLocale(): Locale {
  if (typeof document === "undefined") return BUILD_LOCALE;
  // An explicit cookie records the user's language choice.
  const m = document.cookie.match(/(?:^|;\s*)NEXT_LOCALE=([^;]+)/);
  if (m && (SUPPORTED_LOCALES as readonly string[]).includes(m[1])) return m[1] as Locale;
  return browserLocale();
}

export function ClientLocaleProvider({ children }: { children: React.ReactNode }) {
  // Match the server-rendered locale before applying explicit preferences.
  const [locale, setLocale] = useState<Locale>(BUILD_LOCALE);
  const timeZone = useViewerTimeZone();

  useEffect(() => {
    const detected = detectLocale();
    // A language switch may update the cookie before the next reload.
    if (detected !== locale) {
      setLocale(detected);
      if (typeof document !== "undefined") {
        document.documentElement.lang = detected;
      }
    }
  }, [locale]);

  return (
    <NextIntlClientProvider
      locale={locale}
      messages={MESSAGES[locale]}
      timeZone={timeZone}
    >
      {children}
    </NextIntlClientProvider>
  );
}
