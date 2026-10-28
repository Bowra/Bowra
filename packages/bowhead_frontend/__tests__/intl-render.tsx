// Shared test helper: render a component tree inside a real NextIntlClientProvider
// loaded with the actual zh-CN message catalog, so useTranslations()/t() resolve to
// real strings (default locale = zh-CN, the app default). Use this for component tests
// that exercise components which call useTranslations — instead of mocking next-intl.
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import zhMessages from "@/messages/zh-CN.json";
import enMessages from "@/messages/en-US.json";
import deMessages from "@/messages/de-DE.json";
import frMessages from "@/messages/fr-FR.json";

const CATALOGS = {
  "zh-CN": zhMessages,
  "en-US": enMessages,
  "de-DE": deMessages,
  "fr-FR": frMessages,
} as const;

export function renderIntl(
  ui: ReactElement,
  opts: { locale?: keyof typeof CATALOGS } = {},
) {
  const locale = opts.locale ?? "zh-CN";
  return render(
    <NextIntlClientProvider locale={locale} messages={CATALOGS[locale]}>
      {ui}
    </NextIntlClientProvider>,
  );
}
