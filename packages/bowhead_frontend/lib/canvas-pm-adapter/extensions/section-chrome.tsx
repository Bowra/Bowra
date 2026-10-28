/**
 * SectionChrome — visual shell for section NodeViews (W1-2 + W3-8).
 *
 * Responsibilities:
 *   - Float top-right chrome on hover / focus (drag handle ≡ + action menu ⋮)
 *   - Draw focus ring on focus-within (blue border + soft shadow)
 *   - Portal action menu through floating-ui (avoid overflow/clip); P3.2 command args are placeholders.
 *
 * Invariants:
 *   - PM doc.getJSON output unchanged
 *   - NodeViewWrapper data-drag-handle / data-section-id unaffected
 *   - Pass through data-section-id (citation [chart:S] clicks use it for scrollIntoView)
 */

"use client";

import React, { useState, useId } from "react";
import { useTranslations } from "next-intl";
import { useCanvasContext } from "../canvas-context";
import { trackInteraction as track } from "@/lib/metrics";
import {
  SaveToMemoryButton,
  isVerifiedFactKind,
} from "@/components/canvas/save-to-memory-button";
import { Spark, MoreH, Trash, ArrowUp, ArrowDown } from "@/components/icons";
import { useTouchAffordance } from "@/lib/use-touch-affordance";
import {
  MenuFloating,
  MenuItemReason,
  useMenu,
  useMenuItem,
  withConfirmEllipsis,
} from "@/components/ui/menu";
import {
  GripVertical as LucideGripVertical,
  TextAlignCenter as LucideTextAlignCenter,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

/** One section menu action (order and danger semantics are defined once in SectionChrome). */
interface SectionSheetAction {
  key: string;
  label: string;
  icon?: React.ReactNode;
  onClick: () => void;
  /** Dangerous action: red + preceding divider. */
  danger?: boolean;
  /** AI group: brand color + bold (first item). */
  accent?: boolean;
  /** Current unavailability reason; if present, disable the item and show the reason on line two. */
  disabledReason?: string;
  /** Shows a confirmation before execution (dangerous menu items carry "…"). */
  confirms?: boolean;
}

/** Design D2 GripDots: 2×3 dot glyph for left-slot drag handle ⋮⋮. */
function GripDots() {
  return (
    <LucideGlyph icon={LucideGripVertical} size={10} height={14} aria-hidden="true" />
  );
}

const TEXT_BASED_KINDS = new Set(["narrative", "deck", "callout"]);

/* design r2 (report-edit-affordance) action matrix: drag/split/AI belong only to
 * text-based body blocks (design blocks retain their separate v2a feature path). Skeleton parts such as meta
 * no longer render handles/action clusters; they only support inline editing, and left-edge traces implying drag/delete
 * no longer cover them (see globals.css ::before selectors). */
const AFFORDANCE_OPS_KINDS = new Set(["narrative", "deck", "callout", "design"]);

/* r3 §5.1: content atomic blocks retain delete-only menus; batch 1 gated ⋮ entirely on ops kinds,
 * accidentally removing the sole pre-batch-1 deletion entry (keyboard guard also blocks and directs users to menus).
 * Set = all chrome-wrapped atomic kinds except meta (actual undeletable skeleton). */
const DELETE_ONLY_KINDS = new Set([
  "data_table",
  "trend_chart",
  "sql_block",
  "code_block",
  "spec_block",
  "datarc_iframe",
  "html_artifact",
  "external_iframe",
  "attribution",
  "image",
]);

/** r3 §5.1: keyboard-delete toast guidance criterion: whether this kind menu has "删除". */
export function kindHasDeleteMenu(kind: string): boolean {
  return AFFORDANCE_OPS_KINDS.has(kind) || DELETE_ONLY_KINDS.has(kind);
}

/** r3 §5.2: standard toolbar technique: mousedown does not take focus, preserving PM caret/selection,
 * so "在此处分割" gets the actual user cursor position. */
const keepEditorSelection = (e: React.MouseEvent) => e.preventDefault();

/** design r2 "在此处分割" menu glyph (three horizontal lines). */
function SplitLines({ size = 14 }: { size?: number }) {
  return (
    <LucideGlyph icon={LucideTextAlignCenter} size={size} aria-hidden="true" />
  );
}

interface SectionChromeProps {
  sectionId: string;
  kind: string;
  children: React.ReactNode;
  /** P3.2.6 actions; null hides menu. Props take priority, default to CanvasContext. */
  onDelete?: () => void;
  onDuplicate?: () => void;
  onCopy?: () => void;
}

export function SectionChrome({
  sectionId,
  kind,
  children,
  onDelete: onDeleteProp,
  onDuplicate,
  onCopy,
}: SectionChromeProps) {
  const t = useTranslations("section_chrome");
  const ctx = useCanvasContext();
  // mobile r1 §1.1: form is determined by hover capability, not screen width; tablets follow the same rule.
  const isTouch = useTouchAffordance();
  const [menuOpen, setMenuOpen] = useState(false);
  // Section menus use the shared useMenu (body mount, layer tokens only); keyboard (arrows, Home / End,
  // initial letters, Esc returns to ⋯), narrow bottom sheets, red dangerous items with …, and disabled reasons are shared features.

  // W11-5 / #42: prop takes priority, fallback context.onDeleteSection (with telemetry wrap)
  const onDelete = onDeleteProp ?? (
    ctx.onDeleteSection
      ? () => {
          track("bowhead_section_chrome_menu_total", { action: "delete", kind });
          void ctx.onDeleteSection!(sectionId);
        }
      : undefined
  );

  // Check whether the preceding adjacent text-based section can merge
  const canMergeAbove = (() => {
    if (!ctx.onMergeWithPrev) return false;
    const idx = ctx.sections.findIndex((s) => s.id === sectionId);
    if (idx <= 0) return false;
    // Find preceding text-based section
    for (let i = idx - 1; i >= 0; i--) {
      const k = ctx.sections[i].kind;
      if (k === "narrative" || k === "deck" || k === "callout") {
        // Current section must also be text-based
        const cur = ctx.sections[idx].kind;
        return cur === "narrative" || cur === "deck" || cur === "callout";
      }
    }
    return false;
  })();
  const onMergePrev = canMergeAbove
    ? () => {
        track("bowhead_section_chrome_menu_total", { action: "duplicate", kind });
        void ctx.onMergeWithPrev!(sectionId);
      }
    : undefined;

  // W11-15 / #66: split requires text-based kind + onSplitAtCursor
  const canSplit = !!ctx.onSplitAtCursor && (kind === "narrative" || kind === "deck" || kind === "callout");
  const onSplit = canSplit
    ? () => {
        track("bowhead_section_split_total", { kind });
        void ctx.onSplitAtCursor!(sectionId);
      }
    : undefined;

  // WP3 (design D2): only narrative/deck/callout blocks show "✦ Edit with AI" in edit mode.
  // Chart blocks do not use this path (no backend; report-refactor sprint-status WP3 decision A).
  const isTextBased = TEXT_BASED_KINDS.has(kind);
  const onEditWithAI =
    isTextBased && ctx.isEditMode && ctx.onEditBlockWithAI
      ? () => {
          track("bowhead_section_chrome_menu_total", { action: "edit_with_ai", kind });
          ctx.onEditBlockWithAI!(sectionId);
        }
      : undefined;
  // memory-revive S1 (user decided to hide on 2026-07-22): saving canvas to memory is the wrong shape; canvas
  // facts are mostly point-in-time data (the report itself is the system of record). ~15 bullets of ≤200 chars exhaust
  // the 3000-char notes budget, crowding out valuable definitions/preferences (discovery §5.2 "少存结论，多存
  // 口径"). Dreaming distills definitions from conversation; no save-KPI button is needed. Backend flag
  // BOWHEAD_MEMORY_FROM_CANVAS_ENABLED also defaults off; hide the button to avoid a visible action returning 503.
  // Retain the full component/backend chain: future definition-level saving can revive it by wiring this constant to a real flag.
  const CANVAS_SAVE_TO_MEMORY_ENABLED = false;
  const showSave =
    CANVAS_SAVE_TO_MEMORY_ENABLED && !ctx.isEditMode && isVerifiedFactKind(kind) && !!ctx.canvasId;

  const handleAction = (fn?: () => void) => {
    setMenuOpen(false);
    fn?.();
  };

  // design r2 action-matrix gate: non-text-based/non-design blocks render no drag handle/action cluster
  // (skeleton parts only inline edit). children and data-section-id passthrough remain unchanged.
  // r3 §5.1: delete-only kinds are exceptions: menu only shows "删除" (split/merge/AI have their own kind
  // gates and remain absent); do not extend handles/traces.
  const opsAllowed = AFFORDANCE_OPS_KINDS.has(kind);
  const menuAllowed = opsAllowed || DELETE_ONLY_KINDS.has(kind);

  // W11-3: any action handler missing → hide entire menu (only drag handle remains).
  // #42: menu appears automatically once real handlers are wired.
  const hasAnyAction = menuAllowed && !!(onDelete || onCopy || onDuplicate || onMergePrev || onSplit);
  // Design D2 rc-actions cluster renders if any of Edit with AI / SaveToMemory / ⋮ exists.
  // mobile r1 §2.2: touch does not render the hover-derived pill; use persistent ⋯ instead.
  const showCluster =
    !isTouch && ((opsAllowed && !!(onEditWithAI || showSave)) || hasAnyAction);

  // W11-12 telemetry: open menu
  const handleMenuToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!menuOpen) {
      track("bowhead_section_chrome_menu_total", { action: "open", kind });
      // mobile r1 §2.3: mutually exclusive with soft keyboard; blur editing focus before opening, or fixed sheet is
      // pushed up/covered by keyboard, with different behavior on the two platforms.
      if (isTouch && document.activeElement instanceof HTMLElement) {
        document.activeElement.blur();
      }
    }
    // The shared useClick toggles the menu; here only record telemetry and dismiss the soft keyboard
  };

  // design r2 single-source menu items: order (AI first → structure actions → dangerous last) and danger semantics defined only here,
  // shared by desktop popover/touch action sheet. Render only items with actual handlers (W11-3: no disabled placeholders).
  const menuActions: SectionSheetAction[] = [];
  if (onEditWithAI) {
    menuActions.push({
      key: "ai",
      label: t("edit_with_ai"),
      icon: <Spark width={14} height={14} />,
      onClick: () => handleAction(onEditWithAI),
      accent: true,
    });
  }
  if (onCopy) menuActions.push({ key: "copy", label: t("menu_copy"), onClick: () => handleAction(onCopy) });
  if (onDuplicate) {
    menuActions.push({ key: "duplicate", label: t("menu_duplicate"), onClick: () => handleAction(onDuplicate) });
  }
  if (onMergePrev) {
    menuActions.push({ key: "merge_prev", label: t("menu_merge_prev"), onClick: () => handleAction(onMergePrev) });
  }
  if (onSplit) {
    menuActions.push({
      key: "split",
      label: t("menu_split"),
      icon: <SplitLines />,
      onClick: () => handleAction(onSplit),
      // Disable with reason when cursor is outside this section or text is selected, rather than notifying after click
      ...(ctx.canSplitAtCursor && !ctx.canSplitAtCursor(sectionId)
        ? { disabledReason: t("split_needs_cursor") }
        : null),
    });
  }
  // mobile r1 §2.1 (design C3): move up/down only on touch; desktop uses handles. Do not disable at boundaries
  // (ctx.sections order differs from PM top-level order, giving wrong states); handler notifies on out-of-bounds.
  if (isTouch && opsAllowed && ctx.onMoveSection) {
    menuActions.push({
      key: "move_up",
      label: t("menu_move_up"),
      icon: <ArrowUp size={14} />,
      onClick: () => handleAction(() => ctx.onMoveSection!(sectionId, "up")),
    });
    menuActions.push({
      key: "move_down",
      label: t("menu_move_down"),
      icon: <ArrowDown size={14} />,
      onClick: () => handleAction(() => ctx.onMoveSection!(sectionId, "down")),
    });
  }
  if (onDelete) {
    menuActions.push({
      key: "delete",
      label: t("menu_delete"),
      icon: <Trash size={14} />,
      onClick: () => handleAction(onDelete),
      danger: true,
      // Deletion confirms first (including inline-chart count), so the shared menu carries …
      confirms: true,
    });
  }

  // Section summary atop action sheet lets users confirm which block they are operating on (design C3 ea-sheet-cap)
  const sheetCaption = (() => {
    if (!isTouch) return undefined;
    const section = ctx.sections.find((s) => s.id === sectionId) as
      | { markdown?: string; text?: string; title?: string }
      | undefined;
    const raw = section?.markdown ?? section?.text ?? section?.title ?? "";
    const flat = raw.replace(/\s+/g, " ").trim();
    return flat ? (flat.length > 40 ? `${flat.slice(0, 40)}…` : flat) : undefined;
  })();

  // Shared menu. Touch devices without hover always use bottom sheets with the section summary atop;
  // desktop uses shared thresholds (item count, descriptions, submenus).
  const menu = useMenu({
    open: menuOpen,
    onOpenChange: setMenuOpen,
    placement: "bottom-end",
    offset: 4,
    presentation: isTouch ? "sheet" : "auto",
    sheetTitle: sheetCaption,
  });
  const menuTriggerProps = () =>
    menu.getReferenceProps({ onMouseDown: keepEditorSelection, onClick: handleMenuToggle });
  const setMenuTrigger = (el: HTMLButtonElement | null) => {
    menu.refs.setReference(el);
  };

  return (
    <div className="v2-section-chrome" data-chrome-kind={kind} data-chrome-touch={isTouch ? "on" : undefined}>
      {/* Design D2: left-slot drag handle ⋮⋮ appears only on hover/focus, rendered only in edit mode.
          design r2: skeleton parts cannot drag, so render only with opsAllowed.
          r3 §5.3: place data-drag-handle here (a text-based wrapper cannot be the entire
          handle, which hijacks text selection; atomic wrapper handles are unaffected).
          Explicit draggable is required: PM automatically adds it only to leaf NodeViews without contentDOM
          (prosemirror-view NodeViewDesc); without it on text-based content, the browser
          never starts dragging, confirmed by successful monkey-patching on actual 116 devices.
          mobile r1 §1.3: no drag on touch (long-press drag is different interaction); use sheet move-up/down instead. */}
      {ctx.isEditMode && opsAllowed && !isTouch && (
        <div className="rc-handle" title={t("drag_handle")} aria-hidden="true" data-drag-handle draggable>
          <GripDots />
        </div>
      )}
      {/* mobile r1 §2.2 (design A3): touch has no hover; replace floating pill with persistent ⋯. */}
      {isTouch && hasAnyAction && (
        <button
          ref={setMenuTrigger}
          type="button"
          className={"rc-mdots" + (menuOpen ? " on" : "")}
          aria-label={`${t("menu_aria")} (${kind})`}
          {...menuTriggerProps()}
        >
          <MoreH size={14} />
        </button>
      )}
      {/* Design D2: top-right action cluster, hover/focus only, takes no layout space. */}
      {showCluster && (
        <div className="rc-actions" role="toolbar" aria-label={`${t("menu_aria")} (${kind})`}>
          {onEditWithAI && (
            <button
              type="button"
              className="rc-action-btn ai"
              onMouseDown={keepEditorSelection}
              onClick={onEditWithAI}
              title={t("edit_with_ai")}
            >
              <Spark width={13} height={13} /> {t("edit_with_ai")}
            </button>
          )}
          {onEditWithAI && (showSave || hasAnyAction) && <span className="rc-action-sep" />}
          {/* V4 PRD-D-5: save verified-fact section to memory (kpi_row / chart / data_table) */}
          {showSave && (
            <SaveToMemoryButton canvasId={ctx.canvasId!} sectionId={sectionId} />
          )}
          {hasAnyAction && (
            <button
              ref={setMenuTrigger}
              type="button"
              className="rc-action-btn"
              aria-label={`${t("menu_aria")} (${kind})`}
              {...menuTriggerProps()}
              title={t("menu_aria")}
            >
              <MoreH size={14} />
            </button>
          )}
        </div>
      )}
      {children}
      {/* mobile r1 §2.3 (design C3): touch uses the bottom sheet, desktop the anchored menu; when closed, MenuFloating renders no nodes. */}
      {hasAnyAction && (
        <MenuFloating menu={menu}>
          <div
            ref={menu.refs.setFloating}
            data-section-menu="true"
            {...menu.getFloatingProps({
              className: "rc-menu-pop",
              "aria-label": `${t("menu_aria")} (${kind})`,
              onMouseDown: keepEditorSelection,
            })}
          >
            {menuActions.map((action, i) => (
              <React.Fragment key={action.key}>
                {action.danger && i > 0 && <div className="rc-msep" role="separator" />}
                <SectionMenuItem action={action} />
              </React.Fragment>
            ))}
          </div>
        </MenuFloating>
      )}
    </div>
  );
}

/**
 * Shared section-menu items: dangerous items red (data-danger), confirmation actions carry …; unavailable items disabled
 * (aria-disabled, still reachable by arrows), with reason always on line two. Pressing preserves editor focus.
 */
function SectionMenuItem({ action }: { action: SectionSheetAction }) {
  const reasonId = useId();
  const reason = action.disabledReason ?? null;
  const itemProps = useMenuItem({ onSelect: action.onClick, disabled: Boolean(reason), label: action.label });
  return (
    <>
      <button
        type="button"
        className={"rc-mi" + (action.danger ? " danger" : "")}
        data-danger={action.danger ? "true" : undefined}
        aria-describedby={reason ? reasonId : undefined}
        onMouseDown={keepEditorSelection}
        {...itemProps}
      >
        {action.icon}
        {action.confirms ? withConfirmEllipsis(action.label) : action.label}
      </button>
      {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </>
  );
}
