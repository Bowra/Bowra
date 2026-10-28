"use client";

import {
  FloatingFocusManager,
  FloatingNode,
  FloatingOverlay,
  FloatingPortal,
  FloatingTree,
  useDismiss,
  useFloating,
  useFloatingNodeId,
  useFloatingParentNodeId,
  useFloatingTree,
  useInteractions,
  useRole,
} from "@floating-ui/react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type HTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { useTranslations } from "next-intl";

import { X } from "@/components/icons";
import { useNarrowViewport, useSheetDrag, useSheetViewport, type SheetDragHandlers } from "@/components/ui/sheet";
import { lockBodyScroll } from "@/lib/body-scroll-lock";
import { liftedZIndex } from "@/lib/layer";

export interface DialogProps {
  title: ReactNode;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  width?: number;
  /** While busy, Esc, backdrop clicks and the close button have no effect. */
  busy?: boolean;
  variant?: "center" | "drawer";
  closeLabel?: string;
  /** "top": keep confirmation above all layers (may open from drawers, other dialogs or editor overlays). */
  layer?: "base" | "top";
  /** See DialogShellProps.returnFocusRef. */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** See DialogShellProps.role / describedBy. Associate confirmation consequences for screen readers. */
  role?: "dialog" | "alertdialog";
  describedBy?: string;
  /** See DialogShellProps.presentation. Defaults to `auto`; drawers (`variant="drawer"`) default to `sheet`. */
  presentation?: DialogPresentationOption;
}

/**
 * Narrow-screen presentation (behavior batch B, viewport < 640):
 * - `auto` (default): inspect content (`dialogNeedsSheet`) — use a bottom sheet for 2 or more fields, lists / tables, or content exceeding one screen;
 *   title, description and at most one field (confirmation / rename) remain centered cards (Narrow Spec section 弹窗).
 * - `sheet`: always a bottom sheet on narrow screens (drawers, multi-step flows).
 * - `center`: retain original presentation (full-screen viewers, panels with mobile layouts, navigation drawers).
 * `overlay="none"` callers position using their own classes, skip inspection and always retain original presentation.
 */
export type DialogPresentationOption = "auto" | "center" | "sheet";

/** Dialog content in bottom sheets reads this: `Dialog` uses a title bar with close button and attaches dragging to it. */
interface DialogSheetContextValue {
  sheet: boolean;
  dragHandlers: SheetDragHandlers | null;
}
const DialogSheetContext = createContext<DialogSheetContextValue>({ sheet: false, dragHandlers: null });

/** Inspection: controls counted as one field. Same-name radios count as one; `role="radiogroup"` counts the whole group as one. */
const FIELD_SELECTOR = [
  "input:not([type='hidden']):not([type='button']):not([type='submit']):not([type='reset']):not([type='image']):not([type='radio'])",
  "textarea",
  "select",
  "[contenteditable='']",
  "[contenteditable='true']",
  "[role='textbox']",
  "[role='combobox']",
  "[role='switch']",
  "[role='spinbutton']",
  "[role='slider']",
].join(",");
const LIST_SELECTOR = "table, [role='table'], [role='grid'], [role='listbox'], [role='tree'], [role='treegrid']";
/** Maximum fields for a centered card (Narrow Spec: confirmations, alerts and single-input rename remain cards). */
export const DIALOG_CARD_MAX_FIELDS = 1;

function isRendered(node: Element, root: Element): boolean {
  if (node.closest("[hidden]") || node.getAttribute("aria-hidden") === "true") return false;
  for (let el: Element | null = node; el && el !== root; el = el.parentElement) {
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
  }
  return true;
}

/**
 * Whether this dialog needs a bottom sheet on narrow screens (Narrow Spec section 弹窗):
 * 2 or more fields, lists / tables, or content exceeding one screen (panel / inner scroller overflows, or panel taller than viewport minus 32).
 * Confirmations / rename with only title, description, ≤ 2 buttons and at most one field remain centered cards.
 */
