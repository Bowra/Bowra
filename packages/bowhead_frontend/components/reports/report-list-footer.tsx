"use client";

/**
 * Footer of the report library list (Header, Language & Paging Spec §3, D8): progress "Showing N of M", a manual
 * "Load more" (no auto-loading, so "Shared with me" below stays reachable), the in-place loading state, a failure
 * line with Retry that keeps the loaded rows, and the closing line once everything is shown. With a single batch
 * the whole footer is absent; the toolbar count already says how many there are.
 */

import { useTranslations } from "next-intl";

import { ErrorDiagnosticsInline } from "@/components/error-notice";

/** Rows per batch: the server page size the library requests. */
export const REPORTS_PAGE_SIZE = 50;

export function ReportListFooter({
  shown,
  total,
  hasMore,
  loadingMore,
  loadMoreError,
  searching,
  onLoadMore,
  announcement,
}: {
  /** Rows on screen. */
  shown: number;
  /** Reports matching the current search and filters (first-page count). */
  total: number;
  hasMore: boolean;
  loadingMore: boolean;
  loadMoreError: unknown;
  /** A search term is active: "found" replaces "of" in the counts. */
  searching: boolean;
  onLoadMore: () => void;
  /** Polite announcement after a batch arrives ("Loaded 50. Showing 150 of 236"). */
  announcement: string;
}) {
  const t = useTranslations("reports_page");
  // Only one batch and nothing more: no footer (Misc Spec pagination — a single page hides the whole bar).
  if (!hasMore && shown <= REPORTS_PAGE_SIZE) {
    return <span role="status" aria-live="polite" className="unified-work-sr-only" />;
  }
  const progress = searching
    ? t("count_progress_found", { shown, total })
    : t("count_progress", { shown, total });
  return (
    <div className="reports-list-footer" data-testid="reports-list-footer">
      {!hasMore ? (
        <p className="reports-list-footer__end" data-testid="reports-all-loaded">
          {t("all_loaded", { total: Math.max(total, shown) })}
        </p>
      ) : loadMoreError ? (
        <div className="reports-list-footer__error" role="alert" data-testid="reports-load-more-error">
          <span>{t("load_more_failed", { shown })}</span>
          <button
            type="button"
            className="settings-button settings-button--secondary settings-button--md reports-list-footer__retry"
            onClick={onLoadMore}
          >
            {t("retry")}
          </button>
          <ErrorDiagnosticsInline error={loadMoreError} surface="route" />
        </div>
      ) : (
        <>
          <span className="reports-list-footer__count" data-testid="reports-progress">{progress}</span>
          <button
            type="button"
            className="settings-button settings-button--secondary settings-button--md reports-list-footer__more"
            data-testid="reports-load-more"
            aria-busy={loadingMore || undefined}
            aria-disabled={loadingMore || undefined}
            onClick={() => {
              if (!loadingMore) onLoadMore();
            }}
          >
            {/* aria-busy draws the shared button spinner (styles/ui-refresh.css); no second ring here. */}
            <span>{loadingMore ? t("loading_more") : t("load_more")}</span>
          </button>
        </>
      )}
      <span role="status" aria-live="polite" className="unified-work-sr-only">
        {announcement}
      </span>
    </div>
  );
}
