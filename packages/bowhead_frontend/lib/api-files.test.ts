import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listFiles, promoteFileToKb, removeFileFromKb } from "./api";

beforeEach(() => {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("listFiles", () => {
  it("derives finite aggregates when an older response omits them", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            items: [
              {
                file_id: "art_1",
                filename: "report.csv",
                mime: "text/csv",
                mime_family: "text",
                size: 128,
                source: "upload",
                origin: "upload",
                created_at: "2026-07-23T00:00:00Z",
                last_used_at: null,
                deleted_at: null,
                category: null,
                category_source: null,
                usage_count: 0,
                url: "/api/v1/files/art_1",
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );

    await expect(listFiles()).resolves.toMatchObject({
      total: 1,
      size_total_bytes: 128,
    });
  });
});

describe("promoteFileToKb", () => {
  it("sends the active space context when promoting to a space", async () => {
    window.localStorage.setItem("bowhead.space_id", "ws_test");
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ file_id: "kbf_1" }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(promoteFileToKb("art_1", "space")).resolves.toEqual({
      file_id: "kbf_1",
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/v1/kb/files/from-artifact");
    expect(init.method).toBe("POST");
    // appFetch normalizes headers into a Headers instance (d59ce0c12).
    const headers = new Headers(init.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-Space-Id")).toBe("ws_test");
    expect(headers.get("X-Workspace-Id")).toBe("ws_test");
    expect(JSON.parse(String(init.body))).toEqual({
      artifact_id: "art_1",
      scope: "space",
    });
  });

  it.each([
    [400, "space_required"],
    [403, "forbidden"],
    [422, "unsupported"],
    [500, "promotion_failed"],
  ])("maps HTTP %s to stable UI error %s", async (status, expected) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: { reason: "mime_not_parsable:application/json" },
          }),
          { status },
        ),
      ),
    );

    await expect(promoteFileToKb("art_1", "personal")).rejects.toThrow(
      expected,
    );
  });

  it("reports an unconfigured knowledge base from the server error code", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ detail: { error: { code: "kb_embedding_not_configured" } } }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    )));

    await expect(promoteFileToKb("art_1", "space")).rejects.toThrow("kb_embedding_not_configured");
  });
});

describe("removeFileFromKb", () => {
  it("sends the active space context so shared files can be removed", async () => {
    window.localStorage.setItem("bowhead.space_id", "ws_test");
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(removeFileFromKb("art_1")).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/v1/kb/files/by-artifact/art_1");
    expect(init.method).toBe("DELETE");
    const headers = new Headers(init.headers);
    expect(headers.get("X-Space-Id")).toBe("ws_test");
    expect(headers.get("X-Workspace-Id")).toBe("ws_test");
  });

  it.each([
    [400, "space_required"],
    [403, "forbidden"],
    [404, "removal_failed"],
    [500, "removal_failed"],
  ])("maps HTTP %s to stable UI error %s", async (status, expected) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status })),
    );

    await expect(removeFileFromKb("art_1")).rejects.toThrow(expected);
  });
});
