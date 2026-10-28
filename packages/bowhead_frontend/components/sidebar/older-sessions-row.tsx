"use client";

/**
 * The last row of the session list (Header, Language & Paging spec 4a, D12): a quiet text row that loads the next
 * 50 older sessions in place. Shared by the desktop sidebar (expanded, overlay, compact flyout) and the phone drawer.
 *
 * - more to load: "Show older" (32 high, 44 on touch and in the drawer);
 * - loading: three session-height skeleton rows after the shared 300ms delay;
 * - failed: an error row with Retry replaces the row; loaded sessions stay;
 * - nothing older after at least one load: "No older sessions" in ink4;
 * - otherwise (the list fits one batch): nothing.
 *
 * Never loads on scroll: the identity row sits under the list and would keep jumping.
 */

import { useTranslations } from "next-intl";
import { ChevronDown, CircleAlert } from "lucide-react";
import clsx from "clsx";

import { LucideGlyph } from "@/components/icon-refresh";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import type { SessionListState } from "@/lib/use-sessions";

export interface OlderSessionsPaging {
  hasMore: boolean;
  loadingMore: boolean;
  loadMoreError: boolean;
  /** Older pages loaded so far; the end state needs at least one. */
  olderPagesLoaded: number;
  /** Sessions the latest older page added, in order: focus goes to the first, the count is announced. */
  latestOlderPageIds?: string[];
  onLoadMore: () => void | Promise<void>;
}

/** A session list's paging in the shape the row takes. */
export function olderSessionsPaging(
  list: Pick<
    SessionListState,
    | "hasMore"
    | "loadingMore"
    | "loadMoreError"
    | "olderPagesLoaded"
    | "latestOlderPageIds"
    | "loadMore"
  >,
): OlderSessionsPaging {
  return {
    hasMore: Boolean(list.hasMore),
    loadingMore: Boolean(list.loadingMore),
    loadMoreError: Boolean(list.loadMoreError),
    olderPagesLoaded: list.olderPagesLoaded ?? 0,
    latestOlderPageIds: list.latestOlderPageIds ?? [],
    onLoadMore: list.loadMore,
  };
}

export function OlderSessionsRow({
  paging,
  surface,
}: {
  paging: OlderSessionsPaging;
  surface?: "expanded" | "overlay" | "flyout" | "drawer";
}) {
  const t = useTranslations("session");
  const { hasMore, loadingMore, loadMoreError, olderPagesLoaded, onLoadMore } =
    paging;
  const touch = surface === "drawer";

  if (loadingMore) {
    return (
      <div className="older-sessions" data-state="loading">
        <LoadingSkeleton
          variant="sessions"
          rows={3}
          label={t("rail_loading")}
        />
      </div>
    );
  }
  if (loadMoreError) {
    return (
      <div className="older-sessions" data-state="error">
        <div className="older-sessions-error">
          <LucideGlyph icon={CircleAlert} size={14} aria-hidden="true" />
          <span role="status">{t("rail_load_older_failed")}</span>
          <button type="button" onClick={() => void onLoadMore()}>
            {t("rail_search_retry")}
          </button>
        </div>
      </div>
    );
  }
  if (hasMore) {
    return (
      <div className="older-sessions" data-state="idle">
        <button
          type="button"
          className={clsx("older-sessions-more", { "is-touch": touch })}
          onClick={() => void onLoadMore()}
        >
          <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" />
          <span>{t("rail_load_older")}</span>
        </button>
      </div>
    );
  }
  if (olderPagesLoaded > 0) {
    return (
      <div className="older-sessions" data-state="end">
        <p className={clsx("older-sessions-end", { "is-touch": touch })}>
          {t("rail_no_older")}
        </p>
      </div>
    );
  }
  return null;
}
