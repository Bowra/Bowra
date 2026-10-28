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
import {
  classifyResultsError,
  classifyResultsResponse,
  ResultsWorkspaceError,
  type ResultsWorkspaceErrorClass,
} from "@/lib/results-workspace-errors";

const PDF_PAGES_PER_BATCH = 10;
const MAX_PDF_PREVIEW_BYTES = 25 * 1024 * 1024;
const MAX_DOCX_PREVIEW_BYTES = 15 * 1024 * 1024;

async function fetchDocumentBytes(href: string, maxBytes: number): Promise<ArrayBuffer> {
  let response: Response;
  try {
    response = await appFetch(href, { cache: "no-store" });
  } catch (cause) {
    const errorClass = classifyResultsError(cause);
    throw new ResultsWorkspaceError(errorClass);
  }
  if (!response.ok) {
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

type DocumentProfile = "pdf" | "docx";

export type DocumentPreviewCache = {
  pdfData?: ArrayBuffer;
  pdfRenderedThru?: number;
  docxHtml?: string;
  docxWarningCount?: number;
};

export function ArtifactDocumentPreview({
  profile,
  href,
  title,
  sizeBytes,
  onError,
  cacheRef,
}: {
  profile: DocumentProfile;
  href: string;
  title: string;
  sizeBytes?: number | null;
  onError?: (errorClass: ResultsWorkspaceErrorClass) => void;
  cacheRef: MutableRefObject<DocumentPreviewCache>;
}) {
  if (profile === "pdf") {
    return (
      <PdfArtifactPreview
        href={href}
        title={title}
        sizeBytes={sizeBytes}
        onError={onError}
        cacheRef={cacheRef}
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

function PdfArtifactPreview({
  href,
  title,
  sizeBytes,
  onError,
  cacheRef,
}: {
  href: string;
  title: string;
  sizeBytes?: number | null;
  onError?: (errorClass: ResultsWorkspaceErrorClass) => void;
  cacheRef: MutableRefObject<DocumentPreviewCache>;
}) {
  const t = useTranslations("artifact_viewer");
  const pagesRef = useRef<HTMLDivElement>(null);
  const pdfRef = useRef<PdfDocumentLike | null>(null);
  const cancelledRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageCount, setPageCount] = useState(0);
  const [renderedThru, setRenderedThru] = useState(0);
  const [error, setError] = useState<"too_large" | "load_failed" | null>(null);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | undefined;
    cancelledRef.current = false;
    const fail = (
      code: "too_large" | "load_failed",
      errorClass: ResultsWorkspaceErrorClass,
    ) => {
      if (cancelled) return;
      setError(code);
      setLoading(false);
      onError?.(errorClass);
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
          const fetched = await fetchDocumentBytes(href, MAX_PDF_PREVIEW_BYTES);
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
          Math.max(PDF_PAGES_PER_BATCH, cacheRef.current.pdfRenderedThru ?? 0),
          pdf.numPages,
        );
        await renderPdfPages(pdf, 1, end, pagesRef.current, () => cancelled, title);
        if (!cancelled) {
          cacheRef.current.pdfRenderedThru = end;
          setRenderedThru(end);
        }
      } catch (cause) {
        const classified = classifyResultsError(cause);
        const errorClass = classified === "unknown" ? "corrupt" : classified;
        fail(errorClass === "too_large" ? "too_large" : "load_failed", errorClass);
      }
    })();

    return () => {
      cancelled = true;
      cancelledRef.current = true;
      pdfRef.current = null;
      // PDF.js 6 owns worker teardown on the loading task, including pending loads.
      void loadingTask?.destroy().catch(() => undefined);
    };
  }, [cacheRef, href, onError, sizeBytes, title]);

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

  if (error) {
    return (
      <div className="artifact-viewer-note error" role="alert">
        {t(error === "too_large" ? "document_too_large" : "document_load_failed")}
      </div>
    );
  }

  return (
    <div className="artifact-document-preview" data-testid="artifact-preview-pdf">
      <div className="artifact-document-status" aria-live="polite">
        {loading
          ? t("document_loading_pdf")
          : t("document_pages_shown", { shown: renderedThru, total: pageCount })}
      </div>
      <div className="artifact-pdf-pages" ref={pagesRef} />
      {!loading && renderedThru < pageCount ? (
        <button
          type="button"
          className="artifact-document-load-more"
          onClick={() => void loadMore()}
          disabled={loadingMore}
        >
          {loadingMore ? t("document_loading_pdf") : t("document_load_more")}
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

async function renderPdfPages(
  pdf: PdfDocumentLike,
  start: number,
  end: number,
  container: HTMLDivElement | null,
  isCancelled: () => boolean,
  title: string,
): Promise<void> {
  if (!container) return;
  for (let pageNumber = start; pageNumber <= end; pageNumber += 1) {
    if (isCancelled()) return;
    const page = await pdf.getPage(pageNumber);
    if (isCancelled()) return;
    const viewport = page.getViewport({ scale: 1.4 });
    const figure = document.createElement("figure");
    figure.className = "artifact-pdf-page";
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    canvas.setAttribute("aria-label", `${title} · ${pageNumber}`);
    const caption = document.createElement("figcaption");
    caption.textContent = String(pageNumber);
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
