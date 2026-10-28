"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ComponentType,
  type HTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
  type SVGProps,
} from "react";
import {
  FloatingFocusManager,
  FloatingNode,
  FloatingTree,
  useDismiss,
  useFloating,
  useFloatingNodeId,
  useFloatingParentNodeId,
  useInteractions,
  useMergeRefs,
} from "@floating-ui/react";
import clsx from "clsx";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { flushSync } from "react-dom";

import {
  WorkRecovery,
  type SessionMutationResult,
} from "@/components/sidebar/work-recovery";
import { sessionListStatus } from "@/components/sidebar/session-list-status";
import { DialogShell } from "@/components/ui/dialog";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { UserMenu } from "@/components/user-menu";
import {
  BrandGraphic,
  BrandIdentity,
} from "@/components/brand-identity";
import { useBrand } from "@/components/brand-provider";
import {
  Doc,
  Folder,
  History,
  Plus,
  Search,
  Side,
  Spark,
  Bot,
  Caret,
} from "@/components/icons";
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
import { appPath, basePath } from "@/lib/base-path";
import {
  confirmCanvasNavigation,
  hasCanvasUnsavedChanges,
} from "@/lib/canvas-navigation-guard";
import {
  activeSidebarDestination,
  resolveSidebarActiveContext,
  visibleSidebarNavigation,
  type SidebarGroupId,
  type SidebarGroupItem,
  type SidebarPrimaryId,
  type SidebarPrimaryItem,
} from "@/lib/sidebar-nav";
import {
  createSidebarState,
  deriveSidebarPresentation,
  readBootstrappedDesktopMode,
  readPreferredDesktopMode,
  readStoredPreferredDesktopMode,
  SIDEBAR_OVERLAY_BREAKPOINT,
  sidebarAutoCompactActive,
  sidebarDockedModeApplies,
  sidebarTabletBand,
  sidebarReducer,
  type SidebarDesktopMode,
  writeBootstrappedDesktopMode,
  writePreferredDesktopMode,
} from "@/lib/sidebar-state";
import { navigationContextForRoute, navigationActive } from "@/lib/navigation-centers";
import {
  rememberWorkRoute,
  settingsActiveForRoute,
  type SettingsGroupKey,
  type SettingsTabId,
} from "@/lib/settings-destinations";
import { useSidebarAutoCompactRequested } from "@/lib/sidebar-auto-compact";
import { onSidebarOverlayRequest } from "@/lib/sidebar-overlay-request";
import {
  SettingsCompactNavigation,
  SettingsNavModelScope,
  SettingsSidebarNav,
  SidebarNavCounter,
  type SettingsCounters,
} from "@/components/settings-sidebar-nav";
import type { SessionGroup } from "@/lib/types";
import { trackInteraction as track } from "@/lib/metrics";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { notifySessionListChanged } from "@/lib/session-list-events";
import { useSessionList } from "@/lib/use-sessions";
import {
  olderSessionsPaging,
  type OlderSessionsPaging,
} from "@/components/sidebar/older-sessions-row";
import {
  unifiedWorkResultHref,
  type UnifiedWorkSearchItem,
} from "@/lib/unified-work-search";
import { lockBodyScroll } from "@/lib/body-scroll-lock";

export type SessionPickResult = "picked" | "cancelled";

export interface UnifiedSidebarSessions {
  groups: SessionGroup[];
  /** First load still pending (no cached list yet): the list shows skeleton rows instead of the empty state. */
  loading?: boolean;
  /** The list could not load and nothing is cached: an error row with Retry replaces the empty state. */
  error?: boolean;
  /** Retry after `error`; defaults to the shared session-list refresh. */
  onRetry?: () => void;
  /** The first load passed its bounded wait: names what is still pending for the error copy. */
  stalledOn?: "spaces" | "sessions" | null;
  /** Older-session paging of the same list (spec 4a); absent = the list has no "Show older" row. */
  olderSessions?: OlderSessionsPaging;
  currentSessionId: string | null;
  workspaceId?: string;
  archivedCount?: number;
  onPick: (
    sessionId: string,
  ) => SessionPickResult | Promise<SessionPickResult>;
  onTogglePin?: (
    sessionId: string,
    currentlyPinned: boolean,
  ) => void | Promise<void>;
  onArchive?: (
    sessionId: string,
  ) => SessionMutationResult | Promise<SessionMutationResult>;
  onRestore?: (sessionId: string) => void | Promise<void>;
  onDelete?: (
    sessionId: string,
  ) => SessionMutationResult | Promise<SessionMutationResult>;
  onUndelete?: (sessionId: string) => void | Promise<void>;
  /** Persist a session rename; defaults to the sessions API. */
  onRename?: (sessionId: string, title: string) => Promise<unknown>;
}

interface UnifiedSidebarProps {
  onNewSession: () => NewWorkResult | Promise<NewWorkResult>;
  /**
   * Shell full-page navigation starts after onNewSession returns (continue after leave confirmation). When true,
   * New button remains busy until page replacement.
   */
  navigationPending?: boolean;
  sessions?: UnifiedSidebarSessions;
  preferCompactDesktop?: boolean;
  archiveEnabled?: boolean;
  /** Settings tab rendered by the current page (SettingsShell), refining the route-derived highlight. */
  settingsActiveTab?: SettingsTabId;
}

export type NewWorkResult = "created" | "cancelled" | "navigating";

type ShortcutKeyEvent = Pick<
  KeyboardEvent,
  "key" | "code" | "metaKey" | "ctrlKey" | "preventDefault"
> & { shiftKey?: boolean };

type IconComponent = ComponentType<
  SVGProps<SVGSVGElement> & { size?: number }
>;

const PRIMARY_ICONS: Record<SidebarPrimaryId, IconComponent> = {
  new_work: Plus,
  reports: Doc,
  library: Folder,
  agents: Bot,
  capabilities: Spark,
};

