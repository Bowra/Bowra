"use client";

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";

import {
  X as LucideX,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

export interface ActiveFilter {
  /** Stable key (filter field name). */
  key: string;
  /** Include the field and value, e.g. Status: Failed. */
  label: string;
  onRemove: () => void;
  /** Where focus goes when removing this chip makes the row disappear (e.g. the filter button it came from). */
  restoreFocus?: () => void;
}

/**
 * Active-filter chip row (appearance decision 20; Misc Spec Filter Bar): appears below the toolbar when at least two conditions
 * are active; remove chips individually, with Clear All last. Render nothing with fewer than two conditions.
 * Filter controls remain the sole source of truth: chips are another presentation of selected values; removing one clears its control.
 *
 * Focus: after a chip is removed, focus moves to the chip that took its place (or Clear All); when the row disappears,
 * the removed chip's `restoreFocus` runs.
 */
export function ActiveFilterChips({
  filters,
  onClearAll,
  label,
  className,
}: {
  filters: readonly ActiveFilter[];
  onClearAll: () => void;
  /** Accessible row name, default “已选筛选”. */
  label?: string;
  /** Extra class on the row (page-specific layout, e.g. horizontal scrolling on phones). */
  className?: string;
}) {
  const t = useTranslations("common");
  const listRef = useRef<HTMLUListElement | null>(null);
  const pending = useRef<{ index: number; restore?: () => void } | null>(null);
  useEffect(() => {
    const next = pending.current;
    if (!next) return;
    pending.current = null;
    const list = listRef.current;
    if (!list || !list.isConnected) { next.restore?.(); return; }
    const removes = list.querySelectorAll<HTMLButtonElement>(".ui-filter-chip-remove");
    (removes[Math.min(next.index, removes.length - 1)] ?? list.querySelector<HTMLButtonElement>(".ui-filter-chips-clear"))?.focus();
  });
  if (filters.length < 2) return null;
  return (
    <ul ref={listRef} className={`ui-filter-chips${className ? ` ${className}` : ""}`} aria-label={label ?? t("active_filters")}>
      {filters.map((filter, index) => (
        <li key={filter.key} className="ui-filter-chip">
          <span className="ui-filter-chip-label">{filter.label}</span>
          <button
            type="button"
            className="ui-filter-chip-remove"
            aria-label={t("remove_filter", { label: filter.label })}
            onClick={() => {
              // Only move focus when the remove button had it (keyboard or pointer on this row).
              if (document.activeElement === listRef.current?.querySelectorAll(".ui-filter-chip-remove")[index]) {
                pending.current = { index, restore: filter.restoreFocus };
              }
              filter.onRemove();
            }}
          >
            <LucideGlyph icon={LucideX} size={11} aria-hidden="true" />
          </button>
        </li>
      ))}
      <li>
        <button type="button" className="ui-filter-chips-clear" onClick={onClearAll}>
          {t("clear_all_filters")}
        </button>
      </li>
    </ul>
  );
}
