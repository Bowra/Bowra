"use client";

import {
  defaultRangeExtractor,
  useVirtualizer,
  type Range,
} from "@tanstack/react-virtual";
import clsx from "clsx";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { useLocale, useTranslations } from "next-intl";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CompositionEvent,
} from "react";

import type {
  ResultItemDTO,
  ResultsApiErrorCode,
  ResultsFiltersDTO,
  ResultsQueryResponse,
} from "@/lib/results-api";
import { resultDisplayTitle } from "@/lib/results-workspace-model";
import type { ResultsCompletedView } from "@/lib/use-results-query";
import { CharIcon } from "@/components/char-icon";
import { ResultsStateIcon } from "@/components/canvas/results-state-icon";

type GroupId = "deliverables" | "evidence" | "evidence:data" | "evidence:code" | "evidence:input";

export type ResultsExplorerRow =
  | {
      kind: "group";
      key: `group:${GroupId}`;
      group: GroupId;
      count: number;
      level: 1 | 2;
    }
  | { kind: "item"; key: string; item: ResultItemDTO };

type Props = {
  anchor: { key: string; offset_px: number } | null;
  authorizedTotal: number | null;
  checkingChanges: boolean;
  compactFilters?: boolean;
  completedView: ResultsCompletedView | null;
  entryId: string;
  error: ResultsApiErrorCode | null;
  errorDirection: "base" | "next" | "previous" | null;
  errorAnchorKey: string | null;
  expandedGroups: readonly string[];
  filters: ResultsFiltersDTO;
  focusKey: string | null;
  groupCounts: ResultsQueryResponse["group_counts"] | null;
  hasMore: boolean;
  hasPrevious: boolean;
  items: readonly ResultItemDTO[];
  loadedCount: number;
  loading: boolean;
  loadingMore: boolean;
  loadingPrevious: boolean;
  matchedTotal: number | null;
  newResultsCount: number | null;
  projectionChanged: boolean;
  projectionStatus: ResultsQueryResponse["projection_status"] | null;
  query: string;
  selectedKey: string | null;
  onAcceptChanges: () => void;
  onAnchorChange: (anchor: { key: string; offset_px: number } | null) => void;
  onExpandedGroupsChange: (groups: string[]) => void;
  onFiltersChange: (filters: ResultsFiltersDTO) => void;
  onFocusKeyChange: (key: string | null) => void;
  onLoadNext: () => void;
  onLoadPrevious: () => void;
  onQueryChange: (query: string) => void;
  onRetry: () => void;
  onSelect: (item: ResultItemDTO) => void;
};

export function buildResultsExplorerRows({
  expandedGroups,
  filters,
  groupCounts,
  items,
  query,
}: Pick<Props, "expandedGroups" | "filters" | "groupCounts" | "items" | "query">): ResultsExplorerRow[] {
  if (query.trim() || filters.secondary !== "all") {
    return items.map((item) => ({ kind: "item", key: item.key, item }));
  }
  const counts = groupCounts ?? {
    deliverables: 0,
    evidence: 0,
    data: 0,
    code: 0,
    input: 0,
  };
  const expanded = new Set(expandedGroups);
  const rows: ResultsExplorerRow[] = [];
  const showDeliverables = filters.primary !== "evidence";
  const showEvidence = filters.primary !== "deliverables";
  if (showDeliverables) {
    rows.push({
      kind: "group",
      key: "group:deliverables",
      group: "deliverables",
      count: counts.deliverables,
      level: 1,
    });
    if (expanded.has("deliverables")) {
      rows.push(...items
        .filter((item) => item.primary_group === "deliverables")
        .map((item) => ({ kind: "item" as const, key: item.key, item })));
    }
  }
  if (showEvidence) {
    rows.push({
      kind: "group",
      key: "group:evidence",
      group: "evidence",
      count: counts.evidence,
      level: 1,
    });
    if (expanded.has("evidence")) {
      for (const secondary of ["data", "code", "input"] as const) {
        const group = `evidence:${secondary}` as GroupId;
        rows.push({
          kind: "group",
          key: `group:${group}`,
          group,
          count: counts[secondary],
          level: 2,
        });
        if (expanded.has(group)) {
          rows.push(...items
            .filter((item) => item.secondary_group === secondary)
            .map((item) => ({ kind: "item" as const, key: item.key, item })));
        }
      }
    }
  }
  return rows;
}

