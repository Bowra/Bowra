import { describe, expect, it } from "vitest";
import { agentManagementHref, agentManagementReturnHref, agentManagementViewForPath, withAgentReturnView } from "./agent-navigation";

describe("Agent management destinations", () => {
  it.each(["hired", "created", "shared", "archived", "team"] as const)("reopens the %s view through its dedicated path", (view) => {
    const href = agentManagementHref(view);
    expect(href).toBe(`/agents/manage/${view === "team" ? "governance" : view}/`);
    for (const prefix of ["", "/bowhead", "/nested/bowhead"]) {
      expect(agentManagementViewForPath(prefix + href)).toBe(view);
    }
    const detail = withAgentReturnView("/agents/detail/?id=osa_1", view);
    expect(agentManagementReturnHref(detail.split("?")[1])).toBe(href);
  });

  it("defaults the management index to hired and keeps invalid returns within management", () => {
    expect(agentManagementViewForPath("/agents/manage/")).toBe("hired");
    expect(agentManagementReturnHref("return_view=https://example.com")).toBe("/agents/manage/created/");
  });
});
