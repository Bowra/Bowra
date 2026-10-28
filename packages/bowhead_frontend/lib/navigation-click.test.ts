import { describe, expect, it } from "vitest";

import { shouldHandleAppLinkClick } from "./navigation-click";

describe("shouldHandleAppLinkClick", () => {
  it("handles an unmodified primary-button click", () => {
    expect(
      shouldHandleAppLinkClick({
        defaultPrevented: false,
        button: 0,
        metaKey: false,
        ctrlKey: false,
        shiftKey: false,
        altKey: false,
      }),
    ).toBe(true);
  });

  it.each([
    { button: 1 },
    { metaKey: true },
    { ctrlKey: true },
    { shiftKey: true },
    { altKey: true },
    { defaultPrevented: true },
  ])("leaves browser-native navigation intact for %o", (override) => {
    expect(
      shouldHandleAppLinkClick({
        defaultPrevented: false,
        button: 0,
        metaKey: false,
        ctrlKey: false,
        shiftKey: false,
        altKey: false,
        ...override,
      }),
    ).toBe(false);
  });
});
