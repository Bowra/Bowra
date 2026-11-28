"use client";

import {
  type MutableRefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import { useTranslations } from "next-intl";
import type { PDFDocumentLoadingTask } from "pdfjs-dist";

import { appFetch } from "@/lib/base-path";
import type { SlideToggleOutcome } from "@/lib/presentation-selection";
import {
  classifyResultsError,
  classifyResultsResponse,
  ResultsWorkspaceError,
  type ResultsWorkspaceErrorClass,
} from "@/lib/results-workspace-errors";

const PDF_PAGES_PER_BATCH = 10;
const MAX_PDF_PREVIEW_BYTES = 25 * 1024 * 1024;
const MAX_DOCX_PREVIEW_BYTES = 15 * 1024 * 1024;

class PresentationPreviewBusyError extends ResultsWorkspaceError {
  constructor() { super("unavailable"); }
}

async function fetchDocumentBytes(href: string, maxBytes: number, signal?: AbortSignal): Promise<ArrayBuffer> {
  let response: Response;
  try {
    response = await appFetch(href, { cache: "no-store", ...(signal ? { signal } : {}) });
  } catch (cause) {
    const errorClass = classifyResultsError(cause);
    throw new ResultsWorkspaceError(errorClass);
  }
  if (!response.ok) {
    if (response.status === 503) {
      let code: unknown;
      try {
        const refusal = await response.json() as { detail?: { error?: { code?: unknown } } };
        code = refusal?.detail?.error?.code;
      } catch { /* Non-JSON refusals retain their original status classification. */ }
      if (code === "artifact_representation_busy") throw new PresentationPreviewBusyError();
    }
    throw new ResultsWorkspaceError(classifyResultsResponse(response.status));
  }
  const length = Number(response.headers.get("content-length"));
  if (Number.isFinite(length) && length > maxBytes) {
    throw new ResultsWorkspaceError("too_large");
  }
  let data: ArrayBuffer;
  try {
    data = await response.arrayBuffer();
  } catch (cause) {
    const errorClass = classifyResultsError(cause);
    throw new ResultsWorkspaceError(errorClass);
  }
  if (data.byteLength > maxBytes) {
    throw new ResultsWorkspaceError("too_large");
  }
  return data;
}

/** The rendered PDF of a deck, as the preview fetches it (the editor reuses it, design §7.2). */
export function fetchPresentationPdf(href: string, signal?: AbortSignal): Promise<ArrayBuffer> {
  return fetchDocumentBytes(href, MAX_PDF_PREVIEW_BYTES, signal);
}

export function isPresentationPreviewBusy(error: unknown): boolean {
  return error instanceof PresentationPreviewBusyError;
}

type DocumentProfile = "pdf" | "docx" | "pptx";

export type DocumentPreviewCache = {
  pdfData?: ArrayBuffer;
  pdfRenderedThru?: number;
  /** The page most in view, kept up to date while pages show (a deck editor opens on it). */
  visiblePage?: number;
  /** Scroll to this page once it renders, then forget it (leaving a deck editor, design §13.1). */
  focusPage?: number;
  docxHtml?: string;
  docxWarningCount?: number;
  /** Word pages could not be rendered for this file: the reading view stays (docx inline edit design §5.1). */
  docxPagesFailed?: boolean;
};

export function ArtifactDocumentPreview({
  profile,
  href,
  title,
  sizeBytes,
  onError,
  cacheRef,
  thumbnails = false,
  onReferencePage,
  referencedPages,
  pagesHref,
}: {
  profile: DocumentProfile;
  href: string;
  /** A Word file's rendered pages; without it Word files show the reading view. */
  pagesHref?: string;
  title: string;
  sizeBytes?: number | null;
  onError?: (errorClass: ResultsWorkspaceErrorClass) => void;
  cacheRef: MutableRefObject<DocumentPreviewCache>;
  thumbnails?: boolean;
  /** A deck's pages each offer 「引用这一页」, a toggle (design §13.2, §13.6). */
  onReferencePage?: (page: number) => SlideToggleOutcome;
  /** The pages of this file in the Composer: their buttons read 「已引用」. */
  referencedPages?: ReadonlyArray<number>;
}) {
  if (profile !== "docx") {
    return (
      <PdfArtifactPreview
        href={href}
        title={title}
        sizeBytes={sizeBytes}
        onError={onError}
        cacheRef={cacheRef}
        presentation={profile === "pptx"}
        thumbnails={thumbnails}
        onReferencePage={profile === "pptx" ? onReferencePage : undefined}
        referencedPages={referencedPages}
      />
    );
  }
  if (pagesHref) {
    return (
      <DocxPagesPreview
        href={href}
        pagesHref={pagesHref}
        title={title}
        sizeBytes={sizeBytes}
        onError={onError}
        cacheRef={cacheRef}
        onReferencePage={onReferencePage}
        referencedPages={referencedPages}
      />
    );
  }
  return (
    <DocxArtifactPreview
      href={href}
      sizeBytes={sizeBytes}
      onError={onError}
      cacheRef={cacheRef}
    />
  );
}

/**
 * A Word file's pages as rendered (docx inline edit design §5.1). The reading view stands in while
 * the pages render and stays when they cannot be rendered; an open failure is reported only from
 * the view that remains.
 */
function DocxPagesPreview({
  href,
  pagesHref,
  title,
  sizeBytes,
  onError,
  cacheRef,
  onReferencePage,
  referencedPages,
}: {
  href: string;
  pagesHref: string;
  title: string;
  sizeBytes?: number | null;
  onError?: (errorClass: ResultsWorkspaceErrorClass) => void;
  cacheRef: MutableRefObject<DocumentPreviewCache>;
  /** 「引用这一页」 under each rendered page (docx inline edit design §7.4). */
  onReferencePage?: (page: number) => SlideToggleOutcome;
  referencedPages?: ReadonlyArray<number>;
}) {
  const t = useTranslations("artifact_viewer");
  const [pages, setPages] = useState<"pending" | "ready" | "failed">(
    cacheRef.current.pdfData ? "ready" : cacheRef.current.docxPagesFailed ? "failed" : "pending",
  );

  useEffect(() => {
    if (pages !== "pending") return;
    const controller = new AbortController();
    let cancelled = false;
    fetchDocumentBytes(pagesHref, MAX_PDF_PREVIEW_BYTES, controller.signal).then(
      (data) => {
        if (cancelled) return;
        cacheRef.current.pdfData = data.slice(0);
        setPages("ready");
      },
      () => {
        if (cancelled) return;
        cacheRef.current.docxPagesFailed = true;
        setPages("failed");
      },
    );
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [cacheRef, pages, pagesHref]);

  if (pages === "ready") {
    return (
      <PdfArtifactPreview
        href={pagesHref}
        title={title}
        onError={onError}
        cacheRef={cacheRef}
        presentation={false}
        thumbnails={false}
        onReferencePage={onReferencePage}
        referencedPages={referencedPages}
      />
    );
  }
  return (
    <>
      {pages === "pending" ? (
        <div className="artifact-document-status" role="status" data-testid="artifact-docx-pages-pending">
          {t("document_pages_rendering")}
        </div>
      ) : null}
      <DocxArtifactPreview
        href={href}
        sizeBytes={sizeBytes}
        onError={pages === "failed" ? onError : undefined}
        cacheRef={cacheRef}
      />
    </>
  );
}

function PdfArtifactPreview({
  href,
  title,
  sizeBytes,
  onError,
  cacheRef,
  presentation,
  thumbnails,
  onReferencePage,
  referencedPages,
}: {
  href: string;
  title: string;
  sizeBytes?: number | null;
  onError?: (errorClass: ResultsWorkspaceErrorClass) => void;
  cacheRef: MutableRefObject<DocumentPreviewCache>;
  presentation: boolean;
  thumbnails: boolean;
  onReferencePage?: (page: number) => SlideToggleOutcome;
  referencedPages?: ReadonlyArray<number>;
}) {
  const t = useTranslations("artifact_viewer");
  const pagesRef = useRef<HTMLDivElement>(null);
  // Pages are drawn outside React; their buttons read the latest handler and copy from here.
  const pageActionRef = useRef<PageAction | null>(null);
  const pageStatusRef = useRef<HTMLSpanElement>(null);
  pageActionRef.current = onReferencePage
    ? {
        label: t("reference_page"),
        labelFor: (page) => t("reference_page_label", { page }),
        doneLabelFor: (page) => t("referenced_page_label", { page }),
        doneLabel: t("referenced_page"),
        removedLabelFor: (page) => t("page_reference_removed", { page }),
        isReferenced: (page) => Boolean(referencedPages?.includes(page)),
        onSelect: onReferencePage,
        announce: (text) => { if (pageStatusRef.current) pageStatusRef.current.textContent = text; },
      }
    : null;
  // The Composer decides which pages are referenced: a chip removed there turns its pages' buttons back.
  const referencedKey = referencedPages?.join(",") ?? "";
  useEffect(() => {
    pagesRef.current?.querySelectorAll(".artifact-pdf-page-action").forEach((button) => button.dispatchEvent(new CustomEvent("page-action-sync")));
  }, [referencedKey]);
  const pdfRef = useRef<PdfDocumentLike | null>(null);
  const cancelledRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageCount, setPageCount] = useState(0);
  const [renderedThru, setRenderedThru] = useState(0);
  const [error, setError] = useState<"too_large" | "load_failed" | "busy" | null>(null);
  const [retry, setRetry] = useState(0);
  const loadingMessage = t(presentation ? "presentation_loading" : "document_loading_pdf");

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | undefined;
    cancelledRef.current = false;
    const fail = (
      code: "too_large" | "load_failed" | "busy",
      errorClass: ResultsWorkspaceErrorClass,
    ) => {
      if (cancelled) return;
      setError(code);
      setLoading(false);
      if (code !== "busy") onError?.(errorClass);
    };

    if (sizeBytes != null && sizeBytes > MAX_PDF_PREVIEW_BYTES) {
      fail("too_large", "too_large");
      return () => {
        cancelled = true;
        cancelledRef.current = true;
      };
    }

    void (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        if (!cacheRef.current.pdfData) {
          const fetched = await fetchDocumentBytes(href, MAX_PDF_PREVIEW_BYTES, controller.signal);
          if (cancelled) return;
          cacheRef.current.pdfData = fetched.slice(0);
        }
        if (cancelled) return;
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();
        // pdf.js transfers its input buffer to the worker. Always retain an
        // immutable cache copy and give each viewer mount its own transferable.
        const data = cacheRef.current.pdfData.slice(0);
        loadingTask = pdfjs.getDocument({ data });
        const pdf = await loadingTask.promise as PdfDocumentLike;
        if (cancelled) return;
        pdfRef.current = pdf;
        setPageCount(pdf.numPages);
        setLoading(false);
        const end = Math.min(
          Math.max(PDF_PAGES_PER_BATCH, cacheRef.current.pdfRenderedThru ?? 0, cacheRef.current.focusPage ?? 0),
          pdf.numPages,
        );
        await renderPdfPages(pdf, 1, end, pagesRef.current, () => cancelled, title, presentation, () => pageActionRef.current);
        if (!cancelled) {
          cacheRef.current.pdfRenderedThru = end;
          setRenderedThru(end);
          const focus = cacheRef.current.focusPage;
          cacheRef.current.focusPage = undefined;
          if (focus) {
            pagesRef.current?.querySelector<HTMLElement>(`[data-page="${focus}"]`)?.scrollIntoView({ block: "start" });
            cacheRef.current.visiblePage = focus;
          }
        }
      } catch (cause) {
        if (presentation && cause instanceof PresentationPreviewBusyError) {
          fail("busy", "unavailable");
          return;
        }
        const classified = classifyResultsError(cause);
        const errorClass = classified === "unknown" && !(cause instanceof ResultsWorkspaceError) ? "corrupt" : classified;
        fail(errorClass === "too_large" ? "too_large" : "load_failed", errorClass);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      cancelledRef.current = true;
      pdfRef.current = null;
      // PDF.js 6 owns worker teardown on the loading task, including pending loads.
      void loadingTask?.destroy().catch(() => undefined);
    };
  }, [cacheRef, href, onError, presentation, retry, sizeBytes, title]);

  const loadMore = async () => {
    const pdf = pdfRef.current;
    if (!pdf || loadingMore || renderedThru >= pdf.numPages) return;
    const end = Math.min(renderedThru + PDF_PAGES_PER_BATCH, pdf.numPages);
    setLoadingMore(true);
    try {
      await renderPdfPages(
        pdf,
        renderedThru + 1,
        end,
        pagesRef.current,
        () => cancelledRef.current,
        title,
        presentation,
        () => pageActionRef.current,
      );
      if (!cancelledRef.current) {
        cacheRef.current.pdfRenderedThru = end;
        setRenderedThru(end);
      }
    } catch {
      setError("load_failed");
      onError?.("corrupt");
    } finally {
      if (!cancelledRef.current) setLoadingMore(false);
    }
  };

  // The page most in view, for a deck editor opened from here.
  useEffect(() => {
    const container = pagesRef.current;
    if (!presentation || !container || typeof IntersectionObserver === "undefined") return;
    const ratios = new Map<number, number>();
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) ratios.set(Number((entry.target as HTMLElement).dataset.page), entry.intersectionRatio);
      let best = 0;
      let bestRatio = 0;
      for (const [page, ratio] of ratios) if (ratio > bestRatio || (ratio === bestRatio && ratio > 0 && page < best)) { best = page; bestRatio = ratio; }
      if (best) cacheRef.current.visiblePage = best;
    }, { threshold: [0, 0.25, 0.5, 0.75, 1] });
    container.querySelectorAll<HTMLElement>("[data-page]").forEach((figure) => observer.observe(figure));
    return () => observer.disconnect();
  }, [cacheRef, presentation, renderedThru]);

  if (error) {
    return (
      <div className={`artifact-viewer-note${error === "busy" ? "" : " error"}`} role={error === "busy" ? "status" : "alert"}>
        {t(error === "busy" ? "presentation_preview_busy" : error === "too_large" ? "document_too_large" : presentation ? "presentation_preview_failed" : "document_load_failed")}
        {presentation && error !== "too_large" ? <button type="button" onClick={() => {
          cacheRef.current.pdfData = undefined;
          cacheRef.current.pdfRenderedThru = undefined;
          setError(null);
          setLoading(true);
          setPageCount(0);
          setRenderedThru(0);
          setRetry((value) => value + 1);
        }}>{t("retry")}</button> : null}
      </div>
    );
  }

  return (
    <div className="artifact-document-preview" data-testid="artifact-preview-pdf">
      <span className="sr-only" role="status" ref={pageStatusRef} />
      <div className="artifact-document-status" aria-live="polite">
        {loading
          ? loadingMessage
          : t("document_pages_shown", { shown: renderedThru, total: pageCount })}
      </div>
      <div className={thumbnails ? "artifact-pdf-pages artifact-pptx-thumbnails" : "artifact-pdf-pages"} ref={pagesRef} />
      {!loading && renderedThru < pageCount ? (
        <button
          type="button"
          className="artifact-document-load-more"
          onClick={() => void loadMore()}
          disabled={loadingMore}
        >
          {loadingMore ? loadingMessage : t("document_load_more")}
        </button>
      ) : null}
    </div>
  );
}

