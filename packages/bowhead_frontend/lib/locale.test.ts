import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  buildLocale,
  buildLocaleDescription,
  browserLocale,
  clockOptions,
  DEFAULT_LOCALE,
  formatDate,
  formatDateTime,
  formatMessageTime,
  formatNumber,
  formatPercent,
  formatListDate,
  formatShortDateTime,
  formatTime,
  getCurrentLocale,
  SUPPORTED_LOCALES,
} from "./locale";

describe("buildLocale", () => {
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

  it.each(["zh-CN", "en-US", "de-DE", "fr-FR"] as const)(
    "returns the supported build locale %s",
    (locale) => {
      process.env.NEXT_PUBLIC_BUILD_LOCALE = locale;

      expect(buildLocale()).toBe(locale);
    },
  );

  it("falls back to the default locale when unset or unsupported", () => {
    delete process.env.NEXT_PUBLIC_BUILD_LOCALE;
    expect(buildLocale()).toBe("en-US");

    process.env.NEXT_PUBLIC_BUILD_LOCALE = "ja-JP";
    expect(buildLocale()).toBe(DEFAULT_LOCALE);
  });

  it("provides localized metadata descriptions", () => {
    expect(buildLocaleDescription("de-DE")).toContain("datenbasierte Entscheidungen");
    expect(buildLocaleDescription("de-DE")).not.toContain("面向企业");
    expect(buildLocaleDescription("fr-FR")).toBe(
      "Verified Intelligence pour les décisions fondées sur les données en entreprise",
    );
  });
});

describe("getCurrentLocale during server rendering", () => {
  const originalWindow = globalThis.window;
  const originalBuildLocale = process.env.NEXT_PUBLIC_BUILD_LOCALE;

  afterEach(() => {
    Object.defineProperty(globalThis, "window", { value: originalWindow, writable: true, configurable: true });
    if (originalBuildLocale === undefined) {
      delete process.env.NEXT_PUBLIC_BUILD_LOCALE;
    } else {
      process.env.NEXT_PUBLIC_BUILD_LOCALE = originalBuildLocale;
    }
  });

  it("uses the build locale when there is no window", () => {
    Object.defineProperty(globalThis, "window", { value: undefined, writable: true, configurable: true });
    process.env.NEXT_PUBLIC_BUILD_LOCALE = "de-DE";
    expect(getCurrentLocale()).toBe("de-DE");
    process.env.NEXT_PUBLIC_BUILD_LOCALE = "fr-FR";
    expect(getCurrentLocale()).toBe("fr-FR");
    delete process.env.NEXT_PUBLIC_BUILD_LOCALE;
    expect(getCurrentLocale()).toBe(DEFAULT_LOCALE);
  });
});

describe("getCurrentLocale in the browser", () => {
  const originalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");
  const preferences = new Map<string, string>();
  beforeEach(() => {
    document.documentElement.lang = "";
    document.cookie = "NEXT_LOCALE=; max-age=0; path=/";
    preferences.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => preferences.get(key) ?? null,
        setItem: (key: string, value: string) => preferences.set(key, value),
        removeItem: (key: string) => preferences.delete(key),
      },
    });
  });

  afterEach(() => {
    document.documentElement.lang = "";
    document.cookie = "NEXT_LOCALE=; max-age=0; path=/";
    Object.defineProperty(navigator, "language", { value: "en-US", configurable: true });
    if (originalStorage) Object.defineProperty(window, "localStorage", originalStorage);
  });

  it("reads French from the rendered page and an explicit language cookie", () => {
    document.documentElement.lang = "fr-FR";
    expect(getCurrentLocale()).toBe("fr-FR");

    document.documentElement.lang = "";
    document.cookie = "NEXT_LOCALE=fr-FR; path=/";
    expect(getCurrentLocale()).toBe("fr-FR");

    document.cookie = "NEXT_LOCALE=; max-age=0; path=/";
    Object.defineProperty(navigator, "language", { value: "fr-CA", configurable: true });
    expect(getCurrentLocale()).toBe("fr-FR");
    window.localStorage.setItem("bowhead.user_profile", JSON.stringify({ language: "auto" }));
    expect(getCurrentLocale()).toBe("fr-FR");
  });

  it("follows a Chinese browser while preserving explicit choices", () => {
    Object.defineProperty(navigator, "language", { value: "zh-CN", configurable: true });
    expect(getCurrentLocale()).toBe("zh-CN");

    document.cookie = "NEXT_LOCALE=en-US; path=/";
    expect(getCurrentLocale()).toBe("en-US");

    document.cookie = "NEXT_LOCALE=zh-CN; path=/";
    expect(getCurrentLocale()).toBe("zh-CN");

    document.cookie = "NEXT_LOCALE=; max-age=0; path=/";
    window.localStorage.setItem("bowhead.user_profile", JSON.stringify({ language: "zh-CN" }));
    expect(getCurrentLocale()).toBe("zh-CN");
  });
});

