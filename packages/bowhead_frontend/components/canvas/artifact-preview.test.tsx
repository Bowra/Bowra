import { installHtmlPreviewWorker } from "@/__tests__/helpers/html-preview-worker";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";
import { ArtifactPreview } from "./artifact-preview";
import { readPresentationHandoff } from "./presentation-edit/handoff";
import { setDocxInlineEditCapabilityForTest } from "@/lib/use-docx-inline-edit-capability";

const { pdfGetDocument } = vi.hoisted(() => ({
  pdfGetDocument: vi.fn(),
}));

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  getDocument: pdfGetDocument,
}));

vi.mock("mammoth", () => ({
  convertToHtml: vi.fn().mockResolvedValue({ value: "<p>Reading view text</p>", messages: [] }),
}));

vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({
  default: "mock-pdf-worker.js",
}));

vi.mock("./presentation-edit/presentation-editor", async () => {
  const { forwardRef } = await import("react");
  return {
    PresentationEditor: forwardRef(function EditorStub(props: {
      artifactId: string;
      initialPdfData?: ArrayBuffer;
      initialPage?: number;
      onExit: (exit: { artifactId: string; pdfData?: ArrayBuffer; close: boolean; page?: number } | null) => void;
    }, _ref) {
      return (
        <div data-testid="presentation-editor-stub" data-initial-page={props.initialPage ?? ""}>
          {props.artifactId}
          <button type="button" onClick={() => props.onExit({ artifactId: "art_deck_v2", pdfData: new ArrayBuffer(3), close: false, page: 2 })}>
            saved
          </button>
          <button type="button" onClick={() => props.onExit({ artifactId: props.artifactId, close: false, page: 2 })}>
            leave
          </button>
        </div>
      );
    }),
  };
});

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "rows_shown_total") return `${values?.shown}/${values?.total} rows`;
    if (key === "video_player_label") {
      return `Video player for ${values?.filename ?? ""}`;
    }
    if (key === "video_play") return `Play ${values?.filename ?? ""}`;
    if (key === "table_open_row") return `Open row ${values?.row ?? ""}`;
    if (key === "workbook_column") return `Column ${values?.number ?? ""}`;
    if (key === "safe_html_too_large") return `HTML exceeds the ${values?.limit} preview limit. Download the original file.`;
    const labels: Record<string, string> = {
      close: "Close",
      download: "Download",
      empty: "No preview",
      loading: "Loading",
      no_preview: "No inline preview",
      open_full: "Full view",
      reference_in_chat: "Reference in chat",
      retry: "Retry",
      truncated: " (truncated)",
      truncated_rows: "(truncated)",
      safe_html_badge: "Safe static preview",
      safe_html_network_off: "Network disabled",
      safe_html_loading: "Preparing safe preview",
      safe_html_reload: "Reload",
      safe_html_hint: "Reading view",
      safe_html_unsupported: "No safely readable static HTML content, or the file type is unsupported.",
      video_ready: "Click to play",
      video_speed: "Speed",
      video_unavailable: "Video unavailable",
      video_download_hint: "Download the original",
      image_controls: "Image controls",
      image_fit: "Fit",
      image_actual: "100%",
      zoom_out: "Zoom out",
      zoom_in: "Zoom in",
      text_find: "Find in content",
      text_wrap: "Wrap",
      text_copy: "Copy",
      text_copied: "Copied",
      table_columns: "Columns",
      table_row_details: "Row details",
      document_too_large: "Document too large",
    };
    return labels[key] ?? key;
  },
}));

