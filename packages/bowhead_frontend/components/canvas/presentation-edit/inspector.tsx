"use client";

import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Chat, Check, Lock, Undo } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { ReviewLine } from "@/components/ui/review-line";
import { ChangeRow, StateBlock } from "@/components/canvas/object";
import {
  countFieldTokens,
  findRepeatedValue,
  formatChartValue,
  pendingEditId,
  type InventoryElement,
  type InventorySlide,
  type PendingEdit,
} from "@/lib/presentation-edit";
import { isComposing } from "./keys";
import styles from "./presentation-editor.module.css";

type Translate = (key: string, values?: Record<string, string | number>) => string;

const EXCERPT = 28;

export function excerpt(text: string, fieldToken: string, limit = EXCERPT): string {
  const flat = text.split(fieldToken).join("#").replace(/\s+/g, " ").trim();
  return Array.from(flat).length > limit ? `${Array.from(flat).slice(0, limit).join("")}…` : flat;
}

/** What a person recognizes an element by: its text, its table size, or its name. */
export function elementLabel(element: InventoryElement, t: Translate, fieldToken: string, withSize = true): string {
  if (element.kind === "text") {
    const first = element.paragraphs?.find((paragraph) => paragraph.text.trim())?.text;
    if (first) return excerpt(first, fieldToken);
  }
  if (element.kind === "table" && element.rows?.length) {
    const first = element.rows.flat().find((cell) => cell.text.trim())?.text;
    const size = t("table_size", { rows: element.rows.length, cols: element.rows[0]?.length ?? 0 });
    if (first && !withSize) return excerpt(first, fieldToken);
    return first ? `${excerpt(first, fieldToken, 16)} · ${size}` : size;
  }
  return element.name || t(`kind_${element.kind}`);
}

function kindLabel(element: InventoryElement, t: Translate): string {
  return t(`kind_${element.kind}`);
}

