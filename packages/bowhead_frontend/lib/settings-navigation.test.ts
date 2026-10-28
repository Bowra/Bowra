import { BUILD_EDITION } from "./product-identity";
import { describe, expect, it } from "vitest";

import {
  teamRoleForUser,
  visibleAdministrationDestinations,
} from "./settings-navigation";

describe("settings navigation access schema", () => {
  it("normalizes the legacy admin bit without granting other roles", () => {
    expect(teamRoleForUser({ team_role: undefined, is_team_admin: true })).toBe(
      "team_admin",
    );
    expect(teamRoleForUser({ team_role: undefined, is_team_admin: false })).toBe(
      "member",
    );
    expect(
      teamRoleForUser({ team_role: "audit_admin", is_team_admin: true }),
    ).toBe("audit_admin");
  });

  it.each([
    ["member", []],
    ["audit_admin", ["audit"]],
    ["team_admin", ["traces", "approvals", "credits", "audit", "access"]],
    ["owner", ["traces", "approvals", "credits", "audit", "access"]],
  ] as const)("shows only authorized admin destinations for %s", (role, ids) => {
    expect(
      visibleAdministrationDestinations(role).map(
        (destination) => destination.id,
      ),
    ).toEqual(BUILD_EDITION === "business" ? ids : ids.filter((id) => id !== "audit"));
  });
});
