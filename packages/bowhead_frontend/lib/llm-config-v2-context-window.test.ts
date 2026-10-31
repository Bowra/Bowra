import { describe, expect, it } from "vitest";

import { suggestedContextWindow } from "./llm-config-v2-api";

const CATALOG = { default: 131_072, models: { "deepseek-flash": 1_000_000, "minimax-m3": 1_000_000 } };

describe("suggestedContextWindow", () => {
  it("prefills a known model from the catalog, case-insensitively", () => {
    expect(suggestedContextWindow(CATALOG, "deepseek-flash")).toEqual({ value: 1_000_000, source: "catalog" });
    expect(suggestedContextWindow(CATALOG, " MiniMax-M3 ")).toEqual({ value: 1_000_000, source: "catalog" });
  });

  it("uses the connection declaration before the catalog without changing the saved model", () => {
    expect(suggestedContextWindow(CATALOG, "deepseek-flash", 128000)).toEqual({ value: 128000, source: "provider" });
  });

  it("uses the 131,072 default for an unknown model", () => {
    expect(suggestedContextWindow(CATALOG, "my-private-llm")).toEqual({ value: 131_072, source: "default" });
  });
});

 it("keeps declared provider capacity available when the catalog is unavailable", () => {
  expect(suggestedContextWindow(null, "custom", 32000)).toEqual({ value: 32000, source: "provider" });
  expect(suggestedContextWindow(null, "custom")).toBeNull();
});
