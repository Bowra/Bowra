import { describe, expect, it } from "vitest";

import { teamSkillOwnerName } from "./team-skill-owner";

describe("teamSkillOwnerName", () => {
  it("uses the resolved creator name", () => {
    expect(teamSkillOwnerName({ created_by: "usr_ann", created_by_name: " Ann Lee " }, "Team member")).toBe("Ann Lee");
  });

  it.each([
    ["missing", { created_by: "usr_24f0f61b37bb" }],
    ["null", { created_by: "usr_24f0f61b37bb", created_by_name: null }],
    ["blank", { created_by: "usr_24f0f61b37bb", created_by_name: "  " }],
    ["the raw id echoed by older servers", { created_by: "usr_24f0f61b37bb", created_by_name: "usr_24f0f61b37bb" }],
  ])("falls back to the label when the name is %s", (_case, skill) => {
    expect(teamSkillOwnerName(skill, "Team member")).toBe("Team member");
  });
});
