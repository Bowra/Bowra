"use client";

/**
 * V5 mobile fix · SessionsDrawer + Provider.
 *
 * Near-fullscreen mobile hamburger drawer (Claude/Kimi style):
 *   - Slide from left at 85% width, translucent mask on remaining 15%
 *   - Navigation (Memory / Skills / Reports / Settings) + session list
 *   - Mask click / ESC dismiss
 *   - Never render on desktop (CSS @media gate)
 *
 * State/data:
 *   - SessionsDrawerContext opens/closes (useSessionsDrawer().open() on any page)
 *   - ProductShell injects current sessions/actions; fallback fetch only without page binding
 *   - Picking session navigates /?session=<id> and autocloses
 *
 * Matches docs/V5-mobile-fix-tech-design.md §2.2.
 */

import {
  createContext, useCallback, useContext, useEffect,
  useMemo, useRef, useState, type MouseEvent as ReactMouseEvent,
} from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import clsx from "clsx";

import { appPath, basePath } from "@/lib/base-path";
import {
  archiveSession,
  deleteSession,
  pinSession,
  renameSession,
  restoreSession,
  undeleteSession,
  unpinSession,
} from "@/lib/api";
import { fetchMe, type MeUser } from "@/lib/auth-api";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import {
  activeSidebarDestination,
  resolveSidebarActiveContext,
  visibleSidebarNavigation,
  type SidebarGroupId,
} from "@/lib/sidebar-nav";
import type {
  NewWorkResult,
  UnifiedSidebarSessions,
} from "@/components/unified-sidebar";
import { BrandIdentity } from "@/components/brand-identity";
import { useBrand } from "@/components/brand-provider";
import { WorkRecovery } from "@/components/sidebar/work-recovery";
import { sessionListStatus } from "@/components/sidebar/session-list-status";
import { Plus, Caret, Doc, Folder, Spark, Bot } from "@/components/icons";
import { DialogShell } from "@/components/ui/dialog";
import { UserMenu } from "@/components/user-menu";
import { ShellGlobalControls } from "@/components/shell/global-controls";
import {
  confirmCanvasNavigation,
  hasCanvasUnsavedChanges,
} from "@/lib/canvas-navigation-guard";
import { trackInteraction as track } from "@/lib/metrics";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import {
  unifiedWorkResultHref,
  type UnifiedWorkSearchItem,
} from "@/lib/unified-work-search";
import { lockBodyScroll } from "@/lib/body-scroll-lock";
import { useAppRouter } from "@/lib/use-app-router";
import { notifySessionListChanged } from "@/lib/session-list-events";
import { useSessionList } from "@/lib/use-sessions";
import { olderSessionsPaging } from "@/components/sidebar/older-sessions-row";
import { CharIcon } from "@/components/char-icon";
import {
  SettingsNavModelScope,
  SettingsSidebarNav,
  SidebarNavCounter,
  type SettingsCounters,
} from "@/components/settings-sidebar-nav";
import { navigationContextForRoute, navigationActive } from "@/lib/navigation-centers";
import {
  settingsActiveForRoute,
  type SettingsTabId,
} from "@/lib/settings-destinations";


interface DrawerCtxValue {
  isOpen: boolean;
  /** `opener`: opening element. Safari button click gives no focus; activeElement alone loses trigger. */
  open: (opener?: HTMLElement | null) => void;
  close: (options?: { restoreFocus?: boolean }) => HTMLElement | null;
  /** Focus-return element (focus before opening); `close({ restoreFocus: false })` clears it. */
  returnFocusRef: React.RefObject<HTMLElement | null>;
}

const SessionsDrawerContext = createContext<DrawerCtxValue>({
  isOpen: false,
  open: () => { /* noop */ },
  close: () => null,
  returnFocusRef: { current: null },
});


export function useSessionsDrawer() {
  return useContext(SessionsDrawerContext);
}


