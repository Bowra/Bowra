"use client";

import clsx from "clsx";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Pin } from "@/components/icons";
import {
  resolveSelectedResult,
  type ResultItem,
  type ResultSecondaryGroup,
} from "@/lib/results-workspace-model";
import { CharIcon } from "@/components/char-icon";
import styles from "./results-navigator.module.css";

const AUTO_EXPAND_RUN_ITEM_LIMIT = 30;

export type ResultsSelection =
  | { type: "report" }
  | { type: "artifact"; artifactId: string }
  | { type: "code" }
  | { type: "none" };

type Props = {
  items: readonly ResultItem[];
  open: boolean;
  query: string;
  selection: ResultsSelection;
  presentation?: "overlay" | "pinned" | "mobile" | "inline";
  canPin?: boolean;
  listState?: {
    searchMode: "local" | "server";
    completeness: "complete" | "partial";
    loadedCount: number;
    authorizedTotal: number | null;
    matchedTotal: number | null;
    loading: boolean;
    loadingMore: boolean;
    hasMore: boolean;
    error: boolean;
  };
  onPinnedChange?: (pinned: boolean) => void;
  onClose: () => void;
  onQueryChange: (query: string) => void;
  onSelect: (item: ResultItem) => void;
  onLoadMore?: () => void;
  onRetry?: () => void;
};

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
}

