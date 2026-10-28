"use client";

import clsx from "clsx";
import { useLocale, useTranslations } from "next-intl";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CompositionEvent,
  type KeyboardEvent,
  type RefObject,
} from "react";

import { DialogShell } from "@/components/ui/dialog";
import { useNarrowViewport } from "@/components/ui/sheet";
import type { ResultItemDTO, ResultsApiErrorCode } from "@/lib/results-api";
import { resultDisplayTitle } from "@/lib/results-workspace-model";
import { CharIcon } from "@/components/char-icon";
import { ResultsStateIcon } from "@/components/canvas/results-state-icon";

type QuickSectionId =
  | "search"
  | "report"
  | "current"
  | "recent"
  | "current_report"
  | "deliverables"
  | "other";

type QuickSection = {
  id: QuickSectionId;
  items: ResultItemDTO[];
};

const DIRECT_REPORT_ROLES = new Set([
  "report_chart_data",
  "report_detail_table",
  "report_computation",
  "report_embedded_media",
]);

const SECTION_ORDER: QuickSectionId[] = [
  "report",
  "current",
  "recent",
  "current_report",
  "deliverables",
  "other",
];

export function groupQuickResults({
  currentKey,
  items,
  query,
  recentKeys,
}: {
  currentKey: string | null;
  items: readonly ResultItemDTO[];
  query: string;
  recentKeys: readonly string[];
}): QuickSection[] {
  const maximum = query.trim() ? 8 : 10;
  const uniqueItems = Array.from(
    new Map(items.map((item) => [item.key, item])).values(),
  ).slice(0, maximum);
  if (query.trim()) return [{ id: "search", items: uniqueItems }];
  const recent = new Set(recentKeys);
  const sections = new Map<QuickSectionId, ResultItemDTO[]>();
  for (const item of uniqueItems) {
    const section: QuickSectionId = item.object_type === "report"
      ? "report"
      : item.key === currentKey
        ? "current"
        : recent.has(item.key)
          ? "recent"
          : item.relation_roles.some((role) => DIRECT_REPORT_ROLES.has(role))
            ? "current_report"
            : item.primary_group === "deliverables"
              ? "deliverables"
              : "other";
    const group = sections.get(section) ?? [];
    group.push(item);
    sections.set(section, group);
  }
  return SECTION_ORDER.flatMap((id) => {
    const sectionItems = sections.get(id);
    return sectionItems?.length ? [{ id, items: sectionItems }] : [];
  });
}

type Props = {
  activeKey: string | null;
  authorizedTotal: number | null;
  compact: boolean;
  currentKey: string | null;
  error: ResultsApiErrorCode | null;
  hasMore: boolean;
  items: readonly ResultItemDTO[];
  loading: boolean;
  matchedTotal: number | null;
  open: boolean;
  query: string;
  recentKeys: readonly string[];
  triggerRef: RefObject<HTMLElement | null>;
  onBrowseAll: (query: string) => void;
  onClose: () => void;
  onQueryChange: (query: string) => void;
  onRetry: () => void;
  onSelect: (item: ResultItemDTO) => void;
};

/** `(pointer: coarse)` = the primary input is touch; SSR or no matchMedia reads as a fine pointer. */
function hasCoarsePointer(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(pointer: coarse)").matches;
}

