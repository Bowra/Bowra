import { EXTRA_ACCESS_TABS, EXTRA_ADMIN_DESTINATIONS, EXTRA_TEAM_SEGMENTS } from "@/edition/settings";
import type { CenterDestinationId } from "./navigation-centers";
/**
 * Canonical settings routes and the Personal, current Space and Team directory.
 * Capability Center and Administration Console own their separate directory groups.
 * Pure module: no React or I/O; page and backend guards remain authoritative.
 */
import { isTeamSettingsPathTab, teamSettingsPath } from "./team-settings-paths";
import { isSpaceSettingsPage, spaceSettingsPath } from "./space-settings-paths";
import {
  MODEL_ADMIN_ROLES,
  TEAM_SETTINGS_ROLES,
  type AdministrationDestinationId,
  type SettingsTeamRole,
} from "./settings-navigation";

/** Settings tab ids (same values as `Tab` in components/settings/settings-shell.tsx). */
export type SettingsTabId =
  | "overview" | "profile" | "appearance" | "memory" | "people" | "members" | "workspaces" | "llm"
  | "datasource" | "tools" | "capabilities" | "workflows" | "subagents" | "access-requests" | "instructions"
  | "mcp" | "scheduled" | "integrations" | "email-service" | "public-entries" | "oidc" | "license"
  | "openapi" | "security" | "export" | "team-appearance" | "general" | "feedback-inbox"
  | "reference-retention" | "team-operations";

export type SettingsScope = "personal" | "feature" | "team" | "space";
export type SettingsGateKey = "access_requests" | "feedback_loop";
export type SettingsGroupKey =
  | "personal"
  | "team_people"
  | "team_ai"
  | "team_access"
  | "team_appearance"
  | "team_operations"
  | "workspace"
  | "capability_resources"
  | "admin_operations" | "admin_governance" | "admin_audit";

export type SettingsDestinationId =
  | { kind: "tab"; id: SettingsTabId }
  | { kind: "admin"; id: AdministrationDestinationId }
  | { kind: "center"; id: CenterDestinationId };

/** Row-end counter shown next to a destination (P2 Sidebar & Topbar Spec "row-end badge"). */
export type SettingsCounterKey =
  | "approvals_pending"
  | "access_requests_pending"
  | "feedback_pending"
  | "scheduled_active";

export interface SettingsNavItem {
  key: string;
  destination: SettingsDestinationId;
  route: string;
  counter?: SettingsCounterKey;
}

export interface SettingsNavGroup {
  key: SettingsGroupKey;
  items: SettingsNavItem[];
}

const TEAM_ADMIN_ROLES = TEAM_SETTINGS_ROLES;

/**
 * Tabs that need a team role, mirrored from SettingsShell's SETTINGS_TAB_REQUIRED_TEAM_ROLES (kept as the
 * single source there; this copy is pinned to it by settings-destinations.test.ts).
 */
export const SETTINGS_TAB_ROLES: Partial<Record<SettingsTabId, readonly SettingsTeamRole[]>> = {
  people: TEAM_ADMIN_ROLES,
  llm: MODEL_ADMIN_ROLES,
  datasource: TEAM_ADMIN_ROLES,
  integrations: TEAM_ADMIN_ROLES,
  "email-service": TEAM_ADMIN_ROLES,
  "public-entries": TEAM_ADMIN_ROLES,
  oidc: ["owner"],
  license: TEAM_ADMIN_ROLES,
  openapi: TEAM_ADMIN_ROLES,
  tools: TEAM_ADMIN_ROLES,
  capabilities: TEAM_ADMIN_ROLES,
  "team-appearance": TEAM_ADMIN_ROLES,
  "reference-retention": TEAM_ADMIN_ROLES,
  subagents: TEAM_ADMIN_ROLES,
  "team-operations": TEAM_ADMIN_ROLES,
};

export interface SettingsTabSpec {
  localOnly?: boolean;
  id: SettingsTabId;
  gate?: SettingsGateKey;
  roles?: readonly SettingsTeamRole[];
  counter?: SettingsCounterKey;
}