export function ResultsNavigator({
  items,
  open,
  query,
  selection,
  presentation = "overlay",
  canPin = false,
  listState,
  onPinnedChange,
  onClose,
  onQueryChange,
  onSelect,
  onLoadMore,
  onRetry,
}: Props) {
  const t = useTranslations("results_workspace");
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open || presentation === "pinned" || presentation === "inline") return;
    const frame = requestAnimationFrame(() => {
      const dialog = dialogRef.current;
      const target =
        (presentation === "overlay"
          ? dialog?.querySelector<HTMLElement>(".results-nav-search")
          : null)
        ?? dialog?.querySelector<HTMLElement>('[aria-current="page"]')
        ?? dialog?.querySelector<HTMLElement>(".results-nav-item")
        ?? dialog?.querySelector<HTMLElement>(".results-nav-head button");
      target?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [open, presentation]);

  useEffect(() => {
    if (!open || presentation !== "overlay") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const workspace = dialog.closest<HTMLElement>(".v2-workspace");
    const inertTargets = new Set<HTMLElement>();
    const addTarget = (target: Element | null) => {
      if (target instanceof HTMLElement) inertTargets.add(target);
    };
    addTarget(workspace?.querySelector(".v2-ws-left") ?? null);
    addTarget(workspace?.querySelector(".results-workspace-body") ?? null);
    addTarget(workspace?.querySelector(".v2-ws-right-head > .r-actions") ?? null);
    for (const sibling of Array.from(dialog.parentElement?.children ?? [])) {
      if (sibling !== dialog && !sibling.classList.contains("results-nav-scrim")) {
        addTarget(sibling);
      }
    }
    const priorInert = new Map<HTMLElement, boolean>();
    for (const target of inertTargets) {
      priorInert.set(target, target.inert);
      target.inert = true;
    }
    const focusFirst = () => {
      const focusable = focusableElements(dialog);
      (focusable[0] ?? dialog).focus();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableElements(dialog);
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const onFocusIn = (event: FocusEvent) => {
      if (!(event.target instanceof Node) || dialog.contains(event.target)) return;
      focusFirst();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      for (const [target, inert] of priorInert) target.inert = inert;
    };
  }, [onClose, open, presentation]);

  if (!open) return null;

  const normalizedQuery = query.trim().toLowerCase();
  const visibleItems = normalizedQuery && listState?.searchMode !== "server"
    ? items.filter((item) =>
      item.title.toLowerCase().includes(normalizedQuery)
      || item.metadata.toLowerCase().includes(normalizedQuery),
    )
    : [...items];
  const deliverables = visibleItems.filter((item) =>
    item.primaryGroup === "deliverables",
  );
  const evidence = visibleItems.filter((item) =>
    item.primaryGroup === "evidence",
  );
  const allDataCount = items.filter((item) => (
    item.primaryGroup === "evidence" && item.secondaryGroup === "data"
  )).length;
  const selectedItem = resolveSelectedResult(items, selection);
  const select = (item: ResultItem) => {
    onSelect(item);
    if (presentation !== "pinned" && presentation !== "inline") onClose();
  };
  const overlay = presentation === "overlay";

  return (
    <>
      {overlay ? (
        <button
          type="button"
          className="results-nav-scrim"
          aria-hidden="true"
          tabIndex={-1}
          onClick={onClose}
        />
      ) : null}
      <div
        ref={dialogRef}
        className={clsx("results-navigator", `results-navigator--${presentation}`, presentation === "inline" && styles.inline)}
        role={overlay ? "dialog" : undefined}
        aria-modal={overlay ? "true" : undefined}
        aria-label={t("navigator_label")}
        tabIndex={overlay ? -1 : undefined}
      >
        <div className="results-nav-head">
          <div>
            <strong>{t("title")}</strong>
            <span>{t("navigator_hint")}</span>
          </div>
          <div className="results-nav-head-actions">
            {canPin && onPinnedChange ? (
              <button
                type="button"
                aria-label={t(presentation === "pinned" ? "unpin_navigator" : "pin_navigator")}
                aria-pressed={presentation === "pinned"}
                onClick={() => onPinnedChange(presentation !== "pinned")}
              >
                <Pin size={15} aria-hidden />
              </button>
            ) : null}
            {presentation !== "pinned" && presentation !== "inline" ? (
              <button type="button" onClick={onClose} aria-label={t("close_navigator")}>
                <CharIcon ch="×" aria-hidden />
              </button>
            ) : null}
          </div>
        </div>
        <input
          type="search"
          className="results-nav-search"
          value={query}
          placeholder={t(
            listState?.completeness === "partial"
              ? "search_loaded_results"
              : "search_results",
          )}
          aria-label={t("search_results")}
          onChange={(event) => onQueryChange(event.currentTarget.value)}
        />
        <nav aria-label={t("navigator_label")}>
          {deliverables.length > 0 ? (
            <ResultGroup title={t("deliverables")}>
              {deliverables.map((item) => (
                <ResultButton
                  key={item.key}
                  item={item}
                  active={selectedItem?.key === item.key}
                  onClick={() => select(item)}
                />
              ))}
            </ResultGroup>
          ) : null}
          {evidence.length > 0 ? (
            <ResultGroup title={t("evidence")}>
              {(["data", "code", "input"] as const).map((secondary) => (
                <EvidenceGroup
                  key={secondary}
                  title={secondaryGroupTitle(secondary, t)}
                  items={evidence.filter((item) => item.secondaryGroup === secondary)}
                  groupRuns={secondary === "data" && allDataCount >= 30}
                  queryActive={Boolean(normalizedQuery)}
                  selectedKey={selectedItem?.key ?? null}
                  onSelect={select}
                />
              ))}
            </ResultGroup>
          ) : null}
          {visibleItems.length === 0 ? (
            <p className="results-nav-empty results-nav-search-empty">
              {listState?.loading
                ? t("list_loading")
                : t(normalizedQuery ? "search_empty" : "results_empty")}
            </p>
          ) : null}
        </nav>
        {listState ? (
          <div className="results-nav-footer" aria-live="polite">
            <span>
              {listState.completeness === "partial"
                ? t("list_partial", { count: listState.loadedCount })
                : normalizedQuery
                  ? t("list_matches", {
                      matched: listState.matchedTotal ?? listState.loadedCount,
                      loaded: listState.loadedCount,
                    })
                  : t("list_complete", {
                      loaded: listState.loadedCount,
                      total: listState.authorizedTotal ?? listState.loadedCount,
                    })}
            </span>
            {listState.error && onRetry ? (
              <button type="button" onClick={onRetry}>
                {t("list_retry")}
              </button>
            ) : listState.hasMore && onLoadMore ? (
              <button
                type="button"
                disabled={listState.loadingMore}
                onClick={onLoadMore}
              >
                {t(listState.loadingMore ? "list_loading_more" : "list_load_more")}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </>
  );
}

export function resultsSelectionTitle(
  selection: ResultsSelection,
  items: readonly ResultItem[],
  fallback: { report: string; code: string; missing: string },
): string {
  return resolveSelectedResult(items, selection)?.title
    ?? (selection.type === "report"
      ? fallback.report
      : selection.type === "code"
        ? fallback.code
        : fallback.missing);
}

function ResultGroup({
  children,
  title,
}: {
  children: React.ReactNode;
  title: string;
}) {
  return (
    <section className="results-nav-group">
      <h3>{title}</h3>
      <div>{children}</div>
    </section>
  );
}

function EvidenceGroup({
  groupRuns,
  items,
  onSelect,
  queryActive,
  selectedKey,
  title,
}: {
  groupRuns: boolean;
  items: readonly ResultItem[];
  onSelect: (item: ResultItem) => void;
  queryActive: boolean;
  selectedKey: string | null;
  title: string;
}) {
  const t = useTranslations("results_workspace");
  const [runExpansionOverrides, setRunExpansionOverrides] = useState<Map<string, boolean>>(
    () => new Map(),
  );
  const runMap = new Map<string, ResultItem[]>();
  const unscoped: ResultItem[] = [];
  for (const item of items) {
    if (!groupRuns || !item.runId) {
      unscoped.push(item);
      continue;
    }
    const group = runMap.get(item.runId) ?? [];
    group.push(item);
    runMap.set(item.runId, group);
  }
  const runGroups = Array.from(runMap, ([runId, runItems]) => ({ runId, items: runItems }))
    .sort((a, b) => (b.items[0]?.completedAt ?? "").localeCompare(a.items[0]?.completedAt ?? ""));
  const selectedRunId = runGroups.find((group) => (
    group.items.some((item) => item.key === selectedKey)
  ))?.runId ?? null;

  if (items.length === 0) return null;
  return (
    <section className="results-nav-subgroup">
      <h4>{title}</h4>
      {runGroups.map((group, index) => {
        const defaultExpanded = index === 0 && group.items.length < AUTO_EXPAND_RUN_ITEM_LIMIT;
        const expansionOverride = runExpansionOverrides.get(group.runId);
        const expanded = queryActive
          || group.runId === selectedRunId
          || expansionOverride
          || (expansionOverride === undefined && defaultExpanded);
        return (
          <section className="results-nav-run-group" key={group.runId}>
            <button
              type="button"
              className="results-nav-run-toggle"
              aria-expanded={expanded}
              onClick={() => setRunExpansionOverrides((current) => {
                const next = new Map(current);
                next.set(group.runId, !expanded);
                return next;
              })}
            >
              <span>{t(index === 0 ? "run_group_recent" : "run_group_earlier", { index: index + 1 })}</span>
              <span>{t("run_group_count", { count: group.items.length })}</span>
            </button>
            {expanded ? group.items.map((item) => (
              <ResultButton
                key={item.key}
                item={item}
                active={selectedKey === item.key}
                nested
                onClick={() => onSelect(item)}
              />
            )) : null}
          </section>
        );
      })}
      {unscoped.map((item) => (
        <ResultButton
          key={item.key}
          item={item}
          active={selectedKey === item.key}
          onClick={() => onSelect(item)}
        />
      ))}
    </section>
  );
}

function ResultButton({
  active,
  item,
  nested = false,
  onClick,
}: {
  active: boolean;
  item: ResultItem;
  nested?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={clsx("results-nav-item", { active, nested })}
      aria-current={active ? "page" : undefined}
      onClick={onClick}
      title={item.title}
    >
      <span className="results-nav-badge" aria-hidden>{resultBadge(item)}</span>
      <span className="results-nav-copy">
        <strong>{item.title}</strong>
        <span className={clsx({
          running: item.status === "running" || item.status === "processing",
          failed: item.status === "failed",
        })}>
          {item.metadata}
        </span>
      </span>
    </button>
  );
}

function resultBadge(item: ResultItem): string {
  if (item.objectType === "report") return "DOC";
  if (item.objectType === "code") return "</>";
  if (item.version) return item.version.format.toUpperCase();
  switch (item.viewerProfile) {
    case "html": return "HTML";
    case "image": return "IMG";
    case "video": return "VID";
    case "table": return "DATA";
    case "text": return "TXT";
    case "document": return "DOC";
    default: return "FILE";
  }
}

function secondaryGroupTitle(
  group: NonNullable<ResultSecondaryGroup>,
  t: (key: string) => string,
): string {
  if (group === "data") return t("data");
  if (group === "code") return t("code_group");
  return t("input_files");
}
