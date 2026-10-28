"use client";

import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { ArrowDown, Check } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NarrowStatus } from "@/components/canvas/object/narrow-status";
import styles from "./review-progress.module.css";

export interface ReviewPage {
  /** 1-based page number. */
  n: number;
  thumbUrl?: string | null;
  changeCount: number;
  viewed: boolean;
}

/**
 * Pages that must be viewed before publishing (decision D5): only pages with changes. When a draft has
 * no changes at all, every page is required so the reviewer still sees the preview.
 */
export function requiredReviewPages(pages: readonly ReviewPage[]): ReviewPage[] {
  const changed = pages.filter((page) => page.changeCount > 0);
  return changed.length ? changed : [...pages];
}

/**
 * C8 · review progress: “Viewed x of y” + 4px meter + thumbnail strip. Viewed pages carry a check,
 * changed pages an “edited” chip, the current page a 2px accent ring. The strip is a listbox:
 * ←/→ move, Enter opens.
 */
export function ReviewProgress({
  pages,
  current,
  onSelect,
  onNextChange,
  onThumbsVisible,
  status,
  orientation = "slide",
}: {
  pages: readonly ReviewPage[];
  current: number;
  onSelect: (page: number) => void;
  onNextChange?: () => void;
  /** Pages whose thumbnails scrolled into view, so their previews can load lazily (M4). */
  onThumbsVisible?: (pages: number[]) => void;
  /** Object status shown on this row on narrow canvases (the toolbar row is folded there). */
  status?: ReactNode;
  orientation?: "slide" | "page";
}) {
  const t = useTranslations("documents");
  const total = pages.length;
  const viewed = pages.filter((page) => page.viewed).length;
  const required = requiredReviewPages(pages);
  const requiredLeft = required.filter((page) => !page.viewed).length;
  const changed = pages.some((page) => page.changeCount > 0);
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const selected = listRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    selected?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [current]);
  const reportVisible = useRef(onThumbsVisible);
  reportVisible.current = onThumbsVisible;
  useEffect(() => {
    const list = listRef.current;
    if (!list || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting)
        .map((entry) => Number((entry.target as HTMLElement).dataset.page)).filter(Boolean);
      if (visible.length) reportVisible.current?.(visible);
    }, { root: list, rootMargin: "0px 160px" });
    list.querySelectorAll<HTMLElement>("[data-page]").forEach((thumb) => observer.observe(thumb));
    return () => observer.disconnect();
  }, [total]);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = pages.findIndex((page) => page.n === current);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const next = pages[Math.min(pages.length - 1, Math.max(0, index + (event.key === "ArrowRight" ? 1 : -1)))];
      if (next) {
        onSelect(next.n);
        window.requestAnimationFrame(() => listRef.current?.querySelector<HTMLElement>(`[data-page="${next.n}"]`)?.focus());
      }
    }
  };
  return (
    <div className={styles.progress}>
      <div className={styles.head}>
        <span className={styles.count} role="status">{t("review_progress", { seen: viewed, total })}</span>
        {requiredLeft === 0 ? (
          <Badge tone="positive" size="sm"><Check size={14} aria-hidden />{t(changed ? "review_changed_done" : "review_all_done")}</Badge>
        ) : changed ? (
          <span className={styles.hint}>{t("review_changed_left", { total: required.length, left: requiredLeft })}</span>
        ) : null}
        <span className={styles.spacer} />
        {status ? <NarrowStatus>{status}</NarrowStatus> : null}
        {onNextChange && requiredLeft > 0 && changed ? (
          <Button size="sm" variant="ghost" aria-label={t("next_change")} title={t("next_change")} onClick={onNextChange}>
            <span className={styles.collapsibleText}>{t("next_change")}</span><ArrowDown size={14} aria-hidden />
          </Button>
        ) : null}
      </div>
      <div className={styles.meter} aria-hidden>
        <span className={styles.meterFill} style={{ width: `${total ? Math.round((viewed / total) * 100) : 0}%` }} />
      </div>
      <div
        ref={listRef}
        className={styles.thumbs}
        role="listbox"
        aria-label={t("review_pages")}
        aria-orientation="horizontal"
        onKeyDown={onKeyDown}
      >
        {pages.map((page) => {
          const selected = page.n === current;
          const label = [
            t("thumb_page", { page: page.n }),
            page.changeCount ? t("thumb_changes", { count: page.changeCount }) : null,
            t(page.viewed ? "thumb_viewed" : "thumb_not_viewed"),
          ].filter(Boolean).join(t("thumb_separator"));
          return (
            <button
              key={page.n}
              type="button"
              role="option"
              aria-selected={selected}
              aria-label={label}
              tabIndex={selected ? 0 : -1}
              data-page={page.n}
              className={styles.thumb}
              onClick={() => onSelect(page.n)}
            >
              <span className={styles.image} data-orientation={orientation}>
                {page.thumbUrl
                  // Authenticated preview bytes held in a short-lived object URL.
                  // eslint-disable-next-line @next/next/no-img-element
                  ? <img src={page.thumbUrl} alt="" />
                  : page.n}
                {page.changeCount ? (
                  <span className={styles.changed} aria-hidden>
                    {page.changeCount > 1 ? t("thumb_changed_count", { count: page.changeCount }) : t("thumb_changed")}
                  </span>
                ) : null}
                {page.viewed ? <span className={styles.viewed} aria-hidden><Check size={14} /></span> : null}
              </span>
              <span className={styles.number} aria-hidden>{page.n}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
