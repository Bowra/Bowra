"use client";

/**
 * Shared context directory for Settings, Capability Center and Administration Console.
 * Expanded/overlay sidebar, compact rail and mobile drawer use the same destination model.
 * Existing page and backend guards remain authoritative for access and actions.
 */

import {
  cloneElement,
  isValidElement,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type MouseEvent,
  type ReactNode,
  type SVGProps,
} from "react";
import clsx from "clsx";
import { ChevronLeft, Search as LucideSearch } from "lucide-react";
import { useTranslations } from "next-intl";

import {
  Bolt,
  BookOpen,
  Bot,
  Brain,
  Building,
  CalendarClock,
  Cpu,
  Database,
  FileDown,
  FileText,
  Inbox,
  Lock,
  Mail,
  Palette,
  Plug,
  Sparkles,
  UserIcon,
  Users,
  Workflow,
} from "@/components/icons";
import { renderRefreshSvg } from "@/components/icon-refresh";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { administrationIcon } from "@/components/user-menu";
import { centerNavigationGroups, type NavigationContext, type CenterDestinationId } from "@/lib/navigation-centers";
import type { MeUser } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import {
  TEAM_SETTINGS_ROLES,
  teamRoleForUser,
} from "@/lib/settings-navigation";
import {
  lastWorkRoute,
  settingsItemIsActive,
  visibleSettingsNavigation,
  type SettingsActive,
  type SettingsCounterKey,
  type SettingsGroupKey,
  type SettingsNavGroup,
  type SettingsNavItem,
  type SettingsTabId,
} from "@/lib/settings-destinations";
import {
  useDeploymentFacts,
  useSettingsNavCounts,
  type SettingsCounters,
} from "@/lib/settings-nav-counts";
import { useSpaceSelection } from "@/lib/use-space";

type IconComponent = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

const TAB_ICONS: Partial<Record<SettingsTabId, IconComponent>> = {
  profile: UserIcon,
  security: Lock,
  appearance: Palette,
  memory: Brain,
  export: FileDown,
  "access-requests": Inbox,
  people: Users,
  workspaces: Building,
  llm: Cpu,
  datasource: Database,
  tools: Bolt,
  capabilities: Plug,
  subagents: Bot,
  integrations: Plug,
  "email-service": Mail,
  "public-entries": Building,
  oidc: Lock,
  license: Lock,
  openapi: Bolt,
  "team-appearance": Palette,
  "reference-retention": Database,
  workflows: Workflow,
  scheduled: CalendarClock,
  "feedback-inbox": Inbox,
  general: Building,
  instructions: FileText,
  members: Users,
};

const CENTER_ICONS: Record<CenterDestinationId, IconComponent> = {
  overview: Sparkles, skills: BookOpen, tools_catalog: Bolt, capabilities_catalog: Plug, connections: Plug, scheduled: CalendarClock,
};

/** The account menu wraps these icons in an aria-hidden span; here the icon is the row's first child. */
function decorativeIcon(icon: ReactNode): ReactNode {
  return isValidElement<{ "aria-hidden"?: boolean }>(icon) ? cloneElement(icon, { "aria-hidden": true }) : icon;
}

function itemIcon(item: SettingsNavItem): ReactNode {
  if (item.destination.kind === "admin") return decorativeIcon(administrationIcon(item.destination.id));
  if (item.destination.kind === "center") {
    const Icon = CENTER_ICONS[item.destination.id];
    return <Icon size={16} aria-hidden />;
  }
  const Icon = TAB_ICONS[item.destination.id] ?? Building;
  return <Icon size={16} aria-hidden />;
}

export type { SettingsCounters };

export interface SettingsNavModel {
  groups: SettingsNavGroup[];
  active: SettingsActive;
  activeSpaceName: string | null;
  counters: SettingsCounters;
  context: NavigationContext;
}

/**
 * Everything the settings content needs. Data requests run only while `enabled` (on a settings route, in a
 * visible surface) and go through the shared store in `lib/settings-nav-counts.ts`: every
 * mounted directory reads the same counters, fetched at most once per TTL, and deployment facts once
 * per page load. A failed counter request shows no counter.
 */