export function ResultsQuickSwitcher({
  activeKey,
  authorizedTotal,
  compact,
  currentKey,
  error,
  hasMore,
  items,
  loading,
  matchedTotal,
  open,
  query,
  recentKeys,
  triggerRef,
  onBrowseAll,
  onClose,
  onQueryChange,
  onRetry,
  onSelect,
}: Props) {
  const t = useTranslations("results_workspace");
  const locale = useLocale();
  const headingId = useId();
  const listboxId = useId();
  const optionPrefix = useId();
  const searchRef = useRef<HTMLInputElement | null>(null);
  const composingRef = useRef(false);
  // Initial focus follows the input type, not the pane width: a coarse pointer (touch) focuses the
  // heading so the soft keyboard stays closed; a fine pointer focuses search even in the compact layout.
  // Captured when the dialog opens so a later media change cannot move focus targets mid-session.
  const coarsePointerAtOpenRef = useRef<boolean | null>(null);
  const previousOpenRef = useRef(open);
  if (open && (!previousOpenRef.current || coarsePointerAtOpenRef.current === null)) {
    coarsePointerAtOpenRef.current = hasCoarsePointer();
  }
  previousOpenRef.current = open;
  const [draftQuery, setDraftQuery] = useState(query);
  const [activeOptionKey, setActiveOptionKey] = useState<string | null>(null);
  const [showDelayedLoading, setShowDelayedLoading] = useState(false);
  // Narrow Spec: on phones (< 640) a searchable list opens as the shared bottom sheet instead of covering the top bar.
  const sheet = useNarrowViewport(true);
  const focusHeadingFirst = coarsePointerAtOpenRef.current === true;

  const sections = useMemo(() => groupQuickResults({
    currentKey,
    items,
    query: draftQuery,
    recentKeys,
  }), [currentKey, draftQuery, items, recentKeys]);
  const visibleItems = useMemo(
    () => sections.flatMap((section) => section.items),
    [sections],
  );
  const activeItem = visibleItems.find((item) => item.key === activeOptionKey) ?? null;

  useEffect(() => {
    if (!open) {
      composingRef.current = false;
      setDraftQuery("");
      setActiveOptionKey(null);
      setShowDelayedLoading(false);
      return;
    }
    setDraftQuery(query);
    setActiveOptionKey(null);
  }, [open, query]);

  useEffect(() => {
    if (!loading || visibleItems.length > 0) {
      setShowDelayedLoading(false);
      return;
    }
    const timer = window.setTimeout(() => setShowDelayedLoading(true), 300);
    return () => window.clearTimeout(timer);
  }, [loading, visibleItems.length]);

  const commitQuery = (value: string) => {
    setActiveOptionKey(null);
    onQueryChange(value);
  };
  const onCompositionStart = () => {
    composingRef.current = true;
  };
  const onCompositionEnd = (event: CompositionEvent<HTMLInputElement>) => {
    composingRef.current = false;
    commitQuery(event.currentTarget.value);
  };
  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (composingRef.current || event.nativeEvent.isComposing) {
      if (["Enter", "Escape", "ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.stopPropagation();
      }
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      if (visibleItems.length === 0) return;
      const currentIndex = activeItem
        ? visibleItems.findIndex((item) => item.key === activeItem.key)
        : -1;
      const nextIndex = event.key === "Home"
        ? 0
        : event.key === "End"
          ? visibleItems.length - 1
          : event.key === "ArrowDown"
            ? Math.min(currentIndex + 1, visibleItems.length - 1)
            : currentIndex <= 0 ? visibleItems.length - 1 : currentIndex - 1;
      setActiveOptionKey(visibleItems[nextIndex]?.key ?? null);
      window.requestAnimationFrame(() => {
        document.getElementById(optionId(optionPrefix, visibleItems[nextIndex]?.key ?? ""))
          ?.scrollIntoView({ block: "nearest" });
      });
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      if (activeItem) onSelect(activeItem);
    }
  };

  if (!open) return null;
  const normalizedQuery = draftQuery.trim();
  const zeroResults = Boolean(
    normalizedQuery && !loading && !error && visibleItems.length === 0,
  );
  const emptyResults = Boolean(
    !normalizedQuery && !loading && !error && visibleItems.length === 0,
  );
  const liveMessage = error
    ? t("quick_error")
    : loading
      ? t("quick_loading")
      : t("quick_results_status", {
          count: visibleItems.length,
          total: matchedTotal ?? visibleItems.length,
        });

  return (
    <>
      {/* Visual scrim only; DialogShell outsidePress handles clicks on it (outside the panel). The sheet brings its own backdrop. */}
      {sheet ? null : <div className="results-quick-scrim" aria-hidden="true" />}
      {/* Wider viewports: the panel is absolute below the results header with container-unit widths, rendered inline without a
          fullscreen backdrop. Phones: the shared bottom sheet (handle, title, close; actions above the safe area). */}
      <DialogShell
        onClose={onClose}
        labelledBy={headingId}
        mount={sheet ? "body" : "inline"}
        overlay={sheet ? "fixed" : "none"}
        presentation={sheet ? "sheet" : undefined}
        backdropClassName={sheet ? "settings-dialog-backdrop" : undefined}
        returnFocusRef={triggerRef}
        panelClassName={clsx("results-quick", { "results-quick--compact": compact })}
      >
        <header className="results-quick-head">
          <div>
            <h2 id={headingId} tabIndex={-1} data-dialog-initial-focus={focusHeadingFirst ? "" : undefined}>
              {t("quick_title")}
            </h2>
            <p>{t("quick_hint")}</p>
          </div>
          <button type="button" onClick={onClose} aria-label={t("close_navigator")}><CharIcon ch="×" aria-hidden /></button>
        </header>
        <div className="results-quick-search-wrap">
          <input
            ref={searchRef}
            data-dialog-initial-focus={focusHeadingFirst ? undefined : ""}
            type="search"
            role="combobox"
            aria-autocomplete="list"
            aria-controls={listboxId}
            aria-expanded="true"
            aria-activedescendant={activeItem
              ? optionId(optionPrefix, activeItem.key)
              : undefined}
            aria-label={t("quick_search")}
            placeholder={t("quick_search")}
            maxLength={120}
            value={draftQuery}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setDraftQuery(value);
              if (!composingRef.current) commitQuery(value);
            }}
            onCompositionStart={onCompositionStart}
            onCompositionEnd={onCompositionEnd}
            onKeyDown={onSearchKeyDown}
          />
        </div>
        <div
          id={listboxId}
          className="results-quick-listbox"
          role="listbox"
          aria-label={t("quick_results")}
          aria-busy={loading}
        >
          {sections.map((section) => {
            const labelId = `${optionPrefix}-${section.id}-label`;
            return (
              <div
                key={section.id}
                className="results-quick-group"
                role="group"
                aria-labelledby={labelId}
              >
                <h3 id={labelId}>{sectionTitle(section.id, t)}</h3>
                {section.items.map((item) => {
                  const context = itemContext(item, locale, t);
                  const type = itemType(item, t);
                  const title = resultDisplayTitle(item, t("code"));
                  const selected = item.key === activeKey;
                  const position = visibleItems.findIndex(
                    (visible) => visible.key === item.key,
                  ) + 1;
                  return (
                    <div
                      id={optionId(optionPrefix, item.key)}
                      key={item.key}
                      className={clsx("results-quick-option", {
                        active: item.key === activeOptionKey,
                        selected,
                      })}
                      role="option"
                      aria-label={[title, type, context].filter(Boolean).join(" · ")}
                      aria-selected={selected}
                      aria-posinset={position}
                      aria-setsize={visibleItems.length}
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseEnter={() => setActiveOptionKey(item.key)}
                      onClick={() => onSelect(item)}
                    >
                      <span className="results-quick-badge" aria-hidden>{itemBadge(item)}</span>
                      <span className="results-quick-copy">
                        <strong title={title}>{title}</strong>
                        <span>{[type, context].filter(Boolean).join(" · ")}</span>
                      </span>
                      {selected ? <span className="results-quick-current">{t("quick_selected")}</span> : null}
                    </div>
                  );
                })}
              </div>
            );
          })}
          {showDelayedLoading ? (
            <div className="results-quick-loading" aria-hidden>
              <span /><span /><span />
            </div>
          ) : null}
          {zeroResults ? (
            <div className="results-quick-empty">
              <ResultsStateIcon tone="no-match" />
              <strong>{t("quick_no_match")}</strong>
              <span>{t("quick_no_match_hint", { query: normalizedQuery })}</span>
            </div>
          ) : null}
          {emptyResults ? (
            <div className="results-quick-empty">
              <ResultsStateIcon tone="empty" />
              <strong>{t("results_empty")}</strong>
            </div>
          ) : null}
          {error && visibleItems.length === 0 ? (
            <div className="results-quick-empty">
              <ResultsStateIcon tone="error" />
              <strong>{t("quick_error")}</strong>
            </div>
          ) : null}
        </div>
        <div className="results-quick-status" role="status" aria-live="polite">
          {liveMessage}
        </div>
        <footer className="results-quick-footer">
          {zeroResults ? (
            <>
              <button type="button" onClick={() => {
                setDraftQuery("");
                commitQuery("");
                searchRef.current?.focus();
              }}>
                {t("quick_clear_search")}
              </button>
              <button type="button" onClick={() => onBrowseAll("")}>
                {t("quick_clear_and_browse")}
              </button>
            </>
          ) : error ? (
            <button type="button" onClick={onRetry}>{t("list_retry")}</button>
          ) : normalizedQuery && hasMore ? (
            <button type="button" onClick={() => onBrowseAll(normalizedQuery)}>
              {t("quick_browse_matches", {
                count: matchedTotal ?? visibleItems.length,
              })}
            </button>
          ) : (
            <button type="button" onClick={() => onBrowseAll("")}>
              {authorizedTotal === null
                ? t("quick_browse_all_unknown")
                : t("quick_browse_all", { count: authorizedTotal })}
            </button>
          )}
        </footer>
      </DialogShell>
    </>
  );
}

