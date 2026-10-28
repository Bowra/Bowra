import type { MeUser } from "./auth-api";
import { agentManagementView, isAgentNavigationRoute } from "./agent-navigation";
import { capabilityResourceNavigation, centerActiveForRoute, navigationContextForRoute, type CapabilityResourceId } from "./navigation-centers";

export type SidebarActionId = "new_work";
export type SidebarDirectId = "reports" | "agents";
export type SidebarGroupId = "library" | "capabilities" | "agents";
export type SidebarPrimaryId =
  | SidebarActionId
  | SidebarDirectId
  | SidebarGroupId;
export type SidebarLeafId =
  | SidebarDirectId
  | SidebarGroupId
  | "agent_marketplace"
  | "my_agents"
  | "agent_charts"
  | "files"
  | "knowledge"
  | CapabilityResourceId;
export type SidebarActiveId = SidebarActionId | SidebarLeafId;
export type SidebarTeamRole = NonNullable<MeUser["team_role"]>;

export interface SidebarDestination {
  id: SidebarLeafId;
  labelKey: string;
  route: string;
  requiredTeamRoles?: readonly SidebarTeamRole[];
  counter?: "scheduled_active";
}

export interface SidebarDirectItem extends SidebarDestination {
  kind: "direct";
  id: SidebarDirectId;
}

export interface SidebarActionItem {
  kind: "action";
  id: SidebarActionId;
  labelKey: string;
  action: "new-session";
  activeRoute: string;
}

interface SidebarGroupBase {
  kind: "group";
  id: SidebarGroupId;
  labelKey: string;
  children: readonly SidebarDestination[];
}

/** A group whose name opens its own overview page. */
interface SidebarLinkedGroupItem extends SidebarGroupBase {
  route: string;
  expandOnly?: false;
}

/** A group whose name only expands/collapses its children; it has no overview page. */
interface SidebarExpandOnlyGroupItem extends SidebarGroupBase {
  route?: undefined;
  expandOnly: true;
}

export type SidebarGroupItem = SidebarLinkedGroupItem | SidebarExpandOnlyGroupItem;

export type SidebarPrimaryItem =
  | SidebarActionItem
  | SidebarDirectItem
  | SidebarGroupItem;

export interface SidebarAccessContext {
  teamRole?: SidebarTeamRole | null;
  capabilities?: MeUser["capabilities"];
  /** Retain an existing guarded Owner editor when the market is unavailable. */
  activeAgentId?: string | null;
  authenticated?: boolean;
}

export interface ActiveSidebarDestination {
  primaryId: SidebarPrimaryId;
  leafId: SidebarActiveId;
}

export interface SidebarActiveContext {
  destination: ActiveSidebarDestination | null;
  recoveryCurrent: boolean;
  currentWithinPrimaryId: SidebarGroupId | null;
}

/**
 * One information architecture for Expanded, Compact, Overlay and Drawer.
 * Capability resources expand under their primary entry; Settings and Administration use their own directories.
 */
export const UNIFIED_SIDEBAR_NAV: readonly SidebarPrimaryItem[] = [
  {
    kind: "action",
    id: "new_work",
    labelKey: "new_work",
    action: "new-session",
    activeRoute: "/",
  },
  {
    kind: "direct",
    id: "reports",
    labelKey: "reports",
    route: "/reports/",
  },
  {
    kind: "group",
    id: "library",
    labelKey: "library",
    // Knowledge-base navigation discovery #3 (user decision, 2026-09-27): clicking Library only expands its children.
    // The old /library/ overview now redirects to /files/.
    expandOnly: true,
    children: [
      { id: "files", labelKey: "files", route: "/files/" },
      {
        id: "knowledge",
        labelKey: "knowledge",
        route: "/knowledge/",
      },
    ],
  },
  {
    kind: "group",
    id: "agents",
    labelKey: "centers.agents",
    route: "/agents/manage/hired/",
    children: [
      { id: "agent_marketplace", labelKey: "agent_marketplace", route: "/agents/marketplace/" },
      { id: "my_agents", labelKey: "my_agents", route: "/agents/manage/hired/" },
      { id: "agent_charts", labelKey: "agent_charts", route: "/agents/charts/" },
    ],
  },
  {
    kind: "group",
    id: "capabilities",
    labelKey: "capabilities",
    route: "/capabilities/",
    children: [],
  },
];

export function visibleSidebarNavigation(access: SidebarAccessContext): SidebarPrimaryItem[] {
  return UNIFIED_SIDEBAR_NAV.flatMap<SidebarPrimaryItem>((item) => {
    if (item.kind === "group" && item.id === "capabilities") {
      return [{ ...item, children: capabilityResourceNavigation(access.capabilities, access.authenticated ?? Boolean(access.teamRole)).map((resource) => ({
        id: resource.destination.id as CapabilityResourceId,
        labelKey: `centers.${resource.destination.id}`,
        route: resource.route,
        ...(resource.counter === "scheduled_active" ? { counter: "scheduled_active" as const } : {}),
      })) }];
    }
    if (item.kind === "group" && item.id === "agents") {
      if (access.capabilities?.subagent_management_v2) return [item];
      const canManageBuiltins = (access.teamRole === "owner" || access.teamRole === "team_admin")
        && access.capabilities?.subagent_creator;
      if (canManageBuiltins) return [{ kind: "direct", id: "agents", labelKey: item.labelKey, route: "/team/settings/subagents/" }];
      return access.activeAgentId
        ? [{ kind: "direct", id: "agents", labelKey: item.labelKey, route: `/agents/manage/?agent_id=${encodeURIComponent(access.activeAgentId)}` }]
        : [];
    }
    return [item.kind !== "group" ? item : {
          ...item,
          children: item.children.filter((child) =>
            !child.requiredTeamRoles?.length || Boolean(access.teamRole && child.requiredTeamRoles.includes(access.teamRole)),
          ),
        }];
  });
}