export function UnifiedSidebar({
  onNewSession,
  navigationPending = false,
  sessions,
  preferCompactDesktop = false,
  archiveEnabled = false,
  settingsActiveTab,
}: UnifiedSidebarProps) {
  const { brand } = useBrand();
  const t = useTranslations("sidebar");
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const router = useAppRouter();
  const authSession = useOptionalAuthSession();
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window === "undefined" ? 1440 : window.innerWidth,
  );
  // P2 batch 4: the sidebar is persistent from 1024 (232 below 1280, expanded by default) and an open results
  // canvas collapses it to the icon rail below 1280; 768–1023 it is hidden and the top bar's menu button opens
  // it as an overlay; settings routes show the sidebar's settings content.
  const autoCompactRequested = useSidebarAutoCompactRequested();
  const layoutOptions = { autoCompact: autoCompactRequested };
  const [state, dispatch] = useReducer(
    sidebarReducer,
    { viewportWidth, preferCompactDesktop },
    ({ viewportWidth: width, preferCompactDesktop: compact }) =>
      createSidebarState(
        compact
          ? "compact"
          : readBootstrappedDesktopMode(width),
      ),
  );
  const [fallbackMe, setFallbackMe] = useState<MeUser | null>(null);
  const me = authSession?.me ?? fallbackMe;
  const [query, setQuery] = useState("");
  const [startingNewSession, setIsStartingNewSession] = useState(false);
  const isStartingNewSession = startingNewSession || navigationPending;
  const [openGroups, setOpenGroups] = useState<SidebarGroupId[]>([]);
  const [compactGroup, setCompactGroup] =
    useState<SidebarGroupId | null>(null);
  const [compactRecoveryMode, setCompactRecoveryMode] =
    useState<"search" | "recent">("recent");
  const [sessionsRefreshKey, setSessionsRefreshKey] = useState(0);
  const desktopSidebarVisible = viewportWidth >= SIDEBAR_OVERLAY_BREAKPOINT;
  const tabletBand = sidebarTabletBand(viewportWidth);
  const viewportBand =
    viewportWidth < SIDEBAR_OVERLAY_BREAKPOINT
      ? "mobile"
      : tabletBand
        ? "tablet"
        : viewportWidth < 1280
          ? "narrow"
          : "wide";
  const navigationContext = navigationContextForRoute(pathname, searchParams.toString(), basePath());
  // Capability Center and its resource pages are work routes (Header, Language & Paging Spec D9, P2: the mode
  // follows the route): the sidebar keeps search and the session list there; only highlighting and the open
  // Capability Center group follow the resource context.
  const capabilitiesContext = navigationContext === "capabilities";
  const settingsMode = navigationContext !== "work" && !capabilitiesContext;
  const fetchedSessions = useSessionList(
    sessionsRefreshKey,
    sessions === undefined && desktopSidebarVisible && !settingsMode,
  );
  const searchRef = useRef<HTMLInputElement | null>(null);
  const scrimRef = useRef<HTMLButtonElement | null>(null);
  const shortcutKeyDownRef = useRef<(event: ShortcutKeyEvent) => void>(
    () => {},
  );
  const overlayCloseRef = useRef<HTMLButtonElement | null>(null);
  const compactExpandRef = useRef<HTMLButtonElement | null>(null);
  const flyoutRef = useRef<HTMLDivElement | null>(null);
  const transientTriggerRef = useRef<HTMLButtonElement | null>(null);
  // The top bar's menu button that opened the 768–1023 overlay; focus returns to it.
  const overlayOpenerRef = useRef<HTMLElement | null>(null);
  const [overlayOpenedByKeyboard, setOverlayOpenedByKeyboard] =
    useState(false);
  const compactTransientOpenedByKeyboardRef = useRef(false);
  const pendingDesktopModeRef = useRef<SidebarDesktopMode | null>(
    null,
  );
  const presentation = deriveSidebarPresentation(state, viewportWidth, layoutOptions);
  const dockedModeApplies = sidebarDockedModeApplies(viewportWidth, layoutOptions);
  const autoCompactActive = sidebarAutoCompactActive(viewportWidth, layoutOptions);
  const groups = sessions?.groups ?? fetchedSessions.groups;
  const archivedCount =
    sessions?.archivedCount ?? fetchedSessions.archivedCount;
  const olderSessions = sessions
    ? sessions.olderSessions
    : olderSessionsPaging(fetchedSessions);
  // Polish spec 3b: never judge the list empty before the first load settles.
  const listStatusSlot = sessionListStatus({
    loading: sessions ? Boolean(sessions.loading) : fetchedSessions.loading,
    error: sessions ? Boolean(sessions.error) : fetchedSessions.error,
    stalledOn: sessions ? (sessions.stalledOn ?? null) : fetchedSessions.stalledOn,
    hasRows: groups.some((group) => group.items.length > 0),
    onRetry: sessions ? (sessions.onRetry ?? notifySessionListChanged) : fetchedSessions.retry,
  });
  const currentSearch = searchParams.toString();
  const currentSessionId =
    sessions?.currentSessionId ?? searchParams.get("session");
  const activeContext = resolveSidebarActiveContext({
    pathname,
    search: currentSearch ? `?${currentSearch}` : "",
    appBasePath: basePath(),
    currentSessionId,
  });
  const active = activeContext.destination;
  const compactRecentCurrent =
    presentation === "compact" &&
    state.workRecoveryOpen &&
    compactRecoveryMode === "recent";
  const activeLeafId = compactRecentCurrent
    ? null
    : (active?.leafId ?? null);
  const activePrimaryId = active?.primaryId ?? null;
  const settingsActive = useMemo(() => {
    const routeActive = settingsActiveForRoute(
      pathname,
      currentSearch ? `?${currentSearch}` : "",
      basePath(),
    );
    const active = settingsActiveTab && !routeActive.admin ? { tab: settingsActiveTab, admin: null } : routeActive;
    return navigationActive(active, navigationContext, pathname ?? "/", currentSearch, basePath());
  }, [currentSearch, navigationContext, pathname, settingsActiveTab]);
  const settingsRefreshKey = `${pathname ?? ""}?${currentSearch}`;
  const [compactSettingsGroup, setCompactSettingsGroup] =
    useState<SettingsGroupKey | null>(null);
  const navigation = useMemo(
    () =>
      visibleSidebarNavigation({
        teamRole: me?.team_role ?? (me?.is_team_admin ? "team_admin" : "member"),
        capabilities: me?.capabilities,
        authenticated: Boolean(me),
        activeAgentId: me && activePrimaryId === "agents" && !searchParams.has("release_candidate")
          ? searchParams.get("agent_id") : null,
      }),
    [activePrimaryId, me, searchParams],
  );
  const dismissOverlay = useCallback((restoreFocus: boolean) => {
    track("bowhead_sidebar_state_changed_total", {
      from: "overlay",
      to: "compact",
      viewport: "narrow_desktop",
      trigger: "control",
    });
    dispatch({ type: "set-overlay-open", open: false });
    if (restoreFocus) {
      window.setTimeout(() =>
        (overlayOpenerRef.current?.isConnected
          ? overlayOpenerRef.current
          : compactExpandRef.current
        )?.focus(),
      );
    }
  }, []);

  const closeOverlay = useCallback(
    (restoreFocus = true) => dismissOverlay(restoreFocus),
    [dismissOverlay],
  );

  const openOverlay = useCallback(
    (
      trigger: "control" | "shortcut" = "control",
      openedByKeyboard = trigger === "shortcut",
    ) => {
      setOverlayOpenedByKeyboard(openedByKeyboard);
      overlayOpenerRef.current = null;
      track("bowhead_sidebar_state_changed_total", {
        from: "compact",
        to: "overlay",
        viewport: "narrow_desktop",
        trigger,
      });
      dispatch({ type: "set-overlay-open", open: true });
    },
    [],
  );

  useEffect(() => {
    return onSidebarOverlayRequest((opener) => {
      openOverlay(
        "control",
        document.documentElement.dataset.bowheadInputModality === "keyboard",
      );
      overlayOpenerRef.current = opener;
    });
  }, [openOverlay]);

  const closeCompactTransient = useCallback((restoreFocus = true) => {
    dispatch({ type: "close-transient" });
    setCompactGroup(null);
    if (restoreFocus) {
      window.setTimeout(() => transientTriggerRef.current?.focus());
    }
  }, []);

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    const markPointer = () => {
      root.dataset.bowheadInputModality = "pointer";
    };
    const markKeyboard = () => {
      root.dataset.bowheadInputModality = "keyboard";
    };
    window.addEventListener("pointerdown", markPointer, true);
    window.addEventListener("keydown", markKeyboard, true);
    return () => {
      window.removeEventListener("pointerdown", markPointer, true);
      window.removeEventListener("keydown", markKeyboard, true);
      delete root.dataset.bowheadInputModality;
    };
  }, []);

  useEffect(() => {
    // Overlay/flyout state belongs to one responsive band. Clear it when the
    // shell crosses a breakpoint so resizing back never reopens stale UI.
    dispatch({ type: "close-transient" });
    setCompactGroup(null);
    setCompactSettingsGroup(null);
  }, [viewportBand, autoCompactActive]);

  // "Back to Work" in the settings content returns to the last work page of this tab.
  useEffect(() => {
    if (!pathname || navigationContext !== "work") return;
    const prefix = basePath().replace(/\/+$/, "");
    const internal =
      prefix && pathname.startsWith(`${prefix}/`) ? pathname.slice(prefix.length) : pathname;
    rememberWorkRoute(`${internal}${currentSearch ? `?${currentSearch}` : ""}`);
  }, [currentSearch, navigationContext, pathname]);

  useEffect(() => {
    if (authSession || !desktopSidebarVisible || fallbackMe) return;
    let cancelled = false;
    void fetchMe()
      .then((user) => {
        if (cancelled) return;
        setFallbackMe(user);
      })
      .catch(() => {
        // Navigation remains usable with the least-privileged schema.
      });
    return () => {
      cancelled = true;
    };
  }, [authSession, desktopSidebarVisible, fallbackMe]);

  useLayoutEffect(() => {
    if (!desktopSidebarVisible || !me) return;
    const pendingMode = pendingDesktopModeRef.current;
    const mode =
      pendingMode ??
      readStoredPreferredDesktopMode(me.id) ??
      (preferCompactDesktop
        ? "compact"
        : readPreferredDesktopMode(me.id, window.innerWidth));
    dispatch({
      type: "set-desktop-mode",
      mode,
    });
    if (pendingMode) {
      writePreferredDesktopMode(me.id, pendingMode);
      pendingDesktopModeRef.current = null;
    } else if (!readStoredPreferredDesktopMode(me.id)) {
      writeBootstrappedDesktopMode(mode);
    }
  }, [desktopSidebarVisible, me, preferCompactDesktop]);

  useEffect(() => {
    if (
      activePrimaryId !== "library" && activePrimaryId !== "capabilities" && activePrimaryId !== "agents"
    ) {
      return;
    }
    setOpenGroups((groupsOpen) =>
      groupsOpen.includes(activePrimaryId)
        ? groupsOpen
        : [...groupsOpen, activePrimaryId],
    );
  }, [activePrimaryId]);

  useEffect(() => {
    const onKeyDown = (event: ShortcutKeyEvent) => {
      if (!(event.metaKey || event.ctrlKey)) return;
      // ⌘⇧\ / Ctrl+Shift+\ maximizes Workbench Results. Some layouts report key "\" even with Shift;
      // skip shifted combinations here to avoid both firing.
      if (
        event.key === "\\" &&
        !event.shiftKey
      ) {
        event.preventDefault();
        if (viewportWidth < SIDEBAR_OVERLAY_BREAKPOINT) return;
        if (!dockedModeApplies) {
          if (state.overlayOpen) closeOverlay();
          else openOverlay("shortcut");
          return;
        }
        const mode =
          state.preferredDesktopMode === "expanded" ? "compact" : "expanded";
        track("bowhead_sidebar_state_changed_total", {
          from: state.preferredDesktopMode,
          to: mode,
          viewport: "wide_desktop",
          trigger: "shortcut",
        });
        dispatch({ type: "set-desktop-mode", mode });
        writeBootstrappedDesktopMode(mode);
        pendingDesktopModeRef.current = mode;
        if (me) {
          writePreferredDesktopMode(me.id, mode);
          pendingDesktopModeRef.current = null;
        }
      }
      if (
        event.key.toLocaleLowerCase() === "k" ||
        event.code === "KeyK"
      ) {
        event.preventDefault();
        if (presentation === "compact") {
          compactTransientOpenedByKeyboardRef.current = true;
          setCompactRecoveryMode("search");
          dispatch({ type: "set-work-recovery-open", open: true });
          return;
        }
        window.setTimeout(() => {
          searchRef.current?.focus();
          searchRef.current?.select();
        });
      }
    };
    // Expanded overlay uses DialogShell; panel keys do not bubble to window, forwarded by panel onKeyDown.
    shortcutKeyDownRef.current = onKeyDown;
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    closeOverlay,
    dockedModeApplies,
    me,
    openOverlay,
    presentation,
    preferCompactDesktop,
    state.overlayOpen,
    state.preferredDesktopMode,
    viewportWidth,
  ]);

  // DialogShell owns overlay initial focus, Tab loop, Esc, background inert.
  useEffect(() => {
    if (presentation !== "overlay") return;
    return lockBodyScroll();
  }, [presentation]);

  // Compact navigation groups use useMenu (CompactNavGroup); here manage only Recent work/Search overlay
  // initial focus. CompactRecoveryFlyout owns Esc/outside/focus-leave closing.
  useEffect(() => {
    if (presentation !== "compact" || !state.workRecoveryOpen) return;
    const focusFrame = window.requestAnimationFrame(() => {
      const shouldFocusSearch = compactRecoveryMode === "search";
      const initialFocus = shouldFocusSearch
        ? flyoutRef.current?.querySelector<HTMLElement>("input")
        : compactTransientOpenedByKeyboardRef.current
          ? flyoutRef.current?.querySelector<HTMLElement>(
              ".unified-work-item-main",
            )
          : flyoutRef.current;
      initialFocus?.focus();
      if (
        shouldFocusSearch &&
        initialFocus instanceof HTMLInputElement
      ) {
        initialFocus.select();
      }
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [compactRecoveryMode, presentation, state.workRecoveryOpen]);

  const setDesktopMode = (
    mode: "expanded" | "compact",
    openedByKeyboard = true,
  ) => {
    track("bowhead_sidebar_state_changed_total", {
      from: presentation,
      to: mode,
      viewport: "wide_desktop",
      trigger: "control",
    });
    dispatch({ type: "set-desktop-mode", mode });
    writeBootstrappedDesktopMode(mode);
    pendingDesktopModeRef.current = mode;
    if (me) {
      writePreferredDesktopMode(me.id, mode);
      pendingDesktopModeRef.current = null;
    }
    if (openedByKeyboard) {
      window.setTimeout(() => {
        if (mode === "compact") compactExpandRef.current?.focus();
        else overlayCloseRef.current?.focus();
      });
    }
  };

  const handleNewSession = async () => {
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
      if (presentation === "overlay") closeOverlay();
      else closeCompactTransient(false);
    } finally {
      if (!navigationStarted) setIsStartingNewSession(false);
    }
  };

  const preparePersonalSettingsNavigation = useCallback(() => {
    if (presentation !== "overlay") return;
    flushSync(() => dismissOverlay(false));
    return compactExpandRef.current;
  }, [dismissOverlay, presentation]);

  const handlePick = useCallback(
    async (sessionId: string) => {
      if (sessions) {
        const result = await sessions.onPick(sessionId);
        if (result === "cancelled") return;
      } else {
        if (
          sessionId !== currentSessionId &&
          !(await confirmCanvasNavigation())
        ) {
          return;
        }
        router.push(`/?session=${encodeURIComponent(sessionId)}`);
      }
      if (presentation === "overlay") {
        closeOverlay();
      }
      dispatch({ type: "set-work-recovery-open", open: false });
    },
    [closeOverlay, currentSessionId, presentation, router, sessions],
  );

  const handleTogglePin = useCallback(
    async (sessionId: string, currentlyPinned: boolean) => {
      if (sessions?.onTogglePin) {
        await sessions.onTogglePin(sessionId, currentlyPinned);
        return;
      }
      if (currentlyPinned) await unpinSession(sessionId);
      else await pinSession(sessionId);
      setSessionsRefreshKey((key) => key + 1);
    },
    [sessions],
  );

  const handleArchive = useCallback(
    async (sessionId: string) => {
      if (sessions?.onArchive) {
        return sessions.onArchive(sessionId);
      }
      if (
        sessionId === currentSessionId &&
        !(await confirmCanvasNavigation())
      ) {
        return "cancelled" as const;
      }
      await archiveSession(sessionId);
      notifySessionListChanged();
      if (sessionId === currentSessionId) {
        try {
          router.replace("/");
        } catch {
          try {
            window.location.replace(appPath("/"));
          } catch {
            // The server mutation succeeded; a navigation failure must not
            // be misreported as an archive failure.
          }
        }
      }
      return "success" as const;
    },
    [currentSessionId, router, sessions],
  );

  const handleRestore = useCallback(
    async (sessionId: string) => {
      if (sessions?.onRestore) {
        await sessions.onRestore(sessionId);
        return;
      }
      await restoreSession(sessionId);
      notifySessionListChanged();
    },
    [sessions],
  );

  // feedback-triage-2026-08-24: customer “对话要可以删掉”. Soft deletion + 8-second undo,
  // no confirmation modal; undo Toast is standard for reversible deletion, extra modal adds friction.
  // Deleting current session first confirms unsaved canvas changes (same as archive), then returns home.
  const handleDelete = useCallback(
    async (sessionId: string) => {
      if (sessions?.onDelete) {
        return sessions.onDelete(sessionId);
      }
      if (
        sessionId === currentSessionId &&
        !(await confirmCanvasNavigation())
      ) {
        return "cancelled" as const;
      }
      await deleteSession(sessionId);
      notifySessionListChanged();
      if (sessionId === currentSessionId) {
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
    async (sessionId: string) => {
      if (sessions?.onUndelete) {
        await sessions.onUndelete(sessionId);
        return;
      }
      await undeleteSession(sessionId);
      notifySessionListChanged();
    },
    [sessions],
  );

  const handleNav = useCallback(
    async (event: MouseEvent<HTMLAnchorElement>, route: string) => {
      if (!shouldHandleAppLinkClick(event)) return;
      event.preventDefault();
      if (
        hasCanvasUnsavedChanges() &&
        !(await confirmCanvasNavigation())
      ) {
        return;
      }
      const [routePath, routeQuery = ""] = route.split("?");
      const destination = activeSidebarDestination(
        routePath,
        routeQuery ? `?${routeQuery}` : "",
      );
      if (destination) {
        track("bowhead_nav_destination_opened_total", {
          primary: destination.primaryId,
          leaf: destination.leafId,
          surface:
            presentation === "expanded" || presentation === "overlay"
              ? presentation
              : "compact",
          role: sidebarTrackingRole(me),
        });
      }
      if (presentation === "overlay") closeOverlay();
      else dispatch({ type: "close-transient" });
      setCompactGroup(null);
      router.push(route);
    },
    [closeOverlay, me, presentation, router],
  );

  const handleSearchResult = useCallback(
    async (
      event: MouseEvent<HTMLAnchorElement>,
      item: UnifiedWorkSearchItem,
    ) => {
      if (item.kind !== "session") {
        await handleNav(event, unifiedWorkResultHref(item));
        return;
      }
      if (!shouldHandleAppLinkClick(event)) return;
      event.preventDefault();
      await handlePick(item.session_id ?? item.id);
    },
    [handleNav, handlePick],
  );

  const toggleGroup = (id: SidebarGroupId) => {
    setOpenGroups((groupsOpen) =>
      groupsOpen.includes(id)
        ? groupsOpen.filter((groupId) => groupId !== id)
        : [...groupsOpen, id],
    );
  };

  const renderRecovery = (
    navigationSlot?: ReactNode,
    showSearch = true,
  ) => (
    <WorkRecovery
      groups={groups}
      currentSessionId={currentSessionId}
      workspaceId={sessions?.workspaceId}
      query={query}
      onQueryChange={setQuery}
      onPick={(sessionId) => void handlePick(sessionId)}
      onTogglePin={handleTogglePin}
      archiveEnabled={archiveEnabled}
      archivedCount={archivedCount}
      onArchive={archiveEnabled ? handleArchive : undefined}
      onRestore={archiveEnabled ? handleRestore : undefined}
      onDelete={handleDelete}
      onUndelete={handleUndelete}
      onRename={sessions?.onRename ?? renameSession}
      onNavigateSearchResult={(event, item) =>
        void handleSearchResult(event, item)
      }
      searchInputRef={searchRef}
      showSearch={showSearch}
      navigationSlot={navigationSlot}
      listStatusSlot={listStatusSlot}
      olderSessions={olderSessions}
      surface={
        presentation === "expanded" || presentation === "overlay"
          ? presentation
          : "flyout"
      }
    />
  );

  const expandedInner = (
    <>
      <div className="unified-sidebar-header">
        <a
          href={appPath("/")}
          className="unified-sidebar-brand"
          aria-label={brand.applicationName}
          onClick={(event) => void handleNav(event, "/")}
        >
          <BrandIdentity
            size={28}
            graphicClassName="unified-sidebar-brand-logo"
          />
        </a>
        <button
          ref={overlayCloseRef}
          type="button"
          data-dialog-initial-focus={
            presentation === "overlay" && overlayOpenedByKeyboard
              ? ""
              : undefined
          }
          className="unified-sidebar-icon-button"
          onClick={(event) =>
            presentation === "overlay"
              ? closeOverlay(event.detail === 0)
              : setDesktopMode("compact", event.detail === 0)
          }
          aria-label={t("collapse_sidebar")}
          title={t("collapse_sidebar")}
        >
          <Side size={18} aria-hidden />
        </button>
      </div>
      {capabilitiesContext ? (
        <SettingsNavModelScope context={navigationContext} me={me} active={settingsActive} refreshKey={settingsRefreshKey}>
          {(model) =>
            renderRecovery(
              <>
                <div className="unified-sidebar-section-label">{t("work")}</div>
                <ExpandedNavigation
                  navigation={navigation}
                  activePrimaryId={activePrimaryId}
                  activeLeafId={activeLeafId}
                  openGroups={openGroups}
                  onToggleGroup={toggleGroup}
                  onNavigate={handleNav}
                  onNewSession={() => void handleNewSession()}
                  newSessionPending={isStartingNewSession}
                  translate={t}
                  counters={model.counters}
                />
              </>,
            )
          }
        </SettingsNavModelScope>
      ) : settingsMode ? (
        <SettingsNavModelScope
          context={navigationContext}
          me={me}
          active={settingsActive}
          refreshKey={settingsRefreshKey}
        >
          {(model) => (
            <SettingsSidebarNav
              model={model}
              surface="sidebar"
              onNavigate={(event, route) => void handleNav(event, route)}
            />
          )}
        </SettingsNavModelScope>
      ) : (
        renderRecovery(
          <>
            <div className="unified-sidebar-section-label">
              {t("work")}
            </div>
            <ExpandedNavigation
              navigation={navigation}
              activePrimaryId={activePrimaryId}
              activeLeafId={activeLeafId}
              openGroups={openGroups}
              onToggleGroup={toggleGroup}
              onNavigate={handleNav}
              onNewSession={() => void handleNewSession()}
              newSessionPending={isStartingNewSession}
              translate={t}
            />
          </>,
        )
      )}
      {/* P2 batch 6: the version lives in the account menu's last line and Help › About. */}
      <UserMenu
        initials="N"
        user={me}
        showIdentity
        onBeforePersonalSettingsNavigate={preparePersonalSettingsNavigation}
      />
    </>
  );

  // Narrow-desktop expanded overlay is modal: DialogShell owns focus trap/Esc/background inert/focus return.
  // Account menu is FloatingTree dialog child: immune to inert; Esc with menu open closes menu only.
  // Backdrop is aside sibling, clickable with own onClick. Mouse opening focuses panel,
  // keyboard opening focuses collapse button.
  const expandedContent =
    presentation === "overlay" ? (
      <DialogShell
        mount="inline"
        overlay="none"
        closeOnBackdrop={false}
        onClose={() => closeOverlay()}
        label={brand.applicationName}
        panelClassName="unified-sidebar-expanded"
        panelProps={
          {
            "data-dialog-initial-focus": overlayOpenedByKeyboard
              ? undefined
              : "",
            onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) =>
              shortcutKeyDownRef.current(event),
          } as HTMLAttributes<HTMLDivElement>
        }
        getInsideElements={() =>
          scrimRef.current ? [scrimRef.current] : []
        }
      >
        {expandedInner}
      </DialogShell>
    ) : (
      <div className="unified-sidebar-expanded">{expandedInner}</div>
    );

  // Compact rail for work routes: search, primary navigation (with capability counters on capability routes)
  // and recent work.
  const renderCompactWork = (counters?: SettingsCounters) => (
        <>
        <button
          type="button"
          className={clsx("unified-sidebar-compact-button", {
            "is-active":
              state.workRecoveryOpen && compactRecoveryMode === "search",
          })}
          onClick={(event) => {
            compactTransientOpenedByKeyboardRef.current =
              event.detail === 0;
            transientTriggerRef.current = event.currentTarget;
            setCompactGroup(null);
            setCompactRecoveryMode("search");
            dispatch({
              type: "set-work-recovery-open",
              open:
                compactRecoveryMode !== "search" ||
                !state.workRecoveryOpen,
            });
          }}
          aria-label={t("search_all_work")}
          aria-expanded={
            state.workRecoveryOpen && compactRecoveryMode === "search"
          }
          aria-controls="unified-sidebar-work-recovery"
          aria-haspopup="dialog"
          title={t("search_all_work")}
        >
          <Search size={20} aria-hidden />
          <span className="unified-sidebar-tooltip" role="tooltip">
            {t("search_all_work")} · ⌘K
          </span>
        </button>
        <CompactNavigation
          navigation={navigation}
          activePrimaryId={activePrimaryId}
          currentWithinPrimaryId={
            activeContext.currentWithinPrimaryId
          }
          activeLeafId={activeLeafId}
          openGroup={compactGroup}
          onGroupOpenChange={(id, open) => {
            if (open) {
              dispatch({ type: "set-work-recovery-open", open: false });
              setCompactGroup(id);
              return;
            }
            setCompactGroup((current) => (current === id ? null : current));
          }}
          onNavigate={handleNav}
          onNewSession={() => void handleNewSession()}
          newSessionPending={isStartingNewSession}
          translate={t}
          counters={counters}
        />
        <button
          type="button"
          className={clsx(
            "unified-sidebar-compact-button",
            "is-recent",
            {
              "is-active":
                activeContext.recoveryCurrent || compactRecentCurrent,
            },
          )}
          onClick={(event) => {
            compactTransientOpenedByKeyboardRef.current =
              event.detail === 0;
            transientTriggerRef.current = event.currentTarget;
            setCompactGroup(null);
            setQuery("");
            setCompactRecoveryMode("recent");
            dispatch({
              type: "set-work-recovery-open",
              open:
                compactRecoveryMode !== "recent" ||
                !state.workRecoveryOpen,
            });
          }}
          aria-label={t("recent_work")}
          aria-current={
            activeContext.recoveryCurrent || compactRecentCurrent
              ? "page"
              : undefined
          }
          aria-expanded={
            state.workRecoveryOpen && compactRecoveryMode === "recent"
          }
          aria-controls="unified-sidebar-work-recovery"
          aria-haspopup="dialog"
          title={t("recent_work")}
        >
          <History size={20} aria-hidden />
          <span className="unified-sidebar-tooltip" role="tooltip">
            {t("recent_work")}
          </span>
        </button>
        </>
  );

  const compactContent = (
    <div className="unified-sidebar-compact">
      <a
        href={appPath("/")}
        className="unified-sidebar-mark"
        aria-label={brand.applicationName}
        onClick={(event) => void handleNav(event, "/")}
      >
        <BrandGraphic
          variant="mark"
          size={28}
          className="unified-sidebar-brand-logo"
        />
      </a>
      {dockedModeApplies ? (
        <button
          ref={compactExpandRef}
          type="button"
          className="unified-sidebar-compact-button is-expand"
          onClick={(event) =>
            setDesktopMode("expanded", event.detail === 0)
          }
          aria-label={t("expand_sidebar")}
          title={t("expand_sidebar")}
        >
          <Side size={18} aria-hidden />
        </button>
      ) : (
        <button
          ref={compactExpandRef}
          type="button"
          className="unified-sidebar-compact-button is-expand"
          onClick={(event) =>
            openOverlay("control", event.detail === 0)
          }
          aria-label={t("expand_sidebar")}
          title={t("expand_sidebar")}
        >
          <Side size={18} aria-hidden />
        </button>
      )}
      {capabilitiesContext ? (
        // Capability routes are work routes (D9): the rail keeps search and recent work beside the navigation.
        presentation === "drawer" ? null : (
          <SettingsNavModelScope context={navigationContext} me={me} active={settingsActive} refreshKey={settingsRefreshKey}>
            {(model) => renderCompactWork(model.counters)}
          </SettingsNavModelScope>
        )
      ) : settingsMode ? (
        // The rail is hidden below the docked breakpoint; do not mount the settings data there.
        presentation === "drawer" ? null : (
        <SettingsNavModelScope
          context={navigationContext}
          me={me}
          active={settingsActive}
          refreshKey={settingsRefreshKey}
        >
          {(model) => (
            <SettingsCompactNavigation
              model={model}
              openGroup={compactSettingsGroup}
              onGroupOpenChange={(key, open) => {
                if (open) {
                  dispatch({ type: "set-work-recovery-open", open: false });
                  setCompactSettingsGroup(key);
                  return;
                }
                setCompactSettingsGroup((current) => (current === key ? null : current));
              }}
              onNavigate={(event, route) => {
                setCompactSettingsGroup(null);
                void handleNav(event, route);
              }}
            />
          )}
        </SettingsNavModelScope>
        )
      ) : (
        renderCompactWork()
      )}
      <div className="unified-sidebar-compact-spacer" />
      <UserMenu initials="N" user={me} />
    </div>
  );

  return (
    <>
      {presentation === "overlay" ? (
        <button
          ref={scrimRef}
          type="button"
          className="unified-sidebar-scrim"
          onClick={() => closeOverlay(false)}
          tabIndex={-1}
          aria-hidden="true"
        />
      ) : null}
      <aside
        className={clsx("unified-sidebar", `is-${presentation}`)}
        aria-label={brand.applicationName}
        data-testid="unified-sidebar"
        data-presentation={presentation}
      >
        {presentation === "expanded" || presentation === "overlay"
          ? expandedContent
          : compactContent}
        {presentation === "compact" && state.workRecoveryOpen ? (
          <CompactRecoveryFlyout
            label={
              compactRecoveryMode === "search"
                ? t("search_all_work")
                : t("recent_work")
            }
            flyoutRef={flyoutRef}
            trigger={transientTriggerRef.current}
            onDismiss={closeCompactTransient}
          >
            {renderRecovery(
              undefined,
              compactRecoveryMode === "search",
            )}
          </CompactRecoveryFlyout>
        ) : null}
      </aside>
    </>
  );
}

interface NavigationProps {
  navigation: SidebarPrimaryItem[];
  activePrimaryId: SidebarPrimaryId | null;
  activeLeafId: string | null;
  onNewSession: () => void;
  onNavigate: (
    event: MouseEvent<HTMLAnchorElement>,
    route: string,
  ) => void;
  newSessionPending: boolean;
  translate: ReturnType<typeof useTranslations>;
  counters?: SettingsCounters;
}

function ExpandedNavigation({
  navigation,
  activePrimaryId,
  activeLeafId,
  openGroups,
  onToggleGroup,
  onNavigate,
  onNewSession,
  newSessionPending,
  translate,
  counters = {},
}: NavigationProps & {
  openGroups: SidebarGroupId[];
  onToggleGroup: (id: SidebarGroupId) => void;
}) {
  return (
    <nav
      className="unified-sidebar-nav"
      aria-label={translate("primary_navigation")}
    >
      {navigation.map((item) => {
        const Icon = PRIMARY_ICONS[item.id];
        if (item.kind === "action") {
          const active = activeLeafId === item.id;
          return (
            <button
              key={item.id}
              type="button"
              className={clsx("unified-sidebar-nav-item", {
                "is-active": active,
              })}
              aria-current={active ? "page" : undefined}
              aria-busy={newSessionPending || undefined}
              disabled={newSessionPending}
              data-testid={`unified-nav-${item.id}`}
              onClick={onNewSession}
            >
              <Icon size={20} aria-hidden />
              <span>{translate(item.labelKey)}</span>
            </button>
          );
        }
        if (item.kind === "direct") {
          return (
            <a
              key={item.id}
              href={appPath(item.route)}
              className={clsx("unified-sidebar-nav-item", {
                "is-active": activePrimaryId === item.id,
              })}
              aria-current={activePrimaryId === item.id ? "page" : undefined}
              data-testid={`unified-nav-${item.id}`}
              onClick={(event) => void onNavigate(event, item.route)}
            >
              <Icon size={20} aria-hidden />
              <span>{translate(item.labelKey)}</span>
            </a>
          );
        }
        const open = openGroups.includes(item.id);
        return (
          <div className="unified-sidebar-nav-group" key={item.id}>
            <div
              className={clsx("unified-sidebar-nav-group-row", {
                "is-active": activePrimaryId === item.id,
              })}
            >
              {item.expandOnly ? (
                <button
                  type="button"
                  className="unified-sidebar-nav-item is-group-link"
                  onClick={() => onToggleGroup(item.id)}
                  aria-expanded={open}
                  aria-controls={`unified-sidebar-expanded-group-${item.id}`}
                  data-testid={`unified-nav-${item.id}`}
                >
                  <Icon size={20} aria-hidden />
                  <span>{translate(item.labelKey)}</span>
                </button>
              ) : (
                <a
                  href={appPath(item.route)}
                  className="unified-sidebar-nav-item is-group-link"
                  aria-current={
                    activeLeafId === item.id ? "page" : undefined
                  }
                  data-testid={`unified-nav-${item.id}`}
                  onClick={(event) => void onNavigate(event, item.route)}
                >
                  <Icon size={20} aria-hidden />
                  <span>{translate(item.labelKey)}</span>
                </a>
              )}
              <button
                type="button"
                className="unified-sidebar-group-toggle"
                onClick={() => onToggleGroup(item.id)}
                aria-label={translate(
                  open ? "collapse_group" : "expand_group",
                  { name: translate(item.labelKey) },
                )}
                aria-expanded={open}
                aria-controls={`unified-sidebar-expanded-group-${item.id}`}
                data-testid={`unified-nav-${item.id}-toggle`}
              >
                <Caret className="unified-sidebar-caret" aria-hidden />
              </button>
            </div>
            {open ? (
              <div
                id={`unified-sidebar-expanded-group-${item.id}`}
                className="unified-sidebar-subnav"
              >
                {item.children.map((child) => (
                  <a
                    key={child.id}
                    href={appPath(child.route)}
                    className={clsx("unified-sidebar-subnav-item", {
                      "is-active": activeLeafId === child.id,
                    })}
                    aria-current={
                      activeLeafId === child.id ? "page" : undefined
                    }
                    data-testid={`unified-nav-${child.id}`}
                    onClick={(event) => void onNavigate(event, child.route)}
                  >
                    <span>{translate(child.labelKey)}</span>
                    {child.counter ? <SidebarNavCounter counter={child.counter} value={counters[child.counter]} /> : null}
                  </a>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
    </nav>
  );
}

function CompactNavigation({
  navigation,
  activePrimaryId,
  currentWithinPrimaryId,
  activeLeafId,
  openGroup,
  onGroupOpenChange,
  onNavigate,
  onNewSession,
  newSessionPending,
  translate,
  counters = {},
}: NavigationProps & {
  currentWithinPrimaryId: SidebarGroupId | null;
  openGroup: SidebarGroupId | null;
  onGroupOpenChange: (id: SidebarGroupId, open: boolean) => void;
}) {
  return (
    <nav
      className="unified-sidebar-compact-nav"
      aria-label={translate("primary_navigation")}
    >
      {navigation.map((item) => {
        const Icon = PRIMARY_ICONS[item.id];
        if (item.kind === "action") {
          const active = activeLeafId === item.id;
          return (
            <button
              key={item.id}
              type="button"
              className={clsx("unified-sidebar-compact-button", {
                "is-active": active,
              })}
              aria-label={translate(item.labelKey)}
              aria-current={active ? "page" : undefined}
              aria-busy={newSessionPending || undefined}
              disabled={newSessionPending}
              title={translate(item.labelKey)}
              data-testid={`unified-nav-${item.id}`}
              onClick={onNewSession}
            >
              <Icon size={20} aria-hidden />
              <span className="unified-sidebar-tooltip" aria-hidden="true">
                {translate(item.labelKey)}
              </span>
            </button>
          );
        }
        if (item.kind === "direct") {
          return (
            <a
              key={item.id}
              href={appPath(item.route)}
              className={clsx("unified-sidebar-compact-button", {
                "is-active": activePrimaryId === item.id,
              })}
              aria-label={translate(item.labelKey)}
              aria-current={activePrimaryId === item.id ? "page" : undefined}
              data-testid={`unified-nav-${item.id}`}
              onClick={(event) => void onNavigate(event, item.route)}
            >
              <Icon size={20} aria-hidden />
              <span className="unified-sidebar-tooltip" aria-hidden="true">
                {translate(item.labelKey)}
              </span>
            </a>
          );
        }
        return (
          <CompactNavGroup
            key={item.id}
            item={item}
            icon={Icon}
            open={openGroup === item.id}
            onOpenChange={(open) => onGroupOpenChange(item.id, open)}
            activePrimaryId={activePrimaryId}
            currentWithinPrimaryId={currentWithinPrimaryId}
            activeLeafId={activeLeafId}
            onNavigate={onNavigate}
            translate={translate}
            counters={counters}
          />
        );
      })}
    </nav>
  );
}

/**
 * Compact navigation group: icon trigger + right-side destination menu. useMenu owns keys/Esc/outside/focus-leave dismissal and focus return
 * (component unification step 4); body-mounted menu opens right of trigger, top-aligned.
 */
function CompactNavGroup({
  item,
  icon: Icon,
  open,
  onOpenChange,
  activePrimaryId,
  currentWithinPrimaryId,
  activeLeafId,
  onNavigate,
  translate,
  counters,
}: {
  item: SidebarGroupItem;
  icon: IconComponent;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  activePrimaryId: SidebarPrimaryId | null;
  currentWithinPrimaryId: SidebarGroupId | null;
  activeLeafId: string | null;
  onNavigate: NavigationProps["onNavigate"];
  translate: NavigationProps["translate"];
  counters: SettingsCounters;
}) {
  const menu = useMenu({
    open,
    onOpenChange,
    placement: "right-start",
    offset: 8,
  });
  return (
    <div className="unified-sidebar-compact-group">
      <button
        ref={menu.refs.setReference}
        type="button"
        className={clsx("unified-sidebar-compact-button", {
          "is-active": activePrimaryId === item.id,
        })}
        aria-label={
          currentWithinPrimaryId === item.id
            ? translate("current_section_aria", {
                name: translate(item.labelKey),
              })
            : translate(item.labelKey)
        }
        data-testid={`unified-nav-${item.id}`}
        {...menu.getReferenceProps()}
      >
        <Icon size={20} aria-hidden />
        <span className="unified-sidebar-tooltip" aria-hidden="true">
          {translate(item.labelKey)}
        </span>
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: "unified-sidebar-flyout is-nav",
            "aria-label": translate(item.labelKey),
          })}
        >
          <div className="unified-sidebar-flyout-title" aria-hidden="true">
            {translate(item.labelKey)}
          </div>
          {item.expandOnly || item.id === "agents" ? null : (
            <CompactNavLink
              route={item.route}
              className="is-overview"
              active={activeLeafId === item.id}
              onNavigate={onNavigate}
            >
              {item.id === "capabilities" ? translate(item.labelKey) : translate("overview")}
            </CompactNavLink>
          )}
          {item.children.map((child) => (
            <CompactNavLink
              key={child.id}
              route={child.route}
              active={activeLeafId === child.id}
              onNavigate={onNavigate}
            >
              <span>{translate(child.labelKey)}</span>
              {child.counter ? <SidebarNavCounter counter={child.counter} value={counters[child.counter]} /> : null}
            </CompactNavLink>
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function CompactNavLink({
  route,
  className,
  active,
  onNavigate,
  children,
}: {
  route: string;
  className?: string;
  active: boolean;
  onNavigate: NavigationProps["onNavigate"];
  children: ReactNode;
}) {
  const itemProps = useMenuItem({
    onSelect: (event) =>
      void onNavigate(event as MouseEvent<HTMLAnchorElement>, route),
  });
  return (
    <a
      href={appPath(route)}
      className={clsx("unified-sidebar-flyout-item", className, {
        "is-active": active,
      })}
      aria-current={active ? "page" : undefined}
      {...itemProps}
    >
      {children}
    </a>
  );
}

interface CompactRecoveryFlyoutProps {
  label: string;
  flyoutRef: RefObject<HTMLDivElement | null>;
  trigger: HTMLElement | null;
  onDismiss: (restoreFocus: boolean) => void;
  children: ReactNode;
}

/**
 * Compact Recent work/Search all work overlay: nonmodal dialog, outside dismisses, rendered within sidebar (position/height
 * from `.unified-sidebar-flyout.is-work-recovery` relative to sidebar). Esc closes with trigger focus return;
 * outside click/focus departure closes without stealing focus (nonmodal FloatingFocusManager). Registered FloatingNode:
 * inline “⋯” session menus portal to body as children, not outside; Esc with menu open closes menu only.
 */
function CompactRecoveryFlyout(props: CompactRecoveryFlyoutProps) {
  const parentId = useFloatingParentNodeId();
  if (parentId === null) {
    return (
      <FloatingTree>
        <CompactRecoveryFlyoutNode {...props} />
      </FloatingTree>
    );
  }
  return <CompactRecoveryFlyoutNode {...props} />;
}

function CompactRecoveryFlyoutNode({
  label,
  flyoutRef,
  trigger,
  onDismiss,
  children,
}: CompactRecoveryFlyoutProps) {
  const nodeId = useFloatingNodeId();
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;
  const { refs, context } = useFloating({
    nodeId,
    open: true,
    onOpenChange: (open, _event, reason) => {
      if (!open) onDismissRef.current(reason === "escape-key");
    },
  });
  // Trigger belongs to overlay: own onClick toggles, never predismissed as outside click.
  useLayoutEffect(() => {
    refs.setReference(trigger);
  }, [refs, trigger]);
  const dismiss = useDismiss(context);
  const { getFloatingProps } = useInteractions([dismiss]);
  const ref = useMergeRefs([refs.setFloating, flyoutRef]);
  return (
    <FloatingNode id={nodeId}>
      <FloatingFocusManager
        context={context}
        modal={false}
        initialFocus={-1}
        returnFocus={false}
      >
        <div
          ref={ref}
          id="unified-sidebar-work-recovery"
          className="unified-sidebar-flyout is-work-recovery"
          role="dialog"
          tabIndex={-1}
          aria-label={label}
          {...getFloatingProps()}
        >
          <div className="unified-sidebar-flyout-title">{label}</div>
          {children}
        </div>
      </FloatingFocusManager>
    </FloatingNode>
  );
}

function sidebarTrackingRole(me: MeUser | null): string {
  return me?.team_role ?? (me?.is_team_admin ? "team_admin" : "member");
}
