"use client";

import {
  autoUpdate,
  flip,
  FloatingFocusManager,
  FloatingList,
  FloatingNode,
  FloatingOverlay,
  FloatingPortal,
  offset as offsetMiddleware,
  safePolygon,
  shift,
  size,
  useClick,
  useDismiss,
  useFloating,
  useFloatingNodeId,
  useHover,
  useInteractions,
  useListItem,
  useListNavigation,
  useMergeRefs,
  useRole,
  useTypeahead,
  type Middleware,
  type Placement,
} from "@floating-ui/react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type HTMLProps,
  type KeyboardEvent,
  type MouseEvent,
  type MutableRefObject,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";

import { cycleTabWithin } from "@/components/ui/dialog";
import {
  NARROW_SHEET_MEDIA_QUERY,
  SHEET_DISMISS_DISTANCE,
  SHEET_DISMISS_VELOCITY,
  useNarrowViewport,
} from "@/components/ui/sheet";
import { lockBodyScroll } from "@/lib/body-scroll-lock";
import { liftedZIndex } from "@/lib/layer";

/**
 * Floating-menu behavior primitive (component unification step 4, docs/design/组件统一-技术设计.md section Menu).
 *
 * Behavior only: caller renders trigger, menu container and items with its own classes
 * (unchanged .app-floating-menu visual contract). Usage:
 *
 * ```tsx
 * const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start" });
 * <button ref={menu.refs.setReference} {...menu.getReferenceProps()}>…</button>
 * <MenuFloating menu={menu}>
 *   <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ className: "app-floating-menu" })}>
 *     <Item />  // Inside component, spread `const props = useMenuItem({ onSelect })` onto <button>/<a>
 *   </div>
 * </MenuFloating>
 * ```
 */

export type MenuRole = "menu" | "listbox";
export type MenuItemRole = "menuitem" | "menuitemradio" | "menuitemcheckbox" | "option";

export interface MenuMiddlewareOptions {
  /** Gap between menu and trigger. */
  offset?: number;
  /** Minimum distance from viewport edges. */
  padding?: number;
  /** Constrain maximum height/width to available space and allow vertical scrolling. Enabled by default. */
  fitViewport?: boolean;
  /** Maximum height cap (px), even with more space available; only effective with fitViewport. */
  maxHeight?: number;
  /** Maximum width cap (px), likewise. Content-sized menus use this instead of inline max-width (size overrides inline values). */
  maxWidth?: number;
}

/**
 * Generic placement: offset → flip → shift (alignment axis) → size → shift (both axes).
 *
 * - flip fallbackAxisSideDirection: "start": if neither preferred nor opposite side fits, try perpendicular sides
 *   (horizontal menus try above/below). Account menu in mobile drawer fits neither right nor left, so this opens
 *   upward (UI redesign P0-5). crossAxis: false checks only placement-axis fit; alignment-axis
 *   deficits belong to shift — desktop sidebar identity row touches viewport bottom, otherwise a 6px deficit flips it upward.
 * - shift corrects both axes (crossAxis): trigger may already lie outside visible area, e.g. mobile
 *   keyboard pushes composer triggers behind it (real-device lesson from use-popover-menu);
 *   clamp menu within visible area instead of following its anchor off-screen.
 * - Upward placement uses actual menu height, bottom against trigger; calculating top from maximum height
 *   leaves shorter menus floating (116: plan menu 184px high, cap 320, gap 142px).
 * - Boundary uses floating-ui default rootBoundary: "viewport": on supported browsers this is
 *   visual viewport (visualViewport.width/height, plus offsetLeft/offsetTop for fixed
 *   positioning). autoUpdate also watches visualViewport resize/scroll,
 *   reclamping on keyboard appearance / pinch zoom without separate measurements.
 */
export function menuMiddleware({
  offset = 6,
  padding = 8,
  fitViewport = true,
  maxHeight,
  maxWidth,
}: MenuMiddlewareOptions = {}): Middleware[] {
  const middleware: Middleware[] = [
    offsetMiddleware(offset),
    flip({ padding, crossAxis: false, fallbackAxisSideDirection: "start" }),
  ];
  if (fitViewport) {
    // Before size, shift only along alignment axis: size sees that axis can move and measures the full viewport's available
    // width (dropdowns) or height (horizontal menus), instead of shrinking by trigger-to-edge distance;
    // dropdowns near the right edge should move left, not narrow (116 space switcher).
    // Placement-axis space still comes from the selected side (dropdowns do not cover trigger); placing final two-axis
    // shift before size makes floating-ui count the whole viewport as placement-axis space, so
    // rename it and place last, invisible to size.
    middleware.push(shift({ padding }));
    middleware.push(
      size({
        padding,
        apply({ availableWidth, availableHeight, elements }) {
          const width = `${Math.max(0, Math.floor(maxWidth === undefined ? availableWidth : Math.min(maxWidth, availableWidth)))}px`;
          Object.assign(elements.floating.style, {
            maxWidth: width,
            maxHeight: `${Math.max(0, Math.floor(maxHeight === undefined ? availableHeight : Math.min(maxHeight, availableHeight)))}px`,
            overflowY: "auto",
          });
          // The menu minimum width (220, styles/ui-refresh.css) yields to this: CSS min-width overrides inline max-width, overflowing narrow viewports.
          // Legacy appearance has no rule consuming this variable.
          elements.floating.style.setProperty("--menu-available-width", width);
        },
      }),
    );
  }
  const clamp = shift({ padding, crossAxis: true });
  middleware.push(fitViewport ? { ...clamp, name: "shiftBothAxes" } : clamp);
  return middleware;
}

