"use client";

import { Fragment, useId, useState, type MouseEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { MoreH } from "@/components/icons";
import { MenuFloating, useMenu, useMenuItem, withConfirmEllipsis } from "@/components/ui/menu";

export interface RowAction {
  id: string;
  label: string;
  /**
   * Execute on selection. With `href` it runs on the link click (e.g. client navigation that calls
   * `preventDefault()` for plain clicks), leaving modified clicks / new tabs to the native link.
   */
  onSelect?: (event: MouseEvent<HTMLElement>) => void;
  /** Link item (download/details navigation): render `<a>`, preserve original opening behavior. */
  href?: string;
  /** Open in new tab (original `target="_blank"` link). */
  newTab?: boolean;
  /**
   * Actions deleting data, revoking access, or immediately invalidating active credentials (delete/remove/sign out/revoke/regenerate key/disable public entry):
   * red, last, separated; caller confirms first. Directly reversible toggles (disable/enable key, archive space) do not count.
   */
  danger?: boolean;
  /**
   * Whether danger selection confirms first (default true). Confirmation-first danger labels carry “…” (behavior batch A, Menus Spec §5);
   * deleting/revoking access confirms first, except undoable deletion (Undo Toast afterward, behavior batch B: memory to trash/file deletion).
   * Pass false: no confirmation, no “…”.
   */
  confirms?: boolean;
  disabled?: boolean;
  /** Disabled reason: persistent second-line small text, read via aria-describedby. */
  disabledReason?: string;
  icon?: ReactNode;
}

/**
 * List/table row “⋯” menu (appearance batch 4, decisions 3/5: inline actions move into “⋯”,
 * danger items red and last).
 *
 * Selection invokes original button handler (danger action opens confirmation), preserving confirmation flow. Portal to body,
 * bottom/right-aligned; close when entire row leaves view (`closeWhenAnchorHidden`, same as KB/credits row menus).
 * Retain caller order; move dangers to end with separator before them.
 */
export function RowActionsMenu({
  label,
  actions,
  className,
  busy = false,
  busyLabel,
  pending = false,
}: {
  /** Trigger/menu accessible name; defaults to “更多操作”. For multiple rows include object name. */
  label?: string;
  actions: RowAction[];
  className?: string;
  /** Row operation running: busy trigger (spinner + screen-reader text), cannot open menu again. */
  busy?: boolean;
  busyLabel?: string;
  /**
   * An operation is running elsewhere in list/page (menu can still open): items disabled for that reason without their own explanation show generic
   * “正在处理上一个操作” reason on second line (behavior batch A, persistent disabled reasons).
   */
  pending?: boolean;
}) {
  const t = useTranslations("common");
  const [open, setOpen] = useState(false);
  const menu = useMenu({
    open: open && !busy,
    onOpenChange: (next) => setOpen(next && !busy),
    placement: "bottom-end",
    offset: 4,
    closeWhenAnchorHidden: true,
  });
  if (!actions.length) return null;
  const name = label ?? t("more_actions");
  const regular = actions.filter((action) => !action.danger);
  const danger = actions.filter((action) => action.danger);
  return (
    <>
      <button
        ref={menu.refs.setReference}
        type="button"
        className={`ui-row-more${busy ? " is-busy" : ""}${className ? ` ${className}` : ""}`}
        aria-label={busy && busyLabel ? busyLabel : name}
        aria-busy={busy || undefined}
        aria-disabled={busy || undefined}
        {...menu.getReferenceProps({
          // Rows often open details on click/Enter: trigger/menu clicks and keys must not bubble to row
          // (portal mounts in body, but React events still bubble through component tree).
          onClick: stopPropagation,
          onKeyDown: stopPropagation,
        })}
      >
        {busy ? <span className="ui-row-more-spinner" aria-hidden /> : <MoreH aria-hidden />}
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: "ui-row-menu",
            "aria-label": name,
            onClick: stopPropagation,
            onKeyDown: stopPropagation,
          })}
        >
          {regular.map((action) => (
            <RowActionItem key={action.id} action={action} pendingReason={pending ? t("busy_reason") : undefined} />
          ))}
          {regular.length && danger.length ? <div role="separator" className="ui-row-menu-sep" /> : null}
          {danger.map((action) => (
            <Fragment key={action.id}>
              <RowActionItem action={action} pendingReason={pending ? t("busy_reason") : undefined} />
            </Fragment>
          ))}
        </div>
      </MenuFloating>
    </>
  );
}

function stopPropagation(event: { stopPropagation: () => void }) {
  event.stopPropagation();
}

function RowActionItem({ action, pendingReason }: { action: RowAction; pendingReason?: string }) {
  // Confirmation-first dangerous items carry “…”.
  const label = action.danger && action.confirms !== false ? withConfirmEllipsis(action.label) : action.label;
  const itemProps = useMenuItem({
    onSelect: action.onSelect,
    disabled: action.disabled,
    label,
  });
  const reasonId = useId();
  const reason = action.disabled ? (action.disabledReason ?? pendingReason ?? null) : null;
  const className = `ui-row-menu-item${action.danger ? " is-danger" : ""}`;
  const content = (
    <>
      {action.icon}
      <span>{label}</span>
    </>
  );
  return (
    <>
      {action.href && !action.disabled ? (
        <a
          className={className}
          href={action.href}
          target={action.newTab ? "_blank" : undefined}
          rel={action.newTab ? "noreferrer" : undefined}
          {...itemProps}
        >
          {content}
        </a>
      ) : (
        <button type="button" className={className} aria-describedby={reason ? reasonId : undefined} {...itemProps}>
          {content}
        </button>
      )}
      {reason ? (
        <span id={reasonId} className="ui-row-menu-reason">
          {reason}
        </span>
      ) : null}
    </>
  );
}
