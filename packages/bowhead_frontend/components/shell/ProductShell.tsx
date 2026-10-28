"use client";

import {
  Suspense,
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";

import {
  UnifiedSidebar,
  type NewWorkResult,
  type UnifiedSidebarSessions,
} from "@/components/unified-sidebar";
import { SessionsDrawer } from "@/components/sessions-drawer";
import {
  appNavigationAllowed,
  navigateAppDocument,
} from "@/lib/use-app-router";
import { useSessionArchiveCapability } from "@/lib/use-session-archive-capability";
import type { SettingsTabId } from "@/lib/settings-destinations";

interface ProductShellPageBindings {
  onNewSession?: () => NewWorkResult | Promise<NewWorkResult>;
  sessions?: UnifiedSidebarSessions;
  preferCompactDesktop: boolean;
  /** Settings tab rendered by the page (SettingsShell); refines the route-derived settings highlight. */
  settingsActiveTab?: SettingsTabId;
}

type ProductShellPageBindingsRef =
  MutableRefObject<ProductShellPageBindings>;

interface ProductShellContextValue {
  registerPage: (bindings: ProductShellPageBindingsRef) => () => void;
  refreshPage: () => void;
}

const ProductShellContext =
  createContext<ProductShellContextValue | null>(null);

/**
 * Persistent authenticated product frame.
 *
 * Next keeps the route-group layout mounted while its page child changes. The
 * sidebar therefore belongs here—not inside each page—so route navigation does
 * not recreate its responsive mode, identity request, search, or disclosure
 * state.
 */
export function ProductShell({
  children,
  pathname,
}: {
  children: ReactNode;
  pathname?: string;
}) {
  const t = useTranslations("sidebar");
  const archiveEnabled = useSessionArchiveCapability();
  const activePageRef = useRef<ProductShellPageBindingsRef | null>(null);
  const pendingNewWorkRef = useRef<Promise<NewWorkResult> | null>(null);
  // Page leave guards (such as unsaved model-settings changes) intercept synchronously, then confirm asynchronously; the sidebar already received
  // "cancelled". Signal progress here during the confirmed full-page navigation, disabling repeat clicks and duplicate confirmation.
  const [documentNavigationPending, setDocumentNavigationPending] = useState(false);
  const registrationTokenRef = useRef<symbol | null>(null);
  const [registeredPage, setRegisteredPage] =
    useState<ProductShellPageBindingsRef | null>(null);
  const [pageRevision, refreshPage] = useReducer(
    (version: number) => version + 1,
    0,
  );

  const registerPage = useCallback(
    (bindings: ProductShellPageBindingsRef) => {
      const token = Symbol("product-shell-page");
      registrationTokenRef.current = token;
      activePageRef.current = bindings;
      setRegisteredPage(bindings);

      return () => {
        if (registrationTokenRef.current !== token) return;
        registrationTokenRef.current = null;
        activePageRef.current = null;
        setRegisteredPage(null);
      };
    },
    [],
  );

  const context = useMemo<ProductShellContextValue>(
    () => ({ registerPage, refreshPage }),
    [registerPage],
  );

  const onNewSession = useCallback((): Promise<NewWorkResult> => {
    if (pendingNewWorkRef.current) return pendingNewWorkRef.current;
    const request = (async () => {
      try {
        const handler = activePageRef.current?.current.onNewSession;
        if (handler) return await handler();
        const navigateHome = () => {
          setDocumentNavigationPending(true);
          navigateAppDocument("/");
        };
        if (!appNavigationAllowed("/", navigateHome)) return "cancelled";
        // Pages without a local reset must leave their route state behind. A
        // document navigation updates the browser immediately, while the URL
        // remains derived from the active runtime public-entry configuration.
        navigateAppDocument("/");
        // Do not clear the sidebar's pending state before this document is
        // replaced. A client-side route transition can otherwise leave the
        // control visually unchanged for several seconds.
        return "navigating";
      } finally {
        pendingNewWorkRef.current = null;
      }
    })();
    pendingNewWorkRef.current = request;
    return request;
  }, []);

  const sessions = useMemo<UnifiedSidebarSessions | undefined>(() => {
    if (!registeredPage?.current.sessions) return undefined;
    const registeredSessions = registeredPage.current.sessions;
    const registeredOnArchive = registeredSessions.onArchive;
    return {
      groups: registeredSessions.groups,
      // The list status travels with the rows: without it the sidebar read a page-supplied list as settled and showed
      // "No sessions yet" on every route that registers sessions (116 cold load of /?session=…); only /reports/,
      // which registers none, showed the skeleton.
      loading: registeredSessions.loading,
      error: registeredSessions.error,
      olderSessions: registeredSessions.olderSessions
        ? {
            ...registeredSessions.olderSessions,
            onLoadMore: () => {
              const live =
                activePageRef.current?.current.sessions?.olderSessions;
              return (live ?? registeredSessions.olderSessions)?.onLoadMore();
            },
          }
        : undefined,
      onRetry: registeredSessions.onRetry
        ? () => {
            const liveHandler = activePageRef.current?.current.sessions?.onRetry;
            return (liveHandler ?? registeredSessions.onRetry)?.();
          }
        : undefined,
      currentSessionId:
        registeredSessions.currentSessionId,
      workspaceId: registeredSessions.workspaceId,
      onPick: (sessionId) => {
        const liveHandler =
          activePageRef.current?.current.sessions?.onPick;
        return (liveHandler ?? registeredSessions.onPick)(sessionId);
      },
      onTogglePin: registeredSessions.onTogglePin
        ? (sessionId, currentlyPinned) => {
            const liveHandler =
              activePageRef.current?.current.sessions?.onTogglePin;
            return (liveHandler ?? registeredSessions.onTogglePin)?.(
              sessionId,
              currentlyPinned,
            );
          }
        : undefined,
      archivedCount: registeredSessions.archivedCount,
      onArchive: registeredOnArchive
        ? (sessionId) => {
            const liveHandler =
              activePageRef.current?.current.sessions?.onArchive;
            return (liveHandler ?? registeredOnArchive)(
              sessionId,
            );
          }
        : undefined,
      onRestore: registeredSessions.onRestore
        ? (sessionId) => {
            const liveHandler =
              activePageRef.current?.current.sessions?.onRestore;
            return (liveHandler ?? registeredSessions.onRestore)?.(
              sessionId,
            );
          }
        : undefined,
    };
  }, [pageRevision, registeredPage]);

  // P2 batch 4: settings pages carry no section navigation of their own (the sidebar shows it), so only the
  // registered page decides whether the desktop sidebar prefers the icon rail.
  const preferCompactDesktop = registeredPage?.current.preferCompactDesktop ?? false;
  const settingsActiveTab = registeredPage?.current.settingsActiveTab;

  return (
    <ProductShellContext.Provider value={context}>
      <div className="v2-app-root">
        <a className="app-skip-link" href="#bowhead-main-content">
          {t("skip_to_content")}
        </a>
        <ProductSidebar
          onNewSession={onNewSession}
          navigationPending={documentNavigationPending}
          sessions={sessions}
          preferCompactDesktop={preferCompactDesktop}
          archiveEnabled={archiveEnabled}
          settingsActiveTab={settingsActiveTab}
        />
        {children}
        <Suspense fallback={null}>
          <SessionsDrawer
            onNewSession={onNewSession}
            navigationPending={documentNavigationPending}
            sessions={sessions}
            archiveEnabled={archiveEnabled}
            settingsActiveTab={settingsActiveTab}
          />
        </Suspense>
      </div>
    </ProductShellContext.Provider>
  );
}

function ProductSidebar({
  onNewSession,
  navigationPending,
  sessions,
  preferCompactDesktop,
  archiveEnabled,
  settingsActiveTab,
}: {
  onNewSession: () => Promise<NewWorkResult>;
  navigationPending: boolean;
  sessions?: UnifiedSidebarSessions;
  preferCompactDesktop: boolean;
  archiveEnabled: boolean;
  settingsActiveTab?: SettingsTabId;
}) {
  const [hydrated, setHydrated] = useState(false);
  useLayoutEffect(() => setHydrated(true), []);

  const fallback = (
    <aside
      className="unified-sidebar is-bootstrap"
      aria-hidden="true"
    />
  );

  if (!hydrated) return fallback;

  return (
    <Suspense fallback={fallback}>
      <UnifiedSidebar
        onNewSession={onNewSession}
        navigationPending={navigationPending}
        sessions={sessions}
        preferCompactDesktop={preferCompactDesktop}
        archiveEnabled={archiveEnabled}
        settingsActiveTab={settingsActiveTab}
      />
    </Suspense>
  );
}

/**
 * Registers the current page's sidebar integrations without making the
 * sidebar's lifecycle depend on that page. Callback identities live in a ref;
 * only render-relevant session data and presentation policy notify the frame.
 */
export function ProductShellPage({
  onNewSession,
  sessions,
  preferCompactDesktop,
  settingsActiveTab,
}: ProductShellPageBindings) {
  const shell = useContext(ProductShellContext);
  const bindingsRef = useRef<ProductShellPageBindings>({
    onNewSession,
    sessions,
    preferCompactDesktop,
    settingsActiveTab,
  });
  bindingsRef.current = {
    onNewSession,
    sessions,
    preferCompactDesktop,
    settingsActiveTab,
  };

  useLayoutEffect(() => {
    if (!shell) return;
    return shell.registerPage(bindingsRef);
  }, [shell]);

  useLayoutEffect(() => {
    shell?.refreshPage();
  }, [
    preferCompactDesktop,
    settingsActiveTab,
    sessions?.currentSessionId,
    sessions?.workspaceId,
    sessions?.archivedCount,
    sessions?.groups,
    sessions?.loading,
    sessions?.error,
    sessions?.olderSessions?.hasMore,
    sessions?.olderSessions?.loadingMore,
    sessions?.olderSessions?.loadMoreError,
    sessions?.olderSessions?.olderPagesLoaded,
    sessions?.olderSessions?.latestOlderPageIds,
    shell,
  ]);

  return null;
}