/**
 * Viewport where narrow menus may become sheets (Narrow Spec: < 640 is phone, bottom sheets/list rows/top Toasts).
 * Viewports ≥ 640 always use popovers.
 */
export const MENU_SHEET_MEDIA_QUERY = NARROW_SHEET_MEDIA_QUERY;
/** Decided tradeoff 8: >6 items, descriptions or submenus become bottom sheets on narrow screens; short action menus remain popovers. */
export const MENU_SHEET_MAX_ITEMS = 6;
/**
 * Mark item descriptions (second line of two-line items) with this attribute for threshold detection.
 * Disabled reasons do not count as descriptions.
 */
export const MENU_DESCRIPTION_ATTR = "data-menu-description";
/** Submenu hover-opening delay (Menus Spec §2). */
export const SUBMENU_HOVER_DELAY_MS = 150;
/**
 * Submenu overlaps parent menu by 4px horizontally (Menus Spec §2). Anchor is parent item, inset 6px on both sides
 * within parent menu (menu container padding), so relative gap is 6 − 4 = 2.
 */
export const SUBMENU_OFFSET = 2;
/** Bottom-sheet downward-dismissal threshold (shared with dialogs, defined in ./sheet). */
export { SHEET_DISMISS_DISTANCE, SHEET_DISMISS_VELOCITY };

const MENU_ITEM_SELECTOR = '[role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], [role="option"]';

/**
 * Narrow threshold (tradeoff 8): true for >6 items, any description (data-menu-description), or submenu
 * (aria-haspopup="menu" item). Count only items in this menu container; submenu items live in separate containers and do not count.
 */
export function menuNeedsSheet(container: Element): boolean {
  const items = Array.from(container.querySelectorAll(MENU_ITEM_SELECTOR));
  if (items.length > MENU_SHEET_MAX_ITEMS) return true;
  if (container.querySelector(`[${MENU_DESCRIPTION_ATTR}]`)) return true;
  return items.some((item) => {
    const popup = item.getAttribute("aria-haspopup");
    return popup === "menu" || popup === "true";
  });
}

/** Dangerous items that confirm first get … (Menus Spec §5; same ellipsis character in Chinese/English). */
export function withConfirmEllipsis(label: string): string {
  if (label.endsWith("…")) return label;
  return `${label}…`;
}

/**
 * - popover: floating menu (desktop, or short narrow menu below threshold).
 * - sheet: narrow bottom sheet (viewport < 640, threshold met or caller-specified).
 * - drill: submenu drilled into the same bottom sheet.
 */
export type MenuPresentation = "popover" | "sheet" | "drill";
/** Caller override for narrow presentation: auto (default) follows threshold; only effective with viewport < 640. */
export type MenuPresentationOption = "auto" | "popover" | "sheet";

export interface UseMenuOptions extends MenuMiddlewareOptions {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  placement?: Placement;
  /** "menu" (default): menuitem / menuitemradio / menuitemcheckbox; "listbox": option. */
  role?: MenuRole;
  /** Close when trigger ancestors scroll. Off by default. */
  closeOnScroll?: boolean;
  /**
   * Close when entire trigger leaves visible area (viewport / clipping scroll container). Off by default.
   * For table/list row ⋯ menus: closeOnScroll stays off (focus scrolls near-edge triggers slightly,
   * immediately closing newly opened menus). Positioning clamps menu inside viewport, so after row scrolls away,
   * menu would remain orphaned at the viewport edge. Close only when fully invisible; tiny focus scrolls keep it open.
   */
  closeWhenAnchorHidden?: boolean;
  /** Arrow navigation wraps endpoints. On by default. */
  loop?: boolean;
  /**
   * Selected index (selection menu). Mouse/keyboard opening focuses it instead of first item / menu container.
   */
  selectedIndex?: number | null;
  /**
   * Focus-restoration fallback after closure. If closing/unmounting menu leaves focus on body and trigger
   * is unmounted/disabled, focus the returned element. No fallback for outside-click closure.
   * Example: composer menu disabled while sending restores focus to input.
   */
  returnFocusFallback?: () => HTMLElement | null | undefined;
  /**
   * Narrow presentation (behavior batch A). With viewport < 640, auto (default) uses threshold (>6 items / descriptions /
   * submenus) to select a bottom sheet; popover / sheet explicitly override. All other cases are popovers.
   */
  presentation?: MenuPresentationOption;
  /** Optional bottom-sheet title (omit when menu already has identity header / title). */
  sheetTitle?: string;
}

