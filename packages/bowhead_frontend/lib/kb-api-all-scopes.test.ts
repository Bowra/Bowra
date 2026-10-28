import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchAllScopeKbFiles, loadKbFileScopePreference, parseKbListScope, saveKbFileScopePreference } from "./kb-api";

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedUrl(fetchMock: ReturnType<typeof stubFetch>): URL {
  return new URL(String(fetchMock.mock.calls[0][0]), "http://app.test");
}

describe("fetchAllScopeKbFiles", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("requests scope=all with the cursor and filters, and returns next_cursor", async () => {
    const fetchMock = stubFetch({ files: [{ file_id: "kbf_1", scope: "team" }], total: 3, has_more: true, next_cursor: "c2" });

    const page = await fetchAllScopeKbFiles({ cursor: "c1", limit: 1, q: "plan", order_by: "filename", order_dir: "asc" });

    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/kb/files");
    expect(Object.fromEntries(url.searchParams)).toEqual({ scope: "all", cursor: "c1", limit: "1", q: "plan", order_by: "filename", order_dir: "asc" });
    expect(page.next_cursor).toBe("c2");
    expect(page.files[0].scope).toBe("team");
  });
});

describe("kb file scope preference", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("accepts only list scopes", () => {
    expect(parseKbListScope("all")).toBe("all");
    expect(parseKbListScope("space")).toBe("space");
    expect(parseKbListScope("workspace")).toBeNull();
    expect(parseKbListScope(undefined)).toBeNull();
  });

  it("reads the saved scope from user settings and ignores unknown values", async () => {
    stubFetch({ settings: { kb_file_scope: "team" } });
    await expect(loadKbFileScopePreference()).resolves.toBe("team");

    vi.unstubAllGlobals();
    stubFetch({ settings: { kb_file_scope: "bogus" } });
    await expect(loadKbFileScopePreference()).resolves.toBeNull();
  });

  it("saves the scope under kb_file_scope", async () => {
    const fetchMock = stubFetch({ value: "all" });

    await saveKbFileScopePreference("all");

    expect(requestedUrl(fetchMock).pathname).toBe("/api/v1/user-settings/kb_file_scope");
    const init = fetchMock.mock.calls[0][1];
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({ value: "all" });
  });
});
