"use client";

/**
 * Tabs (component unification P1, step 6, docs/design/组件统一-技术设计.md).
 *
 * Behavior and aria only; legacy appearance adds no visuals: caller supplies unchanged classes, inline styles and children.
 * Appearance batch two: `TabList` `appearance` adds `ui-tabs ui-tabs--underline` (underlined tabs) or
 * `ui-tabs ui-tabs--segmented` (segmented-style tabs); styles/ui-refresh.css unifies them per spec.
 * Omit appearance to add no class (call site opts out of unification).
 *
 * ```tsx
 * <Tabs value={tab} onValueChange={setTab}>
 *   <TabList className="x-tabs" aria-label="…">
 *     <Tab value="a" className={tab === "a" ? "on" : ""}>A</Tab>
 *     <Tab value="b" disabled>B</Tab>
 *   </TabList>
 *   <TabPanel value="a">…</TabPanel>
 * </Tabs>
 * ```
 *
 * - Left/right (up/down with `orientation="vertical"`) move and auto-activate, wrap, skip disabled items; Home/End.
 * - Only arrows / Home / End auto-activate; focus alone (entry via Tab) does not change selection. Clicking any item (even already selected)
 *   invokes `onValueChange`, matching pre-migration onClick behavior; callers wanting no repeat callbacks compare values themselves.
 * - Tab id is `${idBase}-tab-${value}`; add `aria-controls` only when its `TabPanel` is mounted;
 *   `TabPanel` id is `${idBase}-panel-${value}`, with `aria-labelledby` pointing to Tab.
 *   `idBase` defaults to `useId()`; pass explicitly for stable IDs (tests, anchors).
 * - `TabPanel` renders only the current-value panel; use `keepMounted` to retain inactive panels (via `hidden`).
 */
import {
  createContext,
  forwardRef,
  useContext,
  useId,
  useLayoutEffect,
  useMemo,
  useState,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from "react";

import { CompositeGroup, CompositeOption, uiClassName } from "./composite-selection";

interface TabsContextValue {
  value: string;
  select: (value: string) => void;
  idBase: string;
  panels: ReadonlySet<string>;
  registerPanel: (value: string) => () => void;
}

const TabsContext = createContext<TabsContextValue | null>(null);

function useTabsContext(name: string): TabsContextValue {
  const ctx = useContext(TabsContext);
  if (!ctx) throw new Error(`${name} must be rendered inside <Tabs>`);
  return ctx;
}

function safeId(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, "_");
}

export function tabId(idBase: string, value: string): string {
  return `${idBase}-tab-${safeId(value)}`;
}

export function tabPanelId(idBase: string, value: string): string {
  return `${idBase}-panel-${safeId(value)}`;
}

export interface TabsProps<T extends string = string> {
  value: T;
  onValueChange: (value: T) => void;
  idBase?: string;
  children: ReactNode;
}

export function Tabs<T extends string = string>({ value, onValueChange, idBase, children }: TabsProps<T>) {
  const generated = safeId(useId());
  const base = idBase ?? `tabs${generated}`;
  const [panels, setPanels] = useState<ReadonlySet<string>>(() => new Set());
  const ctx = useMemo<TabsContextValue>(
    () => ({
      value,
      select: (next) => {
        onValueChange(next as T);
      },
      idBase: base,
      panels,
      registerPanel: (panelValue) => {
        setPanels((prev) => new Set(prev).add(panelValue));
        return () =>
          setPanels((prev) => {
            const next = new Set(prev);
            next.delete(panelValue);
            return next;
          });
      },
    }),
    [value, onValueChange, base, panels],
  );
  return <TabsContext.Provider value={ctx}>{children}</TabsContext.Provider>;
}

export interface TabListProps extends Omit<HTMLAttributes<HTMLDivElement>, "role" | "onChange"> {
  orientation?: "horizontal" | "vertical";
  /** Appearance: `"underline"` tabs (page/drawer sections); `"segmented"` segmented-control style. */
  appearance?: "underline" | "segmented";
}

export const TabList = forwardRef<HTMLDivElement, TabListProps>(function TabList(
  { appearance, className, ...props },
  ref,
) {
  const ctx = useTabsContext("TabList");
  return (
    <CompositeGroup
      {...props}
      className={uiClassName(appearance ? `ui-tabs ui-tabs--${appearance}` : null, className)}
      ref={ref}
      containerRole="tablist"
      itemRole="tab"
      selectedAttr="aria-selected"
      onSelectValue={ctx.select}
    />
  );
});

export interface TabProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "value" | "role"> {
  value: string;
  "data-active"?: string | boolean;
}

export const Tab = forwardRef<HTMLButtonElement, TabProps>(function Tab({ value, onClick, ...rest }, ref) {
  const ctx = useTabsContext("Tab");
  const selected = ctx.value === value;
  return (
    <CompositeOption
      id={tabId(ctx.idBase, value)}
      aria-controls={ctx.panels.has(value) ? tabPanelId(ctx.idBase, value) : undefined}
      {...rest}
      ref={ref}
      value={value}
      role="tab"
      aria-selected={selected}
      onClick={(event: MouseEvent<HTMLButtonElement>) => {
        onClick?.(event);
        if (!event.defaultPrevented) ctx.select(value);
      }}
    />
  );
});

export interface TabPanelProps extends Omit<HTMLAttributes<HTMLDivElement>, "role"> {
  value: string;
  keepMounted?: boolean;
  /** Rendered element, default div (call sites previously using section pass "section"). */
  as?: "div" | "section";
}

export const TabPanel = forwardRef<HTMLDivElement, TabPanelProps>(function TabPanel(
  { value, keepMounted = false, as: Element = "div", children, ...rest },
  ref,
) {
  const ctx = useTabsContext("TabPanel");
  const { registerPanel } = ctx;
  const selected = ctx.value === value;
  const mounted = selected || keepMounted;
  useLayoutEffect(() => {
    if (!mounted) return;
    return registerPanel(value);
    // registerPanel is new every render; reregister only with value and mounted-state changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, mounted]);
  if (!mounted) return null;
  return (
    <Element
      id={tabPanelId(ctx.idBase, value)}
      aria-labelledby={tabId(ctx.idBase, value)}
      {...rest}
      ref={ref}
      role="tabpanel"
      hidden={selected ? rest.hidden : true}
    >
      {children}
    </Element>
  );
});
