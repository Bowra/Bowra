"use client";

/**
 * The deck editor's parts in the shared editor shell (docx inline edit design §8.1, PPT design
 * §13.8; canvas artboards GFilm, GPanelChanges, GPanelKeys): the page strip, the list of unsaved
 * changes and the shortcuts panel. The editor owns every state; these only show it.
 */

import { type KeyboardEvent, type ReactNode, useRef } from "react";
import { useTranslations } from "next-intl";
import { MessageSquareText as LucideMessageSquareText } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { Check } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { ChangeRow } from "@/components/canvas/object";
import { LazyPdfPageCanvas } from "./pdf-page";
import type { PdfDocumentLike } from "./pdf-page";
import styles from "./presentation-editor.module.css";

export function PageStrip({ pdf, count, page, aspect, pendingPages, referenced, onReference, onSelect, filename }: {
  pdf: PdfDocumentLike | null;
  count: number;
  page: number;
  aspect: number;
  pendingPages: ReadonlySet<number>;
  referenced: (page: number) => boolean;
  /** 「引用这一页」 on a page; absent where pages cannot be referenced. */
  onReference?: (page: number) => void;
  onSelect: (page: number) => void;
  filename: string;
}) {
  const t = useTranslations("presentation_edit");
  const listRef = useRef<HTMLOListElement>(null);
  const move = (event: KeyboardEvent<HTMLButtonElement>, number: number) => {
    // The strip is one Tab stop; the arrow keys, Home and End move between pages.
    const next = event.key === "ArrowDown" || event.key === "ArrowRight" ? number + 1
      : event.key === "ArrowUp" || event.key === "ArrowLeft" ? number - 1
        : event.key === "Home" ? 1 : event.key === "End" ? count : null;
    if (next === null || next < 1 || next > count) return;
    event.preventDefault();
    onSelect(next);
    requestAnimationFrame(() => listRef.current?.querySelector<HTMLElement>(`[data-rail-page="${next}"]`)?.focus());
  };
  return (
    <nav className={styles.strip} aria-label={t("pages")}>
      <ol className={styles.stripList} ref={listRef}>
        {Array.from({ length: count }, (_, index) => index + 1).map((number) => {
          const current = number === page;
          const isReferenced = referenced(number);
          const label = [t("page_label", { page: number }), pendingPages.has(number) ? t("strip_unsaved") : null, isReferenced ? t("referenced_page") : null]
            .filter(Boolean).join(t("strip_separator"));
          return (
            <li key={number} className={styles.stripItem} data-current={current || undefined}>
              <span className={styles.stripNumber} aria-hidden="true">
                {number}
                {pendingPages.has(number) ? <span className={styles.stripDot} /> : null}
              </span>
              <span className={styles.stripThumb}>
                <button type="button" className={styles.stripPage} aria-current={current ? "page" : undefined} aria-label={label}
                  data-rail-page={number} tabIndex={current ? 0 : -1} onKeyDown={(event) => move(event, number)} onClick={() => onSelect(number)}>
                  <LazyPdfPageCanvas pdf={pdf} pageNumber={number} width={140} aspectRatio={aspect} className={styles.railCanvas}
                    label={`${filename} · ${t("page_label", { page: number })}`} />
                </button>
                {onReference ? (
                  <Button size="sm" variant="secondary" className={styles.stripReference} data-shown={isReferenced || undefined}
                    tabIndex={current ? 0 : -1} aria-pressed={isReferenced}
                    aria-label={t(isReferenced ? "referenced_page_label" : "reference_page_label", { page: number })}
                    onClick={() => onReference(number)}>
                    {isReferenced ? <Check size={12} aria-hidden="true" /> : <LucideGlyph icon={LucideMessageSquareText} size={12} aria-hidden="true" />}
                    {t(isReferenced ? "referenced_page" : "reference_page")}
                  </Button>
                ) : null}
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export type ChangeListRow = {
  id: string;
  number: number;
  page: number;
  name: string;
  where: string;
  before: string;
  after: string;
  /** Why the server refused it, or a value that is not a number. */
  error?: string | null;
  note?: string | null;
  /** 「去修改」: open it again on the page. */
  onOpen?: () => void;
  onUndo?: () => void;
};

/** The unsaved changes, by page, each with its number on the page and its own undo (canvas GPanelChanges). */
export function PendingChangeList({ rows }: { rows: ReadonlyArray<ChangeListRow> }) {
  const t = useTranslations("presentation_edit");
  const pages = [...new Set(rows.map((row) => row.page))].sort((a, b) => a - b);
  if (!rows.length) return <p className={styles.panelHint}>{t("changes_empty")}</p>;
  return (
    <>
      {pages.map((page) => (
        <section key={page} className={styles.changeGroup} aria-label={t("page_label", { page })}>
          <h3 className={styles.changeGroupTitle}>{t("page_label", { page })}</h3>
          {rows.filter((row) => row.page === page).map((row) => (
            <ChangeRow key={row.id} id={`change-${row.id}`}
              location={<span className={styles.changeLocation}><span className={styles.changeNumber} aria-hidden="true">{row.number}</span>{row.name}</span>}
              locationNote={row.where}
              before={row.before ? { kind: "text", text: row.before } : { kind: "empty" }}
              after={row.after ? { kind: "text", text: row.after } : { kind: "empty" }}
              afterLabel={t("change_to")}
              trailing={row.onUndo ? (
                <Button size="sm" variant="plain" onClick={row.onUndo} aria-label={t("change_undo_label", { number: row.number })}>{t("change_undo")}</Button>
              ) : undefined}
              rows={row.note ? [{ label: "", value: row.note }] : undefined}>
              {row.error ? (
                <p className="settings-field__error" role="alert">
                  {row.error}
                  {row.onOpen ? <Button size="sm" variant="plain" onClick={row.onOpen}>{t("go_edit")}</Button> : null}
                </p>
              ) : null}
            </ChangeRow>
          ))}
        </section>
      ))}
    </>
  );
}

/** 「快捷键与说明」: the keys of the editor, as the toolbar hints say them (canvas GPanelKeys). */
export function KeysPanelBody({ groups, note }: {
  groups: ReadonlyArray<{ title: string; rows: ReadonlyArray<{ what: string; keys: string }> }>;
  note: ReactNode;
}) {
  return (
    <>
      {groups.map((group) => (
        <section key={group.title} className={styles.keyGroup} aria-label={group.title}>
          <h3 className={styles.changeGroupTitle}>{group.title}</h3>
          <dl className={styles.keyList}>
            {group.rows.map((row) => (
              <div key={row.what} className={styles.keyRow}>
                <dt>{row.what}</dt>
                <dd><kbd>{row.keys}</kbd></dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
      <p className={styles.panelHint}>{note}</p>
    </>
  );
}
