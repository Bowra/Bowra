import { describe, expect, it } from "vitest";

import { GOOGLE_DELIVERY_TOOL_REFS, builtinTools } from "./tool-display";

describe("built-in capabilities and Google Workspace", () => {
  it("lists Google write capabilities only while the team's Google connection is enabled", () => {
    const hidden = builtinTools().map((tool) => tool.ref);
    const shown = builtinTools({ googleWorkspace: true }).map((tool) => tool.ref);

    for (const ref of GOOGLE_DELIVERY_TOOL_REFS) {
      expect(hidden).not.toContain(ref);
      expect(shown).toContain(ref);
    }
    expect(shown.filter((ref) => !GOOGLE_DELIVERY_TOOL_REFS.has(ref))).toEqual(hidden);
  });
});
