"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api-client";
import { documentEvidenceLocation, isDocumentClarificationEvidence, type DocumentJob, type DocumentPreviewAccess } from "@/lib/document-drafts";
import { AlertCircle, ChevronLeft, ChevronRight, Eye } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { CanvasSection, SectionHeading } from "@/components/ui/section-heading";
import { CanvasFooterPortal, CanvasObjectFooter, ChangeRow, StateBlock } from "@/components/canvas/object";
import { ReviewProgress, requiredReviewPages, type ReviewPage } from "./review-progress";
import styles from "./document-draft-review.module.css";

/** A page counts as viewed once its full preview has been shown for at least this long. */
export const PAGE_VIEW_DWELL_MS = 1000;

interface DocumentDraftReviewProps {
  job: DocumentJob;
  busy: boolean;
  /** Publication was submitted and is running (footer shows progress; closing does not cancel). */
  publishing?: boolean;
  onConfirm: (draftDigest: string) => void;
  previewAccess?: DocumentPreviewAccess;
  /** Object status shown on the review progress row on narrow canvases. */
  status?: React.ReactNode;
}

/** Private review content; navigation and command ownership remain with the host. */
export function DocumentDraftReview(props: DocumentDraftReviewProps) {
  if (!props.job.draft) return null;
  return (
    <DocumentDraftReviewVersion
      key={JSON.stringify([props.job.id, props.job.draft_digest ?? null])}
      {...props}
    />
  );
}

type Change = NonNullable<DocumentJob["draft"]>["manifest"]["changes"][number];

/**
 * PowerPoint / Keynote / Google Slides default names ("TextBox 5", "文本框 3", "Google Shape;123") say nothing to
 * the reader; such shapes are described by their page instead.
 */
const DEFAULT_SHAPE_NAME = /^(?:title|subtitle|text ?box|text placeholder|content placeholder|placeholder|rectangle|shape|google shape|table|object|标题|副标题|文本框|文本占位符|内容占位符|占位符|矩形|形状|表格|对象)[\s;:_-]*\d+$/i;

export function isDefaultShapeName(name: string): boolean {
  return DEFAULT_SHAPE_NAME.test(name.trim());
}

function useChangeLocation() {
  const t = useTranslations("documents");
  return useCallback((change: Change) => {
    const name = change.shape_name?.trim();
    if (name && isDefaultShapeName(name)) {
      const parts = [t("change_generic_shape", { page: change.slide })];
      if (change.cell) parts.push(t("change_cell", { row: change.cell[0] + 1, column: change.cell[1] + 1 }));
      return parts.join(" · ");
    }
    const parts = [t("slide", { page: change.slide })];
    if (name) parts.push(t("change_object", { name }));
    if (change.cell) parts.push(t("change_cell", { row: change.cell[0] + 1, column: change.cell[1] + 1 }));
    return parts.join(" · ");
  }, [t]);
}

function ChangeList({ changes, clarificationSources }: { changes: readonly Change[]; clarificationSources: DocumentJob["clarification_evidence_sources"] }) {
  const t = useTranslations("documents");
  const location = useChangeLocation();
  return (
    <ul className={styles.list}>
      {changes.map((change, index) => (
        <li key={index}>
          <ChangeRow
            location={location(change)}
            before={change.before ? { kind: "text", text: change.before } : { kind: "empty" }}
            after={change.after ? { kind: "text", text: change.after } : { kind: "empty" }}
            beforeLabel={t("before")}
            afterLabel={t("after")}
            rows={change.evidence.map((evidence) => {
              const place = documentEvidenceLocation(evidence.source_name, evidence.pointer);
              const value = isDocumentClarificationEvidence(evidence, clarificationSources) ? `${t("source_clarification")} · ${evidence.value}`
                : !evidence.source_name ? `${t("source_request")} · ${evidence.value}`
                : place.row ? t("source_cell", { file: evidence.source_name, sheet: place.sheet ?? "", row: place.row })
                  : t("source_file", { file: evidence.source_name });
              return { label: t("source"), value };
            })}
          />
        </li>
      ))}
    </ul>
  );
}

