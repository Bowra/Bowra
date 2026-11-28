"use client";

/**
 * Document matrix step 1 (S4, mockup ④): the review toolbar above the grid — in-table search, the status filter,
 * per-column value filters, sort and the verified progress "已核 x / y". It only edits a `MatrixView`; filtering and
 * sorting happen in `lib/kb-matrix/review.ts` and the view is kept in the URL by the matrix page.
 *
 * Value filters are edited in a small form under the toolbar (column, condition, value): equals, contains for text
 * and options, greater or less than for numbers and dates, and is empty. Each active filter shows as a button that
 * reopens the form, with its own remove button.
 */

import { type ReactNode, useEffect, useId, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import {
  FILE_SORT,
  type MatrixSort,
  type MatrixView,
  STATUS_FILTERS,
  type StatusFilter,
  type ValueFilter,
  type ValueFilterOp,
  valueFilterOps,
  valueKind,
} from "@/lib/kb-matrix/review";
import type { MatrixColumnVM } from "@/lib/kb-matrix/types";

import { IconCaret, IconMag, IconSort, IconX } from "../kb-icons";

type Translator = ReturnType<typeof useTranslations<"kbMatrix">>;

const ALL = "all";

/** "大于" for numbers, "晚于" for dates. */
function opLabel(t: Translator, op: ValueFilterOp, type: string): string {
  const kind = valueKind(type);
  if ((op === "gt" || op === "lt") && kind === "date") return t(`filter_op_${op}_date` as never);
  return t(`filter_op_${op}` as never);
}

function valueLabel(t: Translator, f: ValueFilter, column: MatrixColumnVM): string {
  if (column.type === "boolean") return f.value === "true" ? t("bool_true") : f.value === "false" ? t("bool_false") : f.value;
  return f.value;
}

/** "付款方式：预付", "金额：大于 100", "到期日：为空". */
export function valueFilterText(t: Translator, f: ValueFilter, column: MatrixColumnVM): string {
  if (f.op === "empty") return t("filter_value_chip", { column: column.name, value: t("filter_op_empty") });
  const value = valueLabel(t, f, column);
  return t("filter_value_chip", { column: column.name, value: f.op === "eq" ? value : `${opLabel(t, f.op, column.type)} ${value}` });
}

/** Menu items say "Smallest First"; the button says "smallest first" (`short`). */
function sortOrderLabel(t: Translator, dir: "asc" | "desc", column: MatrixColumnVM | undefined, short = false): string {
  const kind = column ? valueKind(column.type) : "text";
  const family = kind === "number" ? "number" : kind === "date" ? "date" : "text";
  return t(`sort_${short ? "short_" : ""}${dir}_${family}` as never);
}

function MenuChoice({ label, checked, onSelect }: { label: string; checked: boolean; onSelect: () => void }) {
  const item = useMenuItem({ label, checked, role: "menuitemradio", onSelect });
  return (
    <button type="button" {...item}>
      <span>{label}</span>
    </button>
  );
}

function MenuGroup({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  return (
    <div role="group" aria-labelledby={id}>
      <div className="kb-pop-label" id={id} role="presentation">
        {label}
      </div>
      {children}
    </div>
  );
}

function SortMenu({ sort, columns, onSort }: { sort: MatrixSort | null; columns: MatrixColumnVM[]; onSort: (s: MatrixSort | null) => void }) {
  const t = useTranslations("kbMatrix");
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start", sheetTitle: t("sort_by") });
  const column = sort && sort.key !== FILE_SORT ? columns.find((c) => c.columnId === sort.key) : undefined;
  const keyName = sort ? (sort.key === FILE_SORT ? t("col_file") : column?.name) : undefined;
  const label = sort && keyName ? t("sort_chip", { column: keyName, order: sortOrderLabel(t, sort.dir, column, true) }) : t("sort_button");
  const dir = sort?.dir ?? "asc";
  return (
    <>
      <button
        type="button"
        className={"kb-filterchip" + (sort && keyName ? " active" : "")}
        ref={menu.refs.setReference}
        data-testid="kb-matrix-sort"
        title={label}
        {...menu.getReferenceProps({ "aria-label": t("sort_menu_label", { value: label }) })}
      >
        <span className="kb-chipicon" aria-hidden>
          <IconSort />
        </span>
        <span className="lbl">{label}</span>
        <span className="kb-chevron" aria-hidden>
          <IconCaret />
        </span>
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} data-testid="kb-matrix-sort-menu" {...menu.getFloatingProps({ "aria-label": t("sort_by") })}>
          <MenuGroup label={t("sort_by")}>
            <MenuChoice label={t("sort_none")} checked={!sort} onSelect={() => onSort(null)} />
            <MenuChoice label={t("col_file")} checked={sort?.key === FILE_SORT} onSelect={() => onSort({ key: FILE_SORT, dir })} />
            {columns.map((c) => (
              <MenuChoice key={c.columnId} label={c.name} checked={sort?.key === c.columnId} onSelect={() => onSort({ key: c.columnId, dir })} />
            ))}
          </MenuGroup>
          {sort && (
            <>
              <div role="separator" />
              <MenuGroup label={t("sort_order")}>
                {(["asc", "desc"] as const).map((d) => (
                  <MenuChoice key={d} label={sortOrderLabel(t, d, column)} checked={sort.dir === d} onSelect={() => onSort({ ...sort, dir: d })} />
                ))}
              </MenuGroup>
            </>
          )}
        </div>
      </MenuFloating>
    </>
  );
}

interface Draft {
  columnId: string;
  op: ValueFilterOp;
  value: string;
  /** Editing an active filter (offers Remove). */
  existing: boolean;
}

/** The value filter form: column, condition and value; Enter applies, Esc cancels. */
function ValueFilterForm({
  draft,
  columns,
  onChange,
  onApply,
  onCancel,
  onRemove,
}: {
  draft: Draft;
  columns: MatrixColumnVM[];
  onChange: (d: Draft) => void;
  onApply: () => void;
  onCancel: () => void;
  onRemove: () => void;
}) {
  const t = useTranslations("kbMatrix");
  const column = columns.find((c) => c.columnId === draft.columnId) ?? columns[0];
  const ops = column ? valueFilterOps(column.type) : [];
  const kind = column ? valueKind(column.type) : "text";
  const needsValue = draft.op !== "empty";
  const ready = !!column && (!needsValue || draft.value.trim() !== "");
  const ids = { column: useId(), op: useId(), value: useId() };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onCancel();
    } else if (e.key === "Enter" && ready && (e.target as HTMLElement).tagName !== "BUTTON") {
      e.preventDefault();
      onApply();
    }
  };
  let valueInput: ReactNode = null;
  if (needsValue && column) {
    const options = kind === "boolean" ? ["true", "false"] : kind === "choice" && draft.op === "eq" ? (column.options ?? []) : null;
    valueInput = options ? (
      <select id={ids.value} className="mx-inp" value={draft.value} onChange={(e) => onChange({ ...draft, value: e.target.value })}>
        <option value="">{t("filter_value_pick")}</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {kind === "boolean" ? (o === "true" ? t("bool_true") : t("bool_false")) : o}
          </option>
        ))}
      </select>
    ) : (
      <input
        id={ids.value}
        className="mx-inp"
        type={kind === "date" ? "date" : "text"}
        inputMode={kind === "number" ? "decimal" : undefined}
        value={draft.value}
        autoFocus
        onChange={(e) => onChange({ ...draft, value: e.target.value })}
      />
    );
  }
  return (
    <div className="mx-vfilter-form" role="group" aria-label={t("filter_form_label")} data-testid="kb-matrix-vfilter-form" onKeyDown={onKeyDown}>
      <label htmlFor={ids.column}>{t("filter_column")}</label>
      <select
        id={ids.column}
        className="mx-inp"
        value={column?.columnId ?? ""}
        onChange={(e) => {
          const next = columns.find((c) => c.columnId === e.target.value);
          const nextOps = next ? valueFilterOps(next.type) : [];
          onChange({ ...draft, columnId: e.target.value, op: nextOps.includes(draft.op) ? draft.op : (nextOps[0] ?? "eq"), value: "" });
        }}
      >
        {columns.map((c) => (
          <option key={c.columnId} value={c.columnId}>
            {c.name}
          </option>
        ))}
      </select>
      <label htmlFor={ids.op}>{t("filter_condition")}</label>
      <select
        id={ids.op}
        className="mx-inp"
        value={draft.op}
        onChange={(e) => onChange({ ...draft, op: e.target.value as ValueFilterOp, value: e.target.value === "empty" ? "" : draft.value })}
      >
        {ops.map((op) => (
          <option key={op} value={op}>
            {column ? opLabel(t, op, column.type) : op}
          </option>
        ))}
      </select>
      {needsValue && <label htmlFor={ids.value}>{t("filter_value")}</label>}
      {valueInput}
      <span className="acts">
        <button type="button" className="kb-btn kb-btn-primary kb-btn-sm" disabled={!ready} onClick={onApply}>
          {t("filter_apply")}
        </button>
        <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" onClick={onCancel}>
          {t("cancel")}
        </button>
        {draft.existing && (
          <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" onClick={onRemove}>
            {t("filter_remove")}
          </button>
        )}
      </span>
    </div>
  );
}

