"use client";

/**
 * kb-matrix M2 — run menu + column-header menu (verbatim design r2 RunMenu/ColMenu; run menu per step 1 mockup ⑤).
 * Step 1 (S4): the column menu adds Mark Column Verified… (disabled, with its reason, when nothing can be marked).
 * Column-menu Convert to Template Field is hidden in M2/M3 (appears in M4) — FR-13.
 *
 * Both menus are shared menus (useMenu) (P2 batch 5 accessibility fix): mounted on body, menu
 * semantics, arrow keys, Home/End, type-ahead, Esc returns focus to the trigger. Formerly they were in-place
 * `div role=button` popovers that only answered Enter. The shared menu look applies, narrow screens get the
 * bottom sheet and items that open a confirmation end with "…" (behavior batch A).
 * Portal events still bubble through the React tree: clicks and keys stop at the menu so the matrix does not close
 * panels or move the cell focus.
 */

import { type ReactNode, useEffect, useId, useLayoutEffect } from "react";
import { useTranslations } from "next-intl";

import { MENU_DESCRIPTION_ATTR, MenuFloating, useMenu, useMenuItem, withConfirmEllipsis } from "@/components/ui/menu";
import { type RunMenuCounts, TRIAL_FILE_COUNT } from "@/lib/kb-matrix/types";

import { IcCheck, IcClock, IcDoc, IcPencil, IcPlay, IcRefresh, IcWarn, IcX } from "./matrix-icons";

export type RunMenuAction = "all" | "stale" | "failed" | "trial" | "force";
export type ColMenuAction = "run" | "force" | "verify" | "edit" | "del";

function MatrixMenuItem({ icon, label, confirms = false, tail, danger = false, disabled = false, note, onSelect, testId }: {
  icon: ReactNode;
  label: string;
  /** Opens a confirmation: the label ends with "…". */
  confirms?: boolean;
  tail?: string;
  danger?: boolean;
  disabled?: boolean;
  /** Second line: why the item is disabled, or what it keeps. */
  note?: string;
  onSelect: () => void;
  testId?: string;
}) {
  const shown = confirms ? withConfirmEllipsis(label) : label;
  const noteId = useId();
  const item = useMenuItem({ label: shown, onSelect, disabled });
  // A disabled item's reason is not a description (it does not move narrow menus into a sheet).
  const description = note && !disabled ? { [MENU_DESCRIPTION_ATTR]: "" } : null;
  return (
    <>
      <div className={`kb-opt${danger ? " danger" : ""}`} data-testid={testId} aria-describedby={note ? noteId : undefined} {...item}>
        {icon}
        <span>{shown}</span>
        {tail ? <span className="tail ui-menu-item-tail">{tail}</span> : null}
      </div>
      {note ? (
        <span id={noteId} className="ui-menu-reason" {...description}>
          {note}
        </span>
      ) : null}
    </>
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

/**
 * Step 1 (S3, mockup ⑤): run unfinished, stale or failed cells (cell counts), then, after a separator, a trial on
 * the first few rows and rerun all, whose second line says how many verified and edited cells it keeps.
 */
export function RunMenu({
  open,
  onOpenChange,
  anchor,
  viaKeyboard = false,
  counts,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  anchor: HTMLElement | null;
  viaKeyboard?: boolean;
  /** Snapshot counts (display only; the estimate is authoritative). */
  counts: RunMenuCounts;
  onPick: (a: RunMenuAction) => void;
}) {
  const t = useTranslations("kbMatrix");
  const keeps =
    counts.keptVerified > 0 || counts.keptEdited > 0
      ? t("runmenu_force_keeps", { verified: counts.keptVerified, edited: counts.keptEdited })
      : t("runmenu_force_keeps_none");
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
        <MatrixMenuItem icon={<PlayIcon s={13} />} label={t("runmenu_all")} tail={t("runmenu_cells", { n: counts.pending })} onSelect={() => onPick("all")} testId="kb-matrix-runmenu-all" />
        <MatrixMenuItem icon={<IcClock s={13} />} label={t("runmenu_stale")} tail={t("runmenu_cells", { n: counts.stale })} onSelect={() => onPick("stale")} testId="kb-matrix-runmenu-stale" />
        <MatrixMenuItem icon={<IcWarn s={13} />} label={t("runmenu_failed")} tail={t("runmenu_cells", { n: counts.failed })} onSelect={() => onPick("failed")} testId="kb-matrix-runmenu-failed" />
        <div className="kb-pop-sep" role="separator" />
        <MatrixMenuItem
          icon={<IcDoc s={13} />}
          label={t("runmenu_trial", { n: counts.trialFiles || TRIAL_FILE_COUNT })}
          confirms
          disabled={counts.trialFiles === 0}
          note={counts.trialFiles === 0 ? t("runmenu_trial_none") : undefined}
          onSelect={() => onPick("trial")}
          testId="kb-matrix-runmenu-trial"
        />
        <MatrixMenuItem icon={<IcRefresh s={14} />} label={t("runmenu_force")} confirms danger note={keeps} onSelect={() => onPick("force")} testId="kb-matrix-runmenu-force" />
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
  verifiable = 1,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  anchor: HTMLElement | null;
  viaKeyboard?: boolean;
  columnName: string;
  /** Step 1 (S4): cells "整列标记已核" would mark (done, current, not yet verified); 0 disables it with the reason. */
  verifiable?: number;
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
        <MatrixMenuItem
          icon={<IcCheck s={13} />}
          label={t("colmenu_verify")}
          confirms
          disabled={verifiable === 0}
          note={verifiable === 0 ? t("colmenu_verify_none") : undefined}
          onSelect={() => onPick("verify")}
          testId="kb-matrix-colmenu-verify"
        />
        <MatrixMenuItem icon={<IcPencil s={12} />} label={t("colmenu_edit")} onSelect={() => onPick("edit")} />
        <div className="kb-pop-sep" role="separator" />
        <MatrixMenuItem icon={<IcX s={13} />} label={t("colmenu_delete")} confirms danger onSelect={() => onPick("del")} />
      </div>
    </MenuFloating>
  );
}
