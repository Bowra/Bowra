"use client";

import { useId, useLayoutEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import {
  CanvasObjectTabs,
  canvasPanelId,
  canvasSwitchShortcut,
  canvasTabId,
  type CanvasObjectKind,
  type CanvasObjectMarker,
  type CanvasOpenMenuModel,
  type CanvasTab,
} from "@/components/canvas/object";
import styles from "@/components/canvas/object/canvas-object.module.css";

export interface ConfigurationCanvasObject {
  key: string;
  title: string;
  kind: Exclude<CanvasObjectKind, "results">;
  content: ReactNode;
  /** C1 tab marker: unsaved, in progress, needs your action, failed. */
  marker?: CanvasObjectMarker | null;
  /** Short status word for the tooltip and screen readers (“Name · status”). */
  statusLabel?: string | null;
  /** The tab × is the only close for every object (decision D1). */
  onClose?: () => void;
}

export const CANVAS_RESULTS_TAB = "canvas-results";

/** The wide strip is hidden (display: none) in narrow containers; focus then goes to the panel. */
function rendered(element: HTMLElement): boolean {
  for (let node: HTMLElement | null = element; node; node = node.parentElement) {
    if (node.hidden || window.getComputedStyle(node).display === "none") return false;
  }
  return true;
}

/** Same-name objects get a readable ordinal (“· No. 2”), never an internal ID. */
function useCanvasTabs(objects: readonly ConfigurationCanvasObject[], onResults?: () => void): CanvasTab[] {
  const t = useTranslations("settings_canvas");
  const tabs: CanvasTab[] = objects.map((object) => {
    const matches = objects.filter((item) => item.title === object.title);
    const title = matches.length < 2 ? object.title : `${object.title} · ${t("duplicate_ordinal", { number: matches.indexOf(object) + 1 })}`;
    return { id: object.key, title, kind: object.kind, marker: object.marker ?? null, statusLabel: object.statusLabel ?? null, closable: Boolean(object.onClose) };
  });
  return onResults ? [{ id: CANVAS_RESULTS_TAB, title: t("results"), kind: "results", closable: false }, ...tabs] : tabs;
}

/**
 * The tab strip alone (Results selected), shown above the results pane while Canvas objects stay open,
 * so navigation does not change shape between Results and objects.
 */
export function ConfigurationCanvasStrip({ objects, onSelect, onResults, openMenu }: {
  objects: readonly ConfigurationCanvasObject[];
  onSelect: (key: string) => void;
  onResults: () => void;
  openMenu?: CanvasOpenMenuModel | null;
}) {
  const t = useTranslations("settings_canvas");
  const id = useId().replace(/[^A-Za-z0-9_-]/g, "_");
  const tabs = useCanvasTabs(objects, onResults);
  return (
    <div className={styles.strip}>
      <CanvasObjectTabs
        idBase={`canvas${id}`}
        tabs={tabs}
        selectedId={CANVAS_RESULTS_TAB}
        label={t("objects")}
        openMenu={openMenu}
        onSelect={(key) => { if (key !== CANVAS_RESULTS_TAB) onSelect(key); }}
        onClose={(key) => objects.find((object) => object.key === key)?.onClose?.()}
      />
    </div>
  );
}

/** The parent admits objects; this host only selects and retains their original content. */
export function ConfigurationCanvasHost({
  objects,
  selectedKey,
  onSelect,
  onResults,
  openMenu,
}: {
  objects: readonly ConfigurationCanvasObject[];
  selectedKey: string;
  onSelect: (key: string) => void;
  onResults?: () => void;
  openMenu?: CanvasOpenMenuModel | null;
}) {
  const t = useTranslations("settings_canvas");
  const id = `canvas${useId().replace(/[^A-Za-z0-9_-]/g, "_")}`;
  const host = useRef<HTMLDivElement>(null);
  const focused = useRef<HTMLElement | null>(null);
  const tabs = useCanvasTabs(objects, onResults);
  const objectKeys = JSON.stringify(objects.map((object) => object.key));
  useLayoutEffect(() => {
    // Restore focus only when its original control disappeared and no newer control took it.
    if (!focused.current || focused.current.isConnected || document.activeElement !== document.body) return;
    const tab = document.getElementById(canvasTabId(id, selectedKey));
    const target = tab && rendered(tab) ? tab
      : host.current?.querySelector<HTMLElement>('[role="tabpanel"]:not([hidden])');
    target?.focus({ preventScroll: true });
  }, [objectKeys, selectedKey, id]);
  const select = (key: string) => {
    if (key === CANVAS_RESULTS_TAB) onResults?.();
    else onSelect(key);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = canvasSwitchShortcut(event);
    if (!step || tabs.length < 2) return;
    event.preventDefault();
    const index = tabs.findIndex((tab) => tab.id === selectedKey);
    select(tabs[(index + step + tabs.length) % tabs.length].id);
  };
  return (
    <div className={styles.pane} data-canvas-pane="" ref={host} onKeyDown={onKeyDown} onFocusCapture={(event) => { focused.current = event.target; }}>
      <CanvasObjectTabs
        idBase={id}
        tabs={tabs}
        selectedId={selectedKey}
        label={t("objects")}
        openMenu={openMenu}
        onSelect={select}
        onClose={(key) => objects.find((object) => object.key === key)?.onClose?.()}
      />
      {objects.map((object) => (
        <div
          key={object.key}
          id={canvasPanelId(id, object.key)}
          role="tabpanel"
          aria-labelledby={canvasTabId(id, object.key)}
          hidden={object.key !== selectedKey}
          tabIndex={-1}
          className={styles.panel}
        >
          {object.content}
        </div>
      ))}
    </div>
  );
}