interface InternalMenuOptions extends UseMenuOptions {
  /** Submenu: parent MenuApi supplied by MenuSubmenu. */
  parent?: MenuApi | null;
}

type AnyProps = Record<string, unknown>;

export interface MenuApi {
  open: boolean;
  role: MenuRole;
  nodeId: string | undefined;
  refs: ReturnType<typeof useFloating>["refs"];
  context: ReturnType<typeof useFloating>["context"];
  floatingStyles: CSSProperties;
  activeIndex: number | null;
  getReferenceProps: (userProps?: HTMLProps<Element>) => AnyProps;
  /** Merge placement styles and make container focusable (tabIndex=-1, container receives mouse-open focus). */
  getFloatingProps: (userProps?: HTMLProps<HTMLElement>) => AnyProps;
  getItemProps: (userProps?: HTMLProps<HTMLElement>) => AnyProps;
  /** Close after selection: restore trigger focus synchronously before closing. See useMenuItem. Submenu selection closes ancestors too. */
  closeAfterSelect: () => void;
  elementsRef: MutableRefObject<Array<HTMLElement | null>>;
  labelsRef: MutableRefObject<Array<string | null>>;
  returnFocusRef: MutableRefObject<HTMLElement | null>;
  /** Current presentation, see MenuPresentation. */
  presentation: MenuPresentation;
  presentationOption: MenuPresentationOption;
  /** Narrow top-level menu: MenuFloating wraps a sheet host (display: contents in popover mode). */
  sheetCapable: boolean;
  sheetTitle?: string;
  /** Threshold met: use bottom sheet for this opening (reset after close). */
  requestSheet: () => void;
  parent: MenuApi | null;
  /** Submenu currently drilled into bottom sheet. */
  drilled: boolean;
  setDrilled: (drilled: boolean) => void;
  /** Mount node for bottom-sheet drilled submenu. */
  sheetSlot: HTMLElement | null;
  setSheetSlot: (element: HTMLElement | null) => void;
  sheetHostRef: MutableRefObject<HTMLElement | null>;
  /** Close with specified reason (swipe dismissal / drill-back). */
  dismiss: (event?: Event, reason?: string) => void;
  /** Menu layer (--z-menu, or raised above trigger's root layer). */
  zIndex: number | string;
}

export function useMenu(options: UseMenuOptions): MenuApi {
  return useMenuInternal(options);
}

