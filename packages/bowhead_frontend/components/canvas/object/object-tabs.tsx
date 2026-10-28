"use client";

import { useCallback, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Bot, ChevronDown, Columns2, FileText, ListChecks, Cog, Plug, Sparkles, X } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { MenuFloating, MenuSubmenu, useMenu, useMenuItem } from "@/components/ui/menu";
import { CanvasOpenMenu, CanvasOpenMenuItems, hasOpenMenuItems, type CanvasOpenMenuModel } from "./open-menu";
import { ObjectMarker, type CanvasObjectMarker } from "./status-badge";
import styles from "./canvas-object.module.css";

export type CanvasObjectKind = "results" | "document" | "agent" | "skill" | "plugin" | "settings" | "operation";

export interface CanvasTab {
  id: string;
  title: string;
  kind: CanvasObjectKind;
  marker?: CanvasObjectMarker | null;
  /** Short status word (≤ 6 Chinese characters), e.g. “Ready for your review”. */
  statusLabel?: string | null;
  closable?: boolean;
}

function safeId(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, "_");
}
export function canvasTabId(idBase: string, id: string): string {
  return `${idBase}-tab-${safeId(id)}`;
}
export function canvasPanelId(idBase: string, id: string): string {
  return `${idBase}-panel-${safeId(id)}`;
}

export function CanvasObjectIcon({ kind, size = 14 }: { kind: CanvasObjectKind; size?: number }) {
  if (kind === "results") return <Columns2 size={size} aria-hidden />;
  if (kind === "document") return <FileText size={size} aria-hidden />;
  if (kind === "agent") return <Bot size={size} aria-hidden />;
  if (kind === "skill") return <Sparkles size={size} aria-hidden />;
  if (kind === "plugin") return <Plug size={size} aria-hidden />;
  if (kind === "settings") return <Cog aria-hidden />;
  return <ListChecks size={size} aria-hidden />;
}

/** “Name · status word”, used for the tooltip and the accessible name. */
function useTabLabel() {
  const t = useTranslations("settings_canvas");
  const tc = useTranslations("canvas");
  return useCallback((tab: CanvasTab) => {
    if (!tab.marker) return tab.title;
    const word = tab.statusLabel || (tab.marker === "dirty" ? tc("edit_unsaved") : t(`marker_${tab.marker}`));
    return `${tab.title} · ${word}`;
  }, [t, tc]);
}

const MORE_RESERVE = 88;

/**
 * C1 · object tab strip (44): pinned “Results” + one tab per object (type icon, name, status marker, ×)
 * + “N more” + “Open” (+). Tabs that do not fit fold into “N more”; the selected tab always stays visible.
 * Below 480 px of canvas width the strip becomes a title switcher with the current object's ×.
 * Keyboard: ←/→ move and select, Delete closes the current object, ⌘⇧[ / ] switch objects.
 */