describe("French number and date formatting", () => {
  it("formats with the French CLDR conventions, not the Chinese or English ones", () => {
    // fr-FR uses narrow nonbreaking spaces for thousands and commas for decimals; dates follow day month year.
    expect(formatNumber(1234.5, "fr-FR", { decimals: 1 })).toBe("1\u202f234,5");
    expect(formatPercent(0.125, "fr-FR", 1)).toBe("12,5\u00a0%");
    expect(formatDate("2026-09-30T12:00:00Z", "fr-FR", "long")).toBe("30 septembre 2026");
  });
});


describe("browserLocale", () => {
  const originalLanguage = navigator.language;
  const originalLanguages = navigator.languages;
  afterEach(() => {
    Object.defineProperty(navigator, "language", { value: originalLanguage, configurable: true });
    Object.defineProperty(navigator, "languages", { value: originalLanguages, configurable: true });
  });
  it.each([["zh-TW", "zh-CN"], ["en-GB", "en-US"], ["de-AT", "de-DE"], ["fr-CA", "fr-FR"]])(
    "maps %s to %s", (language, expected) => {
      Object.defineProperty(navigator, "language", { value: language, configurable: true });
      expect(browserLocale()).toBe(expected);
    },
  );
  it("uses the next supported preference and then English fallback", () => {
    Object.defineProperty(navigator, "language", { value: "ja-JP", configurable: true });
    Object.defineProperty(navigator, "languages", { value: ["ja-JP", "fr-CA"], configurable: true });
    expect(browserLocale()).toBe("fr-FR");
    Object.defineProperty(navigator, "languages", { value: ["ja-JP"], configurable: true });
    expect(browserLocale()).toBe("en-US");
  });
});

/** ICU separates "PM" with a narrow no-break space; compare with plain spaces. */
const plain = (text: string) => text.replace(/[  ]/g, " ");

describe("formatShortDateTime", () => {
  const now = new Date(2026, 9, 1, 12, 0);
  it("writes month, day and the locale's clock without seconds; the year only when it differs", () => {
    expect(formatShortDateTime(new Date(2026, 8, 20, 14, 23, 45), "zh-CN", now)).toBe("9月20日 14:23");
    expect(formatShortDateTime(new Date(2025, 8, 20, 8, 5), "zh-CN", now)).toBe("2025年9月20日 08:05");
    expect(plain(formatShortDateTime(new Date(2026, 8, 20, 14, 23, 45), "en-US", now))).toBe("Sep 20, 2:23 PM");
    expect(formatShortDateTime(new Date(2026, 8, 20, 14, 23, 45), "de-DE", now)).toBe("20. Sept., 14:23");
    expect(formatShortDateTime(new Date(2026, 8, 20, 14, 23, 45), "fr-FR", now)).toBe("20 sept., 14:23");
  });

  it("returns an empty string for missing or invalid values", () => {
    expect(formatShortDateTime(null, "en-US", now)).toBe("");
    expect(formatShortDateTime("not a date", "en-US", now)).toBe("");
  });
});

