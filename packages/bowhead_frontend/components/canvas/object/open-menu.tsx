"use client";

import { useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Bot, ChevronRight, AlertCircle, FileText, Plus, RefreshCw, RotateCcw } from "@/components/icons";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { Spinner } from "./status-badge";
import styles from "./canvas-object.module.css";

export interface CanvasOpenDocument {
  id: string;
  title: string;
  /** Second line: status · scale (e.g. “Ready for your review · 20 slides”). */
  detail?: string;
}

export interface CanvasOpenDocuments {
  items: readonly CanvasOpenDocument[];
  loading: boolean;
  error: boolean;
  hasPrevious: boolean;
  hasNext: boolean;
  onOpen: (id: string) => void;
  onRefresh: () => void;
  onPrevious: () => void;
  onNext: () => void;
}

export interface CanvasOpenMenuModel {
  onNewAgent?: () => void;
  documents?: CanvasOpenDocuments;
}

export function hasOpenMenuItems(model: CanvasOpenMenuModel | null | undefined): model is CanvasOpenMenuModel {
  return Boolean(model && (model.onNewAgent || model.documents));
}

function Item({ icon, label, detail, trailing, onSelect, disabled }: {
  icon?: ReactNode; label: string; detail?: string; trailing?: ReactNode; onSelect: () => void; disabled?: boolean;
}) {
  const props = useMenuItem({ label, onSelect, disabled });
  return (
    <button type="button" {...props}>
      <span className={styles.menuItemIcon} aria-hidden>{icon}</span>
      <span className={styles.menuItemText}>
        <span>{label}</span>
        {detail ? <small data-menu-description="">{detail}</small> : null}
      </span>
      {trailing ? <span className={styles.menuItemTrailing} aria-hidden>{trailing}</span> : null}
    </button>
  );
}

/**
 * Items of the C1 “Open” menu, rendered inside any MenuFloating (the “+” menu, or the narrow title
 * switcher's “Open…” submenu). Loading, failure and empty states are message rows, not disabled items.
 */
export function CanvasOpenMenuItems({ model }: { model: CanvasOpenMenuModel }) {
  const t = useTranslations("settings_canvas");
  const td = useTranslations("documents");
  const documents = model.documents;
  return (
    <>
      {model.onNewAgent ? <Item icon={<Bot size={14} />} label={t("new_agent")} onSelect={model.onNewAgent} /> : null}
      {model.onNewAgent && documents ? <div role="separator" className="ui-row-menu-sep" /> : null}
      {documents ? (
        <div role="group" aria-label={td("canvas_menu_group")}>
          <div className={styles.menuGroupTitle} aria-hidden>{td("canvas_menu_group")}</div>
          {documents.items.map((document) => (
            <Item key={document.id} icon={<FileText size={14} />} label={document.title} detail={document.detail}
              onSelect={() => documents.onOpen(document.id)} />
          ))}
          {documents.loading ? (
            <div className={styles.menuMessage} role="status"><span className={styles.menuItemIcon}><Spinner /></span>{td("canvas_loading_list")}</div>
          ) : documents.error ? (
            <div className={styles.menuMessage} data-tone="error" role="alert"><span className={styles.menuItemIcon}><AlertCircle size={14} aria-hidden /></span>{td("canvas_list_error")}</div>
          ) : !documents.items.length ? (
            <div className={styles.menuMessage} role="status">{td("canvas_no_documents")}</div>
          ) : null}
          {documents.error ? <Item icon={<RotateCcw size={14} />} label={td("canvas_retry_list")} onSelect={documents.onRefresh} /> : null}
          {documents.hasPrevious ? <Item label={td("canvas_menu_previous")} disabled={documents.loading} onSelect={documents.onPrevious} /> : null}
          {documents.hasNext ? <Item label={td("canvas_menu_next")} trailing={<ChevronRight size={14} />} disabled={documents.loading} onSelect={documents.onNext} /> : null}
          {!documents.error ? <Item icon={<RefreshCw size={14} />} label={td("canvas_refresh_list")} disabled={documents.loading} onSelect={documents.onRefresh} /> : null}
        </div>
      ) : null}
    </>
  );
}

/** C1 · “+” icon button (tooltip “Open…”) with the Open menu. */
export function CanvasOpenMenu({ model, className }: { model: CanvasOpenMenuModel; className?: string }) {
  const t = useTranslations("settings_canvas");
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-end", maxWidth: 360 });
  return (
    <>
      <button
        type="button"
        ref={menu.refs.setReference}
        className={className ?? styles.iconButton}
        aria-label={t("open_menu")}
        title={t("open_menu")}
        {...menu.getReferenceProps()}
      >
        <Plus size={16} aria-hidden />
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ "aria-label": t("open_menu") })}>
          <CanvasOpenMenuItems model={model} />
        </div>
      </MenuFloating>
    </>
  );
}