/** Display personal settings first, then the active Space, then the five Team groups. */
const TAB_GROUPS: { key: SettingsGroupKey; tabs: SettingsTabSpec[] }[] = [
  {
    key: "personal",
    tabs: [
      { id: "profile" },
      { id: "security" },
      { id: "appearance" },
      { id: "memory" },
      { id: "export" },
      { id: "access-requests", gate: "access_requests", counter: "access_requests_pending" },
    ],
  },
  {
    key: "workspace",
    tabs: [{ id: "general" }, { id: "instructions" }, { id: "members" }],
  },
  {
    key: "team_people",
    tabs: [{ id: "people", roles: SETTINGS_TAB_ROLES.people }, { id: "workspaces" }],
  },
  {
    key: "team_ai",
    tabs: [
      { id: "llm", roles: SETTINGS_TAB_ROLES.llm },
      { id: "datasource", roles: SETTINGS_TAB_ROLES.datasource },
    ],
  },
  {
    key: "team_access",
    tabs: [
      { id: "integrations", roles: SETTINGS_TAB_ROLES.integrations },
      { id: "email-service", roles: SETTINGS_TAB_ROLES["email-service"] },
      { id: "public-entries", roles: SETTINGS_TAB_ROLES["public-entries"] },
      ...EXTRA_ACCESS_TABS,
      { id: "openapi", roles: SETTINGS_TAB_ROLES.openapi },
    ],
  },
  {
    key: "team_appearance",
    tabs: [
      { id: "team-appearance", roles: SETTINGS_TAB_ROLES["team-appearance"] },
      { id: "reference-retention", roles: SETTINGS_TAB_ROLES["reference-retention"] },
    ],
  },
  {
    key: "team_operations",
    tabs: [
      { id: "feedback-inbox", gate: "feedback_loop", roles: TEAM_ADMIN_ROLES, counter: "feedback_pending" },
      { id: "team-operations", roles: TEAM_ADMIN_ROLES },
    ],
  },
];

const PERSONAL_TABS: ReadonlySet<SettingsTabId> = new Set(["profile", "security", "appearance", "memory", "export"]);
const SPACE_TABS: ReadonlySet<SettingsTabId> = new Set(["general", "instructions", "members"]);
const TEAM_TABS: ReadonlySet<SettingsTabId> = new Set([
  "people", "workspaces", "llm", "datasource", "integrations", "email-service", "public-entries", "oidc",
  "license", "openapi", "team-appearance", "tools", "capabilities", "subagents", "workflows", "scheduled",
  "feedback-inbox", "team-operations", "reference-retention",
]);
/** Team tabs that have their own route rather than a `teamSettingsPath` segment. */
const TEAM_OWN_ROUTE_TABS: ReadonlySet<SettingsTabId> = new Set(["tools", "capabilities", "workflows", "subagents"]);

export function settingsTabScope(tab: SettingsTabId): SettingsScope {
  if (tab === "access-requests") return "feature";
  if (TEAM_TABS.has(tab)) return "team";
  if (SPACE_TABS.has(tab)) return "space";
  return "personal";
}

/** The route of a settings tab; the same mapping SettingsShell uses for its own links. */
export function settingsTabHref(tab: SettingsTabId, fromSetup = false): string {
  const scope = settingsTabScope(tab);
  if (scope === "personal") return `/settings/${tab}/`;
  if (scope === "feature") return `/settings/${tab}/`;
  if (isSpaceSettingsPage(tab)) return spaceSettingsPath(tab, fromSetup);
  if (isTeamSettingsPathTab(tab)) return teamSettingsPath(tab, fromSetup);
  if (TEAM_OWN_ROUTE_TABS.has(tab)) return `/team/settings/${tab}/`;
  throw new Error(`Unknown Team settings page: ${tab}`);
}

export interface SettingsNavAccess {
  teamRole: SettingsTeamRole | null;
  capabilities: Partial<Record<SettingsGateKey, boolean>>;
  isLocalDeployment: boolean;
  teamOpenapiServed: boolean;
  /** The user is an administrator of the active space (same check as the account menu). */
  canManageActiveSpace: boolean;
  /** The user belongs to an active space; members read its instructions there (no editing). */
  hasActiveSpace?: boolean;
}

/**
 * Advertise only authorized, available destinations. Opening a deep link never bypasses these checks;
 * page and backend guards separately control whether its content can be accessed.
 */
export function settingsTabVisible(spec: SettingsTabSpec, access: SettingsNavAccess): boolean {
  if (spec.localOnly && !access.isLocalDeployment) return false;
  const role = access.teamRole;
  const scope = settingsTabScope(spec.id);
  if (scope === "team" && !(role && TEAM_ADMIN_ROLES.includes(role))) return false;
  if (scope === "space" && !access.canManageActiveSpace && !(spec.id === "instructions" && access.hasActiveSpace)) return false;
  if (spec.id === "openapi" && !access.teamOpenapiServed) return false;
  if (spec.gate && !access.capabilities[spec.gate]) return false;
  if (spec.roles?.length && !(role && spec.roles.includes(role))) return false;
  return true;
}

