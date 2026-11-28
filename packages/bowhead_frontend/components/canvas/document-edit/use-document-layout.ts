"use client";

/**
 * The Word editor's view of its pages (docx inline edit design §8.2): the rendered pages' PDF, read
 * once per version, and where each inventory paragraph sits on them. The PDF is the `docx-pdf`
 * representation the Results preview already fetched and kept (passed in), else fetched here.
 */

import { useEffect, useMemo, useState } from "react";

import { appPath } from "@/lib/base-path";
import type { DocumentInventory } from "@/lib/document-edit";
import { layoutDocument, readLocatePages, type DocumentLayout } from "@/lib/document-layout";
import type { LocatePage } from "@/lib/document-locate";
import { fetchPresentationPdf, isPresentationPreviewBusy } from "../artifact-document-preview";
import { usePdfDocument, type PdfDocumentLike } from "../presentation-edit/pdf-page";

export type PagesState = "loading" | "ready" | "failed" | "busy";

export function documentPagesHref(artifactId: string): string {
  return appPath(`/api/v1/files/${encodeURIComponent(artifactId)}/representations/docx-pdf`);
}

/**
 * Loads a version's pages, or takes pages already in hand (the preview's, or a save's new render).
 * `artifactId` null loads nothing.
 */
export function useDocumentPages(artifactId: string | null, initialData?: ArrayBuffer | null): {
  pdf: PdfDocumentLike | null;
  data: ArrayBuffer | null;
  state: PagesState;
  reload: () => void;
} {
  const [loaded, setLoaded] = useState<{ id: string | null; data: ArrayBuffer | null; state: PagesState }>(
    () => ({ id: artifactId, data: initialData ?? null, state: initialData ? "ready" : "loading" }),
  );
  const [attempt, setAttempt] = useState(0);
  const current = loaded.id === artifactId ? loaded
    : initialData ? { id: artifactId, data: initialData, state: "ready" as PagesState }
      : { id: artifactId, data: null, state: "loading" as PagesState };
  const needsFetch = Boolean(artifactId) && !current.data && current.state === "loading";
  useEffect(() => {
    if (!artifactId || !needsFetch) return;
    const controller = new AbortController();
    fetchPresentationPdf(documentPagesHref(artifactId), controller.signal).then(
      (data) => { if (!controller.signal.aborted) setLoaded({ id: artifactId, data, state: "ready" }); },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({ id: artifactId, data: null, state: isPresentationPreviewBusy(error) ? "busy" : "failed" });
      },
    );
    return () => controller.abort();
    // A version's pages never change: refetch only for another version or a retry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifactId, attempt, needsFetch]);
  const { pdf, failed } = usePdfDocument(artifactId ? current.data : null);
  return {
    pdf,
    data: current.data,
    state: !artifactId ? "loading" : failed ? "failed" : current.state,
    reload: () => {
      setLoaded({ id: artifactId, data: null, state: "loading" });
      setAttempt((value) => value + 1);
    },
  };
}

/** Where the inventory's paragraphs are on the pages; null until the pages' text has been read. */
export function useDocumentLayout(
  pdf: PdfDocumentLike | null,
  inventory: (Pick<DocumentInventory, "paragraphs"> & Partial<Pick<DocumentInventory, "read_only_paragraphs">>) | null,
): DocumentLayout | null {
  const [pages, setPages] = useState<{ pdf: PdfDocumentLike; pages: LocatePage[] } | null>(null);
  useEffect(() => {
    if (!pdf) return;
    const controller = new AbortController();
    readLocatePages(pdf, controller.signal).then(
      (read) => { if (!controller.signal.aborted) setPages({ pdf, pages: read }); },
      () => { /* No text: every paragraph is edited in 本页内容. */ if (!controller.signal.aborted) setPages({ pdf, pages: [] }); },
    );
    return () => controller.abort();
  }, [pdf]);
  return useMemo(
    () => (pages && pdf && pages.pdf === pdf && inventory ? layoutDocument(pages.pages, inventory) : null),
    [inventory, pages, pdf],
  );
}
