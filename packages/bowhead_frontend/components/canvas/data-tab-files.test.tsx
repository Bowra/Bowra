import "@testing-library/jest-dom/vitest";

import { headersExactly } from "@/__tests__/request-headers";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DataTabFiles,
  type ArtifactItem,
  useArtifactItems,
} from "./data-tab-files";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
});

function detailsResponse(id: string) {
  return new Response(JSON.stringify({ family: "text", representation: "details", revision: "metadata-1", details: { id } }), { status: 200 });
}

describe("DataTabFiles runtime path prefix", () => {
  it("loads artifacts through the current public entry prefix", async () => {
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: null,
      teamId: null,
      baseUrl: "https://public.example/bowhead",
      basePath: "/bowhead",
      matchSource: "trusted_proxy_bootstrap",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<DataTabFiles sessionId="s_1" />);

    await waitFor(() => {
      // appFetch adds the public-base header as a Headers instance (d59ce0c12).
      expect(fetchMock).toHaveBeenCalledWith(
        "/bowhead/api/v1/sessions/s_1/artifacts",
        { headers: headersExactly({}, `${window.location.origin}/bowhead`) },
      );
    });
  });

  it("shows a regenerable legacy export warning and calls the current controller", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{
          id: "art_1", filename: "report.pdf", mime: "application/pdf", size: 10,
          source: "canvas_export", created_at: "2026-08-01T00:00:00Z",
          preview_available: true, kind: "deliverable",
          trust: "export_copy_legacy_regenerable",
        }],
      }), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ family: "pdf", mode: "text" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);
    const regenerate = vi.fn();
    const view = render(<DataTabFiles sessionId="s_1" onRegenerateExport={regenerate} />);
    await waitFor(() => expect(view.getByText("report.pdf")).toBeTruthy());
    fireEvent.click(view.getByText("report.pdf"));
    expect(view.getByText("export_copy_legacy_regenerable")).toBeTruthy();
    fireEvent.click(view.getByText("export_regenerate"));
    expect(regenerate).toHaveBeenCalledWith("pdf");
  });

  it("opens the focused object directly and keeps preview, raw, and details on its exact ID", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/representations/details")) return Promise.resolve(detailsResponse(url.split("/").at(-3)!));
      if (url.endsWith("/api/v1/sessions/s_1/artifacts")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [{
                id: "art_upload",
                filename: "input.csv",
                mime: "text/csv",
                size: 42,
                source: "upload",
                created_at: "2026-07-30T00:00:00Z",
                preview_available: true,
                kind: "user_upload",
                origin: "chat_attachment",
                trust: "trusted",
              }],
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/api/v1/files/art_upload/representations/raw")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              family: "text", representation: "raw", revision: "raw-1",
              mode: "text",
              text: "raw upload",
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/api/v1/files/art_upload/representations/preview")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              family: "text", representation: "preview", revision: "preview-1",
              mode: "text",
              text: "preview upload",
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DataTabFiles
        sessionId="s_1"
        focusArtifactId="art_upload"
        viewerOnly
      />,
    );

    expect(await screen.findByText("preview upload")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "mode_raw" }));
    expect(await screen.findByText("raw upload")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "mode_details" }));
    expect(await screen.findByText("art_upload")).toBeInTheDocument();
    expect(screen.getByText("chat_attachment")).toBeInTheDocument();
    expect(screen.getByText("source_upload")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "download" })).toHaveAttribute(
      "href",
      "/api/v1/files/art_upload",
    );
    expect(screen.queryByRole("button", { name: "close" })).not.toBeInTheDocument();
    const requestedUrls = fetchMock.mock.calls.map(([input]) => String(input));
    expect(requestedUrls).toContain(
      "/api/v1/files/art_upload/representations/preview",
    );
    expect(requestedUrls).toContain(
      "/api/v1/files/art_upload/representations/raw",
    );
    expect(
      requestedUrls.every((url) => !url.includes("another_artifact")),
    ).toBe(true);
  });

  it("reuses the Navigator artifact snapshot in viewer-only mode", async () => {
    const artifact: ArtifactItem = {
      id: "art_shared_snapshot",
      filename: "shared.csv",
      mime: "text/csv",
      size: 24,
      source: "python_sandbox",
      created_at: "2026-07-30T00:00:00Z",
      preview_available: true,
      kind: "sandbox_output",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
      if (url.endsWith("/representations/details")) return Promise.resolve(detailsResponse(url.split("/").at(-3)!));
        if (url.includes("/preview")) {
          return new Response(JSON.stringify({
            family: "text", representation: "preview", revision: "preview-1",
            mode: "text",
            text: "snapshot preview",
          }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        throw new Error(`unexpected list request: ${url}`);
      }),
    );

    render(
      <DataTabFiles
        sessionId="s_snapshot"
        focusArtifactId={artifact.id}
        viewerOnly
        artifactItems={[artifact]}
      />,
    );

    await waitFor(() =>
      expect(screen.getByText("snapshot preview")).toBeInTheDocument(),
    );
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(String(vi.mocked(fetch).mock.calls[1]?.[0])).toContain(
      `/api/v1/files/${artifact.id}/representations/preview`,
    );
  });

  it("offers exact-list retry when a deep focused artifact is not in the catalog", () => {
    const retry = vi.fn();
    render(
      <DataTabFiles
        sessionId="s_deep"
        focusArtifactId="art_101"
        viewerOnly
        artifactItems={[]}
        onArtifactItemsRetry={retry}
      />,
    );

    expect(screen.getByText("focused_missing")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "retry" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("maps representation failures without rendering server error text", async () => {
    const artifact: ArtifactItem = {
      id: "art_corrupt",
      filename: "broken.pdf",
      mime: "application/pdf",
      size: 24,
      source: "python_sandbox",
      created_at: "2026-07-30T00:00:00Z",
      preview_available: true,
      kind: "sandbox_output",
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(String(input).endsWith("/details")
      ? detailsResponse(artifact.id)
      : new Response(JSON.stringify({ detail: { error: { code: "artifact_representation_corrupt", message: "secret storage path /private/artifacts/broken.pdf" } } }), { status: 422 })));
    vi.stubGlobal("fetch", fetchMock);
    const onOpenFailed = vi.fn();

    render(
      <DataTabFiles
        sessionId="s_corrupt"
        focusArtifactId={artifact.id}
        viewerOnly
        artifactItems={[artifact]}
        onOpenFailed={onOpenFailed}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("error_corrupt");
    expect(screen.queryByText(/secret storage path/)).not.toBeInTheDocument();
    expect(onOpenFailed).toHaveBeenCalledWith("corrupt");
    fireEvent.click(screen.getByRole("button", { name: "retry" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
  });

  it("treats the viewer mode as controlled route state when provided", async () => {
    const onViewerModeChange = vi.fn();
    const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(String(input).endsWith("/details")
      ? detailsResponse("art_controlled") : new Response(
        JSON.stringify({
          items: [{
            id: "art_controlled",
            filename: "controlled.csv",
            mime: "text/csv",
            size: 42,
            source: "agent",
            created_at: "2026-07-30T00:00:00Z",
            preview_available: true,
          }],
        }),
        { status: 200 },
      )));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DataTabFiles
        sessionId="s_controlled"
        focusArtifactId="art_controlled"
        viewerOnly
        viewerMode="details"
        onViewerModeChange={onViewerModeChange}
      />,
    );

    expect(await screen.findByText("art_controlled")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("tab", { name: "mode_raw" }));
    expect(onViewerModeChange).toHaveBeenCalledWith("raw");
    expect(screen.getByText("art_controlled")).toBeInTheDocument();
  });

  it("never renders the previous artifact body after focus moves to another ID", async () => {
    let resolveSecondPreview!: (response: Response) => void;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/representations/details")) return Promise.resolve(detailsResponse(url.split("/").at(-3)!));
      if (url.endsWith("/api/v1/sessions/s_1/artifacts")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [
                {
                  id: "art_a",
                  filename: "a.txt",
                  mime: "text/plain",
                  size: 1,
                  source: "agent",
                  created_at: "2026-07-30T00:00:00Z",
                  preview_available: true,
                },
                {
                  id: "art_b",
                  filename: "b.txt",
                  mime: "text/plain",
                  size: 1,
                  source: "agent",
                  created_at: "2026-07-30T00:00:01Z",
                  preview_available: true,
                },
              ],
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/api/v1/files/art_a/representations/preview")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              family: "text", representation: "preview", revision: "preview-1",
              mode: "text",
              text: "artifact A private body",
            }),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/api/v1/files/art_b/representations/preview")) {
        return new Promise<Response>((resolve) => {
          resolveSecondPreview = resolve;
        });
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(
      <DataTabFiles
        sessionId="s_1"
        focusArtifactId="art_a"
        viewerOnly
      />,
    );
    expect(
      await screen.findByText("artifact A private body"),
    ).toBeInTheDocument();

    rerender(
      <DataTabFiles
        sessionId="s_1"
        focusArtifactId="art_b"
        viewerOnly
      />,
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/files/art_b/representations/preview", expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(
      screen.queryByText("artifact A private body"),
    ).not.toBeInTheDocument();

    await act(async () => {
      resolveSecondPreview(
        new Response(
          JSON.stringify({
            family: "text", representation: "preview", revision: "preview-1",
            mode: "text",
            text: "artifact B body",
          }),
          { status: 200 },
        ),
      );
    });
    expect(await screen.findByText("artifact B body")).toBeInTheDocument();
  });

  it("uses the exact video artifact stream and removes meaningless raw mode", async () => {
    const onViewerModeChange = vi.fn();
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/representations/details")) return Promise.resolve(detailsResponse(url.split("/").at(-3)!));
      if (url.endsWith("/api/v1/sessions/s_video/artifacts")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [{
                id: "art_video",
                filename: "result.mp4",
                mime: "video/mp4",
                size: 2048,
                source: "python_sandbox",
                created_at: "2026-07-30T00:00:00Z",
                preview_available: true,
                kind: "deliverable",
                origin: "generated",
                trust: "trusted",
              }],
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DataTabFiles
        sessionId="s_video"
        focusArtifactId="art_video"
        viewerOnly
        viewerMode="raw"
        onViewerModeChange={onViewerModeChange}
      />,
    );

    await waitFor(() =>
      expect(onViewerModeChange).toHaveBeenCalledWith("preview", "replace"),
    );
    await waitFor(() =>
      expect(document.querySelector("video")).toBeInTheDocument(),
    );
    expect(document.querySelector("video")).toHaveAttribute(
      "src",
      "/api/v1/files/art_video?disposition=inline",
    );
    expect(
      screen.queryByRole("tab", { name: "mode_raw" }),
    ).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("derives image and unsupported modes from the actual MIME profile", async () => {
    const onViewerModeChange = vi.fn();
    const image: ArtifactItem = {
      id: "art_image",
      filename: "result.png",
      mime: "image/png",
      size: 128,
      source: "agent",
      created_at: "2026-07-30T00:00:00Z",
      preview_available: true,
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(detailsResponse(String(input).split("/").at(-3)!)));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(
      <DataTabFiles
        sessionId="s_image"
        focusArtifactId={image.id}
        viewerOnly
        viewerMode="raw"
        onViewerModeChange={onViewerModeChange}
        artifactItems={[image]}
      />,
    );

    await waitFor(() => expect(onViewerModeChange).toHaveBeenCalledWith("preview", "replace"));
    expect(screen.queryByRole("tab", { name: "mode_raw" })).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "result.png" })).toHaveAttribute(
      "src",
      "/api/v1/files/art_image?disposition=inline",
    );
    expect(fetchMock.mock.calls.every(([input]) => String(input).endsWith("/representations/details"))).toBe(true);

    const unsupported: ArtifactItem = {
      ...image,
      id: "art_zip",
      filename: "bundle.zip",
      mime: "application/zip",
    };
    onViewerModeChange.mockClear();
    view.rerender(
      <DataTabFiles
        sessionId="s_image"
        focusArtifactId={unsupported.id}
        viewerOnly
        viewerMode="preview"
        onViewerModeChange={onViewerModeChange}
        artifactItems={[unsupported]}
      />,
    );
    await waitFor(() => expect(onViewerModeChange).toHaveBeenCalledWith("details", "replace"));
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(screen.getByRole("tab", { name: "mode_details" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(await screen.findByText("art_zip")).toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([input]) => String(input).endsWith("/representations/details"))).toBe(true);
  });

  it("renders the bounded PPTX slide outline without exposing raw mode", async () => {
    const artifact: ArtifactItem = {
      id: "art_deck",
      filename: "board.pptx",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      size: 2048,
      source: "agent",
      created_at: "2026-07-30T00:00:00Z",
      preview_available: true,
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(String(input).endsWith("/details")
      ? detailsResponse(artifact.id) : new Response(JSON.stringify({
      family: "presentation",
      representation: "preview", revision: "pptx-1",
      mode: "text",
      text: "--- slide.1 ---\nExecutive summary\n\n--- slide.2 ---\nNext step",
    }), { status: 200, headers: { "content-type": "application/json" } })));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DataTabFiles
        sessionId="s_deck"
        focusArtifactId={artifact.id}
        viewerOnly
        artifactItems={[artifact]}
      />,
    );

    expect(await screen.findByText("Executive summary")).toBeInTheDocument();
    expect(screen.getByText("Next step")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "mode_raw" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/files/art_deck/representations/preview", expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("loads safe HTML by default through the session content proxy while keeping the download URL", async () => {
    const artifact: ArtifactItem = {
      id: "art_html_on",
      filename: "result.html",
      mime: "text/html",
      size: 128,
      source: "python_sandbox",
      created_at: "2026-08-08T00:00:00Z",
      preview_available: true,
      kind: "deliverable",
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(String(input).endsWith("/details")
      ? detailsResponse(artifact.id) : new Response(
      "<!doctype html><h1>Safe result</h1>",
      { status: 200, headers: { "content-type": "text/html" } },
    )));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DataTabFiles
        sessionId="s_html"
        focusArtifactId={artifact.id}
        viewerOnly
        artifactItems={[artifact]}
      />,
    );

    await waitFor(() => expect(document.querySelector("iframe[title='result.html']")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/sessions/s_html/artifacts/art_html_on/content",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(screen.getAllByRole("link", { name: "download" })[0]).toHaveAttribute(
      "href",
      "/api/v1/files/art_html_on",
    );
  });

  it("does not expose the previous session list while a new session is loading", async () => {
    let resolveSecond!: (response: Response) => void;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/sessions/s_1/artifacts")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [{
                id: "private_from_s1",
                filename: "s1.csv",
                mime: "text/csv",
                size: 1,
                source: "upload",
                created_at: "2026-07-30T00:00:00Z",
                preview_available: true,
              }],
            }),
            { status: 200 },
          ),
        );
      }
      return new Promise<Response>((resolve) => {
        resolveSecond = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(
      ({ sessionId }) => useArtifactItems(sessionId, "session"),
      { initialProps: { sessionId: "s_1" } },
    );
    await waitFor(() =>
      expect(result.current.items?.[0]?.id).toBe("private_from_s1"),
    );

    rerender({ sessionId: "s_2" });
    expect(result.current.items).toBeNull();
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/sessions/s_2/artifacts",
        { headers: headersExactly({}) },
      ),
    );
    await act(async () => {
      resolveSecond(
        new Response(JSON.stringify({ items: [] }), { status: 200 }),
      );
    });
    await waitFor(() => expect(result.current.items).toEqual([]));
  });

  it("clears the previous session list when the new session request fails", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/sessions/s_1/artifacts")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              items: [{
                id: "private_from_s1",
                filename: "s1.csv",
                mime: "text/csv",
                size: 1,
                source: "upload",
                created_at: "2026-07-30T00:00:00Z",
                preview_available: true,
              }],
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 500 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(
      ({ sessionId }) => useArtifactItems(sessionId, "session"),
      { initialProps: { sessionId: "s_1" } },
    );
    await waitFor(() =>
      expect(result.current.items?.[0]?.id).toBe("private_from_s1"),
    );

    rerender({ sessionId: "s_2" });
    expect(result.current.items).toBeNull();
    await waitFor(() =>
      // A 5xx response is an unknown server failure, not a network one (ed6520128).
      expect(result.current.error).toBe("unknown"),
    );
    expect(result.current.items).toEqual([]);
    act(() => result.current.retry());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("awaits a fresh terminal snapshot instead of returning cached items", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{
          id: "art_before",
          filename: "before.csv",
          mime: "text/csv",
          size: 1,
          source: "python_sandbox",
          created_at: "2026-08-07T00:00:00Z",
          preview_available: true,
        }],
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{
          id: "art_after",
          filename: "after.pdf",
          mime: "application/pdf",
          size: 2,
          source: "canvas_export",
          created_at: "2026-08-07T00:01:00Z",
          preview_available: true,
          kind: "deliverable",
          created_by_turn: "t_1",
        }],
      }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useArtifactItems("s_1", "session"),
    );
    await waitFor(() =>
      expect(result.current.items?.[0]?.id).toBe("art_before"),
    );

    let snapshot: ArtifactItem[] = [];
    await act(async () => {
      snapshot = await result.current.refresh("turn-terminal");
    });

    expect(snapshot.map((item) => item.id)).toEqual(["art_after"]);
    expect(result.current.items?.[0]?.id).toBe("art_after");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
