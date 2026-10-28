import { BUILD_EDITION } from "./product-identity";
import { afterEach, describe, expect, it } from "vitest";

import { SETTINGS_TAB_REQUIRED_TEAM_ROLES } from "@/components/settings/settings-shell";
import {
  lastWorkRoute,
  rememberWorkRoute,
  settingsActiveForRoute,
  settingsTabHref,
  visibleSettingsNavigation,
  type SettingsNavAccess,
  type SettingsNavGroup,
} from "./settings-destinations";

const NONE = { tab: null, admin: null } as const;

function access(overrides: Partial<SettingsNavAccess> = {}): SettingsNavAccess {
  return {
    teamRole: "member",
    capabilities: {},
    isLocalDeployment: false,
    teamOpenapiServed: false,
    canManageActiveSpace: false,
    ...overrides,
  };
}

function outline(groups: SettingsNavGroup[]): Record<string, string[]> {
  return Object.fromEntries(
    groups.map((group) => [group.key, group.items.map((item) => item.destination.id)]),
  );
}

describe("visibleSettingsNavigation: Personal, current Space and Team settings", () => {
  it("lists an owner on a local deployment everything in spec order", () => {
    const groups = visibleSettingsNavigation(
      access({
        teamRole: "owner",
        capabilities: { access_requests: true, feedback_loop: true },
        isLocalDeployment: true,
        teamOpenapiServed: true,
        canManageActiveSpace: true,
      }),
      NONE,
    );
    expect(groups.map((group) => group.key)).toEqual([
      "personal", "workspace", "team_people", "team_ai", "team_access", "team_appearance", "team_operations",
    ]);
    expect(outline(groups)).toEqual({
      personal: ["profile", "security", "appearance", "memory", "export", "access-requests"],
      team_people: ["people", "workspaces"],
      team_ai: ["llm", "datasource"],
      team_access: BUILD_EDITION === "business" ? ["integrations", "email-service", "public-entries", "oidc", "license", "openapi"] : ["integrations", "email-service", "public-entries", "openapi"],
      team_appearance: ["team-appearance", "reference-retention"],
      team_operations: ["feedback-inbox", "team-operations"],
      workspace: ["general", "instructions", "members"],
    });
  });

  it("keeps sign-in & identity for the owner only and local-only rows out of SaaS", () => {
    const admin = outline(visibleSettingsNavigation(access({ teamRole: "team_admin", isLocalDeployment: true }), NONE));
    expect(admin.team_access).toEqual(BUILD_EDITION === "business" ? ["integrations", "email-service", "public-entries", "license"] : ["integrations", "email-service", "public-entries"]);
    const saasOwner = outline(visibleSettingsNavigation(access({ teamRole: "owner" }), NONE));
    expect(saasOwner.team_access).toEqual(["integrations", "email-service", "public-entries"]);
  });

  it("places operations data after the suggestion inbox for team administrators", () => {
    const owner = access({ teamRole: "owner", capabilities: { feedback_loop: true } });
    expect(outline(visibleSettingsNavigation(owner, NONE)).team_operations).toEqual(["feedback-inbox", "team-operations"]);
    expect(outline(visibleSettingsNavigation(access({ teamRole: "team_admin" }), NONE)).team_operations).toEqual(["team-operations"]);
    expect(outline(visibleSettingsNavigation(access({ teamRole: "member" }), NONE)).team_operations).toBeUndefined();
  });

  it("gives an audit admin personal settings", () => {
    expect(outline(visibleSettingsNavigation(access({ teamRole: "audit_admin" }), NONE))).toEqual({
      personal: ["profile", "security", "appearance", "memory", "export"],
    });
  });

  it("gives a member the personal group, plus the space group only as space administrator", () => {
    expect(outline(visibleSettingsNavigation(access(), NONE))).toEqual({
      personal: ["profile", "security", "appearance", "memory", "export"],
    });
    expect(outline(visibleSettingsNavigation(access({ canManageActiveSpace: true }), NONE)).workspace).toEqual([
      "general",
      "instructions",
      "members",
    ]);
  });

  it("lets a space member read the instructions page and nothing else of the space group", () => {
    expect(outline(visibleSettingsNavigation(access({ hasActiveSpace: true }), NONE)).workspace).toEqual(["instructions"]);
  });

  it("does not show the space group to team admins who do not administer the active space", () => {
    expect(outline(visibleSettingsNavigation(access({ teamRole: "owner" }), NONE)).workspace).toBeUndefined();
  });


  it.each(["member", "audit_admin", null] as const)("never advertises Team settings for %s, including the current deep link", (teamRole) => {
    for (const tab of ["people", "workspaces", "llm", "datasource", "integrations", "email-service", "public-entries", "oidc", "license", "openapi", "team-appearance", "reference-retention", "feedback-inbox", "team-operations"] as const) {
      const groups = visibleSettingsNavigation(access({ teamRole, isLocalDeployment: true, teamOpenapiServed: true, capabilities: { feedback_loop: true } }), { tab, admin: null });
      expect(groups.some((group) => group.key.startsWith("team_"))).toBe(false);
    }
  });

  it.each(["access-requests", "feedback-inbox", "openapi", "oidc", "license"] as const)("keeps unavailable %s hidden even on its own route", (tab) => {
    const groups = visibleSettingsNavigation(access({ teamRole: "owner" }), { tab, admin: null });
    expect(groups.flatMap((group) => group.items).some((item) => item.destination.id === tab)).toBe(false);
  });

  it.each(["general", "members"] as const)("does not advertise an unauthorized Space deep link: %s", (tab) => {
    expect(outline(visibleSettingsNavigation(access({ hasActiveSpace: true }), { tab, admin: null })).workspace).toEqual(["instructions"]);
  });

  it("does not advertise Space instructions without an active Space, even on their route", () => {
    expect(outline(visibleSettingsNavigation(access(), { tab: "instructions", admin: null })).workspace).toBeUndefined();
  });

  it("marks access requests as pending", () => {
    const items = visibleSettingsNavigation(
      access({ teamRole: "owner", capabilities: { access_requests: true } }),
      NONE,
    ).flatMap((group) => group.items);
    expect(Object.fromEntries(items.filter((i) => i.counter).map((i) => [i.destination.id, i.counter]))).toEqual({
      "access-requests": "access_requests_pending",
    });
  });

  it("lists each destination once", () => {
    const keys = visibleSettingsNavigation(
      access({
        teamRole: "owner",
        capabilities: { access_requests: true, feedback_loop: true },
        isLocalDeployment: true,
        teamOpenapiServed: true,
        canManageActiveSpace: true,
      }),
      NONE,
    ).flatMap((group) => group.items.map((item) => item.route));
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("shares the tab role table with SettingsShell", () => {
    expect(SETTINGS_TAB_REQUIRED_TEAM_ROLES.oidc).toEqual(["owner"]);
    expect(SETTINGS_TAB_REQUIRED_TEAM_ROLES.people).toEqual(["owner", "team_admin"]);
  });
});

describe("settings routes", () => {
  it("maps tabs to their pages", () => {
    expect(settingsTabHref("profile")).toBe("/settings/profile/");
    expect(settingsTabHref("access-requests")).toBe("/settings/access-requests/");
    expect(settingsTabHref("llm")).toBe("/team/settings/models/");
    expect(settingsTabHref("workspaces")).toBe("/team/settings/spaces/");
    expect(settingsTabHref("tools")).toBe("/team/settings/tools/");
    // Canonical directory URL with the trailing slash (the account menu links the same form).
    expect(settingsTabHref("members")).toBe("/space/settings/members/");
  });

  it.each(["general", "instructions", "members"] as const)("generates and selects the %s page under every deployment prefix", (page) => {
    expect(settingsTabHref(page)).toBe(`/space/settings/${page}/`);
    expect(settingsTabHref(page, true)).toBe(`/space/settings/${page}/?from=setup`);
    for (const prefix of ["", "/bowhead", "/nested/bowhead"]) {
      expect(settingsActiveForRoute(`${prefix}/space/settings/${page}/`, "", prefix)).toEqual({ tab: page, admin: null });
    }
  });

  it("derives the current destination from the route", () => {
    expect(settingsActiveForRoute("/settings/")).toEqual({ tab: "profile", admin: null });
    expect(settingsActiveForRoute("/bowhead/settings/memory/", "", "/bowhead")).toEqual({ tab: "memory", admin: null });
    expect(settingsActiveForRoute("/team/settings/models/connections/new/")).toEqual({ tab: "llm", admin: null });
    expect(settingsActiveForRoute("/team/settings/brand-appearance/chart-styles/")).toEqual({
      tab: "team-appearance",
      admin: null,
    });
    expect(settingsActiveForRoute("/space/settings/members/")).toEqual({ tab: "members", admin: null });
    expect(settingsActiveForRoute("/space/settings/")).toEqual({ tab: "general", admin: null });
    expect(settingsActiveForRoute("/admin/turns/")).toEqual({ tab: null, admin: "traces" });
    expect(settingsActiveForRoute("/admin/credits/users/")).toEqual({ tab: null, admin: "credits" });
    expect(settingsActiveForRoute("/files/")).toEqual(NONE);
  });
});

describe("Back to work target", () => {
  afterEach(() => window.sessionStorage.clear());

  it("returns to the remembered work page, else home", () => {
    expect(lastWorkRoute()).toBe("/");
    rememberWorkRoute("/files/?folder=a");
    expect(lastWorkRoute()).toBe("/files/?folder=a");
  });

  it("ignores values that are not app-internal paths", () => {
    window.sessionStorage.setItem("bowhead.nav.last_work_route", "//evil.example/");
    expect(lastWorkRoute()).toBe("/");
  });
});
