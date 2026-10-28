import { render, screen } from "@testing-library/react";
import { useLocale } from "next-intl";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ClientLocaleProvider } from "./client-locale-provider";

function CurrentLocale() {
  return <output data-testid="current-locale">{useLocale()}</output>;
}

beforeEach(() => {
  document.cookie = "NEXT_LOCALE=; max-age=0; path=/";
  Object.defineProperty(navigator, "language", { value: "zh-CN", configurable: true });
});

afterEach(() => {
  document.cookie = "NEXT_LOCALE=; max-age=0; path=/";
  document.documentElement.lang = "";
});

describe("default interface language", () => {
  it("follows a Chinese browser when no language was selected", () => {
    render(<ClientLocaleProvider><CurrentLocale /></ClientLocaleProvider>);
    expect(screen.getByTestId("current-locale").textContent).toBe("zh-CN");
  });

  it("preserves an explicit English cookie with a Chinese browser", () => {
    document.cookie = "NEXT_LOCALE=en-US; path=/";
    render(<ClientLocaleProvider><CurrentLocale /></ClientLocaleProvider>);
    expect(screen.getByTestId("current-locale").textContent).toBe("en-US");
  });
});