export interface MatrixToolbarProps {
  view: MatrixView;
  columns: MatrixColumnVM[];
  onView: (view: MatrixView) => void;
  /** Rows shown and readable rows in all, for "显示 m / n 行" while filtered. */
  shownRows: number;
  totalRows: number;
  progress: { verified: number; total: number };
  /** Read-only phone layout: progress only. */
  progressOnly?: boolean;
  /** Extra note while filtered (the failed view explains how to leave it). */
  filterNote?: string;
}

export function MatrixToolbar({ view, columns, onView, shownRows, totalRows, progress, progressOnly = false, filterNote }: MatrixToolbarProps) {
  const t = useTranslations("kbMatrix");
  const locale = useLocale();
  const [draft, setDraft] = useState<Draft | null>(null);
  // Search typing stays local; the view (and with it the grid and the URL) follows after a short pause.
  const [query, setQuery] = useState(view.q);
  useEffect(() => setQuery(view.q), [view.q]);
  useEffect(() => {
    if (query === view.q) return;
    const timer = setTimeout(() => onView({ ...view, q: query }), 200);
    return () => clearTimeout(timer);
  }, [query, view, onView]);

  const pct = progress.total ? Math.round((100 * progress.verified) / progress.total) : 0;
  const progressNode = (
    <div
      className="mx-progress"
      role="meter"
      aria-valuemin={0}
      aria-valuemax={progress.total}
      aria-valuenow={progress.verified}
      aria-label={t("progress_label", { verified: progress.verified, total: progress.total })}
      data-testid="kb-matrix-progress"
    >
      <span>{t("progress", { verified: progress.verified.toLocaleString(locale), total: progress.total.toLocaleString(locale) })}</span>
      <span className="mx-bar" aria-hidden>
        <span style={{ width: `${pct}%` }} />
      </span>
    </div>
  );
  if (progressOnly) return <div className="mx-toolbar mx-toolbar--progress">{progressNode}</div>;

  const byId = new Map(columns.map((c) => [c.columnId, c]));
  const values = view.values.filter((f) => byId.has(f.columnId));
  const filtered = !!view.q.trim() || view.status !== null || values.length > 0;
  const statusOptions = [{ value: ALL, label: t("status_all") }, ...STATUS_FILTERS.map((s) => ({ value: s, label: t(`status_${s}` as never) }))];
  const openNew = () => {
    const first = columns.find((c) => !values.some((f) => f.columnId === c.columnId)) ?? columns[0];
    if (first) setDraft({ columnId: first.columnId, op: valueFilterOps(first.type)[0], value: "", existing: false });
  };
  const apply = () => {
    if (!draft) return;
    const next: ValueFilter = { columnId: draft.columnId, op: draft.op, value: draft.op === "empty" ? "" : draft.value.trim() };
    onView({ ...view, values: [...view.values.filter((f) => f.columnId !== next.columnId), next] });
    setDraft(null);
  };
  const remove = (columnId: string) => {
    onView({ ...view, values: view.values.filter((f) => f.columnId !== columnId) });
    setDraft(null);
  };

  return (
    <div className="mx-toolbar-wrap" data-testid="kb-matrix-toolbar">
      <div className="mx-toolbar">
        <div className="kb-search mx-search">
          <span className="mag" aria-hidden>
            <IconMag />
          </span>
          <input
            type="search"
            aria-label={t("search_label")}
            placeholder={t("search_placeholder")}
            value={query}
            data-testid="kb-matrix-search"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onView({ ...view, q: query });
            }}
          />
        </div>
        <FilterMenuButton<string>
          value={view.status ?? ALL}
          options={statusOptions}
          onChange={(v) => onView({ ...view, status: v === ALL ? null : (v as StatusFilter) })}
          format={(value) => t("filter_status_chip", { value })}
          menuLabel={t("filter_status")}
          active={view.status !== null}
          testId="kb-matrix-status-filter"
        />
        {values.map((f) => {
          const column = byId.get(f.columnId) as MatrixColumnVM;
          const text = valueFilterText(t, f, column);
          return (
            <span key={f.columnId} className="mx-vfilter">
              <button
                type="button"
                className="kb-filterchip active"
                data-testid={`kb-matrix-vfilter-${f.columnId}`}
                aria-label={t("filter_edit", { filter: text })}
                onClick={() => setDraft({ columnId: f.columnId, op: f.op, value: f.value, existing: true })}
              >
                <span className="lbl">{text}</span>
              </button>
              <button type="button" className="mx-vfilter-x" aria-label={t("filter_remove_named", { filter: text })} onClick={() => remove(f.columnId)}>
                <IconX />
              </button>
            </span>
          );
        })}
        {columns.length > 0 && (
          <button type="button" className="kb-filterchip" data-testid="kb-matrix-vfilter-add" aria-expanded={!!draft && !draft.existing} onClick={openNew}>
            {t("filter_add")}
          </button>
        )}
        <SortMenu sort={view.sort} columns={columns} onSort={(sort) => onView({ ...view, sort })} />
        {filtered && (
          <span className="mx-toolbar-count" role="status" data-testid="kb-matrix-filtercount">
            {t("filter_count", { shown: shownRows, total: totalRows })}
            <button
              type="button"
              className="mx-viewsrc"
              onClick={() => {
                setQuery("");
                setDraft(null);
                onView({ ...view, q: "", status: null, values: [] });
              }}
            >
              {t("filter_clear")}
            </button>
          </span>
        )}
        <span className="grow" />
        {progressNode}
      </div>
      {filtered && filterNote && <p className="mx-toolbar-note">{filterNote}</p>}
      {draft && (
        <ValueFilterForm
          draft={draft}
          columns={columns}
          onChange={setDraft}
          onApply={apply}
          onCancel={() => setDraft(null)}
          onRemove={() => remove(draft.columnId)}
        />
      )}
    </div>
  );
}
