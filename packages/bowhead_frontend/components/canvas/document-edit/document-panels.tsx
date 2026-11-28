"use client";

/**
 * The Word editor's outline and panel contents (canvas W1–W10): the outline of headings, 本页内容
 * with the paragraphs not found on the pages edited in place there, and the conflict rows. The
 * editor owns every state; these only show it.
 */

import { useId } from "react";
import { useTranslations } from "next-intl";
import { Lock as LucideLock, PanelLeftClose as LucidePanelLeftClose } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { ChangeRow } from "@/components/canvas/object";
import styles from "./document-editor.module.css";

export interface OutlineHeading {
  key: string;
  text: string;
  level: number;
  page: number | null;
  dirty: boolean;
}

export function DocumentOutline({ headings, currentKey, pageCount, onSelect, onHide }: {
  headings: ReadonlyArray<OutlineHeading>;
  currentKey: string | null;
  pageCount: number;
  onSelect: (key: string) => void;
  onHide: () => void;
}) {
  const t = useTranslations("document_edit");
  return (
    <nav className={styles.outline} aria-label={t("outline")}>
      <div className={styles.outlineHead}>
        <h2 className={styles.outlineTitle}>{t("outline")}</h2>
        <button type="button" className="v2-icon-btn" aria-label={t("outline_hide")} title={t("outline_hide")} onClick={onHide}>
          <LucideGlyph icon={LucidePanelLeftClose} size={16} aria-hidden="true" />
        </button>
      </div>
      {headings.length ? (
        <ol className={styles.outlineList}>
          {headings.map((heading) => (
            <li key={heading.key}>
              <button type="button" className={styles.outlineItem} data-level={heading.level}
                aria-current={heading.key === currentKey ? "location" : undefined}
                aria-label={[
                  heading.text,
                  t("outline_level", { level: heading.level }),
                  heading.page ? t("page_label", { page: heading.page }) : null,
                  heading.dirty ? t("outline_unsaved") : null,
                ].filter(Boolean).join(t("label_list_separator"))}
                style={{ paddingLeft: 8 + Math.max(0, heading.level - 1) * 14 }}
                onClick={() => onSelect(heading.key)}>
                <span className={styles.outlineText}>{heading.text}</span>
                {heading.dirty ? <span className={styles.outlineDot} aria-hidden="true" /> : null}
                <span className={styles.outlinePage} aria-hidden="true">{heading.page ?? ""}</span>
              </button>
            </li>
          ))}
        </ol>
      ) : <p className={styles.outlineEmpty}>{t("outline_empty")}</p>}
      <div className={styles.outlineFoot}>{t("page_count", { count: pageCount })}</div>
    </nav>
  );
}

export interface ItemRow {
  key: string;
  name: string;
  kind: string;
  editable: boolean;
  number?: number;
  reason?: string;
  current: boolean;
}

export interface MissingRow {
  key: string;
  name: string;
  text: string;
  number?: number;
  error?: string | null;
  onChange: (text: string) => void;
}

