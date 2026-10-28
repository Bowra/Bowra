import { describe, expect, it } from "vitest";

import enMessages from "@/messages/en-US.json";

import {
  UNIFIED_SIDEBAR_NAV,
  activeSidebarDestination,
  resolveSidebarActiveContext,
  visibleSidebarNavigation,
} from "./sidebar-nav";

describe("unified sidebar navigation schema", () => {
  it("keeps the existing work order and places Agents before Capability Center", () => {
    expect(UNIFIED_SIDEBAR_NAV.map((item) => item.id)).toEqual([
      "new_work",
      "reports",
      "library",
      "agents",
      "capabilities",
    ]);
  });

  it("keeps every static-export path canonically trailing-slashed", () => {
    for (const primary of UNIFIED_SIDEBAR_NAV) {
      if (primary.kind === "action") {
        expect(primary).not.toHaveProperty("route");
        expect(primary.activeRoute).toMatch(/\/$/);
        continue;
      }
      const destinations =
        primary.kind === "direct"
          ? [primary]
          : primary.expandOnly
            ? primary.children
            : [primary, ...primary.children];
      for (const destination of destinations) {
        expect(destination.route.split("?")[0]).toMatch(/\/$/);
      }
    }
  });

  it.each([
    ["/", "", { primaryId: "new_work", leafId: "new_work" }],
    ["/reports/", "", { primaryId: "reports", leafId: "reports" }],
    ["/files/", "", { primaryId: "library", leafId: "files" }],
    ["/knowledge/doc-types/", "", { primaryId: "library", leafId: "knowledge" }],
    ["/capabilities/", "", { primaryId: "capabilities", leafId: "capabilities" }],
    ["/capabilities/", "type=tools", { primaryId: "capabilities", leafId: "tools_catalog" }],
    ["/capabilities/", "type=capabilities", { primaryId: "capabilities", leafId: "capabilities_catalog" }],
    ["/settings/tools/", "oauth=success", { primaryId: "capabilities", leafId: "tools_catalog" }],
    ["/team/settings/workflows/", "", { primaryId: "capabilities", leafId: "skills" }],
    ["/team/settings/scheduled/", "", { primaryId: "capabilities", leafId: "scheduled" }],
    ["/agents/", "", { primaryId: "agents", leafId: "my_agents" }],
    ["/agents/charts/", "chart=trending&domain=finance", { primaryId: "agents", leafId: "agent_charts" }],
    ["/agents/detail/", "agent_id=sa_1", { primaryId: "agents", leafId: "agent_marketplace" }],
    ["/agents/new/", "draft_id=draft_1", { primaryId: "agents", leafId: "my_agents" }],
    ["/agents/manage/versions/", "agent_id=sa_1", { primaryId: "agents", leafId: "my_agents" }],
    ["/settings/subagents/", "agent_id=sa_1", { primaryId: "agents", leafId: "agents" }],
    ["/team/settings/subagents/", "", { primaryId: "agents", leafId: "agents" }],
    ["/settings/", "", null],
  ])("resolves %s%s to its parent and leaf", (pathname, search, expected) => {
    expect(activeSidebarDestination(pathname, search)).toEqual(expected);
  });

  it.each([
    [
      "/bowhead/",
      "",
      { primaryId: "new_work", leafId: "new_work" },
    ],
    [
      "/bowhead/files/",
      "",
      { primaryId: "library", leafId: "files" },
    ],
    [
      "/bowhead/agents/charts/",
      "chart=trending&domain=finance",
      { primaryId: "agents", leafId: "agent_charts" },
    ],
    [
      "/bowhead/capabilities/",
      "view=my",
      { primaryId: "capabilities", leafId: "capabilities" },
    ],
  ])(
    "resolves runtime-prefixed route %s%s",
    (pathname, search, expected) => {
      expect(
        activeSidebarDestination(pathname, search, "/bowhead"),
      ).toEqual(expected);
    },
  );

  it("does not mark New work active while a historical session is open", () => {
    expect(activeSidebarDestination("/", "", "", "s_1")).toBeNull();
    expect(
      activeSidebarDestination("/bowhead/", "", "/bowhead", "s_1"),
    ).toBeNull();
  });

  it("projects a historical root session into recovery current", () => {
    expect(
      resolveSidebarActiveContext({
        pathname: "/",
        currentSessionId: "s_1",
      }),
    ).toEqual({
      destination: null,
      recoveryCurrent: true,
      currentWithinPrimaryId: null,
    });
    expect(
      resolveSidebarActiveContext({
        pathname: "/reports/",
        currentSessionId: "s_1",
      }),
    ).toEqual({
      destination: { primaryId: "reports", leafId: "reports" },
      recoveryCurrent: false,
      currentWithinPrimaryId: null,
    });
  });

  it("projects nested destinations into current-within groups", () => {
    expect(
      resolveSidebarActiveContext({ pathname: "/files/" }),
    ).toEqual({
      destination: { primaryId: "library", leafId: "files" },
      recoveryCurrent: false,
      currentWithinPrimaryId: "library",
    });
  });

  it.each(["owner", "team_admin", "audit_admin", "member", null] as const)("opens the capability overview for %s", (teamRole) => {
    const center = visibleSidebarNavigation({ teamRole }).find((item) => item.id === "capabilities");
    expect(center).toMatchObject({ kind: "group", route: "/capabilities/" });
  });

  it.each(["hired", "created", "shared", "archived", "team"])("keeps Agent view %s selected under both entry modes", (view) => {
    for (const prefix of ["", "/bowhead"]) {
      expect(activeSidebarDestination(`${prefix}/agents/manage/${view === "team" ? "governance" : view}/`, "", prefix)).toEqual({ primaryId: "agents", leafId: "my_agents" });
      expect(activeSidebarDestination(`${prefix}/team/settings/subagents/`, "", prefix)).toEqual({ primaryId: "agents", leafId: "agents" });
    }
  });

  it.each(["owner", "team_admin", "audit_admin", "member"] as const)("offers complete discovery resources to %s without team editor shortcuts", (teamRole) => {
    const center = visibleSidebarNavigation({ teamRole, authenticated: true, capabilities: { personal_mcp: true } }).find((item) => item.id === "capabilities");
    expect(center?.kind).toBe("group");
    if (center?.kind !== "group") throw new Error("Capability resources require a group.");
    expect(center.children.map((child) => child.id)).toEqual(["skills", "tools_catalog", "capabilities_catalog", "connections", "scheduled"]);
    expect(center.children.find((child) => child.id === "tools_catalog")?.route).toBe("/capabilities/?type=tools");
    expect(center.children.find((child) => child.id === "capabilities_catalog")?.route).toBe("/capabilities/?type=capabilities");
  });

  it.each(["owner", "team_admin", "audit_admin", "member"] as const)("offers the Agent market to an eligible %s", (teamRole) => {
    const navigation = visibleSidebarNavigation({ teamRole, capabilities: { subagent_management_v2: true } });
    expect(navigation.map((item) => item.id)).toEqual(["new_work", "reports", "library", "agents", "capabilities"]);
    expect(navigation.find((item) => item.id === "agents")).toMatchObject({ kind: "group", route: "/agents/manage/hired/", children: [
      { id: "agent_marketplace", route: "/agents/marketplace/" },
      { id: "my_agents", route: "/agents/manage/hired/" },
      { id: "agent_charts", route: "/agents/charts/" },
    ] });
  });

  it.each(["owner", "team_admin"] as const)("retains built-in Agent configuration for %s when the market is unavailable", (teamRole) => {
    const navigation = visibleSidebarNavigation({ teamRole, capabilities: { subagent_creator: true } });
    expect(navigation.find((item) => item.id === "agents")).toMatchObject({ kind: "direct", route: "/team/settings/subagents/" });
  });

  it("retains a member's existing Owner editor while the market is unavailable", () => {
    const navigation = visibleSidebarNavigation({ teamRole: "member", capabilities: {}, activeAgentId: "osa_owned" });
    expect(navigation.find((item) => item.id === "agents")).toMatchObject({ kind: "direct", route: "/agents/manage/?agent_id=osa_owned" });
  });

  it.each(["owner", "team_admin", "audit_admin", "member", null] as const)("requires an existing Agent capability for %s", (teamRole) => {
    expect(visibleSidebarNavigation({ teamRole }).some((item) => item.id === "agents")).toBe(false);
    if (teamRole === "member" || teamRole === "audit_admin" || teamRole === null) {
      expect(visibleSidebarNavigation({ teamRole, capabilities: { subagent_creator: true } }).some((item) => item.id === "agents")).toBe(false);
    }
  });
});

