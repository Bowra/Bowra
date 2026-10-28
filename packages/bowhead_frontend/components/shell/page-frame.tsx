"use client";

/**
 * One page header and content column for every first-level work page (Header, Language & Paging Spec §2,
 * D4–D7): Agents (three tabs), Capability Center, Skills & Workflows, My Reports, Library › Files and
 * Library › Knowledge. Generalized from the Agents page frame.
 *
 * - PageFrame: AppShell + desktop Topbar (title · space · notifications · credits) + a scrolling area holding
 *   one column, `min(var(--content-w-table|form), 100%)` wide with `8px var(--page-gutter) 48px` padding,
 *   centered. Page background is always `background` (no gradients). Below 768 the phone top bar names the
 *   page and carries `mobileActions`.
 * - PageHeader: title 22/700 (from the frame), one-line description 13 ink3, optional "‹ Parent" crumb, and on the
 *   right ⋯, at most one secondary and at most one primary action, in that order. Hidden below 768, where the top
 *   bar already names the page and holds the actions.
 * - PageTabs: link tabs for sibling routes, drawn as underline tabs; below 768 three or fewer become a full-width
 *   segmented row, four or more stay underline tabs that scroll sideways with the current tab kept in view.
 *
 * Styles: styles/ui-refresh-page-frame.css.
 */

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type MouseEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import type { LucideIcon } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";

import { AppShell } from "./AppShell";
import { Topbar } from "./Topbar";

const PageFrameContext = createContext<{ title: string; mobileSlot: HTMLElement | null } | null>(null);

export function PageFrame({
  title,
  width = "table",
  mobileActions,
  preferCompactSidebar,
  topbarMeta,
  mainClassName,
  className,
  testId,
  children,
}: {
  /** Page name: the desktop H1, the desktop top-bar title and the phone top-bar title (one string). */
  title: string;
  /** Content width tier (Page Layout Spec): work pages 1120, settings-like forms 880. */
  width?: "table" | "form";
  /** Phone (< 768) top-bar actions after the global controls: the primary action as a 44 icon button, then ⋯. */
  mobileActions?: ReactNode;
  preferCompactSidebar?: boolean;
  /** Optional top-bar meta (Files: size · count). */
  topbarMeta?: ReactNode;
  mainClassName?: string;
  /** Extra class on the content column (page-scoped styles). */
  className?: string;
  testId?: string;
  children: ReactNode;
}) {
  // Page content that owns its actions' state (e.g. an upload dialog) fills the phone top bar through
  // PageFrameMobileActions, which portals into this slot.
  const [mobileSlot, setMobileSlot] = useState<HTMLElement | null>(null);
  return (
    <AppShell
      mobileTitle={title}
      preferCompactSidebar={preferCompactSidebar}
      mainClassName={mainClassName}
      // Phones (< 768) move the header actions into the top bar; wider screens keep them in the page header.
      mobileActions={
        <span className="ui-page-mobile-actions" ref={setMobileSlot}>
          {mobileActions}
        </span>
      }
      topbar={<Topbar titleSlot={<span className="title">{title}</span>} metaSlot={topbarMeta} />}
    >
      <PageFrameContext.Provider value={{ title, mobileSlot }}>
        <div className="ui-page-frame-scroll">
          <div
            className={["ui-page-frame", className].filter(Boolean).join(" ")}
            data-width={width}
            data-testid={testId}
          >
            {children}
          </div>
        </div>
      </PageFrameContext.Provider>
    </AppShell>
  );
}

export function usePageFrameTitle(): string | null {
  return useContext(PageFrameContext)?.title ?? null;
}

/** Render header actions into the phone (< 768) top bar from inside the page content. */
export function PageFrameMobileActions({ children }: { children: ReactNode }) {
  const slot = useContext(PageFrameContext)?.mobileSlot ?? null;
  return slot ? createPortal(children, slot) : null;
}

export function PageHeader({
  title,
  description,
  crumb,
  primary,
  secondary,
  moreActions,
}: {
  /** Defaults to the PageFrame title so the H1 repeats the top-bar title exactly. */
  title?: string;
  /** What this page is for, in one line; counts belong in the toolbar, not here. */
  description?: ReactNode;
  /** "‹ Parent" link above the title, only on drill-down pages. */
  crumb?: ReactNode;
  primary?: ReactNode;
  secondary?: ReactNode;
  /** The ⋯ menu (links to other places go here). */
  moreActions?: ReactNode;
}) {
  const frameTitle = usePageFrameTitle();
  const heading = title ?? frameTitle ?? "";
  const hasActions = Boolean(primary || secondary || moreActions);
  return (
    <header className="ui-page-head ui-page-header">
      <div className="ui-page-header__copy">
        {crumb ? <div className="ui-page-header__crumb">{crumb}</div> : null}
        <h1 className="ui-page-title">{heading}</h1>
        {description ? <p className="ui-page-desc">{description}</p> : null}
      </div>
      {hasActions ? (
        <div className="ui-page-header__actions">
          {moreActions}
          {secondary}
          {primary}
        </div>
      ) : null}
    </header>
  );
}

