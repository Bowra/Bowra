"use client";

import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Lock, Undo } from "@/components/icons";
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
  slide, pending, fieldToken, keyboardHint, onSelect, hideHeading = false,
}: {
  slide: InventorySlide;
  pending: ReadonlyArray<PendingEdit>;
  fieldToken: string;
  keyboardHint?: string;
  onSelect: (key: string) => void;
  /** In a sheet whose title already names the slide. */
  hideHeading?: boolean;
}) {
  const t = useTranslations("presentation_edit");
  const pendingKeys = new Set(pending.map((edit) => edit.key));
  return (
    <div className={styles.panel}>
      {hideHeading ? null : (
        <div className={styles.panelHead}>
          <h3 className={styles.panelTitle}>{t("page_list_title", { page: slide.index })}</h3>
          <p className={styles.panelHint}>{t("page_list_hint")}</p>
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
                  {pendingKeys.has(element.key) ? <Badge tone="warning" size="sm">{t("unsaved_badge")}</Badge> : null}
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
  element: InventoryElement; page: number; meta?: string; onBack: () => void; onReference?: () => void; t: Translate; fieldToken: string;
}) {
  return (
    <div className={styles.panelHead}>
      <span className={styles.panelHeadActions}>
        <Button size="sm" variant="plain" className={styles.backButton} onClick={onBack}>{t("back_to_list")}</Button>
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
  element, page, pending, refusals, fieldToken, onChange, onUndo, onBack, onReference,
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
}) {
  const t = useTranslations("presentation_edit");
  const baseId = useId();
  const edits = pending.filter((edit): edit is Extract<PendingEdit, { kind: "text" }> => edit.kind === "text" && edit.key === element.key);
  const paragraphs = element.paragraphs ?? [];
  const shown = paragraphs.map((paragraph, index) => ({ paragraph, index })).filter(({ paragraph }) => paragraph.text.trim() || paragraph.fields);
  return (
    <div className={styles.panel}>
      <PanelHeader element={element} page={page} onBack={onBack} onReference={onReference} t={t} fieldToken={fieldToken} />
      <div className={styles.fields}>
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
      </div>
      <ElementChanges title={t("changes_here")} rows={edits.map((edit) => ({
        id: pendingEditId(edit),
        location: shown.length > 1 ? t("paragraph_label", { number: edit.paragraph + 1 }) : t("text_label"),
        before: excerpt(edit.expect, fieldToken, 120),
        after: excerpt(edit.text, fieldToken, 120),
      }))} onUndo={onUndo} undoLabel={t("undo_this")} />
    </div>
  );
}

function ElementChanges({ title, rows, onUndo, undoLabel }: {
  title: string;
  rows: Array<{ id: string; location: ReactNode; before: string; after: string }>;
  onUndo: (id: string) => void;
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
          trailing={<UndoButton onClick={() => onUndo(row.id)} label={undoLabel} />}
        />
      ))}
    </section>
  );
}

/** Arrow keys move between cells (left and right at the ends of the text), Enter moves down. */
function moveInGrid(event: KeyboardEvent<HTMLInputElement>, grid: HTMLElement | null) {
  const input = event.currentTarget;
  const row = Number(input.dataset.row);
  const col = Number(input.dataset.col);
  let target: [number, number] | null = null;
  const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
  const atEnd = input.selectionStart === input.value.length && input.selectionEnd === input.value.length;
  if (event.key === "ArrowUp") target = [row - 1, col];
  else if (event.key === "ArrowDown" || (event.key === "Enter" && !event.nativeEvent.isComposing)) target = [row + 1, col];
  else if (event.key === "ArrowLeft" && atStart) target = [row, col - 1];
  else if (event.key === "ArrowRight" && atEnd) target = [row, col + 1];
  if (!target || !grid) return;
  const next = grid.querySelector<HTMLInputElement>(`input[data-row="${target[0]}"][data-col="${target[1]}"]`);
  if (!next) return;
  event.preventDefault();
  next.focus();
  next.select();
}

