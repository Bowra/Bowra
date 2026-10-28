import { act, cleanup, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ArtifactDocumentPreview } from "@/components/canvas/artifact-document-preview";
import { KbPreview } from "@/components/knowledge/kb-preview";
import type { KbFileDTO } from "@/lib/kb-api";

const { getDocument } = vi.hoisted(() => ({ getDocument: vi.fn() }));

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument,
}));

const viewers = ["artifact", "knowledge"] as const;

function openPreview(viewer: typeof viewers[number]) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new Uint8Array([1]))));
  return renderIntl(viewer === "artifact" ? (
    <ArtifactDocumentPreview
      profile="pdf"
      href="/api/v1/files/art_pdf"
      title="Example PDF"
      cacheRef={{ current: {} }}
    />
  ) : (
    <KbPreview
      file={{ file_id: "kbf_pdf", filename: "example.pdf", mime_type: "application/pdf" } as KbFileDTO}
      onClose={() => undefined}
    />
  ));
}

function pdfDocument() {
  return {
    numPages: 1,
    getPage: vi.fn().mockResolvedValue({
      getViewport: () => ({ width: 10, height: 10 }),
      render: () => ({ promise: Promise.resolve() }),
    }),
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  getDocument.mockReset();
});

describe.each(viewers)("%s PDF worker lifecycle", (viewer) => {
  it("destroys a pending loading task and ignores its late document", async () => {
    const pending = Promise.withResolvers<ReturnType<typeof pdfDocument>>();
    const destroy = vi.fn().mockResolvedValue(undefined);
    getDocument.mockReturnValue({ promise: pending.promise, destroy });
    const preview = openPreview(viewer);
    await waitFor(() => expect(getDocument).toHaveBeenCalledOnce());

    preview.unmount();
    expect(destroy).toHaveBeenCalledOnce();
    const document = pdfDocument();
    await act(async () => pending.resolve(document));
    expect(document.getPage).not.toHaveBeenCalled();
  });

  it("renders a PDF.js 6 document and destroys its loading task on close", async () => {
    const document = pdfDocument();
    const destroy = vi.fn().mockResolvedValue(undefined);
    getDocument.mockReturnValue({ promise: Promise.resolve(document), destroy });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D);
    const preview = openPreview(viewer);
    await waitFor(() => expect(document.getPage).toHaveBeenCalledWith(1));
    await waitFor(() => expect(preview.baseElement.querySelector("canvas")).not.toBeNull());

    preview.unmount();
    expect(destroy).toHaveBeenCalledOnce();
  });
});