export interface SettingsActive {
  tab: SettingsTabId | null;
  admin: AdministrationDestinationId | null;
  center?: CenterDestinationId | null;
}

/** Visible groups in display order; empty groups are dropped. */
export function visibleSettingsNavigation(access: SettingsNavAccess, _active: SettingsActive): SettingsNavGroup[] {
  const groups: SettingsNavGroup[] = TAB_GROUPS.map((group) => ({
    key: group.key,
    items: group.tabs
      .filter((spec) => settingsTabVisible(spec, access))
      .map((spec) => ({
        key: `tab:${spec.id}`,
        destination: { kind: "tab", id: spec.id },
        route: settingsTabHref(spec.id),
        counter: spec.counter,
      })),
  }));
  return groups.filter((group) => group.items.length > 0);
}

const TEAM_SEGMENT_TABS: Record<string, SettingsTabId> = {
  people: "people",
  spaces: "workspaces",
  models: "llm",
  llm: "llm",
  datasource: "datasource",
  integrations: "integrations",
  "email-service": "email-service",
  "public-entries": "public-entries",
  ...EXTRA_TEAM_SEGMENTS as Partial<Record<string, SettingsTabId>>,
  openapi: "openapi",
  scheduled: "scheduled",
  "brand-appearance": "team-appearance",
  "feedback-inbox": "feedback-inbox",
  "team-operations": "team-operations",
  "reference-retention": "reference-retention",
  tools: "tools",
  capabilities: "capabilities",
  workflows: "workflows",
  subagents: "subagents",
};

const ADMIN_SEGMENTS: Record<string, AdministrationDestinationId> = {
  ...Object.fromEntries(EXTRA_ADMIN_DESTINATIONS.map(({ id, route }) => [route.split("/")[2], id])),
  turns: "traces",
  approvals: "approvals",
  credits: "credits",
  access: "access",
};

function stripBasePath(pathname: string, appBasePath: string): string {
  const prefix = appBasePath.trim().replace(/\/+$/, "");
  if (!prefix || prefix === "/") return pathname;
  if (pathname === prefix) return "/";
  return pathname.startsWith(`${prefix}/`) ? pathname.slice(prefix.length) : pathname;
}

function segmentsOf(pathname: string, appBasePath: string): string[] {
  return stripBasePath(pathname.split(/[?#]/, 1)[0] || "/", appBasePath).split("/").filter(Boolean);
}

/** The destination a settings or administration route belongs to, derived from the route alone. */
export function settingsActiveForRoute(
  pathname: string | null | undefined,
  _search = "",
  appBasePath = "",
): SettingsActive {
  const none: SettingsActive = { tab: null, admin: null };
  if (!pathname) return none;
  const segments = segmentsOf(pathname, appBasePath);
  const [first, second, third] = segments;
  if (first === "admin") {
    if (second === "members") return { tab: "people", admin: null };
    return { tab: null, admin: (second && ADMIN_SEGMENTS[second]) || null };
  }
  if (first === "settings") {
    if (!second) return { tab: "profile", admin: null };
    if (second === "tools" || second === "workflows") return { tab: second, admin: null };
    if (PERSONAL_TABS.has(second as SettingsTabId) || second === "access-requests") {
      return { tab: second as SettingsTabId, admin: null };
    }
    return none;
  }
  if (first === "team" && second === "settings") {
    if (!third) return { tab: "people", admin: null };
    return { tab: TEAM_SEGMENT_TABS[third] ?? null, admin: null };
  }
  if (first === "space" && second === "settings") {
    if (!third) return { tab: "general", admin: null };
    return { tab: isSpaceSettingsPage(third) ? third : null, admin: null };
  }
  return none;
}

export function settingsItemIsActive(item: SettingsNavItem, active: SettingsActive): boolean {
  if (item.destination.kind === "center") return item.destination.id === active.center;
  return item.destination.kind === "tab"
    ? item.destination.id === active.tab
    : item.destination.id === active.admin;
}

/** Session storage key for the last work route, the target of "Back to Work". */
export const LAST_WORK_ROUTE_KEY = "bowhead.nav.last_work_route";

export function rememberWorkRoute(internalRoute: string): void {
  try {
    window.sessionStorage.setItem(LAST_WORK_ROUTE_KEY, internalRoute);
  } catch {
    // Without storage "Back to Work" falls back to the home page.
  }
}

/** The work page the user was on before entering settings; `/` when there is none. */
export function lastWorkRoute(): string {
  try {
    const stored = window.sessionStorage.getItem(LAST_WORK_ROUTE_KEY);
    if (stored && stored.startsWith("/") && !stored.startsWith("//")) return stored;
  } catch {
    // fall through
  }
  return "/";
}
