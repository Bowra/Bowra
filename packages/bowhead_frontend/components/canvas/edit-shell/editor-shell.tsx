"use client";

/**
 * The editor shell shared by the deck and Word editors (docx inline edit design §8.1; canvas
 * "PPT 就地修改 · 交互设计" › Google 式排列, artboards G0–G9 and W1–W2).
 *
 * A top bar holds the file, its save status and the save controls; one toolbar row says what is
 * selected and offers its actions; the page strip sits on the left and an icon rail on the right
 * opens one panel at a time. Panels are closed by default and push the page; below 1280 px the
 * page strip hides itself while a panel is open, and the toolbar then offers the pages. Phones keep
 * their own sheet flow and do not use this layout. The shell holds no editing state: the editor
 * passes what to show and handles every action.
 */

import { type KeyboardEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import {
  ChevronLeft as LucideChevronLeft,
  ChevronRight as LucideChevronRight,
  CircleAlert as LucideCircleAlert,
  Check as LucideCheck,
  FileText as LucideFileText,
  Keyboard as LucideKeyboard,
  Layers as LucideLayers,
  LoaderCircle as LucideLoaderCircle,
  PanelLeft as LucidePanelLeft,
  Presentation as LucidePresentation,
  Redo2 as LucideRedo2,
  Undo2 as LucideUndo2,
  X as LucideX,
  FileDiff as LucideFileDiff,
  type LucideIcon,
} from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import styles from "./edit-shell.module.css";

export type ShellPanelId = "changes" | "items" | "keys";
export type ShellZoom = "fit" | "100" | "150";

/** Below this width an open panel hides the page strip (canvas G6). */
const STRIP_WITH_PANEL_MIN = 1280;

export function EditorShell({
  topBar,
  toolbar,
  notice,
  strip,
  stage,
  panel,
  rail,
  stripOpen,
  onStripOpenChange,
  testId,
  onKeyDown,
  rootRef,
}: {
  topBar: ReactNode;
  toolbar: (layout: { stripHidden: boolean }) => ReactNode;
  notice?: ReactNode;
  strip?: ReactNode;
  stage: ReactNode;
  /** The open panel, or null. */
  panel?: ReactNode;
  rail: ReactNode;
  /** The person's own choice to show or hide the strip; null follows the width. */
  stripOpen?: boolean | null;
  onStripOpenChange?: (open: boolean | null) => void;
  testId?: string;
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void;
  rootRef?: (element: HTMLDivElement | null) => void;
}) {
  const ownRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ownRef.current;
    if (!element) return;
    const measure = () => setWidth(element.getBoundingClientRect().width);
    measure();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const autoHidden = Boolean(panel) && width > 0 && width < STRIP_WITH_PANEL_MIN;
  const stripHidden = !strip || (stripOpen ?? !autoHidden) === false;
  // The person's choice lasts while the panel stays as it was; opening or closing one follows the width again.
  const panelOpen = Boolean(panel);
  const previousPanel = useRef(panelOpen);
  useEffect(() => {
    if (previousPanel.current !== panelOpen) onStripOpenChange?.(null);
    previousPanel.current = panelOpen;
  }, [onStripOpenChange, panelOpen]);
  return (
    <div
      ref={(element) => { ownRef.current = element; rootRef?.(element); }}
      className={styles.shell}
      data-testid={testId}
      data-strip-hidden={stripHidden || undefined}
      onKeyDown={onKeyDown}
    >
      {topBar}
      {toolbar({ stripHidden })}
      {notice}
      <div className={styles.body}>
        {!stripHidden ? strip : null}
        <div className={styles.stage}>{stage}</div>
        {panel}
        {rail}
      </div>
    </div>
  );
}

export type ShellStatus = {
  tone: "muted" | "warning" | "danger" | "positive";
  icon?: "dirty" | "saving" | "saved" | "error";
  text: string;
  /** Said after the status, quieter (保存后不再保留第 1 版). */
  extra?: string;
};

export type ShellButton = { label: string; onClick: () => void; disabled?: boolean; busy?: boolean; title?: string };

export function EditorTopBar({
  kind,
  filename,
  status,
  discard,
  discardDanger,
  primary,
  continueEditing,
  onExit,
  onCloseFullView,
  extra,
}: {
  kind: "deck" | "document";
  filename: string;
  status: ShellStatus;
  discard?: ShellButton;
  /** Discarding throws away work that a conflict kept (放弃我的修改). */
  discardDanger?: boolean;
  primary?: ShellButton;
  continueEditing?: ShellButton;
  onExit: () => void;
  /** Editing started in the full view: it can also be closed from here. */
  onCloseFullView?: () => void;
  /** Quiet facts beside the controls, such as pages waiting in the Composer. */
  extra?: ReactNode;
}) {
  const t = useTranslations("edit_shell");
  const icon = status.icon === "saving" ? LucideLoaderCircle : status.icon === "saved" ? LucideCheck : status.icon === "error" ? LucideCircleAlert : null;
  return (
    <header className={styles.topBar}>
      <span className={styles.fileIcon} aria-hidden="true">
        <LucideGlyph icon={kind === "deck" ? LucidePresentation : LucideFileText} size={16} />
      </span>
      <div className={styles.fileText}>
        <h2 className={styles.fileName} title={filename}>{filename}</h2>
        <p className={styles.status} role="status" data-tone={status.tone}>
          {status.icon === "dirty" ? <span className={styles.statusDot} aria-hidden="true" /> : null}
          {icon ? <LucideGlyph icon={icon} size={14} aria-hidden="true" className={status.icon === "saving" ? styles.spin : undefined} /> : null}
          <span className={styles.statusText}>{status.text}</span>
          {status.extra ? (
            <>
              <span className={styles.statusSeparator} aria-hidden="true">·</span>
              <span className={styles.statusExtra}>{status.extra}</span>
            </>
          ) : null}
        </p>
      </div>
      <div className={styles.barActions}>
        {extra}
        {discard ? (
          <Button variant={discardDanger ? "danger" : "plain"} disabled={discard.disabled} onClick={discard.onClick} title={discard.title}>
            {discard.label}
          </Button>
        ) : null}
        {continueEditing ? (
          <Button data-shell-action="continue" onClick={continueEditing.onClick} loading={continueEditing.busy} disabled={continueEditing.disabled}>
            {continueEditing.label}
          </Button>
        ) : null}
        {primary ? (
          <Button variant="primary" disabled={primary.disabled} loading={primary.busy} title={primary.title} onClick={primary.onClick}>
            {primary.label}
          </Button>
        ) : null}
        <span className={styles.barSeparator} aria-hidden="true" />
        <Button variant="plain" onClick={onExit}>{t("exit")}</Button>
        {onCloseFullView ? (
          <button type="button" className="v2-icon-btn" aria-label={t("close_full_view")} title={t("close_full_view")} onClick={onCloseFullView}>
            <LucideGlyph icon={LucideX} size={16} aria-hidden="true" />
          </button>
        ) : null}
      </div>
    </header>
  );
}

export type ShellAction = {
  key: string;
  label: string;
  icon?: LucideIcon;
  onClick: () => void;
  /** A toggle (引用这一页 / 已引用, 编辑数据). */
  pressed?: boolean;
  /** The accessible name when it says more than the label (引用这一页 · 第 2 页). */
  ariaLabel?: string;
  disabled?: boolean;
};

export type ShellSelection = {
  icon: LucideIcon;
  kind: string;
  name: string;
  where?: string;
  badge?: { tone: BadgeTone; text: string };
};

export function EditorToolbar({
  undo,
  redo,
  zoom,
  onZoom,
  pages,
  selection,
  pageLabel,
  actions,
  hint,
  hintId,
  compact,
}: {
  undo: ShellButton;
  redo: ShellButton;
  zoom: ShellZoom;
  onZoom: (zoom: ShellZoom) => void;
  /** Shown while the page strip is hidden; `label` names the strip (页面, 大纲). */
  pages?: { page: number; count: number; onPrevious: () => void; onNext: () => void; onShowStrip: () => void; label?: string };
  selection?: ShellSelection | null;
  /** What the row names when nothing is selected (第 2 页). */
  pageLabel: string;
  actions: ReadonlyArray<ShellAction>;
  hint?: string;
  hintId?: string;
  /** Narrow: the place and the hint give way (canvas G6/G7). */
  compact?: boolean;
}) {
  const t = useTranslations("edit_shell");
  const zoomLabel = (value: ShellZoom) => t(value === "fit" ? "zoom_fit" : value === "100" ? "zoom_100" : "zoom_150");
  return (
    <div className={styles.toolbar} role="toolbar" aria-label={t("toolbar_label")}>
      <button type="button" className="v2-icon-btn" aria-label={undo.label} title={undo.label} disabled={undo.disabled} onClick={undo.onClick}>
        <LucideGlyph icon={LucideUndo2} size={16} aria-hidden="true" />
      </button>
      <button type="button" className="v2-icon-btn" aria-label={redo.label} title={redo.label} disabled={redo.disabled} onClick={redo.onClick}>
        <LucideGlyph icon={LucideRedo2} size={16} aria-hidden="true" />
      </button>
      <span className={styles.toolSeparator} aria-hidden="true" />
      <FilterMenuButton<ShellZoom>
        value={zoom}
        options={(["fit", "100", "150"] as const).map((value) => ({ value, label: zoomLabel(value) }))}
        onChange={onZoom}
        format={(value) => value}
        menuLabel={t("zoom")}
        active={false}
      />
      {pages ? (
        <>
          <Button size="sm" variant="plain" aria-expanded={false} onClick={pages.onShowStrip}>
            <LucideGlyph icon={LucidePanelLeft} size={16} aria-hidden="true" />{pages.label ?? t("pages")}
          </Button>
          <button type="button" className="v2-icon-btn" aria-label={t("previous_page")} disabled={pages.page <= 1} onClick={pages.onPrevious}>
            <LucideGlyph icon={LucideChevronLeft} size={16} aria-hidden="true" />
          </button>
          <span className={styles.pageOf} aria-live="polite">{t("page_of", { page: pages.page, count: pages.count })}</span>
          <button type="button" className="v2-icon-btn" aria-label={t("next_page")} disabled={pages.page >= pages.count} onClick={pages.onNext}>
            <LucideGlyph icon={LucideChevronRight} size={16} aria-hidden="true" />
          </button>
        </>
      ) : null}
      <span className={styles.toolSeparator} aria-hidden="true" />
      <div className={styles.selection} role="group"
        aria-label={selection ? t("selection_group", { kind: selection.kind, name: selection.name }) : t("page_group")}>
        {selection ? (
          <span className={styles.selectionFacts}>
            <span className={styles.selectionIcon} aria-hidden="true"><LucideGlyph icon={selection.icon} size={16} /></span>
            <span className={styles.selectionKind}>{selection.kind}</span>
            <span className={styles.statusSeparator} aria-hidden="true">·</span>
            <strong className={styles.selectionName} title={selection.name}>{selection.name}</strong>
            {selection.where && !compact ? <span className={styles.selectionWhere}>{selection.where}</span> : null}
            {selection.badge ? <Badge tone={selection.badge.tone} size="sm">{selection.badge.text}</Badge> : null}
          </span>
        ) : (
          <span className={styles.pageLabel}>{pageLabel}</span>
        )}
        {actions.map((action) => (
          <Button key={action.key} size="sm" variant="plain" className={styles.toolAction} onClick={action.onClick} disabled={action.disabled}
            aria-pressed={action.pressed} aria-label={action.ariaLabel}>
            {action.icon ? <LucideGlyph icon={action.icon} size={14} aria-hidden="true" /> : null}
            {action.label}
          </Button>
        ))}
      </div>
      <span className={styles.toolSpacer} />
      {hint && !compact ? <span className={styles.hint} id={hintId}>{hint}</span> : null}
    </div>
  );
}

/** A line under the toolbar: one notice at a time, which the person can dismiss when it allows. */
export function EditorNoticeRow({ children, action, onDismiss, dismissLabel }: {
  children: ReactNode;
  action?: ReactNode;
  onDismiss?: () => void;
  dismissLabel?: string;
}) {
  return (
    <div className={styles.noticeRow} role="status">
      <span className={styles.noticeText}>{children}</span>
      {action}
      {onDismiss ? <Button size="sm" variant="plain" onClick={onDismiss}>{dismissLabel}</Button> : null}
    </div>
  );
}

export function EditorRail({ active, onToggle, changes, conflict, itemsTitle }: {
  active: ShellPanelId | null;
  onToggle: (panel: ShellPanelId) => void;
  /** Unsaved changes, shown on 修改记录. */
  changes: number;
  conflict?: boolean;
  /** The tooltip of 本页内容 (本页可修改的内容 / 本页可修改的段落和表格). */
  itemsTitle: string;
}) {
  const t = useTranslations("edit_shell");
  const items: Array<{ id: ShellPanelId; label: string; title: string; icon: LucideIcon; aria: string }> = [
    {
      id: "changes",
      label: t("panel_changes"),
      title: t("panel_changes"),
      icon: LucideFileDiff,
      aria: changes ? t(conflict ? "rail_changes_conflict" : "rail_changes_count", { count: changes }) : t("panel_changes"),
    },
    { id: "items", label: t("panel_items"), title: itemsTitle, icon: LucideLayers, aria: t("panel_items") },
    { id: "keys", label: t("panel_keys"), title: t("panel_keys_title"), icon: LucideKeyboard, aria: t("panel_keys") },
  ];
  return (
    <div className={styles.rail} role="toolbar" aria-orientation="vertical" aria-label={t("rail_label")}>
      {items.map((item) => (
        <button key={item.id} type="button" className={styles.railButton} data-rail-panel={item.id}
          aria-expanded={active === item.id} aria-controls={active === item.id ? `edit-shell-panel-${item.id}` : undefined}
          aria-label={item.aria} title={item.title} onClick={() => onToggle(item.id)}>
          <span className={styles.railPill} aria-hidden="true"><LucideGlyph icon={item.icon} size={20} /></span>
          <span className={styles.railLabel} aria-hidden="true">{item.label}</span>
          {item.id === "changes" && changes > 0 ? (
            <span className={styles.railCount} data-tone={conflict ? "danger" : undefined} aria-hidden="true">{changes}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}

/**
 * One panel beside the page. Opening it moves focus to its title, so a screen reader reads what
 * opened; Esc closes it and returns focus to its rail button (the editor does that in `onClose`).
 */
export function EditorPanel({ id, title, subtitle, onClose, footer, children, focusOnOpen = true }: {
  id: ShellPanelId;
  title: string;
  subtitle?: string;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
  /** False when the panel opened by itself (a refused save) and focus should stay where it was. */
  focusOnOpen?: boolean;
}) {
  const t = useTranslations("edit_shell");
  const titleRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focusOnOpen) titleRef.current?.focus();
  }, [focusOnOpen, id]);
  return (
    <aside id={`edit-shell-panel-${id}`} className={styles.panel} aria-labelledby={`edit-shell-panel-${id}-title`}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || event.defaultPrevented) return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}>
      <div className={styles.panelHead}>
        <h2 id={`edit-shell-panel-${id}-title`} ref={titleRef} tabIndex={-1} className={styles.panelTitle}>
          {title}
          {subtitle ? <span className={styles.panelSubtitle}>{subtitle}</span> : null}
        </h2>
        <button type="button" className="v2-icon-btn" aria-label={t("close_panel", { title })} title={t("close_panel_hint")} onClick={onClose}>
          <LucideGlyph icon={LucideX} size={16} aria-hidden="true" />
        </button>
      </div>
      <div className={styles.panelBody}>{children}</div>
      {footer ? <div className={styles.panelFooter}>{footer}</div> : null}
    </aside>
  );
}
