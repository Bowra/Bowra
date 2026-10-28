import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import GlobalError from "./global-error";

describe("GlobalError <html lang>", () => {
  let originalBuildLocale: string | undefined;

  beforeEach(() => {
    originalBuildLocale = process.env.NEXT_PUBLIC_BUILD_LOCALE;
  });

  afterEach(() => {
    if (originalBuildLocale === undefined) {
      delete process.env.NEXT_PUBLIC_BUILD_LOCALE;
    } else {
      process.env.NEXT_PUBLIC_BUILD_LOCALE = originalBuildLocale;
    }
  });

  it.each([
    ["zh-CN", "zh-CN"],
    ["en-US", "en-US"],
    ["de-DE", "en-US"],
    ["fr-FR", "en-US"],
  ])("%s build writes lang=%s, matching the language of the page copy", (buildLocale, expected) => {
    // Error-page copy exists only in Chinese and English; other builds fall back to English. lang matches the copy language.
    process.env.NEXT_PUBLIC_BUILD_LOCALE = buildLocale;

    const html = renderToStaticMarkup(<GlobalError error={new Error("boom")} reset={() => undefined} />);

    expect(html.startsWith(`<html lang="${expected}">`)).toBe(true);
  });
});
