import { describe, expect, it } from "vitest";
import { projectDisplayUsage } from "./document-usage-display";

describe("document usage display", () => {
  it("shows inclusive known usage and its partial marker without replacing billing counters", () => {
    const payload = { tokens_input: 17, tokens_output: 9, display_usage: { tokens_input: 617, tokens_output: 209, partial: true } };
    expect(projectDisplayUsage(payload)).toEqual({ tokensInput: 617, tokensOutput: 209, usagePartial: true });
    expect(payload.tokens_input).toBe(17);
  });

  it.each([undefined, null, [], { tokens_input: -1, tokens_output: 2, partial: false }, { tokens_input: 3, tokens_output: 2 }])(
    "preserves legacy root counts when the optional display projection is unavailable: %j",
    (display_usage) => {
      expect(projectDisplayUsage({ tokens_input: 17, tokens_output: 9, display_usage })).toEqual({ tokensInput: 17, tokensOutput: 9 });
    },
  );
});