describe("clock rule per interface language", () => {
  const evening = new Date(2026, 10, 8, 18, 39, 5);
  const morning = new Date(2026, 10, 8, 8, 5, 0);

  it("reads English as a 12-hour clock and Chinese, German and French as 24-hour time", () => {
    expect(plain(formatTime(evening, "en-US"))).toBe("6:39 PM");
    expect(plain(formatTime(morning, "en-US"))).toBe("8:05 AM");
    for (const locale of ["zh-CN", "de-DE", "fr-FR"]) {
      expect(formatTime(evening, locale)).toBe("18:39");
      expect(formatTime(morning, locale)).toBe("08:05");
    }
    expect(plain(formatTime(evening, "en-US", { seconds: true }))).toBe("6:39:05 PM");
    expect(formatTime(evening, "de-DE", { seconds: true })).toBe("18:39:05");
  });

  it("keeps the same clock in full date-time text", () => {
    expect(plain(formatDateTime(evening, "en-US", { dateStyle: "medium" }))).toBe("Nov 8, 2026, 6:39 PM");
    expect(formatDateTime(evening, "zh-CN", { dateStyle: "medium" })).toBe("2026年11月8日 18:39");
    expect(formatDateTime(evening, "fr-FR", { dateStyle: "medium" })).toBe("8 nov. 2026, 18:39");
  });

  it("returns an empty string instead of throwing for missing or invalid values", () => {
    expect(formatTime(undefined, "en-US")).toBe("");
    expect(formatTime(Number.NaN, "en-US")).toBe("");
    expect(formatDate("not a date", "en-US")).toBe("");
    expect(formatDateTime(null, "zh-CN")).toBe("");
  });

  it("exposes the hour cycle so custom formats keep the rule", () => {
    expect(clockOptions("en-US")).toMatchObject({ hourCycle: "h12", hour: "numeric" });
    expect(clockOptions("zh-CN")).toMatchObject({ hourCycle: "h23", hour: "2-digit" });
    expect(clockOptions("de-DE")).toMatchObject({ hourCycle: "h23" });
    expect(clockOptions("fr-FR")).toMatchObject({ hourCycle: "h23" });
  });
});

describe("formatMessageTime (chat bylines)", () => {
  const now = new Date(2026, 10, 8, 20, 0);
  const sameDay = new Date(2026, 10, 8, 18, 39);
  const earlierDay = new Date(2026, 10, 7, 18, 39);
  const earlierYear = new Date(2025, 10, 7, 18, 39);

  it.each([
    ["en-US", "6:39 PM", "Nov 7, 6:39 PM", "Nov 7, 2025, 6:39 PM"],
    ["zh-CN", "18:39", "11月7日 18:39", "2025年11月7日 18:39"],
    ["de-DE", "18:39", "7. Nov., 18:39", "7. Nov. 2025, 18:39"],
    ["fr-FR", "18:39", "7 nov., 18:39", "7 nov. 2025, 18:39"],
  ])("%s: time today, short date and time earlier, the year for another year", (locale, today, earlier, otherYear) => {
    expect(plain(formatMessageTime(sameDay, locale, { now }))).toBe(today);
    expect(plain(formatMessageTime(earlierDay, locale, { now }))).toBe(earlier);
    expect(plain(formatMessageTime(earlierYear, locale, { now }))).toBe(otherYear);
  });

  it("covers every shipped interface language", () => {
    expect([...SUPPORTED_LOCALES].sort()).toEqual(["de-DE", "en-US", "fr-FR", "zh-CN"]);
  });

  it("reads a server time a few seconds ahead of the browser clock as today", () => {
    const browserNow = new Date(2026, 10, 8, 23, 59, 30);
    const serverTime = new Date(2026, 10, 9, 0, 0, 10);
    expect(formatMessageTime(serverTime, "zh-CN", { now: browserNow })).toBe("00:00");
  });

  it("compares calendar days in the given time zone", () => {
    // 2026-11-08T02:41Z is still Nov 7 in Los Angeles.
    const value = "2026-11-08T02:41:53Z";
    const laNow = new Date("2026-11-08T05:00:00Z");
    expect(formatMessageTime(value, "zh-CN", { now: laNow, timeZone: "America/Los_Angeles" })).toBe("18:41");
    expect(formatMessageTime(value, "zh-CN", { now: new Date("2026-11-08T20:00:00Z"), timeZone: "America/Los_Angeles" }))
      .toBe("11月7日 18:41");
  });

  it("is empty for a missing or invalid time", () => {
    expect(formatMessageTime(null, "en-US", { now })).toBe("");
    expect(formatMessageTime("nope", "en-US", { now })).toBe("");
  });
});

