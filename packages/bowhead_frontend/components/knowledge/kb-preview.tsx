"use client";

import { appFetch as fetch } from "@/lib/base-path";

/**
 * V7 KB · file preview switcher.
 *
 * Tech design v3.6 §7.4-§7.5 spec:
 * - PDF: pdfjs-dist (page-by-page render, controllable + independent of browser native viewer)
 * - DOCX: mammoth.js (~200KB, browser-side .docx → HTML, ~80% docx OK)
 * - Markdown / text/plain: existing MarkdownWithCharts
 *
 * Triggered from KbFileDrawer "preview" toggle button.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import type { PDFDocumentLoadingTask } from "pdfjs-dist";

import { DialogShell } from "@/components/ui/dialog";
import { readApiError } from "@/lib/api-client";
import { kbFileError } from "@/lib/kb-file-error";
import { MarkdownWithCharts } from "@/lib/md";
import { type KbFileDTO, downloadKbFileBlob } from "@/lib/kb-api";
import { spaceHeader } from "@/lib/space-storage";
import { CharIcon, CharIconText } from "@/components/char-icon";


interface Props {
  file: KbFileDTO;
  onClose: () => void;
}


function isPdfMime(mime: string): boolean {
  return mime === "application/pdf";
}

function isMarkdownMime(mime: string): boolean {
  return mime === "text/markdown" || mime === "text/x-markdown" || mime === "text/plain";
}

function getWorkspaceHeader(): HeadersInit {
  // layout-redesign P5 workspace-id centralization: canonical active-workspace key
  // (centralized space-storage); the old sessionStorage key was dead.
  if (typeof window === "undefined") return {};
  return spaceHeader();  // Batch 3/c2 centralization
}


export function KbPreview({ file, onClose }: Props) {
  const t = useTranslations("knowledge_base.preview");
  const tKb = useTranslations("knowledge_base");
  const [failure, setFailure] = useState<{ error: unknown; canDownload: boolean } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [downloading, setDownloading] = useState(false);
  const onFailure = useCallback((error: unknown, canDownload: boolean) => setFailure({ error, canDownload }), []);
  const failureKey = failure ? kbFileError(failure.error, "preview") : null;
  const inaccessible = failureKey === "errors.access_denied" || failureKey === "errors.file_not_found" || failureKey === "errors.sign_in_again";
  // Preview overlays file-detail drawer (DialogShell) as nested DialogShell: Esc closes preview only, never its drawer.
  return (
    <DialogShell
      presentation="center"
      onClose={onClose}
      label={inaccessible ? t("unavailable_title") : file.filename}
      layer="top"
      // Former inline styles live in the kb-preview-* classes (styles/ui-controls-legacy.css); the refreshed viewer look is in
      // styles/ui-refresh-kb.css (P2 batch 5).
      backdropClassName="kb-preview-overlay"
      panelProps={{ "data-testid": "kb-preview-modal" } as Record<string, string>}
      panelClassName={`kb-preview-panel${failure ? " is-compact" : ""}`}
    >
        <header className="kb-preview-head">
          <strong className="kb-preview-title" title={inaccessible ? undefined : file.filename}>{inaccessible ? t("unavailable_title") : file.filename}</strong>
          <button
            data-dialog-initial-focus
            type="button"
            className="kb-preview-close"
            onClick={onClose}
            aria-label={t("close_aria")}
          >
            <CharIcon ch="✕" />
          </button>
        </header>

        <div className="kb-preview-body">
          {failure ? (
            <div data-testid="kb-preview-error" className="kb-preview-error-card">
              <p role="alert">{inaccessible ? t("unavailable_detail") : failure.canDownload ? t("render_failed") : tKb(failureKey!)}</p>
              <div className="dr-acts">
                {!inaccessible && <button type="button" className="kb-btn kb-btn-secondary" onClick={() => { setFailure(null); setAttempt((n) => n + 1); }}>{t("retry")}</button>}
                {failure.canDownload && !inaccessible && <button type="button" disabled={downloading} className="kb-btn kb-btn-secondary" onClick={async () => {
                  setDownloading(true);
                  try { await downloadKbFileBlob(file.file_id, file.filename); }
                  catch (error) { setFailure({ error, canDownload: false }); }
                  finally { setDownloading(false); }
                }}><CharIconText ch="→" at="end">{t("download_to_view")}</CharIconText></button>}
                <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>{t("close_aria")}</button>
              </div>
            </div>
          ) : isPdfMime(file.mime_type) ? (
            <KbPdfPreview key={`${file.file_id}:${attempt}`} file={file} onFailure={onFailure} />
          ) : file.mime_type.includes("wordprocessingml.document") ? (
            <KbDocxPreview key={`${file.file_id}:${attempt}`} file={file} onFailure={onFailure} />
          ) : isMarkdownMime(file.mime_type) ? (
            <KbMarkdownPreview key={`${file.file_id}:${attempt}`} file={file} onFailure={onFailure} />
          ) : (
            <UnsupportedPreview mime={file.mime_type} />
          )}
        </div>
    </DialogShell>
  );
}


// ─── PDF preview (pdfjs-dist) ─────────────────────────


// [P1-15-2] Initial render limited to N pages (~50MB canvas memory cap); large documents paginate with "load more".
const PDF_PAGES_PER_BATCH = 20;


function KbPdfPreview({ file, onFailure }: { file: KbFileDTO; onFailure: (error: unknown, canDownload: boolean) => void }) {
  const t = useTranslations("knowledge_base.preview");
  const containerRef = useRef<HTMLDivElement>(null);
  const pdfRef = useRef<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [renderedThru, setRenderedThru] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);

  // Load PDF + render first batch
  useEffect(() => {
    let cancelled = false;
    let fetchedOriginal = false;
    let loadingTask: PDFDocumentLoadingTask | undefined;
    const url = `/api/v1/kb/files/${encodeURIComponent(file.file_id)}/preview`;

    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();

        const r = await fetch(url, { headers: getWorkspaceHeader() });
        if (!r.ok) throw await readApiError(r);
        const buf = await r.arrayBuffer();
        fetchedOriginal = true;
        if (cancelled) return;

        loadingTask = pdfjs.getDocument({ data: buf });
        const pdf = await loadingTask.promise;
        if (cancelled) return;
        pdfRef.current = pdf;
        setPageCount(pdf.numPages);
        setLoading(false);

        // First batch
        const upTo = Math.min(PDF_PAGES_PER_BATCH, pdf.numPages);
        await renderPdfPages(pdf, 1, upTo, containerRef.current, () => cancelled);
        if (!cancelled) setRenderedThru(upTo);
      } catch (e) {
        if (!cancelled) {
          onFailure(e, fetchedOriginal);
        }
      }
    })();

    return () => {
      cancelled = true;
      pdfRef.current = null;
      // PDF.js 6 owns worker teardown on the loading task, including pending loads.
      void loadingTask?.destroy().catch(() => undefined);
    };
  }, [file.file_id, onFailure]);

  const handleLoadMore = async () => {
    const pdf = pdfRef.current as { numPages: number; getPage: (n: number) => Promise<unknown> } | null;
    if (!pdf || pageCount === null) return;
    const start = renderedThru + 1;
    const upTo = Math.min(renderedThru + PDF_PAGES_PER_BATCH, pageCount);
    setLoadingMore(true);
    try {
      await renderPdfPages(pdf, start, upTo, containerRef.current, () => false);
      setRenderedThru(upTo);
    } catch (e) {
      onFailure(e, true);
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div data-testid="kb-preview-pdf" className="kb-preview-pdf">
      {loading && <p className="kb-preview-state">{t("loading_pdf")}</p>}
      {pageCount !== null && (
        <p className="kb-preview-pages">
          {t("page_count_with_rendered", { rendered: renderedThru, count: pageCount })}
        </p>
      )}
      <div ref={containerRef} />
      {pageCount !== null && renderedThru < pageCount && (
        <div className="kb-preview-more">
          <button
            type="button"
            onClick={handleLoadMore}
            disabled={loadingMore}
            className="kb-btn kb-btn-secondary"
            data-testid="kb-pdf-load-more"
          >
            {loadingMore ? t("loading_pdf") : t("load_next_pages", { n: PDF_PAGES_PER_BATCH })}
          </button>
        </div>
      )}
    </div>
  );
}


/** [P1-15-2] Render PDF pages [start, end] inclusive into container. */
async function renderPdfPages(
  pdf: { getPage: (n: number) => Promise<unknown> },
  start: number,
  end: number,
  container: HTMLDivElement | null,
  isCancelled: () => boolean,
): Promise<void> {
  if (!container) return;
  for (let i = start; i <= end; i++) {
    if (isCancelled()) return;
    const pg = (await pdf.getPage(i)) as {
      getViewport: (opts: { scale: number }) => { width: number; height: number };
      render: (opts: {
        canvas: HTMLCanvasElement;
        canvasContext: CanvasRenderingContext2D;
        viewport: unknown;
      }) => { promise: Promise<void> };
    };
    const viewport = pg.getViewport({ scale: 1.5 });

    const canvas = document.createElement("canvas");
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    canvas.style.display = "block";
    canvas.style.margin = "8px auto";
    canvas.style.boxShadow = "0 1px 3px color-mix(in srgb, var(--c-ink) 20%, transparent)";
    canvas.dataset.page = String(i);
    container.appendChild(canvas);

    const ctx = canvas.getContext("2d");
    if (!ctx) continue;
    await pg.render({ canvas, canvasContext: ctx, viewport }).promise;
  }
}