export function CanvasObjectTabs({
  idBase,
  tabs,
  selectedId,
  onSelect,
  onClose,
  openMenu,
  label,
}: {
  idBase: string;
  tabs: readonly CanvasTab[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  openMenu?: CanvasOpenMenuModel | null;
  label: string;
}) {
  const t = useTranslations("settings_canvas");
  const tabLabel = useTabLabel();
  const navRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const trailRef = useRef<HTMLSpanElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState<ReadonlySet<string> | null>(null);
  const tabKey = tabs.map((tab) => `${tab.id}\u0000${tab.title}\u0000${tab.marker ?? ""}\u0000${tab.closable ? 1 : 0}`).join("\u0001");

  useLayoutEffect(() => {
    const nav = navRef.current;
    const measure = measureRef.current;
    if (!nav || !measure) return;
    const compute = () => {
      const widths = new Map<string, number>();
      measure.querySelectorAll<HTMLElement>("[data-measure-id]").forEach((element) => {
        widths.set(element.dataset.measureId ?? "", element.getBoundingClientRect().width);
      });
      const style = window.getComputedStyle(nav);
      const inner = nav.clientWidth - parseFloat(style.paddingLeft || "0") - parseFloat(style.paddingRight || "0");
      const trailing = trailRef.current?.getBoundingClientRect().width ?? 0;
      const gap = 2;
      const total = tabs.reduce((sum, tab) => sum + (widths.get(tab.id) ?? 0) + gap, 0);
      if (!inner || total + trailing <= inner) {
        setVisible((current) => (current === null ? current : null));
        return;
      }
      let budget = inner - trailing - MORE_RESERVE;
      const chosen = new Set<string>();
      const pinned = tabs.filter((tab) => tab.kind === "results").map((tab) => tab.id);
      for (const id of [...pinned, selectedId]) {
        if (!id || chosen.has(id) || !widths.has(id)) continue;
        chosen.add(id);
        budget -= (widths.get(id) ?? 0) + gap;
      }
      for (const tab of tabs) {
        if (chosen.has(tab.id)) continue;
        const width = (widths.get(tab.id) ?? 0) + gap;
        if (width > budget) break;
        chosen.add(tab.id);
        budget -= width;
      }
      setVisible(chosen);
    };
    compute();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(compute);
    observer.observe(nav);
    return () => observer.disconnect();
    // tabKey captures every input that changes tab widths.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabKey, selectedId]);

  const shown = visible ? tabs.filter((tab) => visible.has(tab.id)) : tabs;
  const hidden = visible ? tabs.filter((tab) => !visible.has(tab.id)) : [];
  const selected = tabs.find((tab) => tab.id === selectedId) ?? null;
  const objectCount = tabs.filter((tab) => tab.kind !== "results").length;

  const focusTab = (id: string) => {
    window.requestAnimationFrame(() => document.getElementById(canvasTabId(idBase, id))?.focus());
  };
  const onListKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = shown.findIndex((tab) => tab.id === selectedId);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      if (!shown.length) return;
      const next = shown[(Math.max(index, 0) + (event.key === "ArrowRight" ? 1 : shown.length - 1)) % shown.length];
      onSelect(next.id);
      focusTab(next.id);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const next = event.key === "Home" ? shown[0] : shown[shown.length - 1];
      if (next) { onSelect(next.id); focusTab(next.id); }
    } else if ((event.key === "Delete" || event.key === "Backspace") && selected?.closable
      && (event.target as HTMLElement).getAttribute("role") === "tab") {
      event.preventDefault();
      onClose(selected.id);
    }
  };

  const renderTab = (tab: CanvasTab, measuring = false) => {
    const isSelected = tab.id === selectedId;
    const name = tabLabel(tab);
    return (
      <span
        key={tab.id}
        className={styles.tabItem}
        data-selected={isSelected ? "true" : undefined}
        data-measure-id={measuring ? tab.id : undefined}
      >
        <button
          type="button"
          role={measuring ? undefined : "tab"}
          id={measuring ? undefined : canvasTabId(idBase, tab.id)}
          aria-selected={measuring ? undefined : isSelected}
          aria-controls={measuring || tab.kind === "results" ? undefined : canvasPanelId(idBase, tab.id)}
          aria-label={name}
          title={name}
          tabIndex={measuring ? -1 : isSelected ? 0 : -1}
          aria-keyshortcuts={!measuring && tab.closable ? "Delete" : undefined}
          className={styles.tab}
          onClick={measuring ? undefined : () => onSelect(tab.id)}
        >
          <span className={styles.tabIcon}><CanvasObjectIcon kind={tab.kind} /></span>
          <span className={styles.tabName}>{tab.title}</span>
          {tab.marker ? <ObjectMarker marker={tab.marker} /> : null}
        </button>
        {tab.closable ? (
          <button
            type="button"
            className={styles.tabClose}
            aria-label={t("close_object", { name: tab.title })}
            title={t("close_object", { name: tab.title })}
            tabIndex={-1}
            onClick={measuring ? undefined : () => onClose(tab.id)}
          >
            <X size={14} aria-hidden />
          </button>
        ) : null}
      </span>
    );
  };

  return (
    <div className={styles.nav} ref={navRef}>
      <span className={styles.wideOnly}>
        {/* The visual row holds each tab and its ×; the tablist owns only the tabs (aria-owns), so the
            close buttons are not tablist children. Delete on a tab closes it from the keyboard. */}
        <div ref={listRef} className={styles.tabList} onKeyDown={onListKeyDown}>
          {shown.map((tab) => renderTab(tab))}
        </div>
        <div role="tablist" aria-label={label} aria-owns={shown.map((tab) => canvasTabId(idBase, tab.id)).join(" ")} className={styles.tabListOwner} />
      </span>
      <span className={styles.wideOnly}><span className={styles.navSpacer} /></span>
      <span className={styles.wideOnly}>
        <span ref={trailRef} className={styles.tabList}>
          {hidden.length ? <OverflowMenu tabs={hidden} onSelect={onSelect} tabLabel={tabLabel} /> : null}
          {hasOpenMenuItems(openMenu) ? <>
            {hidden.length ? <span className={styles.navSeparator} aria-hidden /> : null}
            <CanvasOpenMenu model={openMenu} />
          </> : null}
        </span>
      </span>
      <span className={styles.narrowOnly}>
        <TitleSwitcher
          tabs={tabs}
          selected={selected}
          onSelect={onSelect}
          openMenu={openMenu}
          tabLabel={tabLabel}
          label={label}
        />
        {objectCount > 1 ? <Badge size="sm" className={styles.countBadge}>{objectCount}</Badge> : null}
        <span className={styles.toolbarReserve} aria-hidden />
        {selected?.closable ? (
          <button type="button" className={styles.iconButton} aria-label={t("close_object", { name: selected.title })}
            title={t("close_object", { name: selected.title })} onClick={() => onClose(selected.id)}>
            <X size={16} aria-hidden />
          </button>
        ) : null}
      </span>
      <div className={styles.measureClip} aria-hidden inert>
        <div ref={measureRef} className={styles.measure}>
          {tabs.map((tab) => renderTab(tab, true))}
        </div>
      </div>
    </div>
  );
}

function OverflowItem({ tab, label, onSelect }: { tab: CanvasTab; label: string; onSelect: () => void }) {
  const props = useMenuItem({ label: tab.title, onSelect });
  return (
    <button type="button" {...props} aria-label={label}>
      <span className={styles.menuItemIcon} aria-hidden><CanvasObjectIcon kind={tab.kind} /></span>
      <span className={styles.menuItemText}><span>{tab.title}</span></span>
      {tab.marker ? <ObjectMarker marker={tab.marker} /> : null}
    </button>
  );
}

/** “N more”: tabs that do not fit; a hidden unsaved/attention object shows its marker on the button. */
function OverflowMenu({ tabs, onSelect, tabLabel }: { tabs: readonly CanvasTab[]; onSelect: (id: string) => void; tabLabel: (tab: CanvasTab) => string }) {
  const t = useTranslations("settings_canvas");
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-end", maxWidth: 320 });
  const marker = tabs.find((tab) => tab.marker === "failed")?.marker ?? tabs.find((tab) => tab.marker === "attention")?.marker
    ?? tabs.find((tab) => tab.marker === "dirty")?.marker ?? null;
  const name = t("more_objects", { count: tabs.length });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} className={styles.navButton} {...menu.getReferenceProps()}>
        {name}
        {marker ? <ObjectMarker marker={marker} /> : null}
        <ChevronDown size={14} aria-hidden />
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ "aria-label": name })}>
          {tabs.map((tab) => <OverflowItem key={tab.id} tab={tab} label={tabLabel(tab)} onSelect={() => onSelect(tab.id)} />)}
        </div>
      </MenuFloating>
    </>
  );
}

