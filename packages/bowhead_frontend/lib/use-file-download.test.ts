/**
 * chat-file-download #2 — useFileDownload state machine, extracted from
 * ChatInlineFile's original inline implementation so it can be shared with
 * AttachmentChip (sent-message + pinned-attachment download chips).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useFileDownload } from "./use-file-download";

function streamResponse(body: string, init: ResponseInit = {}): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-length": String(encoder.encode(body).length) },
    ...init,
  });
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  // jsdom doesn't implement these — stub so saveBlobOrUrl's native-anchor path
  // (and the in-memory blob path) don't throw.
  vi.stubGlobal("URL", Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:mock"),
    revokeObjectURL: vi.fn(),
  }));
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

describe("useFileDownload", () => {
  it("starts idle", () => {
    const { result } = renderHook(() =>
      useFileDownload({ downloadUrl: "/api/v1/files/art_1", filename: "report.xlsx", sizeBytes: 1024 }),
    );
    expect(result.current.status).toBe("idle");
    expect(result.current.pct).toBe(0);
  });

  it("successful fetch → downloading → done, then auto-reverts to idle", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock().mockResolvedValueOnce(streamResponse("hello world"));

    const { result } = renderHook(() =>
      useFileDownload({ downloadUrl: "/api/v1/files/art_1", filename: "report.xlsx", sizeBytes: 11 }),
    );

    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("done"));

    act(() => vi.advanceTimersByTime(1800));
    expect(result.current.status).toBe("idle");
    expect(result.current.pct).toBe(0);
    vi.useRealTimers();
  });

  it("real HTTP error (404) → error state, no false success", async () => {
    fetchMock().mockResolvedValueOnce(new Response(null, { status: 404 }));

    const { result } = renderHook(() =>
      useFileDownload({ downloadUrl: "/api/v1/files/art_missing", filename: "gone.xlsx" }),
    );

    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("error"));
  });

  it("retains an API error code and clears it before retrying", async () => {
    fetchMock().mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: "file_unavailable", message: "Private diagnostics" } }), { status: 410 }));
    fetchMock().mockResolvedValueOnce(streamResponse("source bytes"));
    const { result } = renderHook(() => useFileDownload({ downloadUrl: "/api/v1/files/art_1/download", filename: "report.pptx" }));
    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.errorCode).toBe("file_unavailable");
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
    act(() => result.current.download());
    expect(result.current.errorCode).toBeUndefined();
    await waitFor(() => expect(result.current.status).toBe("done"));
  });

  it("refreshes an expired same-origin session before downloading", async () => {
    fetchMock()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })))
      .mockResolvedValueOnce(streamResponse("source bytes"));
    const { result } = renderHook(() => useFileDownload({ downloadUrl: "/api/v1/files/art_1/download", filename: "report.pptx" }));
    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("done"));
    expect(fetchMock().mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/files/art_1/download", "/api/v1/auth/refresh", "/api/v1/files/art_1/download",
    ]);
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
  });

  it("does not refresh application credentials for an external download rejection", async () => {
    fetchMock().mockResolvedValueOnce(new Response(null, { status: 401 }));
    const { result } = renderHook(() => useFileDownload({ downloadUrl: "https://cdn.example.com/report", filename: "report.pptx" }));
    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("aborts on unmount without starting a late native download", async () => {
    let reject!: (error: Error) => void;
    fetchMock().mockImplementationOnce(() => new Promise<Response>((_resolve, fail) => { reject = fail; }));
    const { result, unmount } = renderHook(() => useFileDownload({ downloadUrl: "/api/v1/files/art_1/download", filename: "report.pptx" }));
    act(() => result.current.download());
    const signal = fetchMock().mock.calls[0][1].signal as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { reject(new Error("Request aborted")); });
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("keeps the blob URL alive for the browser handoff", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock().mockResolvedValueOnce(streamResponse("source bytes"));
    const { result } = renderHook(() => useFileDownload({ downloadUrl: "/api/v1/files/art_1/download", filename: "report.pptx" }));
    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("done"));
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1000));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock");
  });

  it("network/CORS fetch rejection falls back to native anchor download (done, not error)", async () => {
    fetchMock().mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const { result } = renderHook(() =>
      useFileDownload({ downloadUrl: "https://cdn.example.com/art_1", filename: "report.xlsx" }),
    );

    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("done"));
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
  });

  it("large file skips the progress fetch entirely (native download, no fetch call)", async () => {
    const { result } = renderHook(() =>
      useFileDownload({
        downloadUrl: "/api/v1/files/art_big",
        filename: "huge.xlsx",
        sizeBytes: 20 * 1024 * 1024, // > 16MB PROGRESS_FETCH_MAX_BYTES
      }),
    );

    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("done"));
    expect(fetchMock()).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
  });

  it("ignores a second download() call while one is already in flight", async () => {
    let resolveFetch!: (v: Response) => void;
    fetchMock().mockImplementationOnce(
      () => new Promise<Response>((resolve) => { resolveFetch = resolve; }),
    );

    const { result } = renderHook(() =>
      useFileDownload({ downloadUrl: "/api/v1/files/art_1", filename: "report.xlsx", sizeBytes: 11 }),
    );

    act(() => result.current.download());
    await waitFor(() => expect(result.current.status).toBe("downloading"));
    act(() => result.current.download()); // no-op — busy guard
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => resolveFetch(streamResponse("hello world")));
    await waitFor(() => expect(result.current.status).toBe("done"));
  });
});