// ─── DOCX preview (mammoth.js) ───────────────────────


function KbDocxPreview({ file, onFailure }: { file: KbFileDTO; onFailure: (error: unknown, canDownload: boolean) => void }) {
  const t = useTranslations("knowledge_base.preview");
  const [html, setHtml] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    let fetchedOriginal = false;
    const url = `/api/v1/kb/files/${encodeURIComponent(file.file_id)}/preview`;

    (async () => {
      try {
        // dynamic import mammoth (~200KB) + DOMPurify (XSS sanitize, P0-15-1)
        const [mammoth, DOMPurify] = await Promise.all([
          import("mammoth"),
          import("dompurify"),
        ]);
        const r = await fetch(url, { headers: getWorkspaceHeader() });
        if (!r.ok) throw await readApiError(r);
        const buf = await r.arrayBuffer();
        fetchedOriginal = true;
        if (cancelled) return;
        const result = await mammoth.convertToHtml({ arrayBuffer: buf });
        if (cancelled) return;
        // [P0-15-1] Sanitize mammoth HTML—malicious uploaded DOCX may inject script / event handlers.
        const sanitized = DOMPurify.default.sanitize(result.value, {
          USE_PROFILES: { html: true },
          // Forbid all scripts / event handlers / data:/javascript: URIs.
          FORBID_TAGS: ["script", "iframe", "object", "embed", "form"],
          FORBID_ATTR: ["onclick", "onerror", "onload", "onmouseover", "onfocus", "onmouseenter"],
        });
        setHtml(sanitized);
        if (result.messages?.length) {
          setWarnings(result.messages.slice(0, 5).map((m: { message?: string }) => m.message || String(m)));
        }
      } catch (e) {
        if (!cancelled) onFailure(e, fetchedOriginal);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [file.file_id, onFailure]);

  if (html === null) {
    return <div className="kb-preview-state">{t("loading_docx")}</div>;
  }

  return (
    <div data-testid="kb-preview-docx" className="kb-preview-docx">
      <div className="kb-docx-html" dangerouslySetInnerHTML={{ __html: html }} />
      {warnings.length > 0 && (
        <details className="kb-preview-docx-warnings">
          <summary>{t("docx_warnings", { count: warnings.length })}</summary>
          <ul>
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}


// ─── Markdown preview ─────────────────────────────────


function KbMarkdownPreview({ file, onFailure }: { file: KbFileDTO; onFailure: (error: unknown, canDownload: boolean) => void }) {
  const t = useTranslations("knowledge_base.preview");
  const [text, setText] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/v1/kb/files/${encodeURIComponent(file.file_id)}/preview`, {
      headers: getWorkspaceHeader(),
    })
      .then(async (r) => {
        if (!r.ok) throw await readApiError(r);
        return r.text();
      })
      .then((s) => {
        if (!cancelled) setText(s);
      })
      .catch((e) => {
        if (!cancelled) onFailure(e, false);
      });
    return () => {
      cancelled = true;
    };
  }, [file.file_id, onFailure]);

  if (text === null) {
    return <div className="kb-preview-state">{t("loading_md")}</div>;
  }
  return (
    <div data-testid="kb-preview-md" className="kb-preview-md">
      <MarkdownWithCharts markdown={text} />
    </div>
  );
}


function UnsupportedPreview({ mime }: { mime: string }) {
  const t = useTranslations("knowledge_base.preview");
  return (
    <div data-testid="kb-preview-unsupported" className="kb-preview-state">
      {t("unsupported_type", { mime })}
    </div>
  );
}