function DocumentDraftReviewVersion({
  job,
  busy,
  publishing = false,
  onConfirm,
  previewAccess,
  status,
}: DocumentDraftReviewProps) {
  const t = useTranslations("documents");
  const common = useTranslations("common");
  const pages = useMemo(() => job.draft?.preview.pages ?? [], [job.draft]);
  const changes = useMemo(() => job.draft?.manifest.changes ?? [], [job.draft]);
  const comparisonAvailable = job.draft?.manifest.comparison !== "unavailable";
  const [acknowledged, setAcknowledged] = useState(false);
  const [page, setPage] = useState(0);
  const [viewed, setViewed] = useState<ReadonlySet<number>>(() => new Set());
  const [urls, setUrls] = useState<Readonly<Record<string, string>>>({});
  const [failed, setFailed] = useState<ReadonlySet<string>>(() => new Set());
  const [attempt, setAttempt] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [open, setOpen] = useState<Record<"fonts" | "retained" | "changes", boolean>>({ fonts: false, retained: false, changes: false });
  const sectionId = useId();
  const owned = useRef<string[]>([]);
  useEffect(() => () => { for (const url of owned.current) URL.revokeObjectURL(url); }, []);

  const current = pages[page];
  const currentName = current?.name ?? "";
  /** Reads one private preview into an object URL (kept for the main preview and the thumbnail strip). */
  const loadPreview = useCallback(async (name: string, signal: AbortSignal, priority: "high" | "low") => {
    if (previewAccess && !previewAccess.isCurrent()) return null;
    const response = await apiFetch(
      `/api/v1/documents/${encodeURIComponent(job.id)}/preview/${encodeURIComponent(name)}`,
      { signal, priority, ...(previewAccess ? { headers: previewAccess.headers } : {}) } as RequestInit,
    );
    if (signal.aborted || (previewAccess && !previewAccess.isCurrent())) return null;
    if ([401, 403, 404].includes(response.status)) previewAccess?.onAccessDenied();
    if (!response.ok) throw new Error("Preview unavailable");
    const blob = await response.blob();
    if (signal.aborted || (previewAccess && !previewAccess.isCurrent())) return null;
    const url = URL.createObjectURL(blob);
    owned.current.push(url);
    return url;
  }, [job.id, previewAccess]);
  // Fetch the current page once; keep its object URL for the thumbnail strip.
  useEffect(() => {
    if (!currentName || urls[currentName] || failed.has(currentName)) return;
    const controller = new AbortController();
    void loadPreview(currentName, controller.signal, "high").then((url) => {
      if (url) setUrls((previous) => ({ ...previous, [currentName]: url }));
    }, () => {
      if (!controller.signal.aborted && (!previewAccess || previewAccess.isCurrent()))
        setFailed((previous) => new Set(previous).add(currentName));
    });
    return () => controller.abort();
  }, [currentName, urls, failed, attempt, loadPreview, previewAccess]);

  // M4: thumbnails that scroll into view load one at a time at low priority, after the current page.
  // Loading a thumbnail never counts as viewing the page; a failed thumbnail keeps its number placeholder.
  const [wantedThumbs, setWantedThumbs] = useState<ReadonlySet<string>>(() => new Set());
  const [thumbFailed, setThumbFailed] = useState<ReadonlySet<string>>(() => new Set());
  const nextThumb = currentName && !urls[currentName] && !failed.has(currentName) ? null
    : pages.find((item) => wantedThumbs.has(item.name) && !urls[item.name] && !thumbFailed.has(item.name) && !failed.has(item.name))?.name ?? null;
  useEffect(() => {
    if (!nextThumb) return;
    const controller = new AbortController();
    void loadPreview(nextThumb, controller.signal, "low").then((url) => {
      if (url) setUrls((previous) => (previous[nextThumb] ? previous : { ...previous, [nextThumb]: url }));
    }, () => {
      if (!controller.signal.aborted) setThumbFailed((previous) => new Set(previous).add(nextThumb));
    });
    return () => controller.abort();
  }, [nextThumb, loadPreview]);
  const showThumbs = useCallback((numbers: number[]) => {
    setWantedThumbs((previous) => {
      const names = numbers.map((n) => pages[n - 1]?.name).filter((name): name is string => Boolean(name) && !previous.has(name as string));
      return names.length ? new Set([...previous, ...names]) : previous;
    });
  }, [pages]);

  // A page is viewed after its full preview has been visible for PAGE_VIEW_DWELL_MS.
  const [loadedPage, setLoadedPage] = useState<number | null>(null);
  useEffect(() => {
    if (loadedPage !== page || viewed.has(page + 1)) return;
    const timer = window.setTimeout(() => setViewed((previous) => new Set(previous).add(page + 1)), PAGE_VIEW_DWELL_MS);
    return () => window.clearTimeout(timer);
  }, [loadedPage, page, viewed]);

  const changeCounts = useMemo(() => {
    const counts = new Map<number, number>();
    for (const change of changes) counts.set(change.slide, (counts.get(change.slide) ?? 0) + 1);
    return counts;
  }, [changes]);
  const reviewPages: ReviewPage[] = pages.map((item, index) => ({
    n: index + 1,
    thumbUrl: urls[item.name] ?? null,
    changeCount: changeCounts.get(index + 1) ?? 0,
    viewed: viewed.has(index + 1),
  }));
  const requireAllPages = job.draft?.requires_full_review === true;
  const required = requiredReviewPages(reviewPages, requireAllPages);
  const requiredLeft = required.filter((item) => !item.viewed);
  const previewFailed = Boolean(currentName && failed.has(currentName));
  const reviewReady = pages.length > 0 && requiredLeft.length === 0;

  const goTo = (target: number) => {
    if (target < 0 || target >= pages.length) return;
    setLoadedPage(null);
    setPage(target);
  };
  const nextChange = () => {
    const after = requiredLeft.find((item) => item.n > page + 1) ?? requiredLeft[0];
    if (after) goTo(after.n - 1);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, [contenteditable='true'], [role='listbox'], [role='menu']")) return;
    if (event.key === "ArrowRight") { event.preventDefault(); goTo(page + 1); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); goTo(page - 1); }
    else if ((event.key === "n" || event.key === "N") && !event.metaKey && !event.ctrlKey && !event.altKey) { event.preventDefault(); nextChange(); }
  };

  const pageChanges = changes.filter((change) => change.slide === page + 1);
  const shownChanges = showAll ? [...changes].sort((a, b) => a.slide - b.slide) : pageChanges;
  const fonts = job.draft?.preview.declared_font_fallbacks ?? [];
  const retained = job.draft?.manifest.retained_unsupported_text ?? [];
  const pageLabel = t("page", { page: page + 1, total: pages.length });
  const reviewing = job.state === "review";

  const blocker = !reviewing || !job.can_confirm ? null
    : previewFailed || !pages.length ? t("publish_needs_preview")
      : requiredLeft.length ? t(requireAllPages ? "publish_pages_left" : "publish_changed_left", { count: requiredLeft.length })
        : !acknowledged ? t("publish_check_first") : null;

  return (
    <div className={styles.root} onKeyDown={onKeyDown}>
      {reviewing ? (
        <ReviewProgress pages={reviewPages} current={page + 1} onSelect={(n) => goTo(n - 1)} onNextChange={nextChange} onThumbsVisible={showThumbs} status={status} requireAllPages={requireAllPages} />
      ) : null}
      <div className={styles.preview}>
        <div className={styles.previewFrame}>
          {!current ? (
            <StateBlock tone="error" icon={<AlertCircle size={20} />} title={t("preview_error_title")} body={t("preview_error")} />
          ) : previewFailed ? (
            <StateBlock
              tone="error"
              icon={<AlertCircle size={20} />}
              title={t("preview_error_title")}
              body={t("preview_error")}
              secondaryActions={[{ label: common("retry"), onClick: () => {
                setFailed((previous) => { const next = new Set(previous); next.delete(currentName); return next; });
                setAttempt((value) => value + 1);
              } }]}
            />
          ) : urls[currentName] ? (
            // Authenticated bytes are kept in a short-lived object URL, never a public image URL.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={currentName}
              src={urls[currentName]}
              alt={pageLabel}
              className={styles.previewImage}
              onLoad={() => setLoadedPage(page)}
              onError={() => setFailed((previous) => new Set(previous).add(currentName))}
            />
          ) : (
            <LoadingSkeleton rows={3} label={t("loading")} />
          )}
        </div>
        {pages.length > 1 ? (
          <div className={styles.pagination}>
            <button type="button" className={styles.pageButton} aria-label={t("previous")} title={t("previous")} disabled={page === 0} onClick={() => goTo(page - 1)}>
              <ChevronLeft size={16} aria-hidden />
            </button>
            <span>{pageLabel}</span>
            <button type="button" className={styles.pageButton} aria-label={t("next")} title={t("next")} disabled={page + 1 >= pages.length} onClick={() => goTo(page + 1)}>
              <ChevronRight size={16} aria-hidden />
            </button>
          </div>
        ) : null}
      </div>

      {reviewing ? (
        <CanvasSection labelledBy={`${sectionId}-changes`}>
          <SectionHeading
            id={`${sectionId}-changes`}
            title={showAll ? t("changes_title") : t("changes_on_page")}
            count={comparisonAvailable ? (showAll ? changes.length : pageChanges.length) : undefined}
            action={changes.length > pageChanges.length || showAll ? (
              <Button size="sm" variant="ghost" onClick={() => setShowAll((value) => !value)}>
                {showAll ? t("changes_on_page") : t("changes_view_all", { count: changes.length })}
              </Button>
            ) : undefined}
          />
          {!comparisonAvailable ? <p className={styles.plain}>{t("comparison_unavailable")}</p> : shownChanges.length ? <ChangeList changes={shownChanges} clarificationSources={job.clarification_evidence_sources} /> : <p className={styles.plain}>{t("changes_none_on_page")}</p>}
        </CanvasSection>
      ) : (
        <CanvasSection labelledBy={`${sectionId}-changes`}>
          <SectionHeading id={`${sectionId}-changes`} title={t("changes_title")} count={comparisonAvailable ? changes.length : undefined} collapsible open={open.changes}
            controls={`${sectionId}-changes-body`} onToggle={() => setOpen((value) => ({ ...value, changes: !value.changes }))} />
          {open.changes ? <div id={`${sectionId}-changes-body`}>{comparisonAvailable ? <ChangeList changes={changes} clarificationSources={job.clarification_evidence_sources} /> : <p className={styles.plain}>{t("comparison_unavailable")}</p>}</div> : null}
        </CanvasSection>
      )}

      {fonts.length ? (
        <CanvasSection labelledBy={`${sectionId}-fonts`}>
          <SectionHeading id={`${sectionId}-fonts`} title={t("font_substitutions")} count={fonts.length} collapsible open={open.fonts}
            controls={`${sectionId}-fonts-body`} onToggle={() => setOpen((value) => ({ ...value, fonts: !value.fonts }))} />
          {open.fonts ? (
            <ul id={`${sectionId}-fonts-body`} className={styles.list}>
              {fonts.map((font) => (
                <li key={font.declared_font} className={styles.plain}>
                  {t("font_substitution", { declared: font.declared_font, fallback: font.environment_fallback })}
                  {font.slides?.length ? t("font_substitution_pages", { pages: font.slides.join(t("page_list_separator")) }) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </CanvasSection>
      ) : null}

      {retained.length ? (
        <CanvasSection labelledBy={`${sectionId}-retained`}>
          <SectionHeading id={`${sectionId}-retained`} title={t("unchanged")} count={retained.length} collapsible open={open.retained}
            controls={`${sectionId}-retained-body`} onToggle={() => setOpen((value) => ({ ...value, retained: !value.retained }))} />
          {open.retained ? (
            <ul id={`${sectionId}-retained-body`} className={styles.list}>
              {retained.map((item, index) => (
                <li key={index} className={styles.plain}>{t("retained_item", { page: item.slide, text: item.text })}</li>
              ))}
            </ul>
          ) : null}
        </CanvasSection>
      ) : null}

      {reviewing ? (
        <CanvasFooterPortal>
          {job.can_confirm ? (
            <CanvasObjectFooter
              leading={(
                <label className={styles.acknowledgement}>
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    disabled={!reviewReady || previewFailed || publishing}
                    onChange={(event) => setAcknowledged(event.target.checked)}
                  />
                  <span>{t("acknowledge")}</span>
                </label>
              )}
              message={publishing ? t("canvas_confirming") : blocker ?? undefined}
              tone={publishing ? "neutral" : "warning"}
              icon={!publishing && blocker ? <Eye size={14} aria-hidden /> : undefined}
              primary={{
                label: t("confirm"),
                busy: publishing,
                disabled: busy || !job.draft_digest || Boolean(blocker),
                disabledReason: blocker ?? undefined,
                onClick: () => { if (job.draft_digest) onConfirm(job.draft_digest); },
              }}
            />
          ) : (
            <CanvasObjectFooter message={t("canvas_read_only")} />
          )}
        </CanvasFooterPortal>
      ) : null}
    </div>
  );
}
