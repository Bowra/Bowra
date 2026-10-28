"use client";

import { createContext, useContext, useEffect, useMemo, useState, type MouseEvent } from "react";
import { useTranslations } from "next-intl";
import { Plus } from "lucide-react";

import { PageActionLink, PageFrame, PageHeader, PageMobileAction } from "@/components/shell/page-frame";
import { RowActionsMenu, type RowAction } from "@/components/ui/row-actions-menu";
import { useAuthSession, useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { teamRoleForUser } from "@/lib/settings-navigation";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";

/** Page whose name titles the phone top bar (P2 batch 6, brand appears once). */
export type AgentPageName = "marketplace" | "manage" | "create" | "charts" | "dashboard";

/** Independent Agent pages available from the sidebar. */
export type AgentMainPage = "marketplace" | "manage" | "charts";

const AGENT_MAIN_PAGES: readonly AgentMainPage[] = ["marketplace", "manage", "charts"];

function isMainPage(page: AgentPageName): page is AgentMainPage {
  return (AGENT_MAIN_PAGES as readonly string[]).includes(page);
}

const PAGE_TITLE_KEYS: Record<Exclude<AgentPageName, AgentMainPage>, "create_agent" | "dashboard_title"> = {
  create: "create_agent",
  dashboard: "dashboard_title",
};

/** The page header reports whether Create Agent is allowed; the frame mirrors it into the phone top bar. */
const AgentHeaderContext = createContext<((canCreate: boolean) => void) | null>(null);

export function AgentPageFrame({
  children,
  allowOwnerDeepLink = false,
  adminOnly = false,
  page = "marketplace",
}: {
  children: React.ReactNode;
  allowOwnerDeepLink?: boolean;
  adminOnly?: boolean;
  page?: AgentPageName;
}) {
  const t = useTranslations("agents_marketplace");
  const pageT = useTranslations("agents_page");
  const router = useAppRouter();
  const session = useAuthSession();
  const marketplaceEnabled = Boolean(
    session.me?.capabilities?.subagent_management_v2,
  );
  const role = teamRoleForUser(session.me);
  const adminAllowed = !adminOnly || (session.status === "authenticated" && (role === "owner" || role === "team_admin"));
  const canManageBuiltins = (role === "owner" || role === "team_admin") && Boolean(session.me?.capabilities?.subagent_creator);
  const [ownerDeepLink, setOwnerDeepLink] = useState(false);
  const [headerCanCreate, setHeaderCanCreate] = useState(false);
  const menuActions = useAgentHeaderMenuActions();

  useEffect(() => {
    if (adminOnly && session.status !== "authenticated") return;
    if (!adminAllowed) {
      router.replace("/agents/");
      return;
    }
    const params = new URLSearchParams(window.location.search);
    const hasAgentId = allowOwnerDeepLink && Boolean(params.get("agent_id")) && !params.has("release_candidate");
    setOwnerDeepLink(hasAgentId);
    if (!marketplaceEnabled && !hasAgentId) {
      router.replace(canManageBuiltins ? "/team/settings/subagents/" : "/capabilities/");
    }
  }, [adminOnly, adminAllowed, session.status, allowOwnerDeepLink, marketplaceEnabled, canManageBuiltins, router]);

  // Every independent page names itself in the top bar.
  const mobileTitle = isMainPage(page) ? page === "charts" ? t("charts_title") : pageT(page === "manage" ? "tab_mine" : "tab_market") : t(PAGE_TITLE_KEYS[page]);

  return (
    // Independent Agent pages share the same frame and action placement.
    <PageFrame
      title={mobileTitle}
      mobileActions={isMainPage(page) && (headerCanCreate || menuActions.length) ? (
        <>
          {menuActions.length ? <RowActionsMenu className="am-mobile-more" label={pageT("more_actions")} actions={menuActions} /> : null}
          {headerCanCreate ? <PageMobileAction href="/agents/new/" label={t("create_agent")} icon={Plus} /> : null}
        </>
      ) : undefined}
    >
      <AgentHeaderContext.Provider value={setHeaderCanCreate}>
        {adminAllowed && (marketplaceEnabled || ownerDeepLink) ? (
          children
        ) : (
          <div className="am-page am-detail-page" aria-busy="true">
            <div className="am-detail-loading" />
          </div>
        )}
      </AgentHeaderContext.Provider>
    </PageFrame>
  );
}

/** Header ⋯ items (D2): Agent overview and built-in configuration, each with its existing visibility. */
export function useAgentHeaderMenuActions(): RowAction[] {
  const pageT = useTranslations("agents_page");
  const centers = useTranslations("sidebar.centers");
  const router = useAppRouter();
  const session = useOptionalAuthSession();
  const role = teamRoleForUser(session?.me);
  const admin = role === "owner" || role === "team_admin";
  const dashboardVisible = session?.status === "authenticated"
    && Boolean(session.me?.capabilities?.subagent_management_v2)
    && admin;
  const builtinsVisible = admin && Boolean(session?.me?.capabilities?.subagent_creator);
  // Navigation items are real links (menu contract): a plain click navigates in-app, modified clicks open new tabs.
  const link = (path: string) => ({
    href: appPath(path),
    onSelect: (event: MouseEvent<HTMLElement>) => {
      // The menu may already have prevented the default action while closing, so judge only the modifiers here.
      if (!shouldHandleAppLinkClick({ ...event, defaultPrevented: false, button: event.button ?? 0 })) return;
      event.preventDefault();
      router.push(path);
    },
  });
  return useMemo(() => [
    ...(dashboardVisible ? [{ id: "dashboard", label: pageT("menu_overview"), ...link("/agents/dashboard/") }] : []),
    ...(builtinsVisible ? [{ id: "builtins", label: centers("manage_builtins"), ...link("/team/settings/subagents/") }] : []),
  ], [builtinsVisible, centers, dashboardVisible, pageT, router]); // eslint-disable-line react-hooks/exhaustive-deps
}

/** Shared page actions with a distinct title for each Agent destination. */
export function AgentPageHeader({
  page,
  canCreate,
  description,
}: {
  page: AgentMainPage;
  canCreate: boolean;
  description: string;
}) {
  const t = useTranslations("agents_marketplace");
  const pageT = useTranslations("agents_page");
  const setHeaderCanCreate = useContext(AgentHeaderContext);
  const menuActions = useAgentHeaderMenuActions();

  useEffect(() => {
    setHeaderCanCreate?.(canCreate);
  }, [canCreate, setHeaderCanCreate]);
  useEffect(() => () => setHeaderCanCreate?.(false), [setHeaderCanCreate]);

  return (
    <>
      <PageHeader
        title={page === "charts" ? t("charts_title") : pageT(page === "manage" ? "tab_mine" : "tab_market")}
        description={description}
        moreActions={menuActions.length ? (
          <RowActionsMenu className="am-page-header-more" label={pageT("more_actions")} actions={menuActions} />
        ) : null}
        primary={canCreate ? (
          <PageActionLink href="/agents/new/" icon={Plus} className="am-page-create">
            {t("create_agent")}
          </PageActionLink>
        ) : null}
      />
    </>
  );
}