type PageActionVariant = "primary" | "secondary";

function pageActionClass(variant: PageActionVariant, className?: string) {
  return ["settings-button", `settings-button--${variant}`, "settings-button--md", "ui-page-action", className]
    .filter(Boolean)
    .join(" ");
}

/** Header action that navigates (28 high, r2, 13px; create/upload actions lead with a 14px icon). */
export function PageActionLink({
  href,
  variant = "primary",
  icon,
  className,
  children,
  ...props
}: Omit<ComponentProps<"a">, "href"> & { href: string; variant?: PageActionVariant; icon?: LucideIcon }) {
  const router = useAppRouter();
  return (
    <a
      {...props}
      href={appPath(href)}
      className={pageActionClass(variant, className)}
      onClick={(event) => {
        props.onClick?.(event);
        if (event.defaultPrevented || !shouldHandleAppLinkClick(event)) return;
        event.preventDefault();
        router.push(href);
      }}
    >
      {icon ? <LucideGlyph icon={icon} size={14} aria-hidden="true" /> : null}
      <span>{children}</span>
    </a>
  );
}

/** Header action that acts in place (e.g. opening an upload dialog). */
export function PageActionButton({
  variant = "primary",
  icon,
  className,
  children,
  type = "button",
  ...props
}: ComponentProps<"button"> & { variant?: PageActionVariant; icon?: LucideIcon }) {
  return (
    <button {...props} type={type} className={pageActionClass(variant, className)}>
      {icon ? <LucideGlyph icon={icon} size={14} aria-hidden="true" /> : null}
      <span>{children}</span>
    </button>
  );
}

/** Phone top-bar primary action: a 44 icon button (or link) named by `label`. */
export function PageMobileAction({
  label,
  icon,
  href,
  onClick,
  testId,
}: {
  label: string;
  icon: LucideIcon;
  href?: string;
  onClick?: () => void;
  testId?: string;
}) {
  const router = useAppRouter();
  const glyph = <LucideGlyph icon={icon} size={20} aria-hidden="true" />;
  if (href) {
    return (
      <a
        href={appPath(href)}
        className="ui-page-mobile-action"
        aria-label={label}
        data-testid={testId}
        onClick={(event) => {
          if (!shouldHandleAppLinkClick(event)) return;
          event.preventDefault();
          router.push(href);
        }}
      >
        {glyph}
      </a>
    );
  }
  return (
    <button type="button" className="ui-page-mobile-action" aria-label={label} data-testid={testId} onClick={onClick}>
      {glyph}
    </button>
  );
}

export interface PageTabItem {
  key: string;
  label: ReactNode;
  /** App route (without the base path). */
  href: string;
  current: boolean;
  /** Count after the label (11/600 ink4); null renders a loading placeholder, undefined nothing. */
  count?: number | null;
  testId?: string;
}

export function PageTabs({
  label,
  items,
  testId,
  onNavigate,
}: {
  /** Accessible name of the tab navigation. */
  label: string;
  items: PageTabItem[];
  testId?: string;
  /** Replaces the default router push (e.g. to keep the scroll position). */
  onNavigate?: (item: PageTabItem) => void;
}) {
  const router = useAppRouter();
  const navRef = useRef<HTMLElement | null>(null);
  const currentKey = items.find((item) => item.current)?.key;

  // Scrolling tabs keep the current one in view; scroll the strip only, never the page.
  useEffect(() => {
    const nav = navRef.current;
    if (!nav || nav.scrollWidth <= nav.clientWidth) return;
    const item = nav.querySelector<HTMLElement>('[aria-current="page"]');
    if (!item) return;
    const navBox = nav.getBoundingClientRect();
    const box = item.getBoundingClientRect();
    if (box.left >= navBox.left && box.right <= navBox.right) return;
    nav.scrollLeft += box.left - navBox.left - (navBox.width - box.width) / 2;
  }, [currentKey]);

  const onClick = (event: MouseEvent<HTMLAnchorElement>, item: PageTabItem) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    if (onNavigate) onNavigate(item);
    else router.push(item.href);
  };

  return (
    <nav
      ref={navRef}
      className="ui-page-tabs"
      aria-label={label}
      data-count={items.length}
      data-testid={testId}
    >
      {items.map((item) => (
        <a
          key={item.key}
          href={appPath(item.href)}
          className="ui-page-tab"
          aria-current={item.current ? "page" : undefined}
          data-testid={item.testId}
          onClick={(event) => onClick(event, item)}
        >
          <span>{item.label}</span>
          {item.count === undefined ? null : item.count === null ? (
            <span className="cnt cnt--loading" aria-hidden="true" />
          ) : (
            <span className="cnt">{item.count}</span>
          )}
        </a>
      ))}
    </nav>
  );
}