function useMenuInternal({
  open,
  onOpenChange,
  placement = "bottom-start",
  role = "menu",
  closeOnScroll = false,
  closeWhenAnchorHidden = false,
  loop = true,
  offset,
  padding,
  fitViewport,
  maxHeight,
  maxWidth,
  selectedIndex,
  returnFocusFallback,
  presentation: presentationOption = "auto",
  sheetTitle,
  parent = null,
}: InternalMenuOptions): MenuApi {
  const nodeId = useFloatingNodeId();
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const elementsRef = useRef<Array<HTMLElement | null>>([]);
  const labelsRef = useRef<Array<string | null>>([]);
  // FloatingFocusManager return-focus target, normally trigger; cleared on selection,
  // see closeAfterSelect.
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  const returnFocusFallbackRef = useRef(returnFocusFallback);
  returnFocusFallbackRef.current = returnFocusFallback;
  const closeReasonRef = useRef<string | undefined>(undefined);

  // Behavior batch A: narrow top-level menus may become sheets; submenus follow parent (drill inside sheet).
  const narrow = useNarrowViewport(!parent);
  const sheetCapable = narrow && !parent;
  const [autoSheet, setAutoSheet] = useState(false);
  const [drilled, setDrilled] = useState(false);
  const [sheetSlot, setSheetSlot] = useState<HTMLElement | null>(null);
  const sheetHostRef = useRef<HTMLElement | null>(null);
  const presentation: MenuPresentation = parent
    ? parent.presentation === "sheet"
      ? "drill"
      : "popover"
    : // Only presentation=auto lets host determine autoSheet (MenuFloating autoDetect).
      sheetCapable && (presentationOption === "sheet" || autoSheet)
      ? "sheet"
      : "popover";
  useLayoutEffect(() => {
    if (open) return;
    setAutoSheet(false);
    setDrilled(false);
  }, [open]);
  const requestSheet = useCallback(() => setAutoSheet(true), []);

  const middleware = useMemo(
    () =>
      menuMiddleware({
        offset,
        padding,
        // Drilled submenus render in place inside sheet, without positioning.
        fitViewport: presentation === "drill" ? false : fitViewport,
        maxHeight,
        maxWidth,
      }),
    [offset, padding, fitViewport, maxHeight, maxWidth, presentation],
  );
  const { refs, floatingStyles, context, elements } = useFloating({
    nodeId,
    open,
    onOpenChange: (next, _event, reason) => {
      if (!next) closeReasonRef.current = reason;
      onOpenChangeRef.current(next);
    },
    placement,
    strategy: "fixed",
    whileElementsMounted: autoUpdate,
    middleware,
  });

  useLayoutEffect(() => {
    if (!open) return;
    const reference = refs.domReference.current;
    returnFocusRef.current = reference instanceof HTMLElement ? reference : null;
  }, [open, refs]);

  // Viewport-root IntersectionObserver accounts for ancestor clipping; scrolling outside table container also means invisible.
  const anchor = elements.domReference;
  useEffect(() => {
    if (!closeWhenAnchorHidden || !open || !(anchor instanceof Element) || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => !entry.isIntersecting)) onOpenChangeRef.current(false);
    });
    observer.observe(anchor);
    return () => observer.disconnect();
  }, [closeWhenAnchorHidden, open, anchor]);

  // Focus fallback: FloatingFocusManager restores only to trigger; disabled/unmounted triggers leave focus on body.
  // Wait for its return-focus microtask, then take over only if focus actually has no target.
  useLayoutEffect(() => {
    if (!open) return;
    closeReasonRef.current = undefined;
    return () => {
      if (!returnFocusFallbackRef.current || closeReasonRef.current === "outside-press") return;
      const reference = refs.domReference.current;
      requestAnimationFrame(() => {
        const active = document.activeElement;
        if (active && active !== document.body && active.isConnected) return;
        if (reference instanceof HTMLElement && reference.isConnected && !reference.matches(":disabled")) {
          reference.focus({ preventScroll: true });
          return;
        }
        returnFocusFallbackRef.current?.()?.focus({ preventScroll: true });
      });
    };
  }, [open, refs]);

  // Layers: default --z-menu is above all dialog layers. If trigger's root layer is higher (custom overlay with larger hard-coded
  // z-index), raise one layer above it. See design-document section 图层.
  const [liftZ, setLiftZ] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (!open) {
      setLiftZ(null);
      return;
    }
    if (!elements.floating) return;
    const floating = elements.floating;
    const reference = elements.domReference;
    // Do not recalculate raised layers: current raised value appears sufficient and would revert to default layer.
    setLiftZ((prev) => prev ?? liftedZIndex(floating, [reference instanceof Element ? reference : null]));
  }, [open, elements.floating, elements.domReference]);

  const parentHostRef = parent?.sheetHostRef;
  const click = useClick(context, { enabled: !parent });
  // Submenus: open after 150ms hover; safe triangle prevents closure when moving diagonally. Narrow drill-down does not use hover.
  const hover = useHover(context, {
    enabled: Boolean(parent) && presentation === "popover",
    delay: { open: SUBMENU_HOVER_DELAY_MS },
    mouseOnly: true,
    handleClose: safePolygon({ blockPointerEvents: true }),
  });
  const dismiss = useDismiss(context, {
    ancestorScroll: closeOnScroll,
    // Sheet handle/title lie outside menu container; pointer-down there (starting swipe dismissal) is not an outside click.
    outsidePress: (event) => {
      const host = (parentHostRef ?? sheetHostRef).current;
      const target = event.target;
      return !(host && target instanceof Node && host.contains(target));
    },
  });
  const roleProps = useRole(context, { role });
  const listNavigation = useListNavigation(context, {
    listRef: elementsRef,
    activeIndex,
    onNavigate: setActiveIndex,
    loop,
    selectedIndex,
    nested: Boolean(parent),
    parentOrientation: parent ? "vertical" : undefined,
    // Keyboard may highlight disabled items (Menus Spec §5, to read reasons); Enter does not execute (useMenuItem).
    // Native disabled buttons cannot focus and remain skipped, and so do empty list slots (e.g. items filtered out
    // of a searchable menu), matching floating-ui's default.
    disabledIndices: (index: number) => {
      const element = elementsRef.current[index];
      return element == null || element.matches(":disabled");
    },
  });
  const typeahead = useTypeahead(context, {
    listRef: labelsRef,
    activeIndex,
    onMatch: open ? setActiveIndex : undefined,
  });
  const interactions = useInteractions([click, hover, dismiss, roleProps, listNavigation, typeahead]);

  const closeAfterSelect = useCallback(() => {
    // Ordering is contractual: restore trigger focus synchronously, then close. Dialogs/drawers opened by caller onSelect
    // record the trigger as pre-open focus, restoring here on close instead of
    // targeting an unmounted menu item and ultimately falling to body.
    // Also clear FloatingFocusManager's return-focus target: its post-unmount microtask runs
    // after a new dialog may already focus its panel, so restoring again would steal focus back to the trigger.
    const reference = refs.domReference.current;
    if (reference instanceof HTMLElement && reference.isConnected) {
      reference.focus({ preventScroll: true });
    }
    returnFocusRef.current = null;
    onOpenChangeRef.current(false);
    // Submenu selection closes the entire menu and restores the outermost trigger's focus.
    parent?.closeAfterSelect();
  }, [refs, parent]);

  const dismissMenu = useCallback(
    (event?: Event, reason?: string) => {
      context.onOpenChange(false, event, reason as never);
    },
    [context],
  );

  // Next App Router mounts React root on document; synthetic stopPropagation cannot block later native listeners also on
  // document, including ancestor DialogShell useDismiss. By its execution, menu has already unmounted in a microtask,
  // so it sees no open submenu and closes dialog too (real browser: Esc with trigger focus closed
  // account menu and sidebar popover together). Stop menu-consumed Esc here while open. Portal events
  // are handled by React on portal container and never reach document; handle consistently here too.
  const consumeEscape = useCallback(
    (event: KeyboardEvent<Element>) => {
      if (event.key === "Escape" && open) event.nativeEvent.stopImmediatePropagation();
    },
    [open],
  );

  const getReferenceProps = useCallback(
    (userProps?: HTMLProps<Element>) => {
      const props = interactions.getReferenceProps({
        ...userProps,
        onKeyDown: (event: KeyboardEvent<Element>) => {
          userProps?.onKeyDown?.(event);
          consumeEscape(event);
        },
      });
      // useRole treats any tree parent as a submenu, setting trigger role="menuitem". Parent here
      // may instead be a DialogShell FloatingNode; trigger remains a normal button. Actual submenu triggers
      // (MenuSubmenu) explicitly supply role="menuitem".
      if (props.role === "menuitem" && userProps?.role === undefined) delete props.role;
      return props;
    },
    [interactions, consumeEscape],
  );

  const zIndex = liftZ ?? "var(--z-menu)";
  const getFloatingProps = useCallback(
    (userProps?: HTMLProps<HTMLElement>) => {
      const { style, className, ...rest } = userProps ?? {};
      // Host positions bottom sheets / drill-down (CSS in ui-refresh.css section 行为批 A); omit floating-ui placement styles.
      const placed = presentation === "popover";
      const modifier = presentation === "sheet" ? " is-sheet" : presentation === "drill" ? " is-drill" : "";
      return interactions.getFloatingProps({
        tabIndex: -1,
        ...rest,
        // Appearance batch three unifies menu appearance through .ui-menu.
        className: className ? `ui-menu${modifier} ${className}` : `ui-menu${modifier}`,
        onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
          rest.onKeyDown?.(event);
          consumeEscape(event);
          // Submenus belong to parent React subtree; stop key bubbling to parent menu (otherwise arrows move both highlights,
          // and Esc closes parent too).
          if (parent) event.stopPropagation();
        },
        style: placed ? { zIndex, ...floatingStyles, ...style } : { ...style },
      });
    },
    [interactions, floatingStyles, consumeEscape, zIndex, presentation, parent],
  );

  return {
    open,
    role,
    nodeId,
    refs,
    context,
    floatingStyles,
    activeIndex,
    getReferenceProps,
    getFloatingProps,
    getItemProps: interactions.getItemProps,
    closeAfterSelect,
    elementsRef,
    labelsRef,
    returnFocusRef,
    presentation,
    presentationOption,
    sheetCapable,
    sheetTitle,
    requestSheet,
    parent,
    drilled,
    setDrilled,
    sheetSlot,
    setSheetSlot,
    sheetHostRef,
    dismiss: dismissMenu,
    zIndex,
  };
}

