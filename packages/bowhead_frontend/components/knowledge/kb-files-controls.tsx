"use client";

/**
 * kb-management M1 · control bar — dual-mode search, status/type/date filters
 * (menu multi-select), sort, clear, upload. Drives server-side query params in the manager.
 *
 * Knowledge base polish (1.5.0): every filter button says what it selects ("Status: Failed, Done") and carries a
 * chevron; the sort button is neutral with a sort icon and "Upload date · newest first"; with two or more selected
 * values (keyword, each status, each type, date — scope never counts) a selected-filters row appears below the
 * toolbar, each value removable on its own, "Clear all" last.
 */
import { useId, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { ActiveFilterChips, type ActiveFilter } from "@/components/ui/active-filter-chips";
import { Dialog } from "@/components/ui/dialog";
import { MENU_DESCRIPTION_ATTR, MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import type { KbListScope } from "@/lib/kb-api";
import { IconCaret, IconCheck, IconMag, IconSliders, IconSort, IconUp } from "./kb-icons";
import type { KbScopeFilterOption } from "./kb-scope-filter";

export type SortKey = "uploadedAt" | "name" | "sizeMB";
export type SortDir = "asc" | "desc";
export type StatusBucket = "processing" | "done" | "failed";

export const STATUS_BUCKETS: readonly StatusBucket[] = ["processing", "done", "failed"];
export const TYPE_GROUPS: readonly string[] = ["pdf", "doc", "sheet", "slide", "image"];
export const DATE_DAYS: readonly (number | null)[] = [null, 7, 30, 90];
const SORT_KEYS: readonly SortKey[] = ["uploadedAt", "name", "sizeMB"];
/** Menu items with a second line (what the bucket contains); the others are single-line. */
const STATUS_DESC = new Set<StatusBucket>(["processing"]);
const TYPE_DESC = new Set(["doc", "sheet", "slide"]);

export interface KbFilesControlsProps {
  query: string;
  deep: boolean;
  fStatus: Set<StatusBucket>;
  fTypes: Set<string>;
  fDays: number | null;
  sortKey: SortKey;
  sortDir: SortDir;
  /** Narrow (≤ 640px): collapse filters into one Filter button (design 06). */
  narrow?: boolean;
  /** Deep-search summary to search-box right: N files found in Scope. */
  deepSummary?: ReactNode;
  onQuery: (q: string) => void;
  /** Clear the keyword at once (selected-filters row); typing still debounces through onQuery. */
  onClearQuery?: () => void;
  onToggleDeep: () => void;
  onSubmitDeep: () => void;
  onToggleStatus: (v: StatusBucket) => void;
  onToggleType: (v: string) => void;
  onSetDays: (v: number | null) => void;
  /** Clear one group at once (menu "Clear status" / "Clear type"). */
  onClearStatus?: () => void;
  onClearType?: () => void;
  onSetSort: (key: SortKey, dir: SortDir) => void;
  onClear: () => void;
  /** Phone sheet "Clear all": clears only what the sheet shows (status, type, date); falls back to onClear. */
  onClearSheet?: () => void;
  /** Omitted = page header owns Upload; controls do not render it. */
  onUpload?: () => void;
  /** P2 batch 5: the scope filter button, rendered first among the filters. */
  scopeFilter?: ReactNode;
  /** Phone filter sheet: the scope group (same options as the scope filter). Absent = no scope group. */
  scopeSheet?: { scope: KbListScope; options: KbScopeFilterOption[]; onSelect: (scope: KbListScope) => void };
  /** Phone filter sheet footer: "Show N files" (null while unknown). */
  resultCount?: number | null;
}

type Translator = ReturnType<typeof useTranslations<"knowledge_base">>;

/** "Failed, Done" for up to two values, "Failed + 2 more" beyond (values in menu order). */
export function filterValueText(t: Translator, values: string[]): string {
  if (values.length <= 2) return values.join(t("filter.value_sep"));
  return t("filter.chip_many", { first: values[0], more: values.length - 1, n: values.length });
}

/** Focus a filter button; on phones those live in the sheet, so fall back to the "Filters" sheet trigger. */
function focusFilter(key: string) {
  (document.querySelector<HTMLElement>(`[data-kb-filter="${key}"]`) ?? document.querySelector<HTMLElement>(".kb-filter-toggle"))?.focus();
}

/** Menu choice; `desc` makes a two-line item (title + description) per the menu contract. */
function Choice({ label, desc, checked, multi = false, onSelect }: { label: string; desc?: string; checked: boolean; multi?: boolean; onSelect: () => void }) {
  const item = useMenuItem({ label, checked, role: multi ? "menuitemcheckbox" : "menuitemradio", closeOnSelect: !multi, onSelect });
  const descId = useId();
  // The shared menu draws the check for checked items.
  if (!desc) return <button type="button" {...item}><span>{label}</span></button>;
  return (
    <button type="button" className="kb-menu-opt" aria-label={label} aria-describedby={descId} {...item}>
      <span className="txt">
        <span className="nm">{label}</span>
        <span className="desc" id={descId} {...{ [MENU_DESCRIPTION_ATTR]: "" }}>{desc}</span>
      </span>
    </button>
  );
}

/** Plain action at the end of a filter menu ("Clear status"); closes the menu. */
function MenuAction({ label, onSelect }: { label: string; onSelect: () => void }) {
  const item = useMenuItem({ label, onSelect });
  return <button type="button" className="kb-menu-clear" {...item}>{label}</button>;
}

/** Labelled group inside a menu (visible group title, per the menu contract). */
function MenuGroup({ label, children }: { label: string; children: ReactNode }) {
  const id = useId();
  return <div role="group" aria-labelledby={id}><div className="kb-pop-label" id={id} role="presentation">{label}</div>{children}</div>;
}

/** Row in the phone filter sheet: a checkbox (multi) or radio (single) with an optional second line. */
function SheetRow({ label, desc, checked, multi, onToggle, testId }: { label: string; desc?: string; checked: boolean; multi: boolean; onToggle: () => void; testId?: string }) {
  const descId = useId();
  return (
    <button type="button" role={multi ? "checkbox" : "radio"} aria-checked={checked} aria-label={label} aria-describedby={desc ? descId : undefined} className="kb-sheet-row" data-testid={testId} onClick={onToggle}>
      <span className="ck" aria-hidden>{checked ? <IconCheck /> : null}</span>
      <span className="txt"><span className="nm">{label}</span>{desc && <span className="desc" id={descId}>{desc}</span>}</span>
    </button>
  );
}

function SheetGroup({ label, single, children }: { label: string; single?: boolean; children: ReactNode }) {
  const id = useId();
  return (
    <section className="kb-sheet-group" aria-labelledby={id}>
      <h3 className="kb-sheet-label" id={id}>{label}</h3>
      <div className="kb-sheet-list" role={single ? "radiogroup" : "group"} aria-labelledby={id}>{children}</div>
    </section>
  );
}

function Filter({ id, label, ariaLabel, icon, active, children }: { id: string; label: string; ariaLabel?: string; icon?: ReactNode; active?: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start" });
  return <>
    <button
      type="button"
      className={`kb-filterchip ${active ? "active" : ""}`}
      data-kb-filter={id}
      title={label}
      ref={menu.refs.setReference}
      {...menu.getReferenceProps(ariaLabel ? { "aria-label": ariaLabel } : undefined)}
    >
      {icon}<span className="lbl">{label}</span><span className="kb-chevron" aria-hidden><IconCaret /></span>
    </button>
    <MenuFloating menu={menu}>
      <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ "aria-label": ariaLabel ?? label })}>{children}</div>
    </MenuFloating>
  </>;
}

