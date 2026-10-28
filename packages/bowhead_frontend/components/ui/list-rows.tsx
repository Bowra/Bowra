"use client";

/**
 * Mobile management-table list rows (appearance decision 12: lists instead of horizontal-scroll tables;
 * Narrow Spec “管理类表格”: < 640 list rows, min height 56, left padding 12, row-end ⋯ 44×44).
 *
 * Callers render them only when the viewport is < 640 (`useListRowsLayout()`); wider viewports keep the
 * table. Each table chooses slots by column importance
 * (P1 identity → title/second line, P2 status → trailing badge, P3 secondary → second line/details, P4 actions → ⋯),
 * design docs/design/组件统一-技术设计.md “逐页验收 B”.
 */

import type { ReactNode } from "react";

import { useNarrowViewport } from "@/components/ui/sheet";

/** Whether list rows replace the table: viewport < 640. */
export function useListRowsLayout(): boolean {
  return useNarrowViewport(true);
}

export function ListRows({
  label,
  className,
  children,
}: {
  /** List accessible name (same as original table heading). */
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <ul className={["ui-list-rows", className].filter(Boolean).join(" ")} aria-label={label}>
      {children}
    </ul>
  );
}

export function ListRow({
  title,
  subtitle,
  meta,
  status,
  leading,
  actions,
  detail,
  onOpen,
  openLabel,
  expanded,
  selected = false,
  muted = false,
  className,
}: {
  /** P1 identity: name. */
  title: ReactNode;
  /** Second line: email/path/most important secondary information. */
  subtitle?: ReactNode;
  /** Third-line small text: other retained mobile metadata (time/counts etc.); caller joins with “ · ”. */
  meta?: ReactNode;
  /** P2 status: trailing badge/value (role/enabled/effect/amount). */
  status?: ReactNode;
  /** Leading area: avatar/file type/etc. */
  leading?: ReactNode;
  /** P4 actions: trailing “⋯” (RowActionsMenu or original row-menu button). */
  actions?: ReactNode;
  /** Expanded details below row (raw audit/event data). */
  detail?: ReactNode;
  /** Whole-row details opening (decision 11): title button fills row, ⋯/inline controls remain separately clickable. */
  onOpen?: () => void;
  /** Open button accessible name; defaults to title text. */
  openLabel?: string;
  /** Row itself expandable (audit): screen reader announces expanded state. */
  expanded?: boolean;
  selected?: boolean;
  /** Disabled/removed object: entire row translucent (same as table). */
  muted?: boolean;
  className?: string;
}) {
  const classes = [
    "ui-list-row",
    onOpen ? "is-openable" : "",
    selected ? "is-selected" : "",
    muted ? "is-muted" : "",
    className,
  ].filter(Boolean).join(" ");
  return (
    <li className={classes} aria-current={selected ? "true" : undefined}>
      <div className="ui-list-row-main">
        {leading ? <span className="ui-list-row-leading">{leading}</span> : null}
        <div className="ui-list-row-text">
          {onOpen ? (
            <button
              type="button"
              className="ui-list-row-title ui-list-row-open"
              aria-label={openLabel}
              aria-expanded={expanded}
              onClick={onOpen}
            >
              {title}
            </button>
          ) : (
            <div className="ui-list-row-title">{title}</div>
          )}
          {subtitle ? <div className="ui-list-row-sub">{subtitle}</div> : null}
          {meta ? <div className="ui-list-row-meta">{meta}</div> : null}
        </div>
        {status ? <div className="ui-list-row-status">{status}</div> : null}
        {actions ? <div className="ui-list-row-actions">{actions}</div> : null}
      </div>
      {detail ? <div className="ui-list-row-detail">{detail}</div> : null}
    </li>
  );
}