interface MenuContextValue {
  menu: MenuApi;
}

const MenuContext = createContext<MenuContextValue | null>(null);

/**
 * Floating-menu shell: FloatingNode (joins FloatingTree, nested Esc closes only top layer, ancestor-dialog
 * outside-click checks treat menu as part of themselves) → FloatingPortal → non-modal FloatingFocusManager.
 * Render children only while open.
 *
 * root: portal container, default document.body. Only for hosts whose inside/outside checks use
 * membership in a specific element; report selection toolbar use-report-overlay-manager checks its toolbar node,
 * so body-mounted menus look like outside clicks, closing toolbar before selection. null waits for container
 * before mounting. Custom-container menus do not automatically become narrow sheets (container transforms may displace fixed positioning).
 *
 * Narrow viewports (behavior batch A): add sheet host. At threshold, use bottom sheet (backdrop, handle, swipe dismissal,
 * row height 44, inset rounded grouped lists); otherwise host display: contents retains popover. Focus management unchanged (non-modal,
 * Esc closes and returns to trigger). Wide screens render no host.
 */
export function MenuFloating({
  menu,
  children,
  root,
}: {
  menu: MenuApi;
  children: ReactNode;
  root?: HTMLElement | null;
}) {
  const value = useMemo(() => ({ menu }), [menu]);
  const list = (
    <MenuContext.Provider value={value}>
      <FloatingList elementsRef={menu.elementsRef} labelsRef={menu.labelsRef}>
        {children}
      </FloatingList>
    </MenuContext.Provider>
  );
  const portalRoot = menu.presentation === "drill" ? (menu.parent?.sheetSlot ?? null) : root;
  // Bottom sheets are modal like DialogShell: focus stays inside, background inert; backdrop and host (handle/title/
  // drill mount) count as inside, otherwise inert makes backdrop unclickable / handle undraggable. Popovers remain non-modal (Tab out closes).
  const sheet = menu.sheetCapable && menu.presentation === "sheet";
  const scrimRef = useRef<HTMLDivElement | null>(null);
  const { sheetHostRef } = menu;
  const getInsideElements = useCallback(
    () => [sheetHostRef.current, scrimRef.current].filter((element): element is HTMLElement => Boolean(element)),
    [sheetHostRef],
  );
  return (
    <FloatingNode id={menu.nodeId}>
      {menu.open ? (
        <FloatingPortal root={portalRoot}>
          {sheet ? (
            // Modal useDismiss ignores backdrop (non-inert sibling inside portal) as third-party node; backdrop closes itself.
            <FloatingOverlay
              ref={scrimRef}
              className="ui-menu-sheet-scrim"
              style={{ zIndex: menu.zIndex }}
              onClick={(event) => menu.dismiss(event.nativeEvent, "outside-press")}
            />
          ) : null}
          <FloatingFocusManager
            context={menu.context}
            modal={sheet}
            initialFocus={0}
            returnFocus={menu.returnFocusRef}
            {...(sheet ? { guards: false, outsideElementsInert: true, getInsideElements } : null)}
          >
            {menu.sheetCapable ? (
              <MenuSheetHost menu={menu} autoDetect={menu.presentationOption === "auto" && root === undefined}>
                {list}
              </MenuSheetHost>
            ) : (
              list
            )}
          </FloatingFocusManager>
        </FloatingPortal>
      ) : null}
    </FloatingNode>
  );
}