function SwitcherItem({ tab, selected, label, onSelect }: { tab: CanvasTab; selected: boolean; label: string; onSelect: () => void }) {
  const props = useMenuItem({ label: tab.title, onSelect, role: "menuitemradio", checked: selected });
  return (
    <button type="button" {...props} aria-label={label}>
      <span className={styles.menuItemIcon} aria-hidden><CanvasObjectIcon kind={tab.kind} /></span>
      <span className={styles.menuItemText}><span>{tab.title}</span></span>
      {tab.marker ? <ObjectMarker marker={tab.marker} /> : null}
      {tab.statusLabel ? <span className={styles.menuItemTrailing}>{tab.statusLabel}</span> : null}
    </button>
  );
}

/** Narrow (< 480) title switcher: current object + chevron; the list opens as a bottom sheet on phones. */
function TitleSwitcher({ tabs, selected, onSelect, openMenu, tabLabel, label }: {
  tabs: readonly CanvasTab[]; selected: CanvasTab | null; onSelect: (id: string) => void;
  openMenu?: CanvasOpenMenuModel | null; tabLabel: (tab: CanvasTab) => string; label: string;
}) {
  const t = useTranslations("settings_canvas");
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start", maxWidth: 360, presentation: "auto", sheetTitle: label });
  const current: ReactNode = selected ? <>
    <span className={styles.tabIcon}><CanvasObjectIcon kind={selected.kind} /></span>
    <span className={styles.tabName}>{selected.title}</span>
    {selected.marker ? <ObjectMarker marker={selected.marker} /> : null}
  </> : <span className={styles.tabName}>{label}</span>;
  return (
    <>
      <button
        type="button"
        ref={menu.refs.setReference}
        className={styles.switcher}
        aria-label={selected ? `${tabLabel(selected)} · ${label}` : label}
        {...menu.getReferenceProps()}
      >
        {current}
        <span className={styles.switcherChevron}><ChevronDown size={14} aria-hidden /></span>
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ "aria-label": label })}>
          {tabs.map((tab) => <SwitcherItem key={tab.id} tab={tab} selected={tab.id === selected?.id} label={tabLabel(tab)} onSelect={() => onSelect(tab.id)} />)}
          {hasOpenMenuItems(openMenu) ? <>
            <div role="separator" className="ui-row-menu-sep" />
            <MenuSubmenu label={t("open_menu")}>
              <CanvasOpenMenuItems model={openMenu} />
            </MenuSubmenu>
          </> : null}
        </div>
      </MenuFloating>
    </>
  );
}

/** ⌘⇧[ / ⌘⇧] (Ctrl on other platforms) switch to the previous / next object. */
export function canvasSwitchShortcut(event: { key: string; code?: string; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean }): -1 | 1 | 0 {
  if (!event.shiftKey || !(event.metaKey || event.ctrlKey)) return 0;
  if (event.code === "BracketLeft" || event.key === "[" || event.key === "{") return -1;
  if (event.code === "BracketRight" || event.key === "]" || event.key === "}") return 1;
  return 0;
}
