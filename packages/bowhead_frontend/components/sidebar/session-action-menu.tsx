"use client";

import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from "react";

import { MoreH } from "@/components/icons";
import { MenuFloating, useMenu, useMenuItem, withConfirmEllipsis } from "@/components/ui/menu";

export interface SessionMenuAction {
  id: string;
  label: string;
  icon: ReactNode;
  disabled?: boolean;
  /** Hover hint; when disabled, also read as the reason via aria-describedby. */
  title?: string;
  /** Destructive action (Delete)—use red text to distinguish it from Archive. */
  danger?: boolean;
  /** Opens a dialog (Rename): the menu shows the label with a trailing “…” (Menus Spec). Store the label unadorned. */
  opensDialog?: boolean;
  onSelect: () => void;
}

/**
 * Session / member-row “⋯” actions. `useMenu` handles arrows, Home/End, typeahead, Esc, outside clicks,
 * closing on ancestor scroll and focus return (component unification step 4); retain the `.app-floating-menu` visual contract.
 * Mount on body, right-aligned below the trigger; flip above when space is insufficient.
 */
export function SessionActionMenu({
  label,
  actions,
  onOpenChange,
  triggerText,
  triggerClassName,
  wrapperClassName,
  menuClassName,
}: {
  label: string;
  actions: SessionMenuAction[];
  onOpenChange?: (open: boolean) => void;
  triggerText?: string;
  triggerClassName?: string;
  wrapperClassName?: string;
  menuClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const onOpenChangeRef = useRef(onOpenChange);

  useEffect(() => {
    onOpenChangeRef.current = onOpenChange;
  }, [onOpenChange]);

  const menu = useMenu({
    open,
    onOpenChange: (next) => {
      setOpen(next);
      onOpenChangeRef.current?.(next);
    },
    placement: "bottom-end",
    offset: 4,
    closeOnScroll: true,
  });

  if (!actions.length) return null;

  return (
    <div className={`unified-work-more-wrap ${wrapperClassName ?? ""}`}>
      <button
        ref={menu.refs.setReference}
        type="button"
        className={`unified-work-more ${triggerClassName ?? ""}`}
        aria-label={label}
        {...menu.getReferenceProps()}
      >
        {triggerText ? <span>{triggerText}</span> : null}
        <MoreH aria-hidden />
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            className: `app-floating-menu unified-work-menu is-portal ${menuClassName ?? ""}`,
            "aria-label": label,
          })}
        >
          {actions.map((action, index) => (
            <Fragment key={action.id}>
              {/* Menus Spec: a separator sets the destructive item apart from the rest. */}
              {action.danger && index > 0 && !actions[index - 1].danger ? (
                <div role="separator" className="ui-row-menu-sep" />
              ) : null}
              <SessionActionItem action={action} />
            </Fragment>
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function SessionActionItem({ action }: { action: SessionMenuAction }) {
  const label = action.opensDialog ? withConfirmEllipsis(action.label) : action.label;
  const itemProps = useMenuItem({
    onSelect: action.onSelect,
    disabled: action.disabled,
    label,
  });
  const reasonId = useId();
  // Behavior batch A: omit native disabled so keyboard highlighting can read the reason (useMenuItem blocks execution).
  // Keep the reason outside the button: placing it inside would include it in the accessible name.
  const reason = action.disabled && action.title ? action.title : null;
  return (
    <>
      <button
        type="button"
        className="app-floating-menu-item"
        title={action.title}
        aria-describedby={reason ? reasonId : undefined}
        data-danger={action.danger ? "true" : undefined}
        {...itemProps}
      >
        {action.icon}
        <span>{label}</span>
      </button>
      {reason ? (
        <span id={reasonId} className="unified-work-sr-only">
          {reason}
        </span>
      ) : null}
    </>
  );
}
