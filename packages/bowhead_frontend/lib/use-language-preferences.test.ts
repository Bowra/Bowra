import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getUserSettings = vi.fn();
vi.mock("./api", () => ({ getUserSettings: () => getUserSettings(), setUserSetting: vi.fn() }));
vi.mock("./api-client", () => ({ apiJson: vi.fn().mockResolvedValue({ value: null }) }));

import { resetLanguagePreferencesForTests, useLanguagePreferences } from "./use-language-preferences";

describe("useLanguagePreferences · interface language", () => {
  beforeEach(() => {
    resetLanguagePreferencesForTests();
    document.cookie = "NEXT_LOCALE=; path=/; max-age=0";
  });
  afterEach(() => {
    document.cookie = "NEXT_LOCALE=; path=/; max-age=0";
  });

  it.each([
    ["unset ui_language with a cookie (cookie-only switch) reads the cookie's language", {}, "de-DE", "de-DE"],
    ["unset ui_language without a cookie follows the browser", {}, null, "auto"],
    ["an explicit auto stays auto even with a leftover cookie", { ui_language: "auto" }, "en-US", "auto"],
    ["a saved locale wins", { ui_language: "fr-FR" }, "en-US", "fr-FR"],
  ])("%s", async (_name, settings, cookie, expected) => {
    if (cookie) document.cookie = `NEXT_LOCALE=${cookie}; path=/`;
    getUserSettings.mockResolvedValue(settings);
    const { result } = renderHook(() => useLanguagePreferences());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.ui).toBe(expected);
  });
});
