import { describe, expect, it } from "vitest";

import { caseWithCurrentDefaults, scorerConfigOnOldDefaults, withCurrentScorerDefaults } from "./agent-eval-defaults";

describe("old Owner-case defaults", () => {
  it("recognises the old zero Tool-call cap; the advisory number check alone fails nothing", () => {
    expect(scorerConfigOnOldDefaults({ max_tool_calls: 0 })).toBe(true);
    expect(scorerConfigOnOldDefaults({ critical: ["no_fabrication"] })).toBe(false);
    expect(scorerConfigOnOldDefaults({ max_tool_calls: 5, critical: ["output_schema"] })).toBe(false);
    expect(scorerConfigOnOldDefaults({})).toBe(false);
  });

  it("removes only the two old settings and keeps the Owner's own choices", () => {
    expect(withCurrentScorerDefaults({ max_tool_calls: 0, critical: ["no_fabrication", "output_schema"], max_duration_ms: 1 })).toEqual({
      critical: ["output_schema"],
      max_duration_ms: 1,
    });
    expect(withCurrentScorerDefaults({ max_tool_calls: 3, critical: ["no_fabrication"] })).toEqual({ max_tool_calls: 3 });
  });

  it("builds a save body from a saved case without the read-only input field", () => {
    const saved = caseWithCurrentDefaults({
      case_key: "quality.1",
      input: { task: "Explain the result" },
      expected_contract: { allowed_terminal_statuses: ["completed"] },
      scorer_config: { max_tool_calls: 0 },
      effect_policy: { mode: "no_effects" },
    });
    expect(saved).toEqual({
      case_key: "quality.1",
      task: "Explain the result",
      fixture_ref: {},
      expected_contract: { allowed_terminal_statuses: ["completed"] },
      scorer_config: {},
      effect_policy: { mode: "no_effects" },
      valid_until: null,
    });
    expect("input" in saved).toBe(false);
  });
});