function optionId(prefix: string, key: string): string {
  return `${prefix}-${key}`;
}

function sectionTitle(
  section: QuickSectionId,
  t: (key: string) => string,
): string {
  switch (section) {
    case "search": return t("quick_group_search");
    case "report": return t("quick_group_report");
    case "current": return t("quick_group_current");
    case "recent": return t("quick_group_recent");
    case "current_report": return t("quick_group_current_report");
    case "deliverables": return t("quick_group_deliverables");
    default: return t("quick_group_other");
  }
}

function itemType(item: ResultItemDTO, t: (key: string) => string): string {
  if (item.object_type === "report") return t("report");
  if (item.object_type === "code") return t("code_group");
  if (item.primary_group === "deliverables") return t("quick_type_deliverable");
  if (item.secondary_group === "input") return t("input_files");
  return t("data");
}

function itemContext(
  item: ResultItemDTO,
  locale: string,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  const context = item.display_context;
  const pieces: string[] = [];
  if (context.execution_label) pieces.push(context.execution_label);
  else if (context.output_role) pieces.push(outputRole(context.output_role, t));
  if (context.duplicate_ordinal && context.duplicate_count) {
    pieces.push(t("quick_duplicate", {
      ordinal: context.duplicate_ordinal,
      count: context.duplicate_count,
    }));
  }
  if (context.occurred_at) {
    const occurredAt = new Date(context.occurred_at);
    if (!Number.isNaN(occurredAt.getTime())) {
      pieces.push(new Intl.DateTimeFormat(locale, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(occurredAt));
    }
  }
  return pieces.join(" · ");
}

function outputRole(
  role: string,
  t: (key: string) => string,
): string {
  switch (role) {
    case "report_chart_data": return t("quick_role_chart_data");
    case "report_detail_table": return t("quick_role_detail_table");
    case "report_computation": return t("quick_role_computation");
    case "report_embedded_media": return t("quick_role_embedded_media");
    case "code_output": return t("quick_role_code_output");
    case "code_input": return t("quick_role_code_input");
    case "run_output": return t("quick_role_run_output");
    case "technical_output": return t("quick_role_technical_output");
    case "main_deliverable": return t("quick_role_main_deliverable");
    default: return t("quick_role_source");
  }
}

function itemBadge(item: ResultItemDTO): string {
  if (item.object_type === "report") return "DOC";
  if (item.object_type === "code") return "</>";
  if (item.version) return item.version.format.toUpperCase();
  switch (item.viewer_profile) {
    case "html": return "HTML";
    case "image": return "IMG";
    case "video": return "VID";
    case "table": return "DATA";
    case "text": return "TXT";
    case "document": return "DOC";
    default: return "FILE";
  }
}