export function ResultsExplorer(props: Props) {
  const {
    anchor,
    authorizedTotal,
    checkingChanges,
    compactFilters = false,
    completedView,
    entryId,
    error,
    errorDirection,
    errorAnchorKey,
    expandedGroups,
    filters,
    focusKey,
    groupCounts,
    hasMore,
    hasPrevious,
    items,
    loadedCount,
    loading,
    loadingMore,
    loadingPrevious,
    matchedTotal,
    newResultsCount,
    projectionChanged,
    projectionStatus,
    query,
    selectedKey,
    onAcceptChanges,
    onAnchorChange,
    onExpandedGroupsChange,
    onFiltersChange,
    onFocusKeyChange,
    onLoadNext,
    onLoadPrevious,
    onQueryChange,
    onRetry,
    onSelect,
  } = props;
  const t = useTranslations("results_workspace");
  const locale = useLocale();
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const composingRef = useRef(false);
  const anchorTimerRef = useRef<number | null>(null);
  const restoredEntryRef = useRef<string | null>(null);
  const viewKeyRef = useRef("");
  const [draftQuery, setDraftQuery] = useState(query);
  useEffect(() => setDraftQuery(query), [query]);
  const displayedFilters = completedView?.filters ?? DEFAULT_EXPLORER_FILTERS;
  const displayedQuery = completedView?.query ?? "";
  const conditionsDiffer = displayedQuery !== query.trim()
    || displayedFilters.lens !== filters.lens
    || displayedFilters.primary !== filters.primary
    || displayedFilters.secondary !== filters.secondary
    || displayedFilters.technical_only !== filters.technical_only;
  const conditionsLabel = (value: ResultsFiltersDTO, search: string) => [
    primaryFilterTitle(value.primary, t),
    value.secondary !== "all" ? secondaryFilterTitle(value.secondary, t) : null,
    value.lens !== "all" ? t("explorer_current_report") : null,
    value.technical_only ? t("explorer_technical") : null,
    search.trim() ? `“${search.trim()}”` : null,
  ].filter(Boolean).join(" · ");

  const effectiveExpandedGroups = useMemo(() => {
    const next = new Set(expandedGroups);
    const selected = items.find((item) => item.key === selectedKey || item.key === focusKey);
    if (selected?.primary_group === "deliverables") next.add("deliverables");
    if (selected?.primary_group === "evidence") {
      next.add("evidence");
      if (selected.secondary_group) next.add(`evidence:${selected.secondary_group}`);
    }
    return [...next];
  }, [expandedGroups, focusKey, items, selectedKey]);
  const rows = useMemo(() => buildResultsExplorerRows({
    expandedGroups: effectiveExpandedGroups,
    filters: displayedFilters,
    groupCounts,
    items,
    query: displayedQuery,
  }), [displayedFilters, displayedQuery, effectiveExpandedGroups, groupCounts, items]);
  const forcedIndexes = useMemo(() => {
    const indexes = [selectedKey, focusKey]
      .filter((key): key is string => Boolean(key))
      .map((key) => rows.findIndex((row) => row.kind === "item" && row.key === key))
      .filter((index) => index >= 0);
    return [...new Set(indexes)];
  }, [focusKey, rows, selectedKey]);
  const rangeExtractor = useCallback((range: Range) => (
    [...new Set([...defaultRangeExtractor(range), ...forcedIndexes])].sort((a, b) => a - b)
  ), [forcedIndexes]);
  const virtual = items.length >= 50;
  const virtualizer = useVirtualizer({
    count: rows.length,
    enabled: virtual,
    estimateSize: (index) => estimateRowSize(rows[index]),
    getItemKey: (index) => rows[index]?.key ?? index,
    getScrollElement: () => scrollerRef.current,
    initialRect: { height: 600, width: 0 },
    overscan: 8,
    rangeExtractor,
  });
  const viewKey = [
    displayedQuery,
    displayedFilters.lens,
    displayedFilters.primary,
    displayedFilters.secondary,
    displayedFilters.technical_only ? "1" : "0",
  ].join("\n");

  useEffect(() => {
    if (restoredEntryRef.current === entryId) return;
    if (!anchor) {
      restoredEntryRef.current = entryId;
      return;
    }
    const index = rows.findIndex((row) => row.kind === "item" && row.key === anchor.key);
    if (index < 0) return;
    restoredEntryRef.current = entryId;
    requestAnimationFrame(() => {
      if (virtual) {
        virtualizer.scrollToIndex(index, { align: "start" });
        if (scrollerRef.current) scrollerRef.current.scrollTop += anchor.offset_px;
        return;
      }
      const element = scrollerRef.current?.querySelector<HTMLElement>(
        `[data-result-key="${CSS.escape(anchor.key)}"]`,
      );
      element?.scrollIntoView({ block: "start" });
      if (scrollerRef.current) scrollerRef.current.scrollTop += anchor.offset_px;
    });
  }, [anchor, entryId, rows, virtual, virtualizer]);

  useEffect(() => {
    if (viewKeyRef.current === "") {
      viewKeyRef.current = viewKey;
      return;
    }
    if (viewKeyRef.current === viewKey) return;
    viewKeyRef.current = viewKey;
    if (scrollerRef.current) scrollerRef.current.scrollTop = 0;
  }, [viewKey]);

  const captureAnchor = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const scrollerTop = scroller.getBoundingClientRect().top;
    const visible = Array.from(
      scroller.querySelectorAll<HTMLElement>("[data-result-key]"),
    ).find((element) => element.getBoundingClientRect().bottom > scrollerTop);
    const key = visible?.dataset.resultKey;
    onAnchorChange(key
      ? { key, offset_px: Math.round((visible?.getBoundingClientRect().top ?? scrollerTop) - scrollerTop) }
      : null);
  }, [onAnchorChange]);
  useEffect(() => () => {
    if (anchorTimerRef.current !== null) window.clearTimeout(anchorTimerRef.current);
  }, []);
  const scheduleAnchor = () => {
    if (anchorTimerRef.current !== null) window.clearTimeout(anchorTimerRef.current);
    anchorTimerRef.current = window.setTimeout(() => {
      anchorTimerRef.current = null;
      captureAnchor();
    }, 120);
  };
  const commitQuery = (value: string) => onQueryChange(truncateCodePoints(value, 120));
  const onCompositionEnd = (event: CompositionEvent<HTMLInputElement>) => {
    composingRef.current = false;
    const value = truncateCodePoints(event.currentTarget.value, 120);
    setDraftQuery(value);
    commitQuery(value);
  };
  const toggleGroup = (group: GroupId) => {
    const next = new Set(expandedGroups);
    if (next.has(group)) next.delete(group);
    else next.add(group);
    onExpandedGroupsChange([...next]);
  };
  const applyPrimary = (primary: ResultsFiltersDTO["primary"]) => {
    onFiltersChange({
      ...filters,
      primary,
      secondary: primary === "evidence" ? filters.secondary : "all",
    });
  };
  const applySecondary = (secondary: ResultsFiltersDTO["secondary"]) => {
    onFiltersChange({
      ...filters,
      primary: secondary === "all" ? filters.primary : "evidence",
      secondary,
    });
  };
  const changeAvailable = projectionChanged || (newResultsCount ?? 0) > 0;
  const initialError = Boolean(error && items.length === 0);
  const initialLoading = loading && items.length === 0;
  const empty = !loading && !error && items.length === 0;
  const conditionsActive = Boolean(
    displayedQuery.trim()
    || displayedFilters.lens !== "all"
    || displayedFilters.primary !== "all"
    || displayedFilters.secondary !== "all"
    || displayedFilters.technical_only,
  );
  const targetConditionsActive = Boolean(query.trim() || filters.lens !== "all"
    || filters.primary !== "all" || filters.secondary !== "all" || filters.technical_only);
  // A locate error also covers missing/unavailable objects. An unfiltered view
  // cannot possibly prove a filter mismatch; an unloaded page proves nothing.
  const anchorError = !loading && error === "results_anchor_not_in_view"
    && errorAnchorKey !== null && errorAnchorKey === (selectedKey ?? anchor?.key);
  const anchorMessage = targetConditionsActive
    ? "explorer_anchor_not_in_view" : "explorer_anchor_unavailable";
  const recoverAnchor = anchorError && targetConditionsActive;
  const statusText = !completedView
    ? t(error ? "quick_error" : "list_loading")
    : conditionsActive
    ? t("explorer_match_status", {
        matched: matchedTotal ?? 0,
        loaded: loadedCount,
      })
    : t("explorer_total_status", {
        total: authorizedTotal ?? 0,
        loaded: loadedCount,
      });

  const renderRow = (row: ResultsExplorerRow) => {
    if (row.kind === "group") {
      const expanded = effectiveExpandedGroups.includes(row.group);
      return (
        <div
          className={clsx("results-explorer-group", { nested: row.level === 2 })}
          role="heading"
          aria-level={row.level + 2}
        >
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => toggleGroup(row.group)}
          >
            <span aria-hidden><CharIcon ch={expanded ? "▾" : "▸"} aria-hidden /></span>
            <strong>{groupTitle(row.group, t)}</strong>
            <span>{row.count}</span>
          </button>
        </div>
      );
    }
    const selected = row.key === selectedKey;
    const context = itemContext(row.item, locale, t);
    return (
      <div
        role="listitem"
        aria-posinset={row.item.position}
        aria-setsize={matchedTotal ?? undefined}
        data-result-key={row.key}
        className="results-explorer-listitem"
      >
        <button
          type="button"
          className={clsx("results-explorer-row", { selected })}
          aria-current={selected ? "page" : undefined}
          onClick={() => {
            if (anchorTimerRef.current !== null) {
              window.clearTimeout(anchorTimerRef.current);
              anchorTimerRef.current = null;
            }
            captureAnchor();
            onSelect(row.item);
          }}
          onFocus={() => onFocusKeyChange(row.key)}
        >
          <span className="results-explorer-badge" aria-hidden>{itemBadge(row.item)}</span>
          <span className="results-explorer-row-copy">
            <strong>{resultDisplayTitle(row.item, t("code"))}</strong>
            <span>{context}</span>
          </span>
          <span className="results-explorer-open" aria-hidden><CharIcon ch="›" aria-hidden /></span>
        </button>
      </div>
    );
  };

  const measuredVirtualRows = virtual ? virtualizer.getVirtualItems() : [];
  const fallbackVirtualRows = useMemo(() => {
    if (!virtual || measuredVirtualRows.length > 0) return [];
    const indexes = [...new Set([
      ...Array.from({ length: Math.min(rows.length, 12) }, (_, index) => index),
      ...forcedIndexes,
    ])].sort((a, b) => a - b);
    const starts = rows.reduce<number[]>((positions, row, index) => {
      positions[index + 1] = positions[index] + estimateRowSize(row);
      return positions;
    }, [0]);
    return indexes.map((index) => ({
      end: starts[index + 1],
      index,
      key: rows[index]?.key ?? index,
      lane: 0,
      size: estimateRowSize(rows[index]),
      start: starts[index],
    }));
  }, [forcedIndexes, measuredVirtualRows.length, rows, virtual]);
  const virtualRows = measuredVirtualRows.length > 0
    ? measuredVirtualRows
    : fallbackVirtualRows;
  const estimatedTotalSize = useMemo(
    () => rows.reduce((total, row) => total + estimateRowSize(row), 0),
    [rows],
  );
  const activeFilterCount = (
    (displayedFilters.lens !== "all" ? 1 : 0)
    + (displayedFilters.secondary !== "all" || displayedFilters.primary !== "all" ? 1 : 0)
    + (displayedFilters.technical_only ? 1 : 0)
  );
  const renderFilterSet = () => (
    <>
      <SegmentedControl
        className="results-explorer-filter-row"
        aria-label={t("explorer_primary_filters")}
        value={displayedFilters.primary}
        onValueChange={applyPrimary}
      >
        {(["all", "deliverables", "evidence"] as const).map((primary) => (
          <Segment
            key={primary}
            value={primary}
            aria-busy={loading && conditionsDiffer && filters.primary === primary}
          >
            {primaryFilterTitle(primary, t)}
            {loading && conditionsDiffer && filters.primary === primary ? <span aria-hidden>…</span> : null}
          </Segment>
        ))}
      </SegmentedControl>
      <SegmentedControl
        className="results-explorer-filter-row"
        aria-label={t("explorer_secondary_filters")}
        value={displayedFilters.secondary}
        onValueChange={applySecondary}
      >
        {(["all", "data", "code", "input"] as const).map((secondary) => (
          <Segment
            key={secondary}
            value={secondary}
            aria-busy={loading && conditionsDiffer && filters.secondary === secondary}
          >
            {secondaryFilterTitle(secondary, t)}
            {loading && conditionsDiffer && filters.secondary === secondary ? <span aria-hidden>…</span> : null}
          </Segment>
        ))}
      </SegmentedControl>
      {projectionStatus?.report_relations !== "unavailable" ? (
        <label className="results-explorer-lens">
          <input
            type="checkbox"
            checked={displayedFilters.lens === "current_report"}
            aria-busy={loading && displayedFilters.lens !== filters.lens}
            disabled={projectionStatus?.report_relations !== "ready"}
            onChange={(event) => onFiltersChange({
              ...filters,
              lens: event.currentTarget.checked ? "current_report" : "all",
            })}
          />
          <span>{t("explorer_current_report")}</span>
        </label>
      ) : null}
      {projectionStatus?.execution_relations === "ready" ? (
        <label className="results-explorer-lens">
          <input
            type="checkbox"
            checked={displayedFilters.technical_only}
            aria-busy={loading && displayedFilters.technical_only !== filters.technical_only}
            onChange={(event) => onFiltersChange({
              ...filters,
              technical_only: event.currentTarget.checked,
            })}
          />
          <span>{t("explorer_technical")}</span>
        </label>
      ) : null}
    </>
  );
  const pageError = error && !initialError ? (
    <div className="results-explorer-page-error" role="status">
      <span>{t(anchorError
        ? anchorMessage
        : "explorer_page_error")}</span>
      <button
        type="button"
        onClick={recoverAnchor
          ? () => clearConditions(setDraftQuery, onQueryChange, onFiltersChange)
          : onRetry}
      >
        {t(recoverAnchor
          ? "explorer_clear_and_show"
          : anchorError ? "explorer_retry_locate" : "list_retry")}
      </button>
    </div>
  ) : null;
  return (
    <section className="results-explorer-pane" aria-labelledby="results-explorer-heading">
      <header className="results-explorer-head">
        <div>
          <h2 id="results-explorer-heading">{t("explorer_title")}</h2>
          <p aria-live="polite">{statusText}</p>
        </div>
        {checkingChanges ? <span>{t("explorer_checking")}</span> : null}
      </header>
      <div className="results-explorer-controls">
        <input
          type="search"
          value={draftQuery}
          placeholder={t("quick_search")}
          aria-label={t("quick_search")}
          onChange={(event) => {
            const rawValue = event.currentTarget.value;
            const value = composingRef.current
              ? rawValue
              : truncateCodePoints(rawValue, 120);
            setDraftQuery(value);
            if (!composingRef.current) commitQuery(value);
          }}
          onCompositionStart={() => { composingRef.current = true; }}
          onCompositionEnd={onCompositionEnd}
        />
        {compactFilters ? (
          <details className="results-explorer-compact-filters">
            <summary>
              <span>{t("explorer_filters")}</span>
              {activeFilterCount > 0 ? <strong>{activeFilterCount}</strong> : null}
            </summary>
            <div className="results-explorer-filter-set">
              {renderFilterSet()}
            </div>
          </details>
        ) : (
          <div className="results-explorer-filter-set">
            {renderFilterSet()}
          </div>
        )}
      </div>
      {conditionsDiffer ? (
        <div className="results-explorer-query-status" role="status">
          <span>{t(loading ? "explorer_query_pending" : "explorer_query_failed", {
            target: conditionsLabel(filters, query),
          })}</span>
          {completedView ? <span>{t("explorer_query_showing", {
            completed: conditionsLabel(displayedFilters, displayedQuery),
          })}</span> : null}
        </div>
      ) : null}
      {changeAvailable ? (
        <div className="results-explorer-new" role="status">
          <span>{projectionChanged
            ? t("explorer_projection_changed")
            : t("explorer_new_results", { count: newResultsCount ?? 0 })}</span>
          <button type="button" onClick={onAcceptChanges}>{t("explorer_refresh")}</button>
        </div>
      ) : null}
      {hasPrevious && !(error && errorDirection === "previous") ? (
        <button
          type="button"
          className="results-explorer-page-action"
          disabled={loading || loadingMore || loadingPrevious || conditionsDiffer}
          onClick={onLoadPrevious}
        >
          {loadingPrevious ? t("list_loading_more") : t("explorer_load_previous")}
        </button>
      ) : null}
      {errorDirection === "previous" ? pageError : null}
      <div
        ref={scrollerRef}
        className="results-explorer-scroll"
        role="list"
        aria-label={t("explorer_results")}
        aria-busy={loading}
        onScroll={scheduleAnchor}
      >
        {initialLoading ? (
          <div className="results-explorer-state" role="status">
            <strong>{t("list_loading")}</strong>
          </div>
        ) : initialError ? (
          <div className="results-explorer-state" role="alert">
            <ResultsStateIcon tone="error" />
            <strong>{t(anchorError ? anchorMessage : "quick_error")}</strong>
            <button
              type="button"
              onClick={recoverAnchor
                ? () => clearConditions(setDraftQuery, onQueryChange, onFiltersChange)
                : onRetry}
            >
              {t(recoverAnchor
                ? "explorer_clear_and_show"
                : anchorError ? "explorer_retry_locate" : "list_retry")}
            </button>
          </div>
        ) : empty ? (
          <div className="results-explorer-state">
            <ResultsStateIcon tone={conditionsActive ? "no-match" : "empty"} />
            <strong>{t(displayedQuery.trim() ? "search_empty" : "results_empty")}</strong>
            {conditionsActive ? (
              <button
                type="button"
                onClick={() => clearConditions(
                  setDraftQuery,
                  onQueryChange,
                  onFiltersChange,
                )}
              >
                {t("explorer_clear_conditions")}
              </button>
            ) : null}
          </div>
        ) : virtual ? (
          <div
            className="results-explorer-virtual"
            style={{ height: measuredVirtualRows.length > 0
              ? virtualizer.getTotalSize()
              : estimatedTotalSize }}
          >
            {virtualRows.map((virtualRow) => {
              const row = rows[virtualRow.index];
              return row ? (
                <div
                  key={row.key}
                  ref={virtualizer.measureElement}
                  data-index={virtualRow.index}
                  className="results-explorer-virtual-row"
                  style={{ transform: `translateY(${virtualRow.start}px)` }}
                >
                  {renderRow(row)}
                </div>
              ) : null;
            })}
          </div>
        ) : rows.map((row) => <div key={row.key}>{renderRow(row)}</div>)}
      </div>
      {errorDirection !== "previous" ? pageError : null}
      {hasMore && !error ? (
        <button
          type="button"
          className="results-explorer-page-action"
          disabled={loading || loadingMore || loadingPrevious || conditionsDiffer}
          onClick={onLoadNext}
        >
          {loadingMore ? t("list_loading_more") : t("list_load_more")}
        </button>
      ) : null}
    </section>
  );
}

