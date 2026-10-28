import { describe, expect, it } from "vitest";

import { deriveTeamSyncStatus } from "./team-skill-approval";
import type { TeamSkillPublic } from "./workflow-solidify-api";

function row(over: Partial<TeamSkillPublic>): TeamSkillPublic {
  return {
    id: "oskill_1",
    name: "channel-gm",
    display_name: "渠道毛利",
    description: "d",
    visibility: "team_wide",
    status: "enabled",
    verdict: null,
    size_bytes: 1,
    created_by: "u_me",
    approval: "approved",
    approved_sha256: "sha-live",
    ...over,
  };
}

describe("deriveTeamSyncStatus（§1 转移表 → drawer 状态互斥）", () => {
  it("无团队行 → none；creator_only 自用态（撤回/拒后收回）同 none", () => {
    expect(deriveTeamSyncStatus(undefined, "x", "u_me").state).toBe("none");
    expect(deriveTeamSyncStatus(row({ visibility: "creator_only" }), "x", "u_me").state).toBe("none");
  });

  it("同名非本人 → name_taken（禁提交）", () => {
    expect(deriveTeamSyncStatus(row({ created_by: "u_other" }), "x", "u_me").state).toBe("name_taken");
  });

  it("L1：pending → pending；rejected → rejected 带理由", () => {
    expect(deriveTeamSyncStatus(row({ approval: "pending" }), "x", "u_me").state).toBe("pending");
    const s = deriveTeamSyncStatus(row({ approval: "rejected", rejected_reason: "缺回滚" }), "x", "u_me");
    expect(s.state).toBe("rejected");
    expect(s.reason).toBe("缺回滚");
  });

  it("L2：候选在审/被拒 优先于 同步比对（live 双槽正交）", () => {
    expect(deriveTeamSyncStatus(row({ candidate_state: "pending" }), "sha-live", "u_me").state).toBe("update_pending");
    const s = deriveTeamSyncStatus(row({ candidate_state: "rejected", candidate_rejected_reason: "改口径" }), "diff", "u_me");
    expect(s.state).toBe("update_rejected");
    expect(s.reason).toBe("改口径");
  });

  it("已批：指纹一致 → synced；不一致 → behind；个人指纹缺失 → synced（不误报落后）", () => {
    expect(deriveTeamSyncStatus(row({}), "sha-live", "u_me").state).toBe("synced");
    expect(deriveTeamSyncStatus(row({}), "sha-new", "u_me").state).toBe("behind");
    expect(deriveTeamSyncStatus(row({}), null, "u_me").state).toBe("synced");
  });
});