export function ElementList({
  slide, pending, numbers, fieldToken, keyboardHint, onSelect, onReferencePage, pageReferenced = false, hideHeading = false,
}: {
  slide: InventorySlide;
  pending: ReadonlyArray<PendingEdit>;
  /** Each changed element's number, matching the outline on the slide and the change list. */
  numbers?: ReadonlyMap<string, number>;
  fieldToken: string;
  keyboardHint?: string;
  onSelect: (key: string) => void;
  /** Reference the whole slide in chat, or stop referencing it (design §13.2, §13.6). */
  onReferencePage?: () => void;
  /** The slide is in the Composer: the button reads 「已引用」 and is pressed. */
  pageReferenced?: boolean;
  /** In a sheet whose title already names the slide. */
  hideHeading?: boolean;
}) {
  const t = useTranslations("presentation_edit");
  const pendingKeys = new Set(pending.map((edit) => edit.key));
  return (
    <div className={styles.panel}>
      {hideHeading ? null : (
        <div className={styles.listHead}>
          <div className={styles.panelHead}>
            <h3 className={styles.panelTitle}>{t("page_list_title", { page: slide.index })}</h3>
            <p className={styles.panelHint}>{t("page_list_hint")}</p>
          </div>
          {onReferencePage ? (
            <Button size="sm" variant="secondary" onClick={onReferencePage} aria-pressed={pageReferenced}
              aria-label={t(pageReferenced ? "referenced_page_label" : "reference_page_label", { page: slide.index })}>
              {pageReferenced ? <Check size={13} aria-hidden="true" /> : <Chat size={13} aria-hidden="true" />}
              {t(pageReferenced ? "referenced_page" : "reference_page")}
            </Button>
          ) : null}
        </div>
      )}
      {slide.elements.length ? (
        <ul className={styles.elementList}>
          {slide.elements.map((element) => (
            <li key={element.key}>
              <button type="button" className={styles.elementRow} onClick={() => onSelect(element.key)} data-editable={element.editable || undefined}
                data-list-key={element.key}>
                <span className={styles.elementName}>{elementLabel(element, t, fieldToken)}</span>
                <span className={styles.elementBadges}>
                  {pendingKeys.has(element.key) ? (
                    numbers?.has(element.key)
                      ? <Badge tone="warning" size="sm"><span aria-hidden="true">{numbers.get(element.key)}</span><span className="sr-only">{t("unsaved_numbered", { number: numbers.get(element.key) ?? 0 })}</span></Badge>
                      : <Badge tone="warning" size="sm">{t("unsaved_badge")}</Badge>
                  ) : null}
                  {element.editable
                    ? <Badge tone="neutral" size="sm">{kindLabel(element, t)}</Badge>
                    : <Badge tone="outline" size="sm">{t("unsupported_badge")}</Badge>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : <p className={styles.panelHint}>{t("page_empty")}</p>}
      {slide.inherited_text ? <p className={styles.panelNote}>{t("inherited_text_note")}</p> : null}
      {keyboardHint ? <p className={styles.keyboardHint}>{keyboardHint}</p> : null}
    </div>
  );
}

function PanelHeader({ element, page, meta, onBack, onReference, t, fieldToken }: {
  element: InventoryElement; page: number; meta?: string; onBack?: () => void; onReference?: () => void; t: Translate; fieldToken: string;
}) {
  return (
    <div className={styles.panelHead}>
      <span className={styles.panelHeadActions}>
        {onBack ? <Button size="sm" variant="plain" className={styles.backButton} onClick={onBack}>{t("back_to_list")}</Button> : <span />}
        {onReference ? <Button size="sm" variant="plain" onClick={onReference}>{t("reference_element")}</Button> : null}
      </span>
      <span className={styles.panelKind}><Badge tone="neutral" size="sm">{kindLabel(element, t)}</Badge></span>
      <h3 className={styles.panelTitle}>{elementLabel(element, t, fieldToken, !meta)}</h3>
      <p className={styles.panelHint}>{meta ?? t("page_label", { page })}</p>
    </div>
  );
}

function refusalMessage(code: string | undefined, t: Translate): string | null {
  if (!code) return null;
  if (code === "target_changed" || code === "field_changed" || code === "not_a_number") return t(`refused_${code}`);
  return t("refused_other");
}

function UndoButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <Button size="sm" variant="plain" onClick={onClick} aria-label={label}>
      <Undo size={13} aria-hidden="true" />
      {label}
    </Button>
  );
}

export function TextPanel({
  element, page, pending, refusals, fieldToken, onChange, onUndo, onBack, onReference, inPlace = false, sheet = false,
}: {
  element: InventoryElement;
  page: number;
  pending: ReadonlyArray<PendingEdit>;
  refusals: Readonly<Record<string, string>>;
  fieldToken: string;
  onChange: (paragraph: number, text: string) => void;
  onUndo: (id: string) => void;
  onBack: () => void;
  onReference?: () => void;
  /** The text is edited on the slide (desktop, design §13.3): no fields here. */
  inPlace?: boolean;
  /** Inside a phone sheet, whose footer holds 「撤销这处」 and 「完成」. */
  sheet?: boolean;
}) {
  const t = useTranslations("presentation_edit");
  const baseId = useId();
  const edits = pending.filter((edit): edit is Extract<PendingEdit, { kind: "text" }> => edit.kind === "text" && edit.key === element.key);
  const paragraphs = element.paragraphs ?? [];
  const shown = paragraphs.map((paragraph, index) => ({ paragraph, index })).filter(({ paragraph }) => paragraph.text.trim() || paragraph.fields);
  const mixed = paragraphs.some((paragraph) => paragraph.mixed_style);
  return (
    <div className={styles.panel}>
      <PanelHeader element={element} page={page} onBack={sheet ? undefined : onBack} onReference={onReference} t={t} fieldToken={fieldToken} />
      {inPlace ? (
        <>
          <p className={styles.panelHint}>{t("in_place_text_hint")}</p>
          {mixed ? <p className={styles.panelNote}>{t("mixed_style_hint")}</p> : null}
          {edits.some((edit) => countFieldTokens(edit.text, fieldToken) !== countFieldTokens(edit.expect, fieldToken))
            ? <p className="settings-field__error" role="alert">{t("field_error", { token: fieldToken })}</p> : null}
          {edits.map((edit) => {
            const refused = refusalMessage(refusals[pendingEditId(edit)], t);
            return refused ? <p key={pendingEditId(edit)} className="settings-field__error" role="alert">{refused}</p> : null;
          })}
        </>
      ) : <div className={styles.fields}>
        {shown.map(({ paragraph, index }) => {
          const id = `${baseId}-p${index}`;
          const edit = edits.find((item) => item.paragraph === index);
          const value = edit?.text ?? paragraph.text;
          const fieldBroken = countFieldTokens(value, fieldToken) !== paragraph.fields;
          const refused = refusalMessage(refusals[pendingEditId({ kind: "text", key: element.key, paragraph: index })], t);
          const error = fieldBroken ? t("field_error", { token: fieldToken }) : refused;
          const hints = [
            paragraph.mixed_style ? t("mixed_style_hint") : null,
            paragraph.fields ? t("field_hint", { token: fieldToken }) : null,
          ].filter(Boolean);
          return (
            <Field key={index} id={id} label={shown.length > 1 ? t("paragraph_label", { number: index + 1 }) : t("text_label")}
              hint={hints.length ? hints.join(" ") : undefined} error={error ?? undefined}>
              <textarea
                id={id}
                className={`settings-input ${styles.textArea}`}
                value={value}
                rows={Math.min(8, Math.max(2, value.split("\n").length + Math.ceil(value.length / 28)))}
                aria-invalid={error ? true : undefined}
                aria-describedby={fieldDescribedBy(id, { hint: hints.length > 0, error: Boolean(error) })}
                data-autofocus={index === shown[0]?.index || undefined}
                onChange={(event) => onChange(index, event.target.value)}
              />
            </Field>
          );
        })}
      </div>}
      <ElementChanges title={t("changes_here")} rows={edits.map((edit) => ({
        id: pendingEditId(edit),
        location: shown.length > 1 ? t("paragraph_label", { number: edit.paragraph + 1 }) : t("text_label"),
        before: excerpt(edit.expect, fieldToken, 120),
        after: excerpt(edit.text, fieldToken, 120),
      }))} onUndo={sheet ? undefined : onUndo} undoLabel={t("undo_this")} />
    </div>
  );
}

function ElementChanges({ title, rows, onUndo, undoLabel }: {
  title: string;
  rows: Array<{ id: string; location: ReactNode; before: string; after: string }>;
  /** Absent in a phone sheet, whose footer undoes the element. */
  onUndo?: (id: string) => void;
  undoLabel: string;
}) {
  if (!rows.length) return null;
  return (
    <section className={styles.changes} aria-label={title}>
      <h4 className={styles.changesTitle}>{title}</h4>
      {rows.map((row) => (
        <ChangeRow
          key={row.id}
          location={row.location}
          before={row.before ? { kind: "text", text: row.before } : { kind: "empty" }}
          after={row.after ? { kind: "text", text: row.after } : { kind: "empty" }}
          trailing={onUndo ? <UndoButton onClick={() => onUndo(row.id)} label={undoLabel} /> : undefined}
        />
      ))}
    </section>
  );
}

/**
 * Arrow keys move between cells (left and right at the ends of the text), Enter moves down. Locked
 * cells take focus too, so a screen reader says why they cannot change.
 */
function moveInGrid(event: KeyboardEvent<HTMLElement>, grid: HTMLElement | null) {
  if (isComposing(event)) return;
  const cell = event.currentTarget;
  const row = Number(cell.dataset.row);
  const col = Number(cell.dataset.col);
  let target: [number, number] | null = null;
  const input = cell instanceof HTMLInputElement ? cell : null;
  const atStart = !input || (input.selectionStart === 0 && input.selectionEnd === 0);
  const atEnd = !input || (input.selectionStart === input.value.length && input.selectionEnd === input.value.length);
  if (event.key === "ArrowUp") target = [row - 1, col];
  else if (event.key === "ArrowDown" || event.key === "Enter") target = [row + 1, col];
  else if (event.key === "ArrowLeft" && atStart) target = [row, col - 1];
  else if (event.key === "ArrowRight" && atEnd) target = [row, col + 1];
  if (!target || !grid) return;
  const next = grid.querySelector<HTMLElement>(`[data-row="${target[0]}"][data-col="${target[1]}"]`);
  if (!next) return;
  event.preventDefault();
  next.focus();
  if (next instanceof HTMLInputElement) next.select();
}

/** A cell's accessible name: its place and, below the first row, the column's heading. */
function cellName(rows: ReadonlyArray<ReadonlyArray<{ text: string }>>, row: number, col: number, t: Translate, fieldToken: string): string {
  const place = t("cell_label", { row: row + 1, col: col + 1 });
  const heading = row > 0 ? excerpt(rows[0]?.[col]?.text ?? "", fieldToken, 24) : "";
  return heading ? `${place} · ${heading}` : place;
}

export function TablePanel({
  element, page, pending, refusals, fieldToken, onChange, onUndo, onBack, onReference, inPlace = false, sheet = false, rowForm = false, focusCell,
}: {
  element: InventoryElement;
  page: number;
  pending: ReadonlyArray<PendingEdit>;
  refusals: Readonly<Record<string, string>>;
  fieldToken: string;
  onChange: (row: number, col: number, text: string) => void;
  onUndo: (id: string) => void;
  onBack: () => void;
  onReference?: () => void;
  /** Cells are edited on the slide (desktop, design §13.3): no grid here. */
  inPlace?: boolean;
  /** Inside a phone sheet, whose footer holds 「撤销这处」 and 「完成」. */
  sheet?: boolean;
  /** A table too wide for a phone is edited one row at a time. */
  rowForm?: boolean;
  /** The cell the person clicked, which takes focus. */
  focusCell?: readonly [number, number] | null;
}) {
  const t = useTranslations("presentation_edit");
  const baseId = useId();
  const gridRef = useRef<HTMLTableElement>(null);
  const [formRow, setFormRow] = useState<number | null>(null);
  const rows = element.rows ?? [];
  const cols = rows[0]?.length ?? 0;
  const edits = pending.filter((edit): edit is Extract<PendingEdit, { kind: "cell" }> => edit.kind === "cell" && edit.key === element.key);
  const errors = edits.flatMap((edit) => {
    const message = countFieldTokens(edit.text, fieldToken) !== countFieldTokens(edit.expect, fieldToken)
      ? t("field_error", { token: fieldToken })
      : refusalMessage(refusals[pendingEditId(edit)], t);
    return message ? [{ edit, message }] : [];
  });
  const focus = focusCell && rows[focusCell[0]]?.[focusCell[1]]?.editable ? focusCell : null;
  const firstEditable = (() => {
    for (let row = 0; row < rows.length; row += 1) for (let col = 0; col < cols; col += 1) if (rows[row][col]?.editable) return [row, col] as const;
    return null;
  })();
  const autofocusAt = focus ?? firstEditable;
  const changes = <ElementChanges title={t("table_changes")} rows={edits.map((edit) => ({
    id: pendingEditId(edit),
    location: cellName(rows, edit.row, edit.col, t, fieldToken),
    before: excerpt(edit.expect, fieldToken, 120),
    after: excerpt(edit.text, fieldToken, 120),
  }))} onUndo={sheet ? undefined : onUndo} undoLabel={t("undo_this")} />;
  const errorLines = errors.map(({ edit, message }) => (
    <p key={pendingEditId(edit)} className="settings-field__error" role="alert">
      {t("cell_label", { row: edit.row + 1, col: edit.col + 1 })}{t("label_separator")}{message}
    </p>
  ));
  const header = <PanelHeader element={element} page={page} t={t} fieldToken={fieldToken} onBack={sheet ? undefined : onBack} onReference={onReference}
    meta={t("table_meta", { page, rows: rows.length, cols })} />;
  if (inPlace) {
    return (
      <div className={styles.panel}>
        {header}
        <p className={styles.panelHint}>{t("in_place_table_hint")}</p>
        {errorLines}
        <ReviewLine>{t("table_formula_note")}</ReviewLine>
        {rows.some((cells) => cells.some((cell) => !cell.editable)) ? <p className={styles.panelNote}>{t("table_locked_note")}</p> : null}
        {changes}
      </div>
    );
  }
  if (rowForm) {
    const heading = (col: number) => excerpt(rows[0]?.[col]?.text ?? "", fieldToken, 24) || t("column_label", { col: col + 1 });
    return (
      <div className={styles.panel}>
        {header}
        {formRow === null ? (
          <>
            <p className={styles.panelHint}>{t("row_form_hint", { cols })}</p>
            <ul className={styles.elementList}>
              {rows.map((cells, row) => (
                <li key={row}>
                  <button type="button" className={styles.elementRow} onClick={() => setFormRow(row)}>
                    <span className={styles.elementName}>{t("row_label", { row: row + 1 })} · {excerpt(cells.find((cell) => cell.text.trim())?.text ?? "", fieldToken, 20)}</span>
                    {edits.some((edit) => edit.row === row) ? <Badge tone="warning" size="sm">{t("unsaved_badge")}</Badge> : null}
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <div className={styles.fields}>
            <Button size="sm" variant="plain" className={styles.backButton} onClick={() => setFormRow(null)}>{t("back_to_rows")}</Button>
            {rows[formRow].map((cell, col) => {
              const id = `${baseId}-form-${formRow}-${col}`;
              if (!cell.editable) {
                return <p key={col} className={styles.panelNote}><strong>{heading(col)}</strong>{t("label_separator")}{t(`reason_${cell.reason ?? "multi_paragraph"}`)}</p>;
              }
              const edit = edits.find((item) => item.row === formRow && item.col === col);
              return (
                <Field key={col} id={id} label={heading(col)}>
                  <input id={id} className="settings-input" value={edit?.text ?? cell.text}
                    data-autofocus={col === rows[formRow].findIndex((item) => item.editable) || undefined}
                    onChange={(event) => onChange(formRow, col, event.target.value)} />
                </Field>
              );
            })}
          </div>
        )}
        {errorLines}
        <p className={styles.panelNote}>{t("table_formula_note")}</p>
        {changes}
      </div>
    );
  }
  return (
    <div className={styles.panel}>
      {header}
      <p className={styles.panelHint}>{t("table_hint")}</p>
      <div className={styles.gridFrame}>
        <table className={styles.grid} ref={gridRef}>
          <caption className="sr-only">{elementLabel(element, t, fieldToken)}</caption>
          <tbody>
            {rows.map((cells, row) => (
              <tr key={row}>
                {cells.map((cell, col) => {
                  const id = `${baseId}-${row}-${col}`;
                  const label = cellName(rows, row, col, t, fieldToken);
                  const Cell = row === 0 ? "th" : "td";
                  if (!cell.editable) {
                    return (
                      <Cell key={col} className={styles.lockedCell} {...(row === 0 ? { scope: "col" } : {})}>
                        <span aria-describedby={`${id}-reason`} tabIndex={-1} data-row={row} data-col={col}
                          onKeyDown={(event) => moveInGrid(event, gridRef.current)}>
                          <Lock size={11} aria-hidden="true" />
                          <span className="sr-only">{label}</span>
                          {cell.reason === "covered_by_merge" ? "" : excerpt(cell.text, fieldToken, 18)}
                        </span>
                        <span id={`${id}-reason`} className="sr-only">{t(`reason_${cell.reason ?? "multi_paragraph"}`)}</span>
                      </Cell>
                    );
                  }
                  const edit = edits.find((item) => item.row === row && item.col === col);
                  const value = edit?.text ?? cell.text;
                  const failed = errors.some((item) => item.edit === edit);
                  return (
                    <Cell key={col} data-pending={edit ? true : undefined} {...(row === 0 ? { scope: "col" } : {})}>
                      <input
                        id={id}
                        className={styles.cellInput}
                        value={value}
                        aria-label={label}
                        aria-invalid={failed || undefined}
                        data-row={row}
                        data-col={col}
                        data-autofocus={(autofocusAt?.[0] === row && autofocusAt?.[1] === col) || undefined}
                        onKeyDown={(event) => moveInGrid(event, gridRef.current)}
                        onChange={(event) => onChange(row, col, event.target.value)}
                      />
                    </Cell>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {errorLines}
      <p className={styles.panelNote}>{t("table_formula_note")}</p>
      {rows.some((cells) => cells.some((cell) => !cell.editable)) ? <p className={styles.panelNote}>{t("table_locked_note")}</p> : null}
      {changes}
    </div>
  );
}

/** The chart's values as a grid of fields, in the inspector, a phone sheet or the popover beside the chart. */
export function ChartGrid({
  element, pending, drafts, invalid, locale, fieldToken, onDraft,
}: {
  element: InventoryElement;
  pending: ReadonlyArray<PendingEdit>;
  drafts: ReadonlyArray<ReadonlyArray<string | undefined>> | undefined;
  invalid: ReadonlyArray<ReadonlyArray<boolean>>;
  locale: string;
  fieldToken: string;
  onDraft: (series: number, category: number, text: string) => void;
}) {
  const t = useTranslations("presentation_edit");
  const baseId = useId();
  const gridRef = useRef<HTMLTableElement>(null);
  // A value that does not parse is announced once the field is left, not on every keystroke.
  const [left, setLeft] = useState<ReadonlySet<string>>(() => new Set());
  const chart = element.chart;
  if (!chart) return null;
  const edit = pending.find((item): item is Extract<PendingEdit, { kind: "chart" }> => item.kind === "chart" && item.key === element.key);
  const values = edit?.values ?? chart.series.map((series) => series.values);
  const example = formatChartValue(chart.series[0]?.values.find((value) => value !== null) ?? 1250, chart.series[0]?.format, locale);
  return (
    <div className={styles.gridFrame}>
      <table className={styles.grid} ref={gridRef}>
        <caption className="sr-only">{elementLabel(element, t, fieldToken)}</caption>
        <thead>
          <tr>
            <th scope="col">{t("chart_category")}</th>
            {chart.series.map((series) => <th key={series.name} scope="col">{series.name}</th>)}
          </tr>
        </thead>
        <tbody>
          {chart.categories.map((category, c) => (
            <tr key={`${category}-${c}`}>
              <th scope="row">{category}</th>
              {chart.series.map((series, s) => {
                const id = `${baseId}-${s}-${c}`;
                const value = drafts?.[s]?.[c] ?? formatChartValue(values[s]?.[c] ?? null, series.format, locale);
                const bad = invalid[s]?.[c] ?? false;
                const shown = bad && left.has(id);
                const changedHere = (values[s]?.[c] ?? null) !== series.values[c];
                return (
                  <td key={series.name} data-pending={changedHere || undefined}>
                    <input
                      id={id}
                      className={styles.cellInput}
                      inputMode="decimal"
                      value={value}
                      aria-label={t("chart_value_label", { category, series: series.name })}
                      aria-invalid={bad || undefined}
                      aria-describedby={shown ? `${id}-error` : undefined}
                      data-autofocus={(s === 0 && c === 0) || undefined}
                      data-row={c}
                      data-col={s}
                      onKeyDown={(event) => moveInGrid(event, gridRef.current)}
                      onBlur={() => setLeft((current) => current.has(id) ? current : new Set(current).add(id))}
                      onFocus={() => setLeft((current) => {
                        if (!current.has(id)) return current;
                        const next = new Set(current);
                        next.delete(id);
                        return next;
                      })}
                      onChange={(event) => onDraft(s, c, event.target.value)}
                    />
                    {shown ? <span id={`${id}-error`} className="settings-field__error" role="alert">{t("not_a_number", { example })}</span> : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChartPanel({
  element, page, slide, pending, drafts, invalid, locale, fieldToken, onDraft, onUndo, onBack, onSelect, onReference, inPlace = false, sheet = false,
}: {
  element: InventoryElement;
  page: number;
  slide: InventorySlide;
  pending: ReadonlyArray<PendingEdit>;
  /** What the person typed, per series and category; `undefined` cells show the current value. */
  drafts: ReadonlyArray<ReadonlyArray<string | undefined>> | undefined;
  invalid: ReadonlyArray<ReadonlyArray<boolean>>;
  locale: string;
  fieldToken: string;
  onDraft: (series: number, category: number, text: string) => void;
  onUndo: (id: string) => void;
  onBack: () => void;
  onSelect: (key: string) => void;
  onReference?: () => void;
  /** The values are edited in a popover beside the chart (desktop, design §13.3). */
  inPlace?: boolean;
  /** Inside a phone sheet, whose footer holds 「撤销这处」 and 「完成」. */
  sheet?: boolean;
}) {
  const t = useTranslations("presentation_edit");
  const chart = element.chart;
  if (!chart) return null;
  const edit = pending.find((item): item is Extract<PendingEdit, { kind: "chart" }> => item.kind === "chart" && item.key === element.key);
  const values = edit?.values ?? chart.series.map((series) => series.values);
  const changed = chart.series.flatMap((series, s) => chart.categories.flatMap((category, c) => {
    const before = series.values[c];
    const after = values[s]?.[c] ?? null;
    return before === after ? [] : [{ series, category, before, after, s, c }];
  }));
  const texts = slide.elements.flatMap((item) => item.kind === "text" && item.editable && item.key !== element.key
    ? [{ key: item.key, text: (item.paragraphs ?? []).map((paragraph) => paragraph.text).join("\n") }]
    : []);
  const repeated = changed.flatMap((change) => change.before === null ? [] : findRepeatedValue(change.before, change.series.format, texts)
    .map((key) => ({ key, value: formatChartValue(change.before, change.series.format, "en-US"), id: `${key}:${change.s}:${change.c}` })));
  const firstRepeat = repeated[0];
  const repeatedElement = firstRepeat ? slide.elements.find((item) => item.key === firstRepeat.key) : undefined;
  return (
    <div className={styles.panel}>
      <PanelHeader element={element} page={page} t={t} fieldToken={fieldToken} onBack={sheet ? undefined : onBack} onReference={onReference}
        meta={t("chart_meta", { page, series: chart.series.length, categories: chart.categories.length })} />
      <p className={styles.panelHint}>{t(inPlace ? "in_place_chart_hint" : "chart_hint")}</p>
      {/* A value that is not a number holds the save back; say which one here too (design §13.7). */}
      {invalid.some((row) => row.some(Boolean)) ? (
        <p className="settings-field__error">
          {t("chart_invalid_values", {
            categories: chart.categories.filter((_, c) => invalid.some((row) => row[c])).map((category) => t("quoted", { text: category })).join(t("list_separator")),
          })}
        </p>
      ) : null}
      {inPlace ? null : <ChartGrid element={element} pending={pending} drafts={drafts} invalid={invalid} locale={locale} fieldToken={fieldToken} onDraft={onDraft} />}
      {firstRepeat && repeatedElement ? (
        <ReviewLine actions={[{ label: t("go_edit"), onSelect: () => onSelect(repeatedElement.key) }]}>
          {t("repeated_value", { name: elementLabel(repeatedElement, t, fieldToken), value: firstRepeat.value })}
        </ReviewLine>
      ) : null}
      {edit ? (
        <section className={styles.changes} aria-label={t("chart_changes")}>
          <h4 className={styles.changesTitle}>{t("chart_changes")}</h4>
          {changed.map((change) => (
            <ChangeRow
              key={`${change.s}:${change.c}`}
              location={chart.series.length > 1 ? `${change.category} · ${change.series.name}` : change.category}
              before={change.before === null ? { kind: "empty" } : { kind: "number", text: formatChartValue(change.before, change.series.format, locale) }}
              after={change.after === null ? { kind: "empty" } : { kind: "number", text: formatChartValue(change.after, change.series.format, locale) }}
            />
          ))}
          {sheet ? null : <UndoButton onClick={() => onUndo(pendingEditId(edit))} label={t("undo_chart")} />}
        </section>
      ) : null}
    </div>
  );
}

const UNSUPPORTED_TITLES: Record<string, string> = {
  picture: "unsupported_title_picture",
  smartart: "unsupported_title_smartart",
  ole: "unsupported_title_ole",
  chart_unsupported: "unsupported_title_chart",
  duplicate_shape_id: "unsupported_title_duplicate",
};

export function UnsupportedPanel({
  element, page, fieldToken, downloadHref, onReference, onReferenceElement, onBack, title: titleOverride, body: bodyOverride, phone = false,
}: {
  element: InventoryElement;
  page: number;
  fieldToken: string;
  downloadHref: string;
  onReference?: () => void;
  /** Reference this element with its slide (design §6.9); preferred over the file reference. */
  onReferenceElement?: () => void;
  onBack: () => void;
  title?: string;
  body?: string;
  phone?: boolean;
}) {
  const t = useTranslations("presentation_edit");
  const title = titleOverride ?? t(UNSUPPORTED_TITLES[element.reason ?? element.kind] ?? "unsupported_title_other");
  const name = elementLabel(element, t, fieldToken);
  const reference = onReferenceElement
    ? { label: t("reference_element"), onClick: onReferenceElement, note: t("reference_element_note", { page, name }) }
    : onReference ? { label: t("reference_file"), onClick: onReference, note: t("reference_note", { page }) } : null;
  return (
    <div className={styles.panel}>
      <Button size="sm" variant="plain" className={styles.backButton} onClick={onBack}>{t("back_to_list")}</Button>
      <StateBlock
        title={title}
        body={bodyOverride ?? t(element.reason === "duplicate_shape_id" ? "unsupported_body_duplicate" : "unsupported_body", { name, page })}
        primaryAction={reference ? { label: reference.label, onClick: reference.onClick } : undefined}
        secondaryActions={[{ label: t("download"), onClick: () => { window.open(downloadHref, "_blank", "noopener"); } }]}
        note={reference?.note}
      />
      <section className={styles.changes}>
        <h4 className={styles.changesTitle}>{t("unsupported_list_title")}</h4>
        <ul className={styles.plainList}>
          {(["pictures", "smartart", "master", "notes", "layout"] as const).map((key) => <li key={key}>{t(`unsupported_list_${key}`)}</li>)}
        </ul>
      </section>
    </div>
  );
}