function normalizePath(pathname: string): string {
  const path = pathname.split("?")[0] || "/";
  return path.endsWith("/") ? path : `${path}/`;
}

function routeMatches(
  route: string,
  pathname: string,
  search: string,
): boolean {
  const [routePath, routeQuery = ""] = route.split("?");
  const normalizedRoute = normalizePath(routePath);
  const normalizedPath = normalizePath(pathname);

  if (routeQuery) {
    if (normalizedPath !== normalizedRoute) return false;
    const expected = new URLSearchParams(routeQuery);
    const actual = new URLSearchParams(search.replace(/^\?/, ""));
    return [...expected.entries()].every(
      ([key, value]) => actual.get(key) === value,
    );
  }
  if (normalizedRoute === "/") return normalizedPath === "/";
  return (
    normalizedPath === normalizedRoute ||
    normalizedPath.startsWith(normalizedRoute)
  );
}

export function activeSidebarDestination(
  pathname: string | null | undefined,
  search = "",
  appBasePath = "",
  currentSessionId: string | null = null,
): ActiveSidebarDestination | null {
  return resolveSidebarActiveContext({
    pathname,
    search,
    appBasePath,
    currentSessionId,
  }).destination;
}

export function resolveSidebarActiveContext({
  pathname,
  search = "",
  appBasePath = "",
  currentSessionId = null,
}: {
  pathname: string | null | undefined;
  search?: string;
  appBasePath?: string;
  currentSessionId?: string | null;
}): SidebarActiveContext {
  const routeDestination = resolveRouteDestination(
    pathname,
    search,
    appBasePath,
  );
  const recoveryCurrent = Boolean(
    currentSessionId && routeDestination?.leafId === "new_work",
  );
  const destination = recoveryCurrent ? null : routeDestination;
  const currentWithinPrimaryId =
    destination?.primaryId === "library" || destination?.primaryId === "capabilities" || destination?.primaryId === "agents"
      ? destination.primaryId
      : null;
  return {
    destination,
    recoveryCurrent,
    currentWithinPrimaryId,
  };
}

function resolveRouteDestination(
  pathname: string | null | undefined,
  search: string,
  appBasePath: string,
): ActiveSidebarDestination | null {
  if (!pathname) return null;
  const internalPathname = stripBasePath(pathname, appBasePath);
  if (isAgentNavigationRoute(internalPathname, search)) {
    const query = new URLSearchParams(search);
    let leafId: SidebarLeafId = "agents";
    if (internalPathname.startsWith("/agents/marketplace")) leafId = "agent_marketplace";
    else if (internalPathname.startsWith("/agents/manage") || internalPathname.startsWith("/agents/new")
      || internalPathname.replace(/\/+$/, "") === "/agents" || agentManagementView(query.get("return_view"))) leafId = "my_agents";
    else if (internalPathname.startsWith("/agents/charts") || query.get("return_page") === "charts") leafId = "agent_charts";
    else if (internalPathname.startsWith("/agents/detail") && query.get("return_page") !== "dashboard") leafId = "agent_marketplace";
    return { primaryId: "agents", leafId };
  }
  if (navigationContextForRoute(internalPathname, search) === "capabilities") {
    const resource = centerActiveForRoute(internalPathname, search);
    return { primaryId: "capabilities", leafId: resource && resource !== "overview" ? resource : "capabilities" };
  }
  let best: (ActiveSidebarDestination & { specificity: number }) | null =
    null;

  for (const primary of UNIFIED_SIDEBAR_NAV) {
    if (primary.kind === "action") {
      if (routeMatches(primary.activeRoute, internalPathname, search)) {
        best = {
          primaryId: primary.id,
          leafId: primary.id,
          specificity: primary.activeRoute.length,
        };
      }
      continue;
    }
    const destinations: readonly { id: SidebarLeafId; route: string }[] =
      primary.kind === "direct"
        ? [primary]
        : primary.expandOnly
          ? primary.children
          : [primary, ...primary.children];
    for (const destination of destinations) {
      if (!routeMatches(destination.route, internalPathname, search)) continue;
      const specificity = destination.route.length;
      if (!best || specificity > best.specificity) {
        best = {
          primaryId: primary.id,
          leafId: destination.id,
          specificity,
        };
      }
    }
  }

  if (!best) return null;
  return { primaryId: best.primaryId, leafId: best.leafId };
}

function stripBasePath(pathname: string, appBasePath: string): string {
  const prefix = appBasePath.trim().replace(/\/+$/, "");
  if (!prefix || prefix === "/") return pathname;
  if (pathname === prefix) return "/";
  return pathname.startsWith(`${prefix}/`)
    ? pathname.slice(prefix.length)
    : pathname;
}