export function dialogNeedsSheet(panel: HTMLElement, viewportHeight = typeof window === "undefined" ? 0 : window.innerHeight): boolean {
  let fields = 0;
  const radioGroups = new Set<Element | string>();
  for (const node of Array.from(panel.querySelectorAll(`${FIELD_SELECTOR}, input[type='radio'], [role='radiogroup'], [role='radio']`))) {
    if (!isRendered(node, panel)) continue;
    const group = node.matches("[role='radiogroup']") ? node : node.closest("[role='radiogroup']");
    if (group && panel.contains(group)) {
      radioGroups.add(group);
      continue;
    }
    if (node.matches("input[type='radio']")) {
      const name = (node as HTMLInputElement).name;
      radioGroups.add(name ? `name:${name}` : node);
      continue;
    }
    if (node.matches("[role='radio']")) {
      radioGroups.add(node);
      continue;
    }
    fields += 1;
  }
  fields += radioGroups.size;
  if (fields > DIALOG_CARD_MAX_FIELDS) return true;
  if (Array.from(panel.querySelectorAll(LIST_SELECTOR)).some((node) => isRendered(node, panel))) return true;
  // Content exceeds one screen if panel / inner scroller overflows, or panel height exceeds viewport minus 16 at top and bottom.
  if (panel.scrollHeight > panel.clientHeight + 1) return true;
  if (viewportHeight > 0 && panel.getBoundingClientRect().height > viewportHeight - 32) return true;
  for (const node of Array.from(panel.querySelectorAll<HTMLElement>("*"))) {
    if (node.scrollHeight <= node.clientHeight + 1) continue;
    const overflowY = getComputedStyle(node).overflowY;
    if ((overflowY === "auto" || overflowY === "scroll") && isRendered(node, panel)) return true;
  }
  return false;
}

/** Layer values: app/globals.css `--z-*` and design-document section 图层. */
const TOP_LAYER_STYLE: CSSProperties = { zIndex: "var(--z-dialog-top)" };

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "iframe",
  "summary",
  "video[controls]",
  "audio[controls]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export interface DialogShellProps {
  children: ReactNode;
  onClose: () => void;
  /** While busy, Esc and backdrop clicks have no effect. */
  busy?: boolean;
  layer?: "base" | "top";
  role?: "dialog" | "alertdialog";
  /** Accessible panel name: prefer labelledBy (title element ID), otherwise label. */
  labelledBy?: string;
  label?: string;
  /** Description element ID: screen readers should announce confirmation consequences along with the title. */
  describedBy?: string;
  /** Whether backdrop clicks close the dialog; closes by default. */
  closeOnBackdrop?: boolean;
  backdropClassName?: string;
  backdropStyle?: CSSProperties;
  panelClassName?: string;
  panelStyle?: CSSProperties;
  /**
   * "body" (default) mounts to body; "inline" renders in place for dialogs whose styles / CSS variables are scoped under page containers
   * (e.g. `.mset`, `.files-main`). Backdrop remains fixed full-screen and background inert still applies.
   */
  mount?: "body" | "inline";
  /**
   * "fixed" (default) wraps in a fixed full-screen `FloatingOverlay`. "none" omits the wrapper; caller
   * classes position the panel (e.g. absolute relative to the workbench with container-unit width in `.results-quick`, or cards
   * whose pages supply backdrop containers). `useDismiss` outsidePress handles outside-panel clicks (still respecting
   * closeOnBackdrop and busy), without scroll locking; Tab cycling, Esc, background inert and focus restoration remain.
   */
  overlay?: "fixed" | "none";
  /**
   * Restore focus to this element on close instead of the opening `document.activeElement`. For triggers already
   * blurred or unmounted when opening: menu items (menu immediately closes), buttons disabled before opening,
   * or Safari clicks that do not focus buttons.
   */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /**
   * Preserve original data-* / id attributes when migrating custom dialogs; existing tests and e2e locate them.
   * `onKeyDown` runs before the shell stops bubbling, for dialog-owned shortcuts still active while open.
   */
  panelProps?: Omit<HTMLAttributes<HTMLDivElement>, "className" | "style" | "role" | "children">;
  /**
   * Elements outside the panel that must remain non-inert. For legacy sibling backdrops (sidebar popovers, mobile
   * drawers): backdrop remains clickable and its own onClick closes it (pass `closeOnBackdrop={false}`).
   */
  getInsideElements?: () => Element[];
  /**
   * Narrow-screen presentation (behavior batch B): with viewport < 640, `auto` (default) inspects content for a bottom sheet,
   * `sheet` always uses one, `center` never does. Skip `overlay="none"`.
   */
  presentation?: DialogPresentationOption;
}

