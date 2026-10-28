"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import {
  documentCanvasTitle,
  documentCanvasDisambiguator,
  documentCanvasStateKey,
  documentCanvasRequestExcerpt,
  type DocumentJob,
} from "@/lib/document-drafts";
import styles from "./document-canvas-list.module.css";

/** Discovery uses the original authorized jobs and opens their existing editors. */
export function DocumentCanvasList({
  jobs,
  loading,
  error,
  hasPrevious,
  hasNext,
  onOpen,
  onRefresh,
  onPrevious,
  onNext,
}: {
  jobs: readonly DocumentJob[];
  loading: boolean;
  error: boolean;
  hasPrevious: boolean;
  hasNext: boolean;
  onOpen: (jobId: string) => void;
  onRefresh: () => void;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const t = useTranslations("documents");
  const titles = jobs.map((job) => documentCanvasTitle(job, t("title")));
  return (
    <section className={styles.root} aria-label={t("canvas_documents")}>
      <div className={styles.heading}>
        <h3>{t("canvas_documents")}</h3>
        <Button variant="ghost" disabled={loading} onClick={onRefresh}>
          {t(error ? "retry" : "canvas_refresh_list")}
        </Button>
      </div>
      {error ? <Notice tone="error">{t("canvas_read_error")}</Notice> : null}
      {loading ? <p role="status">{t("canvas_loading_list")}</p> : null}
      {!jobs.length && !loading && !error ? (
        <p>{t("canvas_no_documents")}</p>
      ) : null}
      <ul className={styles.list}>
        {jobs.map((job, index) => (
          <li key={job.id}>
            <Button
              variant="ghost"
              className={styles.entry}
              onClick={() => onOpen(job.id)}
            >
              <span className={styles.row}>
                <span className={styles.context}>
                  <span className={styles.title}>
                    {titles[index]}
                    {titles.filter((title) => title === titles[index]).length > 1
                      ? ` · ${documentCanvasDisambiguator(job)}`
                      : ""}
                  </span>
                  <span className={styles.request}>{documentCanvasRequestExcerpt(job)}</span>
                </span>
                <span className={styles.state}>{t(documentCanvasStateKey(job))}</span>
              </span>
            </Button>
          </li>
        ))}
      </ul>
      {hasPrevious || hasNext ? (
        <div className={styles.pages}>
          {hasPrevious ? (
            <Button variant="ghost" disabled={loading} onClick={onPrevious}>
              {t("canvas_previous_jobs")}
            </Button>
          ) : null}
          {hasNext ? (
            <Button variant="ghost" disabled={loading} onClick={onNext}>
              {t("canvas_next_jobs")}
            </Button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
