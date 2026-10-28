"use client";

/**
 * Arrow-move-and-select primitive shared by Tabs and SegmentedControl (component unification P1 step 6,
 * docs/design/组件统一-技术设计.md).
 *
 * - floating-ui `Composite` handles left/right (or up/down / all four, by orientation): skip `disabled` /
 *   `aria-disabled` items, wrap ends, roving tabindex.
 * - Home / End: not handled by `Composite`; add here.
 * - Select only on focus caused by arrows / Home / End; mouse clicks select via onClick; Tab entering the group keeps selection
 *   (avoids selecting the first item merely by Tabbing in when none is selected).
 * - Roving current item follows selection: selected-value changes synchronize activeIndex to the marked item;
 *   without selection, use first enabled item so Tab can still enter. Restore selection when focus leaves the group
 *   (after arrows during saving or rejected caller switching, Tab returns to the truly selected item).
 * - `activation="manual"`: arrows only focus; Space / Enter or click selects. Radio selection submitting immediately to the server
 *   must use this (design: arrows must not trigger submission).
 */
import { Composite, CompositeItem } from "@floating-ui/react";
import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type FocusEvent,
  type HTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
} from "react";

type ContainerRole = "tablist" | "radiogroup";
type ItemRole = "tab" | "radio";

interface SelectionContextValue {
  onItemFocus: (value: string) => void;
}

const SelectionContext = createContext<SelectionContextValue | null>(null);

function isDisabled(el: HTMLElement): boolean {
  return el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
}

function ownItems(container: HTMLElement | null, containerRole: ContainerRole, itemRole: ItemRole): HTMLElement[] {
  if (!container) return [];
  return Array.from(container.querySelectorAll<HTMLElement>(`[role="${itemRole}"]`)).filter(
    (el) => el.closest(`[role="${containerRole}"]`) === container,
  );
}

/** Prepend UI-refresh base class to caller classes; with empty base, return caller classes unchanged. */
export function uiClassName(base: string | null, className: string | undefined): string | undefined {
  if (!base) return className;
  return className ? `${base} ${className}` : base;
}

const NAV_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"]);

export interface CompositeGroupProps extends Omit<HTMLAttributes<HTMLDivElement>, "onChange" | "role"> {
  containerRole: ContainerRole;
  itemRole: ItemRole;
  selectedAttr: "aria-selected" | "aria-checked";
  orientation?: "horizontal" | "vertical" | "both";
  /** `"automatic"` (default) selects on arrow movement; `"manual"` moves focus only. */
  activation?: "automatic" | "manual";
  onSelectValue: (value: string) => void;
  children?: ReactNode;
}

export const CompositeGroup = forwardRef<HTMLDivElement, CompositeGroupProps>(function CompositeGroup(
  { containerRole, itemRole, selectedAttr, orientation = "horizontal", activation = "automatic", onSelectValue, children, onKeyDown, onKeyUp, onBlur, ...rest },
  forwardedRef,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const keyNavRef = useRef(false);
  const lastSelectedRef = useRef<number | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const onSelectRef = useRef(onSelectValue);
  onSelectRef.current = onSelectValue;
  const manualRef = useRef(activation === "manual");
  manualRef.current = activation === "manual";

  const selectedIndex = () => {
    const items = ownItems(containerRef.current, containerRole, itemRole);
    let selected = items.findIndex((el) => el.getAttribute(selectedAttr) === "true");
    if (selected < 0) selected = items.findIndex((el) => !isDisabled(el));
    return selected < 0 ? 0 : selected;
  };

  const setRefs = useCallback(
    (node: HTMLDivElement | null) => {
      containerRef.current = node;
      if (typeof forwardedRef === "function") forwardedRef(node);
      else if (forwardedRef) forwardedRef.current = node;
    },
    [forwardedRef],
  );

  // Move the roving item only when selection changes (external value/items added or removed); never pull it back mid-navigation.
  useLayoutEffect(() => {
    const selected = selectedIndex();
    if (selected !== lastSelectedRef.current) {
      lastSelectedRef.current = selected;
      setActiveIndex(selected);
    }
  });

  const [ctxValue] = useState<SelectionContextValue>(() => ({
    onItemFocus: (value: string) => {
      if (!keyNavRef.current) return;
      keyNavRef.current = false;
      if (manualRef.current) return;
      onSelectRef.current(value);
    },
  }));

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    onKeyDown?.(event as KeyboardEvent<HTMLDivElement>);
    if (event.defaultPrevented) return;
    if (!NAV_KEYS.has(event.key)) return;
    keyNavRef.current = true;
    if (event.key !== "Home" && event.key !== "End") return;
    const items = ownItems(containerRef.current, containerRole, itemRole).filter((el) => !isDisabled(el));
    const target = event.key === "Home" ? items[0] : items[items.length - 1];
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    if (target === document.activeElement) {
      // Target already has focus (e.g. first item with no selection): focus will not fire again; select directly.
      keyNavRef.current = false;
      const value = target.getAttribute("data-composite-value");
      if (!manualRef.current && value !== null) onSelectRef.current(value);
      return;
    }
    target.focus();
  };

  return (
    <SelectionContext.Provider value={ctxValue}>
      <Composite
        {...rest}
        ref={setRefs as Ref<HTMLElement>}
        role={containerRole}
        orientation={orientation}
        loop
        activeIndex={activeIndex}
        onNavigate={setActiveIndex}
        onKeyDown={handleKeyDown}
        onBlur={(event) => {
          onBlur?.(event as FocusEvent<HTMLDivElement>);
          const next = event.relatedTarget as Node | null;
          if (next && containerRef.current?.contains(next)) return;
          setActiveIndex(selectedIndex());
        }}
        onKeyUp={(event) => {
          keyNavRef.current = false;
          onKeyUp?.(event as KeyboardEvent<HTMLDivElement>);
        }}
        render={(props) => <div {...(props as HTMLAttributes<HTMLDivElement>)} />}
      >
        {children}
      </Composite>
    </SelectionContext.Provider>
  );
});

export interface CompositeOptionProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  value: string;
  role: ItemRole;
  /** Caller's original data-active (for styling); discard CompositeItem's same-named attribute. */
  "data-active"?: string | boolean;
}

export const CompositeOption = forwardRef<HTMLButtonElement, CompositeOptionProps>(function CompositeOption(
  { value, onFocus, type = "button", ...rest },
  forwardedRef,
) {
  const ctx = useContext(SelectionContext);
  if (!ctx) throw new Error("CompositeOption must be rendered inside CompositeGroup");
  const callerActive = rest["data-active"];
  return (
    <CompositeItem
      ref={forwardedRef as Ref<HTMLElement>}
      onFocus={(event) => {
        onFocus?.(event as FocusEvent<HTMLButtonElement>);
        ctx.onItemFocus(value);
      }}
      render={(props) => {
        const { "data-active": _ignored, ...htmlProps } = props as Record<string, unknown>;
        void _ignored;
        return (
          <button
            {...(rest as ButtonHTMLAttributes<HTMLButtonElement>)}
            {...(htmlProps as ButtonHTMLAttributes<HTMLButtonElement>)}
            type={type}
            data-active={callerActive}
            data-composite-value={value}
          />
        );
      }}
    />
  );
});