/** Actually focusable panel elements: exclude aria-hidden, hidden attributes and non-rendered elements (self / ancestor display:none /
 *  visibility:hidden), e.g. upload dialogs' hidden file inputs; otherwise Tab on the last button fails to wrap.
 *  Also exclude tabindex=-1 (outside Tab order, e.g. mouse-only backdrop buttons) and collapsed details content except summary. */
function focusableIn(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((node) => {
    if (node.getAttribute("aria-hidden") === "true" || node.closest("[hidden]")) return false;
    if (node.getAttribute("tabindex") === "-1") return false;
    const closedDetails = node.closest("details:not([open])");
    if (closedDetails && !(node.tagName === "SUMMARY" && node.parentElement === closedDetails)) return false;
    for (let el: HTMLElement | null = node; el && el !== panel; el = el.parentElement) {
      const style = getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") return false;
    }
    return true;
  });
}

/** Focus the first focusable element in the panel, or the panel itself if none. */
export function focusFirstWithin(panel: HTMLElement): void {
  (focusableIn(panel)[0] ?? panel).focus();
}

/**
 * Restrict Tab to the panel: wrap endpoints, leave the middle to the browser. Shared by DialogShell and panels whose nodes
 * must stay in place while acting as dialogs (e.g. full-screen embedded report HTML; moving would reload the iframe).
 */
export function cycleTabWithin(
  panel: HTMLElement,
  event: { shiftKey: boolean; preventDefault: () => void },
): void {
  // Do not use FloatingFocusManager focus guards: real-browser measurements here showed
  // no redirection; focus stopped once on a guard and once on body (2026-09-25).
  const focusables = focusableIn(panel);
  if (focusables.length === 0) {
    event.preventDefault();
    panel.focus();
    return;
  }
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || active === panel)) {
    event.preventDefault();
    last.focus();
    return;
  }
  if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
    return;
  }
  // Leave intermediate Tab navigation to the browser (native iframe / radio-group order). WebKit defaults skip buttons,
  // sending focus straight to body in button-only panels (2026-09-25: composer tool-panel focus escaped 12/12 times);
  // if focus leaves the panel after the keypress, move to the next element in list order.
  const index = focusables.indexOf(active as HTMLElement);
  const step = event.shiftKey ? -1 : 1;
  window.setTimeout(() => {
    if (panel.contains(document.activeElement)) return;
    const next = focusables[(Math.max(index, 0) + step + focusables.length) % focusables.length];
    next?.focus();
  }, 0);
}

/**
 * Modal behavior shell (component unification P1, docs/design/组件统一-技术设计.md): opens on mount,
 * `onClose` requests closure. No visuals; caller classes style backdrop and panel — migrating
 * custom dialogs preserves appearance while completing behavior.
 *
 * Focus: initially prefer `data-dialog-initial-focus` elements (including panel), otherwise the first focusable element;
 * Tab wraps endpoints; closing restores pre-open focus. `FloatingFocusManager` makes the background inert.
 * Esc / backdrop clicks close; each dialog registers as `FloatingNode`, so nested Esc closes only the topmost.
 */
export function DialogShell(props: DialogShellProps) {
  const parentId = useFloatingParentNodeId();
  if (parentId === null) {
    return (
      <FloatingTree>
        <DialogShellNode {...props} />
      </FloatingTree>
    );
  }
  return <DialogShellNode {...props} />;
}

