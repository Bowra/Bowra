"use client";

import { useContext, useRef, type ReactNode } from "react";

import { PageTitleCollapsedContext } from "@/lib/use-page-title-collapse";
import { useScrollEdge } from "@/lib/use-scroll-edge";

import { ShellGlobalControls } from "./global-controls";

interface TopbarProps {
  /**
   * Route title cluster. R1: built in the PAGE's render scope so runner reads stay there —
   * chat passes a runner-derived node (title + session/turn id + caption); aux pages pass a plain <span.title>.
   */
  titleSlot: ReactNode;
  /** optional meta (chat: · streaming / · ✓ N; files: size·count). */
  metaSlot?: ReactNode;
}

/**
 * layout-redesign P2 — the shared topbar on EVERY route (fixes admin/aux having no
 * desktop header; the old W13 TopAppBar was reverted). Reuses the existing `.v2-main-head`
 * CSS. The right cluster (space / notifications / credits) is app-global; below 1024px the
 * same controls move to the mobile topbar and drawer (see ./global-controls). Route-specific
 * left content arrives via slots so runner state never leaves the page.
 */
export function Topbar({ titleSlot, metaSlot }: TopbarProps) {
  // P2 material: write data-scrolled when content leaves the top (styles: transparent at rest, bar material after scrolling)
  const headRef = useRef<HTMLDivElement>(null);
  useScrollEdge(headRef);
  // Large-title pattern: the title stays hidden while the page H1 repeating it is visible (AppShell).
  const titleCollapsed = useContext(PageTitleCollapsedContext);
  // P2 batch 3: the title and its caption sit on one line and the right cluster is 4 apart;
  // both come from classes in styles/ui-refresh-chrome.css.
  return (
    <div ref={headRef} className="v2-main-head">
      <div
        className="v2-main-head-titles"
        data-collapsed={titleCollapsed ? "true" : undefined}
        aria-hidden={titleCollapsed ? true : undefined}
      >
        {titleSlot}
      </div>
      {metaSlot}
      <div className="v2-main-head-controls">
        <ShellGlobalControls surface="topbar" />
      </div>
    </div>
  );
}
