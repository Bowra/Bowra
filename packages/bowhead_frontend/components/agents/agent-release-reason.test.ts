import { describe, expect, it } from "vitest";

import { agentReleaseReasonKey } from "./agent-release-reason";

describe("agentReleaseReasonKey", () => {
  it("names the exact-grant requirement instead of a generic Skill dependency", () => {
    expect(agentReleaseReasonKey("precise_skill_scope_required")).toBe("release_reason_precise_skill_scope");
  });

  it("keeps other Skill dependency codes on the generic Skill reason", () => {
    expect(agentReleaseReasonKey("skill_revision_changed")).toBe("release_reason_skill_dependency");
  });
});
