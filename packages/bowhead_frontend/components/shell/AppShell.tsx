"use client";

import type { ReactNode } from "react";

import { MobileTopBar } from "@/components/mobile-topbar";
import type {
  NewWorkResult,
  UnifiedSidebarSessions,
} from "@/components/unified-sidebar";

import type { SettingsTabId } from "@/lib/settings-destinations";
import { usePageDefaultDocumentTitle } from "@/lib/use-document-title";
import { PageTitleCollapsedContext, usePageTitleCollapse } from "@/lib/use-page-title-collapse";

import { ShellGlobalControls } from "./global-controls";
import { ProductShellPage } from "./ProductShell";

interface AppShellProps {
  /**
   * Page-owned New work reset. Routes without a local reset contract omit it
   * and use ProductShell's canonical-root fallback.
   */
  onNewSession?: () => NewWorkResult | Promise<NewWorkResult>;
  /**
   * MobileTopBar title (mobile-only chrome; desktop CSS-hidden). Use the page H1's own message so the bar
   * repeats it exactly: while that H1 is on screen the top-bar titles stay hidden (large-title pattern).
   */
  mobileTitle: string;
  /** Show a title skeleton in the phone top bar while the page's real title is still loading. */
  mobileTitleLoading?: boolean;
  /** Disable the large-title treatment when matching H1s are document content, not this page's heading. */
  collapseTopbarTitle?: boolean;
  /** Session state owned by the chat page; other routes let UnifiedSidebar self-fetch. */
  unifiedSidebarSessions?: UnifiedSidebarSessions;
  /** Dense secondary-navigation pages start with the global sidebar compact. */
  preferCompactSidebar?: boolean;
  /** Settings tab shown by the page; the sidebar's settings content highlights it (P2 batch 4). */
  settingsActiveTab?: SettingsTabId;
  /** the route's shared <Topbar/> element, built by the page (R1: runner reads stay in the page). */
  topbar?: ReactNode;
  /** Page actions for the mobile top bar, after the global controls (the chat page's session "⋯" menu, D12):
   *  below 1200 the desktop Topbar is hidden, so its page-owned actions need this second place. */
  mobileActions?: ReactNode;
  /** <main> class; default "v2-main". files passes "v2-main files-main" to keep its scoped CSS. */
  mainClassName?: string;
  children: ReactNode;
}

/**
 * Page slot for the persistent ProductShell.
 *
 * The route-invariant frame and UnifiedSidebar live above the page boundary so
 * they survive client navigation. AppShell keeps page-owned chrome and state
 * (MobileTopBar, main, Topbar, runner/session bindings) inside the route while
 * registering only the integrations the persistent sidebar needs.
 */
export function AppShell({
  onNewSession,
  mobileTitle,
  mobileTitleLoading = false,
  collapseTopbarTitle = true,
  unifiedSidebarSessions,
  preferCompactSidebar = false,
  settingsActiveTab,
  topbar,
  mobileActions,
  mainClassName = "v2-main",
  children,
}: AppShellProps) {
  // Large-title pattern: hide the top-bar titles while the page H1 repeating them is visible.
  const observedTitleCollapsed = usePageTitleCollapse(collapseTopbarTitle ? mobileTitle : "");
  // A route can switch from a large page heading to an active session before the observer's
  // cleanup finishes. Do not carry the former page's hidden state into the session chrome.
  const titleCollapsed = collapseTopbarTitle && observedTitleCollapsed;
  // Tab title "<page> · <application name>" (116: /reports/, /agents/ … all read "Bowhead — Verified Intelligence").
  // The brand metadata controller composes it with the edition/team application name; content such as an open
  // report may register a more specific title over it. A session still opening has no name yet, so it keeps the default.
  usePageDefaultDocumentTitle(mobileTitleLoading ? null : mobileTitle, "app-shell-page");
  return (
    <>
      <ProductShellPage
        onNewSession={onNewSession}
        sessions={unifiedSidebarSessions}
        preferCompactDesktop={preferCompactSidebar}
        settingsActiveTab={settingsActiveTab}
      />
      {/* Below 1200, hide desktop Topbar and move its right-side space switcher and notifications into the mobile top bar;
          as on desktop, show them only on pages with a Topbar. */}
      <MobileTopBar
        key="mobile-topbar"
        pageTitle={mobileTitle}
        titleLoading={mobileTitleLoading}
        titleCollapsed={titleCollapsed}
        rightSlot={
          topbar || mobileActions ? (
            <>
              {topbar ? <ShellGlobalControls surface="mobile-topbar" /> : null}
              {mobileActions}
            </>
          ) : undefined
        }
      />
      <main
        key="app-main"
        id="bowhead-main-content"
        tabIndex={-1}
        className={mainClassName}
      >
        <PageTitleCollapsedContext.Provider value={titleCollapsed}>
          {topbar}
          {children}
        </PageTitleCollapsedContext.Provider>
      </main>
    </>
  );
}
