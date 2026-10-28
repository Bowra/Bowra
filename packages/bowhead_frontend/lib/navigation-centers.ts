import type { MeUser } from "./auth-api";
import { isAgentNavigationRoute } from "./agent-navigation";
import {
  settingsTabHref,
  type SettingsActive,
  type SettingsNavGroup,
  type SettingsNavItem,
} from "./settings-destinations";
import { teamRoleForUser, visibleAdministrationDestinations } from "./settings-navigation";

export type NavigationContext = "work" | "settings" | "capabilities" | "administration";
export type CenterDestinationId =
  | "overview"
  | "skills"
  | "tools_catalog"
  | "capabilities_catalog"
  | "connections"
  | "scheduled";

export type CapabilityResourceId = Exclude<CenterDestinationId, "overview">;
export type CapabilityReturnOrigin = "team" | "my_skills";

export function withCapabilityReturn(route: string, origin: CapabilityReturnOrigin): string {
  const url = new URL(route, "https://navigation.invalid");
  url.searchParams.set("capability_return", origin);
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Only known capability destinations can replace a resource's default return. */
export function capabilityReturnDestination(active: string, search: string) {
  if (!["tools", "capabilities", "workflows", "subagents"].includes(active)) return null;
  const origin = new URLSearchParams(search).get("capability_return");
  // The overview no longer has a Team Management tab (one sectioned page since 2026-10-08), so the
  // team origin returns to the overview itself.
  if (origin === "team") return { href: "/capabilities/", label: "overview" };
  if (origin === "my_skills" && active === "workflows") return { href: "/skills/?scope=mine", label: "my_skills" };
  return null;
}

function segments(pathname: string, prefix: string): string[] {
  const base = prefix.replace(/\/+$/, "");
  const path = base && pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : pathname;
  return path.split(/[?#]/, 1)[0].split("/").filter(Boolean);
}

/** Preserve URLs while assigning every editor, detail and compatibility route to its owning area. */
export function navigationContextForRoute(
  pathname: string | null | undefined,
  search = "",
  prefix = "",
): NavigationContext {
  const [first, second, third] = segments(pathname ?? "/", prefix);
  if (isAgentNavigationRoute(pathname ?? "/", search, prefix)) return "work";
  if (first === "admin") return second === "members" ? "settings" : "administration";
  if (["capabilities", "connections", "skills"].includes(first)) return "capabilities";
  if (first === "settings") {
    return ["tools", "workflows"].includes(second) ? "capabilities" : "settings";
  }
  if ((first === "team" || first === "space") && second === "settings") {
    const tab = third;
    return ["tools", "capabilities", "workflows", "scheduled"].includes(tab ?? "")
      ? "capabilities"
      : "settings";
  }
  return "work";
}

export function centerActiveForRoute(
  pathname: string,
  search = "",
  prefix = "",
): CenterDestinationId | null {
  const [first, second, third] = segments(pathname, prefix);
  const query = new URLSearchParams(search);
  if (first === "capabilities") {
    if (["my", "team"].includes(query.get("view") ?? "")) return "overview";
    if (query.get("type") === "tools") return "tools_catalog";
    if (query.get("type") === "capabilities") return "capabilities_catalog";
    return "overview";
  }
  if (first === "connections") return "connections";
  if (first === "skills") return "skills";
  const tab = first === "settings" ? second
    : (first === "team" || first === "space") && second === "settings"
      ? third : null;
  if (tab === "workflows") return "skills";
  if (tab === "tools") return "tools_catalog";
  if (tab === "capabilities") return "capabilities_catalog";
  if (tab === "scheduled") return "scheduled";
  return null;
}

export function administrationDestinationsForUser(me: MeUser | null | undefined) {
  const credit = me?.credit_capabilities ?? me?.capabilities?.credit;
  return visibleAdministrationDestinations(teamRoleForUser(me)).filter(
    (destination) => destination.id !== "credits" || Boolean(credit?.credits_read),
  );
}

function center(id: CenterDestinationId, route: string): SettingsNavItem {
  return { key: `center:${id}`, destination: { kind: "center", id }, route };
}

function tab(
  id: "scheduled" | "tools" | "capabilities" | "workflows" | "subagents",
): SettingsNavItem {
  return {
    key: `tab:${id}`,
    destination: { kind: "tab", id },
    route: settingsTabHref(id),
    ...(id === "scheduled" ? { counter: "scheduled_active" as const } : {}),
  };
}

export function capabilityResourceNavigation(capabilities: MeUser["capabilities"], authenticated: boolean): SettingsNavItem[] {
  return [
    center("skills", "/skills/"),
    center("tools_catalog", "/capabilities/?type=tools"),
    center("capabilities_catalog", "/capabilities/?type=capabilities"),
    ...(capabilities?.personal_mcp || capabilities?.google_workspace ? [center("connections", "/connections/")] : []),
    ...(authenticated ? [{ ...center("scheduled", settingsTabHref("scheduled")), counter: "scheduled_active" as const }] : []),
  ];
}

/** Resource destinations are shared by the desktop directory, compact rail and phone drawer. */
export function centerNavigationGroups(
  context: NavigationContext,
  me: MeUser | null,
): SettingsNavGroup[] {
  if (context === "administration") {
    const destinations = administrationDestinationsForUser(me);
    return (
      [
        ["admin_operations", ["traces", "approvals"]],
        ["admin_governance", ["credits", "access"]],
        ["admin_audit", ["audit"]],
      ] as const
    )
      .map(([key, ids]) => ({
        key,
        items: destinations
          .filter((destination) => (ids as readonly string[]).includes(destination.id))
          .map((destination) => ({
            key: `admin:${destination.id}`,
            destination: { kind: "admin" as const, id: destination.id },
            route: destination.route,
            ...(destination.id === "approvals" ? { counter: "approvals_pending" as const } : {}),
          })),
      }))
      .filter((group) => group.items.length);
  }
  if (context !== "capabilities") return [];
  return [{
    key: "capability_resources",
    items: capabilityResourceNavigation(me?.capabilities, Boolean(me)),
  }];
}

/** Existing overview views keep their page controls; they do not define sidebar granularity. */
export function capabilityOverviewGroups(me: MeUser | null) {
  const overviewItem = (id: string, route: string) => ({ key: `center:${id}`, destination: { kind: "center", id }, route });
  const overviewTab = (id: "scheduled" | "tools" | "capabilities" | "workflows" | "subagents", origin?: CapabilityReturnOrigin) => ({ ...tab(id), ...(origin ? { route: withCapabilityReturn(settingsTabHref(id), origin) } : {}), destination: { kind: "tab", id: id as string } });
  const role = teamRoleForUser(me);
  const admin = role === "owner" || role === "team_admin";
  const capabilities = me?.capabilities;
  const groups = [
    {
      key: "capability_discovery",
      items: [
        overviewItem("discover", "/capabilities/"),
        ...(capabilities?.subagent_management_v2 ? [overviewItem("agents", "/agents/marketplace/")] : []),
        overviewItem("skills", "/skills/"),
        overviewItem("tools_catalog", "/capabilities/?type=tools"),
        overviewItem("capabilities_catalog", "/capabilities/?type=capabilities"),
      ],
    },
    {
      key: "capability_personal",
      items: [
        overviewItem("mine", "/capabilities/?view=my"),
        ...(capabilities?.subagent_management_v2
          ? [overviewItem("my_agents", "/agents/manage/created/")]
          : []),
        overviewItem("my_skills", "/skills/?scope=mine"),
        ...(capabilities?.personal_mcp || capabilities?.google_workspace
          ? [overviewItem("connections", "/connections/")]
          : []),
        ...(me ? [overviewTab("scheduled")] : []),
      ],
    },
  ];
  if (admin)
    groups.push({
      key: "capability_team",
      items: [
        overviewItem("team", "/capabilities/?view=team"),
        overviewTab("tools", "team"),
        overviewTab("capabilities", "team"),
        ...(capabilities?.workflow_creator ? [overviewTab("workflows", "team")] : []),
        ...(capabilities?.subagent_management_v2
          ? [overviewItem("team_agents", "/agents/manage/governance/")]
          : []),
        ...(capabilities?.subagent_creator ? [overviewTab("subagents", "team")] : []),
      ],
    });
  return groups;
}

export function navigationActive(
  active: SettingsActive,
  context: NavigationContext,
  pathname: string,
  search: string,
  prefix: string,
): SettingsActive {
  return context === "capabilities"
    ? { ...active, center: centerActiveForRoute(pathname, search, prefix) }
    : active;
}

export const CAPABILITY_SETTINGS_TABS = new Set([
  "tools",
  "capabilities",
  "workflows",
  "subagents",
  "scheduled",
]);
