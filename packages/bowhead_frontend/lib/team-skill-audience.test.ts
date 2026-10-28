import { describe, expect, it } from "vitest";

import { teamSkillAudience } from "./team-skill-audience";

describe("teamSkillAudience", () => {
  it("uses the enriched API audience as the source of truth", () => {
    expect(teamSkillAudience({ audience: "private", visibility: "team_wide", share_count: 8 })).toBe("private");
    expect(teamSkillAudience({ audience: "custom", visibility: "creator_only", share_count: 2 })).toBe("custom");
    expect(teamSkillAudience({ audience: "team_wide", visibility: "creator_only", share_count: 0 })).toBe("team_wide");
  });

  it("derives the same audience for legacy responses without enrichment", () => {
    expect(teamSkillAudience({ visibility: "team_wide", share_count: 0 })).toBe("team_wide");
    expect(teamSkillAudience({ visibility: "creator_only", share_count: 2 })).toBe("custom");
    expect(teamSkillAudience({ visibility: "creator_only", share_count: 0 })).toBe("private");
  });
});
