import { describe, expect, it } from "vitest";
import { EXTRA_ADMIN_DESTINATIONS } from "@/edition/settings";
import type { MeUser } from "./auth-api";
import {
  administrationDestinationsForUser,
  centerActiveForRoute,
  centerNavigationGroups,
  capabilityOverviewGroups,
  navigationContextForRoute,
  capabilityReturnDestination,
  withCapabilityReturn,
} from "./navigation-centers";

function user(role: MeUser["team_role"], capabilities: MeUser["capabilities"] = {}): MeUser {
  return { id: "u_member", team_id: "team_a", team_role: role, capabilities } as MeUser;
}

describe("navigation ownership and authorization", () => {
  it.each([
    ["/admin/members/", "", "settings"],
    ["/admin/credits/user/", "user=u_1", "administration"],
    ["/agents/charts/", "chart=trending&domain=finance", "work"],
    ["/agents/detail/", "agent_id=sa_1&release_candidate=rc_1", "work"],
    ["/agents/manage/versions/", "agent_id=sa_1", "work"],
    ["/agents/new/", "draft_id=draft_1", "work"],
    ["/team/settings/subagents/", "", "work"],
    ["/team/settings/tools/", "", "capabilities"],
    ["/team/settings/scheduled/", "bind=sa_1", "capabilities"],
    ["/team/settings/models/connections/edit/", "connection_id=c_1", "settings"],
    ["/space/settings/members/", "", "settings"],
    ["/settings/subagents/", "agent_id=sa_1", "work"],
    ["/connections/", "", "capabilities"],
    ["/skills/", "skill=skill_1", "capabilities"],
    ["/reports/", "", "work"],
  ])("assigns %s to its owning context under both entry modes", (path, search, context) => {
    expect(navigationContextForRoute(path, search)).toBe(context);
    expect(navigationContextForRoute(`/bowhead${path}`, search, "/bowhead")).toBe(context);
  });

  it("keeps personal authoring independent of team administration", () => {
    const groups = centerNavigationGroups(
      "capabilities",
      user("member", { subagent_creator: true, subagent_management_v2: true, personal_mcp: true }),
    );
    expect(groups.map((group) => group.key)).toEqual(["capability_resources"]);
    expect(groups[0].items.map((item) => item.route)).toEqual(["/skills/", "/capabilities/?type=tools", "/capabilities/?type=capabilities", "/connections/", "/team/settings/scheduled/"]);
    const personal = capabilityOverviewGroups(user("member", { subagent_management_v2: true })).find((group) => group.key === "capability_personal");
    expect(personal?.items.map((item) => item.route)).toContain("/agents/manage/created/");
    expect(
      groups.flatMap((group) => group.items).some((item) => item.route === "/team/settings/tools/"),
    ).toBe(false);
  });

  it("offers only the existing audit destination to an audit administrator", () => {
    const auditUser = user("audit_admin", {
      credit: { credits_read: true } as NonNullable<MeUser["capabilities"]>["credit"],
    });
    const destinations = administrationDestinationsForUser(auditUser);
    expect(destinations.map((item) => item.id)).toEqual(
      EXTRA_ADMIN_DESTINATIONS.filter((item) => item.id === "audit").map((item) => item.id),
    );
    expect(
      capabilityOverviewGroups(auditUser).some(
        (group) => group.key === "capability_team",
      ),
    ).toBe(false);
    expect(administrationDestinationsForUser(user("member"))).toEqual([]);
  });

  it("waits for the credit read projection rather than inferring it from role", () => {
    expect(
      administrationDestinationsForUser(user("owner")).some((item) => item.id === "credits"),
    ).toBe(false);
    const owner = user("owner", {
      credit: { credits_read: true } as NonNullable<MeUser["capabilities"]>["credit"],
    });
    expect(administrationDestinationsForUser(owner).some((item) => item.id === "credits")).toBe(
      true,
    );
  });

  it("keeps page views and their management pages under the same resource", () => {
    expect(centerActiveForRoute("/skills/", "scope=mine")).toBe("skills");
    for (const search of ["view=my", "view=team", "type=tools&view=my"]) {
      expect(centerActiveForRoute("/capabilities/", search)).toBe("overview");
    }
    expect(centerActiveForRoute("/capabilities/", "type=tools")).toBe("tools_catalog");
    expect(centerActiveForRoute("/capabilities/", "type=capabilities")).toBe("capabilities_catalog");
    for (const [path, expected] of [["/team/settings/tools/", "tools_catalog"], ["/team/settings/capabilities/", "capabilities_catalog"], ["/team/settings/workflows/", "skills"], ["/team/settings/scheduled/", "scheduled"]]) {
      expect(centerActiveForRoute(`/bowhead${path}`, "", "/bowhead")).toBe(expected);
    }
  });

  it("preserves a known origin without accepting arbitrary return URLs", () => {
    expect(withCapabilityReturn("/team/settings/tools/?oauth=success#source", "team")).toBe("/team/settings/tools/?oauth=success&capability_return=team#source");
    expect(capabilityReturnDestination("tools", "oauth=success&capability_return=team")).toEqual({ href: "/capabilities/", label: "overview" });
    expect(capabilityReturnDestination("workflows", "capability_return=my_skills")).toEqual({ href: "/skills/?scope=mine", label: "my_skills" });
    expect(capabilityReturnDestination("tools", "capability_return=my_skills")).toBeNull();
    expect(capabilityReturnDestination("profile", "capability_return=team")).toBeNull();
    expect(capabilityReturnDestination("tools", "capability_return=https%3A%2F%2Funtrusted.example")).toBeNull();
  });
});