describe("unified sidebar English labels", () => {
  // Copy contract (docs/design/menus.en-US.md): sidebar navigation labels use Title Case; page H1s use
  // sentence case and keep their own keys, so this guard never reaches page headings.
  const MINOR_WORDS = new Set(["a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "via"]);
  const isTitleCase = (label: string) =>
    label.split(/\s+/).every((word, index, words) => {
      const first = word.charAt(0);
      if (first.toLowerCase() === first.toUpperCase()) return true; // digits and symbols
      const minor = MINOR_WORDS.has(word) && index > 0 && index < words.length - 1;
      return minor || first === first.toUpperCase();
    });
  const lookup = (key: string): unknown =>
    key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], enMessages.sidebar);

  it("uses Title Case for every primary, child and section label", () => {
    const navigation = visibleSidebarNavigation({
      teamRole: "owner",
      authenticated: true,
      capabilities: { subagent_management_v2: true, personal_mcp: true },
    });
    const keys = navigation.flatMap((item) =>
      item.kind === "group" ? [item.labelKey, ...item.children.map((child) => child.labelKey)] : [item.labelKey],
    );
    keys.push("work", "recent_work", "search_all_work");
    expect(keys).toEqual(expect.arrayContaining(["new_work", "knowledge", "centers.connections"]));
    for (const key of keys) {
      const label = lookup(key);
      expect(typeof label, key).toBe("string");
      expect(isTitleCase(label as string), `${key}: ${label}`).toBe(true);
    }
    const session = enMessages.session;
    for (const label of [
      session.rail_group_today,
      session.rail_group_yest,
      session.rail_group_last7,
      session.rail_group_older,
      session.rail_back_to_recent,
    ]) {
      expect(isTitleCase(label), label).toBe(true);
    }
  });
});