export function useSettingsNavModel({
  me,
  active,
  enabled,
  refreshKey,
  context,
}: {
  context: NavigationContext;
  me: MeUser | null;
  active: SettingsActive;
  enabled: boolean;
  refreshKey: string;
}): SettingsNavModel {
  const { items: spaces, activeId: activeSpaceId } = useSpaceSelection();
  const teamRole = teamRoleForUser(me);
  const isTeamAdmin = Boolean(teamRole && TEAM_SETTINGS_ROLES.includes(teamRole));
  const accessRequestsOn = Boolean(me?.capabilities?.access_requests);
  const feedbackLoopOn = Boolean(me?.capabilities?.feedback_loop);
  const deployment = useDeploymentFacts(enabled && context === "settings");
  const counters = useSettingsNavCounts(
    {
      key: `${me?.id ?? ""}|${me?.team_id ?? ""}|${teamRole ?? ""}|${accessRequestsOn}|${feedbackLoopOn}|${context}`,
      approvals: isTeamAdmin && context === "administration",
      accessRequests: accessRequestsOn && context === "settings",
      feedbackLoop: isTeamAdmin && feedbackLoopOn && context === "settings",
      scheduled: context === "capabilities",
    },
    enabled && Boolean(me),
    refreshKey,
  );

  const activeSpace = spaces.find((space) => space.id === activeSpaceId) ?? null;
  const canManageActiveSpace = activeSpace?.role === "admin";
  const groups = useMemo(
    () =>
      visibleSettingsNavigation(
        {
          teamRole,
          capabilities: {
            access_requests: accessRequestsOn,
            feedback_loop: feedbackLoopOn,
          },
          isLocalDeployment: deployment.local,
          teamOpenapiServed: deployment.openapi,
          canManageActiveSpace,
          hasActiveSpace: activeSpace !== null,
        },
        active,
      ),
    [accessRequestsOn, active, activeSpace, canManageActiveSpace, deployment, feedbackLoopOn, me, teamRole],
  );
  const scopedGroups = context === "settings" ? groups : centerNavigationGroups(context, me);
  return { groups: scopedGroups, active, activeSpaceName: activeSpace?.name ?? null, counters, context };
}

/**
 * Mounts the settings data only where settings content is shown, so work routes never subscribe to it or issue its requests.
 */
export function SettingsNavModelScope({
  me,
  active,
  refreshKey,
  context,
  children,
}: {
  me: MeUser | null;
  active: SettingsActive;
  refreshKey: string;
  context: NavigationContext;
  children: (model: SettingsNavModel) => ReactNode;
}) {
  const model = useSettingsNavModel({ me, active, enabled: true, refreshKey, context });
  return <>{children(model)}</>;
}

function useGroupTitle():(key: SettingsGroupKey, spaceName: string | null) => string {
  const t = useTranslations("sidebar.settings_nav");
  return (key, spaceName) => {
    if (key === "workspace") return spaceName ? t("space_group", { name: spaceName }) : t("groups.workspace");
    return t(`groups.${key}`);
  };
}

function itemLabel(
  item: SettingsNavItem,
  tabT: ReturnType<typeof useTranslations>,
  sidebarT: ReturnType<typeof useTranslations>,
): string {
  if (item.destination.kind === "center") return sidebarT(`centers.${item.destination.id}`);
  return item.destination.kind === "tab" ? tabT(item.destination.id) : sidebarT(item.destination.id);
}

export function SidebarNavCounter({ counter, value }: { counter: SettingsCounterKey; value: number | undefined }) {
  const t = useTranslations("sidebar.settings_nav");
  if (!value) return null;
  const pending = counter !== "scheduled_active";
  return (
    <span
      className={clsx("unified-settings-count", { "is-pending": pending })}
      data-counter={counter}
    >
      <span aria-hidden="true">{value}</span>
      <span className="unified-settings-count__sr">{pending ? t("pending_count", { count: value }) : t("active_count", { count: value })}</span>
    </span>
  );
}

export interface SettingsSidebarNavProps {
  model: SettingsNavModel;
  surface: "sidebar" | "drawer";
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, route: string) => void;
}

