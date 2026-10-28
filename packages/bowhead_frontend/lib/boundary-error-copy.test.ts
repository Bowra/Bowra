import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { boundaryBuildLocale } from "./boundary-error-copy";

// buildLocale() supplies the build language; replace it here to test new languages not yet in SUPPORTED_LOCALES.
let mockedBuildLocale: string | null = null;
vi.mock("@/lib/locale", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/locale")>();
  return {
    ...actual,
    buildLocale: () => mockedBuildLocale ?? actual.buildLocale(),
  };
});

describe("boundaryBuildLocale", () => {
  let originalBuildLocale: string | undefined;

  beforeEach(() => {
    originalBuildLocale = process.env.NEXT_PUBLIC_BUILD_LOCALE;
    mockedBuildLocale = null;
  });

  afterEach(() => {
    if (originalBuildLocale === undefined) {
      delete process.env.NEXT_PUBLIC_BUILD_LOCALE;
    } else {
      process.env.NEXT_PUBLIC_BUILD_LOCALE = originalBuildLocale;
    }
  });

  it.each(["de-DE", "fr-FR"])("uses English boundary copy for %s builds", (locale) => {
    process.env.NEXT_PUBLIC_BUILD_LOCALE = locale;

    expect(boundaryBuildLocale()).toBe("en-US");
  });

  it("keeps Chinese and English builds on their own copy", () => {
    process.env.NEXT_PUBLIC_BUILD_LOCALE = "zh-CN";
    expect(boundaryBuildLocale()).toBe("zh-CN");
    process.env.NEXT_PUBLIC_BUILD_LOCALE = "en-US";
    expect(boundaryBuildLocale()).toBe("en-US");
  });

  it("uses English, never Chinese, for a build language without boundary copy", () => {
    mockedBuildLocale = "xx-XX";

    expect(boundaryBuildLocale()).toBe("en-US");
  });
});