const DEFAULT_EXPLORER_FILTERS: ResultsFiltersDTO = {
  lens: "all",
  primary: "all",
  secondary: "all",
  technical_only: false,
};

function estimateRowSize(row: ResultsExplorerRow | undefined): number {
  return row?.kind === "group" ? 44 : 62;
}

function truncateCodePoints(value: string, limit: number): string {
  return Array.from(value).slice(0, limit).join("");
}

function clearConditions(
  setDraftQuery: (value: string) => void,
  onQueryChange: (query: string) => void,
  onFiltersChange: (filters: ResultsFiltersDTO) => void,
): void {
  setDraftQuery("");
  onQueryChange("");
  onFiltersChange({ ...DEFAULT_EXPLORER_FILTERS });
}

function groupTitle(group: GroupId, t: ReturnType<typeof useTranslations>): string {
  if (group === "deliverables") return t("deliverables");
  if (group === "evidence") return t("evidence");
  if (group === "evidence:data") return t("data");
  if (group === "evidence:code") return t("code_group");
  return t("input_files");
}

function primaryFilterTitle(
  primary: ResultsFiltersDTO["primary"],
  t: ReturnType<typeof useTranslations>,
): string {
  if (primary === "all") return t("explorer_all");
  return primary === "deliverables" ? t("deliverables") : t("evidence");
}

function secondaryFilterTitle(
  secondary: ResultsFiltersDTO["secondary"],
  t: ReturnType<typeof useTranslations>,
): string {
  if (secondary === "all") return t("explorer_all_types");
  if (secondary === "data") return t("data");
  if (secondary === "code") return t("code_group");
  return t("input_files");
}

function itemBadge(item: ResultItemDTO): string {
  if (item.object_type === "report") return "DOC";
  if (item.object_type === "code") return "</>";
  const extension = item.title.split(".").pop()?.toUpperCase();
  return extension && extension.length <= 5 ? extension : item.viewer_profile.slice(0, 4).toUpperCase();
}

function itemContext(
  item: ResultItemDTO,
  locale: string,
  t: ReturnType<typeof useTranslations>,
): string {
  const type = item.object_type === "report"
    ? t("report")
    : item.object_type === "code"
      ? t("code_group")
      : item.primary_group === "deliverables"
        ? t("quick_type_deliverable")
        : item.secondary_group === "input"
          ? t("input_files")
          : item.secondary_group === "code"
            ? t("code_group")
          : t("data");
  const context = item.display_context;
  const pieces = [type];
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
        year: "numeric",
        month: "short",
        day: "numeric",
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
  t: ReturnType<typeof useTranslations>,
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