afterEach(() => {
  cleanup();
  setDocxInlineEditCapabilityForTest(null);
  pdfGetDocument.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ArtifactPreview", () => {
  it("keeps the generic safe-HTML rejection honest in both locales", () => {
    expect(zhMessages.artifact_viewer.safe_html_unsupported).toBe(
      "该 HTML 没有可安全读取的静态内容，或文件类型不受支持。",
    );
    expect(enMessages.artifact_viewer.safe_html_unsupported).toBe(
      "This HTML has no safely readable static content, or its file type is unsupported.",
    );
  });

  it("labels the header with the file type, keeping the MIME type out of it (narrow desktop layout §5)", () => {
    render(
      <ArtifactPreview
        fileId="art_xlsx_meta"
        filename="model.xlsx"
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        profile="xlsx"
        sizeLabel="5.1 KB"
        preview={{ family: "spreadsheet", mode: "text", text: "--- sheet: Summary ---\nRevenue\t42" }}
      />,
    );
    const meta = document.querySelector(".artifact-viewer-title > span");
    expect(meta?.textContent).toMatch(/^XLSX · 5\.1 KB · /);
    expect(meta?.textContent).not.toContain("application/");
    // The full-view action keeps its name and tooltip when a narrow header shows only its icon.
    const fullView = screen.getByRole("button", { name: "Full view" });
    expect(fullView).toHaveAttribute("title", "Full view");
    expect(fullView.querySelector(".result-action-label")?.textContent).toBe("Full view");
  });

  it("renders text preview with a stable download href and full-view modal", () => {
    render(
      <ArtifactPreview
        fileId="art_1"
        filename="analysis.txt"
        mime="text/plain"
        sizeLabel="1 KB"
        preview={{ family: "text", mode: "text", text: "line one", truncated: true }}
      />,
    );

    expect(screen.getByText("analysis.txt")).toBeInTheDocument();
    expect(screen.getByText(/line one/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/v1/files/art_1");

    fireEvent.click(screen.getByRole("button", { name: "Full view" }));
    const dialog = screen.getByRole("dialog", { name: "analysis.txt" });
    expect(within(dialog).getByText(/line one/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0]!);
    expect(screen.queryByRole("dialog", { name: "analysis.txt" })).not.toBeInTheDocument();
  });

  it("renders row previews with the supplied columns", () => {
    render(
      <ArtifactPreview
        fileId="art_rows"
        filename="query.csv"
        preview={{
          family: "table",
          mode: "rows",
          columns: ["city", "revenue"],
          rows: [{ city: "Shanghai", revenue: 42 }],
          shown_rows: 1,
          total_rows: 12,
          truncated: true,
        }}
        downloadHref="/custom/download"
      />,
    );

    expect(screen.getByText(/1\/12 rows/)).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "city" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Shanghai" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute("href", "/custom/download");
  });

  it("references the current artifact without downloading it", () => {
    const onReference = vi.fn();
    render(
      <ArtifactPreview
        fileId="art_ref"
        filename="evidence.csv"
        preview={{ family: "text", mode: "text", text: "row" }}
        onReference={onReference}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reference in chat" }));
    expect(onReference).toHaveBeenCalledTimes(1);
  });

  it("offers an exact-object retry for a classified preview failure", () => {
    const onRetry = vi.fn();
    render(
      <ArtifactPreview
        fileId="art_failed"
        filename="failed.csv"
        preview={null}
        error="Network problem"
        onRetry={onRetry}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Network problem");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("can hide the inline header title while keeping preview actions", () => {
    render(
      <ArtifactPreview
        fileId="art_inline"
        filename="query.csv"
        preview={{ family: "text", mode: "text", text: "hello" }}
        showHeaderTitle={false}
      />,
    );

    expect(screen.queryByText("query.csv")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/v1/files/art_inline");
    expect(screen.getByRole("button", { name: "Full view" })).toBeInTheDocument();
  });

  it("loads an exact HTML artifact URL into the safe static frame", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        "<html><body><h1>Quarterly result</h1><script>alert(1)</script></body></html>",
        { status: 200, headers: { "content-type": "text/html" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ArtifactPreview
        fileId="art_html"
        filename="result.html"
        mime="text/html"
        sizeBytes={128}
        sizeLabel="128 B"
        preview={null}
        downloadHref="/api/v1/files/art_html"
        contentHref="/api/v1/sessions/s_1/artifacts/art_html/content"
      />,
    );

    await waitFor(() =>
      expect(document.querySelector("iframe[title='result.html']")).toBeInTheDocument(),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/sessions/s_1/artifacts/art_html/content",
      expect.objectContaining({ cache: "no-store" }),
    );
    const frame = document.querySelector("iframe[title='result.html']");
    expect(frame).toHaveAttribute("sandbox", "");
    expect(frame?.getAttribute("srcdoc")).not.toContain("<script>");
    expect(screen.getAllByRole("link", { name: "Download" })[0]).toHaveAttribute(
      "href",
      "/api/v1/files/art_html",
    );

    fireEvent.click(screen.getByRole("button", { name: "Full view" }));
    const dialog = screen.getByRole("dialog", { name: "result.html" });
    const download = within(dialog).getByRole("link", { name: "Download" });
    download.focus();
    fireEvent.keyDown(download, { key: "Tab", shiftKey: true });
    expect(dialog.querySelector("iframe[title='result.html']")).toHaveFocus();
  });

  it("shows the served HTML limit and preserves download access after rejection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<p>Oversize report</p>", {
      headers: { "content-type": "text/html", "X-Bowhead-HTML-Max-Bytes": "16" },
    })));
    const onOpenFailed = vi.fn();
    render(<ArtifactPreview fileId="art_large" filename="report.html" mime="text/html" preview={null} onOpenFailed={onOpenFailed} />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("16 B preview limit"));
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledWith("too_large"));
    expect(screen.getAllByRole("link", { name: "Download" })[0]).toHaveAttribute("href", "/api/v1/files/art_large");
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("explains a script-only HTML rejection without contradicting its MIME", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("<html><body><script>document.body.textContent='unsafe'</script></body></html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      }),
    ));

    render(
      <ArtifactPreview
        fileId="art_script_only"
        filename="app.html"
        mime="text/html"
        sizeBytes={96}
        preview={null}
        contentHref="/api/v1/sessions/s_1/artifacts/art_script_only/content"
      />,
    );

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(
      "No safely readable static HTML content, or the file type is unsupported.",
    ));
    expect(document.querySelector("iframe[title='app.html']")).not.toBeInTheDocument();
  });

  it("renders one exact-ID native video stream and moves it into full view", () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => undefined);
    render(
      <ArtifactPreview
        fileId="art_video"
        filename="result.mp4"
        mime="video/mp4"
        preview={null}
        downloadHref="/api/v1/files/art_video"
      />,
    );

    expect(screen.getByLabelText("Video player for result.mp4")).toHaveAttribute(
      "src",
      "/api/v1/files/art_video?disposition=inline",
    );
    expect(screen.getAllByRole("link", { name: "Download" })[0]).toHaveAttribute(
      "href",
      "/api/v1/files/art_video",
    );
    const inlinePlayer = screen.getByLabelText(
      "Video player for result.mp4",
    );
    Object.defineProperty(inlinePlayer, "paused", {
      configurable: true,
      value: false,
    });

    fireEvent.click(screen.getByRole("button", { name: "Full view" }));
    const dialog = screen.getByRole("dialog", { name: "result.mp4" });
    expect(
      within(dialog).getByLabelText("Video player for result.mp4"),
    ).toBeInTheDocument();
    expect(
      screen.getAllByLabelText("Video player for result.mp4"),
    ).toHaveLength(1);
    expect(pause).toHaveBeenCalledTimes(1);
  });

  it("keeps image zoom state while moving between inline and full view", () => {
    render(
      <ArtifactPreview
        fileId="art_image"
        filename="chart.png"
        mime="image/png"
        preview={null}
        profile="image"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "100%" }));
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByText("125%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Full view" }));

    const dialog = screen.getByRole("dialog", { name: "chart.png" });
    expect(within(dialog).getByText("125%")).toBeInTheDocument();
    expect(within(dialog).getByRole("img", { name: "chart.png" })).toHaveAttribute(
      "src",
      "/api/v1/files/art_image?disposition=inline",
    );
  });

  it("formats JSON, finds matches, toggles wrapping, and copies the readable text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <ArtifactPreview
        fileId="art_json"
        filename="result.json"
        mime="application/json"
        profile="json"
        preview={{ family: "text", mode: "text", text: '{"city":"Paris","value":42}' }}
      />,
    );

    const body = document.querySelector(".artifact-text-preview pre");
    expect(body?.textContent).toContain('"city": "Paris"');
    // Syntax colors while not searching; find marks replace them during a search.
    expect(body?.querySelector(".hljs-attr")?.textContent).toBe('"city"');
    fireEvent.change(screen.getByRole("searchbox", { name: "Find in content" }), {
      target: { value: "Paris" },
    });
    expect(screen.getByText("Paris", { selector: "mark" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Wrap" }));
    expect(document.querySelector("pre.nowrap")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      '{\n  "city": "Paris",\n  "value": 42\n}',
    ));
  });

  it("renders Markdown as safe reading content and XLSX as bounded sheet tables", () => {
    const markdown = render(
      <ArtifactPreview
        fileId="art_md"
        filename="readme.md"
        mime="text/markdown"
        profile="markdown"
        preview={{ family: "text", mode: "text", text: "# Summary\n\n**Safe** <script>alert(1)</script>" }}
      />,
    );
    expect(screen.getByRole("heading", { name: "Summary" })).toBeInTheDocument();
    expect(document.querySelector("script")).not.toBeInTheDocument();
    markdown.unmount();

    render(
      <ArtifactPreview
        fileId="art_xlsx"
        filename="model.xlsx"
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        profile="xlsx"
        preview={{
          family: "spreadsheet",
          mode: "text",
          text: "--- sheet: Summary ---\nRevenue\t42\n\n--- sheet: Detail ---\nCity\tParis\tRegion",
        }}
      />,
    );
    expect(screen.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("cell", { name: "Revenue" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("row", { name: "Open row 1" }));
    expect(screen.getByRole("dialog", { name: /Row details/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Detail" }));
    expect(screen.getByRole("cell", { name: "Paris" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Region" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Row details/ })).not.toBeInTheDocument();
  });

  it("sorts the row sample, hides columns, and opens exact row details", () => {
    render(
      <ArtifactPreview
        fileId="art_table"
        filename="cities.csv"
        mime="text/csv"
        profile="table"
        preview={{
          family: "spreadsheet",
          mode: "rows",
          columns: ["city", "revenue"],
          rows: [
            { city: "Berlin", revenue: 9 },
            { city: "Paris", revenue: 2 },
          ],
          shown_rows: 2,
          total_rows: 10,
          truncated: true,
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "revenue" }));
    const bodyRows = screen.getAllByRole("row").slice(1);
    expect(within(bodyRows[0]!).getByRole("cell", { name: "Paris" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Columns"));
    fireEvent.click(screen.getByRole("checkbox", { name: "revenue" }));
    expect(screen.queryByRole("columnheader", { name: "revenue" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("row", { name: /row 1/i }));
    expect(screen.getByRole("dialog", { name: /Row details/ })).toHaveTextContent("Paris");
    expect(screen.getByRole("dialog", { name: /Row details/ })).toHaveTextContent("2");
  });

  it("traps full-view focus, closes on Escape, restores focus, and unlocks body scroll", async () => {
    render(
      <ArtifactPreview
        fileId="art_focus"
        filename="focus.txt"
        mime="text/plain"
        preview={{ family: "text", mode: "text", text: "focus body" }}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Full view" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "focus.txt" });
    expect(document.body.style.overflow).toBe("hidden");
    expect(within(dialog).getAllByRole("button", { name: "Close" })[0]).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "focus.txt" })).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(document.body.style.overflow).toBe("");
  });

  it("rejects oversized PDF preview before fetching or loading pdf.js", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const onOpenFailed = vi.fn();
    render(
      <ArtifactPreview
        fileId="art_big_pdf"
        filename="large.pdf"
        mime="application/pdf"
        profile="pdf"
        sizeBytes={26 * 1024 * 1024}
        preview={null}
        onOpenFailed={onOpenFailed}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Document too large");
    expect(onOpenFailed).toHaveBeenCalledWith("too_large");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps an immutable PDF cache across inline and full-view mounts", async () => {
    const transferredLengths: number[] = [];
    const pdfDocument = {
      numPages: 1,
      getPage: vi.fn().mockResolvedValue({
        getViewport: () => ({ width: 10, height: 10 }),
        render: () => ({ promise: Promise.resolve() }),
      }),
    };
    pdfGetDocument.mockImplementation(({ data }: { data: ArrayBuffer }) => {
      transferredLengths.push(data.byteLength);
      structuredClone(data, { transfer: [data] });
      return {
        promise: Promise.resolve(pdfDocument),
        destroy: vi.fn().mockResolvedValue(undefined),
      };
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      {} as CanvasRenderingContext2D,
    );
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Uint8Array([1, 2, 3, 4]), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ArtifactPreview
        fileId="art_pdf"
        filename="report.pdf"
        mime="application/pdf"
        profile="pdf"
        preview={null}
        downloadHref="/api/v1/files/art_pdf"
        contentHref="/api/v1/sessions/s_1/artifacts/art_pdf/content"
      />,
    );

    await waitFor(() => expect(pdfGetDocument).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Full view" }));
    await waitFor(() => expect(pdfGetDocument).toHaveBeenCalledTimes(2));
    expect(transferredLengths).toEqual([4, 4]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/sessions/s_1/artifacts/art_pdf/content",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(screen.getAllByRole("link", { name: "Download" })[0]).toHaveAttribute(
      "href",
      "/api/v1/files/art_pdf",
    );
  });

  it("offers Edit on a deck only to an owner Results viewer with the capability, and opens the saved version", async () => {
    pdfGetDocument.mockImplementation(() => ({ promise: new Promise(() => undefined), destroy: vi.fn().mockResolvedValue(undefined) }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new Uint8Array([1]), { status: 200 })));
    const onOpenVersion = vi.fn();
    const deck = {
      fileId: "art_deck",
      filename: "deck.pptx",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      profile: "pptx" as const,
      preview: null,
      resultRole: "deliverable" as const,
      downloadHref: "/api/v1/files/art_deck",
    };
    const { rerender } = render(
      <ArtifactPreview {...deck} capabilities={new Set(["download"])} presentationEdit={{ sessionId: "s_1", onOpenVersion }} />,
    );
    expect(screen.queryByRole("button", { name: "edit_presentation" })).not.toBeInTheDocument();

    rerender(<ArtifactPreview {...deck} capabilities={new Set(["download", "edit_presentation"])} presentationEdit={{ sessionId: "s_1", onOpenVersion }} />);
    fireEvent.click(screen.getByRole("button", { name: "edit_presentation" }));
    const editor = await screen.findByTestId("presentation-editor-stub");
    expect(editor).toHaveTextContent("art_deck");
    expect(screen.getByRole("dialog", { name: "deck.pptx" })).toContainElement(editor);

    // A Results refresh can drop the capability while the list is located again; the editor stays open.
    rerender(<ArtifactPreview {...deck} capabilities={new Set(["download"])} presentationEdit={undefined} />);
    expect(screen.getByTestId("presentation-editor-stub")).toBe(editor);

    // Editing started in the Results panel, so the saved version opens there, on the same page (design §13.1).
    fireEvent.click(within(editor).getByRole("button", { name: "saved" }));
    expect(onOpenVersion).toHaveBeenCalledWith("art_deck_v2", undefined);
    expect(readPresentationHandoff("art_deck_v2")).toMatchObject({ expanded: false, origin: "panel", page: 2 });
    expect(readPresentationHandoff("art_deck_v2")?.pdfData?.byteLength).toBe(3);
  });

  it("returns from deck editing to the panel or the full view it started from", async () => {
    pdfGetDocument.mockImplementation(() => ({ promise: new Promise(() => undefined), destroy: vi.fn().mockResolvedValue(undefined) }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new Uint8Array([1]), { status: 200 })));
    const deck = {
      fileId: "art_deck",
      filename: "deck.pptx",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      profile: "pptx" as const,
      preview: null,
      resultRole: "deliverable" as const,
      downloadHref: "/api/v1/files/art_deck",
      capabilities: new Set(["download", "edit_presentation"] as const),
      presentationEdit: { sessionId: "s_1", onOpenVersion: vi.fn() },
    };
    render(<ArtifactPreview {...deck} />);

    fireEvent.click(screen.getByRole("button", { name: "edit_presentation" }));
    fireEvent.click(within(await screen.findByTestId("presentation-editor-stub")).getByRole("button", { name: "leave" }));
    await waitFor(() => expect(screen.queryByTestId("presentation-editor-stub")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog", { name: "deck.pptx" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Full view" }));
    fireEvent.click(await within(screen.getByRole("dialog", { name: "deck.pptx" })).findByRole("button", { name: "edit_presentation" }));
    fireEvent.click(within(await screen.findByTestId("presentation-editor-stub")).getByRole("button", { name: "leave" }));
    await waitFor(() => expect(screen.queryByTestId("presentation-editor-stub")).not.toBeInTheDocument());
    expect(screen.getByRole("dialog", { name: "deck.pptx" })).toBeInTheDocument();
  });

  it("offers 引用这一页 under each page of a deck as a toggle that follows the Composer", async () => {
    const page = {
      getViewport: () => ({ width: 960, height: 540 }),
      render: () => ({ promise: Promise.resolve() }),
    };
    pdfGetDocument.mockImplementation(() => ({
      promise: Promise.resolve({ numPages: 3, getPage: () => Promise.resolve(page) }),
      destroy: vi.fn().mockResolvedValue(undefined),
    }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new Uint8Array([1]), { status: 200 })));
    const onToggleSlide = vi.fn().mockReturnValue("added");
    const host = (referencedSlides: Record<string, number[]>) => ({ sessionId: "s_1", onOpenVersion: vi.fn(), onToggleSlide, referencedSlides });
    const props = {
      fileId: "art_deck", filename: "deck.pptx", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      profile: "pptx" as const, preview: null, resultRole: "deliverable" as const, downloadHref: "/api/v1/files/art_deck", sizeBytes: 2048,
      capabilities: new Set(["download", "reference_in_chat"]) as never,
    };
    const view = render(<ArtifactPreview {...props} presentationEdit={host({})} />);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "reference_page_label" })).toHaveLength(3));
    fireEvent.click(screen.getAllByRole("button", { name: "reference_page_label" })[1]);
    expect(onToggleSlide).toHaveBeenCalledWith({
      fileId: "art_deck", filename: "deck.pptx", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      size: 2048, referenceKind: "result",
    }, 2, { stay: false });
    // The button confirms on itself, pressed, and its name starts with what it shows.
    const pressed = screen.getByRole("button", { name: "referenced_page_label" });
    expect(pressed).toHaveTextContent("referenced_page");
    expect(pressed).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("img", { name: "deck.pptx · 2" })).toBeInTheDocument();

    // The Composer decides: pages 1 and 3 in it read 「已引用」; a chip removed there turns them back.
    view.rerender(<ArtifactPreview {...props} presentationEdit={host({ art_deck: [1, 3] })} />);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "referenced_page_label" })).toHaveLength(2));
    expect(screen.getAllByRole("button", { name: "reference_page_label" })).toHaveLength(1);
    view.rerender(<ArtifactPreview {...props} presentationEdit={host({})} />);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "reference_page_label" })).toHaveLength(3));

    // Pressing a referenced page removes it.
    view.rerender(<ArtifactPreview {...props} presentationEdit={host({ art_deck: [3] })} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "referenced_page_label" })).toBeInTheDocument());
    onToggleSlide.mockReturnValueOnce("removed");
    fireEvent.click(screen.getByRole("button", { name: "referenced_page_label" }));
    expect(onToggleSlide).toHaveBeenLastCalledWith(expect.objectContaining({ fileId: "art_deck" }), 3, { stay: false });
    expect(screen.getAllByRole("button", { name: "reference_page_label" })).toHaveLength(3);
  });

  describe("Word pages (docx inline edit design §5.1)", () => {
    const docx = (onOpenFailed?: () => void) => (
      <ArtifactPreview
        fileId="art_docx"
        filename="review.docx"
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        profile="docx"
        preview={null}
        downloadHref="/api/v1/files/art_docx"
        onOpenFailed={onOpenFailed}
      />
    );
    const pagesUrl = "/api/v1/files/art_docx/representations/docx-pdf";

    function stubFetch(pages: () => Promise<Response>) {
      const fetchMock = vi.fn((url: string) => url === pagesUrl ? pages() : Promise.resolve(new Response(new Uint8Array([80, 75]), { status: 200 })));
      vi.stubGlobal("fetch", fetchMock);
      return fetchMock;
    }

    it("shows the reading view while the pages render, then the pages", async () => {
      setDocxInlineEditCapabilityForTest(true);
      pdfGetDocument.mockReturnValue({
        promise: Promise.resolve({ numPages: 1, getPage: vi.fn().mockResolvedValue({ getViewport: () => ({ width: 10, height: 10 }), render: () => ({ promise: Promise.resolve() }) }) }),
        destroy: vi.fn().mockResolvedValue(undefined),
      });
      vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D);
      let release: (response: Response) => void = () => undefined;
      const fetchMock = stubFetch(() => new Promise<Response>((resolve) => { release = resolve; }));
      render(docx());

      expect(await screen.findByTestId("artifact-docx-pages-pending")).toHaveTextContent("document_pages_rendering");
      expect(await screen.findByText("Reading view text")).toBeInTheDocument();
      release(new Response(new Uint8Array([37, 80, 68, 70]), { status: 200 }));
      expect(await screen.findByTestId("artifact-preview-pdf")).toBeInTheDocument();
      await waitFor(() => expect(pdfGetDocument).toHaveBeenCalledTimes(1));
      expect(screen.queryByTestId("artifact-docx-pages-pending")).toBeNull();
      expect(fetchMock.mock.calls.filter(([url]) => url === pagesUrl)).toHaveLength(1);
    });

    it("keeps the reading view when the pages cannot be rendered", async () => {
      setDocxInlineEditCapabilityForTest(true);
      const onOpenFailed = vi.fn();
      stubFetch(() => Promise.resolve(new Response(null, { status: 503 })));
      render(docx(onOpenFailed));

      expect(await screen.findByText("Reading view text")).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByTestId("artifact-docx-pages-pending")).toBeNull());
      expect(screen.getByText("Reading view text")).toBeInTheDocument();
      expect(pdfGetDocument).not.toHaveBeenCalled();
      expect(onOpenFailed).not.toHaveBeenCalled();
    });

    it("asks for no pages while the switch is off", async () => {
      const fetchMock = stubFetch(() => Promise.resolve(new Response(null, { status: 503 })));
      render(docx());

      expect(await screen.findByText("Reading view text")).toBeInTheDocument();
      expect(screen.queryByTestId("artifact-docx-pages-pending")).toBeNull();
      expect(fetchMock.mock.calls.some(([url]) => url === pagesUrl)).toBe(false);
    });
  });

  it("reports an unavailable PDF response without misclassifying it as corrupt", async () => {
    const onOpenFailed = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 403 })));

    render(
      <ArtifactPreview
        fileId="art_forbidden_pdf"
        filename="forbidden.pdf"
        mime="application/pdf"
        profile="pdf"
        preview={null}
        onOpenFailed={onOpenFailed}
      />,
    );

    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledWith("unavailable"));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(pdfGetDocument).not.toHaveBeenCalled();
  });
});


beforeEach(() => installHtmlPreviewWorker());
