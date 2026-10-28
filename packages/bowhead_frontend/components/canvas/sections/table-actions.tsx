"use client";

import { createContext, useContext, useId, useState, type ReactNode } from "react";

import { MenuFloating, MenuItemReason, useMenu, useMenuItem } from "@/components/ui/menu";

import { ChartMenuRootContext } from "./chart-menu-root";
import { CharIcon } from "@/components/char-icon";

/**
 * Persistent control (chart-kind switcher) reports menu open state to its action bar: open menu adds `is-open`,
 * keeping report hover-only action bar opaque when pointer leaves chart (menu itself is mounted on body).
 * Null outside action bar (KPI card header).
 */
export const ChartActionsLeadOpenContext = createContext<((open: boolean) => void) | null>(null);

export interface CanvasTableAction {
  key: string;
  label: ReactNode;
  href?: string | null;
  onClick?: () => void;
  title?: string;
  disabled?: boolean;
  hidden?: boolean;
  target?: string;
  rel?: string;
}

interface TableActionsBase {
  actions: CanvasTableAction[];
  /** Persistent control (chart-kind switcher) exempt from quiet-by-default behavior — see `QuietActions`. */
  children?: ReactNode;
  /** Render surface. `"export"` = export visual page (PDF/PPTX screenshot source). */
  surface?: "canvas" | "chat" | "export";
}

/** `moreLabel` is **required** for `variant="quiet"`, forbidden for other variants.
 *  Use union rather than optional property: `⋯` is one character; missing label leaves screen reader with nothing
 *  to announce, without runtime errors or visible clues — enforce at compile time through types. */
export type TableActionsProps = TableActionsBase &
  (
    | { variant?: "chart" | "data-table"; moreLabel?: never }
    | { variant: "quiet"; moreLabel: string }
  );

export function TableActions({
  actions,
  children,
  variant = "chart",
  surface,
  moreLabel,
}: TableActionsProps) {
  // No actions on export surface: paper is not clickable. **Criterion lives only here** —
  // 2026-08-15 dogfood on 116 found PDF charts topped with "Line ▾ / Open data /
  // Download PNG / Download CSV", four dead buttons (also always present in golden PNG).
  // Originally this was only chart-block inspect **one** action's `hidden` condition;
  // remaining three + chart-kind switcher + both chart-table headers were missed. Put it in component itself,
  // covering new callers automatically without repeating checks (half this sprint's defects share this shape).
  // children (chart-kind switcher) also omitted — an interactive control too.
  if (surface === "export") return null;
  const className = variant === "data-table" ? "v2-dt-download" : "v2-chart-action-btn";
  const visible = actions.filter((action) => !action.hidden);
  if (variant === "quiet") {
    // children (chart-kind switcher) **remain outside `⋯`**: see `QuietActions` comment.
    return (
      <QuietActions className={className} lead={children} moreLabel={moreLabel} actions={visible} />
    );
  }
  const buttons = visible.map((action) => (
    <TableAction key={action.key} action={action} className={className} />
  ));
  const content = (
    <>
      {children}
      {buttons}
    </>
  );
  if (variant === "chart") {
    return <div className="v2-chart-actions">{content}</div>;
  }
  return content;
}

/** Batch 2 §3.5 quiet by default: collapse persistent action buttons into one ⋯ entry.
 *  Desktop hover-only, touch always visible (CSS @media (hover:none)) — report charts should remain
 *  quiet, showing actions only when needed.
 *
 *  ★ `lead` (chart-kind switcher) **stays outside ⋯ and is never hidden on hover**: user explicitly requested
 *  "chart 切换还是需要单独的" on 2026-09-02. It is this row's most-used control; hiding it in a submenu hides
 *  the option to try another chart. Thus four buttons become one ⋯ + persistent switcher,
 *  rather than collapsing all five. */
function QuietActions({
  className, lead, moreLabel, actions,
}: {
  className: string;
  lead?: ReactNode;
  moreLabel?: string;
  actions: CanvasTableAction[];
}) {
  const [open, setOpen] = useState(false);
  const [leadOpen, setLeadOpen] = useState(false);
  // useMenu handles keyboard, Esc, outside clicks, and focus return. Selection order: return focus to `⋯` → close menu → perform action,
  // so dialogs opened by actions such as inspection remember `⋯` as focus-return target, rather than unmounted menu item.
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-end", offset: 4 });
  const menuRoot = useContext(ChartMenuRootContext);

  return (
    // `is-open` explicitly prevents ⋯ fading when pointer leaves chart while menu is open,
    // without relying on `:has()` (116 is an old HTTP-only environment; avoid newer selectors when possible).
    <div className={`v2-chart-actions is-quiet${open || leadOpen ? " is-open" : ""}`}>
      <ChartActionsLeadOpenContext.Provider value={setLeadOpen}>{lead}</ChartActionsLeadOpenContext.Provider>
      <button
        type="button"
        ref={menu.refs.setReference}
        className={`${className} is-more`}
        aria-label={moreLabel}
        title={moreLabel}
        {...menu.getReferenceProps()}
      >
        <CharIcon ch="⋯" />
      </button>
      <MenuFloating menu={menu} root={menuRoot}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({ className: "v2-chart-actions-pop", "aria-label": moreLabel })}
        >
          {actions.map((action) => (
            <QuietMenuItem key={action.key} action={action} className={className} />
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function QuietMenuItem({ action, className }: { action: CanvasTableAction; className: string }) {
  const itemProps = useMenuItem({
    onSelect: action.onClick ? () => action.onClick?.() : undefined,
    disabled: action.disabled,
  });
  if (action.href && !action.disabled) {
    return (
      <a
        href={action.href}
        target={action.target ?? "_blank"}
        rel={action.rel ?? "noreferrer"}
        className={className}
        title={action.title}
        {...itemProps}
      >
        {action.label}
      </a>
    );
  }
  return <QuietMenuButton action={action} className={className} itemProps={itemProps} />;
}

/**
 * Button item within ⋯. Disabled items (behavior batch A) omit native disabled (still keyboard-highlightable),
 * with reason (`title`) always on second line and read via aria-describedby, rather than only in hover tooltip.
 */
function QuietMenuButton({
  action,
  className,
  itemProps,
}: {
  action: CanvasTableAction;
  className: string;
  itemProps: ReturnType<typeof useMenuItem>;
}) {
  const reasonId = useId();
  const reason = action.disabled && action.title ? action.title : null;
  return (
    <>
      <button
        type="button"
        className={className}
        title={action.title}
        aria-describedby={reason ? reasonId : undefined}
        {...itemProps}
      >
        {action.label}
      </button>
      {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </>
  );
}

function TableAction({ action, className }: { action: CanvasTableAction; className: string }) {
  if (action.href && !action.disabled) {
    return (
      <a
        href={action.href}
        target={action.target ?? "_blank"}
        rel={action.rel ?? "noreferrer"}
        className={className}
        title={action.title}
      >
        {action.label}
      </a>
    );
  }
  return (
    <button
      type="button"
      onClick={action.onClick}
      className={className}
      title={action.title}
      disabled={action.disabled}
    >
      {action.label}
    </button>
  );
}