export function KbFilesControls(p: KbFilesControlsProps) {
  const t = useTranslations("knowledge_base");
  const tc = useTranslations("common");
  const [sheetOpen, setSheetOpen] = useState(false);
  const sheetTrigger = useRef<HTMLButtonElement | null>(null);
  // Counts what the phone sheet shows and clears (status, type, date); the keyword stays in the search field.
  const count = p.fStatus.size + p.fTypes.size + (p.fDays === null ? 0 : 1);
  const statusValues = STATUS_BUCKETS.filter((v) => p.fStatus.has(v));
  const typeValues = TYPE_GROUPS.filter((v) => p.fTypes.has(v));
  const statusLabel = statusValues.length
    ? t("filter.chip", { label: t("filter.status_label"), value: filterValueText(t, statusValues.map((v) => t(`filter.status.${v}`))) })
    : t("filter.status_label");
  const typeLabel = typeValues.length
    ? t("filter.chip", { label: t("filter.type_label"), value: filterValueText(t, typeValues.map((v) => t(`filter.type.${v}`))) })
    : t("filter.type_label");
  const dateLabel = p.fDays
    ? t("filter.chip", { label: t("filter.date_label"), value: t("filter.last_days", { n: p.fDays }) })
    : t("filter.date_label");
  const sortValue = t("filter.sort_chip", { field: t(`filter.sort.${p.sortKey}`), order: t(`filter.sort_short.${p.sortKey}.${p.sortDir}`) });

  const sortButton = (
    <Filter id="sort" label={sortValue} ariaLabel={t("filter.sort_button", { value: sortValue })} icon={<span className="kb-chipicon" aria-hidden><IconSort /></span>}>
      <MenuGroup label={t("filter.sort_field")}>{SORT_KEYS.map((v) => <Choice key={v} label={t(`filter.sort.${v}`)} checked={p.sortKey === v} onSelect={() => p.onSetSort(v, p.sortDir)} />)}</MenuGroup>
      <div role="separator" />
      <MenuGroup label={t("filter.sort_order_label")}>{(["desc", "asc"] as SortDir[]).map((v) => <Choice key={v} label={t(`filter.sort_order.${p.sortKey}.${v}`)} checked={p.sortDir === v} onSelect={() => p.onSetSort(p.sortKey, v)} />)}</MenuGroup>
    </Filter>
  );

  // Selected-filters row: every selected value is one token; scope is always set and never listed.
  const keyword = p.query.trim();
  const selected: ActiveFilter[] = [
    ...(keyword ? [{
      key: "keyword",
      label: t("filter.chip", { label: t("filter.keyword"), value: keyword }),
      onRemove: () => (p.onClearQuery ?? (() => p.onQuery("")))(),
      restoreFocus: () => document.querySelector<HTMLInputElement>(".kb-controls .kb-search input")?.focus(),
    }] : []),
    ...statusValues.map((v) => ({
      key: `status:${v}`,
      label: t("filter.chip", { label: t("filter.status_label"), value: t(`filter.status.${v}`) }),
      onRemove: () => p.onToggleStatus(v),
      restoreFocus: () => focusFilter("status"),
    })),
    ...typeValues.map((v) => ({
      key: `type:${v}`,
      label: t("filter.chip", { label: t("filter.type_label"), value: t(`filter.type.${v}`) }),
      onRemove: () => p.onToggleType(v),
      restoreFocus: () => focusFilter("type"),
    })),
    ...(p.fDays ? [{
      key: "date",
      label: t("filter.chip", { label: t("filter.date_label"), value: t("filter.last_days", { n: p.fDays }) }),
      onRemove: () => p.onSetDays(null),
      restoreFocus: () => focusFilter("date"),
    }] : []),
  ];

  return <div className="kb-controls-wrap">
    <div className="kb-controls" data-kb-refined>
      <div className="kb-controls-main">
        <div className={`kb-search ${p.deep ? "deep" : ""}`}>
          <span className="mag" aria-hidden><IconMag /></span>
          <input type="search" aria-label={t("files_view.search_label")} placeholder={p.deep ? t("filter.deep_placeholder") : t("search_placeholder")}
            value={p.query} onChange={(e) => p.onQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") p.onSubmitDeep(); }} />
        </div>
        <button type="button" className={`kb-filterchip kb-deep-toggle${p.deep ? " active" : ""}`} aria-pressed={p.deep} onClick={p.onToggleDeep}>
          <span className="kb-chipicon" aria-hidden><IconSliders /></span>{t("filter.deep_search")}
        </button>
        {p.deepSummary && <span className="kb-deep-count" data-testid="kb-deep-count">{p.deepSummary}</span>}
        {p.narrow && (
          <button type="button" ref={sheetTrigger} className={`kb-filterchip kb-filter-toggle${count > 0 ? " active" : ""}`} aria-haspopup="dialog" aria-expanded={sheetOpen} onClick={() => setSheetOpen(true)}>
            {t("files_view.filters")}{count > 0 ? ` · ${count}` : ""}
          </button>
        )}
        {!p.narrow && <>
          {p.scopeFilter}
          <Filter id="status" label={statusLabel} active={statusValues.length > 0}>
            {STATUS_BUCKETS.map((v) => <Choice key={v} multi label={t(`filter.status.${v}`)} desc={STATUS_DESC.has(v) ? t(`filter.status_desc.${v as "processing"}`) : undefined} checked={p.fStatus.has(v)} onSelect={() => p.onToggleStatus(v)} />)}
            {statusValues.length > 0 && p.onClearStatus && <><div role="separator" /><MenuAction label={t("filter.clear_status")} onSelect={p.onClearStatus} /></>}
          </Filter>
          <Filter id="type" label={typeLabel} active={typeValues.length > 0}>
            {TYPE_GROUPS.map((v) => <Choice key={v} multi label={t(`filter.type.${v}`)} desc={TYPE_DESC.has(v) ? t(`filter.type_desc.${v as "doc"}`) : undefined} checked={p.fTypes.has(v)} onSelect={() => p.onToggleType(v)} />)}
            {typeValues.length > 0 && p.onClearType && <><div role="separator" /><MenuAction label={t("filter.clear_type")} onSelect={p.onClearType} /></>}
          </Filter>
          <Filter id="date" label={dateLabel} active={p.fDays !== null}>
            {DATE_DAYS.map((v) => <Choice key={String(v)} label={t(`filter.date.${v ? `d${v}` : "all"}`)} checked={p.fDays === v} onSelect={() => p.onSetDays(v)} />)}
          </Filter>
        </>}
        {sortButton}
      </div>
      {p.onUpload && <button type="button" className="kb-btn kb-btn-primary" onClick={p.onUpload}><IconUp />{t("upload_button")}</button>}
    </div>
    <ActiveFilterChips filters={selected} onClearAll={p.onClear} label={t("filter.selected_label")} className="kb-selrow" />
    {p.narrow && sheetOpen && (
      // Phone: the filters open as a bottom sheet with Scope / Status / Type / Date groups; choices apply at once
      // (same as the desktop menus) and the primary button only closes the sheet with the current result count.
      <Dialog
        title={t("files_view.filters")}
        presentation="sheet"
        onClose={() => setSheetOpen(false)}
        returnFocusRef={sheetTrigger}
        footer={<div className="kb-sheet-foot">
          <button type="button" className="kb-btn kb-btn-secondary" onClick={p.onClearSheet ?? p.onClear}>{tc("clear_all_filters")}</button>
          <button type="button" className="kb-btn kb-btn-primary" data-testid="kb-filter-sheet-done" onClick={() => setSheetOpen(false)}>
            {p.resultCount == null ? t("filter.sheet_done") : t("filter.sheet_show", { n: p.resultCount })}
          </button>
        </div>}
      >
        <div className="kb-filter-sheet" data-testid="kb-filter-sheet">
          {p.scopeSheet && (
            <SheetGroup label={t("filter.scope_label")} single>
              {p.scopeSheet.options.map((o) => (
                <SheetRow key={o.id} label={o.label} desc={o.hint} checked={p.scopeSheet!.scope === o.id} multi={false} testId={`kb-sheet-scope-${o.id}`} onToggle={() => p.scopeSheet!.onSelect(o.id)} />
              ))}
            </SheetGroup>
          )}
          <SheetGroup label={t("filter.status_label")}>
            {STATUS_BUCKETS.map((v) => <SheetRow key={v} multi label={t(`filter.status.${v}`)} desc={STATUS_DESC.has(v) ? t(`filter.status_desc.${v as "processing"}`) : undefined} checked={p.fStatus.has(v)} onToggle={() => p.onToggleStatus(v)} />)}
          </SheetGroup>
          <SheetGroup label={t("filter.type_label")}>
            {TYPE_GROUPS.map((v) => <SheetRow key={v} multi label={t(`filter.type.${v}`)} desc={TYPE_DESC.has(v) ? t(`filter.type_desc.${v as "doc"}`) : undefined} checked={p.fTypes.has(v)} onToggle={() => p.onToggleType(v)} />)}
          </SheetGroup>
          <SheetGroup label={t("filter.date_label")} single>
            {DATE_DAYS.map((v) => <SheetRow key={String(v)} multi={false} label={t(`filter.date.${v ? `d${v}` : "all"}`)} checked={p.fDays === v} onToggle={() => p.onSetDays(v)} />)}
          </SheetGroup>
        </div>
      </Dialog>
    )}
  </div>;
}
