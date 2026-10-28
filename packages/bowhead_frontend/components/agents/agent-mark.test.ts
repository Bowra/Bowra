import { describe, expect, it } from "vitest";

import { agentMark, distinctAgentMarks } from "./agent-mark";

describe("agent marks", () => {
  it("uses word initials, so built-ins sharing a first syllable no longer collide", () => {
    expect(agentMark("Compliance Analyst")).toBe("CA");
    expect(agentMark("Data & Compute Analyst")).toBe("DC");
    expect(agentMark("Marketing")).toBe("MA");
    expect(agentMark("配置智能体")).toBe("配置");
  });

  it("keeps marks distinct within one list (116: Compliance Analyst and Configuration Agent were both CO)", () => {
    const marks = distinctAgentMarks([
      "Business Growth Analyst",
      "Compliance Analyst",
      "Data & Compute Analyst",
      "Configuration Agent",
      "Strategy Deck Consultant",
      "Financial Analyst",
      "Knowledge Researcher",
      "Marketing Analyst",
    ]);
    expect(marks.get("Compliance Analyst")).toBe("CA");
    expect(marks.get("Configuration Agent")).toBe("CO");
    expect(new Set(marks.values()).size).toBe(marks.size);
  });

  it("resolves CJK collisions with later characters", () => {
    const marks = distinctAgentMarks(["合规分析师", "合规审核员"]);
    expect(marks.get("合规分析师")).toBe("合规");
    expect(marks.get("合规审核员")).toBe("合审");
  });
});