function DialogShellNode({
  children,
  onClose,
  busy = false,
  layer = "base",
  role: roleName = "dialog",
  labelledBy,
  label,
  describedBy,
  closeOnBackdrop = true,
  backdropClassName,
  backdropStyle,
  panelClassName,
  panelStyle,
  panelProps,
  mount = "body",
  overlay = "fixed",
  returnFocusRef,
  getInsideElements,
  presentation = "auto",
}: DialogShellProps) {
  const nodeId = useFloatingNodeId();
  const parentNodeId = useFloatingParentNodeId();
  const tree = useFloatingTree();
  // Pre-open focus (usually trigger button). On first render, focus has not yet entered the dialog.
  const [opener] = useState<Element | null>(() => (typeof document === "undefined" ? null : document.activeElement));
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  // Close once per keypress / click: with inline mounting, React's root is on document, so useDismiss's native Esc
  // listener and panel synthetic onKeyDown would each close (running caller telemetry / leave confirmation twice).
  const closeQueuedRef = useRef(false);
  const requestClose = () => {
    if (busyRef.current || closeQueuedRef.current) return;
    closeQueuedRef.current = true;
    setTimeout(() => { closeQueuedRef.current = false; }, 0);
    onCloseRef.current();
  };
  // Backdrop dismissal requires pointer-down on the backdrop too: pressing in an input then dragging selection onto the backdrop
  // dispatches click to their common ancestor (backdrop); this must not close the dialog and lose entered content.
  const pressedOnBackdropRef = useRef(false);

  const { refs, context } = useFloating({
    nodeId,
    open: true,
    onOpenChange: (open) => {
      if (!open) requestClose();
    },
  });
  const dismiss = useDismiss(context, {
    enabled: !busy,
    // With a backdrop, FloatingOverlay determines clicks; without one, outsidePress does. Nested Esc does not bubble to parent dialogs.
    outsidePress: overlay === "none" && closeOnBackdrop,
    bubbles: { escapeKey: false },
  });
  const role = useRole(context, { role: roleName });
  const { getFloatingProps } = useInteractions([dismiss, role]);

  // FloatingPortal mounts content on the next render; wait for the actual panel node before setting initial focus.
  const [panel, setPanel] = useState<HTMLDivElement | null>(null);
  const [backdrop, setBackdrop] = useState<HTMLDivElement | null>(null);

  // Layers: if a body-mounted dialog is not above its parent overlay or the trigger's root layer (e.g. opened from a KB
  // drawer at z-index 9998 or mobile session drawer at 1200), raise it one layer above. Inline dialogs
  // are constrained by their stacking context, so raising has no effect and is skipped. See design-document section 图层.
  const [liftZ, setLiftZ] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (mount !== "body") return;
    const layerElement = overlay === "none" ? panel : backdrop;
    if (!layerElement) return;
    const parentFloating = parentNodeId
      ? tree?.nodesRef.current.find((node) => node.id === parentNodeId)?.context?.elements.floating
      : null;
    // Do not recalculate raised layers: the current raised value appears sufficient and would cause a return to the default layer.
    setLiftZ((prev) => prev ?? liftedZIndex(layerElement, [parentFloating, returnFocusRef?.current, opener]));
  }, [mount, overlay, panel, backdrop, parentNodeId, tree, returnFocusRef, opener]);
  // Lock page scrolling when a backdrop exists. Use the shared counted lock (lib/body-scroll-lock.ts), not FloatingOverlay's
  // lockScroll: its unlock unconditionally clears body overflow, erasing a simultaneously open mobile drawer's lock.
  useLayoutEffect(() => {
    if (overlay === "none") return;
    return lockBodyScroll({ compensate: true });
  }, [overlay]);
  // Narrow bottom sheet (behavior batch B): read viewport only with a fixed backdrop and no caller-specified center.
  // Always wrap a panel host then (display: contents for wide screens / centered cards); on presentation changes (including rotation),
  // panel / forms do not remount and entered content survives.
  const sheetHost = overlay === "fixed" && presentation !== "center";
  const narrow = useNarrowViewport(sheetHost);
  const [autoSheet, setAutoSheet] = useState(false);
  const sheet = sheetHost && narrow && (presentation === "sheet" || autoSheet);
  const sheetShapeRef = useRef({ sheetHost, narrow, presentation, autoSheet });
  sheetShapeRef.current = { sheetHost, narrow, presentation, autoSheet };
  // Inspect thresholds in layout effect: content mounted but not painted; eligible openings start as sheets without flashing cards.
  // Within one opening, only card → sheet transitions occur (e.g. async fields), never the reverse (`sheet` is already false on wide screens).
  useLayoutEffect(() => {
    if (!sheetHost || !narrow || presentation !== "auto" || autoSheet || !panel) return;
    if (dialogNeedsSheet(panel)) setAutoSheet(true);
  });
  const [sheetElement, setSheetElement] = useState<HTMLDivElement | null>(null);
  const sheetViewport = useSheetViewport(sheet, sheetElement);
  const { dragY, handlers: dragHandlers } = useSheetDrag({
    onDismiss: requestClose,
    canStart: () => !busyRef.current,
  });
  const grabRef = useRef<HTMLDivElement | null>(null);
  const getInsideElementsRef = useRef(getInsideElements);
  getInsideElementsRef.current = getInsideElements;
  // Drag handle is a panel sibling (panel is the caller's element); treat it as inside the modal, otherwise inert disables dragging.
  const insideElements = useCallback(
    () => [...(getInsideElementsRef.current?.() ?? []), ...(grabRef.current ? [grabRef.current] : [])],
    [],
  );
  const sheetContext: DialogSheetContextValue = { sheet, dragHandlers: sheet ? dragHandlers : null };

  const layerStyle: CSSProperties | null = liftZ !== null ? { zIndex: liftZ } : layer === "top" ? TOP_LAYER_STYLE : null;
  useEffect(() => {
    if (!panel) return;
    // Panel itself may carry this attribute: mouse-opened overlays focus the panel without a focus ring on the first control.
    const preferred = panel.matches("[data-dialog-initial-focus]")
      ? panel
      : panel.querySelector<HTMLElement>("[data-dialog-initial-focus]");
    // Opening as a bottom sheet focuses the panel (screen reader announces title), not the first input: on phones that immediately opens
    // the keyboard over half the sheet. Tab starts at title-bar Close. Caller-specified initial focus (confirmation "取消") remains.
    // Presentation may not yet have switched (threshold state update is later); compute directly from eligibility conditions.
    const shape = sheetShapeRef.current;
    const opensAsSheet =
      shape.sheetHost &&
      shape.narrow &&
      (shape.presentation === "sheet" || shape.autoSheet || (shape.presentation === "auto" && dialogNeedsSheet(panel)));
    if (opensAsSheet && !preferred) {
      panel.focus();
      return;
    }
    const first = focusableIn(panel)[0];
    (preferred ?? first ?? panel).focus();
  }, [panel]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    panelProps?.onKeyDown?.(event);
    // Portal keyboard events still bubble along the React tree to the rendering component; while a modal is open, outer
    // shortcuts, table navigation and custom-dialog focus traps must not process these keys again.
    event.stopPropagation();
    if (event.key !== "Tab" || !panel) return;
    cycleTabWithin(panel, event);
  };

  const panelNode = (
    <FloatingFocusManager
      context={context}
      modal
      guards={false}
      outsideElementsInert
      initialFocus={-1}
      returnFocus={returnFocusRef ?? true}
      getInsideElements={sheetHost ? insideElements : getInsideElements}
    >
      <div
        {...panelProps}
        {...(sheet ? { "data-dialog-presentation": "sheet" } : null)}
        ref={(node) => {
          refs.setFloating(node);
          setPanel(node);
        }}
        className={panelClassName}
        style={overlay === "none" && layerStyle ? { ...panelStyle, ...layerStyle } : panelStyle}
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-label={labelledBy ? undefined : label}
        aria-describedby={describedBy ?? panelProps?.["aria-describedby"]}
        aria-busy={busy || undefined}
        tabIndex={-1}
        {...getFloatingProps(overlay === "none" ? { onKeyDown } : undefined)}
        // useRole sets its own panel ID; prefer caller ID when supplied (trigger aria-controls points to it).
        {...(panelProps?.id ? { id: panelProps.id } : null)}
      >
        {sheetHost ? <DialogSheetContext.Provider value={sheetContext}>{children}</DialogSheetContext.Provider> : children}
      </div>
    </FloatingFocusManager>
  );
  const keyboardOpen = sheetViewport.keyboardInset > 0;
  const sheetNode = sheetHost ? (
    <div
      ref={setSheetElement}
      className={sheet ? "ui-dialog-sheet" : "ui-dialog-sheet-host"}
      data-keyboard={sheet && keyboardOpen ? "open" : undefined}
      style={
        sheet
          ? ({
              "--ui-sheet-keyboard-inset": `${sheetViewport.keyboardInset}px`,
              ...(sheetViewport.viewportHeight ? { "--ui-sheet-viewport-height": `${sheetViewport.viewportHeight}px` } : null),
              transform: dragY ? `translateY(${dragY}px)` : undefined,
              transition: dragY ? "none" : undefined,
            } as CSSProperties)
          : undefined
      }
    >
      {sheet ? (
        <div ref={grabRef} className="ui-dialog-sheet-grab" aria-hidden="true" data-sheet-grab="" {...dragHandlers} />
      ) : null}
      {panelNode}
    </div>
  ) : panelNode;

  return (
    <FloatingNode id={nodeId}>
      <PortalOrInline mount={mount}>
        {overlay === "none" ? panelNode : (
          <FloatingOverlay
            ref={setBackdrop}
            data-settings-dialog-portal=""
            {...(sheet ? { "data-dialog-presentation": "sheet" } : null)}
            className={backdropClassName}
            style={layerStyle ? { ...backdropStyle, ...layerStyle } : backdropStyle}
            onPointerDown={(event) => {
              pressedOnBackdropRef.current = event.target === event.currentTarget;
            }}
            onClick={(event) => {
              event.stopPropagation();
              const pressedOnBackdrop = pressedOnBackdropRef.current;
              pressedOnBackdropRef.current = false;
              if (closeOnBackdrop && pressedOnBackdrop && event.target === event.currentTarget) requestClose();
            }}
            onKeyDown={onKeyDown}
          >
            {sheetNode}
          </FloatingOverlay>
        )}
      </PortalOrInline>
    </FloatingNode>
  );
}

