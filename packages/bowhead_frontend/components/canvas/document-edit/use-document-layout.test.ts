import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { documentPagesHref, useDocumentLayout, useDocumentPages } from "./use-document-layout";

const { fetchPresentationPdf } = vi.hoisted(() => ({ fetchPresentationPdf: vi.fn() }));
vi.mock("../artifact-document-preview", () => ({ fetchPresentationPdf, isPresentationPreviewBusy: () => false }));
vi.mock("../presentation-edit/pdf-page", () => ({
  usePdfDocument: (data: ArrayBuffer | null) => ({ pdf: data ? { numPages: 0, getPage: vi.fn() } : null, failed: false }),
}));

afterEach(() => vi.clearAllMocks());

describe("Word editor pages (docx inline edit design §8.2)", () => {
  it("fetches the version's pages unless the preview kept them, and says when they failed", async () => {
    fetchPresentationPdf.mockResolvedValueOnce(new ArrayBuffer(4));
    const fetched = renderHook(() => useDocumentPages("art_doc_2"));
    await waitFor(() => expect(fetched.result.current.state).toBe("ready"));
    expect(fetchPresentationPdf).toHaveBeenCalledWith(documentPagesHref("art_doc_2"), expect.any(AbortSignal));
    expect(documentPagesHref("art_doc_2")).toContain("/api/v1/files/art_doc_2/representations/docx-pdf");

    fetchPresentationPdf.mockClear();
    const kept = renderHook(() => useDocumentPages("art_doc_1", new ArrayBuffer(4)));
    expect(kept.result.current.state).toBe("ready");
    expect(fetchPresentationPdf).not.toHaveBeenCalled();

    fetchPresentationPdf.mockRejectedValueOnce(new Error("unavailable"));
    const failed = renderHook(() => useDocumentPages("art_doc_3"));
    await waitFor(() => expect(failed.result.current.state).toBe("failed"));
  });

  it("locates the paragraphs once the pages' text is read", async () => {
    const pdf = {
      numPages: 1,
      getPage: async () => ({
        view: [0, 0, 595, 842],
        getTextContent: async () => ({ items: [{ str: "一季度销售额两千九百万元", transform: [10, 0, 0, 10, 72, 742], width: 120, height: 10 }] }),
      }),
      render: undefined,
    };
    const inventory = { artifact_id: "art_doc_1", paragraphs: [
      { key: "document:b1", kind: "body" as const, table: null, text: "一季度销售额两千九百万元", heading_level: null, editable: true, reason: null },
    ] };
    const { result } = renderHook(() => useDocumentLayout(pdf as never, inventory));
    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current?.paragraphs.get("document:b1")).toMatchObject({ status: "exact" });
    expect(result.current?.missing).toEqual([]);
  });
});