/**
 * Narrow sheet host. Popovers use display: contents (no box, floating-ui still positions menu);
 * sheets attach to bottom with handle (swipe dismissal), optional title, menu and drilled-submenu mount.
 * Subtree positions stay unchanged between presentations; menu items do not remount.
 */
function MenuSheetHost({ menu, autoDetect, children }: { menu: MenuApi; autoDetect: boolean; children: ReactNode }) {
  const sheet = menu.presentation === "sheet";
  const [dragY, setDragY] = useState(0);
  const dragRef = useRef<{ startY: number; startTime: number; pointerId: number } | null>(null);
  const { requestSheet, refs, sheetHostRef, setSheetSlot, dismiss } = menu;

  // Threshold inspection: items already mounted (child layout effects run first). Switch eligible menus to sheets before paint,
  // avoiding popover flash. Within one opening, only popover → sheet (async extra items), never reverse.
  useLayoutEffect(() => {
    if (!autoDetect || sheet) return;
    const floating = refs.floating.current;
    if (floating && menuNeedsSheet(floating)) requestSheet();
  });

  const onPointerDown = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    dragRef.current = { startY: event.clientY, startTime: event.timeStamp, pointerId: event.pointerId };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setDragY(Math.max(0, event.clientY - drag.startY));
  };
  const onPointerUp = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    const distance = Math.max(0, event.clientY - drag.startY);
    const elapsed = Math.max(1, event.timeStamp - drag.startTime);
    if (distance >= SHEET_DISMISS_DISTANCE || (distance > 16 && distance / elapsed > SHEET_DISMISS_VELOCITY)) {
      dismiss(event.nativeEvent, "swipe");
      return;
    }
    setDragY(0);
  };
  const onPointerCancel = () => {
    dragRef.current = null;
    setDragY(0);
  };
  const dragHandlers = sheet ? { onPointerDown, onPointerMove, onPointerUp, onPointerCancel } : null;

  // Lock page scrolling while sheet is open (shared counted lock preserves enclosing mobile drawer's lock when sheet closes).
  // Use drawer-class lock (!important, no compensation): narrow globals.css enables body scrolling with !important,
  // which non-important dialog locks cannot override on phones.
  useLayoutEffect(() => {
    if (!sheet) return;
    return lockBodyScroll();
  }, [sheet]);

  // Modal Tab wraps endpoints using DialogShell's implementation (including WebKit fix). Capture phase is needed because
  // drilled submenus stop key bubbling.
  const onKeyDownCapture = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!sheet || event.key !== "Tab") return;
    const host = sheetHostRef.current;
    if (host) cycleTabWithin(host, event);
  };

  return (
    <div
      ref={(element) => {
        sheetHostRef.current = element;
      }}
      className={sheet ? "ui-menu-sheet" : "ui-menu-sheet-host"}
      data-drilled={sheet && menu.drilled ? "true" : undefined}
      tabIndex={sheet ? -1 : undefined}
      onKeyDownCapture={sheet ? onKeyDownCapture : undefined}
      style={
        sheet
          ? {
              zIndex: menu.zIndex,
              transform: dragY ? `translateY(${dragY}px)` : undefined,
              transition: dragY ? "none" : undefined,
            }
          : undefined
      }
    >
      {sheet ? <div className="ui-menu-sheet-grab" aria-hidden="true" data-sheet-grab="" {...dragHandlers} /> : null}
      {sheet && menu.sheetTitle ? (
        <div className="ui-menu-sheet-title" aria-hidden="true" {...dragHandlers}>
          {menu.sheetTitle}
        </div>
      ) : null}
      {children}
      {sheet ? <div ref={setSheetSlot} className="ui-menu-sheet-drill" /> : null}
    </div>
  );
}