export function SessionsDrawerProvider({ children }: { children: React.ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const open = useCallback((opener?: HTMLElement | null) => {
    // Accept elements only; accidental click-event opener falls back to activeElement.
    returnFocusRef.current = opener instanceof HTMLElement ? opener : (document.activeElement as HTMLElement | null);
    setIsOpen(true);
  }, []);
  const close = useCallback((options?: { restoreFocus?: boolean }) => {
    const returnFocus = returnFocusRef.current;
    // Drawer DialogShell restores focus from returnFocusRef on unmount.
    if (options?.restoreFocus === false) returnFocusRef.current = null;
    setIsOpen(false);
    return returnFocus;
  }, []);

  // Body scroll lock when open. Drawer DialogShell handles Esc (open account menu closes only menu).
  useEffect(() => {
    if (!isOpen) return;
    return lockBodyScroll();
  }, [isOpen]);

  const ctx = useMemo<DrawerCtxValue>(
    () => ({ isOpen, open, close, returnFocusRef }),
    [isOpen, open, close],
  );

  return (
    <SessionsDrawerContext.Provider value={ctx}>
      {children}
    </SessionsDrawerContext.Provider>
  );
}


// ─── Drawer body ─────────────────────────────────────────────

interface SessionsDrawerProps {
  onNewSession: () => NewWorkResult | Promise<NewWorkResult>;
  /** Full-page navigation starts only after onNewSession returns; see same UnifiedSidebar property. */
  navigationPending?: boolean;
  sessions?: UnifiedSidebarSessions;
  archiveEnabled?: boolean;
  /** Settings tab rendered by the current page; see UnifiedSidebar. */
  settingsActiveTab?: SettingsTabId;
}

export function SessionsDrawer({
  onNewSession,
  navigationPending = false,
  sessions,
  archiveEnabled = false,
  settingsActiveTab,
}: SessionsDrawerProps) {
  const { isOpen, close, returnFocusRef } = useSessionsDrawer();
  const router = useAppRouter();
  const { brand } = useBrand();
  const authSession = useOptionalAuthSession();
  const tNav = useTranslations("sidebar");  // Navigation reuses sidebar.* keys.
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [fallbackMe, setFallbackMe] = useState<MeUser | null>(null);
  const me = authSession?.me ?? fallbackMe;
  const [query, setQuery] = useState("");
  const [openGroups, setOpenGroups] = useState<SidebarGroupId[]>([]);
  const [startingNewSession, setIsStartingNewSession] = useState(false);
  const isStartingNewSession = startingNewSession || navigationPending;
  const scrimRef = useRef<HTMLButtonElement | null>(null);

  // The list is owned by `sessionsResource` (lib/use-sessions), shared with the
  // sidebar and the workspace page: opening the drawer reuses whatever they
  // already loaded instead of issuing a third request for the same list. Its
  // refresh triggers (turn done, window focus, 10s interval) live in the
  // resource, so they fire once for every mounted surface.
  // P2 batch 4: settings routes show the settings content in the drawer (the settings page has no section
  // <select>); the session list is not shown there.
  const navigationContext = navigationContextForRoute(pathname, searchParams.toString(), basePath());
  const settingsMode = navigationContext !== "work";
  const settingsSearch = searchParams.size ? `?${searchParams.toString()}` : "";
  const settingsActive = useMemo(() => {
    const routeActive = settingsActiveForRoute(pathname, settingsSearch, basePath());
    const active = settingsActiveTab && !routeActive.admin ? { tab: settingsActiveTab, admin: null } : routeActive;
    return navigationActive(active, navigationContext, pathname ?? "/", settingsSearch, basePath());
  }, [navigationContext, pathname, settingsActiveTab, settingsSearch]);
  const fetched = useSessionList(0, !sessions && isOpen && !settingsMode);
  const loading = fetched.loading;
  const loadError = fetched.error;

  // current session id from URL search params (workspace page only)
  const currentSessionId =
    sessions?.currentSessionId ?? searchParams.get("session");
  const groups = sessions?.groups ?? fetched.groups;
  const archivedCount =
    sessions?.archivedCount ?? fetched.archivedCount;
  const olderSessions = sessions
    ? sessions.olderSessions
    : olderSessionsPaging(fetched);

  const refetchSessions = useCallback(() => {
    if (sessions) return;
    notifySessionListChanged();
  }, [sessions]);

  useEffect(() => {
    if (authSession || !isOpen || fallbackMe) return;
    let cancelled = false;
    void fetchMe()
      .then((user) => {
        if (!cancelled) setFallbackMe(user);
      })
      .catch(() => {
        // Keep the least-privileged navigation when identity is unavailable.
      });
    return () => {
      cancelled = true;
    };
  }, [authSession, fallbackMe, isOpen]);

  // ξ D1 (2026-05-11) refresh triggers (turn done / sessions-changed / window
  // focus) now belong to sessionsResource, effective with drawer open without
  // multiplying requests by mounted surfaces. Never independently listen to SESSION_LIST_CHANGED_EVENT:
  // refetchSessions() broadcasts that event to refresh; self-listening creates infinite feedback loop.

  const handlePickSession = useCallback(async (sid: string) => {
    if (sessions) {
      const result = await sessions.onPick(sid);
      if (result === "cancelled") return;
      close();
      return;
    }
    if (typeof window === "undefined") return;
    if (sid !== currentSessionId && !(await confirmCanvasNavigation())) return;
    // navigate to workspace with this session
    window.location.href = appPath(`/?session=${encodeURIComponent(sid)}`);
    close();
  }, [close, currentSessionId, sessions]);

  const handleTogglePin = useCallback(
    async (sid: string, currentlyPinned: boolean) => {
      if (sessions?.onTogglePin) {
        await sessions.onTogglePin(sid, currentlyPinned);
        return;
      }
      if (currentlyPinned) await unpinSession(sid);
      else await pinSession(sid);
      refetchSessions();
    },
    [refetchSessions, sessions],
  );

  const handleArchive = useCallback(
    async (sid: string) => {
      if (sessions?.onArchive) return sessions.onArchive(sid);
      if (
        sid === currentSessionId &&
        !(await confirmCanvasNavigation())
      ) {
        return "cancelled" as const;
      }
      await archiveSession(sid);
      notifySessionListChanged();
      if (sid === currentSessionId) {
        try {
          router.replace("/");
        } catch {
          try {
            window.location.replace(appPath("/"));
          } catch {
            // The mutation is already durable; keep the archive success state.
          }
        }
      }
      return "success" as const;
    },
    [currentSessionId, router, sessions],
  );

  const handleRestore = useCallback(
    async (sid: string) => {
      if (sessions?.onRestore) {
        await sessions.onRestore(sid);
        return;
      }
      await restoreSession(sid);
      notifySessionListChanged();
    },
    [sessions],
  );

  // feedback-triage-2026-08-24: session soft deletion (same semantics as unified-sidebar).
  const handleDelete = useCallback(
    async (sid: string) => {
      if (sessions?.onDelete) return sessions.onDelete(sid);
      if (
        sid === currentSessionId &&
        !(await confirmCanvasNavigation())
      ) {
        return "cancelled" as const;
      }
      await deleteSession(sid);
      notifySessionListChanged();
      if (sid === currentSessionId) {
        try {
          router.replace("/");
        } catch {
          try {
            window.location.replace(appPath("/"));
          } catch {
            // Server deletion succeeded; navigation failure must not be reported as deletion failure.
          }
        }
      }
      return "success" as const;
    },
    [currentSessionId, router, sessions],
  );

  const handleUndelete = useCallback(
    async (sid: string) => {
      if (sessions?.onUndelete) {
        await sessions.onUndelete(sid);
        return;
      }
      await undeleteSession(sid);
      notifySessionListChanged();
    },
    [sessions],
  );

  const handleNewSession = useCallback(async () => {
    if (isStartingNewSession) return;
    setIsStartingNewSession(true);
    let navigationStarted = false;
    try {
      const result = await onNewSession();
      if (result === "cancelled") return;
      if (result === "navigating") {
        navigationStarted = true;
        return;
      }
      close();
    } finally {
      if (!navigationStarted) setIsStartingNewSession(false);
    }
  }, [close, isStartingNewSession, onNewSession]);

  const unifiedActive = resolveSidebarActiveContext({
    pathname,
    search: searchParams.size ? `?${searchParams.toString()}` : "",
    appBasePath: basePath(),
    currentSessionId,
  }).destination;
  const unifiedActiveLeafId = unifiedActive?.leafId ?? null;
  const unifiedActivePrimaryId = unifiedActive?.primaryId ?? null;
  const unifiedNavigation = useMemo(
    () => visibleSidebarNavigation({
      teamRole: me?.team_role ?? (me?.is_team_admin ? "team_admin" : "member"),
      capabilities: me?.capabilities,
      authenticated: Boolean(me),
      activeAgentId: me && unifiedActivePrimaryId === "agents" && !searchParams.has("release_candidate")
        ? searchParams.get("agent_id") : null,
    }),
    [me, searchParams, unifiedActivePrimaryId],
  );

  useEffect(() => {
    if (
      !isOpen ||
      unifiedActivePrimaryId !== "library" && unifiedActivePrimaryId !== "capabilities" && unifiedActivePrimaryId !== "agents"
    ) {
      return;
    }
    setOpenGroups((current) =>
      current.includes(unifiedActivePrimaryId)
        ? current
        : [...current, unifiedActivePrimaryId],
    );
  }, [isOpen, unifiedActivePrimaryId]);

  const handleUnifiedNav = useCallback(
    async (route: string) => {
      if (hasCanvasUnsavedChanges() && !(await confirmCanvasNavigation())) return;
      const [routePath, routeQuery = ""] = route.split("?");
      const destination = activeSidebarDestination(
        routePath,
        routeQuery ? `?${routeQuery}` : "",
      );
      if (destination) {
        track("bowhead_nav_destination_opened_total", {
          primary: destination.primaryId,
          leaf: destination.leafId,
          surface: "drawer",
          role:
            me?.team_role ??
            (me?.is_team_admin ? "team_admin" : "member"),
        });
      }
      close();
      window.location.href = appPath(route);
    },
    [close, me],
  );

  const handleSearchResult = useCallback(
    (
      event: ReactMouseEvent<HTMLAnchorElement>,
      item: UnifiedWorkSearchItem,
    ) => {
      if (!shouldHandleAppLinkClick(event)) return;
      event.preventDefault();
      if (item.kind === "session") {
        void handlePickSession(item.session_id ?? item.id);
        return;
      }
      void handleUnifiedNav(unifiedWorkResultHref(item));
    },
    [handlePickSession, handleUnifiedNav],
  );

  const drawerNavigation = (counters: SettingsCounters = {}) => (
    <>
      <div className="unified-sidebar-section-label">{tNav("work")}</div>
      <nav
        className="unified-drawer-nav"
        aria-label={tNav("primary_navigation")}
      >
        {unifiedNavigation.map((item) => {
          if (item.kind === "action") {
            const active = unifiedActiveLeafId === item.id;
            return (
              <button
                key={item.id}
                type="button"
                className={clsx("unified-drawer-nav-item", {
                  "is-active": active,
                })}
                aria-current={active ? "page" : undefined}
                aria-busy={isStartingNewSession || undefined}
                disabled={isStartingNewSession}
                data-testid={`unified-drawer-nav-${item.id}`}
                onClick={() => void handleNewSession()}
              >
                <Plus size={20} aria-hidden />
                {tNav(item.labelKey)}
              </button>
            );
          }
          if (item.kind === "direct") {
            return (
              <a
                key={item.id}
                href={appPath(item.route)}
                className={clsx("unified-drawer-nav-item", {
                  "is-active": unifiedActivePrimaryId === item.id,
                })}
                aria-current={
                  unifiedActivePrimaryId === item.id ? "page" : undefined
                }
                data-testid={`unified-drawer-nav-${item.id}`}
                onClick={(event) => {
                  if (!shouldHandleAppLinkClick(event)) return;
                  event.preventDefault();
                  void handleUnifiedNav(item.route);
                }}
              >
                {item.id === "agents" ? <Bot size={20} aria-hidden /> : <Doc size={20} aria-hidden />}
                {tNav(item.labelKey)}
              </a>
            );
          }
          const open = openGroups.includes(item.id);
          return (
            <div className="unified-drawer-nav-group" key={item.id}>
              <div
                className={clsx("unified-drawer-nav-group-row", {
                  "is-active": unifiedActivePrimaryId === item.id,
                })}
              >
                {item.expandOnly ? <button
                  type="button"
                  className="unified-drawer-nav-item is-group-link"
                  aria-expanded={open}
                  aria-controls={`unified-drawer-group-${item.id}`}
                  data-testid={`unified-drawer-nav-${item.id}`}
                  onClick={() => setOpenGroups((current) => current.includes(item.id) ? current.filter((id) => id !== item.id) : [...current, item.id])}
                >
                  <Folder size={20} aria-hidden />
                  {tNav(item.labelKey)}
                </button> : (
                <a
                  href={appPath(item.route)}
                  className="unified-drawer-nav-item is-group-link"
                  aria-current={
                    unifiedActiveLeafId === item.id ? "page" : undefined
                  }
                  data-testid={`unified-drawer-nav-${item.id}`}
                  onClick={(event) => {
                    if (!shouldHandleAppLinkClick(event)) return;
                    event.preventDefault();
                    void handleUnifiedNav(item.route);
                  }}
                >
                  {item.id === "library" ? (
                    <Folder size={20} aria-hidden />
                  ) : item.id === "agents" ? (
                    <Bot size={20} aria-hidden />
                  ) : (
                    <Spark size={20} aria-hidden />
                  )}
                  {tNav(item.labelKey)}
                </a>
                )}
                <button
                  type="button"
                  className="unified-drawer-group-toggle"
                  onClick={() =>
                    setOpenGroups((current) =>
                      current.includes(item.id)
                        ? current.filter((id) => id !== item.id)
                        : [...current, item.id],
                    )
                  }
                  aria-label={tNav(
                    open ? "collapse_group" : "expand_group",
                    { name: tNav(item.labelKey) },
                  )}
                  aria-expanded={open}
                  aria-controls={`unified-drawer-group-${item.id}`}
                  data-testid={`unified-drawer-nav-${item.id}-toggle`}
                >
                  <Caret aria-hidden />
                </button>
              </div>
              {open ? (
                <div
                  id={`unified-drawer-group-${item.id}`}
                  className="unified-drawer-subnav"
                >
                  {item.children.map((child) => (
                    <a
                      key={child.id}
                      href={appPath(child.route)}
                      className={clsx("unified-drawer-subnav-item", {
                        "is-active": unifiedActiveLeafId === child.id,
                      })}
                      aria-current={
                        unifiedActiveLeafId === child.id
                          ? "page"
                          : undefined
                      }
                      data-testid={`unified-drawer-nav-${child.id}`}
                      onClick={(event) => {
                        if (!shouldHandleAppLinkClick(event)) return;
                        event.preventDefault();
                        void handleUnifiedNav(child.route);
                      }}
                    >
                      <span>{tNav(child.labelKey)}</span>
                      {child.counter ? <SidebarNavCounter counter={child.counter} value={counters[child.counter]} /> : null}
                    </a>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </nav>
    </>
  );

  if (!isOpen) return null;

  return (
    <div
      className="v2-mobile-drawer-mask open"
      role="presentation"
    >
      <button
        ref={scrimRef}
        type="button"
        className="v2-mobile-drawer-scrim"
        onClick={() => close()}
        tabIndex={-1}
        aria-hidden="true"
        data-testid="sessions-drawer-mask"
      />
      {/* DialogShell owns focus trap, Esc, background inert, focus return; backdrop is panel sibling,
          staying clickable with own onClick. Account menu joins FloatingTree as child, immune to inert;
          Esc with menu open closes menu only. */}
      <DialogShell
        mount="inline"
        overlay="none"
        closeOnBackdrop={false}
        onClose={() => close()}
        label={tNav("navigation_drawer")}
        panelClassName="v2-mobile-drawer open"
        panelProps={{ "data-testid": "sessions-drawer" } as React.HTMLAttributes<HTMLDivElement>}
        returnFocusRef={returnFocusRef}
        getInsideElements={() => (scrimRef.current ? [scrimRef.current] : [])}
      >
        <header className="v2-mobile-drawer-head">
          <a
            href={appPath("/")}
            className="v2-mobile-drawer-brand"
            aria-label={brand.applicationName}
            onClick={(event) => {
              if (!shouldHandleAppLinkClick(event)) return;
              event.preventDefault();
              void handleUnifiedNav("/");
            }}
          >
            <BrandIdentity
              size={28}
              graphicClassName="v2-mobile-drawer-brand-logo"
            />
          </a>
          <button
            type="button"
            data-dialog-initial-focus=""
            className="v2-mobile-drawer-close"
            aria-label={tNav("close_drawer")}
            onClick={() => close()}
            data-testid="sessions-drawer-close"
          >
            <CharIcon ch="✕" />
          </button>
        </header>

        <div className="v2-mobile-drawer-rail">
          {navigationContext === "capabilities" ? (
            <SettingsNavModelScope context={navigationContext} me={me} active={settingsActive} refreshKey={`${pathname ?? ""}${settingsSearch}`}>
              {(model) => drawerNavigation(model.counters)}
            </SettingsNavModelScope>
          ) : settingsMode ? (
            <SettingsNavModelScope
          context={navigationContext}
              me={me}
              active={settingsActive}
              refreshKey={`${pathname ?? ""}${settingsSearch}`}
            >
              {(model) => (
                <SettingsSidebarNav
                  model={model}
                  surface="drawer"
                  onNavigate={(event, route) => {
                    if (!shouldHandleAppLinkClick(event)) return;
                    event.preventDefault();
                    void handleUnifiedNav(route);
                  }}
                />
              )}
            </SettingsNavModelScope>
          ) : (
          <WorkRecovery
            groups={groups}
            currentSessionId={currentSessionId}
            workspaceId={sessions?.workspaceId}
            query={query}
            onQueryChange={setQuery}
            onPick={(sessionId) => void handlePickSession(sessionId)}
            onTogglePin={handleTogglePin}
            archiveEnabled={archiveEnabled}
            archivedCount={archivedCount}
            onArchive={archiveEnabled ? handleArchive : undefined}
            onRestore={archiveEnabled ? handleRestore : undefined}
            onDelete={handleDelete}
            onUndelete={handleUndelete}
            onRename={sessions?.onRename ?? renameSession}
            onNavigateSearchResult={handleSearchResult}
            surface="drawer"
            navigationSlot={drawerNavigation()}
            olderSessions={olderSessions}
            listStatusSlot={sessionListStatus({
              loading: sessions ? Boolean(sessions.loading) : loading,
              error: sessions ? Boolean(sessions.error) : loadError,
              stalledOn: sessions ? (sessions.stalledOn ?? null) : fetched.stalledOn,
              hasRows: groups.some((group) => group.items.length > 0),
              // The page-supplied list owns its retry; the drawer's own list retries through its hook (which also
              // re-requests a pending Space list). `refetchSessions` is a no-op when the page supplies the list.
              onRetry: sessions ? (sessions.onRetry ?? notifySessionListChanged) : fetched.retry,
            })}
          />
          )}
        </div>

        {/* Account preferences remain reachable through the unified user menu. */}
        <footer className="v2-mobile-drawer-foot">
          {/* P2 batch 6: the version lives in the account menu's last line and Help › About. */}
          {/* <768: desktop Topbar credits badge mounts here (placement table: shell/global-controls). */}
          <div className="v2-mobile-drawer-credits">
            <ShellGlobalControls surface="mobile-drawer" />
          </div>
          <UserMenu
            initials="N"
            user={me}
            showIdentity
            onBeforePersonalSettingsNavigate={() => close({ restoreFocus: false })}
          />
        </footer>
      </DialogShell>
    </div>
  );
}
