"use client";

/**
 * V5 mobile fix · MobileTopBar.
 *
 * Unified mobile top chrome (≡ + page title + theme toggle).
 * Not rendered on desktop (CSS @media gate via .v2-mobile-topbar / .v2-desktop-topbar).
 *
 * Matches docs/V5-mobile-fix-tech-design.md §2.1.
 */

import { useRef } from "react";

import { useSessionsDrawer } from "@/components/sessions-drawer";
import { Menu } from "@/components/icons";
import { useScrollEdge } from "@/lib/use-scroll-edge";
import { useTranslations } from "next-intl";
import { requestSidebarOverlay } from "@/lib/sidebar-overlay-request";
import { sidebarTabletBand } from "@/lib/sidebar-state";

interface Props {
  pageTitle: string;
  /** The page is opening something whose name is not known yet (a session by URL): show a title skeleton instead
   *  of a placeholder name such as "New work" (116 phone). The accessible name stays `pageTitle`. */
  titleLoading?: boolean;
  /** Large-title pattern: the page's own H1 is on screen, so the bar's title fades out (AppShell computes it). */
  titleCollapsed?: boolean;
  /** Extra right-side buttons (e.g. share); global preferences are all accessed through Settings. */
  rightSlot?: React.ReactNode;
}

export function MobileTopBar({ pageTitle, titleLoading = false, titleCollapsed = false, rightSlot }: Props) {
  const { open } = useSessionsDrawer();
  const t = useTranslations("sidebar");
  // P2 material: set data-scrolled when the page leaves the top (styles: transparent at rest, bar material after scrolling).
  const barRef = useRef<HTMLElement>(null);
  useScrollEdge(barRef);
  return (
    <header ref={barRef} className="v2-mobile-topbar" data-testid="mobile-topbar">
      <button
        type="button"
        className="v2-mobile-topbar-hamburger"
        aria-label={t("open_mobile_drawer")}
        onClick={(event) => {
          // 768–1023 opens the sidebar as an overlay; below 768 the drawer.
          if (
            sidebarTabletBand(window.innerWidth) &&
            requestSidebarOverlay(event.currentTarget)
          ) {
            return;
          }
          open(event.currentTarget);
        }}
        data-testid="mobile-topbar-hamburger"
      >
        <Menu aria-hidden />
      </button>
      <div
        className="v2-mobile-topbar-title"
        data-testid="mobile-topbar-title"
        data-collapsed={titleCollapsed ? "true" : undefined}
        aria-hidden={titleCollapsed ? true : undefined}
        aria-busy={titleLoading ? true : undefined}
      >
        {titleLoading ? (
          <>
            <span className="v2-mobile-topbar-title-skeleton" aria-hidden="true" />
            <span className="sr-only">{pageTitle}</span>
          </>
        ) : pageTitle}
      </div>
      {rightSlot ? (
        <div className="v2-mobile-topbar-right">{rightSlot}</div>
      ) : null}
    </header>
  );
}