export interface UseMenuItemOptions {
  /** Execute on selection. Focus already restored to trigger and closure requested beforehand (except closeOnSelect=false). */
  onSelect?: (event: MouseEvent<HTMLElement>) => void;
  disabled?: boolean;
  /** Typeahead text, default element textContent. */
  label?: string;
  role?: MenuItemRole;
  /** aria-checked for menuitemradio / menuitemcheckbox; aria-selected for listbox. */
  checked?: boolean;
  /** Whether selection closes menu; yes by default. Pass false for items showing ongoing state (e.g. logout). */
  closeOnSelect?: boolean;
}

function isNativelyActivatable(element: HTMLElement): boolean {
  return element instanceof HTMLButtonElement || (element instanceof HTMLAnchorElement && element.hasAttribute("href"));
}

/**
 * Menu-item behavior: register list (arrows, Home/End, typeahead), roving tabIndex, role /
 * aria-checked and selection ordering. Spread returned props directly onto button / anchor.
 *
 * Disabled items use aria-disabled; clicks / Enter do not execute. Keyboard highlighting still reaches them (read reasons), requiring
 * callers to omit native disabled (native disabled buttons cannot focus). Render reasons through MenuItemReason.
 */
export function useMenuItem({
  onSelect,
  disabled = false,
  label,
  role,
  checked,
  closeOnSelect = true,
}: UseMenuItemOptions = {}) {
  const ctx = useContext(MenuContext);
  if (!ctx) throw new Error("useMenuItem must be used inside <MenuFloating>");
  const { menu } = ctx;
  const item = useListItem({ label: disabled ? null : label });
  const itemRole: MenuItemRole = role ?? (menu.role === "listbox" ? "option" : "menuitem");
  const checkable = itemRole === "menuitemradio" || itemRole === "menuitemcheckbox";

  const onClick = (event: MouseEvent<HTMLElement>) => {
    if (disabled) {
      event.preventDefault();
      return;
    }
    if (closeOnSelect) menu.closeAfterSelect();
    onSelect?.(event);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (isNativelyActivatable(event.currentTarget)) return;
    event.preventDefault();
    event.currentTarget.click();
  };

  return {
    ref: item.ref,
    role: itemRole,
    tabIndex: menu.activeIndex === item.index ? 0 : -1,
    "aria-disabled": disabled || undefined,
    ...(checkable ? { "aria-checked": Boolean(checked) } : null),
    ...(itemRole === "option" ? { "aria-selected": Boolean(checked) } : null),
    ...menu.getItemProps({ onClick, onKeyDown }),
  };
}

/**
 * Disabled reason: persistent second-line small text (tradeoff 9), announced via item aria-describedby.
 * Usage: const reasonId = useId(); set item aria-describedby={reason ? reasonId : undefined},
 * then render <MenuItemReason id={reasonId}>{reason}</MenuItemReason> immediately after.
 */
export function MenuItemReason({ id, children }: { id: string; children: ReactNode }) {
  return (
    <span id={id} className="ui-menu-reason">
      {children}
    </span>
  );
}

/**
 * Keyboard-hint strip below autocomplete lists / space switcher (Menus Spec §4): ↑↓ 选择 · ↵ 确认 · esc 关闭.
 * CSS shows it only for desktop pointers (hover: hover and pointer: fine).
 * Visual hint only, hidden from screen readers (list roles already convey keyboard behavior).
 */