export function TablePanel({
  element, page, pending, refusals, fieldToken, onChange, onUndo, onBack, onReference,
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
}) {
  const t = useTranslations("presentation_edit");
  const baseId = useId();
  const gridRef = useRef<HTMLTableElement>(null);
  const rows = element.rows ?? [];
  const cols = rows[0]?.length ?? 0;
  const edits = pending.filter((edit): edit is Extract<PendingEdit, { kind: "cell" }> => edit.kind === "cell" && edit.key === element.key);
  const errors = edits.flatMap((edit) => {
    const message = countFieldTokens(edit.text, fieldToken) !== countFieldTokens(edit.expect, fieldToken)
      ? t("field_error", { token: fieldToken })
      : refusalMessage(refusals[pendingEditId(edit)], t);
    return message ? [{ edit, message }] : [];
  });
  return (
    <div className={styles.panel}>
      <PanelHeader element={element} page={page} t={t} fieldToken={fieldToken} onBack={onBack} onReference={onReference}
        meta={t("table_meta", { page, rows: rows.length, cols })} />
      <p className={styles.panelHint}>{t("table_hint")}</p>
      <div className={styles.gridFrame}>
        <table className={styles.grid} ref={gridRef}>
          <caption className="sr-only">{elementLabel(element, t, fieldToken)}</caption>
          <tbody>
            {rows.map((cells, row) => (
              <tr key={row}>
                {cells.map((cell, col) => {
                  const id = `${baseId}-${row}-${col}`;
                  const label = t("cell_label", { row: row + 1, col: col + 1 });
                  if (!cell.editable) {
                    return (
                      <td key={col} className={styles.lockedCell}>
                        <span aria-describedby={`${id}-reason`} tabIndex={-1}>
                          <Lock size={11} aria-hidden="true" />
                          <span className="sr-only">{label}</span>
                          {cell.reason === "covered_by_merge" ? "" : excerpt(cell.text, fieldToken, 18)}
                        </span>
                        <span id={`${id}-reason`} className="sr-only">{t(`reason_${cell.reason ?? "multi_paragraph"}`)}</span>
                      </td>
                    );
                  }
                  const edit = edits.find((item) => item.row === row && item.col === col);
                  const value = edit?.text ?? cell.text;
                  const failed = errors.some((item) => item.edit === edit);
                  return (
                    <td key={col} data-pending={edit ? true : undefined}>
                      <input
                        id={id}
                        className={styles.cellInput}
                        value={value}
                        aria-label={label}
                        aria-invalid={failed || undefined}
                        data-row={row}
                        data-col={col}
                        data-autofocus={(row === 0 && col === 0) || undefined}
                        onKeyDown={(event) => moveInGrid(event, gridRef.current)}
                        onChange={(event) => onChange(row, col, event.target.value)}
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {errors.map(({ edit, message }) => (
        <p key={pendingEditId(edit)} className="settings-field__error" role="alert">
          {t("cell_label", { row: edit.row + 1, col: edit.col + 1 })}：{message}
        </p>
      ))}
      <p className={styles.panelNote}>{t("table_formula_note")}</p>
      {rows.some((cells) => cells.some((cell) => !cell.editable)) ? <p className={styles.panelNote}>{t("table_locked_note")}</p> : null}
      <ElementChanges title={t("table_changes")} rows={edits.map((edit) => ({
        id: pendingEditId(edit),
        location: t("cell_label", { row: edit.row + 1, col: edit.col + 1 }),
        before: excerpt(edit.expect, fieldToken, 120),
        after: excerpt(edit.text, fieldToken, 120),
      }))} onUndo={onUndo} undoLabel={t("undo_this")} />
    </div>
  );
}

export function ChartPanel({
  element, page, slide, pending, drafts, invalid, locale, fieldToken, onDraft, onUndo, onBack, onSelect, onReference,
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
}) {
  const t = useTranslations("presentation_edit");
  const baseId = useId();
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
  const example = formatChartValue(chart.series[0]?.values.find((value) => value !== null) ?? 1250, chart.series[0]?.format, locale);
  return (
    <div className={styles.panel}>
      <PanelHeader element={element} page={page} t={t} fieldToken={fieldToken} onBack={onBack} onReference={onReference}
        meta={t("chart_meta", { page, series: chart.series.length, categories: chart.categories.length })} />
      <p className={styles.panelHint}>{t("chart_hint")}</p>
      <div className={styles.gridFrame}>
        <table className={styles.grid}>
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
                  return (
                    <td key={series.name} data-pending={changed.some((item) => item.s === s && item.c === c) || undefined}>
                      <input
                        id={id}
                        className={styles.cellInput}
                        inputMode="decimal"
                        value={value}
                        aria-label={t("chart_value_label", { category, series: series.name })}
                        aria-invalid={bad || undefined}
                        aria-describedby={bad ? `${id}-error` : undefined}
                        data-autofocus={(s === 0 && c === 0) || undefined}
                        onChange={(event) => onDraft(s, c, event.target.value)}
                      />
                      {bad ? <span id={`${id}-error`} className="settings-field__error" role="alert">{t("not_a_number", { example })}</span> : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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
          <UndoButton onClick={() => onUndo(pendingEditId(edit))} label={t("undo_chart")} />
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
          {(["pictures", "smartart", "master", "notes", "layout", ...(phone ? ["phone_table"] as const : [])] as const).map((key) => <li key={key}>{t(`unsupported_list_${key}`)}</li>)}
        </ul>
      </section>
    </div>
  );
}
