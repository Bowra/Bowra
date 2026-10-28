import { describe, expect, it } from "vitest";

import { safeInternalPath } from "./safe-next-path";

describe("safeInternalPath", () => {
  it("allows same-origin app paths", () => {
    expect(safeInternalPath("/invite?accept=1")).toBe("/invite?accept=1");
    expect(safeInternalPath(" /onboarding/profile ")).toBe("/onboarding/profile");
  });

  it("rejects ambiguous or unsafe paths", () => {
    expect(safeInternalPath(null)).toBeNull();
    expect(safeInternalPath("")).toBeNull();
    expect(safeInternalPath("https://evil.example")).toBeNull();
    expect(safeInternalPath("//evil.example")).toBeNull();
    expect(safeInternalPath("/bad\\path")).toBeNull();
    expect(safeInternalPath("/bad\npath")).toBeNull();
    expect(safeInternalPath(`/${"a".repeat(2049)}`)).toBeNull();
  });
});