describe("formatListDate", () => {
  const now = new Date(2026, 9, 7, 15, 0);
  const copy = {
    justNow: () => "just now",
    minutesAgo: (count: number) => `${count}m ago`,
    hoursAgo: (count: number) => `${count}h ago`,
    yesterday: () => "Yesterday",
    daysAgo: (count: number) => `${count}d ago`,
  };
  const label = (value: Date, locale = "en-US") => formatListDate(value, locale, copy, now)?.label;

  it("is relative within seven days and follows calendar days", () => {
    expect(label(new Date(2026, 9, 7, 14, 59, 30))).toBe("just now");
    expect(label(new Date(2026, 9, 7, 14, 48))).toBe("12m ago");
    expect(label(new Date(2026, 9, 7, 12, 0))).toBe("3h ago");
    expect(label(new Date(2026, 9, 6, 23, 30))).toBe("Yesterday");
    expect(label(new Date(2026, 9, 4, 9, 0))).toBe("3d ago");
    expect(label(new Date(2026, 9, 1, 9, 0))).toBe("6d ago");
  });

  it("switches to month and day after a week and adds the year only for other years", () => {
    expect(label(new Date(2026, 8, 28, 14, 23))).toBe("Sep 28");
    expect(label(new Date(2026, 8, 28, 14, 23), "zh-CN")).toBe("9月28日");
    expect(label(new Date(2025, 11, 30, 9, 0))).toBe("Dec 30, 2025");
    expect(label(new Date(2025, 11, 30, 9, 0), "zh-CN")).toBe("2025年12月30日");
  });

  it("carries the full date and time for title and a machine-readable value for <time>", () => {
    const value = new Date(2026, 8, 28, 14, 23);
    const result = formatListDate(value, "zh-CN", copy, now);
    expect(result?.title).toBe("2026年9月28日 14:23");
    expect(formatListDate(value, "en-US", copy, now)?.title).toBe("Sep 28, 2026, 2:23 PM");
    expect(result?.dateTime).toBe(value.toISOString());
  });

  it("reads a timestamp slightly in the future as just now (clock skew), not as an absolute date", () => {
    expect(label(new Date(2026, 9, 7, 15, 3))).toBe("just now");
    expect(label(new Date(2026, 9, 7, 15, 4, 59))).toBe("just now");
  });

  it("counts calendar days and formats in the given (viewer's) time zone", () => {
    // Now is 2026-10-07 01:30 in Shanghai but still 10-06 17:30 in UTC. 14:00Z is 22:00 the previous evening in
    // Shanghai ("Yesterday") but the same UTC day ("3h ago").
    const zoneNow = new Date("2026-10-06T17:30:00Z");
    const evening = new Date("2026-10-06T14:00:00Z");
    expect(formatListDate(evening, "en-US", copy, zoneNow, "Asia/Shanghai")?.label).toBe("Yesterday");
    expect(formatListDate(evening, "en-US", copy, zoneNow, "UTC")?.label).toBe("3h ago");
    expect(formatListDate(new Date("2025-12-31T17:00:00Z"), "zh-CN", copy, zoneNow, "Asia/Shanghai")?.label).toBe("1月1日");
    expect(formatListDate(new Date("2025-12-31T17:00:00Z"), "zh-CN", copy, zoneNow, "UTC")?.label).toBe("2025年12月31日");
    expect(formatListDate(new Date("2026-09-28T06:23:00Z"), "zh-CN", copy, zoneNow, "Asia/Shanghai")?.title).toBe("2026年9月28日 14:23");
  });

  it("follows non-Shanghai viewer zones: Los Angeles and Berlin", () => {
    // 2026-10-08 03:00Z is Oct 7 20:00 in Los Angeles and Oct 8 05:00 in Berlin. A session six hours earlier
    // (Oct 7 21:00Z) is earlier today in Los Angeles but yesterday in Berlin (Oct 7 23:00).
    const zoneNow = new Date("2026-10-08T03:00:00Z");
    const sixHoursAgo = new Date("2026-10-07T21:00:00Z");
    expect(formatListDate(sixHoursAgo, "en-US", copy, zoneNow, "America/Los_Angeles")?.label).toBe("6h ago");
    expect(formatListDate(sixHoursAgo, "en-US", copy, zoneNow, "Europe/Berlin")?.label).toBe("Yesterday");
    expect(formatListDate(sixHoursAgo, "en-US", copy, zoneNow, "America/Los_Angeles")?.title).toBe("Oct 7, 2026, 2:00 PM");
    expect(formatListDate(sixHoursAgo, "de-DE", copy, zoneNow, "Europe/Berlin")?.title).toBe("07.10.2026, 23:00");
    // From a week on, the absolute day is the viewer's calendar day.
    expect(formatListDate(new Date("2026-09-25T23:30:00Z"), "en-US", copy, zoneNow, "America/Los_Angeles")?.label).toBe("Sep 25");
    expect(formatListDate(new Date("2026-09-25T23:30:00Z"), "en-US", copy, zoneNow, "Europe/Berlin")?.label).toBe("Sep 26");
  });

  it("returns null for missing or invalid values", () => {
    expect(formatListDate(null, "en-US", copy, now)).toBeNull();
    expect(formatListDate("not a date", "en-US", copy, now)).toBeNull();
  });
});