/** Expanded settings content: back link, filter, grouped destinations. */
export function SettingsSidebarNav({ model, surface, onNavigate }: SettingsSidebarNavProps) {
  const t = useTranslations("sidebar.settings_nav");
  const tabT = useTranslations("settings.tabs");
  const sidebarT = useTranslations("sidebar");
  const groupTitle = useGroupTitle();
  const centerT = useTranslations("sidebar.centers");
  const navLabel = model.context === "capabilities" || model.context === "administration" ? centerT(`${model.context}_navigation`) : t("label");
  const searchLabel = model.context === "capabilities" || model.context === "administration" ? centerT(`${model.context}_search`) : t("search");
  const [query, setQuery] = useState("");
  const listRef = useRef<HTMLElement | null>(null);
  const backRoute = useMemo(() => lastWorkRoute(), []);
  const rowClass = surface === "drawer" ? "unified-drawer-nav-item" : "unified-sidebar-nav-item";
  const needle = query.trim().toLocaleLowerCase();
  const groups = model.groups
    .map((group) => ({
      ...group,
      title: groupTitle(group.key, model.activeSpaceName),
      items: needle
        ? group.items.filter((item) => itemLabel(item, tabT, sidebarT).toLocaleLowerCase().includes(needle))
        : group.items,
    }))
    .filter((group) => group.items.length > 0);
  const activeKey = model.groups
    .flatMap((group) => group.items)
    .find((item) => settingsItemIsActive(item, model.active))?.key;

  // Long lists scroll inside the sidebar; keep the current destination in view. Runs again when rows
  // arrive late (space and deployment data load after the first render) and only scrolls the list itself.
  const rowCount = model.groups.reduce((sum, group) => sum + group.items.length, 0);
  useEffect(() => {
    const list = listRef.current;
    if (!activeKey || !list) return;
    const row = list.querySelector<HTMLElement>('[aria-current="page"]');
    if (!row) return;
    const listBox = list.getBoundingClientRect();
    const rowBox = row.getBoundingClientRect();
    if (rowBox.top < listBox.top) list.scrollTop -= listBox.top - rowBox.top;
    else if (rowBox.bottom > listBox.bottom) list.scrollTop += rowBox.bottom - listBox.bottom;
  }, [activeKey, rowCount]);

  return (
    <div className="unified-settings" data-surface={surface} data-context={model.context}>
      <a
        href={appPath(backRoute)}
        className={clsx(rowClass, "unified-settings-back")}
        data-testid="settings-nav-back"
        onClick={(event) => onNavigate(event, backRoute)}
      >
        {renderRefreshSvg(ChevronLeft, 16, 16, { "aria-hidden": true })}
        <span>{t("back_to_work")}</span>
      </a>
      <div className="unified-settings-search">
        {renderRefreshSvg(LucideSearch, 14, 14, { "aria-hidden": true })}
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={searchLabel}
          aria-label={searchLabel}
          data-testid="settings-nav-search"
        />
      </div>
      <nav
        ref={listRef}
        className="unified-settings-list"
        aria-label={navLabel}
        data-testid="settings-nav"
      >
        {groups.length === 0 ? (
          <p className="unified-settings-empty" role="status">
            {t("no_results")}
          </p>
        ) : null}
        {groups.map((group) => {
          const headingId = `settings-nav-${surface}-${group.key}`;
          return (
            <div className="unified-settings-group" role="group" aria-labelledby={headingId} key={group.key}>
              <div className="unified-sidebar-section-label" id={headingId}>
                {group.title}
              </div>
              {group.items.map((item) => {
                const isActive = settingsItemIsActive(item, model.active);
                return (
                  <a
                    key={item.key}
                    href={appPath(item.route)}
                    className={clsx(rowClass, "unified-settings-item", { "is-active": isActive })}
                    aria-current={isActive ? "page" : undefined}
                    data-testid={`settings-nav-${item.key}`}
                    onClick={(event) => onNavigate(event, item.route)}
                  >
                    {itemIcon(item)}
                    <span className="unified-settings-label">{itemLabel(item, tabT, sidebarT)}</span>
                    {item.counter ? <SidebarNavCounter counter={item.counter} value={model.counters[item.counter]} /> : null}
                  </a>
                );
              })}
            </div>
          );
        })}
      </nav>
    </div>
  );
}

/**
 * Icon rail in settings mode: "Back to Work", then one button per group (the group's first icon) that
 * opens a menu of the group's destinations. The button of the group holding the current page is
 * selected, as in the work rail.
 */