export function MenuKeyHints({ hints }: { hints: Array<{ keys: string; label: string }> }) {
  return (
    <div className="ui-menu-hints" aria-hidden="true">
      {hints.map((hint) => (
        <span key={hint.keys}>
          <kbd>{hint.keys}</kbd>
          {hint.label}
        </span>
      ))}
    </div>
  );
}

export interface MenuSubmenuProps {
  /** Parent label, also submenu accessible name / drill-down title. */
  label: string;
  /** Parent accessible name (defaults to label). */
  ariaLabel?: string;
  /** Leading icon (aligned with other items' icon column). */
  icon?: ReactNode;
  /** Trailing current value (Menus Spec §3: Language shows current language), followed by ›. */
  value?: ReactNode;
  /** Parent class (same as other items in menu). */
  className?: string;
  /** Trailing-value class. */
  valueClassName?: string;
  /** Desktop submenu container class. */
  menuClassName?: string;
  /** Parent name on "‹ 返回" when drilled in; defaults to parent menu aria-label. */
  backLabel?: string;
  /** Selected submenu item (receives opening focus). */
  selectedIndex?: number | null;
  /** Submenu items: components using useMenuItem, same as normal menus. */
  children: ReactNode;
}

/**
 * Submenu (behavior batch A, Menus Spec §2), an item inside MenuFloating:
 *
 * - Desktop: trailing ›; open by 150ms hover / → / Enter / click; ← / Esc returns to parent. Top-aligned with parent,
 *   4px horizontal overlap, opens left if right cannot fit; safe triangle prevents closing while pointer moves diagonally. Submenu selection
 *   closes entire menu and restores outermost trigger focus.
 * - Narrow bottom sheet: clicking parent drills into same sheet; top ‹ Parent returns (← / Esc also returns one level).
 */
export function MenuSubmenu({
  label,
  ariaLabel,
  icon,
  value,
  className,
  valueClassName,
  menuClassName,
  backLabel,
  selectedIndex,
  children,
}: MenuSubmenuProps) {
  const ctx = useContext(MenuContext);
  if (!ctx) throw new Error("MenuSubmenu must be used inside <MenuFloating>");
  const parent = ctx.menu;
  const t = useTranslations("common");
  const [open, setOpen] = useState(false);
  const setParentDrilled = parent.setDrilled;
  const parentIsSheet = parent.presentation === "sheet";
  const parentFloating = parent.refs.floating;
  // Drill-back label uses parent menu name, read from its container on opening (DOM complete by then).
  const [parentLabel, setParentLabel] = useState<string | undefined>(undefined);
  const changeOpen = useCallback(
    (next: boolean) => {
      if (next) setParentLabel(parentFloating.current?.getAttribute("aria-label") ?? undefined);
      setOpen(next);
      setParentDrilled(next && parentIsSheet);
    },
    [setParentDrilled, parentIsSheet, parentFloating],
  );
  const sub = useMenuInternal({
    open,
    onOpenChange: changeOpen,
    placement: "right-start",
    offset: SUBMENU_OFFSET,
    selectedIndex,
    parent,
  });
  const { ref: itemRef, ...itemProps } = useMenuItem({ onSelect: () => changeOpen(true), closeOnSelect: false, label });
  const ref = useMergeRefs([itemRef, sub.refs.setReference]);
  const referenceProps = sub.getReferenceProps({ ...(itemProps as HTMLProps<Element>), role: "menuitem" });

  const drill = sub.presentation === "drill";
  const titleId = useId();

  return (
    <>
      <button type="button" ref={ref} className={className} aria-label={ariaLabel} {...referenceProps}>
        {icon}
        <span>{label}</span>
        <span className={`ui-menu-item-tail${valueClassName ? ` ${valueClassName}` : ""}`}>
          {value}
          <span className="ui-menu-chevron" aria-hidden="true" />
        </span>
      </button>
      <MenuFloating menu={sub}>
        <div
          ref={sub.refs.setFloating}
          {...sub.getFloatingProps({
            className: drill ? undefined : menuClassName,
            "aria-label": drill ? undefined : label,
            "aria-labelledby": drill ? titleId : undefined,
          })}
        >
          {drill ? (
            <div className="ui-menu-drill-head">
              <button
                type="button"
                tabIndex={-1}
                className="ui-menu-drill-back"
                onClick={(event) => sub.dismiss(event.nativeEvent, "drill-back")}
              >
                <span className="ui-menu-drill-back-icon" aria-hidden="true" />
                {backLabel ?? parentLabel ?? t("back")}
              </button>
              <span id={titleId} className="ui-menu-drill-title">
                {label}
              </span>
            </div>
          ) : null}
          {children}
        </div>
      </MenuFloating>
    </>
  );
}