function PortalOrInline({ mount, children }: { mount: "body" | "inline"; children: ReactNode }) {
  return mount === "inline" ? <>{children}</> : <FloatingPortal>{children}</FloatingPortal>;
}

/** Standard dialog: settings-dialog title, body and footer around DialogShell. */
export function Dialog({
  title,
  children,
  onClose,
  footer,
  width = 520,
  busy = false,
  variant = "center",
  closeLabel,
  layer = "base",
  returnFocusRef,
  role,
  describedBy,
  presentation,
}: DialogProps) {
  const titleId = useId();
  return (
    <DialogShell
      onClose={onClose}
      busy={busy}
      layer={layer}
      returnFocusRef={returnFocusRef}
      labelledBy={titleId}
      role={role}
      describedBy={describedBy}
      presentation={presentation ?? (variant === "drawer" ? "sheet" : "auto")}
      backdropClassName={`settings-dialog-backdrop${variant === "drawer" ? " settings-dialog-backdrop--drawer" : ""}`}
      panelClassName={`settings-dialog${variant === "drawer" ? " settings-dialog--drawer" : ""}`}
      panelStyle={{
        width: variant === "drawer" ? `min(${width}px, 100vw)` : `min(${width}px, calc(100vw - 32px))`,
      }}
    >
      <DialogFrame
        title={title}
        titleId={titleId}
        variant={variant}
        busy={busy}
        closeLabel={closeLabel}
        onClose={onClose}
        footer={footer}
      >
        {children}
      </DialogFrame>
    </DialogShell>
  );
}