export function SettingsCompactNavigation({
  model,
  openGroup,
  onGroupOpenChange,
  onNavigate,
}: {
  model: SettingsNavModel;
  openGroup: SettingsGroupKey | null;
  onGroupOpenChange: (key: SettingsGroupKey, open: boolean) => void;
  onNavigate: SettingsSidebarNavProps["onNavigate"];
}) {
  const t = useTranslations("sidebar.settings_nav");
  const groupTitle = useGroupTitle();
  const centerT = useTranslations("sidebar.centers");
  const navLabel = model.context === "capabilities" || model.context === "administration" ? centerT(`${model.context}_navigation`) : t("label");
  const backRoute = useMemo(() => lastWorkRoute(), []);
  return (
    <nav className="unified-sidebar-compact-nav unified-settings-compact" aria-label={navLabel} data-context={model.context}>
      <a
        href={appPath(backRoute)}
        className="unified-sidebar-compact-button"
        aria-label={t("back_to_work")}
        data-testid="settings-nav-back"
        onClick={(event) => onNavigate(event, backRoute)}
      >
        {renderRefreshSvg(ChevronLeft, 20, 20, { "aria-hidden": true })}
        <span className="unified-sidebar-tooltip" aria-hidden="true">
          {t("back_to_work")}
        </span>
      </a>
      {model.groups.map((group) => (
        <SettingsCompactGroup
          key={group.key}
          group={group}
          title={groupTitle(group.key, model.activeSpaceName)}
          active={model.active}
          counters={model.counters}
          open={openGroup === group.key}
          onOpenChange={(open) => onGroupOpenChange(group.key, open)}
          onNavigate={onNavigate}
        />
      ))}
    </nav>
  );
}

function SettingsCompactGroup({
  group,
  title,
  active,
  counters,
  open,
  onOpenChange,
  onNavigate,
}: {
  group: SettingsNavGroup;
  title: string;
  active: SettingsActive;
  counters: SettingsCounters;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNavigate: SettingsSidebarNavProps["onNavigate"];
}) {
  const sidebarT = useTranslations("sidebar");
  const menu = useMenu({ open, onOpenChange, placement: "right-start", offset: 8 });
  const containsCurrent = group.items.some((item) => settingsItemIsActive(item, active));
  const first = group.items[0];
  return (
    <div className="unified-sidebar-compact-group">
      <button
        ref={menu.refs.setReference}
        type="button"
        className={clsx("unified-sidebar-compact-button", { "is-active": containsCurrent })}
        aria-label={containsCurrent ? sidebarT("current_section_aria", { name: title }) : title}
        data-testid={`settings-nav-group-${group.key}`}
        {...menu.getReferenceProps()}
      >
        {first ? itemIconSized(first, 20) : null}
        <span className="unified-sidebar-tooltip" aria-hidden="true">
          {title}
        </span>
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({ className: "unified-sidebar-flyout is-nav", "aria-label": title })}
        >
          <div className="unified-sidebar-flyout-title" aria-hidden="true">
            {title}
          </div>
          {group.items.map((item) => (
            <SettingsCompactLink
              key={item.key}
              item={item}
              active={settingsItemIsActive(item, active)}
              count={item.counter ? counters[item.counter] : undefined}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function itemIconSized(item: SettingsNavItem, size: number): ReactNode {
  if (item.destination.kind === "admin") return decorativeIcon(administrationIcon(item.destination.id));
  if (item.destination.kind === "center") {
    const Icon = CENTER_ICONS[item.destination.id];
    return <Icon size={size} aria-hidden />;
  }
  const Icon = TAB_ICONS[item.destination.id] ?? Building;
  return <Icon size={size} aria-hidden />;
}

function SettingsCompactLink({
  item,
  active,
  count,
  onNavigate,
}: {
  item: SettingsNavItem;
  active: boolean;
  count: number | undefined;
  onNavigate: SettingsSidebarNavProps["onNavigate"];
}) {
  const tabT = useTranslations("settings.tabs");
  const sidebarT = useTranslations("sidebar");
  const itemProps = useMenuItem({
    onSelect: (event) => onNavigate(event as MouseEvent<HTMLAnchorElement>, item.route),
  });
  return (
    <a
      href={appPath(item.route)}
      className={clsx("unified-sidebar-flyout-item", "unified-settings-flyout-item", { "is-active": active })}
      aria-current={active ? "page" : undefined}
      {...itemProps}
    >
      <span>{itemLabel(item, tabT, sidebarT)}</span>
      {item.counter ? <SidebarNavCounter counter={item.counter} value={count} /> : null}
    </a>
  );
}
