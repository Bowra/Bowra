"use client";

import { useEffect, useRef, useState } from "react";
import type { PDFDocumentLoadingTask } from "pdfjs-dist";

import { pageTextItems, type PageTextItem, type PdfTextLike } from "@/lib/presentation-edit";

/** The parts of a pdf.js document the editor uses. */
export interface PdfPageLike {
  view: number[];
  getViewport: (options: { scale: number }) => { width: number; height: number };
  render: (options: { canvas: HTMLCanvasElement; canvasContext: CanvasRenderingContext2D; viewport: unknown }) => {
    promise: Promise<void>;
    cancel: () => void;
  };
  getTextContent: () => Promise<{ items: Array<PdfTextLike & { fontName?: string }> }>;
  /** Fonts resolved while drawing; with `fontExtraProperties` a font carries its PDF name. */
  commonObjs?: { get: (id: string) => unknown };
}

export interface PdfDocumentLike {
  numPages: number;
  getPage: (page: number) => Promise<PdfPageLike>;
}

/** Opens a rendered deck. pdf.js takes ownership of its input, so it gets a copy. */
export function usePdfDocument(data: ArrayBuffer | null): { pdf: PdfDocumentLike | null; failed: boolean } {
  const [state, setState] = useState<{ data: ArrayBuffer | null; pdf: PdfDocumentLike | null; failed: boolean }>({ data: null, pdf: null, failed: false });
  useEffect(() => {
    if (!data) return;
    let cancelled = false;
    let task: PDFDocumentLoadingTask | undefined;
    void (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
        // Font names let edits on the slide take the deck's family and weight (design §13.3).
        task = pdfjs.getDocument({ data: data.slice(0), fontExtraProperties: true });
        const pdf = await task.promise as unknown as PdfDocumentLike;
        if (!cancelled) setState({ data, pdf, failed: false });
      } catch {
        if (!cancelled) setState({ data, pdf: null, failed: true });
      }
    })();
    return () => {
      cancelled = true;
      void task?.destroy().catch(() => undefined);
    };
  }, [data]);
  return state.data === data ? { pdf: state.pdf, failed: state.failed } : { pdf: null, failed: false };
}

/** One page drawn at `width` CSS pixels; the canvas is sharp on high-density screens. */
export function PdfPageCanvas({
  pdf, pageNumber, width, label, className, onDrawn,
}: {
  pdf: PdfDocumentLike | null;
  pageNumber: number;
  width: number;
  /** Without a label the page is decorative. */
  label?: string;
  className?: string;
  /** Called after each draw, with the canvas holding it. */
  onDrawn?: (canvas: HTMLCanvasElement) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [drawn, setDrawn] = useState(false);
  const onDrawnRef = useRef(onDrawn);
  onDrawnRef.current = onDrawn;
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!pdf || !canvas || width <= 0) return;
    let cancelled = false;
    let task: { promise: Promise<void>; cancel: () => void } | undefined;
    setDrawn(false);
    void (async () => {
      try {
        const page = await pdf.getPage(pageNumber);
        if (cancelled) return;
        const base = page.getViewport({ scale: 1 });
        const ratio = typeof window === "undefined" ? 1 : Math.min(2, window.devicePixelRatio || 1);
        const viewport = page.getViewport({ scale: (width / base.width) * ratio });
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        const context = canvas.getContext("2d");
        if (!context) return;
        task = page.render({ canvas, canvasContext: context, viewport });
        await task.promise;
        if (!cancelled) {
          setDrawn(true);
          onDrawnRef.current?.(canvas);
        }
      } catch { /* A cancelled or failed draw leaves the previous frame; the editor reports load failures. */ }
    })();
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [pdf, pageNumber, width]);
  return label
    ? <canvas ref={canvasRef} className={className} aria-label={label} role="img" data-drawn={drawn || undefined} />
    : <canvas ref={canvasRef} className={className} aria-hidden="true" data-drawn={drawn || undefined} />;
}

/** The text runs of one rendered page, for the overflow check. */
export async function readPageText(pdf: PdfDocumentLike, pageNumber: number): Promise<PageTextItem[]> {
  const page = await pdf.getPage(pageNumber);
  const content = await page.getTextContent();
  return pageTextItems(content.items, page.view);
}

/** Opens a rendered deck outside React, for a one-off read; always call `close`. */
export async function openPdfDocument(data: ArrayBuffer): Promise<{ pdf: PdfDocumentLike; close: () => void }> {
  const pdfjs = await import("pdfjs-dist");
  pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();
  const task = pdfjs.getDocument({ data: data.slice(0) });
  const pdf = await task.promise as unknown as PdfDocumentLike;
  return { pdf, close: () => { void task.destroy().catch(() => undefined); } };
}

/** A page drawn only once it nears the viewport, for the phone's long list of slides. */
/**
 * Draws a page once it comes near view. `aspectRatio` (width / height) gives the placeholder its
 * height before drawing; without it, a holder in an auto-height box is 0 px tall and every page
 * counts as near.
 */
export function LazyPdfPageCanvas({ aspectRatio, ...props }: Parameters<typeof PdfPageCanvas>[0] & { aspectRatio?: number }) {
  const holderRef = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(() => typeof IntersectionObserver !== "function");
  useEffect(() => {
    const holder = holderRef.current;
    if (near || !holder) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setNear(true);
        observer.disconnect();
      }
    }, { rootMargin: "600px 0px" });
    observer.observe(holder);
    return () => observer.disconnect();
  }, [near]);
  return (
    <div ref={holderRef} style={{ width: "100%", height: "100%", ...(near || !aspectRatio ? {} : { aspectRatio: String(aspectRatio) }) }}>
      {near ? <PdfPageCanvas {...props} /> : null}
    </div>
  );
}