/** 本页内容 (canvas W6, W7): the page's items in reading order; paragraphs not found on the pages are edited here. */
export function ItemsPanelBody({ items, missing, onSelect, intro, emptyText }: {
  items: ReadonlyArray<ItemRow>;
  missing: ReadonlyArray<MissingRow>;
  onSelect: (key: string) => void;
  /** Replaces the Tab-order intro (phones, where an item opens its sheet). */
  intro?: string;
  emptyText?: string;
}) {
  const t = useTranslations("document_edit");
  const baseId = useId();
  return (
    <>
      <p className={styles.itemsHint}>{intro ?? t("items_intro")}</p>
      {items.length ? (
        <ul className={styles.items}>
          {items.map((item) => (
            <li key={item.key}>
              <button type="button" className={styles.itemRow} data-locked={!item.editable || undefined} aria-current={item.current || undefined}
                aria-describedby={item.reason ? `${baseId}-${item.key}` : undefined} onClick={() => onSelect(item.key)}>
                <span className={styles.itemName}>
                  {!item.editable ? <LucideGlyph icon={LucideLock} size={14} aria-hidden="true" /> : null}
                  {item.name}
                </span>
                {item.number ? <Badge tone="warning" size="sm">{t("pending_badge", { number: item.number })}</Badge> : null}
                <Badge tone={item.editable ? "neutral" : "outline"} size="sm">{item.kind}</Badge>
                {item.reason ? <span id={`${baseId}-${item.key}`} className={styles.itemReason}>{item.reason}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : <p className={styles.itemsHint}>{emptyText ?? t("items_empty")}</p>}
      {missing.length ? (
        <section aria-labelledby={`${baseId}-missing`} className={styles.panelField}>
          <h3 id={`${baseId}-missing`} className={styles.itemsTitle}>{t("missing_title", { count: missing.length })}</h3>
          <p className={styles.itemsHint}>{t("missing_hint")}</p>
          {missing.map((row) => (
            <div key={row.key} className={styles.panelField} data-missing-key={row.key}>
              <label htmlFor={`${baseId}-field-${row.key}`} className={styles.itemName}>
                {row.name}
                {row.number ? <Badge tone="warning" size="sm">{t("pending_badge", { number: row.number })}</Badge> : null}
              </label>
              <textarea id={`${baseId}-field-${row.key}`} className={`settings-input ${styles.panelArea}`} value={row.text}
                aria-invalid={row.error ? true : undefined} aria-describedby={row.error ? `${baseId}-error-${row.key}` : undefined}
                onChange={(event) => row.onChange(event.target.value)} />
              {row.error ? <p id={`${baseId}-error-${row.key}`} className="settings-field__error" role="alert">{row.error}</p> : null}
            </div>
          ))}
        </section>
      ) : null}
    </>
  );
}

export interface ConflictRowView {
  id: string;
  location: string;
  before: string;
  after: string;
  applicable: boolean;
  /** What the newer version holds when it differs. */
  theirs: string | null;
  /** 「用我的」 is possible: the paragraph is still there and editable. */
  canChoose: boolean;
  choice: "mine" | "theirs" | null;
}

/** A refused save in 修改记录: the newer version's facts and each change's choice (design §7.2). */
export function ConflictBody({ message, version, rows, checking, failed, onChoose, onCopy, onOpenLatest, applicable }: {
  message: string;
  version: number;
  rows: ReadonlyArray<ConflictRowView>;
  checking: boolean;
  failed: boolean;
  onChoose: (id: string, choice: "mine" | "theirs") => void;
  onCopy: () => void;
  onOpenLatest: () => void;
  applicable: number;
}) {
  const t = useTranslations("document_edit");
  return (
    <>
      <p className="settings-field__error" role="alert">{message}</p>
      {checking ? <p className={styles.itemsHint} role="status">{t("conflict_checking", { version })}</p> : null}
      {failed ? <p className={styles.itemsHint} role="status">{t("conflict_check_failed", { version })}</p> : null}
      {rows.map((row) => (
        <div key={row.id} className={styles.panelField}>
          <ChangeRow location={row.location}
            badge={checking ? undefined : <Badge tone={row.applicable ? "positive" : "warning"} size="sm">{t(row.applicable ? "conflict_applicable" : "conflict_manual")}</Badge>}
            before={row.before ? { kind: "text", text: row.before } : { kind: "empty" }}
            after={{ kind: "text", text: row.after }}
            rows={row.theirs !== null ? [{ label: t("conflict_theirs_label", { version }), value: row.theirs }] : undefined} />
          {row.canChoose && !row.applicable ? (
            <SegmentedControl aria-label={t("conflict_choice_label", { location: row.location })} value={row.choice}
              onValueChange={(value) => onChoose(row.id, value as "mine" | "theirs")}>
              <Segment value="mine">{t("conflict_use_mine")}</Segment>
              <Segment value="theirs">{t("conflict_keep_theirs", { version })}</Segment>
            </SegmentedControl>
          ) : null}
        </div>
      ))}
      {applicable > 0 ? <p className={styles.itemsHint}>{t("reapply_note", { version, next: version + 1 })}</p> : null}
      <div className={styles.blockedActions}>
        <Button onClick={onCopy}>{t("copy_text")}</Button>
        <Button onClick={onOpenLatest}>{t("open_latest", { version })}</Button>
      </div>
    </>
  );
}