type PdfDocumentLike = {
  numPages: number;
  getPage: (page: number) => Promise<{
    getViewport: (options: { scale: number }) => { width: number; height: number };
    render: (options: {
      canvas: HTMLCanvasElement;
      canvasContext: CanvasRenderingContext2D;
      viewport: unknown;
    }) => { promise: Promise<void> };
  }>;
};

type PageAction = {
  label: string;
  /** Names start with the visible text (WCAG 2.5.3): 「引用这一页 · 第 N 页」, then 「已引用 · 第 N 页」. */
  labelFor: (page: number) => string;
  doneLabelFor: (page: number) => string;
  /** Shown on the button while the page is in the Composer (a pressed toggle), instead of a toast. */
  doneLabel: string;
  removedLabelFor: (page: number) => string;
  isReferenced: (page: number) => boolean;
  onSelect: (page: number) => SlideToggleOutcome;
  announce: (text: string) => void;
};

const PAGE_DONE_ICON = '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10"/></svg>';

const PAGE_ACTION_ICON = '<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

async function renderPdfPages(
  pdf: PdfDocumentLike,
  start: number,
  end: number,
  container: HTMLDivElement | null,
  isCancelled: () => boolean,
  title: string,
  presentation: boolean,
  pageAction: () => PageAction | null = () => null,
): Promise<void> {
  if (!container) return;
  // One id prefix per list of pages, so page-number ids stay unique when two previews are open.
  container.dataset.pagesId ||= `pdf-pages-${Math.random().toString(36).slice(2, 8)}`;
  const pageIdPrefix = container.dataset.pagesId;
  for (let pageNumber = start; pageNumber <= end; pageNumber += 1) {
    if (isCancelled()) return;
    const page = await pdf.getPage(pageNumber);
    if (isCancelled()) return;
    const base = page.getViewport({ scale: 1 });
    const scale = presentation ? Math.min(1.4, 1600 / Math.max(base.width, base.height)) : 1.4;
    const viewport = page.getViewport({ scale });
    const figure = document.createElement("figure");
    figure.className = "artifact-pdf-page";
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${title} · ${pageNumber}`);
    figure.dataset.page = String(pageNumber);
    const caption = document.createElement("figcaption");
    const number = document.createElement("span");
    number.textContent = String(pageNumber);
    // The figure is named by its page number, not by the caption's button.
    number.id = `${pageIdPrefix}-${pageNumber}`;
    figure.setAttribute("aria-labelledby", number.id);
    caption.append(number);
    const action = pageAction();
    if (action) {
      caption.className = "has-action";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "artifact-pdf-page-action";
      const show = (done: boolean) => {
        const current = pageAction() ?? action;
        button.setAttribute("aria-pressed", String(done));
        button.setAttribute("aria-label", done ? current.doneLabelFor(pageNumber) : current.labelFor(pageNumber));
        button.innerHTML = done ? PAGE_DONE_ICON : PAGE_ACTION_ICON;
        button.append(document.createTextNode(done ? current.doneLabel : current.label));
        if (done) button.dataset.referenced = "true";
        else delete button.dataset.referenced;
      };
      show(action.isReferenced(pageNumber));
      // A toggle: the page joins the file's page set in the Composer, or leaves it (design §13.6).
      button.addEventListener("click", () => {
        const current = pageAction();
        if (!current) return;
        const outcome = current.onSelect(pageNumber);
        // At the limit the host says why; the page stays as it was.
        if (outcome === "limit") return;
        show(outcome === "added");
        current.announce(outcome === "added" ? current.doneLabelFor(pageNumber) : current.removedLabelFor(pageNumber));
      });
      button.addEventListener("page-action-sync", () => {
        const current = pageAction();
        if (current) show(current.isReferenced(pageNumber));
      });
      caption.prepend(document.createElement("span"));
      caption.append(button);
    }
    figure.append(canvas, caption);
    container.appendChild(figure);
    const context = canvas.getContext("2d");
    if (!context) continue;
    await page.render({ canvas, canvasContext: context, viewport }).promise;
  }
}

function DocxArtifactPreview({
  href,
  sizeBytes,
  onError,
  cacheRef,
}: {
  href: string;
  sizeBytes?: number | null;
  onError?: (errorClass: ResultsWorkspaceErrorClass) => void;
  cacheRef: MutableRefObject<DocumentPreviewCache>;
}) {
  const t = useTranslations("artifact_viewer");
  const [html, setHtml] = useState<string | null>(cacheRef.current.docxHtml ?? null);
  const [warningCount, setWarningCount] = useState(cacheRef.current.docxWarningCount ?? 0);
  const [error, setError] = useState<"too_large" | "load_failed" | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fail = (
      code: "too_large" | "load_failed",
      errorClass: ResultsWorkspaceErrorClass,
    ) => {
      if (cancelled) return;
      setError(code);
      onError?.(errorClass);
    };

    if (sizeBytes != null && sizeBytes > MAX_DOCX_PREVIEW_BYTES) {
      fail("too_large", "too_large");
      return () => {
        cancelled = true;
      };
    }
    if (cacheRef.current.docxHtml !== undefined) {
      setHtml(cacheRef.current.docxHtml);
      setWarningCount(cacheRef.current.docxWarningCount ?? 0);
      return () => {
        cancelled = true;
      };
    }

    void (async () => {
      try {
        const [mammoth, domPurify, arrayBuffer] = await Promise.all([
          import("mammoth"),
          import("dompurify"),
          fetchDocumentBytes(href, MAX_DOCX_PREVIEW_BYTES),
        ]);
        const result = await mammoth.convertToHtml({ arrayBuffer });
        if (cancelled) return;
        const sanitized = domPurify.default.sanitize(result.value, {
          USE_PROFILES: { html: true },
          FORBID_TAGS: ["script", "iframe", "object", "embed", "form", "svg", "math"],
          FORBID_ATTR: ["src", "srcset", "href", "action", "formaction", "target"],
        });
        const nextHtml = String(sanitized);
        const nextWarningCount = result.messages?.length ?? 0;
        cacheRef.current.docxHtml = nextHtml;
        cacheRef.current.docxWarningCount = nextWarningCount;
        setWarningCount(nextWarningCount);
        setHtml(nextHtml);
      } catch (cause) {
        const classified = classifyResultsError(cause);
        const errorClass = classified === "unknown" ? "corrupt" : classified;
        fail(errorClass === "too_large" ? "too_large" : "load_failed", errorClass);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [cacheRef, href, onError, sizeBytes]);

  if (error) {
    return (
      <div className="artifact-viewer-note error" role="alert">
        {t(error === "too_large" ? "document_too_large" : "document_load_failed")}
      </div>
    );
  }
  if (html === null) {
    return <div className="artifact-viewer-note loading">{t("document_loading_docx")}</div>;
  }
  return (
    <article className="artifact-docx-preview" data-testid="artifact-preview-docx">
      <div dangerouslySetInnerHTML={{ __html: html }} />
      {warningCount > 0 ? (
        <div className="artifact-viewer-footnote">
          {t("document_partial_fidelity", { count: warningCount })}
        </div>
      ) : null}
    </article>
  );
}