/**
 * `Dialog` title, body and footer. In bottom sheets (behavior batch B), use a title bar: title plus persistent Close
 * (swipe dismissal is undiscoverable and unusable by screen readers, Narrow Spec section 抽屉); title bar supports downward drag like the handle.
 * Three regions keep their positions (title / title bar, body, footer); body forms do not remount on presentation changes.
 */
function DialogFrame({
  title,
  titleId,
  variant,
  busy,
  closeLabel,
  onClose,
  footer,
  children,
}: {
  title: ReactNode;
  titleId: string;
  variant: "center" | "drawer";
  busy: boolean;
  closeLabel?: string;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
}) {
  const tCommon = useTranslations("common");
  const { sheet, dragHandlers } = useContext(DialogSheetContext);
  const closeName = closeLabel ?? tCommon("close");
  let head: ReactNode;
  if (sheet) {
    head = (
      <div className="ui-dialog-sheet-head" {...dragHandlers}>
        <h3 id={titleId} className="settings-dialog__title">{title}</h3>
        <button
          type="button"
          className="ui-dialog-sheet-close"
          aria-label={closeName}
          disabled={busy}
          onClick={onClose}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <X size={16} aria-hidden />
        </button>
      </div>
    );
  } else if (variant === "drawer") {
    head = (
      <div className="settings-dialog__drawer-head">
        <h3 id={titleId} className="settings-dialog__title">{title}</h3>
        <button type="button" aria-label={closeName} disabled={busy} onClick={onClose}><X size={16} aria-hidden /></button>
      </div>
    );
  } else {
    head = <h3 id={titleId} className="settings-dialog__title">{title}</h3>;
  }
  return (
    <>
      {head}
      <div className="settings-dialog__body">{children}</div>
      {footer ? <div className="settings-dialog__footer">{footer}</div> : null}
    </>
  );
}
