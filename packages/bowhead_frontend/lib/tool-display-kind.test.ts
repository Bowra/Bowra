import { describe, expect, it } from "vitest";

import { TOOL_DISPLAY_TABLE, builtinTools } from "./tool-display";

describe("built-in Tool catalog", () => {
  it("lists the user-visible Tools and hides system parts", () => {
    const refs = builtinTools({ googleWorkspace: true }).map((tool) => tool.ref);

    expect(refs).toHaveLength(35);
    expect(refs).toEqual(expect.arrayContaining(["bi_query", "remove_chart", "send_email", "python_sandbox", "get_current_user"]));
    for (const part of [
      "subagent_return",
      "subagent_spawn",
      "python_exec",
      "render_deck",
      "template_document_submit",
      "skill_read",
      "tool_switch_context",
      "configuration_apply",
      "agent_ci_start",
    ]) {
      expect(TOOL_DISPLAY_TABLE[part]?.kind).toBe("part");
      expect(refs).not.toContain(part);
    }
  });
});
