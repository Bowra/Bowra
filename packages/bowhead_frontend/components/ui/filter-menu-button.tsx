"use client";

/**
 * Single-select "Label: Value ⌄" menu button replacing native selects in toolbars (Agents & Reports polish spec:
 * 28 high, filled; accent-soft while the value narrows the list). The menu uses menuitemradio with the leading check
 * from the shared menu contract; highlight (whole-row accent) and selection (check) stay separate.
 */

import { useId, useState } from "react";
import { ChevronDown } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";

export interface FilterMenuOption<T extends string = string> {
  value: T;
  label: string;
}

/** Sentinel used to split a localized "Label: {value}" message around its value. */
const VALUE_SLOT = "\u0000";

/**
 * Splits `format(VALUE_SLOT)` into the text before and after the value, so locales keep their own punctuation
 * ("分类：", "Catégorie : ") while the value renders with its own emphasis.
 */
export function splitValueTemplate(format: (value: string) => string): [string, string] {
  const text = format(VALUE_SLOT);
  const index = text.indexOf(VALUE_SLOT);
  if (index < 0) return [text, ""];
  return [text.slice(0, index), text.slice(index + VALUE_SLOT.length)];
}

function FilterMenuItem<T extends string>({
  option,
  checked,
  onSelect,
}: {
  option: FilterMenuOption<T>;
  checked: boolean;
  onSelect: (value: T) => void;
}) {
  const item = useMenuItem({ label: option.label, checked, role: "menuitemradio", onSelect: () => onSelect(option.value) });
  return (
    <button type="button" className="ui-filter-menu-item" {...item}>
      <span>{option.label}</span>
    </button>
  );
}

export function FilterMenuButton<T extends string>({
  value,
  options,
  onChange,
  format,
  menuLabel,
  active,
  className,
  testId,
  disabled = false,
  labelledBy,
}: {
  value: T;
  options: Array<FilterMenuOption<T>>;
  onChange: (value: T) => void;
  /** Localized "Label: {value}" message, e.g. `(value) => t("filter_category", { value })`. */
  format: (value: string) => string;
  /** Accessible name of the menu (and the bottom-sheet title on phones), e.g. "Category". */
  menuLabel: string;
  /** Whether the current value narrows the list (accent-soft fill). */
  active: boolean;
  className?: string;
  testId?: string;
  disabled?: boolean;
  /** Id of a visible row label; the button is then named "<label> <value>" (settings rows, like the language rows). */
  labelledBy?: string;
}) {
  const [open, setOpen] = useState(false);
  const valueId = useId();
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start", offset: 4, selectedIndex, sheetTitle: menuLabel });
  const current = options[selectedIndex]?.label ?? "";
  const [prefix, suffix] = splitValueTemplate(format);
  return (
    <>
      <button
        type="button"
        ref={menu.refs.setReference}
        className={["ui-filter-menu-button", className].filter(Boolean).join(" ")}
        data-active={active ? "true" : undefined}
        data-testid={testId}
        disabled={disabled}
        title={format(current)}
        aria-labelledby={labelledBy ? `${labelledBy} ${valueId}` : undefined}
        {...menu.getReferenceProps()}
      >
        <span className="ui-filter-menu-button__text">
          {prefix ? <span className="ui-filter-menu-button__label">{prefix}</span> : null}
          <span id={valueId} className="ui-filter-menu-button__value">{current}</span>
          {suffix ? <span className="ui-filter-menu-button__label">{suffix}</span> : null}
        </span>
        <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" className="ui-filter-menu-button__chevron" />
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          data-testid={testId ? `${testId}-menu` : undefined}
          {...menu.getFloatingProps({ className: "ui-filter-menu", "aria-label": menuLabel })}
        >
          {options.map((option) => (
            <FilterMenuItem key={option.value} option={option} checked={option.value === value} onSelect={onChange} />
          ))}
        </div>
      </MenuFloating>
    </>
  );
}
