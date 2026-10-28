"use client";

/**
 * Knowledge base file scope filter (P2 batch 5, option B).
 *
 * Replaces the scope segments with one single-select "Scope: X" filter button in the toolbar. Each option shows
 * its file count and who can search it; "All" (every scope the caller can read) is offered only when the server
 * supports the cross-scope list. The menu draws the selection check (menuitemradio). Test ids are kb-scope-filter-*
 * (kb-scope-* belongs to the upload and
 * "add to knowledge base" dialogs).
 */

import { useId, useState } from "react";
import { useTranslations } from "next-intl";

import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import type { KbListScope } from "@/lib/kb-api";

import { IconCaret } from "./kb-icons";

export interface KbScopeFilterOption {
  id: KbListScope;
  /** Option name, e.g. "Space · Retail". */
  label: string;
  /** Who can search files in this scope. */
  hint: string;
  /** null = still loading or unknown (no number shown). */
  count: number | null;
}

function ScopeOption({ option, checked, onSelect }: { option: KbScopeFilterOption; checked: boolean; onSelect: () => void }) {
  const item = useMenuItem({ label: option.label, checked, role: "menuitemradio", onSelect });
  const hintId = useId();
  return (
    <button type="button" className="kb-scope-opt" data-testid={`kb-scope-filter-${option.id}`} aria-describedby={hintId} {...item}>
      <span className="txt">
        <span className="nm">{option.label}</span>
        <span className="hint" id={hintId}>{option.hint}</span>
      </span>
      {option.count === null ? null : <span className="count">{option.count}</span>}
    </button>
  );
}

export function KbScopeFilter({
  scope,
  options,
  onSelect,
}: {
  scope: KbListScope;
  options: KbScopeFilterOption[];
  onSelect: (scope: KbListScope) => void;
}) {
  const t = useTranslations("knowledge_base");
  const [open, setOpen] = useState(false);
  const selectedIndex = Math.max(0, options.findIndex((o) => o.id === scope));
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start", selectedIndex, sheetTitle: t("col_scope") });
  const current = options[selectedIndex];
  return (
    <>
      <button
        type="button"
        className={`kb-filterchip kb-scope-filter${scope === "all" ? "" : " active"}`}
        data-testid="kb-scope-filter"
        title={t("scope_filter.button", { scope: current?.label ?? "" })}
        ref={menu.refs.setReference}
        {...menu.getReferenceProps()}
      >
        <span className="lbl">{t("scope_filter.button", { scope: current?.label ?? "" })}</span>
        <span className="kb-chevron" aria-hidden><IconCaret /></span>
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} data-testid="kb-scope-filter-menu" {...menu.getFloatingProps({ className: "kb-scope-menu", "aria-label": t("col_scope") })}>
          {options.map((o) => (
            <ScopeOption key={o.id} option={o} checked={o.id === scope} onSelect={() => onSelect(o.id)} />
          ))}
        </div>
      </MenuFloating>
    </>
  );
}
