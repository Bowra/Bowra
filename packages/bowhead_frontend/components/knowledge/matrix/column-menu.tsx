"use client";

/**
 * kb-matrix M2 — run menu + column-header menu (verbatim design r2 RunMenu/ColMenu).
 * M2 run menus omit Rerun Stale (added in M3); column-menu Convert to Template Field is hidden in M2/M3
 * (appears in M4) — FR-13.
 *
 * Both menus are shared menus (useMenu) (P2 batch 5 accessibility fix): mounted on body, menu
 * semantics, arrow keys, Home/End, type-ahead, Esc returns focus to the trigger. Formerly they were in-place
 * `div role=button` popovers that only answered Enter. The shared menu look applies, narrow screens get the
 * bottom sheet and items that open a confirmation end with "…" (behavior batch A).
 * Portal events still bubble through the React tree: clicks and keys stop at the menu so the matrix does not close
 * panels or move the cell focus.
 */

import { type ReactNode, useEffect, useLayoutEffect } from "react";
import { useTranslations } from "next-intl";

import { MenuFloating, useMenu, useMenuItem, withConfirmEllipsis } from "@/components/ui/menu";

import { IcClock, IcPencil, IcPlay, IcRefresh, IcX } from "./matrix-icons";

export type RunMenuAction = "all" | "force" | "stale";
export type ColMenuAction = "run" | "force" | "edit" | "del";

function MatrixMenuItem({ icon, label, confirms = false, tail, danger = false, onSelect, testId }: {
  icon: ReactNode;
  label: string;
  /** Opens a confirmation: the label ends with "…". */
  confirms?: boolean;
  tail?: string;
  danger?: boolean;
  onSelect: () => void;
  testId?: string;
}) {
  const shown = confirms ? withConfirmEllipsis(label) : label;
  const item = useMenuItem({ label: shown, onSelect });
  return (
    <div className={`kb-opt${danger ? " danger" : ""}`} data-testid={testId} {...item}>
      {icon}
      <span>{shown}</span>
      {tail ? <span className="tail ui-menu-item-tail">{tail}</span> : null}
    </div>
  );
}

/** Play icon in the menu's icon colour. */
function PlayIcon({ s }: { s: number }) {
  return <IcPlay s={s} />;
}

/**
 * Points a menu at an element it does not render (the matrix run caret, a column header button). The trigger is not
 * wired through getReferenceProps, so a keyboard opening (click with detail 0) moves focus to the first item here.
 */
function useExternalAnchor(menu: ReturnType<typeof useMenu>, anchor: HTMLElement | null, open: boolean, viaKeyboard: boolean) {
  useLayoutEffect(() => {
    menu.refs.setReference(anchor);
    if (open) menu.returnFocusRef.current = anchor;
  }, [menu.refs, menu.returnFocusRef, anchor, open]);
  useEffect(() => {
    if (!open || !viaKeyboard) return;
    const frame = requestAnimationFrame(() => menu.elementsRef.current.find(Boolean)?.focus());
    return () => cancelAnimationFrame(frame);
  }, [menu.elementsRef, open, viaKeyboard]);
}

export function RunMenu({
  open,
  onOpenChange,
  anchor,
  viaKeyboard = false,
  pendingLabel,
  forceLabel,
  staleLabel,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  anchor: HTMLElement | null;
  viaKeyboard?: boolean;
  pendingLabel: string;
  forceLabel: string;
  /** M3 Rerun Stale count (snapshot stale && !edited, display only; authoritative count=estimate). */
  staleLabel: string;
  onPick: (a: RunMenuAction) => void;
}) {
  const t = useTranslations("kbMatrix");
  // Offset 5 from the caret puts the menu 6 below the split button, where the former popover sat.
  const menu = useMenu({ open, onOpenChange, placement: "bottom-end", offset: 5 });
  useExternalAnchor(menu, anchor, open, viaKeyboard);
  return (
    <MenuFloating menu={menu}>
      <div
        ref={menu.refs.setFloating}
        {...menu.getFloatingProps({ className: "kb-pop kb-mx-menu kb-mx-menu--run", "aria-label": t("run_options"), onClick: (e) => e.stopPropagation(), onKeyDown: (e) => e.stopPropagation() })}
        data-testid="kb-matrix-runmenu"
      >
        <MatrixMenuItem icon={<PlayIcon s={13} />} label={t("runmenu_all")} confirms tail={pendingLabel} onSelect={() => onPick("all")} />
        <MatrixMenuItem icon={<IcClock s={13} />} label={t("runmenu_stale")} confirms tail={staleLabel} onSelect={() => onPick("stale")} testId="kb-matrix-runmenu-stale" />
        <div className="kb-pop-sep" role="separator" />
        <MatrixMenuItem icon={<IcRefresh s={14} />} label={t("runmenu_force")} confirms tail={forceLabel} danger onSelect={() => onPick("force")} />
      </div>
    </MenuFloating>
  );
}

export function ColMenu({
  open,
  onOpenChange,
  anchor,
  viaKeyboard = false,
  columnName,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  anchor: HTMLElement | null;
  viaKeyboard?: boolean;
  columnName: string;
  onPick: (a: ColMenuAction) => void;
}) {
  const t = useTranslations("kbMatrix");
  const menu = useMenu({ open, onOpenChange, placement: "bottom-end", offset: 2, sheetTitle: columnName });
  useExternalAnchor(menu, anchor, open, viaKeyboard);
  return (
    <MenuFloating menu={menu}>
      <div
        ref={menu.refs.setFloating}
        {...menu.getFloatingProps({ className: "kb-pop kb-mx-menu kb-mx-menu--col", "aria-label": columnName, onClick: (e) => e.stopPropagation(), onKeyDown: (e) => e.stopPropagation() })}
        data-testid="kb-matrix-colmenu"
      >
        <MatrixMenuItem icon={<PlayIcon s={12} />} label={t("colmenu_run")} confirms onSelect={() => onPick("run")} />
        <MatrixMenuItem icon={<IcRefresh s={13} />} label={t("colmenu_force")} confirms danger onSelect={() => onPick("force")} />
        <MatrixMenuItem icon={<IcPencil s={12} />} label={t("colmenu_edit")} onSelect={() => onPick("edit")} />
        <div className="kb-pop-sep" role="separator" />
        <MatrixMenuItem icon={<IcX s={13} />} label={t("colmenu_delete")} confirms danger onSelect={() => onPick("del")} />
      </div>
    </MenuFloating>
  );
}
